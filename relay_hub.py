# -*- coding: utf-8 -*-
"""
relay_hub.py — ESP32 全屋心率中继中枢 (v1.1.0)

架构: 独立线程 + 自有 asyncio 事件循环 + aiohttp WS server(路由 /relay)
节点协议: 详见 ESP32/设计决策.md §6
  节点→EXE: hello{name,fw,ip} / hb{state,rssi,up,adv,at,sc,sf} / hr{bpm,rssi,ts}
            scan{rssi} / evt{evt: ble_up|ble_down|ble_fail{why}|no_band_mac|name_ok|apply_ok|heard{rssi}}
  EXE→节点: cfg{mac} / connect{mac} / disconnect / scan{dur} / set{name} / apply{...} / reboot
            hb_ack{q}(q=1=手环被正常持有, v1.1.8+固件据此2%待命) / wake(失聪扫描令, v1.1.8+)

仲裁时机模型(两个永不 + 三类触发):
  - 永不: 节点自主连接(连接权只在EXE); 报警期间质量切换(数据中断接管仍放行)
  - 空闲自动申请: 无数据源 → 选平滑RSSI最强且>min_rssi的节点
  - 探测式切换: 持有者RSSI<threshold 连续freeze_cycles个周期 → disconnect
                → broadcast scan(1s窗口) → 候选须为"其他节点"且强于原持有者hysteresis_db以上
                → 无更优/最优仍是自己 → 回连原持有者并进入保持模式(no_better_hold:
                  RSSI回升越过阈值或连接权回收前, 不再因弱信号触发探测, 防自我切换死循环)
  - 连接进行中(pending_connect/connecting)不评估弱信号: 命令式断开无ble_down,
    conn_rssi残留旧值会导致陈旧读数二次触发探测、打断在途connect
  - PC直连优先/互斥: PC连着手环时强制节点释放
  - 断连恢复: 节点BLE自愈(固件内指数退避); 超过stale_seconds无数据无事件 → 接管
  - 放手规则: 节点WS断开 → 立即视为放弃连接权(防占坑)

信号标定/双轨规则(个体偏差均衡):
  - 不同ESP32硬件存在3~8dB个体RF偏差, 并排同位标定: 广播扫描N轮取各节点中位数,
    bias_i = median_i − 全体中位数, 零和存储到 config.ini [esp32_relay] node_biases(JSON)
  - 选路比较(空闲申请/探测切换)用校准值 calibrated = raw − bias;
    min_rssi门槛与threshold_drop掉线判定仍用原始raw(链路绝对质量不因标定改变)
"""
import asyncio
import datetime
import json
import socket
import statistics
import threading
import time

from aiohttp import web, WSMsgType

from system_utils import logger, gs, ups

TOKEN_DEFAULT = "HRMLink-ESP32-2025"
ARB_INTERVAL = 2.0          # 仲裁周期(秒), 与节点心跳一致
ARB_INTERVAL_LOST = 1.0     # 手环失联态的仲裁周期(秒): 接管/兜底响应加快(P1-6)
HR_MIN_INTERVAL = 0.9       # 中枢侧心率节流(秒), 与固件1s节流双保险(0.9防时钟漂移互卡)
HR_NODATA_S = 15.0          # 已连接但无心率数据判定(秒): 超过=手环可能取下充电/未佩戴
BAND_HEARD_WINDOW_S = 10.0  # 未连接时"搜索到手环"窗口(秒): 固件hb.rssi新鲜度8s+余量
EMA_ALPHA = 0.4             # RSSI指数平滑系数(≈最近10次)
PROBE_DISCONNECT_S = 3.0    # 探测·断开阶段超时
PROBE_SCAN_S = 2.5          # 探测·扫描窗等待(节点1s扫描+回包余量)
CONNECT_DEADLINE_S = 18.0   # connect命令总超时(与固件15s对齐+余量)

# 心率包率触发(P1-7): 连接活着但心率流断流(链路卡死/深衰落丢包), RSSI看不出来的病
# 触发 = 60s滑窗内hr到达数连续2周期低于HR_RATE_LOW(期望~60) → 探测式切换
# 防循环: ① ble_up后HR_RATE_WARMUP_S内不评估(手环测量间隙/连接初期窗口未填满)
#         ② 触发后HR_PROBE_COOLDOWN_S冷却(手环真没测量时把最坏切换频率压到10分钟一轮)
HR_RATE_WINDOW = 60.0       # 包率统计滑窗(秒)
HR_RATE_LOW = 40            # 窗内包数低于此值=断流(手环1包/s, 满窗~60)
HR_RATE_STREAK = 2          # 连续N个周期低于才触发
HR_RATE_WARMUP_S = 70.0     # ble_up后保护期(秒)
HR_PROBE_COOLDOWN_S = 600.0 # 包率触发冷却(秒), 防手环侧真无数据时无限换手

FAIL_COOLDOWN_S = 30.0      # P1-2: 节点ble_fail达2次后降权时长(秒), 冷却结束自动恢复资格

# PC兜底(开关关闭): 全体节点失聪才由PC连接, 节点恢复后PC让位
# (lost期间仲裁周期加密为1s, 8周期=8秒, 保持"持续失聪约8秒才兜底"的原语义)
PC_FB_AFTER_CYCLES = 8      # lost状态持续N个仲裁周期(lost态1s/周期)才兜底
PC_FB_RETRY_S = 30.0        # 兜底连接命令重试间隔
PC_FB_HEARD_STREAK = 3      # 让位条件: 节点持续N个周期重新听到手环
PC_FB_HOLD_S = 600.0        # 兜底连接的自动让位有效窗(此后仅视为手动直连不断开)
PC_FB_RELEASE_COOLDOWN_S = 120.0  # 让位后冷却(防信号边缘断连-兜底循环)


