"""
Tailscale 心率数据服务 (WS推送 + HTTP轮询)
- aiohttp 服务挂载到 qasync 事件循环, 零新线程
- WS  /ws            : 客户端连接即推当前快照, 此后每次心率更新实时广播(主通道)
- GET /api/heartrate : HTTP轮询接口, 返回最新状态JSON(兜底通道)
- GET /              : HTML演示页, 手机浏览器可直接打开查看实时心率
- 默认绑定Tailscale IP(100.64.0.0/10网段), 不暴露局域网
"""
import asyncio
import ipaddress
import json
import socket
import time
from pathlib import Path

from system_utils import logger

try:
    from aiohttp import web
    AIOHTTP_OK = True
except ImportError:
    web = None
    AIOHTTP_OK = False

# Tailscale CGNAT 网段
TAILSCALE_NET = ipaddress.ip_network('100.64.0.0/10')


def detect_tailscale_ip():
    """探测本机Tailscale IP(100.64.0.0/10网段), 找不到返回None"""
    ips = []
    # 方法1: getaddrinfo列举本机地址
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    # 方法2: UDP connect路由探测(不实际发包, 仅选路), 能命中Tailscale虚拟网卡IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('100.100.111.1', 9))
            ip = s.getsockname()[0]
            if ip not in ips:
                ips.append(ip)
        finally:
            s.close()
    except Exception:
        pass
    for ip in ips:
        try:
            if ipaddress.ip_address(ip) in TAILSCALE_NET:
                return ip
        except Exception:
            continue
    return None


def validate_bind_address(address: str) -> bool:
    """校验用户手填的绑定地址是否为合法IP"""
    try:
        ipaddress.ip_address(address)
        return True
    except Exception:
        return False


