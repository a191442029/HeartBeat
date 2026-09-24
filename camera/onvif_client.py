# -*- coding: utf-8 -*-
"""极简 ONVIF 客户端(手写SOAP, 仅urllib, 零第三方依赖)

覆盖摄像头联动所需的最小方法集:
  GetDeviceInformation / GetProfiles / GetStreamUri / GetSnapshotUri
认证: WS-UsernameToken (Nonce+Created+Password SHA1 摘要, ONVIF 标准方式)
"""
import base64
import hashlib
import os
import re
import time
import uuid
import xml.etree.ElementTree as ET
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

import system_utils as _sysu
from system_utils import dpapi_unprotect


def _log(level: str, msg: str):
    """安全日志: 晚绑定 system_utils.logger(独立工具/测试场景下可能未初始化)"""
    lg = _sysu.logger
    if lg is not None:
        getattr(lg, level)(msg)


# 常见 ONVIF 端口, 探测时按序尝试
COMMON_PORTS = (80, 8899, 8000, 2020, 7575, 5000, 8999)

# media 服务地址很多设备固定为 /onvif/media_service, 个别品牌不同, 探测时兜底尝试
MEDIA_PATHS = ("/onvif/media_service", "/onvif/Media", "/onvif/media", "/onvif/device_service")

SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"          # SOAP 1.2
SOAP11_NS = "http://schemas.xmlsoap.org/soap/envelope/"      # SOAP 1.1
TT_NS = "http://www.onvif.org/ver10/schema"
TRT_NS = "http://www.onvif.org/ver10/media/wsdl"
TD_NS = "http://www.onvif.org/ver10/device/wsdl"
TEV_NS = "http://www.onvif.org/ver10/events/wsdl"            # 事件服务(移动侦测上报)

# events 服务地址很多设备固定为 /onvif/event_service, 探测时按序兜底尝试
EVENT_PATHS = ("/onvif/event_service", "/onvif/Events", "/onvif/events", "/onvif/device_service")


def _wsse_header(username: str, password: str, env: str = "s",
                 plain: bool = False, time_offset: float = 0.0,
                 hash_alg: str = "sha1", nonce_mode: str = "raw") -> str:
    """构造 WS-UsernameToken 安全头; env 为信封前缀(1.2用s/1.1用env);
    plain=True 用明文密码; hash_alg 摘要算法(sha1/sha256); nonce_mode:
    raw=标准(原始字节参与摘要) / b64=部分设备用 base64 字符串参与摘要"""
    nonce = os.urandom(16)
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + time_offset))
    if plain:
        pwd = (f'<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText">'
               f"{_esc(password)}</wsse:Password>")
    else:
        # 摘要输入: 标准为 原始nonce字节; 变体设备(部分国产固件)用 base64(nonce) 字符串
        nonce_in = nonce if nonce_mode == "raw" else base64.b64encode(nonce)
        h = hashlib.sha256() if hash_alg == "sha256" else hashlib.sha1()
        h.update(nonce_in + created.encode("utf-8") + password.encode("utf-8"))
        digest = base64.b64encode(h.digest()).decode("ascii")
        pwd = (f'<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</wsse:Password>')
    nonce_b64 = base64.b64encode(nonce).decode("ascii")
    return (
        f'<wsse:Security {env}:mustUnderstand="1" '
        f'xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
        f"<wsse:UsernameToken><wsse:Username>{_esc(username)}</wsse:Username>"
        f"{pwd}"
        f'<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nonce_b64}</wsse:Nonce>'
        f"<wsu:Created xmlns:wsu=\"http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd\">{created}</wsu:Created>"
        f"</wsse:UsernameToken></wsse:Security>"
    )


# 认证变体尝试序列: 标准摘要 → HTTP Digest → SHA-256摘要 → b64nonce变体 → 明文
AUTH_MODES = ("digest", "http", "digest_sha256", "digest_b64nonce", "plain")
AUTH_MODE_NAMES = {
    "digest": "WS标准摘要", "http": "HTTP Digest", "digest_sha256": "SHA-256摘要",
    "digest_b64nonce": "b64nonce摘要", "plain": "明文密码",
}


