import json
import os
from utils.helpers import to_long_path


def load_fw_system_info(fw_path):
    if not fw_path:
        return None

    system_info_path = to_long_path(os.path.join(os.path.dirname(fw_path), 'system_info.txt'))
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
        'Wi-Fi FW': system_info.get('Wi-Fi FW', ''),
        'BT FW Config': system_info.get('BT FW Config', ''),
        'Dbgc Status Global as seen by BT': system_info.get('Dbgc Status Global as seen by BT', ''),
        'Dbgc Status as read from Mailbox': system_info.get('Dbgc Status as read from Mailbox', ''),
    }


_BT_ONLY_WIFI_FW = {"release default", "bt_only_kpi", "bt_only_d3"}


def infer_fw_parse_type(fw_path: str, wifi_or_bt: str) -> str | None:
    """Return 'bt', 'wifi', 'coex', or None (needs manual selection).

    Decision table (system_info.txt fields -> return value):
      system_info.txt  | Wi-Fi FW          | BT FW Config | Result
      -----------------+-------------------+--------------+--------
      absent           | -                 | -            | 'bt' if hint=='bt', else None
      present          | '' (empty)        | 'none'       | None  (unparseable)
      present          | bt-only keyword*  | != 'none'    | 'bt'
      present          | any               | 'none'       | 'wifi'
      present          | non-bt-only       | != 'none'    | 'coex'

    * bt-only keywords: {"release default", "bt_only_kpi", "bt_only_d3"}

    wifi_or_bt hint is only consulted when system_info.txt is absent:
      IPS flow     : hint = case_context.wifi_or_bt (from Salesforce subcategory)
      local-upload : hint = 'wifi' (hardcoded); 'coex'/None trigger fwTypeSelectModal
    """
    import os
    system_info = load_fw_system_info(fw_path)

    if system_info is None:
        result = 'bt' if wifi_or_bt == 'bt' else None
        print(f"[infer_fw_parse_type] {os.path.basename(fw_path)} | no system_info.txt | wifi_or_bt={wifi_or_bt!r} -> {result!r}")
        if wifi_or_bt == 'bt':
            return 'bt'
        # Shouldn't happen: FW ETL in a wifi-only case without system_info.txt
        return None

    wifi_fw = system_info.get('Wi-Fi FW', '')
    bt_fw_config = system_info.get('BT FW Config', '')
    wifi_fw_lower = wifi_fw.lower()
    bt_fw_config_lower = bt_fw_config.lower()
    is_bt_only_fw = any(kw in wifi_fw_lower for kw in _BT_ONLY_WIFI_FW)

    if wifi_fw_lower == '' and bt_fw_config_lower == 'none':
        # both fields absent — unparseable dataset
        result = None
    elif is_bt_only_fw and bt_fw_config_lower != 'none':
        result = 'bt'
    elif bt_fw_config_lower == 'none':
        result = 'wifi'
    elif not is_bt_only_fw and bt_fw_config_lower != 'none':
        result = 'coex'
    else:
        result = None

    print(f"[infer_fw_parse_type] {os.path.basename(fw_path)} | Wi-Fi FW={wifi_fw!r} | BT FW Config={bt_fw_config!r} -> {result!r}")
    return result