class WebPushServer:
    """心率数据 WS/HTTP 广播服务(aiohttp, 挂qasync事件循环)"""

    def __init__(self, host, port, device_name=""):
        if not AIOHTTP_OK:
            raise RuntimeError("aiohttp 未安装")
        self.host = host
        self.port = int(port)
        self.device_name = device_name or ""
        self.running = False
        self.error = None
        self.runner = None
        self.site = None
        self.clients = set()  # 当前连接的WS客户端
        self._alarm_task = None  # 报警自动复位任务(重复报警时取消旧的, 防提前复位新报警)
        self.stop_done = True    # 停止协程完成标志(退出清理等待用)
        # 最新状态快照(HTTP轮询/WS新连接共用)
        # info: 附加状态文本(如智能重连进度), 空串=无附加状态
        # alarm: 远程报警标志(true=接收端循环响铃, seconds后自动复位; 接收端收到false立即停铃)
        # clip_url: 报警剪辑流式播放地址(剪辑成型后推送, 报警开始时清空防播旧片段)
        # clips: 全量相机剪辑列表[{"cam","url"}](方案1, 接收端视频面板tab切换; 兼容旧版仅用clip_url)
        # hr_source: 心率数据源状态(中继中枢推送, 接收端通知栏显示当前来源房间/PC)
        # alarm_cam/alarm_room: 报警视频联动(报警房间绑定的摄像头名与房间名, 空=未绑定用默认)
        # alarm_live: 报警房间摄像头HLS实时流m3u8地址(空=未就绪/不支持, 接收端回退快照轮询)
        self._state = {
            "heart_rate": 0,
            "timestamp": "",
            "status": "disconnected",
            "device": self.device_name,
            "info": "",
            "alarm": False,
            "clip_url": "",
            "clips": [],
            "hr_source": {},
            "alarm_cam": "",
            "alarm_room": "",
            "alarm_live": "",
        }
        # 期3 报警视频联动: 摄像头管理器引用(快照接口用) + 报警起止回调(启停快照流)
        self.camera_mgr = None
        self.on_alarm_start = None
        self.on_alarm_end = None

    def set_camera_manager(self, mgr):
        """注入CameraManager(/camera/snapshot 提供默认摄像头画面)"""
        self.camera_mgr = mgr

    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def push_clip(self, clip_url: str, clips=None):
        """报警剪辑成型: clip_url并入状态快照并广播(接收端显示回放按钮+相机tab)
        clips=None保持现有列表不变, 传list则整体覆盖(方案1全量剪辑)"""
        new_state = dict(self._state, clip_url=clip_url or "")
        if clips is not None:
            new_state["clips"] = list(clips)
        self._state = new_state
        self._schedule_broadcast()

    def push_source(self, status: dict):
        """中继数据源状态变化: hr_source并入状态快照并广播(接收端通知栏显示数据源)
        由MainWindow经Qt信号中转到GUI线程后调用(hub线程不可直接进qasync)"""
        self._state = dict(self._state, hr_source=status or {})
        self._schedule_broadcast()

    def set_live_url(self, url: str):
        """报警HLS实时流地址就绪(或报警结束清空): 并入状态快照并广播
        由MainWindow经Qt信号中转到GUI线程后调用"""
        if url == self._state.get("alarm_live"):
            return
        self._state = dict(self._state, alarm_live=url or "")
        self._schedule_broadcast()

    # ---------- 生命周期 ----------
    def start(self):
        """调度到事件循环启动服务(可在Qt槽中直接调用)"""
        asyncio.ensure_future(self._start())

    def stop(self):
        """调度到事件循环停止服务"""
        self.stop_done = False
        asyncio.ensure_future(self._stop())

    async def _start(self):
        if self.running:
            return
        app = web.Application()
        app.router.add_get('/', self.handle_index)
        app.router.add_get('/api/heartrate', self.handle_api)
        app.router.add_get('/ws', self.handle_ws)
        # 期3 报警视频联动: 快照轮询(报警期间2fps) + 剪辑流式播放(HTTP Range)
        app.router.add_get('/camera/snapshot', self.handle_snapshot)
        app.router.add_get('/camera/clip', self.handle_clip)
        # 期5 HLS实时流: m3u8播放列表 + 分片(报警期间, 接收端AVPlayer直接播)
        app.router.add_get('/camera/live/index.m3u8', self.handle_live_playlist)
        app.router.add_get('/camera/live/segment.ts', self.handle_live_segment)
        self.runner = web.AppRunner(app, access_log=None)  # 轮询频率高, 关闭访问日志刷屏
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        try:
            await self.site.start()
        except Exception as e:
            self.error = str(e)
            self.running = False
            self.runner = None
            logger.error(f"Tailscale数据服务启动失败({self.host}:{self.port}): {e}")
            return False
        self.running = True
        logger.info(f"Tailscale数据服务已启动: http://{self.host}:{self.port}")
        return True

    async def _stop(self):
        self.running = False
        for ws in list(self.clients):
            try:
                await ws.close()
            except Exception:
                pass
        self.clients.clear()
        if self.runner:
            try:
                await self.runner.cleanup()
            except Exception:
                pass
        self.runner = None
        self.site = None
        self.stop_done = True
        logger.info("Tailscale数据服务已停止")

    # ---------- 数据入口(Qt主线程直接调用, qasync下即loop线程) ----------
    def update(self, heart_rate, timestamp=None, status="connected", info=None):
        """心率/状态更新: 刷新快照并广播给所有WS客户端
        info=None 表示保持原info不变, 传入字符串则覆盖(空串=清除)"""
        st = dict(self._state)
        st["heart_rate"] = int(heart_rate or 0)
        st["timestamp"] = timestamp or time.strftime("%Y-%m-%d %H:%M:%S")
        st["status"] = status or "connected"
        if info is not None:
            st["info"] = info
        self._state = st
        self._schedule_broadcast()

    def update_info(self, info):
        """仅更新附加状态文本(如智能重连进度)并广播"""
        if info == self._state.get("info"):
            return  # 未变化不重复广播
        self._state = dict(self._state, info=info or "")
        self._schedule_broadcast()

    def trigger_alarm(self, seconds=10, cam_name="", room=""):
        """远程报警: 快照alarm置true并广播(接收端开始响铃), seconds后自动复位false(接收端停铃)
        cam_name/room: 报警视频联动(当前持有手环节点=房间→绑定摄像头), 空串=未绑定接收端用默认摄像头
        Qt主线程调用(qasync下即loop线程), ensure_future可直接调度"""
        # 报警开始: 清空上一次的clip_url/clips/alarm_live(防止接收端误播旧流/旧片段) + 启动快照流回调
        self._state = dict(self._state, alarm=True, clip_url="", clips=[],
                           alarm_cam=cam_name or "", alarm_room=room or "",
                           alarm_live="")
        self._schedule_broadcast()
        self._fire(self.on_alarm_start)
        try:
            # 重复报警: 先取消上一次的自动复位任务再起新的, 防旧任务提前把新报警复位为false
            if self._alarm_task and not self._alarm_task.done():
                self._alarm_task.cancel()
            self._alarm_task = asyncio.ensure_future(self._alarm_auto_clear(seconds))
        except RuntimeError:
            self._state = dict(self._state, alarm=False)  # 无事件循环则不进入报警态
            self._schedule_broadcast()

    async def _alarm_auto_clear(self, seconds):
        """报警窗口到期: alarm复位false并广播, 接收端收到后停止响铃"""
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            # 被新一次trigger_alarm取消: 报警仍在继续, 不复位不触发结束回调
            raise
        else:
            if self._state.get("alarm"):
                self._state = dict(self._state, alarm=False)
                self._schedule_broadcast()
            self._fire(self.on_alarm_end)

    @staticmethod
    def _fire(cb, *args):
        if cb is None:
            return
        try:
            cb(*args)
        except Exception as e:
            logger.error(f"报警回调执行失败: {e}")

    def _schedule_broadcast(self):
        if self.running and self.clients:
            try:
                asyncio.ensure_future(self._broadcast())
            except RuntimeError:
                pass  # 无运行中的事件循环(理论上qasync下不会发生)

    async def _broadcast(self):
        payload = json.dumps(self._state, ensure_ascii=False)
        for ws in list(self.clients):
            # 每个客户端独立任务, 慢/死客户端不阻塞其他推送
            asyncio.ensure_future(self._send_to(ws, payload))

    async def _send_to(self, ws, payload):
        try:
            await asyncio.wait_for(ws.send_str(payload), timeout=3)
        except Exception:
            self.clients.discard(ws)

    # ---------- 路由 ----------
    async def handle_api(self, request):
        data = dict(self._state)
        data["clients"] = len(self.clients)
        return web.json_response(data)

    async def handle_ws(self, request):
        ws = web.WebSocketResponse(heartbeat=25)
        await ws.prepare(request)
        self.clients.add(ws)
        try:
            # 连接即推当前快照
            await ws.send_str(json.dumps(self._state, ensure_ascii=False))
            async for _msg in ws:
                pass  # 客户端消息仅用于心跳/关闭检测
        finally:
            self.clients.discard(ws)
        return ws

    # ---------- 期3 报警视频联动路由 ----------
    async def handle_snapshot(self, request):
        """报警期间快照(手机2fps轮询JPEG); ?cam=按绑定房间联动摄像头取帧,
        缺省/未知摄像头名回退默认摄像头; 帧未就绪返回503"""
        mgr = self.camera_mgr
        cam = request.query.get("cam", "")
        if not mgr:
            return web.json_response({"error": "no snapshot"}, status=503)
        jpeg = mgr.snapshot_jpeg_by_name(cam) if cam else mgr.default_snapshot_jpeg()
        if not jpeg:
            return web.json_response({"error": "no snapshot"}, status=503)
        return web.Response(body=jpeg, content_type="image/jpeg")

    async def handle_clip(self, request):
        """报警剪辑流式播放: 仅允许 captures/ 目录下的 .mp4(防路径穿越),
        FileResponse 原生支持 HTTP Range(手机播放器拖动/边下边播)"""
        from camera.stream_manager import CREATES_DIR
        name = request.query.get("name", "")
        if not name or "/" in name or "\\" in name or name != Path(name).name:
            return web.json_response({"error": "bad name"}, status=400)
        root = Path(CREATES_DIR).resolve()
        path = (root / name).resolve()
        if path.parent != root or path.suffix.lower() != ".mp4" or not path.is_file():
            return web.json_response({"error": "not found"}, status=404)
        return web.FileResponse(path, headers={"Content-Type": "video/mp4"})

    async def handle_index(self, request):
        return web.Response(text=INDEX_HTML, content_type='text/html', charset='utf-8')

    # ---------- 期5 HLS实时流路由 ----------
    async def handle_live_playlist(self, request):
        """报警房间摄像头HLS m3u8播放列表(?cam=摄像头名); 流未运行/未就绪返回404"""
        from camera.hls_stream import get_hls_manager
        s = get_hls_manager().get(request.query.get("cam", ""))
        p = s.playlist_path if s else None
        if p is None or not p.is_file():
            return web.json_response({"error": "no live"}, status=404)
        return web.FileResponse(p, headers={
            "Content-Type": "application/vnd.apple.mpegurl",
            "Cache-Control": "no-cache"})

    async def handle_live_segment(self, request):
        """HLS分片(?cam=摄像头名&seg=seg_NNNNN.ts); 只允许白名单文件名(防路径穿越)"""
        from camera.hls_stream import get_hls_manager, SEG_RE
        seg = request.query.get("seg", "")
        if not SEG_RE.match(seg):
            return web.json_response({"error": "bad seg"}, status=400)
        s = get_hls_manager().get(request.query.get("cam", ""))
        p = s.segment_path(seg) if s else None
        if p is None or not p.is_file():
            return web.json_response({"error": "not found"}, status=404)
        return web.FileResponse(p, headers={"Content-Type": "video/mp2t"})


INDEX_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>HRMLink 心率</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{font-family:sans-serif;display:flex;flex-direction:column;align-items:center;
justify-content:center;height:100vh;margin:0;background:#111;color:#eee}
#hr{font-size:20vw;font-weight:bold;color:#ff5a5a}
#st{color:#888}
#dot{display:inline-block;width:12px;height:12px;border-radius:50%;margin-right:6px}
</style></head>
<body><div id="hr">--</div><div id="st"><span id="dot"></span><span id="txt">连接中...</span></div>
<script>
var ws;
function conn(){
  ws = new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws');
  ws.onmessage = function(e){
    var d = JSON.parse(e.data);
    document.getElementById('hr').textContent = d.heart_rate > 0 ? d.heart_rate : '--';
    var ok = d.status === 'connected';
    document.getElementById('dot').style.background = ok ? '#4caf50' : '#f44336';
    document.getElementById('txt').textContent =
      (ok ? '已连接' : '设备未连接') + ' | ' + d.timestamp + (d.device ? ' | ' + d.device : '');
  };
  ws.onclose = function(){
    document.getElementById('txt').textContent = '连接断开, 3秒后重连';
    setTimeout(conn, 3000);
  };
}
conn();
</script></body></html>"""
