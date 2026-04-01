"""
ZFS监控模块 - 检测ZFS池状态变化
"""

import time
import threading
from typing import Dict, List


class ZFSMonitor:
    """ZFS池监控"""

    def __init__(self, config, sys_cmd):
        self.config = config
        self.sys_cmd = sys_cmd
        self.pools_cache = {}  # {pool_name: {size, allocated, health, ...}}
        self.last_check_time = 0
        self.lock = threading.Lock()
        self.initialized = False

    def should_check(self) -> bool:
        """判断是否应该执行ZFS检测（90秒周期）"""
        elapsed = time.time() - self.last_check_time
        return elapsed >= self.config.ZFS_CHECK_INTERVAL

    def detect_changes(self) -> Dict:
        """
        检测ZFS池变化

        返回:
        {
            'has_changes': bool,
            'created': ['tank'],
            'destroyed': ['backup'],
            'health_changed': [{'pool': 'data', 'old': 'ONLINE', 'new': 'DEGRADED'}],
            'capacity_alerts': [{'pool': 'data', 'usage': 95}],
            'current_pools': {...}
        }
        """
        if not self.config.ENABLE_ZFS_MONITORING:
            return {'has_changes': False}

        current_pools = self._fetch_pools()
        self.last_check_time = time.time()

        if not self.initialized:
            with self.lock:
                self.pools_cache = current_pools
            self.initialized = True
            return {
                'has_changes': False,
                'created': [],
                'destroyed': [],
                'health_changed': [],
                'capacity_alerts': [],
                'current_pools': current_pools
            }

        with self.lock:
            previous = self.pools_cache.copy()

        # 检测创建/销毁
        prev_names = set(previous.keys())
        curr_names = set(current_pools.keys())
        created = sorted(curr_names - prev_names)
        destroyed = sorted(prev_names - curr_names)

        # 检测健康状态变化
        health_changed = []
        for pool_name in curr_names & prev_names:
            old_health = previous[pool_name].get('health', '')
            new_health = current_pools[pool_name].get('health', '')
            if old_health != new_health and new_health:
                health_changed.append({
                    'pool': pool_name,
                    'old': old_health,
                    'new': new_health
                })

        has_changes = bool(created or destroyed or health_changed)

        with self.lock:
            self.pools_cache = current_pools

        return {
            'has_changes': has_changes,
            'created': created,
            'destroyed': destroyed,
            'health_changed': health_changed,
            'current_pools': current_pools
        }

    def get_pools_info(self) -> Dict:
        """获取当前缓存的池信息"""
        with self.lock:
            return self.pools_cache.copy()

    def _fetch_pools(self) -> Dict:
        """查询ZFS池信息"""
        pools = {}
        pool_list = self.sys_cmd.run_zpool_list()

        for pool in pool_list:
            pools[pool['name']] = {
                'size': pool['size'],
                'allocated': pool['allocated'],
                'free': pool['free'],
                'health': pool['health']
            }

        return pools

    def _parse_device_to_pool_map(self, disk_monitor) -> Dict[str, str]:
        """
        解析 zpool status 建立设备到池的映射

        返回: {
            'sdb': 'zfs_12t',
            'nvme0n1': 'rpool'
        }
        """
        device_map = {}

        # 获取所有池的 status
        status_output = self.sys_cmd.run_zpool_status()
        if not status_output:
            return device_map

        # 获取 DiskMonitor 缓存（用于 WWN/Serial 匹配）
        devices_cache = disk_monitor.last_devices_by_serial  # {serial_key: device_info}

        current_pool = None
        for line in status_output.split('\n'):
            # 提取池名
            if line.strip().startswith('pool:'):
                current_pool = line.split(':')[1].strip()
                continue

            if not current_pool:
                continue

            # 匹配设备行（包含 wwn- 或 nvme- 或 /dev/）
            line_stripped = line.strip()

            # 提取设备标识符
            device_identifier = None

            # 模式1: wwn-0x5000cca270debbe4-part1
            if 'wwn-0x' in line_stripped:
                import re
                match = re.search(r'wwn-(0x[0-9a-fA-F]+)', line_stripped)
                if match:
                    device_identifier = ('wwn', match.group(1))

            # 模式2: nvme-...-SERIAL-partN
            elif 'nvme-' in line_stripped:
                import re
                # 提取序列号（通常在最后一个 '-part' 之前）
                match = re.search(r'nvme-.*?-([A-Z0-9]+)-part\d+', line_stripped)
                if match:
                    device_identifier = ('serial', match.group(1))

            # 模式3: 通用序列号/WWN匹配 (无需前缀)
            # 遍历所有已知设备，检查其序列号或WWN是否出现在当前行中
            # 这能覆盖 scsi-xxx, ata-xxx, wwn-xxx 等各种 /dev/disk/by-id/ 格式
            match_found = False
            for key, dev_info in devices_cache.items():
                # 检查序列号
                serial = dev_info.get('serial')
                if serial and len(serial) > 4 and serial in line_stripped:
                    match_found = True
                
                # 检查WWN
                if not match_found:
                    wwn = dev_info.get('wwn')
                    if wwn and len(wwn) > 6 and wwn in line_stripped:
                        match_found = True

                if match_found:
                    device_name = dev_info.get('name')
                    if device_name:
                        device_map[device_name] = current_pool
                    break
            
            if match_found:
                continue

            # 模式3: /dev/sdb
            elif '/dev/' in line_stripped:
                import re
                match = re.search(r'/dev/([a-z0-9]+)', line_stripped)
                if match:
                    device_name = match.group(1)
                    device_map[device_name] = current_pool
                    continue
            
            # 模式4: 纯设备名 (如 'sda' 或 'virtio-...')，常见于VM或某些ZFS版本
            else:
                # 尝试直接匹配已知设备名
                parts = line_stripped.split()
                if parts:
                    potential_dev = parts[0]
                    # 检查是否为已知设备（精确匹配）
                    if potential_dev in devices_cache:
                        device_map[potential_dev] = current_pool
                        continue
                     # 有些显示为 sda1，尝试去掉数字后缀匹配 sda
                    import re
                    match = re.match(r'([a-z]+)\d+$', potential_dev)
                    if match:
                        base_dev = match.group(1) # sda
                        if base_dev in devices_cache:
                             device_map[base_dev] = current_pool
                             continue

            # 如果提取到标识符，在缓存中查找设备名
            if device_identifier:
                id_type, id_value = device_identifier

                for key, dev_info in devices_cache.items():
                    # Check against both key (which is often serial) and fields inside
                    if id_type == 'wwn' and dev_info.get('wwn') == id_value:
                        device_name = dev_info.get('name')
                        if device_name:
                            device_map[device_name] = current_pool
                        break
                    elif id_type == 'serial' and dev_info.get('serial') == id_value:
                        device_name = dev_info.get('name')
                        if device_name:
                            device_map[device_name] = current_pool
                        break

        return device_map

    def update_device_mapping(self, disk_monitor):
        """更新设备到池的映射（在主循环中调用）"""
        device_map = self._parse_device_to_pool_map(disk_monitor)
        with self.lock:
            self.device_pool_map = device_map

    def get_device_to_pool_map(self) -> Dict[str, str]:
        """获取缓存的设备到池映射"""
        with self.lock:
            return getattr(self, 'device_pool_map', {}).copy()
