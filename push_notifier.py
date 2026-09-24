"""
多渠道手机推送通知模块
支持三个渠道, 可同时启用:
- MeoW (鸿蒙): https://www.chuckfang.com/MeoW/api_doc.html  GET /{昵称}/{title}/{msg}
- Bark  (iOS): GET {server}/{key}/{title}/{msg}             默认服务器 https://api.day.app
- ntfy  (安卓/全平台): GET {server}/{topic}/publish?title=&message=  默认服务器 https://ntfy.sh

设计原则:
- 事件驱动: 仅在心率异常/设备断连等关键事件时推送, 心率正常不产生任何请求
- 冷却机制: 同类告警在冷却期内不重复推送, 避免轰炸和服务端限频
- 网络隔离: 推送在后台线程执行, 失败仅记日志, 绝不阻塞UI或影响心率记录
"""
import json
import os
import re
import time
import datetime
import threading
import urllib.request
import urllib.error
import urllib.parse

from system_utils import logger, gs
from irregularity_detector import IrregularityDetector

MEOW_API = "https://api.chuckfang.com"
BARK_API = "https://api.day.app"
NTFY_API = "https://ntfy.sh"
BARK_LEVELS = ("active", "timeSensitive", "passive", "critical")

# 推送记录: JSON文件存储, 保留最近HISTORY_MAX条(含测试推送), 供"推送记录"页展示
HISTORY_FILE = os.path.join("log", "push_history.json")
HISTORY_MAX = 200
_history_lock = threading.Lock()


# 推送记录变化回调(UI层设置, 用于推送记录表自动刷新; 可能在后台线程被调用, UI层需自行保证线程安全)
on_push_recorded = None

# 报警摄像头剪辑钩子: 由UI层设置, 签名 alarm_ts(str) -> None;
# 心率类报警触发时在后台线程调用(UI层异步剪辑后经 set_alarm_clips 回填片段)
on_camera_alarm_hook = None


