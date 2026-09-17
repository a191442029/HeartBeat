from PyQt5.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QGroupBox, QPushButton, QSpinBox, QCheckBox, QMessageBox,
    QWidget, QSizePolicy, QApplication, QFormLayout
)
from PyQt5.QtCore import Qt, pyqtSignal, QTimer

from system_utils import logger, try_except, ups, gs, dpapi_protect, dpapi_unprotect
from .basicwidgets import group_layout, button_row, FORM_MAX_WIDTH

class MQTTSettingsUI(QWidget):
    """MQTT设置界面"""
    mqtt_settings_changed = pyqtSignal(dict)
    
    @try_except("MQTT设置UI初始化")
    def __init__(self):
        super().__init__()
        self.setup_ui()
        self.load_settings()
    
    def setup_ui(self):
        main_layout = QVBoxLayout()

        # 创建一个分组框来包含所有MQTT设置
        mqtt_group = QGroupBox("MQTT设置")
        layout = group_layout(QVBoxLayout())
        mqtt_group.setLayout(layout)
        mqtt_group.setMaximumWidth(FORM_MAX_WIDTH)  # 表单限宽, 避免输入框通栏过宽

        # MQTT启用开关
        self.mqtt_enabled = QCheckBox("启用MQTT")
        self.mqtt_enabled.stateChanged.connect(self.on_mqtt_enabled_changed)
        layout.addWidget(self.mqtt_enabled)

        # 表单区: 标签左对齐统一列宽, 输入框右对齐
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)

        # 服务器设置
        self.broker_input = QLineEdit()
        self.broker_input.setPlaceholderText("例如: 192.168.1.100")
        self.broker_input.setMinimumWidth(280)
        form.addRow("服务器地址:", self.broker_input)

        # 端口设置
        self.port_input = QSpinBox()
        self.port_input.setRange(1, 65535)
        self.port_input.setValue(1883)
        form.addRow("端口:", self.port_input)

        # 用户名设置
        self.username_input = QLineEdit()
        self.username_input.setPlaceholderText("(可选)")
        self.username_input.setMinimumWidth(280)
        form.addRow("用户名:", self.username_input)

        # 密码设置
        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("(可选)")
        self.password_input.setEchoMode(QLineEdit.Password)
        self.password_input.setMinimumWidth(280)
        form.addRow("密码:", self.password_input)

        # 主题设置
        self.topic_input = QLineEdit()
        self.topic_input.setPlaceholderText("例如: homeassistant/sensor/heartrate/state")
        self.topic_input.setMinimumWidth(340)
        form.addRow("主题:", self.topic_input)

        # 自动发现设置
        self.discovery_enabled = QCheckBox("启用Home Assistant自动发现")
        self.discovery_enabled.setChecked(True)
        form.addRow("", self.discovery_enabled)

        self.discovery_topic_input = QLineEdit()
        self.discovery_topic_input.setPlaceholderText("例如: homeassistant/sensor/heartrate/config")
        self.discovery_topic_input.setMinimumWidth(340)
        form.addRow("发现主题:", self.discovery_topic_input)

        layout.addLayout(form)

        # 操作按钮行: 测试连接 | 保存设置 (与InfluxDB/推送页交互模式一致)
        self.test_button = QPushButton("测试连接")
        self.test_button.clicked.connect(self.test_connection)
        self.save_button = QPushButton("保存设置")
        self.save_button.clicked.connect(self.save_settings)
        layout.addLayout(button_row(self.test_button, self.save_button))

        # 将分组框添加到主布局
        main_layout.addWidget(mqtt_group)
        main_layout.addStretch()

        self.setLayout(main_layout)
        
    def load_settings(self):
        """加载MQTT设置"""
        self.mqtt_enabled.setChecked(self._get_set("enabled", False, bool))
        self.broker_input.setText(self._get_set("broker", "localhost", str))
        self.port_input.setValue(self._get_set("port", 1883, int))
        self.username_input.setText(self._get_set("username", "", str))
        self.password_input.setText(dpapi_unprotect(self._get_set("password", "", str)))
        self.topic_input.setText(self._get_set("topic", "homeassistant/sensor/heartrate/state", str))
        self.discovery_enabled.setChecked(self._get_set("discovery_enabled", True, bool))
        self.discovery_topic_input.setText(self._get_set("discovery_topic", "homeassistant/sensor/heartrate/config", str))
        
        # 根据是否启用MQTT来设置控件状态
        self.update_ui_state()
    
    def update_ui_state(self):
        """根据MQTT启用状态更新UI控件状态"""
        enabled = self.mqtt_enabled.isChecked()
        self.broker_input.setEnabled(enabled)
        self.port_input.setEnabled(enabled)
        self.username_input.setEnabled(enabled)
        self.password_input.setEnabled(enabled)
        self.topic_input.setEnabled(enabled)
        self.discovery_enabled.setEnabled(enabled)
        self.discovery_topic_input.setEnabled(enabled and self.discovery_enabled.isChecked())
        self.test_button.setEnabled(enabled)
    
    def on_mqtt_enabled_changed(self, state):
        """MQTT启用状态改变时更新UI"""
        self.update_ui_state()
    
    def save_settings(self):
        """保存MQTT设置"""
        self._up_set("enabled", self.mqtt_enabled.isChecked())
        self._up_set("broker", self.broker_input.text())
        self._up_set("port", self.port_input.value())
        self._up_set("username", self.username_input.text())
        self._up_set("password", dpapi_protect(self.password_input.text()))
        self._up_set("topic", self.topic_input.text())
        self._up_set("discovery_enabled", self.discovery_enabled.isChecked())
        self._up_set("discovery_topic", self.discovery_topic_input.text())
        
        # 发送设置更改信号
        self.mqtt_settings_changed.emit(self.get_config())
        
        QMessageBox.information(self, "成功", "MQTT设置已保存")
    
    def get_config(self):
        """获取当前MQTT配置"""
        return {
            "enabled": self.mqtt_enabled.isChecked(),
            "broker": self.broker_input.text(),
            "port": self.port_input.value(),
            "username": self.username_input.text(),
            "password": self.password_input.text(),
            "topic": self.topic_input.text(),
            "discovery_enabled": self.discovery_enabled.isChecked(),
            "discovery_topic": self.discovery_topic_input.text(),
            "client_id": "heartbeat_monitor"
        }
    
    def test_connection(self):
        """测试MQTT连接"""
        from mqtt_client import MQTTClient
        import time
        
        if not self.mqtt_enabled.isChecked():
            QMessageBox.warning(self, "警告", "MQTT未启用")
            return
        
        # 获取当前配置
        config = self.get_config()
        
        # 创建测试对话框
        test_dialog = QMessageBox(self)
        test_dialog.setWindowTitle("测试中")
        test_dialog.setText("正在尝试连接到MQTT服务器...")
        test_dialog.setStandardButtons(QMessageBox.Cancel)
        test_dialog.setDefaultButton(QMessageBox.Cancel)
        test_dialog.show()
        
        # 设置定时器，确保即使用户不点击也会在10秒后关闭
        close_timer = QTimer()
        close_timer.setSingleShot(True)
        close_timer.timeout.connect(test_dialog.close)
        close_timer.start(10000)  # 10秒后自动关闭
        
        # 创建临时MQTT客户端进行测试
        mqtt_client = MQTTClient()
        
        # 处理连接结果的函数
        def handle_result(connected):
            test_dialog.close()
            if connected:
                QMessageBox.information(self, "成功", "成功连接到MQTT服务器")
                # 发布测试消息
                try:
                    mqtt_client.publish_heart_rate(0, time.strftime("%Y-%m-%d %H:%M:%S"), "test")
                except Exception as e:
                    from system_utils import logger
                    logger.error(f"发送测试消息失败: {str(e)}")
            else:
                QMessageBox.warning(self, "失败", "无法连接到MQTT服务器，请检查设置")
        
        # 创建取消标志
        self.test_cancelled = False
        
        # 连接取消按钮
        test_dialog.rejected.connect(lambda: self.cancel_test_connection())
        
        try:
            # 尝试连接
            success = mqtt_client.connect(config)
            
            # 等待连接回调执行，每0.5秒检查一次，最多等待5秒
            for i in range(10):
                if self.test_cancelled:
                    # 用户取消了测试
                    try:
                        mqtt_client.disconnect()
                    except:
                        pass
                    test_dialog.close()
                    return
                
                QApplication.processEvents()
                time.sleep(0.5)
                QApplication.processEvents()
                
                # 如果已连接，提前退出循环
                if mqtt_client.connected:
                    break
            
            # 检查连接状态
            connected = mqtt_client.connected
            
            # 无论成功与否，都断开连接
            try:
                mqtt_client.disconnect()
            except:
                pass
            
            # 关闭测试对话框
            test_dialog.close()
            
            # 处理结果
            handle_result(connected)
            
        except Exception as e:
            from system_utils import logger
            logger.error(f"测试MQTT连接时出错: {str(e)}")
            test_dialog.close()
            handle_result(False)
    
    def _get_set(self, option: str, default, type_=None):
        """获取设置项"""
        return gs('MQTT', option, default, type_, "MQTT设置")
    
    def _up_set(self, option: str, value):
        """更新设置项"""
        ups('MQTT', option, value, "MQTT设置")
        
    def cancel_test_connection(self):
        """取消测试连接"""
        self.test_cancelled = True
        logger.info("用户取消了MQTT连接测试")
