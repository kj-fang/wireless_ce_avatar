"""
Shared timezone utilities for the chatbot, log picker, and event log views.

Background
----------
Two distinct timestamp sources need timezone awareness:

1. **Windows event log timestamps** — `services/event_log_service.py` reads
   them via the Win32 EvtLog API which returns UTC. The display is then
   converted to whatever timezone the customer's machine was in, as recorded
   in `system_info.txt` ("System Time Zone"). Conversion: UTC -> customer tz.

2. **Decoded `.log` file timestamps and ETL folder/filename timestamps**
   (e.g. ``10/28/2025-11:25:49.900`` inside a log line, or
   ``LAPTOP-..._27-10-2025_16-09-04_...`` on a folder). These get
   auto-aligned to Taiwan time (GMT+8) during the in-house ETL decode step.
   To match a customer-reported issue time (always in the customer's local
   tz) we have to shift these back. Conversion: Taiwan -> customer tz.

The system_info.txt parsing and the timezone-string resolver are shared
between the two flows; only the *source* timezone assumption differs.

Daylight saving
---------------
The customer tz comes from Windows as a STANDARD-time display string, e.g.
``(UTC-08:00) Pacific Time (US & Canada)``. The ``(UTC-08:00)`` prefix is the
zone's *standard* offset; during daylight saving the real wall clock is an hour
ahead (PDT = UTC-07:00). ``resolve_timezone`` therefore resolves the zone NAME
to a real DST-observing tzinfo (via the Windows registry or an IANA fallback)
and lets ``astimezone`` choose the correct offset for the specific instant being
converted. The fixed-offset reading of a ``(UTC±..)`` token is only used as a
fallback for strings that name no recognisable zone.

Manual override
---------------
If `system_info.txt` is missing or carries an unparseable timezone, the
chatbot UI lets the user pick one. The selection is persisted as a sidecar
file `.timezone_override.json` next to (or one level above) the artifacts,
so subsequent picks reuse it without re-prompting.

Resolution order for the *effective* timezone:
  1. Manual override sidecar (`.timezone_override.json`)
  2. system_info.txt "System Time Zone" / systeminfo.txt "Time Zone:"
  3. Empty string (caller decides — no conversion)
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    from dateutil import tz as _dateutil_tz
except ImportError:  # pragma: no cover - dateutil is a hard runtime dep
    _dateutil_tz = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Fixed +08:00 used for the chatbot/ETL "raw is Taiwan time" assumption.
TAIWAN_TZ = timezone(timedelta(hours=8))

#: Sidecar filename for the manual override.
OVERRIDE_FILENAME = ".timezone_override.json"

#: How many parent directories to walk when hunting for system_info.txt.
_SEARCH_DEPTH = 4

# Matches "(UTC-08:00) Pacific Time (US & Canada)" style.
_UTC_PREFIX_RE = re.compile(r"^\(UTC([+-])(\d{2}):(\d{2})\)\s*(.*)$", re.IGNORECASE)
# Matches a trailing "(GMT-0800)" or "(UTC-08:00)" anywhere in the string.
_UTC_GMT_OFFSET_RE = re.compile(r"\((?:UTC|GMT)([+-])(\d{2}):?(\d{2})\)", re.IGNORECASE)
# Matches a bare "GMT-0500" / "UTC-05:00" / "GMT+8" (no surrounding parens).
_BARE_OFFSET_RE = re.compile(r"^(?:UTC|GMT)\s*([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Daylight-saving-aware zone resolution
# ---------------------------------------------------------------------------
#
# The customer's region/time zone comes EXCLUSIVELY from the customer's ZIP
# (``system_info.txt`` / ``systeminfo.txt`` -> "System Time Zone"), never from
# the machine that happens to open the files — that host is just a decoder and
# its own clock/locale is irrelevant. This module therefore resolves the zone
# from a built-in, machine-INDEPENDENT lookup table; it deliberately does NOT
# read the host Windows registry or the host's current time-zone setting, so
# the same ZIP yields the same answer on any analyst's machine.
#
# Windows reports the zone as a *standard-time* display string, e.g.
# ``(UTC-08:00) Pacific Time (US & Canada)``. The ``(UTC-08:00)`` prefix is the
# zone's STANDARD offset and is wrong by one hour whenever the customer's
# incident actually happened during daylight saving (PDT = UTC-07:00 in
# summer). Converting with that fixed prefix offset silently shifts every
# customer-frame timestamp by an hour for ~8 months of the year.
#
# To fix it we map the Windows zone name to an IANA zone id and resolve that
# via ``dateutil`` (which uses the bundled tz database, not the host) so
# ``astimezone`` picks the correct offset for the *specific* instant.

# Machine-independent map: lowercased Windows zone identifier -> IANA zone.
# Keyed on BOTH forms Windows can emit:
#   * the "Display" NAME (the part after the "(UTC±..)" prefix), e.g.
#     "pacific time (us & canada)"
#   * the registry KEY name, e.g. "pacific standard time"
# so a string like "(UTC-08:00) Pacific Time (US & Canada)" or
# "Pacific Standard Time (GMT-0800)" both resolve. The IANA targets observe
# DST on their own; this table only identifies WHICH zone, it never encodes a
# fixed offset. Anything not listed falls back to the fixed-offset reading.
_WIN_DISPLAY_NAME_TO_IANA = {
    # --- Display NAME form -------------------------------------------------
    "pacific time (us & canada)": "America/Los_Angeles",
    "mountain time (us & canada)": "America/Denver",
    "central time (us & canada)": "America/Chicago",
    "eastern time (us & canada)": "America/New_York",
    "atlantic time (canada)": "America/Halifax",
    "newfoundland": "America/St_Johns",
    "alaska": "America/Anchorage",
    "hawaii": "Pacific/Honolulu",
    "arizona": "America/Phoenix",
    "saskatchewan": "America/Regina",
    "central america": "America/Guatemala",
    "indiana (east)": "America/Indiana/Indianapolis",
    "guadalajara, mexico city, monterrey": "America/Mexico_City",
    "bogota, lima, quito, rio branco": "America/Bogota",
    "caracas": "America/Caracas",
    "santiago": "America/Santiago",
    "brasilia": "America/Sao_Paulo",
    "buenos aires": "America/Argentina/Buenos_Aires",
    "dublin, edinburgh, lisbon, london": "Europe/London",
    "gmt time": "Europe/London",
    "monrovia, reykjavik": "Atlantic/Reykjavik",
    "amsterdam, berlin, bern, rome, stockholm, vienna": "Europe/Berlin",
    "belgrade, bratislava, budapest, ljubljana, prague": "Europe/Budapest",
    "brussels, copenhagen, madrid, paris": "Europe/Paris",
    "sarajevo, skopje, warsaw, zagreb": "Europe/Warsaw",
    "athens, bucharest": "Europe/Bucharest",
    "helsinki, kyiv, riga, sofia, tallinn, vilnius": "Europe/Kiev",
    "cairo": "Africa/Cairo",
    "harare, pretoria": "Africa/Johannesburg",
    "jerusalem": "Asia/Jerusalem",
    "istanbul": "Europe/Istanbul",
    "moscow, st. petersburg": "Europe/Moscow",
    "abu dhabi, muscat": "Asia/Dubai",
    "baku": "Asia/Baku",
    "tehran": "Asia/Tehran",
    "kabul": "Asia/Kabul",
    "islamabad, karachi": "Asia/Karachi",
    "chennai, kolkata, mumbai, new delhi": "Asia/Kolkata",
    "india standard time": "Asia/Kolkata",
    "kathmandu": "Asia/Kathmandu",
    "astana": "Asia/Almaty",
    "dhaka": "Asia/Dhaka",
    "yangon (rangoon)": "Asia/Yangon",
    "bangkok, hanoi, jakarta": "Asia/Bangkok",
    "beijing, chongqing, hong kong, urumqi": "Asia/Shanghai",
    "kuala lumpur, singapore": "Asia/Singapore",
    "perth": "Australia/Perth",
    "taipei": "Asia/Taipei",
    "osaka, sapporo, tokyo": "Asia/Tokyo",
    "tokyo, osaka, sapporo": "Asia/Tokyo",
    "seoul": "Asia/Seoul",
    "adelaide": "Australia/Adelaide",
    "darwin": "Australia/Darwin",
    "brisbane": "Australia/Brisbane",
    "canberra, melbourne, sydney": "Australia/Sydney",
    "auckland, wellington": "Pacific/Auckland",
    # --- Registry KEY name form -------------------------------------------
    "pacific standard time": "America/Los_Angeles",
    "mountain standard time": "America/Denver",
    "us mountain standard time": "America/Phoenix",
    "central standard time": "America/Chicago",
    "central standard time (mexico)": "America/Mexico_City",
    "canada central standard time": "America/Regina",
    "central america standard time": "America/Guatemala",
    "eastern standard time": "America/New_York",
    "us eastern standard time": "America/Indiana/Indianapolis",
    "atlantic standard time": "America/Halifax",
    "newfoundland standard time": "America/St_Johns",
    "alaskan standard time": "America/Anchorage",
    "hawaiian standard time": "Pacific/Honolulu",
    "sa pacific standard time": "America/Bogota",
    "venezuela standard time": "America/Caracas",
    "pacific sa standard time": "America/Santiago",
    "e. south america standard time": "America/Sao_Paulo",
    "argentina standard time": "America/Argentina/Buenos_Aires",
    "gmt standard time": "Europe/London",
    "greenwich standard time": "Atlantic/Reykjavik",
    "w. europe standard time": "Europe/Berlin",
    "central europe standard time": "Europe/Budapest",
    "romance standard time": "Europe/Paris",
    "central european standard time": "Europe/Warsaw",
    "gtb standard time": "Europe/Bucharest",
    "e. europe standard time": "Europe/Bucharest",
    "fle standard time": "Europe/Kiev",
    "egypt standard time": "Africa/Cairo",
    "south africa standard time": "Africa/Johannesburg",
    "israel standard time": "Asia/Jerusalem",
    "turkey standard time": "Europe/Istanbul",
    "russian standard time": "Europe/Moscow",
    "arabian standard time": "Asia/Dubai",
    "azerbaijan standard time": "Asia/Baku",
    "iran standard time": "Asia/Tehran",
    "afghanistan standard time": "Asia/Kabul",
    "pakistan standard time": "Asia/Karachi",
    "nepal standard time": "Asia/Kathmandu",
    "central asia standard time": "Asia/Almaty",
    "bangladesh standard time": "Asia/Dhaka",
    "myanmar standard time": "Asia/Yangon",
    "se asia standard time": "Asia/Bangkok",
    "china standard time": "Asia/Shanghai",
    "singapore standard time": "Asia/Singapore",
    "w. australia standard time": "Australia/Perth",
    "taipei standard time": "Asia/Taipei",
    "tokyo standard time": "Asia/Tokyo",
    "korea standard time": "Asia/Seoul",
    "cen. australia standard time": "Australia/Adelaide",
    "aus central standard time": "Australia/Darwin",
    "e. australia standard time": "Australia/Brisbane",
    "aus eastern standard time": "Australia/Sydney",
    "new zealand standard time": "Pacific/Auckland",
}


def _normalise_tz_text(s: str) -> str:
    """Lowercase + collapse whitespace so lookups are stable."""
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def _strip_offset_affixes(tz_name: str) -> str:
    """Drop a leading ``(UTC±..)`` prefix and/or trailing ``(GMT±..)`` suffix,
    leaving just the zone's display NAME (e.g. ``Pacific Time (US & Canada)``).
    """
    name = re.sub(r"^\s*\(UTC[+\-]\d{2}:\d{2}\)\s*", "", tz_name, flags=re.IGNORECASE)
    name = re.sub(r"\s*\((?:UTC|GMT)[+\-]\d{2}:?\d{2}\)\s*$", "", name, flags=re.IGNORECASE)
    return name.strip()


def _resolve_dst_aware_zone(tz_name: str):
    """Best-effort resolve ``tz_name`` to a DST-OBSERVING tzinfo, or None.

    Resolution is fully machine-independent — it relies only on the built-in
    ``_WIN_DISPLAY_NAME_TO_IANA`` table plus ``dateutil``'s bundled tz
    database, NOT on the host's Windows registry or current locale. Tries the
    static map (Display NAME or registry KEY form), then an IANA-looking name
    handed straight to ``dateutil``. Returns None when nothing names a real
    zone, so the caller can fall back to the fixed-offset interpretation.
    """
    if _dateutil_tz is None:
        return None

    name_only = _strip_offset_affixes(tz_name)

    # 1) Static Windows-name -> IANA map (machine-independent).
    iana = _WIN_DISPLAY_NAME_TO_IANA.get(_normalise_tz_text(name_only))
    if iana:
        z = _dateutil_tz.gettz(iana)
        if z is not None:
            return z

    # 2) Already an IANA id ("America/Los_Angeles")? Let dateutil resolve it
    #    from its bundled database. Restricted to slash-form names so we don't
    #    accidentally accept a Windows key the table didn't cover and have
    #    dateutil fall back to the host registry.
    if "/" in name_only:
        z = _dateutil_tz.gettz(name_only)
        if z is not None:
            return z
    return None


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def _search_dirs_from(any_path: str) -> list[str]:
    """Return [path's dir, parent, grandparent, ...] up to _SEARCH_DEPTH levels."""
    if not any_path:
        return []
    out: list[str] = []
    cur = os.path.dirname(any_path) if os.path.splitext(any_path)[1] else any_path
    for _ in range(_SEARCH_DEPTH):
        if not cur or cur in out:
            break
        out.append(cur)
        cur = os.path.dirname(cur)
    return out


def _read_system_info_tz(base_dir: str) -> str:
    """Look in ``base_dir`` (or its immediate children) for a system_info file
    and return its "System Time Zone" / "Time Zone:" string. Empty if nothing
    parses.
    """
    direct_json = os.path.join(base_dir, "system_info.txt")
    direct_text = os.path.join(base_dir, "systeminfo.txt")

    if os.path.exists(direct_json):
        try:
            with open(direct_json, "r", encoding="utf-8") as f:
                return (json.load(f).get("System Time Zone") or "").strip()
        except Exception as e:
            print(f"[timezone_utils] read system_info.txt failed @ {base_dir}: {e}")

    elif os.path.exists(direct_text):
        try:
            with open(direct_text, "r", encoding="utf-16le") as f:
                for line in f:
                    if line.startswith("Time Zone:"):
                        return line.split(":", 1)[1].strip()
        except Exception as e:
            print(f"[timezone_utils] read systeminfo.txt failed @ {base_dir}: {e}")

    # Check immediate children — the file is sometimes nested one level down.
    try:
        for child in os.listdir(base_dir):
            child_dir = os.path.join(base_dir, child)
            if not os.path.isdir(child_dir):
                continue
            nested = _read_system_info_tz(child_dir)  # one-shot recurse
            if nested:
                return nested
    except Exception:
        pass

    return ""


def get_system_timezone(any_path: str) -> str:
    """Walk up from ``any_path`` looking for system_info.txt / systeminfo.txt
    and return the "System Time Zone" value, or '' if none was found.
    """
    for base in _search_dirs_from(any_path):
        tz_name = _read_system_info_tz(base)
        if tz_name:
            return tz_name
    return ""


# ---------------------------------------------------------------------------
# Manual override sidecar
# ---------------------------------------------------------------------------

def _override_path_for(any_path: str) -> Optional[str]:
    """Pick a directory to host the sidecar. Prefer the directory that already
    has system_info.txt; otherwise use the nearest existing parent.
    """
    dirs = _search_dirs_from(any_path)
    if not dirs:
        return None
    for d in dirs:
        if os.path.exists(os.path.join(d, "system_info.txt")) or os.path.exists(
            os.path.join(d, "systeminfo.txt")
        ):
            return os.path.join(d, OVERRIDE_FILENAME)
    # Fall back to the deepest existing directory we encountered.
    return os.path.join(dirs[0], OVERRIDE_FILENAME)


#: Default ``issue_time_basis`` when the sidecar is missing or silent.
#:
#: Two semantic values:
#:   "log"      — the typed issue_time is in the same frame as the ETL-decoded
#:                ``.log`` content (GMT+8 in practice). This is the common case
#:                when an engineer transcribes a time straight from the log into
#:                the case description.
#:   "customer" — the typed issue_time is in the customer's local clock as
#:                reported in ``system_info.txt``. Use this when the customer
#:                stated the wall-clock time themselves.
#:
#: We pick "customer" as the default so every artifact (folder ts, log range,
#: typed issue_time, LLM suggestion) lands in the same wall-clock frame the
#: customer reasons about. The autologger folder is already in customer tz, so
#: this default needs the FEWEST conversions to keep the picker and the LLM
#: aligned. (Naming note: "log" / "customer" replace the earlier
#: implementation-leaky "taiwan" / "local" pair.)
DEFAULT_ISSUE_TIME_BASIS = "customer"
VALID_ISSUE_TIME_BASES = ("log", "customer")


def _read_sidecar(any_path: str) -> dict:
    """Return the full sidecar dict (closest to ``any_path``), or {}."""
    for base in _search_dirs_from(any_path):
        candidate = os.path.join(base, OVERRIDE_FILENAME)
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            print(f"[timezone_utils] read sidecar failed @ {candidate}: {e}")
    return {}


def get_manual_override(any_path: str) -> str:
    """Read the override sidecar (if any) and return the saved tz string."""
    return (_read_sidecar(any_path).get("timezone") or "").strip()


def get_issue_time_basis(any_path: str) -> str:
    """Return the issue-time frame for this case: "log" or "customer".

    Reads ``issue_time_basis`` from the sidecar; falls back to
    ``DEFAULT_ISSUE_TIME_BASIS`` when missing or unrecognised.

    Three artifact frames in play (callers act on this per their data):

      | Artifact                | Frame         |
      |-------------------------|---------------|
      | ETL-decoded ``.log``    | log (GMT+8)   |
      | Autologger folder name  | customer      |
      | issue_time (typed)      | depends — basis tells us |

    Decision matrix for callers:

      - basis="log":
          ``.log`` ts  — no-op (already log-frame).
          folder ts    — shift FROM customer tz TO log frame (``local_to_taiwan``).
          issue_time   — no-op.
      - basis="customer":
          ``.log`` ts  — shift FROM log frame TO customer tz (``taiwan_to_local``).
          folder ts    — no-op (already customer-frame).
          issue_time   — no-op.
    """
    val = (_read_sidecar(any_path).get("issue_time_basis") or "").strip().lower()
    return val if val in VALID_ISSUE_TIME_BASES else DEFAULT_ISSUE_TIME_BASIS


def set_manual_override(
    any_path: str,
    tz_name: str,
    issue_time_basis: Optional[str] = None,
) -> Optional[str]:
    """Persist sidecar values next to the case artifacts.

    Pass an empty ``tz_name`` to clear the override (falls back to
    system_info.txt). ``issue_time_basis`` accepts ``"log"`` or
    ``"customer"`` (see ``VALID_ISSUE_TIME_BASES``); ``None`` leaves the
    existing value untouched (or applies the default on a fresh sidecar).

    Returns the absolute sidecar path written, or ``None`` on failure.
    """
    target = _override_path_for(any_path)
    if not target:
        return None

    # Preserve any pre-existing fields the caller did NOT pass so a partial
    # update (e.g. only basis) doesn't wipe the tz the user set earlier.
    existing = _read_sidecar(any_path) or {}
    basis = (issue_time_basis or existing.get("issue_time_basis")
             or DEFAULT_ISSUE_TIME_BASIS).strip().lower()
    if basis not in VALID_ISSUE_TIME_BASES:
        basis = DEFAULT_ISSUE_TIME_BASIS

    try:
        payload = {
            "timezone": (tz_name or "").strip(),
            "issue_time_basis": basis,
            "set_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        with open(target, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return target
    except Exception as e:
        print(f"[timezone_utils] write override failed @ {target}: {e}")
        return None


def get_effective_timezone(any_path: str) -> str:
    """Manual override wins; otherwise fall back to system_info.txt."""
    return get_manual_override(any_path) or get_system_timezone(any_path)


# ---------------------------------------------------------------------------
# Timezone-string resolver
# ---------------------------------------------------------------------------

def resolve_timezone(tz_name: str):
    """Return a tzinfo for ``tz_name`` (best effort), or None if unparseable.

    DST-aware by design: a named zone (Windows display string, Windows registry
    key name, or IANA name) maps to an IANA zone and resolves to a real
    DST-observing tzinfo so the actual offset is picked per-instant by
    ``astimezone``. The fixed-offset interpretation of a ``(UTC±..)`` /
    ``(GMT±..)`` token is only a *fallback* for strings that name no
    recognisable zone — using it directly would peg the conversion to standard
    time and be an hour off during daylight saving.

    Machine-independent: resolution uses a built-in mapping table + dateutil's
    bundled tz database only. It never reads the host's Windows registry or its
    current locale, so the same customer ``system_info.txt`` value resolves
    identically on any analyst's machine.

    Accepted formats (DST-aware first, fixed-offset fallback):
      - "(UTC-08:00) Pacific Time (US & Canada)"  -> America/Los_Angeles (DST)
      - "Pacific Standard Time (GMT-0800)"        -> America/Los_Angeles (DST)
      - "Central Standard Time"                   -> America/Chicago (DST)
      - "America/Chicago"                         -> America/Chicago (DST)
      - "GMT-0500", "(GMT-05:00)"                 -> fixed offset (no zone name)
    """
    tz_name = (tz_name or "").strip()
    if not tz_name:
        return None

    # 1) Prefer a real, DST-observing zone resolved from the NAME via the
    #    built-in table. This covers the Windows "(UTC±..) Display" strings
    #    whose prefix is only the standard-time offset and would otherwise
    #    lose an hour during DST.
    dst_zone = _resolve_dst_aware_zone(tz_name)
    if dst_zone is not None:
        return dst_zone

    # 2) Windows-style "(UTC±HH:MM) Long Name" prefix — fixed offset fallback
    #    (the name didn't resolve to a known zone above).
    m = _UTC_PREFIX_RE.match(tz_name)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        hours, minutes = int(m.group(2)), int(m.group(3))
        return timezone(timedelta(minutes=sign * (hours * 60 + minutes)))

    # 3) Trailing "(GMT±HHMM)" / "(UTC±HH:MM)" — fixed offset fallback.
    m = _UTC_GMT_OFFSET_RE.search(tz_name)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        hours, minutes = int(m.group(2)), int(m.group(3))
        return timezone(timedelta(minutes=sign * (hours * 60 + minutes)))

    # 4) Bare "GMT-0500" / "UTC-05:00" / "GMT+8" (no parens) — fixed offset.
    m = _BARE_OFFSET_RE.match(tz_name)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        hours = int(m.group(2))
        minutes = int(m.group(3) or 0)
        return timezone(timedelta(minutes=sign * (hours * 60 + minutes)))

    # 5) Last resort: only accept an IANA-style ("Area/Location") name from
    #    dateutil's bundled database. We deliberately DON'T pass bare Windows
    #    key names here — on Windows that path would consult the host registry,
    #    making the result machine-dependent, which we must avoid (the region
    #    comes from the customer ZIP, not this host).
    if _dateutil_tz is not None and "/" in tz_name:
        return _dateutil_tz.gettz(tz_name)
    return None


def format_tz_label(tz_name: str) -> str:
    """Pretty label for the chatbot UI, e.g. "Central Standard Time (UTC-05:00)".
    Returns '' when ``tz_name`` cannot be resolved.
    """
    target = resolve_timezone(tz_name)
    if target is None:
        return ""
    offset = datetime.now(target).utcoffset()
    if offset is None:
        return tz_name
    total = int(offset.total_seconds() // 60)
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hh, mm = divmod(total, 60)
    pretty = f"UTC{sign}{hh:02d}:{mm:02d}"
    # Strip any existing "(UTC...)" / "(GMT...)" wrapper (leading prefix or
    # trailing tail) so we don't double-print the offset.
    cleaned = re.sub(r"^\s*\((?:UTC|GMT)[+\-][^)]+\)\s*", "", tz_name)
    cleaned = re.sub(r"\s*\((?:UTC|GMT)[+\-][^)]+\)\s*$", "", cleaned).strip()
    return f"{cleaned} ({pretty})" if cleaned else pretty


def to_iana_timezone(tz_name: str) -> str:
    """Return an IANA zone id (e.g. ``America/Los_Angeles``) for ``tz_name``,
    or '' when only a fixed offset is known.

    The IANA id is what a browser's ``Intl.DateTimeFormat`` understands, so the
    chatbot can recompute the customer wall clock DST-correctly for *any* date
    the engineer types into the picker — not just the standard-time offset baked
    into the Windows label. When this returns '' the frontend falls back to the
    fixed offset parsed from the label (no DST), which is the pre-correction
    behaviour and only matters for zones outside the curated map.
    """
    name_only = _strip_offset_affixes((tz_name or "").strip())
    if not name_only:
        return ""
    # Already an IANA id (e.g. typed/overridden as "America/Chicago").
    if "/" in name_only and _dateutil_tz is not None and _dateutil_tz.gettz(name_only):
        return name_only
    return _WIN_DISPLAY_NAME_TO_IANA.get(_normalise_tz_text(name_only), "")


# ---------------------------------------------------------------------------
# Datetime conversions
# ---------------------------------------------------------------------------

def utc_to_local(dt: Optional[datetime], tz_name: str) -> Optional[datetime]:
    """UTC-naive `dt` -> local-naive datetime (Windows event log direction)."""
    if dt is None or not tz_name:
        return dt
    target = resolve_timezone(tz_name)
    if target is None:
        return dt
    return dt.replace(tzinfo=timezone.utc).astimezone(target).replace(tzinfo=None)


def taiwan_to_local(dt: Optional[datetime], tz_name: str) -> Optional[datetime]:
    """Taiwan-naive `dt` -> local-naive datetime (.log / ETL direction).

    If `tz_name` is empty or unresolvable, returns `dt` unchanged so callers
    can safely chain without checking first.
    """
    if dt is None or not tz_name:
        return dt
    target = resolve_timezone(tz_name)
    if target is None:
        return dt
    return dt.replace(tzinfo=TAIWAN_TZ).astimezone(target).replace(tzinfo=None)


def local_to_taiwan(dt: Optional[datetime], tz_name: str) -> Optional[datetime]:
    """Reverse of taiwan_to_local — used when we have to align a customer-
    issued local time against an artifact that's still in Taiwan time.
    """
    if dt is None or not tz_name:
        return dt
    target = resolve_timezone(tz_name)
    if target is None:
        return dt
    return dt.replace(tzinfo=target).astimezone(TAIWAN_TZ).replace(tzinfo=None)
