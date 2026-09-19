from PyQt5.QtWidgets import (QVBoxLayout, QLabel, QWidget
    ,QGroupBox, QHBoxLayout, QPushButton, QCheckBox, QListWidget
    ,QSpinBox, QTextEdit, QMessageBox,  QFileDialog, QListWidgetItem)
from PyQt5.QtCore import pyqtSignal, QTimer, Qt, QEvent

from bleak.exc import BleakDeviceNotFoundError, BleakError
from .basicwidgets import CheackBox_, group_layout
from system_utils import try_except, ups, gs
import logging
logger = logging.getLogger('__main__')
from Blegetheartbeat import BLEHeartRateMonitor

import json
import asyncio
import datetime

from qasync import asyncSlot

__all__ = ["DeviceConnectionUI", "HeartRateMonitorUI"]

class HeartRateMonitorUI(QWidget):
    """心率数据与波形独立页面（"心率监测"标签页内容）"""

    def __init__(self, ble_monitor, parent=None):
        super().__init__(parent)
        self.ble_monitor = ble_monitor
        # 限制心率显示文本的最大行数，防止内存溢出（最多保留500行）
        self.max_display_lines = 500
        self._init_ui()

    def _init_ui(self):
        # 左右分栏: 左列(波形+日志+保存按钮) 约60%, 右列(推送记录, 含底部刷新/清空) 约40%
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)  # 页面级边距由主窗口统一包装提供
        layout.setSpacing(8)

        # === 左列：波形图(上) + 心率日志(下) + 保存按钮 ===
        data_group = QGroupBox("实时心率监测")
        left_layout = QVBoxLayout()
        # 内边距与右侧"推送记录"分组框统一, 保证两框内容起始位置和底部按钮行对齐
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.setSpacing(8)

        # 波形图
        from .HeartRateWaveform import HeartRateWaveform
        self.waveform = HeartRateWaveform(max_points=60)
        left_layout.addWidget(self.waveform, 65)  # 波形约占65%

        # 心率数据记录: 左侧缩进与波形图Y轴垂直对齐(边距随画布宽度动态同步)
        self.heart_rate_display = QTextEdit()
        self.heart_rate_display.setReadOnly(True)
        self.heart_rate_display.setMinimumWidth(250)  # 设置最小宽度
        self._log_row = QHBoxLayout()
        self._log_row.setContentsMargins(30, 0, 0, 0)  # 初始值, 显示后由eventFilter按Y轴位置校准
        self._log_row.addWidget(self.heart_rate_display)
        left_layout.addLayout(self._log_row, 35)  # 日志区约占35%

        # 数据保存按钮
        self.save_button = QPushButton("保存数据到文件")
        self.save_button.clicked.connect(self.save_data)
        left_layout.addWidget(self.save_button)

        data_group.setLayout(left_layout)
        layout.addWidget(data_group, 3)  # 左右约60:40

        # === 右列：推送记录(原"推送记录"标签页组件, 刷新/清空在组件底部) ===
        # 包一层"推送记录"分组框, 标题高度与左侧"实时心率监测"一致, 两栏内容起始位置对齐
        from .PushHistoryUI import PushHistoryUI
        self.push_history_ui = PushHistoryUI()
        push_group = QGroupBox("推送记录")
        push_group_lay = QVBoxLayout()
        # 内边距与左侧"实时心率监测"分组框统一(8px), 底部刷新/清空按钮行随之对齐
        push_group_lay.setContentsMargins(8, 8, 8, 8)
        push_group_lay.addWidget(self.push_history_ui)
        push_group.setLayout(push_group_lay)
        layout.addWidget(push_group, 2)

        # 监听波形画布尺寸变化, 校准日志框左边距与波形Y轴对齐
        self.waveform.canvas.installEventFilter(self)

    def eventFilter(self, obj, event):
        """波形画布尺寸变化时, 让心率日志框左边缘缩进到与波形Y轴同一垂直线"""
        if obj is self.waveform.canvas and event.type() == QEvent.Resize:
            indent = self.waveform.y_axis_left_px()
            if indent > 0:
                self._log_row.setContentsMargins(indent, 0, 0, 0)
        return super().eventFilter(obj, event)

    def append_heart_rate(self, timestamp, heart_rate):
        """心率数据更新：追加记录并刷新波形"""
        self.heart_rate_display.append(f"[{timestamp}] 心率: {heart_rate} BPM")
        # 限制文本显示的最大行数，防止内存溢出
        if self.heart_rate_display.document().blockCount() > self.max_display_lines:
            # 删除最旧的行
            cursor = self.heart_rate_display.textCursor()
            cursor.movePosition(cursor.Start)
            cursor.select(cursor.BlockUnderCursor)
            cursor.removeSelectedText()
        self.waveform.add_heart_rate(heart_rate)

    def save_data(self):
        """保存心率数据到文件"""
        if not self.ble_monitor.heart_rate_data:
            QMessageBox.warning(self, "警告", "没有可保存的数据")
            return

        filename, _ = QFileDialog.getSaveFileName(
            self, "保存心率数据", "", "CSV文件 (*.csv);;所有文件 (*)")

        if filename:
            try:
                with open(filename, 'w', encoding='utf-8') as f:
                    f.write("时间,心率(BPM)\n")
                    for timestamp, hr in self.ble_monitor.heart_rate_data:
                        f.write(f"{timestamp},{hr}\n")
                QMessageBox.information(self, "成功", "数据已保存")
            except Exception as e:
                QMessageBox.warning(self, "错误", f"保存失败: {str(e)}")
                logger.error(f"保存数据时出错: {str(e)}")

