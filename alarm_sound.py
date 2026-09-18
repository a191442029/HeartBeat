"""
本地报警音播放模块 (Windows MCI, 零新依赖)
- 用 winmm.dll mciSendString 直接播放 mp3(Windows自带解码, PyInstaller无需额外打包QtMultimedia)
- 循环播放指定时长后自动停止; 重复触发先停旧实例再开新的
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

    def play(self, seconds: int = 10):
        """循环播放报警音seconds秒(重复调用=重新开始一轮); 文件缺失/播放失败仅记日志"""
        with self._lock:
            self._close_locked()
            path = _default_sound()
            if not path:
                logger.warning("报警音效文件缺失(music/报警.mp3), 跳过本地报警声")
                return
            if self._cmd(f'open "{path}" type mpegvideo alias {_ALIAS}'):
                self._open = True
                if self._cmd(f"play {_ALIAS} repeat"):
                    t = threading.Timer(seconds, self.stop)
                    t.daemon = True
                    t.start()
                    self._stop_timer = t
                    logger.info(f"本地报警声播放中: {os.path.basename(path)} ({seconds}秒)")
                    return
            logger.warning("报警音效MCI播放失败, 跳过本地报警声")
            self._close_locked()

    def stop(self):
        """立即停止并释放(线程安全, 幂等)"""
        with self._lock:
            self._close_locked()


_instance = None


def play(seconds: int = 10):
    """播放本地报警音(模块级入口)"""
    global _instance
    if _instance is None:
        _instance = _AlarmSound()
    _instance.play(seconds)


def stop():
    """停止本地报警音(模块级入口)"""
    if _instance is not None:
        _instance.stop()
