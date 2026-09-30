# mdns_announce.py — 中枢 mDNS 服务注册 (v1.2.19)
# 配套 ESP32 固件 v1.1.9: 节点启动/WS断连时经 MDNS.queryService("_hrmlink") 自动发现中枢,
# 换中枢/换IP零重配。注册失败静默降级(节点退回NVS缓存/配网地址), 不影响主链路。
# 注意: 注册在守护线程执行 — Zeroconf 在多网卡(含Tailscale)机器上可能阻塞, 绝不能拖住主启动链。
import socket
import logging
import threading

log = logging.getLogger("hrmlink")

_zc = None
_info = None
_lock = threading.Lock()


def _lan_ip():
    """取默认路由出口IP(局域网IP; Tailscale不接管默认路由故不会被误选)。
    若出口恰为100.x(Tailscale), 再从本机地址表里挑非100./127.的候选。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        if not ip.startswith("100."):
            return ip
    except Exception:
        pass
    finally:
        s.close()
    try:
        for a in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not a.startswith("100.") and not a.startswith("127."):
                return a
    except Exception:
        pass
    return None


def start_hub_announce(port: int, name: str = "HRMLink"):
    """注册 _hrmlink._tcp 服务(指向局域网IP); 幂等; 在守护线程执行防阻塞启动链。"""
    with _lock:
        if _info is not None:
            return
    threading.Thread(target=_register_worker, args=(port, name), daemon=True).start()


def _register_worker(port: int, name: str):
    global _zc, _info
    try:
        from zeroconf import ServiceInfo, Zeroconf
    except ImportError:
        log.info("[mDNS] zeroconf 未安装, 跳过服务注册(节点退回配网地址)")
        return
    try:
        ip = _lan_ip()
        if not ip:
            log.warning("[mDNS] 未找到局域网IP, 跳过服务注册")
            return
        zc = Zeroconf()
        info = ServiceInfo(
            "_hrmlink._tcp.local.",
            "%s._hrmlink._tcp.local." % name,
            addresses=[socket.inet_aton(ip)],
            port=port,
            properties={"ver": "1"},
        )
        zc.register_service(info)
        with _lock:
            _zc, _info = zc, info
        log.info("[mDNS] 中枢服务已注册: _hrmlink._tcp %s:%d", ip, port)
    except Exception as e:
        log.warning("[mDNS] 服务注册失败(降级为节点配网地址): %s", e)
        stop_hub_announce()


def stop_hub_announce():
    global _zc, _info
    if _zc is not None and _info is not None:
        try:
            _zc.unregister_service(_info)
            _zc.close()
        except Exception:
            pass
    _zc = None
    _info = None
