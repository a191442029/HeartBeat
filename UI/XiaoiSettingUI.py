from PyQt5.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QGroupBox,
    QPushButton, QCheckBox, QMessageBox, QWidget, QSizePolicy, QFormLayout
)
from PyQt5.QtCore import pyqtSignal, QTimer, Qt
from system_utils import logger, ups, gs
from push_notifier import XiaoiPush, record_push
from .basicwidgets import group_layout, hint_label, button_row, FORM_MAX_WIDTH
import threading


class XiaoiSettingsUI(QWidget):
    """小爱音箱语音播报设置界面 (经xiaoi桥接服务Webhook播报, 可作为第4推送渠道)"""
    xiaoi_settings_changed = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.setup_ui()
        self.load_settings()

    def setup_ui(self):
        main_layout = QVBoxLayout()

        group = QGroupBox("小爱音箱语音播报 (xiaoi桥接)")
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

        self.server_input = QLineEdit()
        self.server_input.setPlaceholderText("xiaoi Webhook地址, 默认 127.0.0.1:51666")
        self.server_input.setMinimumWidth(320)
        form.addRow("服务地址:", self.server_input)

        self.token_input = QLineEdit()
        self.token_input.setEchoMode(QLineEdit.Password)
        self.token_input.setPlaceholderText("可选: 服务端webhook.token鉴权时填写")
        self.token_input.setMinimumWidth(320)
        form.addRow("令牌:", self.token_input)

        did_row = QHBoxLayout()
        self.dids_input = QLineEdit()
        self.dids_input.setPlaceholderText("可选: 音箱did/名称, 多台用逗号分隔; 留空按桥接服务默认音箱路由")
        self.dids_input.setMinimumWidth(320)
        did_row.addWidget(self.dids_input)
        self.test_btn = QPushButton("测试")
        self.test_btn.clicked.connect(self.test_push)
        did_row.addWidget(self.test_btn)
        form.addRow("音箱did:", did_row)

        layout.addLayout(form)

        self.save_button = QPushButton("保存设置")
        self.save_button.clicked.connect(self.save_settings)
        layout.addLayout(button_row(self.save_button))
        main_layout.addWidget(group)

        main_layout.addWidget(hint_label(
            "使用前提: 先在本机/局域网部署 xiaoi 桥接服务并登录小米账号, 例如:\n"
            "  docker run -d --name xiaoi-webhook --restart unless-stopped -p 51666:51666 \\\n"
            "    -e XIAOI_USER_ID=你的小米ID -e XIAOI_PASS_TOKEN=你的passToken iusy/xiaoi\n"
            "保存后, 心率告警/设备断连/心律不齐等告警文本会POST到其 /webhook/tts 由小爱音箱语音播报;\n"
            "body传did时该音箱必须已添加到桥接服务的音箱列表且启用, 否则返回400。"))

        main_layout.addStretch()

        self.setLayout(main_layout)

    def update_ui_state(self, state=None):
        """根据启用状态更新控件可用性"""
        on = self.enabled.isChecked()
        for w in (self.server_input, self.dids_input, self.token_input, self.test_btn):
            w.setEnabled(on)

    def load_settings(self):
        """加载小爱音箱设置"""
        self.enabled.setChecked(self._get("xiaoi_enabled", False, bool))
        self.server_input.setText(self._get("xiaoi_server", "", str))
        self.dids_input.setText(self._get("xiaoi_dids", "", str))
        self.token_input.setText(self._get("xiaoi_token", "", str))
        self.update_ui_state()

    def save_settings(self):
        """保存小爱音箱设置"""
        self._up("xiaoi_enabled", self.enabled.isChecked())
        self._up("xiaoi_server", self.server_input.text().strip())
        self._up("xiaoi_dids", self.dids_input.text().strip())
        self._up("xiaoi_token", self.token_input.text().strip())
        self.xiaoi_settings_changed.emit(self.get_config())
        QMessageBox.information(self, "成功", "小爱音箱设置已保存")

    def get_config(self):
        """获取当前小爱音箱配置"""
        return {
            "xiaoi_enabled": self.enabled.isChecked(),
            "xiaoi_server": self.server_input.text().strip(),
            "xiaoi_dids": self.dids_input.text().strip(),
            "xiaoi_token": self.token_input.text().strip(),
        }

    def test_push(self):
        """测试播报: 后台线程发请求, QTimer轮询结果回UI线程弹窗"""
        if not self.enabled.isChecked():
            QMessageBox.warning(self, "警告", "请先启用小爱音箱播报")
            return
        ch = XiaoiPush(self.server_input.text(), self.dids_input.text(),
                       token=self.token_input.text())
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
                self.poll_timer.stop()
                self.test_btn.setEnabled(True)
                if result["ok"]:
                    QMessageBox.information(self, "成功", f"[{ch.name}] {result['msg']}")
                else:
                    QMessageBox.warning(self, "失败", f"[{ch.name}] {result['msg']}")

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(check)
        self.poll_timer.start(300)

    def _get(self, option: str, default, type_=None):
        """获取设置项"""
        return gs('Push', option, default, type_, "推送设置")

    def _up(self, option: str, value):
        """更新设置项"""
        ups('Push', option, value, "推送设置")
