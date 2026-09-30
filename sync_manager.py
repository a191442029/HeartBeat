# sync_manager.py — EXE 与安卓中枢(HRHub)的双向参数同步 (v1.2.20 起)
# 与安卓侧 SyncManager.java 完全对称(规则一一对应, 任一侧改动须两端同步检查):
# - 发现: zeroconf 浏览 _hrmlink._tcp, 排除本端注册名 "HRMLink"(精确比较——
#   contains 会把安卓的 "HRMLink-Hub" 一并误杀)与本机 IP
# - 通道: GET  /api/settings = 全量快照(对账); POST /api/settings = 接收同步包
#   挂在 webpush_server(数据服务)上; 端口口径 = 本机 [Tailscale] port
#   (安卓侧用其 srv_port, 默认同为 8765; mDNS 只取对端 IP, 端口用本机口径)
# - 鉴权: Bearer token([sync] sync_token, 两端各存同值, 默认 HRMLink-Sync-2025)
# - 冲突: LWW —— (settings_rev, ts) 字典序高者胜; 本地改动批次 rev+1;
#   应用远端包后不回发(_applying_remote 抑制写路径回调, 防回环)
# - 本地出口: system_utils 落盘回调(save_settings/ups) → 差分检测 → rev+1 → 推送
#   (UI 连续保存由 1.5s 去抖合并; 推送失败基线不更新, 待下轮改动/对账重推;
#    [sync] 节自身的写入不触发差分, 防推启风暴)
# - 离线兜底: 对端发现时 + 每 300s 握手对账, rev 低者拉取高者全量
# - 生效: 应用远端包后经回调桥通知 GUI 线程热重载(notifier.load_config / 相机流
#   重建 / 中继信号偏差); MQTT/InfluxDB 连接与中继仲裁参数属重启生效项(低频)
import json
import socket
import threading
import time
import urllib.request

from system_utils import gs, ups, logger

MDNS_TYPE = "_hrmlink._tcp.local."
SELF_SERVICE_NAME = "HRMLink"          # 本端 mdns_announce 注册的实例名(精确排除自己)
DEFAULT_TOKEN = "HRMLink-Sync-2025"
HANDSHAKE_INTERVAL = 300.0             # 对账周期(秒)
DEBOUNCE = 1.5                         # 本地改动去抖(秒), 合并UI连续保存
LOOP_STEP = 5.0                        # 守护线程步进(秒)