def get_local_ip():
    """取本机局域网IP(供UI显示节点应连的地址)"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class RelayHub:
    """中继中枢单例: WS服务 + 节点注册表 + 仲裁器"""

    def __init__(self):
        self._thread = None
        self._loop = None
        self._runner = None
        self.port = gs("esp32_relay", "port", 8899, int, "-RelayHub端口")
        self.token = gs("esp32_relay", "token", TOKEN_DEFAULT, str, "-RelayHub令牌") or TOKEN_DEFAULT

        # 仲裁参数(保存设置后重启hub生效)
        self.threshold_drop = gs("esp32_relay", "threshold_drop", -75, int, "-切换阈值")
        self.hysteresis_db = gs("esp32_relay", "hysteresis_db", 10, int, "-切换迟滞dB")
        self.min_rssi = gs("esp32_relay", "min_rssi", -80, int, "-最低可连RSSI")
        self.stale_seconds = gs("esp32_relay", "stale_seconds", 30, int, "-接管判定秒数")
        self.freeze_cycles = gs("esp32_relay", "freeze_cycles", 3, int, "-切换判定周期数")

        # PC虚拟节点: 开关开启时EXE以普通节点身份参与仲裁(命令经bridge转发给bleak)
        self.pc_as_node = gs("esp32_relay", "pc_as_node", 0, int, "-PC虚拟节点开关") == 1
        self.pc_node_name = (gs("esp32_relay", "pc_node_name", "书房", str, "-PC节点房间名")
                             or "书房").strip()[:24] or "书房"
        self._pc_bridge = None        # PC节点命令接口(DevCtrl.PcNodeBridge), hub重启保留
        self._pc_name = None          # PC虚拟节点在_nodes中的键名
        # PC兜底状态(开关关闭时生效)
        self._fb_lost_streak = 0      # lost持续周期数
        self._fb_heard_streak = 0     # 节点重新听到手环的持续周期数
        self._fb_last_try = 0         # 上次兜底connect命令时间
        self._fb_cmd_ts = 0           # 最近一次兜底连接确认成功时间(让位判定窗)
        self._fb_release_ts = 0       # 上次让位时间(冷却)

        # 节点表: name -> dict(ws, ip, fw, state, rssi_ema, conn_rssi,
        #                      last_seen, last_data_ts, last_evt_ts,
        #                      connected_at, connect_deadline, fail_count)
        self._nodes = {}
        self._lock = threading.Lock()

        # 仲裁状态
        self.active_node = None       # 当前持有手环连接的节点名
        self.pending_connect = None   # 已下发connect待ble_up确认的节点名
        self.low_streak = 0           # 持有者弱信号连续周期数
        self.no_better_hold = False   # 保持模式: 探测已确认当前持有者即最优, RSSI回升前不再因弱信号探测
        self.probe_stage = None       # None/'disconnecting'/'scanning'
        self.probe_deadline = 0.0
        self.probe_pre_rssi = 0       # 探测前持有者RSSI
        self.probe_old_name = None
        self._scan_results = {}       # 探测窗内 name->rssi

        # 心率包率状态(P1-7): 当前持有者hr到达时间滑窗(仅active的hr计入, ble_up清空)
        self._hr_rate = []            # list[float] 时间戳, 评估时惰性清理过期项
        self._hr_low_streak = 0       # 包率连续低于门槛的周期数
        self._hr_probe_ts = 0.0       # 上次包率触发探测时间(冷却)

        # 信号标定/个体偏差: bias>0表示该节点读数偏强, 选路用 calibrated = raw - bias
        self._biases = {}             # name -> int dB (config.ini [esp32_relay] node_biases JSON)
        self._calib = None            # 标定状态dict: running/round/total/samples/result/error/note
        self._calib_round = None      # 当前标定扫描窗 name->rssi (hub loop线程内读写, 无需锁)
        self._load_biases()

        # 主线程注入的钩子
        self._hr_callback = None      # fn(timestamp, bpm)
        self._alarm_check = None      # fn() -> bool  报警中
        self._direct_check = None     # fn() -> bool  PC直连手环中
        self._source_callback = None  # fn(status_dict) 数据源状态变化(hub线程调用)
        self._last_source_sig = None  # 上次推送的数据源签名(去重)
        self._warned_no_mac = False   # 未配置手环的告警只打一次(防仲裁循环刷日志)

    # ---------- 主线程接口(UI/DevCtrl调用) ----------

    def set_hr_callback(self, fn):
        """注册统一心率入口(DevCtrl.on_heart_rate_update, 中继与直连同一下游)"""
        self._hr_callback = fn

    def set_alarm_check(self, fn):
        """注册报警状态探测(webpush _state['alarm'])"""
        self._alarm_check = fn

    def set_direct_check(self, fn):
        """注册PC直连状态探测(ble_monitor.client.is_connected)"""
        self._direct_check = fn

    def set_source_callback(self, fn):
        """注册数据源状态变化回调(fn(status_dict), hub线程调用)"""
        self._source_callback = fn

    def set_band_status_callback(self, fn):
        """注册手环三态回调(fn(status_dict), hub线程调用); 注册时立即补发当前状态"""
        self._band_status_callback = fn
        self._last_band_sig = None
        self._notify_band_status()

    def start(self):
        """启动中继服务(幂等); config.ini [esp32_relay] enabled=1 时由UI触发"""
        if self._thread and self._thread.is_alive():
            return True
        self.port = gs("esp32_relay", "port", 8899, int, "-RelayHub端口")
        self.token = gs("esp32_relay", "token", TOKEN_DEFAULT, str, "-RelayHub令牌") or TOKEN_DEFAULT
        self.threshold_drop = gs("esp32_relay", "threshold_drop", -75, int, "-切换阈值")
        self.hysteresis_db = gs("esp32_relay", "hysteresis_db", 10, int, "-切换迟滞dB")
        self.min_rssi = gs("esp32_relay", "min_rssi", -80, int, "-最低可连RSSI")
        self.stale_seconds = gs("esp32_relay", "stale_seconds", 30, int, "-接管判定秒数")
        self.freeze_cycles = gs("esp32_relay", "freeze_cycles", 3, int, "-切换判定周期数")
        self.pc_as_node = gs("esp32_relay", "pc_as_node", 0, int, "-PC虚拟节点开关") == 1
        self.pc_node_name = (gs("esp32_relay", "pc_node_name", "书房", str, "-PC节点房间名")
                             or "书房").strip()[:24] or "书房"
        self._fb_lost_streak = 0
        self._fb_heard_streak = 0
        self._load_biases()   # 重启hub时重载偏差(保存设置后立即生效)
        self._thread = threading.Thread(target=self._run, daemon=True, name="relay-hub")
        self._thread.start()
        # PC虚拟节点随hub重启恢复注册(bridge引用跨stop/start保留)
        if self.pc_as_node and self._pc_bridge is not None:
            self.register_pc_node(self._pc_bridge)
        return True

    def stop(self):
        """停止中继服务(断开全部节点)"""
        loop = self._loop
        if loop:
            loop.call_soon_threadsafe(loop.stop)
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None
        self._loop = None
        with self._lock:
            self._nodes.clear()
        self.active_node = None
        self.pending_connect = None
        self.probe_stage = None
        self.probe_old_name = None
        self.no_better_hold = False
        self._pc_name = None      # PC节点条目随_nodes清空; bridge保留供start()恢复注册
        self._calib = None        # loop已停, 标定协程随之消亡
        self._calib_round = None
        logger.info("RelayHub 已停止")

    def node_table(self):
        """给UI的节点快照列表"""
        with self._lock:
            rows = []
            for name, n in self._nodes.items():
                rows.append({
                    "name": name,
                    "ip": n.get("ip", ""),
                    "fw": n.get("fw", ""),
                    "state": n.get("state", "offline"),
                    "rssi": n.get("conn_rssi") if n.get("state") == "active"
                            else (int(n["rssi_ema"]) if n.get("rssi_ema") else 0),
                    "bias": self._biases.get(name, 0),
                    "age": int(time.time() - n.get("last_seen", 0)),
                })
            return rows

    # ---------- 信号标定/个体偏差 ----------

    def _load_biases(self):
        """从 config.ini [esp32_relay] node_biases(JSON) 载入节点偏差"""
        try:
            raw = gs("esp32_relay", "node_biases", "", str, "-RelayHub节点偏差")
            d = json.loads(raw) if raw else {}
            self._biases = {str(k): int(round(v)) for k, v in d.items()
                            if isinstance(v, (int, float))} if isinstance(d, dict) else {}
        except (ValueError, TypeError) as e:
            logger.warning(f"RelayHub 节点偏差载入失败, 已清零: {e}")
            self._biases = {}

    def _save_biases(self):
        ups("esp32_relay", "node_biases", json.dumps(self._biases, ensure_ascii=False))

    def get_biases(self):
        """UI显示用: 当前节点偏差快照"""
        return dict(self._biases)

    def clear_biases(self):
        """UI调用: 清除全部偏差并持久化(标定进行中拒绝)"""
        if self._calib and self._calib.get("running"):
            return False, "标定进行中, 无法清除"
        self._biases = {}
        self._save_biases()
        logger.info("RelayHub 已清除全部节点偏差")
        return True, "已清除全部节点偏差"

    def start_calibration(self, rounds=15, scan_ms=800):
        """UI调用: 并排同位标定(全部在线节点同窗采样→中位数→零和偏差)
        返回 (ok, msg)"""
        if self._calib and self._calib.get("running"):
            return False, "标定正在进行中"
        if not (self._loop and self._thread and self._thread.is_alive()):
            return False, "中继服务未启动, 请先启用ESP32中继"
        online = [1 for n in self._nodes.values() if n.get("ws")]
        if len(online) < 2:
            return False, "至少需要2台在线节点才能标定"
        if self._direct_now():
            return False, "PC直连手环中, 请先断开直连"
        if self._alarm_now():
            return False, "报警进行中, 请稍后再试"
        self._calib = {
            "running": True, "round": 0, "total": rounds,
            "samples": {}, "result": None, "error": None,
            "note": "准备: 通知持有节点放手...",
        }
        asyncio.run_coroutine_threadsafe(
            self._calibration_run(rounds, scan_ms), self._loop)
        logger.info(f"RelayHub 信号标定开始: {rounds}轮 x {scan_ms}ms")
        return True, "标定开始"

    def calibration_status(self):
        """UI轮询: 标定进度/结果快照(线程安全只读)"""
        c = self._calib
        if not c:
            return {"running": False, "started": False}
        out = dict(c)
        out["started"] = True
        return out

    async def _calibration_run(self, rounds, scan_ms):
        """标定协程(hub loop内): 放手→N轮广播扫描→中位数→零和偏差→持久化"""
        try:
            # 0) 让持有节点放手, 全员恢复可扫(连接即静默)
            if self.active_node:
                self._send_to(self.active_node, {"type": "disconnect"})
                self.active_node = None
            await asyncio.sleep(3.0)   # 等节点BLE释放并恢复广播可见
            samples = {}
            for rnd in range(1, rounds + 1):
                if not (self._thread and self._thread.is_alive()):
                    return             # hub停止, 随loop一起消亡
                self._calib["round"] = rnd
                self._calib["note"] = f"第 {rnd}/{rounds} 轮采样"
                round_rssi = {}
                self._calib_round = round_rssi
                self._broadcast({"type": "scan", "dur": scan_ms})
                await asyncio.sleep(scan_ms / 1000.0 + 0.9)   # 扫描窗+回包余量
                self._calib_round = None
                for name, r in round_rssi.items():
                    samples.setdefault(name, []).append(r)
                self._calib["samples"] = {k: len(v) for k, v in samples.items()}   # UI只要计数
            self._finish_calibration(samples)
        except Exception as e:
            logger.error(f"RelayHub 标定异常: {e}")
            if self._calib:
                self._calib["running"] = False
                self._calib["error"] = str(e)

    def _finish_calibration(self, samples):
        """收口: 各节点中位数(采样≥5才有效) → 零和偏差 → 写config并全量替换"""
        self._calib["running"] = False
        med = {name: statistics.median(rs) for name, rs in samples.items() if len(rs) >= 5}
        if len(med) < 2:
            self._calib["error"] = "有效节点不足2台(手环未广播或节点采样过少), 标定失败"
            self._calib["note"] = ""
            logger.warning(f"RelayHub 标定失败: 各节点采样数 "
                           f"{ {k: len(v) for k, v in samples.items()} }")
            return
        ref = statistics.median(med.values())   # 全体中位数为基准(零和)
        lines = []
        for name, m in sorted(med.items()):
            b = int(round(m - ref))
            self._biases[name] = b
            lines.append(f"{name}: 中位数 {m:.0f}dBm → 偏差 {b:+d}dB")
        self._save_biases()   # 全量替换: 未参标节点偏差一并清零(防陈旧值污染选路)
        self._calib["result"] = {"ref": int(round(ref)), "items": lines}
        self._calib["note"] = "标定完成, 偏差已应用并保存"
        logger.info("RelayHub 标定完成: " + "; ".join(lines))

    # ---------- 服务线程 ----------

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            app = web.Application()
            app.router.add_get("/relay", self._ws_handler)
            self._runner = web.AppRunner(app)
            loop.run_until_complete(self._runner.setup())
            site = web.TCPSite(self._runner, "0.0.0.0", self.port)
            loop.run_until_complete(site.start())
            task = loop.create_task(self._arbiter_loop())
            logger.info(f"RelayHub 已启动: ws://0.0.0.0:{self.port}/relay (token鉴权)")
            loop.run_forever()
            task.cancel()
        except Exception as e:
            logger.error(f"RelayHub 服务异常退出: {e}")
        finally:
            try:
                loop.run_until_complete(self._runner.cleanup())
            except Exception:
                pass
            loop.close()

    async def _arbiter_loop(self):
        """仲裁器: 每2秒决策一次(时机模型见模块docstring); 手环失联态加密到1s(P1-6)"""
        while True:
            try:
                await asyncio.sleep(ARB_INTERVAL_LOST if self._band_lost_now()
                                    else ARB_INTERVAL)
                self._decide()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"RelayHub 仲裁异常: {e}")

    # ---------- WS处理 ----------

    async def _ws_handler(self, request):
        # token鉴权: Authorization: Bearer xxx 或 ?token=xxx
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {self.token}" and request.query.get("token") != self.token:
            logger.warning(f"RelayHub 拒绝未授权连接: {request.remote}")
            return web.Response(status=401, text="unauthorized")
        wsr = web.WebSocketResponse(heartbeat=30)
        await wsr.prepare(request)
        peer = request.remote
        name = None
        try:
            async for msg in wsr:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except (ValueError, TypeError):
                        continue
                    name = self._dispatch(peer, name, data, wsr) or name
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSED):
                    break
        finally:
            self._node_offline(name, peer)
        return wsr

    def _dispatch(self, peer, name, data, wsr):
        """消息分发(在hub线程loop内); 返回该连接已确认的节点名"""
        mtype = data.get("type")
        if mtype == "hello":
            nname = str(data.get("name") or "").strip()[:24] or "unnamed"
            renamed = False
            with self._lock:
                old = self._nodes.get(nname)
                def _ws_alive(w):
                    return bool(w) and not w.closed
                if old and (_ws_alive(old.get("ws")) or old.get("local")) and old.get("ws") is not wsr:
                    # 重名冲突: 拒绝新节点(节点名是仲裁与UI的唯一标识; PC虚拟节点同名同样拒绝;
                    # 旧条目ws已死则允许接管, 异常断线自恢复)
                    asyncio.ensure_future(self._safe_send(wsr, {"type": "evt", "evt": "name_conflict"}))
                    asyncio.ensure_future(wsr.close())
                    return name
                # 同连接改名换绑(rename_node下发set后固件补发hello): 迁移统计与持有状态, 删旧名条目防僵尸
                prev = None
                if name and name != nname:
                    stale = self._nodes.get(name)
                    if stale is not None and stale.get("ws") is wsr:
                        prev = self._nodes.pop(name)
                        renamed = True
                if old is not None and old.get("ws") is wsr:
                    entry = old   # 同连接重复hello: 保留原条目
                elif prev is not None:
                    entry = prev  # 改名: 保留统计与持有状态
                else:
                    entry = {
                        "ws": wsr, "ip": peer, "fw": str(data.get("fw", "")),
                        "state": "idle", "rssi_ema": 0, "conn_rssi": 0,
                        "last_seen": time.time(), "last_data_ts": 0, "last_evt_ts": time.time(),
                        "connected_at": 0, "connect_deadline": 0, "fail_count": 0,
                        "last_hr_ts": 0,
                    }
                entry.update({"ws": wsr, "ip": peer, "fw": str(data.get("fw", "")),
                              "last_seen": time.time(), "last_evt_ts": time.time()})
                self._nodes[nname] = entry
                if renamed and self.pending_connect == name:
                    self.pending_connect = nname
            if renamed and self.active_node == name:
                self.active_node = nname  # 改名后保留持有权
            logger.info(f"RelayHub 节点上线: {nname} ({peer}, fw={data.get('fw')})"
                        + (f" (由 {name} 改名)" if renamed else ""))
            asyncio.ensure_future(self._safe_send(wsr, {"type": "cfg", "mac": self._band_mac()}))
            return nname

        if not name:
            return name
        with self._lock:
            node = self._nodes.get(name)
        if not node or node.get("ws") is not wsr:
            return name
        node["last_seen"] = time.time()

        if mtype == "hb":
            r = data.get("rssi") or 0
            node["state"] = str(data.get("state", "idle"))
            node["last_evt_ts"] = time.time()
            adv = data.get("adv")
            if isinstance(adv, (int, float)):
                a = int(adv)
                node["adv_delta"] = a   # 2s内收到的所有BLE广播包数(诊断射频)
                # a>0=手环在场(2026-09-29实锤: 现役固件hb.rssi恒为0也无heard边沿上报,
                # 包计数是唯一在场信号; active持有态不看广播)
                if a > 0 and node.get("state") != "active":
                    node["band_heard_ts"] = time.time()
                # 沿触发日志: 开始听到/从听到变聋 各打一条, 持续听到的节点不刷屏
                if a > 0 and not node.get("_adv_logged"):
                    node["_adv_logged"] = True
                    logger.info(f"RelayHub 节点 {name} 听到BLE广播: {a}包/2s (hb.rssi={r})")
                elif a == 0 and node.get("_adv_logged"):
                    node["_adv_logged"] = False
                    if not node.get("quiesce"):   # P3-15: 2%待命期adv=0是常态, 非射频聋
                        logger.warning(f"RelayHub 节点 {name} BLE广播消失 (adv=0, hb.rssi={r})")
            if isinstance(r, (int, float)) and r < 0:
                ema = node["rssi_ema"]
                node["rssi_ema"] = r if not ema else ema * (1 - EMA_ALPHA) + r * EMA_ALPHA
                if node["state"] == "active":
                    node["conn_rssi"] = r
                    node["last_data_ts"] = time.time()
                else:
                    node["band_heard_ts"] = time.time()  # 未连接时hb.rssi<0=最近8s内听到手环广播
            # BLE扫描状态遥测(v1.1.2): sc=0表示节点扫描未运行, sf>0表示扫描启动失败过;
            # active态sc=0是正常停扫(连接后省电), 只有非active态sc=0才是扫描真挂了
            sc = data.get("sc")
            if isinstance(sc, (int, float)):
                node["scan_on"] = int(sc)
                node["scan_fail"] = int(data.get("sf") or 0)
                if int(sc) == 0 and node["state"] != "active" and not node.get("_sc_warned"):
                    node["_sc_warned"] = True
                    logger.warning(f"RelayHub 节点 {name} BLE扫描未运行(sc=0 sf={node['scan_fail']})!")
                elif int(sc) == 1 and node.get("_sc_warned"):
                    node["_sc_warned"] = False
                    logger.info(f"RelayHub 节点 {name} BLE扫描已恢复")
            # 广播包累计(v1.1.4): at=启动以来收到的所有BLE广播包总数, 首条hb≈纯BLE基线期包数
            at = data.get("at")
            if isinstance(at, (int, float)):
                at = int(at)
                first = "adv_total" not in node
                node["adv_total"] = at
                now_ = time.time()
                if first or now_ - node.get("_at_log_ts", 0) > 60:
                    node["_at_log_ts"] = now_
                    tag = "(启动基线,含60s纯BLE期)" if first else ""
                    logger.info(f"RelayHub 节点 {name} BLE广播累计: {at}包 {tag}")
            self._notify_band_status()  # hb每2s评估手环三态(签名去重,无变化零开销)
            # P3-15: 心跳应答下发待命标志(q=1=手环被正常持有 → 空闲节点2%占空比待命;
            # 固件v1.1.8+仅在idle非探测窗时应用, active/connecting天然忽略; 旧固件忽略本消息)
            q = 0
            try:
                an = self._nodes.get(self.active_node) if self.active_node else None
                if an is not None and an.get("state") == "active" and self.probe_stage is None:
                    q = 1
            except Exception:
                q = 0
            node["quiesce"] = q   # 供adv=0诊断区分"待命静默"与"射频聋"
            try:
                asyncio.ensure_future(self._safe_send(wsr, {"type": "hb_ack", "q": q}))
            except Exception:
                pass
        elif mtype == "hr":
            bpm = data.get("bpm") or 0
            if isinstance(bpm, (int, float)) and 0 < bpm <= 250:
                node["conn_rssi"] = data.get("rssi") or node.get("conn_rssi") or 0
                node["last_data_ts"] = time.time()
                node["last_evt_ts"] = time.time()
                if self.active_node in (None, name):
                    self.active_node = name
                if self.active_node == name:
                    self._hr_rate.append(time.time())   # P1-7: 包率统计(节流前记录, 真实到达率)
                if time.time() - node.get("last_hr_ts", 0) >= HR_MIN_INTERVAL:
                    node["last_hr_ts"] = time.time()
                    self._notify_band_status()  # 心率到货 → 立即转"正常"态
                    if self._hr_callback:
                        try:
                            # 时间戳与直连同格式(字符串), 避免下游按str处理时显示epoch数字
                            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            self._hr_callback(ts, int(bpm))  # 统一入口, 与直连同路
                        except Exception as e:
                            logger.error(f"RelayHub 心率回调异常: {e}")
        elif mtype == "scan":
            r = data.get("rssi") or 0
            if isinstance(r, (int, float)) and r < 0:
                if self._calib and self._calib.get("running") and self._calib_round is not None:
                    self._calib_round[name] = r   # 标定窗采样
                else:
                    self._scan_results[name] = r  # 探测窗采样
        elif mtype == "evt":
            evt = data.get("evt", "")
            node["last_evt_ts"] = time.time()
            if evt == "ble_up":
                node["state"] = "active"
                node["connected_at"] = time.time()
                node["connect_deadline"] = 0
                old_pending = self.pending_connect
                self.active_node = name
                self.pending_connect = None
                self.low_streak = 0
                node["fail_count"] = 0   # 连接成功: P1-2失败降权清零
                node["last_fail_ts"] = 0
                self._hr_rate = []      # 新持有者: 包率窗口重建(warmup期保护)
                self._hr_low_streak = 0
                node["last_hr_ts"] = 0  # 新连接未见过hr: 首条hr立即过节流, 状态条先显"无数据"再转正常
                self._notify_band_status()
                logger.info(f"RelayHub 节点 {name} 已连接手环")
                if old_pending and old_pending != name:
                    # 自愈回连抢先上手环(固件v1.1.8 P1-1): 在途派活的节点立即撤回,
                    # 防双持有抖动(手环单连接, 后来者本会失败, 主动断开省其30s挣扎)
                    logger.info(f"RelayHub {name} 已持有手环, 撤销在途派活 {old_pending}")
                    self._send_to(old_pending, {"type": "disconnect"})
            elif evt == "heard":
                # P1-6②(固件v1.1.8): idle节点"重新听到手环广播"边沿即时上报,
                # 断联接管/回家发现不再等2s心跳; 刷新可听性与EMA供派活评估
                r = data.get("rssi")
                node["band_heard_ts"] = time.time()
                if isinstance(r, (int, float)) and r < 0:
                    ema = node.get("rssi_ema")
                    node["rssi_ema"] = r if not ema else ema * (1 - EMA_ALPHA) + r * EMA_ALPHA
                self._notify_band_status()
                if not self.active_node and not self._any_connecting():
                    self._decide()   # 手环无人持有: 立即评估派活, 不等下一仲裁周期
            elif evt == "ble_down":
                node["state"] = "idle"
                node["conn_rssi"] = 0
                if self.active_node == name:
                    logger.info(f"RelayHub 节点 {name} 手环断开, 连接权回收")
                    self.active_node = None
                    self.no_better_hold = False   # 连接权回收, 保持模式随之解除(重新仲裁)
                self._notify_band_status()
            elif evt == "ble_fail":
                node["state"] = "idle"
                node["fail_count"] += 1
                node["last_fail_ts"] = time.time()   # P1-2: 降权冷却起点
                node["connect_deadline"] = 0
                node["conn_rssi"] = 0   # 清残留: 防保持模式/迟滞判定吃陈旧读数
                if self.pending_connect == name:
                    self.pending_connect = None
                if self.active_node == name:
                    # 持有者连接失败=持有权实际已空: 回收+解除保持模式, 让空闲申请
                    # 按当前广播EMA重新派活。否则_decide会在保持模式处每周期提前
                    # return(读残留conn_rssi), 仲裁器锁死, 手环挂起无人连(2026-09-28 实锤)
                    self.active_node = None
                    self.no_better_hold = False
                    self.low_streak = 0
                    self._hr_low_streak = 0
                self._notify_band_status()
                why = str(data.get("why") or "?")
                logger.warning(f"RelayHub 节点 {name} 连接手环失败({node['fail_count']}次 原因:{why})")
        return name

    def _node_offline(self, name, peer):
        if not name:
            return
        with self._lock:
            node = self._nodes.get(name)
            if node and node.get("ip") == peer:
                node["ws"] = None
                node["state"] = "offline"
                node["connect_deadline"] = 0
                # 放手规则: 节点失联立即回收连接权(防占坑)
                if name and self.active_node == name:
                    self.active_node = None
                    logger.info(f"RelayHub 节点 {name} 失联, 连接权回收")
                if self.pending_connect == name:
                    self.pending_connect = None
        self._notify_band_status()  # 节点掉线可能改变手环可见性, 锁外推送(回调内band_status自取锁)

    async def _safe_send(self, ws, obj):
        try:
            if ws and not ws.closed:
                await ws.send_str(json.dumps(obj, ensure_ascii=False))
        except Exception:
            pass

    def _send_to(self, name, obj):
        with self._lock:
            node = self._nodes.get(name)
            bridge = node.get("bridge") if node else None
            ws = node.get("ws") if node else None
        if bridge is not None:   # PC虚拟节点: 命令经bridge转给bleak(GUI线程)
            try:
                bridge.on_command(obj)
            except Exception as e:
                logger.warning(f"RelayHub PC节点命令下发失败: {e}")
            return
        if not self._loop or not ws:
            return
        asyncio.run_coroutine_threadsafe(self._safe_send(ws, obj), self._loop)

    def _broadcast(self, obj):
        with self._lock:
            items = [(n.get("bridge"), n.get("ws")) for n in self._nodes.values()]
        for bridge, ws in items:
            if bridge is not None:
                try:
                    bridge.on_command(obj)
                except Exception:
                    pass
            elif ws and self._loop:
                asyncio.run_coroutine_threadsafe(self._safe_send(ws, obj), self._loop)

    def _wake_idle_nodes(self):
        """P3-15: 扫描令(wake) — 2%待命的空闲节点立即恢复99%全速扫描。
        只发idle且WS在线的WS节点: active/connecting不需要, PC节点不走WS心跳应答通道
        (PC采样频率由v1.2.2降频策略自行管理)"""
        with self._lock:
            targets = [nm for nm, n in self._nodes.items()
                       if n.get("state") == "idle" and n.get("ws")]
        for nm in targets:
            self._send_to(nm, {"type": "wake"})

    # ---------- PC虚拟节点(开关开启时EXE以普通节点身份参与仲裁) ----------

    def pc_node_mode(self):
        """PC虚拟节点是否已注册(注册后直连互斥/状态特判全部让位于节点语义)"""
        return self._pc_name is not None

    def _pc_active(self):
        """PC节点上报有效的前提: 已注册且hub线程在跑(中继停用时上报一律忽略)"""
        return self._pc_name is not None and self._thread is not None

    def set_pc_bridge(self, bridge):
        """DevCtrl启动时注入PC节点命令接口(此时未必注册)"""
        self._pc_bridge = bridge

    def register_pc_node(self, bridge):
        """注册/更新PC虚拟节点(幂等): 名称取self.pc_node_name, 与ESP32重名自动加后缀;
        改名重注册保留统计与持有状态。返回生效节点名"""
        want = (self.pc_node_name or "书房").strip()[:24] or "书房"
        with self._lock:
            old_name = self._pc_name
            renamed = old_name is not None and old_name != want
            if old_name is None:
                old = None
            elif renamed:
                old = self._nodes.pop(old_name)
            else:
                old = self._nodes.get(old_name)
            cand, i = want, 1
            while cand in self._nodes and self._nodes.get(cand) is not old:
                cand = f"{want}({i})"; i += 1
            if old is not None and old.get("local"):
                entry = old            # 重复注册/改名: 保留统计与持有状态
            else:
                entry = {"ws": None, "ip": "本机", "fw": "PC", "local": True,
                         "state": "idle", "rssi_ema": 0, "conn_rssi": 0,
                         "last_seen": time.time(), "last_data_ts": 0,
                         "last_evt_ts": time.time(), "connected_at": 0,
                         "connect_deadline": 0, "fail_count": 0, "last_hr_ts": 0}
            entry["bridge"] = bridge
            self._nodes[cand] = entry
            self._pc_bridge = bridge
            self._pc_name = cand
            if renamed:
                if self.active_node == old_name:
                    self.active_node = cand
                if self.pending_connect == old_name:
                    self.pending_connect = cand
        logger.info(f"RelayHub PC虚拟节点已注册: {cand}")
        return cand

    def unregister_pc_node(self):
        """注销PC虚拟节点(开关关闭); bridge引用保留供再次开启"""
        with self._lock:
            name = self._pc_name
            self._pc_name = None
            if name:
                self._nodes.pop(name, None)
        if name:
            if self.active_node == name:
                self.active_node = None
                logger.info("RelayHub PC虚拟节点注销, 连接权回收")
            if self.pending_connect == name:
                self.pending_connect = None
            logger.info(f"RelayHub PC虚拟节点已注销: {name}")
            self._notify_band_status()

    def rename_node(self, old_name, new_name):
        """EXE端节点改名入口(设置页调用, GUI线程安全):
        ESP32节点下发set命令, 固件存NVS后补发hello → 本hub同连接换绑(不断WS不断BLE);
        PC虚拟节点改走register_pc_node改名迁移。返回(是否受理, 提示文案)"""
        new_name = str(new_name or "").replace('"', "").replace("\\", "").strip()[:24]
        old_name = str(old_name or "").strip()
        if not new_name:
            return False, "名字不能为空(引号会被自动过滤)"
        if new_name == old_name:
            return True, "名字未变化"
        with self._lock:
            node = self._nodes.get(old_name)
            if not node:
                return False, f"节点 {old_name} 不存在(可能刚离线, 请刷新)"
            if new_name in self._nodes:
                return False, f"名字 [{new_name}] 已被占用, 换一个"
            is_local = bool(node.get("local"))
            bridge = node.get("bridge")
            ws_up = bool(bridge) or bool(node.get("ws"))
        if is_local:
            self.pc_node_name = new_name
            self.register_pc_node(bridge)
            return True, f"PC节点已改名: {new_name}"
        if not ws_up:
            return False, "节点离线, 无法远程改名(等节点上线, 或连它的热点走配网页改名)"
        self._send_to(old_name, {"type": "set", "name": new_name})
        return True, (f"改名命令已下发, 节点数秒内生效, 列表自动刷新。\n"
                      f"若报警视频联动绑定了 [{old_name}], 请同步更新绑定房间。")

    def reboot_node(self, name):
        """EXE端远程重启单个节点入口(设置页调用, GUI线程安全):
        ESP32节点下发reboot命令, 固件delay后ESP.restart, WS断开由节点自动重连+hello重新登记;
        PC虚拟节点即EXE自身, 不适用远程重启。返回(是否受理, 提示文案)"""
        name = str(name or "").strip()
        with self._lock:
            node = self._nodes.get(name)
            if not node:
                return False, f"节点 {name} 不存在(可能刚离线, 请刷新)"
            if node.get("local") or node.get("bridge"):
                return False, "PC节点即本机EXE, 不支持远程重启"
            ws_up = bool(node.get("ws"))
        if not ws_up:
            return False, "节点离线, 无法远程重启(等节点上线或现场断电重启)"
        self._send_to(name, {"type": "reboot"})
        return True, (f"重启命令已下发到 [{name}], 节点数秒内重启。\n"
                      f"重启期间该节点短暂离线, 重连后会自动重新登记; "
                      f"若它正持有手环, 中枢会自动仲裁切换到其他节点。")

    def pc_report_hb(self, connected, rssi=0):
        """PC虚拟节点心跳(bridge定时调用): 刷新在位信息+连接状态对账。
        手动直连/静默掉线等不经hub命令的状态变化在此收敛为ble_up/ble_down语义"""
        name = self._pc_name
        if not self._pc_active() or self._pc_bridge is None:
            return
        with self._lock:
            n = self._nodes.get(name)
            if not n:
                return
            n["last_seen"] = time.time()
            n["state"] = "active" if connected else "idle"
            if isinstance(rssi, (int, float)) and rssi < 0:
                r = int(rssi)
                ema = n["rssi_ema"]
                n["rssi_ema"] = r if not ema else ema * (1 - EMA_ALPHA) + r * EMA_ALPHA
                if connected:
                    n["conn_rssi"] = r
                else:
                    n["band_heard_ts"] = time.time()
        was_active = (self.active_node == name)
        if not connected and was_active:
            self.pc_report_evt("ble_down")
        elif connected and not was_active:
            other = self.active_node
            if other:
                logger.info(f"RelayHub PC虚拟节点发现手环被 {other} 持有, 命令其释放(PC接管优先)")
                self._send_to(other, {"type": "disconnect"})
            self.pc_report_evt("ble_up")
        self._notify_band_status()

    def pc_report_evt(self, evt, why=""):
        """PC虚拟节点事件(ble_up/ble_down/ble_fail), 语义与WS节点evt一致"""
        name = self._pc_name
        if not self._pc_active():
            return
        with self._lock:
            n = self._nodes.get(name)
            if not n:
                return
            n["last_evt_ts"] = time.time()
        if evt == "ble_up":
            with self._lock:
                n["state"] = "active"
                n["connected_at"] = time.time()
                n["connect_deadline"] = 0
                self.active_node = name
                self.pending_connect = None
                self.low_streak = 0
                self._hr_rate = []      # 新持有者: 包率窗口重建(warmup期保护)
                self._hr_low_streak = 0
                n["last_hr_ts"] = 0
            self._notify_band_status()
            logger.info(f"RelayHub 节点 {name}(PC) 已连接手环")
        elif evt == "ble_down":
            with self._lock:
                n["state"] = "idle"
                n["conn_rssi"] = 0
                if self.active_node == name:
                    logger.info(f"RelayHub 节点 {name}(PC) 手环断开, 连接权回收")
                    self.active_node = None
                    self.no_better_hold = False
            self._notify_band_status()
        elif evt == "ble_fail":
            with self._lock:
                n["state"] = "idle"
                n["fail_count"] += 1
                n["connect_deadline"] = 0
                n["conn_rssi"] = 0   # 清残留: 防保持模式/迟滞判定吃陈旧读数
                if self.pending_connect == name:
                    self.pending_connect = None
                if self.active_node == name:
                    # 持有者连接失败=持有权实际已空: 回收+解除保持模式(同WS节点分支)
                    self.active_node = None
                    self.no_better_hold = False
                    self.low_streak = 0
                    self._hr_low_streak = 0
            self._notify_band_status()
            logger.warning(f"RelayHub 节点 {name}(PC) 连接手环失败({n['fail_count']}次 原因:{why or '?'})")

    def pc_report_hr(self, bpm, rssi=0):
        """PC虚拟节点心率上报: 与WS节点hr同路(节流+统一回调入口)"""
        name = self._pc_name
        if not self._pc_active() or not (isinstance(bpm, (int, float)) and 0 < bpm <= 250):
            return
        fire = False
        with self._lock:
            n = self._nodes.get(name)
            if not n:
                return
            n["last_data_ts"] = time.time()
            n["last_evt_ts"] = time.time()
            if isinstance(rssi, (int, float)) and rssi < 0:
                n["conn_rssi"] = int(rssi)
            if self.active_node in (None, name):
                self.active_node = name
            if self.active_node == name:
                self._hr_rate.append(time.time())   # P1-7: 包率统计(节流前记录)
            if time.time() - n.get("last_hr_ts", 0) >= HR_MIN_INTERVAL:
                n["last_hr_ts"] = time.time()
                fire = True
        if fire:
            self._notify_band_status()   # 心率到货 → 立即转"正常"态
            if self._hr_callback:
                try:
                    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self._hr_callback(ts, int(bpm))
                except Exception as e:
                    logger.error(f"RelayHub 心率回调异常: {e}")

    def pc_report_scan(self, rssi):
        """PC虚拟节点探测窗采样(对齐WS节点scan消息; 不参与标定)"""
        name = self._pc_name
        if not self._pc_active() or not (isinstance(rssi, (int, float)) and rssi < 0):
            return
        if self.probe_stage == "scanning":
            self._scan_results[name] = int(rssi)

    # ---------- 仲裁决策 ----------

    def _band_lost_now(self):
        """手环失联快速判定(P1-6: lost期间仲裁周期2s→1s, 接管/兜底响应加快)"""
        try:
            return self.band_status().get("state") == "lost"
        except Exception:
            return False

    def band_status(self):
        """手环三态(心率监测页状态条/日志用):
        ok=已连接且心率数据正常; nodata=有信号无心率(已连接无数据=可能取下充电/未佩戴; 仅听到广播=未连接);
        lost=所有节点均搜索不到(外出/关机/被手机占用)"""
        # PC直连优先: 手环被PC占用时节点按互斥规则已释放且手环停止广播,
        # 节点侧必然"全聋", 不能据此汇报失联
        if self._direct_now():
            return {"state": "ok", "node": "PC直连", "rssi": 0}
        now = time.time()
        with self._lock:
            n = self._nodes.get(self.active_node) if self.active_node else None
            if n and n.get("state") == "active":
                if now - (n.get("last_hr_ts") or 0) <= HR_NODATA_S:
                    return {"state": "ok", "node": self.active_node, "rssi": int(n.get("conn_rssi") or 0)}
                return {"state": "nodata", "node": self.active_node,
                        "reason": "已连接但无心率数据(可能取下充电/未佩戴)",
                        "rssi": int(n.get("conn_rssi") or 0)}
            heard = [(nm, nd) for nm, nd in self._nodes.items()
                     if now - (nd.get("band_heard_ts") or 0) <= BAND_HEARD_WINDOW_S]
            if heard:
                best = max(heard, key=lambda kv: kv[1].get("rssi_ema") or 0)
                return {"state": "nodata", "node": best[0], "reason": "已搜索到手环但未连接",
                        "rssi": int(best[1].get("rssi_ema") or 0)}
        return {"state": "lost", "node": "", "reason": "所有节点均未搜索到手环", "rssi": 0}

    def _notify_band_status(self):
        """手环三态变化推送(签名去重); 回调在hub线程执行, UI侧需经信号排队"""
        try:
            st = self.band_status()
        except Exception as e:
            logger.error(f"RelayHub 手环状态评估异常: {e}")
            return
        sig = (st.get("state"), st.get("node"), st.get("reason", ""))
        if sig == getattr(self, "_last_band_sig", None):
            return
        self._last_band_sig = sig
        if st.get("state") == "lost":
            # P3-15: 扫描令 — 转入失聪即广播wake, 2%待命的空闲节点立即恢复全速
            # (覆盖ble_down/WS掉线/心跳失联接管/PC事件全部入径; sig去重防重复拉响)
            try:
                self._wake_idle_nodes()
            except Exception as e:
                logger.warning(f"RelayHub 扫描令下发失败: {e}")
        cb = getattr(self, "_band_status_callback", None)
        if cb:
            try:
                cb(st)
            except Exception as e:
                logger.error(f"RelayHub 手环状态回调异常: {e}")

    def source_status(self):
        """当前心率数据源状态(接收端通知栏显示用)
        返回: {"phase": active|switching|direct|none, "source": 名字,
               "target": 切换目标(仅switching), "rssi": 负数或0(未知)}"""
        try:
            direct = self._direct_now()
        except Exception:
            direct = False
        if direct:
            return {"phase": "direct", "source": "PC", "rssi": 0}
        # 目标节点必须真的处于connecting态才报switching(防冻结滞留误报, 兜底校验);
        # probe_stage同样只在其时限内可信——节点全下线时_decide在"无节点"处早退不再推进
        # 探测状态机, probe_stage会冻结在disconnecting/scanning, 没有deadline兜底
        # 接收端会永远显示"切换中"(2026-09-29)
        probe_live = bool(self.probe_stage) and time.time() <= self.probe_deadline + 30.0
        pc_node = self._nodes.get(self.pending_connect) if self.pending_connect else None
        if probe_live or (pc_node and pc_node.get("state") == "connecting"):
            old = self.probe_old_name or self.active_node or ""
            tgt = self.pending_connect or ""
            return {"phase": "switching", "source": old, "target": tgt, "rssi": 0}
        n = self._nodes.get(self.active_node) if self.active_node else None
        if n and n.get("state") == "active":
            rssi = n.get("conn_rssi") or 0
            return {"phase": "active", "source": self.active_node, "rssi": int(rssi)}
        return {"phase": "none", "source": "", "rssi": 0}

    def _notify_source(self):
        """数据源状态变化推送(签名去重; rssi按5dB分档, 防通知抖动)"""
        st = self.source_status()
        rssi = st.get("rssi") or 0
        sig = (st.get("phase"), st.get("source"), st.get("target", ""),
               (rssi // 5) if rssi else 0)
        if sig == self._last_source_sig:
            return
        self._last_source_sig = sig
        if self._source_callback:
            try:
                self._source_callback(st)
            except Exception as e:
                logger.error(f"RelayHub 数据源回调异常: {e}")

    def _band_mac(self):
        """手环MAC取自config.ini [Device] last_selected_device"""
        raw = gs("Device", "last_selected_device", "", str, "-RelayHub手环MAC")
        try:
            dev = json.loads(raw) if raw else None
            return (dev or {}).get("address", "")
        except (ValueError, TypeError):
            return ""

    def _alarm_now(self):
        try:
            return bool(self._alarm_check and self._alarm_check())
        except Exception:
            return False

    def _direct_now(self):
        if self.pc_node_mode():
            # PC已节点化: 手环由PC连接=PC节点active, 由仲裁管理, 直连互斥让位
            return False
        try:
            return bool(self._direct_check and self._direct_check())
        except Exception:
            return False

    def _any_connecting(self):
        now = time.time()
        for n in self._nodes.values():
            if n.get("state") == "connecting" and n.get("connect_deadline", 0) > now:
                return True
        return False

    def _best_idle(self):
        """空闲节点中选校准RSSI最强者(失败次数少优先)
        双轨规则: min_rssi门槛用原始rssi_ema, 排名比较用校准值 raw - bias
        P1-2降权: 30s内失败≥2次的节点暂缓派活(信号边缘反复失败的刹车); 全员冷却时放宽
        在场校验(2026-09-29): 必须窗口内听到过手环广播才派活——rssi_ema永不衰减,
        手环外出/关机后残值会驱动对"聋"节点循环下发注定失败的connect(18s一轮),
        pending_connect常年connecting → 接收端永远显示"切换中"(EXE侧却报失联)"""
        now = time.time()

        def _cooling(n):
            return n.get("fail_count", 0) >= 2 and now - (n.get("last_fail_ts") or 0) < FAIL_COOLDOWN_S

        def _hearing(n):
            return now - (n.get("band_heard_ts") or 0) <= BAND_HEARD_WINDOW_S

        def _has_rssi(n):
            r = n.get("rssi_ema")
            return isinstance(r, (int, float)) and r < 0 and r > self.min_rssi

        def _q(name, n):
            """派活排序质量: rssi_ema<0用校准RSSI(新固件路径);
            旧固件无广播RSSI → 广播包数映射伪RSSI(adv-100, 仅同批固件内可比,
            包数越多越接近0=越强; 绝对值域与真实RSSI不互通但排序语义一致)"""
            if _has_rssi(n):
                return n["rssi_ema"] - self._biases.get(name, 0)
            return min(int(n.get("adv_delta") or 0), 99) - 100

        cands = [(name, n) for name, n in self._nodes.items()
                 if n.get("state") == "idle" and (n.get("ws") or n.get("local"))
                 and (_has_rssi(n) or (n.get("adv_delta") or 0) > 0)
                 and _hearing(n) and not _cooling(n)]
        if not cands:
            # 全员冷却中: 降权是软惩罚, 有数据源比没数据源重要, 放宽冷却再选(在场校验不放宽)
            cands = [(name, n) for name, n in self._nodes.items()
                     if n.get("state") == "idle" and (n.get("ws") or n.get("local"))
                     and (_has_rssi(n) or (n.get("adv_delta") or 0) > 0)
                     and _hearing(n)]
        if not cands:
            return None
        cands.sort(key=lambda kv: (kv[1].get("fail_count", 0), -_q(kv[0], kv[1])))
        return cands[0][0]

    def _issue_connect(self, name):
        mac = self._band_mac()
        if not mac:
            if not self._warned_no_mac:
                self._warned_no_mac = True
                logger.warning("RelayHub 未配置手环(last_selected_device), 无法下发连接命令")
            return
        self._warned_no_mac = False
        node = self._nodes.get(name)
        if not node or (not node.get("ws") and not node.get("local")):
            return
        node["state"] = "connecting"
        node["connect_deadline"] = time.time() + CONNECT_DEADLINE_S
        self.pending_connect = name
        self._send_to(name, {"type": "connect", "mac": mac})
        logger.info(f"RelayHub 下发连接命令 -> {name} (mac={mac})")

    def _nodes_heard(self, now):
        """是否有WS节点当前窗口内听到手环广播(PC兜底让位判定用)。
        2026-09-29放宽: 现役固件hb.rssi恒为0, band_heard_ts由广播包计数驱动,
        rssi_ema条件会让让位永不触发(PC直连霸占手环)"""
        with self._lock:
            return any(
                now - n.get("band_heard_ts", 0) < BAND_HEARD_WINDOW_S
                for n in self._nodes.values() if n.get("ws"))

    def pc_fallback_evt(self, ok, why=""):
        """PC兜底连接结果回报(开关关闭、PC未注册为节点时; 节点模式走pc_report_evt)"""
        if ok:
            self._fb_cmd_ts = time.time()
            logger.info("RelayHub PC兜底连接已建立")
        else:
            logger.warning(f"RelayHub PC兜底连接失败: {why or '?'}")

    def _decide(self):
        self._notify_source()   # 数据源状态变化推送(2s仲裁周期即节流)
        now = time.time()
        # 僵尸连接清理(必须最先执行): 标定冻结/探测推进/节点全下线等任何早退路径
        # 都不能让pending_connect滞留, 否则接收端永远显示"切换中"(2026-09-29 实锤)
        with self._lock:
            for nname, n in self._nodes.items():
                if n.get("state") == "connecting" and (n.get("ws") or n.get("local")) \
                        and 0 < n.get("connect_deadline", 0) <= now:
                    n["state"] = "idle"
                    logger.warning(f"RelayHub 节点 {nname} connect超时未回报, 重置为idle")
            if self.pending_connect:
                pn = self._nodes.get(self.pending_connect)
                if not pn or pn.get("state") != "connecting":
                    self.pending_connect = None
        # 标定进行中: 冻结仲裁, 节点调度全权交给标定协程(避免抢占/误切换)
        if self._calib and self._calib.get("running"):
            return

        # 0) PC直连互斥: PC连着手环 → 节点必须放手(永不并存)
        if self._direct_now():
            if self.active_node:
                self._send_to(self.active_node, {"type": "disconnect"})
                logger.info("RelayHub PC直连生效, 命令节点释放手环")
                self.active_node = None
            # 兜底让位: 手环是中枢派PC去兜底连的(非手动直连), 且节点已持续重新听到
            # 手环 → PC放手交还节点(带冷却, 防信号边缘"断连-兜底"循环)
            if now - self._fb_cmd_ts < PC_FB_HOLD_S:
                if self._nodes_heard(now):
                    self._fb_heard_streak += 1
                else:
                    self._fb_heard_streak = 0
                if self._fb_heard_streak >= PC_FB_HEARD_STREAK \
                        and now - self._fb_release_ts >= PC_FB_RELEASE_COOLDOWN_S:
                    logger.info(f"RelayHub 节点已持续{self._fb_heard_streak}周期听到手环, "
                                f"PC兜底让位(交还节点)")
                    self._fb_release_ts = now
                    self._fb_heard_streak = 0
                    try:
                        self._pc_bridge.on_command({"type": "disconnect"})
                    except Exception as e:
                        logger.warning(f"RelayHub PC兜底让位命令下发失败: {e}")
            return

        # 0.5) PC兜底(开关关闭): 全体节点持续失聪(无人持有且窗口内都听不到广播)
        #      → PC兜底连接; 节点恢复后由step0的让位逻辑交还
        if not self.pc_as_node and self._pc_bridge is not None:
            st = self.band_status()
            if st.get("state") == "lost":
                self._fb_lost_streak += 1
            else:
                self._fb_lost_streak = 0
            if self._fb_lost_streak >= PC_FB_AFTER_CYCLES \
                    and now - self._fb_last_try >= PC_FB_RETRY_S:
                mac = self._band_mac()
                if mac:
                    self._fb_last_try = now
                    logger.info(f"RelayHub 全部节点{self._fb_lost_streak}周期未听到手环, "
                                f"PC兜底连接(mac={mac})")
                    try:
                        self._pc_bridge.on_command({"type": "connect", "mac": mac})
                    except Exception as e:
                        logger.warning(f"RelayHub PC兜底命令下发失败: {e}")

        if not self._nodes:
            return

        # 1) 探测状态机推进(不受报警冻结影响的机械推进, 决策点受冻结)
        if self.probe_stage == "disconnecting":
            if not self.active_node or now > self.probe_deadline:
                # 已断开(或超时): 开窗扫描
                self._scan_results = {}
                self._broadcast({"type": "scan", "dur": 1000})
                self.probe_stage = "scanning"
                self.probe_deadline = now + PROBE_SCAN_S
            return
        if self.probe_stage == "scanning":
            if now > self.probe_deadline:
                self._resolve_probe()
            return

        # 2) 无数据源 → 空闲自动申请
        # (connect超时僵尸清理已前移到_decide入口, 见函数开头)
        with self._lock:
            active = self._nodes.get(self.active_node) if self.active_node else None
        if not active or (not active.get("ws") and not active.get("local")):
            if self.active_node:
                self.active_node = None   # 持有者失联, 回收
            self.low_streak = 0
            self.no_better_hold = False   # 持有权易主/回收, 保持模式解除
            if not self._any_connecting():
                best = self._best_idle()
                if best:
                    self._issue_connect(best)
            return

        # 3) 接管判定: 持有者心跳失联(WS离线清理兜底之外的兜底; 按心跳而非数据判定,
        #    避免"手环停止测量心率"时在节点间反复切换)
        stale = self.stale_seconds
        if now - active.get("last_seen", 0) > stale:
            logger.warning(f"RelayHub 节点 {self.active_node} 超过{stale}s无心跳, 接管")
            self._send_to(self.active_node, {"type": "disconnect"})
            self.active_node = None
            return

        # 4) 报警冻结: 只保数据连续, 不做质量切换
        if self._alarm_now():
            return

        # 5) 迟滞切换判定: 持有者RSSI跌破阈值连续N个周期
        # 连接进行中不评估: 命令式断开固件不上报ble_down, conn_rssi残留旧值,
        # 此时会用陈旧读数继续累计low_streak并二次触发探测, 打断在途的connect(日志表现为"连续4周期")
        if self.pending_connect or self._any_connecting():
            return
        conn_rssi = active.get("conn_rssi") or active.get("rssi_ema") or 0

        # 4.5) 心率包率触发(P1-7): RSSI正常但心率流断流(链路卡死/深衰落丢包),
        #      RSSI看不出来的病。放在保持模式之前: 包率断流是链路级疾病,
        #      值得打破"RSSI回升才解除"的保持; 探测无更优则回连重开链路自愈。
        #      防循环: ble_up后warmup保护 + 触发后10分钟冷却(手环真没测量时最坏~11分钟一轮)
        if conn_rssi and conn_rssi >= self.threshold_drop \
                and active.get("state") == "active" \
                and now - self._hr_probe_ts >= HR_PROBE_COOLDOWN_S \
                and now - (active.get("connected_at") or 0) >= HR_RATE_WARMUP_S:
            cutoff = now - HR_RATE_WINDOW
            self._hr_rate = [t for t in self._hr_rate if t > cutoff]
            if len(self._hr_rate) < HR_RATE_LOW:
                self._hr_low_streak += 1
                if self._hr_low_streak >= HR_RATE_STREAK:
                    logger.info(f"RelayHub 触发探测切换: {self.active_node} 心率包率过低"
                                f"({len(self._hr_rate)}包/{HR_RATE_WINDOW:.0f}s, 期望~60) "
                                f"连续{self._hr_low_streak}周期")
                    self._hr_probe_ts = now
                    self._hr_low_streak = 0
                    self.probe_stage = "disconnecting"
                    self.probe_deadline = now + PROBE_DISCONNECT_S
                    self.probe_pre_rssi = conn_rssi
                    self.probe_old_name = self.active_node
                    self._send_to(self.active_node, {"type": "disconnect"})
                    return
            else:
                self._hr_low_streak = 0

        # 保持模式: 已探测确认当前持有者就是最优, RSSI回升越过阈值前不再因弱信号探测
        # (真正断开走ble_down→空闲申请, 心跳失联走接管, 均不受保持影响)
        if self.no_better_hold:
            if conn_rssi and conn_rssi >= self.threshold_drop:
                self.no_better_hold = False
                logger.info(f"RelayHub 节点 {self.active_node} 信号回升至{conn_rssi}, 解除保持模式")
            else:
                return
        if conn_rssi and conn_rssi < self.threshold_drop:
            self.low_streak += 1
            if self.low_streak >= max(self.freeze_cycles, 1):
                logger.info(f"RelayHub 触发探测切换: {self.active_node} rssi={conn_rssi}"
                            f" 连续{self.low_streak}周期低于{self.threshold_drop}")
                self.probe_stage = "disconnecting"
                self.probe_deadline = now + PROBE_DISCONNECT_S
                self.probe_pre_rssi = conn_rssi
                self.probe_old_name = self.active_node
                self._send_to(self.active_node, {"type": "disconnect"})
        else:
            self.low_streak = 0

    def _resolve_probe(self):
        """探测窗收口: 只有"其他节点"强于原持有者hysteresis_db以上才切, 否则回连原持有者并进入保持模式
        双轨规则: min_rssi门槛用原始RSSI, 强弱比较双方都用校准值 raw - bias"""
        self.probe_stage = None
        old_name = self.probe_old_name
        old_cal = (self.probe_pre_rssi - self._biases.get(old_name, 0)
                   if self.probe_pre_rssi else 0)
        best_name, best_raw, best_cal = None, 0, 0
        now = time.time()
        for name, r in self._scan_results.items():
            if r < 0 and r > self.min_rssi:   # 门槛: 原始链路质量
                # P1-2降权: 30s内失败≥2次的候选暂缓(否则信号边缘会"探测→派活→再失败"循环)
                n = self._nodes.get(name) or {}
                if n.get("fail_count", 0) >= 2 and now - (n.get("last_fail_ts") or 0) < FAIL_COOLDOWN_S:
                    continue
                cal = r - self._biases.get(name, 0)
                if best_name is None or cal > best_cal:   # 排名: 校准值
                    best_name, best_raw, best_cal = name, r, cal
        self._scan_results = {}
        # 候选必须是"别的节点"且校准值强于原持有者hysteresis_db以上才切换
        # (候选=原持有者自己时不可比: 连接态RSSI与广播RSSI量纲差~10dB, 直接比较必然"显著更优",
        #  会自己切自己, 造成断连→重连死循环)
        if best_name and best_name != old_name and self.probe_pre_rssi \
                and best_cal >= old_cal + self.hysteresis_db:
            logger.info(f"RelayHub 漫游切换: {old_name}({self.probe_pre_rssi}) -> "
                        f"{best_name}({best_raw}, 校准{best_cal:.0f})")
            self._issue_connect(best_name)
            self._probe_cleanup()
            return
        if best_name and best_name != old_name:
            logger.info(f"RelayHub 探测无显著更优(候选{best_name}校准{best_cal:.0f} vs "
                        f"原持有者校准{old_cal:.0f}), 回连原持有者")
        elif best_name == old_name:
            logger.info(f"RelayHub 探测确认: 最优仍是原持有者 {old_name}"
                        f"(广播校准{best_cal:.0f}), 回连并进入保持模式")
        else:
            logger.info(f"RelayHub 探测无候选达标(≥{self.min_rssi}), 回连原持有者 {old_name}")
        self._issue_connect(old_name)  # 回连原持有者
        # 保持模式: 本次探测已确认现状即最优; 连接态RSSI低于阈值在该环境可能长期成立,
        # 无保持会形成"弱信号→探测→无更优→回连"死循环, 每30s白断连一次
        self.no_better_hold = True
        self._probe_cleanup()

    def _probe_cleanup(self):
        """探测收尾: 清origin残值(2026-09-29)——probe_old_name不清会永远顶着旧来源名,
        后续任何"切换中"显示都误标成早已失效的旧持有者(实锤: 客厅备用→主卧室冻结显示)"""
        self.probe_old_name = None
        self.probe_pre_rssi = 0


# ---------------- 模块级接口 ----------------

_hub = None


def get_hub():
    global _hub
    if _hub is None:
        _hub = RelayHub()
    return _hub


def start_relay_hub():
    hub = get_hub()
    r = hub.start()
    try:  # v1.2.19: mDNS注册供ESP32节点自动发现(失败降级, 不影响主链路)
        from mdns_announce import start_hub_announce
        start_hub_announce(hub.port)
    except Exception:
        pass
    return r


def stop_relay_hub():
    try:  # 退出时注销mDNS服务
        from mdns_announce import stop_hub_announce
        stop_hub_announce()
    except Exception:
        pass
    get_hub().stop()


def set_hr_callback(fn):
    get_hub().set_hr_callback(fn)


def set_alarm_check(fn):
    get_hub().set_alarm_check(fn)


def set_direct_check(fn):
    get_hub().set_direct_check(fn)


def set_source_callback(fn):
    get_hub().set_source_callback(fn)


def set_band_status_callback(fn):
    get_hub().set_band_status_callback(fn)


def node_table():
    return get_hub().node_table()


def start_calibration(rounds=15, scan_ms=800):
    return get_hub().start_calibration(rounds, scan_ms)


def calibration_status():
    return get_hub().calibration_status()


def get_biases():
    return get_hub().get_biases()


def clear_biases():
    return get_hub().clear_biases()


def hub_running():
    h = _hub
    return bool(h and h._thread and h._thread.is_alive())
