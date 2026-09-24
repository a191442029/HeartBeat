# -*- coding: utf-8 -*-
"""摄像头流管理: ffmpeg 环形缓冲(报警前录像) + 报警剪辑 + 实时画面帧源

架构要点(与《摄像头联动方案评估.md》一致):
- 报警联动摄像头 7x24 持续取流: ffmpeg -c copy 转封装成 MPEGTS 吐到 stdout,
  进程内环形缓冲仅保留约 buffer_seconds 秒(按4Mbps码率上限预留约32MB/路),
  全程不碰磁盘不解码
- 报警触发时 cut_clip(): 环形缓冲尾部 pre_seconds 秒落盘 → mp4 + 封面帧,
  这是唯一触碰 SSD 的时机(每路 2~4MB)
- 实时画面独立按需解码: ffmpeg rawvideo(bgr24) 管道, UI 用 QTimer 拉最新帧,
  关闭画面即销毁进程
"""
from __future__ import annotations

import atexit
import json
import os
import re
import subprocess
import sys
import threading
import time
import weakref
from collections import deque
from pathlib import Path

import system_utils as _su
from .onvif_client import OnvifClient, OnvifError, detect_onvif_port


def _log(level: str, msg: str):
    """安全日志: 独立工具/测试场景下 system_utils.logger 可能尚未初始化"""
    lg = _su.logger  # 晚绑定: 应用启动时 getlogger() 会重新赋值
    if lg is not None:
        getattr(lg, level)(msg)

CREATES_DIR = Path("captures")  # 报警片段存档目录(相对工作目录)
_SAFE_FILE = re.compile(r'[\\/:*?"<>|\r\n\t]+')  # Windows文件名非法字符(摄像头名清洗用)

# windowed EXE 中启动 ffmpeg(控制台程序)会弹出黑窗, 统一压制
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# RTSP 输入超时参数(微秒, 5秒): 断流/无数据时 ffmpeg 自行报错退出, 由各读循环的
# EOF→重连逻辑接管, 避免 stdout.read 永久阻塞; 必须置于 -i 之前
RTSP_INPUT_OPTS = ["-rw_timeout", "5000000", "-stimeout", "5000000"]

# ---- 子进程兜底回收: Win32 Job Object(kill-on-close) 为主 + atexit 遍历补杀 ----
# 主进程无论正常退出还是崩溃/被强杀, 内核关闭 Job 句柄时都会连带终止所有 ffmpeg,
# 消除"主程序异常退出后 ffmpeg 孤儿进程持续拉流占带宽内存"的泄漏
_live_children: "weakref.WeakSet" = weakref.WeakSet()  # 弱引用: 正常清理后自动移出
_JOB_HANDLE = None
if os.name == "nt":
    try:
        import ctypes

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class _JOB_BASIC_LIMIT(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class _JOB_EXT_LIMIT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _JOB_BASIC_LIMIT),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        _JOB_HANDLE = ctypes.windll.kernel32.CreateJobObjectW(None, None)
        if _JOB_HANDLE:
            _info = _JOB_EXT_LIMIT()
            _info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not ctypes.windll.kernel32.SetInformationJobObject(
                    _JOB_HANDLE, 9, ctypes.byref(_info), ctypes.sizeof(_info)):
                _JOB_HANDLE = None
    except Exception:
        _JOB_HANDLE = None


def _attach_job(proc: subprocess.Popen) -> None:
    """把子进程挂入 kill-on-close Job(失败不影响运行, 由 atexit 兜底)"""
    if os.name != "nt" or not _JOB_HANDLE:
        return
    try:
        import ctypes
        ctypes.windll.kernel32.AssignProcessToJobObject(_JOB_HANDLE, int(proc._handle))
    except Exception:
        pass


def _atexit_kill_children() -> None:
    """正常退出兜底: 补杀仍在运行的子进程(Job Object 失败/不支持时)"""
    for p in list(_live_children):
        try:
            if p.poll() is None:
                p.kill()
        except Exception:
            pass


atexit.register(_atexit_kill_children)


