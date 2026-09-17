import time
import json
import datetime
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QWidget,
    QMessageBox, QGroupBox,
    QSystemTrayIcon, QMenu, QTabWidget, QProgressDialog)
from PyQt5.QtCore import Qt, pyqtSignal, QTimer, QThread

import threading

from .DevCtrl import *
from .basicwidgets import *
from .basicwidgets import page_layout, group_layout, hint_label, button_row, wrap_scroll, GLOBAL_QSS
from .heartratepng import *
from .UpDownloadwin import UpdWindow as DownloadWindow
from .MQTTSettingUI import MQTTSettingsUI
from .InfluxDBSettingUI import InfluxDBSettingsUI
from .TailscaleSettingUI import TailscaleSettingsUI
from .PushSettingUI import PushSettingsUI
from .PushHistoryUI import PushHistoryUI
from .XiaoiSettingUI import XiaoiSettingsUI
from system_utils import check_run, AppisRunning, vname, logger, try_except, ups, gs, checkupdate, add_to_startup, remove_from_startup, check_startup, dpapi_protect, dpapi_unprotect
from .Floatingwin_old import *
from mqtt_client import MQTTClient
from heart_rate_logger import HeartRateLogger
from influxdb_writer import InfluxDBWriter
from push_notifier import NotifierManager
from webpush_server import WebPushServer, detect_tailscale_ip

