#!/usr/bin/env python3
"""
PVE磁盘I/O监控程序 v2.21
使用双日志触发检测方案：
- 精简快照（lsblk简单输出）用于触发检测
- 详细信息（lsblk完整输出）用于序列号对比
- 解决重启掉盘检测问题
"""

import os
import sys
import time
import signal
import stat

# 智能模块路径检测
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if os.path.exists(os.path.join(SCRIPT_DIR, 'disklog')):
    sys.path.insert(0, SCRIPT_DIR)
elif os.path.exists('/usr/local/lib/python3.13/dist-packages/disklog'):
    sys.path.insert(0, '/usr/local/lib/python3.13/dist-packages')
elif os.path.exists('/usr/local/lib/python3/dist-packages/disklog'):
    sys.path.insert(0, '/usr/local/lib/python3/dist-packages')

from disklog.config import Config
from disklog.system_interface import SystemCommand
from disklog.monitors import DiskMonitor
from disklog.monitors.zfs_monitor import ZFSMonitor
from disklog.monitors.io_monitor import IOMonitor
from disklog.loggers.event_logger import EventLogger
from disklog.utils import slot_utils


class PVEDiskMonitor:
    """PVE磁盘监控主程序（v2.21双日志方案）"""

    def __init__(self):
        self.config = Config()
        self.config.validate()

        self.sys_cmd = SystemCommand(timeout=self.config.SMARTCTL_TIMEOUT)
        self.event_logger = EventLogger(self.config, self.sys_cmd)

        # 磁盘监控器（双日志方案）
        self.disk_monitor = DiskMonitor(self.config, self.event_logger)
        self.zfs_monitor = ZFSMonitor(self.config, self.sys_cmd)
        self.io_monitor = IOMonitor(self.config, self.sys_cmd)

        # 插槽相关
        self.enclosure_path = None

        self.running = False

        # 注册信号处理
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """信号处理器"""
        print(f"\n收到信号 {signum}，正在停止...")
        self.running = False

    def start(self):
        """启动监控"""
        # 启动时生成日志摘要（如果需要）
        self.event_logger._trigger_analyze_script()

        print("="*60)
        print("PVE Disk I/O Monitor v2.21 (双日志触发检测方案)")
        print("="*60)
        print(f"精简快照检测间隔: {self.config.CHECK_INTERVAL}秒")
        print(f"启动延迟: {self.config.STARTUP_DELAY}秒")
        print(f"ZFS检测间隔: {self.config.ZFS_CHECK_INTERVAL}秒")
        print(f"温度检测间隔: {self.config.TEMP_CHECK_INTERVAL}秒")
        print("="*60)

        # 设置输出目录权限（777，允许www-data写入LED命令）
        self._setup_output_directory()

        # 检测SAS背板enclosure
        self._setup_enclosure()

        # 启动时强制检测（延迟60秒）
        print("\n[启动阶段] 执行启动检测...")
        self.disk_monitor.startup_check()

        # 进入主循环
        print("\n[监控阶段] 进入主循环...")
        self.running = True
        self._main_loop()

    def _setup_output_directory(self):
        """设置输出目录权限（777，允许www-data写入LED命令文件）"""
        output_dir = os.path.dirname(self.config.DISKS_IO_LOG_FILE)
        try:
            os.makedirs(output_dir, exist_ok=True)
            # 设置777权限（rwxrwxrwx）
            os.chmod(output_dir, stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)
            print(f"输出目录权限已设置: {output_dir} (777)")
        except Exception as e:
            print(f"设置目录权限失败: {e}")

    def _setup_enclosure(self):
        """检测SAS背板enclosure并初始化LED"""
        self.enclosure_path = slot_utils.detect_enclosure()

        if self.enclosure_path:
            print(f"检测到SAS背板: {self.enclosure_path}")
            # 传递给io_monitor用于LED状态读取
            self.io_monitor.enclosure_path = self.enclosure_path
            # 启动时关闭所有LED
            slot_utils.turn_off_all_leds(self.enclosure_path)
        else:
            print("未检测到SAS背板，插槽功能不可用")

    def _main_loop(self):
        """主监控循环"""
        while self.running:
            try:
                self._monitor_cycle()
                time.sleep(self.config.CHECK_INTERVAL)

            except KeyboardInterrupt:
                print("\n用户中断，正在退出...")
                break
            except Exception as e:
                print(f"监控周期异常: {e}")
                time.sleep(5)

        print("监控程序已停止")

    def _monitor_cycle(self):
        """单次监控周期"""

        # 0. 检查LED命令（来自Web界面）
        if self.enclosure_path:
            if slot_utils.execute_led_command(self.enclosure_path):
                pass  # 命令已执行，日志已在execute_led_command中打印

        # 1. 精简快照触发检测
        disk_changed = self.disk_monitor.check_changes()
        if disk_changed:
            print(f"[{time.strftime('%H:%M:%S')}] 磁盘变化已记录")

        # 2. ZFS监控（90秒周期）
        if self.config.ENABLE_ZFS_MONITORING and self.zfs_monitor.should_check():
            zfs_changes = self.zfs_monitor.detect_changes()
            
            # 【新增】更新设备到池的映射
            self.zfs_monitor.update_device_mapping(self.disk_monitor)

            if zfs_changes.get('has_changes') or zfs_changes.get('capacity_alerts'):
                self.event_logger.log_zfs_events(zfs_changes)
                if zfs_changes.get('has_changes'):
                    print(f"[ZFS变化] 创建: {zfs_changes.get('created', [])}, "
                          f"销毁: {zfs_changes.get('destroyed', [])}")

        # 3. I/O统计和温度监控
        try:
            io_stats = self.io_monitor.collect_all_stats(self.disk_monitor, self.zfs_monitor)
            self._update_output_file(io_stats)
        except Exception as e:
            print(f"I/O统计收集失败: {e}")

    def _update_output_file(self, io_stats: dict):
        """更新输出文件（Perl端读取）"""
        try:
            output_dir = os.path.dirname(self.config.DISKS_IO_LOG_FILE)
            os.makedirs(output_dir, exist_ok=True)

            # 自定义排序逻辑
            # 1. 有插槽的设备按插槽号排序（优先显示）
            # 2. 无插槽的普通盘（_zfs_pool为空），按设备名排
            # 3. ZFS盘按池名分组（_zfs_pool），组内按设备名排
            # 4. NVMe槽位设备（nvme_X格式）
            def sort_key(item):
                device, stats = item
                slot = stats.get('_slot')
                pool = stats.get('_zfs_pool', '')
                slot_status = stats.get('_slot_status')

                # 排序优先级：
                # (0, slot_num, '', device) - 3.5寸有插槽的在线设备
                # (1, slot_num, '', device) - 3.5寸有插槽的离线设备
                # (2, 999, '', device) - 无插槽的普通盘
                # (3, 999, pool, device) - ZFS盘
                # (4, slot_num, '', device) - 2.5寸NVMe在线设备
                # (5, slot_num, '', device) - 2.5寸NVMe离线设备
                if slot:
                    # 处理NVMe槽位格式 (nvme_2, nvme_3, ...)
                    if str(slot).startswith('nvme_'):
                        nvme_slot_num = int(slot.split('_')[1])
                        if slot_status == 'offline':
                            return (5, nvme_slot_num, '', device)
                        else:
                            return (4, nvme_slot_num, '', device)
                    else:
                        slot_num = int(slot)
                        if slot_status == 'offline':
                            return (1, slot_num, '', device)
                        else:
                            return (0, slot_num, '', device)
                elif pool:
                    return (3, 999, pool, device)
                else:
                    return (2, 999, '', device)

            sorted_items = sorted(io_stats.items(), key=sort_key)

            # 遍历设备，生成输出行
            device_rows = []

            for device, stats in sorted_items:
                slot_status = stats.get('_slot_status')

                # 判断状态
                if slot_status == 'offline':
                    status = "离线"
                else:
                    io_count = stats.get('io_count', 0.0)
                    status = "活跃" if io_count > self.config.IO_THRESHOLD else "空闲"

                # 格式化容量（使用 or 处理 None 值）
                size = stats.get('size') or ''
                mountpoint = stats.get('mountpoint') or '未挂载'

                capacity = size  # 单个值，Perl会自己显示

                # 格式化IO数据
                read_speed = f"{stats.get('reads_per_sec', 0):.2f}"
                write_speed = f"{stats.get('writes_per_sec', 0):.2f}"
                io_count_str = f"{int(stats.get('io_count', 0))}/s"

                # 获取插槽和LED信息
                slot = stats.get('_slot') or ''
                led_state = stats.get('_led_state')
                led_str = str(led_state) if led_state is not None else ''

                # 处理离线设备的设备路径显示
                if device.startswith('__offline_slot_') or device.startswith('__offline_nvme_slot_'):
                    disk_path = ''  # 离线设备无设备路径
                else:
                    disk_path = f"/dev/{device}"

                # 字段顺序必须与Perl代码一致（12字段格式）
                # 注意：所有字段必须是字符串，None会导致join失败
                fields = [
                    str(slot) if slot else '',                      # 0: slot（插槽号，可空）
                    disk_path,                                      # 1: disk_path
                    stats.get('product') or '',                     # 2: product
                    stats.get('serial') or '',                      # 3: serial_number
                    mountpoint or '',                               # 4: mount_point
                    status,                                         # 5: status
                    capacity or '',                                 # 6: capacity
                    read_speed,                                     # 7: read_speed
                    write_speed,                                    # 8: write_speed
                    io_count_str,                                   # 9: io_count
                    stats.get('temp') or 'N/A',                     # 10: disk_temp
                    led_str                                         # 11: led_state（0/1/空）
                ]
                row = self.config.FIELD_SEPARATOR.join(fields)
                device_rows.append(row)

            # 拼接所有设备，格式：DISKS_IO_DATA=设备1##ROW##设备2##ROW##
            all_data = self.config.ROW_SEPARATOR.join(device_rows)
            if device_rows:
                all_data += self.config.ROW_SEPARATOR  # 末尾也要加ROW分隔符

            # 写入单行（Perl期待的格式）
            with open(self.config.DISKS_IO_LOG_FILE, 'w', encoding='utf-8') as f:
                f.write(f"DISKS_IO_DATA={all_data}\n")

        except (IOError, PermissionError) as e:
            print(f"更新输出文件失败: {e}")


def main():
    """主入口"""
    try:
        monitor = PVEDiskMonitor()
        monitor.start()
    except Exception as e:
        print(f"程序启动失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
