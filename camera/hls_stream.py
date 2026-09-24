# -*- coding: utf-8 -*-
"""报警房间摄像头 HLS 实时流 (期5)

设计:
- 报警期间按需为"绑定房间摄像头"起一路 ffmpeg: RTSP子码流 → 转码h264(强制1秒GOP)
  → HLS(1秒分片, 滑动窗口3片) 写入系统临时目录; 接收端 AVPlayer 直接播 m3u8
- 转码而非copy: 摄像头GOP普遍2~4秒, copy会导致分片时长失控(延迟8秒+);
  640x360@15 veryfast 转码对PC几乎无压力, 且仅报警期间运行
- 接收端延迟约2~4秒(分片窗口+播放器缓冲), 失败自动回退既有2fps快照轮询
- 分片目录按摄像头名(清洗后)隔离; stop即清理目录; atexit兜底清理整个根目录
- 分片URI经 -hls_base_url 写成 /camera/live/segment.ts?cam=X&seg=N 形式,
  播放器从playlist拿到带query的绝对路径URI(相对解析会丢query, 必须写死)
"""
import atexit
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

import system_utils as _su
from .stream_manager import (
    _spawn, RTSP_INPUT_OPTS, NO_WINDOW, find_ffmpeg, _SAFE_FILE, CameraStream
)


def _log(level: str, msg: str):
    lg = _su.logger  # 晚绑定: 应用启动时 getlogger() 会重新赋值
    if lg is not None:
        getattr(lg, level)(msg)


# 分片文件名白名单(路由校验, 防路径穿越)
SEG_RE = re.compile(r"^seg_\d{5}\.ts$")

# 根目录: 系统临时目录/hrmlink_hls/<摄像头名>/
ROOT_DIR = Path(tempfile.gettempdir()) / "hrmlink_hls"


class HlsStream:
    """单路摄像头 HLS 实时流: ffmpeg 常驻重连循环 + 分片落临时目录"""

    READY_TIMEOUT = 4.0  # 启动后等待首个分片的最长时间(秒)

    def __init__(self, name: str, stream: CameraStream, base_url: str):
        self.name = name
        self.stream = stream
        self.base_url = base_url          # 分片URI前缀: /camera/live/segment.ts?cam=X&seg=
        safe = _SAFE_FILE.sub("_", str(name)).strip(". ")[:80] or "cam"
        self.dir = ROOT_DIR / safe
        self._proc = None
        self._thread = None
        self._stop_evt = threading.Event()
        self._ffmpeg = None

    @property
    def playlist_path(self) -> Path:
        return self.dir / "index.m3u8"

    def segment_path(self, seg: str) -> Path:
        """路由取分片路径(仅白名单名; 不存在由调用方判)"""
        return self.dir / seg

    def start(self):
        """启动(幂等): 起重连循环线程"""
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"camhls-{self.name}")
        self._thread.start()

    def stop(self):
        """停止并清理分片目录"""
        self._stop_evt.set()
        CameraStream._kill(self._proc)
        self._proc = None
        self._thread = None
        shutil.rmtree(self.dir, ignore_errors=True)

    def _run(self):
        backoff = 2
        while not self._stop_evt.is_set():
            proc = None
            try:
                if self._ffmpeg is None:
                    self._ffmpeg = find_ffmpeg()
                uri = self.stream.resolve_rtsp_uri()
                self.dir.mkdir(parents=True, exist_ok=True)
                cmd = [
                    self._ffmpeg, "-hide_banner", "-loglevel", "error",
                    "-rtsp_transport", "tcp", *RTSP_INPUT_OPTS, "-i", uri,
                    # 转码: 强制15fps/1秒GOP, 保证分片精确1秒(低延迟关键)
                    "-vf", "scale=640:360", "-r", "15",
                    "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
                    "-g", "15", "-keyint_min", "15", "-sc_threshold", "0",
                    "-pix_fmt", "yuv420p", "-an",
                    "-f", "hls",
                    "-hls_time", "1", "-hls_list_size", "3",
                    "-hls_flags", "delete_segments+independent_segments",
                    "-hls_base_url", self.base_url,
                    "-hls_segment_filename", str(self.dir / "seg_%05d.ts"),
                    str(self.playlist_path),
                ]
                proc = _spawn(cmd, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL,
                              creationflags=NO_WINDOW)
                self._proc = proc
                backoff = 2
                _log("info", f"[摄像头:{self.name}] HLS实时流已启动")
                while not self._stop_evt.is_set() and proc.poll() is None:
                    time.sleep(0.5)
            except Exception as e:
                _log("warning", f"[摄像头:{self.name}] HLS流异常: {e}")
            CameraStream._kill(proc)
            if self._proc is proc:  # 只清自己的引用, 防误清新一轮进程
                self._proc = None
            self._stop_evt.wait(backoff)
            backoff = min(backoff * 2, 60)

    def wait_until_ready(self, timeout: float = None) -> bool:
        """等待playlist出现且含分片(阻塞调用线程, 供启动方确认可播)"""
        deadline = time.time() + (self.READY_TIMEOUT if timeout is None else timeout)
        p = self.playlist_path
        while time.time() < deadline:
            try:
                if p.is_file() and "seg_" in p.read_text(encoding="utf-8", errors="ignore"):
                    return True
            except OSError:
                pass
            if self._stop_evt.is_set():
                return False
            time.sleep(0.2)
        return False


