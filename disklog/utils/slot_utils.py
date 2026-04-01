"""
插槽工具模块 - 提供背板插槽相关的公共函数

功能：
- Enclosure检测
- Slot映射管理
- Slot历史管理
- LED控制
"""

import os
import json
import subprocess
import re
from pathlib import Path
from typing import Dict, Optional


# ========== 常量 ==========

# 3.5寸槽位 (SAS/SATA)
VALID_SLOTS = [1, 2, 3, 5, 6, 7, 9, 10, 11]
SLOT_MAPPING_FILE = "/var/lib/disklog/slot_mapping.json"
SLOT_HISTORY_FILE = "/var/lib/disklog/slot_history.json"
LED_COMMAND_FILE = "/run/pve-disk-io/led_command.json"

# 2.5寸槽位 (NVMe)
NVME_VALID_SLOTS = [0, 1, 2, 3, 4, 5]
NVME_SLOT_MAPPING_FILE = "/var/lib/disklog/nvme_slot_mapping.json"
NVME_SLOT_HISTORY_FILE = "/var/lib/disklog/nvme_slot_history.json"

# NVMe PCIe扩展卡默认配置 (PLX PEX9733)
# 如果配置文件不存在，使用此默认配置
NVME_DEFAULT_CONFIG = {
    'vendor_device': '10b5:9733',  # PLX PEX9733
    'detect_port': '01',           # 用于确定bus号的端口（内置功能，一定存在）
    'slot_map': {
        '04': '2',  # 子端口04 -> 槽位2
        '05': '3',  # 子端口05 -> 槽位3
        '06': '4',  # 子端口06 -> 槽位4
        '07': '5',  # 子端口07 -> 槽位5
    }
}


# ========== Enclosure检测 ==========

def detect_enclosure() -> Optional[str]:
    """
    检测enclosure路径

    扫描 /sys/class/enclosure/ 查找支持LED控制的SAS expander

    返回: enclosure路径（如 /sys/class/enclosure/0:3:0:0）或 None
    """
    enclosure_base = Path("/sys/class/enclosure")

    if not enclosure_base.exists():
        return None

    try:
        for enclosure_dir in enclosure_base.iterdir():
            if not enclosure_dir.is_dir():
                continue

            # 检查是否有带locate文件的子目录（表明支持LED控制）
            for item in enclosure_dir.iterdir():
                if item.is_dir() and (item / "locate").exists():
                    return str(enclosure_dir)
    except Exception as e:
        print(f"检测enclosure失败: {e}")

    return None