def record_push(channel: str, title: str, msg: str, ok: bool, note: str, extra: dict = None):
    """追加一条推送结果并落盘(线程安全, 失败仅记日志)
    extra: 附加字段(如 {"alarm_ts": 报警时刻}), 合并进记录供后续回填/展示"""
    rec = {"time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "channel": channel, "ok": bool(ok),
           "title": title, "msg": str(msg), "note": str(note)}
    if extra:
        rec.update(extra)
    with _history_lock:
        records = load_push_history()
        records.append(rec)
        try:
            os.makedirs(os.path.dirname(HISTORY_FILE) or ".", exist_ok=True)
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(records[-HISTORY_MAX:], f, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存推送记录失败: {e}")
    # 通知UI层刷新推送记录表(回调异常不影响推送本身)
    cb = on_push_recorded
    if cb:
        try:
            cb()
        except Exception as e:
            logger.error(f"推送记录回调执行失败: {e}")


def set_alarm_clips(alarm_ts: str, clips: list):
    """报警剪辑完成后按 alarm_ts 把片段回填到匹配的推送记录(后台线程调用)
    clips: [{"cam": 名称, "mp4": 路径, "jpg": 封面}]; 无匹配记录时静默忽略"""
    hit = False
    with _history_lock:
        records = load_push_history()
        for rec in records:
            if isinstance(rec, dict) and rec.get("alarm_ts") == alarm_ts:
                rec["clips"] = clips
                hit = True
        if hit:
            try:
                with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                    json.dump(records[-HISTORY_MAX:], f, ensure_ascii=False)
            except Exception as e:
                logger.error(f"保存报警剪辑记录失败: {e}")
    if hit:
        cb = on_push_recorded
        if cb:
            try:
                cb()
            except Exception as e:
                logger.error(f"推送记录回调执行失败: {e}")


def load_push_history() -> list:
    """读取全部推送记录(旧→新), 文件缺失/损坏时返回空列表"""
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            records = json.load(f)
        return records if isinstance(records, list) else []
    except Exception:
        return []


def clear_push_history():
    """清空推送记录文件"""
    with _history_lock:
        try:
            os.remove(HISTORY_FILE)
        except OSError:
            pass


def _quote(segment: str) -> str:
    """URL路径段编码"""
    return urllib.parse.quote(str(segment), safe="")


def _norm_server(server: str, default: str) -> str:
    """规范服务器地址: 空用默认; 缺协议前缀时自动补 https://"""
    s = (server or "").strip().rstrip("/")
    if not s:
        return default
    if "://" not in s:
        s = f"https://{s}"
    return s


def _parse_targets(raw: str) -> list:
    """解析多设备标识(昵称/Key/主题): 逗号/分号/空白分隔, 去重保序"""
    targets, seen = [], set()
    for t in re.split(r"[,，;；\s]+", (raw or "").strip()):
        if t and t not in seen:
            seen.add(t)
            targets.append(t)
    return targets


def _push_multi(targets: list, push_one) -> tuple:
    """多目标逐个推送: 任一成功即算成功, 汇总各目标结果说明"""
    if len(targets) == 1:
        return push_one(targets[0])
    notes, ok_any = [], False
    for i, t in enumerate(targets, 1):
        ok, note = push_one(t)
        ok_any = ok_any or ok
        notes.append(f"设备{i}:{note}")
    return ok_any, "; ".join(notes)


class MeowPush:
    """MeoW 渠道 (鸿蒙), 支持多台设备: 昵称用逗号/分号/空格分隔, 逐台推送"""
    name = "MeoW"

    def __init__(self, nickname: str):
        self.nicknames = _parse_targets(nickname)

    def push(self, title: str, msg: str) -> tuple:
        if not self.nicknames:
            return False, "未填写昵称"
        if any("/" in n for n in self.nicknames):
            return False, "昵称不能包含斜杠/, 请只填写MeoW App中注册的昵称"
        return _push_multi(self.nicknames,
                           lambda n: self._push_nick(n, title, msg))

    def _push_nick(self, nick: str, title: str, msg: str) -> tuple:
        # MeoW服务端(Tomcat)默认拒绝URL中的编码斜杠%2F: 实报文案含"/"(如"150次/分")会被HTTP 400拒绝
        # 替换为全角斜杠"／"(不会被网关解码还原, 视觉几乎一致); 昵称含斜杠已在push()中拦截
        title = str(title).replace("/", "／")
        msg = str(msg).replace("/", "／")
        url = f"{MEOW_API}/{_quote(nick)}/{_quote(title)}/{_quote(msg)}"
        return _http_get_json(url)


class BarkPush:
    """Bark 渠道 (iOS), 支持多台设备: Key用逗号/分号/空格分隔, 逐台推送
    level: 推送级别 active/timeSensitive/passive/critical(紧急无视静音), 空则不传由App默认
    sound: 自定义铃声名, group: 分组名(同组通知归拢), 均可选(对所有设备生效)"""
    name = "Bark"

    def __init__(self, device_key: str, server: str = "",
                 level: str = "", sound: str = "", group: str = ""):
        self.keys = _parse_targets(device_key)
        self.server = _norm_server(server, BARK_API)
        self.level = (level or "").strip().lower()
        self.sound = (sound or "").strip()
        self.group = (group or "").strip()

    def push(self, title: str, msg: str) -> tuple:
        if not self.keys:
            return False, "未填写Bark推送Key"
        return _push_multi(self.keys, lambda k: self._push_key(k, title, msg))

    def _push_key(self, key: str, title: str, msg: str) -> tuple:
        url = f"{self.server}/{_quote(key)}/{_quote(title)}/{_quote(msg)}"
        params = []
        if self.level in BARK_LEVELS:
            params.append(f"level={self.level}")
        if self.sound:
            params.append(f"sound={urllib.parse.quote(self.sound)}")
        if self.group:
            params.append(f"group={urllib.parse.quote(self.group)}")
        if params:
            url += "?" + "&".join(params)
        return _http_get_json(url)


class NtfyPush:
    """ntfy 渠道 (安卓/全平台), 支持多台设备: 主题用逗号/分号/空格分隔, 逐台推送
    priority: 1-5(5=紧急), 0不传; tags: emoji短代码逗号分隔; token: 访问令牌(自建/受保护主题鉴权)"""
    name = "ntfy"

    def __init__(self, topic: str, server: str = "",
                 priority: int = 0, tags: str = "", token: str = ""):
        self.topics = [t for t in (x.strip("/") for x in _parse_targets(topic)) if t]
        self.server = _norm_server(server, NTFY_API)
        try:
            self.priority = int(priority)
        except (TypeError, ValueError):
            self.priority = 0
        self.tags = (tags or "").strip()
        self.token = (token or "").strip()

    def push(self, title: str, msg: str) -> tuple:
        if not self.topics:
            return False, "未填写ntfy订阅主题"
        return _push_multi(self.topics, lambda t: self._push_topic(t, title, msg))

    def _push_topic(self, topic: str, title: str, msg: str) -> tuple:
        url = (f"{self.server}/{_quote(topic)}/publish"
               f"?title={urllib.parse.quote(title)}&message={urllib.parse.quote(msg)}")
        if 1 <= self.priority <= 5:
            url += f"&priority={self.priority}"
        if self.tags:
            url += f"&tags={urllib.parse.quote(self.tags)}"
        headers = None
        if self.token:
            headers = {"Authorization": f"Bearer {self.token}"}
        return _http_get_json(url, headers)


class XiaoiPush:
    """小爱音箱渠道(直连小米云端): EXE内完成账号登录与TTS播报, 无需xiaoi桥接服务/Docker
    双链路与xiaoi一致: MiOT TTS动作(siid=5)优先, 失败回退MiNA text_to_speech
    dids为勾选音箱的MiNA deviceID, 留空播第一台; 密码经DPAPI加密存配置"""
    name = "小爱音箱"

    def __init__(self, user: str = "", pass_b64: str = "", dids: str = ""):
        self.user = (user or "").strip()
        self.pass_b64 = (pass_b64 or "").strip()
        self.dids = _parse_targets(dids)

    def push(self, title: str, msg: str) -> tuple:
        text = f"{title},{msg}"
        from xiaomi_tts import xiaoi_tts_sync
        return xiaoi_tts_sync(self.user, self.pass_b64, self.dids, text)


def _http_error_note(e: urllib.error.HTTPError) -> str:
    """从HTTPError构造带提示的失败说明, 并读取服务端错误响应体"""
    body = ""
    try:
        body = e.read().decode("utf-8", "ignore").strip()[:200]
    except Exception:
        pass
    logger.error(f"推送HTTP {e.code}: {body}")
    hints = {400: "请求被服务端拒绝, 请检查参数(昵称/Key/did/服务器地址)",
             401: "鉴权失败, 请检查访问令牌",
             403: "内容被拒绝或当前IP受限",
             404: "接口路径不存在, 请检查服务器地址",
             429: "发送频率超限, 请稍后再试",
             500: "服务器内部错误, 请稍后再试"}
    hint = hints.get(e.code, "")
    note = f"HTTP {e.code}" + (f": {hint}" if hint else "")
    if body:
        note += f", 服务端说明: {body}"
    return note


def _retryable_note(note: str) -> bool:
    """判断失败说明是否属于连接类错误(超时/连接被拒/网络不可达等)
    仅这类错误才值得重试: 首次请求可能已被服务端处理但响应超时,
    非连接类失败(如404/参数错误)重试必然复现, 只会造成重复推送"""
    if not note:
        return False
    n = str(note).lower()
    return any(k in n for k in (
        "timeout", "timed out", "connection", "refused", "reset", "unreachable",
        "network", "getaddrinfo", "ssl", "10060", "10054", "10053"))


def _http_get_json(url: str, headers: dict = None) -> tuple:
    """发起GET请求并解析JSON响应, 返回 (成功, 说明), 网络异常不外抛"""
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not isinstance(data, dict):
            return False, f"响应格式异常: {str(data)[:100]}"
        # MeoW: {"status":200} / Bark: {"code":200} / ntfy: {"id":...}
        code = data.get("status", data.get("code", 0))
        if code == 200 or (code == 0 and "id" in data):
            return True, "推送成功"
        if code == 404:
            return False, "目标不存在(昵称/Key/主题未注册或服务器地址错误)"
        if code == 429:
            return False, "发送频率超限, 请稍后再试"
        return False, f"服务端返回: {data}"
    except urllib.error.HTTPError as e:
        return False, _http_error_note(e)
    except Exception as e:
        logger.error(f"推送请求异常: {e}")
        return False, f"网络请求失败: {e}"


def _http_post_json(url: str, payload: dict, headers: dict = None) -> tuple:
    """POST JSON并检查HTTP状态(xiaoi桥接用), 返回(成功, 说明), 网络异常不外抛"""
    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers or {},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8", "ignore").strip()[:150]
        return True, "播报成功" + (f"({body})" if body else "")
    except urllib.error.HTTPError as e:
        return False, _http_error_note(e)
    except Exception as e:
        logger.error(f"推送请求异常: {e}")
        return False, f"网络请求失败: {e}"


class NotifierManager:
    """推送管理器: 汇集所有已启用渠道, 统一处理告警判定与冷却"""

    def __init__(self):
        self.max_hr = 150      # 心率上限告警阈值
        self.min_hr = 45       # 心率下限告警阈值
        self.cooldown_seconds = 300   # 告警冷却期(秒)
        self.abnormal_duration = 10   # 异常持续判定(秒)
        self.channels = []            # 已启用的渠道实例列表
        self.irr_detector = None      # 疑似心律不齐检测器(可选启用)
        self.periods = []             # 自定义时段规则列表(每条含独立上下限/持续/冷却)
        self._hr_state = {}           # 各告警类别独立状态: "规则:high/low" -> {since,last}
        self._dev_lost_last = 0.0     # 设备断开推送冷却(独立计时)
        self._dev_back_last = 0.0     # 设备恢复推送冷却(独立计时: 断开推送后很快恢复时恢复通知不被吞)
        # 报警音: 本地报警(EXE播放)与远程报警(接收端响铃)独立开关, 由UI勾选框控制
        self.local_alarm_enabled = False
        self.remote_alarm_enabled = False
        self.alarm_seconds = 10       # 单次报警持续秒数(本地/远程共用)
        # 告警钩子: 由MainWindow注入, 签名(seconds) -> None; 触发时回调(如通知接收端响铃)
        self.on_alarm_hook = None
        self.load_config()

    def load_config(self):
        """从 config.ini [Push] 节加载配置并构建渠道列表"""
        try:
            self.max_hr = gs("Push", "max_hr", 150, int, "-Push")
            self.min_hr = gs("Push", "min_hr", 45, int, "-Push")
            self.cooldown_seconds = gs("Push", "cooldown_seconds", 300, int, "-Push")
            self.abnormal_duration = gs("Push", "abnormal_duration", 10, int, "-Push")
            self.channels = []
            if gs("Push", "meow_enabled", False, bool, "-Push"):
                self.channels.append(MeowPush(gs("Push", "meow_nickname", "", str, "-Push")))
            if gs("Push", "bark_enabled", False, bool, "-Push"):
                self.channels.append(BarkPush(
                    gs("Push", "bark_device_key", "", str, "-Push"),
                    gs("Push", "bark_server", "", str, "-Push"),
                    level=gs("Push", "bark_level", "", str, "-Push"),
                    sound=gs("Push", "bark_sound", "", str, "-Push"),
                    group=gs("Push", "bark_group", "", str, "-Push")))
            if gs("Push", "ntfy_enabled", False, bool, "-Push"):
                self.channels.append(NtfyPush(
                    gs("Push", "ntfy_topic", "", str, "-Push"),
                    gs("Push", "ntfy_server", "", str, "-Push"),
                    priority=gs("Push", "ntfy_priority", 0, int, "-Push"),
                    tags=gs("Push", "ntfy_tags", "", str, "-Push"),
                    token=gs("Push", "ntfy_token", "", str, "-Push")))
            if gs("Push", "xiaoi_enabled", False, bool, "-Push"):
                self.channels.append(XiaoiPush(
                    gs("Push", "xiaoi_user", "", str, "-Push"),
                    gs("Push", "xiaoi_pass_b64", "", str, "-Push"),
                    gs("Push", "xiaoi_dids", "", str, "-Push")))
            # 疑似心律不齐检测器(独立于渠道开关, 未启用时为None)
            if gs("Push", "irregular_enabled", False, bool, "-Push"):
                self.irr_detector = IrregularityDetector(
                    window_seconds=gs("Push", "irregular_window_seconds", 60, int, "-Push"),
                    sd_threshold=gs("Push", "irregular_sd_threshold", 5, int, "-Push"),
                    jump_bpm=gs("Push", "irregular_jump_bpm", 5, int, "-Push"),
                    jump_ratio=gs("Push", "irregular_jump_ratio_pct", 30, int, "-Push") / 100.0,
                    rest_max_hr=gs("Push", "irregular_rest_max_hr", 100, int, "-Push"),
                    sustain_windows=gs("Push", "irregular_sustain_windows", 2, int, "-Push"),
                    cooldown_seconds=gs("Push", "irregular_cooldown_minutes", 10, int, "-Push") * 60)
            else:
                self.irr_detector = None
            # 报警音开关与时长(本地响铃/远程接收端响铃, 由推送设置页勾选)
            self.local_alarm_enabled = gs("Push", "alarm_local_enabled", False, bool, "-Push")
            self.remote_alarm_enabled = gs("Push", "alarm_remote_enabled", False, bool, "-Push")
            self.alarm_seconds = max(1, gs("Push", "alarm_seconds", 10, int, "-Push"))
            # 夜间时段监护旧配置(仅用于迁移到 periods 规则)
            self.night_enabled = gs("Push", "night_enabled", False, bool, "-Push")
            self.night_start = str(gs("Push", "night_start", "22:00", str, "-Push")).strip()
            self.night_end = str(gs("Push", "night_end", "07:00", str, "-Push")).strip()
            self.night_max_hr = gs("Push", "night_max_hr", 80, int, "-Push")
            self.night_min_hr = gs("Push", "night_min_hr", 40, int, "-Push")
            # 自定义时段规则列表(JSON数组); 为空时若旧夜间配置启用则迁移为一条规则
            self.periods = self._load_periods()
            self._hr_state = {}  # 规则集变化后清空各类别计时状态
        except Exception as e:
            logger.error(f"加载推送配置失败: {e}")

    @property
    def enabled(self):
        return len(self.channels) > 0

    def _fire_alarm_sound(self):
        """报警音触发: 本地报警=EXE响铃(MCI); 远程报警=经on_alarm_hook通知WS服务推给接收端
        设备断连/恢复不响铃, 仅心率类告警触发"""
        try:
            if self.local_alarm_enabled:
                import alarm_sound
                alarm_sound.play(self.alarm_seconds)
            if self.remote_alarm_enabled and self.on_alarm_hook:
                self.on_alarm_hook(self.alarm_seconds)
        except Exception as e:
            logger.error(f"报警音触发失败: {e}")

    def _push_all(self, title: str, msg: str, alarm_ts: str | None = None,
                  skip_xiaoi: bool = False):
        """后台线程向所有已启用渠道推送
        每渠道独立线程并行发送: 单渠道响应慢/超时不拖累其他渠道
        失败自动重试1次(间隔2秒): 兜底网络抖动与服务端瞬时故障(如MeoW偶发超时)
        alarm_ts: 调用方预置的时刻戳(摄像头移动侦测用); 心率类告警内部自行生成
        skip_xiaoi: 跳过小爱音箱渠道(移动侦测只推手机通知, 音箱不播报)"""
        if alarm_ts is None and title in ("心率告警", "疑似心律不齐"):
            self._fire_alarm_sound()
            # 报警时刻戳: 推送记录据此关联摄像头剪辑(剪辑异步完成后经set_alarm_clips回填)
            alarm_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                if on_camera_alarm_hook:
                    on_camera_alarm_hook(alarm_ts)
            except Exception as e:
                logger.error(f"摄像头报警剪辑钩子执行失败: {e}")
        if not self.channels:
            return

        def push_channel(ch):
            ok, note = ch.push(title, msg)
            if not ok and _retryable_note(note):
                # 仅连接类错误才重试: 首次请求可能已被服务端处理但响应超时,
                # 无条件重试会对已送达的通知造成重复推送; 非连接类失败直接落记录
                time.sleep(2)
                ok, retry_note = ch.push(title, msg)
                note = (f"首次失败({note}), 重试成功" if ok
                        else f"{note}; 重试仍失败: {retry_note}")
            record_push(ch.name, title, msg, ok, note,
                        extra={"alarm_ts": alarm_ts} if alarm_ts else None)
            if ok:
                logger.info(f"[{ch.name}]推送成功: [{title}] {msg}")
            else:
                logger.warning(f"[{ch.name}]推送失败: {note}")

        for ch in self.channels:
            if skip_xiaoi and isinstance(ch, XiaoiPush):
                continue
            threading.Thread(target=push_channel, args=(ch,), daemon=True).start()

    @staticmethod
    def _norm_hhmm(value) -> str:
        """时间规范化为 HH:MM; 非法输入返回空串(兼容 9:05 / 0905 等手改格式)"""
        v = str(value or "").strip()
        for fmt in ("%H:%M", "%H%M"):
            try:
                return datetime.datetime.strptime(v, fmt).strftime("%H:%M")
            except ValueError:
                continue
        return ""

    def _load_periods(self) -> list:
        """解析 [Push] periods(JSON数组)。字段: enabled/start/end/max/min/sustain/cooldown
        max或min为0表示该项不检测; sustain为持续判定秒数; cooldown为冷却秒数(独立不共用)
        无 periods 配置且旧夜间监护启用时, 迁移为一条等效规则
        单条规则非法时跳过该条, 不影响其余规则"""
        raw = str(gs("Push", "periods", "", str, "-Push")).strip()
        if not raw:
            if gs("Push", "night_enabled", False, bool, "-Push"):
                s, e = self._norm_hhmm(self.night_start), self._norm_hhmm(self.night_end)
                if s and e and s != e:
                    return [{"enabled": True, "start": s, "end": e,
                             "max": self.night_max_hr, "min": self.night_min_hr,
                             "sustain": self.abnormal_duration, "cooldown": self.cooldown_seconds}]
                if gs("Push", "night_enabled", False, bool, "-Push"):
                    logger.warning(f"旧夜间监护时间格式非法({self.night_start}-{self.night_end}), 已忽略")
            return []
        try:
            items = json.loads(raw)
        except Exception as e:
            logger.error(f"解析时段规则失败, 忽略: {e}")
            return []
        if not isinstance(items, list):
            return []
        rules = []
        for p in items:
            if not isinstance(p, dict):
                continue
            try:
                s = self._norm_hhmm(p.get("start", ""))
                e = self._norm_hhmm(p.get("end", ""))
                if not s or not e or s == e:
                    logger.warning(f"时段规则起止时间非法, 已跳过: {p}")
                    continue
                rules.append({"enabled": bool(p.get("enabled", True)),
                              "start": s, "end": e,
                              "max": max(0, int(p.get("max", 0))),
                              "min": max(0, int(p.get("min", 0))),
                              "sustain": max(1, int(p.get("sustain", self.abnormal_duration))),
                              "cooldown": max(0, int(p.get("cooldown", self.cooldown_seconds)))})
            except (TypeError, ValueError) as ex:
                logger.warning(f"时段规则字段非法, 已跳过: {p} ({ex})")
        return rules

    def _active_rule(self, now: float) -> dict:
        """按当前时刻返回生效规则(首个命中的时段规则, 否则默认规则)
        时段为 [起始, 结束) 半开区间(含起始整分, 不含结束整分), 支持跨零点"""
        t = datetime.datetime.fromtimestamp(now).strftime("%H:%M")
        for i, p in enumerate(self.periods):
            if not p.get("enabled"):
                continue
            s, e = p["start"], p["end"]
            if (s <= e and s <= t < e) or (s > e and (t >= s or t < e)):
                return {"tag": f"p{i}", "label": f"[{s}-{e}]",
                        "max": p["max"], "min": p["min"],
                        "sustain": p["sustain"], "cooldown": p["cooldown"]}
        return {"tag": "global", "label": "",
                "max": self.max_hr, "min": self.min_hr,
                "sustain": self.abnormal_duration, "cooldown": self.cooldown_seconds}

    def check_heart_rate(self, heart_rate: int):
        """心率检查入口: 高/低心率告警(默认规则+自定义时段规则) + 疑似心律不齐检测
        各告警类别(规则×过高/过低)独立计时持续判定与冷却, 互不共CD
        在心率回调中调用(约每秒一次), 仅做状态判断, 不阻塞"""
        if heart_rate <= 0:
            # 断连/无效值: 心律不齐检测器需收到无效值以清空窗口
            self._check_irregularity(heart_rate)
            return
        if self.enabled:
            now = time.time()
            rule = self._active_rule(now)
            for kind, exceeded in (("high", rule["max"] > 0 and heart_rate > rule["max"]),
                                   ("low", rule["min"] > 0 and heart_rate < rule["min"])):
                key = f"{rule['tag']}:{kind}"
                if not exceeded:
                    # 恢复正常即清空该类别计时, 新一轮异常从零重新判定持续时长
                    self._hr_state.pop(key, None)
                    continue
                st = self._hr_state.setdefault(key, {"since": None, "last": 0.0})
                if st["since"] is None:
                    st["since"] = now
                elif (now - st["since"] >= rule["sustain"]
                        and now - st["last"] >= rule["cooldown"]):
                    st["last"] = now
                    self._push_all("心率告警",
                                   f"{rule['label']}心率{'过高' if kind == 'high' else '过低'}: "
                                   f"{heart_rate}次/分, 已持续{rule['sustain']}秒")
                # 持续异常期间 since 保持, 冷却到期后自动再次告警
        self._check_irregularity(heart_rate)

    def _check_irregularity(self, heart_rate: int):
        """疑似心律不齐检测(独立启停, 推送到所有已启用渠道)"""
        if not self.irr_detector:
            return
        metrics = self.irr_detector.check(heart_rate)
        if metrics:
            self._push_all(
                "疑似心律不齐",
                f"静息心率无序波动: 平均{metrics['mean']:.0f}次/分, "
                f"波动±{metrics['sd']:.1f}, 大幅跳变占比{metrics['jump_ratio']:.0%}。"
                f"此为筛查提示非医学诊断, 建议静息复测或就医确认")

    def _dev_notify_ok(self, slot: str) -> bool:
        """设备断开/恢复推送独立冷却闸门: 各自5分钟内最多推1条
        (蓝牙连接反复失败时is_connected会翻转多次, 无冷却会轰炸手机通知)"""
        now = time.monotonic()
        if now - getattr(self, slot) >= 300.0:
            setattr(self, slot, now)
            return True
        return False

    def notify_device_lost(self, devname: str = ""):
        """设备断连提醒"""
        if self.enabled and self._dev_notify_ok("_dev_lost_last"):
            name = f" {devname}" if devname else ""
            self._push_all("设备断开", f"蓝牙设备{name}已断开连接")

    def notify_device_back(self, devname: str = ""):
        """设备重连成功提醒"""
        if self.enabled and self._dev_notify_ok("_dev_back_last"):
            name = f" {devname}" if devname else ""
            self._push_all("设备恢复", f"蓝牙设备{name}已重新连接")

    def notify_reconnect_abandoned(self, devname: str = ""):
        """智能重连多轮未果放弃提醒(不走设备状态冷却: 放弃意味着监护中断, 必须送达)"""
        if self.enabled:
            name = f" {devname}" if devname else ""
            self._push_all("重连放弃",
                           f"蓝牙设备{name}多轮自动重连失败, 已停止重连, "
                           f"请检查手环是否被手机抢占或已离开范围")

    def notify_camera_motion(self, cam: str, source: str, alarm_ts: str):
        """摄像头移动侦测提醒(与心率报警体系独立: 不响铃/不推WS/不触发全路剪辑)
        仅剪辑触发路自身(由侦测watcher完成)并推送手机通知渠道, 片段经alarm_ts回填记录
        小爱音箱不播报移动侦测(音箱只负责心率类告警, 避免画面一动就说话)"""
        src = f"({source})" if source else ""
        self._push_all("摄像头移动侦测", f"{cam} 检测到画面变动{src}",
                       alarm_ts=alarm_ts, skip_xiaoi=True)
