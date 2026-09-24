# -*- coding: utf-8 -*-
"""摄像头设置页: 摄像头配置表格(ONVIF) + 主/从显示器实时画面

配置保存在 config.ini [Camera] 段, 密码经 DPAPI 加密。
报警联动摄像头在保存后立即启动后台环形缓冲(7x24, 不落盘不解码);
页面布局: 左下为主显示器(选中摄像头大画面), 右侧为从显示器缩略图列
(每路一个, 点击切换主画面), 随录入数量增长, 页面隐藏时全部停止省资源。
"""
from __future__ import annotations

import threading

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QImage, QPixmap
from PyQt5.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QGroupBox, QHBoxLayout,
                             QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
                             QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
                             QWidget)

from system_utils import logger, try_except, dpapi_protect, gs, ups
from .basicwidgets import group_layout, button_row, hint_label
from camera.stream_manager import CameraManager, get_manager, cleanup_captures
from camera.onvif_client import OnvifClient, OnvifError, detect_onvif_port

COLS = ["名称", "IP地址", "ONVIF端口", "用户名", "密码", "报警联动", "默认摄像头", "高清剪辑", "移动侦测"]
COL_NAME, COL_IP, COL_PORT, COL_USER, COL_PASS, COL_ALARM, COL_DEFAULT, COL_CLIP_MAIN, COL_MOTION = range(9)
VIDEO_W, VIDEO_H = 720, 405   # 主显示器
THUMB_W, THUMB_H = 256, 144   # 从显示器缩略图


class ThumbLabel(QLabel):
    """从显示器缩略图块: 点击把该路切到主显示器; 选中态红色描边"""

    def __init__(self, ui, name):
        super().__init__()
        self._ui = ui
        self.cam_name = name
        self.setFixedSize(THUMB_W, THUMB_H)
        self.setAlignment(Qt.AlignCenter)
        self.setText("连接中...")
        self.setStyleSheet(self._style(False))

    @staticmethod
    def _style(selected):
        border = "#E53935" if selected else "#333333"
        return (f"background: black; color: #888; font-size: 9pt; "
                f"border: 2px solid {border}; border-radius: 2px;")

    def set_selected(self, selected: bool):
        self.setStyleSheet(self._style(selected))

    def mousePressEvent(self, ev):
        self._ui.select_camera(self.cam_name)


