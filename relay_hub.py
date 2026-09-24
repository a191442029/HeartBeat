# -*- coding: utf-8 -*-
"""
relay_hub.py — ESP32 全屋心率中继中枢 (v1.1.0)

架构: 独立线程 + 自有 asyncio 事件循环 + aiohttp WS server(路由 /relay)
节点协议: 详见 ESP32/设计决策.md §6
  节点→EXE: hello{name,fw,ip} / hb{state,rssi,up} / hr{bpm,rssi,ts}
            scan{rssi} / evt{evt: ble_up|ble_down|ble_fail|no_band_mac|name_ok|apply_ok}
  EXE→节点: cfg{mac} / connect{mac} / disconnect / scan{dur} / set{name} / apply{...} / reboot

仲裁时机模型(两个永不 + 三类触发):
  - 永不: 节点自主连接(连接权只在EXE); 报警期间质量切换(数据中断接管仍放行)
  - 空闲自动申请: 无数据源 → 选平滑RSSI最强且>min_rssi的节点
  - 探测式切换: 持有者RSSI<threshold 连续freeze_cycles个周期 → disconnect
                → broadcast scan(1s窗口) → 最强者需强于原持有者hysteresis_db以上
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
HR_MIN_INTERVAL = 4.5       # 中枢侧心率节流(秒), 与固件5s双保险
EMA_ALPHA = 0.4             # RSSI指数平滑系数(≈最近10次)
PROBE_DISCONNECT_S = 3.0    # 探测·断开阶段超时
PROBE_SCAN_S = 2.5          # 探测·扫描窗等待(节点1s扫描+回包余量)
CONNECT_DEADLINE_S = 18.0   # connect命令总超时(与固件15s对齐+余量)


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

        # 节点表: name -> dict(ws, ip, fw, state, rssi_ema, conn_rssi,
        #                      last_seen, last_data_ts, last_evt_ts,
        #                      connected_at, connect_deadline, fail_count)
        self._nodes = {}
        self._lock = threading.Lock()

        # 仲裁状态
        self.active_node = None       # 当前持有手环连接的节点名
        self.pending_connect = None   # 已下发connect待ble_up确认的节点名
        self.low_streak = 0           # 持有者弱信号连续周期数
        self.probe_stage = None       # None/'disconnecting'/'scanning'
        self.probe_deadline = 0.0
        self.probe_pre_rssi = 0       # 探测前持有者RSSI
        self.probe_old_name = None
        self._scan_results = {}       # 探测窗内 name->rssi

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
        self._load_biases()   # 重启hub时重载偏差(保存设置后立即生效)
        self._thread = threading.Thread(target=self._run, daemon=True, name="relay-hub")
        self._thread.start()
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
        """仲裁器: 每2秒决策一次(时机模型见模块docstring)"""
        while True:
            try:
                await asyncio.sleep(ARB_INTERVAL)
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
            with self._lock:
                old = self._nodes.get(nname)
                if old and old.get("ws") and not old["ws"].closed and old.get("ws") is not wsr:
                    # 重名冲突: 拒绝新节点(节点名是仲裁与UI的唯一标识)
                    asyncio.ensure_future(self._safe_send(wsr, {"type": "evt", "evt": "name_conflict"}))
                    asyncio.ensure_future(wsr.close())
                    return name
                self._nodes[nname] = {
                    "ws": wsr, "ip": peer, "fw": str(data.get("fw", "")),
                    "state": "idle", "rssi_ema": 0, "conn_rssi": 0,
                    "last_seen": time.time(), "last_data_ts": 0, "last_evt_ts": time.time(),
                    "connected_at": 0, "connect_deadline": 0, "fail_count": 0,
                    "last_hr_ts": 0,
                }
            if name and self.active_node == name and name != nname:
                self.active_node = nname  # 极少见: 改名后重连, 保留持有权
            logger.info(f"RelayHub 节点上线: {nname} ({peer}, fw={data.get('fw')})")
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
            if isinstance(r, (int, float)) and r < 0:
                ema = node["rssi_ema"]
                node["rssi_ema"] = r if not ema else ema * (1 - EMA_ALPHA) + r * EMA_ALPHA
                if node["state"] == "active":
                    node["conn_rssi"] = r
                    node["last_data_ts"] = time.time()
        elif mtype == "hr":
            bpm = data.get("bpm") or 0
            if isinstance(bpm, (int, float)) and 0 < bpm <= 250:
                node["conn_rssi"] = data.get("rssi") or node.get("conn_rssi") or 0
                node["last_data_ts"] = time.time()
                node["last_evt_ts"] = time.time()
                if self.active_node in (None, name):
                    self.active_node = name
                if time.time() - node.get("last_hr_ts", 0) >= HR_MIN_INTERVAL:
                    node["last_hr_ts"] = time.time()
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
                self.active_node = name
                self.pending_connect = None
                self.low_streak = 0
                logger.info(f"RelayHub 节点 {name} 已连接手环")
            elif evt == "ble_down":
                node["state"] = "idle"
                node["conn_rssi"] = 0
                if self.active_node == name:
                    logger.info(f"RelayHub 节点 {name} 手环断开, 连接权回收")
                    self.active_node = None
            elif evt == "ble_fail":
                node["state"] = "idle"
                node["fail_count"] += 1
                node["connect_deadline"] = 0
                if self.pending_connect == name:
                    self.pending_connect = None
                logger.warning(f"RelayHub 节点 {name} 连接手环失败({node['fail_count']}次)")
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

    async def _safe_send(self, ws, obj):
        try:
            if ws and not ws.closed:
                await ws.send_str(json.dumps(obj, ensure_ascii=False))
        except Exception:
            pass

    def _send_to(self, name, obj):
        if not self._loop:
            return
        with self._lock:
            node = self._nodes.get(name)
            ws = node.get("ws") if node else None
        if ws:
            asyncio.run_coroutine_threadsafe(self._safe_send(ws, obj), self._loop)

    def _broadcast(self, obj):
        if not self._loop:
            return
        with self._lock:
            sockets = [n.get("ws") for n in self._nodes.values() if n.get("ws")]
        for ws in sockets:
            asyncio.run_coroutine_threadsafe(self._safe_send(ws, obj), self._loop)

    # ---------- 仲裁决策 ----------

    def source_status(self):
        """当前心率数据源状态(接收端通知栏显示用)
        返回: {"phase": active|switching|direct|none, "source": 名字,
               "target": 切换目标(仅switching), "rssi": 负数或0(未知)}"""
        try:
            direct = bool(self._direct_check and self._direct_check())
        except Exception:
            direct = False
        if direct:
            return {"phase": "direct", "source": "PC", "rssi": 0}
        if self.probe_stage or self.pending_connect:
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
        双轨规则: min_rssi门槛用原始rssi_ema, 排名比较用校准值 raw - bias"""
        cands = [(name, n) for name, n in self._nodes.items()
                 if n.get("state") == "idle" and n.get("ws")
                 and n.get("rssi_ema") and n["rssi_ema"] < 0
                 and n["rssi_ema"] > self.min_rssi]
        if not cands:
            return None
        cands.sort(key=lambda kv: (kv[1].get("fail_count", 0),
                                   -(kv[1]["rssi_ema"] - self._biases.get(kv[0], 0))))
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
        if not node or not node.get("ws"):
            return
        node["state"] = "connecting"
        node["connect_deadline"] = time.time() + CONNECT_DEADLINE_S
        self.pending_connect = name
        self._send_to(name, {"type": "connect", "mac": mac})
        logger.info(f"RelayHub 下发连接命令 -> {name} (mac={mac})")

    def _decide(self):
        self._notify_source()   # 数据源状态变化推送(2s仲裁周期即节流)
        # 标定进行中: 冻结仲裁, 节点调度全权交给标定协程(避免抢占/误切换)
        if self._calib and self._calib.get("running"):
            return
        if not self._nodes:
            return
        now = time.time()

        # 0) PC直连互斥: PC连着手环 → 节点必须放手(永不并存)
        if self._direct_now():
            if self.active_node:
                self._send_to(self.active_node, {"type": "disconnect"})
                logger.info("RelayHub PC直连生效, 命令节点释放手环")
                self.active_node = None
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
        with self._lock:
            active = self._nodes.get(self.active_node) if self.active_node else None
        if not active or not active.get("ws"):
            if self.active_node:
                self.active_node = None   # 持有者失联, 回收
            self.low_streak = 0
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
        conn_rssi = active.get("conn_rssi") or active.get("rssi_ema") or 0
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
        """探测窗收口: 候选须强于原持有者hysteresis_db以上才切, 否则回连原持有者
        双轨规则: min_rssi门槛用原始RSSI, 强弱比较双方都用校准值 raw - bias"""
        self.probe_stage = None
        old_name = self.probe_old_name
        old_cal = (self.probe_pre_rssi - self._biases.get(old_name, 0)
                   if self.probe_pre_rssi else 0)
        best_name, best_raw, best_cal = None, 0, 0
        for name, r in self._scan_results.items():
            if r < 0 and r > self.min_rssi:   # 门槛: 原始链路质量
                cal = r - self._biases.get(name, 0)
                if best_name is None or cal > best_cal:   # 排名: 校准值
                    best_name, best_raw, best_cal = name, r, cal
        self._scan_results = {}
        # 候选(校准)须强于原持有者(校准)hysteresis_db以上(数值更大)才切换
        if best_name and self.probe_pre_rssi and best_cal >= old_cal + self.hysteresis_db:
            logger.info(f"RelayHub 漫游切换: {old_name}({self.probe_pre_rssi}) -> "
                        f"{best_name}({best_raw}, 校准{best_cal:.0f})")
            self._issue_connect(best_name)
        else:
            if best_name:
                logger.info(f"RelayHub 探测无显著更优(候选{best_name}校准{best_cal:.0f} vs "
                            f"原持有者校准{old_cal:.0f}), 回连原持有者")
            self._issue_connect(old_name)  # 回连原持有者


# ---------------- 模块级接口 ----------------

_hub = None


def get_hub():
    global _hub
    if _hub is None:
        _hub = RelayHub()
    return _hub


def start_relay_hub():
    return get_hub().start()


def stop_relay_hub():
    get_hub().stop()


def set_hr_callback(fn):
    get_hub().set_hr_callback(fn)


def set_alarm_check(fn):
    get_hub().set_alarm_check(fn)


def set_direct_check(fn):
    get_hub().set_direct_check(fn)


def set_source_callback(fn):
    get_hub().set_source_callback(fn)


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