def _fault_summary(detail: str) -> str:
    """从设备返回的 SOAP 应答中提取 Fault 关键信息, 避免整段 namespace 轰炸UI"""
    try:
        root = ET.fromstring(detail)
    except ET.ParseError:
        return detail[:150]
    parts = []
    for t in root.iter():
        name = t.tag.rsplit("}", 1)[-1]
        if name in ("Value", "Text", "faultcode", "faultstring"):
            txt = (t.text or "").strip()
            if txt and txt not in parts:
                parts.append(txt)
    return " | ".join(parts)[:200] if parts else detail[:150]


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class OnvifError(Exception):
    pass


class OnvifClient:
    def __init__(self, ip: str, port: int, username: str, password: str, timeout: float = 5.0):
        self.ip = ip
        self.port = int(port)
        self.username = username or ""
        self.password = dpapi_unprotect(password) if str(password).startswith("dpapi:") else (password or "")
        self.timeout = timeout
        self.base = f"http://{ip}:{self.port}"
        # 探测成功后缓存的服务地址
        self.device_url = f"{self.base}/onvif/device_service"
        self.media_url = f"{self.base}/onvif/media_service"
        # 认证兼容: 设备时间偏移(从HTTP Date头学习) + 当前认证模式索引
        self._time_offset = 0.0
        self._auth_idx = 0
        # HTTP Digest 支持(401 challenge 自动重放): opener 预填本机凭据, realm 通配
        pm = urlrequest.HTTPPasswordMgrWithDefaultRealm()
        pm.add_password(None, f"http://{self.ip}:{self.port}", self.username, self.password)
        self._opener = urlrequest.build_opener(urlrequest.HTTPDigestAuthHandler(pm))

    def _wsse_kwargs(self) -> dict | None:
        """当前认证模式对应的 wsse 头参数; None=不带 wsse 头(HTTP Digest 用)"""
        m = AUTH_MODES[self._auth_idx]
        if m == "http":
            return None
        if m == "plain":
            return {"plain": True}
        if m == "digest_sha256":
            return {"hash_alg": "sha256"}
        if m == "digest_b64nonce":
            return {"nonce_mode": "b64"}
        return {}

    # ---------- 底层 ----------

    def _post(self, url: str, body: str, soap11: bool = False) -> str:
        """发送 SOAP 请求; soap11=True 时用 SOAP 1.1 格式(部分设备不支持 1.2)"""
        env = "env" if soap11 else "s"
        ns = SOAP11_NS if soap11 else SOAP_NS
        kw = self._wsse_kwargs()
        hdr = "" if kw is None else _wsse_header(
            self.username, self.password, env, time_offset=self._time_offset, **kw)
        xml_envelope = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<{env}:Envelope xmlns:{env}="{ns}">'
            f"<{env}:Header>{hdr}</{env}:Header>"
            f"<{env}:Body>{body}</{env}:Body></{env}:Envelope>"
        ).encode("utf-8")
        req = urlrequest.Request(url, data=xml_envelope, method="POST")
        if soap11:
            req.add_header("Content-Type", "text/xml; charset=utf-8")
            req.add_header("SOAPAction", '""')
        else:
            req.add_header("Content-Type", 'application/soap+xml; charset=utf-8; action=""')
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                self._sync_time(resp.headers.get("Date"))  # 学习设备时间偏移
                return resp.read().decode("utf-8", errors="replace")
        except HTTPError as e:
            # 设备常以 400/500 返回 SOAP Fault, 提取关键信息便于定位
            detail = ""
            try:
                self._sync_time(e.headers.get("Date") if e.headers else None)
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise OnvifError(f"HTTP {e.code} {_fault_summary(detail)}") from e
        except (URLError, OSError) as e:
            raise OnvifError(f"连接失败: {e}") from e

    def _sync_time(self, date_hdr: str):
        """从 HTTP Date 响应头学习设备时间偏移(摘要认证的 Created 需与设备时间同步)"""
        if not date_hdr:
            return
        try:
            from email.utils import parsedate_to_datetime
            dev_ts = parsedate_to_datetime(date_hdr).timestamp()
            self._time_offset = dev_ts - time.time()
        except Exception:
            pass

    def _attempt(self, url: str, body: str) -> tuple:
        """按当前认证模式尝试 1.2 → 1.1 两种信封; 返回 (raw, None) 或 (None, 最后错误)"""
        last = None
        for soap11 in (False, True):
            try:
                return self._post(url, body, soap11=soap11), None
            except OnvifError as e:
                last = e
                s = str(e)
                retryable = ("HTTP 400" in s or "HTTP 401" in s or "HTTP 415" in s
                             or "HTTP 426" in s or "HTTP 500" in s
                             or "VersionMismatch" in s
                             or "NotAuthorized" in s or "not authorized" in s.lower())
                if not retryable:
                    raise
        return None, last

    def _call(self, url: str, body: str) -> ET.Element:
        """执行请求; 认证失败时按 AUTH_MODES 序列逐个尝试(成功后缓存该模式)"""
        raw = None
        err = None
        tried = []
        for i in range(len(AUTH_MODES)):
            self._auth_idx = i
            raw, e = self._attempt(url, body)
            if raw is not None:
                break
            err = e
            s = str(e)
            tried.append(AUTH_MODE_NAMES.get(AUTH_MODES[i], AUTH_MODES[i]))
            not_auth = ("NotAuthorized" in s or "not authorized" in s.lower()
                        or "http 401" in s.lower())  # 裸401(无SOAP Fault体)也属认证类错误
            if not not_auth:
                raise err  # 非认证类错误(网络/版本等)不换认证方式
        if raw is None:
            raise OnvifError(
                f"{err} (已尝试{'/'.join(tried)}均被拒, 请核对ONVIF密码)")
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as e:
            raise OnvifError(f"应答解析失败: {e}") from e
        fault = root.find(f".//{{{SOAP_NS}}}Fault")
        if fault is None:  # SOAP 1.1 信封的 Fault
            fault = root.find(f".//{{{SOAP11_NS}}}Fault")
        if fault is not None:
            texts = [t.text or "" for t in fault.iter() if t.text and (t.text or "").strip()]
            raise OnvifError("SOAP Fault: " + " | ".join(dict.fromkeys(texts))[:200])
        return root

    # find() 前缀路径所需的命名空间映射(ElementPath 要求显式传入, 否则报 prefix not found)
    NS_MAP = {"tt": TT_NS, "td": TD_NS, "trt": TRT_NS}

    @staticmethod
    def _find_local(node: ET.Element, local: str):
        """按局部名查找子元素(忽略命名空间), 用于兼容设备私有格式"""
        for e in node.iter():
            if isinstance(e.tag, str) and e.tag.rsplit("}", 1)[-1] == local:
                return e
        return None

    @staticmethod
    def _text(node: ET.Element, tag: str) -> str:
        # 注意: Element 带有"无子节点即 False"的历史行为, 必须显式判 None
        el = node.find(f".//{tag}", namespaces=OnvifClient.NS_MAP)
        if el is None:
            return ""
        return (el.text or "").strip()

    # ---------- 设备服务 ----------

    def get_device_information(self) -> dict:
        body = f'<td:GetDeviceInformation xmlns:td="{TD_NS}"/>'
        root = self._call(self.device_url, body)
        return {
            "manufacturer": self._text(root, "tt:Manufacturer"),
            "model": self._text(root, "tt:Model"),
            "firmware": self._text(root, "tt:FirmwareVersion"),
            "serial": self._text(root, "tt:SerialNumber"),
        }

    # ---------- 媒体服务 ----------

    def _media_call(self, body: str) -> ET.Element:
        last_err = None
        for path in MEDIA_PATHS:
            try:
                return self._call(self.base + path, body)
            except OnvifError as e:
                last_err = e
                # SOAP Fault(认证/参数问题)换路径也无解, 直接抛
                if "SOAP Fault" in str(e):
                    raise
        raise last_err or OnvifError("media服务不可达")

    def get_profiles(self) -> list:
        """返回 [(token, name, 宽x高), ...]
        解析对命名空间宽容: 部分设备(Arenti等)Profile元素不在标准trt命名空间下"""
        body = f'<trt:GetProfiles xmlns:trt="{TRT_NS}"/>'
        root = self._media_call(body)
        profiles = []
        for p in root.iter():
            # ONVIF规范用单数Profile, 部分设备(Arenti)返回复数Profiles, 两者都接受
            if p.tag.rsplit("}", 1)[-1] not in ("Profile", "Profiles"):
                continue
            token = p.get("token") or p.get("Token") or ""
            name_el = self._find_local(p, "Name")
            name = (name_el.text or "").strip() if name_el is not None else ""
            res = ""
            vec = self._find_local(p, "VideoEncoderConfiguration")
            if vec is not None:
                rs = self._find_local(vec, "Resolution")
                if rs is not None:
                    w_el = self._find_local(rs, "Width")
                    h_el = self._find_local(rs, "Height")
                    if w_el is not None and h_el is not None and w_el.text and h_el.text:
                        res = f"{w_el.text.strip()}x{h_el.text.strip()}"
            profiles.append((token, name, res))
        if not profiles:
            # 诊断: 设备私有格式解析失败时dump响应片段, 便于按真实结构适配
            try:
                raw = ET.tostring(root, encoding="unicode")[:600]
            except Exception:
                raw = "<dump失败>"
            _log("warning", f"[ONVIF:{self.ip}] GetProfiles 未解析到Profile, 响应片段: {raw}")
        return profiles

    def get_stream_uri(self, profile_token: str, stream_type: str = "RTP-Unicast",
                       protocol: str = "RTSP") -> str:
        body = (
            f'<trt:GetStreamUri xmlns:trt="{TRT_NS}">'
            f"<StreamSetup><Stream xmlns=\"http://www.onvif.org/ver10/schema\">{stream_type}</Stream>"
            f'<Transport xmlns="http://www.onvif.org/ver10/schema"><Protocol>{protocol}</Protocol></Transport>'
            f"</StreamSetup><ProfileToken>{_esc(profile_token)}</ProfileToken></trt:GetStreamUri>"
        )
        root = self._media_call(body)
        uri = self._text(root, "tt:Uri")
        if not uri:
            u_el = self._find_local(root, "Uri")
            uri = (u_el.text or "").strip() if u_el is not None else ""
        if not uri:
            raise OnvifError("设备未返回流地址")
        return _inject_credentials(uri, self.ip, self.username, self.password)

    def get_snapshot_uri(self) -> str:
        body = f'<trt:GetSnapshotUri xmlns:trt="{TRT_NS}"><ProfileToken>0</ProfileToken></trt:GetSnapshotUri>'
        # 多数设备要求合法 token, 先取第一个 profile
        try:
            profiles = self.get_profiles()
            if profiles:
                body = (f'<trt:GetSnapshotUri xmlns:trt="{TRT_NS}">'
                        f"<ProfileToken>{_esc(profiles[0][0])}</ProfileToken></trt:GetSnapshotUri>")
        except OnvifError:
            pass
        root = self._media_call(body)
        uri = self._text(root, "tt:Uri")
        if not uri:  # 非标准命名空间设备兜底(与 get_stream_uri 一致)
            u_el = self._find_local(root, "Uri")
            uri = (u_el.text or "").strip() if u_el is not None else ""
        return uri

    def pick_profile(self, prefer_sub: bool = True) -> tuple:
        """选取码流 profile, 返回 (token, name, res)
        prefer_sub=True 取子码流(分辨率最低), False 取主码流(分辨率最高);
        全部无分辨率信息时按名字猜(sub/子/low 或 main/主/high), 再不行取第一个"""
        profiles = self.get_profiles()
        if not profiles:
            raise OnvifError("设备无可用 Profile")
        have_res = []
        for it in profiles:
            m = re.match(r"(\d+)x(\d+)", it[2] or "")
            if m:
                have_res.append((int(m.group(1)) * int(m.group(2)), it))
        if have_res:
            return (min if prefer_sub else max)(have_res, key=lambda x: x[0])[1]
        names = [(it, (it[1] or "").lower()) for it in profiles]
        if prefer_sub:
            for it, n in names:
                if "sub" in n or "子" in (it[1] or "") or "low" in n or "second" in n:
                    return it
        else:
            for it, n in names:
                if "main" in n or "主" in (it[1] or "") or "high" in n:
                    return it
        return profiles[0]

    # ---------- 事件服务(移动侦测上报, 支持度依固件而定) ----------

    def create_pullpoint_subscription(self) -> str:
        """创建 PullPoint 事件订阅, 返回 PullMessages 应 POST 的订阅地址"""
        body = (f'<tev:CreatePullPointSubscription xmlns:tev="{TEV_NS}">'
                f"<tev:InitialTerminationTime>PT10M</tev:InitialTerminationTime>"
                f"</tev:CreatePullPointSubscription>")
        last = None
        for path in EVENT_PATHS:
            try:
                root = self._call(self.base + path, body)
            except OnvifError as e:
                last = e
                # 认证类Fault换路径也无解; ActionNotSupported(该路径无事件服务)继续试下一路径
                s = str(e).lower()
                if "soap fault" in s and ("notauthorized" in s or "auth" in s):
                    raise
                continue
            addr = self._find_local(root, "Address")
            if addr is not None and (addr.text or "").strip():
                return addr.text.strip()
            return self.base + path  # 无显式地址时按标准回原服务地址
        raise last or OnvifError("事件服务不可达")

    def pull_messages(self, sub_url: str, timeout_s: int = 3) -> tuple:
        """拉取订阅消息(长轮询), 返回 (topics字符串列表, [(Name, Value), ...])"""
        body = (f'<tev:PullMessages xmlns:tev="{TEV_NS}">'
                f"<tev:Timeout>PT{max(1, int(timeout_s))}S</tev:Timeout>"
                f"<tev:MessageLimit>10</tev:MessageLimit></tev:PullMessages>")
        root = self._call(sub_url, body)
        topics = [(t.text or "").strip() for t in root.iter()
                  if isinstance(t.tag, str) and t.tag.rsplit("}", 1)[-1] == "Topic"]
        items = [(e.get("Name", ""), e.get("Value", "")) for e in root.iter()
                 if isinstance(e.tag, str) and e.tag.rsplit("}", 1)[-1] == "SimpleItem"]
        return [t for t in topics if t], items

    def renew_subscription(self, sub_url: str):
        """续期订阅(续10分钟)"""
        body = (f'<tev:Renew xmlns:tev="{TEV_NS}">'
                f"<tev:TerminationTime>PT10M</tev:TerminationTime></tev:Renew>")
        self._call(sub_url, body)


