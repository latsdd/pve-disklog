"""
I/O监控模块 - 统计和格式化磁盘IO数据
"""

import time
import threading
from typing import Dict, Tuple, Optional

from ..utils import slot_utils


class IOMonitor:
    """I/O统计监控"""

    def __init__(self, config, sys_cmd):
        self.config = config
        self.sys_cmd = sys_cmd
        self.previous_stats = {}  # 上次的IO统计
        self.last_sample_time = time.time()
        self.temp_cache = {}  # 温度缓存
        self.temp_check_times = {}  # 温度检测时间
        self.lock = threading.Lock()

        # 插槽相关
        self.enclosure_path = None  # 启动时由主程序设置
        self.slot_history = {}  # 插槽历史缓存

    def update_stats(self) -> Dict[str, Dict]:
        """
        更新IO统计

        返回:
        {
            'sda': {
                'read_speed': 10.5,  # MB/s
                'write_speed': 5.2,  # MB/s
                'io_count': 150.3,   # IOPS
                'is_active': True
            },
            ...
        }
        """
        current_stats_raw = self.sys_cmd.read_diskstats()
        current_time = time.time()

        with self.lock:
            elapsed = current_time - self.last_sample_time
            if elapsed == 0:
                elapsed = 1.0  # 避免除零

            result = {}
            for device, current in current_stats_raw.items():
                if device in self.previous_stats:
                    prev = self.previous_stats[device]
                    read_speed, write_speed, io_count = self._calculate_rates(
                        prev, current, elapsed
                    )

                    is_active = io_count > self.config.IO_THRESHOLD

                    result[device] = {
                        'read_speed': read_speed,
                        'write_speed': write_speed,
                        'io_count': io_count,
                        'is_active': is_active
                    }
                else:
                    # 新设备，无历史数据
                    result[device] = {
                        'read_speed': 0.0,
                        'write_speed': 0.0,
                        'io_count': 0.0,
                        'is_active': False
                    }

            self.previous_stats = current_stats_raw
            self.last_sample_time = current_time

        return result

    def get_temperature(self, device: str, is_active: bool) -> str:
        """
        获取设备温度

        参数:
            device: 设备名
            is_active: 是否活跃（仅活跃设备实时查询）

        返回: "45°C" 或 "N/A"
        """
        if not self.config.ENABLE_TEMPERATURE_MONITORING:
            return "N/A"

        current_time = time.time()

        # 检查缓存
        if device in self.temp_check_times:
            last_check = self.temp_check_times[device]
            if current_time - last_check < self.config.TEMP_CHECK_INTERVAL:
                # 缓存有效
                cached_temp = self.temp_cache.get(device, "N/A")
                if not is_active and cached_temp != "N/A":
                    return f"{cached_temp}*"  # 非活跃设备加*标记
                return cached_temp

        # 仅活跃设备实时查询
        if not is_active:
            cached_temp = self.temp_cache.get(device, "N/A")
            if cached_temp != "N/A":
                return f"{cached_temp}?"  # 超过120秒加?标记
            return cached_temp

        # 优先使用 drivetemp
        temp = None
        if self.config.ENABLE_DRIVETEMP:
            temp_celsius = self.sys_cmd.read_hwmon_temp(device)
            if temp_celsius is not None:
                temp = f"{int(temp_celsius)}°C"

        # 降级到 smartctl
        if temp is None:
            temp = self.sys_cmd.get_smartctl_temp(device)

        if temp is None:
            temp = "N/A"

        # 更新缓存
        self.temp_cache[device] = temp
        self.temp_check_times[device] = current_time

        return temp

    def _find_device_usage(self, device: str, df_data: Dict) -> Dict:
        """
        从 df_data 中查找设备的容量信息

        参数:
            device: 设备名，如 'sdb'
            df_data: parse_all_df_usage() 返回的字典

        返回: {
            'usage_display': '5.6T / 5.2T (93%)',
            'mountpoint': '/mnt/disk1'
        }
        """
        # 查找该设备的分区（sdb1, sdb2...）
        regular_devices = df_data.get('regular_devices', {})

        for part_name, usage_info in sorted(regular_devices.items()):
            if part_name.startswith(device):  # sdb1.startswith('sdb')
                # 找到第一个分区
                return {
                    'usage_display': f"{usage_info['total']} / {usage_info['used']} ({usage_info['percent']})",
                    'mountpoint': usage_info['mountpoint']
                }

        # 未找到
        return {
            'usage_display': 'N/A',
            'mountpoint': ''
        }

    def collect_all_stats(self, disk_monitor, zfs_monitor) -> Dict[str, Dict]:
        """
        收集所有磁盘的I/O统计和设备信息（零额外系统调用）

        返回:
        {
            'sda': {
                ...
                'capacity_usage': '1.8T / 1.5T (85%)',
                '_slot': '1',      # 插槽号或None
                '_led_state': 0,   # LED状态或None
                ...
            },
            ...
        }
        """
        # 1. 计算IO统计
        io_stats = self.update_stats()

        # 2. 从DiskMonitor获取所有设备信息
        devices_info_map = disk_monitor.get_all_devices_info()

        # 3. 全局执行一次 df，解析普通设备容量
        df_data = self.sys_cmd.parse_all_df_usage()

        # 4. 获取 ZFS 设备映射和池信息（来自 zpool list，每90秒更新）
        zfs_device_map = zfs_monitor.get_device_to_pool_map()  # {'sdb': 'zfs_12t'}
        zfs_pools_status = zfs_monitor.get_pools_info()        # {'zfs_12t': {'health': 'ONLINE', 'size': '10.9T', ...}}

        # 5. 加载插槽映射和历史（3.5寸 SAS/SATA）
        slot_mapping = slot_utils.load_slot_mapping()
        slot_history = slot_utils.load_slot_history()
        slot_history_updated = False

        # 5.1 加载NVMe槽位映射和历史（2.5寸 NVMe）
        nvme_slot_mapping = slot_utils.get_nvme_slot_mapping()  # {device: slot}
        nvme_slot_history = slot_utils.load_nvme_slot_history()
        nvme_slot_history_updated = False

        # 6. 构建 wwn -> device 映射（用于插槽匹配）
        wwn_to_device = {}
        for key, dev_info in disk_monitor.last_devices_by_serial.items():
            wwn = dev_info.get('wwn', '')
            dev_name = dev_info.get('name', '')
            if wwn and dev_name:
                wwn_to_device[wwn.lower().replace('0x', '')] = (dev_name, dev_info)

        # 7. 遍历有效插槽，匹配在线设备
        slot_to_device = {}  # slot_num -> device_name
        for slot_num_str, sas_addr in slot_mapping.items():
            if not sas_addr:
                continue
            # 查找匹配的在线磁盘
            for wwn, (dev_name, dev_info) in wwn_to_device.items():
                if wwn[:14] == sas_addr:
                    slot_to_device[slot_num_str] = dev_name
                    # 更新 slot_history
                    slot_history[slot_num_str] = {
                        'model': dev_info.get('model', ''),
                        'serial': dev_info.get('serial', ''),
                        'wwn': dev_info.get('wwn', '')
                    }
                    slot_history_updated = True
                    break

        # 8. 保存更新后的 slot_history
        if slot_history_updated:
            slot_utils.save_slot_history(slot_history)

        # 8.1 更新NVMe槽位历史
        # 获取扩展卡配置（用于更新历史中的子端口号）
        nvme_config = slot_utils.load_nvme_slot_config()
        slot_to_port = {v: k for k, v in nvme_config['slot_map'].items()}

        for device, slot_num in nvme_slot_mapping.items():
            dev_info = devices_info_map.get(device, {})
            if dev_info:
                nvme_slot_history[slot_num] = {
                    'model': dev_info.get('model', ''),
                    'serial': dev_info.get('serial', ''),
                    'pcie_port': slot_to_port.get(slot_num, '')  # 子端口号（固定）
                }
                nvme_slot_history_updated = True

        # 8.2 保存更新后的 nvme_slot_history
        if nvme_slot_history_updated:
            slot_utils.save_nvme_slot_history(nvme_slot_history)

        # 9. 构建 device -> slot 反向映射（3.5寸）
        device_to_slot = {v: k for k, v in slot_to_device.items()}

        # 9.1 合并NVMe设备到槽位映射（2.5寸，使用特殊前缀区分）
        for device, slot_num in nvme_slot_mapping.items():
            device_to_slot[device] = f"nvme_{slot_num}"  # 如 "nvme_2"

        # 10. 组装结果
        result = {}
        processed_slots = set()  # 已处理的插槽

        for device, stats in io_stats.items():
            device_info = devices_info_map.get(device, {})
            if not device_info:
                continue

            read_speed = stats.get('read_speed', 0.0)
            write_speed = stats.get('write_speed', 0.0)
            io_count = stats.get('io_count', 0.0)
            is_active = stats.get('is_active', False)

            # 获取温度（唯一的系统调用）
            temp = self.get_temperature(device, is_active)

            # === 关键：判断是否为 ZFS 设备 ===
            pool_name = zfs_device_map.get(device)

            if pool_name:
                # 【ZFS 设备】使用 zpool list 的池级别容量（每90秒更新）
                pool_status = zfs_pools_status.get(pool_name, {})
                health_status = pool_status.get('health', 'UNKNOWN')
                pool_size = pool_status.get('size', 'N/A')
                pool_alloc = pool_status.get('allocated', 'N/A')

                # 挂载点栏显示：ZFS:池名 池容量(状态)
                mountpoint_display = f"ZFS:{pool_name} {pool_size}/{pool_alloc}({health_status})"

                # 容量栏显示：物理磁盘大小
                capacity_usage = device_info.get('size', '')

                # 标记属于哪个池
                current_zfs_pool = pool_name

            else:
                # 【普通设备】
                usage_info = self._find_device_usage(device, df_data)

                # 仅显示 /mnt 下的挂载点（由 df 解析），不再使用 lsblk 的原始挂载点
                mountpoint_display = usage_info['mountpoint']

                # 如果有挂载，优先显示使用率；否则显示物理大小
                if usage_info['usage_display'] != 'N/A':
                    capacity_usage = usage_info['usage_display']
                else:
                    capacity_usage = device_info.get('size', '')

                current_zfs_pool = ''

            # 获取插槽信息
            slot_num = device_to_slot.get(device)
            led_state = None
            if slot_num and self.enclosure_path:
                # NVMe 槽位（nvme_ 前缀）不支持 LED 控制，跳过
                if not str(slot_num).startswith('nvme_'):
                    led_state = slot_utils.read_led_state(self.enclosure_path, int(slot_num))
                    if led_state == -1:
                        led_state = None
                processed_slots.add(slot_num)

            # 组装设备完整信息
            result[device] = {
                'product': device_info.get('model', ''),
                'serial': device_info.get('serial', ''),
                'size': capacity_usage,
                'mountpoint': mountpoint_display,
                'temp': temp,
                'reads_per_sec': read_speed,
                'writes_per_sec': write_speed,
                'io_count': io_count,
                'type': 'disk',
                '_zfs_pool': current_zfs_pool,
                '_slot': slot_num,
                '_led_state': led_state,
                '_slot_status': 'online' if slot_num else None
            }

        # 11. 处理离线插槽（有历史但当前不在线）- 3.5寸
        for slot_num_str in slot_mapping.keys():
            if slot_num_str in processed_slots:
                continue  # 已处理

            if slot_num_str not in slot_history:
                continue  # 无历史记录，跳过

            # 离线设备
            history_info = slot_history[slot_num_str]
            led_state = None
            if self.enclosure_path:
                led_state = slot_utils.read_led_state(self.enclosure_path, int(slot_num_str))
                if led_state == -1:
                    led_state = None

            # 生成离线设备记录（使用特殊key）
            offline_key = f"__offline_slot_{slot_num_str}"
            result[offline_key] = {
                'product': history_info.get('model', ''),
                'serial': history_info.get('serial', ''),
                'size': '',
                'mountpoint': '',
                'temp': 'N/A',
                'reads_per_sec': 0.0,
                'writes_per_sec': 0.0,
                'io_count': 0.0,
                'type': 'disk',
                '_zfs_pool': '',
                '_slot': slot_num_str,
                '_led_state': led_state,
                '_slot_status': 'offline'
            }

        # 11.1 处理离线NVMe插槽（有历史但当前不在线）- 2.5寸
        processed_nvme_slots = set(nvme_slot_mapping.values())  # 当前在线的NVMe槽位
        for slot_num_str, history_info in nvme_slot_history.items():
            if slot_num_str in processed_nvme_slots:
                continue  # 已处理

            # 离线NVMe设备
            offline_key = f"__offline_nvme_slot_{slot_num_str}"
            result[offline_key] = {
                'product': history_info.get('model', ''),
                'serial': history_info.get('serial', ''),
                'size': '',
                'mountpoint': '',
                'temp': 'N/A',
                'reads_per_sec': 0.0,
                'writes_per_sec': 0.0,
                'io_count': 0.0,
                'type': 'disk',
                '_zfs_pool': '',
                '_slot': f"nvme_{slot_num_str}",  # 使用nvme_前缀
                '_led_state': None,  # NVMe无LED控制
                '_slot_status': 'offline'
            }

        return result

    def format_output(self, disk_monitor, zfs_monitor) -> str:
        """
        格式化输出为 ##SPLIT## ##ROW## 格式 (保留旧接口兼容性，实际上已被 monitor loop 中的 _update_output_file 替代)
        """
        # 此方法主要保留给旧逻辑调用，或者调试用
        pass

    def _calculate_rates(self, prev: Dict, current: Dict, elapsed: float) -> Tuple[float, float, float]:
        """
        计算IO速率

        返回: (read_mb_per_sec, write_mb_per_sec, io_per_sec)
        """
        # 扇区差值（1扇区 = 512字节）
        sector_size = 512

        read_sectors_delta = current['sectors_read'] - prev['sectors_read']
        write_sectors_delta = current['sectors_written'] - prev['sectors_written']
        reads_delta = current['reads_completed'] - prev['reads_completed']
        writes_delta = current['writes_completed'] - prev['writes_completed']

        # 计算速率
        read_mb_per_sec = (read_sectors_delta * sector_size) / (1024 * 1024 * elapsed)
        write_mb_per_sec = (write_sectors_delta * sector_size) / (1024 * 1024 * elapsed)
        io_per_sec = (reads_delta + writes_delta) / elapsed

        return (
            max(0, read_mb_per_sec),
            max(0, write_mb_per_sec),
            max(0, io_per_sec)
        )
