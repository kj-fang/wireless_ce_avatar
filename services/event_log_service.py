import os
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

try:
    from dateutil import tz
    import win32evtlog
except ImportError as e:
    print("Required modules not found. Please ensure 'pywin32' and 'python-dateutil' are installed.")
    raise e


_CACHE = {}

_SPECIAL_SOURCES = ['ibtusb', 'ibtpci', 'bthmini', 'bthusb', 'netwaw', 'netwtw']

_SOURCE_GROUPS = {
    'pci_bt':      ['ibtpci', 'bthmini'],
    'usb_bt':      ['ibtusb', 'bthusb'],
    'pci_bt_wifi': ['ibtpci', 'bthmini', 'netwaw', 'netwtw'],
    'usb_bt_wifi': ['ibtusb', 'bthusb', 'netwaw', 'netwtw'],
}

_LEVEL_MAP = {'1': 'Critical', '2': 'Error', '3': 'Warning', '4': 'Information', '5': 'Verbose'}

_UTC_PREFIX_RE = re.compile(r'^\(UTC([+-])(\d{2}):(\d{2})\)\s*(.*)$', re.IGNORECASE)
_UTC_GMT_OFFSET_RE = re.compile(r'\((?:UTC|GMT)([+-])(\d{2}):?(\d{2})\)', re.IGNORECASE)


def get_system_timezone(event_path):
    if not event_path:
        return ''

    search_dirs = []
    current_dir = os.path.dirname(event_path)
    for _ in range(4):
        if not current_dir or current_dir in search_dirs:
            break
        search_dirs.append(current_dir)
        current_dir = os.path.dirname(current_dir)
    for base_dir in search_dirs:
        direct_info = os.path.join(base_dir, 'system_info.txt')
        special_direct_info = os.path.join(base_dir, 'systeminfo.txt')
        if os.path.exists(direct_info):
            try:
                with open(direct_info, 'r', encoding='utf-8') as f:
                    return json.load(f).get('System Time Zone', '') or ''
            except Exception as e:
                print(f"[Error] reading system_info.txt for timezone: {e}")
                pass
        elif os.path.exists(special_direct_info):
            try:
                with open(special_direct_info, 'r', encoding='utf-16 le') as f:
                    for line in f:
                        if line.startswith('Time Zone:'):
                            return line.split(':', 1)[1].strip()
            except Exception as e:
                print(f"[Error] reading systeminfo.txt for timezone: {e}")
                pass

        try:
            for child_name in os.listdir(base_dir):
                child_dir = os.path.join(base_dir, child_name)
                child_info = os.path.join(child_dir, 'system_info.txt')
                child_special_info = os.path.join(child_dir, 'systeminfo.txt')
                if not os.path.isdir(child_dir):
                    continue

                if os.path.exists(child_info):
                    try:
                        with open(child_info, 'r', encoding='utf-8') as f:
                            tz_name = json.load(f).get('System Time Zone', '') or ''
                        if tz_name:
                            return tz_name
                    except Exception as e:
                        print(f"[Error] reading system_info.txt for timezone: {e}")
                        continue
                elif os.path.exists(child_special_info):
                    try:
                        with open(child_special_info, 'r', encoding='utf-16 le') as f:
                            for line in f:
                                if line.startswith('Time Zone:'):
                                    return line.split(':', 1)[1].strip()
                    except Exception as e:
                        print(f"[Error] reading systeminfo.txt for timezone: {e}")
                        continue
            
        except Exception:
            continue

    return ''


def convert_time(time_str, timezone_name):
    if not time_str or time_str == 'Unknown' or not timezone_name:
        return time_str
    try:
        event_time_utc = datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=tz.UTC)
        target_timezone = _resolve_timezone(timezone_name)
        if target_timezone is None:
            return time_str
        return event_time_utc.astimezone(target_timezone).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return time_str