class HlsManager:
    """全部 HLS 流的生命周期(按摄像头名管理, 与报警联动启停)
    键统一用清洗后的名字(_SAFE_FILE, 与分片目录名一致), 路由查询参数原样传入即可"""

    def __init__(self):
        self._streams = {}  # 清洗后摄像头名 -> HlsStream
        self._lock = threading.Lock()
        atexit.register(self._atexit_cleanup)

    @staticmethod
    def _key(name: str) -> str:
        return _SAFE_FILE.sub("_", str(name or "")).strip(". ")[:80] or "cam"

    def start_for(self, name: str, stream: CameraStream, base_url: str,
                  wait: float = None) -> HlsStream | None:
        """启动指定摄像头的HLS流(幂等), 阻塞等待可播(≤READY_TIMEOUT秒)。
        返回 HlsStream 实例(调用方再拼 m3u8 地址); 参数非法返回None"""
        if not name or stream is None or not base_url:
            return None
        key = self._key(name)
        with self._lock:
            s = self._streams.get(key)
            if s is None:
                s = HlsStream(name, stream, base_url)
                self._streams[key] = s
        s.start()
        ready = s.wait_until_ready(s.READY_TIMEOUT if wait is None else wait)
        if not ready:
            _log("warning", f"[摄像头:{name}] HLS流{wait or s.READY_TIMEOUT}秒内未就绪")
        return s

    def get(self, name: str) -> HlsStream | None:
        key = self._key(name)
        if not key:
            return None
        with self._lock:
            return self._streams.get(key)

    def stop(self, name: str):
        with self._lock:
            s = self._streams.pop(self._key(name), None)
        if s is not None:
            s.stop()

    def stop_all(self):
        with self._lock:
            items = list(self._streams.items())
            self._streams.clear()
        for _name, s in items:
            s.stop()

    def _atexit_cleanup(self):
        """进程退出兜底: 清临时目录残留分片"""
        self.stop_all()
        shutil.rmtree(ROOT_DIR, ignore_errors=True)


_manager: HlsManager | None = None
_manager_lock = threading.Lock()


def get_hls_manager() -> HlsManager:
    """进程内唯一 HlsManager"""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = HlsManager()
        return _manager


def live_url_for(host: str, port, cam: str) -> str:
    """拼接 m3u8 播放地址(cam经URL编码, 播放器按扩展名嗅探HLS格式)"""
    return f"http://{host}:{port}/camera/live/index.m3u8?cam={urllib.parse.quote(cam)}"


def seg_base_url(cam: str) -> str:
    """分片URI前缀(写入playlist): 绝对路径带query, 播放器按原样解析"""
    return f"/camera/live/segment.ts?cam={urllib.parse.quote(cam)}&seg="
