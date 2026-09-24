import time
import json
import datetime
import os
import urllib.parse
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
from .XiaoiSettingUI import XiaoiSettingsUI
from .CameraUI import CameraUI
from .RelaySettingUI import RelaySettingsUI
from camera.stream_manager import get_manager
from system_utils import check_run, AppisRunning, vname, logger, try_except, ups, gs, checkupdate, add_to_startup, remove_from_startup, check_startup, dpapi_protect, dpapi_unprotect
from .Floatingwin_old import *
from mqtt_client import MQTTClient
from heart_rate_logger import HeartRateLogger
from influxdb_writer import InfluxDBWriter
from push_notifier import NotifierManager
import push_notifier  # 模块引用: 设置on_push_recorded回调驱动推送记录表自动刷新
from webpush_server import WebPushServer, detect_tailscale_ip

# 主窗口类
class MainWindow(QMainWindow):
    updata_window_show_ = pyqtSignal(str, str, str, str)
    status_msg = pyqtSignal(str)  # 后台线程向主线程安全更新状态栏文案
    errorwinopen = pyqtSignal(str, bool, bool)
    push_history_changed = pyqtSignal()  # 推送记录落盘后通知UI刷新(可由后台线程触发)
    clip_ready = pyqtSignal(str)  # 报警剪辑成型(后台线程→主线程推WS clip_url)
    clips_ready = pyqtSignal(str)  # 全量相机剪辑JSON(方案1, tab切换; 后台线程→主线程)
    live_ready = pyqtSignal(str)  # 报警HLS实时流地址就绪(后台线程→主线程推WS alarm_live)
    hr_source_changed = pyqtSignal(dict)  # 中继数据源状态变化(hub线程→主线程推WS)
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
        self._pending_watch_state = None   # 设备状态防抖: 已安排确认的待定状态
        self.notifier = NotifierManager()  # 多渠道手机推送通知器(MeoW/Bark/ntfy)
        self.notifier.on_alarm_hook = self._on_remote_alarm  # 远程报警: WS推给安卓接收端响铃
        # 摄像头移动侦测推送桥: 侦测watcher经motion_watch模块级引用调用通知器
        from camera import motion_watch as _motion_watch
        _motion_watch.set_notifier(self.notifier)
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

    def _make_page(self, content, is_layout=False, fill=False, scroll=True):
        """统一页面构造: 统一边距/间距 + 底部弹性留白 + 滚动区包装(小屏/超高内容出滚动条)
        fill=True 时内容撑满整页(无底部弹性留白), 用于心率监测等需要随窗口拉伸的页面;
        scroll=False 时不包外层滚动区(页面内各区域自带内部滚动, 如推送记录表格)"""
        page = QWidget()
        lay = QVBoxLayout(page)
        page_layout(lay)
        if is_layout:
            lay.addLayout(content)
        else:
            lay.addWidget(content)
        if not fill:
            lay.addStretch()
        return wrap_scroll(page) if scroll else page

    def setup_ui(self):

        self.auto_FixedSize()

        # 设置窗口图标
        self.setWindowIcon(get_icon())

        # 状态栏
        self.status_label = QLabel("准备就绪")
        self.status_label.setAlignment(Qt.AlignCenter)
        # 底部状态栏加高、字号略大, 长内容(如"心率数据服务 (host:port)")显示更清楚
        self.status_label.setStyleSheet("font-size: 11pt;")
        self.status_label.setMinimumHeight(36)

        # 主布局
        main_widget = QWidget()
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 2)
        main_layout.setSpacing(2)
        main_widget.setLayout(main_layout)
        self.setCentralWidget(main_widget)

        # 创建主选项卡
        main_tab_widget = QTabWidget()
        # TAB标签块加大(字号保持默认): 加高加宽点击区, 标签之间留间距
        main_tab_widget.setStyleSheet(
            "QTabBar::tab { padding: 8px 18px; margin-right: 6px; }")
        main_layout.addWidget(main_tab_widget, 2)

        # 创建设备管理页面(已并入"基本设置"页上部)
        self.device_ui = DeviceConnectionUI(self.status_label)

        # 创建设置页面 (浮动窗口设置已并入基本设置页)
        self.settings_ui = AppSettingsUI()
        self.float_ui = self.settings_ui.float_settings  # 兼容原有引用(信号连接/浮窗更新)

        # 合并页: 上部设备管理(设备管理/连接设置分组) + 下部软件设置
        merged_widget = QWidget()
        merged_lay = QVBoxLayout(merged_widget)
        page_layout(merged_lay)
        merged_lay.addLayout(self.device_ui)
        merged_lay.addWidget(self.settings_ui)
        merged_lay.addStretch()
        settings_page = wrap_scroll(merged_widget)

        # 创建心率监测页面(左右分栏: 左日志+波形, 右推送记录; 撑满整页随窗口拉伸,
        # 不包外层滚动区——滚动只发生在日志文本框与推送记录表格内部)
        hr_page = self._make_page(self.device_ui.monitor_ui, fill=True, scroll=False)

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

        # 创建摄像头页面(配置 + 实时画面; 报警缓冲在load_settings中即启动, 与页面是否显示无关)
        self.camera_ui = CameraUI()
        camera_page = self._make_page(self.camera_ui)

        # 创建ESP32中继设置页面(勾选启用才启动 relay_hub 服务, 见 RelaySettingUI)
        self.relay_ui = RelaySettingsUI()
        relay_page = self._make_page(self.relay_ui)

        # 创建推送记录组件: 已并入心率监测页右列(由 HeartRateMonitorUI 创建)
        self.push_history_ui = self.device_ui.monitor_ui.push_history_ui

        # 将页面添加到主选项卡(摄像头页放最右: 低频配置页)
        main_tab_widget.addTab(hr_page, "心率监测")
        main_tab_widget.addTab(settings_page, "基本设置")
        main_tab_widget.addTab(mqtt_page, "MQTT设置")
        main_tab_widget.addTab(influxdb_page, "InfluxDB设置")
        main_tab_widget.addTab(tailscale_page, "Tailscale")
        main_tab_widget.addTab(push_page, "消息推送")
        main_tab_widget.addTab(xiaoi_page, "小爱音箱")
        main_tab_widget.addTab(camera_page, "摄像头")
        main_tab_widget.addTab(relay_page, "ESP32中继")

        # 添加状态栏
        main_layout.addWidget(self.status_label)

    def setup_connections(self):
        # 连接各模块之间的信号和槽
        self.status_msg.connect(self.status_label.setText)
        self.device_ui.heart_rate_updated.connect(self.float_ui.update_heart_rate)
        self.device_ui.heart_rate_updated.connect(self.on_heart_rate_updated)
        self.device_ui.reconnect_status.connect(self.on_reconnect_status)
        self.device_ui.reconnect_abandoned.connect(self._on_reconnect_abandoned)
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
        # 推送记录落盘后自动刷新心率监测页右列的推送记录表
        # (推送可能在后台线程发生, 经信号队列化切回主线程执行refresh)
        push_notifier.on_push_recorded = self.push_history_changed.emit
        self.push_history_changed.connect(self.push_history_ui.refresh)
        # 报警时摄像头剪辑: 异步剪辑联动摄像头的报警前10秒, 完成后回填推送记录(可点击回放)
        push_notifier.on_camera_alarm_hook = self._on_camera_alarm
        self.clip_ready.connect(self._push_clip_url)  # 剪辑成型→WS推clip_url给接收端
        self.clips_ready.connect(self._push_clips)  # 方案1: 全量剪辑JSON→WS(clips字段, tab切换)
        self.live_ready.connect(self._push_live_url)  # HLS实时流就绪→WS推alarm_live给接收端
        # ESP32中继数据源状态: hub线程emit→主线程槽→WS广播(接收端通知栏显示数据源)
        self.hr_source_changed.connect(self._push_hr_source)
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

        # 询问阶段不动摄像头: 用户点"No"取消退出时, 报警录制/移动侦测须继续工作
        if self.device_ui.ble_monitor.client and self.device_ui.ble_monitor.client.is_connected:
            reply = QMessageBox.question(
                self, '确认',
                "当前已连接设备，确定要退出吗?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)

            if reply == QMessageBox.Yes:
                self.camera_ui.shutdown()  # 确认退出后才停摄像头流
                self.close_application()
                e_accept()
            else:
                e_ignore()  # 取消退出: 直接返回, 不产生任何副作用
        else:
            self.camera_ui.shutdown()  # 无设备连接免确认, 同样到实际退出时才停流
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

        # 主动断开BLE设备(asyncSlot协程需事件循环运转才执行, 原QThread.msleep(500)
        # 阻塞的恰是该循环, 断开帧根本没发出) — 发起后泵事件等待完成, 上限2秒
        try:
            ble = self.device_ui.ble_monitor
            if ble and getattr(ble, 'client', None):
                self.device_ui._disconnect_done = False
                self.device_ui.disconnect_device()  # asyncSlot: 调度到qasync循环
                _wait0 = time.time()
                while not self.device_ui._disconnect_done and time.time() - _wait0 < 2.0:
                    QApplication.processEvents()  # 泵事件驱动qasync执行断开协程
                    QThread.msleep(20)
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
            # 断流心跳: 把设备层的每秒-1状态信号转成WS心跳推给接收端
            # (hr=0 + status=disconnected) —— 手机波形据此每秒滚动一格断点(示波器式),
            # 同时证明EXE在线, 手机不会误报"EXE超时"; EXE↔手机断连时由手机本地兜底
            if self.web_server:
                self.web_server.update(
                    0, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "disconnected")
            # 心律不齐检测器需收到断流值以清空窗口(防断连前旧数据跨重连拼接误判)
            self.notifier.check_heart_rate(heart_rate)
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
            # 心率真实到达且重连未激活 → 数据链路已健康, 清除智能重连终态残留文本
            # (如"智能重连已停止": 重连失败停止后蓝牙自行恢复/手动连接成功时,
            #  原清除路径watch_device_connection的边沿检测可能落空, APP端会长期
            #  挂着过时提示误导用户以为数据不可靠; 重连激活期间的提示由重连流程维护)
            if not self.device_ui.reconnect_active and self.web_server._state.get("info"):
                self.web_server.update_info("")
            self.web_server.update(heart_rate, timestamp, "connected")
        # MQTT发送由 send_mqtt_update 定时器统一处理, 避免与定时器重复发送
    
    def on_reconnect_status(self, text):
        """智能重连状态中继: 同步到Tailscale数据服务info字段(安卓端波形上方显示)"""
        if self.web_server:
            self.web_server.update_info(text)

    def _on_reconnect_abandoned(self, devname):
        """智能重连多轮未果放弃(主线程): 推送提醒用户监护已中断(不走设备状态冷却)"""
        try:
            self.notifier.notify_reconnect_abandoned(devname)
        except Exception as e:
            logger.error(f"重连放弃推送提醒失败: {e}")

    def _on_remote_alarm(self, seconds):
        """远程报警钩子(notifier心率类告警触发, Qt主线程): WS快照alarm=true推给接收端响铃
        报警视频联动: 按"当前持有手环的节点=所在房间"查绑定摄像头, 接收端报警面板显示该房间画面"""
        if self.web_server:
            cam, room = self._alarm_room_camera()
            self.web_server.trigger_alarm(seconds, cam_name=cam, room=room)

    def _alarm_room_camera(self):
        """解析报警房间与绑定摄像头: 中继数据源(relay_hub.source_status)得到房间名,
        查config.ini [esp32_relay] room_camera_map(房间/PC→摄像头名)绑定;
        中继未启用/无数据源/未绑定返回空串(接收端回退默认摄像头)"""
        try:
            import relay_hub
            room = (relay_hub.get_hub().source_status() or {}).get("source") or ""
        except Exception:
            room = ""
        if not room:
            return "", ""
        try:
            raw = gs("esp32_relay", "room_camera_map", "", str, "-RelayHub报警视频联动")
            mapping = json.loads(raw) if raw else {}
            cam = str(mapping.get(room) or "")
        except Exception as e:
            logger.error(f"报警房间摄像头绑定解析失败: {e}")
            cam = ""
        return cam, room

    def _on_camera_alarm(self, alarm_ts):
        """报警摄像头剪辑钩子(后台线程): 异步剪辑全部联动摄像头的报警前10秒,
        完成后把片段写入推送记录供回放(剪辑耗时所以不放主线程)"""
        def worker():
            try:
                clips = get_manager().cut_clips_for_alarm()
                if clips:
                    push_notifier.set_alarm_clips(alarm_ts, clips)
                    self._emit_default_clip_url(clips)
            except Exception as e:
                logger.error(f"报警摄像头剪辑失败: {e}")
            finally:
                # 报警剪辑落盘后按用户策略自动清理历史剪辑(默认不清理)
                try:
                    from camera.stream_manager import cleanup_captures
                    cleanup_captures(gs("Camera", "cleanup_mode", 0, int),
                                     gs("Camera", "cleanup_value", 30, int),
                                     gs("Camera", "cleanup_value", 30, int))
                except Exception as e:
                    logger.warning(f"历史剪辑清理失败: {e}")
        threading.Thread(target=worker, daemon=True, name="alarm-camera-cut").start()

    def _emit_default_clip_url(self, clips):
        """期3: 默认摄像头剪辑(无则第一条)拼流式URL, 连同全量剪辑列表一并推WS
        (clip_url保持单默认地址兼容旧接收端; clips列表供新接收端视频面板tab切换)"""
        try:
            if not (self.web_server and self.web_server.running):
                return
            base = self.web_server.base_url()
            want = get_manager().default_name()
            pick = next((c for c in clips if c.get("cam") == want), clips[0])
            url = f"{base}/camera/clip?name={urllib.parse.quote(os.path.basename(pick['mp4']))}"
            clip_list = [{"cam": c.get("cam", "监控"),
                          "url": f"{base}/camera/clip?name={urllib.parse.quote(os.path.basename(c['mp4']))}"}
                         for c in clips]
            self.clip_ready.emit(url)
            self.clips_ready.emit(json.dumps(clip_list, ensure_ascii=False))
        except Exception as e:
            logger.error(f"clip_url 推送失败: {e}")

    def _push_clip_url(self, url):
        """clip_ready信号槽(主线程): clip_url并入WS状态广播给接收端"""
        if self.web_server and self.web_server.running:
            self.web_server.push_clip(url, clips=None)

    def _push_clips(self, clips_json):
        """clips_ready信号槽(主线程): 全量剪辑列表并入WS状态广播(方案1 tab切换)"""
        if not (self.web_server and self.web_server.running):
            return
        try:
            clips = json.loads(clips_json)
            self.web_server.push_clip(self.web_server._state.get("clip_url", ""), clips=clips)
        except Exception as e:
            logger.warning(f"clips 推送失败: {e}")

    def _push_hr_source(self, status):
        """hr_source_changed信号槽(主线程): 数据源状态并入WS状态广播给接收端"""
        if self.web_server and self.web_server.running:
            self.web_server.push_source(status)

    def _on_alarm_cam_start(self):
        """报警开始回调(主线程): 启动绑定房间摄像头快照流(兜底) + HLS实时流(主画面)
        (摄像头名取自web_server状态alarm_cam, 无绑定/绑定无效回退默认摄像头;
        报警剪辑只经push_notifier._push_all的on_camera_alarm_hook触发, 携带能与推送
        记录精确匹配的alarm_ts; 此处再触发会重复剪辑且回填永不命中, 已移除)"""
        try:
            cam = self.web_server._state.get("alarm_cam", "") if self.web_server else ""
            get_manager().snapshot_start_for(cam)
            # HLS实时流: RTSP连接+首分片需1~4秒, 放后台线程等就绪后经信号推WS
            # (接收端响铃不等待: alarm=true先广播, 流就绪后alarm_live跟随)
            threading.Thread(target=self._hls_start_worker, args=(cam,), daemon=True,
                             name="alarm-hls-start").start()
        except Exception as e:
            logger.error(f"报警快照流启动失败: {e}")

    def _hls_start_worker(self, cam):
        """报警HLS流启动(后台线程): 就绪后经live_ready信号回主线程推WS
        绑定无效时回退默认摄像头(与快照流行为一致)"""
        try:
            from camera.hls_stream import get_hls_manager, seg_base_url, live_url_for
            mgr = get_manager()
            stream = mgr.get(cam) if cam else None
            name = cam if cam else ""
            if stream is None:
                stream = mgr.default_stream()
                name = mgr.default_name()
            if stream is None or not name:
                return  # 无摄像头配置, 接收端走快照兜底(同样无画面)
            s = get_hls_manager().start_for(name, stream, seg_base_url(name))
            if s is not None:
                self.live_ready.emit(live_url_for(self.web_server.host,
                                                  self.web_server.port, name))
        except Exception as e:
            logger.error(f"报警HLS流启动失败: {e}")

    def _push_live_url(self, url):
        """live_ready信号槽(主线程): HLS实时流地址并入WS状态广播给接收端"""
        if self.web_server and self.web_server.running:
            self.web_server.set_live_url(url)

    def _on_alarm_cam_end(self):
        """报警结束回调: 停全部快照流与HLS实时流(空闲自停兜底)"""
        try:
            get_manager().snapshot_stop_all()
        except Exception as e:
            logger.warning(f"报警快照流停止异常: {e}")
        try:
            from camera.hls_stream import get_hls_manager
            get_hls_manager().stop_all()
            if self.web_server and self.web_server.running:
                self.web_server.set_live_url("")
        except Exception as e:
            logger.warning(f"报警HLS流停止异常: {e}")

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
                logger.debug(f"定时发送心率数据到InfluxDB: {heart_rate}, 设备连接状态: {device_connected}")
                self.influxdb_client.write_heart_rate(heart_rate, timestamp)
            else:
                # 设备未连接
                logger.debug("设备未连接，跳过InfluxDB发送")
    
    def on_push_settings_changed(self, config):
        """消息推送设置改变时重新加载通知器配置"""
        self.notifier.load_config()
        logger.info("推送配置已更新")

    def watch_device_connection(self):
        """设备连接状态监视: 状态变化防抖8秒确认后推送断连/恢复通知
        bleak连接过程中WinRT的is_connected会瞬时翻转(服务发现前短暂Connected,
        失败时回Disconnected), 不防抖会在连接反复失败时造成恢复/断开通知轰炸;
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
        if (connected != self._last_watch_connected
                and connected != self._pending_watch_state):
            # 安排防抖确认(同一待定状态只安排一次, 8秒后仍为新状态才认定翻转)
            self._pending_watch_state = connected
            QTimer.singleShot(8000, lambda: self._confirm_device_state(connected))

    def _confirm_device_state(self, state):
        """防抖到期确认: 状态仍为待定值且与基线不同才推送并更新基线"""
        current = None
        try:
            if self.device_ui.ble_monitor and self.device_ui.ble_monitor.client:
                current = self.device_ui.ble_monitor.client.is_connected
        except Exception:
            current = False
        self._pending_watch_state = None
        if current != state or current == self._last_watch_connected:
            return  # 8秒内又翻转(仍连接中)或已回基线: 不推送, 等下轮轮询
        devname = ""
        try:
            dev = gs("Device", "last_selected_device", None, json.loads, "-MeOW设备名")
            devname = dev["name"] if dev else ""
        except Exception:
            pass
        if current is False:
            self.notifier.notify_device_lost(devname)
        elif current is True:
            self.notifier.notify_device_back(devname)
            # 设备恢复: 清除APP端智能重连状态显示
            if self.web_server:
                self.web_server.update_info("")
        # Tailscale数据服务: 设备状态变化时推送断连/恢复状态
        if self.web_server:
            self.web_server.update(
                self.last_heart_rate if current else 0,
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "connected" if current else "disconnected")
        self._last_watch_connected = current

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
            # 期3 报警视频联动: 快照接口提供画面 + 报警起止启停快照流
            self.web_server.set_camera_manager(get_manager())
            self.web_server.on_alarm_start = self._on_alarm_cam_start
            self.web_server.on_alarm_end = self._on_alarm_cam_end
            # ESP32中继仲裁钩子: 报警期间冻结节点切换(数据中断接管不受限)
            try:
                import relay_hub
                relay_hub.set_alarm_check(lambda: bool(
                    self.web_server and self.web_server._state.get("alarm")))
                # 数据源状态变化: hub线程经Qt信号中转回GUI线程后广播
                relay_hub.set_source_callback(self.hr_source_changed.emit)
            except Exception:
                pass
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
                srv = self.web_server
                srv.stop()  # 调度asyncio清理协程(不阻塞)
                # 泵事件等待清理协程完成(上限2秒): 退出路径随后即quit, 不等待则协程永不执行
                _wait0 = time.time()
                while not srv.stop_done and time.time() - _wait0 < 2.0:
                    QApplication.processEvents()
                    QThread.msleep(20)
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
