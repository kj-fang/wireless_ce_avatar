"""
LLM-assisted issue-time suggestion.

Companion to ``issue_time_utils`` (which owns strict-format parsing): this
module turns a free-form problem description plus a rough browse of the log
into one or more issue-time suggestions for the chatbot sidebar.

User-first: when the description already carries explicit, correctly-formatted
time(s) those are returned verbatim and the LLM is never called. Only vague,
malformed, or missing times are sent to the LLM.

Deliberately free of Flask and of the agent class — callers pass plain inputs
(text, log lines, the log's time range, and an OpenAI-compatible LLM client +
model name) so the logic is unit-testable in isolation.
"""

import atexit
import json
import re
import hashlib
import threading
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from typing import Any, List, Optional, Tuple, Union

from utils.issue_time_utils import parse_issue_time_string, format_issue_time


# ---------------------------------------------------------------------------
# Response cache for build_issue_time_suggestions
# ---------------------------------------------------------------------------
# Users often click the "AI suggest issue time" button multiple times per case
# (edit description → retry → tweak → retry). When (log, description, frames)
# are identical the LLM answer is deterministic enough that we can hand back
# the previous payload — zero tokens, zero latency. Bounded LRU so a
# long-running server doesn't grow unbounded.
_SUGGEST_CACHE_MAX = 64
_suggest_cache: "OrderedDict[str, dict]" = OrderedDict()


def _log_fingerprint(log_lines: Optional[List[str]]) -> str:
    """Small hashable fingerprint for a possibly-huge log line list. Uses the
    first + last 100 lines plus the total count — collision-safe for real
    captures (which change either content or length when swapped)."""
    if not log_lines:
        return "no-log"
    n = len(log_lines)
    head = "\n".join(log_lines[:100])
    tail = "\n".join(log_lines[-100:]) if n > 100 else ""
    return hashlib.md5(f"{n}\n{head}\n{tail}".encode(errors="replace")).hexdigest()


def _suggest_cache_key(
    text: str,
    log_lines: Optional[List[str]],
    first_ts: Optional[datetime],
    last_ts: Optional[datetime],
    log_has_date: Optional[bool],
    tz_label: str,
    log_frame_first_ts: Optional[datetime],
    log_frame_last_ts: Optional[datetime],
    event_events_len: int,
    current_issue_time: Optional[str] = None,
    stage1_model: Optional[str] = None,
    force_ai: bool = False,
) -> str:
    payload = "|".join([
        (text or "").strip(),
        _log_fingerprint(log_lines),
        str(first_ts), str(last_ts),
        str(log_has_date), str(tz_label),
        str(log_frame_first_ts), str(log_frame_last_ts),
        str(event_events_len),
        (current_issue_time or "").strip(),
        (stage1_model or "").strip(),
        str(bool(force_ai)),
    ])
    return hashlib.md5(payload.encode(errors="replace")).hexdigest()


def _cache_get(key: str) -> Optional[dict]:
    hit = _suggest_cache.get(key)
    if hit is not None:
        _suggest_cache.move_to_end(key)
    return hit


def _cache_put(key: str, value: dict) -> None:
    _suggest_cache[key] = value
    _suggest_cache.move_to_end(key)
    while len(_suggest_cache) > _SUGGEST_CACHE_MAX:
        _suggest_cache.popitem(last=False)


# ---------------------------------------------------------------------------
# Per-log digest cache (separate from the full-answer cache above)
# ---------------------------------------------------------------------------
# build_event_timeline_digest / build_log_digest both do a full LINEAR SCAN of
# every line in the log to find strong/weak anchor hits. For a huge trace
# (e.g. a multi-million-line "-boot" BT HCI capture) this scan alone can take
# tens of seconds — independent of, and much larger than, the LLM call that
# follows. The full-answer cache above is keyed on the DESCRIPTION TEXT, so
# it misses every time an engineer tweaks the description and retries — but
# the underlying LOG never changed, so re-running the multi-million-line scan
# on every retry is pure waste. This cache is keyed ONLY on the log
# fingerprint + digest parameters (no description text), so a retry with
# different wording reuses the already-computed digest string instantly —
# turning a repeat ~80s scan into a dict lookup.
_DIGEST_CACHE_MAX = 8
_digest_cache: "OrderedDict[str, str]" = OrderedDict()


def _digest_cache_get(key: str) -> Optional[str]:
    hit = _digest_cache.get(key)
    if hit is not None:
        _digest_cache.move_to_end(key)
    return hit


def _digest_cache_put(key: str, value: str) -> None:
    _digest_cache[key] = value
    _digest_cache.move_to_end(key)
    while len(_digest_cache) > _DIGEST_CACHE_MAX:
        _digest_cache.popitem(last=False)


# Full datetime tokens (carry their own date) — user-first, no LLM.
_EXPLICIT_FULL_DT_RE = re.compile(
    r'\d{1,2}/\d{1,2}/\d{4}[-\sT]\d{1,2}:\d{2}:\d{2}(?:\.\d{1,3})?'
    r'|\d{4}[-/]\d{1,2}[-/]\d{1,2}[-\sT]\d{1,2}:\d{2}:\d{2}(?:\.\d{1,6})?'
)
# Clock-only tokens (HH:MM[:SS][.mmm]) — user-first, date aligned to the log.
_EXPLICIT_TIME_ONLY_RE = re.compile(r'(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?:\.\d{1,3})?(?!\d)')

# Generic time-anchor signals — events likely to be the "interesting moment"
# the user wants to analyse, regardless of the log's domain. Deliberately kept
# domain-agnostic so the same digest works for Wi-Fi, Bluetooth, driver, or
# any other time-only / dated log. Four buckets:
#   * Failures   — terms that flag something going wrong
#   * Lifecycle  — terms that flag a start / end / state change
#   * Activity   — common verbs that anchor what the system is doing
#   * Sensing    — common discovery / observation verbs (frequent anchors
#                  for "when did the system detect / scan for X" questions)
# Don't list product-specific abbreviations here — the LLM gets the full
# description as context and will narrow things down on its own.
_LOG_DIGEST_KEYWORDS = re.compile(
    r'\b(?:'
    # Failures
    r'fail|error|timeout|abort|drop|crash|exception|warn|panic|hang'
    # Lifecycle
    r'|init|load|start|attach|ready|boot|launch|reset|stop|teardown|complete'
    # Activity / state transitions
    r'|connect|disconnect|enable|disable|query|request|response|notif|event'
    # Sensing / discovery
    r'|scan|discover|search|probe|detect|monitor|observ|interrupt|\bisr\b'
    # Bluetooth generic anchors — HCI/LMP protocol + link lifecycle terms that
    # frequently sit next to the interesting moment in a BT capture (the
    # concrete BT FAULT words live in _LOG_DIGEST_STRONG_RE above).
    r'|\bhci\b|\blmp\b|advertis|inquiry|\bpair|bond|encrypt|\brole\b'
    r'|firmware|\bfw\b|controller|recover|coex|supervis|\bacl\b|\bsco\b'
    r')\b',
    re.IGNORECASE,
)

# Stronger anchors than the generic sweep above. The generic keywords match
# on nearly every line of a busy Wi-Fi ETL trace ("connect"/"event"/"scan"
# fire dozens of times a second), so on a large log the keyword-hit list can
# run into the tens of thousands before the file is even half read. These
# tokens name a concrete fault or state change instead of a generic verb —
# lines matching them get first claim on the (still small) digest budget, see
# ``build_log_digest``.
#
# Two vocabularies, one regex — kept together so a mixed capture (Wi-Fi ETL +
# BT HCI in the same session) is handled by a single sweep:
#   * Wi-Fi ETL   — bracketed [ERROR]/[WARN]/[CRIT] severity tags plus
#                   DEAUTH/DISASSOC/MISBEHAV/EXCLUD and friends.
#   * BT HCI/driver (ibtpci/ibtusb .hci.txt) — Intel driver traces do NOT use
#                   [ERROR] tags; the fault signal is inline: "Error!" /
#                   "Warning!", firmware EXCEPTION / FATAL / "critical assert
#                   failure", "Trigger dump", error-recovery ("recovery",
#                   RECOVERY_TYPE_*, watchdog), and concrete NT STATUS_* codes
#                   (a bare STATUS_ is too broad — STATUS_SUCCESS is the most
#                   common line — so only the fault codes are listed). The bare
#                   ``timeout`` alt requires non-letter/underscore boundaries so
#                   it hits "STATUS_IO_TIMEOUT" but skips the benign, high-
#                   frequency "GetTrasactionTimeoutMs" / "IOSF_TRANS_TIMEOUT_
#                   DEFAULT" config chatter.
_LOG_DIGEST_STRONG_RE = re.compile(
    # Wi-Fi ETL
    r'\[(?:ERROR|WARN|CRIT(?:ICAL)?|FATAL)\]'
    r'|MISBEHAV|FAILED|DEAUTH|DISASSOC|EXCLUD|CRASH|PANIC|REJECT'
    # BT HCI / driver
    r'|Error!|Warning!|\bfatal\b|\bexception\b|\bassert'
    r'|Trigger dump|\bunexpected\b'
    r'|STATUS_(?:NO_SUCH_DEVICE|IO_TIMEOUT|UNSUCCESSFUL|CANCELLED'
    r'|DEVICE_NOT_CONNECTED|INSUFFICIENT_RESOURCES|DEVICE_POWER_FAILURE)'
    r'|\brecover(?:y|ed|ing)?\b|\bwatchdog\b'
    r'|(?<![A-Za-z_])timeout(?![A-Za-z_])',
    re.IGNORECASE,
)


# Fast, dependency-free PRE-FILTER for ``_LOG_DIGEST_STRONG_RE`` — a plain
# tuple of lowercase literal substrings that MUST be present for the real
# regex to have ANY chance of matching a line (a strict SUPERSET of true
# matches: every branch of ``_LOG_DIGEST_STRONG_RE`` requires at least one of
# these to appear, so this can never produce a false NEGATIVE). Checking
# these with plain ``in`` on a once-lowercased copy of the line (CPython's
# highly optimized C string search) is far cheaper per line than dispatching
# the real regex's multi-alternative, IGNORECASE-folding search — so on a
# huge trace (millions of lines, only a tiny fraction ever match) this lets
# the vast majority of lines skip the expensive regex call entirely. Every
# candidate that DOES pass this pre-filter is still confirmed with the real
# ``_LOG_DIGEST_STRONG_RE.search()`` before counting as a hit (see
# ``_is_strong_hit``), so this can only make scanning FASTER — it never
# changes WHICH lines end up classified as strong anchors.
#
# (An Aho-Corasick automaton — e.g. the third-party ``pyahocorasick``
# package — would make this pre-filter itself O(1)-per-character regardless
# of substring count, instead of this list's O(#substrings) worst case. That
# package needs a network install and a compiled C-extension, which this
# offline-packaged, PyInstaller-distributed app can't rely on being
# available on every machine it runs on, so this pure-stdlib version is the
# safe default — no new dependency, no packaging risk.)
_STRONG_PREFILTER_LITERALS = (
    "[error", "[warn", "[crit", "[fatal",
    "misbehav", "failed", "deauth", "disassoc", "exclud", "crash", "panic",
    "reject", "error!", "warning!", "fatal", "exception", "assert",
    "trigger dump", "unexpected",
    "status_no_such_device", "status_io_timeout", "status_unsuccessful",
    "status_cancelled", "status_device_not_connected",
    "status_insufficient_resources", "status_device_power_failure",
    "recover", "watchdog", "timeout",
)


def _is_strong_hit(line: str) -> bool:
    """True iff ``line`` matches ``_LOG_DIGEST_STRONG_RE`` — identical result
    to calling that regex directly, just faster on average: a cheap literal
    substring pre-filter (see ``_STRONG_PREFILTER_LITERALS``) skips the real
    regex dispatch for lines that can't possibly match."""
    if not any(lit in line.lower() for lit in _STRONG_PREFILTER_LITERALS):
        return False
    return bool(_LOG_DIGEST_STRONG_RE.search(line))


# Description-side "does this text seem to carry a time hint?" — used by the
# stage-1 gate. When the description has no time cue, stage1 (description-only
# LLM call) has no basis to infer a time from, so we skip it and go straight
# to stage2 (log-driven) to avoid burning tokens on a guess with no evidence.
#
# Deliberately STRICT / high-precision: only matches unambiguous, absolute
# time-pointing tokens. Generic connectives like "at", "around", "before",
# "after", "when", "during" were tried and REMOVED — they're some of the most
# common words in English/support-case prose (e.g. "at the AP", "failure
# occurs when device roams") and matched almost every description, causing
# stage1 to fire on text with NO real time information and hallucinate a
# "high confidence" answer purely from wording (no log evidence at all). A
# false NEGATIVE here only costs a few hundred tokens (falls through to the
# now-cheap stage2a timeline digest); a false POSITIVE can produce a wrong
# time with no way for the model to know better — so precision is prioritized
# over recall.
#
# Hits any of:
#   * digit patterns   — "12:34", "3pm", "5 minutes ago", "o'clock"
#   * day-segment words — "morning", "afternoon", "evening", "night",
#                         "midnight", "noon" (informative even standalone)
#   * Chinese clock units / day-segments — 「點」「分」「秒」「早上」「下午」…
_TIME_HINT_RE = re.compile(
    r'\d{1,2}:\d{2}'
    r'|\d+\s*(?:am|pm|a\.m\.|p\.m\.)'
    r'|\d+\s*(?:hr|hrs|hour|hours|min|mins|minute|minutes|sec|secs|second|seconds)\s*(?:ago|before|after|earlier|later)\b'
    r"|\bo['’]?clock\b"
    r'|\b(?:this|last|early|late|yesterday)\s+(?:morning|afternoon|evening|night)\b'
    r'|\b(?:morning|afternoon|evening|night|midnight|noon|midday)\b'
    r'|[點分秒時]'
    r'|早上|下午|上午|晚上|半夜|凌晨|中午|傍晚|清晨',
    re.IGNORECASE,
)


def _has_time_hint(text: str) -> bool:
    """Return True when the description likely carries a time cue worth asking
    the LLM about (description-only stage-1). Purely regex — no LLM."""
    if not text:
        return False
    m = _TIME_HINT_RE.search(text)
    if m:
        # Debug visibility: print exactly which token triggered stage1 so a
        # future false-positive (a word that matches but isn't really a time
        # cue) can be diagnosed from the console log instead of guessed at.
        print(f"[issue_time_ai] _has_time_hint matched {m.group(0)!r} "
              f"at offset {m.start()} in description "
              f"(context: …{text[max(0, m.start()-20):m.end()+20]!r}…)")
        return True
    return False


