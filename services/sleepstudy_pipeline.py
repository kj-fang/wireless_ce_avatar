"""
Sleep Study -> driver log pipeline
==================================
A Sleep Study report says WHEN the platform failed to reach low power and
WHICH device kept it awake. A driver log says WHY. This module joins the two:

  1. Read the report once and pick the worst Modern Standby drain sessions
     (software DRIPS below a threshold, a RED top blocker), at most
     MAX_TIME_POINTS of them and at most PER_DEVICE_LIMIT per device family.
  2. Turn each session into a time window: the session's own start..end,
     padded, and capped so a long session cannot pull in hours of log.
  3. Suggest, per time point, the driver log under the report's folder that
     covers its window; the user confirms or changes it (plan()). A window
     its log does not cover is reported as such and never reaches the LLM --
     an agent handed the wrong part of a log still writes a confident answer.
  4. Cut the covered lines into a small excerpt with full dates, and run the
     same agent the Wi-Fi or BT chatbot uses on it, primed with the session as
     the issue description and the window as the issue time.

Audio and "other" blockers are classified and listed, but no agent here reads
their logs, so they stop after step 1.

Nothing here imports Flask. The NW chatbot route drives it today; an MCP
server can drive the same generator later without touching the logic.
"""

from __future__ import annotations

import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator, Optional

from services import sleepstudy_analyzer as ssa


# ── Selection policy ───────────────────────────────────────────────────────────
SW_DRIP_THRESHOLD = 50.0      # percent: software DRIPS below this is a bad session
MAX_TIME_POINTS   = 10        # at most this many sessions are analysed per run
PER_DEVICE_LIMIT  = 5         # and at most this many per device family

# ── Window policy ──────────────────────────────────────────────────────────────
# Entering and leaving standby are where a driver most often misbehaves, so
# the window reaches a little past both edges of the session.
WINDOW_PAD = timedelta(minutes=2)
# A multi-hour session would hand the agent hours of log. Past this length
# the window keeps the session's opening, where the entry flow is.
WINDOW_CAP = timedelta(minutes=60)

# Device families an agent in this tool can read a driver log for.
ANALYZABLE_DEVICES = (ssa.DEVICE_WIFI, ssa.DEVICE_BT)

DEVICE_LABELS = {
    ssa.DEVICE_WIFI:  "Wi-Fi",
    ssa.DEVICE_BT:    "Bluetooth",
    ssa.DEVICE_AUDIO: "Audio",
    ssa.DEVICE_OTHER: "Other",
}

# ── Point status ───────────────────────────────────────────────────────────────
STATUS_ANALYZED     = "analyzed"
STATUS_NOT_COVERED  = "not_covered"     # the driver log does not reach this window
STATUS_NO_LOG       = "no_driver_log"   # no driver log of this device was given
STATUS_UNSUPPORTED  = "unsupported"     # no agent reads this device's log
STATUS_NO_LINES     = "no_lines"        # covered on paper, but nothing was logged
STATUS_AGENT_FAILED = "agent_failed"
STATUS_DRY_RUN      = "dry_run"         # covered, excerpt cut, agent not asked


# ══════════════════════════════════════════════════════════════════════════════
#  DRIVER LOG DISCOVERY
# ══════════════════════════════════════════════════════════════════════════════

# WRT names each rolling Wi-Fi log after the moment it was opened:
#   WiFiLog--HH-MM-SS-mmm--DD-MM-YYYY--00001.LOG
_WIFILOG_NAME = re.compile(
    r"^WiFiLog--(\d{2})-(\d{2})-(\d{2})-(\d{3})--(\d{2})-(\d{2})-(\d{4})--\d+\.LOG$",
    re.IGNORECASE,
)
# A WiFiLog line: sequence number, HH:MM:SS:mmm, message -- tab separated.
_WIFILOG_TIME = re.compile(r"^(\d{2}):(\d{2}):(\d{2}):(\d{3})$")
# The decoded Wi-Fi ETL shape, which the Wi-Fi agent reads natively and which
# every excerpt is written in.
_WIFI_ETL_TS = re.compile(r"^﻿?(\d{2}/\d{2}/\d{4}-\d{2}:\d{2}:\d{2}\.\d{3})")
_WIFI_ETL_FMT = "%m/%d/%Y-%H:%M:%S.%f"

