#!/usr/bin/env python3
"""
磁盘和ZFS日志摘要生成器
从 disklog.txt 和 zpoollog.txt 中提取事件标头
合并后按时间排序，通过时间戳避免重复生成
"""

import re
import os
from datetime import datetime

DISK_LOG_FILE = "/var/log/disklog.txt"
ZPOOL_LOG_FILE = "/var/log/zpoollog.txt"
SUMMARY_FILE = "/var/log/disk-zfs-summary.txt"

# 事件标头正则: [时间戳] 内容
EVENT_PATTERN = re.compile(r'^\[[\d/: ]+\]\s+.+')
# 提取时间戳
TIMESTAMP_PATTERN = re.compile(r'^\[([\d/: ]+)\]')


def parse_timestamp(ts_str: str) -> datetime:
    """解析时间戳字符串为datetime对象"""
    # 格式1: 2026/01/15 14:41:25
    # 格式2: 2026/01/15/14:45
    ts_str = ts_str.strip()

    # 尝试格式1
    try:
        return datetime.strptime(ts_str, '%Y/%m/%d %H:%M:%S')
    except ValueError:
        pass

    # 尝试格式2
    try:
        return datetime.strptime(ts_str, '%Y/%m/%d/%H:%M')
    except ValueError:
        pass

    # 返回最小时间作为fallback
    return datetime.min


def extract_events(log_file: str) -> list[str]:
    """从日志文件中提取事件标头行"""
    events = []
    if not os.path.exists(log_file):
        return events

    try:
        with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.rstrip('\n')
                if EVENT_PATTERN.match(line):
                    events.append(line)
    except Exception:
        pass

    return events


def get_timestamp_from_line(line: str) -> str:
    """从事件行提取时间戳字符串"""
    match = TIMESTAMP_PATTERN.match(line)
    return match.group(1) if match else ""


def sort_events_by_time(events: list[str]) -> list[str]:
    """按时间戳排序事件"""
    def sort_key(line):
        ts_str = get_timestamp_from_line(line)
        return parse_timestamp(ts_str)

    return sorted(events, key=sort_key)


def get_last_timestamp(lines: list[str]) -> str:
    """获取最后一条事件的时间戳"""
    for line in reversed(lines):
        ts = get_timestamp_from_line(line)
        if ts:
            return ts
    return ""


def get_summary_last_timestamp() -> str:
    """获取摘要文件最后一条事件的时间戳"""
    if not os.path.exists(SUMMARY_FILE):
        return ""

    try:
        with open(SUMMARY_FILE, 'r', encoding='utf-8', errors='ignore') as f:
            lines = [l.rstrip('\n') for l in f.readlines()]
            return get_last_timestamp(lines)
    except Exception:
        return ""


def generate_summary():
    """生成摘要文件"""
    # 提取所有事件
    all_events = extract_events(DISK_LOG_FILE) + extract_events(ZPOOL_LOG_FILE)

    if not all_events:
        return

    # 按时间排序
    all_events = sort_events_by_time(all_events)

    # 比较最后事件时间戳
    new_last_ts = get_last_timestamp(all_events)
    old_last_ts = get_summary_last_timestamp()

    if new_last_ts and new_last_ts == old_last_ts:
        return  # 时间戳相同，无需更新

    # 写入摘要文件
    try:
        with open(SUMMARY_FILE, 'w', encoding='utf-8') as f:
            f.write('\n'.join(all_events))
            f.write('\n')
    except Exception:
        pass


if __name__ == "__main__":
    generate_summary()
