"""
工具模块
"""

# 导入模块本身（用于 from ..utils import slot_utils）
from . import slot_utils

# 导入常用函数（用于 from ..utils import load_slot_mapping）
from .slot_utils import (
    VALID_SLOTS,
    SLOT_MAPPING_FILE,
    SLOT_HISTORY_FILE,
    LED_COMMAND_FILE,
    detect_enclosure,
    detect_ses_device,
    get_slot_sas_addresses,
    load_slot_mapping,
    save_slot_mapping,
    update_slot_mapping,
    load_slot_history,
    save_slot_history,
    read_led_state,
    set_led_state,
    turn_off_all_leds,
    check_led_command,
    execute_led_command,
)

__all__ = [
    'VALID_SLOTS',
    'SLOT_MAPPING_FILE',
    'SLOT_HISTORY_FILE',
    'LED_COMMAND_FILE',
    'detect_enclosure',
    'detect_ses_device',
    'get_slot_sas_addresses',
    'load_slot_mapping',
    'save_slot_mapping',
    'update_slot_mapping',
    'load_slot_history',
    'save_slot_history',
    'read_led_state',
    'set_led_state',
    'turn_off_all_leds',
    'check_led_command',
    'execute_led_command',
]