# A clock that runs backwards by more than this has crossed midnight; anything
# smaller is a thread printing out of order.
_MIDNIGHT_JUMP = timedelta(hours=12)


@dataclass
class LogSegment:
    """One driver log file and the wall-clock span it covers."""
    path: str
    device: str
    kind: str                     # "wrt_wifilog" | "wifi_etl"
    start: datetime
    end: Optional[datetime]

    def overlaps(self, lo: datetime, hi: datetime) -> bool:
        return self.start < hi and (self.end is None or self.end > lo)


def _wifilog_start(name: str) -> Optional[datetime]:
    m = _WIFILOG_NAME.match(name)
    if not m:
        return None
    hh, mi, ss, ms, dd, mo, yy = (int(g) for g in m.groups())
    try:
        return datetime(yy, mo, dd, hh, mi, ss, ms * 1000)
    except ValueError:
        return None


def _wifilog_clock(text: str) -> Optional[tuple[int, int, int, int]]:
    m = _WIFILOG_TIME.match(text)
    if not m:
        return None
    hh, mi, ss, ms = (int(g) for g in m.groups())
    if hh > 23 or mi > 59 or ss > 59:
        return None
    return hh, mi, ss, ms


def _tail_lines(path: str, nbytes: int = 65536) -> list[str]:
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - nbytes))
        return fh.read().decode("utf-8", errors="replace").splitlines()


def _wifilog_last_time(path: str, start: datetime) -> Optional[datetime]:
    """
    The last timestamp of a WiFiLog, placed on the calendar.

    The file names only the day it was opened. A rolling file is capped in
    size and spans hours, not days, so a clock that ends earlier than it
    started has crossed one midnight.
    """
    for line in reversed(_tail_lines(path)):
        parts = line.split("\t", 2)
        clock = _wifilog_clock(parts[1]) if len(parts) == 3 else None
        if clock is None:
            continue
        hh, mi, ss, ms = clock
        last = datetime.combine(start.date(), datetime.min.time()).replace(
            hour=hh, minute=mi, second=ss, microsecond=ms * 1000)
        if last < start - timedelta(minutes=5):
            last += timedelta(days=1)
        return last
    return None


def _wifi_etl_range(path: str) -> tuple[Optional[datetime], Optional[datetime]]:
    first = last = None
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = _WIFI_ETL_TS.match(line)
            if m:
                first = datetime.strptime(m.group(1), _WIFI_ETL_FMT)
                break
    for line in reversed(_tail_lines(path)):
        m = _WIFI_ETL_TS.match(line)
        if m:
            last = datetime.strptime(m.group(1), _WIFI_ETL_FMT)
            break
    return first, last


def discover_driver_logs(path: str) -> tuple[dict[str, list[LogSegment]], list[str]]:
    """
    Index the driver logs under `path` by device family.

    `path` is a WRT capture folder or a single log file. Recognised today:
      - WRT rolling Wi-Fi logs (WiFiLog--*.LOG), anywhere under the folder;
        a single WiFiLog file means its whole folder
      - a decoded Wi-Fi ETL log (MM/DD/YYYY-HH:MM:SS.mmm lines)

    Returns ({device: [segments, oldest first]}, warnings).
    """
    warnings: list[str] = []
    found: dict[str, list[LogSegment]] = {}
    if not path or not os.path.exists(path):
        return found, [f"Driver log not found: {path}"]

    # One WiFiLog picked in a file dialog stands for the capture it came from:
    # a session usually falls in one of its siblings, not in that file.
    if os.path.isfile(path) and _wifilog_start(os.path.basename(path)) is not None:
        path = os.path.dirname(path)

    if os.path.isdir(path):
        files = [str(p) for p in Path(path).rglob("*") if p.is_file()]
    else:
        files = [path]

    wifilogs = []
    for f in files:
        start = _wifilog_start(os.path.basename(f))
        if start is not None:
            wifilogs.append((start, f))
            continue
        if not os.path.isdir(path):
            # A single file that is not a WiFiLog must be a decoded ETL log.
            first, last = _wifi_etl_range(f)
            if first is None:
                warnings.append(
                    f"{os.path.basename(f)}: no timestamp this tool can read; "
                    "expected a WRT WiFiLog or a decoded Wi-Fi ETL log.")
                continue
            found.setdefault(ssa.DEVICE_WIFI, []).append(
                LogSegment(f, ssa.DEVICE_WIFI, "wifi_etl", first, last))

    # Rolling files hand over to each other, so each one runs until the next
    # one opens. Only the newest has to be read to know where it ends.
    wifilogs.sort()
    for i, (start, f) in enumerate(wifilogs):
        if i + 1 < len(wifilogs):
            end = wifilogs[i + 1][0]
        else:
            end = _wifilog_last_time(f, start)
        found.setdefault(ssa.DEVICE_WIFI, []).append(
            LogSegment(f, ssa.DEVICE_WIFI, "wrt_wifilog", start, end))

    if os.path.isdir(path) and not found:
        warnings.append(f"No WiFiLog--*.LOG files under {path}.")
    return found, warnings