def _spawn(cmd: list, **kw) -> subprocess.Popen:
    """统一子进程创建入口: 登记 atexit 补杀名单 + 挂 Job Object"""
    proc = subprocess.Popen(cmd, **kw)
    _live_children.add(proc)
    _attach_job(proc)
    return proc


def find_ffmpeg() -> str:
    """定位 ffmpeg: PATH → PyInstaller 解包目录 → 本地 bin/ 目录"""
    candidates = ["ffmpeg"]
    exe_dir = Path(sys.executable).parent
    if getattr(sys, "frozen", False):
        candidates += [str(exe_dir / "ffmpeg.exe"), str(exe_dir / "_internal" / "ffmpeg.exe")]
    candidates += [str(exe_dir / "bin" / "ffmpeg.exe"), str(Path(__file__).resolve().parent.parent / "bin" / "ffmpeg.exe")]
    for c in candidates:
        try:
            r = subprocess.run([c, "-version"], capture_output=True, timeout=10,
                               creationflags=NO_WINDOW)
            if r.returncode == 0:
                return c
        except (OSError, subprocess.SubprocessError):
            continue
    raise FileNotFoundError("未找到 ffmpeg, 请将其放入程序目录 bin/ 下或系统 PATH")


class _RingBuffer:
    """线程安全的定长字节环形缓冲(deque 分块存储: 追加只进尾、淘汰只出头,
    消除 bytearray 头部删除的 O(n) memmove; snapshot/tail 按需拼接)"""

    def __init__(self, max_bytes: int):
        self._chunks: deque = deque()
        self._size = 0
        self._max = max_bytes
        self._lock = threading.Lock()

    def append(self, data: bytes):
        if not data:
            return
        with self._lock:
            self._chunks.append(data)  # bytes 不可变, 直接持有引用零拷贝
            self._size += len(data)
            while self._size > self._max and self._chunks:
                self._size -= len(self._chunks.popleft())  # O(1), 无整段搬移

    def snapshot(self) -> bytes:
        with self._lock:
            return b"".join(self._chunks)

    def tail(self, frac: float) -> bytes:
        """取尾部约 frac 比例的数据(整块粒度), 只拼接所需块避免全量拷贝"""
        with self._lock:
            target = int(self._size * frac)
            parts = []
            got = 0
            for chunk in reversed(self._chunks):
                parts.append(chunk)
                got += len(chunk)
                if got >= target:
                    break
        return b"".join(reversed(parts))

    def __len__(self) -> int:
        with self._lock:
            return self._size

    def clear(self):
        with self._lock:
            self._chunks.clear()
            self._size = 0


def _ts_tail(data: bytes, frac: float) -> bytes:
    """取缓冲尾部 frac 比例的 TS 数据, 并对齐到 188 字节包边界(0x47 同步字节):
    MPEGTS 是广播流设计, ffmpeg 可从任意完整包起点同步, 因此可直接经 stdin
    喂给 ffmpeg, 全程内存管道, 不再产生磁盘临时文件"""
    tail = data[-int(len(data) * frac):] if len(data) > 8192 else data
    for i in range(0, min(len(tail) - 188, 4096)):
        if tail[i] == 0x47 and tail[i + 188] == 0x47:
            return tail[i:]
    return tail


def _feed_and_wait(proc: subprocess.Popen, payload: bytes, timeout: float = 30) -> int:
    """后台线程写stdin(管道写阻塞不拖死剪辑线程), 主线程限时wait;
    超时kill进程令写端BrokenPipe退出 —— 修复剪辑进程因管道/ffmpeg卡死而泄漏"""
    def _body():
        try:
            proc.stdin.write(payload)
        except OSError:
            pass  # ffmpeg 按 -t 提前退出/被kill后管道断开, 属正常截断
        try:
            proc.stdin.close()
        except OSError:
            pass

    t = threading.Thread(target=_body, daemon=True)
    t.start()
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        CameraStream._kill(proc)
        raise


