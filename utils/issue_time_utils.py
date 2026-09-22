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
from datetime import datetime, timedelta
from typing import Callable, Optional, Tuple, Union

_LogTsFormat = Union[str, Callable[[str], Optional[datetime]]]


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
# Each entry is (compiled-regex, tuple-of-strptime-formats-or-parser-callables).
#   * Wi-Fi ETL   : MM/DD/YYYY-HH:MM:SS.mmm         (date/time joined by '-')
#   * BT HCI      : YYYY/MM/DD HH:MM:SS(.mmm)       (date/time space-separated, ms optional)
#   * Linux ISO   : YYYY-MM-DDTHH:MM:SS.ffffff(Z|+HH:MM)  (journalctl / syslog, RFC3339)
#   * Linux dmesg : [  12.345678]                   (kernel-uptime seconds, no wall-clock date)
# The Wi-Fi pattern is kept exactly as before so existing Wi-Fi cases are
# unaffected; later entries are purely additive.


def _parse_kernel_uptime_token(token: str) -> Optional[datetime]:
    """Linux kernel ring-buffer style '[   12.345678]' -> epoch + uptime seconds.

    There is no wall-clock date in this format, so it's anchored to the Unix
    epoch purely to preserve relative ordering when scanning for first/last.
    """
    m = re.search(r"[\d.]+", token)
    if not m:
        return None
    try:
        seconds = float(m.group(0))
    except ValueError:
        return None
    return datetime(1970, 1, 1) + timedelta(seconds=seconds)


_LOG_TS_SPECS = (
    (re.compile(r"\d{2}/\d{2}/\d{4}-\d{2}:\d{2}:\d{2}\.\d{3}"),
     ("%m/%d/%Y-%H:%M:%S.%f",)),
    (re.compile(r"\d{4}/\d{2}/\d{2}\s\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?"),
     ("%Y/%m/%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S")),
    (re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{1,6}(?:Z|[+-]\d{2}:\d{2})"),
     ("%Y-%m-%dT%H:%M:%S.%f%z",)),
    (re.compile(r"\[\s*\d+\.\d+\]"),
     (_parse_kernel_uptime_token,)),
)


def _parse_log_ts_token(token: str, fmts: Tuple[_LogTsFormat, ...]) -> Optional[datetime]:
    """Parse a matched timestamp token against its candidate strptime formats
    (or custom parser callables for formats strptime can't express)."""
    for fmt in fmts:
        try:
            dt = fmt(token) if callable(fmt) else datetime.strptime(token, fmt)
        except (ValueError, TypeError):
            continue
        if dt is not None:
            return dt.replace(tzinfo=None) if dt.tzinfo else dt
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


def _customer_timezone_for(log_path: str) -> str:
    """Customer timezone label for a capture, or "" when it cannot be read."""
    if not log_path:
        return ""
    try:
        from utils.timezone_utils import get_effective_timezone
        return get_effective_timezone(log_path) or ""
    except Exception:
        return ""


def _full_datetime_to_log_frame(
    parsed: datetime,
    log_path: str,
    first_ts: Optional[datetime],
    last_ts: Optional[datetime],
) -> datetime:
    """Map a dated issue time onto the log (decoder host, GMT+8) frame.

    Mirrors ``get_issue_context``'s ``_to_log_frame``: which frame a dated
    value is already in is ambiguous, so ``determine_issue_time_frames`` scores
    both interpretations against the capture folder / log range. Returns the
    input untouched when no customer timezone is known — the frames coincide.
    """
    if not log_path:
        return parsed
    try:
        # Lazy: issue_time_ai imports this module at module level.
        from utils.issue_time_ai import determine_issue_time_frames
        frames = determine_issue_time_frames(
            parsed, [log_path], log_first_ts=first_ts, log_last_ts=last_ts,
        )
        return frames.get("log_frame") or parsed
    except Exception:
        return parsed


def validate_issue_time_in_log_range(
    raw_str: str,
    log_path: str,
) -> Tuple[Optional[datetime], Optional[datetime], Optional[datetime], str]:
    """Parse a carried issue time and reject it when it cannot exist in the log.

    This is the hand-off guard between ``download_result`` and the chatbot.  A
    time already resolved on the former page must not be re-guessed from the
    case description after the selected log is known.  Instead, validate that
    exact value against the selected log's first/last timestamps.

    Frames are the subtlety here: the carried value is the CUSTOMER wall clock,
    while ``first_ts`` / ``last_ts`` come from the decoded log (host clock,
    GMT+8). Comparing them raw rejects every non-GMT+8 customer by exactly
    their UTC offset, so the value is moved into the log frame first — dated
    values through ``determine_issue_time_frames``, clock-only values by
    walking the customer dates the log covers and converting each back.

    Returns ``(issue_dt, first_ts, last_ts, error)``.  ``issue_dt`` is a
    LOG-frame datetime, or ``None`` when the value is malformed or outside a
    readable log range.  Time-only values keep the latest matching occurrence
    (the capture end is the most relevant anchor).
    """
    parsed, is_time_only = parse_issue_time_string(raw_str)
    first_ts, last_ts = read_log_time_range(log_path) if log_path else (None, None)

    if parsed is None:
        return None, first_ts, last_ts, "The carried issue time could not be parsed."

    # Frames coincide only when the customer timezone is actually readable.
    # Without it a failed range check cannot tell "wrong time" apart from
    # "right time in a frame we could not convert", so the error says so
    # instead of asserting the value is out of range. Validation still
    # proceeds: when the customer really is GMT+8 (or the capture is ours)
    # the raw comparison is the correct one, and refusing outright would
    # block those valid values.
    customer_tz = _customer_timezone_for(log_path)
    unverified = "" if customer_tz else (
        " The customer timezone could not be read from the capture, so the "
        "value could not be converted to the log's clock."
    )

    if is_time_only:
        if not first_ts or not last_ts:
            return None, first_ts, last_ts, (
                "A time-only issue time cannot be validated because the log has no readable date range."
            )
        # Walk dates in the frame the clock was written in (the customer's),
        # converting each candidate back so the range check stays log-frame.
        walk_first, walk_last = first_ts, last_ts
        if customer_tz:
            from utils.timezone_utils import taiwan_to_local
            walk_first = taiwan_to_local(first_ts, customer_tz) or first_ts
            walk_last = taiwan_to_local(last_ts, customer_tz) or last_ts
        candidates = []
        day = walk_first.date()
        # A corrupt log should not make validation walk an unbounded date span.
        max_days = min((walk_last.date() - day).days, 366)
        for offset in range(max_days + 1):
            candidate = datetime.combine(day + timedelta(days=offset), parsed.time())
            if customer_tz:
                from utils.timezone_utils import local_to_taiwan
                candidate = local_to_taiwan(candidate, customer_tz) or candidate
            if first_ts <= candidate <= last_ts:
                candidates.append(candidate)
        if not candidates:
            return None, first_ts, last_ts, (
                "The carried issue clock does not occur inside the selected log range."
                + unverified
            )
        parsed = max(candidates)
    else:
        parsed = _full_datetime_to_log_frame(parsed, log_path, first_ts, last_ts)

    if first_ts and last_ts and not (first_ts <= parsed <= last_ts):
        return None, first_ts, last_ts, (
            "The carried issue time is outside the selected log range." + unverified
        )

    return parsed, first_ts, last_ts, ""


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
