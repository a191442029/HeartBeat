# -*- coding: utf-8 -*-
"""摄像头移动侦测: 双源事件(A: ONVIF事件订阅 / B: 本地帧差) → 剪辑存档 + 手机推送

背景: Arenti 等 APP 的报警推送走厂商私有云, 无法截获; 但"画面变动"事件的源头是
摄像头本身, 可经两条路直接获取:
  A. ONVIF PullPoint 长轮询: 局域网直连摄像头事件服务, 设备端移动侦测触发即上报
     (零解码开销, 支持度依固件而定, 失败自动退避重建, 不影响B路)
  B. 本地帧差: ffmpeg 1fps 输出 64x36 灰度 rawvideo, 帧均差超阈值判定画面变动
     (与品牌无关 100% 可行, 每秒2304字节纯Python比较, 开销可忽略)
双源事件经每摄像头独立冷却去重(先到先得)后:
  1. cut_clip() 剪辑前10秒存 captures/(与心率报警剪辑同机制)
  2. NotifierManager.notify_camera_motion 推送各渠道, 片段就绪后回填推送记录
"""
from __future__ import annotations

import subprocess
import threading
import time

import system_utils as _su
from .onvif_client import OnvifClient, EVENT_PATHS

# 帧差灵敏度阈值(帧均绝对差 0~255): 高/中/低
DIFF_THRESHOLDS = (3.0, 6.0, 12.0)
FRAME_W, FRAME_H = 64, 36  # 帧差采样分辨率(灰度1字节/像素 = 2304字节/帧)
ONVIF_ERR_LOG_INTERVAL = 60  # ONVIF失败日志去重间隔(秒), 防固件不支持时刷屏

# 推送通知器引用(UI层启动时注入, 侦测触发时调用notify_camera_motion;
# 模块级而非实例级: watcher实例由CameraManager惰性创建, 生命周期解耦)
_notifier = None


def set_notifier(notifier):
    global _notifier
    _notifier = notifier


def _log(level: str, msg: str):
    lg = _su.logger
    if lg is not None:
        getattr(lg, level)(msg)


def _is_motion(topics: list, items: list) -> bool:
    """宽容判定一条通知是否为移动侦测: topic 或 SimpleItem 名/值含 motion;
    值可解析为布尔时需为真(标准设备上升沿/下降沿都发同名事件, 需过滤下降沿)"""
    hay = " ".join(topics).lower()
    for n, v in items:
        hay += f" {str(n).lower()} {str(v).lower()}"
    if "motion" not in hay and "moved" not in hay:
        return False
    for n, v in items:
        if "motion" in (str(n) + str(v)).lower():
            lv = str(v).strip().lower()
            if lv in ("false", "0", "off", "no", "inactive", "cleared"):
                return False
    return True


