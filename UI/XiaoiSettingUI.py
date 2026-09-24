from PyQt5.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QGroupBox,
    QPushButton, QCheckBox, QMessageBox, QWidget, QFormLayout, QListWidget,
    QListWidgetItem
)
from PyQt5.QtCore import pyqtSignal, QTimer, Qt
from system_utils import logger, ups, gs, dpapi_protect
from push_notifier import XiaoiPush, record_push
import json
import threading

from .basicwidgets import group_layout, hint_label, button_row, FORM_MAX_WIDTH


class XiaoiSettingsUI(QWidget):
    """小爱音箱语音播报设置界面 (EXE直连小米云端播报, 无需xiaoi桥接/Docker)"""
    xiaoi_settings_changed = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self._pass_b64 = ""   # 已保存的加密密码(留空密码框时沿用)
        self._speakers = []   # 登录后缓存的音箱列表 [{'deviceID','name','hardware'}]
        self.setup_ui()
        self.load_settings()

    def setup_ui(self):
        main_layout = QVBoxLayout()

        group = QGroupBox("小爱音箱语音播报 (直连小米云端)")
        group.setMaximumWidth(FORM_MAX_WIDTH)  # 与MQTT/InfluxDB页保持一致的表单限宽
        layout = group_layout(QVBoxLayout())
        group.setLayout(layout)

        self.enabled = QCheckBox("启用 (告警时语音播报, 与手机推送渠道可同时生效)")
        self.enabled.stateChanged.connect(self.update_ui_state)
        layout.addWidget(self.enabled)

        # 表单区: 标签右对齐统一列宽
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)

        self.user_input = QLineEdit()
        self.user_input.setPlaceholderText("小米账号 (手机号/邮箱/小米ID)")
        self.user_input.setMinimumWidth(320)
        form.addRow("小米账号:", self.user_input)

        self.pass_input = QLineEdit()
        self.pass_input.setEchoMode(QLineEdit.Password)
        self.pass_input.setPlaceholderText("小米密码 (首次填写; 已保存时留空沿用)")
        self.pass_input.setMinimumWidth(320)
        form.addRow("密码:", self.pass_input)

        login_row = QHBoxLayout()
        self.login_btn = QPushButton("登录并获取音箱")
        self.login_btn.clicked.connect(self.login_fetch_speakers)
        login_row.addWidget(self.login_btn)
        self.status_label = QLabel("")
        login_row.addWidget(self.status_label)
        login_row.addStretch()
        form.addRow("音箱列表:", login_row)

        self.speaker_list = QListWidget()
        self.speaker_list.setMinimumHeight(100)
        self.speaker_list.setMaximumHeight(140)
        self.speaker_list.setToolTip("勾选要播报的音箱(可多台); 全不勾则播第一台")
        form.addRow("", self.speaker_list)

        test_row = QHBoxLayout()
        self.test_btn = QPushButton("测试")
        self.test_btn.clicked.connect(self.test_push)
        test_row.addWidget(self.test_btn)
        test_row.addStretch()
        form.addRow("测试:", test_row)

        layout.addLayout(form)

        self.save_button = QPushButton("保存设置")
        self.save_button.clicked.connect(self.save_settings)
        layout.addLayout(button_row(self.save_button))
        main_layout.addWidget(group)

        main_layout.addWidget(hint_label(
            "EXE内置小米云端直连(MiOT TTS优先/MiNA兜底), 无需再部署xiaoi桥接服务或Docker。\n"
            "填写小米账号密码后点\"登录并获取音箱\", 勾选音箱并保存;\n"
            "密码经Windows DPAPI加密存储; 登录凭证缓存在程序目录 xiaomi_token.json。\n"
            "如提示需要二次验证: 先在手机米家APP或网页版登录一次并允许新设备后再试。\n"
            "心率告警/设备断连/心律不齐等告警会由小爱音箱语音播报。"))

        main_layout.addStretch()

        self.setLayout(main_layout)

    def update_ui_state(self, state=None):
        """根据启用状态更新控件可用性"""
        on = self.enabled.isChecked()
        for w in (self.user_input, self.pass_input, self.login_btn,
                  self.speaker_list, self.test_btn):
            w.setEnabled(on)

    def load_settings(self):
        """加载小爱音箱设置(含音箱列表缓存, 免重复登录即可回显勾选)"""
        self.enabled.setChecked(self._get("xiaoi_enabled", False, bool))
        self.user_input.setText(self._get("xiaoi_user", "", str))
        self._pass_b64 = self._get("xiaoi_pass_b64", "", str)
        try:
            self._speakers = json.loads(self._get("xiaoi_speakers_json", "[]", str) or "[]")
        except Exception:
            self._speakers = []
        self._fill_speaker_list(self._get("xiaoi_dids", "", str))
        if self._pass_b64:
            self.pass_input.setPlaceholderText("已加密保存 (留空沿用原密码)")
        self.update_ui_state()

    def _fill_speaker_list(self, dids_csv: str):
        """按缓存列表填充勾选框; dids_csv为已选deviceID逗号串"""
        selected = {x.strip() for x in (dids_csv or "").split(",") if x.strip()}
        self.speaker_list.clear()
        for spk in self._speakers:
            label = f"{spk.get('name', '')} [{spk.get('hardware', '')}]"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, spk.get("deviceID", ""))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if str(spk.get("deviceID", "")) in selected
                               else Qt.Unchecked)
            self.speaker_list.addItem(item)

    def _selected_dids(self) -> list:
        """勾选中的deviceID列表"""
        out = []
        for i in range(self.speaker_list.count()):
            it = self.speaker_list.item(i)
            if it.checkState() == Qt.Checked:
                out.append(str(it.data(Qt.UserRole)))
        return out

    def _effective_pass_b64(self) -> str:
        """当前生效密码的加密串: 新填优先, 否则沿用已存"""
        pwd = self.pass_input.text().strip()
        return dpapi_protect(pwd) if pwd else self._pass_b64

    def save_settings(self):
        """保存小爱音箱设置"""
        self._up("xiaoi_enabled", self.enabled.isChecked())
        self._up("xiaoi_user", self.user_input.text().strip())
        self._pass_b64 = self._effective_pass_b64()
        self._up("xiaoi_pass_b64", self._pass_b64)
        self._up("xiaoi_dids", ",".join(self._selected_dids()))
        if self.pass_input.text().strip():
            self.pass_input.clear()
            self.pass_input.setPlaceholderText("已加密保存 (留空沿用原密码)")
        self.xiaoi_settings_changed.emit(self.get_config())
        QMessageBox.information(self, "成功", "小爱音箱设置已保存")

    def get_config(self):
        """获取当前小爱音箱配置(保存后 notifier.load_config 据此重建渠道)"""
        return {
            "xiaoi_enabled": self.enabled.isChecked(),
            "xiaoi_user": self.user_input.text().strip(),
            "xiaoi_pass_b64": self._pass_b64,
            "xiaoi_dids": ",".join(self._selected_dids()),
        }

    def login_fetch_speakers(self):
        """登录小米账号并拉取音箱列表: 后台线程执行, QTimer轮询结果回UI线程"""
        user = self.user_input.text().strip()
        if not user:
            QMessageBox.warning(self, "提示", "请先填写小米账号")
            return
        pass_b64 = self._effective_pass_b64()
        if not pass_b64:
            QMessageBox.warning(self, "提示", "请先填写小米密码")
            return
        self.login_btn.setEnabled(False)
        self.status_label.setText("登录中...")
        result = {}

        def worker():
            from xiaomi_tts import xiaoi_speakers_sync
            result["r"] = xiaoi_speakers_sync(user, pass_b64)

        threading.Thread(target=worker, daemon=True).start()

        def check():
            if "r" not in result:
                return
            self._login_timer.stop()
            self.login_btn.setEnabled(True)
            ok, data = result["r"]
            if not ok:
                self.status_label.setText("登录失败")
                QMessageBox.warning(self, "登录失败", str(data))
                return
            self._speakers = data
            logger.info(f"[小爱音箱] 登录成功, 获取到{len(data)}台音箱")
            # 凭证与列表立即落盘(下次打开免登录; 保存按钮负责did勾选)
            self._up("xiaoi_user", user)
            self._pass_b64 = pass_b64
            self._up("xiaoi_pass_b64", pass_b64)
            self._up("xiaoi_speakers_json", json.dumps(data, ensure_ascii=False))
            self._fill_speaker_list(self._get("xiaoi_dids", "", str))
            if self.pass_input.text().strip():
                self.pass_input.clear()
                self.pass_input.setPlaceholderText("已加密保存 (留空沿用原密码)")
            self.status_label.setText(f"已获取{len(data)}台音箱")
            QMessageBox.information(self, "成功",
                                    f"登录成功, 获取到{len(data)}台音箱, 请勾选后保存")

        # 登录/测试各用独立QTimer(共用会被后者覆盖, 致登录轮询永续而测试按钮禁死)
        old = getattr(self, "_login_timer", None)
        if old:
            old.stop()
        self._login_timer = QTimer(self)
        self._login_timer.timeout.connect(check)
        self._login_timer.start(300)

    def test_push(self):
        """测试播报: 后台线程直连播报, QTimer轮询结果回UI线程弹窗"""
        if not self.enabled.isChecked():
            QMessageBox.warning(self, "警告", "请先启用小爱音箱播报")
            return
        user = self.user_input.text().strip()
        pass_b64 = self._effective_pass_b64()
        if not user or not pass_b64:
            QMessageBox.warning(self, "提示", "请先填写账号密码并登录获取音箱")
            return
        dids = self._selected_dids()
        if not dids and not self._speakers:
            QMessageBox.warning(self, "提示", "请先\"登录并获取音箱\"并勾选要播报的音箱")
            return
        ch = XiaoiPush(user, pass_b64, ",".join(dids))
        self.test_btn.setEnabled(False)
        result = {}

        def worker():
            test_msg = "HRMLink小爱音箱播报测试成功, 收到即配置正确"
            try:
                result["ok"], result["msg"] = ch.push("测试播报", test_msg)
            except Exception as e:
                result["ok"], result["msg"] = False, str(e)
            record_push(ch.name, "测试播报", test_msg, result["ok"], result["msg"])

        threading.Thread(target=worker, daemon=True).start()

        def check():
            if "ok" in result:
                self._test_timer.stop()
                self.test_btn.setEnabled(True)
                if result["ok"]:
                    QMessageBox.information(self, "成功", f"[{ch.name}] {result['msg']}")
                else:
                    QMessageBox.warning(self, "失败", f"[{ch.name}] {result['msg']}")

        # 独立QTimer, 与登录的_login_timer互不覆盖
        old = getattr(self, "_test_timer", None)
        if old:
            old.stop()
        self._test_timer = QTimer(self)
        self._test_timer.timeout.connect(check)
        self._test_timer.start(300)

    def _get(self, option: str, default, type_=None):
        """获取设置项"""
        return gs('Push', option, default, type_, "推送设置")

    def _up(self, option: str, value):
        """更新设置项"""
        ups('Push', option, value, "推送设置")
