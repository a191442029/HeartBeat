# -*- coding: utf-8 -*-
"""报警录像回放窗: ffmpeg 解码(rawvideo bgr24) + QTimer 渲染的内置播放器

- 单实例规则: 全局仅一个播放窗, 再次点击回放时销毁前一解码进程并装载新文件
- 播放/暂停: 暂停即暂停读取解码输出(ffmpeg 管道自然阻塞, 无需重启进程)
- 进度条拖动 seek: 以 -ss 重启解码进程(简单可靠)
- 时长从 ffmpeg stderr 的 Duration 行解析; 播放位置按 帧数/固定输出帧率 计算
- 关闭窗口即 kill 解码进程
"""
from __future__ import annotations

import re
import subprocess
import threading
import time

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QImage, QPixmap
from PyQt5.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton, QSlider,
                             QVBoxLayout)

from camera.stream_manager import find_ffmpeg

VW, VH = 640, 360
FPS = 25  # 解码输出强制帧率(位置按 帧数/FPS 计算, 与源帧率无关)

# 等比缩放+黑边填充: 任意分辨率片段统一输出 640x360 帧
_VF = (f"scale={VW}:{VH}:force_original_aspect_ratio=decrease,"
       f"pad={VW}:{VH}:(ow-iw)/2:(oh-ih)/2:black")

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


