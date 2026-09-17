from typing import List, Optional, Dict
import datetime
import asyncio
import re
from bleak import BleakScanner, BleakClient, BleakError
from collections import deque

from importlib.metadata import version

from system_utils import logger
# 心率服务UUID
HEART_RATE_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
# 心率测量特征UUID
HEART_RATE_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

def _bleak_ver_tuple(v: str) -> tuple:
    """解析版本号为数字元组(兼容预发布标签如 0.22.3rc1 / 本地版本号, 原int()会崩溃导致程序起不来)"""
    try:
        nums = re.findall(r'\d+', v)
        return tuple(int(n) for n in nums[:3]) if nums else (0, 0, 0)
    except Exception:
        return (0, 0, 0)

if _bleak_ver_tuple(version("bleak")) < (1, 0, 0):
    async def check_service(client: BleakClient):
        services = await client.get_services()
        return HEART_RATE_SERVICE_UUID in [ser.uuid for ser in services]
else:
    async def check_service(client: BleakClient):
        rtry = 0
        while rtry <= 10:
            try:
                return any(ser.uuid == HEART_RATE_SERVICE_UUID for ser in client.services)
            except BleakError as e:
                if "Service Discovery has not been performed yet" in str(e):
                    logger.warning(f"服务发现未完成({rtry}/10秒)")
                    rtry += 1
                    await asyncio.sleep(1)
                else:
                    raise e
        raise TimeoutError("获取设备服务超时")


class BLEHeartRateMonitor:
    """BLE连接和心率数据处理类"""
    def __init__(self):
        self.client = None
        self.devices = []
        # 使用deque限制心率数据最大长度，防止内存溢出（最多保留10000条数据）
        self.heart_rate_data = deque(maxlen=10000)
        self.heart_rate_callback = None

        self.filter_empty: bool = True

    async def scan_devices(self, timeout: float = 5.0) -> List:
        """
        扫描BLE设备

        Args:
            timeout: 扫描超时时间(秒)

        Returns:
            发现的设备列表
        """
        self.devices = await BleakScanner.discover()
        # 过滤掉名称为None的设备
        return [d for d in self.devices if d.name is not None] if self.filter_empty else self.devices

    async def connect_device(self, device_address: str) -> tuple[bool, str]:
        """
        连接设备

        Args:
            device_address: 要连接的设备地址

        Returns:
            连接是否成功
        """
        self.client = BleakClient(device_address)
        await self.client.connect()
        if await check_service(self.client):
            # 启用心率通知
            await self.client.start_notify(
                HEART_RATE_MEASUREMENT_UUID,
                self._notification_handler
            )
            return True, "已连接 {device_address}"
        else:
            await self.disconnect_device()
            return False, "{device_address} 不是支持心率服务的设备"

    async def disconnect_device(self):
        """断开设备连接"""
        try:
            if self.client and self.client.is_connected:
                await self.client.stop_notify(HEART_RATE_MEASUREMENT_UUID)
                await self.client.disconnect()
                return True
            return False
        except Exception as e:
            logger.error(f"断开蓝牙设备时出错: {e}")
            # 即使出错也尝试清理资源
            try:
                if self.client:
                    self.client = None
            except:
                pass
            return False

    def _notification_handler(self, sender: str, data: bytearray):
        """
        处理心率通知数据
        
        Args:
            sender: 特征UUID
            data: 接收到的原始数据
        """
        heart_rate = self._parse_heart_rate(data)
        if heart_rate is None:
            # 脏包/短包不入库不回调, 防止越界异常或0值污染数据链
            logger.warning(f"心率数据格式异常(长度={len(data)}), 已丢弃: {bytes(data).hex()}")
            return
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 保存数据
        self.heart_rate_data.append((timestamp, heart_rate))

        # 调用回调函数通知UI更新
        if self.heart_rate_callback:
            self.heart_rate_callback(timestamp, heart_rate)

    def _parse_heart_rate(self, data: bytearray) -> Optional[int]:
        """
        解析心率数据

        Args:
            data: 原始心率数据

        Returns:
            解析出的心率值; 数据长度不足(脏包/短包)时返回None
        """
        # 长度校验: flags(1字节) + 心率值(1或2字节); 原实现无校验, 空包data[0]直接IndexError
        if len(data) < 2:
            return None
        flags = data[0]
        heart_rate_value_format = (flags & 0x01) == 0x01

        if heart_rate_value_format:
            if len(data) < 3:
                return None
            heart_rate = int.from_bytes(data[1:3], byteorder='little')
        else:
            heart_rate = int(data[1])

        return heart_rate

    def get_heart_rate_stats(self) -> Optional[Dict[str, float]]:
        """
        获取心率统计数据

        Returns:
            包含最小值、最大值、平均值和数据点数量的字典，如果没有数据则返回None
        """
        if not self.heart_rate_data:
            return None

        hrs = [hr for _, hr in self.heart_rate_data]
        return {
            'min': min(hrs),
            'max': max(hrs),
            'avg': round(sum(hrs) / len(hrs), 1),
            'count': len(hrs)
        }
