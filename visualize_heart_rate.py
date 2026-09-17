"""
心率数据可视化脚本
使用matplotlib绘制心率趋势图

使用前需要安装matplotlib:
    pip install matplotlib
"""

import os
import csv
import sys
from datetime import datetime
from pathlib import Path

try:
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib import rcParams
    
    # 设置中文字体
    rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'Arial Unicode MS']
    rcParams['axes.unicode_minus'] = False
except ImportError:
    print("错误: 需要安装 matplotlib 库")
    print("请运行: pip install matplotlib")
    sys.exit(1)


def read_heart_rate_log(filepath):
    """
    读取心率日志文件
    
    Args:
        filepath: 日志文件路径
        
    Returns:
        包含时间戳和心率值的列表
    """
    data = []
    try:
        with open(filepath, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    timestamp_str = row['时间戳']
                    heart_rate = int(row['心率值'])
                    timestamp = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                    data.append((timestamp, heart_rate))
                except (ValueError, KeyError) as e:
                    print(f"警告: 跳过无效数据行")
                    continue
    except FileNotFoundError:
        print(f"错误: 找不到文件 {filepath}")
        return None
    except Exception as e:
        print(f"错误: 读取文件失败 - {e}")
        return None
    
    return data


def plot_heart_rate(data, filename):
    """
    绘制心率趋势图
    
    Args:
        data: 心率数据列表
        filename: 文件名（用于图表标题）
    """
    if not data:
        print("错误: 没有数据可绘制")
        return
    
    timestamps = [t for t, _ in data]
    heart_rates = [hr for _, hr in data]
    
    # 计算统计数据
    avg_hr = sum(heart_rates) / len(heart_rates)
    min_hr = min(heart_rates)
    max_hr = max(heart_rates)
    
    # 创建图表
    fig, ax = plt.subplots(figsize=(14, 7))
    
    # 绘制心率曲线
    ax.plot(timestamps, heart_rates, linewidth=1.5, color='#FF6B6B', label='心率')
    
    # 绘制平均值线
    ax.axhline(y=avg_hr, color='#4ECDC4', linestyle='--', linewidth=2, 
               label=f'平均: {avg_hr:.1f} bpm')
    
    # 标记心率区间
    ax.axhspan(60, 100, alpha=0.1, color='green', label='正常区间 (60-100)')
    ax.axhspan(100, 120, alpha=0.1, color='yellow')
    ax.axhspan(120, max(heart_rates), alpha=0.1, color='red')
    
    # 设置标题和标签
    ax.set_title(f'心率趋势图 - {filename}', fontsize=16, fontweight='bold', pad=20)
    ax.set_xlabel('时间', fontsize=12)
    ax.set_ylabel('心率 (bpm)', fontsize=12)
    
    # 格式化x轴时间显示
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=1))
    plt.xticks(rotation=45)
    
    # 添加网格
    ax.grid(True, alpha=0.3, linestyle='--')
    
    # 添加图例
    ax.legend(loc='upper right', fontsize=10)
    
    # 添加统计信息文本框
    stats_text = f'数据点数: {len(heart_rates)}\n'
    stats_text += f'最低心率: {min_hr} bpm\n'
    stats_text += f'最高心率: {max_hr} bpm\n'
    stats_text += f'平均心率: {avg_hr:.1f} bpm'
    
    props = dict(boxstyle='round', facecolor='wheat', alpha=0.5)
    ax.text(0.02, 0.98, stats_text, transform=ax.transAxes, fontsize=10,
            verticalalignment='top', bbox=props)
    
    # 自动调整布局
    plt.tight_layout()
    
    # 显示图表
    plt.show()


def plot_heart_rate_distribution(data, filename):
    """
    绘制心率分布直方图
    
    Args:
        data: 心率数据列表
        filename: 文件名（用于图表标题）
    """
    if not data:
        print("错误: 没有数据可绘制")
        return
    
    heart_rates = [hr for _, hr in data]
    
    # 创建图表
    fig, ax = plt.subplots(figsize=(12, 7))
    
    # 绘制直方图
    n, bins, patches = ax.hist(heart_rates, bins=30, color='#FF6B6B', 
                                alpha=0.7, edgecolor='black')
    
    # 为不同心率区间设置不同颜色
    for i, patch in enumerate(patches):
        if bins[i] < 60:
            patch.set_facecolor('#95E1D3')  # 静息 - 青色
        elif bins[i] < 100:
            patch.set_facecolor('#4ECDC4')  # 正常 - 绿色
        elif bins[i] < 120:
            patch.set_facecolor('#FFE66D')  # 偏高 - 黄色
        else:
            patch.set_facecolor('#FF6B6B')  # 很高 - 红色
    
    # 添加平均值线
    avg_hr = sum(heart_rates) / len(heart_rates)
    ax.axvline(x=avg_hr, color='red', linestyle='--', linewidth=2, 
               label=f'平均: {avg_hr:.1f} bpm')
    
    # 设置标题和标签
    ax.set_title(f'心率分布直方图 - {filename}', fontsize=16, fontweight='bold', pad=20)
    ax.set_xlabel('心率 (bpm)', fontsize=12)
    ax.set_ylabel('频次', fontsize=12)
    
    # 添加网格
    ax.grid(True, alpha=0.3, axis='y', linestyle='--')
    
    # 添加图例
    ax.legend(loc='upper right', fontsize=10)
    
    # 自动调整布局
    plt.tight_layout()
    
    # 显示图表
    plt.show()