def _resolve_timezone(timezone_name):
    tz_name = (timezone_name or '').strip()
    if not tz_name:
        return None

    # 1) Handle strings like: (UTC-08:00) Pacific Time (US & Canada)
    prefix_match = _UTC_PREFIX_RE.match(tz_name)
    if prefix_match:
        sign = 1 if prefix_match.group(1) == '+' else -1
        hours = int(prefix_match.group(2))
        minutes = int(prefix_match.group(3))
        total_minutes = sign * (hours * 60 + minutes)
        return timezone(timedelta(minutes=total_minutes))

        display_name = (prefix_match.group(4) or '').strip()
        if display_name:
            target_timezone = tz.gettz(display_name)
            if target_timezone is not None:
                return target_timezone

    # 2) Legacy handling for strings like: Pacific Standard Time (GMT-0800)
    legacy_name = tz_name.split(' (')[0].strip()
    if legacy_name:
        target_timezone = tz.gettz(legacy_name)
        if target_timezone is not None:
            return target_timezone

    # 3) Fallback: build fixed-offset timezone from UTC/GMT offset text.
    offset_match = _UTC_GMT_OFFSET_RE.search(tz_name)
    if offset_match:
        sign = 1 if offset_match.group(1) == '+' else -1
        hours = int(offset_match.group(2))
        minutes = int(offset_match.group(3))
        total_minutes = sign * (hours * 60 + minutes)
        return timezone(timedelta(minutes=total_minutes))

    # 4) Direct parse last to avoid dateutil misreading composite strings.
    return tz.gettz(tz_name)