def coverage_of(segments: list[LogSegment], lo: datetime, hi: datetime) -> tuple[str, list[LogSegment]]:
    """'full', 'partial' or 'none', and the segments that overlap [lo, hi)."""
    hits = [s for s in segments if s.overlaps(lo, hi)]
    if not hits:
        return "none", []
    first = min(s.start for s in hits)
    ends = [s.end for s in hits]
    last = None if any(e is None for e in ends) else max(ends)
    # A log's first and last lines rarely land on the window's exact second;
    # only a shortfall longer than this is worth a "partial" warning.
    slack = timedelta(minutes=1)
    full = first <= lo + slack and (last is None or last >= hi - slack)
    return ("full" if full else "partial"), hits


# ══════════════════════════════════════════════════════════════════════════════
#  EXCERPT
# ══════════════════════════════════════════════════════════════════════════════

def _iter_wifilog(seg: LogSegment) -> Iterator[tuple[Optional[datetime], str]]:
    """
    Yield (timestamp, message) for each line of a WiFiLog, with the date
    carried across midnight. Continuation lines yield (None, text).
    """
    day = seg.start.date()
    prev: Optional[datetime] = None
    with open(seg.path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\r\n")
            parts = line.split("\t", 2)
            clock = _wifilog_clock(parts[1]) if len(parts) == 3 else None
            if clock is None:
                yield None, line
                continue
            hh, mi, ss, ms = clock
            dt = datetime(day.year, day.month, day.day, hh, mi, ss, ms * 1000)
            if prev is None and dt > seg.start + _MIDNIGHT_JUMP:
                # The first line was written before the midnight the file
                # name already sits past.
                dt -= timedelta(days=1)
                day = dt.date()
            elif prev is not None and dt < prev - _MIDNIGHT_JUMP:
                dt += timedelta(days=1)
                day = dt.date()
            prev = dt
            yield dt, parts[2]


def _iter_wifi_etl(seg: LogSegment) -> Iterator[tuple[Optional[datetime], str]]:
    with open(seg.path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\r\n")
            m = _WIFI_ETL_TS.match(line)
            if not m:
                yield None, line
                continue
            yield datetime.strptime(m.group(1), _WIFI_ETL_FMT), line[m.end():].lstrip()


def write_excerpt(segments: list[LogSegment], lo: datetime, hi: datetime, out_path: str) -> dict:
    """
    Copy every line logged in [lo, hi] into `out_path`, written in the decoded
    Wi-Fi ETL shape (MM/DD/YYYY-HH:MM:SS.mmm message) so the agent reads it as
    a dated log and never has to guess which day a clock belongs to.
    """
    kept = 0
    first = last = None
    sources = []
    with open(out_path, "w", encoding="utf-8", newline="\n") as out:
        for seg in sorted(segments, key=lambda s: s.start):
            reader = _iter_wifilog if seg.kind == "wrt_wifilog" else _iter_wifi_etl
            in_window = False
            seg_kept = 0
            for dt, msg in reader(seg):
                if dt is None:
                    # A wrapped line belongs to whatever came before it.
                    if in_window and msg:
                        out.write(msg + "\n")
                    continue
                if dt > hi:
                    break
                in_window = dt >= lo
                if not in_window:
                    continue
                out.write(f"{dt.strftime('%m/%d/%Y-%H:%M:%S')}.{dt.microsecond // 1000:03d} {msg}\n")
                kept += 1
                seg_kept += 1
                first = first or dt
                last = dt
            if seg_kept:
                sources.append(seg.path)
    return {"lines": kept, "first": first, "last": last, "sources": sources}


# ══════════════════════════════════════════════════════════════════════════════
#  TIME POINTS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class TimePoint:
    index: int
    session_id: object
    device: str
    type_label: str
    start: Optional[datetime]
    end: Optional[datetime]
    duration: str
    sw_drip_pct: Optional[float]
    hw_drip_pct: Optional[float]
    blockers: list
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    window_truncated: bool = False
    status: str = ""
    coverage: str = ""
    message: str = ""
    sleep_summary: str = ""
    issue_description: str = ""
    driver_log_path: str = ""     # suggested by plan(), confirmed by the user
    excerpt_path: str = ""
    excerpt_lines: int = 0
    driver_log_sources: list = field(default_factory=list)
    agent_result_type: str = ""
    agent_report: str = ""
    agent_usage: dict = field(default_factory=dict)

    @property
    def issue_time(self) -> Optional[datetime]:
        if self.window_start is None or self.window_end is None:
            return self.start
        return self.window_start + (self.window_end - self.window_start) / 2

    @property
    def half_window_minutes(self) -> int:
        if self.window_start is None or self.window_end is None:
            return 5
        return max(1, math.ceil((self.window_end - self.window_start).total_seconds() / 120))

    def to_dict(self) -> dict:
        d = asdict(self)
        for key in ("start", "end", "window_start", "window_end"):
            d[key] = _fmt(d[key])
        d["issue_time"] = _fmt(self.issue_time)
        d["device_label"] = DEVICE_LABELS.get(self.device, self.device)
        return d


def _fmt(dt: Optional[datetime]) -> str:
    return dt.strftime("%m/%d/%Y %H:%M:%S") if dt else ""


def _basename(path: str) -> str:
    return os.path.basename(path.rstrip("\\/")) or path


def _pct(v: Optional[float]) -> str:
    return f"{v:.1f}%" if v is not None else "N/A"


def select_time_points(summaries: list, max_points: int = MAX_TIME_POINTS,
                       per_device: int = PER_DEVICE_LIMIT) -> list[TimePoint]:
    """
    Pick the sessions worth a driver-log look: worst software DRIPS first,
    no device family taking more than `per_device` of the `max_points` slots.
    """
    def sw_rank(s):
        return s["sw_drip_pct"] if s["sw_drip_pct"] is not None else float("inf")

    taken: dict[str, int] = {}
    points: list[TimePoint] = []
    for s in sorted(summaries, key=sw_rank):
        if len(points) >= max_points:
            break
        if taken.get(s["device"], 0) >= per_device:
            continue
        taken[s["device"]] = taken.get(s["device"], 0) + 1
        start = ssa.parse_local_timestamp(s.get("start") or "")
        end = ssa.parse_local_timestamp(s.get("end") or "")
        p = TimePoint(
            index=len(points) + 1,
            session_id=s["session_id"],
            device=s["device"],
            type_label=s["type_label"],
            start=start,
            end=end,
            duration=s["duration"],
            sw_drip_pct=s["sw_drip_pct"],
            hw_drip_pct=s["hw_drip_pct"],
            blockers=s["blockers"],
        )
        if start is not None:
            lo = start - WINDOW_PAD
            hi = (end or start) + WINDOW_PAD
            if hi - lo > WINDOW_CAP:
                hi = lo + WINDOW_CAP
                p.window_truncated = True
            p.window_start, p.window_end = lo, hi
        points.append(p)
    return points


def _sleep_summary(point: TimePoint, raw_session: Optional[dict]) -> str:
    """The Sleep Study evidence for one session, as text an agent can read."""
    if raw_session is not None and point.device == ssa.DEVICE_WIFI:
        return ssa.build_session_summary(raw_session, ssa.extract_wifi_content(raw_session))
    lines = [
        f"Session {point.session_id} ({point.type_label}) "
        f"{_fmt(point.start)} -> {_fmt(point.end)} ({point.duration})",
        f"SW DRIPS {_pct(point.sw_drip_pct)}, HW DRIPS {_pct(point.hw_drip_pct)}",
        "RED top blockers:",
    ]
    for b in point.blockers:
        lines.append(f"  - {b['name']} [{DEVICE_LABELS.get(b['device'], b['device'])}] "
                     f"active {b['active_time_pct']}%")
    return "\n".join(lines)


def _issue_description(point: TimePoint, threshold: float) -> str:
    top = point.blockers[0] if point.blockers else {}
    label = DEVICE_LABELS.get(point.device, point.device)
    return (
        f"Modern Standby battery drain found by Sleep Study. Session {point.session_id} "
        f"({point.type_label}) ran {_fmt(point.start)} to {_fmt(point.end)} ({point.duration}) "
        f"on battery with software DRIPS {_pct(point.sw_drip_pct)} "
        f"(hardware {_pct(point.hw_drip_pct)}), below the {threshold:.0f}% bar. "
        f"The RED top blocker was {top.get('name', 'unknown')} "
        f"({top.get('active_time_pct', '?')}% active). "
        f"Find what the {label} driver was doing in this window that kept the "
        f"platform out of its low-power state: suspend/resume flow, D3 entry, "
        f"wake reasons, traffic or timers that held it active."
    )


# ══════════════════════════════════════════════════════════════════════════════
#  AGENT
# ══════════════════════════════════════════════════════════════════════════════

def default_agent_factory(device: str):
    """
    A fresh agent of the device's chatbot, sharing the boot-time agent's
    client, model, skills and ACE runner -- the same agent a user gets on the
    Wi-Fi or BT page, so its prompts and skills apply here unchanged.
    """
    from configs.global_configs import app_config

    llm = app_config.llm_helper
    if llm is None or getattr(llm, "client", None) is None:
        raise RuntimeError("No LLM client is configured; set an API key first.")

    if device == ssa.DEVICE_BT:
        from services.chatbot.engine.bluetooth import BtLogAgentSystem as agent_cls
        base = getattr(app_config, "bt_chatbot_agent", None)
    else:
        from services.chatbot.engine.system import WifiLogAgentSystem as agent_cls
        base = getattr(app_config, "log_chatbot_agent", None)

    skills = getattr(base, "skills", None) or getattr(llm, "skills", None) or {}
    agent = agent_cls(client=llm.client, model=getattr(llm, "model", "gpt-4.1"), skills=skills)
    ace_runner = getattr(base, "ace_runner", None)
    if ace_runner is not None:
        agent.attach_ace(ace_runner)
    return agent


def _report_text(result) -> tuple[str, str]:
    """(result type, readable text) from an agent.chat() result."""
    if not isinstance(result, dict):
        return "", str(result or "")
    kind = str(result.get("type") or "")
    data = result.get("data")
    if kind in ("report", "partial_report") and isinstance(data, dict):
        parts = [str(data.get("root_cause_summary") or ""), str(data.get("markdown_summary") or "")]
        return kind, "\n\n".join(p for p in parts if p)
    if isinstance(data, str):
        return kind, data
    return kind, "" if data is None else str(data)


def _run_agent(point: TimePoint, agent, max_steps: int, on_step: Callable[[str], None]) -> None:
    agent.current_log_path = point.excerpt_path
    agent.prime_with_context(
        subject=f"Sleep Study session {point.session_id}: low DRIPS on battery",
        description=point.issue_description,
        attachment_time=_fmt(point.issue_time),
    )
    # prime_with_context assumes a log decoded in another timezone and may
    # shift the time. The excerpt was cut in the report's own clock, so the
    # window is set back exactly, and wide enough to see the whole session.
    agent.issue_time = point.issue_time
    agent.issue_time_customer = None
    agent.issue_time_tz = ""
    agent.issue_time_window_minutes = point.half_window_minutes

    def _step(step):
        if isinstance(step, dict) and step.get("content"):
            on_step(str(step["content"]))

    message = (
        f"{point.issue_description}\n\nSleep Study evidence:\n```\n"
        f"{point.sleep_summary[:4000]}\n```\n"
        f"Issue time: around {_fmt(point.issue_time)}"
    )
    result = agent.chat(message, max_steps=max_steps, step_callback=_step)
    point.agent_result_type, point.agent_report = _report_text(result)
    usage = getattr(agent, "last_turn_usage", None)
    if isinstance(usage, dict):
        point.agent_usage = {k: v for k, v in usage.items() if isinstance(v, (int, float))}


# ══════════════════════════════════════════════════════════════════════════════
#  PIPELINE
# ══════════════════════════════════════════════════════════════════════════════
#
# Two steps, so a person can check which driver log goes with which session
# before any tokens are spent:
#
#   plan()          reads the report, picks the time points and suggests, for
#                   each one, the driver log under the report's folder that
#                   covers it. No LLM.
#   run_pipeline()  takes a driver log per time point (as suggested, edited,
#                   or one path for all of them) and analyses each one.

def _select(html_path: str, sw_threshold: float, max_points: int, per_device: int):
    payload = ssa.load_sleepstudy(html_path)
    collected = ssa.collect_sessions_from_payload(
        payload, source=html_path, devices=ssa.DEVICE_ORDER,
        drip_threshold=sw_threshold, drip_metric=ssa.DRIP_METRIC_SW,
    )
    raw_by_id = {s.get("SessionId"): s for s in payload.get("ScenarioInstances") or []}
    points = select_time_points(collected["sessions"], max_points, per_device)
    for p in points:
        p.sleep_summary = _sleep_summary(p, raw_by_id.get(p.session_id))
        p.issue_description = _issue_description(p, sw_threshold)
    return collected, points


def _criteria(collected: dict, max_points: int, per_device: int) -> dict:
    return {
        **collected["criteria"],
        "max_points": max_points,
        "per_device_limit": per_device,
        "window_pad_minutes": WINDOW_PAD.total_seconds() / 60,
        "window_cap_minutes": WINDOW_CAP.total_seconds() / 60,
    }


def plan(
    html_path: str,
    *,
    search_root: Optional[str] = None,
    sw_threshold: float = SW_DRIP_THRESHOLD,
    max_points: int = MAX_TIME_POINTS,
    per_device: int = PER_DEVICE_LIMIT,
) -> dict:
    """
    Pick the time points and suggest a driver log for each.

    Driver logs are looked for under `search_root` (default: the report's
    folder, where a WRT capture usually sits next to it). A point's
    suggestion is the log file covering its window, or "" when none does --
    the caller then asks the user for one. Raises on an unreadable report.
    """
    collected, points = _select(html_path, sw_threshold, max_points, per_device)
    search_root = search_root or os.path.dirname(os.path.abspath(html_path))
    log_index, warnings = discover_driver_logs(search_root)
    for p in points:
        if p.device not in ANALYZABLE_DEVICES:
            p.status = STATUS_UNSUPPORTED
            p.message = (f"{DEVICE_LABELS.get(p.device, p.device)} blocker: classified "
                         "only; no agent here reads that driver log.")
        elif p.window_start is None:
            p.status = STATUS_NOT_COVERED
            p.message = "The report gives no readable start time for this session."
        else:
            p.coverage, hits = coverage_of(log_index.get(p.device, []),
                                           p.window_start, p.window_end)
            if hits:
                p.driver_log_path = hits[0].path
            else:
                p.status = STATUS_NO_LOG
                p.message = (f"No {DEVICE_LABELS[p.device]} driver log under the report's "
                             "folder covers this window; choose one.")
    return {
        "source": html_path,
        "search_root": search_root,
        "total_sessions": collected["total_sessions"],
        "qualifying_count": collected["qualifying_count"],
        "criteria": _criteria(collected, max_points, per_device),
        "devices": collected["devices"],
        "warnings": collected["warnings"] + warnings,
        "points": [p.to_dict() for p in points],
    }


def run_pipeline(
    html_path: str,
    driver_logs,
    *,
    agent_factory: Optional[Callable[[str], object]] = default_agent_factory,
    out_dir: Optional[str] = None,
    sw_threshold: float = SW_DRIP_THRESHOLD,
    max_points: int = MAX_TIME_POINTS,
    per_device: int = PER_DEVICE_LIMIT,
    max_steps: int = 6,
) -> Iterator[dict]:
    """
    Analyse each time point in its own driver log and yield events:

      {"type": "step",  "content": <markdown>}     progress
      {"type": "point", "data": <TimePoint dict>}  one finished time point
      {"type": "done",  "data": <result dict>}     terminal success; carries
                                                   "markdown" and every point
      {"type": "error", "content": <message>}      terminal failure

    `driver_logs` is one path used for every point, or {point index: path}
    as plan() suggested and the user confirmed. A point with no path is
    reported as such and never reaches the LLM. `agent_factory=None` is a dry
    run: every step up to the agent runs, without spending tokens.
    """
    try:
        yield {"type": "step", "content": f"Loading Sleep Study report: `{html_path}`"}
        collected, points = _select(html_path, sw_threshold, max_points, per_device)
        yield {"type": "step", "content": (
            f"**{collected['total_sessions']}** sessions in the report; "
            f"**{collected['qualifying_count']}** are battery drains with SW DRIPS "
            f"< {sw_threshold:.0f}% and a RED top blocker. "
            f"Analysing the worst **{len(points)}** (max {max_points}, "
            f"{per_device} per device)."
        )}

        if isinstance(driver_logs, dict):
            chosen = {int(k): str(v or "").strip() for k, v in driver_logs.items()}
        else:
            chosen = {p.index: str(driver_logs or "").strip() for p in points}

        # Several points usually share one capture, so each path is indexed once.
        indexed: dict[str, dict[str, list[LogSegment]]] = {}
        log_warnings: list[str] = []

        out_dir = out_dir or tempfile.mkdtemp(prefix="sleepstudy_")
        os.makedirs(out_dir, exist_ok=True)

        for p in points:
            p.driver_log_path = chosen.get(p.index, "")
            head = (f"#{p.index} Session {p.session_id} · "
                    f"{DEVICE_LABELS.get(p.device, p.device)} · "
                    f"{_fmt(p.window_start)} → {_fmt(p.window_end)}")

            if p.device not in ANALYZABLE_DEVICES:
                p.status = STATUS_UNSUPPORTED
                p.message = (f"{DEVICE_LABELS.get(p.device, p.device)} blocker: this tool has "
                             "no agent for that driver log. Hand the Sleep Study evidence "
                             "to the owning team.")
            elif p.window_start is None:
                p.status = STATUS_NOT_COVERED
                p.message = "The report gives no readable start time for this session."
            elif not p.driver_log_path:
                p.status = STATUS_NO_LOG
                p.message = f"No {DEVICE_LABELS[p.device]} driver log was chosen for this time point."
            else:
                if p.driver_log_path not in indexed:
                    found, warns = discover_driver_logs(p.driver_log_path)
                    indexed[p.driver_log_path] = found
                    log_warnings.extend(warns)
                    for w in warns:
                        yield {"type": "step", "content": f"⚠️ {w}"}
                    for dev, segs in found.items():
                        yield {"type": "step", "content": (
                            f"{DEVICE_LABELS.get(dev, dev)} driver log `{p.driver_log_path}`: "
                            f"{len(segs)} file(s), {_fmt(segs[0].start)} → {_fmt(segs[-1].end)}")}
                segments = indexed[p.driver_log_path].get(p.device, [])
                p.coverage, hits = coverage_of(segments, p.window_start, p.window_end)
                if p.coverage == "none":
                    p.status = STATUS_NOT_COVERED
                    p.message = "Driver log 沒有涵蓋這段時間 (the driver log does not cover this window)."
                else:
                    p.excerpt_path = os.path.join(
                        out_dir, f"point{p.index:02d}_session{p.session_id}_{p.device}.log")
                    yield {"type": "step", "content": f"{head}: cutting driver log excerpt…"}
                    ex = write_excerpt(hits, p.window_start, p.window_end, p.excerpt_path)
                    p.excerpt_lines = ex["lines"]
                    p.driver_log_sources = ex["sources"]
                    if p.coverage == "partial":
                        p.message = "The driver log covers only part of this window."
                    if not ex["lines"]:
                        p.status = STATUS_NO_LINES
                        p.message = "The driver log covers this window but logged nothing in it."
                    elif agent_factory is None:
                        p.status = STATUS_DRY_RUN
                    else:
                        yield {"type": "step", "content": (
                            f"{head}: {ex['lines']} lines → "
                            f"{DEVICE_LABELS[p.device]} agent…")}
                        try:
                            _run_agent(p, agent_factory(p.device), max_steps,
                                       on_step=lambda _content: None)
                            p.status = STATUS_ANALYZED
                        except Exception as exc:
                            p.status = STATUS_AGENT_FAILED
                            p.message = f"Agent failed: {type(exc).__name__}: {exc}"
            yield {"type": "step", "content": f"{head}: **{p.status}** {p.message}".rstrip()}
            yield {"type": "point", "data": p.to_dict()}

        result = {
            "source": html_path,
            "driver_logs": sorted({p.driver_log_path for p in points if p.driver_log_path}),
            "total_sessions": collected["total_sessions"],
            "qualifying_count": collected["qualifying_count"],
            "criteria": _criteria(collected, max_points, per_device),
            "devices": collected["devices"],
            "warnings": collected["warnings"] + log_warnings,
            "points": [p.to_dict() for p in points],
            "excerpt_dir": out_dir,
        }
        result["markdown"] = render_markdown(result)
        yield {"type": "done", "data": result}

    except Exception as exc:
        yield {"type": "error", "content": f"{type(exc).__name__}: {exc}"}


def render_markdown(result: dict) -> str:
    c = result["criteria"]
    out = [
        "# Sleep Study × Driver Log Analysis",
        f"**Report:** `{result['source']}`  ",
        "**Driver log:** " + (", ".join(f"`{d}`" for d in result["driver_logs"]) or "none") + "  ",
        f"**Sessions:** {result['qualifying_count']} of {result['total_sessions']} qualify "
        f"(battery drain, SW DRIPS < {c['drip_threshold_pct']:.0f}%, RED top blocker); "
        f"{len(result['points'])} analysed.",
    ]
    if result["warnings"]:
        out.append("")
        out += [f"> ⚠️ {w}" for w in result["warnings"]]
    if not result["points"]:
        out += ["", "No session matches the criteria. Nothing needs a driver-log look."]
        return "\n".join(out)

    out += ["", "| # | Session | Window | SW / HW DRIPS | Blocker | Driver log | Status |",
            "|---|---|---|---|---|---|---|"]
    for p in result["points"]:
        top = p["blockers"][0]["name"] if p["blockers"] else "-"
        lines = f"{p['excerpt_lines']} lines" if p["excerpt_lines"] else (p["coverage"] or "-")
        if p["driver_log_path"]:
            lines = f"{_basename(p['driver_log_path'])}: {lines}"
        out.append(
            f"| {p['index']} | {p['session_id']} ({p['type_label']}) | "
            f"{p['window_start']} → {p['window_end']} | "
            f"{_pct(p['sw_drip_pct'])} / {_pct(p['hw_drip_pct'])} | "
            f"{p['device_label']}: {top} | {lines} | {p['status']} |")

    for p in result["points"]:
        out += ["", "---", "",
                f"## #{p['index']} Session {p['session_id']} — {p['device_label']}",
                f"_Issue time {p['issue_time']} · window {p['window_start']} → {p['window_end']}"
                + (" (capped)" if p["window_truncated"] else "") + "_"]
        if p["message"]:
            out.append(f"\n> {p['message']}")
        out += ["", "<details><summary>Sleep Study evidence</summary>", "",
                "```", p["sleep_summary"], "```", "</details>"]
        if p["agent_report"]:
            out += ["", "### Driver log analysis", "", p["agent_report"]]
    return "\n".join(out)