def cleanup_captures(mode: int = 0, days: int = 30, max_mb: int = 1024) -> int:
    """按策略清理 captures/ 历史报警剪辑. mode: 0=不清理 1=按保留天数 2=按空间上限(MB).
    以 mp4 为主文件, 同名封面 jpg 跟随删除. 返回删除的剪辑组数"""
    if mode == 0 or not CREATES_DIR.exists():
        return 0
    try:
        clips = sorted((p for p in CREATES_DIR.glob("*.mp4") if p.is_file()),
                       key=lambda p: p.stat().st_mtime)  # 旧 → 新
    except OSError:
        return 0
    if not clips:
        return 0
    victims = []
    if mode == 1:
        limit = time.time() - max(1, days) * 86400
        victims = [p for p in clips if p.stat().st_mtime < limit]
    elif mode == 2:
        cap = max(50, max_mb) * 1024 * 1024
        total = sum(p.stat().st_size for p in clips)
        for p in clips:  # 从最旧开始删, 直到总占用回到上限内
            if total <= cap:
                break
            try:
                total -= p.stat().st_size
            except OSError:
                continue
            victims.append(p)
    removed = 0
    for p in victims:
        try:
            p.unlink()
            jpg = p.with_suffix(".jpg")
            if jpg.exists():
                jpg.unlink()
            removed += 1
        except OSError:
            continue
    if removed:
        _log("info", f"[摄像头] 历史剪辑清理: 删除 {removed} 组过期剪辑")
    return removed