# 主窗口类
class MainWindow(QMainWindow):
    updata_window_show_ = pyqtSignal(str, str, str, str)
    status_msg = pyqtSignal(str)  # 后台线程向主线程安全更新状态栏文案
    errorwinopen = pyqtSignal(str, bool, bool)
    iserror = False
    @try_except("主窗口初始化")
    def __init__(self):
        super().__init__()
        self.version = vname
        self.cupd = 0
        self.cupdtime = 0.0
        
        # 保存最新心率值
        self.last_heart_rate = 0
        self.mqtt_timer = None
        self.influxdb_timer = None  # InfluxDB定时器
        self._mqtt_disconnected_sent = False  # 设备断开状态是否已发送到MQTT
        self._minimize_hint_shown = False  # 是否已提示过"最小化到托盘"
        self._last_watch_connected = None  # 设备状态监视: 上次连接状态(None=未初始化)
        self.notifier = NotifierManager()  # 多渠道手机推送通知器(MeoW/Bark/ntfy)
        self.web_server = None  # Tailscale心率数据服务(WS推送+HTTP轮询)

        self.errorwinopen.connect(self.errorwin)

        # 初始化MQTT客户端
        self.mqtt_client = MQTTClient()
        
        # 初始化InfluxDB客户端
        self.influxdb_client = InfluxDBWriter()
        
        # 初始化心率日志记录器
        # 心率日志: 缓冲落盘间隔可配置 (config.ini -> [Logger] buffer_minutes, 单位分钟, 默认5)
        try:
            _buffer_minutes = float(gs("Logger", "buffer_minutes", 5, float))
        except Exception:
            _buffer_minutes = 5.0
        self.heart_rate_logger = HeartRateLogger(buffer_seconds=_buffer_minutes * 60)

        self.setup_ui()
        self.setup_connections()
        
        self.updata_window_show_.connect(self.updata_window_show)

        # 启动后台线程检查更新
        if self.settings_ui._get_set("update_check", False, bool):
            self.start_update_check()
        
        try:
            check_run()
        except AppisRunning as e:
            self.verylarge_error("程序已经在运行了!!!")
            import sys
            sys.exit(1)
        
        self.settings_ui.check_startup()
        
        # 如果MQTT设置为启用，则连接MQTT服务器
        if gs('MQTT', 'enabled', False, bool):
            self.connect_mqtt()
            self.start_mqtt_timer()
        
        # 如果InfluxDB设置为启用，则连接InfluxDB服务器
        if gs('InfluxDB', 'enabled', False, bool):
            self.connect_influxdb()
            self.start_influxdb_timer()

        # 如果Tailscale数据服务设置为启用，则启动WS/HTTP广播服务
        if gs('Tailscale', 'enabled', False, bool):
            self.start_tailscale_server()

    def auto_FixedSize(self):
        self.setWindowTitle(f"心率监测设置 -[{self.version}]")
        # 获取逻辑DPI
        sc = self.screen()
        x_ = sc.logicalDotsPerInchX()
        y_ = sc.logicalDotsPerInchY()
        def sfs(x_,y_):
            self.logical_dpix = x_
            self.logical_dpiy = y_
            logger.info(f"逻辑DPI: {self.logical_dpix}x{self.logical_dpiy}")
            x =  int(self.logical_dpix / 96 * 1200)  # 增加到1200以适应左右布局
            y = int(self.logical_dpiy / 96 * 750)    # 增加到750
            self.setFixedSize(x, y)
            # 应用字体大小 + 全局统一样式
            self.setStyleSheet("font-size: " + str(int(self.logical_dpiy / 96 * 12)) + "px;" + GLOBAL_QSS)

        if not hasattr(self, "logical_dpix") or not hasattr(self, "logical_dpiy"):
            sfs(x_,y_)
        elif self.logical_dpix != x_ or self.logical_dpiy != y_:
            sfs(x_,y_)

    def _make_page(self, content, is_layout=False):
        """统一页面构造: 统一边距/间距 + 底部弹性留白 + 滚动区包装(小屏/超高内容出滚动条)"""
        page = QWidget()
        lay = QVBoxLayout(page)
        page_layout(lay)
        if is_layout:
            lay.addLayout(content)
        else:
            lay.addWidget(content)
        lay.addStretch()
        return wrap_scroll(page)

    def setup_ui(self):

        self.auto_FixedSize()

        # 设置窗口图标
        self.setWindowIcon(get_icon())

        # 状态栏
        self.status_label = QLabel("准备就绪")
        self.status_label.setAlignment(Qt.AlignCenter)

        # 主布局
        main_widget = QWidget()
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 2)
        main_layout.setSpacing(2)
        main_widget.setLayout(main_layout)
        self.setCentralWidget(main_widget)

        # 创建主选项卡
        main_tab_widget = QTabWidget()
        main_layout.addWidget(main_tab_widget, 2)

        # 创建设备管理页面
        self.device_ui = DeviceConnectionUI(self.status_label)
        device_page = self._make_page(self.device_ui, is_layout=True)

        # 创建心率监测页面（心率数据与波形独立展示）
        hr_page = self._make_page(self.device_ui.monitor_ui)

        # 创建设置页面 (浮动窗口设置已并入基本设置页)
        self.settings_ui = AppSettingsUI()
        settings_page = self._make_page(self.settings_ui)
        self.float_ui = self.settings_ui.float_settings  # 兼容原有引用(信号连接/浮窗更新)

        # 创建MQTT设置页面
        self.mqtt_ui = MQTTSettingsUI()
        mqtt_page = self._make_page(self.mqtt_ui)

        # 创建InfluxDB设置页面
        self.influxdb_ui = InfluxDBSettingsUI()
        influxdb_page = self._make_page(self.influxdb_ui)

        # 创建Tailscale数据服务设置页面
        self.tailscale_ui = TailscaleSettingsUI()
        tailscale_page = self._make_page(self.tailscale_ui)

        # 创建消息推送设置页面
        self.push_ui = PushSettingsUI()
        push_page = self._make_page(self.push_ui)

        # 创建小爱音箱设置页面
        self.xiaoi_ui = XiaoiSettingsUI()
        xiaoi_page = self._make_page(self.xiaoi_ui)

        # 创建推送记录页面
        self.push_history_ui = PushHistoryUI()
        push_history_page = self._make_page(self.push_history_ui)

        # 将页面添加到主选项卡
        main_tab_widget.addTab(hr_page, "心率监测")
        main_tab_widget.addTab(device_page, "设备管理")
        main_tab_widget.addTab(settings_page, "基本设置")
        main_tab_widget.addTab(mqtt_page, "MQTT设置")
        main_tab_widget.addTab(influxdb_page, "InfluxDB设置")
        main_tab_widget.addTab(tailscale_page, "Tailscale")
        main_tab_widget.addTab(push_page, "消息推送")
        main_tab_widget.addTab(xiaoi_page, "小爱音箱")
        main_tab_widget.addTab(push_history_page, "推送记录")

        # 添加状态栏
        main_layout.addWidget(self.status_label)

    def setup_connections(self):
        # 连接各模块之间的信号和槽
        self.status_msg.connect(self.status_label.setText)
        self.device_ui.heart_rate_updated.connect(self.float_ui.update_heart_rate)
        self.device_ui.heart_rate_updated.connect(self.on_heart_rate_updated)
        self.device_ui.status_changed.connect(self.status_label.setText)
        self.device_ui.upd_lastST.connect(self.settings_ui.change_devname)
        self.device_ui.set_act_Devstatus.connect(self.settings_ui.dev_status)
        self.settings_ui.quit_application.connect(self.check_device_status_before_close)
        self.settings_ui.show_settings.connect(self.show_window)
        self.settings_ui.updsig.connect(self.start_update_check)
        self.mqtt_ui.mqtt_settings_changed.connect(self.on_mqtt_settings_changed)
        self.influxdb_ui.influxdb_settings_changed.connect(self.on_influxdb_settings_changed)
        self.tailscale_ui.tailscale_settings_changed.connect(self.on_tailscale_settings_changed)
        self.push_ui.push_settings_changed.connect(self.on_push_settings_changed)
        self.xiaoi_ui.xiaoi_settings_changed.connect(self.on_push_settings_changed)
        # 设备连接状态监视定时器(2秒): 状态变化时触发推送断连/恢复通知
        self.device_watch_timer = QTimer(self)
        self.device_watch_timer.timeout.connect(self.watch_device_connection)
        self.device_watch_timer.start(2000)
        def act_HR_clicked():
            if self.settings_ui.devstautus == "连接":
                self.device_ui.connect_device()
            else:
                self.device_ui.disconnect_device()
        self.settings_ui.act_HR_clicked.connect(act_HR_clicked)

    def show_window(self):
        """显示设置窗口"""
        self.show()
        self.activateWindow()

    def closeEvent(self, a0):
        """窗口关闭事件: 点击右上角X时最小化到系统托盘, 不退出程序;
        真正退出只能通过托盘菜单的"退出程序"(quit_application -> check_device_status_before_close)"""
        a0.ignore()
        self.hide()
        if hasattr(self, 'tray_icon'):
            self.tray_icon.show()
            if not self._minimize_hint_shown:
                self._minimize_hint_shown = True
                self.tray_icon.showMessage(
                    "HRMLink",
                    "程序已最小化到系统托盘, 点击托盘图标可打开菜单, 选择\"退出程序\"才会真正退出",
                    QSystemTrayIcon.Information, 3000)

    # 检查设备连接状态后执行退出逻辑
    def check_device_status_before_close(self, event = None):
        if event:
            def e_accept():event.accept()
            def e_ignore():event.ignore()
        else:
            e_accept = e_ignore = lambda: None

        # 否则正常退出
        if self.device_ui.ble_monitor.client and self.device_ui.ble_monitor.client.is_connected:
            reply = QMessageBox.question(
                self, '确认',
                "当前已连接设备，确定要退出吗?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)

            if reply == QMessageBox.Yes:
                self.close_application()
                e_accept()
            else:
                e_ignore()
        else:
            self.close_application()
            e_accept()

    def errorwin(self, error_message: str, exit_ = True, setiserror = True):
        if not self.iserror:
            self.iserror = setiserror
            QMessageBox.critical(self, f"{"严重"if exit_ else""}错误", error_message, QMessageBox.Ok)
        if exit_:
            self.close_application()

    def verylarge_error(self, error_message: str, exit_ = True, setiserror = True):
        self.errorwinopen.emit(error_message, exit_, setiserror)

    def close_application(self):
        """执行退出程序的操作"""
        if self.settings_ui.tray_icon:
            self.settings_ui.tray_icon.hide()
        self.float_ui.floating_window.close()

        # 主动断开BLE设备(原实现退出不断开, 手环侧悬挂连接需等超时)
        try:
            ble = self.device_ui.ble_monitor
            if ble and getattr(ble, 'client', None):
                self.device_ui.disconnect_device()  # asyncSlot: 调度到qasync循环
                QThread.msleep(500)  # 留出BLE断开帧发送时间
        except Exception as e:
            logger.warning(f"退出时断开BLE设备失败: {e}")

        # 停止MQTT定时器
        self.stop_mqtt_timer()
        
        # 停止InfluxDB定时器
        self.stop_influxdb_timer()
        
        # 断开MQTT连接
        if self.mqtt_client:
            self.mqtt_client.disconnect()
        
        # 断开InfluxDB连接
        if self.influxdb_client:
            self.influxdb_client.disconnect()

        # 停止Tailscale数据服务
        self.stop_tailscale_server()
        
        # 关闭心率日志记录器
        if hasattr(self, 'heart_rate_logger'):
            self.heart_rate_logger.close()
            
        QApplication.quit()
        
    def connect_mqtt(self):
        """连接到MQTT服务器"""
        try:
            # 获取MQTT配置
            config = {
                "broker": gs('MQTT', 'broker', 'localhost', str),
                "port": gs('MQTT', 'port', 1883, int),
                "username": gs('MQTT', 'username', '', str),
                "password": dpapi_unprotect(gs('MQTT', 'password', '', str)),
                "topic": gs('MQTT', 'topic', 'homeassistant/sensor/heartrate/state', str),
                "discovery_topic": gs('MQTT', 'discovery_topic', 'homeassistant/sensor/heartrate/config', str),
                "discovery_enabled": gs('MQTT', 'discovery_enabled', True, bool),
                "client_id": f"heartbeat_monitor_{int(time.time())}"  # 添加时间戳确保ID唯一
            }
            
            # 连接MQTT (paho connect() 为同步TCP连接, 返回True即已建立; on_connect回调仅置标志位)
            success = self.mqtt_client.connect(config)

            # 短暂等待连接回调确认(最多0.5秒; 原实现3秒processEvents忙等阻塞UI)
            if success:
                for _ in range(10):
                    if self.mqtt_client.connected:
                        break
                    time.sleep(0.05)
            
            if self.mqtt_client.connected:
                self.status_label.setText("已连接到MQTT服务器")
                logger.info("已连接到MQTT服务器")
                return True
            else:
                self.status_label.setText("MQTT连接失败")
                logger.error("MQTT连接失败")
                return False
        except Exception as e:
            self.status_label.setText(f"MQTT连接错误: {str(e)}")
            logger.error(f"MQTT连接错误: {str(e)}")
            return False
    
    def on_mqtt_settings_changed(self, config):
        """MQTT设置改变时的处理"""
        # 如果MQTT已启用，则连接或重新连接
        if config["enabled"]:
            # 显示进度对话框
            progress = QProgressDialog("正在连接到MQTT服务器...", "取消", 0, 100, self)
            progress.setWindowTitle("连接中")
            progress.setWindowModality(Qt.WindowModal)
            progress.setAutoClose(True)
            progress.setValue(10)
            QApplication.processEvents()
            
            # 停止现有定时器
            self.stop_mqtt_timer()
            progress.setValue(30)
            QApplication.processEvents()
            
            # 断开现有连接
            if self.mqtt_client:
                self.mqtt_client.disconnect()
            progress.setValue(50)
            QApplication.processEvents()
            
            # 重新连接
            connected = self.connect_mqtt()
            progress.setValue(80)
            QApplication.processEvents()
            
            # 启动定时器
            if connected:
                self.start_mqtt_timer()
                progress.setValue(100)
                QApplication.processEvents()
            else:
                progress.close()
                QMessageBox.warning(self, "连接失败", "无法连接到MQTT服务器，请检查设置")
        else:
            # 停止定时器
            self.stop_mqtt_timer()
            
            # 断开连接
            if self.mqtt_client:
                self.mqtt_client.disconnect()
                self.status_label.setText("已断开MQTT连接")
    
    def on_heart_rate_updated(self, heart_rate):
        """心率数据更新时的处理"""
        # 过滤无效心率值: 设备未连接时 update_ui 每秒 emit(-1) 作为状态信号,
        # 波形图和浮窗均有过滤, 此处也需过滤, 避免把 -1 写入CSV/MQTT
        if heart_rate <= 0:
            return
        # 保存最新的心率值
        self.last_heart_rate = heart_rate
        
        # 记录心率数据到日志文件
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.heart_rate_logger.log_heart_rate(heart_rate, timestamp)
        # 消息推送: 检查心率异常告警(内部带持续判定+冷却期, 正常心率无任何请求)
        self.notifier.check_heart_rate(heart_rate)
        # Tailscale数据服务: 刷新快照并WS广播给安卓接收端
        if self.web_server:
            self.web_server.update(heart_rate, timestamp, "connected")
        # MQTT发送由 send_mqtt_update 定时器统一处理, 避免与定时器重复发送
    
    def start_mqtt_timer(self):
        """启动MQTT定时发送定时器"""
        if self.mqtt_timer is None:
            # 创建定时器，每1秒发送一次心率数据
            self.mqtt_timer = QTimer(self)
            self.mqtt_timer.timeout.connect(self.send_mqtt_update)
            self.mqtt_timer.start(1000)  # 1秒
            logger.info("已启动MQTT定时发送")
    
    def stop_mqtt_timer(self):
        """停止MQTT定时器"""
        if self.mqtt_timer:
            self.mqtt_timer.stop()
            self.mqtt_timer = None
            logger.info("已停止MQTT定时发送")
    
    def send_mqtt_update(self):
        """定时发送MQTT更新"""
        if not (self.mqtt_client and self.mqtt_client.connected):
            return
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]  # 包含毫秒
        
        # 检查设备连接状态
        device_connected = False
        if hasattr(self.device_ui, "ble_monitor") and self.device_ui.ble_monitor:
            device_connected = (self.device_ui.ble_monitor.client and 
                               self.device_ui.ble_monitor.client.is_connected)
        
        if device_connected and self.last_heart_rate > 0:
            # 设备已连接且有心率数据
            self._mqtt_disconnected_sent = False
            self.mqtt_client.publish_heart_rate(self.last_heart_rate, timestamp, "connected")
        elif not device_connected and not self._mqtt_disconnected_sent:
            # 设备未连接: 仅在状态变化时发送一次断开状态, 避免每秒发0刷屏
            self._mqtt_disconnected_sent = True
            logger.info("设备未连接，发送一次断开状态")
            self.mqtt_client.publish_heart_rate(0, timestamp, "disconnected")
    
    def connect_influxdb(self):
        """连接到InfluxDB服务器"""
        try:
            # 获取InfluxDB配置
            url = gs('InfluxDB', 'url', '', str)
            token = dpapi_unprotect(gs('InfluxDB', 'token', '', str))
            org = gs('InfluxDB', 'org', '', str)
            bucket = gs('InfluxDB', 'bucket', '', str)
            enabled = gs('InfluxDB', 'enabled', False, bool)
            
            logger.info(f"InfluxDB配置读取: enabled={enabled}, url={url}, org={org}, bucket={bucket}")
            
            # 验证必填项
            if not url or not token or not org or not bucket:
                logger.warning("InfluxDB配置不完整，跳过连接")
                return False
            
            # 创建InfluxDB客户端
            self.influxdb_client = InfluxDBWriter(url=url, token=token, org=org, bucket=bucket)
            
            # 连接
            if self.influxdb_client.connect():
                self.status_label.setText("已连接到InfluxDB服务器")
                logger.info("已连接到InfluxDB服务器")
                return True
            else:
                self.status_label.setText("InfluxDB连接失败")
                logger.error("InfluxDB连接失败")
                return False
                
        except Exception as e:
            self.status_label.setText(f"InfluxDB连接错误: {str(e)}")
            logger.error(f"InfluxDB连接错误: {str(e)}")
            return False
    
    def start_influxdb_timer(self):
        """启动InfluxDB定时发送定时器"""
        if self.influxdb_timer is None:
            # 创建定时器，每1秒发送一次心率数据
            self.influxdb_timer = QTimer(self)
            self.influxdb_timer.timeout.connect(self.send_influxdb_update)
            self.influxdb_timer.start(1000)  # 1秒
            logger.info("已启动InfluxDB定时发送")
    
    def stop_influxdb_timer(self):
        """停止InfluxDB定时器"""
        if self.influxdb_timer:
            self.influxdb_timer.stop()
            self.influxdb_timer = None
            logger.info("已停止InfluxDB定时发送")
    
    def send_influxdb_update(self):
        """定时发送InfluxDB更新"""
        if self.influxdb_client and self.influxdb_client.is_connected():
            timestamp = datetime.datetime.now()
            
            # 检查设备连接状态
            device_connected = False
            if hasattr(self.device_ui, "ble_monitor") and self.device_ui.ble_monitor:
                device_connected = (self.device_ui.ble_monitor.client and 
                                   self.device_ui.ble_monitor.client.is_connected)
            
            if device_connected:
                # 设备已连接，发送心率数据（即使心率值为0也发送）
                heart_rate = self.last_heart_rate
                logger.info(f"定时发送心率数据到InfluxDB: {heart_rate}, 设备连接状态: {device_connected}")
                self.influxdb_client.write_heart_rate(heart_rate, timestamp)
            else:
                # 设备未连接
                logger.debug("设备未连接，跳过InfluxDB发送")
    
    def on_push_settings_changed(self, config):
        """消息推送设置改变时重新加载通知器配置"""
        self.notifier.load_config()
        logger.info("推送配置已更新")

    def watch_device_connection(self):
        """设备连接状态监视: 状态变化时触发推送断连/恢复通知
        首次运行仅记录初始状态, 不推送"""
        connected = None
        try:
            if self.device_ui.ble_monitor and self.device_ui.ble_monitor.client:
                connected = self.device_ui.ble_monitor.client.is_connected
        except Exception:
            connected = False

        if self._last_watch_connected is None:
            self._last_watch_connected = connected
            return
        if connected != self._last_watch_connected:
            devname = ""
            try:
                dev = gs("Device", "last_selected_device", None, json.loads, "-MeOW设备名")
                devname = dev["name"] if dev else ""
            except Exception:
                pass
            if connected is False:
                self.notifier.notify_device_lost(devname)
            elif connected is True:
                self.notifier.notify_device_back(devname)
            # Tailscale数据服务: 设备状态变化时推送断连/恢复状态
            if self.web_server:
                self.web_server.update(
                    self.last_heart_rate if connected else 0,
                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "connected" if connected else "disconnected")
            self._last_watch_connected = connected

    def on_influxdb_settings_changed(self, config):
        """InfluxDB设置改变时的处理"""
        # 如果InfluxDB已启用，则连接或重新连接
        if config["enabled"]:
            # 显示进度对话框
            progress = QProgressDialog("正在连接到InfluxDB服务器...", "取消", 0, 100, self)
            progress.setWindowTitle("连接中")
            progress.setWindowModality(Qt.WindowModal)
            progress.setAutoClose(True)
            progress.setValue(10)
            QApplication.processEvents()
            
            # 停止现有定时器
            self.stop_influxdb_timer()
            progress.setValue(30)
            QApplication.processEvents()
            
            # 断开现有连接
            if self.influxdb_client:
                self.influxdb_client.disconnect()
            progress.setValue(50)
            QApplication.processEvents()
            
            # 重新连接
            connected = self.connect_influxdb()
            progress.setValue(80)
            QApplication.processEvents()
            
            # 启动定时器
            if connected:
                self.start_influxdb_timer()
                progress.setValue(100)
                QApplication.processEvents()
            else:
                progress.close()
                QMessageBox.warning(self, "连接失败", "无法连接到InfluxDB服务器，请检查设置")
        else:
            # 停止定时器
            self.stop_influxdb_timer()
            
            # 断开连接
            if self.influxdb_client:
                self.influxdb_client.disconnect()
                self.status_label.setText("已断开InfluxDB连接")

    def start_tailscale_server(self):
        """启动Tailscale心率数据服务(WS推送+HTTP轮询)"""
        try:
            address = (gs('Tailscale', 'address', 'auto', str) or 'auto').strip()
            port = gs('Tailscale', 'port', 8765, int)
            host = address
            if address.lower() == 'auto':
                detected = detect_tailscale_ip()
                if detected:
                    host = detected
                else:
                    host = '127.0.0.1'
                    logger.warning("未探测到Tailscale IP, 数据服务回退绑定 127.0.0.1(仅本机可访问)")
            # 设备名用于接收端显示
            devname = ""
            try:
                dev = gs("Device", "last_selected_device", None, json.loads, "-Tailscale设备名")
                devname = dev["name"] if dev else ""
            except Exception:
                pass
            self.web_server = WebPushServer(host, port, device_name=devname)
            self.web_server.start()
            state_text = f"服务启动中: ws://{host}:{port}/ws\n手机浏览器可直接访问 http://{host}:{port}"
            self.tailscale_ui.set_server_state(state_text, running=True)
            self.status_label.setText(f"心率数据服务 ({host}:{port})")
        except Exception as e:
            logger.error(f"心率数据服务启动失败: {e}")
            self.tailscale_ui.set_server_state(f"服务启动失败: {e}", running=False)

    def stop_tailscale_server(self):
        """停止Tailscale心率数据服务"""
        if self.web_server:
            try:
                self.web_server.stop()
            except Exception as e:
                logger.warning(f"停止心率数据服务异常: {e}")
            self.web_server = None

    def on_tailscale_settings_changed(self, config):
        """Tailscale数据服务设置改变: 按新配置重启/停止服务"""
        self.stop_tailscale_server()
        if config.get("enabled"):
            self.start_tailscale_server()
        else:
            self.tailscale_ui.set_server_state("服务未运行", running=False)
            self.status_label.setText("已停止心率数据服务")

    def start_update_check(self):
        """启动后台线程进行自动更新检查"""
        # 添加线程管理，防止重复创建
        if hasattr(self, '_update_check_thread') and self._update_check_thread and self._update_check_thread.is_alive():
            logger.info("更新检查线程已在运行，跳过")
            return
        
        def update_check_thread():
            self.status_msg.emit("正在检查更新...")
            # 检查更新
            update_available, index, vname, gxjs, down_url = checkupdate()
            if update_available:
                # 使用信号机制将结果显示到主线程
                self.updata_window_show_.emit(index, vname, gxjs, down_url)
            else:
                if index == "":
                    self.status_msg.emit("当前已是最新版本")
                elif index == "时限禁用":
                    print(f"{self.cupd} {self.cupdtime}")
                    if time.time() - self.cupdtime > 15:
                        self.cupd = 0
                    self.cupdtime = time.time()
                    self.cupd += 1
                    if self.cupd <= 3:
                        self.status_msg.emit("刚刚已经检查过更新了")
                    elif self.cupd <= 20:
                        self.status_msg.emit("刚刚已经检查过更新了喵~")
                    elif self.cupd <= 30:
                        self.status_msg.emit("不要再点了喵~~")
                    elif self.cupd <= 35:
                        self.status_msg.emit(f"再点我要罢工了喵({self.cupd-30}/5)")
                    elif self.cupd <= 36:
                        self.status_msg.emit("哈! 我没有开玩笑喵!!!!")
                    elif self.cupd <= 37:
                        logger.error("频繁点击更新让猫猫生气了")
                        self.verylarge_error("频繁点击更新让猫猫生气了, 再按猫猫要把进程吃掉了喵", False, False)
                        self.verylarge_error("拦截了一个奇怪的错误", False, False)
                    else:
                        logger.error("疑似进程被吃了, 程序退出")
                        self.verylarge_error("嘎嘣一响, 程序崩溃了<(> w <)>")
                else:
                    self.status_msg.emit("更新检查失败")

        # 创建并启动线程，保存引用以便管理
        self._update_check_thread = threading.Thread(target=update_check_thread, daemon=True)
        self._update_check_thread.start()

    def updata_window_show(self, index, vname, gxjs, down_url):
        self.updmsg_box = QMessageBox(self)
        logger.debug(f"开启了更新提示窗口(-1/-2)")
        self.updmsg_box.setWindowTitle('提示')
        self.updmsg_box.setText(f'版本-{vname} 已更新:\n {gxjs}')
        self.updmsg_box.addButton("查看新版本", QMessageBox.YesRole)
        btn_no = self.updmsg_box.addButton("取消", QMessageBox.NoRole)
        self.updmsg_box.setDefaultButton(btn_no)
        logger.debug(f"窗口正常加载 (-1)")
        reply = self.updmsg_box.exec()
        logger.debug(f"reply: {reply} (-2)")
        if reply == 0:
            self.updwin = DownloadWindow(self)
            self.updwin.set_url(down_url,index)
            self.updwin.show()

