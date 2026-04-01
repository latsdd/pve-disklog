"""
配置模块 - 统一管理所有配置参数
"""

class Config:
    """全局配置类"""

    # ========== 文件路径 ==========
    DISK_LOG_FILE = "/var/log/disklog.txt"
    ZPOOL_LOG_FILE = "/var/log/zpoollog.txt"
    DISKS_IO_LOG_FILE = "/run/pve-disk-io/pve-disks-io.log"
    SNAPRAID_LOG_FILE = "/var/log/snapraid.log"
    ANALYZE_SCRIPT = "/opt/pve-disk-io-monitor/analyze-disk-zfs-log.py"

    # ========== 检测间隔（秒） ==========
    CHECK_INTERVAL = 5  # 精简快照检测间隔（v2方案：触发检测）
    ZFS_CHECK_INTERVAL = 90  # ZFS池检测间隔
    TEMP_CHECK_INTERVAL = 120  # 温度检测间隔（从15秒延长到120秒降低开销）
    DEVICE_INFO_UPDATE_INTERVAL = 30  # 设备信息更新间隔
    STARTUP_DELAY = 60  # 启动延迟（秒），等待设备稳定后再检测

    # ========== 阈值设置 ==========
    IO_THRESHOLD = 1  # 每秒IO操作数阈值，超过此值认为磁盘活跃

    # ========== 历史记录数量 ==========
    ZFS_HISTORY_ENTRIES = 2  # ZFS历史记录保留数量
    DISK_HISTORY_ENTRIES = 5  # 磁盘历史记录保留数量

    # ========== 功能开关 ==========
    ENABLE_TEMPERATURE_MONITORING = True  # 是否启用温度监控
    ENABLE_ZFS_MONITORING = True  # 是否启用ZFS监控
    ENABLE_DRIVETEMP = True  # 优先使用drivetemp（hwmon）读取温度

    # ========== 输出格式 ==========
    FIELD_SEPARATOR = "##SPLIT##"  # 字段分隔符（Perl端依赖，不可修改）
    ROW_SEPARATOR = "##ROW##"  # 行分隔符（Perl端依赖，不可修改）

    # ========== 日志格式 ==========
    LOG_TIME_FORMAT = "%Y/%m/%d %H:%M:%S"  # 日志时间格式
    EVENT_TIME_FORMAT = "%Y/%m/%d/%H:%M"  # 事件时间格式

    # ========== 性能优化 ==========
    USE_PROC_DISKSTATS = True  # 使用/proc/diskstats替代iostat
    SMARTCTL_TIMEOUT = 5  # smartctl命令超时时间（秒）

    @classmethod
    def validate(cls):
        """验证配置有效性"""
        assert cls.CHECK_INTERVAL > 0, "CHECK_INTERVAL must be positive"
        assert cls.TEMP_CHECK_INTERVAL >= 60, "TEMP_CHECK_INTERVAL should >= 60s to reduce overhead"