# 白名单 44 键: 安卓键 → (EXE section, option, 类型 i/b/s)
# 判定标准: 业务语义(对人的策略/环境事实)同步; 本机硬件网络/本机角色/凭证不同步
KEY_MAP = {
    # ---- 告警规则(Push) ----
    "push_max_hr":            ("Push", "max_hr", "i"),
    "push_min_hr":            ("Push", "min_hr", "i"),
    "push_abnormal_duration": ("Push", "abnormal_duration", "i"),
    "push_cooldown_seconds":  ("Push", "cooldown_seconds", "i"),
    "alarm_seconds":          ("Push", "alarm_seconds", "i"),
    "local_alarm_enabled":    ("Push", "alarm_local_enabled", "b"),
    "alarm_remote_enabled":   ("Push", "alarm_remote_enabled", "b"),
    "push_periods":           ("Push", "periods", "s"),
    # ---- 心律不齐 ----
    "irr_enabled":            ("Push", "irregular_enabled", "b"),
    "irr_window_seconds":     ("Push", "irregular_window_seconds", "i"),
    "irr_sd_threshold":       ("Push", "irregular_sd_threshold", "i"),
    "irr_jump_bpm":           ("Push", "irregular_jump_bpm", "i"),
    "irr_jump_ratio_pct":     ("Push", "irregular_jump_ratio_pct", "i"),
    "irr_rest_max_hr":        ("Push", "irregular_rest_max_hr", "i"),
    "irr_sustain_windows":    ("Push", "irregular_sustain_windows", "i"),
    "irr_cooldown_minutes":   ("Push", "irregular_cooldown_minutes", "i"),
    # ---- 推送渠道(Bark/ntfy/MeoW; bark_device_key/ntfy_token 凭证不入表) ----
    "bark_enabled":           ("Push", "bark_enabled", "b"),
    "bark_server":            ("Push", "bark_server", "s"),
    "bark_level":             ("Push", "bark_level", "s"),
    "bark_sound":             ("Push", "bark_sound", "s"),
    "bark_group":             ("Push", "bark_group", "s"),
    "ntfy_enabled":           ("Push", "ntfy_enabled", "b"),
    "ntfy_topic":             ("Push", "ntfy_topic", "s"),
    "ntfy_server":            ("Push", "ntfy_server", "s"),
    "ntfy_priority":          ("Push", "ntfy_priority", "i"),
    "ntfy_tags":              ("Push", "ntfy_tags", "s"),
    "meow_enabled":           ("Push", "meow_enabled", "b"),
    "meow_nickname":          ("Push", "meow_nickname", "s"),
    # ---- ESP32 中继(仲裁参数/信号标定/房间绑定; relay_token 已固化节点固件不入表) ----
    "relay_threshold_drop":   ("esp32_relay", "threshold_drop", "i"),
    "relay_hysteresis_db":    ("esp32_relay", "hysteresis_db", "i"),
    "relay_min_rssi":         ("esp32_relay", "min_rssi", "i"),
    "relay_stale_seconds":    ("esp32_relay", "stale_seconds", "i"),
    "relay_freeze_cycles":    ("esp32_relay", "freeze_cycles", "i"),
    "relay_node_biases":      ("esp32_relay", "node_biases", "s"),
    "room_camera_map":        ("esp32_relay", "room_camera_map", "s"),
    # ---- 摄像头清单(含URL凭证, 用户拍板同步) ----
    "cameras_json":           ("Camera", "cameras", "s"),
    # ---- MQTT(环境事实; password/client_id 本机专有不入表) ----
    "mqtt_broker":            ("MQTT", "broker", "s"),
    "mqtt_port":              ("MQTT", "port", "i"),
    "mqtt_username":          ("MQTT", "username", "s"),
    "mqtt_topic":             ("MQTT", "topic", "s"),
    "mqtt_discovery_topic":   ("MQTT", "discovery_topic", "s"),
    "mqtt_discovery_enabled": ("MQTT", "discovery_enabled", "b"),
    # ---- InfluxDB(环境事实; token 本机专有不入表) ----
    "influx_url":             ("InfluxDB", "url", "s"),
    "influx_org":             ("InfluxDB", "org", "s"),
    "influx_bucket":          ("InfluxDB", "bucket", "s"),
}


def _is_local_ip(ip: str) -> bool:
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if info[4][0] == ip:
                return True
    except Exception:
        pass
    return False


# int 键的快照回退默认(对齐安卓侧 Prefs 默认值): EXE 未配置过该键时,
# 快照推安卓默认而非 0, 避免首次全量同步把对端合法默认冲成非法值
_INT_DEFAULTS = {
    "alarm_seconds": 10, "mqtt_port": 1883,
    "relay_threshold_drop": -75, "relay_hysteresis_db": 10, "relay_min_rssi": -80,
    "relay_stale_seconds": 30, "relay_freeze_cycles": 3,
    "irr_window_seconds": 300, "irr_sd_threshold": 50, "irr_jump_bpm": 5,
    "irr_jump_ratio_pct": 100, "irr_rest_max_hr": 200, "irr_sustain_windows": 2,
    "irr_cooldown_minutes": 10,
}


