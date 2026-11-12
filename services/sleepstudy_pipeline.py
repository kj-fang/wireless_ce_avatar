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

import copy
from html import escape as html_escape, unescape as html_unescape
import math
import os
import queue
import re
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator, Optional

from services import sleepstudy_analyzer as ssa


# ── Selection policy ───────────────────────────────────────────────────────────
# A session is bad when software OR hardware DRIPS is below this, the same
# bar the Sleep Study analyzer has always used.
DRIP_THRESHOLD = ssa.DRIP_THRESHOLD      # 80 percent
DRIP_METRIC = ssa.DRIP_METRIC_EITHER
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
    # Where each edge was read from, so a person can check the time a file is
    # said to cover: its own name, the next file's name, or a line inside it.
    start_from: str = ""
    end_from: str = ""

    def overlaps(self, lo: datetime, hi: datetime) -> bool:
        return self.start < hi and (self.end is None or self.end > lo)

    def describe(self) -> dict:
        return {
            "file": os.path.basename(self.path),
            "path": self.path,
            "start": _fmt(self.start),
            "end": _fmt(self.end),
            "start_from": self.start_from,
            "end_from": self.end_from,
        }


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
                LogSegment(f, ssa.DEVICE_WIFI, "wifi_etl", first, last,
                           "first timestamped line", "last timestamped line"))

    # Rolling files hand over to each other, so each one runs until the next
    # one opens. Only the newest has to be read to know where it ends.
    wifilogs.sort()
    for i, (start, f) in enumerate(wifilogs):
        if i + 1 < len(wifilogs):
            end = wifilogs[i + 1][0]
            end_from = "next file's name (" + os.path.basename(wifilogs[i + 1][1]) + ")"
        else:
            end = _wifilog_last_time(f, start)
            end_from = "last line of the file"
        found.setdefault(ssa.DEVICE_WIFI, []).append(
            LogSegment(f, ssa.DEVICE_WIFI, "wrt_wifilog", start, end,
                       "file name", end_from))

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


def check_driver_log(path: str, device: str, window_start: str, window_end: str) -> dict:
    """
    Whether one driver log covers a time point's window, and what time span
    each overlapping file covers and where that span was read from. For the
    page to show under a path the user typed or picked.
    """
    lo = datetime.strptime(window_start, "%m/%d/%Y %H:%M:%S")
    hi = datetime.strptime(window_end, "%m/%d/%Y %H:%M:%S")
    index, warnings = discover_driver_logs(path)
    segments = index.get(device, [])
    coverage, hits = coverage_of(segments, lo, hi)
    return {
        "coverage": coverage,
        "files": [s.describe() for s in hits],
        # When nothing overlaps, the span the log does cover says how far off it is.
        "log_span": ({"start": _fmt(segments[0].start), "end": _fmt(segments[-1].end)}
                     if segments else None),
        "warnings": warnings,
    }


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
    wake_evidence: dict = field(default_factory=dict)   # _wake_evidence()
    issue_description: str = ""
    driver_log_path: str = ""     # suggested by plan(), confirmed by the user
    driver_log_files: list = field(default_factory=list)   # LogSegment.describe() of the hits
    excerpt_path: str = ""
    excerpt_lines: int = 0
    driver_log_sources: list = field(default_factory=list)
    agent_result_type: str = ""
    agent_report: str = ""
    agent_result: dict = field(default_factory=dict)   # agent.chat()'s return value
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
        d["html_verdict"] = html_verdict(self.wake_evidence or {})
        d["short_device_wakes"] = short_device_wakes(self.wake_evidence or {})
        # Every wireless time point is checked by default. The HTML verdict is
        # a useful hypothesis for the driver agent, not a reason to skip the
        # driver log: it can confirm or refine the report's app/OS attribution.
        d["analyse_default"] = True
        return d


def _fmt(dt: Optional[datetime]) -> str:
    return dt.strftime("%m/%d/%Y %H:%M:%S") if dt else ""


def _basename(path: str) -> str:
    return os.path.basename(path.rstrip("\\/")) or path


def criteria_text(criteria: dict) -> str:
    """'SW or HW DRIPS < 80%' / 'SW DRIPS < 50%', from collect_sessions' criteria."""
    which = "SW DRIPS" if criteria.get("drip_metric") == ssa.DRIP_METRIC_SW else "SW or HW DRIPS"
    return f"{which} < {float(criteria.get('drip_threshold_pct') or 0):.0f}%"


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


def _wake_evidence(raw: Optional[dict], device: str) -> dict:
    """
    What the report says kept the device awake in one session: for each of
    its FX Devices entries, the children that carry metadata. For Wi-Fi these
    are the OS WiFiCx jobs that woke the device and the D0 window they held
    it in ("[Detailed][12:22:54][WIFICX]OS Wi-Fi Jobs Waked Up Device":
    WiFiJobRoam = 2, ...), the net wake reasons (NetWakeReasonTypeDevice = 2)
    and how long each wake lasted ("0-29 seconds": 3). Plus network activity
    per app (SRUM network power, NDIS bytes and outgoing wakes), the
    platform's DRIPS wake sources and why it entered and left standby.
    """
    ev = {"enter_reason": "", "exit_reason": "", "drips_wake_sources": [],
          "device_entries": [], "apps": []}
    if not raw:
        return ev
    ev["enter_reason"] = str(raw.get("EnterReason") or "")
    ev["exit_reason"] = str(raw.get("ExitReason") or "")
    for w in raw.get("DripsWakeSources") or []:
        ev["drips_wake_sources"].append({
            "reason": str(w.get("ReasonDescription") or ""),
            "type": str(w.get("Type") or ""),
            "count": int(w.get("Count") or 0),
        })
    for group in raw.get("BlockerGroups") or []:
        if group.get("Name") != "FX Devices":
            continue
        for blocker in group.get("Blockers") or []:
            if ssa.classify_device(blocker.get("Name", "")) != device:
                continue
            entry = {
                "name": blocker.get("Name", ""),
                "active_pct": blocker.get("ActiveTimePercent", 0),
                "metadata": {str(v.get("Key")): str(v.get("Value"))
                             for v in (blocker.get("Metadata") or {}).get("Values", [])},
                "wakes": [],
            }
            for child in blocker.get("Children") or []:
                values = (child.get("Metadata") or {}).get("Values", [])
                buckets = {str(b.get("BucketName")): int(b.get("Value") or 0)
                           for b in child.get("BlockingTimeBuckets") or []}
                if not values and not any(buckets.values()):
                    continue
                entry["wakes"].append({
                    "name": child.get("Name", ""),
                    "active_pct": child.get("ActiveTimePercent", 0),
                    "values": {str(v.get("Key")): str(v.get("Value")) for v in values},
                    "buckets": {k: v for k, v in buckets.items() if v},
                })
            ev["device_entries"].append(entry)
    if device == ssa.DEVICE_WIFI:
        wifi = ssa.extract_wifi_content(raw)
        apps: dict = {}
        for a in wifi["srum_network_apps"]:
            slot = apps.setdefault(a["app_id"], {"app": a["app_id"]})
            slot["network_mw"] = a["network_power_mw"] + a["mbb_power_mw"]
        for a in wifi["ndis_app_traffic"]:
            slot = apps.setdefault(a["app_id"], {"app": a["app_id"]})
            slot["bytes"] = slot.get("bytes", 0) + a["bytes_sent"] + a["bytes_received"]
            slot["outgoing_wakes"] = slot.get("outgoing_wakes", 0) + a["outgoing_wake_count"]
        ev["apps"] = sorted(apps.values(), key=lambda a: (-(a.get("network_mw") or 0),
                                                         -(a.get("bytes") or 0)))[:8]
    return ev


# Appended to every time point's question, after the editable sleepstudy_point
# prompt. The scenario rules and the first answer line live here, not in the
# prompt, because the report's verdict (html_verdict) is computed by the same
# rules and the run summary reads the agent's verdict back by that line.
POINT_ANSWER_CONTRACT = """Classify the session into exactly ONE scenario:
- Scenario A: activity by OS / app. One or a few App IDs, or OS jobs, clearly dominate the network power or traffic (SRUM mW, NDIS bytes or outgoing wakes, WiFiCx jobs) and drive the drain; an app counts as dominating at {app_share} or more of it. Name them and by how much.
- Scenario B: activity by remote. A continuous stream of short wakes raised by the device (NetWakeReasonTypeDevice) from unsolicited inbound traffic. Name the packets if the driver log shows them. Exception: with MORE THAN {short_wake_limit} short (0-29 s) NetWakeReasonTypeDevice wakes in the session, do not answer Scenario B; answer "Please contact Intel Wi-Fi team".
- Neither is clearly supported by the evidence: answer "Please contact Intel Wi-Fi team".

Start root_cause_summary with exactly this line:
Scenario: <A | B | Please contact Intel Wi-Fi team> | Sleep Study: <confirmed | partly | refuted> | Dominant wake cause: <NDIS / OS request | wake on packet | wake on key or user | timer | app traffic | other> -- <one concise sentence naming the specific trigger (OS command and its GUID / opcode, roam reason, packet type) and what kept the device in D0>
Keep apart what WOKE the device (the D0 Entry: wake reason, the OS request that brought it up) and what HELD it in D0 (traffic, repeated OS commands, scans or roams that kept it from going back to Dx). The report's "activity by app" is the app's traffic while the device was active: it is about what held the device in D0, not what woke it, so an app that raised no wakes is not ruled out by that alone.
"Sleep Study" judges the report's suspected cause ({html_verdict}):
- confirmed: the driver log shows that cause held the device in D0 and nothing else did;
- partly: that cause may have held it, and the driver log shows other causes that did too (the usual case when traffic ran while OS commands also kept the device busy);
- refuted: only when the driver log shows that cause could NOT have held the device in D0. For app traffic, that means the TX/RX counters (PROP_GET_STATISTICS) or the data-path lines show little or no data while the device was in D0, or the traffic fell outside the D0 window. Quote that evidence. Without it, answer partly, never refuted.
Then add at most three short bullets: "Woke:" what brought the device to D0, with timestamps; "Held in D0:" what kept it there, with the cadence or counters; "Sleep Study:" why confirmed / partly / refuted, plus the skill you used in a few words. Do not list every OS job, write a full timeline, repeat the conclusion, or add generic recommendations. Keep the whole response under 140 words. Use only evidence; do not invent numbers."""


