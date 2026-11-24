"""
WiFi Driver Log Event Analyzer
Analyzes parsed WiFi driver ETL logs (TicTac3 output) to identify and explain
disconnect and roaming events with timestamps and root cause classification.

Usage:
    python wifi_event_analyzer.py <log_file_or_directory> [-d] [--verbose]
    python wifi_event_analyzer.py -TicTac3 <etl_file>    [-d] [--verbose]

  (default)     Print flow history + statistics summary (counts only)
  -d            Add per-event details: disconnect list, roam RSSI table
  --verbose     Add raw log context lines for each event (implies -d)
  -TicTac3 <f>  Parse ETL file with TicTac3 first, then analyze
"""

import re
import sys
import os
import subprocess
from dataclasses import dataclass, field
from enum import Enum

# Ensure UTF-8 output on Windows (avoids UnicodeEncodeError for -> and other non-cp1252 chars)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

VERSION = '1.0'

def _git_sha():
    """Return the short HEAD sha1, or 'unknown' if git is unavailable."""
    try:
        sha = subprocess.check_output(
            ['git', 'rev-parse', '--short', 'HEAD'],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return sha
    except Exception:
        return 'unknown'
from pathlib import Path


class EventType(Enum):
    DISCONNECT = "DISCONNECT"
    ROAM = "ROAM"
    CONNECTION_SUCCESS = "CONNECT"
    CONNECTION_FAIL = "CONNECT_FAIL"


class DisconnectReason(Enum):
    OS_INITIATED = "OS-initiated disconnect (TASK_DISCONNECT from WDI)"
    DRIVER_DISABLED = "Driver disabled (HALT_FW system flow - adapter disable/teardown)"
    PNP_DEVICE_RESTART = "PnP device restart (Close Adapter Handler / Miniport HALT)"
    ADAPTER_RESTART = "Adapter restart (driver + PnP teardown)"
    BEACON_LOSS = "Beacon loss (missed beacons from AP)"
    ROAM_WD_EXPIRED = "Driver roaming watchdog expired (internal disconnect after missed beacons + failed roam)"
    AP_DEAUTH = "AP sent deauthentication frame"
    AP_DISASSOC = "AP sent disassociation frame"
    LOW_RSSI = "Low RSSI / signal quality degradation"
    DRIVER_INTERNAL = "Driver internal error or reset"
    USER_INITIATED = "User-initiated disconnect"
    NETWORK_SWITCH = "Network profile switch (connecting to different SSID)"
    FW_ERROR = "Firmware error / assert"
    UNKNOWN = "Unknown disconnect reason"


class RoamReason(Enum):
    BG_SCAN_BETTER_AP = "Background scan found better AP"
    LOW_RSSI = "Low RSSI triggered roaming"
    BEACON_LOSS = "Beacon loss triggered roaming"
    BTM_REQUEST = "AP-initiated BSS Transition Management request"
    LINK_QUALITY = "Link quality degradation"
    AP_RANK_CHANGED = "AP ranking changed"
    AP_DEGRADATION = "Serving AP signal degraded"
    TOPOLOGY_PENALTY = "Topology penalty (current link/band penalized) triggered roaming"
    PERIODIC_SCAN = "Periodic scan triggered roaming evaluation"
    UNKNOWN = "Unknown roaming reason"


@dataclass
class WifiEvent:
    """Represents a WiFi disconnect or roaming event."""
    timestamp: str
    event_type: EventType
    reason: object  # DisconnectReason or RoamReason
    details: str
    context_lines: list = field(default_factory=list)
    ssid: str = ""
    bssid: str = ""
    rssi: str = ""              # RSSI at TASK_ROAM execution time (printCurrentConnectionParameters)
    rssi_at_trigger: str = ""     # RSSI when the roam decision was triggered (threshold crossing)
    target_bssid: str = ""
    ap_selection_factors: dict = field(default_factory=dict)


# Regex patterns for log line parsing
# Format 1: TicTac3 parsed ETL - "HH:MM:SS.mmm [MODULE] [LEVEL] [CPU] [FUNCTION] (line): message"
LOG_LINE_RE = re.compile(
    r'(\d{2}:\d{2}:\d{2}\.\d{3})\s+'
    r'\[([^\]]*)\]\s+'
    r'\[([^\]]*)\]\s+'
    r'\[(\d+)\]\s+'
    r'\[([^\]]*)\]\s*'
    r'(?:\((\d+)\):\s*)?'
    r'(.*)'
)

# Format 2: AutoTesting UT log - "SSSSSS.UUUUUU   [MODULE |CPU] [LEVEL] message"
UT_LOG_LINE_RE = re.compile(
    r'[\[]?(\d{6}\.\d{6})[\]]?\s+'
    r'\[([^\]|]*)\|?\s*(\d*)\]\s*'
    r'(?:\[([^\]]*)\]\s*)?'
    r'(.*)'
)

# Simpler timestamp extraction for lines that don't match full format
TIMESTAMP_RE = re.compile(r'(\d{2}:\d{2}:\d{2}\.\d{3}|\d{6}\.\d{6})')


def parse_log_line(line):
    """Parse a single log line into components."""
    m = LOG_LINE_RE.match(line.strip())
    if m:
        return {
            'timestamp': m.group(1),
            'module': m.group(2).strip(),
            'level': m.group(3).strip(),
            'cpu': m.group(4),
            'function': m.group(5).strip(),
            'line_num': m.group(6),
            'message': m.group(7).strip(),
            'raw': line.strip()
        }
    # Try UT log format
    m2 = UT_LOG_LINE_RE.match(line.strip())
    if m2:
        return {
            'timestamp': m2.group(1),
            'module': m2.group(2).strip(),
            'level': m2.group(4).strip() if m2.group(4) else '',
            'cpu': m2.group(3) or '',
            'function': '',
            'line_num': '',
            'message': m2.group(5).strip() if m2.group(5) else '',
            'raw': line.strip()
        }
    # Try just extracting timestamp
    m3 = TIMESTAMP_RE.search(line)
    if m3:
        return {
            'timestamp': m3.group(1),
            'module': '',
            'level': '',
            'cpu': '',
            'function': '',
            'line_num': '',
            'message': line.strip(),
            'raw': line.strip()
        }
    return None


def find_context(lines, idx, window=5):
    """Get surrounding context lines."""
    start = max(0, idx - window)
    end = min(len(lines), idx + window + 1)
    return [l.strip() for l in lines[start:end] if l.strip()]


def timestamp_to_seconds(ts):
    """Convert timestamp string to seconds (float). Handles both formats:
    - HH:MM:SS.mmm (TicTac3 parsed ETL)
    - SSSSSS.UUUUUU (AutoTesting UT log)
    """
    if ':' in ts:
        parts = ts.split(':')
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    else:
        # UT format: seconds.microseconds
        return float(ts)


def extract_ssid_from_context(lines, idx, window=20):
    """Search nearby lines for SSID information."""
    start = max(0, idx - window)
    end = min(len(lines), idx + window)
    for i in range(start, end):
        line = lines[i]
        # Look for SSID patterns
        m = re.search(r'[Ss][Ss][Ii][Dd][:\s=]+["\']?([^"\',\]\n]+)', line)
        if m:
            return m.group(1).strip()
        m = re.search(r'profileName[:\s=]+["\']?([^"\',\]\n]+)', line)
        if m:
            return m.group(1).strip()
    return ""


def extract_bssid_from_context(lines, idx, window=20):
    """Search nearby lines for BSSID information."""
    start = max(0, idx - window)
    end = min(len(lines), idx + window)
    for i in range(start, end):
        line = lines[i]
        m = re.search(r'[Bb][Ss][Ss][Ii][Dd][:\s=]+([0-9A-Fa-f:]{17})', line)
        if m:
            return m.group(1)
        m = re.search(r'Address\(([0-9A-Fa-f:]{17})\)', line)
        if m:
            return m.group(1)
    return ""


def extract_rssi_from_context(lines, idx, window=30):
    """Search nearby lines for RSSI value."""
    start = max(0, idx - window)
    end = min(len(lines), idx + window)
    for i in range(start, end):
        line = lines[i]
        m = re.search(r'[Rr]ssi[:\s=]+(-\d+)', line)
        if m:
            return m.group(1)
        m = re.search(r'rssi changed to (-\d+)', line)
        if m:
            return m.group(1)
    return ""


def extract_ap_selection_factors(lines, roam_idx):
    """Scan AP_SELECTION grading output after a roam trigger.

    Returns a dict with AP selection scoring factors that influenced why the
    driver decided to stay on the current AP or move:
        bt_connected         : bool  - BT was active during this AP selection run
        current_ap_ch_load   : bool  - current (active) AP received a channel-load penalty
        target_bt_coex       : bool  - winning candidate has BTcoex:TRUE flag
        poorly_disc_count    : int   - number of candidates penalised for being poorly discoverable
        current_ap_bssid     : str   - BSSID of the AP we were associated to
        target_ch_load_pct   : int   - channel-load % on the winning target AP (0 = not found)
        target_rssi          : str   - RSSI of the winning target AP (empty if not found)
    """
    factors = {
        'bt_connected':       False,
        'current_ap_ch_load': False,
        'target_bt_coex':     False,
        'poorly_disc_count':  0,
        'current_ap_bssid':   '',
        'target_ch_load_pct': 0,
        'target_rssi':        '',
        'prev_scan_rssi_map':    {},   # {bssid: rssi_str} from the AP_SELECTION run before this one
        'current_scan_rssi_map': {},   # {bssid: rssi_str} from the current (roam-triggering) scan
        'target_bw':             0,    # BW (MHz) of the winning target AP
        'current_scan_bw_map':   {},   # {bssid: bw_mhz_int} from the current scan
    }

    # Backward search: collect RSSI of each AP from the most recent previous AP_SELECTION run.
    # Two outputs:
    #   prev_scan_rssi_map  – [Best Candidate N] format only; drives new-AP prev/scan column.
    #   _bc0_appearances    – every [BC 0] line found going backward, reverse-chronological.
    #                          Used after the forward scan to identify the old AP's BSSID/RSSI
    #                          from the previous [BC N]-format scan.
    _prev = {}
    _bc0_appearances = []  # [(bssid, rssi)] in reverse-chronological order
    _bc_prev_scan    = {}  # [BC N] format: prev-scan entries captured via forward sub-scan
    _bc_prev_all     = {}  # [BC N] format fallback: all BC entries from most-recent backward [BC 0]
    for _bi in range(roam_idx - 1, max(0, roam_idx - 30000), -1):
        _bl = lines[_bi]
        if 'Triggering Roaming Flow due to reason' in _bl:
            # In multi-threaded logs the previous scan's [BC N] lines often appear AFTER
            # the trigger line (different VIF/scanner context).  Do a small forward sub-scan
            # from just past this trigger to capture them for prev_scan_rssi_map.
            _sub_end = min(len(lines), _bi + 500)
            for _si in range(_bi + 1, _sub_end):
                _sl = lines[_si]
                if 'Triggering Roaming Flow due to reason' in _sl:
                    break  # hit an even-older trigger; stop
                _bcm = re.search(
                    r'\[BC \d+\]:.*?RSSI:(-?\d+).*?Address\(([0-9A-Fa-f:]{17})\)', _sl)
                if _bcm and _bcm.group(2) not in _bc_prev_scan:
                    _bc_prev_scan[_bcm.group(2)] = _bcm.group(1)
            break  # end outer backward scan
        _bm = re.search(
            r'\[Best Candidate \d+\].*?RSSI:(-?\d+).*?Address\(([0-9A-Fa-f:]{17})\)', _bl)
        if _bm and _bm.group(2) not in _prev:
            _prev[_bm.group(2)] = _bm.group(1)
        if '[BC 0]:' in _bl:
            _bcm = re.search(
                r'\[BC 0\]:.*?RSSI:(-?\d+).*?Address\(([0-9A-Fa-f:]{17})\)', _bl)
            if _bcm:
                _bc0_appearances.append((_bcm.group(2), _bcm.group(1)))
                # Collect ALL [BC N] entries from this scan for prev_scan fallback
                # (only from the most-recent [BC 0] hit = first one going backward).
                if not _bc_prev_all:
                    for _pbi in range(_bi, min(len(lines), _bi + 30)):
                        _pbm = re.search(
                            r'\[BC \d+\]:.*?RSSI:(-?\d+).*?Address\(([0-9A-Fa-f:]{17})\)',
                            lines[_pbi])
                        if _pbm and _pbm.group(2) not in _bc_prev_all:
                            _bc_prev_all[_pbm.group(2)] = _pbm.group(1)
    # For [BC N]-format logs prev_scan_rssi_map comes from the sub-scan above (when a
    # previous trigger is found) or from the most-recent backward [BC 0] scan (_bc_prev_all);
    # for [Best Candidate N]-format logs it comes from the backward scan (_prev).
    factors['prev_scan_rssi_map'] = _prev if _prev else (_bc_prev_scan if _bc_prev_scan else _bc_prev_all)
    factors['_bc0_appearances']   = _bc0_appearances  # resolved at end of function

    search_end = min(len(lines), roam_idx + 1100)
    last_calc_bssid = ''   # BSSID from most recent "Calculating BSS entry grade for:"
    current_ap_bssid = ''

    for i in range(roam_idx, search_end):
        line = lines[i]

        # Stop when a new roaming evaluation starts
        if 'Triggering Roaming Flow due to reason' in line and i > roam_idx + 5:
            break

        # BT connection state - emitted once per AP selection run
        if 'BT is Connected' in line and 'NOT' not in line:
            factors['bt_connected'] = True

        # Track which AP is currently being graded
        m = re.search(r'Calculating BSS entry grade for:\s*Address\(([0-9A-Fa-f:]{17})\)', line)
        if m:
            last_calc_bssid = m.group(1)

        # Mark the AP that is the active (associated) entry
        if 'Candidate is the active entry' in line and last_calc_bssid:
            current_ap_bssid = last_calc_bssid
            factors['current_ap_bssid'] = current_ap_bssid

        # Channel-load penalty applied to a specific candidate
        m = re.search(
            r'Penalty for\s+AP_SELECTION_CHANNEL_LOAD.*Address\(([0-9A-Fa-f:]{17})\)', line)
        if m and current_ap_bssid and m.group(1) == current_ap_bssid:
            factors['current_ap_ch_load'] = True

        # Poorly-discoverable penalty (candidate not seen in last scan)
        if 'AP_SELECTION_POORLY_DISCOVERABLE_AP_PENALTY' in line and 'Penalty for' in line:
            factors['poorly_disc_count'] += 1

        # Best Candidate 0 summary line - contains final chLoad and BTcoex for winner
        # Format: [Best Candidate 0]: band:1, ..., RSSI:-73, ..., chLoad:27, BTcoex:FALSE, ...
        m = re.search(r'\[Best Candidate 0\].*chLoad:(\d+).*BTcoex:(TRUE|FALSE)', line)
        if m:
            factors['target_ch_load_pct'] = int(m.group(1))
            if m.group(2) == 'TRUE':
                factors['target_bt_coex'] = True
            m_r = re.search(r'RSSI:(-?\d+)', line)
            if m_r:
                factors['target_rssi'] = m_r.group(1)
            # Collect this and subsequent Best Candidate lines (for current_scan_rssi_map)
            # then stop after a short window so we don't scan TASK_ROAM parameters.
            _bc_limit = min(len(lines), i + 20)
            for _bci in range(i, _bc_limit):
                _bcm = re.search(
                    r'\[Best Candidate \d+\].*?RSSI:(-?\d+).*?Address\(([0-9A-Fa-f:]{17})\)',
                    lines[_bci])
                if _bcm and _bcm.group(2) not in factors['current_scan_rssi_map']:
                    factors['current_scan_rssi_map'][_bcm.group(2)] = _bcm.group(1)
            break  # done with this AP_SELECTION run
        # Alternative format: [BC 0]: grade:... RSSI:-52 ... Address(...)
        if '[BC 0]:' in line:
            m_r = re.search(r'RSSI:(-?\d+)', line)
            if m_r:
                factors['target_rssi'] = m_r.group(1)
            m_addr = re.search(r'Address\(([0-9A-Fa-f:]{17})\)', line)
            if m_addr:
                factors['target_bssid_bc'] = m_addr.group(1)  # new AP BSSID; used to resolve old AP
            m_bw = re.search(r'BW:(\d+)MHz', line)
            if m_bw:
                factors['target_bw'] = int(m_bw.group(1))
            # Collect this and subsequent BC lines (for current_scan_rssi_map / current_scan_bw_map)
            _bc_limit = min(len(lines), i + 20)
            for _bci in range(i, _bc_limit):
                _bl_bc = lines[_bci]
                _bcm = re.search(
                    r'\[BC \d+\]:.*?RSSI:(-?\d+).*?Address\(([0-9A-Fa-f:]{17})\)', _bl_bc)
                if _bcm:
                    _b2 = _bcm.group(2)
                    if _b2 not in factors['current_scan_rssi_map']:
                        factors['current_scan_rssi_map'][_b2] = _bcm.group(1)
                    if _b2 not in factors['current_scan_bw_map']:
                        _m_bw = re.search(r'BW:(\d+)MHz', _bl_bc)
                        if _m_bw:
                            factors['current_scan_bw_map'][_b2] = int(_m_bw.group(1))
            # Do NOT break here — there may be a second AP_SELECTION run (the TASK_ROAM
            # execution scan) whose [BC 0] is the actual roam target.  The outer loop
            # already stops at the next 'Triggering Roaming Flow' event.

    # Resolve old AP BSSID and RSSI from the previous [BC N]-format scan.
    # _bc0_appearances is reverse-chronological; the first entry whose BSSID != the current
    # roam's target (new AP) is the previous scan's winner = the AP we are roaming away from.
    _target_bc = factors.pop('target_bssid_bc', '')
    _bc0s = factors.pop('_bc0_appearances', [])  # reverse-chronological list of (bssid, rssi)
    factors['prev_bc0_bssid'] = ''
    factors['prev_bc0_rssi']  = ''
    # Identify old AP: first [BC 0] entry that is not the new AP (target).
    _old_ap_bc = ''
    for _bssid, _rssi in _bc0s:
        if _bssid != _target_bc:
            _old_ap_bc = _bssid
            break
    # T1 = RSSI from the OLDEST scan in old AP's consecutive [BC 0] winning streak.
    # Going backward (reverse-chron), we walk while the BSSID equals old AP and keep
    # updating; when a different BSSID appears the streak ends.  The last update before
    # that break is the oldest / streak-start entry, which corresponds to when the driver
    # first selected the old AP -- i.e., the connection-time scan.
    if _old_ap_bc:
        _streak_bssid = ''
        _streak_rssi  = ''
        _in_streak = False
        for _bssid, _rssi in _bc0s:
            if _bssid == _target_bc:
                continue  # skip current-scan [BC 0] (the new AP)
            if _bssid == _old_ap_bc:
                _streak_bssid = _bssid
                _streak_rssi  = _rssi
                _in_streak = True
            elif _in_streak:
                break     # streak ended at a different winner -- stop here
        if _streak_bssid:
            factors['prev_bc0_bssid'] = _streak_bssid
            factors['prev_bc0_rssi']  = _streak_rssi

    return factors


def _bg_scan_real_reason(factors):
    """Infer the underlying reason a BG_SCAN triggered a roam from AP_SELECTION data.
    Compares new AP RSSI vs old AP RSSI in the roam-triggering scan."""
    target_rssi  = factors.get('target_rssi', '')
    # current_ap_bssid is set for [Best Candidate N] format; prev_bc0_bssid covers [BC N] format
    cur_bssid    = factors.get('current_ap_bssid', '') or factors.get('prev_bc0_bssid', '')
    # Prefer current-scan RSSI (most accurate for Grade/RSSI decision); fall back to prev-scan
    old_ap_rssi  = (factors.get('current_scan_rssi_map', {}).get(cur_bssid, '') or
                    factors.get('prev_bc0_rssi', ''))
    ch_load      = factors.get('current_ap_ch_load', False)
    target_bw    = factors.get('target_bw', 0)
    old_ap_bw    = factors.get('current_scan_bw_map', {}).get(cur_bssid, 0)

    if target_rssi and old_ap_rssi:
        try:
            if int(target_rssi) >= int(old_ap_rssi):
                return 'RSSI'           # new AP has equal or better RSSI
            # New AP won on grade despite weaker signal; check if BW upgrade is the cause
            if target_bw and old_ap_bw and target_bw > old_ap_bw:
                return 'BW'             # wider channel on new AP drove the grade advantage
            return 'Ch.Load' if ch_load else 'Grade'
        except ValueError:
            pass
    return 'Ch.Load' if ch_load else 'RSSI'


def analyze_disconnect(lines, idx, parsed_line):
    """Analyze a disconnect event and determine root cause."""
    context = find_context(lines, idx, window=15)
    context_str = '\n'.join(context)

    # Check for PnP device restart (Close Adapter Handler)
    close_adapter_nearby = any(
        'CloseAdapte' in lines[i] or 'Close Adapter' in lines[i]
        for i in range(max(0, idx - 5), min(len(lines), idx + 30))
    )

    # Check for NIC halt / miniport halt
    nic_halt_nearby = any(
        'ACTIVE' in lines[i] and 'STDBY' in lines[i]
        for i in range(max(0, idx), min(len(lines), idx + 30))
    )

    # Check for beacon loss
    beacon_loss = any(
        re.search(r'beacon[Ll]oss|beacon[Mm]iss|BEACON_LOSS|missedBeacon', lines[i])
        for i in range(max(0, idx - 30), min(len(lines), idx + 5))
    )

    # Check for AP deauth
    ap_deauth = any(
        re.search(r'[Rr]eceived.*[Dd]eauth|AP.*deauth|deauth.*from.*AP|RxDeauth', lines[i])
        for i in range(max(0, idx - 10), min(len(lines), idx + 10))
    )

    # Check for network switch
    network_switch = any(
        re.search(r'user wants to establish a new connection|TASK_CONNECT', lines[i])
        for i in range(max(0, idx), min(len(lines), idx + 30))
    )

    # Check for FW error
    fw_error = any(
        re.search(r'uCode ASSERT|FW_ERROR|fwError|NMI|firmwareError', lines[i])
        for i in range(max(0, idx - 20), min(len(lines), idx + 10))
    )

    # Check TASK_DISCONNECT pattern (OS-initiated)
    is_task_disconnect = 'TASK_DISCONNECT' in parsed_line.get('message', '')

    # Check for driver disable (LCM HALT_FW system flow). When the adapter is
    # disabled/torn down, the OS issues TASK_DISCONNECT and the LCM serializer
    # requests the HALT_FW flow. The flow request can be hundreds of log lines
    # away from the disconnect (depending on log density) yet only a fraction of
    # a second apart, so search by TIME proximity (+/- 3 seconds) rather than by
    # a fixed line-count window.
    disc_sec = None
    m_ts = TIMESTAMP_RE.search(parsed_line.get('raw', '') or lines[idx])
    if m_ts:
        try:
            disc_sec = timestamp_to_seconds(m_ts.group(1))
        except (ValueError, IndexError):
            disc_sec = None

    driver_disabled = False
    if disc_sec is not None:
        for i in range(idx, min(len(lines), idx + 3000)):
            if 'HALT_FW' in lines[i]:
                lm = TIMESTAMP_RE.search(lines[i])
                if lm:
                    try:
                        if abs(timestamp_to_seconds(lm.group(1)) - disc_sec) <= 3.0:
                            driver_disabled = True
                            break
                    except (ValueError, IndexError):
                        pass
    else:
        driver_disabled = any(
            'HALT_FW' in lines[i]
            for i in range(max(0, idx - 60), min(len(lines), idx + 60))
        )

    # Determine reason
    if fw_error:
        reason = DisconnectReason.FW_ERROR
        details = "Firmware error/assert detected before disconnect"
    elif close_adapter_nearby or nic_halt_nearby:
        reason = DisconnectReason.PNP_DEVICE_RESTART
        details = "OS PnP subsystem restarted the WiFi adapter (device stack teardown and reinit)"
    elif beacon_loss:
        reason = DisconnectReason.BEACON_LOSS
        details = "Connection lost due to missed beacons from the AP"
    elif ap_deauth:
        reason = DisconnectReason.AP_DEAUTH
        details = "AP sent deauthentication frame to the station"
    elif network_switch:
        reason = DisconnectReason.NETWORK_SWITCH
        details = "Disconnect to switch to a different network profile"
    elif driver_disabled:
        reason = DisconnectReason.DRIVER_DISABLED
        details = "Driver was disabled - LCM serializer requested the HALT_FW system flow (adapter disable/teardown)"
    elif is_task_disconnect:
        reason = DisconnectReason.OS_INITIATED
        details = "Windows WDI framework sent TASK_DISCONNECT to the driver"
    else:
        reason = DisconnectReason.UNKNOWN
        details = "Could not determine specific disconnect reason from available log context"

    # Extract additional info
    deauth_reason = ""
    for i in range(max(0, idx - 5), min(len(lines), idx + 20)):
        m = re.search(r'deauthReason[:\s=]+(0x[0-9A-Fa-f]+|\d+)', lines[i])
        if m:
            deauth_reason = m.group(1)
            break
        m = re.search(r'Deauth reason[:\s]+(0x[0-9A-Fa-f]+|\d+)', lines[i])
        if m:
            deauth_reason = m.group(1)
            break

    if deauth_reason:
        details += f" [deauth reason: {deauth_reason}]"

    term_reason = ""
    for i in range(max(0, idx - 5), min(len(lines), idx + 20)):
        m = re.search(r'terminationReason[:\s=]+(0x[0-9A-Fa-f]+|\d+)', lines[i])
        if m:
            term_reason = m.group(1)
            break

    if term_reason:
        details += f" [termination reason: {term_reason}]"

    event = WifiEvent(
        timestamp=parsed_line['timestamp'],
        event_type=EventType.DISCONNECT,
        reason=reason,
        details=details,
        context_lines=context,
        ssid=extract_ssid_from_context(lines, idx),
        bssid=extract_bssid_from_context(lines, idx),
        rssi=extract_rssi_from_context(lines, idx),
    )
    return event


def analyze_roam(lines, idx, parsed_line, connected_index=None):
    """Analyze a roaming event and determine trigger reason.

    Roaming FSM flow in the driver:
    1. 'Triggering Roaming Flow due to reason ROAM_REASON_*' - evaluation starts
    2. If NOT executed: 'PRE_TASK_ROAM ==> ROAMING_DECISION_STATE_ASSOCIATED'
    3. If EXECUTED:
       a. 'PRE_TASK_ROAM ==> ROAMING_DECISION_STATE_WAITING_FOR_TASK_ROAM'
       b. OS sends 'Got Command (M1 Message) TASK_ROAM'
       c. 'IND_ASSOC_RESULT[0] - status=0, ... reassoc=1' (success)
       d. 'WDI_IND_ROAM_COMPLETE - task complete indication. Status= 0x0'
    """
    msg = parsed_line.get('message', '')
    context = find_context(lines, idx, window=10)

    # Extract roam reason from log
    reason_match = re.search(
        r'ROAM_REASON_(\w+)|[Rr]oam reason[:\s]+(\w+)|'
        r'Triggering Roaming Flow due to reason (\w+)',
        msg
    )

    if not reason_match:
        # Search in nearby lines
        for i in range(max(0, idx - 3), min(len(lines), idx + 3)):
            reason_match = re.search(
                r'ROAM_REASON_(\w+)|[Rr]oam reason[:\s]+(\w+)|'
                r'Triggering Roaming Flow due to reason (\w+)',
                lines[i]
            )
            if reason_match:
                break

    roam_reason_str = ""
    if reason_match:
        roam_reason_str = next(g for g in reason_match.groups() if g)

    # Map to RoamReason enum
    if 'BG_SCAN' in roam_reason_str or 'PERIODIC' in roam_reason_str:
        reason = RoamReason.BG_SCAN_BETTER_AP
        details = "Background scan completed and found a potentially better AP candidate"
    elif 'LOW_RSSI' in roam_reason_str or 'RSSI' in roam_reason_str:
        reason = RoamReason.LOW_RSSI
        details = "Signal strength dropped below roaming threshold"
    elif 'BEACON' in roam_reason_str or 'MISS' in roam_reason_str:
        reason = RoamReason.BEACON_LOSS
        details = "Missed beacons from current AP triggered roaming"
    elif 'DEGRAD' in roam_reason_str:
        reason = RoamReason.AP_DEGRADATION
        details = "Serving AP signal degraded (active-link RSSI/distance check) triggered roaming"
        # Look back for the specific degradation message from bssVifIsApDegradedApi
        # (logged a few lines before the "Triggering Roaming Flow" anchor line)
        try:
            _degrad_trigger_sec = timestamp_to_seconds(parsed_line['timestamp'])
        except (ValueError, IndexError):
            _degrad_trigger_sec = None
        for _j in range(idx - 1, max(0, idx - 40) - 1, -1):
            _lj = lines[_j]
            if _degrad_trigger_sec is not None:
                _lpj = parse_log_line(_lj)
                if _lpj:
                    try:
                        if abs(timestamp_to_seconds(_lpj['timestamp']) - _degrad_trigger_sec) > 2.0:
                            break
                    except (ValueError, IndexError):
                        pass
            _m_bss = re.search(
                r'Bss load has increased by at least (\d+)'
                r' \(stored chUtiliztion = (\d+), current chUtiliztion = (\d+)\)',
                _lj)
            if _m_bss:
                details += " [Channel load]"
                break
            _m_lat = re.search(
                r'Latency Penalty has increased by at least (\d+)'
                r' \(stored latencyPenalty = (\d+), current latencyPenalty = (\d+)\)',
                _lj)
            if _m_lat:
                details += " [Latency penalty]"
                break
    elif 'TOPOLOGY' in roam_reason_str:
        reason = RoamReason.TOPOLOGY_PENALTY
        details = "Current link/band received a topology penalty, triggering roaming evaluation"
    elif 'BTM' in roam_reason_str or 'BSS_TM' in roam_reason_str:
        reason = RoamReason.BTM_REQUEST
        details = "AP sent BSS Transition Management request (suggesting roam)"
    elif 'LINK' in roam_reason_str or 'QUALITY' in roam_reason_str:
        reason = RoamReason.LINK_QUALITY
        details = "Link quality degraded triggering roaming evaluation"
    elif 'AP_RANK' in roam_reason_str:
        reason = RoamReason.AP_RANK_CHANGED
        details = "AP ranking changed based on signal/quality metrics"
    else:
        reason = RoamReason.UNKNOWN
        details = f"Roaming triggered (raw reason: {roam_reason_str or 'not specified'})"

    # Determine execution status using FSM state transitions
    # Look ahead for the definitive state transition after PRE_TASK_ROAM.
    # NOTE: AP_SELECTION grading + scan can produce thousands of lines before the
    # FSM settles, so use a generous 3000-line window.
    # State name differences across log formats:
    #   ETL logs: ROAMING_DECISION_STATE_WAITING_FOR_TASK_ROAM
    #   UT logs:  ROAMING_DECISION_STATE_ROAM_DECISION_IN_TASK_ROAM
    roam_executed = False
    roam_aborted = False
    roam_completed_ok = False
    roam_failed = False
    roam_wd_timeout = False  # roam was executed but WD fired before OS could connect
    roam_os_abort = False    # roam was executing but OS sent PROP_ABORT_TASK
    fail_reason = ""
    try:
        trigger_sec = timestamp_to_seconds(parsed_line['timestamp'])
    except (ValueError, IndexError):
        trigger_sec = None
    search_limit = min(len(lines), idx + 3000)
    for i in range(idx, search_limit):
        line_i = lines[i]
        # Skip lines that are far in wall-clock time from this trigger. In concatenated
        # ring-buffer logs the forward window can contain lines from a DIFFERENT capture
        # section (hours apart) whose FSM / TASK_ROAM transitions belong to other roams.
        # A roam resolves within a couple of seconds, so anything >10s away is not ours.
        if trigger_sec is not None:
            lp_skip = parse_log_line(line_i)
            if lp_skip:
                try:
                    if abs(timestamp_to_seconds(lp_skip['timestamp']) - trigger_sec) > 10.0:
                        continue
                except (ValueError, IndexError):
                    pass
        # Definitive NO: went back to ASSOCIATED without roaming
        if 'PRE_TASK_ROAM ==> ROAMING_DECISION_STATE_ASSOCIATED' in line_i and i > idx:
            roam_executed = False
            break
        # Definitive YES (ETL format): proceeded to WAITING_FOR_TASK_ROAM
        if 'PRE_TASK_ROAM ==> ROAMING_DECISION_STATE_WAITING_FOR_TASK_ROAM' in line_i:
            roam_executed = True
            break
        # Definitive YES (UT format): PRE_TASK_ROAM ==> IN_TASK_ROAM
        if 'PRE_TASK_ROAM ==> ROAMING_DECISION_STATE_ROAM_DECISION_IN_TASK_ROAM' in line_i:
            roam_executed = True
            break
        # Also detect via secondary FSM transition (both formats)
        if 'WAITING_FOR_TASK_ROAM ==> ROAMING_DECISION_STATE_IS_SECONDARY' in line_i or \
           'IN_TASK_ROAM ==> ROAMING_DECISION_STATE_IS_SECONDARY' in line_i:
            roam_executed = True
            break
        # Fallback: OS sent TASK_ROAM (used when FSM log is in a different ring-buffer
        # section and the forward search can't reach the FSM transition line). The
        # top-of-loop proximity guard already ensures this match is time-local.
        if re.search(r'Got Command.*TASK_ROAM', line_i):
            roam_executed = True
            break
        # Aborted: roaming flow was cancelled mid-way
        if 'PRE_TASK_ROAM ==> ROAMING_DECISION_STATE_ABORTING' in line_i:
            roam_aborted = True
            break
        # Stop if we hit the next roam trigger (avoid crossing event boundaries)
        if 'Triggering Roaming Flow due to reason' in line_i and i > idx:
            break

    # Check if roam completed successfully or failed to connect.
    # The window extends to the next roam trigger so a failure that resolves only
    # after several auth retries (ending in 'Roam Failed') is still captured.
    if roam_executed:
        for i in range(idx, min(len(lines), idx + 8000)):
            line_c = lines[i]
            # The emergency WD timeout ends the roam. Any CONNECTION FAILED that follows
            # belongs to the OS reconnect (WDI_TASK_CONNECT), NOT to this roam attempt.
            if 'EmergencyRoamingWD' in line_c and 'Triggering disconnection' in line_c:
                roam_wd_timeout = True
                break
            # OS cancelled the roam task mid-flight (e.g. higher-priority OS request)
            if 'PROP_ABORT_TASK' in line_c:
                roam_os_abort = True
                break
            # Definitive success - wins over any earlier failed attempt
            if ('WDI_IND_ROAM_COMPLETE' in line_c or 'ROAM COMPLETE' in line_c) and \
               ('Status= 0x0' in line_c or 'lastRoamCompleted' in line_c):
                roam_completed_ok = True
                roam_failed = False
                break
            # Capture connection-failure reason (e.g. AUTH_TX_FAILURE(9)); keep
            # scanning since a later retry within the same roam may still succeed.
            m = re.search(r'CONNECTION FAILED - (\w+\(\d+\))', line_c)
            if m:
                roam_failed = True
                fail_reason = m.group(1)
            # Definitive roam failure (final outcome of the roaming decision)
            if 'ROAMING DECISION' in line_c and 'Roam Failed' in line_c:
                roam_failed = True
                break
            if 'BSS_VIF_CLC_SM_EV_FAILED_TO_CONNECT' in line_c:
                roam_failed = True
                break
            # Check for failed association
            m = re.search(r'IND_ASSOC_RESULT.*status=([1-9]\d*)', line_c)
            if m:
                roam_completed_ok = False
            # Stop at next trigger
            if 'Triggering Roaming Flow due to reason' in line_c and i > idx + 5:
                break

    if roam_aborted:
        details += " [roam ABORTED - attempt was cancelled mid-flow]"
    elif not roam_executed:
        details += " [evaluated but did NOT execute roam - stayed on current AP]"
    elif roam_wd_timeout:
        details += (" [roam EXECUTED - emergency WD timer fired before OS could connect; "
                    "subsequent AUTH failures belong to the OS reconnect, not this roam]")
    elif roam_os_abort:
        details += " [roam ABORTED by OS - PROP_ABORT_TASK]"
    elif roam_failed:
        if fail_reason:
            details += f" [roam FAILED to connect - {fail_reason}]"
        else:
            details += " [roam FAILED to connect]"
    elif roam_completed_ok:
        details += " [roam EXECUTED - successfully associated to new AP]"
    else:
        details += " [roam EXECUTED]"

    # Collect AP selection scoring factors for executed roams
    ap_sel_factors = {}
    if roam_executed or roam_aborted:
        ap_sel_factors = extract_ap_selection_factors(lines, idx)

    # Find target BSSID: prefer the actual 'CONNECTED - to:' result (authoritative),
    # fall back to [Best Candidate 0] from AP selection (can be wrong in multi-buffer logs)
    target_bssid = ""
    if roam_executed:
        search_end = min(len(lines), idx + 3500)
        # Pass 1: look for the CONNECTED indication - most accurate
        for i in range(idx, search_end):
            m = re.search(r'CONNECTED - to:\s+\S+\s+([0-9A-Fa-f:]{17})', lines[i])
            if m:
                target_bssid = m.group(1)
                break
            if 'Triggering Roaming Flow due to reason' in lines[i] and i > idx + 2:
                break
        # Pass 2: timestamp-based lookup in the pre-built connected_index
        # Preferred over Best Candidate 0 because it uses the actual connection result.
        # Essential for multi-ring-buffer logs where completion is in a different section.
        if not target_bssid and connected_index:
            try:
                trigger_sec = timestamp_to_seconds(parsed_line['timestamp'])
                for ts_sec, bssid in connected_index:
                    if 0 < ts_sec - trigger_sec < 5.0:
                        target_bssid = bssid
                        break
            except (ValueError, IndexError):
                pass
        # Pass 3: fall back to Best Candidate 0 from AP selection
        if not target_bssid:
            for i in range(idx, search_end):
                m = re.search(r'Best candidate.*BSSID:\s*([0-9A-Fa-f:]{17})', lines[i])
                if m:
                    target_bssid = m.group(1)
                    break
                m = re.search(r'\[Best Candidate 0\].*Address\(([0-9A-Fa-f:]{17})\)', lines[i])
                if m:
                    target_bssid = m.group(1)
                    break
                if 'Triggering Roaming Flow due to reason' in lines[i] and i > idx + 2:
                    break

    # Extract current AP BSSID from AP_SELECTION context (look backwards)
    current_bssid = ""
    current_rssi = ""
    # Look for RSSI in the printCurrentConnectionParameters near TASK_ROAM
    for i in range(max(0, idx - 5), min(len(lines), idx + 1000)):
        m = re.search(r'printCurrentConnectionParameters.*RSSI\[(-?\d+)\]', lines[i])
        if m:
            current_rssi = m.group(1)
            break
        # Stop at next trigger
        if 'Triggering Roaming Flow due to reason' in lines[i] and i > idx + 5:
            break

    # Fall back to generic RSSI extraction
    if not current_rssi:
        current_rssi = extract_rssi_from_context(lines, idx)

    # Decision RSSI: most-recent 'AP rssi changed to X' before this trigger
    # (from roamingDecisionHandleRssiLevelChanged, logged right before triggering roaming)
    rssi_at_trigger = ""
    for i in range(idx - 1, max(0, idx - 100) - 1, -1):
        m_t = re.search(r'AP rssi changed to (-?\d+)', lines[i])
        if m_t:
            rssi_at_trigger = m_t.group(1)
            break
        lp_t = parse_log_line(lines[i])
        if lp_t and trigger_sec is not None:
            try:
                if trigger_sec - timestamp_to_seconds(lp_t['timestamp']) > 5.0:
                    break
            except (ValueError, IndexError):
                pass

    event = WifiEvent(
        timestamp=parsed_line['timestamp'],
        event_type=EventType.ROAM,
        reason=reason,
        details=details,
        context_lines=context,
        ssid=extract_ssid_from_context(lines, idx),
        bssid=extract_bssid_from_context(lines, idx),
        rssi=current_rssi,
        rssi_at_trigger=rssi_at_trigger,
        target_bssid=target_bssid,
        ap_selection_factors=ap_sel_factors,
    )
    return event


def _dedup_adapter_restart(disc_list):
    """Merge DRIVER_DISABLED + PNP_DEVICE_RESTART pairs within 2 s into one ADAPTER_RESTART event.

    When the adapter is torn down, two separate log lines fire:
      - TASK_DISCONNECT (classified as DRIVER_DISABLED via the HALT_FW proximity search)
      - Close Adapter Handler (force-classified as PNP_DEVICE_RESTART)
    Both describe the same hardware restart cycle.  Keep the earlier timestamp and
    reclassify to ADAPTER_RESTART; drop the duplicate.
    """
    _FAMILY = {DisconnectReason.DRIVER_DISABLED, DisconnectReason.PNP_DEVICE_RESTART}
    skip = set()
    result = []
    for i, d in enumerate(disc_list):
        if i in skip:
            continue
        if d.reason in _FAMILY:
            for j in range(i + 1, len(disc_list)):
                if disc_list[j].reason in _FAMILY:
                    try:
                        dt = abs(timestamp_to_seconds(disc_list[j].timestamp)
                                 - timestamp_to_seconds(d.timestamp))
                    except (ValueError, IndexError):
                        dt = 999.0
                    if dt <= 2.0:
                        d.reason = DisconnectReason.ADAPTER_RESTART
                        d.details = ("Adapter teardown - driver initiated HALT_FW flow "
                                     "and OS completed PnP removal/restart")
                        skip.add(j)
                        break
        result.append(d)
    return result


def analyze_log_file(filepath, verbose=False, detail=False):
    """Main analysis function - scans a log file for disconnect and roaming events."""
    print(f"\n{'='*80}")
    print(f"  WiFi Event Analyzer v{VERSION} (sha1: {_git_sha()})")
    print(f"  File: {filepath}")
    print(f"{'='*80}\n")

    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()

    print(f"  Log file: {len(lines)} lines\n")

    # Pre-build a sorted list of (timestamp_sec, bssid) from all CONNECTED events.
    # Used as a cross-section fallback for target BSSID lookup in multi-buffer logs.
    connected_index = []
    for line in lines:
        m = re.search(r'CONNECTED - to:\s+\S+\s+([0-9A-Fa-f:]{17})', line)
        if m:
            lp = parse_log_line(line)
            if lp:
                try:
                    connected_index.append((timestamp_to_seconds(lp['timestamp']), m.group(1)))
                except (ValueError, IndexError):
                    pass
    connected_index.sort()

    events = []
    connections = []  # separate list: successful and failed connection attempts
    roam_indications = []  # (timestamp_str, timestamp_sec) for INDICATION_ROAMING_NEEDED
    processed_timestamps = set()
    task_connect_ts = None   # timestamp of the active TASK_CONNECT, None if none in progress

    for idx, line in enumerate(lines):
        # Skip if we already processed an event near this line
        parsed = parse_log_line(line)
        if not parsed:
            continue

        ts = parsed['timestamp']

        # === DISCONNECT DETECTION ===
        # Pattern 1: WDI_TASK_DISCONNECT received
        if 'WDI_TASK_DISCONNECT' in line and 'got task' in line:
            if ts not in processed_timestamps:
                processed_timestamps.add(ts)
                event = analyze_disconnect(lines, idx, parsed)
                events.append(event)

        # Pattern 2: Close Adapter Handler (PnP removal)
        elif 'Close Adapter Handler' in line or 'CloseAdapterHandler' in line:
            if ts not in processed_timestamps:
                processed_timestamps.add(ts)
                parsed['message'] = line.strip()
                event = analyze_disconnect(lines, idx, parsed)
                # Override to PnP restart
                event.reason = DisconnectReason.PNP_DEVICE_RESTART
                event.details = "OS called Close Adapter Handler - PnP device removal/restart in progress"
                events.append(event)

        # Pattern 3: Beacon loss disconnect
        elif re.search(r'beaconLoss.*disconnect|beacon.*loss.*trigger.*disconnect', line, re.IGNORECASE):
            if ts not in processed_timestamps:
                processed_timestamps.add(ts)
                event = WifiEvent(
                    timestamp=ts,
                    event_type=EventType.DISCONNECT,
                    reason=DisconnectReason.BEACON_LOSS,
                    details="Beacon loss caused disconnect from AP",
                    context_lines=find_context(lines, idx),
                    rssi=extract_rssi_from_context(lines, idx),
                    bssid=extract_bssid_from_context(lines, idx),
                )
                events.append(event)

        # Pattern 4: Emergency roaming watchdog - driver-internal disconnect decision.
        # The ROAM_DECISION FSM watchdog fired because beacons were missed AND all
        # roam/connection attempts failed within the watchdog window.
        # Root cause: INTERNAL DRIVER DECISION, not the AP or OS.
        elif 'EmergencyRoamingWD' in line and 'Triggering disconnection' in line:
            if ts not in processed_timestamps:
                processed_timestamps.add(ts)
                event = WifiEvent(
                    timestamp=ts,
                    event_type=EventType.DISCONNECT,
                    reason=DisconnectReason.ROAM_WD_EXPIRED,
                    details="Emergency roaming watchdog (ROAM_DECISION FSM) expired - "
                            "driver internally triggered disconnect after missed beacons and failed roam attempts",
                    context_lines=find_context(lines, idx),
                    rssi=extract_rssi_from_context(lines, idx),
                    bssid=extract_bssid_from_context(lines, idx),
                )
                events.append(event)

        # Pattern 5: AP sent deauthentication frame        # Matches: "<-- DEAUTH (reason_X)" in DP_MONITOR RX line
        # or: "DEAUTH - received from:" in CNCT_FLOW
        # or: "BSS_VIF_CLC_SM_EV_DEAUTH_DISASSOC_RECEIVED"
        elif re.search(r'<--\s*DEAUTH|DEAUTH\s*-\s*received from|DEAUTH_DISASSOC_RECEIVED|RxDeauth|hLdlHandleDeauthReceived', line):
            if ts not in processed_timestamps:
                processed_timestamps.add(ts)
                # Extract deauth reason code
                reason_code = ""
                m = re.search(r'DEAUTH.*reason[_:\s]+(\d+)|reason[_:\s]+(\d+).*DEAUTH', line, re.IGNORECASE)
                if m:
                    reason_code = next(g for g in m.groups() if g)
                else:
                    # Search nearby lines for reason code
                    for i in range(idx, min(len(lines), idx + 5)):
                        m = re.search(r'<--\s*DEAUTH\s*\(reason[_]?(\d+)\)|deauthReason[:\s=]+(\d+)', lines[i])
                        if m:
                            reason_code = next(g for g in m.groups() if g)
                            break

                # Extract AP BSSID from "DEAUTH - received from: <SSID> XX:XX:XX:XX:XX:XX"
                ap_bssid = ""
                ap_ssid = ""
                for i in range(max(0, idx - 2), min(len(lines), idx + 5)):
                    m = re.search(r'DEAUTH\s*-\s*received from:\s*"([^"]*)"?\s*([0-9A-Fa-f:]{17})', lines[i])
                    if m:
                        ap_ssid = m.group(1).strip('"').strip()
                        ap_bssid = m.group(2)
                        break
                    m = re.search(r'DEAUTH.*Address\(([0-9A-Fa-f:]{17})\)', lines[i])
                    if m:
                        ap_bssid = m.group(1)
                        break

                reason_str = f"reason code {reason_code}" if reason_code else "unknown reason"
                ssid_str = f'"{ap_ssid}" ' if ap_ssid else ""
                details = f"AP {ssid_str}sent deauthentication frame ({reason_str})"

                event = WifiEvent(
                    timestamp=ts,
                    event_type=EventType.DISCONNECT,
                    reason=DisconnectReason.AP_DEAUTH,
                    details=details,
                    context_lines=find_context(lines, idx),
                    ssid=ap_ssid,
                    bssid=ap_bssid or extract_bssid_from_context(lines, idx),
                    rssi=extract_rssi_from_context(lines, idx),
                )
                events.append(event)

        # === ROAMING DETECTION ===
        # Pattern 1: Roaming flow triggered (driver-initiated evaluation)
        # This is the PRIMARY roaming event - captures the full flow start
        elif 'Triggering Roaming Flow due to reason' in line:
            # Every 'Triggering Roaming Flow' line is a distinct roam evaluation, so always
            # emit an event. Do NOT gate on 'ts not in processed_timestamps': that shared set
            # is keyed on the bare HH:MM:SS.mmm timestamp and would silently drop a roam
            # trigger whenever it collides with another event (disconnect/deauth/earlier roam)
            # at the same millisecond. We still record the ts so a following OS TASK_ROAM is
            # recognised as a follow-up rather than counted as a separate event.
            processed_timestamps.add(ts)
            event = analyze_roam(lines, idx, parsed, connected_index)
            events.append(event)

        # Pattern 2: TASK_ROAM from OS without a preceding "Triggering Roaming Flow"
        # Only capture these if there was NO driver-initiated trigger within 30 seconds.
        # A driver-initiated roam trigger starts a scan which can take many seconds before
        # the OS sends TASK_ROAM, so the old 2-second window was too narrow.
        elif re.search(r'Got Command.*TASK_ROAM', line):
            if ts not in processed_timestamps:
                # Check 1: an already-detected ROAM event covers this TASK_ROAM.
                # Scan ALL prior ROAM events (not just the last few): in concatenated
                # ring-buffer logs the originating trigger can be tens of thousands of
                # lines away in a different section, so a small tail window misses it.
                is_followup = False
                curr_sec = timestamp_to_seconds(ts)
                for prev_evt in reversed(events):
                    if prev_evt.event_type == EventType.ROAM:
                        try:
                            prev_sec = timestamp_to_seconds(prev_evt.timestamp)
                            diff = curr_sec - prev_sec
                            if 0 <= diff < 30.0:
                                is_followup = True
                                break
                        except (ValueError, IndexError):
                            pass

                # Check 2: backward scan in raw lines for 'Triggering Roaming Flow'
                # within 30 seconds (covers cases where Pattern 1 event isn't in events yet)
                if not is_followup:
                    for i in range(max(0, idx - 5000), idx):
                        if 'Triggering Roaming Flow due to reason' in lines[i]:
                            trig_p = parse_log_line(lines[i])
                            if trig_p:
                                try:
                                    trig_sec = timestamp_to_seconds(trig_p['timestamp'])
                                    curr_sec = timestamp_to_seconds(ts)
                                    if 0 <= curr_sec - trig_sec < 30.0:
                                        is_followup = True
                                except (ValueError, IndexError):
                                    pass

                # Check 3: forward scan for 'Triggering Roaming Flow' that follows this
                # 'Got Command TASK_ROAM' line.  The OS TASK_ROAM OID arrives BEFORE the
                # driver logs "Triggering Roaming Flow" (different threads), so Pattern 1
                # can appear a few hundred lines later.  If we find such a trigger within
                # 500 lines and within 10 seconds, Pattern 1 will own this event; suppress
                # Pattern 2 so we don't create a duplicate with wrong "EXECUTED" details.
                if not is_followup:
                    for i in range(idx + 1, min(len(lines), idx + 500)):
                        if 'Triggering Roaming Flow due to reason' in lines[i]:
                            trig_p = parse_log_line(lines[i])
                            if trig_p:
                                try:
                                    trig_sec = timestamp_to_seconds(trig_p['timestamp'])
                                    curr_sec = timestamp_to_seconds(ts)
                                    if 0 <= trig_sec - curr_sec < 10.0:
                                        is_followup = True
                                except (ValueError, IndexError):
                                    pass
                            break  # only look at the first trigger found

                if not is_followup:
                    processed_timestamps.add(ts)
                    # Try to determine the real reason by searching backwards for
                    # bssVifOnRssiChangedAPI or other trigger context
                    inferred_reason = RoamReason.UNKNOWN
                    inferred_details = "OS sent TASK_ROAM command to driver (no prior driver-initiated trigger) [OS-initiated roam EXECUTED]"
                    rssi_val = ""

                    # Search backwards (up to 2000 lines) for RSSI change or BG_SCAN trigger
                    for i in range(max(0, idx - 2000), idx):
                        line_i = lines[i]
                        # Pattern: bssVifOnRssiChangedAPI - RSSI dropped below threshold
                        m = re.search(
                            r'bssVifOnRssiChangedAPI.*Rssi Level Changed from (-?\d+) to (-?\d+).*threshold[:\s]+\[?(-?\d+)',
                            line_i)
                        if m:
                            rssi_from = m.group(1)
                            rssi_to = m.group(2)
                            threshold = m.group(3)
                            inferred_reason = RoamReason.LOW_RSSI
                            inferred_details = (
                                f"Low RSSI triggered roaming (RSSI changed {rssi_from} -> {rssi_to} dBm, "
                                f"threshold: {threshold} dBm) [roam EXECUTED]"
                            )
                            rssi_val = rssi_to
                            # Don't break - keep searching for more recent one

                        # Pattern: BG_SCAN completed trigger
                        if 'ROAM_REASON_BG_SCAN_COMPLETE' in line_i or 'roamingDecisionFindIfBetter' in line_i:
                            if 'BG_SCAN' in line_i:
                                inferred_reason = RoamReason.BG_SCAN_BETTER_AP
                                inferred_details = "Background scan found better AP [roam EXECUTED]"

                    # Execution RSSI: printCurrentConnectionParameters at TASK_ROAM time
                    # (always extracted independently from the decision RSSI)
                    exec_rssi = ""
                    for i in range(max(0, idx - 2), min(len(lines), idx + 60)):
                        m_ex = re.search(r'printCurrentConnectionParameters.*RSSI\[(-?\d+)\]', lines[i])
                        if m_ex:
                            exec_rssi = m_ex.group(1)
                            break
                    if not exec_rssi:
                        for i in range(max(0, idx - 2), min(len(lines), idx + 60)):
                            m_ex = re.search(r'RSSI\[(-?\d+)\]', lines[i])
                            if m_ex:
                                exec_rssi = m_ex.group(1)
                                break

                    # Search forward for target BSSID from AP_SELECTION "Best candidate"
                    target_bssid = ""
                    for i in range(idx, min(len(lines), idx + 1000)):
                        m = re.search(r'Best candidate.*BSSID:\s*([0-9A-Fa-f:]{17})', lines[i])
                        if m:
                            target_bssid = m.group(1)
                            break
                        m = re.search(r'\[Best Candidate 0\].*Address\(([0-9A-Fa-f:]{17})\)', lines[i])
                        if m:
                            target_bssid = m.group(1)
                            break
                        # Stop at next roaming trigger
                        if 'Triggering Roaming Flow due to reason' in lines[i] and i > idx + 2:
                            break
                    # Fallback: first Address() in the TASK_ROAM OID dump (Preferred BSS Entry List)
                    if not target_bssid:
                        for i in range(idx, min(len(lines), idx + 30)):
                            m = re.search(r'Address\(([0-9A-Fa-f:]{17})\)', lines[i])
                            if m:
                                target_bssid = m.group(1)
                                break

                    event = WifiEvent(
                        timestamp=ts,
                        event_type=EventType.ROAM,
                        reason=inferred_reason,
                        details=inferred_details,
                        context_lines=find_context(lines, idx),
                        bssid=extract_bssid_from_context(lines, idx),
                        rssi=exec_rssi or rssi_val or extract_rssi_from_context(lines, idx),
                        rssi_at_trigger=rssi_val,
                        target_bssid=target_bssid,
                        ap_selection_factors=extract_ap_selection_factors(lines, idx),
                    )
                    events.append(event)

        # Pattern 3: Roam due to beacon loss
        elif re.search(r'[Rr]oam.*beacon.*[Ll]oss|beacon.*[Mm]iss.*roam|'
                       r'ROAM_REASON_BEACON', line):
            if ts not in processed_timestamps:
                processed_timestamps.add(ts)
                event = WifiEvent(
                    timestamp=ts,
                    event_type=EventType.ROAM,
                    reason=RoamReason.BEACON_LOSS,
                    details="Beacon loss triggered roaming to find a new AP",
                    context_lines=find_context(lines, idx),
                    bssid=extract_bssid_from_context(lines, idx),
                    rssi=extract_rssi_from_context(lines, idx),
                )
                events.append(event)

        # === CONNECTION DETECTION ===
        # Track TASK_CONNECT lifecycle so PROP_ABORT_TASK can be attributed correctly.
        # The "Got Command" line uses the short name "TASK_CONNECT[6]" (no WDI_ prefix).
        elif re.search(r'Got Command.*\bTASK_CONNECT\b', line):
            task_connect_ts = ts

        # PROP_ABORT_TASK during an active TASK_CONNECT = connect failure caused by OS
        elif 'PROP_ABORT_TASK' in line and task_connect_ts is not None:
            connections.append(WifiEvent(
                timestamp=ts,
                event_type=EventType.CONNECTION_FAIL,
                reason=None,
                details="OS abort (PROP_ABORT_TASK)",
                bssid="",
            ))
            task_connect_ts = None

        # Pattern: successful connection (hPolicyAssociationSuccessEvent)
        # "CONNECTED - to:  SSID  BSSID, channel ..."
        elif 'CONNECTED - to:' in line:
            m = re.search(r'CONNECTED - to:\s+(\S+)\s+([0-9A-Fa-f:]{17})', line)
            ssid_c = m.group(1) if m else ""
            bssid_c = m.group(2) if m else ""
            ch_m = re.search(r'channel[:\s]+(\d+)', line)
            ch_str = f" ch{ch_m.group(1)}" if ch_m else ""
            conn_rssi = ""
            if idx + 1 < len(lines):
                m_rssi = re.search(r'AVG_RSSI_ON_ASSOC.*Rssi:(-?\d+)', lines[idx + 1])
                if m_rssi:
                    conn_rssi = m_rssi.group(1)
            connections.append(WifiEvent(
                timestamp=ts,
                event_type=EventType.CONNECTION_SUCCESS,
                reason=None,
                details=f"Connected to {ssid_c}{ch_str}",
                ssid=ssid_c,
                bssid=bssid_c,
                rssi=conn_rssi,
            ))
            task_connect_ts = None  # TASK_CONNECT completed successfully

        # Fallback: some driver builds do not log hPolicyAssociationSuccessEvent
        # ("CONNECTED - to:") for an initial TASK_CONNECT.  The first EAPOL RECEIVED
        # packet confirms 802.11 association succeeded.  Fire only when a TASK_CONNECT
        # is still open (task_connect_ts set) so roam-time EAPOL is never misclassified.
        elif 'EAPOL RECEIVED' in line and task_connect_ts is not None:
            # Recover target details from the [ATTEMPT_TO_CONNECT] line emitted by
            # bssVifLinkProcessAttemptToConnect during the same TASK_CONNECT flow.
            ssid_c, bssid_c, ch_c, rssi_c = "", "", "", ""
            for bi in range(idx - 1, max(0, idx - 5000), -1):
                ma = re.search(
                    r'\[ATTEMPT_TO_CONNECT\]\s+Ssid:(\S+)\s+Ch:(\d+)\s+Rssi:(-?\d+)\s+Bssid:\s+Address\(([0-9A-Fa-f:]{17})\)',
                    lines[bi])
                if ma:
                    ssid_c, ch_c, rssi_c, bssid_c = ma.group(1), ma.group(2), ma.group(3), ma.group(4)
                    break
                if 'Got Command' in lines[bi] and 'TASK_CONNECT' in lines[bi]:
                    break  # do not cross into a previous connection attempt
            connections.append(WifiEvent(
                timestamp=task_connect_ts,
                event_type=EventType.CONNECTION_SUCCESS,
                reason=None,
                details=f"Connected to {ssid_c}" + (f" ch{ch_c}" if ch_c else ""),
                ssid=ssid_c,
                bssid=bssid_c,
                rssi=rssi_c,
            ))
            task_connect_ts = None  # consumed

        # Pattern: OS indicates roaming is needed (INDICATION_ROAMING_NEEDED)
        # Collect for post-processing: if followed by a WD-timeout disconnect within
        # ~2 s, it means the roam was aborted by the watchdog before completion.
        # Look backward (up to 2000 lines) for the most recent roam-trigger reason.
        elif 'INDICATION_ROAMING_NEEDED' in line and 'coreInd:' in line:
            try:
                ind_reason = "unknown reason"
                ind_sec = timestamp_to_seconds(ts)
                # Use a large line window (50000); the 5-second time check below
                # is the real stop condition.  Dense AP-selection output can span
                # 10000+ lines in under one second, making a small line limit
                # miss the trigger that fired only milliseconds earlier.
                for bi in range(idx - 1, max(0, idx - 50000), -1):
                    rm = re.search(r'Triggering Roaming Flow due to reason (ROAM_REASON_\w+)', lines[bi])
                    if rm:
                        # Map raw reason string to human-readable label via RoamReason logic
                        rr = rm.group(1)
                        if 'BG_SCAN' in rr or 'PERIODIC' in rr:
                            ind_reason = RoamReason.BG_SCAN_BETTER_AP.value
                        elif 'RSSI' in rr or 'LOW_RSSI' in rr:
                            ind_reason = RoamReason.LOW_RSSI.value
                        elif 'BEACON' in rr or 'MISS' in rr:
                            ind_reason = RoamReason.BEACON_LOSS.value
                        elif 'BTM' in rr or 'BSS_TM' in rr:
                            ind_reason = RoamReason.BTM_REQUEST.value
                        elif 'LINK' in rr or 'QUALITY' in rr:
                            ind_reason = RoamReason.LINK_QUALITY.value
                        elif 'AP_RANK' in rr:
                            ind_reason = RoamReason.AP_RANK_CHANGED.value
                        elif 'DEGRAD' in rr:
                            ind_reason = RoamReason.AP_DEGRADATION.value
                        elif 'TOPOLOGY' in rr:
                            ind_reason = RoamReason.TOPOLOGY_PENALTY.value
                        else:
                            ind_reason = rr
                        break
                    # Stop search if we cross into a different time window (>5 s)
                    lp_b = parse_log_line(lines[bi])
                    if lp_b:
                        try:
                            if ind_sec - timestamp_to_seconds(lp_b['timestamp']) > 5.0:
                                break
                        except (ValueError, IndexError):
                            pass
                roam_indications.append((ts, ind_sec, ind_reason))
            except (ValueError, IndexError):
                pass

        # Pattern: connection failure (cnctPluginBssFailureApPropertiesM)
        # "CONNECTION FAILED - AUTH_RESPONSE_FAIL(7)"
        elif 'CONNECTION FAILED' in line and 'CNCT_FLOW' in line:
            reason_m = re.search(r'CONNECTION FAILED\s*-\s*([\w_]+(?:\(\d+\))?)', line)
            fail_reason = reason_m.group(1) if reason_m else "unknown"
            # BSSID appears in nearby AUTH_RSP line: "AUTH_RSP - received from: SSID BSSID"
            bssid_fail = ""
            for bi in range(max(0, idx - 15), idx + 1):
                bm = re.search(r'AUTH_RSP.*from:\s+\S+\s+([0-9A-Fa-f:]{17})', lines[bi])
                if bm:
                    bssid_fail = bm.group(1)
                    break
            if not bssid_fail:
                bssid_fail = extract_bssid_from_context(lines, idx, window=15)
            connections.append(WifiEvent(
                timestamp=ts,
                event_type=EventType.CONNECTION_FAIL,
                reason=None,
                details=fail_reason,
                bssid=bssid_fail,
            ))
            task_connect_ts = None  # TASK_CONNECT completed (failed)

        # Pattern 4: BTM request roam
        elif re.search(r'BTM.*[Rr]equest|BSS.*Transition.*Management|btmReq', line):
            if ts not in processed_timestamps:
                processed_timestamps.add(ts)
                event = WifiEvent(
                    timestamp=ts,
                    event_type=EventType.ROAM,
                    reason=RoamReason.BTM_REQUEST,
                    details="AP sent BSS Transition Management (BTM) request advising STA to roam",
                    context_lines=find_context(lines, idx),
                    bssid=extract_bssid_from_context(lines, idx),
                    rssi=extract_rssi_from_context(lines, idx),
                )
                events.append(event)

    # === OUTPUT RESULTS ===
    if not events:
        print("  No disconnect or roaming events found in this log.\n")
        return events

    # Separate by type
    disconnects = _dedup_adapter_restart(
        [e for e in events if e.event_type == EventType.DISCONNECT])
    conn_success = [e for e in connections if e.event_type == EventType.CONNECTION_SUCCESS]
    conn_fail    = [e for e in connections if e.event_type == EventType.CONNECTION_FAIL]
    # Every roaming evaluation the driver performed (each 'Triggering Roaming Flow due to
    # reason ...' line, plus any standalone OS TASK_ROAM). Reported in full so the count
    # matches the raw log; the detailed sections below still split these into
    # attempted-move vs evaluated-only.
    all_roams = [e for e in events if e.event_type == EventType.ROAM]
    roams = [e for e in all_roams if 'did NOT execute' not in e.details]
    evaluated_only = [e for e in all_roams if 'did NOT execute' in e.details]

    # Print summary - report the FULL roaming-evaluation count plus the breakdown.
    print(f"  SUMMARY: Found {len(disconnects)} disconnect(s) and {len(all_roams)} roaming "
          f"event(s) ({len(roams)} attempted a move, {len(evaluated_only)} evaluated-only)\n")

    # === FLOW HISTORY (chronological merge of all key events) ===
    def _flow_sort_key(evt):
        try:
            return timestamp_to_seconds(evt.timestamp)
        except (ValueError, IndexError):
            return 0.0

    # Build flow entries: (timestamp, sort_key, type_label, description)
    # Pre-identify CONNECT OK events that are direct results of a ROAM OK so they
    # can be suppressed (the roam line already carries the outcome).
    consumed_conns     = set()  # indices into conn_success consumed by a roam
    roam_ssid_map      = {}     # id(roam_event) -> SSID string from the matching conn_success
    roam_conn_rssi_map = {}     # id(roam_event) -> RSSI of new AP at connection time
    roam_windows       = []     # (roam_sec, conn_sec, id(roam_event)) for successful roams
    for e in roams:
        if any(k in e.details for k in ('FAILED to connect', 'WD timer fired',
                                         'emergency WD timer fired', 'ABORTED by OS',
                                         'did NOT execute')):
            continue
        try:
            roam_sec = timestamp_to_seconds(e.timestamp)
        except (ValueError, IndexError):
            continue
        for j, c in enumerate(conn_success):
            if j in consumed_conns:
                continue
            try:
                conn_sec = timestamp_to_seconds(c.timestamp)
            except (ValueError, IndexError):
                continue
            bssid_match = (not e.target_bssid) or (e.target_bssid == c.bssid)
            if 0 < conn_sec - roam_sec < 8.0 and bssid_match:
                consumed_conns.add(j)
                roam_ssid_map[id(e)] = c.ssid
                roam_conn_rssi_map[id(e)] = c.rssi
                roam_windows.append((roam_sec, conn_sec, id(e)))
                break

    # Build old_ap_conn_rssi_map: id(roam_event) -> RSSI when we first connected to that AP.
    # Walk a merged timeline of standalone CONNECT OK events and ROAM OK events in order,
    # carrying forward the RSSI at each "connection established" point.
    _roam_ok_ids = {id(e) for e in roams if not any(k in e.details for k in (
        'FAILED to connect', 'WD timer fired', 'emergency WD timer fired',
        'ABORTED by OS', 'did NOT execute'))}
    _tl = []
    for _j, _c in enumerate(conn_success):
        if _j in consumed_conns:
            continue  # result of a roam — RSSI already tracked in roam_conn_rssi_map
        try:
            _sk = timestamp_to_seconds(_c.timestamp)
        except (ValueError, IndexError):
            _sk = 0.0
        _tl.append((_sk, _c.rssi, None))
    for _e in roams:
        if id(_e) not in _roam_ok_ids:
            continue
        try:
            _sk = timestamp_to_seconds(_e.timestamp)
        except (ValueError, IndexError):
            _sk = 0.0
        _tl.append((_sk, roam_conn_rssi_map.get(id(_e), ''), id(_e)))
    _tl.sort(key=lambda x: x[0])
    old_ap_conn_rssi_map = {}   # id(roam_event) -> RSSI when old AP was first connected
    _last_conn_rssi = ''
    for _sk, _rssi, _eid in _tl:
        if _eid is not None:
            old_ap_conn_rssi_map[_eid] = _last_conn_rssi
        _last_conn_rssi = _rssi

    # Suppress conn_fail entries that are intermediate auth retries during a
    # roam that ultimately succeeded.  These fall between TASK_ROAM start and
    # the successful CONNECTED event and add noise to the flow history.
    consumed_conn_fails = set()   # indices into conn_fail to hide
    roam_auth_retries   = {}      # id(roam_event) -> retry count
    for k, cf in enumerate(conn_fail):
        try:
            cf_sec = timestamp_to_seconds(cf.timestamp)
        except (ValueError, IndexError):
            continue
        for r_sec, c_sec, r_id in roam_windows:
            if r_sec <= cf_sec <= c_sec + 0.5:
                consumed_conn_fails.add(k)
                roam_auth_retries[r_id] = roam_auth_retries.get(r_id, 0) + 1
                break

    # Abbreviations shared by flow history and statistics table
    _REASON_ABBR = {
        RoamReason.LOW_RSSI.value:          "Low RSSI",
        RoamReason.BG_SCAN_BETTER_AP.value: "BG Scan",
        RoamReason.AP_DEGRADATION.value:    "AP Degrad.",
        RoamReason.BEACON_LOSS.value:       "Beacon Loss",
        RoamReason.BTM_REQUEST.value:       "BTM Req",
        RoamReason.TOPOLOGY_PENALTY.value:  "Topology",
        RoamReason.LINK_QUALITY.value:      "Link Qual.",
        RoamReason.AP_RANK_CHANGED.value:   "AP Rank",
    }

    # flow_entries 7-tuple: (timestamp, sort_key, type_lbl, old_rssi, new_rssi, target_bssid, trigger)
    flow_entries = []
    for e in disconnects:
        flow_entries.append((e.timestamp, _flow_sort_key(e),
                             "DISCONNECT  ", "", "", "", e.reason.value))
    for e in roams:
        if 'FAILED to connect' in e.details:
            # True connection failure (WD did not fire before the auth/assoc failed)
            fail_m = re.search(r'\[roam FAILED to connect - ([^\]]+)\]', e.details)
            fail_r = fail_m.group(1) if fail_m else "connection failed"
            rsn = _REASON_ABBR.get(e.reason.value, e.reason.value)
            flow_entries.append((e.timestamp, _flow_sort_key(e),
                                 "CONN FAIL(roam)", "", "", e.target_bssid or "",
                                 f"{fail_r}  [by: {rsn}]"))
        elif 'WD timer fired' in e.details or 'emergency WD timer fired' in e.details:
            # WD-aborted: already covered by the ROAM FAIL(WD) entry from
            # INDICATION_ROAMING_NEEDED detection — skip to avoid duplication.
            pass
        elif 'ABORTED by OS' in e.details:
            conn_r = old_ap_conn_rssi_map.get(id(e), '')
            exec_r = e.rssi
            old_s = f"{conn_r}/{exec_r}" if (conn_r and exec_r) else (conn_r or exec_r)
            rsn = _REASON_ABBR.get(e.reason.value, e.reason.value)
            flow_entries.append((e.timestamp, _flow_sort_key(e),
                                 "ROAM ABORT(OS)", old_s, "", e.target_bssid or "",
                                 f"{rsn}  [OS ABORT]"))
        elif 'did NOT execute' not in e.details:
            ssid_s = f"  {roam_ssid_map[id(e)]}" if id(e) in roam_ssid_map else ""
            prev_rssi  = e.ap_selection_factors.get('prev_scan_rssi_map', {}).get(e.target_bssid or '', '')
            new_rssi_scan = e.ap_selection_factors.get('target_rssi', '')
            new_rssi_conn = roam_conn_rssi_map.get(id(e), '')
            # T1 = prev-scan RSSI (NA if new AP not visible in the scan before this one)
            # T2 = current-scan RSSI; fall back to connection RSSI only when scan data absent
            if new_rssi_scan:
                _t1 = prev_rssi if prev_rssi else 'NA'
                new_rssi = f"{_t1}/{new_rssi_scan}"
            elif new_rssi_conn:
                new_rssi = f"N/A/{new_rssi_conn}"
            else:
                new_rssi = ""
            conn_r = old_ap_conn_rssi_map.get(id(e), '')
            exec_r = e.rssi   # RSSI of old AP at T2 (TASK_ROAM execution)
            # T1: prev_bc0_rssi is the old AP's RSSI from the previous [BC N]-format scan
            # (the scan where the old AP was still [BC 0] = winner). Use it when available.
            _ap_sel_t1 = e.ap_selection_factors.get('prev_bc0_rssi', '')
            t1_r = _ap_sel_t1 or conn_r
            old_s = f"{t1_r}/{exec_r}" if (t1_r and exec_r) else (t1_r or exec_r)
            # For AP_DEGRADATION roams, extract the specific degradation sub-reason
            _dm = re.search(r'\[((?:Channel load|Latency penalty))\]', e.details)
            degrad_s = f" [{_dm.group(1)}]" if _dm else ""
            rsn = _REASON_ABBR.get(e.reason.value, e.reason.value)
            if e.reason == RoamReason.BG_SCAN_BETTER_AP:
                rsn = f"{rsn} [{_bg_scan_real_reason(e.ap_selection_factors)}]"
            retry_n = roam_auth_retries.get(id(e), 0)
            retry_s = f"  [{retry_n} auth retr.]" if retry_n else ""
            flow_entries.append((e.timestamp, _flow_sort_key(e),
                                 "ROAM OK     ", old_s, new_rssi, e.target_bssid or "",
                                 f"{rsn}{degrad_s}{ssid_s}{retry_s}"))
    for j, e in enumerate(conn_success):
        if j in consumed_conns:
            continue  # already shown on the ROAM OK line
        ch_str = e.details[e.details.find(' ch'):] if ' ch' in e.details else ''
        flow_entries.append((e.timestamp, _flow_sort_key(e),
                             "CONNECT OK  ", "", "", e.bssid,
                             f"{e.ssid}{ch_str}"))
    for k, e in enumerate(conn_fail):
        if k in consumed_conn_fails:
            continue  # intermediate auth failure during a roam that ultimately succeeded
        flow_entries.append((e.timestamp, _flow_sort_key(e),
                             "CONNECT FAIL", "", "", e.bssid or "", e.details))

    # INDICATION_ROAMING_NEEDED events aborted by the emergency WD timer:
    # if the OS triggered a roam but the WD fired within 2 s before any successful
    # connection, the roam never completed — show it as ROAM FAIL(WD).
    disc_wdtimeout = [e for e in disconnects if e.reason == DisconnectReason.ROAM_WD_EXPIRED]
    for ind_ts, ind_sec, ind_reason in roam_indications:
        for wddisc in disc_wdtimeout:
            try:
                wd_sec = timestamp_to_seconds(wddisc.timestamp)
                if 0 < wd_sec - ind_sec <= 2.0:
                    flow_entries.append((ind_ts, ind_sec, "ROAM FAIL(WD)",
                                         "", "", "",
                                         f"{ind_reason}  [WD expired]"))
                    break
            except (ValueError, IndexError):
                pass

    flow_entries.sort(key=lambda x: x[1])

    print(f"  {'='*70}")
    print(f"  FLOW HISTORY  (chronological)")
    print(f"  {'='*70}")
    _FW = (3, 13, 17, 14, 17, 22)
    _FH_HDR1 = (f"  {'#':>{_FW[0]}}  {'Timestamp':<{_FW[1]}}  "
                f"{'Old AP (dBm)':<{_FW[2]}}  {'New AP':<{_FW[3]}}  "
                f"{'Target BSSID':<{_FW[4]}}  Trigger")
    _FH_HDR2 = (f"  {'':>{_FW[0]}}  {'HH:MM:SS.mmm':<{_FW[1]}}  "
                f"{'@T1/@T2':<{_FW[2]}}  {'prev/scan':<{_FW[3]}}  "
                f"{'':>{_FW[4]}}")
    _FH_SEP  = (f"  {'-'*_FW[0]}  {'-'*_FW[1]}  "
                f"{'-'*_FW[2]}  {'-'*_FW[3]}  "
                f"{'-'*_FW[4]}  {'-'*40}")
    print(_FH_HDR1)
    print(_FH_HDR2)
    print(_FH_SEP)
    for i, (ts_f, _, type_lbl, old_rssi, new_rssi, tgt_bssid, trigger) in enumerate(flow_entries, 1):
        trig_col = f"{type_lbl.strip():<15}  {trigger}" if trigger else type_lbl.strip()
        print(f"  {i:>{_FW[0]}}  {ts_f:<{_FW[1]}}  "
              f"{old_rssi or '':<{_FW[2]}}  {new_rssi or '':<{_FW[3]}}  "
              f"{tgt_bssid or '':<{_FW[4]}}  {trig_col}")
    print(_FH_SEP)
    print()

    # Print disconnects
    if detail and disconnects:
        print(f"  {'='*70}")
        print(f"  DISCONNECT EVENTS ({len(disconnects)})")
        print(f"  {'='*70}")
        for i, evt in enumerate(disconnects, 1):
            print(f"\n  [{i}] Time: {evt.timestamp}")
            print(f"      Reason: {evt.reason.value}")
            print(f"      Details: {evt.details}")
            if evt.ssid:
                print(f"      SSID: {evt.ssid}")
            if evt.bssid:
                print(f"      BSSID: {evt.bssid}")
            if evt.rssi:
                print(f"      RSSI: {evt.rssi} dBm")
            if verbose and evt.context_lines:
                print(f"      Context:")
                for cl in evt.context_lines[:8]:
                    print(f"        | {cl[:150]}")

    # Print roaming events
    if detail and all_roams:
        print(f"\n  {'='*70}")
        print(f"  ROAMING EVENTS ({len(all_roams)})")
        print(f"  {'='*70}")
        for i, evt in enumerate(all_roams, 1):
            print(f"\n  [{i}] Time: {evt.timestamp}")
            print(f"      Reason: {evt.reason.value}")
            print(f"      Details: {evt.details}")
            if evt.bssid:
                print(f"      Current BSSID: {evt.bssid}")
            if evt.target_bssid:
                print(f"      Target BSSID: {evt.target_bssid}")
            if evt.rssi:
                print(f"      RSSI: {evt.rssi} dBm")
            # Show AP selection factors inline for executed/aborted roams
            af = evt.ap_selection_factors
            if af:
                factors_str = []
                if af.get('bt_connected'):
                    factors_str.append("BT active")
                if af.get('current_ap_ch_load'):
                    factors_str.append("current-AP channel-load penalty")
                if af.get('target_bt_coex'):
                    factors_str.append("target BTcoex:TRUE")
                if factors_str:
                    print(f"      AP Selection: {', '.join(factors_str)}")
            if verbose and evt.context_lines:
                print(f"      Context:")
                for cl in evt.context_lines[:8]:
                    print(f"        | {cl[:150]}")

    # === STATISTICS SUMMARY ===
    # WD-timeout roams: driver executed the roam FSM but the emergency watchdog
    # fired before the OS could complete authentication -> NOT a successful roam.
    executed_roams   = [r for r in roams if 'EXECUTED' in r.details
                        and 'emergency WD timer fired' not in r.details]
    wd_roams         = [r for r in roams if 'emergency WD timer fired' in r.details]
    aborted_roams    = [r for r in roams if 'ABORTED' in r.details]
    failed_roams     = [r for r in roams if 'FAILED to connect' in r.details]
    # evaluated_only was already computed above from all_roams

    # Check if any disconnect was preceded by a failed/aborted roam within 5 seconds
    def _is_post_roam_disconnect(disc_evt, roam_list):
        try:
            d_sec = timestamp_to_seconds(disc_evt.timestamp)
            for r in roam_list:
                r_sec = timestamp_to_seconds(r.timestamp)
                if 0 <= d_sec - r_sec <= 5.0:
                    return True
        except (ValueError, IndexError):
            pass
        return False

    # Classify disconnects
    disc_os        = [d for d in disconnects if d.reason in (DisconnectReason.OS_INITIATED,)]
    disc_restart   = [d for d in disconnects if d.reason in (
                          DisconnectReason.ADAPTER_RESTART,
                          DisconnectReason.DRIVER_DISABLED,
                          DisconnectReason.PNP_DEVICE_RESTART)]
    disc_deauth    = [d for d in disconnects if d.reason == DisconnectReason.AP_DEAUTH]
    disc_beacon    = [d for d in disconnects if d.reason == DisconnectReason.BEACON_LOSS]
    disc_wdexpired = [d for d in disconnects if d.reason == DisconnectReason.ROAM_WD_EXPIRED]
    disc_fw        = [d for d in disconnects if d.reason == DisconnectReason.FW_ERROR]
    disc_after_roam= [d for d in disconnects
                      if _is_post_roam_disconnect(d, aborted_roams + failed_roams)
                      and d.reason not in (DisconnectReason.AP_DEAUTH,
                                           DisconnectReason.PNP_DEVICE_RESTART,
                                           DisconnectReason.ADAPTER_RESTART,
                                           DisconnectReason.BEACON_LOSS,
                                           DisconnectReason.ROAM_WD_EXPIRED)]
    disc_other     = [d for d in disconnects
                      if d not in disc_os and d not in disc_restart
                      and d not in disc_deauth and d not in disc_beacon and d not in disc_wdexpired
                      and d not in disc_fw]

    def _count_by_reason(roam_list):
        counts = {}
        for r in roam_list:
            counts[r.reason.value] = counts.get(r.reason.value, 0) + 1
        return counts

    W = 54  # label column width

    print(f"\n  {'='*70}")
    print(f"  STATISTICS SUMMARY")
    print(f"  {'='*70}")

    # --- Disconnections ---
    print(f"\n  {'-'*70}")
    print(f"  1. DISCONNECTIONS  (total: {len(disconnects)})")
    print(f"  {'-'*70}")
    lines_disc = [
        ("  OS-initiated (TASK_DISCONNECT / user)",          len(disc_os),          disc_os),
        ("  Adapter restart (driver + PnP teardown)",        len(disc_restart),     disc_restart),
        ("  Driver roaming watchdog expired (internal)",      len(disc_wdexpired),   disc_wdexpired),
        ("  AP sent deauthentication frame",                  len(disc_deauth),      disc_deauth),
        ("  Beacon loss (missed beacons from AP)",            len(disc_beacon),      disc_beacon),
        ("  After failed/aborted roaming attempt",           len(disc_after_roam),  disc_after_roam),
        ("  Firmware error",                                  len(disc_fw),          disc_fw),
        ("  Other / Unknown",                                 len(disc_other),       disc_other),
    ]
    for label, count, evts in lines_disc:
        if count > 0:
            print(f"  (*) {label:<{W-4}} {count}")
            if detail:
                for i, evt in enumerate(evts, 1):
                    extra = f"  {evt.details}" if evt.details else ""
                    print(f"       [{i}] {evt.timestamp}{extra}")

    # --- Successful roaming ---
    print(f"\n  {'-'*70}")
    print(f"  2. SUCCESSFUL ROAMS  (total: {len(executed_roams)})  [WD-expired (not connected): {len(wd_roams)}]")
    print(f"  {'-'*70}")
    if executed_roams:
        exec_counts = _count_by_reason(executed_roams)
        for reason_str, count in sorted(exec_counts.items(), key=lambda x: -x[1]):
            print(f"  (*) {reason_str:<{W}} {count}")

        if detail:
            # --- Roam table ---
            _W = (3, 13, 17, 14, 17, 22)   # #, timestamp, old-AP, new-AP, bssid, trigger+notes
            _HDR1 = (f"  {'#':>{_W[0]}}  {'Timestamp':<{_W[1]}}  "
                     f"{'Old AP (dBm)':<{_W[2]}}  {'New AP':<{_W[3]}}  "
                     f"{'Target BSSID':<{_W[4]}}  Trigger")
            _HDR2 = (f"  {'':>{_W[0]}}  {'HH:MM:SS.mmm':<{_W[1]}}  "
                     f"{'@T1/@T2':<{_W[2]}}  {'prev/scan':<{_W[3]}}  "
                     f"{'':>{_W[4]}}")
            _SEP  = (f"  {'-'*_W[0]}  {'-'*_W[1]}  "
                     f"{'-'*_W[2]}  {'-'*_W[3]}  "
                     f"{'-'*_W[4]}  {'-'*_W[5]}")
            print()
            print(_HDR1)
            print(_HDR2)
            print(_SEP)
            for i, evt in enumerate(executed_roams, 1):
                exec_r = evt.rssi
                conn_r = old_ap_conn_rssi_map.get(id(evt), '')
                _ap_sel_t1 = evt.ap_selection_factors.get('prev_bc0_rssi', '')
                t1_r = _ap_sel_t1 or conn_r
                _dm = re.search(r'\[((?:Channel load|Latency penalty))\]', evt.details)

                old_col = f"{t1_r}/{exec_r}" if (t1_r and exec_r) else (t1_r or exec_r or "N/A")
                _prev_rssi = evt.ap_selection_factors.get('prev_scan_rssi_map', {}).get(evt.target_bssid or '', '')
                _scan = evt.ap_selection_factors.get('target_rssi', '')
                _conn = roam_conn_rssi_map.get(id(evt), '')
                # T1 = prev-scan RSSI (NA if new AP not visible in the scan before this one)
                # T2 = current-scan RSSI; fall back to connection RSSI only when scan data absent
                if _scan:
                    _t1 = _prev_rssi if _prev_rssi else 'NA'
                    new_col = f"{_t1}/{_scan}"
                elif _conn:
                    new_col = f"N/A/{_conn}"
                else:
                    new_col = "N/A"
                bssid_col = evt.target_bssid or "N/A"
                rsn = _REASON_ABBR.get(evt.reason.value if evt.reason else "", "?")
                if _dm:
                    rsn += f" [{_dm.group(1)}]"
                af = evt.ap_selection_factors
                af_parts = []
                if af.get('bt_connected'):       af_parts.append("BT")
                if af.get('current_ap_ch_load'): af_parts.append("chLoad")
                if af.get('target_bt_coex'):     af_parts.append("BTcoex")
                if af_parts:
                    rsn += f" [{', '.join(af_parts)}]"
                print(f"  {i:>{_W[0]}}  {evt.timestamp:<{_W[1]}}  "
                      f"{old_col:<{_W[2]}}  {new_col:<{_W[3]}}  "
                      f"{bssid_col:<{_W[4]}}  {rsn}")
            print(_SEP)

            # AP Selection Factor aggregate
            n_bt      = sum(1 for r in executed_roams if r.ap_selection_factors.get('bt_connected'))
            n_chload  = sum(1 for r in executed_roams if r.ap_selection_factors.get('current_ap_ch_load'))
            n_btcoex  = sum(1 for r in executed_roams if r.ap_selection_factors.get('target_bt_coex'))
            if any([n_bt, n_chload, n_btcoex]):
                print(f"\n  AP Selection Factors (across successful roams):")
                if n_bt:     print(f"       BT active during selection            : {n_bt}")
                if n_chload: print(f"       Current AP had channel-load penalty   : {n_chload}")
                if n_btcoex: print(f"       Target AP has BTcoex:TRUE              : {n_btcoex}")
    else:
        print(f"     (none)")

    # --- WD-expired roams (FSM executed but watchdog fired before OS connected) ---
    if wd_roams:
        print(f"\n  {'-'*70}")
        print(f"  2b. WD-EXPIRED ROAMS  (executed, watchdog fired before OS connected, total: {len(wd_roams)})")
        print(f"  {'-'*70}")
        for i, evt in enumerate(wd_roams, 1):
            target_info = f" -> {evt.target_bssid}" if evt.target_bssid else " -> (target N/A)"
            print(f"       [{i}] {evt.timestamp}  {evt.reason.value}{target_info}")
            print(f"            (WD fired - driver disconnected; subsequent auth failures belong to OS reconnect)")

    # --- Failed roams (executed but could not connect) ---
    if failed_roams:
        print(f"\n  {'-'*70}")
        print(f"  3. FAILED ROAMS  (executed but failed to connect, total: {len(failed_roams)})")
        print(f"  {'-'*70}")
        fail_counts = _count_by_reason(failed_roams)
        for reason_str, count in sorted(fail_counts.items(), key=lambda x: -x[1]):
            print(f"  (*) {reason_str:<{W}} {count}")
        print()
        for i, evt in enumerate(failed_roams, 1):
            target_info = f" -> {evt.target_bssid}" if evt.target_bssid else " -> (target N/A)"
            print(f"       [{i}] {evt.timestamp}  {evt.reason.value}{target_info}  {evt.details}")

    # --- Aborted roams ---
    if aborted_roams:
        print(f"\n  {'-'*70}")
        print(f"  4. ABORTED ROAMS  (total: {len(aborted_roams)})")
        print(f"  {'-'*70}")
        abort_counts = _count_by_reason(aborted_roams)
        for reason_str, count in sorted(abort_counts.items(), key=lambda x: -x[1]):
            print(f"  (*) {reason_str:<{W}} {count}")
        print()
        for i, evt in enumerate(aborted_roams, 1):
            print(f"       [{i}] {evt.timestamp}  {evt.reason.value}  {evt.details}")

    # --- Evaluated only (roam considered but driver stayed on current AP) ---
    # Only shown in detail mode (-d); excluded from the default summary as noise.
    if detail and evaluated_only:
        print(f"\n  {'-'*70}")
        print(f"  5. EVALUATED ONLY  (roam considered but stayed on current AP, total: {len(evaluated_only)})")
        print(f"  {'-'*70}")
        eval_counts = _count_by_reason(evaluated_only)
        for reason_str, count in sorted(eval_counts.items(), key=lambda x: -x[1]):
            print(f"  (*) {reason_str:<{W}} {count}")
        print()
        for i, evt in enumerate(evaluated_only, 1):
            rssi_info = f" RSSI:{evt.rssi}dBm" if evt.rssi else ""
            print(f"       [{i}] {evt.timestamp}  {evt.reason.value}{rssi_info}")

    print(f"\n  {'='*70}\n")

    return events


def find_log_files(path):
    """Find parsed driver log files in a directory."""
    log_files = []
    p = Path(path)
    if p.is_file():
        return [str(p)]

    # Look for TicTac3 parsed output files
    patterns = ['WifiDriverIHVSession*.log', '*.etl.*.log']
    for pattern in patterns:
        log_files.extend(str(f) for f in p.rglob(pattern))

    # Also look for WiFiLog*.LOG files (direct driver log format)
    log_files.extend(str(f) for f in p.rglob('WiFiLog*.LOG'))

    return sorted(log_files)


def _fix_path(p):
    """Normalize J\\foo -> J:\\foo (missing colon after drive letter)."""
    m = re.match(r'^([A-Za-z])\\(.+)$', p)
    return f"{m.group(1)}:\\{m.group(2)}" if m else p


def run_tictac3(etl_path):
    """Run TicTac3 on an ETL file and return the path to the parsed .log file."""
    tictac_path = r'\\infs089.iil.intel.com\Tictac\ticTac3\source\main.py'
    out_path = etl_path + '.log'

    if os.path.exists(out_path):
        print(f"  Parsed log already exists: {out_path}")
        return out_path

    if not os.path.exists(etl_path):
        print(f"Error: ETL file not found: {etl_path}")
        sys.exit(1)

    if not os.path.exists(tictac_path):
        print(f"Error: TicTac3 not found at {tictac_path}")
        sys.exit(1)

    print(f"  Running TicTac3 on {etl_path} ...")
    env = os.environ.copy()
    env['TRACE_FORMAT_PREFIX'] = r'%%4!s! [%%1!s!] [%%!FLAGS!] [%%!LEVEL!] [%%9!d!]%%8!04x! [%%!FUNC!] %%2!s!:'
    env['_NT_SYMBOL_PATH'] = r'SRV*C:\Symbols*http://symweb'
    subprocess.run(['python', '-W', 'ignore', tictac_path, '-i', etl_path],
                   env=env, timeout=600, check=False)

    if not os.path.exists(out_path):
        print(f"Error: TicTac3 did not produce expected output: {out_path}")
        sys.exit(1)

    print(f"  Parsed: {out_path}")
    return out_path


def main():
    if len(sys.argv) < 2:
        print("Usage: python wifi_event_analyzer.py <log_file_or_directory> [-d] [--verbose]")
        print("       python wifi_event_analyzer.py -TicTac3 <etl_file> [-d] [--verbose]")
        print()
        print("  (default)     Print flow history + statistics summary (counts only)")
        print("  -d            Add per-event details: disconnect list, roam RSSI table")
        print("  --verbose     Add raw log context lines for each event (implies -d)")
        print("  -TicTac3 <f>  Parse ETL file with TicTac3 first, then analyze")
        print()
        print("Analyzes WiFi driver logs to explain disconnect and roaming events.")
        print("\nSupported log formats:")
        print("  - TicTac3 parsed ETL logs (WifiDriverIHVSession.etl.*.log)")
        print("  - Direct WiFi driver logs (WiFiLog*.LOG)")
        sys.exit(1)

    verbose = '--verbose' in sys.argv or '-v' in sys.argv
    detail  = '-d' in sys.argv or verbose  # verbose implies detail

    if '-TicTac3' in sys.argv:
        idx = sys.argv.index('-TicTac3')
        if idx + 1 >= len(sys.argv):
            print("Error: -TicTac3 requires an ETL file path argument")
            sys.exit(1)
        etl_path = _fix_path(sys.argv[idx + 1])
        target = run_tictac3(etl_path)
    else:
        target = _fix_path(sys.argv[1])
        if not os.path.exists(target):
            print(f"Error: Path does not exist: {target}")
            sys.exit(1)

    log_files = find_log_files(target)
    if not log_files:
        print(f"No log files found at: {target}")
        print("Looking for: WifiDriverIHVSession*.log, *.etl.*.log, WiFiLog*.LOG")
        sys.exit(1)

    print(f"\nFound {len(log_files)} log file(s) to analyze:")
    for f in log_files:
        print(f"  - {f}")

    all_events = []
    for log_file in log_files:
        events = analyze_log_file(log_file, verbose=verbose, detail=detail)
        all_events.extend(events)

    if len(log_files) > 1 and all_events:
        print(f"\n{'='*80}")
        print(f"  OVERALL SUMMARY ACROSS ALL FILES")
        print(f"{'='*80}")
        total_disc = sum(1 for e in all_events if e.event_type == EventType.DISCONNECT)
        total_roam = sum(1 for e in all_events if e.event_type == EventType.ROAM)
        print(f"  Total disconnects: {total_disc}")
        print(f"  Total roaming events: {total_roam}")


if __name__ == '__main__':
    main()

