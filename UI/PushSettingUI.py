from PyQt5.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QGroupBox, QPushButton, QSpinBox, QCheckBox, QMessageBox,
    QWidget, QSizePolicy, QHeaderView, QTableWidget, QTableWidgetItem, QFrame,
    QComboBox
)
from PyQt5.QtCore import pyqtSignal, QTimer, Qt
from system_utils import logger, ups, gs
from push_notifier import record_push
from .basicwidgets import group_layout, hint_label, button_row
import datetime
import json
import threading


class PushSettingsUI(QWidget):
    """多渠道消息推送设置界面 (MeoW / Bark / ntfy)"""
    push_settings_changed = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.setup_ui()
        self.load_settings()

    def setup_ui(self):
        main_layout = QVBoxLayout()

        # ---- 默认规则 ----
        common_group = QGroupBox("默认规则 (无时段规则命中时生效)")
        common_layout = group_layout(QVBoxLayout())
        common_group.setLayout(common_layout)

        # 两行紧凑排布: 上限/下限一行, 持续/冷却一行 (原4行压缩为2行)
        def _spin_row(items):
            row = QHBoxLayout()
            for label, spin, suffix in items:
                row.addWidget(QLabel(label))
                spin.setSuffix(suffix)
                row.addWidget(spin)
            row.addStretch()
            return row

        self.max_hr_input = QSpinBox()
        self.max_hr_input.setRange(0, 999)  # 不做范围限制, 保存时校验兜底
        self.max_hr_input.setValue(150)
        self.min_hr_input = QSpinBox()
        self.min_hr_input.setRange(0, 999)  # 不做范围限制, 保存时校验兜底
        self.min_hr_input.setValue(45)
        common_layout.addLayout(_spin_row([
            ("心率上限告警:", self.max_hr_input, " 次/分"),
            ("心率下限告警:", self.min_hr_input, " 次/分"),
        ]))

        self.duration_input = QSpinBox()
        self.duration_input.setRange(1, 600)  # 与时段规则持续校验(1~600)一致
        self.duration_input.setValue(10)
        self.cooldown_input = QSpinBox()
        self.cooldown_input.setRange(0, 1440)  # 与时段规则冷却校验(0~1440)一致, 0=不冷却
        self.cooldown_input.setValue(5)
        common_layout.addLayout(_spin_row([
            ("超限持续判定:", self.duration_input, " 秒"),
            ("告警冷却:", self.cooldown_input, " 分钟"),
        ]))

        hint = hint_label("超限持续达到设定秒数才推送; 冷却期内该类别不重复推送(填0不冷却, 各类别独立计时);\n上限/下限填0表示该项不检测; 设备断开/重连时也会推送提醒。可同时启用多个渠道。")
        common_layout.addWidget(hint)

        # 触发方式示例面板(位于默认规则右侧)
        example_label = QLabel(
            "触发方式示例 (以上限80/下限40/持续50秒为例)\n"
            "──────────────────────────\n"
            "① 触发: 心率≥81 或 ≤39, 且连续50秒每秒都超限\n"
            "     70 70 85 88 ... 95 → 第50秒推送\n"
            "② 清零: 中途哪怕回落一秒(如 85→75), 计时清零,\n"
            "     重新计满50秒; 骤降到35则按过低重新计时\n"
            "③ 不触发: 心率始终在40~80之间(含40和80);\n"
            "     超限49秒就恢复也不推送\n"
            "④ 冷却: 推送后冷却期内该类别不再推;\n"
            "     冷却结束时仍超限则自动再推, 恢复后重新计50秒")
        example_label.setWordWrap(True)
        example_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        example_label.setFrameShape(QFrame.StyledPanel)
        example_label.setStyleSheet("color: #555; padding: 6px; background: #f7f7f7;")
        common_row = QHBoxLayout()
        common_row.addWidget(common_group, 3)
        common_row.addWidget(example_label, 2)
        main_layout.addLayout(common_row)

        # ---- MeoW 渠道 (鸿蒙) ----
        meow_group = QGroupBox("MeoW (鸿蒙)")
        meow_layout = QHBoxLayout()
        meow_group.setLayout(meow_layout)
        self.meow_enabled = QCheckBox("启用")
        self.meow_enabled.stateChanged.connect(self.update_ui_state)
        meow_layout.addWidget(self.meow_enabled)
        meow_layout.addWidget(QLabel("昵称:"))
        self.meow_nickname = QLineEdit()
        self.meow_nickname.setPlaceholderText("MeoW App 中注册的昵称; 多台手机用逗号分隔")
        meow_layout.addWidget(self.meow_nickname)
        self.meow_test_btn = QPushButton("测试")
        self.meow_test_btn.clicked.connect(lambda: self.test_channel("meow"))
        meow_layout.addWidget(self.meow_test_btn)
        main_layout.addWidget(meow_group)

        # ---- Bark 渠道 (iOS) ----
        bark_group = QGroupBox("Bark (iOS)")
        bark_layout = QVBoxLayout()
        bark_group.setLayout(bark_layout)
        bark_row1 = QHBoxLayout()
        bark_layout.addLayout(bark_row1)
        self.bark_enabled = QCheckBox("启用")
        self.bark_enabled.stateChanged.connect(self.update_ui_state)
        bark_row1.addWidget(self.bark_enabled)
        bark_row1.addWidget(QLabel("Key:"))
        self.bark_key_input = QLineEdit()
        self.bark_key_input.setPlaceholderText("Bark App 显示的推送Key; 多台手机用逗号分隔")
        bark_row1.addWidget(self.bark_key_input)
        bark_row1.addWidget(QLabel("服务器:"))
        self.bark_server_input = QLineEdit()
        self.bark_server_input.setPlaceholderText("默认 api.day.app, 自建填地址")
        bark_row1.addWidget(self.bark_server_input)
        self.bark_test_btn = QPushButton("测试")
        self.bark_test_btn.clicked.connect(lambda: self.test_channel("bark"))
        bark_row1.addWidget(self.bark_test_btn)
        bark_row2 = QHBoxLayout()
        bark_layout.addLayout(bark_row2)
        bark_row2.addWidget(QLabel("推送级别:"))
        self.bark_level = QComboBox()
        for text, data in [("默认(不传)", ""), ("主动弹窗 active", "active"),
                           ("时效性 timeSensitive", "timeSensitive"),
                           ("被动轻提示 passive", "passive"),
                           ("紧急-无视静音 critical", "critical")]:
            self.bark_level.addItem(text, data)
        bark_row2.addWidget(self.bark_level)
        bark_row2.addWidget(QLabel("铃声:"))
        self.bark_sound = QLineEdit()
        self.bark_sound.setPlaceholderText("可选: 铃声名如 bell,alarm (留空用App默认)")
        bark_row2.addWidget(self.bark_sound)
        bark_row2.addWidget(QLabel("分组:"))
        self.bark_group = QLineEdit()
        self.bark_group.setPlaceholderText("可选: 同组通知在手机上归拢, 如 HRM")
        bark_row2.addWidget(self.bark_group)
        main_layout.addWidget(bark_group)

        # ---- ntfy 渠道 (安卓/全平台) ----
        ntfy_group = QGroupBox("ntfy (安卓/全平台)")
        ntfy_layout = QVBoxLayout()
        ntfy_group.setLayout(ntfy_layout)
        ntfy_row1 = QHBoxLayout()
        ntfy_layout.addLayout(ntfy_row1)
        self.ntfy_enabled = QCheckBox("启用")
        self.ntfy_enabled.stateChanged.connect(self.update_ui_state)
        ntfy_row1.addWidget(self.ntfy_enabled)
        ntfy_row1.addWidget(QLabel("主题:"))
        self.ntfy_topic_input = QLineEdit()
        self.ntfy_topic_input.setPlaceholderText("ntfy App 中订阅的主题名(建议随机字符串防骚扰); 多台用逗号分隔")
        ntfy_row1.addWidget(self.ntfy_topic_input)
        ntfy_row1.addWidget(QLabel("服务器:"))
        self.ntfy_server_input = QLineEdit()
        self.ntfy_server_input.setPlaceholderText("默认 ntfy.sh, 自建填地址")
        ntfy_row1.addWidget(self.ntfy_server_input)
        self.ntfy_test_btn = QPushButton("测试")
        self.ntfy_test_btn.clicked.connect(lambda: self.test_channel("ntfy"))
        ntfy_row1.addWidget(self.ntfy_test_btn)
        ntfy_row2 = QHBoxLayout()
        ntfy_layout.addLayout(ntfy_row2)
        ntfy_row2.addWidget(QLabel("优先级:"))
        self.ntfy_priority = QComboBox()
        for text, data in [("默认(不传)", 0), ("1 最小", 1), ("2 低", 2),
                           ("3 普通", 3), ("4 高", 4), ("5 紧急-连续提醒", 5)]:
            self.ntfy_priority.addItem(text, data)
        ntfy_row2.addWidget(self.ntfy_priority)
        ntfy_row2.addWidget(QLabel("标签:"))
        self.ntfy_tags = QLineEdit()
        self.ntfy_tags.setPlaceholderText("可选: emoji短代码,逗号分隔 如 warning,heart")
        ntfy_row2.addWidget(self.ntfy_tags)
        ntfy_row2.addWidget(QLabel("访问令牌:"))
        self.ntfy_token = QLineEdit()
        self.ntfy_token.setEchoMode(QLineEdit.Password)
        self.ntfy_token.setPlaceholderText("可选: 自建服务器/受保护主题的令牌(tk_...)")
        ntfy_row2.addWidget(self.ntfy_token)
        main_layout.addWidget(ntfy_group)

        # ---- 疑似心律不齐提示 (实验性) ----
        irr_group = QGroupBox("疑似心律不齐提示 (实验性, 非医学诊断)")
        irr_layout = QHBoxLayout()
        irr_group.setLayout(irr_layout)
        self.irr_enabled = QCheckBox("启用")
        self.irr_enabled.stateChanged.connect(self.update_ui_state)
        irr_layout.addWidget(self.irr_enabled)
        irr_layout.addWidget(QLabel("判定窗口:"))
        self.irr_window = QSpinBox()
        self.irr_window.setRange(30, 300)  # 下限30=检测器内部物理下限, 上限放宽
        self.irr_window.setSuffix(" 秒")
        irr_layout.addWidget(self.irr_window)
        irr_layout.addWidget(QLabel("波动阈值:"))
        self.irr_sd = QSpinBox()
        self.irr_sd.setRange(1, 50)
        self.irr_sd.setSuffix(" bpm")
        irr_layout.addWidget(self.irr_sd)
        irr_layout.addWidget(QLabel("跳变占比:"))
        self.irr_ratio = QSpinBox()
        self.irr_ratio.setRange(1, 100)
        self.irr_ratio.setSuffix(" %")
        irr_layout.addWidget(self.irr_ratio)
        irr_layout.addWidget(QLabel("静息上限:"))
        self.irr_rest = QSpinBox()
        self.irr_rest.setRange(30, 200)
        self.irr_rest.setSuffix(" bpm")
        irr_layout.addWidget(self.irr_rest)

        # 判定示例面板(位于心律不齐设置右侧, 样式同默认规则示例)
        irr_example = QLabel(
            "判定示例 (以窗口60秒/波动5/跳变30%/静息100为例)\n"
            "──────────────────────────\n"
            "① 触发: 静息下逐秒无规律乱跳, 如\n"
            "     70 62 85 58 92 65 ...(标准差>5 且\n"
            "     相邻秒差≥5的占比>30%), 连续2个\n"
            "     60秒窗口均如此 → 推送提示\n"
            "② 不触发: 平稳静息 68 69 68 70 ...(波动小);\n"
            "     运动上升 75 85 95 105 ...(均值>100);\n"
            "     恢复下降 95 85 75 65 ...(趋势>5)\n"
            "③ 清零: 心率断流(断连/丢信号)窗口作废重新\n"
            "     积累; 中途任一窗口恢复正常则连击清零\n"
            "④ 冷却: 触发后10分钟内不重复推送")
        irr_example.setWordWrap(True)
        irr_example.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        irr_example.setFrameShape(QFrame.StyledPanel)
        irr_example.setStyleSheet("color: #555; padding: 6px; background: #f7f7f7;")
        irr_row = QHBoxLayout()
        irr_row.addWidget(irr_group, 3)
        irr_row.addWidget(irr_example, 2)
        main_layout.addLayout(irr_row)

        irr_hint = hint_label("静息下心率无序大幅跳动且持续时推送(独立10分钟冷却);\n不可区分房颤/窦性不齐/早搏, 仅供筛查提醒, 请以就医确诊为准。")
        main_layout.addWidget(irr_hint)

        # ---- 自定义时段规则 ----
        period_group = QGroupBox("自定义时段规则 (时段内使用独立参数, 支持跨零点; 可添加多条)")
        period_layout = QVBoxLayout()
        period_group.setLayout(period_layout)
        self.periods_table = QTableWidget(0, 7)
        self.periods_table.setHorizontalHeaderLabels(
            ["启用", "开始", "结束", "上限(0=不限)", "下限(0=不限)", "持续(秒)", "冷却(分)"])
        self.periods_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.periods_table.verticalHeader().setVisible(False)
        period_layout.addWidget(self.periods_table)
        period_btn_layout = QHBoxLayout()
        add_btn = QPushButton("添加规则")
        add_btn.clicked.connect(self._add_period_row)
        del_btn = QPushButton("删除选中行")
        del_btn.clicked.connect(self._delete_period_row)
        period_btn_layout.addWidget(add_btn)
        period_btn_layout.addWidget(del_btn)
        period_btn_layout.addStretch()
        period_layout.addLayout(period_btn_layout)
        period_hint = hint_label("每条规则: 该时段内心率超限且持续设定秒数才推送, 各规则×过高/过低冷却独立不共用;\n上限/下限填0表示该项不检测; 多条规则同时命中时按顺序取第一条。")
        period_layout.addWidget(period_hint)
        main_layout.addWidget(period_group)

        # ---- 保存 ----
        self.save_button = QPushButton("保存设置")
        self.save_button.clicked.connect(self.save_settings)
        main_layout.addLayout(button_row(self.save_button))

        main_layout.addStretch()

        self.setLayout(main_layout)

    def load_settings(self):
        """加载推送设置"""
        self.max_hr_input.setValue(self._get_set("max_hr", 150, int))
        self.min_hr_input.setValue(self._get_set("min_hr", 45, int))
        self.meow_enabled.setChecked(self._get_set("meow_enabled", False, bool))
        self.meow_nickname.setText(self._get_set("meow_nickname", "", str))
        self.bark_enabled.setChecked(self._get_set("bark_enabled", False, bool))
        self.bark_key_input.setText(self._get_set("bark_device_key", "", str))
        self.bark_server_input.setText(self._get_set("bark_server", "", str))
        idx = self.bark_level.findData(self._get_set("bark_level", "", str))
        self.bark_level.setCurrentIndex(idx if idx >= 0 else 0)
        self.bark_sound.setText(self._get_set("bark_sound", "", str))
        self.bark_group.setText(self._get_set("bark_group", "", str))
        self.ntfy_enabled.setChecked(self._get_set("ntfy_enabled", False, bool))
        self.ntfy_topic_input.setText(self._get_set("ntfy_topic", "", str))
        self.ntfy_server_input.setText(self._get_set("ntfy_server", "", str))
        idx = self.ntfy_priority.findData(self._get_set("ntfy_priority", 0, int))
        self.ntfy_priority.setCurrentIndex(idx if idx >= 0 else 0)
        self.ntfy_tags.setText(self._get_set("ntfy_tags", "", str))
        self.ntfy_token.setText(self._get_set("ntfy_token", "", str))
        self.irr_enabled.setChecked(self._get_set("irregular_enabled", False, bool))
        self.irr_window.setValue(self._get_set("irregular_window_seconds", 60, int))
        self.irr_sd.setValue(self._get_set("irregular_sd_threshold", 5, int))
        self.irr_ratio.setValue(self._get_set("irregular_jump_ratio_pct", 30, int))
        self.irr_rest.setValue(self._get_set("irregular_rest_max_hr", 100, int))
        self.duration_input.setValue(self._get_set("abnormal_duration", 10, int))
        self.cooldown_input.setValue(self._get_set("cooldown_seconds", 300, int) // 60)
        self.periods_table.setRowCount(0)
        for p in self._load_periods_cfg():
            self._add_period_row(p)
        self.update_ui_state()

    def update_ui_state(self, state=None):
        """根据启用状态更新各渠道控件可用性"""
        for enabled_cb, widgets in [
            (self.meow_enabled, [self.meow_nickname, self.meow_test_btn]),
            (self.bark_enabled, [self.bark_key_input, self.bark_server_input,
                                 self.bark_level, self.bark_sound, self.bark_group,
                                 self.bark_test_btn]),
            (self.ntfy_enabled, [self.ntfy_topic_input, self.ntfy_server_input,
                                 self.ntfy_priority, self.ntfy_tags, self.ntfy_token,
                                 self.ntfy_test_btn]),
            (self.irr_enabled, [self.irr_window, self.irr_sd, self.irr_ratio, self.irr_rest]),
        ]:
            for w in widgets:
                w.setEnabled(enabled_cb.isChecked())

    def _collect_channel(self, kind: str):
        """按渠道类型构造推送实例, 返回 (实例或None, 参数错误说明)"""
        from push_notifier import MeowPush, BarkPush, NtfyPush
        if kind == "meow":
            if not self.meow_enabled.isChecked():
                return None, "MeoW未启用"
            ch = MeowPush(self.meow_nickname.text())
            if not ch.nicknames:
                return None, "请先填写MeoW昵称"
            return ch, ""
        if kind == "bark":
            if not self.bark_enabled.isChecked():
                return None, "Bark未启用"
            ch = BarkPush(self.bark_key_input.text(), self.bark_server_input.text(),
                          level=self.bark_level.currentData(),
                          sound=self.bark_sound.text(),
                          group=self.bark_group.text())
            if not ch.keys:
                return None, "请先填写Bark推送Key"
            return ch, ""
        if kind == "ntfy":
            if not self.ntfy_enabled.isChecked():
                return None, "ntfy未启用"
            ch = NtfyPush(self.ntfy_topic_input.text(), self.ntfy_server_input.text(),
                          priority=self.ntfy_priority.currentData(),
                          tags=self.ntfy_tags.text(),
                          token=self.ntfy_token.text())
            if not ch.topics:
                return None, "请先填写ntfy订阅主题"
            return ch, ""
        return None, "未知渠道"

    def test_channel(self, kind: str):
        """单渠道测试推送: 后台线程发请求, QTimer轮询结果回UI线程弹窗"""
        ch, err = self._collect_channel(kind)
        if ch is None:
            QMessageBox.warning(self, "警告", err)
            return

        btn = self.sender()
        btn.setEnabled(False)
        result = {}

        def worker():
            test_msg = f"HRMLink {ch.name}通知测试成功, 收到即配置正确"
            try:
                result["ok"], result["msg"] = ch.push("测试推送", test_msg)
            except Exception as e:
                result["ok"], result["msg"] = False, str(e)
            record_push(ch.name, "测试推送", test_msg, result["ok"], result["msg"])

        threading.Thread(target=worker, daemon=True).start()

        def check():
            if "ok" in result:
                self.poll_timer.stop()
                btn.setEnabled(True)
                if result["ok"]:
                    QMessageBox.information(self, "成功", f"[{ch.name}] {result['msg']}")
                else:
                    QMessageBox.warning(self, "失败", f"[{ch.name}] {result['msg']}")

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(check)
        self.poll_timer.start(300)

    def save_settings(self):
        """保存推送设置"""
        # 启用的渠道必须填写参数
        if self.meow_enabled.isChecked() and not self.meow_nickname.text().strip():
            QMessageBox.warning(self, "警告", "MeoW已启用, 请填写昵称")
            return
        if self.bark_enabled.isChecked() and not self.bark_key_input.text().strip():
            QMessageBox.warning(self, "警告", "Bark已启用, 请填写推送Key")
            return
        if self.ntfy_enabled.isChecked() and not self.ntfy_topic_input.text().strip():
            QMessageBox.warning(self, "警告", "ntfy已启用, 请填写订阅主题")
            return
        if (self.min_hr_input.value() > 0 and self.max_hr_input.value() > 0
                and self.min_hr_input.value() >= self.max_hr_input.value()):
            QMessageBox.warning(self, "警告", "心率下限必须小于上限(填0表示该项不检测)")
            return
        rules = self._collect_periods()
        if rules is None:
            return

        self._up_set("max_hr", self.max_hr_input.value())
        self._up_set("min_hr", self.min_hr_input.value())
        self._up_set("abnormal_duration", self.duration_input.value())
        self._up_set("cooldown_seconds", self.cooldown_input.value() * 60)
        self._up_set("meow_enabled", self.meow_enabled.isChecked())
        self._up_set("meow_nickname", self.meow_nickname.text().strip())
        self._up_set("bark_enabled", self.bark_enabled.isChecked())
        self._up_set("bark_device_key", self.bark_key_input.text().strip())
        self._up_set("bark_server", self.bark_server_input.text().strip())
        self._up_set("bark_level", self.bark_level.currentData())
        self._up_set("bark_sound", self.bark_sound.text().strip())
        self._up_set("bark_group", self.bark_group.text().strip())
        self._up_set("ntfy_enabled", self.ntfy_enabled.isChecked())
        self._up_set("ntfy_topic", self.ntfy_topic_input.text().strip())
        self._up_set("ntfy_server", self.ntfy_server_input.text().strip())
        self._up_set("ntfy_priority", self.ntfy_priority.currentData())
        self._up_set("ntfy_tags", self.ntfy_tags.text().strip())
        self._up_set("ntfy_token", self.ntfy_token.text().strip())
        self._up_set("irregular_enabled", self.irr_enabled.isChecked())
        self._up_set("irregular_window_seconds", self.irr_window.value())
        self._up_set("irregular_sd_threshold", self.irr_sd.value())
        self._up_set("irregular_jump_ratio_pct", self.irr_ratio.value())
        self._up_set("irregular_rest_max_hr", self.irr_rest.value())
        self._up_set("periods", json.dumps(rules, ensure_ascii=False))

        # 通知主窗口重新加载配置
        self.push_settings_changed.emit(self.get_config())

        QMessageBox.information(self, "成功", "推送设置已保存")

    def get_config(self):
        """获取当前推送配置"""
        return {
            "max_hr": self.max_hr_input.value(),
            "min_hr": self.min_hr_input.value(),
            "abnormal_duration": self.duration_input.value(),
            "cooldown_seconds": self.cooldown_input.value() * 60,
            "meow_enabled": self.meow_enabled.isChecked(),
            "meow_nickname": self.meow_nickname.text().strip(),
            "bark_enabled": self.bark_enabled.isChecked(),
            "bark_device_key": self.bark_key_input.text().strip(),
            "bark_server": self.bark_server_input.text().strip(),
            "bark_level": self.bark_level.currentData(),
            "bark_sound": self.bark_sound.text().strip(),
            "bark_group": self.bark_group.text().strip(),
            "ntfy_enabled": self.ntfy_enabled.isChecked(),
            "ntfy_topic": self.ntfy_topic_input.text().strip(),
            "ntfy_server": self.ntfy_server_input.text().strip(),
            "ntfy_priority": self.ntfy_priority.currentData(),
            "ntfy_tags": self.ntfy_tags.text().strip(),
            "ntfy_token": self.ntfy_token.text().strip(),
            "irregular_enabled": self.irr_enabled.isChecked(),
            "irregular_window_seconds": self.irr_window.value(),
            "irregular_sd_threshold": self.irr_sd.value(),
            "irregular_jump_ratio_pct": self.irr_ratio.value(),
            "irregular_rest_max_hr": self.irr_rest.value(),
            "periods_count": self.periods_table.rowCount(),
        }

    def _add_period_row(self, p=None):
        """向规则表格添加一行; p 为规则字典(载入配置时), 缺省为默认值"""
        if not isinstance(p, dict):
            p = {}
        r = self.periods_table.rowCount()
        self.periods_table.insertRow(r)
        enabled_item = QTableWidgetItem()
        enabled_item.setCheckState(Qt.Checked if p.get("enabled", True) else Qt.Unchecked)
        enabled_item.setTextAlignment(Qt.AlignCenter)
        self.periods_table.setItem(r, 0, enabled_item)
        for col, (key, dft) in enumerate(
                [("start", "22:00"), ("end", "07:00"), ("max", "80"),
                 ("min", "40"), ("sustain", "10"), ("cooldown", "10")], start=1):
            self.periods_table.setItem(r, col, QTableWidgetItem(str(p.get(key, dft))))
        self.periods_table.setRowHeight(r, 30)

    def _delete_period_row(self):
        """删除规则表格当前选中行"""
        r = self.periods_table.currentRow()
        if r >= 0:
            self.periods_table.removeRow(r)

    def _load_periods_cfg(self) -> list:
        """从配置读取时段规则列表(仅用于回填表格)"""
        raw = str(gs("Push", "periods", "", str, "-Push")).strip()
        if raw:
            try:
                items = json.loads(raw)
                if isinstance(items, list):
                    return [p for p in items if isinstance(p, dict)]
            except Exception as e:
                logger.warning(f"解析时段规则失败: {e}")
        return []

    def _collect_periods(self):
        """收集并校验表格中的时段规则, 校验失败返回 None"""
        rules = []
        for r in range(self.periods_table.rowCount()):
            def cell(c, _r=r):
                item = self.periods_table.item(_r, c)
                return item.text().strip() if item else ""
            item0 = self.periods_table.item(r, 0)
            enabled = item0 is not None and item0.checkState() == Qt.Checked
            start, end = cell(1), cell(2)
            try:
                max_hr, min_hr = int(cell(3)), int(cell(4))
                sustain, cooldown = int(cell(5)), int(cell(6))
            except ValueError:
                QMessageBox.warning(self, "警告", f"第{r + 1}行规则: 上限/下限/持续/冷却必须为整数")
                return None
            try:
                datetime.datetime.strptime(start, "%H:%M")
                datetime.datetime.strptime(end, "%H:%M")
            except ValueError:
                QMessageBox.warning(self, "警告", f"第{r + 1}行规则: 时间格式应为 HH:MM, 如 22:00")
                return None
            if start == end:
                QMessageBox.warning(self, "警告", f"第{r + 1}行规则: 时段起止不能相同")
                return None
            if max_hr < 0 or min_hr < 0 or (max_hr > 0 and min_hr > 0 and min_hr >= max_hr):
                QMessageBox.warning(self, "警告", f"第{r + 1}行规则: 上下限无效(需下限<上限, 或填0表示不检测)")
                return None
            if not 1 <= sustain <= 600 or not 0 <= cooldown <= 1440:
                QMessageBox.warning(self, "警告", f"第{r + 1}行规则: 持续秒数(1-600)或冷却分钟(0-1440)超出范围")
                return None
            rules.append({"enabled": enabled, "start": start, "end": end,
                          "max": max_hr, "min": min_hr,
                          "sustain": sustain, "cooldown": cooldown * 60})
        return rules

    def _get_set(self, option: str, default, type_=None):
        """获取设置项"""
        return gs('Push', option, default, type_, "推送设置")

    def _up_set(self, option: str, value):
        """更新设置项"""
        ups('Push', option, value, "推送设置")
