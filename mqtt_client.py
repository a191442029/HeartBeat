import paho.mqtt.client as mqtt
import json
import time
import threading
from system_utils import logger

class MQTTClient:
    """MQTT客户端，用于将心率数据发送到Home Assistant"""
    def __init__(self):
        self.client = None
        self.connected = False
        self.config = {
            "broker": "localhost",
            "port": 1883,
            "username": "",
            "password": "",
            "client_id": "heartbeat_monitor",
            "topic": "homeassistant/sensor/heartrate/state",
            "discovery_topic": "homeassistant/sensor/heartrate/config",
            "discovery_enabled": True
        }
        self.reconnect_thread = None
        self.stop_reconnect = False

    def connect(self, config=None):
        """连接到MQTT服务器"""
        if config:
            self.config.update(config)
        
        # 复位重连标志, 否则第二次连接后 on_disconnect 不再触发自动重连
        self.stop_reconnect = False
        
        if self.client:
            self.disconnect()
        
        try:
            try:
                # paho-mqtt 2.x 需显式指定回调API版本, 使用V1保持旧回调签名(client, userdata, flags, rc)兼容
                self.client = mqtt.Client(
                    callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
                    client_id=self.config["client_id"])
            except AttributeError:
                # paho-mqtt 1.x 无 CallbackAPIVersion 属性
                self.client = mqtt.Client(client_id=self.config["client_id"])
            
            # 设置用户名和密码（如果有）
            if self.config["username"] and self.config["password"]:
                self.client.username_pw_set(self.config["username"], self.config["password"])
            
            # 设置回调函数
            self.client.on_connect = self.on_connect
            self.client.on_disconnect = self.on_disconnect
            
            # 连接到服务器
            self.client.connect(self.config["broker"], self.config["port"], 60)
            
            # 启动后台线程
            self.client.loop_start()
            
            # 如果启用了自动发现，发送配置信息到Home Assistant
            if self.config["discovery_enabled"]:
                self.send_discovery_config()
                
            return True
        except Exception as e:
            logger.error(f"MQTT连接失败: {str(e)}")
            return False

    def disconnect(self):
        """断开MQTT连接"""
        if self.client:
            self.stop_reconnect = True
            
            # 等待重连线程停止，增加超时时间并多次尝试
            if self.reconnect_thread and self.reconnect_thread.is_alive():
                for _ in range(5):  # 最多等待5秒
                    self.reconnect_thread.join(timeout=1.0)
                    if not self.reconnect_thread.is_alive():
                        break
                    logger.info(f"等待重连线程停止... ({_+1}/5)")
            
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception as e:
                logger.warning(f"断开MQTT连接时出现异常: {e}")
            finally:
                self.client = None
                self.connected = False

    def on_connect(self, client, userdata, flags, rc):
        """连接成功回调"""
        if rc == 0:
            self.connected = True
            logger.info("已连接到MQTT服务器")
            
            # 发送在线状态
            try:
                avail_topic = f"{self.config['topic']}/availability"
                self.client.publish(
                    avail_topic,
                    "online",
                    qos=1,
                    retain=True
                )
                logger.info("已发布设备在线状态")
            except Exception as e:
                logger.error(f"发布设备在线状态失败: {str(e)}")
        else:
            self.connected = False
            error_messages = {
                1: "连接被拒绝：协议版本不正确",
                2: "连接被拒绝：无效的客户端标识符",
                3: "连接被拒绝：服务器不可用",
                4: "连接被拒绝：用户名或密码错误",
                5: "连接被拒绝：未授权"
            }
            error_msg = error_messages.get(rc, f"未知错误 (代码: {rc})")
            logger.error(f"MQTT连接失败: {error_msg}")

    def on_disconnect(self, client, userdata, rc):
        """断开连接回调"""
        self.connected = False
        logger.warning(f"MQTT连接断开，返回码: {rc}")
        
        # 发送离线状态
        try:
            avail_topic = f"{self.config['topic']}/availability"
            self.client.publish(
                avail_topic,
                "offline",
                qos=1,
                retain=True
            )
            logger.info("已发布设备离线状态")
        except Exception as e:
            logger.error(f"发布设备离线状态失败: {str(e)}")
        
        # 如果不是主动断开，尝试重新连接
        if not self.stop_reconnect and rc != 0:
            self.reconnect_thread = threading.Thread(target=self.reconnect)
            self.reconnect_thread.daemon = True
            self.reconnect_thread.start()

    def reconnect(self):
        """尝试重新连接"""
        retry_count = 0
        while not self.connected and not self.stop_reconnect and retry_count < 5:
            retry_count += 1
            logger.info(f"尝试重新连接MQTT服务器 ({retry_count}/5)...")
            try:
                time.sleep(5)  # 等待5秒再重试
                self.client.reconnect()
                if self.connected:
                    if self.config["discovery_enabled"]:
                        self.send_discovery_config()
                    break
            except Exception as e:
                logger.error(f"MQTT重连失败: {str(e)}")

    def send_discovery_config(self):
        """发送Home Assistant MQTT自动发现配置"""
        if not self.connected or not self.client:
            return False
        
        try:
            # 创建传感器配置
            avail_topic = f"{self.config['topic']}/availability"
            
            config_data = {
                "name": "Heart Rate",
                "unique_id": "heartbeat_monitor_hr",
                "state_topic": self.config["topic"],
                "value_template": "{{ value_json.heart_rate }}",
                "unit_of_measurement": "BPM",
                "icon": "mdi:heart-pulse",
                "force_update": True,  # 强制更新，即使值没有变化
                "expire_after": 60,    # 60秒无更新后显示为不可用
                "availability_topic": avail_topic,  # 使用单独的可用性主题
                "payload_available": "online",
                "payload_not_available": "offline",
                "json_attributes_topic": self.config["topic"],
                "json_attributes_template": '{"last_updated": "{{ value_json.timestamp }}", "status": "{{ value_json.status }}"}',
                "device": {
                    "identifiers": ["heartbeat_monitor"],
                    "name": "HeartBeat Monitor",
                    "model": "BLE Heart Rate Monitor",
                    "manufacturer": "HeartBeat App"
                }
            }
            
            # 发送配置到Home Assistant
            result = self.client.publish(
                self.config["discovery_topic"],
                json.dumps(config_data),
                qos=1,
                retain=True
            )
            
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                logger.info("已发送MQTT自动发现配置")
                return True
            else:
                logger.error(f"发送MQTT自动发现配置失败: {result.rc}")
                return False
        except Exception as e:
            logger.error(f"发送MQTT自动发现配置时出错: {str(e)}")
            return False

    def publish_heart_rate(self, heart_rate, timestamp=None, status="connected"):
        """发布心率数据到MQTT
        
        Args:
            heart_rate: 心率值
            timestamp: 时间戳
            status: 设备状态，'connected' 或 'disconnected'
        """
        if not self.connected or not self.client:
            return False
        
        try:
            # 准备数据
            payload = {
                "heart_rate": heart_rate,
                "timestamp": timestamp or time.strftime("%Y-%m-%d %H:%M:%S"),
                "status": status
            }
            
            # 添加随机值和毫秒级时间戳，确保每次消息都不同
            import random
            payload["timestamp_ms"] = int(time.time() * 1000)
            payload["random"] = random.randint(1, 1000000)  # 添加随机数确保消息唯一
            
            # 发布数据
            logger.debug(f"准备发布MQTT数据: {payload} 到主题: {self.config['topic']}")
            
            # 然后发送实际数据
            result = self.client.publish(
                self.config["topic"],
                json.dumps(payload),
                qos=1,  # 使用QoS 1确保消息至少送达一次
                retain=False  # 不保留消息，确保每次都是新消息
            )
            
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                logger.debug(f"成功发布心率数据: {heart_rate}, 状态: {status}")
                return True
            else:
                logger.error(f"发布心率数据失败: {result.rc}")
                return False
        except Exception as e:
            logger.error(f"发布心率数据时出错: {str(e)}")
            return False
