"""
本地报警音播放模块 (Windows MCI, 零新依赖)
- 用 winmm.dll mciSendStringW 直接播放 mp3(Windows自带解码, PyInstaller无需额外打包QtMultimedia)
- 完整播放一遍(不循环), 播到文件自然结束自动释放; 重复触发先停旧实例再开新的
- 音效文件: 与EXE同目录的 music/报警.mp3 (缺失时回退 电子报警嘟嘟声.mp3, 再缺失则跳过仅记日志)
"""
import ctypes
import os
import threading

from system_utils import logger

_ALIAS = "hrmbuzz"
_winmm = None


def _win():
    global _winmm
    if _winmm is None:
        _winmm = ctypes.WinDLL("winmm")
    return _winmm


def _default_sound() -> str:
    """按优先级返回报警音效绝对路径, 找不到返回空串"""
    for name in ("报警.mp3", "电子报警嘟嘟声.mp3"):
        p = os.path.abspath(os.path.join("music", name))
        if os.path.isfile(p):
            return p
    return ""


class _AlarmSound:
    """MCI播放器单例: play()循环播放seconds秒后自动停; stop()立即停"""

    def __init__(self):
        self._lock = threading.Lock()
        self._open = False
        self._stop_timer = None

    def _cmd(self, s: str) -> bool:
        try:
            return _win().mciSendStringW(s, None, 0, 0) == 0
        except Exception as e:
            logger.error(f"MCI命令异常: {e}")
            return False

    def _close_locked(self):
        if self._stop_timer is not None:
            self._stop_timer.cancel()
            self._stop_timer = None
        if self._open:
            self._cmd(f"close {_ALIAS}")
            self._open = False

    def play(self, seconds: int = 0):
        """完整播放报警音一遍(不循环, 播到文件自然结束自动释放);
        seconds仅当MCI查不到文件时长时作兜底时长(秒, <=0取60)"""
        with self._lock:
            self._close_locked()
            path = _default_sound()
            if not path:
                logger.warning("报警音效文件缺失(music/报警.mp3), 跳过本地报警声")
                return
            if self._cmd(f'open "{path}" type mpegvideo alias {_ALIAS}'):
                self._open = True
                # 查询文件自然时长(ms), 失败时用seconds兜底
                buf = ctypes.create_unicode_buffer(64)
                ms = 0
                if _win().mciSendStringW(f"status {_ALIAS} length", buf, 64, 0) == 0:
                    try:
                        ms = int(buf.value.strip() or "0")
                    except ValueError:
                        ms = 0
                if ms <= 0:
                    ms = max(1, seconds) * 1000 if seconds > 0 else 60000
                # 不带repeat: 只播一遍到文件尾自然停止; 定时器仅负责播完后释放MCI资源
                if self._cmd(f"play {_ALIAS}"):
                    t = threading.Timer(ms / 1000.0 + 1.0, self.stop)
                    t.daemon = True
                    t.start()
                    self._stop_timer = t
                    logger.info(f"本地报警声播放中: {os.path.basename(path)} (播放1遍, 约{ms // 1000}秒)")
                    return
            logger.warning("报警音效MCI播放失败, 跳过本地报警声")
            self._close_locked()

    def stop(self):
        """立即停止并释放(线程安全, 幂等)"""
        with self._lock:
            self._close_locked()


_instance = None


def play(seconds: int = 0):
    """播放本地报警音(模块级入口): 完整播一遍, seconds仅作时长未知时的兜底"""
    global _instance
    if _instance is None:
        _instance = _AlarmSound()
    _instance.play(seconds)


def stop():
    """停止本地报警音(模块级入口)"""
    if _instance is not None:
        _instance.stop()