class CameraStream:
    """单个摄像头: 报警缓冲(常驻) + 实时画面(按需)"""

    def __init__(self, cfg: dict, buffer_seconds: int = 65, on_status=None):
        self.cfg = dict(cfg)
        self.buffer_seconds = buffer_seconds
        self.on_status = on_status or (lambda s, m="": None)
        # 按码流上限预留: 子码流 ~4Mbps(500KB/s), 高清剪辑(主码流) ~8Mbps(1MB/s);
        # 保证 pre_seconds=20 的剪辑仍有完整缓冲 + GOP 余量(修复高码率下缓冲时长不足)
        rate = 1_000_000 if bool(self.cfg.get("clip_main")) else 500_000
        self._ring = _RingBuffer(max_bytes=int(rate * buffer_seconds))
        self._ffmpeg = None  # 延迟到启动时定位, 避免导入期报错
        self._buf_proc: subprocess.Popen | None = None
        self._buf_thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._uri_cache = {}  # {"main"/"sub": (uri, resolved_at)}, 主/子码流分别缓存
        # 实时画面(异步打开: _live_ctl 保护 gen/opening/proc, 防调用线程与worker竞态)
        self._live_proc: subprocess.Popen | None = None
        self._live_thread: threading.Thread | None = None
        self._live_lock = threading.Lock()
        self._live_ctl = threading.Lock()
        self._live_gen = 0
        self._live_opening = False
        self._live_frame: bytes | None = None
        self._live_size = (640, 360)
        # 报警快照(MJPEG): 报警期间手机2fps轮询用, 空闲自动停, 不落盘
        self._snap_proc: subprocess.Popen | None = None
        self._snap_thread: threading.Thread | None = None
        self._snap_data_lock = threading.Lock()
        self._snap_buf = bytearray()
        self._snap_frame: bytes | None = None
        self._snap_last_req = 0.0
        self._snap_gen = 0
        self._snap_opening = False
        self._alarm_snap_hold = False  # 报警保活: True时UI侧snapshot_stop不杀报警快照流

    # ---------- 状态 ----------

    @property
    def status(self) -> str:
        if self._live_proc is not None:
            return "live"
        if self._buf_proc is not None:
            return "buffering"
        return "stopped"

    def _set_status(self, s: str, msg: str = ""):
        try:
            self.on_status(s, msg)
        except Exception:
            pass

    # ---------- 取流地址 ----------

    def resolve_rtsp_uri(self, force=False, main: bool = False) -> str:
        """经 ONVIF 取 RTSP 地址(主/子码流分别缓存1小时, 失败时自动重试)
        main=True 取主码流(报警高清剪辑用), 否则子码流(实时画面/快照用)"""
        kind = "main" if main else "sub"
        cached = self._uri_cache.get(kind)
        if not force and cached and time.time() - cached[1] < 3600:
            return cached[0]
        c = OnvifClient(self.cfg["ip"], self.cfg.get("onvif_port", 80),
                        self.cfg.get("username", ""), self.cfg.get("password", ""))
        token, _name, res = c.pick_profile(prefer_sub=not main)
        uri = c.get_stream_uri(token)
        _log("info", f"[摄像头:{self.cfg.get('name')}] {'主码流' if main else '子码流'} {res} → {uri.split('@')[-1]}")
        self._uri_cache[kind] = (uri, time.time())
        return uri

    # ---------- 报警缓冲(常驻线程) ----------

    def start_buffering(self):
        if self._buf_thread and self._buf_thread.is_alive():
            return
        self._stop_evt.clear()
        self._buf_thread = threading.Thread(target=self._buffer_loop, daemon=True,
                                            name=f"cambuf-{self.cfg.get('name', '')}")
        self._buf_thread.start()

    def stop_buffering(self):
        self._stop_evt.set()
        self._kill(self._buf_proc)
        self._buf_proc = None

    def _buffer_loop(self):
        backoff = 2
        # 高清剪辑开关: 勾选后缓冲改取主码流(剪辑2K级), 实时画面/快照仍走子码流
        clip_main = bool(self.cfg.get("clip_main"))
        while not self._stop_evt.is_set():
            proc = None
            try:
                if self._ffmpeg is None:
                    self._ffmpeg = find_ffmpeg()
                kind = "main" if clip_main else "sub"
                uri = self.resolve_rtsp_uri(force=kind not in self._uri_cache, main=clip_main)
                cmd = [
                    self._ffmpeg, "-hide_banner", "-loglevel", "error",
                    "-rtsp_transport", "tcp", *RTSP_INPUT_OPTS, "-i", uri,
                    "-c", "copy", "-f", "mpegts", "pipe:1",
                ]
                proc = _spawn(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL,
                              creationflags=NO_WINDOW)
                self._buf_proc = proc
                self._set_status("buffering")
                backoff = 2
                assert proc.stdout is not None
                # 局部引用读取: 防止 stop_buffering 置 None 后本线程误触 None.stdout
                while not self._stop_evt.is_set():
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    self._ring.append(chunk)
                self._set_status("reconnecting", "流中断, 5秒后重连")
            except Exception as e:
                _log("warning", f"[摄像头:{self.cfg.get('name')}] 缓冲线程异常: {e}")
                self._set_status("reconnecting", str(e))
            self._kill(proc)
            if self._buf_proc is proc:  # 只清自己的引用, 防误清新一轮进程
                self._buf_proc = None
            self._stop_evt.wait(backoff)
            backoff = min(backoff * 2, 60)
        self._set_status("stopped")

    # ---------- 报警剪辑(唯一落盘时机) ----------

    def cut_clip(self, pre_seconds: int = 10) -> tuple | None:
        """把环形缓冲尾部 pre_seconds 秒存为 mp4+封面帧, 返回 (mp4路径, 封面路径);
        封面帧失败时降级返回 (mp4路径, None), 不丢成品 mp4"""
        if len(self._ring) < 200_000:  # 缓冲几乎为空(刚启动/断流), 放弃
            _log("warning", f"[摄像头:{self.cfg.get('name')}] 缓冲不足, 放弃剪辑")
            return None
        mp4 = jpg = None
        mp4_ok = jpg_ok = False  # 产物有效标记(True 则失败清理时保留)
        try:
            if self._ffmpeg is None:
                self._ffmpeg = find_ffmpeg()
            CREATES_DIR.mkdir(exist_ok=True)
            # 摄像头名清洗: 去除Windows文件名非法字符, 防止建文件失败丢剪辑/路径逃逸
            safe_name = _SAFE_FILE.sub("_", str(
                self.cfg.get("name") or self.cfg.get("ip") or "cam"))[:80].strip(". ") or "cam"
            # 时间戳追加毫秒后缀: 报警剪辑与移动侦测剪辑同秒触发同一路时防文件名碰撞
            # (两路 ffmpeg 并发写同一 mp4 会互损)
            base = f"{safe_name}_{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}"
            mp4 = CREATES_DIR / f"{base}.mp4"
            jpg = CREATES_DIR / f"{base}.jpg"
            # 全内存管道剪辑: 从缓冲尾部切 TS 片段直接喂 ffmpeg stdin
            # (剪辑源取尾部 50% ≈ 30秒余量, 覆盖 pre_seconds + GOP 丢帧余量;
            #  copy 模式从第一个关键帧起写, 输出按 -t 截断)
            clip_src = _ts_tail(self._ring.tail(0.5), 1.0)
            p = _spawn([
                self._ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-i", "pipe:0",
                "-c", "copy", "-t", str(pre_seconds), "-movflags", "+faststart", str(mp4),
            ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
            if _feed_and_wait(p, clip_src) != 0:
                raise RuntimeError("mp4 管道剪辑失败")
            if not mp4.exists() or mp4.stat().st_size == 0:
                raise RuntimeError("mp4 生成失败")
            mp4_ok = True
            # 封面帧: 从成品 mp4 末尾3秒取第一帧(=报警时刻最新画面);
            # 修复原实现"取缓冲尾15%首帧"导致封面比报警时刻旧8~10秒的问题
            p2 = _spawn([
                self._ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-sseof", "-3", "-i", str(mp4),
                "-frames:v", "1", "-q:v", "4", str(jpg),
            ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
            try:
                if p2.wait(timeout=30) != 0:
                    raise RuntimeError("封面帧提取失败")
            except subprocess.TimeoutExpired:
                self._kill(p2)
                raise
            jpg_ok = True
            _log("info", f"[摄像头:{self.cfg.get('name')}] 报警剪辑已存: {mp4.name}")
            return str(mp4), str(jpg)
        except Exception as e:
            if mp4_ok:
                # mp4 已成功仅封面失败 → 降级只返回 mp4, 不丢成品
                _log("warning", f"[摄像头:{self.cfg.get('name')}] 封面帧提取失败, 降级仅保留mp4: {e}")
                return str(mp4), None
            _log("error", f"[摄像头:{self.cfg.get('name')}] 剪辑失败: {e}")
            return None
        finally:
            # 失败路径清理: 删除本次调用产生的无效产物(零字节/被中断的半成品),
            # 防孤儿文件; 有效成品(mp4_ok/jpg_ok)不在此删除
            for f, ok in ((mp4, mp4_ok), (jpg, jpg_ok)):
                if f is None or ok:
                    continue
                try:
                    if f.exists():
                        f.unlink()
                except Exception:
                    pass

    # ---------- 实时画面(按需) ----------

    def live_start(self, size=(640, 360)) -> bool:
        """打开实时画面解码进程(独立于缓冲链路)。
        URI解析/起ffmpeg在工作线程执行, 立即返回True(已受理), 成败经 on_status 回报;
        修复原实现在GUI线程/事件循环线程同步做ONVIF解析导致界面卡死数秒的问题"""
        with self._live_ctl:
            if self._live_proc is not None or self._live_opening:
                self._live_size = size
                return True
            self._live_gen += 1
            gen = self._live_gen
            self._live_opening = True
        threading.Thread(target=self._live_open_worker, args=(size, gen), daemon=True,
                         name=f"camlive-open-{self.cfg.get('name', '')}").start()
        return True

    def _live_open_worker(self, size, gen: int):
        """工作线程: 解析URI + 起ffmpeg; 完成前被stop/重启(gen不匹配)则自弃进程"""
        proc = None
        try:
            if self._ffmpeg is None:
                self._ffmpeg = find_ffmpeg()
            uri = self.resolve_rtsp_uri()
            w, h = size
            cmd = [
                self._ffmpeg, "-hide_banner", "-loglevel", "error",
                "-rtsp_transport", "tcp", *RTSP_INPUT_OPTS, "-i", uri,
                "-vf", f"scale={w}:{h}", "-r", "15",
                "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1",
            ]
            proc = _spawn(cmd, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, bufsize=w * h * 3,
                          creationflags=NO_WINDOW)
        except Exception as e:
            _log("error", f"[摄像头:{self.cfg.get('name')}] 实时画面启动失败: {e}")
            self._kill(proc)
            with self._live_ctl:
                self._live_opening = False
                failed = (gen == self._live_gen)
            if failed:
                self._set_status("error", str(e))
            return
        with self._live_ctl:
            if gen != self._live_gen:  # 打开期间被 live_stop/重启取代, 自弃防泄漏
                self._kill(proc)
                self._live_opening = False
                return
            self._live_size = size
            self._live_proc = proc
            self._live_opening = False
        self._live_thread = threading.Thread(target=self._live_loop, daemon=True,
                                             name=f"camlive-{self.cfg.get('name', '')}")
        self._live_thread.start()
        self._set_status("live")

    def _live_loop(self):
        proc = self._live_proc  # 局部引用: 防stop/重启后误读新进程stdout致花屏
        if proc is None or proc.stdout is None:
            return
        w, h = self._live_size
        frame_len = w * h * 3
        while proc is self._live_proc:
            data = proc.stdout.read(frame_len)
            if not data or len(data) < frame_len:
                break
            with self._live_lock:
                self._live_frame = data

    def live_frame(self, size=None):
        """返回 (bgr bytes, w, h) 或 None"""
        with self._live_ctl:
            opening = self._live_opening
        if opening:
            return None
        if size and tuple(size) != tuple(self._live_size):
            self.live_restart(size)
            return None
        with self._live_lock:
            if self._live_frame is None:
                return None
            w, h = self._live_size
            return self._live_frame, w, h

    def live_stop(self):
        """销毁实时画面进程并释放内存(单实例规则)"""
        with self._live_ctl:
            self._live_gen += 1
            self._live_opening = False
            proc = self._live_proc
            self._live_proc = None
        self._kill(proc)
        with self._live_lock:
            self._live_frame = None
        self._set_status("buffering" if self._buf_proc is not None else "stopped")

    def live_restart(self, size):
        self.live_stop()
        self.live_start(size)

    # ---------- 报警快照(按需MJPEG, 手机报警期间轮询) ----------

    SNAP_IDLE_SECONDS = 10  # 无人轮询超过该秒数自动停掉解码进程

    def snapshot_start(self, size=(640, 360)):
        """启动MJPEG快照流(报警开始时调用; 幂等; URI解析/起ffmpeg在工作线程,
        避免在Qt主线程/事件循环线程同步做ONVIF解析卡死数秒)"""
        with self._snap_data_lock:
            self._snap_last_req = time.time()
            if self._snap_proc is not None or self._snap_opening:
                return
            self._snap_opening = True
            self._snap_gen += 1
            gen = self._snap_gen
        threading.Thread(target=self._snapshot_open_worker, args=(size, gen), daemon=True,
                         name=f"camsnap-open-{self.cfg.get('name', '')}").start()

    def _snapshot_open_worker(self, size, gen: int):
        """工作线程: 解析URI + 起ffmpeg; 完成前被stop(gen不匹配)/已在跑则自弃进程"""
        proc = None
        try:
            if self._ffmpeg is None:
                self._ffmpeg = find_ffmpeg()
            uri = self.resolve_rtsp_uri()
            w, h = size
            cmd = [
                self._ffmpeg, "-hide_banner", "-loglevel", "error",
                "-rtsp_transport", "tcp", *RTSP_INPUT_OPTS, "-i", uri,
                "-vf", f"scale={w}:{h}", "-r", "5",
                "-f", "mjpeg", "-q:v", "5", "pipe:1",
            ]
            proc = _spawn(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          creationflags=NO_WINDOW)
        except Exception as e:
            _log("error", f"[摄像头:{self.cfg.get('name')}] 快照流启动失败: {e}")
            self._kill(proc)
            with self._snap_data_lock:
                self._snap_opening = False
            return
        with self._snap_data_lock:
            stale = (gen != self._snap_gen or self._snap_proc is not None)
            if not stale:
                self._snap_proc = proc
                self._snap_buf = bytearray()
                self._snap_opening = False
        if stale:  # 双启动竞态/期间被stop: 后来者自弃
            self._kill(proc)
            return
        self._snap_thread = threading.Thread(target=self._snap_loop, daemon=True,
                                             name=f"camsnap-{self.cfg.get('name', '')}")
        self._snap_thread.start()
        _log("info", f"[摄像头:{self.cfg.get('name')}] 报警快照流已启动")

    def _snap_loop(self):
        proc = self._snap_proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                if time.time() - self._snap_last_req > self.SNAP_IDLE_SECONDS:
                    break  # 无人轮询, 自动停
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                with self._snap_data_lock:
                    self._snap_buf.extend(chunk)
                self._extract_jpegs()
        except Exception:
            pass
        finally:
            self._snap_cleanup(proc)

    def _snap_cleanup(self, proc: subprocess.Popen):
        """快照流自身退出(EOF/空闲)后的进程级清理, 不受报警保活影响"""
        with self._snap_data_lock:
            if self._snap_proc is proc:
                self._snap_proc = None
                self._snap_last_req = 0.0
                self._snap_buf = bytearray()
                self._snap_frame = None
        self._kill(proc)

    def _extract_jpegs(self):
        """按 0xFFD8...0xFFD9 标记从缓冲切出JPEG帧, 只保留最新一帧"""
        with self._snap_data_lock:
            buf = self._snap_buf
            while True:
                start = buf.find(b"\xff\xd8\xff")
                if start < 0:
                    buf.clear()
                    return
                if start > 0:
                    del buf[:start]
                end = buf.find(b"\xff\xd9", 3)
                if end < 0:
                    if len(buf) > 2_000_000:  # 防异常流无限增长
                        buf.clear()
                    return
                self._snap_frame = bytes(buf[:end + 2])
                del buf[:end + 2]

    def snapshot_jpeg(self):
        """返回最新JPEG帧(手机轮询入口), 同时刷新活跃时间防空闲自停; 无则None"""
        self._snap_last_req = time.time()
        with self._snap_data_lock:
            return self._snap_frame

    def snapshot_stop(self, force: bool = False):
        """停快照流并释放(报警结束/空闲超时/UI停止; 幂等)。
        force=False 时报警保活(_alarm_snap_hold)中的流跳过停止,
        修复报警期间打开摄像头页/页面隐藏误杀手机快照流的问题"""
        with self._snap_data_lock:
            if not force and self._alarm_snap_hold:
                return
            self._snap_gen += 1
            self._snap_opening = False
            proc = self._snap_proc
            self._snap_proc = None
            self._snap_last_req = 0.0
            self._snap_buf = bytearray()
            self._snap_frame = None
        if proc is not None:
            self._kill(proc)

    # ---------- 通用 ----------

    @staticmethod
    def _kill(proc: subprocess.Popen | None):
        if proc is None:
            return
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            pass


class CameraManager:
    """全部摄像头的生命周期 + 配置桥接"""

    def __init__(self, buffer_seconds: int = 65):
        self.streams: dict[str, CameraStream] = {}
        self.buffer_seconds = buffer_seconds
        self._motion_watcher = None  # 惰性创建(rebuild时), 避免导入期循环依赖

    @staticmethod
    def load_configs() -> list:
        from system_utils import gs
        raw = gs("Camera", "cameras", "[]", str, "-Camera")
        try:
            return json.loads(raw) if raw else []
        except Exception:
            _log("warning", "[Camera] cameras 配置损坏, 按空列表处理")
            return []

    @staticmethod
    def save_configs(cameras: list):
        from system_utils import update_settings
        update_settings(Camera={"cameras": json.dumps(cameras, ensure_ascii=False)})

    def rebuild(self, cameras: list, on_status=None):
        """按新配置重建所有流(报警联动的启动缓冲, 其余仅待命)。
        名称重复时仅保留第一个(名称是 streams 字典键与剪辑记录 cam 字段),
        修复重名相互覆盖导致其中一路永不缓冲、剪辑缺失的问题"""
        self.stop_all()
        self.streams = {}
        for cfg in cameras:
            key = cfg.get("name") or cfg.get("ip", "cam")
            if key in self.streams:
                _log("warning", f"[摄像头] 名称重复: {key}, 仅保留第一个, 请修改配置")
                continue
            self.streams[key] = CameraStream(cfg, buffer_seconds=self.buffer_seconds,
                                             on_status=on_status)
        self.start_alarm_buffers()
        # 移动侦测线程池: 与流同步重建(motion_enabled 摄像头双源侦测)
        from .motion_watch import MotionWatcher
        if self._motion_watcher is None:
            self._motion_watcher = MotionWatcher()
        self._motion_watcher.rebuild(cameras, self)

    def start_alarm_buffers(self):
        for s in self.streams.values():
            # 报警联动与移动侦测都依赖常驻环形缓冲(侦测触发时剪辑前10秒)
            if s.cfg.get("alarm_enabled") or s.cfg.get("motion_enabled"):
                s.start_buffering()

    def stop_all(self):
        if self._motion_watcher is not None:
            self._motion_watcher.stop_all()
        for s in self.streams.values():
            s.live_stop()
            s.stop_buffering()

    def default_stream(self) -> CameraStream | None:
        for s in self.streams.values():
            if s.cfg.get("is_default"):
                return s
        return next(iter(self.streams.values()), None)

    def get(self, name: str) -> CameraStream | None:
        return self.streams.get(name)

    # ---- 报警快照(期3: 手机端报警视频面板) ----

    def default_name(self) -> str:
        """默认摄像头名称(与剪辑记录里的 cam 字段对齐)"""
        s = self.default_stream()
        return (s.cfg.get("name") or s.cfg.get("ip", "cam")) if s else ""

    def snapshot_start_default(self):
        """报警开始: 启动默认摄像头的快照流(供 /camera/snapshot 轮询), 并挂报警保活
        (保活期间 UI 侧 snapshot_stop 不生效, 防止打开摄像头页误杀手机快照)"""
        self.snapshot_start_for("")

    def snapshot_start_for(self, name: str):
        """报警视频联动: 启动指定名称摄像头的快照流(绑定房间画面),
        名称无效/为空回退默认摄像头; 保活语义与 snapshot_start_default 一致"""
        s = self.streams.get(name) if name else None
        if s is None:
            s = self.default_stream()
        if s is not None:
            s._alarm_snap_hold = True
            s.snapshot_start()

    def snapshot_stop_all(self):
        """报警结束: 解除保活并停掉所有快照流"""
        for s in self.streams.values():
            s._alarm_snap_hold = False
            s.snapshot_stop(force=True)

    def snapshot_stop_ui_all(self):
        """UI页面隐藏: 停全部快照流(报警保活中的流除外)"""
        for s in self.streams.values():
            s.snapshot_stop()

    def default_snapshot_jpeg(self):
        """默认摄像头最新JPEG帧(无默认摄像头/未启动返回None)"""
        s = self.default_stream()
        return s.snapshot_jpeg() if s is not None else None

    def snapshot_jpeg_by_name(self, name: str):
        """报警视频联动: 指定名称摄像头最新JPEG帧(名称未知视为未绑定回退默认);
        已知名称但帧未就绪返回None(接收端显示等待占位, 不回退默认防看错房间)"""
        s = self.streams.get(name) if name else None
        if s is None:
            return self.default_snapshot_jpeg()
        return s.snapshot_jpeg()

    def cut_clips_for_alarm(self, pre_seconds: int = 10) -> list:
        """报警触发: 所有报警联动摄像头并行剪辑报警前片段(每路一个线程, 阻塞至全部完成,
        调用方应放在后台线程)。返回 [{"cam": 名称, "mp4": 路径, "jpg": 封面}], 仅含成功项"""
        streams = [s for s in self.streams.values() if s.cfg.get("alarm_enabled")]
        clips: list = []
        lock = threading.Lock()

        def cut_one(s: CameraStream):
            r = s.cut_clip(pre_seconds)
            if r:
                with lock:
                    clips.append({"cam": s.cfg.get("name") or s.cfg.get("ip", "cam"),
                                  "mp4": r[0], "jpg": r[1]})

        threads = [threading.Thread(target=cut_one, args=(s,), daemon=True,
                                    name=f"alarmcut-{s.cfg.get('name', '')}") for s in streams]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)  # 单路剪辑最长约两个30s超时, 防单路卡死永久拖住报警链路
        return clips


# 模块级单例: 摄像头UI页与报警剪辑钩子(push_notifier联动)共享同一实例
_manager: CameraManager | None = None
_manager_lock = threading.Lock()


def get_manager() -> CameraManager:
    """进程内唯一 CameraManager(首次调用时创建, 配置由摄像头UI页加载时rebuild)"""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = CameraManager()
        return _manager
