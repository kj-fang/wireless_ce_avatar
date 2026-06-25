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

import json
import re
from datetime import datetime, timedelta
from typing import Any, List, Optional, Tuple

from utils.issue_time_utils import parse_issue_time_string, format_issue_time


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
    r')\b',
    re.IGNORECASE,
)


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


def build_log_digest(
    log_lines: List[str],
    head: int = 50,
    tail: int = 50,
    max_keyword: int = 60,
    max_chars: int = 12000,
) -> str:
    """Rough browse of the log for the LLM: head + tail + symptom-keyword hits,
    deduped and kept in original order, capped to keep the prompt small.

    Keyword hits are SAMPLED EVENLY across the whole log rather than just
    the first N matches. Without this, a long log whose head is dominated
    by init/lifecycle events (e.g. a multi-thousand-line DDD trace) would
    burn the entire ``max_keyword`` budget on the first few hundred lines
    and never surface the actual issue-relevant events buried in the
    middle/tail. Capping the collected-indices list keeps memory bounded
    on very large logs while preserving the sampling property.
    """
    lines = log_lines or []
    n = len(lines)
    if n == 0:
        return ""
    picked = set(range(min(head, n)))
    picked.update(range(max(0, n - tail), n))
    # First collect every keyword hit (capped to keep memory bounded), then
    # sample evenly so the digest spans the entire log, not just the head.
    _max_collect = 5000
    hit_indices: List[int] = []
    for i, line in enumerate(lines):
        if _LOG_DIGEST_KEYWORDS.search(line):
            hit_indices.append(i)
            if len(hit_indices) >= _max_collect:
                break
    if len(hit_indices) > max_keyword:
        step = len(hit_indices) / max_keyword
        sampled = [hit_indices[int(j * step)] for j in range(max_keyword)]
    else:
        sampled = hit_indices
    for i in sampled:
        picked.add(i)
    digest = "\n".join(str(lines[i]).rstrip("\n") for i in sorted(picked))
    if len(digest) > max_chars:
        digest = digest[:max_chars] + "\n…(truncated)"
    return digest


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
    system = (
        "You determine the 'issue time' that anchors log analysis. Logs are "
        "typically Wi-Fi or Bluetooth, but the user's problem description "
        "(which may be vague, non-English, or use a wrong time format) is "
        "the source of truth for the specific scenario. Read it plus a "
        "rough sample of the log, then infer the most likely issue time(s) "
        "— use the description to decide which lines in the log are "
        "relevant, and pick the timestamp that sits next to one of those "
        "lines. "
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
    user = (
        "User description:\n"
        f"{text or fallback_text}\n\n"
        f"=== Rough log sample ===\n{log_digest or '(no log loaded)'}"
    )
    response = llm_client.chat.completions.create(
        model=llm_model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        temperature=0.1,
        max_tokens=900,
    )
    return parse_json_loose(response.choices[0].message.content or "")


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
    """
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
    if explicit:
        if no_date:
            reason = "Clock time taken from your message (log has no date)."
        elif kind == "time_only":
            reason = "Clock time taken from your message; date aligned to the log."
        else:
            reason = "Explicit timestamp taken directly from your message."
        suggestions = [make_suggestion(dt, "high", reason, "user",
                                       log_has_date=not no_date)
                       for dt in explicit]
        return {
            "success": True,
            "user_explicit": True,
            "interpretation": "You provided explicit time(s) — using them as-is.",
            "needs_user_input": False,
            "suggestions": suggestions,
            "message": f"Found {len(suggestions)} explicit time(s) in your text.",
        }

    # 2) LLM inference for vague / malformed / missing times.
    if llm_client is None or not llm_model:
        return {
            "success": False,
            "user_explicit": False,
            "interpretation": "",
            "needs_user_input": True,
            "suggestions": [],
            "error": "LLM is not configured on this server.",
            "message": "LLM is not configured on this server.",
        }

    log_digest = build_log_digest(log_lines or [])
    # Wrap the LLM call so any network / parse / API-error failure degrades
    # gracefully into "no suggestions" instead of a 500 — the log-first
    # fallback below then still gives the user a usable anchor. Without
    # this, a transient LLM hiccup makes the AI button look broken.
    try:
        llm = llm_suggest(
            llm_client, llm_model, text, log_digest, first_ts, last_ts,
            log_has_date=log_has_date,
            log_frame_first_ts=log_frame_first_ts,
            log_frame_last_ts=log_frame_last_ts,
            tz_label=tz_label,
        )
    except Exception as _e:
        print(f"[issue_time_ai] llm_suggest failed, deferring to fallback: {_e}")
        llm = {"interpretation": "", "needs_user_input": True, "suggestions": []}

    ref = last_ts or first_ts
    suggestions = []
    for s in (llm.get("suggestions") or [])[:5]:
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
    if not suggestions and log_lines:
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

    msg = (f"AI suggested {len(suggestions)} time(s)."
           if suggestions else
           "AI couldn't pin down a specific time — please review or fill it in.")
    return {
        "success": True,
        "user_explicit": False,
        "interpretation": str(llm.get("interpretation", "")),
        # Even with a low-confidence fallback we still want the user to
        # double-check, so keep needs_user_input True unless the LLM
        # itself confidently said otherwise.
        "needs_user_input": bool(llm.get("needs_user_input", not suggestions)),
        "suggestions": suggestions,
        "message": msg,
    }


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
) -> dict:
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
    """
    description = (description or "").strip()
    ref = last_ts or first_ts

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
        return {"clean_description": "", "issue_times": [], "interpretation": ""}
    if llm_client is None or not llm_model:
        return _fallback()

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
        data = parse_json_loose(response.choices[0].message.content or "")
    except Exception as e:  # noqa: BLE001 - network/LLM errors must not break the page
        print(f"[issue_time_ai] organize_issue_context LLM call failed: {e}")
        return _fallback()

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
    return {
        "clean_description": clean,
        "issue_times": times,
        "interpretation": str(data.get("interpretation") or ""),
    }


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


def align_issue_datetime_to_log_frame(
    issue_dt: datetime,
    folder_paths: List[str],
) -> datetime:
    """Return ``issue_dt`` aligned to the log's own frame (chatbot path).

    Thin wrapper around :func:`determine_issue_time_frames` for callers
    that only need the log-frame value. When detection isn't possible the
    input is returned unchanged.
    """
    frames = determine_issue_time_frames(issue_dt, folder_paths)
    return frames["log_frame"] or issue_dt
