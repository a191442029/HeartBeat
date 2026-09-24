# -*- coding: utf-8 -*-
"""ESP32 全屋心率中继设置页 (v1.3.0)

核心约束: 只有勾选"启用ESP32中继"才启动 relay_hub 服务与节点接入;
取消勾选立即停止服务并释放全部节点。配置持久化到 config.ini [esp32_relay]。
信号标定: 并排同位测各节点个体RF偏差(零和bias), 选路用校准值/门槛用原始值(双轨)。
报警视频联动: 房间(节点/PC)→摄像头绑定, 心率报警时接收端显示当前房间摄像头画面。
"""
import json

from PyQt5.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QGroupBox, QPushButton, QSpinBox, QCheckBox, QMessageBox,
    QWidget, QHeaderView, QTableWidget, QTableWidgetItem, QAbstractItemView,
    QComboBox
)
from PyQt5.QtCore import QTimer, Qt
from system_utils import logger, ups, gs
from .basicwidgets import group_layout, hint_label
import relay_hub


class RelaySettingsUI(QWidget):
    """ESP32 全屋心率中继设置 (勾选启用才生效)"""

    def __init__(self):
        super().__init__()
        self.setup_ui()
        self.load_settings()
        # 节点表刷新定时器(轻量: 仅每3s复制一次快照)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self.update_display)
        self._refresh_timer.start(3000)
        # 标定进度轮询定时器(仅在标定期间运行)
        self._calib_timer = QTimer(self)
        self._calib_timer.timeout.connect(self._poll_calibration)
        self.update_display()

    # ---------------- UI ----------------

    def setup_ui(self):
        main_layout = QVBoxLayout(self)

        # ---- 启用开关 ----
        enable_group = QGroupBox("ESP32 全屋心率中继")
        enable_layout = group_layout(QVBoxLayout())
        enable_group.setLayout(enable_layout)

        self.chk_enabled = QCheckBox("启用ESP32中继 (勾选后启动中继服务, 节点才能接入)")
        enable_layout.addWidget(self.chk_enabled)

        info_row = QHBoxLayout()
        self.lbl_endpoint = QLabel("服务地址: 未启动")
        self.lbl_endpoint.setStyleSheet("color: #666;")
        info_row.addWidget(self.lbl_endpoint)
        info_row.addStretch()
        enable_layout.addLayout(info_row)

        enable_layout.addWidget(hint_label(
            "每个房间放一台ESP32节点(固件见 esp32/hr_relay/, 首次上电开热点 HRM-Link-XXXX 配网)。\n"
            "节点自动连本页服务并待命; 手环被PC/节点连接时其余节点测不到信号(连接即静默),\n"
            "信号变差时中枢单向切换数据源。报警期间冻结切换保证数据连续。"))
        main_layout.addWidget(enable_group)

        # ---- 漫游参数 ----
        param_group = QGroupBox("漫游仲裁参数")
        param_layout = group_layout(QVBoxLayout())
        param_group.setLayout(param_layout)

        def _spin_row(items):
            row = QHBoxLayout()
            for label, spin, suffix in items:
                row.addWidget(QLabel(label))
                spin.setSuffix(suffix)
                row.addWidget(spin)
            row.addStretch()
            return row

        self.spin_threshold = QSpinBox()
        self.spin_threshold.setRange(-100, -40)
        self.spin_threshold.setValue(-75)
        self.spin_hysteresis = QSpinBox()
        self.spin_hysteresis.setRange(5, 30)
        self.spin_hysteresis.setValue(10)
        self.spin_cycles = QSpinBox()
        self.spin_cycles.setRange(1, 10)
        self.spin_cycles.setValue(3)
        param_layout.addLayout(_spin_row([
            ("切换阈值:", self.spin_threshold, " dBm"),
            ("迟滞:", self.spin_hysteresis, " dB"),
            ("连续判定周期:", self.spin_cycles, " 个"),
        ]))
        param_layout.addWidget(hint_label(
            "持有节点信号低于阈值且连续N个周期, 断开后全屋探测; 候选须强于原持有者迟滞值以上才切换。"))
        main_layout.addWidget(param_group)

        # ---- 节点列表 ----
        node_group = QGroupBox("节点列表")
        node_layout = group_layout(QVBoxLayout())
        node_group.setLayout(node_layout)

        self.node_table = QTableWidget(0, 7)
        self.node_table.setHorizontalHeaderLabels(
            ["节点名", "IP", "固件", "状态", "手环RSSI", "偏差dB", "最后活跃(秒前)"])
        self.node_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.node_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.node_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.node_table.verticalHeader().setVisible(False)
        self.node_table.setMinimumHeight(180)
        node_layout.addWidget(self.node_table)

        btn_row = QHBoxLayout()
        self.btn_save = QPushButton("保存并应用")
        self.btn_save.clicked.connect(self.save_settings)
        self.btn_refresh = QPushButton("刷新节点")
        self.btn_refresh.clicked.connect(self.update_display)
        btn_row.addWidget(self.btn_save)
        btn_row.addWidget(self.btn_refresh)
        btn_row.addStretch()
        node_layout.addLayout(btn_row)
        main_layout.addWidget(node_group)

        # ---- 信号标定 (个体偏差均衡) ----
        calib_group = QGroupBox("信号标定 (个体偏差均衡)")
        calib_layout = group_layout(QVBoxLayout())
        calib_group.setLayout(calib_layout)

        calib_layout.addWidget(hint_label(
            "不同ESP32存在3~8dB个体射频偏差, 会导致仲裁选路误判。标定方法: 全部节点在线并\n"
            "排同位放置(天线朝向一致, 偏差与摆放位置/隔墙无关), 手环放2米内并保持心率测量中,\n"
            "点开始约需30秒。标定期间会暂时断开手环连接, 完成后自动恢复。选路用校准值, \n"
            "掉线判定仍用原始值(双轨)。\n"
            "隔墙验证: 标定后把节点并排贴墙同一侧再标一次, 偏差应与首次一致(±2dB内);\n"
            "墙衰减=隔墙读数−同位读数(看节点表), 用于确认切换阈值余量。隔墙差值是真实信号, \n"
            "不可作为偏差校准, 否则仲裁会误选隔墙节点。"))
        calib_btn_row = QHBoxLayout()
        self.btn_calib = QPushButton("开始标定")
        self.btn_calib.clicked.connect(self.start_calibration)
        self.btn_clear_bias = QPushButton("清除偏差")
        self.btn_clear_bias.clicked.connect(self.clear_biases)
        calib_btn_row.addWidget(self.btn_calib)
        calib_btn_row.addWidget(self.btn_clear_bias)
        calib_btn_row.addStretch()
        calib_layout.addLayout(calib_btn_row)

        self.lbl_calib = QLabel("")
        self.lbl_calib.setStyleSheet("color: #666;")
        self.lbl_calib_result = QLabel("")
        self.lbl_calib_result.setWordWrap(True)
        calib_layout.addWidget(self.lbl_calib)
        calib_layout.addWidget(self.lbl_calib_result)
        main_layout.addWidget(calib_group)

        # ---- 报警视频联动 (房间→摄像头绑定) ----
        cam_group = QGroupBox("报警视频联动")
        cam_layout = group_layout(QVBoxLayout())
        cam_group.setLayout(cam_layout)

        cam_layout.addWidget(hint_label(
            "心率报警时, 中枢按\"当前持有手环的节点\"定位所在房间, 接收端报警面板自动切换\n"
            "显示该房间绑定摄像头的实时画面(经Tailscale访问EXE转发, 摄像头无需接入VPN)。\n"
            "PC=手环由电脑直连时EXE所在房间。未绑定/绑定无效的房间显示默认摄像头。\n"
            "定位为房间级精度: 节点切换探测期间报警按切换前持有节点定位。"))
        self.cam_table = QTableWidget(0, 2)
        self.cam_table.setHorizontalHeaderLabels(["房间(数据源)", "摄像头"])
        self.cam_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.cam_table.verticalHeader().setVisible(False)
        self.cam_table.setMinimumHeight(96)
        cam_layout.addWidget(self.cam_table)

        cam_btn_row = QHBoxLayout()
        self.btn_add_binding = QPushButton("添加绑定")
        self.btn_add_binding.clicked.connect(self.add_binding_row)
        self.btn_del_binding = QPushButton("删除选中绑定")
        self.btn_del_binding.clicked.connect(self.remove_binding_row)
        cam_btn_row.addWidget(self.btn_add_binding)
        cam_btn_row.addWidget(self.btn_del_binding)
        cam_btn_row.addStretch()
        cam_layout.addLayout(cam_btn_row)
        main_layout.addWidget(cam_group)

        main_layout.addStretch()

    # ---------------- 配置 ----------------

    def load_settings(self):
        try:
            self.chk_enabled.setChecked(gs("esp32_relay", "enabled", False, bool, "-RelayUI"))
            self.spin_threshold.setValue(gs("esp32_relay", "threshold_drop", -75, int, "-RelayUI"))
            self.spin_hysteresis.setValue(gs("esp32_relay", "hysteresis_db", 10, int, "-RelayUI"))
            self.spin_cycles.setValue(gs("esp32_relay", "freeze_cycles", 3, int, "-RelayUI"))
            # 恢复上次启用状态: 启动即拉起服务(幂等)
            if self.chk_enabled.isChecked():
                relay_hub.start_relay_hub()
            self._show_biases()
            self._load_bindings()
        except Exception as e:
            logger.error(f"ESP32中继配置加载失败: {e}")

    def save_settings(self):
        try:
            ups("esp32_relay", "enabled", self.chk_enabled.isChecked())
            ups("esp32_relay", "threshold_drop", self.spin_threshold.value())
            ups("esp32_relay", "hysteresis_db", self.spin_hysteresis.value())
            ups("esp32_relay", "freeze_cycles", self.spin_cycles.value())
            self._save_bindings()
            # 勾选=启动 / 取消=停止(节点会因WS断开自动放手)
            if self.chk_enabled.isChecked():
                relay_hub.start_relay_hub()
            else:
                relay_hub.stop_relay_hub()
            self.update_display()
            QMessageBox.information(self, "提示", "ESP32中继设置已应用")
        except Exception as e:
            logger.error(f"ESP32中继设置保存失败: {e}")
            QMessageBox.warning(self, "错误", f"保存失败: {str(e)}")

    # ---------------- 报警视频联动 (房间→摄像头绑定) ----------------

    def _room_names(self) -> list:
        """房间下拉选项: PC(电脑直连时EXE所在房间) + 当前在线节点名"""
        names = ["PC"]
        try:
            for r in relay_hub.node_table():
                n = str(r.get("name", "")).strip()
                if n and n not in names:
                    names.append(n)
        except Exception:
            pass
        return names

    @staticmethod
    def _camera_names() -> list:
        """摄像头下拉选项: 摄像头页已配置名称(名称是报警剪辑/快照取流的主键)"""
        try:
            from camera.stream_manager import CameraManager
            cfgs = CameraManager.load_configs()
            return [str(c.get("name") or c.get("ip") or "cam")
                    for c in cfgs if isinstance(c, dict)]
        except Exception:
            return []

    def _add_binding_row(self, room="", cam=""):
        """追加一行绑定(两列均为下拉; 房间可手输以支持离线节点名)"""
        row = self.cam_table.rowCount()
        self.cam_table.insertRow(row)
        rooms = self._room_names()
        if room and room not in rooms:
            rooms.insert(0, room)
        rc = QComboBox()
        rc.setEditable(True)
        rc.addItems(rooms)
        if room:
            rc.setCurrentText(room)
        self.cam_table.setCellWidget(row, 0, rc)
        cc = QComboBox()
        cc.addItem("未绑定")
        cc.addItems(self._camera_names())
        cc.setCurrentText(cam if cam else "未绑定")
        self.cam_table.setCellWidget(row, 1, cc)
        self.cam_table.setRowHeight(row, 30)

    def add_binding_row(self):
        """添加绑定按钮: 房间下拉即时刷新(取此刻在线节点)"""
        self._add_binding_row()

    def remove_binding_row(self):
        """删除选中绑定行"""
        row = self.cam_table.currentRow()
        if row >= 0:
            self.cam_table.removeRow(row)

    def _load_bindings(self):
        """从 config.ini [esp32_relay] room_camera_map(JSON) 恢复绑定表"""
        try:
            raw = gs("esp32_relay", "room_camera_map", "", str, "-RelayUI")
            mapping = json.loads(raw) if raw else {}
            if isinstance(mapping, dict):
                for room, cam in mapping.items():
                    self._add_binding_row(str(room), str(cam or ""))
        except Exception as e:
            logger.error(f"报警视频联动绑定载入失败: {e}")

    def _save_bindings(self):
        """绑定表序列化到 config.ini [esp32_relay] room_camera_map(JSON: 房间→摄像头名)"""
        mapping = {}
        for i in range(self.cam_table.rowCount()):
            rc = self.cam_table.cellWidget(i, 0)
            cc = self.cam_table.cellWidget(i, 1)
            if not (rc and cc):
                continue
            room = rc.currentText().strip()
            cam = cc.currentText().strip()
            if room and cam and cam != "未绑定":
                mapping[room] = cam  # 同房间多行时后行覆盖前行
        ups("esp32_relay", "room_camera_map",
            json.dumps(mapping, ensure_ascii=False) if mapping else "")

    # ---------------- 状态刷新 ----------------

    def update_display(self):
        running = relay_hub.hub_running()
        if running:
            self.lbl_endpoint.setText(f"服务地址: ws://{relay_hub.get_local_ip()}:{relay_hub.get_hub().port}/relay (运行中)")
        else:
            self.lbl_endpoint.setText("服务地址: 未启动 (勾选启用并保存)")

        state_map = {
            "idle": "待命(空闲)", "connecting": "连接中", "active": "● 持有手环",
            "offline": "离线",
        }
        try:
            rows = relay_hub.node_table()
        except Exception:
            rows = []
        self.node_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            bias = r.get("bias", 0)
            values = [
                r.get("name", ""),
                r.get("ip", ""),
                r.get("fw", ""),
                state_map.get(r.get("state", "offline"), r.get("state", "")),
                (f"{r['rssi']} dBm" if r.get("rssi") else "—"),
                f"{bias:+d} dB",
                str(r.get("age", "")) if r.get("age") is not None else "",
            ]
            for j, v in enumerate(values):
                item = QTableWidgetItem(str(v))
                if j == 3 and r.get("state") == "active":
                    item.setForeground(Qt.green)
                self.node_table.setItem(i, j, item)

    # ---------------- 信号标定 ----------------

    def start_calibration(self):
        """开始并排同位标定(需要≥2台节点在线+手环广播中)"""
        try:
            ok, msg = relay_hub.start_calibration()
        except Exception as e:
            ok, msg = False, str(e)
        if not ok:
            QMessageBox.warning(self, "无法标定", msg)
            return
        self.btn_calib.setEnabled(False)
        self.btn_clear_bias.setEnabled(False)
        self.lbl_calib_result.setText("")
        self.lbl_calib.setText("标定开始...")
        self._calib_timer.start(500)

    def _poll_calibration(self):
        """500ms轮询标定进度, 结束后展示结果并恢复按钮"""
        try:
            st = relay_hub.calibration_status()
        except Exception:
            st = {}
        if st.get("running"):
            samples = st.get("samples") or {}
            samp_txt = "  ".join(f"{k}:{v}次" for k, v in sorted(samples.items())) or "等待回包..."
            self.lbl_calib.setText(f"{st.get('note', '标定中')} | 采样 {samp_txt}")
            return
        self._calib_timer.stop()
        self.btn_calib.setEnabled(True)
        self.btn_clear_bias.setEnabled(True)
        if st.get("error"):
            self.lbl_calib.setText("标定失败")
            self.lbl_calib_result.setText(st["error"])
        elif st.get("started") and st.get("result"):
            self.lbl_calib.setText("标定完成")
            res = st["result"]
            self.lbl_calib_result.setText(
                f"基准 {res.get('ref', 0)}dBm | " + "; ".join(res.get("items", [])))
            self.update_display()
        else:
            self.lbl_calib.setText("")

    def clear_biases(self):
        try:
            ok, msg = relay_hub.clear_biases()
        except Exception as e:
            ok, msg = False, str(e)
        if not ok:
            QMessageBox.warning(self, "提示", msg)
            return
        self.lbl_calib.setText("")
        self.lbl_calib_result.setText("当前偏差: 无")
        self.update_display()

    def _show_biases(self):
        try:
            b = relay_hub.get_biases()
        except Exception:
            b = {}
        self.lbl_calib_result.setText(
            "当前偏差: "
            + ("; ".join(f"{k} {v:+d}dB" for k, v in sorted(b.items())) if b else "无"))