class CameraUI(QWidget):
    """摄像头配置与实时画面页"""

    camera_settings_changed = pyqtSignal(list)      # 保存后发出(后续推送/联动模块用)
    status_changed = pyqtSignal(str, str)           # (摄像头名, 状态描述) 队列信号, 线程安全
    test_finished = pyqtSignal(bool, str)           # ONVIF 测试结果
    port_detected = pyqtSignal(str, int)            # (ip, port) 探测端口回主线程写表格

    @try_except("摄像头UI初始化")
    def __init__(self):
        super().__init__()
        self.manager = get_manager()  # 模块级单例: 与报警剪辑钩子共享同一实例
        self._live_name = None   # 主画面当前解码的摄像头
        self._sel_name = None    # 当前选中(主显示器显示)的摄像头
        self.setup_ui()
        # 先连接信号再加载配置: 缓冲线程启动时的状态回调经队列化信号送达
        self.status_changed.connect(self._on_status)
        self.test_finished.connect(self._on_test_done)
        self.port_detected.connect(self._on_port_detected)
        self.load_settings()

    # ---------------- UI ----------------

    def setup_ui(self):
        main = QHBoxLayout()
        main.setSpacing(10)

        # ---- 左列: 摄像头配置(上) + 实时画面主显示器(下), 二者右侧边缘垂直对齐 ----
        # 列宽以主画面(720)为基准 + 分组框内边距, 配置表与主画面同宽对齐
        LEFT_W = VIDEO_W + 24
        left_col = QVBoxLayout()

        # 分组1: 摄像头配置
        cfg_group = QGroupBox("摄像头配置 (ONVIF)")
        cfg_lay = group_layout(QVBoxLayout())
        cfg_group.setLayout(cfg_lay)
        cfg_group.setFixedWidth(LEFT_W)

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setFixedHeight(140)
        self.table.itemChanged.connect(self._on_pass_changed)  # 密码列编辑后打码+存UserRole
        cfg_lay.addWidget(self.table)

        btn_lay = button_row(
            self._btn("添加摄像头", self.add_camera),
            self._btn("删除选中", self.remove_selected),
            self._btn("测试连接", self.test_selected),
            self._btn("保存设置", self.save_settings),
        )
        cfg_lay.addLayout(btn_lay)

        # 历史剪辑清理策略行(手动可调: 不清理/按天数/按空间上限)
        tidy_row = QHBoxLayout()
        tidy_row.addWidget(QLabel("历史剪辑清理:"))
        self.tidy_mode = QComboBox()
        self.tidy_mode.addItems(["不清理", "按保留天数", "按空间上限"])
        self.tidy_mode.currentIndexChanged.connect(self._on_tidy_mode)
        self.tidy_value = QSpinBox()
        self.tidy_value.setRange(1, 1024 * 100)
        tidy_row.addWidget(self.tidy_mode)
        tidy_row.addWidget(self.tidy_value)
        tidy_row.addWidget(self._btn("立即清理", self.manual_cleanup))
        tidy_row.addStretch()
        cfg_lay.addLayout(tidy_row)

        # 移动侦测全局参数行(帧差灵敏度 + 推送冷却; 逐摄像头开关在表格"移动侦测"列)
        motion_row = QHBoxLayout()
        motion_row.addWidget(QLabel("移动侦测:"))
        motion_row.addWidget(QLabel("灵敏度"))
        self.motion_sens = QComboBox()
        self.motion_sens.addItems(["高", "中", "低"])
        motion_row.addWidget(self.motion_sens)
        motion_row.addWidget(QLabel("冷却"))
        self.motion_cooldown = QSpinBox()
        self.motion_cooldown.setRange(10, 3600)
        self.motion_cooldown.setSuffix(" 秒")
        motion_row.addWidget(self.motion_cooldown)
        motion_row.addStretch()
        cfg_lay.addLayout(motion_row)

        cfg_lay.addWidget(hint_label(
            "端口不确定可填 0, 测试/保存时自动探测常见端口(80/8899/8000等)。"
            "\"报警联动\"勾选后该摄像头后台持续缓冲(仅内存), 报警时自动剪辑前10秒; "
            "\"默认摄像头\"报警时优先显示, 全局唯一; "
            "\"高清剪辑\"勾选后该路剪辑改用主码流(设备最高分辨率, 内存/带宽占用更高), "
            "不勾则用子码流。每次报警剪辑落盘后自动按上方策略清理历史剪辑。"))
        cfg_lay.addWidget(hint_label(
            "\"移动侦测\"勾选后该路后台侦测画面变动(ONVIF事件+本地帧差双源, 固件不支持"
            "ONVIF事件时帧差兜底), 触发即剪辑前10秒并推送手机, 冷却期内不重复推送。"))
        left_col.addWidget(cfg_group)

        # 分组2: 实时画面主显示器(选中的摄像头大画面)
        live_group = QGroupBox("实时画面 (主显示器)")
        live_lay = group_layout(QVBoxLayout())
        live_group.setLayout(live_lay)
        live_group.setFixedWidth(LEFT_W)

        self.video_label = QLabel("点击右侧摄像头画面打开")
        self.video_label.setFixedSize(VIDEO_W, VIDEO_H)
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setStyleSheet("background: black; color: #888; font-size: 12pt;")
        live_lay.addWidget(self.video_label)
        self.live_status = QLabel(" ")
        live_lay.addWidget(self.live_status)
        live_lay.addLayout(button_row(self._btn("关闭画面", self.close_live)))
        left_col.addWidget(live_group)

        left_col.addStretch()
        main.addLayout(left_col)

        # ---- 右列: 每个摄像头的画面(从显示器), 随录入数量增长, 超出滚动 ----
        thumb_group = QGroupBox("摄像头画面 (点击切换主画面)")
        tgl = group_layout(QVBoxLayout())
        thumb_group.setLayout(tgl)
        self.thumb_scroll = QScrollArea()
        self.thumb_scroll.setWidgetResizable(True)
        self.thumb_scroll.setFixedWidth(THUMB_W + 46)
        self.thumb_scroll.setStyleSheet("QScrollArea{border: none; background: transparent;}")
        thumb_box = QWidget()
        self.thumb_lay = QVBoxLayout(thumb_box)
        self.thumb_lay.setContentsMargins(4, 4, 4, 4)
        self.thumb_lay.setSpacing(6)
        self.thumb_lay.addStretch()
        self.thumb_scroll.setWidget(thumb_box)
        self.thumbs = {}  # 摄像头名 -> ThumbLabel
        tgl.addWidget(self.thumb_scroll)
        main.addWidget(thumb_group, 1)

        self.setLayout(main)

        # 主画面刷新定时器(15fps拉取最新帧) + 缩略图刷新定时器(2.5fps)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._pull_frame)
        self._timer.setInterval(66)
        self._thumb_timer = QTimer(self)
        self._thumb_timer.timeout.connect(self._refresh_thumbs)
        self._thumb_timer.setInterval(400)

    @staticmethod
    def _btn(text, slot) -> QPushButton:
        b = QPushButton(text)
        b.clicked.connect(slot)
        return b

    # ---------------- 配置读写 ----------------

    def load_settings(self):
        cams = CameraManager.load_configs()
        self.table.setRowCount(0)
        for c in cams:
            self._append_row(c)
        if not self.table.rowCount():
            self.add_camera()
        self._rebuild_thumbs()
        # 按当前配置重建后台缓冲(报警联动摄像头开缓冲), 状态经信号回UI
        self.manager.rebuild(cams, on_status=lambda n, m: self.status_changed.emit(n, m))
        # 历史剪辑清理策略(手动设定: 不清理/按天数/按空间上限)
        self._tidy_loading = True
        self.tidy_mode.setCurrentIndex(gs("Camera", "cleanup_mode", 0, int))
        self._apply_tidy_mode()
        self.tidy_value.setValue(gs("Camera", "cleanup_value", 30, int))
        self.motion_sens.setCurrentIndex(max(0, min(2, gs("Camera", "motion_sensitivity", 1, int))))
        self.motion_cooldown.setValue(gs("Camera", "motion_cooldown", 60, int))
        self._tidy_loading = False
        threading.Thread(target=cleanup_captures, args=self.tidy_args(),
                         daemon=True).start()

    # ---------------- 历史剪辑清理 ----------------

    def tidy_args(self):
        """当前清理策略参数: (mode, days, max_mb)"""
        mode = self.tidy_mode.currentIndex()
        return (mode, self.tidy_value.value(), self.tidy_value.value())

    def _apply_tidy_mode(self):
        m = self.tidy_mode.currentIndex()
        self.tidy_value.setEnabled(m != 0)
        if m == 1:
            self.tidy_value.setRange(1, 3650)
            self.tidy_value.setSuffix(" 天")
        elif m == 2:
            self.tidy_value.setRange(50, 102400)
            self.tidy_value.setSuffix(" MB")

    def _on_tidy_mode(self):
        if getattr(self, "_tidy_loading", False):
            return
        self._apply_tidy_mode()

    def manual_cleanup(self):
        n = cleanup_captures(*self.tidy_args())
        QMessageBox.information(self, "清理完成",
                                f"已删除 {n} 组历史报警剪辑" if n else "没有需要清理的剪辑")

    def _rebuild_thumbs(self, keep=None):
        """根据表格重建缩略图块: 新增缺失的, 删除多余的; 优先保持keep选中;
        重名只保留首个(与 CameraManager.rebuild 的去重一致, 防缩略图覆盖泄漏)"""
        names = []
        seen = set()
        for r in range(self.table.rowCount()):
            it = self.table.item(r, COL_NAME)
            n = (it.text().strip() if it else "") or f"摄像头{r + 1}"
            if n in seen:
                continue
            seen.add(n)
            names.append(n)
        # 删除已不存在的
        for n in list(self.thumbs):
            if n not in names:
                self._stop_thumb(n)
        # 新增缺失的
        for n in names:
            if n not in self.thumbs:
                lbl = ThumbLabel(self, n)
                # 插到 stretch 之前
                self.thumb_lay.insertWidget(self.thumb_lay.count() - 1, lbl)
                self.thumbs[n] = lbl
        self.thumb_lay.parent().update()
        # 恢复选中(keep仍存在)或默认选中第一路; 页面可见时才启动解码
        target = keep if keep in self.thumbs else (names[0] if names else None)
        if target and not self._live_name and self.isVisible():
            self.select_camera(target)

    def _append_row(self, c: dict):
        row = self.table.rowCount()
        self.table.insertRow(row)
        pwd = c.get("password", "")
        # 密码真实值存 UserRole(可能是明文或 dpapi: 密文), 显示文本永远打码
        pass_item = QTableWidgetItem()
        pass_item.setData(Qt.UserRole, pwd)
        items = [
            QTableWidgetItem(c.get("name", "")),
            QTableWidgetItem(c.get("ip", "")),
            QTableWidgetItem(str(c.get("onvif_port", 0))),
            QTableWidgetItem(c.get("username", "")),
            pass_item,
        ]
        self.table.blockSignals(True)  # 填表期间屏蔽 itemChanged(避免密码UserRole被清)
        for col, it in enumerate(items):
            it.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            self.table.setItem(row, col, it)
        alarm = QCheckBox()
        alarm.setChecked(bool(c.get("alarm_enabled")))
        self.table.setCellWidget(row, COL_ALARM, alarm)
        default = QCheckBox()
        default.setChecked(bool(c.get("is_default")))
        default.stateChanged.connect(self._exclusive_default)
        self.table.setCellWidget(row, COL_DEFAULT, default)
        clip_main = QCheckBox()
        clip_main.setChecked(bool(c.get("clip_main")))
        self.table.setCellWidget(row, COL_CLIP_MAIN, clip_main)
        motion = QCheckBox()
        motion.setChecked(bool(c.get("motion_enabled")))
        self.table.setCellWidget(row, COL_MOTION, motion)
        self.table.item(row, COL_PASS).setText("•" * len(pwd))
        self.table.blockSignals(False)

    def _on_pass_changed(self, it):
        """密码列被用户编辑: 更新 UserRole 真实值并重新打码"""
        if it.column() != COL_PASS:
            return
        txt = (it.text() or "").strip()
        self.table.blockSignals(True)
        it.setData(Qt.UserRole, txt)
        it.setText("•" * len(txt))
        self.table.blockSignals(False)

    def _row_password(self, r) -> str:
        """取行真实密码: UserRole 优先(明文或 dpapi: 密文), 兜底单元格文本"""
        it = self.table.item(r, COL_PASS)
        if not it:
            return ""
        real = it.data(Qt.UserRole)
        if real is not None:
            return str(real)
        return (it.text() or "").strip()

    def _exclusive_default(self, state):
        """默认摄像头全局唯一: 勾选一行时取消其他行"""
        if not state:
            return
        sender = self.sender()
        for r in range(self.table.rowCount()):
            w = self.table.cellWidget(r, COL_DEFAULT)
            if w is not None and w is not sender:
                w.blockSignals(True)
                w.setChecked(False)
                w.blockSignals(False)

    def _read_table(self) -> list:
        cams = []
        for r in range(self.table.rowCount()):
            def cell(c):
                it = self.table.item(r, c)
                return it.text().strip() if it else ""
            name = cell(COL_NAME) or cell(COL_IP) or f"摄像头{r + 1}"
            cams.append({
                "name": name,
                "ip": cell(COL_IP),
                "onvif_port": self._parse_port(cell(COL_PORT)),
                "username": cell(COL_USER),
                "password": self._row_password(r),
                "alarm_enabled": self._chk(r, COL_ALARM),
                "is_default": self._chk(r, COL_DEFAULT),
                "clip_main": self._chk(r, COL_CLIP_MAIN),
                "motion_enabled": self._chk(r, COL_MOTION),
            })
        return cams

    def _chk(self, row, col) -> bool:
        w = self.table.cellWidget(row, col)
        return bool(w and w.isChecked())

    def _reload_combo(self):
        """(已废弃) 兼容保留: 缩略图列表取代下拉框"""
        self._rebuild_thumbs()

    # ---------------- 主/从显示器选流 ----------------

    def select_camera(self, name):
        """点击缩略图: 该路切到主显示器大画面(单实例: 先停前一路),
        其余路转为缩略图模式(独立小尺寸MJPEG); 页面未显示时不启动解码"""
        if not name or name not in self.thumbs:
            return
        if not self.isVisible():
            return  # 页面未显示: showEvent 时再统一启动
        if name == self._sel_name and self._timer.isActive():
            return  # 已在主显示器显示
        prev = self._sel_name
        stream = self.manager.get(name)
        if stream is None:
            self.live_status.setText("请先保存摄像头配置")
            return
        # 主画面切流: 停旧开新(单实例规则); 打开为异步, 失败经状态回调显示错误
        self._stop_live_stream()
        stream.live_start((VIDEO_W, VIDEO_H))
        self._sel_name = name
        self._live_name = name
        self.video_label.setText("连接中...")
        self._timer.start()
        # 缩略图模式切换: 选中路停独立快照(由主画面镜像供图), 其余路开快照
        stream.snapshot_stop()
        for n, tl in self.thumbs.items():
            tl.set_selected(n == name)
            if n != name:
                self._ensure_thumb_snap(n)
        self._thumb_timer.start()

    def _stop_live_stream(self):
        """停掉主画面解码与定时器(缩略图不受影响)"""
        self._timer.stop()
        if self._live_name:
            s = self.manager.get(self._live_name)
            if s:
                s.live_stop()
        self._live_name = None

    def _ensure_thumb_snap(self, name):
        """确保某路的小尺寸快照流在跑(缩略图供图, 空闲自停机制兜底)"""
        s = self.manager.get(name)
        if s is not None:
            s.snapshot_start((THUMB_W, THUMB_H))

    def _stop_thumb(self, name):
        """移除某路缩略图并停掉其快照流"""
        lbl = self.thumbs.pop(name, None)
        if lbl is not None:
            lbl.setParent(None)
            lbl.deleteLater()
        s = self.manager.get(name)
        if s is not None:
            s.snapshot_stop()

    def close_live(self):
        """关闭画面按钮/页面隐藏: 停主画面解码"""
        self._stop_live_stream()
        self._sel_name = None
        self.video_label.setText("画面未开启")
        self.video_label.setPixmap(QPixmap())
        for tl in self.thumbs.values():
            tl.set_selected(False)

    def _pull_frame(self):
        """主画面15fps: 拉选中路最新帧, 同时镜像到该路缩略图"""
        if not self._live_name:
            return
        s = self.manager.get(self._live_name)
        frame = s.live_frame() if s else None
        if frame is None:
            return
        data, w, h = frame
        img = QImage(data, w, h, w * 3, QImage.Format_BGR888)
        pm = QPixmap.fromImage(img)
        self.video_label.setPixmap(pm.scaled(
            self.video_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        tl = self.thumbs.get(self._live_name)
        if tl is not None:
            tl.setPixmap(pm.scaled(
                tl.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _refresh_thumbs(self):
        """缩略图2.5fps: 拉各路独立MJPEG快照流最新帧(选中路由主画面供图)"""
        for name, tl in self.thumbs.items():
            if name == self._live_name and self._timer.isActive():
                continue
            s = self.manager.get(name)
            if s is None:
                continue
            jpeg = s.snapshot_jpeg()  # 顺带刷新活跃时间, 防空闲自停
            if jpeg:
                img = QImage.fromData(jpeg)
                if not img.isNull():
                    tl.setPixmap(QPixmap.fromImage(img))

    # ---------------- 状态显示 ----------------

    def _on_status(self, name: str, msg: str):
        """缓冲线程状态回调(经信号队列切回主线程); 错误文本截断防 SOAP 正文刷屏"""
        if len(msg) > 90:
            msg = msg[:90] + "…"
        if self._live_name == name or not self._live_name:
            self.live_status.setText(f"{name}: {msg}" if msg else name)

    def showEvent(self, event):
        """页面显示: 恢复选中路主画面 + 启动其余路缩略图流"""
        if self._sel_name:
            self.select_camera(self._sel_name)
        for n in self.thumbs:
            if n != self._live_name:
                self._ensure_thumb_snap(n)
        if self.thumbs:
            self._thumb_timer.start()
        return super().showEvent(event)

    def hideEvent(self, event):
        """页面隐藏: 停掉全部解码(主画面+缩略图)释放资源;
        报警缓冲不受影响; 若报警期间手机轮询快照, 报警开始回调会按需重启默认摄像头快照"""
        self._timer.stop()
        self._thumb_timer.stop()
        self._stop_live_stream()
        try:
            self.manager.snapshot_stop_ui_all()  # 报警保活中的快照流不杀(手机轮询用)
        except Exception as e:
            logger.warning(f"停止缩略图快照流异常: {e}")
        return super().hideEvent(event)

    # ---------------- 按钮动作 ----------------

    def add_camera(self):
        self._append_row({"name": f"摄像头{self.table.rowCount() + 1}", "ip": "",
                          "onvif_port": 0, "username": "", "password": ""})

    def remove_selected(self):
        r = self.table.currentRow()
        if r >= 0:
            self.table.removeRow(r)

    def _selected_row(self):
        r = self.table.currentRow()
        if r < 0:
            QMessageBox.warning(self, "提示", "请先在表格中选中一行")
        return r

    def test_selected(self):
        """ONVIF 测试: 自动探测端口 → 设备信息 + Profile 列表(后台线程, 不卡UI)"""
        r = self._selected_row()
        if r < 0:
            return
        cfg = self._row_cfg(r)
        if not cfg["ip"]:
            QMessageBox.warning(self, "提示", "请先填写IP地址")
            return
        self.live_status.setText(f"正在测试 {cfg['ip']} ...")
        threading.Thread(target=self._test_worker, args=(cfg,), daemon=True).start()

    @staticmethod
    def _parse_port(txt) -> int:
        """端口列宽容解析: 非数字输入(如'80,8899')按0处理, 保存不再崩、测试时自动重探"""
        try:
            return int(str(txt).strip())
        except (ValueError, TypeError):
            return 0

    def _row_cfg(self, r) -> dict:
        def cell(c):
            it = self.table.item(r, c)
            return it.text().strip() if it else ""
        return {
            "name": cell(COL_NAME) or f"摄像头{r + 1}",
            "ip": cell(COL_IP),
            "onvif_port": self._parse_port(cell(COL_PORT)),
            "username": cell(COL_USER),
            "password": self._row_password(r),
        }

    def _test_worker(self, cfg: dict):
        try:
            port = cfg["onvif_port"]
            note = ""
            if not port:  # 自动探测
                port = detect_onvif_port(cfg["ip"], cfg["username"], cfg["password"])
                note = f"(自动探测端口 {port}) "
            try:
                c = OnvifClient(cfg["ip"], port, cfg["username"], cfg["password"])
                info = c.get_device_information()
            except OnvifError:
                # 当前端口拒绝/不可达: 自动全端口重探
                if note:
                    raise
                new_port = detect_onvif_port(cfg["ip"], cfg["username"], cfg["password"])
                note = f"(端口 {port} 被拒, 自动改用 {new_port}) "
                port = new_port
                c = OnvifClient(cfg["ip"], port, cfg["username"], cfg["password"])
                info = c.get_device_information()
            profiles = c.get_profiles()
            lines = [f"{note}端口 {port} | {info.get('manufacturer', '?')} {info.get('model', '?')}"
                     f" (固件 {info.get('firmware', '?')})"]
            for token, name, res in profiles:
                lines.append(f"Profile: {name or token} {res}")
            # 探测端口经信号回主线程写回表格(工作线程不碰任何QWidget, 修复跨线程GUI崩溃)
            self.port_detected.emit(cfg["ip"], port)
            self.test_finished.emit(True, "\n".join(lines))
        except Exception as e:
            self.test_finished.emit(False, str(e)[:300])

    def _on_port_detected(self, ip: str, port: int):
        """主线程槽: 把后台探测到的 ONVIF 端口写回对应行"""
        for r in range(self.table.rowCount()):
            if self._row_cfg(r)["ip"] == ip:
                it = self.table.item(r, COL_PORT)
                if it is not None:
                    it.setText(str(port))

    def _on_test_done(self, ok: bool, msg: str):
        self.live_status.setText(msg.splitlines()[0] if msg else "")
        (QMessageBox.information if ok else QMessageBox.warning)(self, "测试成功" if ok else "测试失败", msg)

    def save_settings(self):
        cams = self._read_table()
        # 唯一默认: 若无一勾选, 取第一行
        if cams and not any(c["is_default"] for c in cams):
            cams[0]["is_default"] = True
        # 密码 DPAPI 加密后落盘(已加密的 dpapi: 密文不再重复加密)
        import copy
        to_save = copy.deepcopy(cams)
        for c in to_save:
            if c["password"] and not c["password"].startswith("dpapi:"):
                c["password"] = dpapi_protect(c["password"])
        CameraManager.save_configs(to_save)
        # 历史剪辑清理策略落盘 + 立即按新策略执行一次
        ups("Camera", "cleanup_mode", str(self.tidy_mode.currentIndex()))
        ups("Camera", "cleanup_value", str(self.tidy_value.value()))
        # 移动侦测全局参数落盘(逐摄像头开关随上方 cameras 配置一起保存)
        ups("Camera", "motion_sensitivity", str(self.motion_sens.currentIndex()))
        ups("Camera", "motion_cooldown", str(self.motion_cooldown.value()))
        threading.Thread(target=cleanup_captures, args=self.tidy_args(),
                         daemon=True).start()
        # 重建后台流(新配置生效), 尽量保持当前选中
        keep = self._sel_name
        self.close_live()
        self.manager.rebuild(cams, on_status=lambda n, m: self.status_changed.emit(n, m))
        self._rebuild_thumbs(keep=keep)
        self.camera_settings_changed.emit(cams)
        QMessageBox.information(self, "成功", "摄像头设置已保存")

    def shutdown(self):
        """程序退出时调用: 停掉全部流"""
        try:
            self.manager.stop_all()
        except Exception as e:
            logger.warning(f"摄像头管理器关闭异常: {e}")
