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
from datetime import datetime
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
) -> Tuple[List[datetime], Optional[str]]:
    """User-first deterministic pass.

    Returns ``(datetimes, kind)`` where kind is ``'full'`` | ``'time_only'`` |
    ``None``. 'full' tokens carry their own date; 'time_only' clock tokens
    borrow the log's date so they land inside the actual capture range.
    """
    ref = last_ts or first_ts
    full: List[datetime] = []
    seen = set()
    for m in _EXPLICIT_FULL_DT_RE.finditer(text):
        tok = m.group(0).strip()
        dt, is_time_only = parse_issue_time_string(tok)
        if dt and not is_time_only and tok not in seen:
            seen.add(tok)
            full.append(dt)
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
) -> dict:
    """Ask the LLM to infer issue time(s) from the description + log sample.

    Returns the parsed JSON dict (see ``parse_json_loose`` for the fallback).

    ``log_has_date`` (when known) gates only the OUTPUT FORMAT:
      - True / None  → canonical full ``MM/DD/YYYY-HH:MM:SS.mmm``.
      - False        → time-only ``HH:MM:SS.mmm`` (no fabricated date).
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
        f"{rng} {format_rule}\n"
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
    explicit, kind = extract_explicit_times(text, first_ts, last_ts)
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
        llm = llm_suggest(llm_client, llm_model, text, log_digest, first_ts, last_ts,
                          log_has_date=log_has_date)
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

    # Fallback when the LLM returned nothing: at least offer the FIRST
    # parseable timestamp in the log as a low-confidence anchor. Useful any
    # time the relevant event sits at the start of the log (or the LLM is
    # over-conservative about pinning a time). The user still sees "low"
    # confidence + a reason explaining it's a guess, and needs_user_input
    # stays True so they're prompted to confirm or edit.
    if not suggestions and log_lines:
        fallback_dt = _find_first_log_timestamp(log_lines, no_date_log=no_date)
        if fallback_dt is not None:
            suggestions.append(make_suggestion(
                fallback_dt, "low",
                ("No exact anchor matched the description; using the log's "
                 "first event time as a starting point. Edit if the issue "
                 "happens later in the log."),
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


def _find_first_log_timestamp(
    log_lines: List[str], no_date_log: bool, scan_limit: int = 200
) -> Optional[datetime]:
    """Return the FIRST parseable timestamp in the head of the log, or None.

    Used as a low-confidence fallback when the LLM can't pin a time — the
    first event time is a reasonable starting anchor regardless of the
    log's domain. Scans only the first ``scan_limit`` lines, since the
    "first event time" sits at the head of the file.

    For dated logs returns a real ``datetime``. For time-only logs returns a
    placeholder-dated datetime (only H/M/S matter — caller serialises via
    ``make_suggestion(log_has_date=False)`` which drops the date).
    """
    rx = _FALLBACK_TIME_ONLY_RE if no_date_log else _FALLBACK_DATED_RE
    for line in (log_lines or [])[:scan_limit]:
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
                         last_ts: Optional[datetime] = None) -> list:
    """Give clock-only / undated issue-time strings the loaded log's date so they
    land inside its range. Full-date strings are left untouched. Deterministic —
    no LLM — so a result organized earlier (e.g. on the select-attachments page,
    before any log existed) can be reused and dated once a log is loaded."""
    ref = last_ts or first_ts
    out = []
    for s in (times or []):
        s = str(s).strip()
        if not s:
            continue
        dt, is_time_only = parse_issue_time_string(s)
        if dt is None:
            continue
        undated = is_time_only or dt.year < _MIN_PLAUSIBLE_YEAR
        if undated and ref:
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
    """
    # Local import — etl_utils is upstream and may change shape; keep it
    # out of this module's top-level imports.
    from utils.etl_utils import extract_timestamp_from_folder

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

    # Pair each path with its folder timestamp. Folders without a parseable
    # DD-MM-YYYY_HH-MM-SS stamp simply drop out of the comparison (they're
    # still pickable via the upstream fallback if AI-time pick fails).
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
