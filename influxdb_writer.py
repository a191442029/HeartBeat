"""
InfluxDB客户端模块
用于将心率数据发送到InfluxDB数据库
"""
import logging
import datetime
from influxdb_client import InfluxDBClient as InfluxDBClientLib, Point, WriteOptions
from influxdb_client.client.write_api import SYNCHRONOUS

# 获取logger
logger = logging.getLogger('__main__')


class InfluxDBWriter:
    """InfluxDB客户端类"""
    
    def __init__(self, url=None, token=None, org=None, bucket=None):
        """
        初始化InfluxDB客户端
        
        Args:
            url: InfluxDB服务器地址，例如：http://192.168.1.100:8086
            token: 访问令牌
            org: 组织名称
            bucket: 存储桶名称
        """
        self.url = url
        self.token = token
        self.org = org
        self.bucket = bucket
        self.client = None
        self.write_api = None
        self.connected = False
    
    def connect(self):
        """连接到InfluxDB"""
        try:
            # 创建InfluxDB客户端(timeout毫秒: 真实网络调用在每秒定时写入,
            # 不限超时则服务器不可达时每次写入都会长时间阻塞Qt主线程)
            self.client = InfluxDBClientLib(
                url=self.url,
                token=self.token,
                org=self.org,
                timeout=5000
            )
            
            # 获取写入API
            self.write_api = self.client.write_api(write_options=WriteOptions(batch_size=1))
            
            # 测试连接
            # 注意：InfluxDB v2使用token认证，连接时不会立即验证
            # 只有在实际写入数据时才会验证连接
            self.connected = True
            logger.info(f"InfluxDB客户端已初始化: {self.url}")
            return True
            
        except Exception as e:
            logger.error(f"连接InfluxDB失败: {e}")
            self.connected = False
            return False
    
    def disconnect(self):
        """断开InfluxDB连接"""
        try:
            if self.client:
                self.client.close()
                self.connected = False
                logger.info("InfluxDB客户端已断开")
        except Exception as e:
            logger.error(f"断开InfluxDB时出错: {e}")
    
    def write_heart_rate(self, heart_rate, timestamp=None):
        """
        写入心率数据到InfluxDB
        
        Args:
            heart_rate: 心率值（BPM）
            timestamp: 时间戳（可选），如果不提供则使用当前时间
        """
        if not self.connected or not self.write_api:
            logger.warning("InfluxDB未连接，无法写入数据")
            return False
        
        try:
            # 创建数据点
            point = Point("heart_rate") \
                .tag("device", "heart_rate_monitor") \
                .field("value", heart_rate)
            
            # 如果提供了时间戳，设置时间戳（转换为纳秒）
            if timestamp:
                if isinstance(timestamp, datetime.datetime):
                    # 将datetime转换为纳秒时间戳
                    timestamp_ns = int(timestamp.timestamp() * 1e9)
                    point = point.time(timestamp_ns)
            
            # 写入数据
            self.write_api.write(bucket=self.bucket, record=point)
            logger.info(f"已写入心率数据到InfluxDB: {heart_rate} BPM")
            return True
            
        except Exception as e:
            logger.error(f"写入心率数据到InfluxDB失败: {e}")
            return False
    
    def test_connection(self):
        """测试InfluxDB连接(只读探活: /ping + 组织校验, 不再向正式bucket写测试点污染数据)"""
        try:
            if not self.client:
                logger.error("InfluxDB客户端未初始化")
                return False
            # 服务可达性检查
            if not self.client.ping():
                logger.error("InfluxDB服务不可达(/ping 失败)")
                return False
            # token与组织有效性检查(只读API)
            orgs = self.client.orgs_api().find_organizations(org=self.org)
            if not orgs:
                logger.error(f"InfluxDB组织不存在或token无权限: {self.org}")
                return False
            logger.info("InfluxDB连接测试成功(服务可达, token/org有效)")
            return True

        except Exception as e:
            logger.error(f"InfluxDB连接测试失败: {e}")
            return False
    
    def is_connected(self):
        """检查是否已连接"""
        return self.connected