class MotionWatcher:
    """全部摄像头的移动侦测线程池(由 CameraManager 持有, 随配置 rebuild)"""

    def __init__(self):
        self._threads: dict[str, list] = {}   # 摄像头名 -> [线程, ...]
        self._stop_evt = threading.Event()
        self._fire_lock = threading.Lock()
        self._last_fire: dict[str, float] = {}  # 摄像头名 -> 上次触发单调时间
        self._last_err_log = 0.0              # ONVIF 失败日志去重
        self._gen = 0                         # 线程代数: rebuild后旧代线程自弃
                                          # (旧线程可能阻塞在RTSP读取/ONVIF长轮询,
                                          #  仅靠共享Event在rebuild清标志后会复活续跑)

    def _alive(self, gen: int) -> bool:
        return not self._stop_evt.is_set() and self._gen == gen

    # ---------- 生命周期 ----------

    def rebuild(self, cameras: list, manager):
        """按新配置重建侦测线程(仅 motion_enabled 的摄像头)"""
        self._gen += 1  # 先使旧代线程失效(它们退出前不再共享停止标志)
        self._stop_evt.clear()
        self._last_fire.clear()
        gen = self._gen
        want = []
        for cfg in cameras:
            if not cfg.get("motion_enabled"):
                continue
            key = cfg.get("name") or cfg.get("ip", "cam")
            stream = manager.get(key)
            if stream is None:
                continue
            want.append((key, stream))
        for key, stream in want:
            threads = [
                threading.Thread(target=self._diff_loop, args=(stream, gen), daemon=True,
                                 name=f"camdiff-{key}"),
                threading.Thread(target=self._onvif_loop, args=(stream, gen), daemon=True,
                                 name=f"camonvif-{key}"),
            ]
            for t in threads:
                t.start()
            self._threads[key] = threads
            _log("info", f"[摄像头:{key}] 移动侦测已启动(ONVIF事件+本地帧差双源)")

    def stop_all(self):
        self._gen += 1
        self._stop_evt.set()
        self._threads = {}

    # ---------- 触发汇聚 ----------

    def _fire(self, stream, source: str):
        """移动侦测触发(两源共用): 每摄像头独立冷却 → 剪辑前10秒 → 推送+回填"""
        key = str(stream.cfg.get("name") or stream.cfg.get("ip", "cam"))
        cooldown = self._cooldown()
        now = time.monotonic()
        with self._fire_lock:
            if now - self._last_fire.get(key, -1e9) < cooldown:
                return  # 冷却期内(双源并发/连续侦测去重)
            self._last_fire[key] = now
        _log("info", f"[摄像头:{key}] 移动侦测触发({source})")
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        clips = []
        try:
            r = stream.cut_clip(10)  # 环形缓冲不足时返回None(仅推送不带片段)
            if r:
                clips = [{"cam": key, "mp4": r[0], "jpg": r[1]}]
        except Exception as e:
            _log("warning", f"[摄像头:{key}] 侦测剪辑失败: {e}")
        if _notifier is None:
            return
        try:
            _notifier.notify_camera_motion(key, source, ts)
        except Exception as e:
            _log("error", f"[摄像头:{key}] 侦测推送失败: {e}")
            return
        if clips:  # 片段就绪后回填推送记录(推送记录为各渠道异步落盘, 轮询等待)
            threading.Thread(target=self._attach_clips, args=(ts, clips),
                             daemon=True, name="motion-attach").start()

    def _cooldown(self) -> int:
        from system_utils import gs
        return max(10, gs("Camera", "motion_cooldown", 60, int, "-Camera"))

    @staticmethod
    def _attach_clips(ts: str, clips: list):
        try:
            from push_notifier import load_push_history, set_alarm_clips
            deadline = time.time() + 20
            while time.time() < deadline:  # 等任一渠道记录先落盘(最多20秒)
                for rec in load_push_history():
                    if isinstance(rec, dict) and rec.get("alarm_ts") == ts:
                        set_alarm_clips(ts, clips)
                        return
                time.sleep(0.5)
        except Exception as e:
            _log("warning", f"侦测片段回填失败: {e}")

    # ---------- 路线B: 本地帧差(与品牌无关, 保底) ----------

    def _diff_loop(self, stream, gen: int):
        from .stream_manager import _spawn, NO_WINDOW, find_ffmpeg, RTSP_INPUT_OPTS
        key = str(stream.cfg.get("name") or stream.cfg.get("ip", "cam"))
        backoff = 5
        frame_len = FRAME_W * FRAME_H
        while self._alive(gen):
            proc = None
            try:
                ffmpeg = find_ffmpeg()
                uri = stream.resolve_rtsp_uri()
                cmd = [
                    ffmpeg, "-hide_banner", "-loglevel", "error",
                    "-rtsp_transport", "tcp", *RTSP_INPUT_OPTS, "-i", uri,
                    "-vf", f"fps=1,scale={FRAME_W}:{FRAME_H}", "-f", "rawvideo",
                    "-pix_fmt", "gray", "pipe:1",
                ]
                proc = _spawn(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              creationflags=NO_WINDOW)
                backoff = 5
                prev = None
                while self._alive(gen):
                    data = proc.stdout.read(frame_len)  # BufferedReader.read 阻塞至满/EOF
                    if not data or len(data) < frame_len:
                        break
                    if prev is not None:
                        diff = sum(abs(a - b) for a, b in zip(data, prev)) / frame_len
                        if diff >= self._threshold():
                            self._fire(stream, "帧差")
                    prev = data
            except Exception as e:
                if time.time() - self._last_err_log > ONVIF_ERR_LOG_INTERVAL:
                    self._last_err_log = time.time()
                    _log("warning", f"[摄像头:{key}] 帧差侦测异常(5秒后重连): {e}")
            finally:
                try:
                    if proc is not None:
                        proc.kill()
                        proc.wait(timeout=3)
                except Exception:
                    pass
            self._stop_evt.wait(backoff)
            backoff = min(backoff * 2, 60)

    def _threshold(self) -> float:
        from system_utils import gs
        idx = gs("Camera", "motion_sensitivity", 1, int, "-Camera")
        return DIFF_THRESHOLDS[max(0, min(2, idx))]

    # ---------- 路线A: ONVIF 事件订阅(设备端侦测直报) ----------

    def _onvif_loop(self, stream, gen: int):
        cfg = stream.cfg
        backoff = 30
        while self._alive(gen):
            try:
                c = OnvifClient(cfg["ip"], cfg.get("onvif_port", 80),
                                cfg.get("username", ""), cfg.get("password", ""),
                                timeout=12)  # PullMessages长轮询需大于设备保持时间
                sub = c.create_pullpoint_subscription()  # 内部已逐路径兜底
                last_renew = time.time()
                backoff = 30
                while self._alive(gen):
                    if time.time() - last_renew > 540:  # 9分钟续期(订阅期10分钟)
                        c.renew_subscription(sub)
                        last_renew = time.time()
                    topics, items = c.pull_messages(sub, 3)
                    if topics and _is_motion(topics, items):
                        self._fire(stream, "ONVIF")
            except Exception as e:
                if time.time() - self._last_err_log > ONVIF_ERR_LOG_INTERVAL:
                    self._last_err_log = time.time()
                    _log("info", f"[摄像头:{cfg.get('name')}] ONVIF事件不可用"
                                 f"(帧差侦测不受影响, 30秒后重试): {e}")
            self._stop_evt.wait(backoff)
            backoff = min(backoff * 2, 300)
