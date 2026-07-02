"""
Single source of truth for parsing & resolving the chatbot's "issue time".

The issue time is the timestamp the agent uses to anchor log analysis. It can
arrive from many places (Salesforce attachment subtitle, sidebar form, URL
query, free-form user message). This module owns the strict-format parsing
and the fallback chain so the rest of the codebase doesn't have to repeat
the same format loops.

Free-form description extraction (e.g. "Issue happened at 14:15:18") still
lives in etl_utils.extract_time_from_description; this module is for strict
canonical strings + log-file-based fallback.
"""

import os
import re
from datetime import datetime
from typing import Optional, Tuple


# Strict datetime formats accepted from sidebar / attachment_time / URL.
_FULL_FORMATS = (
    "%m/%d/%Y-%H:%M:%S.%f",
    "%m/%d/%Y-%H:%M:%S",
    "%m/%d/%Y %H:%M:%S.%f",
    "%m/%d/%Y %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
)
# 24-hour first so "04:45" / "04:45:00" keep their existing meaning; the 12-hour
# `%p` variants only match when an explicit AM/PM is present (e.g. "04:45 PM").
_TIME_ONLY_FORMATS = (
    "%H:%M:%S", "%H:%M",
    "%I:%M:%S %p", "%I:%M %p", "%I:%M:%S%p", "%I:%M%p",
)

# Recognised in-log timestamp formats (tried per line; first/best match wins).
# Each entry is (compiled-regex, tuple-of-strptime-formats-to-try).
#   * Wi-Fi ETL : MM/DD/YYYY-HH:MM:SS.mmm    (date/time joined by '-')
#   * BT HCI    : YYYY/MM/DD HH:MM:SS(.mmm)  (date/time space-separated, ms optional)
# The Wi-Fi pattern is kept exactly as before so existing Wi-Fi cases are
# unaffected; the HCI pattern is purely additive.
_LOG_TS_SPECS = (
    (re.compile(r"\d{2}/\d{2}/\d{4}-\d{2}:\d{2}:\d{2}\.\d{3}"),
     ("%m/%d/%Y-%H:%M:%S.%f",)),
    (re.compile(r"\d{4}/\d{2}/\d{2}\s\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?"),
     ("%Y/%m/%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S")),
)


def _parse_log_ts_token(token: str, fmts: Tuple[str, ...]) -> Optional[datetime]:
    """Parse a matched timestamp token against its candidate strptime formats."""
    for fmt in fmts:
        try:
            return datetime.strptime(token, fmt)
        except ValueError:
            continue
    return None


def _first_log_ts(text: str) -> Optional[datetime]:
    """Datetime of the EARLIEST-positioned in-log timestamp in ``text`` (any
    recognised format), or None."""
    best_pos: Optional[int] = None
    best_dt: Optional[datetime] = None
    for rx, fmts in _LOG_TS_SPECS:
        m = rx.search(text)
        if not m:
            continue
        if best_pos is None or m.start() < best_pos:
            dt = _parse_log_ts_token(m.group(0), fmts)
            if dt:
                best_pos, best_dt = m.start(), dt
    return best_dt


def _last_log_ts(text: str) -> Optional[datetime]:
    """Datetime of the LATEST-positioned in-log timestamp in ``text`` (any
    recognised format), or None."""
    best_pos: Optional[int] = None
    best_dt: Optional[datetime] = None
    for rx, fmts in _LOG_TS_SPECS:
        last = None
        for m in rx.finditer(text):
            last = m
        if last is None:
            continue
        if best_pos is None or last.start() > best_pos:
            dt = _parse_log_ts_token(last.group(0), fmts)
            if dt:
                best_pos, best_dt = last.start(), dt
    return best_dt


def parse_issue_time_string(s: str) -> Tuple[Optional[datetime], bool]:
    """Parse a strict issue-time string.

    Returns (datetime, is_time_only). When `is_time_only` is True the string
    only carried HH:MM[:SS] and the date portion of the returned datetime is
    `datetime.min.date()` — callers should align it with a real log date.
    Returns (None, False) when nothing parses.
    """
    if not s:
        return None, False
    s = str(s).strip()
    for fmt in _FULL_FORMATS:
        try:
            return datetime.strptime(s, fmt), False
        except ValueError:
            continue
    for fmt in _TIME_ONLY_FORMATS:
        try:
            t = datetime.strptime(s, fmt)
            return datetime.combine(datetime.min.date(), t.time()), True
        except ValueError:
            continue
    return None, False