class VideoPlayerDialog(QDialog):
    """内置回放窗(非模态, 单实例)"""

    _instance: "VideoPlayerDialog | None" = None

    @classmethod
    def play(cls, parent, mp4_path: str, title: str = "报警录像"):
        """单实例播放入口: 已有窗口则复用并切换文件(销毁旧解码进程)"""
        inst = cls._instance
        if inst is not None:
            inst.load(mp4_path, title)
            inst.showNormal()
            inst.raise_()
            inst.activateWindow()
            return inst
        dlg = cls(parent, mp4_path, title)
        cls._instance = dlg
        dlg.show()
        return dlg

    def __init__(self, parent, mp4_path: str, title: str):
        super().__init__(parent)
        self.setModal(False)
        self.setWindowTitle(f"回放 - {title}")

        self._mp4 = ""
        self._proc: subprocess.Popen | None = None
        self._frame_lock = threading.Lock()
        self._frame: bytes | None = None   # 最新一帧 bgr bytes
        self._frames = 0                   # 当前seek点起已解码帧数
        self._seek = 0.0                   # 当前解码起点(秒)
        self._duration = 0.0
        self._eof = False
        self._pause = False
        self._dragging = False             # 进度条拖动中(不回写位置)

        lay = QVBoxLayout(self)
        self.video = QLabel("加载中...")
        self.video.setFixedSize(VW, VH)
        self.video.setAlignment(Qt.AlignCenter)
        self.video.setStyleSheet("background: black; color: #888; font-size: 12pt;")
        lay.addWidget(self.video)

        ctrl = QHBoxLayout()
        self.play_btn = QPushButton("暂停")
        self.play_btn.setFixedWidth(64)
        self.play_btn.clicked.connect(self.toggle_play)
        ctrl.addWidget(self.play_btn)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.sliderPressed.connect(self._on_slider_press)
        self.slider.sliderReleased.connect(self._on_seek)
        ctrl.addWidget(self.slider, 1)
        self.time_label = QLabel("0.0 / 0.0s")
        ctrl.addWidget(self.time_label)
        lay.addLayout(ctrl)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._render)
        self._timer.setInterval(40)  # 25fps

        self.load(mp4_path, title)

    # ---------- 装载/解码进程 ----------

    def load(self, mp4_path: str, title: str):
        """装载新文件: 销毁旧解码进程并从头播放(单实例切换时调用)"""
        self.setWindowTitle(f"回放 - {title}")
        self._stop_decoder()
        self._mp4 = mp4_path
        self._seek = 0.0
        self._duration = 0.0
        self._frames = 0
        self._eof = False
        self._pause = False
        self.play_btn.setText("暂停")
        self.video.setText("加载中...")
        self.time_label.setText("0.0 / 0.0s")
        self._start_decoder(0.0)
        self._timer.start()

    def _start_decoder(self, seek: float):
        try:
            ff = find_ffmpeg()
        except FileNotFoundError as e:
            self.video.setText(str(e))
            return
        cmd = [ff, "-hide_banner", "-loglevel", "info", "-ss", f"{seek:.3f}",
               "-i", self._mp4, "-vf", _VF, "-r", str(FPS),
               "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE)
        except Exception as e:
            self.video.setText(f"解码启动失败: {e}")
            return
        threading.Thread(target=self._read_loop, daemon=True, name="vidplay").start()
        threading.Thread(target=self._drain_stderr, daemon=True, name="viderr").start()

    def _stop_decoder(self):
        old = self._proc
        self._proc = None
        if old is not None:
            try:
                old.kill()
            except Exception:
                pass
            try:
                old.wait(timeout=2)
            except Exception:
                pass

    def _read_loop(self):
        """解码输出读取线程: 逐帧放入最新帧槽位(输出恒为 VWxVH bgr24)"""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        frame_len = VW * VH * 3
        stream = proc.stdout
        while proc is self._proc and proc.poll() is None:
            if self._pause:
                time.sleep(0.04)
                continue
            data = stream.read(frame_len)  # BufferedReader: 阻塞到满帧或EOF
            if not data or len(data) < frame_len:
                break
            with self._frame_lock:
                self._frame = data
            self._frames += 1
        if proc is self._proc:
            self._eof = True

    def _drain_stderr(self):
        """排空stderr避免管道阻塞, 并解析 Duration 行得到总时长"""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                if proc is not self._proc:
                    break
                if not self._duration:
                    m = _DURATION_RE.search(raw.decode("utf-8", "ignore"))
                    if m:
                        self._duration = (int(m.group(1)) * 3600
                                          + int(m.group(2)) * 60 + float(m.group(3)))
        except Exception:
            pass

    # ---------- 控制 ----------

    def toggle_play(self):
        if self._eof and not self._pause:
            self._restart_at(0.0)  # 播完再按=重播
            return
        self._pause = not self._pause
        self.play_btn.setText("继续" if self._pause else "暂停")

    def _on_slider_press(self):
        self._dragging = True

    def _on_seek(self):
        self._dragging = False
        if not self._duration:
            return
        self._restart_at(self.slider.value() / 1000 * self._duration)

    def _restart_at(self, pos: float):
        """从指定位置重启解码(seek即恢复播放)"""
        self._stop_decoder()
        self._seek = pos
        self._frames = 0
        self._eof = False
        self._pause = False
        self.play_btn.setText("暂停")
        with self._frame_lock:
            self._frame = None
        self._start_decoder(pos)

    # ---------- 渲染 ----------

    def _render(self):
        with self._frame_lock:
            frame = self._frame
        if frame is not None:
            img = QImage(frame, VW, VH, VW * 3, QImage.Format_BGR888)
            self.video.setPixmap(QPixmap.fromImage(img))
        # 位置回写(拖动中不回写)
        if self._duration and not self._dragging:
            pos = min(self._seek + self._frames / FPS, self._duration)
            self.slider.blockSignals(True)
            self.slider.setValue(int(pos / self._duration * 1000))
            self.slider.blockSignals(False)
            self.time_label.setText(f"{pos:.1f} / {self._duration:.1f}s")
        if self._eof:
            if frame is None and self._frames == 0:
                self.video.setText("解码失败或文件无法播放")
            elif not self._pause:
                self.play_btn.setText("重播")

    def closeEvent(self, event):
        self._timer.stop()
        self._stop_decoder()
        VideoPlayerDialog._instance = None
        super().closeEvent(event)