def build_time_header(timezone_name):
    if not timezone_name:
        return 'Time'
    try:
        target_timezone = _resolve_timezone(timezone_name)
        if target_timezone is None:
            return 'Time'

        utc_offset = datetime.now(target_timezone).utcoffset()
        if utc_offset is None:
            return 'Time'

        total_minutes = int(utc_offset.total_seconds() // 60)
        sign = '+' if total_minutes >= 0 else '-'
        total_minutes = abs(total_minutes)
        hours = total_minutes // 60
        minutes = total_minutes % 60

        return f"Time (UTC{sign}{hours:02d}:{minutes:02d})"
    except Exception:
        return 'Time'


def _source_matches(source, source_filter):
    source_filter = (source_filter or 'all').lower()
    if source_filter == 'all':
        return True
    keywords = _SOURCE_GROUPS.get(source_filter)
    if not keywords:
        return True
    source_lower = (source or '').lower()
    return any(kw in source_lower for kw in keywords)


def _level_matches(level_text, level_filter):
    level_filter = (level_filter or 'all').lower()
    level_text = (level_text or '').lower()
    if level_filter == 'all':
        return True
    if level_filter == 'warning_error':
        return level_text in {'warning', 'error', 'critical'}
    if level_filter == 'information':
        return level_text == 'information'
    return True


def _load_raw_rows(path):
    system_timezone = get_system_timezone(path)
    if system_timezone:
        print(f"[UI View] Using system timezone: {system_timezone}")

    query_handle = win32evtlog.EvtQuery(
        path,
        win32evtlog.EvtQueryFilePath | win32evtlog.EvtQueryReverseDirection,
        None
    )

    ns = {'e': 'http://schemas.microsoft.com/win/2004/08/events/event'}
    events_list = []
    normal_kept = 0
    total_scanned = 0

    while True:
        batch = win32evtlog.EvtNext(query_handle, 100)
        if not batch:
            break
        for event in batch:
            total_scanned += 1
            try:
                root = ET.fromstring(win32evtlog.EvtRender(event, win32evtlog.EvtRenderEventXml))
                system = root.find('e:System', ns)
                if system is None:
                    continue

                level_val = getattr(system.find('e:Level', ns), 'text', '') or ''
                provider = system.find('e:Provider', ns)
                source = provider.get('Name', '') if provider is not None else ''

                is_important = level_val in ('1', '2', '3')
                is_special = any(kw in source.lower() for kw in _SPECIAL_SOURCES)

                if not (is_important or is_special):
                    if normal_kept >= 1000:
                        continue
                    normal_kept += 1

                time_created = system.find('e:TimeCreated', ns)
                time_str = time_created.get('SystemTime', '')[:19].replace('T', ' ') if time_created is not None else 'Unknown'

                event_id_elem = system.find('e:EventID', ns)
                id_str = event_id_elem.text if event_id_elem is not None else 'Unknown'

                event_data = root.find('e:EventData', ns)
                message = ''
                if event_data is not None:
                    message = ' | '.join(d.text or '' for d in event_data.findall('e:Data', ns) if d.text)

                events_list.append({
                    'time': time_str,
                    'level': _LEVEL_MAP.get(level_val, 'Unknown'),
                    'source': source,
                    'event_id': id_str,
                    'message': message[:500],
                })
            except Exception:
                continue

    print(f"[UI View] ✅ Scanned {total_scanned}, kept {len(events_list)} (normal: {normal_kept})")
    return events_list, system_timezone


def _get_cached_raw_rows(path):
    # print('[DEBUG] Checking cache for path:', path)
    mtime = os.path.getmtime(path)

    # Keep only the currently viewed event log in memory.
    if path not in _CACHE and _CACHE:
        # print('[DEBUG] Clearing cache for other paths')
        _CACHE.clear()

    cached = _CACHE.get(path)
    if cached and cached.get('mtime') == mtime:
        # print('[DEBUG] Cache hit for path:', path)
        return cached['events'], cached['system_timezone']
    
    events_list, system_timezone = _load_raw_rows(path)
    _CACHE[path] = {'mtime': mtime, 'events': events_list, 'system_timezone': system_timezone}
    return events_list, system_timezone


def get_paged_events(path, offset=0, limit=0, source_filter='all', level_filter='all'):
    """Return a page of filtered, timezone-converted events for the UI virtual scroll."""
    raw_events, system_timezone = _get_cached_raw_rows(path)

    filtered = [
        row for row in raw_events
        if _source_matches(row.get('source', ''), source_filter)
        and _level_matches(row.get('level', ''), level_filter)
    ]

    total = len(filtered)
    page = filtered[offset: offset + limit] if limit > 0 else filtered[offset:]

    events_out = [
        {
            'time': convert_time(row.get('time'), system_timezone),
            'level': row.get('level', ''),
            'source': row.get('source', ''),
            'event_id': row.get('event_id', ''),
            'message': row.get('message', ''),
        }
        for row in page
    ]

    return {
        'events': events_out,
        'total': total,
        'offset': offset,
        'limit': limit,
        'has_more': offset + len(events_out) < total,
        'time_header': build_time_header(system_timezone),
    }


def dump_event_to_txt(path):
    """Decode all events in an .evt/.evtx file to a plain-text file. Returns (txt_path, count)."""
    txt_path = f"{path}.txt"
    ns = {'e': 'http://schemas.microsoft.com/win/2004/08/events/event'}
    level_map = _LEVEL_MAP

    query_handle = win32evtlog.EvtQuery(
        path,
        win32evtlog.EvtQueryFilePath | win32evtlog.EvtQueryForwardDirection,
        None
    )

    count = 0
    with open(txt_path, 'w', encoding='utf-8') as f:
        while True:
            batch = win32evtlog.EvtNext(query_handle, 100)
            if not batch:
                break
            for event in batch:
                count += 1
                try:
                    root = ET.fromstring(win32evtlog.EvtRender(event, win32evtlog.EvtRenderEventXml))
                    system = root.find('e:System', ns)
                    if system is not None:
                        tc = system.find('e:TimeCreated', ns)
                        time_str = tc.get('SystemTime', '')[:19].replace('T', ' ') if tc is not None else 'Unknown'
                        prov = system.find('e:Provider', ns)
                        source_str = prov.get('Name', '') if prov is not None else 'Unknown'
                        lvl = system.find('e:Level', ns)
                        level_str = level_map.get(lvl.text if lvl is not None else '', 'Unknown')
                        eid = system.find('e:EventID', ns)
                        id_str = eid.text if eid is not None else 'Unknown'
                        event_data = root.find('e:EventData', ns)
                        message = ''
                        if event_data is not None:
                            message = ' | '.join(d.text or '' for d in event_data.findall('e:Data', ns) if d.text)
                        f.write(f"[{time_str}] [{level_str}] [{source_str}] Event ID: {id_str}\tMessage Data: {message}\n")
                except Exception as parse_e:
                    f.write(f"[Error parsing event]: {parse_e}\n")

    return txt_path, count
