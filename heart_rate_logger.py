"""
心率数据日志记录模块
按天保存心率数据到CSV文件
"""
import os
import csv
import time
import datetime
from pathlib import Path
from system_utils import logger


class HeartRateLogger:
    """心率数据日志记录器"""
    
    def __init__(self, log_dir="log", buffer_seconds=300.0, buffer_max=1000):
        """
        初始化心率日志记录器
        
        Args:
            log_dir: 日志目录路径，默认为 "log"
            buffer_seconds: 缓冲落盘间隔（秒），距上次落盘超过该时长即写盘，默认 300（5分钟）
            buffer_max: 缓冲条数上限，达到即提前写盘（防止设备高频上报时内存增长）
        """
        self.log_dir = log_dir
        self.current_date = None
        self.current_file = None
        self.current_writer = None
        self.csv_file_handle = None
        
        # 缓冲机制: 数据先入内存, 超过 buffer_seconds 或攒满 buffer_max 条才写盘
        self.buffer = []
        self.buffer_max = int(buffer_max)
        self.buffer_seconds = float(buffer_seconds)
        self._last_flush = time.time()
        
        # 确保日志目录存在
        self._ensure_log_dir()
        
    def _ensure_log_dir(self):
        """确保日志目录存在"""
        try:
            Path(self.log_dir).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.error(f"创建日志目录失败: {e}")
    
    def _get_log_filename(self):
        """
        获取当天的日志文件名
        
        Returns:
            日志文件的完整路径
        """
        today = datetime.date.today()
        filename = f"heart_rate_{today.strftime('%Y-%m-%d')}.csv"
        return os.path.join(self.log_dir, filename)
    
    def _check_and_update_file(self):
        """检查日期是否变更，如果变更则切换到新的日志文件"""
        today = datetime.date.today()
        
        # 如果日期变更或者是第一次写入
        if self.current_date != today:
            # 关闭旧文件
            if self.csv_file_handle:
                try:
                    self.csv_file_handle.close()
                except Exception as e:
                    logger.error(f"关闭旧日志文件失败: {e}")
            
            # 更新日期
            self.current_date = today
            self.current_file = self._get_log_filename()
            
            # 检查文件是否存在，决定是否需要写入表头
            file_exists = os.path.exists(self.current_file)
            
            try:
                # 打开新文件（追加模式）
                self.csv_file_handle = open(self.current_file, 'a', newline='', encoding='utf-8-sig')
                self.current_writer = csv.writer(self.csv_file_handle)
                
                # 如果是新文件，写入表头
                if not file_exists:
                    self.current_writer.writerow(['时间戳', '心率值'])
                    logger.info(f"创建新的心率日志文件: {self.current_file}")
                else:
                    logger.info(f"继续写入心率日志文件: {self.current_file}")
                    
            except Exception as e:
                logger.error(f"打开日志文件失败: {e}")
                self.csv_file_handle = None
                self.current_writer = None
    
    def log_heart_rate(self, heart_rate: int, timestamp: str = None):
        """
        记录心率数据
        
        Args:
            heart_rate: 心率值
            timestamp: 时间戳（可选），如果不提供则使用当前时间
        """
        # 检查并更新日志文件
        self._check_and_update_file()
        
        # 如果没有提供时间戳，使用当前时间
        if timestamp is None:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # 添加到缓冲区; 防止写盘持续失败时缓冲无限累积, 超过5倍上限丢弃最旧数据
        if len(self.buffer) >= self.buffer_max * 5:
            self.buffer.pop(0)
        self.buffer.append((timestamp, heart_rate))
        
        # 攒满条数上限, 或距上次落盘超过配置间隔时, 批量写入磁盘
        if (len(self.buffer) >= self.buffer_max 
                or time.time() - self._last_flush >= self.buffer_seconds):
            self._flush_buffer()
    
    def _flush_buffer(self):
        """将缓冲区数据写入磁盘"""
        if not self.buffer or not self.current_writer:
            return
        
        try:
            # 批量写入所有缓冲数据
            for timestamp, heart_rate in self.buffer:
                self.current_writer.writerow([timestamp, heart_rate])
            
            # 刷新到磁盘
            self.csv_file_handle.flush()
            
            # 清空缓冲区
            self.buffer.clear()
        except Exception as e:
            logger.error(f"批量写入心率数据失败: {e}")
        finally:
            # 无论成败都重置计时: 失败时数据留在缓冲区, 等下个周期重试, 避免每次log都重试刷盘
            self._last_flush = time.time()
    
    def close(self):
        """关闭日志文件"""
        # 先flush缓冲区中剩余的数据
        self._flush_buffer()
        
        if self.csv_file_handle:
            try:
                self.csv_file_handle.close()
                logger.info("心率日志文件已关闭")
            except Exception as e:
                logger.error(f"关闭日志文件失败: {e}")
            finally:
                self.csv_file_handle = None
                self.current_writer = None
                self.current_date = None
                self.current_file = None
    
    def get_today_stats(self):
        """
        获取今天的心率统计信息
        
        Returns:
            包含最小值、最大值、平均值和记录数的字典，如果没有数据则返回None
        """
        self._check_and_update_file()
        
        if not self.current_file or not os.path.exists(self.current_file):
            return None
        
        try:
            with open(self.current_file, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                heart_rates = []
                
                for row in reader:
                    try:
                        hr = int(row['心率值'])
                        heart_rates.append(hr)
                    except (ValueError, KeyError):
                        continue
                
                if not heart_rates:
                    return None
                
                return {
                    'min': min(heart_rates),
                    'max': max(heart_rates),
                    'avg': round(sum(heart_rates) / len(heart_rates), 1),
                    'count': len(heart_rates)
                }
        except Exception as e:
            logger.error(f"读取今日心率统计失败: {e}")
            return None

