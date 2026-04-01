"""
磁盘监控模块 v2 - 双日志触发检测
使用精简快照触发 + 详细信息对比的方案
"""

import os
import subprocess
import time
from typing import Dict, Set, List, Optional

from ..utils import slot_utils


class DiskMonitor:
    """磁盘设备监控（双日志方案）"""

    def __init__(self, config, event_logger):
        self.config = config
        self.event_logger = event_logger
        self.last_simple_snapshot = ""  # 上次精简快照（字符串）
        self.last_devices_by_serial = {}  # 上次设备映射（序列号->设备信息）
        self.last_simple_devices_info = {}  # 缓存NAME/SIZE映射（每5秒更新）
        self.initialized = False

    def startup_check(self):
        """启动时的强制检测（60秒延迟后执行）"""
        print("等待设备稳定...")
        time.sleep(self.config.STARTUP_DELAY)

        print("初始化设备缓存...")

        # 1. 从日志读取循环历史 → 缓存c（用于后续与a对比）
        self.last_simple_snapshot = self._load_last_simple_snapshot_from_log()

        # 2. 从日志读取最新永久日志 → 缓存bb（用于后续与dd对比）
        self.last_devices_by_serial = self._load_last_state_from_log()

        # 3. 启动时不获取a、不对比、不触发事件、不执行 dd → bb
        # 缓存a会在运行时循环中定时刷新
        print(f"缓存初始化完成:")
        print(f"  - 缓存bb: {len(self.last_devices_by_serial)} 个设备（从日志读取）")
        print(f"  - 缓存c: {len(self.last_simple_snapshot)} 字符（从日志读取）")

        # 4. 更新插槽映射（启动时生成 slot_mapping.json）
        if slot_utils.update_slot_mapping():
            print("插槽映射初始化完成")
        else:
            print("未检测到SAS背板，插槽功能不可用")

        self.initialized = True

    def check_changes(self) -> bool:
        """检查是否有变化（精简快照触发）

        返回: True=有变化并已处理, False=无变化
        """
        if not self.initialized:
            return False

        # 1. 获取当前精简快照
        current_simple = self._get_simple_snapshot()

        # 2. 字符串对比
        if current_simple == self.last_simple_snapshot:
            return False  # 无变化，跳过

        print("精简快照检测到变化，触发设备改变事件...")

        # 3. 步骤1：a → c（更新缓存c）
        self.last_simple_snapshot = current_simple

        # 4. 步骤2：执行详细检测 → dd（临时缓存）
        dd = self._get_detailed_devices()

        # 5. dd与bb对比（按 model+serial，检测插入/拔出/名称变化）
        changes = self._compare_by_serial(
            self.last_devices_by_serial,  # bb
            dd
        )

        # 6. dd → bb（内存覆盖）
        self.last_devices_by_serial = dd

        # 7. 缓存bb写入永久日志（c同时写入循环历史）
        # 注意：即使dd与bb对比无变化，也要更新循环历史区
        self._write_log(changes, self.last_devices_by_serial, reason="设备变化检测")

        # 8. 清空 dd（完成使命，显式清空表达设计意图）
        dd = None

        # 9. 如果有设备变化，更新插槽映射
        if changes['has_changes']:
            slot_utils.update_slot_mapping()

        return changes['has_changes']

    def _get_simple_snapshot(self) -> str:
        """获取精简快照（用于触发检测）同时缓存设备SIZE信息

        返回: lsblk 输出字符串（-P格式，仅disk类型）
        """
        import re
        try:
            result = subprocess.run(
                ['lsblk', '-o', 'NAME,SIZE,TYPE', '-P', '-n'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10
            )

            if result.returncode == 0:
                # 过滤出 disk 类型设备
                lines = []
                devices_info = {}  # 临时字典存储解析结果
                pattern = r'(\w+)="([^"]*)"'

                for line in result.stdout.strip().split('\n'):
                    if not line.strip():
                        continue

                    # 解析键值对
                    fields = dict(re.findall(pattern, line))

                    # 检查是否为 disk 类型
                    if fields.get('TYPE') not in ['disk', 'rom']:
                        continue

                    # 排除虚拟设备
                    name = fields.get('NAME', '')
                    if any(name.startswith(prefix) for prefix in
                           ['loop', 'ram', 'dm-', 'zram', 'zd']):
                        continue

                    lines.append(line)

                    # 缓存设备SIZE信息
                    if name:
                        devices_info[name] = fields.get('SIZE', '')

                # 更新缓存
                self.last_simple_devices_info = devices_info

                return '\n'.join(lines)

        except Exception as e:
            print(f"获取精简快照失败: {e}")

        return ""

    def _get_detailed_devices(self) -> Dict[str, Dict]:
        """获取详细设备信息（按序列号索引）

        返回: {
            'Samsung##S5H2NS0N123456': {
                'name': 'sda',
                'model': 'Samsung SSD 970',
                'serial': 'S5H2NS0N123456',
                'size': '1.8T',
                'type': 'disk',
                'mountpoint': '',
                'fstype': 'ext4',
                'wwn': '0x50014...'
            }
        }
        """
        import re
        devices = {}

        try:
            # 使用 -P 格式输出（键值对，易于解析）
            result = subprocess.run(
                ['lsblk', '-o', 'NAME,MODEL,SERIAL,SIZE,TYPE,MOUNTPOINT,FSTYPE,WWN', '-P', '-n'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10
            )

            if result.returncode == 0:
                # 正则提取键值对
                pattern = r'(\w+)="([^"]*)"'

                for line in result.stdout.strip().split('\n'):
                    if not line.strip():
                        continue

                    # 解析键值对
                    fields = dict(re.findall(pattern, line))

                    if not fields:
                        continue

                    name = fields.get('NAME', '')
                    device_type = fields.get('TYPE', '')

                    # 排除虚拟设备
                    if any(name.startswith(prefix) for prefix in
                           ['loop', 'ram', 'dm-', 'zram', 'zd']):
                        continue

                    # 只处理 disk 类型
                    if device_type != 'disk':
                        continue

                    model = fields.get('MODEL', '')
                    serial = fields.get('SERIAL', '')
                    size = fields.get('SIZE', '')
                    mountpoint = fields.get('MOUNTPOINT', '')
                    fstype = fields.get('FSTYPE', '')
                    wwn = fields.get('WWN', '')

                    # 生成唯一标识（优先使用序列号）
                    if serial:
                        key = f"{model}##{serial}"
                    elif wwn:
                        key = f"{model}##{wwn}"
                    else:
                        # 没有序列号的设备（如虚拟磁盘），用设备名
                        key = f"NOSERIAL##{name}"

                    devices[key] = {
                        'name': name,
                        'model': model,
                        'serial': serial,
                        'size': size,
                        'type': device_type,
                        'mountpoint': mountpoint,
                        'fstype': fstype,
                        'wwn': wwn,
                        'identity_key': key
                    }

        except Exception as e:
            print(f"获取详细设备信息失败: {e}")

        return devices

    def _compare_by_serial(self, old: Dict, new: Dict) -> Dict:
        """按序列号对比设备变化

        参数:
            old: 旧设备映射（序列号->设备信息）
            new: 新设备映射（序列号->设备信息）

        返回: {
            'has_changes': bool,
            'added': [设备信息],
            'removed': [设备信息],
            'name_changed': [{key, old_name, new_name}]
        }
        """
        old_keys = set(old.keys())
        new_keys = set(new.keys())

        added_keys = new_keys - old_keys
        removed_keys = old_keys - new_keys

        # 检测设备名变化（同一序列号，设备名改变）
        name_changed = []
        for key in old_keys & new_keys:
            if old[key]['name'] != new[key]['name']:
                name_changed.append({
                    'key': key,
                    'old_name': old[key]['name'],
                    'new_name': new[key]['name'],
                    'model': new[key]['model']
                })

        has_changes = bool(added_keys or removed_keys or name_changed)

        return {
            'has_changes': has_changes,
            'added': [new[k] for k in added_keys],
            'removed': [old[k] for k in removed_keys],
            'name_changed': name_changed
        }

    def _load_last_simple_snapshot_from_log(self) -> str:
        """从日志文件读取最新的循环历史快照 → 缓存c

        返回: 最新的简化快照字符串
        """
        try:
            if not os.path.exists(self.config.DISK_LOG_FILE):
                return ""

            with open(self.config.DISK_LOG_FILE, 'r', encoding='utf-8') as f:
                content = f.read()

            # 查找最后一次历史记录
            if '--- 历史记录 [' not in content:
                return ""

            # 分离历史区
            if '=' * 50 not in content:
                return ""

            parts = content.split('=' * 50, 1)
            history_section = parts[0]

            # 解析历史记录，取最新的一条
            history_list = self.event_logger._parse_history(history_section)
            if history_list:
                return history_list[-1]['content']

            return ""

        except Exception as e:
            print(f"从日志加载循环历史失败: {e}")
            return ""

    def _load_last_state_from_log(self) -> Dict[str, Dict]:
        """从日志文件读取上次状态

        读取最后一次详细lsblk记录，提取设备序列号映射
        """
        import re
        devices = {}

        try:
            if not self.config.DISK_LOG_FILE or \
               not os.path.exists(self.config.DISK_LOG_FILE):
                return devices

            with open(self.config.DISK_LOG_FILE, 'r', encoding='utf-8') as f:
                content = f.read()

            # 查找最后一次 "--- lsblk 输出 ---" 区块
            if '--- lsblk 输出 ---' not in content:
                return devices

            parts = content.split('--- lsblk 输出 ---')
            last_block = parts[-1]

            # 使用正则解析 -P 格式（键值对）
            pattern = r'(\w+)="([^"]*)"'

            for line in last_block.split('\n'):
                if not line.strip() or line.startswith('#') or line.startswith('='):
                    continue

                # 解析键值对
                fields = dict(re.findall(pattern, line))
                if not fields:
                    continue

                name = fields.get('NAME', '')
                device_type = fields.get('TYPE', '')

                # 排除虚拟设备
                if any(name.startswith(prefix) for prefix in
                       ['loop', 'ram', 'dm-', 'zram', 'zd']):
                    continue

                # 只处理 disk 类型
                if device_type != 'disk':
                    continue

                model = fields.get('MODEL', '')
                serial = fields.get('SERIAL', '')
                wwn = fields.get('WWN', '')
                size = fields.get('SIZE', '')
                mountpoint = fields.get('MOUNTPOINT', '')
                fstype = fields.get('FSTYPE', '')

                # 生成唯一标识（与 _get_detailed_devices 保持一致）
                if serial:
                    key = f"{model}##{serial}"
                elif wwn:
                    key = f"{model}##{wwn}"
                else:
                    key = f"NOSERIAL##{name}"

                devices[key] = {
                    'name': name,
                    'model': model,
                    'serial': serial,
                    'size': size,
                    'type': device_type,
                    'mountpoint': mountpoint,
                    'fstype': fstype,
                    'wwn': wwn,
                    'identity_key': key
                }

        except Exception as e:
            print(f"从日志加载设备状态失败: {e}")

        return devices

    def get_all_devices_info(self) -> Dict[str, Dict[str, str]]:
        """
        获取所有磁盘的完整信息（从缓存中获取，零系统调用）

        返回: {
            'sda': {
                'model': 'Samsung SSD 970',      # 从last_devices_by_serial
                'serial': 'S5H2NS0N123456',      # 从last_devices_by_serial
                'size': '1.8T',                  # 从last_simple_devices_info
                'mountpoint': '/mnt/data'        # 从last_devices_by_serial（简化显示）
            },
            ...
        }
        """
        result = {}

        # 遍历last_devices_by_serial获取所有已知设备
        for key, device_info in self.last_devices_by_serial.items():
            device_name = device_info.get('name', '')
            if not device_name:
                continue

            # 处理挂载点显示逻辑
            mountpoint = device_info.get('mountpoint', '')
            if mountpoint and mountpoint.startswith('/mnt/'):
                mountpoint_display = mountpoint  # /mnt/data
            elif mountpoint:
                mountpoint_display = '系统挂载'  # 系统盘（/、/boot等）
            else:
                mountpoint_display = '未挂载'

            result[device_name] = {
                'model': device_info.get('model', ''),
                'serial': device_info.get('serial', ''),
                'size': self.last_simple_devices_info.get(device_name, ''),  # 从simple_snapshot缓存
                'mountpoint': mountpoint_display  # 简化的挂载点显示
            }

        return result

    def _write_log(self, changes: Dict, current_devices: Dict, reason: str):
        """写入日志（历史快照 + 详细事件）"""
        # 调用 EventLogger 写入
        self.event_logger.log_disk_events_v2(
            changes=changes,
            current_devices=current_devices,
            simple_snapshot=self.last_simple_snapshot,
            reason=reason
        )
