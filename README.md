# PVE Disk Monitor (pve-disklog)

Proxmox VE 磁盘监控工具，提供磁盘插拔检测、I/O 监控、容量显示、LED 定位灯控制，并通过 Web 注入集成到 PVE 管理界面。

## 功能概览

- **磁盘插拔检测**：通过 lsblk 快照对比，自动检测磁盘热插拔事件并记录日志
- **I/O 监控**：实时采集磁盘读写速度、IO 操作数、温度等指标
- **容量显示**：支持普通分区和 ZFS 池的已用/总容量显示
- **LED 定位灯**：通过 Web 按钮控制 SAS/SATA 背板插槽定位灯
- **NVMe 槽位**：支持 PCIe 扩展卡的 2.5 寸 NVMe 槽位检测与显示
- **Web 集成**：注入 PVE 前端，在节点状态页展示磁盘面板

## 项目结构

```
pve-disklog/
├── pve-disk-io-monitor-v2.20.py   # 主程序入口
├── install-v2.20.sh               # 安装脚本（含 Perl/JS 注入）
├── analyze-disk-zfs-log.py        # 日志分析工具
└── disklog/                       # Python 包
    ├── config.py                  # 配置常量
    ├── system_interface.py        # 系统命令封装（lsblk, df, zpool 等）
    ├── loggers/
    │   └── event_logger.py        # 磁盘事件日志记录
    ├── monitors/
    │   ├── disk_monitor.py        # 磁盘插拔检测
    │   ├── io_monitor.py          # I/O 统计采集与输出
    │   └── zfs_monitor.py         # ZFS 池状态监控与设备映射
    └── utils/
        └── slot_utils.py          # 插槽工具（arcconf 映射、LED 控制、NVMe 检测）
```

## 核心模块设计

### 磁盘插拔检测 (disk_monitor)

采用双层缓存对比机制：

| 缓存 | 变量 | 用途 |
|------|------|------|
| a | `current_simple` | 当前 lsblk 简化快照（NAME,SIZE,TYPE），定时刷新 |
| c | `last_simple_snapshot` | 上次快照，与 a 对比检测变化 |
| dd | 临时变量 | 详细设备信息（NAME,MODEL,SERIAL 等），用完即清 |
| bb | `last_devices_by_serial` | 持久化设备缓存，按 model+serial 对比确定具体变化 |

**启动流程**：从日志文件恢复 c 和 bb，不触发事件。

**运行时流程**：
1. 定时获取快照 a，与 c 对比 NAME 列
2. 若不同 → 触发事件：a→c，获取详细信息 dd，dd 与 bb 按 model+serial 对比
3. dd→bb，写入日志（循环历史区始终更新，永久事件区仅有变化时追加）

### 容量显示

- **普通磁盘**：全局一次 `df -B1`，过滤 `/mnt` 第一层挂载点
- **ZFS 设备**：`zpool status` 建立设备→池映射（90 秒周期），df 数据按池名聚合；无 `/mnt` 挂载时降级到 `zpool list`
- **显示格式**：`总容量 / 已用容量 (百分比)`，如 `1.8T / 0.9T (50%)`

### LED 定位灯控制

**硬件**：Supermicro 服务器 + Adaptec ASR 8885z HBA + PMC Sierra SAS Expander

**插槽映射**：通过 `arcconf getconfig 1 PD` 获取 Slot→WWN 映射，WWN 前 14 位匹配 lsblk 设备。

**LED 控制**：读写 `/sys/class/enclosure/0:3:0:0/{slot}/locate`

**Web 通信**：前端按钮 → Perl API 写命令文件 → Python 服务（root）执行 toggle

```
前端按钮 → Perl API (www-data) → /run/pve-disk-io/led_command.json → Python (root) → sysfs
```

### NVMe 2.5 寸槽位

在 9 个 3.5 寸槽位基础上，支持 6 个 2.5 寸 NVMe 槽位（通过 PLX PEX9733 PCIe 扩展卡）。

- 通过 `lspci -d 10b5:9733` 定位扩展卡 bus 号
- 检查 NVMe 设备 sysfs 路径是否包含扩展卡端口地址
- 配置文件 `/var/lib/disklog/nvme_slot_mapping.json` 可手动调整端口→槽位映射
- 槽位 0,1 为占位符，槽位 2-5 对应子端口 04-07
- NVMe 槽位不支持 LED 控制

## 运行时文件

| 文件 | 用途 |
|------|------|
| `/run/pve-disk-io/pve-disks-io.log` | I/O 统计输出（Perl 前端读取） |
| `/run/pve-disk-io/led_command.json` | LED 控制命令中转 |
| `/var/lib/disklog/slot_mapping.json` | 3.5 寸插槽→WWN 映射 |
| `/var/lib/disklog/slot_history.json` | 3.5 寸插槽设备历史（离线显示） |
| `/var/lib/disklog/nvme_slot_mapping.json` | NVMe 扩展卡配置 |
| `/var/lib/disklog/nvme_slot_history.json` | NVMe 槽位设备历史 |

## 输出格式

12 字段，`##SPLIT##` 分隔，`##ROW##` 分行：

```
DISKS_IO_DATA=slot##SPLIT##disk_path##SPLIT##product##SPLIT##serial##SPLIT##mount##SPLIT##status##SPLIT##capacity##SPLIT##read##SPLIT##write##SPLIT##io##SPLIT##temp##SPLIT##led_state##ROW##
```

## 安装

```bash
bash install-v2.20.sh
```

安装脚本会：
1. 部署 Python 监控服务
2. 注入 PVE 前端（Nodes.pm 参数定义 + 处理逻辑，pvemanagerlib.js 事件处理）
3. 创建 systemd 服务
4. 设置运行时目录权限

## 设备状态

| 状态 | 含义 | 显示 |
|------|------|------|
| 活跃 | 在线且有 IO | 绿色 |
| 空闲 | 在线无 IO | 默认 |
| 离线 | 历史设备，当前不在 | 灰色半透明 |

## 依赖

- Python 3
- Proxmox VE
- `arcconf` — Adaptec HBA 管理工具（LED 插槽映射）
- `sg3-utils` — 备用 SAS 工具
