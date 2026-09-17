"""
心率数据分析示例脚本
用于分析log目录下的心率日志文件
"""

import os
import csv
import sys
from datetime import datetime, timedelta
from pathlib import Path


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
                    print(f"警告: 跳过无效数据行 - {e}")
                    continue
    except FileNotFoundError:
        print(f"错误: 找不到文件 {filepath}")
        return None
    except Exception as e:
        print(f"错误: 读取文件失败 - {e}")
        return None
    
    return data


def calculate_statistics(data):
    """
    计算心率统计信息
    
    Args:
        data: 心率数据列表
        
    Returns:
        统计信息字典
    """
    if not data:
        return None
    
    heart_rates = [hr for _, hr in data]
    
    stats = {
        '数据点数': len(heart_rates),
        '最低心率': min(heart_rates),
        '最高心率': max(heart_rates),
        '平均心率': round(sum(heart_rates) / len(heart_rates), 1),
        '心率中位数': sorted(heart_rates)[len(heart_rates) // 2],
    }
    
    # 计算心率区间分布
    zones = {
        '静息 (<60)': 0,
        '正常 (60-100)': 0,
        '偏高 (100-120)': 0,
        '很高 (>120)': 0
    }
    
    for hr in heart_rates:
        if hr < 60:
            zones['静息 (<60)'] += 1
        elif hr <= 100:
            zones['正常 (60-100)'] += 1
        elif hr <= 120:
            zones['偏高 (100-120)'] += 1
        else:
            zones['很高 (>120)'] += 1
    
    stats['心率区间分布'] = zones
    
    return stats


def print_statistics(stats, filename):
    """
    打印统计信息
    
    Args:
        stats: 统计信息字典
        filename: 文件名
    """
    print(f"\n{'='*60}")
    print(f"文件: {filename}")
    print(f"{'='*60}")
    print(f"数据点数: {stats['数据点数']}")
    print(f"最低心率: {stats['最低心率']} bpm")
    print(f"最高心率: {stats['最高心率']} bpm")
    print(f"平均心率: {stats['平均心率']} bpm")
    print(f"心率中位数: {stats['心率中位数']} bpm")
    print(f"\n心率区间分布:")
    
    total = stats['数据点数']
    for zone, count in stats['心率区间分布'].items():
        percentage = (count / total * 100) if total > 0 else 0
        bar = '█' * int(percentage / 2)  # 每个█代表2%
        print(f"  {zone:15s}: {count:6d} ({percentage:5.1f}%) {bar}")


def analyze_latest_log():
    """分析最新的日志文件"""
    log_dir = Path("log")
    
    if not log_dir.exists():
        print("错误: log 目录不存在")
        return
    
    # 查找所有心率日志文件
    log_files = list(log_dir.glob("heart_rate_*.csv"))
    
    if not log_files:
        print("错误: 没有找到心率日志文件")
        return
    
    # 按修改时间排序，获取最新的文件
    log_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    latest_file = log_files[0]
    
    print(f"正在分析: {latest_file}")
    
    data = read_heart_rate_log(latest_file)
    
    if not data:
        print("错误: 无法读取数据")
        return
    
    stats = calculate_statistics(data)
    
    if stats:
        print_statistics(stats, latest_file.name)
        
        # 显示时间范围
        if data:
            start_time = data[0][0]
            end_time = data[-1][0]
            duration = end_time - start_time
            print(f"\n时间范围:")
            print(f"  开始: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"  结束: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"  持续: {duration}")


def analyze_specific_date(date_str):
    """
    分析特定日期的日志
    
    Args:
        date_str: 日期字符串，格式为 YYYY-MM-DD
    """
    log_file = Path(f"log/heart_rate_{date_str}.csv")
    
    if not log_file.exists():
        print(f"错误: 找不到日期 {date_str} 的日志文件")
        return
    
    print(f"正在分析: {log_file}")
    
    data = read_heart_rate_log(log_file)
    
    if not data:
        print("错误: 无法读取数据")
        return
    
    stats = calculate_statistics(data)
    
    if stats:
        print_statistics(stats, log_file.name)
        
        # 显示时间范围
        if data:
            start_time = data[0][0]
            end_time = data[-1][0]
            duration = end_time - start_time
            print(f"\n时间范围:")
            print(f"  开始: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"  结束: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"  持续: {duration}")


def list_all_logs():
    """列出所有日志文件"""
    log_dir = Path("log")
    
    if not log_dir.exists():
        print("错误: log 目录不存在")
        return
    
    log_files = sorted(log_dir.glob("heart_rate_*.csv"))
    
    if not log_files:
        print("没有找到心率日志文件")
        return
    
    print(f"\n找到 {len(log_files)} 个心率日志文件:")
    print(f"{'='*60}")
    
    for log_file in log_files:
        # 从文件名提取日期
        filename = log_file.name
        date_part = filename.replace("heart_rate_", "").replace(".csv", "")
        
        # 读取文件统计基本信息
        try:
            with open(log_file, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                count = sum(1 for _ in reader)
                file_size = log_file.stat().st_size
                
            print(f"{date_part:12s} - {count:6d} 条记录 - {file_size:8d} 字节")
        except Exception as e:
            print(f"{date_part:12s} - 读取失败: {e}")


def main():
    """主函数"""
    print("="*60)
    print("心率数据分析工具")
    print("="*60)
    
    if len(sys.argv) == 1:
        # 没有参数，分析最新日志
        analyze_latest_log()
    elif sys.argv[1] == "--list" or sys.argv[1] == "-l":
        # 列出所有日志
        list_all_logs()
    elif sys.argv[1] == "--help" or sys.argv[1] == "-h":
        # 显示帮助
        print("\n用法:")
        print("  python analyze_heart_rate.py              # 分析最新的日志文件")
        print("  python analyze_heart_rate.py YYYY-MM-DD   # 分析指定日期的日志")
        print("  python analyze_heart_rate.py --list       # 列出所有日志文件")
        print("  python analyze_heart_rate.py --help       # 显示此帮助信息")
        print("\n示例:")
        print("  python analyze_heart_rate.py 2025-10-30")
    else:
        # 分析指定日期
        date_str = sys.argv[1]
        analyze_specific_date(date_str)


if __name__ == "__main__":
    main()