def detect_ses_device(enclosure_path: str) -> Optional[str]:
    """
    检测SES设备路径

    方法1: 通过enclosure的device链接找到scsi_generic
    方法2: 使用lsscsi找type=13 (enclosure)的设备

    参数:
        enclosure_path: enclosure sysfs路径

    返回: SES设备路径（如 /dev/sg8）或 None
    """
    if not enclosure_path:
        return None

    # 方法1: 通过sysfs链接
    try:
        device_path = Path(enclosure_path) / "device" / "scsi_generic"
        if device_path.exists():
            for sg_dir in device_path.iterdir():
                if sg_dir.name.startswith("sg"):
                    return f"/dev/{sg_dir.name}"
    except Exception:
        pass

    # 方法2: 使用lsscsi
    try:
        result = subprocess.run(
            ['lsscsi', '-g'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5
        )

        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if 'enclosu' in line.lower():
                    parts = line.split()
                    for part in reversed(parts):
                        if part.startswith('/dev/sg'):
                            return part
    except Exception:
        pass

    return None


# ========== Slot映射（disk_monitor调用） ==========

def get_slot_wwn_from_arcconf() -> Dict[int, str]:
    """
    通过 arcconf 获取插槽到WWN的映射（支持SAS和SATA设备）

    解析 arcconf getconfig 1 PD 输出，提取：
    - Reported Location : Enclosure 0, Slot X(Connector 1)
    - World-wide name   : XXXXXXXX

    返回: {slot_num: wwn_14chars}
    """
    addresses = {}

    try:
        result = subprocess.run(
            ['arcconf', 'getconfig', '1', 'PD'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30
        )

        if result.returncode != 0:
            return {}

        current_slot = None
        current_wwn = None

        for line in result.stdout.split('\n'):
            line_stripped = line.strip()

            # 匹配 "Reported Location : Enclosure 0, Slot X(Connector Y)"
            if 'Reported Location' in line_stripped:
                match = re.search(r'Slot\s*(\d+)', line_stripped)
                if match:
                    current_slot = int(match.group(1))

            # 匹配 "World-wide name : XXXXXXXX"
            elif 'World-wide name' in line_stripped and current_slot is not None:
                match = re.search(r'World-wide name\s*:\s*([0-9A-Fa-f]+)', line_stripped)
                if match:
                    wwn = match.group(1).lower()
                    # 取前14位用于匹配
                    if len(wwn) >= 14 and current_slot in VALID_SLOTS:
                        addresses[current_slot] = wwn[:14]
                    current_slot = None

    except FileNotFoundError:
        # arcconf 未安装
        pass
    except subprocess.TimeoutExpired:
        print("arcconf命令超时")
    except Exception as e:
        print(f"arcconf获取插槽映射失败: {e}")

    return addresses


def get_slot_sas_addresses(ses_device: str) -> Dict[int, str]:
    """
    获取插槽SAS地址映射

    使用 sg_ses -p 0x0a 解析Additional Element Status

    参数:
        ses_device: SES设备路径（如 /dev/sg8）

    返回: {slot_num: sas_address_14chars}
    """
    if not ses_device:
        return {}

    addresses = {}

    try:
        result = subprocess.run(
            ['sg_ses', '-p', '0x0a', ses_device],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10
        )

        if result.returncode != 0:
            print(f"sg_ses执行失败: {result.stderr}")
            return {}

        # 解析输出
        # 格式示例:
        #   Element index: 1  eiioe=0
        #     Transport protocol: SAS
        #     number of phys: 1, not all phys: 1, device slot number: 1
        #     phy index: 0
        #       ...
        #       attached SAS address: 0x56c92bf0001029df  <- expander地址，忽略
        #       SAS address: 0x5000c50084299e69           <- 磁盘地址，需要这个

        current_slot_num = None

        for line in result.stdout.split('\n'):
            line_stripped = line.strip()

            # 匹配 "device slot number: X"（这是真正的插槽号）
            if 'device slot number:' in line_stripped:
                match = re.search(r'device slot number:\s*(\d+)', line_stripped)
                if match:
                    current_slot_num = int(match.group(1))

            # 匹配磁盘SAS地址（必须是行首的 "SAS address:"，不是 "attached SAS address:"）
            elif line_stripped.startswith('SAS address:') and current_slot_num is not None:
                match = re.search(r'SAS address:\s*(0x)?([0-9a-fA-F]+)', line_stripped)
                if match:
                    full_address = match.group(2).lower()
                    # 取前14位用于匹配
                    if len(full_address) >= 14:
                        # 只保存有效插槽
                        if current_slot_num in VALID_SLOTS:
                            addresses[current_slot_num] = full_address[:14]
                    current_slot_num = None

    except subprocess.TimeoutExpired:
        print("sg_ses命令超时")
    except Exception as e:
        print(f"获取SAS地址失败: {e}")

    return addresses


def load_slot_mapping() -> Dict[str, str]:
    """
    从JSON文件加载插槽映射

    返回: {"slot_num": "sas_address_14chars", ...}
    """
    try:
        if os.path.exists(SLOT_MAPPING_FILE):
            with open(SLOT_MAPPING_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f"加载slot_mapping失败: {e}")

    return {}


def save_slot_mapping(data: Dict):
    """
    保存插槽映射到JSON文件

    参数:
        data: {slot_num: sas_address, ...}
    """
    try:
        os.makedirs(os.path.dirname(SLOT_MAPPING_FILE), exist_ok=True)
        with open(SLOT_MAPPING_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存slot_mapping失败: {e}")


def update_slot_mapping() -> bool:
    """
    更新插槽映射（封装：检测+获取+保存）

    使用 arcconf 获取 Slot → WWN 映射（支持SAS和SATA设备）
    注意：不回退到 sg_ses，因为 sg_ses 对 SATA 设备返回错误地址

    返回: True=成功, False=失败
    """
    # 使用 arcconf（Adaptec HBA，支持SAS和SATA）
    addresses = get_slot_wwn_from_arcconf()
    if addresses:
        print(f"通过arcconf获取插槽映射: {len(addresses)} 个插槽")
        data = {str(k): v for k, v in addresses.items()}
        save_slot_mapping(data)
        return True

    print("未能获取插槽映射（需要arcconf工具）")
    return False


# ========== Slot历史（io_monitor调用） ==========

def load_slot_history() -> Dict[str, Dict]:
    """
    从JSON文件加载插槽历史

    返回: {"slot_num": {"model": "...", "serial": "...", "wwn": "..."}, ...}
    """
    try:
        if os.path.exists(SLOT_HISTORY_FILE):
            with open(SLOT_HISTORY_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f"加载slot_history失败: {e}")

    return {}


def save_slot_history(data: Dict):
    """
    保存插槽历史到JSON文件

    参数:
        data: {slot_num: {model, serial, wwn}, ...}
    """
    try:
        os.makedirs(os.path.dirname(SLOT_HISTORY_FILE), exist_ok=True)
        with open(SLOT_HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存slot_history失败: {e}")


# ========== LED控制 ==========

def _find_slot_path(enclosure: str, slot: int) -> Optional[str]:
    """
    查找插槽的sysfs路径

    支持多种命名格式: "1", "Slot 01", "Slot01"
    """
    if not enclosure:
        return None

    slot_names = [
        str(slot),
        f"Slot {slot:02d}",
        f"Slot{slot:02d}",
        f"Slot {slot}",
        f"slot{slot}",
    ]

    for slot_name in slot_names:
        slot_path = Path(enclosure) / slot_name
        if slot_path.exists():
            return str(slot_path)

    return None


def read_led_state(enclosure: str, slot: int) -> int:
    """
    读取LED当前状态

    返回: 0=关, 1=开, -1=错误
    """
    slot_path = _find_slot_path(enclosure, slot)
    if not slot_path:
        return -1

    try:
        locate_file = Path(slot_path) / "locate"
        if locate_file.exists():
            return int(locate_file.read_text().strip())
    except Exception:
        pass

    return -1


def set_led_state(enclosure: str, slot: int, state: int) -> bool:
    """
    设置LED状态

    参数:
        enclosure: enclosure路径
        slot: 插槽号
        state: 0=关, 1=开

    返回: True=成功, False=失败
    """
    slot_path = _find_slot_path(enclosure, slot)
    if not slot_path:
        print(f"未找到插槽 {slot} 的路径")
        return False

    try:
        locate_file = Path(slot_path) / "locate"
        if locate_file.exists():
            locate_file.write_text(str(state))
            return True
    except PermissionError:
        print(f"无权限写入 {locate_file}，需要root权限")
    except Exception as e:
        print(f"设置LED失败: {e}")

    return False


def turn_off_all_leds(enclosure: str):
    """
    关闭所有有效插槽的LED

    参数:
        enclosure: enclosure路径
    """
    if not enclosure:
        return

    for slot in VALID_SLOTS:
        set_led_state(enclosure, slot, 0)

    print("所有LED已关闭")


def check_led_command() -> Optional[Dict]:
    """
    检查LED命令文件是否存在

    返回: 命令字典 {"slot": 1, "action": "toggle", "timestamp": ...} 或 None
    """
    if not os.path.exists(LED_COMMAND_FILE):
        return None

    try:
        with open(LED_COMMAND_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"读取LED命令文件失败: {e}")
        # 删除损坏的文件
        try:
            os.remove(LED_COMMAND_FILE)
        except:
            pass

    return None


def execute_led_command(enclosure: str) -> bool:
    """
    执行LED命令并删除命令文件

    参数:
        enclosure: enclosure路径

    返回: True=执行了命令, False=无命令或失败
    """
    command = check_led_command()
    if not command:
        return False

    slot = command.get('slot')
    action = command.get('action')

    if slot is None or action != 'toggle':
        print(f"无效的LED命令: {command}")
        try:
            os.remove(LED_COMMAND_FILE)
        except:
            pass
        return False

    # 读取当前状态，取反
    current_state = read_led_state(enclosure, slot)
    if current_state == -1:
        print(f"无法读取插槽 {slot} 的LED状态")
        try:
            os.remove(LED_COMMAND_FILE)
        except:
            pass
        return False

    new_state = 0 if current_state == 1 else 1

    # 设置新状态
    success = set_led_state(enclosure, slot, new_state)

    if success:
        print(f"插槽 {slot} LED已{'开启' if new_state == 1 else '关闭'}")

    # 删除命令文件
    try:
        os.remove(LED_COMMAND_FILE)
    except Exception as e:
        print(f"删除LED命令文件失败: {e}")

    return success


def match_wwn_to_slot(wwn: str, slot_mapping: Dict[str, str]) -> Optional[str]:
    """
    通过WWN匹配插槽

    参数:
        wwn: 磁盘WWN（如 5000c50084299e6b 或 0x5000c50084299e6b）
        slot_mapping: 插槽映射 {"slot_num": "sas_address_14chars"}

    返回: 插槽号字符串或None
    """
    if not wwn or not slot_mapping:
        return None

    # 清理WWN格式
    clean_wwn = wwn.lower().replace('0x', '')

    if len(clean_wwn) < 14:
        return None

    wwn_prefix = clean_wwn[:14]

    # 遍历插槽查找匹配
    for slot_num, sas_addr in slot_mapping.items():
        if sas_addr == wwn_prefix:
            return slot_num

    return None


# ========== NVMe槽位管理 ==========

def load_nvme_slot_config() -> Dict:
    """
    从JSON文件加载NVMe扩展卡配置

    返回: {
        "vendor_device": "10b5:9733",
        "detect_port": "01",
        "slot_map": {"04": "2", ...}
    }
    如果文件不存在，返回默认配置
    """
    try:
        if os.path.exists(NVME_SLOT_MAPPING_FILE):
            with open(NVME_SLOT_MAPPING_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                # 验证必要字段
                if all(k in config for k in ['vendor_device', 'detect_port', 'slot_map']):
                    return config
                else:
                    print(f"配置文件缺少必要字段，使用默认配置")
    except Exception as e:
        print(f"加载nvme_slot_mapping失败: {e}")

    return NVME_DEFAULT_CONFIG.copy()


def find_nvme_switch_bus(vendor_device: str = None, detect_port: str = None) -> Optional[str]:
    """
    找到扩展卡的bus号

    参数:
        vendor_device: 扩展卡PCI ID，如 '10b5:9733'（可选，默认从配置读取）
        detect_port: 检测端口，如 '01'（可选，默认从配置读取）

    通过 lspci -d {vendor_device} 找到所有端口
    找到 xx:{detect_port}.0 格式的端口，提取bus号

    返回: bus号如 '67' 或 None
    """
    # 如果未提供参数，从配置文件读取
    if vendor_device is None or detect_port is None:
        config = load_nvme_slot_config()
        vendor_device = vendor_device or config['vendor_device']
        detect_port = detect_port or config['detect_port']

    try:
        result = subprocess.run(
            ['lspci', '-d', vendor_device],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10
        )

        if result.returncode != 0:
            return None

        # 解析输出，找到 xx:{detect_port}.0 格式的地址
        for line in result.stdout.strip().split('\n'):
            if not line.strip():
                continue

            # 格式: "67:01.0 PCI bridge: PLX Technology..."
            parts = line.split()
            if not parts:
                continue

            addr = parts[0]  # "67:01.0"

            # 检查是否是检测端口 (xx:01.0)
            if ':' in addr and '.' in addr:
                bus_dev = addr.split(':')
                if len(bus_dev) == 2:
                    dev_func = bus_dev[1]  # "01.0"
                    dev = dev_func.split('.')[0]  # "01"
                    if dev == detect_port:
                        return bus_dev[0]  # 返回bus号

    except FileNotFoundError:
        print("lspci命令未找到")
    except subprocess.TimeoutExpired:
        print("lspci命令超时")
    except Exception as e:
        print(f"查找NVMe扩展卡失败: {e}")

    return None


def get_nvme_slot_mapping(nvme_devices: list = None) -> Dict[str, str]:
    """
    获取NVMe设备到槽位的映射

    参数:
        nvme_devices: disklog中的nvme设备列表，如 ['nvme1n1', 'nvme2n1']
                      （可选，当前实现通过sysfs路径匹配，不依赖此参数）

    流程:
        1. 从JSON文件读取扩展卡配置（vendor_device, detect_port, slot_map）
        2. 用 vendor_device 和 detect_port 找到扩展卡bus号
        3. 遍历slot_map中的端口，检查sysfs路径下的NVMe设备
        4. 匹配则分配槽位

    返回: {device_name: slot_num}，如 {'nvme1n1': '2'}
    """
    result = {}

    # 1. 从配置文件读取扩展卡配置
    config = load_nvme_slot_config()
    vendor_device = config['vendor_device']
    detect_port = config['detect_port']
    slot_map = config['slot_map']

    # 2. 找到扩展卡bus号
    bus = find_nvme_switch_bus(vendor_device, detect_port)
    if not bus:
        return result

    # 3. 检查每个端口下的NVMe设备

    for port, slot in slot_map.items():
        # 构建PCIe地址，如 0000:67:04.0
        pcie_addr = f"0000:{bus}:{port}.0"
        pcie_path = Path(f"/sys/bus/pci/devices/{pcie_addr}")

        if not pcie_path.exists():
            continue

        # 查找该端口下的NVMe设备
        # 端口下可能有子设备，需要递归查找nvme
        nvme_device = _find_nvme_under_pcie(pcie_path)
        if nvme_device:
            result[nvme_device] = slot

    return result


def _find_nvme_under_pcie(pcie_path: Path, depth: int = 0) -> Optional[str]:
    """
    在PCIe端口下查找NVMe块设备

    参数:
        pcie_path: PCIe设备路径，如 /sys/bus/pci/devices/0000:67:04.0
        depth: 递归深度（防止无限递归）

    返回: 块设备名如 'nvme1n1' 或 None
    """
    if depth > 5:  # 防止无限递归
        return None

    try:
        for item in pcie_path.iterdir():
            if not item.is_dir():
                continue

            # 方法1: 处理 nvme 子系统目录（名字恰好是 'nvme'）
            # 结构: 0000:69:00.0/nvme/nvme1/nvme1n1
            if item.name == 'nvme':
                for ctrl_item in item.iterdir():
                    if not ctrl_item.is_dir():
                        continue
                    # 查找控制器目录如 nvme1, nvme2（必须是 nvme + 数字）
                    if ctrl_item.name.startswith('nvme') and ctrl_item.name != 'nvme':
                        nvme_ctrl = ctrl_item.name
                        block_prefix = nvme_ctrl + 'n'  # 如 'nvme1n'
                        # 查找块设备
                        for sub in ctrl_item.iterdir():
                            if sub.is_dir() and sub.name.startswith(block_prefix):
                                return sub.name
                        # 备用：直接返回 nvmeXn1
                        return f"{nvme_ctrl}n1"
                continue

            # 方法2: 直接查找nvme控制器目录（如 nvme1）
            # 结构: 0000:69:00.0/nvme1/nvme1n1
            if item.name.startswith('nvme') and not item.name.startswith('nvme-') and item.name != 'nvme':
                nvme_ctrl = item.name
                block_prefix = nvme_ctrl + 'n'  # 如 'nvme1n'
                # 查找对应的块设备 nvmeXn1
                for sub in item.iterdir():
                    if sub.is_dir() and sub.name.startswith(block_prefix):
                        return sub.name
                # 尝试更深一层: nvme1/nvme/nvme1/nvme1n1
                nvme_sub = item / 'nvme' / nvme_ctrl
                if nvme_sub.exists():
                    for block_item in nvme_sub.iterdir():
                        if block_item.name.startswith(block_prefix):
                            return block_item.name
                # 备用：直接返回nvmeXn1
                return f"{nvme_ctrl}n1"

            # 方法3: 递归查找子PCIe设备（如 0000:69:00.0）
            if ':' in item.name and item.name.startswith('0000:'):
                sub_result = _find_nvme_under_pcie(item, depth + 1)
                if sub_result:
                    return sub_result

    except Exception as e:
        if depth == 0:
            print(f"查找NVMe设备失败: {e}")

    return None


def load_nvme_slot_history() -> Dict[str, Dict]:
    """
    从JSON文件加载NVMe槽位历史

    返回: {"slot_num": {"model": "...", "serial": "...", "pcie_port": "..."}, ...}
    """
    try:
        if os.path.exists(NVME_SLOT_HISTORY_FILE):
            with open(NVME_SLOT_HISTORY_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f"加载nvme_slot_history失败: {e}")

    return {}


def save_nvme_slot_history(data: Dict):
    """
    保存NVMe槽位历史到JSON文件

    参数:
        data: {slot_num: {model, serial, pcie_port}, ...}
    """
    try:
        os.makedirs(os.path.dirname(NVME_SLOT_HISTORY_FILE), exist_ok=True)
        with open(NVME_SLOT_HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存nvme_slot_history失败: {e}")
