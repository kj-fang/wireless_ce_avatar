import os
import xml.etree.ElementTree as ET
from datetime import datetime

try:
    import win32evtlog
except ImportError as e:
    print("Required modules not found. Please ensure 'pywin32' and 'python-dateutil' are installed.")
    raise e

# Shared timezone helpers — system_info.txt parsing + resolver are now in
# utils.timezone_utils so the chatbot / find_best_log / filter_folders_by_time
# paths can reuse the same logic without copy-pasting it.
from utils.timezone_utils import (
    get_effective_timezone as _get_effective_timezone,
    utc_to_local as _utc_to_local,
    format_tz_label as _format_tz_label,
)


_CACHE = {}

_SPECIAL_SOURCES = ['ibtusb', 'ibtpci', 'bthmini', 'bthusb', 'netwaw', 'netwtw']

_SOURCE_GROUPS = {
    'pci_bt':      ['ibtpci', 'bthmini'],
    'usb_bt':      ['ibtusb', 'bthusb'],
    'pci_bt_wifi': ['ibtpci', 'bthmini', 'netwaw', 'netwtw'],
    'usb_bt_wifi': ['ibtusb', 'bthusb', 'netwaw', 'netwtw'],
}

_LEVEL_MAP = {'1': 'Critical', '2': 'Error', '3': 'Warning', '4': 'Information', '5': 'Verbose'}


def get_system_timezone(event_path):
    """Backwards-compatible wrapper around the shared helper.

    Honours the manual override sidecar in addition to system_info.txt so
    event log views stay in lockstep with the chatbot's timezone picker.
    """
    return _get_effective_timezone(event_path)


def convert_time(time_str, timezone_name):
    """Convert a UTC-naive event log timestamp to the customer's local time."""
    if not time_str or time_str == 'Unknown' or not timezone_name:
        return time_str
    try:
        utc_naive = datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S')
        local_naive = _utc_to_local(utc_naive, timezone_name)
        if local_naive is None:
            return time_str
        return local_naive.strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return time_str


def build_time_header(timezone_name):
    """Pretty column-header label, e.g. "Time (UTC-05:00)"."""
    if not timezone_name:
        return 'Time'
    label = _format_tz_label(timezone_name)
    if not label:
        return 'Time'
    # `format_tz_label` returns "<name> (UTC±HH:MM)"; we only want the offset
    # in the column header to keep it compact.
    m = label.rsplit('(', 1)
    if len(m) == 2:
        return f"Time ({m[1].rstrip(')').strip()})"
    return f"Time ({label})"


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
    print(f"[System Event Log] evt file: {path}")
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