# The driver's wake reasons (WOWLAN_WAKE_UP_REASON_API_E_VER_2), as the log
# names them ("Wakeup due to: WAKEUP_BY_<NAME>") or sets them in the
# WakeupReason bit mask. Grouped the way the NW skills group them
# (nw_skills.yaml, Modern Standby Wake step 2), so the report files a wake
# where the agent's skill did: name -> (bit, group).
WAKE_REASONS = {
    "NON_WIRELESS": (0x0, "os request"),
    "MAGIC_PACKET": (0x1, "packet"),
    "PATTERN": (0x2, "packet"),
    "DISCONNECTION_ON_MISSED_BEACON": (0x4, "link lost"),
    "DISCONNECTION_ON_DEAUTH": (0x8, "link lost"),
    "GTK_REKEY_FAILURE": (0x10, "key exchange"),
    "RFKILL_DEASSERTED": (0x20, "user"),
    "UCODE_ERROR": (0x40, "firmware"),
    "EAPOL_REQUEST": (0x80, "key exchange"),
    "FOUR_WAY_HANDSHAKE": (0x100, "key exchange"),
    "REM_WAKE_LINK_LOSS": (0x200, "remote wake ended"),
    "REM_WAKE_SIGNATURE_TABLE": (0x400, "remote wake ended"),
    "REM_WAKE_TCP_EXTERNAL": (0x800, "packet"),
    "REM_WAKE_WAKEUP_PACKET": (0x1000, "packet"),
    "D3_WAKEUP_HOST_TIMER": (0x4000, "timer"),
    "RXFRAME_FILTERED_IN": (0x8000, "packet"),
    "BEACON_FILTERED_IN": (0x10000, "packet"),
    "11W_UNPROTECTED_DEAUTH_OR_DISASSOC": (0x20000, "link lost"),
    "PATTERN_IPV4_TCP_SYN": (0x40000, "packet"),
    "PATTERN_IPV4_TCP_SYN_WILDCARD": (0x80000, "packet"),
    "PATTERN_IPV6_TCP_SYN": (0x100000, "packet"),
    "PATTERN_IPV6_TCP_SYN_WILDCARD": (0x200000, "packet"),
    "ACTION_FRAME": (0x400000, "packet"),
}
_WAKEUP_REASON_HEX = re.compile(r"(WakeupReason\s*[=:]?\s*)(0x[0-9A-Fa-f]+)\b")


def wake_reason_re(group: str) -> str:
    """A regex for the WAKEUP_BY_ names of one group; longest first, so
    PATTERN_IPV4_TCP_SYN is not read as PATTERN."""
    names = sorted((n for n, (_, g) in WAKE_REASONS.items() if g == group), key=len, reverse=True)
    return r"WAKEUP_BY_(?:" + "|".join(names) + r")\b"


def decode_wake_reason(value: int) -> list[str]:
    """The WAKEUP_BY_ names of a WakeupReason bit mask; 0 is NON_WIRELESS.
    A bit the enum does not define is kept as its hex value."""
    if not value:
        return ["WAKEUP_BY_NON_WIRELESS"]
    names = [f"WAKEUP_BY_{n}" for n, (bit, _) in WAKE_REASONS.items() if bit and value & bit]
    unknown = value & ~sum(bit for bit, _ in WAKE_REASONS.values())
    return names + ([f"unknown bits 0x{unknown:X}"] if unknown else [])


def _name_wake_reasons(text: str) -> str:
    """'WakeupReason = 0x00000008' -> '... 0x00000008 (WAKEUP_BY_DISCONNECTION_ON_DEAUTH)',
    unless the text already names it, so a raw value is classified too."""
    def named(m):
        names = decode_wake_reason(int(m.group(2), 16))
        if all(n in text for n in names):
            return m.group(0)
        return f"{m.group(0)} ({' | '.join(names)})"
    return _WAKEUP_REASON_HEX.sub(named, text)


# The dominant wake cause of a session, in the categories the per-session
# answer uses (POINT_ANSWER_CONTRACT), each with the words that identify it
# when an answer phrases it loosely. First match wins, so the order matters:
# "wake on packet" must not be taken for an OS request.
WAKE_CAUSES = (
    ("Wake on packet", ("packet", "wol", "wake on lan", "pattern")),
    ("Wake on key / user", ("key", "user", "button", "lid", "keyboard", "touch")),
    ("NDIS / OS request", ("ndis", "oid", "os request", "os job", "wificx", "job")),
    ("App traffic", ("app",)),
    ("Timer", ("timer",)),
)


def dominant_cause(text: str) -> tuple[str, str]:
    """Extract the cause line, with an evidence-based fallback for loose reports."""
    source = html_unescape(re.sub(r"<[^>]+>", " ", str(text or "")))
    source = source.replace("**", "").replace("__", "").replace("`", "")
    m = re.search(r"(?:Dominant\s+)?wake\s+cause\s*:\s*([^\r\n]+)", source, re.I)
    if m:
        line = m.group(1).strip().strip(" *|")
        parts = re.split(r"\s+(?:--|—|–|-)\s+", line, maxsplit=1)
        label = parts[0].strip(" *|").lower()
        detail = parts[1].strip() if len(parts) > 1 else ""
        for name, words in WAKE_CAUSES:
            if any(w in label for w in words):
                return name, detail
        return "Other", detail or line

    # Older agent responses sometimes put the same finding in an executive
    # summary without the contract header. Infer only from a named trigger.
    # A driver wake reason counts by its group: only a packet or a key
    # exchange is a wake on packet (WAKEUP_BY_NON_WIRELESS is the OS's).
    lead = _name_wake_reasons(source[:1800]).lower()
    detail = _first_causal_sentence(source)
    if re.search(r"wake[- ]on[- ]packet|magic packet|" + wake_reason_re("packet")
                 + "|" + wake_reason_re("key exchange"), lead, re.I):
        return "Wake on packet", detail
    if re.search(wake_reason_re("user"), lead, re.I):
        return "Wake on key / user", detail
    if re.search(wake_reason_re("timer"), lead, re.I):
        return "Timer", detail
    if re.search(r"roam|background scan|wificx|wifi.?job|device.?service.?command|\boid\b|os request|os job", lead):
        return "NDIS / OS request", detail
    if re.search(r"(?i)(?:app traffic|application traffic|pstcore.{0,100}(?:kept|held|prevent|delay)|(?:kept|held|prevent|delay).{0,100}pstcore|bulk (?:upload|download|transfer).{0,100}(?:kept|held|prevent|delay))", lead):
        return "App traffic", detail
    if re.search(r"timer|keepalive", lead):
        return "Timer", detail
    return "Unknown", detail


def _first_causal_sentence(text: str) -> str:
    """Pick the first short sentence that explains a wake or delayed Dx."""
    clean = re.sub(r"<[^>]+>", " ", html_unescape(str(text or "")))
    clean = re.sub(r"(?im)^\s*(?:#{1,6}\s*|[-*]\s*)", "", clean)
    for line in clean.splitlines():
        line = re.sub(r"\s+", " ", line).strip(" -*`\t")
        if not line or re.match(r"(?i)(scenario|dominant wake cause|skill|evidence|recommendation)\s*:", line):
            continue
        if re.search(r"(?i)(kept|held|remain|prevent|delay|block|D0|Dx|low.power|wake|roam|scan|traffic)", line):
            sentence = re.split(r"(?<=[.!?])\s+", line, maxsplit=1)[0]
            return _short_detail(sentence, 230)
    return ""


def _short_detail(text: str, limit: int = 170) -> str:
    """Keep the leading cause readable in the always-visible session finding."""
    text = re.sub(r"\s+", " ", str(text or "")).strip(" -*`\t\r\n")
    if len(text) <= limit:
        return text
    clipped = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return clipped + "…"


def _remove_empty_additional_logs_section(text: str) -> str:
    """Hide the stock empty-log note while retaining real customer requests."""
    return re.sub(
        r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?Additional Logs Needed(?: from Customer)?[ \t]*\r?\n"
        r"[ \t]*\(none\b[^\r\n]*\)[ \t]*(?:\r?\n|$)",
        "",
        str(text or ""),
    ).strip()


def _fill(text: str, values: dict) -> str:
    """Replace the known {fields}; leave any other braces as written."""
    return re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1), m.group(0))), text)


def _rule_values() -> dict:
    return {"short_wake_limit": str(SHORT_DEVICE_WAKE_LIMIT),
            "app_share": f"{APP_DOMINANCE_SHARE:.0%}"}


SHORT_WAKE_BUCKET = "0-29 seconds"
# The old Sleep Study prompt's line: more short wakes raised by the device
# than this in one session is not a remote-traffic pattern a user can fix,
# so it goes to the Intel Wi-Fi team.
SHORT_DEVICE_WAKE_LIMIT = 900
# An app counts as dominating when it has at least this share of the
# session's network power (or, without SRUM power, of its NDIS bytes).
APP_DOMINANCE_SHARE = 0.5


def short_device_wakes(ev: dict) -> int:
    """Short (0-29 s) wakes on the entries that record a device-raised wake."""
    total = 0
    for entry in ev.get("device_entries") or []:
        for wake in entry["wakes"]:
            if any(k.startswith("NetWakeReasonTypeDevice") for k in wake["values"]):
                total += int((wake.get("buckets") or {}).get(SHORT_WAKE_BUCKET, 0))
    return total


