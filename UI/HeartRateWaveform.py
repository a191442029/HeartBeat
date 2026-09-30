"""
心率波形图组件
用于实时显示心率数据的波形图
"""
from collections import deque
from PyQt5.QtWidgets import QWidget, QVBoxLayout
from matplotlib.figure import Figure
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.animation import FuncAnimation
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import datetime
import numpy as np
import logging

# 获取logger
logger = logging.getLogger('__main__')

# 设置中文字体
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'Arial Unicode MS']
plt.rcParams['axes.unicode_minus'] = False

class HeartRateWaveform(QWidget):
    """心率波形图组件"""
    
    def __init__(self, parent=None, max_points=60):
        """
        初始化心率波形图
        
        Args:
            parent: 父组件
            max_points: 最多显示的数据点数量（默认60个点，约1分钟的数据）
        """
        super().__init__(parent)
        
        self.max_points = max_points
        # 使用deque来存储心率数据，自动维护固定长度
        self.heart_rates = deque(maxlen=max_points)
        self.timestamps = deque(maxlen=max_points)
        
        # 初始化为空数据
        for i in range(max_points):
            self.heart_rates.append(0)
            self.timestamps.append(i)
        
        # 存储需要清除的图形元素
        self.zone_patches = []  # 心率区间背景
        
        self.setup_ui()
    
    def setup_ui(self):
        """设置UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # 创建matplotlib图形 - 适应水平布局
        self.figure = Figure(figsize=(8, 5), dpi=100)
        self.canvas = FigureCanvas(self.figure)
        self.canvas.setMinimumWidth(400)  # 设置最小宽度
        
        # 添加子图
        self.ax = self.figure.add_subplot(111)
        
        # 设置现代化的配色
        self.ax.set_facecolor('#f8f9fa')  # 浅灰背景
        self.figure.patch.set_facecolor('white')
        
        # 初始化绘图元素
        self.line, = self.ax.plot([], [], color='#FF6B6B', linewidth=2.5, 
                                   label='心率', antialiased=True, zorder=3)
        self.fill = None  # 填充区域
        self.avg_line = None  # 平均值线
        self.current_hr_text = None  # 当前心率文本
        
        # 标题由外层"实时心率监测"分组框提供, 图内不再重复显示
        # 不设轴标题: 刻度已标明"-60秒/现在"与心率数值, "BPM"单位在"当前/平均"文本中体现;
        # 去掉两侧轴标题后绘图区可以几乎撑满画布, 与下方心率日志框左右边缘对齐
        
        # 优化网格 - 更精细
        self.ax.grid(True, linestyle=':', alpha=0.3, color='#bdc3c7', linewidth=0.8)
        self.ax.set_axisbelow(True)  # 网格在图形下方
        
        # 美化坐标轴
        self.ax.spines['top'].set_visible(False)
        self.ax.spines['right'].set_visible(False)
        self.ax.spines['left'].set_color('#bdc3c7')
        self.ax.spines['bottom'].set_color('#bdc3c7')
        self.ax.tick_params(colors='#5a6c7d', labelsize=9)
        
        # 固定绘图区边距(不用tight_layout, 它会为轴标题预留边距):
        # 左右几乎贴边, 与下方心率日志框左右边缘垂直对齐
        self.figure.subplots_adjust(left=0.045, right=0.995, top=0.97, bottom=0.10)
        
        layout.addWidget(self.canvas)

        # 初始化图表
        self.update_plot()

    def y_axis_left_px(self):
        """Y轴(绘图区左边缘)相对画布左侧的像素偏移, 供外部组件(如心率日志框)对齐"""
        return int(self.ax.get_position().x0 * self.canvas.width())
    
    def add_heart_rate(self, heart_rate):
        """
        添加新的心率数据
        heart_rate>0: 正常数据点
        heart_rate<=0: 断流心跳——屏上还有历史波形时, 每秒滚动一格断点(示波器式),
        波形持续左移直至历史点全部滚出后自动静止(空图不重绘, 节省CPU)
        """
        if heart_rate > 0:  # 只添加有效数据
            # 添加新数据
            self.heart_rates.append(heart_rate)
            # 时间戳使用相对时间
            if len(self.timestamps) > 0:
                self.timestamps.append(self.timestamps[-1] + 1)
            else:
                self.timestamps.append(0)

            # 更新图表
            self.update_plot()
        elif any(v > 0 for v in self.heart_rates):  # 屏上有历史波形才开始滚动
            self.heart_rates.append(0)  # 0=断点, update_plot中以NaN断线
            if len(self.timestamps) > 0:
                self.timestamps.append(self.timestamps[-1] + 1)
            else:
                self.timestamps.append(0)
            self.update_plot()
    
    def update_plot(self):
        """更新波形图"""
        # 准备数据(0/负值=断点, 用NaN断线; matplotlib对NaN不绘制不填充)
        x_data = list(range(len(self.heart_rates)))
        y_data = [v if v > 0 else np.nan for v in self.heart_rates]
        
        # 更新线条数据
        self.line.set_data(x_data, y_data)
        
        # 获取有效数据
        valid_data = [y for y in y_data if y > 0]
        
        # === 清除所有旧的图形元素 ===
        
        # 移除填充区域
        if self.fill:
            self.fill.remove()
            self.fill = None
        
        # 移除平均线
        if self.avg_line:
            self.avg_line.remove()
            self.avg_line = None
        
        # 移除当前心率文本
        if self.current_hr_text:
            self.current_hr_text.remove()
            self.current_hr_text = None
        
        # 清除所有心率区间背景
        for patch in self.zone_patches:
            try:
                patch.remove()
            except (ValueError, AttributeError) as e:
                # 忽略已经移除的元素或属性错误
                pass
            except Exception as e:
                # 记录其他异常但继续执行
                logger.warning(f"移除图形元素时出现异常: {e}")
        self.zone_patches = []
        
        # 动态调整Y轴范围
        if valid_data:
            min_hr = min(valid_data)
            max_hr = max(valid_data)
            avg_hr = sum(valid_data) / len(valid_data)
            current_hr = valid_data[-1] if valid_data else 0
            
            # 添加一些余量
            margin = max((max_hr - min_hr) * 0.25, 15)
            y_min = max(40, min_hr - margin)
            y_max = min(200, max_hr + margin)
            self.ax.set_ylim(y_min, y_max)
            
            # 添加心率区间背景色（并保存到列表中以便清除）
            # 静息区（<60）
            if y_min < 60:
                patch = self.ax.axhspan(y_min, min(60, y_max), alpha=0.1, color='#3498db', zorder=0)
                self.zone_patches.append(patch)
            # 正常区（60-100）
            if y_max > 60:
                patch = self.ax.axhspan(max(60, y_min), min(100, y_max), alpha=0.1, color='#2ecc71', zorder=0)
                self.zone_patches.append(patch)
            # 偏高区（100-120）
            if y_max > 100:
                patch = self.ax.axhspan(max(100, y_min), min(120, y_max), alpha=0.1, color='#f39c12', zorder=0)
                self.zone_patches.append(patch)
            # 很高区（>120）
            if y_max > 120:
                patch = self.ax.axhspan(max(120, y_min), y_max, alpha=0.1, color='#e74c3c', zorder=0)
                self.zone_patches.append(patch)
            
            # 添加填充区域（渐变效果）
            if len(x_data) > 1:
                self.fill = self.ax.fill_between(x_data, y_data, y_min, 
                                                  alpha=0.15, color='#FF6B6B', zorder=1)
            
            # 添加平均值线
            self.avg_line = self.ax.axhline(y=avg_hr, color='#3498db', linestyle='--', 
                                           linewidth=1.5, alpha=0.7, zorder=2,
                                           label=f'平均: {avg_hr:.0f} BPM')
            
            # 显示当前心率值
            self.current_hr_text = self.ax.text(0.98, 0.95, f'当前: {current_hr:.0f} BPM',
                                                transform=self.ax.transAxes,
                                                fontsize=12, fontweight='bold',
                                                color='#FF6B6B',
                                                ha='right', va='top',
                                                bbox=dict(boxstyle='round,pad=0.5', 
                                                         facecolor='white', 
                                                         edgecolor='#FF6B6B',
                                                         alpha=0.9),
                                                zorder=4)
            
            # 更新图例
            self.ax.legend(loc='upper left', fontsize=9, framealpha=0.9)
        else:
            # 无有效数据时的默认范围: 覆盖静息/正常心率区间即可, 避免空图看起来空旷
            self.ax.set_ylim(40, 120)
        
        # 设置X轴范围
        self.ax.set_xlim(0, self.max_points)
        
        # 更新X轴标签（显示最近的时间）
        if len(x_data) > 0:
            # 每10秒显示一个刻度
            step = 10
            ticks = list(range(0, self.max_points + 1, step))
            labels = [f'-{self.max_points - t}秒' if t < self.max_points else '现在' for t in ticks]
            self.ax.set_xticks(ticks)
            self.ax.set_xticklabels(labels, fontsize=9)
        
        # 重绘画布
        self.canvas.draw()
    
    def clear_data(self):
        """清除所有数据"""
        self.heart_rates.clear()
        self.timestamps.clear()
        
        # 重新填充空数据
        for i in range(self.max_points):
            self.heart_rates.append(0)
            self.timestamps.append(i)
        
        self.update_plot()
    
    def get_statistics(self):
        """
        获取心率统计信息
        
        Returns:
            dict: 包含最小值、最大值、平均值的字典
        """
        valid_data = [hr for hr in self.heart_rates if hr > 0]
        
        if not valid_data:
            return {'min': 0, 'max': 0, 'avg': 0, 'count': 0}
        
        return {
            'min': min(valid_data),
            'max': max(valid_data),
            'avg': round(sum(valid_data) / len(valid_data), 1),
            'count': len(valid_data)
        }


