"""
系统接口模块 - 封装所有系统命令调用
优势：便于测试mock、统一错误处理、性能优化
"""

import subprocess
import os
from typing import Dict, Optional, List
from pathlib import Path


class SystemCommand:
    """系统命令封装类"""

    def __init__(self, timeout: int = 5):
        self.timeout = timeout

    def read_diskstats(self) -> Dict[str, Dict[str, int]]:
        """
        读取 /proc/diskstats（替代iostat，零进程开销）

        返回格式:
        {
            'sda': {
                'reads_completed': 12345,
                'sectors_read': 234567,
                'writes_completed': 6789,
                'sectors_written': 123456,
                'io_ms': 5678
            },
            ...
        }
        """
        stats = {}
        try:
            with open('/proc/diskstats', 'r') as f:
                for line in f:
                    fields = line.split()
                    if len(fields) < 14:
                        continue

                    device = fields[2]  # 设备名

                    stats[device] = {
                        'reads_completed': int(fields[3]),
                        'sectors_read': int(fields[5]),
                        'writes_completed': int(fields[7]),
                        'sectors_written': int(fields[9]),
                        'io_ms': int(fields[12])  # 总IO耗时（ms）
                    }
        except (FileNotFoundError, PermissionError, ValueError) as e:
            print(f"读取 /proc/diskstats 失败: {e}")

        return stats

    def read_hwmon_temp(self, device: str) -> Optional[float]:
        """
        读取 drivetemp hwmon 温度（优先方案，零开销）

        路径格式: /sys/block/sda/device/hwmon/hwmon*/temp1_input
        返回: 温度值（摄氏度）或 None
        """
        try:
            hwmon_path = Path(f"/sys/block/{device}/device/hwmon")
            if not hwmon_path.exists():
                return None

            # 查找 hwmon* 目录
            for hwmon_dir in hwmon_path.iterdir():
                temp_file = hwmon_dir / "temp1_input"
                if temp_file.exists():
                    # 读取温度（单位：毫摄氏度）
                    temp_millidegree = int(temp_file.read_text().strip())
                    return temp_millidegree / 1000.0
        except (FileNotFoundError, PermissionError, ValueError):
            pass

        return None

    def get_smartctl_temp(self, device: str) -> Optional[str]:
        """
        使用 smartctl 读取温度（降级方案）

        返回: 温度字符串（如 "45°C"）或 None
        """
        try:
            device_path = f"/dev/{device}"
            result = subprocess.run(
                ['smartctl', '-A', device_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.timeout
            )

            if result.returncode not in [0, 4, 8, 16]:
                return None

            # 查找温度信息
            for line in result.stdout.split('\n'):
                if 'Temperature' in line or 'Airflow_Temperature' in line:
                    parts = line.split()
                    # 从后向前找第一个纯数字
                    for part in reversed(parts):
                        if part.isdigit() and '(' not in part:
                            return f"{part}°C"
                    break
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            print(f"smartctl 查询 {device} 温度失败: {e}")

        return None

    def get_smartctl_info(self, device: str) -> Dict[str, str]:
        """
        获取设备 smartctl 基本信息（产品型号、序列号）

        返回:
        {
            'product': 'Samsung SSD 970 EVO',
            'serial': 'S5H2NS0N123456',
            'is_virtual': False
        }
        """
        try:
            device_path = f"/dev/{device}"
            result = subprocess.run(
                ['smartctl', '-i', '-n', 'standby', device_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.timeout
            )

            product = ""
            serial_number = ""

            if result.returncode in [0, 2]:  # 0=成功, 2=待机跳过
                for line in result.stdout.split('\n'):
                    # 支持多种型号字段
                    if any(line.startswith(prefix) for prefix in
                           ['Model Number:', 'Device Model:', 'Product:']):
                        parts = line.split(':', 1)
                        if len(parts) == 2:
                            product = parts[1].strip()

                    # 支持不同大小写的序列号字段
                    elif any(line.startswith(prefix) for prefix in
                             ['Serial Number:', 'Serial number:']):
                        parts = line.split(':', 1)
                        if len(parts) == 2:
                            serial_number = parts[1].strip()

            # 判断是否为虚拟磁盘
            is_virtual = self._is_virtual_disk(product, serial_number)

            return {
                'product': product,
                'serial': serial_number,
                'is_virtual': is_virtual
            }
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            print(f"获取 {device} 信息失败: {e}")
            return {'product': '', 'serial': '', 'is_virtual': False}

    def _is_virtual_disk(self, product: str, serial: str) -> bool:
        """判断是否为虚拟磁盘"""
        virtual_keywords = [
            'QEMU', 'VMware', 'Virtual', 'VBOX',
            'Msft', 'DELLBOSS'
        ]
        combined = f"{product} {serial}".upper()
        return any(keyword.upper() in combined for keyword in virtual_keywords)

    def get_lsblk_info(self, device: str) -> Dict[str, str]:
        """
        获取设备 lsblk 信息（挂载点、容量）

        返回:
        {
            'mountpoint': '/mnt/data',
            'size': '1.8T',
            'type': 'disk'
        }
        """
        try:
            result = subprocess.run(
                ['lsblk', '-bno', 'MOUNTPOINT,SIZE,TYPE', f"/dev/{device}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.timeout
            )

            if result.returncode != 0:
                return {'mountpoint': '', 'size': '', 'type': ''}

            lines = result.stdout.strip().split('\n')
            if not lines:
                return {'mountpoint': '', 'size': '', 'type': ''}

            parts = lines[0].split()
            return {
                'mountpoint': parts[0] if len(parts) > 0 else '',
                'size': self._format_size(int(parts[1])) if len(parts) > 1 else '',
                'type': parts[2] if len(parts) > 2 else ''
            }
        except (subprocess.TimeoutExpired, FileNotFoundError, ValueError) as e:
            print(f"lsblk 查询 {device} 失败: {e}")
            return {'mountpoint': '', 'size': '', 'type': ''}

    def _format_size(self, bytes_size: int) -> str:
        """格式化容量大小"""
        for unit in ['B', 'K', 'M', 'G', 'T', 'P']:
            if bytes_size < 1024.0:
                return f"{bytes_size:.1f}{unit}"
            bytes_size /= 1024.0
        return f"{bytes_size:.1f}P"

    def run_zpool_list(self) -> List[Dict[str, str]]:
        """
        执行 zpool list -H 获取ZFS池信息

        返回: 池信息列表
        [
            {
                'name': 'tank',
                'size': '10T',
                'allocated': '5T',
                'free': '5T',
                'health': 'ONLINE'
            },
            ...
        ]
        """
        pools = []
        try:
            result = subprocess.run(
                ['zpool', 'list', '-H', '-o',
                 'name,size,alloc,free,health'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.timeout
            )

            if result.returncode != 0:
                return pools

            for line in result.stdout.strip().split('\n'):
                if not line:
                    continue
                parts = line.split('\t')
                if len(parts) >= 5:
                    pools.append({
                        'name': parts[0],
                        'size': parts[1],
                        'allocated': parts[2],
                        'free': parts[3],
                        'health': parts[4]
                    })
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            print(f"zpool list 执行失败: {e}")

        return pools

    def get_physical_devices(self) -> List[str]:
        """
        获取物理磁盘列表（从 /sys/block 扫描）

        返回: 设备名列表 ['sda', 'sdb', ...]
        """
        devices = []
        try:
            for device_dir in Path('/sys/block').iterdir():
                device_name = device_dir.name

                # 过滤虚拟设备
                if any(device_name.startswith(prefix) for prefix in
                       ['loop', 'ram', 'dm-', 'zram', 'zd']):
                    continue

                # 检查是否有 device 子目录（物理设备标志）
                if (device_dir / 'device').exists():
                    devices.append(device_name)
        except (FileNotFoundError, PermissionError) as e:
            print(f"扫描 /sys/block 失败: {e}")

        return sorted(devices)

    def run_zpool_status(self, pool_name: str = None) -> str:
        """
        执行 zpool status 获取详细状态

        参数:
            pool_name: 指定池名，None则返回所有池
        返回: 完整的zpool status文本输出
        """
        try:
            cmd = ['zpool', 'status']
            if pool_name:
                cmd.append(pool_name)

            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10  # 较长超时，输出可能很大
            )

            if result.returncode == 0:
                return result.stdout.strip()
            return ""
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            print(f"zpool status 执行失败: {e}")
            return ""

    def parse_all_df_usage(self) -> Dict:
        """
        一次性解析 df 输出，分类处理普通设备和 ZFS 数据集

        返回: {
            'regular_devices': {  # 普通设备（仅第一层 /mnt）
                'sda1': {
                    'mountpoint': '/mnt/disk1',
                    'total': '5.6T',
                    'used': '5.2T',
                    'percent': '93%'
                }
            },
            'zfs_pools': {  # ZFS 池（聚合所有 /mnt 下的数据集）
                'zfs_12t': {
                    'mountpoint': '/mnt/zfs_12t',  # 第一层挂载点
                    'total': '11.0T',              # 聚合后的总容量
                    'used': '2.9T',                # 聚合后的已用容量
                    'percent': '27%',
                    'datasets': ['/mnt/zfs_12t', '/mnt/zfs_12t/data_zfs']  # 包含的数据集
                }
            }
        }
        """
        regular_devices = {}
        zfs_aggregated = {}  # 按池名聚合

        try:
            result = subprocess.run(
                ['df', '-B1'],  # 字节单位
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                return {'regular_devices': {}, 'zfs_pools': {}}

            lines = result.stdout.strip().split('\n')

            for line in lines[1:]:  # 跳过表头
                parts = line.split()
                if len(parts) < 6:
                    continue

                filesystem = parts[0]    # /dev/sda1 或 zfs_12t/data_zfs
                # df output format: Filesystem 1B-blocks Used Available Use% Mounted on
                # parts[0]=Filesystem, parts[1]=1B-blocks, parts[2]=Used, parts[3]=Avail, parts[4]=Use%, parts[5]=Mounted
                try:
                    total_bytes = int(parts[1])
                    used_bytes = int(parts[2])
                except ValueError:
                    continue
                    
                percent = parts[4]
                mountpoint = parts[5]

                # === 关键：只处理物理挂载点 ===
                # 过滤掉虚拟文件系统和非主要挂载点
                if not mountpoint.startswith('/') or any(mountpoint.startswith(p) for p in ['/proc', '/sys', '/dev', '/run', '/boot', '/var/lib/docker']):
                    continue
                
                # 原逻辑只允许 /mnt/，现在放宽以支持更多场景（如系统盘 /）
                # if not mountpoint.startswith('/mnt/'):
                #    continue

                # 计算层级深度（/mnt/disk1 -> 2, /mnt/disk1/sub -> 3）
                # /mnt/disk1 counts as (/, mnt, disk1) if splitting by / but wait.
                # '/mnt/disk1'.split('/') -> ['', 'mnt', 'disk1'] -> len 3.
                # '/mnt/disk1'.count('/') -> 2.
                depth = mountpoint.count('/')

                # === 分类处理 ===
                if filesystem.startswith('/dev/'):
                    # 【普通设备】：仅保留第一层挂载点
                    if depth == 2:  # /mnt/xxx（第一层）
                        device_name = filesystem.replace('/dev/', '')  # sda1
                        regular_devices[device_name] = {
                            'mountpoint': mountpoint,
                            'total': self._format_size(total_bytes),
                            'used': self._format_size(used_bytes),
                            'percent': percent
                        }

                else:
                    # 【ZFS 数据集】：显示池级别容量（避免累加）
                    # filesystem 如 'rrr' 或 'rrr/dpppz'
                    pool_name = filesystem.split('/')[0]

                    if pool_name not in zfs_aggregated:
                        zfs_aggregated[pool_name] = {
                            'mountpoint': '',
                            'total_bytes': 0,
                            'used_bytes': 0,
                            'percent': '0%',
                            'datasets': [],
                            'found_root': False
                        }

                    # 记录所有挂载点用于参考
                    zfs_aggregated[pool_name]['datasets'].append(mountpoint)
                    
                    # 优先使用根数据集（各ZFS数据集的大小显示的都是整个池的大小，除以配额限制等情况）
                    # 策略：如果找到跟池名一致的设备名（如 'rrr'），直接使用其数据
                    # 否则，使用第一个找到的数据集作为代表（通常所有数据集显示的Size都一样，是池的剩余空间+该数据集已用）
                    
                    is_root = (filesystem == pool_name)
                    
                    if is_root or not zfs_aggregated[pool_name]['found_root']:
                        zfs_aggregated[pool_name]['total_bytes'] = total_bytes
                        zfs_aggregated[pool_name]['used_bytes'] = used_bytes
                        zfs_aggregated[pool_name]['percent'] = percent
                        zfs_aggregated[pool_name]['root_mountpoint'] = mountpoint # 记录根挂载点
                        
                        if is_root:
                            zfs_aggregated[pool_name]['found_root'] = True

            # 格式化 ZFS 结果
            for pool_name, pool_data in zfs_aggregated.items():
                total_bytes = pool_data['total_bytes']
                used_bytes = pool_data['used_bytes']
                percent = pool_data['percent']

                pool_data['total'] = self._format_size(total_bytes)
                pool_data['used'] = self._format_size(used_bytes)
                pool_data['percent'] = percent
                
                # 优先使用根挂载点显示，如果没有则用第一个
                if 'root_mountpoint' in pool_data:
                    pool_data['mountpoint'] = pool_data['root_mountpoint']
                elif pool_data['datasets']:
                     pool_data['mountpoint'] = pool_data['datasets'][0]
                     
                del pool_data['total_bytes']
                del pool_data['used_bytes']
                del pool_data['found_root']
                if 'root_mountpoint' in pool_data:
                    del pool_data['root_mountpoint']

        except Exception as e:
            print(f"解析 df 失败: {e}")

        return {
            'regular_devices': regular_devices,
            'zfs_pools': zfs_aggregated
        }