def _inject_credentials(uri: str, ip: str, username: str, password: str) -> str:
    """设备返回的 rtsp 地址可能不带账号密码, 注入 http(s)/rtsp 标准形式 user:pass@host"""
    if "@" in uri or not username:
        return uri
    m = re.match(r"^(rtsp://)([^/]+)(.*)$", uri)
    if m:
        from urllib.parse import quote
        cred = quote(username, safe="") + ":" + quote(password or "", safe="")
        return f"{m.group(1)}{cred}@{m.group(2)}{m.group(3)}"
    return uri


def detect_onvif_port(ip: str, username: str, password: str,
                      ports=COMMON_PORTS, timeout: float = 3.0) -> int:
    """依次探测常见端口, 返回第一个能通过 GetDeviceInformation 认证的端口"""
    last_err = None
    for port in ports:
        client = OnvifClient(ip, port, username, password, timeout=timeout)
        try:
            client.get_device_information()
            return port
        except OnvifError as e:
            last_err = e
            # 连接层面失败继续试下一端口; 认证层面失败(端口对但密码错)直接抛出更友好
            low = str(e).lower()
            if "notauthorized" in low or "auth" in low or "sender" in low or "user" in low:
                raise OnvifError(f"端口 {port} 可达但认证失败: {e}") from e
    raise OnvifError(f"所有常见端口均不可达({ip}), 最后错误: {last_err}")