def plot_combined(data, filename):
    """
    绘制组合图表（趋势图和分布图）
    
    Args:
        data: 心率数据列表
        filename: 文件名（用于图表标题）
    """
    if not data:
        print("错误: 没有数据可绘制")
        return
    
    timestamps = [t for t, _ in data]
    heart_rates = [hr for _, hr in data]
    
    # 计算统计数据
    avg_hr = sum(heart_rates) / len(heart_rates)
    min_hr = min(heart_rates)
    max_hr = max(heart_rates)
    
    # 创建子图
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10))
    fig.suptitle(f'心率数据分析 - {filename}', fontsize=16, fontweight='bold')
    
    # 第一个子图：趋势图
    ax1.plot(timestamps, heart_rates, linewidth=1.5, color='#FF6B6B', label='心率')
    ax1.axhline(y=avg_hr, color='#4ECDC4', linestyle='--', linewidth=2, 
                label=f'平均: {avg_hr:.1f} bpm')
    ax1.axhspan(60, 100, alpha=0.1, color='green', label='正常区间')
    ax1.set_ylabel('心率 (bpm)', fontsize=12)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    ax1.xaxis.set_major_locator(mdates.HourLocator(interval=1))
    plt.setp(ax1.xaxis.get_majorticklabels(), rotation=45)
    ax1.grid(True, alpha=0.3, linestyle='--')
    ax1.legend(loc='upper right')
    ax1.set_title('心率趋势', fontsize=14, pad=10)
    
    # 第二个子图：分布直方图
    n, bins, patches = ax2.hist(heart_rates, bins=30, color='#FF6B6B', 
                                 alpha=0.7, edgecolor='black')
    
    for i, patch in enumerate(patches):
        if bins[i] < 60:
            patch.set_facecolor('#95E1D3')
        elif bins[i] < 100:
            patch.set_facecolor('#4ECDC4')
        elif bins[i] < 120:
            patch.set_facecolor('#FFE66D')
        else:
            patch.set_facecolor('#FF6B6B')
    
    ax2.axvline(x=avg_hr, color='red', linestyle='--', linewidth=2, 
                label=f'平均: {avg_hr:.1f} bpm')
    ax2.set_xlabel('心率 (bpm)', fontsize=12)
    ax2.set_ylabel('频次', fontsize=12)
    ax2.grid(True, alpha=0.3, axis='y', linestyle='--')
    ax2.legend(loc='upper right')
    ax2.set_title('心率分布', fontsize=14, pad=10)
    
    # 添加统计信息
    stats_text = f'统计信息:\n'
    stats_text += f'数据点数: {len(heart_rates)}\n'
    stats_text += f'时间跨度: {timestamps[0].strftime("%H:%M")} - {timestamps[-1].strftime("%H:%M")}\n'
    stats_text += f'最低心率: {min_hr} bpm\n'
    stats_text += f'最高心率: {max_hr} bpm\n'
    stats_text += f'平均心率: {avg_hr:.1f} bpm'
    
    props = dict(boxstyle='round', facecolor='wheat', alpha=0.5)
    ax1.text(0.02, 0.98, stats_text, transform=ax1.transAxes, fontsize=10,
             verticalalignment='top', bbox=props)
    
    # 自动调整布局
    plt.tight_layout()
    
    # 显示图表
    plt.show()


def main():
    """主函数"""
    if len(sys.argv) == 1:
        # 没有参数，使用最新日志
        log_dir = Path("log")
        if not log_dir.exists():
            print("错误: log 目录不存在")
            return
        
        log_files = list(log_dir.glob("heart_rate_*.csv"))
        if not log_files:
            print("错误: 没有找到心率日志文件")
            return
        
        log_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        log_file = log_files[0]
        
    elif sys.argv[1] == "--help" or sys.argv[1] == "-h":
        print("\n心率数据可视化工具")
        print("="*60)
        print("\n用法:")
        print("  python visualize_heart_rate.py                  # 可视化最新的日志文件")
        print("  python visualize_heart_rate.py YYYY-MM-DD       # 可视化指定日期的日志")
        print("  python visualize_heart_rate.py -t YYYY-MM-DD    # 仅显示趋势图")
        print("  python visualize_heart_rate.py -d YYYY-MM-DD    # 仅显示分布图")
        print("  python visualize_heart_rate.py --help           # 显示此帮助信息")
        print("\n示例:")
        print("  python visualize_heart_rate.py 2025-10-30")
        print("  python visualize_heart_rate.py -t 2025-10-30")
        return
        
    else:
        date_str = sys.argv[-1]  # 获取最后一个参数作为日期
        log_file = Path(f"log/heart_rate_{date_str}.csv")
        
        if not log_file.exists():
            print(f"错误: 找不到日期 {date_str} 的日志文件")
            return
    
    print(f"正在读取: {log_file}")
    data = read_heart_rate_log(log_file)
    
    if not data:
        return
    
    print(f"成功读取 {len(data)} 条记录")
    
    # 根据命令行参数决定绘制哪种图表
    if len(sys.argv) >= 2 and sys.argv[1] == "-t":
        plot_heart_rate(data, log_file.name)
    elif len(sys.argv) >= 2 and sys.argv[1] == "-d":
        plot_heart_rate_distribution(data, log_file.name)
    else:
        plot_combined(data, log_file.name)


if __name__ == "__main__":
    main()