class DeviceConnectionUI(QVBoxLayout):
    heart_rate_updated = pyqtSignal(int)
    status_changed = pyqtSignal(str)
    upd_lastST = pyqtSignal(str)
    set_act_Devstatus = pyqtSignal(str)
    reconnect_status = pyqtSignal(str)  # 智能重连状态文本(中继到Tailscale数据服务info字段, 空串=清除)
    DEVICE_DATA_ROLE = Qt.UserRole + 1

    @property
    def heart_rate_display(self):
        """心率文本显示区（实际位于"心率监测"独立页面）"""
        return self.monitor_ui.heart_rate_display

    @property
    def waveform(self):
        """心率波形图（实际位于"心率监测"独立页面）"""
        return self.monitor_ui.waveform

    @try_except("设备链接界面初始化")
    def __init__(self, status_label):
        super().__init__()
        self.ble_monitor = BLEHeartRateMonitor()
        self.ble_monitor.heart_rate_callback = self.on_heart_rate_update
        self.status_label = status_label
        self.linking = False
        self.quit_ = False
        self.be_timeout = False
        self.usedevlist = True
        self.auto_connect_now = True
        self.selected_device = self._get_set("last_selected_device", None, json.loads)
        self.auto_connect = self._get_set("auto_connect", False, bool)
        self.auto_reconnect = self._get_set("auto_reconnect", True, bool)  # 默认启用自动重连
        self.last_connected_device = None  # 记录最后连接的设备

        # 智能重连机制参数
        self.reconnect_delay = 30  # 重连延迟30秒
        self.reconnect_count = 0  # 当前重连次数
        self.MAX_RECONNECT_ROUNDS = 3  # 候选设备完整轮询轮数上限, 达到后停止重连
        self.reconnect_rounds = 0  # 已完整轮询的轮数
        self.reconnect_candidates = []  # 重连候选设备(最后连接优先+收藏设备)
        self.reconnect_timer = None  # 重连定时器
        self.current_favorite_index = 0  # 当前尝试的收藏设备索引
        self.reconnect_active = False  # 重连是否激活
        self.heart_rate_received = False  # 是否成功获取心率数据
        # 收藏的设备列表
        favorite_devices_str = self._get_set("favorite_devices", "[]", str)
        try:
            self.favorite_devices = json.loads(favorite_devices_str) if favorite_devices_str else []
            if not isinstance(self.favorite_devices, list):
                self.favorite_devices = []
        except (json.JSONDecodeError, TypeError):
            self.favorite_devices = []
            logger.warning("收藏设备列表解析失败，使用空列表")
        self.setup_ui()
        # 扫描一次设备
        self.scan_devices()

        self.scan_timer = QTimer()
        self.scan_timer.timeout.connect(self.scan_devices)
        self.scan_timer.start(10000)

    def setup_ui(self):

        # 设备扫描区域
        scan_group = QGroupBox("设备管理")
        scan_layout = group_layout(QVBoxLayout())

        btn_layout = QHBoxLayout()
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.clicked.connect(self.scan_devices)
        btn_layout.addWidget(self.refresh_button)

        CheackBox_(
            "自动刷新"
            ,btn_layout
            ,True
            ,self.auto_scan
        )

        CheackBox_(
            "自动连接"
            ,btn_layout
            ,self.auto_connect
            ,self.check_auto_connect
        )

        CheackBox_(
            "自动重连"
            ,btn_layout
            ,self.auto_reconnect
            ,self.check_auto_reconnect
        )

        CheackBox_(
            "过滤无名设备"
            ,btn_layout
            ,True
            ,self.filter_empty
        )
        btn_layout.addStretch()

        scan_layout.addLayout(btn_layout)

        # === 设备列表区域：左右并排布局 ===
        devices_horizontal_layout = QHBoxLayout()
        
        # --- 左侧：可用的BLE设备 ---
        available_devices_panel = QWidget()
        available_layout = QVBoxLayout()
        available_layout.setContentsMargins(0, 0, 0, 0)
        
        # 可用设备标题
        device_textlayout = QHBoxLayout()
        self.device_list_status = QLabel()
        device_textlayout.addWidget(QLabel("可用的BLE设备:"))
        device_textlayout.addWidget(self.device_list_status)
        available_layout.addLayout(device_textlayout)
        
        # 可用设备列表（调整为较小的高度）
        self.device_list = QListWidget()
        self.device_list.itemClicked.connect(self.on_device_selected)
        self.device_list.setMinimumHeight(150)  # 减小高度
        self.device_list.setMaximumHeight(200)  # 限制最大高度
        available_layout.addWidget(self.device_list)
        
        # 收藏按钮
        self.add_favorite_button = QPushButton("★ 收藏当前设备")
        self.add_favorite_button.clicked.connect(self.add_to_favorites)
        self.add_favorite_button.setEnabled(False)
        available_layout.addWidget(self.add_favorite_button)
        
        available_devices_panel.setLayout(available_layout)
        devices_horizontal_layout.addWidget(available_devices_panel, 1)  # 比例1
        
        # --- 右侧：收藏的设备 ---
        favorite_devices_panel = QWidget()
        favorite_layout = QVBoxLayout()
        favorite_layout.setContentsMargins(0, 0, 0, 0)
        
        # 收藏设备标题
        favorite_textlayout = QHBoxLayout()
        favorite_textlayout.addWidget(QLabel("★ 收藏的设备:"))
        self.favorite_count_label = QLabel(f"({len(self.favorite_devices)})")
        favorite_textlayout.addWidget(self.favorite_count_label)
        favorite_layout.addLayout(favorite_textlayout)
        
        # 收藏设备列表
        self.favorite_list = QListWidget()
        self.favorite_list.itemClicked.connect(self.on_favorite_selected)
        self.favorite_list.setMinimumHeight(150)  # 与可用设备保持一致
        self.favorite_list.setMaximumHeight(200)  # 限制最大高度
        favorite_layout.addWidget(self.favorite_list)
        
        # 移除收藏按钮
        self.remove_favorite_button = QPushButton("移除收藏")
        self.remove_favorite_button.clicked.connect(self.remove_from_favorites)
        self.remove_favorite_button.setEnabled(False)
        favorite_layout.addWidget(self.remove_favorite_button)
        
        favorite_devices_panel.setLayout(favorite_layout)
        devices_horizontal_layout.addWidget(favorite_devices_panel, 1)  # 比例1
        
        # 将左右并排布局添加到扫描布局
        scan_layout.addLayout(devices_horizontal_layout)
        
        # 加载收藏设备到列表
        self.update_favorite_list()

        scan_group.setLayout(scan_layout)

        # 连接控制区域
        control_group = QGroupBox("连接设置")
        control_layout = group_layout(QVBoxLayout())

        duration_layout = QHBoxLayout()
        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(0, 86400)  # 0-24小时
        self.duration_spin.setValue(0)  # 0表示不自动断开
        self.duration_spin.setSuffix("秒 (0=持续连接)")

        duration_layout.addWidget(QLabel("自动断开时间:"))
        duration_layout.addWidget(self.duration_spin)
        duration_layout.addStretch()
        control_layout.addLayout(duration_layout)

        btn_layout = QHBoxLayout()
        self.connect_button = QPushButton("连接")
        self.connect_button.clicked.connect(self.connect_device)
        self.disconnect_button = QPushButton("断开连接")
        self.disconnect_button.clicked.connect(self.disconnect_device)
        self.disconnect_button.setEnabled(False)

        btn_layout.addWidget(self.connect_button)
        btn_layout.addWidget(self.disconnect_button)
        control_layout.addLayout(btn_layout)

        control_group.setLayout(control_layout)

        # 心率数据与波形已独立为"心率监测"标签页，由 HeartRateMonitorUI 承载
        self.monitor_ui = HeartRateMonitorUI(self.ble_monitor)

        self.addWidget(scan_group)
        self.addWidget(control_group)

        # 定时器用于更新链接信息UI
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self.update_ui)
        self.update_timer.start(1000)
    
    def on_heart_rate_update(self, timestamp, heart_rate):
        """心率数据更新时的处理"""
        # 心率记录与波形显示在"心率监测"独立页面
        self.monitor_ui.append_heart_rate(timestamp, heart_rate)
        self.heart_rate_updated.emit(heart_rate)
        
        # 标记已成功获取心率数据
        self.heart_rate_received = True
        
        # 如果启用了智能重连，且成功获取心率数据，则停止重连
        if self.reconnect_active:
            self.stop_smart_reconnect(success=True)
            logger.info("已成功获取心率数据，停止智能重连")
    
    def start_smart_reconnect(self):
        """启动智能重连机制"""
        if self.reconnect_active:
            logger.info("智能重连已在运行中")
            return

        self.reconnect_active = True
        self.reconnect_count = 0
        self.reconnect_rounds = 0  # 完整轮询候选设备的轮数(达上限自动停止)
        self.current_favorite_index = 0
        self.heart_rate_received = False

        # 重连候选: 最后连接的设备优先(非收藏设备断开后也能重连), 其后为收藏设备(去重)
        self.reconnect_candidates = []
        if self.last_connected_device:
            self.reconnect_candidates.append(self.last_connected_device)
        for fav in self.favorite_devices:
            if not self.last_connected_device or fav["address"] != self.last_connected_device["address"]:
                self.reconnect_candidates.append(fav)

        if not self.reconnect_candidates:
            logger.info("无重连候选设备(无最后连接记录且无收藏), 停止智能重连")
            self.stop_smart_reconnect()
            return

        # 显示重连状态
        self.status_label.setText(f"智能重连已启动 (尝试第1个候选设备)")
        self.reconnect_status.emit("智能重连已启动 (尝试第1个候选设备)")
        logger.info(f"启动智能重连机制, 候选设备{len(self.reconnect_candidates)}个")

        # 开始轮询候选设备
        self.try_connect_next_favorite()

    def try_connect_next_favorite(self):
        """尝试连接下一个重连候选设备"""
        # 检查是否成功获取心率数据
        if self.heart_rate_received:
            logger.info("已成功获取心率数据，停止智能重连")
            self.stop_smart_reconnect()
            return

        # 检查是否还有候选设备
        if not self.reconnect_candidates:
            logger.info("没有重连候选设备，停止智能重连")
            self.stop_smart_reconnect()
            return

        # 获取当前要尝试的设备
        if self.current_favorite_index < len(self.reconnect_candidates):
            device = self.reconnect_candidates[self.current_favorite_index]
            logger.info(f"尝试连接候选设备 {self.current_favorite_index + 1}/{len(self.reconnect_candidates)}: {device['name']}")

            # 设置为当前设备
            self.selected_device = device

            # 增加重连次数
            self.reconnect_count += 1

            # 显示重连进度
            self.status_label.setText(f"智能重连中... (第{self.reconnect_count}次) - 尝试设备 {self.current_favorite_index + 1}/{len(self.reconnect_candidates)}")
            self.reconnect_status.emit(f"智能重连中... (第{self.reconnect_count}次) - 尝试设备 {self.current_favorite_index + 1}/{len(self.reconnect_candidates)}")

            # 尝试连接
            asyncio.ensure_future(self.connect_device())
        else:
            # 所有候选设备都尝试过了
            self.reconnect_rounds += 1
            # 轮数上限: 连续MAX_RECONNECT_ROUNDS轮未成功则停止, 避免无限重连耗电
            if self.reconnect_rounds >= self.MAX_RECONNECT_ROUNDS:
                logger.warning(f"智能重连已连续{self.reconnect_rounds}轮未成功, 停止重连")
                self.status_label.setText("智能重连已停止(多轮未成功), 请手动连接")
                self.reconnect_status.emit("智能重连已停止(多轮未成功), 请手动连接")
                self.stop_smart_reconnect()
                return
            logger.info(f"所有候选设备都已尝试({self.reconnect_rounds}/{self.MAX_RECONNECT_ROUNDS}轮)，等待设备重新出现...")
            self.status_label.setText(f"智能重连暂停，等待设备重新出现... (第{self.reconnect_rounds}/{self.MAX_RECONNECT_ROUNDS}轮)")
            self.reconnect_status.emit(f"智能重连暂停，等待设备重新出现... (第{self.reconnect_rounds}/{self.MAX_RECONNECT_ROUNDS}轮)")

            # 创建定时器，30秒后继续尝试
            if self.reconnect_timer:
                self.reconnect_timer.stop()

            self.reconnect_timer = QTimer()
            self.reconnect_timer.timeout.connect(self.continue_smart_reconnect)
            self.reconnect_timer.start(self.reconnect_delay * 1000)  # 转换为毫秒
    
    def continue_smart_reconnect(self):
        """继续智能重连"""
        # 重置当前索引，从头开始
        self.current_favorite_index = 0
        logger.info("继续智能重连，从头开始尝试收藏设备")
        self.try_connect_next_favorite()
    
    def stop_smart_reconnect(self, success=False):
        """停止智能重连 (success=True 表示因成功获取心率而停止, 中继端清除重连状态)"""
        self.reconnect_active = False
        self.heart_rate_received = False

        if self.reconnect_timer:
            self.reconnect_timer.stop()
            self.reconnect_timer = None

        logger.info(f"智能重连已停止 (总重连次数: {self.reconnect_count})")
        if success:
            self.reconnect_status.emit("")  # 重连成功: 清除APP端重连状态显示
        else:
            self.status_label.setText("智能重连已停止")
            self.reconnect_status.emit("智能重连已停止")

    def filter_empty(self, state):
            self.ble_monitor.filter_empty = state

    def on_device_selected(self, item):
        """处理设备选择事件"""
        if not self.usedevlist: return
        # 清除之前选择的标记
        for i in range(self.device_list.count()):
            list_item = self.device_list.item(i)
            text = list_item.text()
            if text.startswith("[已选择]"):
                # 恢复原始名称
                original_text = list_item.data(self.DEVICE_DATA_ROLE)
                if original_text:
                    list_item.setText(original_text)

        # 存储当前选择的设备信息
        device_text = item.text()
        # 移除可能的"★"标记
        if device_text.startswith("★ "):
            device_text = device_text[2:]
        
        self.selected_device = {
            "name": device_text.split(" (")[0].strip(),
            "address": device_text[device_text.find("(")+1:device_text.find(")")]
        }

        self.upd_lastST.emit(self.selected_device["name"])
        self._up_set("last_selected_device", json.dumps(self.selected_device))

        # 添加"[已选择]"标记并更新显示
        marked_text = f"[已选择]{device_text}"
        item.setText(marked_text)

        # 保存原始文本到用户数据
        item.setData(self.DEVICE_DATA_ROLE, device_text)
        
        # 启用收藏按钮
        self.add_favorite_button.setEnabled(True)

    @asyncSlot()
    async def scan_devices(self):
        """扫描BLE设备"""
        # 保存当前选择状态
        if not self.usedevlist: return
        current_address = self.selected_device["address"] if self.selected_device else None
        
        # 获取最后连接的设备地址，用于自动重连
        last_connected_address = self.last_connected_device["address"] if self.last_connected_device else None

        self.device_list_status.setText("正在扫描设备...")

        try:
            devices = await self.ble_monitor.scan_devices()

            self.device_list.clear()
            
            # 用于跟踪是否找到最后连接的设备
            found_last_connected = False

            for device in devices:
                # 检查是否是收藏的设备
                is_favorite = any(fav["address"] == device.address for fav in self.favorite_devices)
                
                # 如果是收藏设备，添加星标
                if is_favorite:
                    item_text = f"★ {device.name} ({device.address})"
                else:
                    item_text = f"{device.name} ({device.address})"
                    
                item = QListWidgetItem(item_text)
                item.setData(self.DEVICE_DATA_ROLE, item_text)  # 存储原始文本

                # 如果这是之前选择的设备，添加标记
                if current_address and device.address == current_address:
                    item.setText(f"[已选择]{item_text}")
                    self.selected_device = {
                        "name": device.name,
                        "address": device.address
                    }
                    # 如果开启了自动连接，则尝试连接(不await, 避免阻塞扫描循环导致列表展示卡顿)
                    if self.usedevlist:
                        asyncio.ensure_future(self.use_for_auto_connect())
                
                # 检查是否是最后连接的设备或收藏的设备
                is_last_connected = last_connected_address and device.address == last_connected_address
                is_favorite = any(fav["address"] == device.address for fav in self.favorite_devices)
                
                if is_last_connected:
                    found_last_connected = True
                    # 如果启用了自动重连，且当前没有连接，则尝试重连
                    if (self.auto_reconnect and not (self.ble_monitor.client and 
                                                    self.ble_monitor.client.is_connected) and 
                        not self.linking):
                        logger.info(f"检测到之前连接的设备 {device.name}，尝试自动重连...")
                        self.selected_device = {
                            "name": device.name,
                            "address": device.address
                        }
                        # 标记为已选择
                        item.setText(f"[已选择]{item_text}")
                        # 添加到列表后立即尝试连接
                        self.device_list.addItem(item)
                        asyncio.ensure_future(self.connect_device())
                        continue
                elif is_favorite and not is_last_connected:
                    # 如果是收藏的设备但不是最后连接的设备
                    # 在启用自动重连且没有找到最后连接的设备时，尝试连接收藏设备
                    if (self.auto_reconnect and not found_last_connected and 
                        not (self.ble_monitor.client and self.ble_monitor.client.is_connected) and 
                        not self.linking):
                        logger.info(f"检测到收藏的设备 {device.name}，尝试自动连接...")
                        self.selected_device = {
                            "name": device.name,
                            "address": device.address
                        }
                        # 标记为已选择
                        item.setText(f"[已选择]{item_text}")
                        # 添加到列表后立即尝试连接
                        self.device_list.addItem(item)
                        asyncio.ensure_future(self.connect_device())
                        found_last_connected = True  # 标记为已找到，避免重复连接
                        continue

                self.device_list.addItem(item)

            self.device_list_status.setText(f"找到 {len(devices)} 个设备")
            
            # 如果启用了自动重连，但没有找到最后连接的设备，记录日志
            if self.auto_reconnect and last_connected_address and not found_last_connected:
                logger.info(f"未找到最后连接的设备，等待设备重新出现...")
                
            self.noscanerror_win = False
        except WindowsError as e:
            print(e.winerror)
            if e.winerror == -2147020577:
                self.device_list_status.setText("请打开蓝牙")
                logger.warning("蓝牙未开启")
                errortxt = "蓝牙未开启，请打开蓝牙"
            else:
                self.device_list_status.setText(f"未知错误: {e.winerror}")
                errortxt = f"-{e.strerror} [{e.winerror}] "
                logger.error(f"窗口错误: {errortxt}")

            if not self.noscanerror_win:
                # parent不能传self(DeviceConnectionUI是QVBoxLayout子类, 非QWidget, 会TypeError弹窗失效)
                QMessageBox.warning(None, "错误", errortxt)
                self.noscanerror_win = True

        except Exception as e:
            self.device_list_status.setText(f"扫描错误: {str(e)}")
            logger.error(f"扫描BLE设备错误: {e}", exc_info=True)

    async def use_for_auto_connect(self):
        """自动连接"""
        if self.auto_connect and self.auto_connect_now:
            await self.connect_device()

    def auto_scan(self, state):
        """启停自动扫描设备"""
        if state == Qt.Checked:
            self.scan_timer.start(10000)
        else:
            self.scan_timer.stop()

    @asyncSlot()
    async def connect_device(self):
        """连接选定的设备"""
        self.auto_connect_now = True
        if not self.selected_device:
            self.status_label.setText("请先选择设备")
            return
        # 如果正在连接，则返回
        if self.linking: return

        device_name = self.selected_device["name"]
        device_address = self.selected_device["address"]
        
        # 保存最后连接的设备信息，用于自动重连
        self.last_connected_device = {
            "name": device_name,
            "address": device_address
        }

        self.linking = True

        self.status_label.setText(f"正在连接 {device_name}...")
        # 同步连接过程提示到接收端(波形上方info行)
        self.reconnect_status.emit(f"正在连接 {device_name}...")
        logger.info(f"尝试连接 {device_name} ({device_address})")

        try:
            success, rtext = await self.ble_monitor.connect_device(device_address)
            self.status_label.setText(rtext.format(device_address=device_name))
            # 同步连接结果到接收端: 成功清除提示, 失败显示原因
            self.reconnect_status.emit("" if success else rtext.format(device_address=device_name))
            logger.info(rtext.format(device_address=f"{device_name} ({device_address})"))
            if success:
                self.set_act_Devstatus.emit("断开")
                self.heart_rate_display.append(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 已连接到设备")
                
                # 停止智能重连
                self.stop_smart_reconnect()

                # 设置自动断开定时器（如果设置了时间）
                duration = self.duration_spin.value()
                logger.info(f"自动断开时间 {duration} 秒(0表示不自动断开)")
                if duration > 0:
                    QTimer.singleShot(duration * 1000, self.disconnect_device)

        # 链接设备时错误处理===========
        #
        except BleakDeviceNotFoundError:
            self.status_label.setText(f"未找到设备 {device_name}")
            self.reconnect_status.emit(f"未找到设备 {device_name}")
            logger.error(f"未找到设备 {device_name}")
            # 如果是智能重连模式，继续尝试下一个设备
            if self.reconnect_active:
                self.current_favorite_index += 1
                QTimer.singleShot(self.reconnect_delay * 1000, self.try_connect_next_favorite)
        except BleakError as e:
            if "Could not get GATT services: Unreachable" in str(e):
                self.status_label.setText(f"设备GATT服务不可用, 请尝试重新启动设备心率广播功能")
                self.reconnect_status.emit(f"设备GATT服务不可用, 请尝试重新启动设备心率广播功能")
            else:
                self.status_label.setText(f"连接错误: {str(e)}")
                self.reconnect_status.emit(f"连接错误: {str(e)}")
            self.heart_rate_display.append(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 连接失败: {str(e)}")
            logger.error(f"连接设备时出错: {e}", exc_info=True)
            # 如果是智能重连模式，继续尝试下一个设备
            if self.reconnect_active:
                self.current_favorite_index += 1
                QTimer.singleShot(self.reconnect_delay * 1000, self.try_connect_next_favorite)
        except OSError as e:
            if e.winerror == -2147023673:
                self.status_label.setText(f"链接请求被中断({e.winerror})")
                self.reconnect_status.emit(f"链接请求被中断({e.winerror})")
                self.heart_rate_display.append(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 连接失败: {str(e)}")
                logger.warning(f"连接设备时出错: {e}")
            else:
                self.status_label.setText(f"连接错误: {str(e)}")
                self.reconnect_status.emit(f"连接错误: {str(e)}")
                self.heart_rate_display.append(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 链接错误: {str(e)}")
                logger.error(f"连接设备时出错: {e}", exc_info=True)
            # 如果是智能重连模式，继续尝试下一个设备
            if self.reconnect_active:
                self.current_favorite_index += 1
                QTimer.singleShot(self.reconnect_delay * 1000, self.try_connect_next_favorite)
        except Exception as e:
            self.status_label.setText(f"连接错误: {str(e)}")
            self.reconnect_status.emit(f"连接错误: {str(e)}")
            self.heart_rate_display.append(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 连接失败: {str(e)}")
            logger.error(f"连接设备时出错: {e}", exc_info=True)
            # 如果是智能重连模式，继续尝试下一个设备
            if self.reconnect_active:
                self.current_favorite_index += 1
                QTimer.singleShot(self.reconnect_delay * 1000, self.try_connect_next_favorite)
        #
        # =============================

        self.linking = False

    def disconnect_error(self, e):
        self.status_label.setText(e)
        self.reconnect_status.emit(e)
    
    @asyncSlot()
    async def disconnect_device(self):
        """断开当前连接"""
        @try_except('断开连接错误',self.disconnect_error)
        async def disconnect():
            self.disconnect_button.setEnabled(False)
            self.quit_ = True
            success = await self.ble_monitor.disconnect_device()
            if success:
                self.set_act_Devstatus.emit("连接")
                self.auto_connect_now = False
                self.be_timeout = False
                self.status_label.setText("已断开连接")
                # 同步断开状态到接收端
                self.reconnect_status.emit("已断开连接")
                self.set_devicelist_use(True)
                self.device_list_status.setText("断开连接后重新扫描设备...")
                self.heart_rate_display.append(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 已断开连接")
                
                # 清除波形图数据
                if self.waveform:
                    self.waveform.clear_data()
                
                # 停止智能重连
                self.stop_smart_reconnect()
                
                # 手动断开连接时，清除最后连接的设备信息，防止自动重连
                if self.auto_reconnect:
                    logger.info("手动断开连接，暂时禁用自动重连")
                    # 保存设备信息但标记为手动断开
                    if self.last_connected_device:
                        self.last_connected_device = None
                
                # 断开连接后立即重新扫描设备，确保设备列表更新
                asyncio.ensure_future(self.scan_devices())
                logger.info("已断开连接")

                # 显示收集的数据摘要
                stats = self.ble_monitor.get_heart_rate_stats()
                if stats:
                    self.heart_rate_display.append(
                        f"\n心率统计:\n"
                        f"最低: {stats['min']} BPM\n"
                        f"最高: {stats['max']} BPM\n"
                        f"平均: {stats['avg']:.1f} BPM\n"
                        f"共记录 {stats['count']} 条数据"
                    )
            else:
                self.status_label.setText("断开连接失败")
                self.reconnect_status.emit("断开连接失败")
            self.quit_ = False
        await disconnect()

    def update_ui(self):
        """更新UI状态"""
        if self.quit_ == True or self.linking:
            pass
        elif self.ble_monitor.client and self.ble_monitor.client.is_connected:
            self.be_timeout = True
            self.connect_button.setEnabled(False)
            self.disconnect_button.setEnabled(True)
            self.set_devicelist_use(False)
        else:
            if self.be_timeout:
                # 检测到连接断开
                self.status_label.setText("链接被断开")
                # 同步断连提示到接收端(智能重连文本随后覆盖)
                self.reconnect_status.emit("链接被断开")
                self.set_act_Devstatus.emit("连接")
                self.be_timeout = False
                self.set_devicelist_use(True)
                
                # 如果启用了自动重连，启动智能重连机制
                if self.auto_reconnect:
                    logger.info("检测到连接断开，启动智能重连机制")
                    self.heart_rate_display.append(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 连接断开，启动智能重连...")
                    # 启动智能重连
                    self.start_smart_reconnect()
                
            self.heart_rate_updated.emit(-1)
            self.connect_button.setEnabled(True)
            self.disconnect_button.setEnabled(False)

    def set_devicelist_use(self, checked):
        if checked:
            self.device_list_status.setStyleSheet("color: green;")
            self.device_list.setEnabled(True)
            self.usedevlist = True
        else:
            self.device_list_status.setText("已禁用")
            self.device_list_status.setStyleSheet("color: red;")
            self.device_list.setEnabled(False)
            self.usedevlist = False

    def check_auto_connect(self, state):
        if state == Qt.Checked:
            self.auto_connect = True
            self._up_set(option="auto_connect", value=True)
        else:
            self.auto_connect = False
            self._up_set(option="auto_connect", value=False)
            
    def check_auto_reconnect(self, state):
        """处理自动重连选项的变更"""
        if state == Qt.Checked:
            self.auto_reconnect = True
            self._up_set(option="auto_reconnect", value=True)
            logger.info("自动重连功能已启用")
        else:
            self.auto_reconnect = False
            self._up_set(option="auto_reconnect", value=False)
            logger.info("自动重连功能已禁用")

    def _get_set(self, option: str, default, type_=None):
        """获取设置项"""
        return gs('Device', option, default, type_, "浮窗")

    def _up_set(self, option: str, value):
        """更新设置项"""
        ups('Device', option, value, "浮窗")
    
    def update_favorite_list(self):
        """更新收藏设备列表显示"""
        self.favorite_list.clear()
        for device in self.favorite_devices:
            item_text = f"★ {device['name']} ({device['address']})"
            item = QListWidgetItem(item_text)
            item.setData(self.DEVICE_DATA_ROLE, device)
            self.favorite_list.addItem(item)
        self.favorite_count_label.setText(f"({len(self.favorite_devices)})")
    
    def add_to_favorites(self):
        """添加当前选择的设备到收藏"""
        if not self.selected_device:
            QMessageBox.warning(None, "提示", "请先选择一个设备")
            return
        
        # 检查设备是否已经在收藏列表中
        for fav in self.favorite_devices:
            if fav["address"] == self.selected_device["address"]:
                QMessageBox.information(None, "提示", "该设备已在收藏列表中")
                return
        
        # 添加到收藏列表
        self.favorite_devices.append({
            "name": self.selected_device["name"],
            "address": self.selected_device["address"]
        })
        
        # 保存到配置
        self._up_set("favorite_devices", json.dumps(self.favorite_devices))
        
        # 更新UI
        self.update_favorite_list()
        
        # 在设备列表中添加星标
        for i in range(self.device_list.count()):
            item = self.device_list.item(i)
            text = item.text()
            if self.selected_device["address"] in text and not text.startswith("★"):
                # 获取原始文本
                original_text = item.data(self.DEVICE_DATA_ROLE)
                if original_text and not original_text.startswith("★"):
                    new_text = f"★ {original_text}"
                    item.setData(self.DEVICE_DATA_ROLE, new_text)
                    if text.startswith("[已选择]"):
                        item.setText(f"[已选择]{new_text}")
                    else:
                        item.setText(new_text)
        
        logger.info(f"已将设备 {self.selected_device['name']} 添加到收藏")
        QMessageBox.information(None, "成功", f"已将 {self.selected_device['name']} 添加到收藏")
    
    def on_favorite_selected(self, item):
        """处理收藏设备选择事件"""
        device = item.data(self.DEVICE_DATA_ROLE)
        if device:
            # 启用移除收藏按钮
            self.remove_favorite_button.setEnabled(True)
            # 如果设备在可用列表中，自动选择它
            for i in range(self.device_list.count()):
                list_item = self.device_list.item(i)
                text = list_item.text()
                if device["address"] in text:
                    self.on_device_selected(list_item)
                    break
    
    def remove_from_favorites(self):
        """从收藏中移除选中的设备"""
        current_item = self.favorite_list.currentItem()
        if not current_item:
            QMessageBox.warning(None, "提示", "请先选择要移除的设备")
            return
        
        device = current_item.data(self.DEVICE_DATA_ROLE)
        if not device:
            return
        
        # 从收藏列表中移除
        self.favorite_devices = [fav for fav in self.favorite_devices 
                                 if fav["address"] != device["address"]]
        
        # 保存到配置
        self._up_set("favorite_devices", json.dumps(self.favorite_devices))
        
        # 更新UI
        self.update_favorite_list()
        self.remove_favorite_button.setEnabled(False)
        
        # 从设备列表中移除星标
        for i in range(self.device_list.count()):
            item = self.device_list.item(i)
            text = item.data(self.DEVICE_DATA_ROLE)
            if text and device["address"] in text and text.startswith("★ "):
                new_text = text[2:]  # 移除"★ "
                item.setData(self.DEVICE_DATA_ROLE, new_text)
                current_text = item.text()
                if current_text.startswith("[已选择]★ "):
                    item.setText(f"[已选择]{new_text}")
                else:
                    item.setText(new_text)
        
        logger.info(f"已将设备 {device['name']} 从收藏中移除")
        QMessageBox.information(None, "成功", f"已将 {device['name']} 从收藏中移除")