# 应用设置UI类
class AppSettingsUI(QGroupBox):
    quit_application = pyqtSignal()
    show_settings = pyqtSignal()
    updsig = pyqtSignal()
    act_HR_clicked = pyqtSignal()

    @try_except("设置UI初始化")
    def __init__(self):
        super().__init__()
        self.devstautus = "连接"
        self.setup_ui()
        self.setup_tray_icon()
    
    def check_startup(self):
        Csup1, Csup2 = check_startup()
        print(Csup1, end=", ")
        print(Csup2)
        if Csup2 != "":
            logger.debug("[GUI] 检查到启动项")
            if Csup1: 
                self._up_set('startup', True)
                self.set_starup.setChecked(True)
            else:
                re = QMessageBox.warning(self, "提示", f"启动项被其它应用程序占用,是否覆盖?\n相关启动项位置: {Csup2}", QMessageBox.Yes | QMessageBox.No)
                if re == QMessageBox.Yes:
                    add_to_startup()
                    self._up_set('startup', True)
                    self.set_starup.setChecked(True)
                else:
                    self._up_set('startup', False)
                    self.set_starup.setChecked(False)
        else:
            logger.debug("[GUI] 未检查到启动项")
            self._up_set('startup', False)
            self.set_starup.setChecked(False)

    def setup_ui(self):
        self.app_icon = get_icon()
        self.setTitle("软件设置")
        settings_layout = group_layout(QVBoxLayout())

        # 三个开关合并为一行, 避免整页只有纵向4个控件的松散排布
        check_row = QHBoxLayout()
        CheackBox_(
             "允许后台运行"
            ,check_row
            ,self._get_set("use_bg", False, bool)
            ,self.toggle_use_bg
        )

        self.set_starup = CheackBox_(
             "开机自启动"
             ,check_row
             ,self._get_set("startup", False, bool)
             ,self.toggle_startup
        )

        CheackBox_(
             "启动时检查更新"
            ,check_row
            ,self._get_set("update_check", False, bool)
            ,lambda state: self._up_set("update_check", state==Qt.Checked)
        )
        check_row.addStretch()
        settings_layout.addLayout(check_row)

        # 添加手动检查更新按钮
        self.check_update_btn = QPushButton("检查更新")
        self.check_update_btn.clicked.connect(self.updsig.emit)
        settings_layout.addLayout(button_row(self.check_update_btn))

        # 内嵌浮动窗口设置分组 (原独立TAB页, 2026-09-17合并)
        self.float_settings = FloatingWindowSettingUI()
        settings_layout.addWidget(self.float_settings)

        self.setLayout(settings_layout)

    def setup_tray_icon(self):
        """设置系统托盘图标"""

        if QSystemTrayIcon.isSystemTrayAvailable():

            self.tray_icon = QSystemTrayIcon(self)
            self.tray_icon.setIcon(get_icon())
            self.tray_icon.setToolTip("HRMLink")

            self.trme = tray_menu = QMenu()

            # 添加菜单项
            show_settings_action = tray_menu.addAction("打开设置")
            show_settings_action.triggered.connect(self.show_settings.emit)

            tray_menu.addSeparator()

            quit_action = tray_menu.addAction("退出程序")
            quit_action.triggered.connect(self.quit_application.emit)

            self.tray_icon.setContextMenu(tray_menu)
            self.tray_icon.show()
            self.tray_icon.activated.connect(self.on_tray_icon_activated)

            dev = gs("Device","last_selected_device",None,json.loads,"-获取自动连接设备名称")
            self.devname = dev["name"] if dev is not None else None
            if dev is not None:
                self.act_HR = tray_menu.addAction(f"连接 {dev["name"]}")
                self.act_HR.triggered.connect(self.act_HR_clicked.emit)

    def on_tray_icon_activated(self, reason):
        """托盘图标点击事件"""
        if reason == QSystemTrayIcon.Trigger:  # 单击
            # 显示托盘菜单
            self.tray_icon.contextMenu().popup(self.tray_icon.geometry().center())

    def toggle_use_bg(self, state):
        """切换允许后台运行"""
        if state == Qt.Checked:
            self._up_set('use_bg', True)
        else:
            self._up_set('use_bg', False)
    
    def toggle_startup(self, state):
        """切换开机启动"""
        if state == Qt.Checked:
            output = add_to_startup()
            if output == "成功":
                self._up_set('startup', True)
            elif output == "脚本":
                QMessageBox.information(self, "提示", "测试启动脚本 start.bat 不通过, 请用文本编辑器打开并修改 PYTHONPATH 项为python目录", QMessageBox.Ok)
                self.set_starup.setChecked(False)
            elif output == "启动项":
                QMessageBox.information(self, "错误", "添加启动项失败", QMessageBox.Ok)
                self.set_starup.setChecked(False)
        else:
            remove_from_startup()
            self._up_set('startup', False)

    def dev_status(self, status):
        self.devstautus = status
        self.change_devname()

    def change_devname(self, devname=None):
        devname = devname or self.devname
        self.devname = devname
        if devname is None:
            return
        if not hasattr(self, "act_HR"):
            self.act_HR = self.trme.addAction(f"{self.devstautus} {devname}")
            self.act_HR.triggered.connect(self.act_HR_clicked.emit)
        else:
            self.act_HR.setText(f"{self.devstautus} {devname}")

    def _up_set(self, option: str, value):
        ups('GUI', option, value, debugn="GUI")

    def _get_set(self, option: str, default, type_ = None):
        return gs('GUI', option, default, type_ , debugn="GUI")