def read_log_time_range(log_path: str) -> Tuple[Optional[datetime], Optional[datetime]]:
    """Return (first_ts, last_ts) parsed from a .log file. Either may be None.

    Timestamps are returned in the log's own frame (the decoder host's
    clock — GMT+8 in our deployment). The chatbot keeps ``issue_time``
    aligned to that same frame for in-log matching, with a customer-tz
    annotation surfaced separately for the UI.
    """
    if not log_path or not os.path.exists(log_path):
        return None, None
    first_ts = last_ts = None
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f):
                if i > 200:
                    break
                dt = _first_log_ts(line)
                if dt:
                    first_ts = dt
                    break

            f.seek(0, 2)
            file_size = f.tell()
            read_size = min(file_size, 65536)
            f.seek(file_size - read_size)
            tail = f.read()
            last_ts = _last_log_ts(tail) or last_ts
    except Exception as e:
        print(f"[issue_time] read_log_time_range failed for {log_path}: {e}")
    return first_ts, last_ts


# Time-of-day matcher for time-only (DDD / tracefmt) logs whose lines carry no
# date, e.g. "17:07:24:599 ...". The negative look-arounds stop it latching
# onto digits inside a longer number. Group 4 (optional) is fractional seconds
# with either ':' or '.' as the separator.
_TIME_ONLY_RE = re.compile(
    r'(?<!\d)(\d{1,2}):(\d{2}):(\d{2})(?:[:.](\d{1,6}))?(?!\d)'
)


def _last_time_of_day_in(lines) -> str:
    """Scan ``lines`` from the tail backwards for the last valid HH:MM:SS[.mmm]
    time-of-day and return it as a string ("" when none matches)."""
    for line in reversed(lines):
        m = _TIME_ONLY_RE.search(line or "")
        if not m:
            continue
        hh, mm, ss = (int(m.group(i)) for i in (1, 2, 3))
        if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
            continue
        raw_ms = m.group(4)
        if raw_ms:
            ms = int(raw_ms.ljust(6, "0")[:6]) // 1000
            return f"{hh:02d}:{mm:02d}:{ss:02d}.{ms:03d}"
        return f"{hh:02d}:{mm:02d}:{ss:02d}"
    return ""


def read_log_last_time_only(log_path: str) -> str:
    """Last HH:MM:SS[.mmm] time-of-day of a TIME-ONLY (DDD/tracefmt) log.

    For DATED logs use ``read_log_time_range`` — its regex only matches lines
    that carry a date, so it returns no last timestamp for DDD logs (whose
    lines are just "HH:MM:SS:mmm ..."). This helper covers that gap: it reads
    only the file's TAIL (never pulls the whole file into memory), mirroring
    ``read_log_time_range``'s 64KB seek, and scans backwards for the final
    time-of-day.

    The tail window escalates (64KB → 1MB → …) only when the initial window
    holds no parseable time (a single >64KB line, or a long run of
    continuation lines with no leading time). Bounded and guaranteed to
    terminate because the read size is capped at the file size. Returns ""
    when no time-of-day can be found (or the file is missing/unreadable).
    """
    if not log_path or not os.path.exists(log_path):
        return ""
    try:
        file_size = os.path.getsize(log_path)
    except OSError:
        return ""
    if file_size <= 0:
        return ""

    win = 65536
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            while True:
                read_size = min(file_size, win)
                f.seek(file_size - read_size)
                chunk = f.read()
                lines = chunk.splitlines()
                # Drop the first (possibly partial) line only when the seek
                # started mid-file; a full read starts at a real line.
                if read_size < file_size:
                    lines = lines[1:]
                found = _last_time_of_day_in(lines)
                if found or read_size >= file_size:
                    return found
                win *= 16
    except Exception as e:
        print(f"[issue_time] read_log_last_time_only failed for {log_path}: {e}")
        return ""


def resolve_issue_time(raw_str: str, log_path: str = "") -> Tuple[Optional[datetime], str]:
    """Resolve a final issue-time datetime from whatever inputs are available.

    Resolution order:
      1. Full datetime in `raw_str`           → ("input")
      2. Time-only in `raw_str` + log file    → aligned to log date ("input+log_date")
      3. No `raw_str` parse, log file present → latest log timestamp ("log_latest")
      4. Nothing usable                       → (None, "none")
    """
    parsed, is_time_only = parse_issue_time_string(raw_str)
    if parsed and not is_time_only:
        return parsed, "input"

    first_ts, last_ts = read_log_time_range(log_path) if log_path else (None, None)
    ref = last_ts or first_ts

    if parsed and is_time_only:
        if ref:
            aligned = ref.replace(
                hour=parsed.hour, minute=parsed.minute,
                second=parsed.second, microsecond=parsed.microsecond,
            )
            return aligned, "input+log_date"
        return parsed, "input_time_only"

    if ref:
        return ref, "log_latest"

    return None, "none"


def format_issue_time(dt: Optional[datetime], with_ms: bool = True) -> str:
    """Canonical chatbot serialization (matches sidebar `MM/DD/YYYY-HH:MM:SS.mmm`)."""
    if not dt:
        return ""
    if with_ms:
        return dt.strftime("%m/%d/%Y-%H:%M:%S.%f")[:-3]
    return dt.strftime("%m/%d/%Y-%H:%M:%S")