def make_suggestion(
    dt: datetime,
    confidence: str,
    reason: str,
    source: str,
    log_has_date: bool = True,
) -> dict:
    """Serialize a datetime into the shape the sidebar capture popup consumes.

    When ``log_has_date`` is False (DDD / tracefmt logs), the date component
    is intentionally dropped — both from the printable ``issue_time`` string
    and from the structured month/day/year fields — so the suggestion is
    honestly a clock-only value. Down-stream code (sidebar pickers, feedback
    JSONL) then never sees a placeholder year/month/day for a log that has
    no date in its lines.
    """
    if log_has_date:
        return {
            "issue_time": format_issue_time(dt),
            "month": dt.month, "day": dt.day, "year": dt.year,
            "hh": dt.hour, "mm": dt.minute, "ss": dt.second,
            "ms": int(dt.microsecond // 1000),
            "confidence": confidence, "reason": reason, "source": source,
        }
    # log_has_date == False (time-only log): time-only printable + null date.
    ms_s = f".{int(dt.microsecond // 1000):03d}" if dt.microsecond else ""
    return {
        "issue_time": f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}{ms_s}",
        "month": None, "day": None, "year": None,
        "hh": dt.hour, "mm": dt.minute, "ss": dt.second,
        "ms": int(dt.microsecond // 1000),
        "confidence": confidence, "reason": reason, "source": source,
    }


def extract_explicit_times(
    text: str,
    first_ts: Optional[datetime],
    last_ts: Optional[datetime],
    *,
    log_frame_first_ts: Optional[datetime] = None,
    log_frame_last_ts: Optional[datetime] = None,
    tz_to_customer_delta: Optional[timedelta] = None,
) -> Tuple[List[datetime], Optional[str]]:
    """User-first deterministic pass.

    Returns ``(datetimes, kind)`` where kind is ``'full'`` | ``'time_only'`` |
    ``None``. 'full' tokens carry their own date; 'time_only' clock tokens
    borrow the log's date so they land inside the actual capture range.

    Timezone disambiguation (basis="customer" path):
      ``first_ts`` / ``last_ts`` are the **customer-frame** range. When the
      user typed a fully-qualified timestamp that lies OUTSIDE that range but
      INSIDE the log-frame range (``log_frame_first_ts`` / ``log_frame_last_ts``),
      the user almost certainly transcribed from the log — we then shift the
      typed value by ``tz_to_customer_delta`` (negative for CST: log -> CST is
      ~-13h) so the resulting datetime is in customer frame, matching folder
      timestamps and the customer-shifted log range that ``find_best_log``
      compares against. Pass ``tz_to_customer_delta=None`` to disable
      (preserves the original frame-agnostic behaviour).
    """
    ref = last_ts or first_ts

    def _maybe_to_customer(dt: datetime) -> datetime:
        """Shift dt to customer frame if it only fits the log frame range."""
        if tz_to_customer_delta is None:
            return dt
        in_customer = (first_ts and last_ts and first_ts <= dt <= last_ts)
        in_log = (log_frame_first_ts and log_frame_last_ts
                  and log_frame_first_ts <= dt <= log_frame_last_ts)
        if (not in_customer) and in_log:
            return dt + tz_to_customer_delta
        return dt

    full: List[datetime] = []
    seen = set()
    for m in _EXPLICIT_FULL_DT_RE.finditer(text):
        tok = m.group(0).strip()
        dt, is_time_only = parse_issue_time_string(tok)
        if dt and not is_time_only and tok not in seen:
            seen.add(tok)
            full.append(_maybe_to_customer(dt))
    if full:
        return full, "full"

    time_only: List[datetime] = []
    for m in _EXPLICIT_TIME_ONLY_RE.finditer(text):
        tok = m.group(0).strip()
        dt, is_time_only = parse_issue_time_string(tok)
        if dt and is_time_only and tok not in seen:
            seen.add(tok)
            if ref:
                dt = ref.replace(hour=dt.hour, minute=dt.minute,
                                 second=dt.second, microsecond=dt.microsecond)
            time_only.append(dt)
    if time_only:
        return time_only, "time_only"
    return [], None


# ---------------------------------------------------------------------------
# Per-line log compression (for the LLM digest)
# ---------------------------------------------------------------------------
# Wi-Fi ETL / BT HCI / DDD lines all start with a recognisable timestamp
# followed by bracketed metadata tags. For issue-time inference the LLM only
# needs the TIMESTAMP + a short human-readable message; thread IDs, uniform
# level tags (SPECIAL/INFO/DEBUG), C-function names and multi-hundred-item
# numeric channel lists are pure noise. Stripping them here shrinks the
# per-line footprint by ~40-60% without touching signal.

_WIFI_ETL_TS_RE = re.compile(r'^(\d{2}/\d{2}/\d{4}-\d{2}:\d{2}:\d{2}\.\d{1,6})\s+')
_BT_HCI_TS_RE = re.compile(r'^(\d{4}/\d{2}/\d{2}\s\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)\s+')
_TIME_ONLY_TS_RE = re.compile(r'^(\d{1,2}:\d{2}:\d{2}[.:]\d{1,6})\s+')
# Intel BT driver trace (ibtpci / ibtusb .hci.txt) prefixes every line with
# ``[idx]<procHex>.<threadHex>::`` BEFORE the timestamp, e.g.
#   [0]32CC.43E4::10/28/2025-15:01:38.904 [ibtpci][Func]Error! ...
# The timestamp AFTER the prefix is the exact Wi-Fi ``MM/DD/YYYY-HH:MM:SS.mmm``
# shape, so we just strip this prefix up front and let _WIFI_ETL_TS_RE do the
# rest. Anchored + requires the ``HEX.HEX::`` shape so it can never touch a
# Wi-Fi ETL line (starts with a digit) or a time-only DDD line.
_BT_DRV_PREFIX_RE = re.compile(r'^\[\d+\][0-9A-Fa-f]{1,8}\.[0-9A-Fa-f]{1,8}::')
_LEADING_TAG_RE = re.compile(r'^\[([^\]]*)\]\s*')
# Uniform, low-signal severity tags to drop. WARN / ERROR / CRIT are KEPT —
# they're real anchors for the issue moment.
_NOISE_LEVEL_TAGS = frozenset({"SPECIAL", "INFO", "DEBUG", "VERBOSE", "TRACE"})
# C-identifier pattern for function-name tags right before the message ':'
_FUNC_TAG_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
# Long comma-separated list inside parens (channels, PMKIDs, hex dumps …).
_LONG_LIST_RE = re.compile(r'\(([^)]{20,})\)')


def _compress_log_line(line: str, max_len: int = 240) -> Tuple[str, str]:
    """Return ``(compressed, category)`` for one raw log line.

    ``category`` is used only by the caller's per-run stats print — one of:
      * ``'wifi'`` / ``'bt'`` / ``'ddd'`` — timestamped line recognised,
        metadata tags stripped
      * ``'other'`` — unrecognised format, only truncated to ``max_len``

    Never returns None; on any parse quirk the line is passed through unchanged
    (truncated to ``max_len``) so we can't lose signal.
    """
    # Strip trailing newline AND any leading whitespace/BOM so the timestamp
    # anchor matches on the first line of a UTF-8-with-BOM file too.
    s = line.rstrip("\n").lstrip("\ufeff \t")

    # Intel BT driver traces put ``[idx]procHex.threadHex::`` before the
    # timestamp. Strip it so the (Wi-Fi-shaped) timestamp is at line start;
    # remember it was BT so the category stays honest for the stats print.
    bt_driver = bool(_BT_DRV_PREFIX_RE.match(s))
    if bt_driver:
        s = _BT_DRV_PREFIX_RE.sub("", s, count=1)

    m = _WIFI_ETL_TS_RE.match(s)
    category = "bt" if bt_driver else "wifi"
    if m is None:
        m = _BT_HCI_TS_RE.match(s)
        category = "bt"
    if m is None:
        m = _TIME_ONLY_TS_RE.match(s)
        category = "ddd"
    if m is None:
        return (s if len(s) <= max_len else s[:max_len] + "…"), "other"

    ts = m.group(1)
    rest = s[m.end():]

    # Peel leading [tag] blocks. Drop thread-id-only, drop uniform level tags,
    # drop a function-name tag when the next char is ':' (message separator).
    kept_tags: List[str] = []
    while True:
        tm = _LEADING_TAG_RE.match(rest)
        if not tm:
            break
        tag = tm.group(1).strip()
        after = rest[tm.end():]
        if tag.isdigit():
            rest = after
            continue
        if tag.upper() in _NOISE_LEVEL_TAGS:
            rest = after
            continue
        # Function-name tag right before the ':' separator — drop.
        if after.startswith(":") and _FUNC_TAG_RE.match(tag):
            rest = after
            continue
        kept_tags.append(f"[{tag}]")
        rest = after

    # Collapse the ": :" separator (Wi-Fi ETL uses "]: : message") and
    # any leading colons on the message body.
    rest = re.sub(r'^\s*:\s*:\s*', ': ', rest)
    rest = re.sub(r'^\s*:\s*', ': ', rest)
    rest = rest.strip()

    def _shrink(m2: "re.Match") -> str:
        inner = m2.group(1)
        parts = [p.strip() for p in inner.split(",") if p.strip()]
        if len(parts) > 8:
            return f"({','.join(parts[:3])},…+{len(parts) - 3}more)"
        return m2.group(0)
    rest = _LONG_LIST_RE.sub(_shrink, rest)

    tag_block = " ".join(kept_tags)
    out = f"{ts} {tag_block} {rest}".strip() if tag_block else f"{ts} {rest}".strip()
    if len(out) > max_len:
        out = out[:max_len] + "…"
    return out, category


# Prefix splitter for _coalesce_same_prefix: pulls "TS [tag1] [tag2] :" as the
# grouping key and the rest as the coalescable message. Runs on already-
# compressed lines so the prefix is short and predictable.
_PREFIX_SPLIT_RE = re.compile(r'^(\S+(?:\s\[[^\]]*\])*)\s*(?::\s*)?(.*)$')


def _coalesce_same_prefix(
    lines: List[str], max_group: int = 6, sep: str = " ‖ ",
) -> List[str]:
    """Merge consecutive lines sharing the same ``TS [tags]`` prefix into one
    row. Wi-Fi ETL SCAN_REQUEST bursts fire 5 sub-lines at the same ms with
    identical module tag; DDD traces likewise fire clusters at one timestamp.
    Folding them saves ~20-30% chars with zero timestamp loss — the LLM still
    sees every distinct message payload, just without the repeated header.

    ``max_group`` caps how many payloads get concatenated onto one prefix so a
    pathological 100-line burst doesn't produce an unreadable mega-line;
    overflow becomes ``…+Nmore`` at the end. ``sep`` is the on-line separator
    (double-bar U+2016 by default — visually clear, never appears in log
    text).
    """
    if not lines:
        return list(lines)
    out: List[str] = []
    cur_prefix: Optional[str] = None
    cur_msgs: List[str] = []

    def _flush() -> None:
        if cur_prefix is None:
            return
        if not cur_msgs:
            out.append(cur_prefix)
        elif len(cur_msgs) == 1:
            payload = cur_msgs[0]
            out.append(f"{cur_prefix} : {payload}" if payload else cur_prefix)
        else:
            kept = cur_msgs[:max_group]
            tail = (f"{sep}…+{len(cur_msgs) - max_group}more"
                    if len(cur_msgs) > max_group else "")
            out.append(f"{cur_prefix} : {sep.join(kept)}{tail}")

    for ln in lines:
        m = _PREFIX_SPLIT_RE.match(ln)
        if not m:
            _flush()
            out.append(ln)
            cur_prefix, cur_msgs = None, []
            continue
        prefix = m.group(1).strip()
        msg = m.group(2).strip()
        if prefix == cur_prefix:
            if msg:
                cur_msgs.append(msg)
        else:
            _flush()
            cur_prefix = prefix
            cur_msgs = [msg] if msg else []
    _flush()
    return out


def build_log_digest(
    log_lines: List[str],
    head: int = 50,
    tail: int = 50,
    max_keyword: int = 60,
    max_chars: int = 12000,
) -> str:
    """Rough browse of the log for the LLM: head + tail + symptom-keyword hits,
    deduped and kept in original order, capped to keep the prompt small.

    Keyword hits are collected in fixed-size BUCKETS spanning the whole file
    (not a single running counter), then sampled evenly within a severity
    tier. Two tiers: ``_LOG_DIGEST_STRONG_RE`` (ERROR/WARN tags, FAILED,
    TIMEOUT, DEAUTH, MISBEHAV, ...) gets first claim on ``max_keyword``;
    ``_LOG_DIGEST_KEYWORDS`` (generic connect/scan/event/... verbs that match
    on nearly every line of a busy Wi-Fi ETL trace) only fills what's left.
    Both the bucketing and the tiering exist for the same reason: on a large,
    busy log the generic keyword sweep alone can rack up tens of thousands of
    hits before the file is even half read, so a naive "collect first N then
    sample" approach silently loses the entire back half of the log (and with
    it whatever repeating failure pattern lives there) — plain
    even-index sampling over a front-loaded hit list still front-loads the
    digest. Bucketing guarantees every region of the file can contribute;
    tiering guarantees a real fault line doesn't lose its slot to a "connect"
    match. This costs zero extra LLM tokens — the regex sweep is local CPU
    work, and ``max_keyword``/``max_chars`` are unchanged.

    Each picked line is passed through ``_compress_log_line`` to strip thread
    IDs, uniform level tags (``[SPECIAL]``, ``[INFO]``, ``[DEBUG]``),
    C-function-name tags, and to shrink long numeric lists (e.g.
    ``Channels(1,2,3,...,165,)``). Trims ~40-60% of characters on Wi-Fi ETL /
    BT captures without touching the timestamp or the message payload. A
    per-run ``[TOKEN] build_log_digest`` stats line prints the raw vs.
    compressed footprint plus estimated tokens (~4 chars/token) so callers can
    track savings.
    """
    lines = log_lines or []
    n = len(lines)
    if n == 0:
        return ""

    # Cache on (log fingerprint + these size params) — NOT on any description
    # text, so a retry with a tweaked description reuses this instantly
    # instead of re-scanning every line. Fingerprinting itself is cheap (only
    # touches the first/last 100 lines), so this check costs nothing even on
    # a multi-million-line trace.
    _cache_key = (
        "full|" + _log_fingerprint(lines) +
        f"|{n}|{head}|{tail}|{max_keyword}|{max_chars}"
    )
    _cached = _digest_cache_get(_cache_key)
    if _cached is not None:
        print(f"[TOKEN] build_log_digest: CACHE HIT (skipped {n}-line scan) "
              f"final_chars={len(_cached)} est_tokens_sent~{len(_cached) // 4}")
        return _cached

    picked = set(range(min(head, n)))
    picked.update(range(max(0, n - tail), n))

    # Bucket the file into ~4x max_keyword equal slices and cap hits PER
    # BUCKET (not globally) so a dense region early in the log can't exhaust
    # the collection budget before later buckets are even visited — see the
    # docstring above. Strong and weak hits get independent per-bucket caps
    # so a bucket full of generic "event"/"connect" noise can't crowd out an
    # ERROR/FAILED line landing in that same bucket.
    _num_buckets = max(1, max_keyword * 4)
    _bucket_size = max(1, -(-n // _num_buckets))  # ceil div
    _cap_per_bucket = 25
    strong_hits: List[int] = []
    weak_hits: List[int] = []
    if n > _MP_LINE_THRESHOLD:
        # Above the multiprocessing threshold, split the scan so the strong
        # tier goes through the same parallel scan build_event_timeline_digest
        # uses, instead of a second, hand-duplicated, always-sequential
        # full-file pass — this is exactly the stage2b escalation path for
        # the multi-million-line BT traces _scan_strong_hits was built for.
        # The weak tier still needs its own full pass (a different regex
        # with no literal prefilter — unlike _is_strong_hit's, so it
        # dominates wall time on a huge file either way), but only
        # re-checks _is_strong_hit for lines that pass it — cheap, since
        # generic-keyword hits are a small fraction of a busy log — while
        # preserving the original per-line priority: a strong-tier line is
        # NEVER also counted as weak, even if the strong bucket cap left it
        # out of strong_hits. Measured on a real 2.94M-line file: still a
        # net win (~9%) even though the weak pass dominates — see
        # scripts/bench_issue_time_ai.py history for the full numbers.
        strong_hits = _scan_strong_hits(lines, _bucket_size, _num_buckets, _cap_per_bucket)
        weak_bucket_counts = [0] * _num_buckets
        for i, line in enumerate(lines):
            if not _LOG_DIGEST_KEYWORDS.search(line) or _is_strong_hit(line):
                continue
            bucket = min(i // _bucket_size, _num_buckets - 1)
            if weak_bucket_counts[bucket] < _cap_per_bucket:
                weak_hits.append(i)
                weak_bucket_counts[bucket] += 1
    else:
        # At or below the threshold there's no multiprocessing win to chase
        # (_scan_strong_hits would just fall back to sequential anyway), and
        # a single combined pass — strong first, weak only as an elif — is
        # measurably cheaper than two separate full-file passes (~30% faster
        # on a real 108K-line file), so keep that simpler, faster form here.
        strong_bucket_counts = [0] * _num_buckets
        weak_bucket_counts = [0] * _num_buckets
        for i, line in enumerate(lines):
            bucket = min(i // _bucket_size, _num_buckets - 1)
            if _is_strong_hit(line):
                if strong_bucket_counts[bucket] < _cap_per_bucket:
                    strong_hits.append(i)
                    strong_bucket_counts[bucket] += 1
            elif _LOG_DIGEST_KEYWORDS.search(line):
                if weak_bucket_counts[bucket] < _cap_per_bucket:
                    weak_hits.append(i)
                    weak_bucket_counts[bucket] += 1

    def _even_sample(idxs: List[int], budget: int) -> List[int]:
        if budget <= 0 or not idxs:
            return []
        if len(idxs) <= budget:
            return idxs
        step = len(idxs) / budget
        return [idxs[int(j * step)] for j in range(budget)]

    # Strong (ERROR/WARN/FAILED/DEAUTH/...) anchors claim the budget first;
    # generic keyword hits only fill whatever's left.
    sampled_strong = _even_sample(strong_hits, max_keyword)
    sampled_weak = _even_sample(weak_hits, max_keyword - len(sampled_strong))
    for i in sampled_strong + sampled_weak:
        picked.add(i)
    ordered = sorted(picked)

    raw_pieces = [str(lines[i]).rstrip("\n") for i in ordered]
    # Chars of the picked lines BEFORE any compression or truncation. Newlines
    # (one per join) are counted so raw vs. compressed are apples-to-apples.
    raw_chars = sum(len(p) for p in raw_pieces) + max(0, len(raw_pieces) - 1)

    def _assemble(idxs: List[int]) -> Tuple[str, int, dict, int]:
        """Compress + coalesce a chronologically-sorted index subset.
        Returns (joined_text, line_count, cat_counts, coalesced_from)."""
        pcs = [str(lines[i]).rstrip("\n") for i in idxs]
        cc = {"wifi": 0, "bt": 0, "ddd": 0, "other": 0}
        compressed: List[str] = []
        for p in pcs:
            out, cat = _compress_log_line(p)
            cc[cat] = cc.get(cat, 0) + 1
            compressed.append(out)
        pre_coalesce = len(compressed)
        coalesced = _coalesce_same_prefix(compressed)
        return "\n".join(coalesced), len(coalesced), cc, pre_coalesce - len(coalesced)

    # head/tail lines are a hard floor — they're never dropped for budget.
    # If the full pick doesn't fit max_chars, binary-search DOWN how many of
    # the WEAK-tier sampled hits to keep first (least important — generic
    # connect/scan/event chatter), then the STRONG tier if still needed.
    # This replaces a flat ``digest[:max_chars]`` string slice: on a large
    # log the flat slice always cuts the CHRONOLOGICALLY LATEST picked lines
    # (since ``ordered`` is sorted ascending) — which silently drops the
    # explicit tail(50) lines this function is supposed to guarantee, and
    # whatever late-log evidence the strong/weak sampling just surfaced.
    # Same max_chars budget either way — zero extra tokens, just spent on
    # the highest-priority lines instead of "whichever come first."
    head_tail_set = set(range(min(head, n))) | set(range(max(0, n - tail), n))

    def _pick(n_strong: int, n_weak: int) -> List[int]:
        k1 = _even_sample(sampled_strong, n_strong) if n_strong < len(sampled_strong) else sampled_strong
        k2 = _even_sample(sampled_weak, n_weak) if n_weak < len(sampled_weak) else sampled_weak
        return sorted(head_tail_set | set(k1) | set(k2))

    # Full (untrimmed) assembly first — its length is what pure compression
    # produced, independent of whether budget-trimming kicks in below.
    digest, line_count, cat_counts, coalesced_from = _assemble(_pick(len(sampled_strong), len(sampled_weak)))
    compressed_pre_trunc = len(digest)
    n_strong_kept, n_weak_kept = len(sampled_strong), len(sampled_weak)
    truncated = False
    if len(digest) > max_chars:
        lo, hi = 0, len(sampled_weak)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            text, *_ = _assemble(_pick(len(sampled_strong), mid))
            if len(text) <= max_chars:
                lo = mid
            else:
                hi = mid - 1
        n_weak_kept = lo
        digest, line_count, cat_counts, coalesced_from = _assemble(_pick(len(sampled_strong), n_weak_kept))
        if len(digest) > max_chars:
            lo, hi = 0, len(sampled_strong)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                text, *_ = _assemble(_pick(mid, n_weak_kept))
                if len(text) <= max_chars:
                    lo = mid
                else:
                    hi = mid - 1
            n_strong_kept = lo
            digest, line_count, cat_counts, coalesced_from = _assemble(_pick(n_strong_kept, n_weak_kept))
        if len(digest) > max_chars:
            # Last-resort safety net: even head+tail alone don't fit (e.g. a
            # single pathologically long line) — fall back to a flat cut.
            digest = digest[:max_chars] + "\n…(truncated)"
            truncated = True

    # Two separate numbers so the log line is honest:
    #   * compression_saved — what stripping tags/lists actually removed
    #     (raw_chars - compressed_pre_trunc).
    #   * final_chars — what actually goes to the LLM (after max_chars cap).
    # The LLM's real prompt tokens still come from the `usage` field printed
    # by llm_suggest right after the call; this line is the pre-call estimate.
    final_chars = len(digest)
    compression_saved = max(0, raw_chars - compressed_pre_trunc)
    compression_pct = (compression_saved * 100.0 / raw_chars) if raw_chars else 0.0
    budget_dropped_weak = len(sampled_weak) - n_weak_kept
    budget_dropped_strong = len(sampled_strong) - n_strong_kept
    print(
        f"[TOKEN] build_log_digest: picked={line_count} lines "
        f"raw_chars={raw_chars} "
        f"compressed_chars_pre_trunc={compressed_pre_trunc} "
        f"final_chars={final_chars} "
        f"compression_saved_chars={compression_saved} "
        f"compression_pct={compression_pct:.1f}% "
        f"coalesced_lines={coalesced_from} "
        f"budget_dropped_weak={budget_dropped_weak} "
        f"budget_dropped_strong={budget_dropped_strong} "
        f"est_tokens_before_compress~{raw_chars // 4} "
        f"est_tokens_after_compress~{compressed_pre_trunc // 4} "
        f"est_tokens_sent~{final_chars // 4} "
        f"truncated={truncated} "
        f"categories={cat_counts}"
    )
    _digest_cache_put(_cache_key, digest)
    return digest


# Volatile tokens to blank out when deciding whether two strong-anchor lines
# are "the same KIND of event": MACs, hex handles, bare decimals, thread ids.
# Folding by this normalized key collapses a 24x AUTH_TX_FAILURE burst (one per
# BSSID) into a single counted row — the single biggest token saver on a Wi-Fi
# connection-failure trace, where the same fault repeats for every candidate AP.
_TL_VOLATILE_RE = re.compile(
    r'Address\([^)]*\)'                        # Address(74:9E:75:48:CA:E1)
    r'|[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}'   # bare MAC
    r'|0x[0-9A-Fa-f]+'                         # hex handle / status
    r'|\bTS=\d+'                               # firmware TS counters
    r'|\b\d+\b'                                # any bare decimal
)
_TL_TS_RE = re.compile(r'^(\d{2}/\d{2}/\d{4}-\d{2}:\d{2}:\d{2}\.\d{1,6}'
                       r'|\d{1,2}:\d{2}:\d{2}[.:]\d{1,6})')


def _timeline_key(compressed_line: str) -> str:
    """Normalized identity of a strong-anchor line (timestamp + volatile ids
    removed) so repeated failures of the same kind fold together."""
    body = _TL_TS_RE.sub("", compressed_line)
    return _TL_VOLATILE_RE.sub("#", body).strip()


# ---------------------------------------------------------------------------
# Parallel strong-hit scan for huge logs (multi-million-line BT "-boot" HCI
# captures). Empirically measured on a real 2.94M-line file: sequential
# ~21s -> 6 worker processes ~7.4s (2.84x), with the parallel result index-
# for-index IDENTICAL to the sequential scan (verified, not assumed — see
# scripts/bench_mp_scan.py). Only engages above _MP_LINE_THRESHOLD: ordinary
# Wi-Fi ETL logs (~100K lines) are sub-second sequentially and would only
# pay process overhead for nothing, so they never touch this path.
# ---------------------------------------------------------------------------
_MP_LINE_THRESHOLD = 300_000
_MP_WORKERS = 6

_scan_pool: Optional[ProcessPoolExecutor] = None
_scan_pool_lock = threading.Lock()


def _get_scan_pool() -> ProcessPoolExecutor:
    """Lazily create the shared worker pool, once, on the first log big
    enough to need it. Most sessions never touch this at all.

    Thread-safe (double-checked locking): the app runs Flask-SocketIO with
    ``async_mode='threading'``, so multiple request threads could reach here
    at the same instant for two different huge logs.

    Deliberately NOT created at import time or tied to Flask's app lifecycle
    (this module is "free of Flask... unit-testable in isolation" by
    design) — it only ever exists if something actually calls
    ``_scan_strong_hits`` on a log past the threshold. Cleanup is via
    ``atexit`` rather than any app-level shutdown hook, so it tears down
    correctly regardless of which code path exits the process.
    """
    global _scan_pool
    if _scan_pool is None:
        with _scan_pool_lock:
            if _scan_pool is None:  # re-check inside the lock
                pool = ProcessPoolExecutor(max_workers=_MP_WORKERS)
                atexit.register(pool.shutdown, wait=False, cancel_futures=True)
                _scan_pool = pool
    return _scan_pool


def _scan_bucket_capped(lines: List[str], start: int, bucket_size: int,
                         num_buckets: int, cap_per_bucket: int) -> List[int]:
    """Strong-hit scan over ``lines``, bucket-capped. ``start`` is the
    ABSOLUTE index of ``lines[0]`` (0 for a whole-file sequential scan; a
    chunk's own offset when called per-worker) — the one shared loop body
    for both the sequential path and each multiprocessing worker, so the
    two don't hand-duplicate the same scan.

    Returns ABSOLUTE line indices. Callers that split a file into multiple
    chunks must choose boundaries that are multiples of ``bucket_size``, so
    no bucket ever straddles two chunks — each call enforces
    ``cap_per_bucket`` independently and correctly, with no cross-chunk
    coordination needed. That is what makes concatenated per-chunk results
    identical to one whole-file sequential call.
    """
    bucket_counts = [0] * num_buckets
    picked: List[int] = []
    for offset, line in enumerate(lines):
        if not _is_strong_hit(line):
            continue
        i = start + offset
        b = min(i // bucket_size, num_buckets - 1)
        if bucket_counts[b] < cap_per_bucket:
            picked.append(i)
            bucket_counts[b] += 1
    return picked


def _scan_chunk_for_strong_hits(args):
    """Multiprocessing worker entry point: unpack one chunk's args and
    delegate to ``_scan_bucket_capped``. Must be a plain module-level
    function (Windows ``spawn`` pickles it by reference — the child process
    re-imports ``utils.issue_time_ai`` to find it) and must not depend on
    anything but its arguments."""
    start, chunk_lines, bucket_size, num_buckets, cap_per_bucket = args
    return _scan_bucket_capped(chunk_lines, start, bucket_size, num_buckets, cap_per_bucket)


def _scan_strong_hits_sequential(lines: List[str], bucket_size: int,
                                  num_buckets: int, cap_per_bucket: int) -> List[int]:
    """Original single-threaded scan — unchanged behaviour, used directly
    for every log at or below ``_MP_LINE_THRESHOLD`` and as the fallback
    when the parallel path fails for any reason."""
    return _scan_bucket_capped(lines, 0, bucket_size, num_buckets, cap_per_bucket)


def _scan_strong_hits(lines: List[str], bucket_size: int, num_buckets: int,
                       cap_per_bucket: int) -> List[int]:
    """Strong-hit index scan, bucket-capped. Sequential below
    ``_MP_LINE_THRESHOLD``; above it, splits into ``_MP_WORKERS``
    bucket-aligned chunks and scans them in parallel worker processes.

    Falls back to the sequential scan on ANY multiprocessing failure (pool
    creation, pickling, a worker crash) so a broken pool can never turn a
    log-analysis request into a 500 — same defensive philosophy as the LLM
    call guards elsewhere in this module.
    """
    n = len(lines)
    if n <= _MP_LINE_THRESHOLD:
        return _scan_strong_hits_sequential(lines, bucket_size, num_buckets, cap_per_bucket)
    try:
        chunk_size_in_buckets = -(-num_buckets // _MP_WORKERS)  # ceil
        chunk_lines = max(1, chunk_size_in_buckets * bucket_size)
        tasks = []
        start = 0
        while start < n:
            end = min(start + chunk_lines, n)
            tasks.append((start, lines[start:end], bucket_size, num_buckets, cap_per_bucket))
            start = end
        pool = _get_scan_pool()
        picked: List[int] = []
        for chunk_result in pool.map(_scan_chunk_for_strong_hits, tasks):
            picked.extend(chunk_result)
        return picked
    except Exception as e:
        print(f"[issue_time_ai] parallel scan failed ({e}); falling back to sequential.")
        return _scan_strong_hits_sequential(lines, bucket_size, num_buckets, cap_per_bucket)


def build_event_timeline_digest(
    log_lines: List[str],
    max_rows: int = 40,
    max_chars: int = 3000,
    *,
    cap_per_bucket: int = 30,
) -> str:
    """Compact, high-signal FAILURE TIMELINE for the LLM — the cheap stage-2a
    pass that replaces the full head/tail/keyword browse for most Wi-Fi cases.

    Unlike ``build_log_digest`` (which samples ~160 lines: 50 head init noise,
    50 tail, 60 generic connect/scan/event hits), this keeps ONLY strong-tier
    anchor lines — ``_LOG_DIGEST_STRONG_RE``: ERROR/WARN tags plus
    FAILED/TIMEOUT/DEAUTH/DISASSOC/MISBEHAV/EXCLUD/CRASH/PANIC/REJECT. For a
    Wi-Fi connection failure the issue time is, by construction, the timestamp
    of one of these events, so a pure timeline is both SMALLER (~1/3 the
    tokens) and MORE accurate (no init/telemetry noise to distract the model).

    Two compaction steps keep it tiny and readable:
      * bucket the file into slices and cap strong hits per bucket, so a dense
        early burst can't crowd out later events (same rationale as
        ``build_log_digest``);
      * fold consecutive same-KIND events (see ``_timeline_key``) into one row
        ``TS  message  (×N → last_clock)`` — a 24-line AUTH_TX_FAILURE burst
        becomes one line naming the count and the time span.

    Returns "" when the log has NO strong anchors (e.g. a clean throughput /
    latency complaint with no fault lines). The caller then falls back to the
    full ``build_log_digest`` so accuracy is never sacrificed for the saving.
    """
    lines = log_lines or []
    n = len(lines)
    if n == 0:
        return ""

    # Cache on (log fingerprint + these size params) — NOT on description
    # text. This is the single biggest win for huge traces: a multi-million-
    # line "-boot" capture can take tens of seconds to scan for strong-anchor
    # hits; a retry with a tweaked description (common — the full-answer
    # cache upstream is keyed on the text and misses on every retry) would
    # otherwise redo that whole scan for an unchanged log. Fingerprinting
    # itself only touches the first/last 100 lines, so this check is cheap
    # even here.
    _cache_key = (
        "timeline|" + _log_fingerprint(lines) +
        f"|{n}|{max_rows}|{max_chars}|{cap_per_bucket}"
    )
    _cached = _digest_cache_get(_cache_key)
    if _cached is not None:
        print(f"[TOKEN] build_event_timeline_digest: CACHE HIT "
              f"(skipped {n}-line scan) final_chars={len(_cached)} "
              f"est_tokens_sent~{len(_cached) // 4}")
        return _cached

    num_buckets = max(1, max_rows * 3)
    bucket_size = max(1, -(-n // num_buckets))  # ceil div
    picked = _scan_strong_hits(lines, bucket_size, num_buckets, cap_per_bucket)
    if not picked:
        # Cache the negative result too — "no strong anchors" still cost a
        # full scan to determine, and callers fall back to build_log_digest
        # (which has its own independent cache) so re-scanning here on every
        # retry would be pure waste.
        _digest_cache_put(_cache_key, "")
        return ""

    def _clock(compressed: str) -> str:
        mm = _TL_TS_RE.match(compressed)
        if not mm:
            return ""
        tok = mm.group(0)
        # keep just HH:MM:SS(.mmm) for the "→ last" marker
        return tok.split("-")[-1] if "-" in tok else tok

    def _render(k: int) -> Tuple[str, int]:
        """Even-sample k strong-hit lines across the WHOLE file span, compress,
        then fold adjacent same-KIND rows into one ``… (×N → last_clock)``.

        Sampling the picked indices directly (rather than folding first) is
        what guarantees chronological coverage of the entire capture: with
        interleaved fault kinds, strictly-consecutive folding almost never
        triggers, so a fold-then-sample order would still let an even sample
        skip the late (01:26) cluster. Sampling first pins the time span; the
        adjacent-fold is then just cosmetic burst removal.
        """
        if k >= len(picked):
            sel = picked
        else:
            step = len(picked) / k
            sel = [picked[int(j * step)] for j in range(k)]
        out: List[str] = []
        cur = None  # (compressed, key, count, last_clock)
        for i in sel:
            compressed, _ = _compress_log_line(str(lines[i]))
            key = _timeline_key(compressed)
            if cur and key and key == cur[1]:
                cur = (cur[0], cur[1], cur[2] + 1, _clock(compressed))
            else:
                if cur:
                    out.append(cur[0] if cur[2] == 1
                               else f"{cur[0]}  (×{cur[2]} → {cur[3]})")
                cur = (compressed, key, 1, "")
        if cur:
            out.append(cur[0] if cur[2] == 1
                       else f"{cur[0]}  (×{cur[2]} → {cur[3]})")
        return "\n".join(out), len(out)

    # Shrink the sample count until it fits BOTH max_rows and max_chars —
    # always by even sampling across the whole span, NEVER a flat
    # ``digest[:max_chars]`` tail cut (which drops the chronologically latest
    # rows, i.e. the recurring failure near the end of the capture — the 01:26
    # deauth cluster in the reference case).
    k = min(len(picked), max_rows)
    digest, kept = _render(k)
    while len(digest) > max_chars and k > 1:
        k -= 1
        digest, kept = _render(k)
    truncated = k < len(picked)

    print(
        f"[TOKEN] build_event_timeline_digest: strong_hits={len(picked)} "
        f"sampled={k} rows={kept} final_chars={len(digest)} "
        f"est_tokens_sent~{len(digest) // 4} truncated={truncated}"
    )
    _digest_cache_put(_cache_key, digest)
    return digest


def build_event_log_digest(
    events: Optional[List[dict]],
    max_events: int = 40,
    max_chars: int = 4000,
) -> str:
    """Format System-Event-Log Warning/Error rows into a compact, HIGH-SIGNAL
    block the LLM can use as a strong anchor for the issue time.

    Each ``event`` is the dict shape produced by
    ``services.event_log_service.get_paged_events`` — keys: ``time`` (already
    timezone-converted ``YYYY-MM-DD HH:MM:SS``), ``level``, ``source``,
    ``event_id``, ``message``.

    Unlike the rough raw-log browse (which only samples lines and is noisy on a
    huge BT capture), these rows are pre-filtered by the page's Warn+Err drop-
    down, so every line here is already a likely "something went wrong" moment.
    We keep them chronological, cap the count/length, and trim each message so
    the prompt stays small while still carrying the timestamp + symptom.

    Returns "" when there are no usable events (caller then omits the section).
    """
    rows = [e for e in (events or []) if isinstance(e, dict) and e.get("time")]
    if not rows:
        return ""

    # Chronological so the LLM reads them as a timeline. ``time`` is a sortable
    # ISO-ish string already; fall back to original order on any odd value.
    try:
        rows.sort(key=lambda e: str(e.get("time") or ""))
    except Exception:
        pass

    # Errors/Critical are stronger anchors than Warnings — if we have to drop
    # rows to fit the cap, keep the most severe first, then re-sort by time.
    _sev_rank = {"critical": 0, "error": 1, "warning": 2}
    if len(rows) > max_events:
        rows.sort(key=lambda e: _sev_rank.get(str(e.get("level", "")).lower(), 9))
        rows = rows[:max_events]
        try:
            rows.sort(key=lambda e: str(e.get("time") or ""))
        except Exception:
            pass

    lines = []
    for e in rows:
        msg = re.sub(r"\s+", " ", str(e.get("message", "") or "")).strip()[:160]
        lines.append(
            f"[{str(e.get('level', '')).upper()}] {e.get('time', '')} | "
            f"{e.get('source', '')} | ID {e.get('event_id', '')} | {msg}"
        )
    digest = "\n".join(lines)
    if len(digest) > max_chars:
        digest = digest[:max_chars] + "\n…(truncated)"
    return digest


def find_nearest_event_error(
    events: Optional[List[dict]],
    target_dt: Optional[datetime],
    max_diff_seconds: int = 600,
    levels: Tuple[str, ...] = ("error", "critical"),
) -> Optional[dict]:
    """Find the System-Event-Log Error/Critical entry closest in time to
    ``target_dt`` (an AI-suggested or current issue time).

    Shared back-end for the sidebar "Found a nearby system error — which time
    to use?" refine picker: the AI-suggest route already loads the capture's
    Warning/Error rows to anchor the LLM, so we reuse THOSE same rows here to
    compute the nearest fault — no second event-log scan / fetch needed.

    ``events`` are the dicts produced by ``event_log_service.get_paged_events``
    (``time`` already timezone-converted ``YYYY-MM-DD HH:MM:SS``). Returns a
    dict shaped to match the frontend ``findClosestEventError`` contract
    (``formatted_time`` as ``MM/DD/YYYY-HH:MM:SS``), or None when nothing
    qualifies within ``max_diff_seconds``.
    """
    if not events or target_dt is None:
        return None
    best = None
    best_diff = None
    for e in events:
        if not isinstance(e, dict):
            continue
        if str(e.get("level", "")).lower() not in levels:
            continue
        t = str(e.get("time") or "").strip()
        if not t:
            continue
        edt = None
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                edt = datetime.strptime(t[:26], fmt)
                break
            except ValueError:
                continue
        if edt is None:
            continue
        diff = abs((edt - target_dt).total_seconds())
        if best_diff is None or diff < best_diff:
            best_diff, best = diff, (e, edt)
    if best is None or best_diff > max_diff_seconds:
        return None
    e, edt = best
    return {
        "formatted_time": edt.strftime("%m/%d/%Y-%H:%M:%S"),
        "diff_seconds": best_diff,
        "source": str(e.get("source", "")),
        "event_id": str(e.get("event_id", "")),
        "level": str(e.get("level", "")),
        "message": str(e.get("message", "")),
    }


def parse_json_loose(raw: str) -> dict:
    """Best-effort JSON extraction from an LLM reply (tolerates code fences /
    surrounding prose)."""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()
    try:
        return json.loads(raw)
    except Exception:
        a, b = raw.find("{"), raw.rfind("}")
        if a != -1 and b > a:
            try:
                return json.loads(raw[a:b + 1])
            except Exception:
                pass
    return {"interpretation": "", "needs_user_input": True, "suggestions": []}


def _call_llm_and_log(
    llm_client: Any,
    llm_model: str,
    system: str,
    user: str,
    stage_label: str,
    usage_out: Optional[List[dict]],
    max_tokens: int,
    digest_chars: int = 0,
) -> dict:
    """Shared tail for ``llm_suggest`` / ``llm_suggest_desc_only``: print the
    ``[TOKEN] ... request`` line, make the call, print + record usage, parse
    the reply. Extracted because both callers had this block byte-for-byte
    identical apart from ``stage_label``/``max_tokens``/``digest_chars``."""
    system_chars = len(system)
    user_chars = len(user)
    print(
        f"[TOKEN] issue_time_ai {stage_label} request: "
        f"model={llm_model} "
        f"system_chars={system_chars} user_chars={user_chars} "
        f"log_digest_chars={digest_chars} "
        f"total_prompt_chars={system_chars + user_chars}"
    )
    response = llm_client.chat.completions.create(
        model=llm_model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        temperature=0.1,
        max_tokens=max_tokens,
    )
    usage = getattr(response, "usage", None)
    if usage:
        print(
            f"[TOKEN] issue_time_ai {stage_label} usage: "
            f"prompt={usage.prompt_tokens} "
            f"completion={usage.completion_tokens} "
            f"total={usage.total_tokens}"
        )
        if usage_out is not None:
            usage_out.append({
                "stage": stage_label,
                "prompt": int(usage.prompt_tokens),
                "completion": int(usage.completion_tokens),
                "total": int(usage.total_tokens),
            })
    else:
        print(f"[TOKEN] issue_time_ai {stage_label} usage: <no usage on response>")
    return parse_json_loose(response.choices[0].message.content or "")


def llm_suggest(
    llm_client: Any,
    llm_model: str,
    text: str,
    log_digest: str,
    first_ts: Optional[datetime],
    last_ts: Optional[datetime],
    log_has_date: Optional[bool] = None,
    *,
    log_frame_first_ts: Optional[datetime] = None,
    log_frame_last_ts: Optional[datetime] = None,
    tz_label: str = "",
    event_digest: Optional[str] = None,
    stage_label: str = "llm_suggest",
    usage_out: Optional[List[dict]] = None,
    candidate_hint: Optional[Tuple[str, str]] = None,
) -> dict:
    """Ask the LLM to infer issue time(s) from the description + log sample.

    Returns the parsed JSON dict (see ``parse_json_loose`` for the fallback).

    ``log_has_date`` (when known) gates only the OUTPUT FORMAT:
      - True / None  → canonical full ``MM/DD/YYYY-HH:MM:SS.mmm``.
      - False        → time-only ``HH:MM:SS.mmm`` (no fabricated date).

    Timezone-aware kwargs (basis="customer" path):
      ``first_ts`` / ``last_ts`` are presented as the **customer-frame** range
      (already shifted by the caller). ``log_frame_first_ts`` / ``last_ts``
      give the same range in the LOG FRAME so the model can disambiguate when
      a description's time only fits the log frame — it should then convert
      to customer frame on the way out. ``tz_label`` names the customer tz
      (e.g. "Central Standard Time (UTC-05:00)") for the prompt.

    The prompt keeps a Wi-Fi / Bluetooth default (matches the app's primary
    use case) but defers to the user's description for the specific
    scenario, and deliberately does NOT enumerate event types so the model
    isn't primed away from the actual issue.

    ``event_digest`` (optional) is a compact list of System-Event-Log
    Warning/Error rows (see ``build_event_log_digest``). When present it is a
    SECONDARY reference — NOT a forced anchor: the user's description plus the
    raw-log sample remain the source of truth, and the model decides for itself
    whether a nearby fault row is actually relevant before leaning on it. Their
    timestamps are real dated clock times that correlate with the BT log's own
    timeline, so they help cross-check / refine the inferred time.

    ``candidate_hint`` (optional): ``(issue_time_str, reason)`` from a prior
    description-only pass (stage 1). It is presented as a PRELIMINARY guess
    made WITHOUT any log evidence — the model must VALIDATE it against the
    log sample below, not echo it blindly. If the log confirms/is consistent,
    return it (or a nearby precise anchor) with high confidence. If the log
    evidence points elsewhere, prefer the log. This lets a genuine text-based
    time cue narrow the search without ever skipping the log check entirely.
    """
    if first_ts and last_ts:
        rng = f"The log spans {format_issue_time(first_ts)} to {format_issue_time(last_ts)}."
    elif first_ts:
        rng = f"The log starts at {format_issue_time(first_ts)}."
    else:
        rng = "No log timestamps are available."

    # When the caller has applied customer-frame conversion, expose BOTH
    # frames so the model can disambiguate a typed time that only matches
    # the log frame and convert it to customer frame on output.
    #
    # The block is kept frame-name-agnostic: instead of declaring the log
    # frame is "GMT+8" or telling the model to "subtract N hours", we hand
    # over two SAME-INSTANT anchors (raw vs. customer) and ask it to
    # pattern-match. That stays correct regardless of the actual offset and
    # avoids leaking case-specific implementation details into the prompt.
    #
    # When tz_label is empty (no tz detected, or basis="log") or the two
    # frames coincide (offset ~ 0), the block stays out of the prompt and
    # behaviour matches the pre-tz default.
    tz_block = ""
    if (tz_label and log_frame_first_ts and log_frame_last_ts
            and first_ts and last_ts):
        offset_seconds = abs((first_ts - log_frame_first_ts).total_seconds())
        if offset_seconds >= 60:  # any meaningful offset (>= 1 minute)
            tz_block = (
                f"\nTimezone context:"
                f"\n- The log lines in the digest below carry RAW timestamps "
                f"from the log's own frame."
                f"\n- The customer's wall clock is {tz_label}."
                f"\n- Same-instant anchors (use these to convert):"
                f"\n    raw {format_issue_time(log_frame_first_ts)}"
                f"  =  customer {format_issue_time(first_ts)}"
                f"\n    raw {format_issue_time(log_frame_last_ts)}"
                f"  =  customer {format_issue_time(last_ts)}"
                f"\n- OUTPUT every issue_time in the CUSTOMER frame."
                f"\n- If you pick a timestamp from a log line, convert it to "
                f"customer using the anchors above before emitting."
                f"\n- If the description's time only fits the raw range, the "
                f"user transcribed from the log — convert the same way."
            )

    if log_has_date is False:
        format_rule = (
            "This log carries NO date — only a time-of-day on each line. "
            "Output every issue_time as time-only HH:MM:SS.mmm (NO date, "
            "NO year, NO month). Do not invent a year or a date."
        )
        format_example = '"issue_time":"HH:MM:SS.mmm"'
    else:
        format_rule = (
            "Always output times in the canonical format MM/DD/YYYY-HH:MM:SS.mmm."
        )
        format_example = '"issue_time":"MM/DD/YYYY-HH:MM:SS.mmm"'

    # Wi-Fi / Bluetooth is the default backdrop (that's what this app
    # analyses), but we deliberately don't enumerate event types or
    # vocabulary — the user's description carries the actual specifics
    # and the digest carries the actual log lines.
    # When System-Event-Log Warn/Err rows are available, offer them as a
    # SECONDARY reference only: the description + raw-log sample stay the
    # source of truth, and the model itself decides whether a fault row is
    # actually relevant before using it to refine the inferred time.
    event_priority = (
        " A list of System Event Log Warning/Error entries is also provided as "
        "a SECONDARY reference (not a mandatory anchor). Judge the issue time "
        "primarily from the user's description and the raw log sample; only if "
        "a fault entry's timing and source genuinely match the scenario, use it "
        "to cross-check or fine-tune the time. Do NOT force the time onto an "
        "event-log row when the description and log point elsewhere. "
        if event_digest else ""
    )
    candidate_block = ""
    if candidate_hint:
        cand_time, cand_reason = candidate_hint
        candidate_block = (
            f" A PRELIMINARY candidate time was inferred from the description "
            f"ALONE, with NO log evidence: {cand_time} (reason: {cand_reason or 'n/a'}). "
            f"Treat this ONLY as a starting hint to narrow your search — you MUST "
            f"validate it against the actual log sample below. If the log shows "
            f"real evidence (a fault/failure line) at or very near this time, "
            f"confirm it (or refine to the exact evidencing line) with high "
            f"confidence. If the log evidence clearly points to a different "
            f"moment, prefer the log evidence instead — do NOT echo the "
            f"candidate without checking. "
        )
    system = (
        "You determine the 'issue time' that anchors log analysis. Logs are "
        "typically Wi-Fi or Bluetooth, but the user's problem description "
        "(which may be vague, non-English, or use a wrong time format) is "
        "the source of truth for the specific scenario. Read it plus a "
        "rough sample of the log, then infer the most likely issue time(s) "
        "— use the description to decide which lines in the log are "
        "relevant, and pick the timestamp that sits next to one of those "
        "lines. "
        f"{event_priority}{candidate_block}"
        f"{rng}{tz_block} {format_rule}\n"
        "Reply with STRICT JSON only (no markdown, no prose) of the form:\n"
        '{"interpretation":"<one short sentence on what the user means>",'
        '"needs_user_input":<true|false>,'
        f'"suggestions":[{{{format_example},'
        '"confidence":"high|medium|low","reason":"<short why>",'
        '"source":"description|log|inferred"}]}\n'
        "At most 5 suggestions, best first. If you genuinely cannot infer a time, "
        "return an empty suggestions list and needs_user_input=true."
    )
    # When the user gave no description, the LLM has to pick a time purely
    # from the log. Keep this hint abstract — "notable" is enough; listing
    # concrete event types here pushes the model toward the wrong domain.
    fallback_text = (
        '(none provided — pick the time of the most notable event you can '
        'identify in the log)'
    )
    event_section = (
        f"\n\n=== System Event Log (Warning/Error — secondary reference) ===\n{event_digest}"
        if event_digest else ""
    )
    user = (
        "User description:\n"
        f"{text or fallback_text}\n\n"
        f"=== Rough log sample ===\n{log_digest or '(no log loaded)'}"
        f"{event_section}"
    )
    return _call_llm_and_log(
        llm_client, llm_model, system, user, stage_label, usage_out,
        max_tokens=900, digest_chars=len(log_digest or ""),
    )


def llm_suggest_desc_only(
    llm_client: Any,
    llm_model: str,
    text: str,
    first_ts: Optional[datetime],
    last_ts: Optional[datetime],
    log_has_date: Optional[bool] = None,
    *,
    tz_label: str = "",
    usage_out: Optional[List[dict]] = None,
) -> dict:
    """Cheap stage-1: infer issue time from description + log time RANGE only.

    No log digest, no event digest, no tz-anchor block — just the user text and
    the log's first/last timestamps. Typical prompt ~200-400 tokens (vs. the
    ~6000 tokens of a full ``llm_suggest`` call). The caller inspects the
    returned suggestions and only escalates to ``llm_suggest`` when the model
    itself signals uncertainty (empty list, low/medium confidence, or
    ``needs_user_input=true``).

    We deliberately do NOT accept a "current sidebar issue_time" hint here.
    Users press the AI button precisely because they're unsure about the
    sidebar value; feeding it back to a description-only LLM (which has no
    log to check against) just yields a biased echo. Sidebar-anchored
    validation belongs in stage2, where actual log evidence is available.

    Same output shape as ``llm_suggest``.
    """
    if first_ts and last_ts:
        rng = f"The related log spans {format_issue_time(first_ts)} to {format_issue_time(last_ts)}."
    elif first_ts:
        rng = f"The related log starts at {format_issue_time(first_ts)}."
    else:
        rng = "No log time range available."

    tz_note = (f" Customer wall clock: {tz_label}. Output times in the customer frame."
               if tz_label else "")

    if log_has_date is False:
        format_rule = ("Log has NO date — output HH:MM:SS.mmm only. "
                       "Do NOT invent a year/month/day.")
        format_example = '"issue_time":"HH:MM:SS.mmm"'
    else:
        format_rule = "Output MM/DD/YYYY-HH:MM:SS.mmm."
        format_example = '"issue_time":"MM/DD/YYYY-HH:MM:SS.mmm"'

    system = (
        "You infer the 'issue time' for Wi-Fi/Bluetooth log analysis from the "
        "user's problem description alone. You do NOT have a log preview — "
        f"only the log's time range. {rng}{tz_note} {format_rule}\n"
        "Rules:\n"
        "1. If the description carries an explicit clock time OR you can "
        "PRECISELY pin the moment (e.g. user says 'at 04:45 PM', 'around 13:22:10'), "
        "return a HIGH-confidence suggestion.\n"
        "2. If the description is vague (e.g. 'wifi disconnected', 'connection "
        "failed today', no clock hint), return EMPTY suggestions and "
        "needs_user_input=true — a follow-up pass will read the log.\n"
        "Reply with STRICT JSON only:\n"
        '{"interpretation":"<one short sentence>",'
        '"needs_user_input":<true|false>,'
        f'"suggestions":[{{{format_example},'
        '"confidence":"high|medium|low","reason":"<short why>",'
        '"source":"description"}}]}\n'
        "At most 3 suggestions."
    )
    user = f"User description:\n{text}"
    return _call_llm_and_log(
        llm_client, llm_model, system, user, "stage1_desc_only", usage_out,
        max_tokens=400,
    )


def _first_high_confidence(sugs: List[dict]) -> Optional[dict]:
    """First suggestion (in list order) with ``confidence=='high'`` and a
    non-empty ``issue_time``, else ``None``. Shared by the stage1 and stage2a
    promotion checks in ``build_issue_time_suggestions`` — both only act on a
    genuinely high-confidence, evidenced suggestion, never a maybe-right
    guess."""
    for s in sugs:
        if (str(s.get("confidence", "")).lower() == "high"
                and (s.get("issue_time") or "").strip()):
            return s
    return None


# high=0, medium=1, low=2; unknown/missing treated as medium. This is the
# only place confidence gets ranked or filtered — neither frontend template
# re-ranks or re-filters suggestions; both just render them in the order
# this module returns them (row 0 = best).
_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}


def _filter_to_highest_confidence(sugs: List[dict]) -> List[dict]:
    """Keep only the suggestions at the single HIGHEST confidence tier
    present (high > medium > low) — e.g. if any suggestion is "high", every
    "medium"/"low" one is dropped from what the user sees; if the best
    present is only "medium", the "low" ones are dropped and the "medium"
    ones are kept. Applies to BOTH chatbots (log_chatbot and bt_chatbot),
    since they share this function. Preserves the model's own relative
    order within the kept tier — never re-sorts. A suggestion list that's
    already empty is returned as-is."""
    if not sugs:
        return sugs
    ranked = [(s, _CONFIDENCE_RANK.get(str(s.get("confidence", "")).lower(), 1)) for s in sugs]
    best_rank = min(r for _, r in ranked)
    return [s for s, r in ranked if r == best_rank]


def _explicit_only_payload(explicit_suggestions: List[dict], message: str) -> dict:
    """Response payload for the two "explicit time(s), AI not consulted"
    returns in build_issue_time_suggestions (the plain not-force_ai
    short-circuit, and the force_ai-requested-but-no-LLM-configured case) —
    identical shape, only the message differs per caller."""
    return {
        "success": True,
        "user_explicit": True,
        "ai_also_analyzed": False,
        "interpretation": "You provided explicit time(s) — using them as-is.",
        "needs_user_input": False,
        "suggestions": explicit_suggestions,
        "message": message,
    }


def build_issue_time_suggestions(
    text: str,
    log_lines: Optional[List[str]] = None,
    first_ts: Optional[datetime] = None,
    last_ts: Optional[datetime] = None,
    llm_client: Any = None,
    llm_model: Optional[str] = None,
    log_has_date: Optional[bool] = None,
    *,
    log_frame_first_ts: Optional[datetime] = None,
    log_frame_last_ts: Optional[datetime] = None,
    tz_label: str = "",
    event_log_events: Optional[List[dict]] = None,
    current_issue_time: Optional[str] = None,
    stage1_model: Optional[str] = None,
    force_ai: bool = False,
) -> dict:
    """Full orchestration. Returns a response payload dict ready to ``jsonify``.

    Keys: ``success``, ``user_explicit``, ``interpretation``,
    ``needs_user_input``, ``suggestions``, ``message`` (and ``error`` on the
    no-LLM-configured path). ``success`` is False ONLY when the LLM is needed
    but no client/model was supplied — the caller can map that to HTTP 503.

    ``log_has_date`` (when known) propagates to ``make_suggestion`` so a
    time-only log (DDD / tracefmt) yields suggestions WITHOUT a placeholder
    date (month/day/year = None, issue_time formatted as ``HH:MM:SS.mmm``).
    Default ``None`` keeps the original behaviour (treat as dated).

    ``event_log_events`` (optional) is the list of System-Event-Log
    Warning/Error rows (``get_paged_events`` shape) for the loaded capture.
    When supplied they are turned into a SECONDARY-reference digest and fed
    to the LLM alongside the raw-log sample — the model weighs them itself
    rather than being forced onto a fault row. Omitting it (Wi-Fi /
    log_chatbot) keeps the original behaviour.

    ``current_issue_time`` (optional): the time-string the user has already
    filled into the sidebar (typically ``agent.issue_time`` formatted). Used
    for CACHE INVALIDATION ONLY — a sidebar edit changes the cache key so a
    retry does not reuse a stale payload. We deliberately do NOT feed it to
    the stage-1 LLM as a validation hint: users press the AI button precisely
    because they're unsure about the sidebar value, and a description-only
    LLM (no log evidence) that receives the sidebar value tends to echo it
    back as "high confidence", biasing the result toward a possibly-wrong
    guess. Stage-2 sees actual log evidence and can't be tricked that way.

    ``stage1_model`` (optional): a cheaper/faster model to use for stage 1
    (description-only, no log digest) ONLY. Defaults to ``llm_model`` when
    omitted, so passing nothing keeps today's single-model behaviour exactly.
    Stage 1 NEVER produces the final answer by itself when a log is loaded —
    it only proposes a CANDIDATE hint (kept when the model claims "high"
    confidence) that stage 2 (which has actual log evidence) must validate,
    confirm, or override. This means a weaker stage-1 model's mistakes are
    always caught by the log-driven stage 2 rather than shipped straight to
    the user — the only way stage 1 alone determines the outcome is when
    there is no log at all to check it against. Stage 2a/2b intentionally do
    NOT get a cheaper-model override here: those steps disambiguate between
    multiple plausible log events using the free-text description, which is
    a real reasoning task, not a mechanical check.

    ``force_ai`` (default False): when the description carries an explicit
    time, the default behaviour is to short-circuit on it — deterministic,
    zero LLM tokens (see step 1 below). Set ``force_ai=True`` to ALSO run the
    LLM cascade against the log even though an explicit time was found (the
    "also let AI check the log" button) — e.g. when the user isn't sure their
    typed time is actually the best-evidenced moment. The explicit time(s)
    are kept and returned FIRST (still marked ``source="user"``), with the
    AI's own suggestions appended after. Has no effect when the description
    has no explicit time — that path already always runs the AI cascade.
    """
    stage1_llm_model = stage1_model or llm_model
    no_date = log_has_date is False

    # 1) User-first deterministic pass — explicit time(s) win, no LLM.
    # Compute the log-frame → customer-frame shift so a typed value that
    # only fits the log frame gets transparently converted. When both
    # ranges are present the delta is just (customer - log frame), with
    # `first_ts` already in customer frame and `log_frame_first_ts` in the
    # raw log frame.
    tz_to_customer_delta = None
    if (first_ts and log_frame_first_ts):
        tz_to_customer_delta = first_ts - log_frame_first_ts
    explicit, kind = extract_explicit_times(
        text, first_ts, last_ts,
        log_frame_first_ts=log_frame_first_ts,
        log_frame_last_ts=log_frame_last_ts,
        tz_to_customer_delta=tz_to_customer_delta,
    )
    explicit_suggestions: List[dict] = []
    if explicit:
        if no_date:
            reason = "Clock time taken from your message (log has no date)."
        elif kind == "time_only":
            reason = "Clock time taken from your message; date aligned to the log."
        else:
            reason = "Explicit timestamp taken directly from your message."
        explicit_suggestions = [make_suggestion(dt, "high", reason, "user",
                                                 log_has_date=not no_date)
                                 for dt in explicit]
        if not force_ai:
            return _explicit_only_payload(
                explicit_suggestions,
                f"Found {len(explicit_suggestions)} explicit time(s) in your text.",
            )
        # force_ai=True: fall through to also run the LLM cascade below,
        # merging its suggestions after these explicit ones.

    # 2) LLM inference for vague / malformed / missing times (or, with
    # force_ai, an additional opinion alongside an explicit time already
    # found above).
    if llm_client is None or not llm_model:
        if explicit_suggestions:
            # force_ai was requested but there's no LLM to honor it with —
            # still return the explicit time(s) rather than erroring out.
            return _explicit_only_payload(
                explicit_suggestions,
                f"Found {len(explicit_suggestions)} explicit time(s) in your "
                f"text. AI check skipped — LLM is not configured on this server.",
            )
        return {
            "success": False,
            "user_explicit": False,
            "ai_also_analyzed": False,
            "interpretation": "",
            "needs_user_input": True,
            "suggestions": [],
            "error": "LLM is not configured on this server.",
            "message": "LLM is not configured on this server.",
        }

    # 2a) Response cache — same (text, log fingerprint, frames, current
    # candidate) reuses the last payload. Users retry the button often when
    # fine-tuning the description; identical inputs shouldn't burn fresh
    # tokens. current_issue_time IS part of the key so a sidebar edit
    # invalidates the cache automatically.
    cache_key = _suggest_cache_key(
        text=text, log_lines=log_lines, first_ts=first_ts, last_ts=last_ts,
        log_has_date=log_has_date, tz_label=tz_label,
        log_frame_first_ts=log_frame_first_ts,
        log_frame_last_ts=log_frame_last_ts,
        event_events_len=len(event_log_events or []),
        current_issue_time=current_issue_time,
        stage1_model=stage1_llm_model,
        force_ai=force_ai,
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        print(f"[TOKEN] issue_time_ai TOTAL: cache=HIT stage1=- stage2=- "
              f"prompt=0 completion=0 total=0 (key={cache_key[:8]}…)")
        # Return a shallow copy so downstream mutation can't poison the cache.
        return dict(cached)

    # Per-request token accumulator populated by llm_suggest / stage1 helper.
    usage_out: List[dict] = []

    # 2b) Stage 1: cheap description-only pass (~300-500 tokens), run only
    # when the description carries a genuine time cue (`_has_time_hint`).
    # IMPORTANT: stage 1's result is NEVER treated as final by itself when a
    # log is loaded — it produces only a CANDIDATE hint that stage 2 (which
    # has actual log evidence) must validate/confirm/override. This is
    # deliberate: a description-only LLM has no way to check whether its
    # guess actually matches a real event in the log, so shipping it straight
    # to the user risks a confidently-wrong answer (e.g. picking a plausible-
    # sounding but wrong minute). The candidate still saves tokens overall —
    # it lets stage 2 anchor its search instead of scanning blind — but the
    # log always gets the final say. The ONLY case stage 1 stands alone is
    # when there's no log loaded at all (nothing to validate against).
    #
    # We deliberately do NOT trigger stage1 based on a pre-filled sidebar
    # issue_time — the user usually pressed the AI button precisely BECAUSE
    # they're unsure about the sidebar value. The sidebar value still
    # participates in the cache key so a sidebar edit invalidates stale
    # results.
    llm: dict = {"interpretation": "", "needs_user_input": True, "suggestions": []}
    stage1_skipped_reason = ""
    candidate_hint: Optional[Tuple[str, str]] = None
    if not text:
        stage1_skipped_reason = "no_description_text"
    elif not _has_time_hint(text):
        stage1_skipped_reason = "no_time_hint_in_text"
    else:
        try:
            stage1 = llm_suggest_desc_only(
                llm_client, stage1_llm_model, text, first_ts, last_ts,
                log_has_date=log_has_date, tz_label=tz_label,
                usage_out=usage_out,
            )
        except Exception as _e:
            print(f"[issue_time_ai] stage1 failed, escalating to stage2: {_e}")
            stage1 = {"interpretation": "", "needs_user_input": True, "suggestions": []}
        s1_sugs = stage1.get("suggestions") or []
        # Only promote to a candidate hint when stage 1 itself claims "high"
        # confidence — a maybe-right guess without log evidence isn't good
        # enough even to seed the search.
        top = _first_high_confidence(s1_sugs)
        if top is not None and not stage1.get("needs_user_input", False):
            # Keep stage1's full result as a fallback ONLY for the no-log
            # case (handled below); do NOT treat it as final here.
            llm = stage1
            candidate_hint = (str(top.get("issue_time") or ""),
                               str(top.get("reason") or ""))

    log_digest = ""
    event_digest = ""
    stage2b_escalated = False
    # Whenever a log is loaded, ALWAYS run the log-driven stage(s) — even
    # when stage 1 produced a high-confidence candidate. A description-only
    # guess is never allowed to be the final answer while real log evidence
    # is available to check it against; the candidate is passed through as a
    # hint (see ``candidate_hint`` in ``llm_suggest``) so it narrows rather
    # than replaces the log-based search. Only when there's NO log at all
    # (``log_lines`` empty) does stage 1's own result stand as the answer,
    # since there's nothing left to validate it against.
    stage2_needed = bool(log_lines) or bool(event_log_events)
    if stage2_needed:
        event_digest = build_event_log_digest(event_log_events)

        # 2a) Cheap high-signal pass: a compact failure-timeline digest (strong
        # anchors only) instead of the full head/tail/keyword browse. For a
        # Wi-Fi failure the issue time IS one of these rows, so this usually
        # resolves the time at ~1/3 the tokens of the full digest. We escalate
        # to the full digest (2b) only when the timeline is empty (no fault
        # lines) or the model isn't confident — so accuracy is never traded
        # away, only tokens are saved on the common path.
        stage2b_needed = True
        timeline_digest = build_event_timeline_digest(log_lines or [])
        if timeline_digest:
            try:
                llm2a = llm_suggest(
                    llm_client, llm_model, text, timeline_digest, first_ts, last_ts,
                    log_has_date=log_has_date,
                    log_frame_first_ts=log_frame_first_ts,
                    log_frame_last_ts=log_frame_last_ts,
                    tz_label=tz_label,
                    event_digest=event_digest,
                    stage_label="stage2a_timeline",
                    usage_out=usage_out,
                    candidate_hint=candidate_hint,
                )
            except Exception as _e:
                print(f"[issue_time_ai] stage2a failed, escalating to stage2b: {_e}")
                llm2a = {"interpretation": "", "needs_user_input": True, "suggestions": []}
            s2a_sugs = llm2a.get("suggestions") or []
            if (_first_high_confidence(s2a_sugs) is not None
                    and not llm2a.get("needs_user_input", False)):
                llm = llm2a
                stage2b_needed = False

        # 2b) Full-digest fallback. Wrap the LLM call so any network / parse /
        # API-error failure degrades gracefully into "no suggestions" instead
        # of a 500 — the log-first fallback below then still gives the user a
        # usable anchor. Without this, a transient hiccup makes the AI button
        # look broken.
        if stage2b_needed:
            stage2b_escalated = True
            log_digest = build_log_digest(log_lines or [])
            try:
                llm = llm_suggest(
                    llm_client, llm_model, text, log_digest, first_ts, last_ts,
                    log_has_date=log_has_date,
                    log_frame_first_ts=log_frame_first_ts,
                    log_frame_last_ts=log_frame_last_ts,
                    tz_label=tz_label,
                    event_digest=event_digest,
                    stage_label="stage2b_full",
                    usage_out=usage_out,
                    candidate_hint=candidate_hint,
                )
            except Exception as _e:
                print(f"[issue_time_ai] stage2b failed, deferring to fallback: {_e}")
                llm = {"interpretation": "", "needs_user_input": True, "suggestions": []}

    ref = last_ts or first_ts
    suggestions = []
    # Only surface the single highest confidence tier the model actually
    # reported (see _filter_to_highest_confidence) — applies uniformly to
    # both chatbots since they share this function.
    for s in _filter_to_highest_confidence(llm.get("suggestions") or [])[:5]:
        dt, is_time_only = parse_issue_time_string((s.get("issue_time") or "").strip())
        if dt is None:
            continue
        # Models occasionally invent placeholder dates (year 1970 epoch, etc.)
        # even when told to output time-only. Treat any year < _MIN_PLAUSIBLE_YEAR
        # as undated so it gets re-anchored against the log instead of leaking
        # a bogus date through into the sidebar.
        undated = is_time_only or dt.year < _MIN_PLAUSIBLE_YEAR
        if undated and ref and not no_date:
            # Align clock to the log's date — but only for dated logs. For
            # time-only logs there's no real date to attach.
            dt = ref.replace(hour=dt.hour, minute=dt.minute,
                             second=dt.second, microsecond=dt.microsecond)
        suggestions.append(make_suggestion(
            dt,
            str(s.get("confidence", "medium")),
            str(s.get("reason", "")),
            str(s.get("source", "inferred")),
            log_has_date=not no_date,
        ))

    # Fallback when the LLM returned nothing: at least offer the LAST
    # parseable timestamp in the log as a low-confidence anchor. We align
    # with the project-wide convention for "no specific time known" —
    # /set_log auto-fills with log_last_time, the "Use log's last time"
    # button uses it, and the inline "Log ends at:" hint surfaces it.
    # Using the same value here keeps the AI fallback consistent with the
    # rest of the UX and matches the common case where the user's issue
    # describes the trailing state of the log (failure observed near the
    # end). The user still sees "low" confidence + a reason explaining
    # it's a guess, and needs_user_input stays True so they're prompted
    # to confirm or edit.
    # Skip the "log's last timestamp" low-confidence filler when we already
    # have a real user-provided anchor (force_ai + explicit path) — it would
    # just clutter the list next to a suggestion the user typed themselves.
    if not suggestions and log_lines and not explicit_suggestions:
        fallback_dt = _find_last_log_timestamp(log_lines, no_date_log=no_date)
        if fallback_dt is not None:
            suggestions.append(make_suggestion(
                fallback_dt, "low",
                ("No exact anchor matched the description; using the log's "
                 "last event time as a starting point. Edit if the issue "
                 "happened earlier in the trace."),
                "inferred",
                log_has_date=not no_date,
            ))

    # Explicit time(s) — if any (force_ai path) — lead the list; the AI's own
    # suggestions follow. Order matters here: the user's own typed time stays
    # the default pick in the frontend's capture popup.
    all_suggestions = explicit_suggestions + suggestions if explicit_suggestions else suggestions

    if explicit_suggestions:
        msg = (f"Found {len(explicit_suggestions)} explicit time(s) in your text; "
               f"AI also suggests {len(suggestions)} more.") if suggestions else (
               f"Found {len(explicit_suggestions)} explicit time(s) in your text. "
               f"AI didn't find an additional anchor to add.")
    else:
        msg = (f"AI suggested {len(suggestions)} time(s)."
               if suggestions else
               "AI couldn't pin down a specific time — please review or fill it in.")
    payload = {
        "success": True,
        "user_explicit": bool(explicit_suggestions),
        "ai_also_analyzed": True,
        "interpretation": str(llm.get("interpretation", "")),
        # With an explicit anchor already in hand, don't force a confirm
        # prompt just because the AI's own read was uncertain. Otherwise
        # (no explicit time), keep needs_user_input True unless the LLM
        # itself confidently said otherwise — even with a low-confidence
        # fallback, the user should still double-check.
        "needs_user_input": (False if explicit_suggestions
                              else bool(llm.get("needs_user_input", not suggestions))),
        "suggestions": all_suggestions,
        "message": msg,
    }

    # Per-request token summary: sum every LLM call (stage1 alone, or stage1 +
    # stage2 on escalate) so the console shows the real spend for this button
    # click. The individual [TOKEN] stageN lines above already report per-call
    # detail; this line is the "grand total" the user asked for.
    total_prompt = sum(u.get("prompt", 0) for u in usage_out)
    total_completion = sum(u.get("completion", 0) for u in usage_out)
    total_all = sum(u.get("total", 0) for u in usage_out)
    stage1_total = sum(u.get("total", 0) for u in usage_out
                       if u.get("stage") == "stage1_desc_only")
    stage2a_total = sum(u.get("total", 0) for u in usage_out
                        if u.get("stage") == "stage2a_timeline")
    stage2b_total = sum(u.get("total", 0) for u in usage_out
                        if u.get("stage") == "stage2b_full")
    print(
        f"[TOKEN] issue_time_ai TOTAL: cache=MISS "
        f"stage1_model={stage1_llm_model} "
        f"stage1_skipped_reason={stage1_skipped_reason or '-'} "
        f"stage1_candidate_hint={candidate_hint[0] if candidate_hint else '-'} "
        f"log_available={bool(log_lines) or bool(event_log_events)} "
        f"stage2b_escalated={stage2b_escalated} "
        f"stage1_total={stage1_total} stage2a_total={stage2a_total} "
        f"stage2b_total={stage2b_total} "
        f"prompt={total_prompt} completion={total_completion} total={total_all} "
        f"(key={cache_key[:8]}…)"
    )

    _cache_put(cache_key, payload)
    return payload


# Regexes used by the log-first-timestamp fallback. Two flavours:
#   * Dated:     MM/DD/YYYY-HH:MM:SS.fff  (Wi-Fi ETL)
#   * Time-only: HH:MM:SS[:.]fff           (DDD / tracefmt — ms separator can
#                                           be ':' or '.')
_FALLBACK_DATED_RE = re.compile(
    r'(\d{2}/\d{2}/\d{4})[-\s]+(\d{1,2}:\d{2}:\d{2})(?:[.](\d{1,6}))?'
)
_FALLBACK_TIME_ONLY_RE = re.compile(
    r'(?<!\d)(\d{1,2}:\d{2}:\d{2})(?:[:.](\d{1,6}))?(?!\d)'
)


def _find_last_log_timestamp(
    log_lines: List[str], no_date_log: bool, scan_limit: int = 200
) -> Optional[datetime]:
    """Return the LAST parseable timestamp in the tail of the log, or None.

    Used as a low-confidence fallback when the LLM can't pin a time. The
    "last event time" matches the project-wide convention for unknown
    issue times (see /set_log → log_last_time, the "Use log's last time"
    button, and the DDD "Log ends at:" inline hint), so the AI fallback
    stays consistent with the rest of the UX. Scans only the trailing
    ``scan_limit`` lines, since the "last event time" sits at the end
    of the file.

    For dated logs returns a real ``datetime``. For time-only logs returns a
    placeholder-dated datetime (only H/M/S matter — caller serialises via
    ``make_suggestion(log_has_date=False)`` which drops the date).
    """
    rx = _FALLBACK_TIME_ONLY_RE if no_date_log else _FALLBACK_DATED_RE
    # Walk the trailing scan_limit lines from the END toward the front, so
    # the very last parseable timestamp wins. `reversed` returns lines in
    # tail-first order; the first regex hit is the latest in chronological
    # order assuming the log is time-ordered (which both dated WiFi ETL and
    # DDD/tracefmt traces are).
    tail = (log_lines or [])[-scan_limit:]
    for line in reversed(tail):
        m = rx.search(line)
        if not m:
            continue
        try:
            if no_date_log:
                hms = m.group(1)
                ms_raw = m.group(2) or "0"
                hh, mm, ss = (int(x) for x in hms.split(":"))
                micro = int(ms_raw.ljust(6, "0")[:6])
                # datetime.min.date() — caller knows to drop the date.
                return datetime.min.replace(
                    hour=hh, minute=mm, second=ss, microsecond=micro
                )
            date_s = m.group(1)         # MM/DD/YYYY
            hms = m.group(2)            # HH:MM:SS
            ms_raw = m.group(3) or "0"
            mo, dy, yr = (int(x) for x in date_s.split("/"))
            hh, mm, ss = (int(x) for x in hms.split(":"))
            micro = int(ms_raw.ljust(6, "0")[:6])
            return datetime(yr, mo, dy, hh, mm, ss, micro)
        except (ValueError, IndexError):
            continue
    return None


# Below this year a parsed date is treated as "no real date" — i.e. a clock-only
# token (year 1) or a fabricated placeholder (e.g. epoch 1970 the LLM invents
# when it has no log to anchor to). These get a real date later, from the log.
_MIN_PLAUSIBLE_YEAR = 2000


def _canonical_or_clock(dt: datetime) -> str:
    """Full canonical ``MM/DD/YYYY-HH:MM:SS.mmm`` string, or a bare ``HH:MM:SS``
    when the datetime carries no real date (a clock-only token resolved without
    a log reference, or an implausible/placeholder date). Keeps undated times
    honest instead of stamping a guessed year."""
    if dt.year < _MIN_PLAUSIBLE_YEAR:
        return dt.strftime("%H:%M:%S")
    return format_issue_time(dt)


def realign_times_to_log(times, first_ts: Optional[datetime] = None,
                         last_ts: Optional[datetime] = None,
                         log_path: str = "") -> list:
    """Give clock-only / undated issue-time strings the loaded log's date so they
    land inside its range. Full-date strings are left untouched. Deterministic —
    no LLM — so a result organized earlier (e.g. on the select-attachments page,
    before any log existed) can be reused and dated once a log is loaded.

    Frame handling for time-only clocks (e.g. "04:45 PM"): the clock is the
    CUSTOMER wall clock (locked default #4), but ``first_ts`` / ``last_ts`` are
    in the log frame (decoder host, GMT+8). Stamping the clock straight onto the
    log date mixes frames and lands ~tz-offset hours off. When ``log_path`` lets
    us detect the customer tz, we instead anchor the clock to the customer
    CAPTURE date (the log's last ts shifted into the customer tz) and convert
    customer→log, so the returned value is a correct log-frame datetime. Without
    a tz (Taiwan customer / unknown) the clock and log frame coincide, so the
    original stamp-onto-log-date behaviour is the right no-op."""
    ref = last_ts or first_ts

    # Detect customer tz + the capture date in the customer frame, used to
    # frame-correctly date time-only (customer-frame) clocks.
    customer_tz = ""
    cust_capture_date = None
    if log_path:
        from utils.timezone_utils import get_effective_timezone, taiwan_to_local
        customer_tz = get_effective_timezone(log_path)
        if customer_tz and ref:
            cust_capture_date = (taiwan_to_local(ref, customer_tz) or ref).date()

    out = []
    for s in (times or []):
        s = str(s).strip()
        if not s:
            continue
        dt, is_time_only = parse_issue_time_string(s)
        if dt is None:
            continue
        undated = is_time_only or dt.year < _MIN_PLAUSIBLE_YEAR
        if undated and customer_tz and cust_capture_date:
            from utils.timezone_utils import local_to_taiwan
            cust_dt = datetime.combine(cust_capture_date, dt.time())
            log_dt = local_to_taiwan(cust_dt, customer_tz) or cust_dt
            out.append(format_issue_time(log_dt))
        elif undated and ref:
            dt = ref.replace(hour=dt.hour, minute=dt.minute,
                             second=dt.second, microsecond=dt.microsecond)
            out.append(format_issue_time(dt))
        elif undated:
            out.append(dt.strftime("%H:%M:%S"))
        else:
            out.append(format_issue_time(dt))
    return out


# Section markers after which a case description turns into environment dumps
# / boilerplate that the LLM doesn't need — cutting here saves tokens.
_NOISE_MARKERS = (
    "Show Environment Details", "Environment Details", "Show Comments",
    "System Information", "System Info", "Device Manager", "Driver Version:",
    "OS Version:", "===",
)


def prefilter_description(text: str, max_chars: int = 1500) -> str:
    """Cheap, deterministic noise reduction applied BEFORE the LLM call so we
    spend fewer tokens. Normalizes whitespace, drops the environment/boilerplate
    tail (the core problem statement comes first), and caps the length. Purely
    string work — no LLM, no network."""
    if not text:
        return ""
    t = str(text).replace("\xa0", " ").replace("\r", "\n")
    # Cut at the first environment/boilerplate marker, but only once we've kept
    # enough lead-in text that we won't throw away the actual problem statement.
    cut = len(t)
    for marker in _NOISE_MARKERS:
        idx = t.find(marker)
        if idx >= 80:
            cut = min(cut, idx)
    t = t[:cut]
    # Collapse whitespace runs.
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    if len(t) > max_chars:
        t = t[:max_chars].rsplit(" ", 1)[0] + " …"
    return t


def organize_issue_context(
    description: str,
    first_ts: Optional[datetime] = None,
    last_ts: Optional[datetime] = None,
    llm_client: Any = None,
    llm_model: Optional[str] = None,
    return_usage: bool = False,
) -> Union[dict, Tuple[dict, dict]]:
    """
    LLM-organize a raw case "Issue Description" into a clean problem statement
    plus the issue time point(s) it mentions — there may be SEVERAL (e.g.
    "1.23:16 ... 23:17:09 ..."). This replaces the old single-regex extraction
    with a smarter pass, and falls back to that deterministic regex when no LLM
    is configured or the model returns nothing usable.

    Inputs are plain (no Flask / agent coupling): the raw description plus the
    related log's time range (used to attach a date to clock-only times).

    Returns:
      {
        "clean_description": str,    # problem statement for the chat input box
        "issue_times": [str, ...],   # canonical MM/DD/YYYY-HH:MM:SS.mmm, best-first
        "interpretation": str,
      }

    With ``return_usage=True`` the return is instead a ``(data, usage)`` tuple,
    where ``data`` is the dict above and ``usage`` carries the token counts of
    every LLM call this function made — ``llm_calls``, ``input_tokens``,
    ``cache_read_tokens``, ``cache_write_tokens``, ``output_tokens``,
    ``total_tokens``. It is all zeros on the deterministic fallback path, which
    makes no calls. Callers that only want the data must leave the flag off;
    unpacking a two-item tuple from the default shape will not work.
    """
    description = (description or "").strip()
    ref = last_ts or first_ts
    operation_usage = {
        "llm_calls": 0, "input_tokens": 0, "cache_read_tokens": 0,
        "cache_write_tokens": 0, "output_tokens": 0, "total_tokens": 0,
    }

    def _finish(data: dict):
        return (data, operation_usage) if return_usage else data

    def _capture_usage(usage) -> None:
        if usage is None:
            return

        def _n(*names: str) -> int:
            for name in names:
                try:
                    value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
                    if value is not None:
                        return max(0, int(value or 0))
                except (TypeError, ValueError):
                    pass
            return 0

        prompt = _n("prompt_tokens", "input_tokens")
        output = _n("completion_tokens", "output_tokens")
        cache_read = _n("cache_read_input_tokens", "cache_read_tokens")
        cache_write = _n("cache_creation_input_tokens", "cache_write_tokens")
        operation_usage["llm_calls"] += 1
        operation_usage["input_tokens"] += prompt
        operation_usage["cache_read_tokens"] += cache_read
        operation_usage["cache_write_tokens"] += cache_write
        operation_usage["output_tokens"] += output
        operation_usage["total_tokens"] += prompt + cache_read + cache_write + output

    def _fallback() -> dict:
        # Deterministic backstop: pull explicit clock/full tokens, keep the
        # description text as-is.
        dts, _kind = extract_explicit_times(description, first_ts, last_ts)
        return {
            "clean_description": description,
            "issue_times": [_canonical_or_clock(dt) for dt in dts],
            "interpretation": "",
        }

    if not description:
        return _finish({"clean_description": "", "issue_times": [], "interpretation": ""})
    if llm_client is None or not llm_model:
        return _finish(_fallback())

    if first_ts and last_ts:
        rng = f"The related log spans {format_issue_time(first_ts)} to {format_issue_time(last_ts)}."
        time_instruction = ("Output each in canonical MM/DD/YYYY-HH:MM:SS.mmm. "
                            "If only a clock time is given, use the log's date.")
    elif first_ts:
        rng = f"The related log starts at {format_issue_time(first_ts)}."
        time_instruction = ("Output each in canonical MM/DD/YYYY-HH:MM:SS.mmm. "
                            "If only a clock time is given, use the log's date.")
    else:
        rng = "No log is loaded yet, so its date is unknown."
        time_instruction = ("DO NOT invent a date — output each time as HH:MM:SS only "
                            "(a real date is attached later from the actual log). Only "
                            "include a date (MM/DD/YYYY-HH:MM:SS) if the description text "
                            "itself explicitly states one.")

    system = (
        "You organize a Wi-Fi/Bluetooth support case 'Issue Description' for a "
        "log-analysis chatbot. From the text, extract:\n"
        "1. clean_description: a concise plain-English problem statement of what "
        "the user wants analyzed, WITHOUT the raw timestamps.\n"
        "2. issue_times: EVERY distinct issue time point mentioned (there may be "
        f"several). {time_instruction}\n"
        f"{rng}\n"
        "Reply with STRICT JSON only (no markdown, no prose):\n"
        '{"clean_description":"...","issue_times":["MM/DD/YYYY-HH:MM:SS.mmm", ...],'
        '"interpretation":"<one short sentence>"}\n'
        "If no time is present, issue_times = []. Keep at most 5 times, best-first."
    )
    # Deterministic pre-filter trims environment dumps / boilerplate before the
    # LLM sees it, so we send fewer tokens. Times live in the lead-in problem
    # statement, so they survive the cut; the fallback below still scans the
    # FULL text if the model returns nothing parseable.
    user = f"Issue Description:\n{prefilter_description(description)}"
    try:
        response = llm_client.chat.completions.create(
            model=llm_model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=0.1,
            max_tokens=700,
        )
        _capture_usage(getattr(response, "usage", None))
        data = parse_json_loose(response.choices[0].message.content or "")
    except Exception as e:  # noqa: BLE001 - network/LLM errors must not break the page
        print(f"[issue_time_ai] organize_issue_context LLM call failed: {e}")
        return _finish(_fallback())

    times = []
    for raw in (data.get("issue_times") or [])[:5]:
        dt, is_time_only = parse_issue_time_string(str(raw).strip())
        if dt is None:
            continue
        if is_time_only and ref:
            dt = ref.replace(hour=dt.hour, minute=dt.minute,
                             second=dt.second, microsecond=dt.microsecond)
        times.append(_canonical_or_clock(dt))
    if not times:
        # Model gave no parseable time — keep its clean description but try the
        # deterministic extractor as a backstop for the time(s).
        times = _fallback()["issue_times"]
    clean = str(data.get("clean_description") or "").strip() or description
    return _finish({
        "clean_description": clean,
        "issue_times": times,
        "interpretation": str(data.get("interpretation") or ""),
    })


# ---------------------------------------------------------------------------
# AI-time → log file picker (Run Analysis path)
# ---------------------------------------------------------------------------
# Upstream `get_auto_analysis_etl` picks the newest .etl by file number /
# address-digit sorting — a reasonable default, but blind to WHEN the issue
# actually happened. When an AI-extracted issue time is available, we can do
# better: find the .etl whose folder timestamp best matches that time.
#
# Rule (mirrors the manual "Auto-pick log by AI time" checkbox on
# download_result): among all .etl files whose folder timestamp is
# `>= issue_time`, return the one with the SMALLEST timestamp (closest after
# the issue). Returns None when nothing is at-or-after the issue time (so
# the caller keeps the upstream newest-by-number pick as the safety net) —
# the helper never picks a before-issue log on its own, since that would
# silently anchor analysis to a log that doesn't actually cover the issue.

def _parse_issue_time_for_pick(issue_time_str: str):
    """Parse the canonical issue-time string used elsewhere in the app
    (``MM/DD/YYYY HH:MM:SS.mmm``, ``MM/DD/YYYY-HH:MM:SS``, or time-only
    ``HH:MM:SS[.mmm]``). Returns one of:
      (datetime, None)       — full date+time
      (None, time-string)    — time-only (no date carried)
      (None, None)           — unparseable
    """
    if not isinstance(issue_time_str, str):
        return None, None
    s = issue_time_str.strip()
    if not s:
        return None, None

    # MM/DD/YYYY HH:MM:SS(.mmm)  or  MM/DD/YYYY-HH:MM:SS(.mmm)
    m = re.match(
        r'^(\d{1,2})/(\d{1,2})/(\d{4})[\s-](\d{1,2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?$',
        s,
    )
    if m:
        try:
            mo, d, y, hh, mm, ss, ms = m.groups()
            micro = int((ms or "0").ljust(6, "0")[:6])
            return datetime(int(y), int(mo), int(d),
                            int(hh), int(mm), int(ss), micro), None
        except ValueError:
            return None, None

    # YYYY-MM-DD HH:MM:SS(.mmm)
    m = re.match(
        r'^(\d{4})[-/](\d{1,2})[-/](\d{1,2})[\sT-](\d{1,2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?$',
        s,
    )
    if m:
        try:
            y, mo, d, hh, mm, ss, ms = m.groups()
            micro = int((ms or "0").ljust(6, "0")[:6])
            return datetime(int(y), int(mo), int(d),
                            int(hh), int(mm), int(ss), micro), None
        except ValueError:
            return None, None

    # HH:MM:SS(.mmm) — time-only (no date)
    m = re.match(r'^(\d{1,2}):(\d{2}):(\d{2})(?:\.\d{1,6})?$', s)
    if m:
        try:
            hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59:
                return None, f"{hh:02d}:{mm:02d}:{ss:02d}"
        except ValueError:
            return None, None

    return None, None


def log_anchor_date_for_etl(etl_path: str, customer_tz: str = "",
                            frame: str = "customer"):
    """Date of the decoded ``.log`` sibling's LAST timestamp, or ``None``.

    A date-less issue time (e.g. an attachment subtitle "issue happened at
    04:45 PM") needs a year/month/day to become a real datetime. The chatbot
    anchors it to the log's last timestamp (``resolve_issue_time`` →
    ``ref = last_ts or first_ts``); this is the download/filter-side
    equivalent so /download_result picks line up with the same convention.

    The candidate's ``.log`` (written by the ETL decoder as ``<etl>.log``) is
    in the decoder-host / log frame (Taiwan GMT+8). ``frame='log'`` returns
    that date as-is; ``frame='customer'`` shifts it to the customer wall clock
    via ``taiwan_to_local`` so it lines up with the customer-frame folder
    names. Returns ``None`` when there's no ``.log`` sibling or no parseable
    timestamp in it — callers then fall back to the folder-name date.

    Note: autologger folder names already encode the customer-frame capture
    date, so this usually AGREES with the folder-name date; it changes the
    outcome mainly when the folder name carries no parseable timestamp.
    """
    import os
    from utils.issue_time_utils import read_log_time_range

    if not etl_path:
        return None
    log_path = str(etl_path) + ".log"
    if not os.path.exists(log_path):
        return None
    first_ts, last_ts = read_log_time_range(log_path)
    ref = last_ts or first_ts
    if not ref:
        return None
    if frame == "customer" and customer_tz:
        from utils.timezone_utils import taiwan_to_local
        shifted = taiwan_to_local(ref, customer_tz)
        if shifted:
            ref = shifted
    return ref.date()


def pick_etl_by_ai_time(file_dicts: dict, issue_time_str: str):
    """Pick the .etl file whose folder timestamp best matches an
    AI-extracted issue time.

    Args:
        file_dicts: ``{'wifi_dict': {...}, 'ddd_dict': {...}}`` mapping zip
                    name → list of folder paths (each path contains an .etl
                    or .etl.N file). Other keys are ignored.
        issue_time_str: canonical issue-time string from
                        ``organize_issue_context`` (full datetime or
                        time-only).

    Returns:
        Selected .etl path (str), or ``None`` when no usable AI time / no
        matching log — caller falls back to the upstream newest-by-number
        pick.

    Timezone alignment:
        This helper drives the /download_result page, which compares the
        issue time against ETL FOLDER names — autologger-written in the
        customer's wall-clock frame. The caller hands us ``issue_time_str``
        already aligned to the customer frame (via
        ``align_issue_time_for_display``), so both sides are in the same
        frame and we compare folder ts as-is. (The chatbot's
        ``find_best_log`` is the log-frame counterpart — it matches against
        .log CONTENT, not folder names.)
    """
    from utils.etl_utils import extract_timestamp_from_folder
    from utils.timezone_utils import get_effective_timezone

    issue_dt, issue_clock = _parse_issue_time_for_pick(issue_time_str or "")
    if issue_dt is None and not issue_clock:
        return None

    # Collect every .etl (or .etl.N) path under wifi_dict + ddd_dict.
    etl_paths: List[str] = []
    for dict_name in ("ddd_dict", "wifi_dict"):
        d = (file_dicts or {}).get(dict_name) or {}
        for paths in d.values():
            if not paths:
                continue
            for p in paths:
                pl = str(p)
                if pl.lower().endswith(".etl") or re.search(r'\.etl\.\d+$', pl, re.IGNORECASE):
                    etl_paths.append(pl)
    if not etl_paths:
        return None

    # Pair each path with its folder timestamp (customer frame, same as the
    # customer-aligned issue_time). Folders without a parseable
    # DD-MM-YYYY_HH-MM-SS stamp drop out of the comparison (still pickable
    # via the upstream fallback if AI-time pick fails).
    timed = []
    for p in etl_paths:
        ts = extract_timestamp_from_folder(p)
        if ts:
            timed.append((p, ts))
    if not timed:
        return None

    if issue_dt is not None:
        after = [(p, ts) for p, ts in timed if ts >= issue_dt]
        if after:
            after.sort(key=lambda x: x[1])  # smallest timestamp wins
            return after[0][0]
        # Nothing at-or-after the issue → return None so the caller's
        # existing newest-by-number fallback decides. Guessing the closest
        # before-issue file would silently pick a log that DOESN'T contain
        # the issue, which is exactly what this helper is supposed to avoid.
        return None

    # Time-only path: group by date, compare time-of-day within each date.
    # On each date, find the FIRST ts at-or-after the clock (the candidate
    # for that date). Among the per-date candidates, pick the one with the
    # SMALLEST gap from its own per-date issue time — that matches the
    # upstream filter_folders_by_time semantic and is independent of which
    # date is chronologically earliest.
    try:
        ih, im_, is_ = (int(x) for x in issue_clock.split(":"))
    except Exception:
        return None
    from datetime import time as _time
    issue_clock_t = _time(ih, im_, is_)

    # Anchor the date-less clock to a candidate's decoded .log last-timestamp
    # date when one exists (frame-converted to customer, matching folder
    # names). Promotes the time-only pick to the exact dated logic above so a
    # folder on the wrong date can't win on time-of-day alone. Falls through
    # to per-folder-date grouping when no candidate carries a .log.
    customer_tz = ""
    for p, _ts in timed:
        customer_tz = get_effective_timezone(str(p))
        if customer_tz:
            break
    anchor_date = None
    for p, _ts in timed:
        anchor_date = log_anchor_date_for_etl(p, customer_tz, frame="customer")
        if anchor_date:
            break
    if anchor_date:
        issue_dt_anchored = datetime.combine(anchor_date, issue_clock_t)
        after = [(p, ts) for p, ts in timed if ts >= issue_dt_anchored]
        if after:
            after.sort(key=lambda x: x[1])  # smallest timestamp wins
            return after[0][0]
        return None

    by_date: dict = {}
    for p, ts in timed:
        by_date.setdefault(ts.date(), []).append((p, ts))
    after_t = []   # list of (path, ts, per_date_issue_dt)
    for date_, items in by_date.items():
        items.sort(key=lambda x: x[1])
        per_date_issue = datetime.combine(date_, issue_clock_t)
        for p, ts in items:
            if ts.time() >= issue_clock_t:
                after_t.append((p, ts, per_date_issue))
                break
    if after_t:
        after_t.sort(key=lambda x: (x[1] - x[2]).total_seconds())
        return after_t[0][0]
    # No date has an at-or-after match → return None, let the caller's
    # newest-by-number fallback decide. Same rationale as the dated branch.
    return None


def anchor_time_only_to_folder_date(issue_time_str: str, folder_path: str) -> str:
    """Give a date-less issue time a date taken from an ETL folder name.

    On /download_result the AI may pick an ETL from a date-less issue time
    (e.g. "04:45 PM" → "16:45:00"). For the chip to read as a real moment
    rather than a bare clock, anchor that clock to the picked ETL's folder
    capture date. Folder names and the (already customer-aligned) chip value
    are BOTH in the customer wall-clock frame here, so this is a pure date
    fill with no timezone shift.

    Returns the original string unchanged when it already carries a date, when
    the clock is unparseable, or when ``folder_path`` has no parseable
    ``DD-MM-YYYY_HH-MM-SS`` stamp.
    """
    issue_dt, issue_clock = _parse_issue_time_for_pick(issue_time_str or "")
    if issue_dt is not None or not issue_clock:
        return issue_time_str  # already dated, or unparseable
    from utils.etl_utils import extract_timestamp_from_folder
    folder_ts = extract_timestamp_from_folder(str(folder_path or ""))
    if not folder_ts:
        return issue_time_str
    try:
        hh, mm, ss = (int(x) for x in issue_clock.split(":"))
    except Exception:
        return issue_time_str
    dated = datetime.combine(
        folder_ts.date(),
        datetime.min.time().replace(hour=hh, minute=mm, second=ss),
    )
    return format_issue_time(dated)


# ---------------------------------------------------------------------------
# Frame alignment — surface the AI issue time in the customer wall-clock so
# the chip on /download_result reads the SAME way the folder names read.
# ---------------------------------------------------------------------------

def align_issue_time_for_display(
    issue_time_str: str,
    file_dicts: dict,
    llm_client: Any = None,
    llm_model: Optional[str] = None,
) -> str:
    """Return ``issue_time_str`` rewritten in the customer wall-clock frame.

    ``organize_issue_context`` extracts times from the case description
    verbatim, so when the engineer transcribed a time straight from the
    ETL-decoded log the result lives in the log frame (e.g. ``04/14/2026
    01:26:00`` = Taiwan time for a CST customer = ``04/13/2026 12:26:00``
    locally). Folder names and the customer's wall clock are in the
    customer frame, so the log-frame chip ends up confusing — it doesn't
    line up with any folder name on the page.

    Strategy (deterministic-first, LLM only as a tie-breaker):

      1. Detect the customer tz from the first folder path with a sidecar
         override / system_info.txt. Pull every folder timestamp on the
         page (each is in customer frame).
      2. For both interpretations of ``issue_time_str`` —
           a) "already customer"  → use as-is
           b) "log frame"         → shift via ``taiwan_to_local``
         compute the smallest absolute gap against any folder ts.
      3. Pick the interpretation with the smaller gap. If both gaps are
         comparable (within 60 s of each other) and an LLM client is
         provided, ask the LLM to break the tie using the description
         context that lives in the raw ``issue_time_str``.
      4. When no tz can be detected, return the input unchanged — the
         caller will display the raw string, same as before.
    """
    # Local imports — keep this function importable without circular issues.
    from utils.etl_utils import extract_timestamp_from_folder
    from utils.timezone_utils import (
        get_effective_timezone, taiwan_to_local,
    )

    if not issue_time_str or not isinstance(issue_time_str, str):
        return issue_time_str or ""

    issue_dt, issue_clock = _parse_issue_time_for_pick(issue_time_str)
    if issue_dt is None:
        # time-only or unparseable — frame conversion isn't meaningful
        # (no date carried to shift), return as-is.
        return issue_time_str

    # Walk every ETL folder once to get tz + folder ts list.
    customer_tz = ""
    folder_ts: List[datetime] = []
    for dict_name in ("wifi_dict", "ddd_dict"):
        d = (file_dicts or {}).get(dict_name) or {}
        for paths in d.values():
            for p in (paths or []):
                if not customer_tz:
                    customer_tz = get_effective_timezone(str(p))
                ts = extract_timestamp_from_folder(str(p))
                if ts:
                    folder_ts.append(ts)
            if customer_tz and folder_ts:
                break
        if customer_tz and folder_ts:
            break
    if not customer_tz or not folder_ts:
        return issue_time_str

    as_customer = issue_dt
    as_log_shifted = taiwan_to_local(issue_dt, customer_tz)
    if as_log_shifted is None:
        return issue_time_str

    def _min_gap(dt: datetime) -> float:
        return min(abs((dt - ts).total_seconds()) for ts in folder_ts)

    gap_customer = _min_gap(as_customer)
    gap_log = _min_gap(as_log_shifted)

    # PRIORITY: trust the value as customer-frame unless the evidence
    # against that is overwhelming. Autologger packages the folder within
    # minutes of the user-perceived issue moment, so a customer-frame
    # issue_time should land VERY close to a folder ts (single-digit
    # minutes). Anything farther than the threshold below is suspicious —
    # at which point we check if shifting from log frame produces a
    # tighter match, and only switch frames when it clearly does.
    #
    # 1 h chosen because:
    #   - autologger normally captures within ~5 min of the event,
    #   - 1 h leaves plenty of headroom for slow human reporting,
    #   - it's far smaller than any plausible tz offset (~13 h for CST), so
    #     a frame-mismatched value lands well outside it.
    REASONABLE_GAP_SECONDS = 60 * 60

    if gap_customer <= REASONABLE_GAP_SECONDS:
        # Customer frame is plausible — trust it, no shift needed even if
        # the log-shifted candidate happens to be marginally closer.
        return format_issue_time(as_customer)

    if gap_log <= REASONABLE_GAP_SECONDS and gap_log < gap_customer:
        # Customer-frame is implausibly far AND the log→customer shift
        # produces a reasonable match → engineer transcribed from the log,
        # convert.
        return format_issue_time(as_log_shifted)

    # Neither candidate sits inside the reasonable window — no clear
    # evidence the value is in the wrong frame. Optionally consult the LLM
    # when one is available; otherwise stay with the customer interpretation
    # rather than forcing an unjustified shift.
    if llm_client is not None and llm_model:
        try:
            sample_folders = "\n".join(f"- {ts:%m/%d/%Y %H:%M:%S}" for ts in folder_ts[:6])
            sys_msg = (
                "You decide which timezone frame a typed issue time lives in. "
                "Two candidates: A (assume customer wall-clock, as-is) or B "
                "(assume the engineer transcribed from the ETL-decoded log "
                "frame; shift to customer wall-clock). Reply with strict JSON "
                '{"pick":"A"|"B","why":"<one short sentence>"} — no prose. '
                "Default to A unless B clearly lines up with a folder ts."
            )
            user_msg = (
                f"Raw issue time:    {issue_time_str}\n"
                f"Customer tz:       {customer_tz}\n"
                f"Candidate A (customer): {format_issue_time(as_customer)}  "
                f"(gap to nearest folder: {int(gap_customer)}s)\n"
                f"Candidate B (log→customer): {format_issue_time(as_log_shifted)}  "
                f"(gap to nearest folder: {int(gap_log)}s)\n"
                f"Folder timestamps on the page (already customer frame):\n"
                f"{sample_folders}\n"
            )
            resp = llm_client.chat.completions.create(
                model=llm_model,
                messages=[{"role": "system", "content": sys_msg},
                          {"role": "user", "content": user_msg}],
                temperature=0.0,
                max_tokens=150,
            )
            choice = parse_json_loose(resp.choices[0].message.content or "")
            pick = str(choice.get("pick", "")).strip().upper()
            if pick == "B":
                return format_issue_time(as_log_shifted)
            # Any non-B answer (A, blank, unparseable) falls through to
            # the safe-default "keep as customer" path below.
        except Exception as e:
            print(f"[align_issue_time_for_display] LLM tie-break failed: {e}")

    return format_issue_time(as_customer)


def align_issue_datetime_to_customer_frame(
    issue_dt: datetime,
    folder_paths: List[str],
) -> datetime:
    """Datetime-flavoured sibling of ``align_issue_time_for_display``.

    Takes a raw ``issue_dt`` (typically pulled from an attachment subtitle in
    log frame) plus the list of folder paths it'll be compared against, and
    returns the datetime in the SAME frame as those folders (the customer
    wall clock). When no customer tz can be detected from the folder paths
    or no parseable folder timestamps are available, the input is returned
    unchanged — same behaviour as ``align_issue_time_for_display``.

    This is the deterministic-only path: callers use it on tight inner
    loops (``filter_folders_by_time``-feeding code) where the per-call cost
    of an LLM tie-break would be prohibitive and the ambiguous-gap case is
    rare enough to fall back to "smaller gap wins" silently.
    """
    if not isinstance(issue_dt, datetime) or not folder_paths:
        return issue_dt

    from utils.etl_utils import extract_timestamp_from_folder
    from utils.timezone_utils import get_effective_timezone, taiwan_to_local

    customer_tz = ""
    folder_ts: List[datetime] = []
    for p in folder_paths:
        if not customer_tz:
            customer_tz = get_effective_timezone(str(p))
        ts = extract_timestamp_from_folder(str(p))
        if ts:
            folder_ts.append(ts)
    if not customer_tz or not folder_ts:
        return issue_dt

    shifted = taiwan_to_local(issue_dt, customer_tz)
    if shifted is None:
        return issue_dt

    def _min_gap(dt: datetime) -> float:
        return min(abs((dt - ts).total_seconds()) for ts in folder_ts)

    # Mirror ``align_issue_time_for_display``: trust the value as customer
    # frame UNLESS its gap to the nearest folder is implausibly large AND
    # the log→customer shift produces a notably better match. Same 1-hour
    # threshold (autologger captures within minutes of the issue, so any
    # reasonable customer-typed value is sub-hour from a folder; the only
    # way to be off by hours is a frame mismatch).
    REASONABLE_GAP_SECONDS = 60 * 60
    gap_customer = _min_gap(issue_dt)
    if gap_customer <= REASONABLE_GAP_SECONDS:
        return issue_dt
    gap_shifted = _min_gap(shifted)
    if gap_shifted <= REASONABLE_GAP_SECONDS and gap_shifted < gap_customer:
        return shifted
    # No strong evidence of frame mismatch — stay with the input rather
    # than force an unjustified shift.
    return issue_dt


# ---------------------------------------------------------------------------
# Log-frame alignment (the chatbot direction)
# ---------------------------------------------------------------------------
# The chatbot needs ``issue_time`` in the log's own frame so PreScan's
# ±5-minute Segment-2 window catches the right lines. The same priority
# logic applies — trust the input as customer-frame first — but we go the
# OTHER way: if the input looks like customer, shift it to log frame via
# ``local_to_taiwan``; if it already looks like log frame, keep it. The
# helpers below also expose both frames at once so the UI can show a
# customer annotation alongside the canonical log-frame value.

def determine_issue_time_frames(
    issue_dt: datetime,
    folder_paths: List[str],
    log_first_ts: Optional[datetime] = None,
    log_last_ts: Optional[datetime] = None,
) -> dict:
    """Resolve a raw ``issue_dt`` into both log-frame and customer-frame
    datetimes plus the customer tz string.

    Returns a dict::

        {
            "log_frame": datetime | None,       # for PreScan / log content match
            "customer_frame": datetime | None,  # for UI annotation
            "customer_tz": str,                 # tz label (empty when unknown)
            "source_frame": "customer" | "log" | "unknown",
        }

    Two physical anchors disambiguate which frame the input is in:

      * **Folder timestamp** — the autologger names the capture folder in the
        CUSTOMER's local clock, so it anchors the *customer* frame.
      * **Log content range** (``log_first_ts`` / ``log_last_ts``) — the decoded
        ``.log`` lines are in our engineer/decode-host clock (GMT+8), so the
        range anchors the *log* frame. The issue time must fall inside the
        capture window, which makes this the strongest signal.

    Fail-safe for a mis-entered ATTACH / issue time
    -----------------------------------------------
    The ATTACH time is supposed to be the customer's packed (customer-local)
    time, but an engineer sometimes transcribes OUR side's GMT+8 clock instead
    (e.g. copied straight from a ``.log`` line). The log-range anchor catches
    this: a value that, taken as-is, lands INSIDE the GMT+8 log range — yet only
    matches the folder ts after a Taiwan→customer shift — is recognised as
    engineer/log-frame input and converted back to the customer frame for the
    UI, while the picker keeps the log-frame value for PreScan.

    Frame detection priority (trust the customer first):
      1. If the *customer* interpretation is plausible against any available
         anchor (input ≈ folder ts, or its log-frame shift sits in the log
         range), lock it in — even if the log interpretation also fits.
      2. Otherwise, if only the *log/engineer* interpretation is plausible,
         conclude the input was already in log frame and shift it to customer
         for display.
      3. When neither is plausible but the log interpretation is decisively
         tighter, flip to log; else stay with the customer assumption so the
         UI never silently moves a value the engineer typed deliberately.

    No-op fallbacks: when no customer tz, no folder ts AND no log range are
    available, both frames collapse to ``issue_dt`` and
    ``source_frame == "unknown"``.
    """
    from utils.etl_utils import extract_timestamp_from_folder
    from utils.timezone_utils import (
        get_effective_timezone, taiwan_to_local, local_to_taiwan,
    )

    blank = {
        "log_frame": issue_dt,
        "customer_frame": issue_dt,
        "customer_tz": "",
        "source_frame": "unknown",
    }
    if not isinstance(issue_dt, datetime) or not folder_paths:
        return blank

    customer_tz = ""
    folder_ts: List[datetime] = []
    for p in folder_paths:
        if not customer_tz:
            customer_tz = get_effective_timezone(str(p))
        ts = extract_timestamp_from_folder(str(p))
        if ts:
            folder_ts.append(ts)

    have_range = bool(log_first_ts and log_last_ts)
    # Need the customer tz (to convert at all) plus at least one anchor.
    if not customer_tz or (not folder_ts and not have_range):
        return blank

    REASONABLE_GAP_SECONDS = 60 * 60
    # Only flip to log frame against the customer default — when neither
    # interpretation is plausible — if log beats customer by a clear margin.
    DECISIVE_MARGIN_SECONDS = 2 * 60 * 60

    def _folder_gap(dt: datetime) -> Optional[float]:
        """Distance (s) to the nearest folder ts (customer-frame anchor)."""
        if not folder_ts:
            return None
        return min(abs((dt - ts).total_seconds()) for ts in folder_ts)

    def _range_gap(dt: datetime) -> Optional[float]:
        """Distance (s) to the log content range (log-frame anchor); 0 inside."""
        if not have_range:
            return None
        if log_first_ts <= dt <= log_last_ts:
            return 0.0
        return min(abs((dt - log_first_ts).total_seconds()),
                   abs((dt - log_last_ts).total_seconds()))

    # Interpretation A — input is customer wall-clock. customer_frame == input;
    # log_frame == local_to_taiwan(input). Customer support = folder gap of the
    # input; log support = range gap of its log-frame shift.
    cust_a = issue_dt
    log_a = local_to_taiwan(issue_dt, customer_tz) or issue_dt
    gaps_a = [g for g in (_folder_gap(cust_a), _range_gap(log_a)) if g is not None]

    # Interpretation B — input was already in log/engineer frame. log_frame ==
    # input; customer_frame == taiwan_to_local(input). Log support = range gap
    # of the input; customer support = folder gap of its customer shift.
    log_b = issue_dt
    cust_b = taiwan_to_local(issue_dt, customer_tz) or issue_dt
    gaps_b = [g for g in (_range_gap(log_b), _folder_gap(cust_b)) if g is not None]

    # Each anchor is independent evidence, so the BEST-fitting anchor scores the
    # interpretation (a large folder gap from a long capture shouldn't veto a
    # value that sits squarely inside the log range, and vice versa).
    score_a = min(gaps_a) if gaps_a else float("inf")
    score_b = min(gaps_b) if gaps_b else float("inf")
    plausible_a = score_a <= REASONABLE_GAP_SECONDS
    plausible_b = score_b <= REASONABLE_GAP_SECONDS

    customer_result = {
        "log_frame": log_a,
        "customer_frame": cust_a,
        "customer_tz": customer_tz,
        "source_frame": "customer",
    }
    log_result = {
        "log_frame": log_b,
        "customer_frame": cust_b,
        "customer_tz": customer_tz,
        "source_frame": "log",
    }

    # 1) Trust the customer interpretation whenever it's plausible.
    if plausible_a:
        return customer_result
    # 2) Only the engineer/log interpretation fits → the input was log-frame.
    if plausible_b:
        return log_result
    # 3) Neither is plausible: flip to log only when it's decisively tighter.
    if score_b + DECISIVE_MARGIN_SECONDS < score_a:
        return log_result
    # 4) No clear signal — stay with the customer assumption.
    return customer_result