def html_verdict(ev: dict) -> str:
    """
    The scenario the report alone points to, by the old Sleep Study rules:
      more than 900 short device wakes  -> Please contact Intel Wi-Fi team
      one app has >= half the network   -> A (app)
      short device wakes                -> B (activity by remote)
      OS jobs woke the device           -> A (OS jobs)
      none of these                     -> Please contact Intel Wi-Fi team
    Deterministic, so it can be counted and set against the driver logs.
    """
    short = short_device_wakes(ev)
    if short > SHORT_DEVICE_WAKE_LIMIT:
        return f"Please contact Intel Wi-Fi team ({short} short device wakes > {SHORT_DEVICE_WAKE_LIMIT})"
    apps = ev.get("apps") or []
    for metric in ("network_mw", "bytes"):
        total = sum(a.get(metric) or 0 for a in apps)
        if total:
            top = max(apps, key=lambda a: a.get(metric) or 0)
            share = (top.get(metric) or 0) / total
            if share >= APP_DOMINANCE_SHARE:
                return f"A: activity by app ({_app_name(top['app'])}, {share:.0%} of {metric.replace('_', ' ')})"
            break
    if short:
        return f"B: activity by remote ({short} short device wakes)"
    jobs = {}
    for entry in ev.get("device_entries") or []:
        for wake in entry["wakes"]:
            for k, v in wake["values"].items():
                if k.startswith("WiFiJob"):
                    try:
                        jobs[k] = jobs.get(k, 0) + int(v)
                    except ValueError:
                        pass
    if jobs:
        top = max(jobs, key=jobs.get)
        return f"A: activity by OS jobs (most: {top.replace('WiFiJob', '')} ×{jobs[top]})"
    return "Please contact Intel Wi-Fi team (no clear cause in the report)"


def sleepstudy_agreement(text: str) -> str:
    """'confirmed' / 'refuted' / 'partly' from an answer's first line, or ''."""
    m = re.search(r"Sleep\s*Study\s*:\s*\**\s*(confirmed|refuted|partly)", str(text or ""), re.I)
    return m.group(1).lower() if m else ""


# Contributing factors, as a driver conclusion names them. Read from the
# agent's root-cause summary only (a full report also lists what was ruled
# out), so a session counts for a factor when its conclusion blames it.
CONTRIBUTING_FACTORS = (
    ("Device-service polling", r"PROP_DEVICE_SERVICE_COMMAND|device[- ]service"),
    ("Statistics / BSS queries", r"PROP_GET_STATISTICS|QueryStatistics|GetBSSInfo|QueryConnectionInfo|statistics quer"),
    ("Background-scan roam", r"ROAM_REASON_BG_SCAN|background[- ]scan|\bbg[- ]scan|\broam"),
    # The driver wake reasons (WAKE_REASONS) fall into the factor of their
    # group, as the NW skills file them.
    ("Firmware error / reload", r"(?<!no need to )reload(?:ing|ed)? (?:the )?(?:fw|firmware)|RT full reset|FW was dead|"
                                r"image (?:is )?not intact|" + wake_reason_re("firmware")),
    ("Wake on packet", r"wake[- ]on[- ]packet|magic packet|pattern match|" + wake_reason_re("packet")),
    ("Key exchange wake", r"GTK rekey (?:failure|failed)|" + wake_reason_re("key exchange")),
    ("Disconnect / reconnect", r"disconnect|deauth|disassoc|reconnect|" + wake_reason_re("link lost")),
    ("Remote-wake session ended", wake_reason_re("remote wake ended")),
    ("Radio switch (RF kill)", r"\brf[- ]?kill|" + wake_reason_re("user")),
    ("Timer", r"\btimer\b|" + wake_reason_re("timer")),
)


# A factor named only to be ruled out ("no roam", "not a FW reload").
_NEGATED = re.compile(r"(?i)\b(?:no|not|without|never|ruled out|excluded|rather than)\b[^.;:]{0,30}$")


def _pieces(text: str) -> list[str]:
    """A root-cause summary cut into its sentences and bullets, without the
    contract line's labels, so a factor is matched to the claim naming it."""
    t = html_unescape(re.sub(r"<[^>]+>", " ", str(text or ""))).replace("**", "").replace("`", "")
    t = _name_wake_reasons(t)
    t = re.sub(r"(?is)^.*?Dominant\s+wake\s+cause\s*:[^\n]*?\s(?:--|—|–)\s", "", t, count=1)
    out = []
    for chunk in re.split(r"\n+|\s*•\s*|(?<=[.;])\s+(?=[A-Z(])", t):
        chunk = re.sub(r"\s+", " ", chunk).strip(" -*\t")
        if chunk:
            out.append(chunk)
    return out


def _factor_match(piece: str, pattern: str):
    for m in re.finditer(pattern, piece, re.I):
        if not _NEGATED.search(piece[:m.start()]):
            return m
    return None


def contributing_factors(text: str) -> list[str]:
    pieces = _pieces(text)
    return [name for name, pattern in CONTRIBUTING_FACTORS
            if any(_factor_match(p, pattern) for p in pieces)]


