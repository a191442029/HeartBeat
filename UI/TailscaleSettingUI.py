"""
Tailscale 数据服务设置界面
配置安卓接收端通过 Tailscale 访问的 WS推送/HTTP轮询 服务:
- 绑定地址可编辑(auto=自动探测Tailscale IP, 或手填具体IP)
- 端口可配置(默认8765)
- 保存后由主窗口重启/停止服务
"""
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QLabel, QLineEdit, QPushButton,
                             QGroupBox, QMessageBox, QCheckBox, QFormLayout)
from PyQt5.QtCore import Qt, pyqtSignal
import logging
from webpush_server import detect_tailscale_ip, validate_bind_address
from .basicwidgets import group_layout, button_row, hint_label, FORM_MAX_WIDTH

# 获取logger
logger = logging.getLogger('__main__')


class TailscaleSettingsUI(QWidget):
    """Tailscale 数据服务设置界面"""

    # 定义信号：设置变化
    tailscale_settings_changed = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_ui()
        self.load_settings()

    def setup_ui(self):
        """设置UI布局"""
        layout = QVBoxLayout()

        # === 启用服务 ===
        self.enable_checkbox = QCheckBox("启用心率数据服务(WS推送 + HTTP轮询)")
        self.enable_checkbox.clicked.connect(self.toggle_enabled)
        layout.addWidget(self.enable_checkbox)

        # === 服务配置 ===
        config_group = QGroupBox("Tailscale 服务配置")
        config_group.setMaximumWidth(FORM_MAX_WIDTH)  # 表单限宽, 与MQTT/InfluxDB页保持一致
        config_layout = group_layout(QVBoxLayout())

        # 表单区: 标签右对齐统一列宽
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)

        # 绑定地址(可编辑)
        self.address_input = QLineEdit()
        self.address_input.setPlaceholderText("auto=自动探测Tailscale IP, 或手填如 100.100.1.25")
        self.address_input.setMinimumWidth(340)
        form.addRow("绑定地址:", self.address_input)

        # 端口
        self.port_input = QLineEdit()
        self.port_input.setPlaceholderText("默认 8765")
        form.addRow("端口:", self.port_input)

        config_layout.addLayout(form)

        # 服务运行状态提示
        self.status_label = QLabel("服务未运行")
        self.status_label.setWordWrap(True)
        config_layout.addWidget(self.status_label)

        config_layout.addWidget(hint_label(
            "安卓手机需安装 Tailscale 并登录同一账号(虚拟网卡IP为100.x.x.x), "
            "然后在接收端APP中填写上方显示的服务地址。绑定 auto 时自动使用本机 Tailscale IP, "
            "不会暴露到局域网; 无 Tailscale 时回退 127.0.0.1 仅本机可访问。"
            "手机浏览器访问 http://服务地址 可直接打开网页版心率显示。"))
        config_group.setLayout(config_layout)
        layout.addWidget(config_group)

        # === 按钮区域: 探测IP | 保存设置 ===
        self.detect_button = QPushButton("探测本机Tailscale IP")
        self.detect_button.clicked.connect(self.show_detected_ip)
        self.save_button = QPushButton("保存设置")
        self.save_button.clicked.connect(self.save_settings)
        layout.addLayout(button_row(self.detect_button, self.save_button))

        layout.addStretch()

        self.setLayout(layout)

        # 初始化UI状态
        self.update_ui_state()

    def load_settings(self):
        """加载设置"""
        try:
            from system_utils import gs

            enabled = gs("Tailscale", "enabled", False, bool)
            address = gs("Tailscale", "address", "auto", str)
            port = gs("Tailscale", "port", 8765, int)

            self.address_input.setText(address)
            self.port_input.setText(str(port))
            self.enable_checkbox.setChecked(enabled)

            logger.info("已加载Tailscale设置")
        except Exception as e:
            logger.error(f"加载Tailscale设置失败: {e}")

    def save_settings(self):
        """保存设置"""
        try:
            from system_utils import ups

            address = self.address_input.text().strip() or "auto"
            port_text = self.port_input.text().strip() or "8765"

            # 校验端口
            try:
                port = int(port_text)
                if not (1 <= port <= 65535):
                    raise ValueError
            except ValueError:
                QMessageBox.warning(self, "验证失败", "端口必须为 1-65535 的整数")
                return False

            # 校验地址: auto 或合法IP
            if address.lower() != "auto" and not validate_bind_address(address):
                QMessageBox.warning(self, "验证失败", "绑定地址必须为 auto 或合法IP地址")
                return False

            ups("Tailscale", "address", address)
            ups("Tailscale", "port", port)
            ups("Tailscale", "enabled", self.enable_checkbox.isChecked())

            logger.info("已保存Tailscale设置")
            QMessageBox.information(self, "成功", "Tailscale设置已保存")

            # 发出设置变化信号(主窗口按新配置重启/停止服务)
            self.tailscale_settings_changed.emit({
                "enabled": self.enable_checkbox.isChecked(),
                "address": address,
                "port": port
            })
            return True

        except Exception as e:
            logger.error(f"保存Tailscale设置失败: {e}")
            QMessageBox.critical(self, "错误", f"保存设置失败: {str(e)}")
            return False

    def toggle_enabled(self):
        """切换服务启用状态"""
        enabled = self.enable_checkbox.isChecked()

        # 禁用/启用配置字段
        self.update_ui_state()

        # 保存启用状态
        try:
            from system_utils import ups
            ups("Tailscale", "enabled", enabled)
            logger.info(f"Tailscale服务启用状态已更改: {enabled}")
        except Exception as e:
            logger.error(f"保存Tailscale启用状态失败: {e}")

        # 即时生效: 启用则启动服务, 停用则停止
        self.tailscale_settings_changed.emit({
            "enabled": enabled,
            "address": self.address_input.text().strip() or "auto",
            "port": self.port_input.text().strip() or "8765"
        })

    def update_ui_state(self):
        """更新UI状态(启用联动)"""
        enabled = self.enable_checkbox.isChecked()
        self.address_input.setEnabled(enabled)
        self.port_input.setEnabled(enabled)
        self.save_button.setEnabled(enabled)
        self.detect_button.setEnabled(enabled)

    def set_server_state(self, text, running=False):
        """由主窗口回写服务运行状态"""
        self.status_label.setText(text)
        self.status_label.setStyleSheet(
            "color: green; font-weight: bold;" if running else "color: gray;")

    def show_detected_ip(self):
        """探测并显示本机Tailscale IP"""
        ip = detect_tailscale_ip()
        if ip:
            QMessageBox.information(
                self, "探测成功",
                f"本机 Tailscale IP: {ip}\n\n"
                f"手机端连接地址: ws://{ip}:{self.port_input.text().strip() or '8765'}/ws")
        else:
            QMessageBox.warning(
                self, "未探测到",
                "未发现 Tailscale 网卡(100.64.0.0/10网段)。\n"
                "请确认本机已安装并启动 Tailscale 且已登录 tailnet。")