class _SyncListener:
    """zeroconf 浏览回调(zeroconf线程): 只做轻量筛选/记录, 网络重活交守护线程"""

    def __init__(self, mgr):
        self.mgr = mgr

    @staticmethod
    def _short(name):
        return (name or "").split(".")[0]

    def add_service(self, zc, type_, name):
        if self._short(name) == SELF_SERVICE_NAME:  # 精确排除自己
            return
        try:
            info = zc.get_service_info(type_, name, 2000)
        except Exception:
            return
        if not info or not info.addresses:
            return
        ip = socket.inet_ntoa(info.addresses[0])
        if _is_local_ip(ip):
            return                                  # 另一网卡上的自己
        mgr = self.mgr
        if mgr._peer_host != ip:
            mgr._peer_host = ip
            mgr._last_handshake = 0.0
            mgr._wake.set()
            logger.info(f"[Sync] 发现同步对端(安卓中枢): {ip}")

    def remove_service(self, zc, type_, name):
        if self._short(name) == SELF_SERVICE_NAME:
            return
        self.mgr._peer_host = None
        logger.info("[Sync] 同步对端下线, 回到重发现循环")

    def update_service(self, zc, type_, name):
        pass


class SyncManager:

    def __init__(self):
        self._started = False
        self._stop_flag = False
        self._thread = None
        self._op_lock = threading.Lock()      # 串行化 LWW 读改写(守护线程/HTTP线程)
        self._dirty = threading.Event()       # 本地改动待推送标记
        self._wake = threading.Event()        # 提前唤醒守护线程(发现对端/标脏)
        self._applying_remote = False         # 应用远端包中: 抑制写路径回调防回环
        self._peer_host = None                # 对端安卓中枢 IP(None=未发现)
        self._last_handshake = 0.0
        self._zc = None
        self._browser = None
        self._effect_cb = None                # 远端包应用后的GUI生效回调(MainWindow注入emit, 线程安全)

    # ---------- 生命周期(随 webpush 数据服务启停, start 幂等) ----------

    def start(self):
        with self._op_lock:
            if self._started:
                return
            self._started = True
            self._stop_flag = False
        self._ensure_sync_section()
        self._ensure_baseline()
        self._start_discovery()
        self._thread = threading.Thread(target=self._run, daemon=True, name="settings-sync")
        self._thread.start()
        logger.info("[Sync] 参数同步已启动(mDNS _hrmlink._tcp 发现安卓中枢)")

    def stop(self):
        with self._op_lock:
            if not self._started:
                return
            self._started = False
        self._stop_flag = True
        self._wake.set()
        self._stop_discovery()
        logger.info("[Sync] 参数同步已停止")

    def set_effect_callback(self, fn):
        """MainWindow 注入远端参数生效回调(Qt信号emit, 可跨线程调用)"""
        self._effect_cb = fn

    # ---------- 本地改动统一出口(system_utils 落盘回调, 任意线程可调) ----------

    def notify_local_change(self, section=None):
        if self._applying_remote or not self._started:
            return
        if section == "sync":
            return  # rev/ts/基线自身写入不再触发差分, 防推启风暴
        self._dirty.set()
        self._wake.set()

    # ---------- HTTP 同步路由(webpush_server 调用) ----------

    def sync_token(self):
        return gs("sync", "sync_token", DEFAULT_TOKEN, str, "-Sync") or DEFAULT_TOKEN

    def snapshot_json(self):
        """GET /api/settings: 全量快照(对账用); 服务未跑返回 None → 503"""
        if not self._started:
            return None
        with self._op_lock:
            try:
                return json.dumps({
                    "settings_rev": self._get_rev(),
                    "ts": self._get_ts(),
                    "settings": self._snapshot(),
                }, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"[Sync] 快照生成失败: {e}")
                return None

    def apply_packet_json(self, body):
        """POST /api/settings: 接收远端同步包(LWW 仲裁), 返回结果+本端 rev"""
        if not self._started:
            return '{"ok":false,"err":"service stopped"}'
        with self._op_lock:
            try:
                pkt = json.loads(body)
                if not isinstance(pkt, dict):
                    return '{"ok":false,"err":"bad packet"}'
                result = self._apply_packet(pkt)
                return json.dumps({
                    "ok": result in ("applied", "stale"),  # stale=旧包重放, 幂等成功
                    "result": result,
                    "settings_rev": self._get_rev(),
                })
            except Exception:
                return '{"ok":false,"err":"bad packet"}'

    # ---------- 守护线程调度 ----------

    def _run(self):
        while not self._stop_flag:
            self._wake.wait(LOOP_STEP)
            self._wake.clear()
            if self._stop_flag:
                break
            if self._dirty.is_set():
                time.sleep(DEBOUNCE)            # 合并UI连续保存的落盘风暴
                self._dirty.clear()
                with self._op_lock:
                    self._push_if_changed()
            if self._peer_host and time.time() - self._last_handshake > HANDSHAKE_INTERVAL:
                with self._op_lock:
                    self._handshake()

    # ---------- LWW 核心 ----------

    def _push_if_changed(self):
        if not self._differs_from_baseline():
            return
        rev = self._get_rev() + 1
        ts = int(time.time() * 1000)
        ups("sync", "settings_rev", rev, "参数同步")
        ups("sync", "sync_ts", ts, "参数同步")
        if self._push(rev, ts):
            self._store_baseline()
        else:
            logger.info(f"[Sync] 参数变更已记 rev={rev}, 推送失败/对端未在线, 待对账补推")

    def _apply_packet(self, pkt):
        """应用远端同步包: (rev, ts) 字典序高者胜; 应用后不回发; 类型不符的键丢弃"""
        try:
            rev = int(pkt.get("settings_rev", 0) or 0)
            ts = int(pkt.get("ts", 0) or 0)
            my_rev, my_ts = self._get_rev(), self._get_ts()
            if rev < my_rev or (rev == my_rev and ts <= my_ts):
                return "stale"
            settings = pkt.get("settings")
            if not isinstance(settings, dict):
                return "bad"
            applied = 0
            self._applying_remote = True
            try:
                for key, val in settings.items():
                    mapping = KEY_MAP.get(key)
                    if mapping is None:
                        continue                # 专有键不在白名单, 丢弃
                    sec, opt, t = mapping
                    try:
                        # bool 是 int 子类, 必须先排除再判 int
                        if t == "i" and isinstance(val, int) and not isinstance(val, bool):
                            ups(sec, opt, int(val), "参数同步")
                        elif t == "b" and isinstance(val, bool):
                            ups(sec, opt, bool(val), "参数同步")
                        elif t == "s" and isinstance(val, str):
                            ups(sec, opt, val, "参数同步")
                        else:
                            continue            # 类型不符, 跳过不整包拒绝
                        applied += 1
                    except Exception:
                        continue
                ups("sync", "settings_rev", rev, "参数同步")
                ups("sync", "sync_ts", ts, "参数同步")
                self._store_baseline()          # 防止被误判为本地改动再次推送
            finally:
                self._applying_remote = False
            logger.info(f"[Sync] 已应用远端参数 {applied} 项(rev={rev})")
            self._fire_effect()
            return "applied"
        except Exception as e:
            self._applying_remote = False
            logger.warning(f"[Sync] 应用远端参数失败: {e}")
            return "error"

    def _fire_effect(self):
        cb = self._effect_cb
        if cb is None:
            return
        try:
            cb()
        except Exception as e:
            logger.warning(f"[Sync] 远端参数生效回调失败: {e}")

    # ---------- 对账握手 ----------

    def _handshake(self):
        peer = self._peer_host
        if not peer:
            return
        self._last_handshake = time.time()
        resp = self._http_json("GET", peer, None)
        if not isinstance(resp, dict):
            return
        try:
            peer_rev = int(resp.get("settings_rev", 0) or 0)
            peer_ts = int(resp.get("ts", 0) or 0)
        except Exception:
            return
        my_rev, my_ts = self._get_rev(), self._get_ts()
        if peer_rev > my_rev or (peer_rev == my_rev and peer_ts > my_ts):
            self._apply_packet(resp)            # 低者拉取高者全量
        elif peer_rev < my_rev or self._differs_from_baseline():
            self._push(my_rev, self._get_ts() or int(time.time() * 1000))  # 我更新 → 强推

    # ---------- 快照与基线 ----------

    def _snapshot(self):
        snap = {}
        for key, (sec, opt, t) in KEY_MAP.items():
            if t == "i":
                snap[key] = int(gs(sec, opt, _INT_DEFAULTS.get(key, 0), int, "-Sync") or 0)
            elif t == "b":
                snap[key] = bool(gs(sec, opt, False, bool, "-Sync"))
            else:
                snap[key] = str(gs(sec, opt, "", str, "-Sync") or "")
        return snap

    def _get_baseline(self):
        try:
            base = json.loads(gs("sync", "baseline_json", "{}", str, "-Sync") or "{}")
            return base if isinstance(base, dict) else {}
        except Exception:
            return {}

    def _store_baseline(self):
        ups("sync", "baseline_json", json.dumps(self._snapshot(), ensure_ascii=False), "参数同步")

    def _differs_from_baseline(self):
        base = self._get_baseline()
        cur = self._snapshot()
        if set(base.keys()) != set(cur.keys()):
            return True
        for k, v in cur.items():
            bv = base.get(k)
            if type(bv) is not type(v) or bv != v:  # type()严格比较: bool/int不可混
                return True
        return False

    def _get_rev(self):
        return int(gs("sync", "settings_rev", 0, int, "-Sync") or 0)

    def _get_ts(self):
        try:
            return int(gs("sync", "sync_ts", 0, int, "-Sync") or 0)
        except Exception:
            return 0

    def _ensure_sync_section(self):
        if not gs("sync", "sync_token", "", str, "-Sync"):
            ups("sync", "sync_token", DEFAULT_TOKEN, "参数同步")

    def _ensure_baseline(self):
        if not gs("sync", "baseline_json", "", str, "-Sync"):
            self._store_baseline()              # 首次: 以当前配置为基线(之后靠差分)

    # ---------- 网络 ----------

    def _push(self, rev, ts):
        peer = self._peer_host
        if not peer:
            logger.info(f"[Sync] 参数变更已记 rev={rev}, 对端未在线, 待发现后对账补推")
            return False
        body = json.dumps({
            "settings_rev": rev, "ts": ts, "settings": self._snapshot(),
        }, ensure_ascii=False).encode("utf-8")
        resp = self._http_json("POST", peer, body)
        ok = bool(resp and resp.get("ok"))
        logger.info(f"[Sync] 参数推送 → {peer} {'成功' if ok else '被拒(对端更新)或失败'}")
        return ok

    def _http_json(self, method, host, body):
        # 与对端 HTTP 数据服务同端口口径(本机 [Tailscale] port; 安卓侧用其 srv_port, 默认同为 8765)
        try:
            port = int(gs("Tailscale", "port", 8765, int, "-Sync") or 8765)
            req = urllib.request.Request(
                f"http://{host}:{port}/api/settings", method=method,
                headers={"Authorization": "Bearer " + self.sync_token(),
                         "Content-Type": "application/json"})
            if body:
                req.data = body
            with urllib.request.urlopen(req, timeout=4) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            logger.debug(f"[Sync] HTTP {method} {host} 失败: {e}")
            return None

    # ---------- mDNS 发现 ----------

    def _start_discovery(self):
        try:
            from zeroconf import ServiceBrowser, Zeroconf
        except ImportError:
            logger.info("[Sync] zeroconf 未安装, 参数同步无发现通道(不启用)")
            return
        try:
            self._zc = Zeroconf()
            self._browser = ServiceBrowser(self._zc, MDNS_TYPE, _SyncListener(self))
        except Exception as e:
            logger.warning(f"[Sync] mDNS 发现启动失败: {e}")

    def _stop_discovery(self):
        try:
            if self._zc:
                self._zc.close()
        except Exception:
            pass
        self._zc = None
        self._browser = None


_instance = None


def get() -> SyncManager:
    global _instance
    if _instance is None:
        _instance = SyncManager()
    return _instance
