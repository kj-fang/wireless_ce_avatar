import json
import os


def load_fw_system_info(fw_path):
    if not fw_path:
        return None

    system_info_path = os.path.join(os.path.dirname(fw_path), 'system_info.txt')
    if not os.path.exists(system_info_path):
        return None

    try:
        with open(system_info_path, 'r', encoding='utf-8') as file:
            system_info = json.load(file)
    except Exception:
        return None

    versions = system_info.get('Versions', {})
    return {
        'BT Driver Version': versions.get('BT Driver Version', ''),
        'Wi-Fi Driver Version': versions.get('Wi-Fi Driver Version', ''),
        'Device Name': system_info.get('Device Name', ''),
        'BT FW SHA1': system_info.get('BT FW SHA1', ''),
        'Wi-Fi Adapter': system_info.get('Wi-Fi Adapter', ''),
        'OS Information': system_info.get('OS Information', ''),
        'Intel® Smart Sound Technology BUS': system_info.get('Intel® Smart Sound Technology BUS', ''),
        'Intel® Smart Sound Technology OED': system_info.get('Intel® Smart Sound Technology OED', ''),
        'Intel® Smart Sound Technology for Bluetooth® Audio': system_info.get('Intel® Smart Sound Technology for Bluetooth® Audio', ''),
        'WRT::2G Version': versions.get('WRT::2G Version', ''),
        'preset': system_info.get('preset', ''),
        'BT FW Config': system_info.get('BT FW Config', ''),
        'Dbgc Status Global as seen by BT': system_info.get('Dbgc Status Global as seen by BT', ''),
        'Dbgc Status as read from Mailbox': system_info.get('Dbgc Status as read from Mailbox', ''),
    }
