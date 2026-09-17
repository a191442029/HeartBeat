"""
InfluxDB设置界面
用于配置InfluxDB连接参数
"""
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QLineEdit, QPushButton, QGroupBox, QMessageBox, QCheckBox, QFormLayout)
from PyQt5.QtCore import Qt, pyqtSignal
import logging
from influxdb_writer import InfluxDBWriter
from .basicwidgets import group_layout, button_row, FORM_MAX_WIDTH

# 获取logger
logger = logging.getLogger('__main__')


class InfluxDBSettingsUI(QWidget):
    """InfluxDB设置界面"""
    
    # 定义信号：InfluxDB设置变化
    influxdb_settings_changed = pyqtSignal(dict)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.config = None
        self.setup_ui()
        self.load_settings()
    
    def setup_ui(self):
        """设置UI布局"""
        layout = QVBoxLayout()

        # === 启用InfluxDB ===
        self.enable_checkbox = QCheckBox("启用InfluxDB")
        self.enable_checkbox.clicked.connect(self.toggle_enabled)
        layout.addWidget(self.enable_checkbox)

        # === InfluxDB连接配置 ===
        influxdb_group = QGroupBox("InfluxDB配置")
        influxdb_group.setMaximumWidth(FORM_MAX_WIDTH)  # 表单限宽, 与MQTT页保持一致
        influxdb_layout = group_layout(QVBoxLayout())

        # 表单区: 标签右对齐统一列宽
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)

        # 服务器地址
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("例如：http://192.168.1.100:8086")
        self.url_input.setMinimumWidth(340)
        form.addRow("服务器地址:", self.url_input)

        # 访问令牌
        self.token_input = QLineEdit()
        self.token_input.setEchoMode(QLineEdit.Password)
        self.token_input.setPlaceholderText("InfluxDB访问令牌")
        self.token_input.setMinimumWidth(340)
        form.addRow("访问令牌:", self.token_input)

        # 组织名称
        self.org_input = QLineEdit()
        self.org_input.setPlaceholderText("例如：my-org")
        self.org_input.setMinimumWidth(280)
        form.addRow("组织名称:", self.org_input)

        # 存储桶名称
        self.bucket_input = QLineEdit()
        self.bucket_input.setPlaceholderText("例如：heart_rate_data")
        self.bucket_input.setMinimumWidth(280)
        form.addRow("存储桶:", self.bucket_input)

        influxdb_layout.addLayout(form)
        influxdb_group.setLayout(influxdb_layout)
        layout.addWidget(influxdb_group)

        # === 按钮区域: 测试连接 | 保存设置 (与MQTT/推送页交互模式一致) ===
        self.test_button = QPushButton("测试连接")
        self.test_button.clicked.connect(self.test_connection)
        self.save_button = QPushButton("保存设置")
        self.save_button.clicked.connect(self.save_settings)
        layout.addLayout(button_row(self.test_button, self.save_button))

        layout.addStretch()

        self.setLayout(layout)

        # 初始化UI状态
        self.update_ui_state()
    
    def load_settings(self):
        """加载设置"""
        try:
            from system_utils import gs, dpapi_unprotect

            enabled = gs("InfluxDB", "enabled", False, bool)
            self.url_input.setText(gs("InfluxDB", "url", "", str))
            self.token_input.setText(dpapi_unprotect(gs("InfluxDB", "token", "", str)))
            self.org_input.setText(gs("InfluxDB", "org", "", str))
            self.bucket_input.setText(gs("InfluxDB", "bucket", "", str))
            
            # 设置启用状态
            self.enable_checkbox.setChecked(enabled)
            
            logger.info("已加载InfluxDB设置")
            
        except Exception as e:
            logger.error(f"加载InfluxDB设置失败: {e}")
    
    def save_settings(self):
        """保存设置"""
        try:
            from system_utils import ups, dpapi_protect

            url = self.url_input.text().strip()
            token = self.token_input.text().strip()
            org = self.org_input.text().strip()
            bucket = self.bucket_input.text().strip()
            
            # 验证必填项
            if not url or not token or not org or not bucket:
                QMessageBox.warning(self, "验证失败", "请填写所有必填项")
                return False
            
            # 保存到配置文件 (token经DPAPI加密后存储, 仅当前Windows用户可解)
            ups("InfluxDB", "url", url)
            ups("InfluxDB", "token", dpapi_protect(token))
            ups("InfluxDB", "org", org)
            ups("InfluxDB", "bucket", bucket)
            ups("InfluxDB", "enabled", self.enable_checkbox.isChecked())
            
            logger.info("已保存InfluxDB设置")
            QMessageBox.information(self, "成功", "InfluxDB设置已保存")
            
            # 发出设置变化信号
            self.influxdb_settings_changed.emit({
                "url": url,
                "token": token,
                "org": org,
                "bucket": bucket,
                "enabled": self.enable_checkbox.isChecked()
            })
            
            return True
            
        except Exception as e:
            logger.error(f"保存InfluxDB设置失败: {e}")
            QMessageBox.critical(self, "错误", f"保存设置失败: {str(e)}")
            return False
    
    def toggle_enabled(self):
        """切换InfluxDB启用状态"""
        enabled = self.enable_checkbox.isChecked()
        
        # 禁用/启用配置字段
        self.url_input.setEnabled(enabled)
        self.token_input.setEnabled(enabled)
        self.org_input.setEnabled(enabled)
        self.bucket_input.setEnabled(enabled)
        self.test_button.setEnabled(enabled)
        
        # 保存启用状态
        try:
            from system_utils import ups
            ups("InfluxDB", "enabled", enabled)
            logger.info(f"InfluxDB启用状态已更改: {enabled}")
        except Exception as e:
            logger.error(f"保存InfluxDB启用状态失败: {e}")
    
    def update_ui_state(self):
        """更新UI状态"""
        try:
            from system_utils import gs
            enabled = gs("InfluxDB", "enabled", False, bool)
            
            # 设置复选框状态
            self.enable_checkbox.setChecked(enabled)
            
            # 根据启用状态禁用/启用字段
            self.url_input.setEnabled(enabled)
            self.token_input.setEnabled(enabled)
            self.org_input.setEnabled(enabled)
            self.bucket_input.setEnabled(enabled)
            self.test_button.setEnabled(enabled)
            
        except Exception as e:
            logger.error(f"更新InfluxDB UI状态失败: {e}")
    
    def test_connection(self):
        """测试InfluxDB连接"""
        try:
            import importlib
            influxdb_module = importlib.import_module('influxdb_writer')
            InfluxDBWriter = influxdb_module.InfluxDBWriter
            
            url = self.url_input.text().strip()
            token = self.token_input.text().strip()
            org = self.org_input.text().strip()
            bucket = self.bucket_input.text().strip()
            
            # 验证必填项
            if not url or not token or not org or not bucket:
                QMessageBox.warning(self, "验证失败", "请填写所有必填项")
                return
            
            # 创建客户端并测试
            client = InfluxDBWriter(url=url, token=token, org=org, bucket=bucket)
            
            if client.connect():
                if client.test_connection():
                    QMessageBox.information(self, "测试成功", "InfluxDB连接测试成功！")
                else:
                    QMessageBox.warning(self, "测试失败", "InfluxDB连接失败，请检查配置")
                client.disconnect()
            else:
                QMessageBox.warning(self, "测试失败", "InfluxDB连接失败，请检查配置")
                
        except Exception as e:
            logger.error(f"测试InfluxDB连接时出错: {e}")
            QMessageBox.critical(self, "错误", f"测试连接时出错: {str(e)}")
    
    def get_config(self):
        """获取配置"""
        return {
            "url": self.url_input.text().strip(),
            "token": self.token_input.text().strip(),
            "org": self.org_input.text().strip(),
            "bucket": self.bucket_input.text().strip()
        }