def factor_evidence(text: str, name: str, limit: int = 240) -> str:
    """The part of a summary that names the factor: the session's evidence
    for it, timestamps and cadence included. A long sentence naming several
    factors is cut around this one, so each factor shows its own words."""
    pattern = dict(CONTRIBUTING_FACTORS).get(name, "")
    for piece in _pieces(text):
        m = _factor_match(piece, pattern) if pattern else None
        if not m:
            continue
        if len(piece) <= limit:
            return piece
        lo = max(0, m.start() - limit // 3)
        hi = min(len(piece), lo + limit)
        lo = piece.rfind(" ", 0, lo) + 1 if lo else 0
        hi = piece.find(" ", hi) if hi < len(piece) and piece.find(" ", hi) != -1 else len(piece)
        return ("…" if lo else "") + piece[lo:hi].strip(" ,;") + ("…" if hi < len(piece) else "")
    return ""


def os_jobs(ev: dict) -> dict:
    """WiFiCx OS jobs that joined the device's wakes, summed: {'GetBSSInfo': 8, ...}."""
    jobs = {}
    for entry in (ev or {}).get("device_entries") or []:
        for wake in entry.get("wakes") or []:
            for key, value in (wake.get("values") or {}).items():
                if key.startswith("WiFiJob"):
                    try:
                        jobs[key[len("WiFiJob"):]] = jobs.get(key[len("WiFiJob"):], 0) + int(value)
                    except (TypeError, ValueError):
                        pass
    return jobs


# What pins a factor down, read from the sentence that names it: the
# identifier that lets someone act on it, and how often it fired.
FACTOR_IDS = {
    "Device-service polling": r"GUID\b[^\n]{0,20}?(0x[0-9A-Fa-f]{8})",
    "Background-scan roam": r"(ROAM_REASON_[A-Z_]+)",
}
_CADENCE = r"(?:every\s*)?[~≈]\s*(\d{1,3})(?:\s*[–-]\s*\d{1,3})?\s*s\b"

# Driver-level facts the Sleep Study cannot show, read from the full analysis.
DRIVER_FACTS = (
    ("Woken by the OS, not by the device (non-wireless wake)",
     r"WAKEUP_BY_NON_WIRELESS|non-wireless wake|did not self-wake|OS pulled it"),
    ("Firmware intact on resume, no reload",
     r"(?:FW|firmware)\s+(?:was\s+|image\s+)?intact|no (?:FW|firmware) reload"),
)

# The action each driver-level factor calls for; {id} is its identifier.
FACTOR_ACTIONS = {
    "Device-service polling": ("P0", "Find the service sending PROP_DEVICE_SERVICE_COMMAND{id} and stop its polling in standby"),
    "Firmware error / reload": ("P0", "Firmware failed or was reloaded in D3 (WAKEUP_BY_UCODE_ERROR, RT full reset): send the driver log to the Intel Wi-Fi team"),
    "Key exchange wake": ("P1", "The AP's key exchange woke the host: check that GTK rekey offload is armed and why it did not complete in D3"),
    "Remote-wake session ended": ("P2", "The offloaded TCP remote-wake session ended in D3: check the remote-wake configuration and its server side"),
    "Radio switch (RF kill)": ("P2", "A radio kill switch change woke the device: a user or platform action, not a driver issue"),
    "Background-scan roam": ("P1", "Check why the OS runs background scans and roams in standby{id}: scan / roam policy, AP signal"),
    "Wake on packet": ("P1", "Review the WoWLAN patterns and the remote traffic that matched them"),
    "Disconnect / reconnect": ("P1", "Find why the link drops in standby (disconnect / deauth reason, AP side)"),
    "Statistics / BSS queries": ("P2", "Find which client keeps querying statistics / BSS info in standby (WLAN API user, OEM service)"),
    "Timer": ("P2", "Identify the timer that wakes the device"),
}

# Follow-ups the agents recommend that are not a root cause of the drain
# but recur across sessions: (name, pattern, default priority, action).
# An action row shows when two or more sessions recommend it.
FOLLOW_UP_TOPICS = (
    ("D0 entry / exit bounce", r"double D0|D0 Entry/Exit bounce|spurious double|back-to-back TASK_DOT11_RESET",
     "P1", "Investigate the brief D0 Entry / Exit bounce right after suspend (back-to-back TASK_DOT11_RESET)"),
)


def _section_lines(report: str, title_re: str) -> list[str]:
    """The lines under a heading of the agent's report, up to the next heading."""
    out, inside = [], False
    for line in str(report or "").splitlines():
        bare = re.sub(r"^[#>*\s]+|[*\s:]+$", "", line).strip()
        if not inside:
            inside = bool(bare) and len(bare) < 60 and re.match(title_re, bare, re.I) is not None
            continue
        s = line.strip()
        if re.match(r"^#{1,6}\s", s) or (re.match(r"^\*\*[^*]+\*\*:?$", s) and not re.match(r"^\*\*P\d", s)):
            break
        out.append(line)
    return out


def agent_recommendations(report: str) -> list[tuple[str, str]]:
    """(priority, action) from the agent's Recommendations section."""
    out, prio = [], "P1"
    for line in _section_lines(report, r"recommendations?$"):
        s = line.strip()
        m = re.match(r"^[-*\s]*\**\s*(P[0-3])\b[^:]*:?\**\s*(.*)$", s)
        if m:
            prio, s = m.group(1), m.group(2).strip()
        s = re.sub(r"^[-*\d.)\s]+", "", s).replace("**", "").replace("`", "")
        s = re.split(r"\s+[—–-]+\s*evidence\s*:", s, maxsplit=1, flags=re.I)[0].strip()
        if len(s) > 15:
            out.append((prio, _short_detail(s, 220)))
    return out


def logs_needed(report: str) -> list[str]:
    """The extra logs the agent asked for, without the stock 'none' line."""
    out = []
    for line in _section_lines(report, r"additional logs needed"):
        s = re.sub(r"^[-*\d.)\s]+", "", line.strip()).replace("**", "").replace("`", "")
        if len(s) > 10 and not re.match(r"(?i)\(?none\b", s):
            out.append(_short_detail(re.split(r"\s+[—–]\s+", s, maxsplit=1)[0], 160))
    return out


def _hypothesis(verdict: str) -> str:
    """The report's verdict without its numbers, so sessions can be grouped:
    'A: activity by app (PSTCore.exe, 99% of bytes)' -> 'A: activity by app (PSTCore.exe)'."""
    v = re.sub(r",\s*\d+%\s*of\s+[a-z ]+\)", ")", str(verdict or ""))
    return re.sub(r"\s*\(\d+ short device wakes?\)", "", v).strip()


def verdict_scenario(text: str) -> str:
    """'A' / 'B' / 'contact Intel' / '?' from a verdict or a finding."""
    t = str(text or "")
    m = re.search(r"Scenario\s*:?\s*(A|B)\b", t)
    if m:
        return m.group(1)
    if re.search(r"contact Intel", t, re.I):
        return "contact Intel"
    m = re.match(r"\s*(A|B)\s*:", t)
    return m.group(1) if m else "?"


def _app_name(app_id: str) -> str:
    return re.split(r"[\\/]", str(app_id))[-1] or str(app_id)


def wake_evidence_lines(ev: dict) -> list[str]:
    """The device's wake evidence as short lines, for the agent and the summary."""
    lines = []
    for entry in ev.get("device_entries") or []:
        meta = ", ".join(f"{k} = {v}" for k, v in entry["metadata"].items())
        lines.append(f"{entry['name']}: active {entry['active_pct']}% of the session"
                     + (f" ({meta})" if meta else ""))
        for wake in entry["wakes"]:
            values = ", ".join(f"{k} = {v}" for k, v in wake["values"].items()
                               if k != "[Name]")
            buckets = ", ".join(f"{k}: {v}" for k, v in (wake.get("buckets") or {}).items())
            lines.append(f"  - {wake['name']} (active {wake['active_pct']}%)"
                         + (f": {values}" if values else "")
                         + (f" [wake durations {buckets}]" if buckets else ""))
    sources = ", ".join(f"{w['reason']} ×{w['count']}" for w in ev.get("drips_wake_sources") or [])
    if sources:
        lines.append(f"Platform DRIPS wake sources: {sources}")
    if ev.get("enter_reason") or ev.get("exit_reason"):
        lines.append(f"Entered standby: {ev.get('enter_reason') or '?'}; "
                     f"left it: {ev.get('exit_reason') or '?'}")
    return lines


def app_activity_lines(ev: dict) -> list[str]:
    lines = []
    for a in ev.get("apps") or []:
        parts = []
        if a.get("network_mw"):
            parts.append(f"{a['network_mw']} mW network power")
        if a.get("bytes"):
            parts.append(f"{a['bytes']} bytes")
        if a.get("outgoing_wakes"):
            parts.append(f"{a['outgoing_wakes']} outgoing wakes")
        if parts:
            lines.append(f"  - {_app_name(a['app'])}: " + ", ".join(parts))
    return lines


def _issue_description(point: TimePoint, threshold: float,
                       template: Optional[str] = None) -> str:
    """
    The question put to the agent for one time point: the sleepstudy_point
    speclet with this session's facts filled in. Only the known {fields} are
    replaced, so braces an editor writes for any other reason stay as they are.
    """
    from services.chatbot.engine.speclet_defaults import default_speclet

    top = point.blockers[0] if point.blockers else {}
    ev = point.wake_evidence or {}
    values = {
        "session_id": str(point.session_id),
        "session_type": point.type_label,
        "start": _fmt(point.start),
        "end": _fmt(point.end),
        "duration": point.duration,
        "sw_drips": _pct(point.sw_drip_pct),
        "hw_drips": _pct(point.hw_drip_pct),
        "threshold": f"{threshold:.0f}%",
        "top_blocker": str(top.get("name", "unknown")),
        "top_blocker_active": f"{top.get('active_time_pct', '?')}%",
        "device": DEVICE_LABELS.get(point.device, point.device),
        "driver_request_examples": (
            "Wi-Fi OS/OID or WiFiCx jobs (for example DeviceServiceCommand, Roam, "
            "QueryStatistics, or scan)" if point.device == ssa.DEVICE_WIFI else
            "Windows Bluetooth/USB-stack requests (for example discovery/inquiry, "
            "connection, or power-state transitions)" if point.device == ssa.DEVICE_BT else
            "OS and driver-framework requests visible in the log"
        ),
        "wake_evidence": "\n".join(f"  {l}" for l in wake_evidence_lines(ev))
                         or "  (no wake detail in the report)",
        "app_activity": "\n".join(app_activity_lines(ev)) or "  (none recorded)",
        "short_device_wakes": str(short_device_wakes(ev)),
        "html_verdict": html_verdict(ev),
        **_rule_values(),
    }
    text = template or default_speclet("nw", "sleepstudy_point")
    return _fill(text, values) + "\n\n" + _fill(POINT_ANSWER_CONTRACT, values)


def render_point_finding(p: dict) -> str:
    """
    One time point, showing what the driver log ADDED: the wake cause it
    found and why the driver stayed out of Dx. What the Sleep Study already
    said (its verdict, short wakes, SW DRIPS) shrinks to one muted line --
    the reader saw it on the plan card -- and everything else is folded.
    """
    ev = p.get("wake_evidence") or {}
    summary = ""
    if isinstance(p.get("agent_result"), dict) and isinstance(p["agent_result"].get("data"), dict):
        summary = str(p["agent_result"]["data"].get("root_cause_summary") or "")
    driver_analysis = p.get("agent_report") or summary
    esc = lambda value: html_escape(str(value or ""), quote=True)
    drip_label = _pct(p.get("sw_drip_pct"))
    report = html_verdict(ev)

    if p["status"] == STATUS_ANALYZED:
        cause, detail = dominant_cause(summary or driver_analysis)
        if not detail:
            detail = _first_causal_sentence(driver_analysis)
        if cause == "Unknown":
            cause = "Cause not isolated"
        finding = (_short_detail(detail, 320) if detail else
                   "The driver log does not isolate a wake trigger or what delayed the return to Dx.")
        # The agent says whether the report's suspected cause held up; the
        # scenario letter alone cannot, since A covers both an app and OS jobs.
        agreement = sleepstudy_agreement(summary or driver_analysis)
        driver_s, report_s = verdict_scenario(summary or driver_analysis), verdict_scenario(report)
        if agreement == "confirmed" or (not agreement and driver_s == report_s and driver_s != "?"):
            state, state_class = "Confirms Sleep Study", "done"
        elif agreement == "partly":
            state, state_class = "Partly confirms", "pending"
        elif agreement == "refuted" or (not agreement and driver_s not in ("?", report_s)):
            state, state_class = "Changes the picture", "pending"
        else:
            state, state_class = "Analysed", "done"
    else:
        cause = "Not analysed"
        finding = p.get("message") or "No driver-log conclusion is available for this session."
        state, state_class = "Not analysed", "pending"

    driver_files = ", ".join(_basename(f.get("file", "")) for f in p.get("driver_log_files") or [])
    driver_files = driver_files or _basename(p.get("driver_log_path", "")) or "No driver log selected"
    report_evidence = wake_evidence_lines(ev) + app_activity_lines(ev)
    evidence_list = ("<ul>" + "".join(f"<li>{esc(line.strip())}</li>" for line in report_evidence[:8])
                     + "</ul>" if report_evidence else "<p>No additional wake detail was recorded.</p>")
    full_analysis = _remove_empty_additional_logs_section(driver_analysis)
    short_wakes = short_device_wakes(ev)

    return "\n".join([
        '<article class="ss-point-card">',
        '  <header class="ss-point-head">',
        f'    <span class="ss-point-number">{int(p["index"]):02d}</span>',
        '    <div class="ss-point-title">',
        f'      <h3>Session {esc(p["session_id"])}</h3>',
        f'      <span class="ss-point-window">{esc(p.get("device_label") or "Device")} · SW DRIPS {esc(drip_label)}</span>',
        '    </div>',
        f'    <span class="ss-point-status {state_class}">{esc(state)}</span>',
        '  </header>',
        '  <section class="ss-point-core">',
        '    <span>DRIVER LOG FINDING</span>',
        f'    <div><b>{esc(cause)}</b> · {esc(finding)}</div>',
        '  </section>',
        # The Sleep Study's side, already shown on the plan card: one line.
        f'  <p class="ss-point-window" style="margin:6px 2px 0;">Sleep Study said: {esc(report)}'
        f' · {short_wakes} short device wake(s)</p>',
        '</article>',
        '<details class="ss-point-details"><summary>Evidence and full analysis</summary>',
        '',
        f'**Window:** {esc(p.get("window_start"))} → {esc(p.get("window_end"))}  ',
        f'**Driver log:** `{esc(driver_files)}`  ',
        f'**DRIPS:** SW {esc(drip_label)} · HW {esc(_pct(p.get("hw_drip_pct")))} · 80% target',
        '',
        '<div class="ss-point-evidence-grid">',
        f'<section><b>Report evidence</b>{evidence_list}</section>',
        '</div>',
        '',
        '**Detailed driver analysis**',
        '',
        str(full_analysis),
        '</details>',
    ])


# ══════════════════════════════════════════════════════════════════════════════
#  AGENT
# ══════════════════════════════════════════════════════════════════════════════

def default_agent_factory(device: str, wifi_skills: Optional[dict] = None):
    """
    A fresh agent of the device's chatbot, sharing the boot-time agent's
    client, model, skills and ACE runner -- the same agent a user gets on the
    Wi-Fi or BT page, so its prompts and skills apply here unchanged.

    `wifi_skills` replaces the boot-time Wi-Fi skills: the NW page passes its
    conversation's, so a skill appended there ("Append Skills from YAML",
    e.g. Modern Standby Wake) is used by the run at once, not after a restart.
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
    if wifi_skills and device != ssa.DEVICE_BT:
        skills = wifi_skills
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
        root = str(data.get("root_cause_summary") or "").strip()
        markdown = str(data.get("markdown_summary") or "").strip()
        if root and markdown.startswith(root):
            markdown = markdown[len(root):].strip()
        parts = [root, markdown]
        return kind, "\n\n".join(p for p in parts if p)
    if isinstance(data, str):
        return kind, data
    return kind, "" if data is None else str(data)


def _run_agent(point: TimePoint, agent, max_steps: int, on_step: Callable[[dict], None]) -> None:
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
            on_step(step)

    message = (
        f"{point.issue_description}\n\nSleep Study evidence:\n```\n"
        f"{point.sleep_summary[:4000]}\n```\n"
        f"Issue time: around {_fmt(point.issue_time)}"
    )
    result = agent.chat(message, max_steps=max_steps, step_callback=_step)
    point.agent_result_type, point.agent_report = _report_text(result)
    point.agent_result = result if isinstance(result, dict) else {}
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
#   run_point()     analyses one time point in the driver log chosen for it,
#                   streaming the agent's own steps. The page calls it once
#                   per point, so the points run one after another and can be
#                   paused between them.
#   run_pipeline()  all points in one go, for callers without a page (an MCP
#                   server, a dry run).

_SELECT_CACHE: dict = {}


def _select(html_path: str, drip_threshold: float, max_points: int, per_device: int):
    key = (os.path.abspath(html_path), os.path.getmtime(html_path),
           drip_threshold, max_points, per_device)
    if key not in _SELECT_CACHE:
        if len(_SELECT_CACHE) >= 4:
            _SELECT_CACHE.clear()
        _SELECT_CACHE[key] = _select_uncached(html_path, drip_threshold, max_points, per_device)
    # Callers fill in the points, so each gets its own copy.
    return copy.deepcopy(_SELECT_CACHE[key])


def _select_uncached(html_path: str, drip_threshold: float, max_points: int, per_device: int):
    payload = ssa.load_sleepstudy(html_path)
    # Time points are the sessions a driver log can explain: a wireless RED
    # top blocker. Every other bad session is the "system" group below.
    collected = ssa.collect_sessions_from_payload(
        payload, source=html_path, devices=ANALYZABLE_DEVICES,
        drip_threshold=drip_threshold, drip_metric=DRIP_METRIC,
    )
    raw_by_id = {s.get("SessionId"): s for s in payload.get("ScenarioInstances") or []}
    points = select_time_points(collected["sessions"], max_points, per_device)
    for p in points:
        p.sleep_summary = _sleep_summary(p, raw_by_id.get(p.session_id))
        p.wake_evidence = _wake_evidence(raw_by_id.get(p.session_id), p.device)
    return collected, points, _system_sessions(payload, drip_threshold)


def _system_sessions(payload: dict, drip_threshold: float) -> dict:
    """
    Bad sessions (on battery, Modern Standby, DRIPS below the bar by the same
    rule as the time points) with no wireless RED top blocker. No driver log
    of ours explains them, but they belong in the report: a platform where
    most of the drain is not wireless is itself the conclusion.
    """
    bad, wireless, groups = 0, 0, {}
    for s in payload.get("ScenarioInstances") or []:
        if s.get("OnAc", True) or s.get("Type") not in ssa.MS_SESSION_TYPES:
            continue
        sw, hw = ssa.compute_drip_percentages(s)
        below = [v for v in ((sw, hw) if DRIP_METRIC == ssa.DRIP_METRIC_EITHER else (sw,))
                 if v is not None and v < drip_threshold]
        if not below:
            continue
        bad += 1
        reds = [t for t in s.get("TopBlockers") or [] if (t.get("ActivityLevel") or 0) == ssa.RED_ACTIVITY_LEVEL]
        if any(ssa.classify_device(t.get("Name", "")) in ANALYZABLE_DEVICES for t in reds):
            wireless += 1
            continue
        top = max(reds, key=lambda t: t.get("ActiveTimePercent") or 0) if reds else None
        name = top.get("Name", "") if top else "(no RED top blocker)"
        g = groups.setdefault(name, {"blocker": name, "sessions": [], "worst_sw": None,
                                     "device": ssa.classify_device(name) if top else ""})
        g["sessions"].append(s.get("SessionId"))
        if sw is not None and (g["worst_sw"] is None or sw < g["worst_sw"]):
            g["worst_sw"] = round(sw, 1)
    ordered = sorted(groups.values(), key=lambda g: -len(g["sessions"]))
    return {"bad_total": bad, "wireless": wireless, "system": bad - wireless, "groups": ordered}


def time_points(html_path: str, *, drip_threshold: float = DRIP_THRESHOLD,
                max_points: int = MAX_TIME_POINTS, per_device: int = PER_DEVICE_LIMIT) -> list[dict]:
    """The time points as plan() numbers them, with their Sleep Study evidence."""
    return [p.to_dict() for p in _select(html_path, drip_threshold, max_points, per_device)[1]]


def system_sessions(html_path: str, *, drip_threshold: float = DRIP_THRESHOLD,
                    max_points: int = MAX_TIME_POINTS, per_device: int = PER_DEVICE_LIMIT) -> dict:
    """The report's bad sessions split into wireless and system, with the
    system ones grouped by their top blocker."""
    return _select(html_path, drip_threshold, max_points, per_device)[2]


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
    drip_threshold: float = DRIP_THRESHOLD,
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
    collected, points, system = _select(html_path, drip_threshold, max_points, per_device)
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
                p.driver_log_files = [s.describe() for s in hits]
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
        "sessions": system,
    }


def _head(p: TimePoint) -> str:
    return (f"#{p.index} Session {p.session_id} · {DEVICE_LABELS.get(p.device, p.device)} · "
            f"{_fmt(p.window_start)} → {_fmt(p.window_end)}")


def _analyze_point(p: TimePoint, indexed: dict, log_warnings: list, out_dir: str,
                   agent_factory, max_steps: int, on_agent=None) -> Iterator[dict]:
    """
    Analyse one time point in p.driver_log_path. Yields "step" events for
    the pipeline's own progress and "agent_step" events carrying each step
    of the agent (its skill choices and tool calls), and leaves the outcome
    on `p`. `indexed` caches discover_driver_logs() per path across points.
    """
    head = _head(p)
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
        segments = indexed[p.driver_log_path].get(p.device, [])
        p.coverage, hits = coverage_of(segments, p.window_start, p.window_end)
        if p.coverage == "none":
            p.status = STATUS_NOT_COVERED
            p.message = "Driver log 沒有涵蓋這段時間 (the driver log does not cover this window)."
        else:
            p.driver_log_files = [h.describe() for h in hits]
            p.excerpt_path = os.path.join(
                out_dir, f"point{p.index:02d}_session{p.session_id}_{p.device}.log")
            yield {"type": "step", "content": (
                f"{head}: cutting the driver log — "
                + ", ".join(f"`{h.describe()['file']}`" for h in hits) + "…")}
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
                    f"{head}: {ex['lines']} lines → {DEVICE_LABELS[p.device]} agent…")}
                yield from _agent_steps(p, agent_factory, max_steps, on_agent)
    yield {"type": "step", "content": f"{head}: **{p.status}** {p.message}".rstrip()}


def _agent_steps(p: TimePoint, agent_factory, max_steps: int, on_agent) -> Iterator[dict]:
    """Run the agent on a worker thread and yield its steps as they happen."""
    steps: queue.Queue = queue.Queue()

    def work():
        try:
            agent = agent_factory(p.device)
            if on_agent is not None:
                on_agent(agent)          # lets the caller cancel it
            _run_agent(p, agent, max_steps, on_step=lambda st: steps.put(("step", st)))
            steps.put(("ok", None))
        except Exception as exc:
            steps.put(("failed", exc))

    threading.Thread(target=work, daemon=True).start()
    while True:
        kind, value = steps.get()
        if kind == "step":
            yield {"type": "agent_step", "step": value}
        elif kind == "ok":
            p.status = STATUS_ANALYZED
            return
        else:
            p.status = STATUS_AGENT_FAILED
            p.message = f"Agent failed: {type(value).__name__}: {value}"
            return


def run_point(
    html_path: str,
    index: int,
    driver_log: str,
    *,
    agent_factory: Optional[Callable[[str], object]] = default_agent_factory,
    out_dir: Optional[str] = None,
    drip_threshold: float = DRIP_THRESHOLD,
    max_points: int = MAX_TIME_POINTS,
    per_device: int = PER_DEVICE_LIMIT,
    max_steps: int = 6,
    on_agent: Optional[Callable[[object], None]] = None,
    point_template: Optional[str] = None,
) -> Iterator[dict]:
    """
    Analyse the time point `index` (as plan() numbered it) in `driver_log`
    ("" for none), and yield events:

      {"type": "start",      "data": <TimePoint dict>} the point, before any work
      {"type": "step",       "content": <markdown>}   pipeline progress
      {"type": "agent_step", "step": <agent step>}    the agent's own steps
      {"type": "done",       "data": <TimePoint dict, plus "markdown">}
      {"type": "error",      "content": <message>}

    `on_agent(agent)` is called with the agent before it starts, so the
    caller can stop it (agent.cancel_event).
    """
    try:
        _collected, points, _system = _select(html_path, drip_threshold, max_points, per_device)
        p = next((x for x in points if x.index == int(index)), None)
        if p is None:
            yield {"type": "error", "content": f"The report has no time point #{index}."}
            return
        p.driver_log_path = str(driver_log or "").strip()
        p.issue_description = _issue_description(p, drip_threshold, point_template)
        yield {"type": "start", "data": p.to_dict()}
        out_dir = out_dir or tempfile.mkdtemp(prefix="sleepstudy_")
        os.makedirs(out_dir, exist_ok=True)
        yield from _analyze_point(p, {}, [], out_dir, agent_factory, max_steps, on_agent)
        data = p.to_dict()
        data["markdown"] = render_point_markdown(data)
        data["finding"] = render_point_finding(data)
        yield {"type": "done", "data": data}
    except Exception as exc:
        yield {"type": "error", "content": f"{type(exc).__name__}: {exc}"}


def run_pipeline(
    html_path: str,
    driver_logs,
    *,
    agent_factory: Optional[Callable[[str], object]] = default_agent_factory,
    out_dir: Optional[str] = None,
    drip_threshold: float = DRIP_THRESHOLD,
    max_points: int = MAX_TIME_POINTS,
    per_device: int = PER_DEVICE_LIMIT,
    max_steps: int = 6,
    point_template: Optional[str] = None,
) -> Iterator[dict]:
    """
    Analyse every time point and yield events: "step" and "agent_step" as in
    run_point(), {"type": "point", "data": <TimePoint dict>} per finished
    point, then {"type": "done", "data": <result dict with "markdown">} or
    {"type": "error", "content": <message>}.

    `driver_logs` is one path used for every point, or {point index: path}.
    A point with no path is reported as such and never reaches the LLM.
    `agent_factory=None` is a dry run: every step up to the agent runs.
    """
    try:
        yield {"type": "step", "content": f"Loading Sleep Study report: `{html_path}`"}
        collected, points, _system = _select(html_path, drip_threshold, max_points, per_device)
        yield {"type": "step", "content": (
            f"**{collected['total_sessions']}** sessions in the report; "
            f"**{collected['qualifying_count']}** are battery drains with "
            f"{criteria_text(collected['criteria'])} and a RED top blocker. "
            f"Analysing the worst **{len(points)}** (max {max_points}, "
            f"{per_device} per device)."
        )}
        if isinstance(driver_logs, dict):
            chosen = {int(k): str(v or "").strip() for k, v in driver_logs.items()}
        else:
            chosen = {p.index: str(driver_logs or "").strip() for p in points}

        indexed: dict = {}
        log_warnings: list[str] = []
        out_dir = out_dir or tempfile.mkdtemp(prefix="sleepstudy_")
        os.makedirs(out_dir, exist_ok=True)
        for p in points:
            p.driver_log_path = chosen.get(p.index, "")
            p.issue_description = _issue_description(p, drip_threshold, point_template)
            yield from _analyze_point(p, indexed, log_warnings, out_dir, agent_factory, max_steps)
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


def render_point_markdown(p: dict) -> str:
    """One time point: its window, outcome, Sleep Study evidence and analysis."""
    out = [f"## #{p['index']} Session {p['session_id']} — {p['device_label']}",
           f"_Issue time {p['issue_time']} · window {p['window_start']} → {p['window_end']}"
           + (" (capped)" if p["window_truncated"] else "") + f" · **{p['status']}**_"]
    if p["driver_log_files"]:
        out.append("")
        out += [f"- Driver log `{f['file']}`: {f['start']} → {f['end']} "
                f"(start from {f['start_from']}, end from {f['end_from']})"
                for f in p["driver_log_files"]]
    if p["message"]:
        out.append(f"\n> {p['message']}")
    out += ["", "<details><summary>Sleep Study evidence</summary>", "",
            "```", p["sleep_summary"], "```", "</details>"]
    if p["agent_report"]:
        out += ["", "### Driver log analysis", "", p["agent_report"]]
    return "\n".join(out)


def render_markdown(result: dict) -> str:
    c = result["criteria"]
    out = [
        "# Sleep Study × Driver Log Analysis",
        f"**Report:** `{result['source']}`  ",
        "**Driver log:** " + (", ".join(f"`{d}`" for d in result["driver_logs"]) or "none") + "  ",
        f"**Sessions:** {result['qualifying_count']} of {result['total_sessions']} qualify "
        f"(battery drain, {criteria_text(c)}, RED top blocker); "
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
        out += ["", "---", "", render_point_markdown(p)]
    return "\n".join(out)


# ══════════════════════════════════════════════════════════════════════════════
#  RUN SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

def wake_statistics(points: list[dict]) -> dict:
    """Counts across the time points, straight from the report (no LLM)."""
    jobs: dict = {}
    reasons: dict = {}
    sources: dict = {}
    for p in points:
        ev = p.get("wake_evidence") or {}
        for entry in ev.get("device_entries") or []:
            for wake in entry["wakes"]:
                for key, value in wake["values"].items():
                    if key in ("[Name]", "[Wake Session Time Range]"):
                        continue
                    try:
                        n = int(value)
                    except ValueError:
                        continue
                    table = reasons if key.startswith("NetWakeReason") else jobs
                    slot = table.setdefault(key, {"count": 0, "sessions": set()})
                    slot["count"] += n
                    slot["sessions"].add(p["session_id"])
        for w in ev.get("drips_wake_sources") or []:
            slot = sources.setdefault(w["reason"], {"count": 0, "sessions": set()})
            slot["count"] += w["count"]
            slot["sessions"].add(p["session_id"])
    return {"os_jobs": jobs, "net_wake_reasons": reasons, "drips_wake_sources": sources}


def _verdict_tables(analysed: list[dict], findings: dict) -> tuple[list[str], list[dict]]:
    """Per session: the report's scenario against the driver log's, counted."""
    rows = []
    for p in analysed:
        report = html_verdict(p.get("wake_evidence") or {})
        driver = verdict_scenario(findings[p["index"]])
        rows.append({"p": p, "report": report, "report_s": verdict_scenario(report),
                     "driver_s": driver})
    if not rows:
        return [], rows
    out = ["", "**Report vs driver log, per session**", "",
           "| Session | Short device wakes | Report says | Driver log says | Agree |",
           "|---|---|---|---|---|"]
    for r in rows:
        agree = "?" if r["driver_s"] == "?" else ("✔" if r["driver_s"] == r["report_s"] else "✘")
        out.append(f"| {r['p']['session_id']} | {short_device_wakes(r['p'].get('wake_evidence') or {})} | "
                   f"{r['report']} | {r['driver_s']} | {agree} |")
    scenarios = ("A", "B", "contact Intel", "?")
    n = len(rows)
    out += ["", "**Scenario counts**", "", "| Scenario | Report | Driver log |", "|---|---|---|"]
    for sc in scenarios:
        rep = sum(1 for r in rows if r["report_s"] == sc)
        drv = sum(1 for r in rows if r["driver_s"] == sc)
        if rep or drv:
            out.append(f"| {sc} | {rep}/{n} ({100 * rep // n}%) | {drv}/{n} ({100 * drv // n}%) |")
    judged = [r for r in rows if r["driver_s"] != "?"]
    if judged:
        same = sum(1 for r in judged if r["driver_s"] == r["report_s"])
        out.append(f"\nReport and driver log agree on **{same}/{len(judged)}** session(s) "
                   f"({100 * same // len(judged)}%)"
                   + (f"; {n - len(judged)} driver verdict(s) could not be read." if len(judged) < n else "."))
    return out, rows


def _stats_tables(stats: dict, total: int) -> list[str]:
    out = []
    for title, key in (("OS jobs that woke the device", "os_jobs"),
                       ("Net wake reasons", "net_wake_reasons"),
                       ("Platform DRIPS wake sources", "drips_wake_sources")):
        rows = sorted(stats[key].items(), key=lambda kv: -kv[1]["count"])
        if not rows:
            continue
        out += ["", f"**{title}**", "", "| | Count | Sessions |", "|---|---|---|"]
        out += [f"| {name.replace('WiFiJob', '')} | {v['count']} | "
                f"{len(v['sessions'])}/{total} ({100 * len(v['sessions']) // max(total, 1)}%) |"
                for name, v in rows]
    return out


# Appended to the editable sleepstudy_summary prompt: the report puts the
# conclusion on top and folds the rest, so it has to know where one ends.
SUMMARY_OUTPUT_CONTRACT = """Return only a **Conclusion:** of at most two short sentences. Name the specific driver-level root cause shared across the checked sessions (the OS command and its GUID, the roam reason, the cadence), not just its category, and say what the driver logs add to the Sleep Study interpretation: confirmed, partly (other causes also held the device in D0), or refuted. Do not add headings, tables, timelines, per-session restatements, or recommendations."""
_DETAILS_MARK = "---DETAILS---"


def _folded(title: str, lines: list[str], open_: bool = False) -> list[str]:
    """A collapsible block; markdown inside needs the blank lines around it."""
    return ["", f"<details{' open' if open_ else ''}><summary><b>{title}</b></summary>", "",
            *lines, "", "</details>"]


def cause_table(points: list[dict], findings: dict) -> tuple[list[str], list[str]]:
    """
    The driver logs' answer across the checked sessions: each session's
    wake cause in one line, and the share of sessions per cause.
    """
    checked = [p for p in points if p["index"] in findings]
    if not checked:
        return [], []
    per_session, counts = [], {}
    for p in checked:
        cause, detail = dominant_cause(findings[p["index"]])
        counts.setdefault(cause, []).append(p["session_id"])
        per_session.append(f"{p['index']}. Session {p['session_id']} · SW {_pct(p['sw_drip_pct'])} → "
                           f"**{cause}**" + (f" — {detail}" if detail else ""))
    n = len(checked)
    table = ["| Wake cause (driver log) | Sessions | Share |", "|---|---|---|"]
    for cause, ids in sorted(counts.items(), key=lambda kv: -len(kv[1])):
        table.append(f"| **{cause}** | {len(ids)} ({', '.join(str(i) for i in ids)}) | "
                     f"**{100 * len(ids) // n}%** |")
    return table, per_session


def _most_common(values) -> str:
    counts = {}
    for v in values:
        if v:
            counts[v] = counts.get(v, 0) + 1
    return max(counts, key=counts.get) if counts else ""


def summarize_run(source: str, points: list[dict], findings: dict,
                  llm_call: Optional[Callable[[str, str], str]] = None,
                  system_prompt: Optional[str] = None,
                  sessions: Optional[dict] = None,
                  reports: Optional[dict] = None) -> str:
    """
    The run's report: what the driver logs found across sessions, not a
    second copy of the session cards above it.
      - the conclusion (one LLM call), in full
      - the driver-level root causes, ranked, with a session matrix and the
        identifier that pins each one down (GUID, roam reason, cadence)
      - Sleep Study vs driver log side by side: what the report could see,
        and what the driver logs add
      - the actions those causes call for, merged across sessions
      - one compact row per session, and the system sessions
    The per-cause evidence and each session's own recommendations and log
    requests fold below their section.
    `points` are all the wireless time points (TimePoint dicts), `findings`
    maps the index of each checked one to its agent's root-cause summary,
    `reports` (optional) to its full driver analysis, and `sessions` is
    system_sessions().
    """
    from services.chatbot.engine.speclet_defaults import default_speclet

    sessions = sessions or {"bad_total": 0, "wireless": 0, "system": 0, "groups": []}
    reports = reports or {}
    checked = [p for p in points if p["index"] in findings]
    n_checked = len(checked)
    esc = lambda value: html_escape(str(value if value is not None else ""), quote=True)
    all_system = not points and sessions["system"] > 0
    top_group = sessions["groups"][0] if sessions["groups"] else None
    ids_of = lambda ps: ", ".join(str(p["session_id"]) for p in ps)

    # ── What the checked sessions add up to ──
    cause_of, agree_of, factors_of, full = {}, {}, {}, {}
    for p in checked:
        i = p["index"]
        cause_of[i] = dominant_cause(findings[i])[0]
        agree_of[i] = sleepstudy_agreement(findings[i]) or "unclear"
        factors_of[i] = contributing_factors(findings[i])
        full[i] = reports.get(i) or findings[i]
    causes = {}
    for p in checked:
        causes.setdefault(cause_of[p["index"]], []).append(p)
    ranked = sorted(causes.items(), key=lambda kv: -len(kv[1]))

    order = [name for name, _ in CONTRIBUTING_FACTORS]
    factor_hits = {name: [p for p in checked if name in factors_of[p["index"]]] for name in order}
    factor_rank = sorted(((k, v) for k, v in factor_hits.items() if v),
                         key=lambda kv: (-len(kv[1]), order.index(kv[0])))
    evidence = {name: {p["index"]: factor_evidence(findings[p["index"]], name) for p in ps}
                for name, ps in factor_rank}
    ident = {}
    for name, ps in factor_rank:
        bits = []
        pattern = FACTOR_IDS.get(name)
        if pattern:
            found = [re.search(pattern, evidence[name][p["index"]]) or re.search(pattern, findings[p["index"]])
                     for p in ps]
            value = _most_common(m.group(1) for m in found if m)
            if value:
                bits.append(f"GUID {value}" if name == "Device-service polling" else value)
        if name == "Device-service polling":
            cadence = _most_common(m.group(1) for m in
                                   (re.search(_CADENCE, evidence[name][p["index"]]) for p in ps) if m)
            if cadence:
                bits.append(f"~{cadence} s")
        ident[name] = " · ".join(bits)
    fact_hits = []
    for label, pattern in DRIVER_FACTS:
        ps = [p for p in checked if any(_factor_match(x, pattern) for x in _pieces(full[p["index"]]))]
        if ps:
            fact_hits.append((label, ps))

    agree_counts = {}
    for p in checked:
        agree_counts[agree_of[p["index"]]] = agree_counts.get(agree_of[p["index"]], 0) + 1
    hypotheses = {}
    for p in points:
        hypotheses.setdefault(_hypothesis(p.get("html_verdict")), []).append(p)

    # ── Actions, merged across sessions ──
    # Each session's own recommendations, grouped by the cause or follow-up
    # they address; a topic is matched before a factor, since a follow-up
    # line can mention a factor in passing.
    topics = [(name, pat) for name, pat, _, _ in FOLLOW_UP_TOPICS] + list(CONTRIBUTING_FACTORS)
    recs_by = {}
    for p in checked:
        seen = set()
        for prio, text in agent_recommendations(reports.get(p["index"], "")):
            name = next((n for n, pat in topics if re.search(pat, text, re.I)), "Other")
            if name not in seen:
                seen.add(name)
                recs_by.setdefault(name, []).append((p, prio, text))

    def agents_priority(name, default):
        """The priority most of the agents gave it (the more urgent on a tie)."""
        prios = [prio for _, prio, _ in recs_by.get(name, [])]
        return min(set(prios), key=lambda x: (-prios.count(x), x)) if prios else default

    actions = []
    for name, ps in factor_rank:
        if name in FACTOR_ACTIONS:
            prio, text = FACTOR_ACTIONS[name]
            actions.append((agents_priority(name, prio),
                            text.replace("{id}", f" ({ident[name]})" if ident[name] else ""), name, ps))
    for name, _, prio, text in FOLLOW_UP_TOPICS:
        ps = [p for p, _, _ in recs_by.get(name, [])]
        if len(ps) >= 2:
            actions.append((agents_priority(name, prio), text, name, ps))
    actions.sort(key=lambda a: (a[0], -len(a[3])))
    logs = {}
    for p in checked:
        for line in logs_needed(reports.get(p["index"], "")):
            # Two sessions asking about the same GUID / ID ask for one log.
            hex_id = re.search(r"0x[0-9A-Fa-f]{6,8}", line)
            key = (f"id:{hex_id.group(0).lower()}" if hex_id
                   else re.sub(r"[^a-z0-9]+", " ", line.lower()).strip()[:40])
            entry = logs.setdefault(key, [line, []])
            if p not in entry[1]:
                entry[1].append(p)
    logs_ranked = sorted(logs.values(), key=lambda e: -len(e[1]))

    # ── The conclusion ──
    conclusion = ""
    if all_system:
        conclusion = (
            f"No driver log check needed: none of the {sessions['bad_total']} bad session(s) has a "
            f"wireless RED top blocker, so this is a platform (system) issue, not a wireless driver one."
            + (f" The leading blocker is {top_group['blocker']} ({len(top_group['sessions'])} "
               f"session(s)); hand it to the platform team." if top_group else ""))
    elif llm_call is not None and (points or sessions["groups"]):
        lines = [f"Bad sessions: {sessions['bad_total']}; wireless: {sessions['wireless']}; "
                 f"system: {sessions['system']}. Checked against driver logs: {n_checked} of {len(points)}."]
        lines += [f"Driver-level root cause {name}" + (f" ({ident[name]})" if ident[name] else "")
                  + f": {len(ps)}/{n_checked} sessions" for name, ps in factor_rank]
        lines += [f"Driver fact '{label}': {len(ps)}/{n_checked} sessions" for label, ps in fact_hits]
        lines += [f"Wake-cause category {name}: {len(ps)}/{n_checked} sessions" for name, ps in ranked]
        lines += [f"Sleep Study verdict {k}: {v}/{n_checked}" for k, v in agree_counts.items()]
        for p in points:
            line = (f"Session {p['session_id']} SW DRIPS {_pct(p['sw_drip_pct'])}; Sleep Study: "
                    f"{p.get('html_verdict', '')}; ")
            if p["index"] in findings:
                line += f"driver log: {_short_detail(findings[p['index']], 320)}"
            else:
                line += "driver log not checked."
            lines.append(line)
        if sessions["groups"]:
            lines.append("System blockers: " + "; ".join(
                f"{g['blocker']} ({len(g['sessions'])})" for g in sessions["groups"]))
        prompt = (_fill(system_prompt or default_speclet("nw", "sleepstudy_summary"), _rule_values())
                  + "\n\n" + SUMMARY_OUTPUT_CONTRACT)
        try:
            text = llm_call(prompt, "\n".join(lines)) or ""
            conclusion = text.split(_DETAILS_MARK, 1)[0].strip().split("\n\n", 1)[0]
            conclusion = re.sub(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?Conclusion:?\s*", "", conclusion,
                                flags=re.IGNORECASE)
            conclusion = re.sub(r"\*{1,2}", "", conclusion.replace(r"\*", "")).strip()
            # Shown whole: the contract keeps it to two sentences, and a cut
            # here would drop the end of the run's main finding.
            conclusion = _short_detail(conclusion, 1200)
        except Exception as exc:
            conclusion = f"Summary unavailable ({type(exc).__name__}); see the comparison below."
    elif factor_rank:
        name, ps = factor_rank[0]
        conclusion = (f"Driver logs most often point to {name}" + (f" ({ident[name]})" if ident[name] else "")
                      + f": {len(ps)}/{n_checked} checked sessions.")
    elif ranked:
        lead, ps = ranked[0]
        conclusion = f"Driver logs most often point to {lead} ({len(ps)}/{n_checked} checked sessions)."
    elif points:
        conclusion = "No driver logs were checked; this report reflects the Sleep Study evidence only."

    # ── Header ──
    if all_system:
        worst = [g["worst_sw"] for g in sessions["groups"] if g.get("worst_sw") is not None]
        stats = [("Not needed", "driver log check"),
                 (_pct(min(worst)) if worst else "N/A", "lowest SW DRIPS"),
                 (_short_detail(top_group["blocker"], 40) if top_group else "—", "leading blocker"),
                 (sessions["system"], "system sessions")]
        kicker, subtitle = ("MODERN STANDBY · PLATFORM ISSUE",
                            "No wireless session to explain: every bad session is blocked outside the wireless driver.")
    else:
        if factor_rank:
            lead_name, lead_ps = factor_rank[0]
            lead_label = " · ".join(x for x in ("top root cause", ident[lead_name],
                                                 f"{len(lead_ps)}/{n_checked} sessions") if x)
        elif ranked:
            lead_name, lead_ps = ranked[0]
            lead_label = f"leading cause · {len(lead_ps)}/{n_checked} sessions"
        else:
            lead_name, lead_label = "—", "top root cause"
        judged = sum(v for k, v in agree_counts.items() if k != "unclear")
        held = agree_counts.get("confirmed", 0) + agree_counts.get("partly", 0)
        stats = [(lead_name, lead_label),
                 (f"{n_checked} / {len(points)}", "sessions checked"),
                 (f"{held} / {judged}" if judged else "—", "Sleep Study cause held (fully or partly)"),
                 (sessions["system"], "system sessions") if sessions["system"]
                 else (len(factor_rank), "driver-level causes found")]
        kicker = "MODERN STANDBY · DRIVER LOG CORRELATION"
        subtitle = (f"{sessions['bad_total']} bad session(s) on battery · {len(points)} wireless analysed · "
                    f"{sessions['system']} system")
    out = ['<section class="ss-report">',
           '  <header class="ss-report-hero">',
           f'    <div class="ss-report-kicker">{kicker}</div>',
           '    <h2>Sleep Study report</h2>',
           f'    <p class="ss-report-subtitle">{esc(subtitle)}</p>',
           '    <div class="ss-report-stats">',
           *[f'      <div class="ss-stat"><strong>{esc(v)}</strong><span>{esc(l)}</span></div>' for v, l in stats],
           '    </div>',
           '  </header>']
    if conclusion:
        out += ['  <section class="ss-report-conclusion">',
                '    <span class="ss-section-kicker">OVERALL FINDING</span>',
                f'    <p>{esc(conclusion)}</p>',
                '  </section>']

    def heading(kicker_text, title, note=""):
        return ('    <div class="ss-section-heading"><div>'
                f'<span class="ss-section-kicker">{kicker_text}</span><h3>{title}</h3></div>'
                + (f'<span class="ss-muted">{esc(note)}</span>' if note else "") + '</div>')

    def fold(title, body):
        return (f'    <details class="ss-fold"><summary>{esc(title)}</summary>'
                f'<div class="ss-fold-body">{body}</div></details>')

    # ── Root causes, ranked, by session ──
    if factor_rank:
        cols = "".join(f'<th>{esc(p["session_id"])}</th>' for p in checked)
        out += ['  <section class="ss-report-section">',
                heading("DRIVER LOG", "Root causes, ranked", f"{n_checked} session(s) checked"),
                '    <table class="ss-matrix ss-rank-table"><thead><tr><th>Root cause</th><th>Share</th>'
                + cols + '</tr></thead><tbody>']
        for name, ps in factor_rank:
            share = 100 * len(ps) / n_checked
            cells = "".join('<td class="hit">●</td>' if p in ps else '<td></td>' for p in checked)
            sub = f'<br><small>{esc(ident[name])}</small>' if ident[name] else ""
            out.append(f'      <tr><th>{esc(name)}{sub}</th><td class="bar">'
                       f'<span class="ss-drips-track"><i class="ss-drips-fill" style="width:{share:.0f}%"></i></span>'
                       f'<b>{len(ps)}/{n_checked}</b></td>{cells}</tr>')
        out.append('    </tbody></table>')
        body = ""
        for name, ps in factor_rank:
            items = "".join(f'<li><b>{esc(p["session_id"])}</b> · {esc(evidence[name][p["index"]])}</li>'
                            for p in ps if evidence[name][p["index"]])
            if items:
                body += f'<h4>{esc(name)}</h4><ul>{items}</ul>'
        if body:
            out.append(fold("Evidence by root cause", body))
        out.append('  </section>')
    elif ranked:
        out += ['  <section class="ss-report-section">',
                heading("DRIVER LOG", "Root causes, ranked", f"{n_checked} session(s) checked")]
        for name, ps in ranked:
            share = 100 * len(ps) / n_checked
            out.append(
                f'    <div class="ss-rank-row"><span class="ss-rank-name">{esc(name)}</span>'
                f'<span class="ss-drips-track"><i class="ss-drips-fill" style="width:{share:.0f}%"></i></span>'
                f'<span class="ss-rank-value"><b>{share:.0f}%</b> · {len(ps)}/{n_checked}</span>'
                f'<span class="ss-rank-sessions">{esc(ids_of(ps))}</span></div>')
        out.append('  </section>')

    # ── Sleep Study vs driver log, side by side ──
    # With nothing checked it still says what the report suspected.
    if points:
        chip = lambda label, cls, n: (f'<span class="ss-state-pill {cls}">{label} {n}</span>' if n else "")
        out += ['  <section class="ss-report-section">',
                heading("CROSS-CHECK", "Sleep Study vs driver log", "what each one can see")]
        for hyp, ps in sorted(hypotheses.items(), key=lambda kv: -len(kv[1])):
            saw = [f'Suspected <b>{esc(hyp or "no clear cause")}</b> in {len(ps)}/{len(points)} session(s)']
            shares = [re.search(r"(\d+)% of ([a-z ]+)\)", str(p.get("html_verdict") or "")) for p in ps]
            shares = [m for m in shares if m]
            app = re.search(r"\(([^)]+)\)", hyp or "")
            if shares and app:
                lo, hi = min(int(m.group(1)) for m in shares), max(int(m.group(1)) for m in shares)
                saw.append(f'{esc(app.group(1))}: {lo}%' + (f'–{hi}%' if hi != lo else '')
                           + f' of {esc(shares[0].group(2))}')
            jobs = {}
            for p in ps:
                for k, v in os_jobs(p.get("wake_evidence") or {}).items():
                    jobs[k] = jobs.get(k, 0) + v
            if jobs:
                saw.append("OS jobs in its wakes: " + esc(", ".join(
                    f"{k} ×{v}" for k, v in sorted(jobs.items(), key=lambda kv: -kv[1])[:4])))
            short = sum(short_device_wakes(p.get("wake_evidence") or {}) for p in ps)
            saw.append(f"Short device wakes: {short}")

            judged_here = [p for p in ps if p["index"] in findings]
            if judged_here:
                adds = []
                for name, hits in factor_rank:
                    here = [p for p in hits if p in judged_here]
                    if here:
                        adds.append(f'<b>{esc(name)}</b>' + (f' ({esc(ident[name])})' if ident[name] else "")
                                    + f' · {len(here)}/{len(judged_here)}')
                for label, hits in fact_hits:
                    here = [p for p in hits if p in judged_here]
                    if here:
                        adds.append(f'{esc(label)} · {len(here)}/{len(judged_here)}')
                counts = {k: sum(1 for p in judged_here if agree_of[p["index"]] == k)
                          for k in ("confirmed", "partly", "refuted", "unclear")}
                verdict = ('<div class="ss-compare-chips">'
                           + chip("Confirmed", "aligned", counts["confirmed"])
                           + chip("Partly", "review", counts["partly"])
                           + chip("Refuted", "review", counts["refuted"])
                           + chip("Unclear", "pending", counts["unclear"]) + '</div>')
                right = ("<ul>" + "".join(f"<li>{a}</li>" for a in adds) + "</ul>" if adds
                         else '<p class="ss-muted">No driver-level cause isolated.</p>') + verdict
            else:
                right = '<p class="ss-muted">Not checked against a driver log.</p>'
            out += ['    <div class="ss-vs">',
                    '      <div class="ss-vs-col"><h4>Sleep Study saw</h4><ul>'
                    + "".join(f"<li>{s}</li>" for s in saw) + '</ul>'
                    '<p class="ss-muted">Which app used the network and which OS jobs joined a wake; '
                    'not the command behind them, how often it fired, or why the driver stayed in D0.</p></div>',
                    f'      <div class="ss-vs-col adds"><h4>Driver log adds</h4>{right}</div>',
                    '    </div>']
        out.append('  </section>')

    # ── What to do next, merged across sessions ──
    if actions or logs_ranked:
        out += ['  <section class="ss-report-section">',
                heading("NEXT STEPS", "Actions", "merged across sessions")]
        for prio, text, name, ps in actions:
            out.append(f'    <div class="ss-action"><span class="ss-prio {prio.lower()}">{prio}</span>'
                       f'<span class="ss-action-text">{esc(text)}</span>'
                       f'<span class="ss-action-sessions">{len(ps)}/{n_checked} · {esc(ids_of(ps))}</span></div>')
        if logs_ranked:
            line, ps = logs_ranked[0]
            out.append(f'    <div class="ss-action"><span class="ss-prio logs">LOGS</span>'
                       f'<span class="ss-action-text">{esc(line)}</span>'
                       f'<span class="ss-action-sessions">{len(ps)}/{n_checked} · {esc(ids_of(ps))}</span></div>')
        body = ""
        for name in ([n for n, _ in factor_rank] + [t[0] for t in FOLLOW_UP_TOPICS] + ["Other"]):
            if recs_by.get(name):
                body += (f'<h4>{esc(name)}</h4><ul>' + "".join(
                    f'<li><b>{esc(p["session_id"])}</b> · {esc(prio)} · {esc(text)}</li>'
                    for p, prio, text in recs_by[name]) + '</ul>')
        if logs_ranked:
            body += ('<h4>Logs needed</h4><ul>' + "".join(
                f'<li>{esc(line)} <span class="ss-muted">({esc(ids_of(ps))})</span></li>'
                for line, ps in logs_ranked[:6]) + '</ul>')
        if body:
            out.append(fold("Each session's recommendations and logs needed", body))
        out.append('  </section>')

    # ── One row per session ──
    if points:
        out += ['  <section class="ss-report-section">',
                heading("SESSIONS", "Per session", "details are in the session cards above"),
                '    <table class="ss-matrix ss-session-table"><thead><tr><th>Session</th><th>SW DRIPS</th>'
                '<th>Driver log root cause</th><th>vs Sleep Study</th></tr></thead><tbody>']
        for p in points:
            if p["index"] in findings:
                agree = agree_of[p["index"]]
                cls = {"confirmed": "aligned", "partly": "review", "refuted": "review"}.get(agree, "pending")
                named = factors_of[p["index"]] or [cause_of[p["index"]]]
                cause_cell = " ".join(f'<span class="ss-cause-pill">{esc(n)}</span>' for n in named[:3])
                agree_cell = f'<span class="ss-state-pill {cls}">{esc(agree)}</span>'
            else:
                cause_cell = '<span class="ss-muted">not checked</span>'
                agree_cell = ''
            out.append(f'      <tr><th>{esc(p["session_id"])} <small>{esc(p.get("device_label"))}</small></th>'
                       f'<td>{esc(_pct(p.get("sw_drip_pct")))}</td><td>{cause_cell}</td><td>{agree_cell}</td></tr>')
        out += ['    </tbody></table>', '  </section>']

    if sessions["groups"]:
        out += ['  <section class="ss-report-section ss-system-section">',
                heading("PLATFORM", f"System blockers ({sessions['system']})", "not a wireless driver issue")]
        out += [f'    <div class="ss-system-row"><b>{esc(g["blocker"])}</b>'
                f'<span>{len(g["sessions"])} session(s)</span>'
                f'<span>lowest SW DRIPS {esc(_pct(g["worst_sw"]))}</span></div>' for g in sessions["groups"]]
        out.append('  </section>')
    out.append('</section>')
    return "\n".join(out)
