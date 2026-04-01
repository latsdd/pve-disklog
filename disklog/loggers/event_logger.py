"""
事件日志模块 - 记录磁盘和ZFS事件（完整版）
包含历史记录、详细事件、循环更新
"""

import os
import subprocess
from datetime import datetime
from typing import Dict, List


class EventLogger:
    """事件日志记录（完整实现）"""

    def __init__(self, config, sys_cmd=None):
        self.config = config
        self.sys_cmd = sys_cmd  # 新增：用于调用zpool命令
        self.disk_last_history = None  # 上次历史记录缓存
        self._ensure_log_files()

    def _ensure_log_files(self):
        """确保日志文件存在（仅检查，不创建）"""
        log_dir = os.path.dirname(self.config.DISK_LOG_FILE)
        os.makedirs(log_dir, exist_ok=True)

        # 检查日志文件是否存在
        if not os.path.exists(self.config.DISK_LOG_FILE):
            print(f"警告: 日志文件不存在: {self.config.DISK_LOG_FILE}")
            print("请先运行安装脚本初始化日志文件")

        if not os.path.exists(self.config.ZPOOL_LOG_FILE):
            print(f"警告: 日志文件不存在: {self.config.ZPOOL_LOG_FILE}")
            print("请先运行安装脚本初始化日志文件")


    def log_disk_events(self, changes: Dict, disk_monitor):
        """
        【已废弃】记录磁盘事件（完整格式）

        此方法已被 log_disk_events_v2() 替代，不再使用。
        保留此方法仅供参考，请使用 log_disk_events_v2() 代替。

        仅在检测到变化时触发更新

        格式:
        === lsblk 历史（最近N次）===
        [历史记录快照]

        ====================================
        [事件记录]
        [时间] 磁盘插入/拔出: sda
        --- lsblk 输出 ---
        [详细设备信息]
        """
        if not changes.get('has_changes'):
            return  # 无变化不记录

        try:
            timestamp = datetime.now().strftime(self.config.LOG_TIME_FORMAT)

            # 确定变化类型
            if changes['added']:
                change_type = "磁盘插入"
                changed_device = ', '.join(changes['added'])
            elif changes['removed']:
                change_type = "磁盘拔出"
                changed_device = ', '.join(changes['removed'])
            else:
                change_type = "设备列表更新"
                changed_device = ""

            # 读取现有日志
            event_content = ""
            history_list = []

            if os.path.exists(self.config.DISK_LOG_FILE):
                with open(self.config.DISK_LOG_FILE, 'r', encoding='utf-8') as f:
                    content = f.read()

                # 分离历史记录和事件记录
                if '=' * 50 in content:
                    parts = content.split('=' * 50, 1)
                    if len(parts) == 2:
                        event_content = parts[1].strip() + "\n\n"
                        # 解析历史记录
                        history_list = self._parse_history(parts[0])

            # 生成新的历史记录快照
            current_snapshot = self._generate_lsblk_snapshot()
            if current_snapshot:
                history_list.append({
                    'timestamp': timestamp,
                    'content': current_snapshot
                })
                # 保留最近N条
                if len(history_list) > self.config.DISK_HISTORY_ENTRIES:
                    history_list = history_list[-self.config.DISK_HISTORY_ENTRIES:]

            # 生成详细事件记录
            event_entry = f"[{timestamp}] {change_type}"
            if changed_device:
                event_entry += f": {changed_device}"
            event_entry += "\n"

            # 添加详细设备信息
            detailed_info = self._get_detailed_lsblk()
            if detailed_info:
                event_entry += "--- lsblk 输出 ---\n"
                event_entry += "# 获取命令：lsblk -o NAME,MODEL,SERIAL,SIZE,TYPE,MOUNTPOINT,FSTYPE,WWN -n\n"
                event_entry += "NAME   MODEL              SERIAL              SIZE TYPE MOUNTPOINT   FSTYPE   WWN\n"
                event_entry += detailed_info + "\n"

            # 重新组装日志文件
            with open(self.config.DISK_LOG_FILE, 'w', encoding='utf-8') as f:
                # 写入历史记录区
                if history_list:
                    f.write(f"=== lsblk 历史（最近{len(history_list)}次）===\n\n")
                    for hist in history_list:
                        f.write(f"--- 历史记录 [{hist['timestamp']}] ---\n")
                        f.write("# 获取命令：lsblk -o NAME,SIZE,TYPE,MOUNTPOINT -n\n")
                        f.write("NAME      SIZE TYPE MOUNTPOINT\n")
                        f.write(hist['content'] + "\n")
                    f.write("\n" + "="*50 + "\n\n")

                # 写入事件记录区
                f.write(event_content)
                f.write(event_entry)

            # 触发分析脚本
            self._trigger_analyze_script()

        except Exception as e:
            print(f"记录磁盘事件失败: {e}")

    def _generate_lsblk_snapshot(self) -> str:
        """【已废弃】生成lsblk快照（简化版）- 使用列格式，已由 disk_monitor._get_simple_snapshot() 替代"""
        try:
            result = subprocess.run(
                ['lsblk', '-o', 'NAME,SIZE,TYPE,MOUNTPOINT', '-n'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10
            )

            if result.returncode == 0:
                # 过滤物理设备
                lines = []
                for line in result.stdout.strip().split('\n'):
                    if not line.strip():
                        continue
                    # 排除虚拟设备
                    if any(line.startswith(prefix) for prefix in ['loop', 'ram', 'dm-', 'zram', 'zd']):
                        continue
                    if 'disk' in line or 'part' in line:
                        lines.append(line)
                return '\n'.join(lines)
        except Exception as e:
            print(f"生成lsblk快照失败: {e}")

        return ""

    def _get_detailed_lsblk(self) -> str:
        """【已废弃】获取详细lsblk信息 - 使用列格式，已由 disk_monitor._get_detailed_devices() 替代"""
        try:
            result = subprocess.run(
                ['lsblk', '-o', 'NAME,MODEL,SERIAL,SIZE,TYPE,MOUNTPOINT,FSTYPE,WWN', '-n'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10
            )

            if result.returncode == 0:
                lines = []
                for line in result.stdout.strip().split('\n'):
                    if not line.strip():
                        continue
                    if any(line.startswith(prefix) for prefix in ['loop', 'ram', 'dm-', 'zram', 'zd']):
                        continue
                    if 'disk' in line:
                        lines.append(line)
                return '\n'.join(lines)
        except Exception as e:
            print(f"获取详细lsblk失败: {e}")

        return ""

    def _parse_history(self, history_section: str) -> List[Dict]:
        """解析历史记录区"""
        history_list = []
        lines = history_section.split('\n')

        current_entry = None
        for line in lines:
            if line.startswith('--- 历史记录 ['):
                # 提取时间戳
                start = line.index('[') + 1
                end = line.index(']', start)
                timestamp = line[start:end]
                current_entry = {'timestamp': timestamp, 'content': ''}
            elif current_entry and line and not line.startswith('#') and not line.startswith('==='):
                if line.strip():
                    current_entry['content'] += line + '\n'
            elif line.startswith('===') or line.startswith('='*50):
                if current_entry and current_entry['content']:
                    current_entry['content'] = current_entry['content'].strip()
                    history_list.append(current_entry)
                current_entry = None

        if current_entry and current_entry['content']:
            current_entry['content'] = current_entry['content'].strip()
            history_list.append(current_entry)

        return history_list

    def _generate_zpool_snapshot(self) -> str:
        """生成zpool list快照(精简版)"""
        try:
            result = subprocess.run(
                ['zpool', 'list', '-H', '-o', 'name,size,allocated,health'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                return result.stdout.strip()  # tab分隔的输出
        except Exception as e:
            print(f"生成zpool快照失败: {e}")
        return ""

    def _parse_zfs_history(self, history_section: str) -> List[Dict]:
        """解析ZFS历史记录区"""
        history_list = []
        lines = history_section.split('\n')

        current_entry = None
        for line in lines:
            if line.startswith('--- 历史记录 ['):
                # 提取时间戳
                start = line.index('[') + 1
                end = line.index(']', start)
                timestamp = line[start:end]
                current_entry = {'timestamp': timestamp, 'content': ''}
            elif current_entry and line and not line.startswith('#') and not line.startswith('==='):
                if line.strip():
                    current_entry['content'] += line + '\n'
            elif line.startswith('===') or line.startswith('='*50):
                if current_entry and current_entry['content']:
                    current_entry['content'] = current_entry['content'].strip()
                    history_list.append(current_entry)
                current_entry = None

        if current_entry and current_entry['content']:
            current_entry['content'] = current_entry['content'].strip()
            history_list.append(current_entry)

        return history_list

    def log_zfs_events(self, changes: Dict):
        """
        记录ZFS事件（双日志方案）

        参数:
            changes: {
                'has_changes': bool,
                'created': ['tank'],
                'destroyed': ['backup'],
                'health_changed': [{'pool': 'rrr', 'old': 'ONLINE', 'new': 'DEGRADED'}],
                'current_pools': {...}
            }
        """
        # 只有变化才记录（创建/销毁/健康变化）
        if not changes.get('has_changes'):
            return

        try:
            timestamp = datetime.now().strftime(self.config.LOG_TIME_FORMAT)

            # 1. 读取现有日志，分离历史区和事件区
            event_content = ""
            history_list = []

            if os.path.exists(self.config.ZPOOL_LOG_FILE):
                with open(self.config.ZPOOL_LOG_FILE, 'r', encoding='utf-8') as f:
                    content = f.read()

                if '=' * 50 in content:
                    parts = content.split('=' * 50, 1)
                    if len(parts) == 2:
                        event_content = parts[1].strip() + "\n\n"
                        history_list = self._parse_zfs_history(parts[0])

            # 2. 添加新的历史快照（zpool list输出）
            snapshot = self._generate_zpool_snapshot()
            if snapshot:
                history_list.append({
                    'timestamp': timestamp,
                    'content': snapshot
                })
                # 保留最近N条
                if len(history_list) > self.config.ZFS_HISTORY_ENTRIES:
                    history_list = history_list[-self.config.ZFS_HISTORY_ENTRIES:]

            # 3. 生成事件描述
            event_lines = []
            pools_to_status = set()  # 需要获取status的池

            # 池创建
            for pool in changes.get('created', []):
                event_lines.append(f"ZFS池创建: {pool}")
                pools_to_status.add(pool)

            # 池销毁
            for pool in changes.get('destroyed', []):
                event_lines.append(f"ZFS池销毁: {pool}")
                # 销毁的池无法获取status，跳过

            # 健康状态变化
            for change in changes.get('health_changed', []):
                pool = change['pool']
                old = change['old']
                new = change['new']
                event_lines.append(f"HEALTH状态变化: {pool} {old} → {new}")
                pools_to_status.add(pool)

            event_summary = ', '.join(event_lines) if event_lines else "ZFS状态更新"

            # 4. 组装事件记录
            event_entry = f"[{timestamp}] {event_summary}\n"

            # 添加详细zpool status输出
            if pools_to_status and self.sys_cmd:
                event_entry += "--- zpool status ---\n"
                for pool in sorted(pools_to_status):
                    # 通过system_interface获取status
                    status_output = self.sys_cmd.run_zpool_status(pool)
                    if status_output:
                        event_entry += status_output + "\n"

            event_entry += "\n"

            # 5. 重新组装完整日志
            with open(self.config.ZPOOL_LOG_FILE, 'w', encoding='utf-8') as f:
                # 写入历史记录区
                f.write(f"=== zpool list 历史（最近{len(history_list)}次）===\n\n")
                for hist in history_list:
                    f.write(f"--- 历史记录 [{hist['timestamp']}] ---\n")
                    f.write("# 获取命令：zpool list -H -o name,size,allocated,health\n")
                    f.write(hist['content'] + "\n\n")

                # 写入分隔线
                f.write("=" * 50 + "\n\n")

                # 写入事件记录区
                f.write(event_content)
                f.write(event_entry)

            print(f"ZFS日志已写入: {event_summary}")

            # 触发分析脚本
            self._trigger_analyze_script()

        except Exception as e:
            print(f"写入ZFS日志失败: {e}")

    def log_temp_alert(self, device: str, temp: float, threshold: float):
        """记录温度告警"""
        timestamp = datetime.now().strftime(self.config.LOG_TIME_FORMAT)
        line = f"[{timestamp}] 温度告警: {device} 温度 {temp}°C 超过阈值 {threshold}°C\n"
        self._append_to_file(self.config.DISK_LOG_FILE, [line])

    def _append_to_file(self, filepath: str, lines: List[str]):
        """追加写入日志文件"""
        try:
            with open(filepath, 'a', encoding='utf-8') as f:
                f.writelines(lines)
                f.flush()
        except (IOError, PermissionError) as e:
            print(f"写入日志文件失败 {filepath}: {e}")

    def _trigger_analyze_script(self):
        """触发日志分析脚本（后台运行）"""
        try:
            if os.path.exists(self.config.ANALYZE_SCRIPT):
                subprocess.Popen(
                    ['python3', self.config.ANALYZE_SCRIPT],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
        except Exception as e:
            print(f"触发分析脚本失败: {e}")

    def log_disk_events_v2(self, changes: Dict, current_devices: Dict,
                           simple_snapshot: str, reason: str):
        """
        记录磁盘事件（v2双日志方案）

        参数:
            changes: 变化信息 {'has_changes', 'added', 'removed', 'name_changed'}
            current_devices: 当前所有设备详细信息（按序列号索引）
            simple_snapshot: 精简快照字符串
            reason: 触发原因

        注意：
            - 只要被调用，就更新循环历史区（simple_snapshot → c）
            - 只有在has_changes=True时，才追加到永久事件区
        """
        try:
            timestamp = datetime.now().strftime(self.config.LOG_TIME_FORMAT)

            # 读取现有日志
            event_content = ""
            history_list = []

            if os.path.exists(self.config.DISK_LOG_FILE):
                with open(self.config.DISK_LOG_FILE, 'r', encoding='utf-8') as f:
                    content = f.read()

                # 分离历史记录和事件记录
                if '=' * 50 in content:
                    parts = content.split('=' * 50, 1)
                    if len(parts) == 2:
                        event_content = parts[1].strip() + "\n\n"
                        history_list = self._parse_history(parts[0])

            # 添加新的历史快照（精简版）
            if simple_snapshot:
                history_list.append({
                    'timestamp': timestamp,
                    'content': simple_snapshot
                })
                # 保留最近N条
                if len(history_list) > self.config.DISK_HISTORY_ENTRIES:
                    history_list = history_list[-self.config.DISK_HISTORY_ENTRIES:]

            # 生成永久事件（只在有变化时）
            event_entry = ""
            event_summary = ""

            if changes.get('has_changes'):
                # 描述变化
                event_lines = []

                if changes['added']:
                    for dev in changes['added']:
                        desc = f"磁盘插入: {dev['name']}"
                        if dev.get('serial'):
                            desc += f" ({dev['model']}##{dev['serial']})"
                        event_lines.append(desc)

                if changes['removed']:
                    for dev in changes['removed']:
                        desc = f"磁盘拔出: {dev['name']}"
                        if dev.get('serial'):
                            desc += f" ({dev['model']}##{dev['serial']})"
                        event_lines.append(desc)

                if changes.get('name_changed'):
                    for change in changes['name_changed']:
                        event_lines.append(
                            f"设备名变化: {change['old_name']} → {change['new_name']} "
                            f"({change.get('model', 'Unknown')})"
                        )

                event_summary = ', '.join(event_lines) if event_lines else "设备列表更新"

                # 组装事件记录
                event_entry = f"[{timestamp}] {event_summary}\n"
                if reason and reason != "设备变化检测":
                    event_entry += f"# 触发原因: {reason}\n"

                # 添加详细lsblk输出（-P格式，方便解析）
                event_entry += "--- lsblk 输出 ---\n"
                event_entry += "# 获取命令：lsblk -o NAME,MODEL,SERIAL,SIZE,TYPE,MOUNTPOINT,FSTYPE,WWN -P -n\n"

                for dev in sorted(current_devices.values(), key=lambda x: x['name']):
                    # 使用 -P 格式（键值对），方便后续解析
                    event_entry += f'NAME="{dev["name"]}" MODEL="{dev["model"]}" SERIAL="{dev["serial"]}" ' \
                                  f'SIZE="{dev["size"]}" TYPE="{dev["type"]}" MOUNTPOINT="{dev["mountpoint"]}" ' \
                                  f'FSTYPE="{dev["fstype"]}" WWN="{dev["wwn"]}"\n'

                event_entry += "\n"

            # 重新组装完整日志
            with open(self.config.DISK_LOG_FILE, 'w', encoding='utf-8') as f:
                # 写入历史记录区（始终更新）
                f.write(f"=== lsblk 历史（最近{len(history_list)}次）===\n\n")
                for hist in history_list:
                    f.write(f"--- 历史记录 [{hist['timestamp']}] ---\n")
                    f.write("# 获取命令：lsblk -o NAME,SIZE,TYPE -P -n\n")
                    f.write(hist['content'] + "\n\n")

                # 写入分隔线
                f.write("=" * 50 + "\n\n")

                # 写入事件记录区（只在有变化时追加）
                f.write(event_content)
                if event_entry:
                    f.write(event_entry)

            if event_summary:
                print(f"日志已写入: {event_summary}")
            else:
                print("循环历史已更新（无设备变化）")

            # 触发分析脚本
            self._trigger_analyze_script()

        except Exception as e:
            print(f"写入日志失败: {e}")
