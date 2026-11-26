"""
Sleep Study -> driver log pipeline
==================================
A Sleep Study report says WHEN the platform failed to reach low power and
WHICH device kept it awake. A driver log says WHY. This module joins the two:

  1. Read the report once and pick the worst Modern Standby drain sessions
     (software DRIPS below a threshold, a RED top blocker), at most
     MAX_TIME_POINTS of them and at most PER_DEVICE_LIMIT per device family.
  2. Turn each session into a time window: the session's own start..end,
     padded, at most SESSION_TIME_CAP long; its excerpt stops at
     SESSION_LINE_BUDGET lines, so a busy log is read from the session's
     start only as far as the budget goes (covered_until says where).
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
import json
import math
import os
import queue
import re
import shutil
import tempfile
import threading
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
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
# A session is read from its start for as long as its log stays within a line
# budget: the whole session when the driver logged little (an autologger in
# standby logs almost nothing while the device is in D3), only its opening,
# where the entry flow is, when it logged a lot (a verbose WRT log runs to
# tens of thousands of lines an hour). The time cap only guards a log so
# sparse that the window would otherwise run for days; it covers a night.
# The agent never reads the excerpt whole (its skill filters hand it at most
# MAX_ASSEMBLED_LOG_LINES_PER_TOOL_CALL lines a call), so the budget bounds
# the cutting and searching, not the LLM context: 200,000 lines hold about
# 8 h of a WRT log in standby (about 23,000 lines an hour, measured).
SESSION_LINE_BUDGET = 200_000
SESSION_TIME_CAP = timedelta(hours=12)
# A window built from the caller's issue times (ISSUE_TIME_BEFORE / AFTER,
# merged when they are close) stops growing at this length.
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

# A decoded Bluetooth HCI log (.hci.txt, from the WRT BT decoder) stamps its
# lines with the time of day only -- <HH:MM:SS.mmm>, no date anywhere in the
# file. Its segments are therefore indexed dateless and placed on a calendar
# by _anchored(), against the window they are being checked for.
_BT_HCI_TS = re.compile(r"<(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?>")
_BT_HCI_SUFFIX = ".hci.txt"
# The BT decoder Avatar runs on ibtusb / ibtpci ETLs writes a full date on
# every line instead:
#   [0]0000.0000::01/12/2026-11:13:48.087 [ibtusb][UtilHCIDump][EVT] 3E B3 ...
# and in the capture machine's clock -- it takes the time zone recorded in the
# ETL, unlike tracefmt -- so it needs no anchoring and no shift.
_BT_DATED_TS = re.compile(
    r"^﻿?(?:\[\d+\][0-9A-Fa-f]+\.[0-9A-Fa-f]+::)?(\d{2}/\d{2}/\d{4}-\d{2}:\d{2}:\d{2}\.\d{3})\s?")
_DATELESS_DAY = datetime(1900, 1, 1)

# Decoding an ETL takes minutes (PDB download, tracefmt), so at most this many
# are decoded for one time point before it is reported as not covered.
MAX_ETL_DECODES = 3

# A clock that runs backwards by more than this has crossed midnight; anything
# smaller is a thread printing out of order.
_MIDNIGHT_JUMP = timedelta(hours=12)


@dataclass
class LogSegment:
    """One driver log file and the wall-clock span it covers."""
    path: str
    device: str
    kind: str                     # "wrt_wifilog" | "wifi_etl" | "bt_hci"
    start: datetime
    end: Optional[datetime]
    # Where each edge was read from, so a person can check the time a file is
    # said to cover: its own name, the next file's name, or a line inside it.
    start_from: str = ""
    end_from: str = ""
    # True for a log that stamps the time of day but no date (BT HCI). Its
    # start/end sit on _DATELESS_DAY until _anchored() moves them.
    dateless: bool = False
    # The clock the log's timestamps are in, as minutes east of UTC. None is
    # the test machine's own clock, which is also the report's: WRT writes its
    # logs there. A log decoded by tracefmt carries the clock of the machine
    # that decoded it. `clock_guessed` marks a clock nobody recorded, so the
    # coverage check may try the other likely ones (_align).
    clock: Optional[int] = None
    clock_from: str = "test machine"
    clock_guessed: bool = False
    # Added to every timestamp read from the file, to put it on the report's
    # clock (in_report_clock). start/end already include it.
    shift: timedelta = timedelta(0)

    def overlaps(self, lo: datetime, hi: datetime) -> bool:
        return self.start < hi and (self.end is None or self.end > lo)

    def anchored(self, day) -> "LogSegment":
        """This segment placed on `day`. A no-op once it carries a date."""
        if not self.dateless:
            return self
        shift = datetime.combine(day, datetime.min.time()) - _DATELESS_DAY
        return replace(self, start=self.start + shift,
                       end=None if self.end is None else self.end + shift,
                       dateless=False)

    def describe(self) -> dict:
        return {
            "file": os.path.basename(self.path),
            "path": self.path,
            "start": _fmt(self.start),
            "end": _fmt(self.end),
            "start_from": self.start_from,
            "end_from": self.end_from,
            "kind": self.kind,
            "clock": self.clock_label(),
            # start/end are on the report's (customer's) clock; when the log
            # was in another one, its own times are kept for the page to show
            # side by side with the converted ones.
            "shifted": bool(self.shift),
            "shift": _fmt_delta(self.shift) if self.shift else "",
            "log_clock": _fmt_offset(self.clock) if self.clock is not None else "",
            "log_clock_guessed": self.clock_guessed,
            "log_start": _fmt(self.start - self.shift),
            "log_end": _fmt(self.end - self.shift) if self.end is not None else "",
        }

    def clock_label(self) -> str:
        if self.clock is None:
            return "test machine's clock" + (" (assumed)" if self.clock_guessed else "")
        text = f"{_fmt_offset(self.clock)}, {self.clock_from}"
        if self.shift:
            text += f"; shifted {_fmt_delta(self.shift)} to the report's clock"
        return text


def _anchored(segments: list[LogSegment], day) -> list[LogSegment]:
    """
    Every segment on a calendar, dateless ones placed on `day`.

    A BT HCI log knows only the time of day, so the day it belongs to has to
    come from the window it is being checked against. The report and the
    capture are from the same machine, so that window is the only sane guess.
    """
    return [s.anchored(day) for s in segments]


# ── Clocks ─────────────────────────────────────────────────────────────────────
# The report gives every session in UTC and in the test machine's local time.
# A WRT log is written on that machine, so it shares the report's clock. A
# log decoded by tracefmt is stamped in the clock of the machine that decoded
# it: a US report checked against a log decoded in Taiwan is 15 hours apart,
# and would show "not covered" or line up with the wrong session.

# Avatar's decoded Wi-Fi logs are in Taiwan time unless this tool decoded them
# and recorded the clock (utils.timezone_utils works in the same frame).
TAIWAN_UTC_OFFSET = 8 * 60
# Written next to a log this tool decoded: {"utc_offset_minutes": <int>}.
_CLOCK_SIDECAR = ".clock.json"


def _fmt_offset(minutes: Optional[int]) -> str:
    if minutes is None:
        return "test machine's clock"
    sign = "+" if minutes >= 0 else "-"
    return f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def _fmt_delta(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return f"{sign}{abs(minutes) // 60}h{abs(minutes) % 60:02d}m" if minutes % 60 else f"{sign}{abs(minutes) // 60}h"


def local_utc_offset(at: Optional[datetime] = None) -> int:
    """This machine's offset from UTC in minutes, with its DST rules at `at`."""
    when = at or datetime.now()
    return int(when.astimezone().utcoffset().total_seconds() // 60)


def read_clock_sidecar(log_path: str) -> Optional[int]:
    try:
        with open(log_path + _CLOCK_SIDECAR, "r", encoding="utf-8") as fh:
            return int(json.load(fh)["utc_offset_minutes"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_clock_sidecar(log_path: str, minutes: int) -> bool:
    """Record a decoded log's clock, atomically (a reader never sees half a
    file). Written before the log itself is published (decode_wifi_etl)."""
    target = log_path + _CLOCK_SIDECAR
    tmp = target + ".partial"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"utc_offset_minutes": minutes,
                       "note": "clock of the decoded log's timestamps (decoding machine)"}, fh)
        os.replace(tmp, target)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def in_report_clock(segments: list[LogSegment], session_offset: Optional[int],
                    log_clock: Optional[int] = None) -> list[LogSegment]:
    """
    Every segment on the report's clock for a session `session_offset`
    minutes east of UTC. A segment on the test machine's clock is left as it
    is; any other is shifted by the difference. `log_clock` overrides the
    clock of decoded Wi-Fi logs (a person's choice, or a guess being tried).
    """
    out = []
    for s in segments:
        clock = log_clock if (log_clock is not None and s.kind == "wifi_etl") else s.clock
        if clock is None or session_offset is None:
            out.append(s)
            continue
        delta = timedelta(minutes=session_offset - clock)
        out.append(replace(s, start=s.start + delta,
                           end=None if s.end is None else s.end + delta,
                           shift=s.shift + delta, clock=clock,
                           clock_from=s.clock_from if log_clock is None else "tried"))
    return out


def _clock_candidates(segments: list[LogSegment], session_offset: Optional[int]) -> list[int]:
    """The clocks a guessed decoded log was most likely stamped in."""
    tried = {s.clock for s in segments if s.kind == "wifi_etl" and s.clock_guessed}
    if None in tried:                 # the test machine's clock is the report's
        tried.add(session_offset)
    out = []
    for c in (TAIWAN_UTC_OFFSET, local_utc_offset(), 0, session_offset):
        if c is not None and c not in tried and c not in out:
            out.append(c)
    return out


def _align(segments: list[LogSegment], lo: datetime, hi: datetime,
           session_offset: Optional[int], log_clock: Optional[int] = None):
    """
    coverage_of() on the report's clock: (coverage, hits, clock_note).

    A decoded log whose clock was never recorded is first read in its guessed
    clock; when that covers nothing, the other likely clocks are tried, and
    the note says which one lined up -- the person sees the assumption
    instead of a silent "not covered" or a quietly shifted log.
    """
    aligned = in_report_clock(segments, session_offset, log_clock)
    coverage, hits = coverage_of(aligned, lo, hi)
    note = _clock_note(hits, session_offset)
    if coverage != "none" or log_clock is not None or session_offset is None:
        return coverage, hits, note
    if not any(s.kind == "wifi_etl" and s.clock_guessed for s in segments):
        return coverage, hits, note
    for clock in _clock_candidates(segments, session_offset):
        coverage, hits = coverage_of(in_report_clock(segments, session_offset, clock), lo, hi)
        if coverage != "none":
            return coverage, hits, (
                f"Lined up only when the decoded log is read in {_fmt_offset(clock)} (the report is "
                f"{_fmt_offset(session_offset)}); its time zone was not recorded, so check it was "
                f"decoded in {_fmt_offset(clock)}.")
    return "none", [], ""


def _clock_note(hits: list[LogSegment], session_offset: Optional[int]) -> str:
    shifted = [h for h in hits if h.shift]
    if not shifted:
        return ""
    h = shifted[0]
    guess = " (assumed: not recorded)" if h.clock_guessed else ""
    return (f"Decoded log is in {_fmt_offset(h.clock)}{guess}; the report is "
            f"{_fmt_offset(session_offset)}, so its times were shifted {_fmt_delta(h.shift)}.")


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


def _is_bt_hci(name: str) -> bool:
    return name.lower().endswith(_BT_HCI_SUFFIX)


# The Wi-Fi driver ETL Avatar's decoder reads (wpp_ddd_parser.PARSERS_DICT:
# "WifiDriver.*\.etl"), the autologger trace customers send; .etl.N are its
# rolling parts. tracefmt writes the decoded text next to it as <etl>.log.
_WIFI_DRIVER_ETL = re.compile(r"WifiDriver.*\.etl(?:\.\d+)?$")
_DECODED_ETL_LOG = re.compile(r"\.etl(?:\.\d+)?\.log$", re.IGNORECASE)
# Indexed under this key instead of a device: an ETL has no time span until
# it is decoded, so it is a candidate to decode, not a log to cut.
RAW_ETL_KEY = "wifi_etl_raw"
# The ETLs a run could not decode, {path: (segment, why)}: not tried again,
# and the reason is given to every later point they might have held.
FAILED_ETL_KEY = "wifi_etl_failed"


def _is_wifi_driver_etl(name: str) -> bool:
    return bool(_WIFI_DRIVER_ETL.search(name))


def _capture_time(path: str) -> Optional[datetime]:
    """The capture time in an autologger / WRT folder name
    (<host>_DD-MM-YYYY_HH-MM-SS_...), on the test machine's clock: the time
    the capture was collected, so it contains what happened before it. The
    nearest dated folder up the path counts: autologger keeps older parts in
    a WifiDriverIHVSession_History folder inside the capture."""
    folder = os.path.dirname(path)
    for _ in range(3):
        m = re.search(r"(\d{2})-(\d{2})-(\d{4})_(\d{2})-(\d{2})-(\d{2})", os.path.basename(folder))
        if m:
            dd, mo, yy, hh, mi, ss = (int(g) for g in m.groups())
            try:
                return datetime(yy, mo, dd, hh, mi, ss)
            except ValueError:
                return None
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return None


def _decoded_log_segment(path: str, alone: bool) -> Optional[LogSegment]:
    """A decoded Wi-Fi ETL log, with the clock its timestamps are in: the one
    this tool recorded when it decoded it, else a guess -- Taiwan time for an
    Avatar-decoded <etl>.log, the test machine's for any other text log."""
    first, last = _wifi_etl_range(path)
    if first is None:
        return None
    clock = read_clock_sidecar(path)
    if clock is not None:
        return LogSegment(path, ssa.DEVICE_WIFI, "wifi_etl", first, last,
                          "first timestamped line", "last timestamped line",
                          clock=clock, clock_from="recorded when this tool decoded it")
    if _DECODED_ETL_LOG.search(path):
        return LogSegment(path, ssa.DEVICE_WIFI, "wifi_etl", first, last,
                          "first timestamped line", "last timestamped line",
                          clock=TAIWAN_UTC_OFFSET, clock_from="Avatar's decode clock",
                          clock_guessed=True)
    return LogSegment(path, ssa.DEVICE_WIFI, "wifi_etl", first, last,
                      "first timestamped line", "last timestamped line",
                      clock_from="not recorded", clock_guessed=alone)


def _bt_hci_clock(line: str) -> Optional[datetime]:
    """The <HH:MM:SS.mmm> of one HCI line, on _DATELESS_DAY."""
    m = _BT_HCI_TS.search(line)
    if not m:
        return None
    hh, mi, ss = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if hh > 23 or mi > 59 or ss > 59:
        return None
    ms = int((m.group(4) or "0").ljust(3, "0")[:3])
    return _DATELESS_DAY.replace(hour=hh, minute=mi, second=ss, microsecond=ms * 1000)


def _bt_dated_range(path: str) -> tuple[Optional[datetime], Optional[datetime]]:
    """First and last timestamp of a dated BT HCI log, or (None, None) when
    its opening lines carry no date (the older <HH:MM:SS> shape)."""
    first = last = None
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            m = _BT_DATED_TS.match(line)
            if m:
                first = datetime.strptime(m.group(1), _WIFI_ETL_FMT)
                break
            if i >= 200:
                return None, None
    if first is None:
        return None, None
    for line in reversed(_tail_lines(path)):
        m = _BT_DATED_TS.match(line)
        if m:
            last = datetime.strptime(m.group(1), _WIFI_ETL_FMT)
            break
    return first, last


def _bt_hci_range(path: str) -> tuple[Optional[datetime], Optional[datetime]]:
    """
    First and last clock of a .hci.txt, both on _DATELESS_DAY.

    A capture that ends earlier than it started has crossed one midnight; a
    BT decode spans hours, not days, so one is the only correction needed.
    """
    first = last = None
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            first = _bt_hci_clock(line)
            if first is not None:
                break
    if first is None:
        return None, None
    for line in reversed(_tail_lines(path)):
        last = _bt_hci_clock(line)
        if last is not None:
            break
    if last is not None and last < first:
        last += timedelta(days=1)
    return first, last


def discover_driver_logs(path: str) -> tuple[dict[str, list[LogSegment]], list[str]]:
    """
    Index the driver logs under `path` by device family.

    `path` is a WRT capture folder or a single log file. Recognised today:
      - WRT rolling Wi-Fi logs (WiFiLog--*.LOG), anywhere under the folder;
        a single WiFiLog file means its whole folder
      - a decoded Wi-Fi ETL log (MM/DD/YYYY-HH:MM:SS.mmm lines)
      - a decoded Bluetooth HCI log (*.hci.txt): Avatar's BT decoder dates
        every line (MM/DD/YYYY-HH:MM:SS.mmm, the capture machine's clock);
        the older <HH:MM:SS> shape is indexed dateless and _anchored() puts
        it on the window's day
      - Wi-Fi driver ETLs (WifiDriver*.etl[.N]): an already decoded
        <etl>.log next to one is indexed as a decoded log; an undecoded one
        goes under RAW_ETL_KEY, a candidate for decode_wifi_etl()

    Returns ({device: [segments, oldest first], RAW_ETL_KEY: [...]}, warnings).
    """
    warnings: list[str] = []
    found: dict[str, list[LogSegment]] = {}
    if not path or not os.path.exists(path):
        return found, [f"Driver log not found: {path}"]

    # One WiFiLog or Wi-Fi driver ETL picked in a file dialog stands for the
    # capture it came from: a session usually falls in one of its siblings
    # (another rolling file, or the ETL's decoded log), not in that file.
    if os.path.isfile(path) and (_wifilog_start(os.path.basename(path)) is not None
                                 or _is_wifi_driver_etl(os.path.basename(path))):
        path = os.path.dirname(path)

    if os.path.isdir(path):
        files = [str(p) for p in Path(path).rglob("*") if p.is_file()]
    else:
        files = [path]

    wifilogs = []
    for f in files:
        name = os.path.basename(f)
        start = _wifilog_start(name)
        if start is not None:
            wifilogs.append((start, f))
            continue
        if _is_wifi_driver_etl(name):
            # Decoded already: its <etl>.log is indexed on its own; one this
            # tool decoded into the cache (a read-only folder) is indexed here.
            cached = None if os.path.exists(f + ".log") else decoded_log_of(f)
            if cached:
                seg = _decoded_log_segment(cached, alone=False)
                if seg is not None:
                    found.setdefault(ssa.DEVICE_WIFI, []).append(seg)
            elif not os.path.exists(f + ".log"):
                captured = _capture_time(f)
                found.setdefault(RAW_ETL_KEY, []).append(
                    LogSegment(f, ssa.DEVICE_WIFI, RAW_ETL_KEY,
                               captured or datetime.fromtimestamp(os.path.getmtime(f)), None,
                               "unknown until decoded",
                               "capture folder name" if captured else "file modified time"))
            continue
        if _DECODED_ETL_LOG.search(name):
            seg = _decoded_log_segment(f, alone=not os.path.isdir(path))
            if seg is None:
                warnings.append(f"{name}: no timestamp this tool can read.")
            else:
                found.setdefault(ssa.DEVICE_WIFI, []).append(seg)
            continue
        if _is_bt_hci(os.path.basename(f)):
            # A decoded BT log sits next to the Wi-Fi ones in a WRT capture,
            # so it is picked up from a folder as well as when chosen alone.
            first, last = _bt_dated_range(f)
            if first is not None:
                found.setdefault(ssa.DEVICE_BT, []).append(
                    LogSegment(f, ssa.DEVICE_BT, "bt_hci_dated", first, last,
                               "first timestamped line", "last timestamped line",
                               clock_from="capture machine (the BT decoder uses the ETL's time zone)"))
                continue
            first, last = _bt_hci_range(f)
            if first is None:
                warnings.append(
                    f"{os.path.basename(f)}: no timestamp this tool can read "
                    "(expected MM/DD/YYYY-HH:MM:SS.mmm or <HH:MM:SS> lines).")
                continue
            found.setdefault(ssa.DEVICE_BT, []).append(
                LogSegment(f, ssa.DEVICE_BT, "bt_hci", first, last,
                           "first timestamped line", "last timestamped line",
                           dateless=True))
            continue
        if not os.path.isdir(path):
            # A single file that is neither must be a decoded Wi-Fi ETL log.
            seg = _decoded_log_segment(f, alone=True)
            if seg is None:
                warnings.append(
                    f"{os.path.basename(f)}: no timestamp this tool can read; expected a "
                    "WRT WiFiLog, a Wi-Fi driver ETL or its decoded log, or a decoded BT *.hci.txt.")
                continue
            found.setdefault(ssa.DEVICE_WIFI, []).append(seg)

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
        warnings.append(f"No WiFiLog--*.LOG, WifiDriver*.etl or *.hci.txt files under {path}.")
    for segments in found.values():
        segments.sort(key=lambda s: s.start)
    return found, warnings


def coverage_of(segments: list[LogSegment], lo: datetime, hi: datetime) -> tuple[str, list[LogSegment]]:
    """'full', 'partial' or 'none', and the segments that overlap [lo, hi)."""
    segments = _anchored(segments, lo.date())
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


def _decoded_copy_path(etl_path: str) -> str:
    """Where an ETL's decoded log goes when its own folder is read-only (a
    share): a per-file cache, so later points and runs find it again."""
    try:
        st = os.stat(etl_path)
        stamp = f"{os.path.abspath(etl_path).lower()}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        stamp = os.path.abspath(etl_path).lower()
    import hashlib
    digest = hashlib.sha1(stamp.encode("utf-8", "replace")).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), "sleepstudy_etl_decoded", digest,
                        os.path.basename(etl_path) + ".log")


def decoded_log_of(etl_path: str) -> Optional[str]:
    """The ETL's decoded log, next to it or in the cache, if it exists."""
    for candidate in (etl_path + ".log", _decoded_copy_path(etl_path)):
        if os.path.exists(candidate):
            return candidate
    return None


def decode_wifi_etl(etl_path: str) -> tuple[Optional[str], str]:
    """
    Decode a Wi-Fi driver ETL with Avatar's Wi-Fi decoder (tracefmt with the
    PDBs of the driver build the ETL names), and return (decoded log, error).

    The decoder writes <etl>.log next to the ETL; when it cannot (a read-only
    share) the text is copied from its workspace into a cache instead. The
    decoding machine's UTC offset is recorded beside the log, since tracefmt
    stamps every line in that machine's local time.
    """
    done = decoded_log_of(etl_path)
    if done:
        return done, ""
    try:
        from services.etl_parser.wpp_ddd_parser import DECODE_LOCK, WppParser, parse_single_binary
        from utils.helpers import to_long_path
    except Exception as exc:          # noqa: BLE001 -- reported to the user
        return None, f"Avatar's Wi-Fi ETL decoder is unavailable here ({type(exc).__name__}: {exc})."

    parser = None
    # The decoder chdir()s the whole process into its workspace and works
    # with relative paths, so decodes run one at a time across every entry
    # point (DECODE_LOCK), and the directory is put back before the lock is
    # released.
    with DECODE_LOCK:
        done = decoded_log_of(etl_path)       # decoded while this one waited
        if done:
            return done, ""
        previous_headless = os.environ.get("AVATAR_HEADLESS_DECODE")
        # One decode per ETL must not open a TextAnalysisTool and an Explorer
        # window each, as the interactive Wi-Fi flow does.
        os.environ["AVATAR_HEADLESS_DECODE"] = "1"
        cwd = os.getcwd()
        try:
            parser = WppParser(to_long_path(etl_path), False, False)
            # The parser would copy its output straight to <etl>.log, where
            # discovery and other points take any file of that name as a
            # finished decode. It is published below instead, atomically.
            parser.copy_parsed_log_to_orig_path = lambda: None
            parse_single_binary(parser)
        except Exception as exc:          # noqa: BLE001 -- reported to the user
            return None, f"Decoding {os.path.basename(etl_path)} failed: {type(exc).__name__}: {exc}"
        finally:
            if previous_headless is None:
                os.environ.pop("AVATAR_HEADLESS_DECODE", None)
            else:
                os.environ["AVATAR_HEADLESS_DECODE"] = previous_headless
            try:
                os.chdir(cwd)
            except OSError:
                pass

    parsed = os.path.join(parser.workspace, parser.parsed_txt_name) if parser else ""
    if not parsed or not os.path.exists(parsed):
        return None, f"Decoding {os.path.basename(etl_path)} produced no log."
    # Published whole: the text goes to a .partial name, its clock record is
    # written, and only then is it renamed to the name readers accept. Next
    # to the ETL when that folder is writable, else in the cache.
    clock = local_utc_offset()
    for log_path in (etl_path + ".log", _decoded_copy_path(etl_path)):
        tmp = log_path + ".partial"
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            shutil.copyfile(parsed, tmp)
            if not write_clock_sidecar(log_path, clock):
                raise OSError("could not record the decoded log's clock")
            os.replace(tmp, log_path)
            return log_path, ""
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return None, f"Decoding {os.path.basename(etl_path)} worked, but the decoded log could not be written."


def etl_candidates(raw: list[LogSegment], lo: datetime, hi: datetime,
                   start: Optional[datetime] = None,
                   end: Optional[datetime] = None) -> list[LogSegment]:
    """
    The undecoded Wi-Fi driver ETLs that may hold the session in window
    [lo, hi), most likely first. Judged against the session itself
    (session_span: the window minus its padding, or `start`/`end`), since a
    capture holds what happened before it was collected:
      - collected before the session started: cannot hold it, dropped
      - collected after it ended: can hold all of it; the soonest after is
        the likeliest (the rule Avatar's issue-time pick uses)
      - collected while it ran: holds at most its start; the latest first
      - no capture time in the folder name: last
    Within one capture, its rolling part last written soonest after the window.
    """
    s_lo, s_hi = session_span(lo, hi, start, end)

    def key(s: LogSegment):
        captured = s.start if s.end_from == "capture folder name" else None
        written = datetime.fromtimestamp(os.path.getmtime(s.path)) if os.path.exists(s.path) else s.start
        if captured is None:
            group, when = 2, 0.0
        elif captured >= s_hi:
            group, when = 0, captured.timestamp()
        else:
            group, when = 1, -captured.timestamp()
        return (group, when, written < hi, written)
    keep = [s for s in raw if not (s.end_from == "capture folder name" and s.start < s_lo)]
    return sorted(keep, key=key)


def session_span(lo: datetime, hi: datetime, start: Optional[datetime] = None,
                 end: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """The session itself inside its window: the window minus WINDOW_PAD on
    each side, unless the session's own start/end are known."""
    s_lo = start or (lo + WINDOW_PAD)
    s_hi = end or (hi - WINDOW_PAD)
    return (s_lo, s_hi) if s_hi > s_lo else (lo, hi)


def covers_session(hits: list[LogSegment], s_lo: datetime, s_hi: datetime) -> bool:
    """Whether any overlapping log reaches the session itself, not only the
    margin padded around it (a log that ends as standby begins covers the
    window "partially" yet holds nothing of the standby)."""
    return any(h.overlaps(s_lo, s_hi) for h in hits)


def outside_session_note(hits: list[LogSegment], s_lo: datetime, s_hi: datetime) -> str:
    first = min(h.start for h in hits)
    ends = [h.end for h in hits if h.end is not None]
    last = max(ends) if ends else None
    session = f"the session ({_fmt(s_lo)} → {_fmt(s_hi)[11:]})"
    if last is not None and last <= s_lo:
        return (f"The driver log ends at {_fmt(last)}, before {session} starts: only the "
                "margin before it is logged, nothing of the standby itself.")
    if first >= s_hi:
        return (f"The driver log starts at {_fmt(first)}, after {session} ended: only the "
                "margin after it is logged, nothing of the standby itself.")
    return f"The driver log does not reach {session}, only the margin around it."


def check_driver_log(path: str, device: str, window_start: str, window_end: str,
                     utc_offset: Optional[int] = None, log_clock: Optional[int] = None,
                     session_start: str = "", session_end: str = "") -> dict:
    """
    Whether one driver log covers a time point's window, and what time span
    each overlapping file covers and where that span was read from. For the
    page to show under a path the user typed or picked. `utc_offset` is the
    session's (TimePoint.utc_offset_minutes): logs in another clock are
    compared on the report's. With no decoded log covering a Wi-Fi window,
    the Wi-Fi driver ETLs that may hold it are listed ("etl").
    """
    lo = datetime.strptime(window_start, "%m/%d/%Y %H:%M:%S")
    hi = datetime.strptime(window_end, "%m/%d/%Y %H:%M:%S")

    def parsed(value):
        try:
            return datetime.strptime(value, "%m/%d/%Y %H:%M:%S") if value else None
        except ValueError:
            return None
    # The session's own start/end, as the plan and the run use them: a window
    # capped at SESSION_TIME_CAP no longer ends WINDOW_PAD after the session.
    start, end = parsed(session_start), parsed(session_end)
    index, warnings = discover_driver_logs(path)
    segments = _anchored(index.get(device, []), lo.date())
    coverage, hits, note = _align(segments, lo, hi, utc_offset, log_clock)
    # A log that reaches only the margin padded around the session holds
    # nothing of the standby: reported as not covering it, with why.
    gap = ""
    s_lo, s_hi = session_span(lo, hi, start, end)
    if hits and not covers_session(hits, s_lo, s_hi):
        gap = outside_session_note(hits, s_lo, s_hi)
        coverage, hits = "none", []
    etls = []
    if coverage == "none" and device == ssa.DEVICE_WIFI:
        etls = etl_candidates(index.get(RAW_ETL_KEY, []), lo, hi, start, end)
        if etls:
            coverage = "etl"
    shown = in_report_clock(segments, utc_offset, log_clock)
    return {
        "coverage": coverage,
        "files": [s.describe() for s in hits],
        "etl_candidates": [{"file": os.path.basename(s.path), "path": s.path,
                            "captured": _fmt(s.start), "captured_from": s.end_from}
                           for s in etls[:MAX_ETL_DECODES]],
        "clock_note": note,
        "gap_note": gap,
        # When nothing overlaps, the span the log does cover says how far off it is.
        "log_span": ({"start": _fmt(shown[0].start), "end": _fmt(shown[-1].end)}
                     if shown else None),
        "warnings": warnings if not etls else [],
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


def _iter_bt_hci(seg: LogSegment) -> Iterator[tuple[Optional[datetime], str]]:
    """
    Yield (timestamp, message) for each line of a decoded BT HCI log.

    The file has no date of its own, so the day comes from the segment as
    _anchored() placed it, and is carried across midnight the same way the
    WiFiLog reader does.
    """
    day = seg.start.date()
    prev: Optional[datetime] = None
    with open(seg.path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\r\n")
            m = _BT_HCI_TS.search(line)
            clock = _bt_hci_clock(line) if m else None
            if clock is None:
                yield None, line
                continue
            dt = datetime.combine(day, clock.time())
            if prev is None and dt > seg.start + _MIDNIGHT_JUMP:
                dt -= timedelta(days=1)
                day = dt.date()
            elif prev is not None and dt < prev - _MIDNIGHT_JUMP:
                dt += timedelta(days=1)
                day = dt.date()
            prev = dt
            yield dt, (line[:m.start()] + line[m.end():]).strip(" -:|\t")


def _iter_bt_dated(seg: LogSegment) -> Iterator[tuple[Optional[datetime], str]]:
    """Yield (timestamp, message) for each line of a dated BT HCI log; the
    "[cpu]pid.tid::" prefix and the timestamp are dropped from the message."""
    with open(seg.path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\r\n")
            m = _BT_DATED_TS.match(line)
            if not m:
                yield None, line
                continue
            yield datetime.strptime(m.group(1), _WIFI_ETL_FMT), line[m.end():]


def write_excerpt(segments: list[LogSegment], lo: datetime, hi: datetime, out_path: str,
                  max_lines: Optional[int] = None) -> dict:
    """
    Copy every line logged in [lo, hi] into `out_path`, written in the decoded
    Wi-Fi ETL shape (MM/DD/YYYY-HH:MM:SS.mmm message) so the agent reads it as
    a dated log and never has to guess which day a clock belongs to. With
    `max_lines`, the copy stops at that many lines: "truncated" is then True
    and "last" is the time of the last line kept, how far the excerpt reaches.
    """
    kept = 0
    first = last = None
    sources = []
    truncated = False
    readers = {"wrt_wifilog": _iter_wifilog, "bt_hci": _iter_bt_hci, "bt_hci_dated": _iter_bt_dated}
    with open(out_path, "w", encoding="utf-8", newline="\n") as out:
        for seg in sorted(_anchored(segments, lo.date()), key=lambda s: s.start):
            reader = readers.get(seg.kind, _iter_wifi_etl)
            in_window = False
            seg_kept = 0
            for dt, msg in reader(seg):
                if dt is None:
                    # A wrapped line belongs to whatever came before it.
                    if in_window and msg:
                        out.write(msg + "\n")
                    continue
                # A log in another clock is cut and written on the report's,
                # so the agent's timestamps match the Sleep Study's.
                dt = dt + seg.shift
                if dt > hi:
                    break
                in_window = dt >= lo
                if not in_window:
                    continue
                if max_lines is not None and kept >= max_lines:
                    truncated = True
                    break
                out.write(f"{dt.strftime('%m/%d/%Y-%H:%M:%S')}.{dt.microsecond // 1000:03d} {msg}\n")
                kept += 1
                seg_kept += 1
                first = first or dt
                last = dt
            if seg_kept:
                sources.append(seg.path)
            if truncated:
                break
    return {"lines": kept, "first": first, "last": last, "sources": sources, "truncated": truncated}


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
    # How far the analysis reads, and why it stops before the window's end:
    # "time_cap" (the session outlasts SESSION_TIME_CAP), "line_budget" (its
    # log outruns SESSION_LINE_BUDGET), or "" when it reads the whole window.
    covered_until: Optional[datetime] = None
    stop_reason: str = ""
    # The session's local time minus UTC, from the report: start/end and the
    # window are on this clock, and a log on another is shifted onto it.
    utc_offset_minutes: Optional[int] = None
    log_clock_note: str = ""
    # Set by a caller of run_pipeline() (the command line): the window it
    # asked for instead of the report's, and its own question to the agent.
    window_source: str = "report"
    caller_question: str = ""
    # How the point was chosen: "report" (a bad wireless session), "forced"
    # (a report session the caller asked for although it is not one), or
    # "window" (a window of the caller's, with the session it falls in if any).
    selection: str = "report"
    # The caller's own findings for this standby (e.g. another team's ETW
    # analysis) and its name for the device (an ACPI path), for the agent.
    caller_evidence: list = field(default_factory=list)
    device_id: str = ""
    # The issue times the caller reported inside this point's window.
    issue_times: list = field(default_factory=list)
    # Why each Wi-Fi driver ETL tried for this point could not be decoded.
    etl_decode_errors: list = field(default_factory=list)
    status: str = ""
    coverage: str = ""
    message: str = ""
    sleep_summary: str = ""
    wake_evidence: dict = field(default_factory=dict)   # _wake_evidence()
    issue_description: str = ""
    driver_log_path: str = ""     # suggested by plan(), confirmed by the user
    driver_log_files: list = field(default_factory=list)   # LogSegment.describe() of the hits
    # The Wi-Fi driver ETLs the plan would decode for this point, likeliest
    # first (shown on the page; the run looks them up again under the path).
    etl_candidates: list = field(default_factory=list)
    excerpt_path: str = ""
    excerpt_lines: int = 0
    driver_log_sources: list = field(default_factory=list)
    agent_result_type: str = ""
    agent_report: str = ""
    agent_result: dict = field(default_factory=dict)   # agent.chat()'s return value
    agent_usage: dict = field(default_factory=dict)

    @property
    def read_end(self) -> Optional[datetime]:
        """The end of what the analysis reads: the window's, or earlier where
        the line budget stopped the excerpt."""
        if self.window_end is None or self.covered_until is None:
            return self.window_end
        return min(self.window_end, self.covered_until)

    @property
    def issue_time(self) -> Optional[datetime]:
        if self.window_start is None or self.read_end is None:
            return self.start
        return self.window_start + (self.read_end - self.window_start) / 2

    @property
    def half_window_minutes(self) -> int:
        if self.window_start is None or self.read_end is None:
            return 5
        return max(1, math.ceil((self.read_end - self.window_start).total_seconds() / 120))

    def to_dict(self) -> dict:
        d = asdict(self)
        for key in ("start", "end", "window_start", "window_end", "covered_until"):
            d[key] = _fmt(d[key])
        d["issue_time"] = _fmt(self.issue_time)
        d["issue_times"] = [_fmt(t) for t in self.issue_times]
        # The clock every time of this point is shown in: the customer
        # machine's, as the report records it.
        d["utc_offset_label"] = (_fmt_offset(self.utc_offset_minutes)
                                 if self.utc_offset_minutes is not None else "")
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
        set_session_window(p)
        points.append(p)
    return points


def set_session_window(p: TimePoint) -> None:
    """A session's window: its start..end padded by WINDOW_PAD, at most
    SESSION_TIME_CAP from the start. Its excerpt is held to
    SESSION_LINE_BUDGET lines when it is cut (_analyze_point)."""
    if p.start is None:
        return
    lo = p.start - WINDOW_PAD
    hi = (p.end or p.start) + WINDOW_PAD
    p.window_truncated = hi - lo > SESSION_TIME_CAP
    if p.window_truncated:
        hi = lo + SESSION_TIME_CAP
    p.window_start, p.window_end = lo, hi
    p.covered_until = hi
    p.stop_reason = "time_cap" if p.window_truncated else ""


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
    asked = ""
    if point.caller_question:
        # A caller's own focus (the command line's issue_description). It
        # steers the analysis; the evidence above and the contract still hold.
        asked = f"\n\nThe caller also asks about this session: {point.caller_question.strip()}"
    if point.window_source == "caller":
        asked += (f"\nThe driver log was cut to the window the caller chose: "
                  f"{_fmt(point.window_start)} -> {_fmt(point.window_end)}.")
    if point.issue_times:
        asked += ("\nThe caller reported the issue at: "
                  + ", ".join(_fmt(t) for t in point.issue_times) + ".")
    if point.selection == "forced":
        asked += ("\nThe caller asked for this session although it does not meet the bad-session "
                  "criteria above; judge from the driver log whether the device behaved normally.")
    elif point.selection == "window" and not point.blockers and point.sw_drip_pct is None:
        asked += ("\nNo Sleep Study session in the report covers this window, so the Sleep Study "
                  "values above are not available; rely on the caller's evidence and the log.")
    if point.device_id:
        asked += f"\nThe caller identifies the device as {point.device_id}."
    if point.caller_evidence:
        asked += ("\n\nEvidence from the caller's own analysis of this standby (for example a "
                  "system-level ETW trace). Explain it from the driver log: say what in the log "
                  "accounts for it, or where the log contradicts it. Do not restate it as a "
                  "finding the log does not support.\n"
                  + "\n".join(f"  - {line}" for line in point.caller_evidence))
    return _fill(text, values) + asked + "\n\n" + _fill(POINT_ANSWER_CONTRACT, values)


def coverage_text(p: dict) -> str:
    """'23:05:12 of a session to 03:14:06 (line budget: 200,000 lines)' for a
    point the analysis did not read to the end; "" when it read it all."""
    reason = p.get("stop_reason")
    if not reason:
        return ""
    why = (f"line budget: {SESSION_LINE_BUDGET:,} lines" if reason == "line_budget"
           else f"{SESSION_TIME_CAP.total_seconds() / 3600:.0f} h cap")
    until = p.get("covered_until") or p.get("window_end") or ""
    return f"{until} of a session to {(p.get('end') or '')[11:] or '?'} ({why})"


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
        f'**Window:** {esc(p.get("window_start"))} → {esc(p.get("window_end"))}'
        + (f' ({esc(p.get("utc_offset_label"))}, customer clock)' if p.get("utc_offset_label") else "") + '  ',
        *([f'**Analysed to:** {esc(coverage_text(p))}  '] if p.get("stop_reason") else []),
        f'**Driver log:** `{esc(driver_files)}`  ',
        *([f'**Time zone:** {esc(p.get("log_clock_note"))}  '] if p.get("log_clock_note") else []),
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
        "Issue time: " + (", ".join(_fmt(t) for t in point.issue_times) + " (reported by the caller)"
                          if point.issue_times else f"around {_fmt(point.issue_time)}")
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
    report_offset = (payload.get("ReportInformation") or {}).get("UtcOffset")
    for p in points:
        p.sleep_summary = _sleep_summary(p, raw_by_id.get(p.session_id))
        p.wake_evidence = _wake_evidence(raw_by_id.get(p.session_id), p.device)
        p.utc_offset_minutes = session_utc_offset(raw_by_id.get(p.session_id), report_offset)
    return collected, points, _system_sessions(payload, drip_threshold)


def session_utc_offset(raw: Optional[dict], report_offset=None) -> Optional[int]:
    """
    A session's local time minus UTC, in minutes. The report stamps every
    session both ways (EntryTimestampLocal / EntryTimestamp), which also
    follows a DST change inside the report; its UtcOffset (taken when the
    report was generated) is the fallback.
    """
    if raw:
        local = ssa.parse_local_timestamp(raw.get("EntryTimestampLocal") or "")
        utc = ssa.parse_local_timestamp(raw.get("EntryTimestamp") or "")
        if local is not None and utc is not None:
            return int(round((local - utc).total_seconds() / 60))
    try:
        return int(report_offset) if report_offset is not None else None
    except (TypeError, ValueError):
        return None


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
        "session_line_budget": SESSION_LINE_BUDGET,
        "session_time_cap_hours": SESSION_TIME_CAP.total_seconds() / 3600,
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
            p.coverage, hits, p.log_clock_note = _align(
                log_index.get(p.device, []), p.window_start, p.window_end, p.utc_offset_minutes)
            s_lo, s_hi = session_span(p.window_start, p.window_end, p.start, p.end)
            margin_only = bool(hits) and not covers_session(hits, s_lo, s_hi)
            if margin_only:
                gap = outside_session_note(hits, s_lo, s_hi)
                hits, p.coverage = [], "none"
            etls = (etl_candidates(log_index.get(RAW_ETL_KEY, []), p.window_start, p.window_end,
                                   p.start, p.end)
                    if not hits and p.device == ssa.DEVICE_WIFI else [])
            if hits:
                p.driver_log_path = hits[0].path
                p.driver_log_files = [s.describe() for s in hits]
            elif etls:
                # Decoded when the point runs: decoding takes minutes, and the
                # plan is the quick step where a person checks the picks. The
                # path is the folder searched, not the first ETL, so the run
                # can still fall back to the candidates in sibling captures.
                p.coverage = "etl"
                p.driver_log_path = search_root
                p.etl_candidates = [{"file": os.path.basename(s.path), "path": s.path,
                                     "captured": _fmt(s.start), "captured_from": s.end_from}
                                    for s in etls[:MAX_ETL_DECODES]]
                p.message = (f"Wi-Fi driver ETL {os.path.basename(etls[0].path)} is decoded "
                             "when this point is analysed.")
            else:
                p.status = STATUS_NO_LOG
                p.message = (f"No {DEVICE_LABELS[p.device]} driver log under the report's "
                             "folder covers this window; choose one.")
                if margin_only:
                    p.message = f"{gap} Choose a driver log that covers the standby."
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


# Decodes in progress, by ETL path. A decode cannot be interrupted (share
# copies, tracefmt), so a run that is paused or whose page goes away leaves it
# running; the run that resumes waits for that decode instead of starting a
# second one of the same file.
_DECODES_IN_FLIGHT: dict = {}
_DECODES_LOCK = threading.Lock()


def _decode_steps(etl_path: str, head: str):
    """decode_wifi_etl() on a worker thread, yielding a progress step every
    20 s so a page following the stream sees it is still working. A decode of
    the same ETL already running is joined, not repeated. Returns
    (decoded log, error) through `yield from`."""
    key = os.path.normcase(os.path.abspath(etl_path))
    with _DECODES_LOCK:
        job = _DECODES_IN_FLIGHT.get(key)
        joined = job is not None
        if job is None:
            job = {"done": threading.Event(), "value": None}
            _DECODES_IN_FLIGHT[key] = job

            def work():
                try:
                    job["value"] = decode_wifi_etl(etl_path)
                except Exception as exc:      # noqa: BLE001 -- reported to the user
                    job["value"] = (None, f"{type(exc).__name__}: {exc}")
                finally:
                    with _DECODES_LOCK:
                        _DECODES_IN_FLIGHT.pop(key, None)
                    job["done"].set()

            threading.Thread(target=work, daemon=True).start()
    if joined:
        yield {"type": "step", "content": (
            f"{head}: `{os.path.basename(etl_path)}` is already being decoded "
            "(an earlier run of this point); waiting for it…")}
    waited = 0
    while not job["done"].wait(20):
        waited += 20
        yield {"type": "step", "content": (
            f"{head}: still decoding `{os.path.basename(etl_path)}` ({waited} s)…")}
    return job["value"] or (None, "The decoder stopped without a result.")


def _analyze_point(p: TimePoint, indexed: dict, log_warnings: list, out_dir: str,
                   agent_factory, max_steps: int, on_agent=None,
                   log_clock: Optional[int] = None) -> Iterator[dict]:
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
        index = indexed[p.driver_log_path]
        segments = index.get(p.device, [])
        p.coverage, hits, p.log_clock_note = _align(
            segments, p.window_start, p.window_end, p.utc_offset_minutes, log_clock)
        # What a log has to reach: the session itself, or all of a window a
        # caller chose (it has no padding to discount).
        span = ((p.window_start, p.window_end) if p.window_source == "caller"
                else (p.start, p.end))
        s_lo, s_hi = session_span(p.window_start, p.window_end, *span)
        reaches = lambda hs: covers_session(hs, s_lo, s_hi)
        # No decoded log reaches the session (none at all, or one covering
        # only the margin around it): decode the Wi-Fi driver ETLs that may,
        # likeliest first, until one does.
        decode_errors = []
        raw_etls = index.get(RAW_ETL_KEY, []) if p.device == ssa.DEVICE_WIFI else []
        candidates = (etl_candidates(raw_etls, p.window_start, p.window_end, *span)
                      if raw_etls else [])
        failed = index.get(FAILED_ETL_KEY, {}) if p.device == ssa.DEVICE_WIFI else {}
        if not reaches(hits) and failed:
            decode_errors += [failed[s.path][1] for s in etl_candidates(
                [seg for seg, _ in failed.values()], p.window_start, p.window_end, *span)]
        if not reaches(hits) and candidates:
            for etl in candidates[:MAX_ETL_DECODES]:
                name = os.path.basename(etl.path)
                yield {"type": "step", "content": (
                    f"{head}: decoding Wi-Fi driver ETL `{name}` (captured {_fmt(etl.start)})…")}
                log_path, error = yield from _decode_steps(etl.path, head)
                index[RAW_ETL_KEY] = [s for s in index.get(RAW_ETL_KEY, []) if s.path != etl.path]
                if not log_path:
                    decode_errors.append(error)
                    index.setdefault(FAILED_ETL_KEY, {})[etl.path] = (etl, error)
                    yield {"type": "step", "content": f"⚠️ {error}"}
                    continue
                seg = _decoded_log_segment(log_path, alone=False)
                if seg is None:
                    error = f"{name}: the decoded log has no timestamp this tool can read."
                    decode_errors.append(error)
                    index.setdefault(FAILED_ETL_KEY, {})[etl.path] = (etl, error)
                    continue
                index.setdefault(ssa.DEVICE_WIFI, []).append(seg)
                segments = index[ssa.DEVICE_WIFI]
                p.coverage, hits, p.log_clock_note = _align(
                    segments, p.window_start, p.window_end, p.utc_offset_minutes, log_clock)
                yield {"type": "step", "content": (
                    f"{head}: decoded `{name}` covers {_fmt(seg.start)} → {_fmt(seg.end)} "
                    f"({_fmt_offset(seg.clock)})"
                    + ("; it holds this session." if reaches(hits) else "; not this session."))}
                if reaches(hits):
                    break
        p.etl_decode_errors = list(decode_errors)
        if p.coverage != "none" and not reaches(hits):
            # Cutting the margin alone would hand the agent the minutes before
            # (or after) standby as if they were the session.
            p.status = STATUS_NOT_COVERED
            p.message = outside_session_note(hits, s_lo, s_hi)
            if decode_errors:
                p.message += " " + " ".join(decode_errors)
            p.coverage = "none"
        elif p.coverage == "none":
            p.status = STATUS_NOT_COVERED
            p.message = "The driver log does not cover this window."
            if decode_errors:
                p.message += " " + " ".join(decode_errors)
            elif raw_etls and not candidates and not segments:
                p.message += (" Every Wi-Fi driver ETL here was captured before this session "
                              "started, so none can hold it.")
            elif any(s.clock is not None for s in segments) or p.utc_offset_minutes is None:
                p.message += " Its times were compared on the report's clock."
            else:
                p.message += (" A log decoded on another machine is stamped in that machine's "
                              "clock; check it is in the report's time zone "
                              f"({_fmt_offset(p.utc_offset_minutes)}).")
        else:
            if p.log_clock_note:
                yield {"type": "step", "content": f"🕐 {p.log_clock_note}"}
            p.driver_log_files = [h.describe() for h in hits]
            p.excerpt_path = os.path.join(
                out_dir, f"point{p.index:02d}_session{p.session_id}_{p.device}.log")
            yield {"type": "step", "content": (
                f"{head}: cutting the driver log — "
                + ", ".join(f"`{h.describe()['file']}`" for h in hits) + "…")}
            # A session's own window is read only as far as the line budget
            # goes; a window the caller chose is read whole.
            budget = SESSION_LINE_BUDGET if p.window_source != "caller" else None
            ex = write_excerpt(hits, p.window_start, p.window_end, p.excerpt_path, max_lines=budget)
            p.excerpt_lines = ex["lines"]
            p.driver_log_sources = ex["sources"]
            if ex["truncated"]:
                p.covered_until, p.stop_reason = ex["last"], "line_budget"
                yield {"type": "step", "content": (
                    f"{head}: the driver log outruns {SESSION_LINE_BUDGET:,} lines; analysing "
                    f"{_fmt(p.window_start)} → {_fmt(ex['last'])[11:]} of the session.")}
            if p.coverage == "partial":
                p.message = (f"The driver log covers only part of this window: "
                             f"{_fmt(ex['first'])} → {_fmt(ex['last'])[11:]} of "
                             f"{_fmt(p.window_start)} → {_fmt(p.window_end)[11:]}."
                             if ex["lines"] else "The driver log covers only part of this window.")
            if not ex["lines"]:
                p.status = STATUS_NO_LINES
                p.message = "The driver log covers this window but logged nothing in it."
            elif agent_factory is None:
                p.status = STATUS_DRY_RUN
            else:
                # The excerpt alone cannot tell "the driver logged nothing" (a
                # device asleep in D3) from "the log ended": say how far it runs.
                span_lo = min((h.start for h in hits if h.start is not None), default=None)
                span_hi = max((h.end for h in hits if h.end is not None), default=None)
                # Over a minute between two files, inside the window: the
                # capture may be missing there, which silence cannot tell.
                gaps, reached = [], None
                for h in sorted((h for h in hits if h.start is not None and h.end is not None),
                                key=lambda h: h.start):
                    if (reached is not None and h.start - reached > timedelta(minutes=1)
                            and reached < p.window_end and h.start > p.window_start):
                        gaps.append((reached, h.start))
                    reached = h.end if reached is None else max(reached, h.end)
                if span_lo is not None and span_hi is not None:
                    p.issue_description += (
                        f"\n\nDriver log coverage: the log runs from {_fmt(span_lo)} to {_fmt(span_hi)} on the "
                        f"report's clock; the excerpt holds its lines inside the window, the last at "
                        f"{_fmt(ex['last'])}. " + (
                            "The log continues past the window, so a stretch of the window without lines "
                            "means the driver logged nothing then (for example while the device was in D3), "
                            "not that the log ended." if span_hi >= p.window_end else
                            "The log ends inside the window; nothing after its end is known.")
                        + (" Exception: between its files the log has nothing from "
                           + "; ".join(f"{_fmt(a)} to {_fmt(b)}" for a, b in gaps)
                           + ". Whether the driver was quiet or the capture is missing there is not known: "
                             "do not read that stretch as the device being asleep." if gaps else ""))
                if p.stop_reason == "line_budget":
                    p.issue_description += (
                        f"\n\nThe excerpt stops at {_fmt(p.covered_until)}: the session runs to "
                        f"{_fmt(p.end)}, but the excerpt is held to {SESSION_LINE_BUDGET:,} lines. Nothing "
                        "after that time was read; do not conclude what happened later in the session.")
                elif p.stop_reason == "time_cap":
                    p.issue_description += (
                        f"\n\nThe window stops at {_fmt(p.window_end)}: the session runs to {_fmt(p.end)}, "
                        f"longer than the {SESSION_TIME_CAP.total_seconds() / 3600:.0f} h this analysis "
                        "reads. Nothing after that time was read.")
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
            # agent.chat() reports a provider failure or an exhausted reasoning
            # budget in its return value instead of raising, and that text must
            # not reach the combined report as a driver's conclusion.
            if p.agent_result_type == "error":
                p.status = STATUS_AGENT_FAILED
                p.message = f"Agent failed: {p.agent_report or 'no reason given'}"
            else:
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
    log_clock: Optional[int] = None,
) -> Iterator[dict]:
    """
    Analyse the time point `index` (as plan() numbered it) in `driver_log`
    ("" for none), and yield events. `driver_log` may be a Wi-Fi driver ETL
    (or its capture folder): an undecoded one is decoded first. `log_clock`
    (minutes east of UTC) says which clock a decoded log is in, when the
    person knows it; otherwise it is the recorded or guessed one.

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
        yield from _analyze_point(p, {}, [], out_dir, agent_factory, max_steps, on_agent,
                                  log_clock=log_clock)
        data = p.to_dict()
        data["markdown"] = render_point_markdown(data)
        data["finding"] = render_point_finding(data)
        yield {"type": "done", "data": data}
    except Exception as exc:
        yield {"type": "error", "content": f"{type(exc).__name__}: {exc}"}


# A caller that gives only an issue time gets this much driver log around it.
ISSUE_TIME_BEFORE = timedelta(minutes=5)
ISSUE_TIME_AFTER = timedelta(minutes=10)
# Issue times whose cuts are closer than this are analysed as one window.
ISSUE_TIME_MERGE_GAP = timedelta(minutes=5)


def apply_override(p: TimePoint, override: dict) -> None:
    """
    Set on a time point what a caller (the command line) asked for:
      window_start / window_end / issue_time -- timezone-aware datetimes,
        converted to the session's own clock (the customer machine's, which
        the report and every log comparison use). Naive times are refused:
        guessing their zone is how a log lines up with the wrong session.
      only an issue time -- the window is ISSUE_TIME_BEFORE to
        ISSUE_TIME_AFTER around it, kept inside the session
      issue_times -- several issue times the caller merged into the window
        it gives; the window is kept inside the session the same way
      question -- added to the point's question to the agent
    Raises ValueError for a time without a zone, a window that ends first or
    an issue time outside its session.
    """
    def local(value):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("times must carry a time zone (an ISO 8601 offset such as +08:00 or Z)")
        if p.utc_offset_minutes is None:
            raise ValueError(f"session {p.session_id}: the report gives no UTC offset to convert to")
        clock = timezone(timedelta(minutes=p.utc_offset_minutes))
        return value.astimezone(clock).replace(tzinfo=None)

    lo, hi = local(override.get("window_start")), local(override.get("window_end"))
    at = local(override.get("issue_time"))
    times = sorted(local(t) for t in override.get("issue_times") or [])
    if (lo is None) != (hi is None):
        raise ValueError(f"session {p.session_id}: give both window_start and window_end")
    # A window that comes from issue times (one, or several merged) belongs
    # to the session asked for. A window without a session id is the
    # caller's own, taken as given.
    from_times = bool(times) or (at is not None and lo is None)
    if not times and at is not None:
        times = [at]
    if from_times and p.selection != "window":
        for t in times:
            if (p.start is not None and t < p.start) or (p.end is not None and t > p.end):
                raise ValueError(f"session {p.session_id}: the issue time {_fmt(t)} is outside the session "
                                 f"({_fmt(p.start)} → {_fmt(p.end)})")
        if lo is None:
            lo, hi = times[0] - ISSUE_TIME_BEFORE, times[-1] + ISSUE_TIME_AFTER
        # Inside the session itself, not its report window: that one is held
        # to SESSION_LINE_BUDGET lines, which may end short of a late time.
        if p.start is not None:
            lo = max(lo, p.start)
        if p.end is not None:
            hi = min(hi, p.end)
    if lo is not None:
        if hi <= lo:
            raise ValueError(f"session {p.session_id}: the window ends before it starts")
        p.window_start, p.window_end = lo, hi
        p.window_truncated = False
        p.covered_until, p.stop_reason = hi, ""
        p.window_source = "caller"
    if times:
        p.issue_times = times
    if override.get("question"):
        p.caller_question = str(override["question"])
    if override.get("evidence"):
        p.caller_evidence = [str(e) for e in override["evidence"]]
    if override.get("device_id"):
        p.device_id = str(override["device_id"])


def driver_log_reach(path: str, report_offset: Optional[int]) -> dict:
    """
    How far back the driver log under `path` reaches, per device, on the
    report's clock (`report_offset` minutes east of UTC):
        {device: {"from", "to" (datetimes), "exact", "files", "undecoded_etl"}}
    The Intel Wi-Fi autologger is circular, so a session older than "from"
    cannot be analysed from it. An undecoded Wi-Fi driver ETL is known only by
    when it was last written; with one, the log reaches back at least to
    "from" and maybe further ("exact": False).
    """
    index, _ = discover_driver_logs(path)
    clock = timezone(timedelta(minutes=report_offset)) if report_offset is not None else None

    def written(file_path: str) -> Optional[datetime]:
        try:
            ts = os.path.getmtime(file_path)
        except OSError:
            return None
        if clock is None:
            return datetime.fromtimestamp(ts)
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(clock).replace(tzinfo=None)

    reach = {}
    for device in ANALYZABLE_DEVICES:
        segments = [s for s in in_report_clock(index.get(device, []), report_offset) if not s.dateless]
        raw = index.get(RAW_ETL_KEY, []) if device == ssa.DEVICE_WIFI else []
        raw_times = [t for t in (written(r.path) for r in raw) if t is not None]
        times_from = [s.start for s in segments] + raw_times
        if not times_from:
            continue
        reach[device] = {
            "from": min(times_from),
            "to": max([s.end or s.start for s in segments] + raw_times),
            "exact": not raw_times,
            "files": len(segments) + len(raw),
            "undecoded_etl": [os.path.basename(r.path) for r in raw],
        }
    return reach


def _session_covering(sessions: list, lo: datetime, hi: datetime, report_offset) -> Optional[dict]:
    """The Modern Standby session of the report overlapping the timezone-aware
    window lo..hi the most, or None. Each session is compared on its own clock."""
    best, best_overlap = None, timedelta(0)
    for s in sessions:
        if s.get("Type") not in ssa.MS_SESSION_TYPES:
            continue
        start = ssa.parse_local_timestamp(s.get("EntryTimestampLocal") or "")
        end = ssa.parse_local_timestamp(s.get("ExitTimestampLocal") or "")
        offset = session_utc_offset(s, report_offset)
        if start is None or end is None or offset is None:
            continue
        clock = timezone(timedelta(minutes=offset))
        overlap = (min(hi.astimezone(clock).replace(tzinfo=None), end)
                   - max(lo.astimezone(clock).replace(tzinfo=None), start))
        if overlap > best_overlap:
            best, best_overlap = s, overlap
    return best


def caller_point(payload: dict, request: dict, index: int) -> TimePoint:
    """
    A time point the caller asks for that the bad-session selection does not
    give, for request["device"] ("wifi" / "bt"; Wi-Fi by default):
      session_id -- that report session, analysed although it is not a bad
        wireless session ("forced");
      window_start / window_end (timezone-aware), no session_id -- that window,
        with the report session it falls in when there is one ("window").
    The caller's window, question and evidence are applied by apply_override().
    Raises ValueError for a session the report does not have.
    """
    device = request.get("device") or ssa.DEVICE_WIFI
    report_offset = (payload.get("ReportInformation") or {}).get("UtcOffset")
    sessions = payload.get("ScenarioInstances") or []
    sid = str(request.get("session_id") or "").strip()
    lo, hi = request.get("window_start"), request.get("window_end")
    if sid:
        raw = next((s for s in sessions if str(s.get("SessionId")) == sid), None)
        if raw is None:
            raise ValueError(f"session {sid}: the report has no session with this id")
    elif lo is not None and hi is not None:
        raw = _session_covering(sessions, lo, hi, report_offset)
    else:
        raise ValueError("a requested window needs both window_start and window_end")

    if raw is not None:
        start = ssa.parse_local_timestamp(raw.get("EntryTimestampLocal") or "")
        end = ssa.parse_local_timestamp(raw.get("ExitTimestampLocal") or "")
        sw, hw = ssa.compute_drip_percentages(raw)
        p = TimePoint(
            index=index, session_id=raw.get("SessionId"), device=device,
            type_label=ssa.MS_SESSION_TYPES.get(raw.get("Type"), f"Type {raw.get('Type')}"),
            start=start, end=end, duration=ssa.format_duration(raw.get("Duration") or 0),
            sw_drip_pct=sw, hw_drip_pct=hw,
            blockers=[{"name": h["blocker"].get("Name", ""), "device": h["device"],
                       "active_time_pct": h["blocker"].get("ActiveTimePercent", 0)}
                      for h in ssa.red_top_blockers(raw)],
            selection="forced" if sid else "window")
        p.utc_offset_minutes = session_utc_offset(raw, report_offset)
        p.sleep_summary = _sleep_summary(p, raw)
        p.wake_evidence = _wake_evidence(raw, device)
        set_session_window(p)
        return p

    # No report session holds the window: the window alone, on the report's clock.
    offset = session_utc_offset(None, report_offset)
    if offset is None:
        offset = int(lo.utcoffset().total_seconds() // 60)
    clock = timezone(timedelta(minutes=offset))
    start, end = lo.astimezone(clock).replace(tzinfo=None), hi.astimezone(clock).replace(tzinfo=None)
    p = TimePoint(index=index, session_id=f"window-{index}", device=device, type_label="Caller window",
                  start=start, end=end,
                  duration=ssa.format_duration(int((end - start).total_seconds() * 1_000_000)),
                  sw_drip_pct=None, hw_drip_pct=None, blockers=[], selection="window",
                  window_start=start, window_end=end, covered_until=end)
    p.utc_offset_minutes = offset
    p.sleep_summary = "No Sleep Study session in the report covers this window."
    return p


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
    session_ids=None,
    overrides: Optional[dict] = None,
    caller_points: Optional[list] = None,
) -> Iterator[dict]:
    """
    Analyse every time point and yield events: "step" and "agent_step" as in
    run_point(), {"type": "point", "data": <TimePoint dict>} per finished
    point, then {"type": "done", "data": <result dict with "markdown">} or
    {"type": "error", "content": <message>}.

    `driver_logs` is one path used for every point, or {point index: path}.
    A point with no path is reported as such and never reaches the LLM.
    `agent_factory=None` is a dry run: every step up to the agent runs.

    `session_ids` analyses those sessions only, chosen from every bad
    wireless session rather than the worst `max_points`. An item is a
    session id, or (session id, device) to take that device's point alone.
    `overrides` maps
    (session id, device) -- device "" for every device of the session -- to
    what a caller sets for it (apply_override): a window and an issue time
    as timezone-aware datetimes, a question, evidence and a device id. An
    empty `session_ids` (not None) takes none of the report's own picks:
    only `caller_points`.

    `caller_points` are requests the bad-session selection does not give
    (caller_point()): a report session to analyse anyway ({"session_id",
    "device"}), or a window ({"window_start", "window_end", "device"}), each
    with what apply_override() takes. A session asked for by id whose point
    for that device is already among the bad ones gets the request's
    override instead; a window is always a point of its own; a session the
    report lacks is skipped. A request marked "another" is a further window
    of a session already asked for: that session again, as a point of its own.
    """
    try:
        if session_ids is not None:
            # Asked for by id: any bad wireless session qualifies, not only
            # the worst few the page lists.
            max_points = per_device = 100000
        if html_path:
            yield {"type": "step", "content": f"Loading Sleep Study report: `{html_path}`"}
            collected, points, _system = _select(html_path, drip_threshold, max_points, per_device)
        else:
            # No report: only the caller's windows, on the clock of the times
            # it gave, without Sleep Study evidence.
            yield {"type": "step", "content": "No Sleep Study report: analysing the caller's windows alone."}
            collected = {"total_sessions": 0, "qualifying_count": 0, "sessions": [], "devices": [],
                         "warnings": [], "criteria": {"drip_metric": DRIP_METRIC,
                                                      "drip_threshold_pct": drip_threshold}}
            points = []
        if session_ids is not None:
            wanted: dict = {}                 # session id -> the devices asked for ("": any)
            for item in session_ids:
                sid, device = item if isinstance(item, tuple) else (item, "")
                wanted.setdefault(str(sid), set()).add(device or "")
            points = [p for p in points if str(p.session_id) in wanted
                      and wanted[str(p.session_id)] & {"", p.device}]
        # The report's points as selected, for a further window of the same session.
        selected = {(str(p.session_id), p.device): copy.deepcopy(p) for p in points}
        for p in points:
            override = ((overrides or {}).get((str(p.session_id), p.device))
                        or (overrides or {}).get((str(p.session_id), "")))
            if override:
                apply_override(p, override)
        if caller_points:
            payload = ssa.load_sleepstudy(html_path) if html_path else {}
            targets = {(str(p.session_id), p.device): p for p in points}
            next_index = max([p.index for p in points] + [len(collected["sessions"])]) + 1
            for request in caller_points:
                sid = str(request.get("session_id") or "")
                if request.get("another"):
                    again = [copy.deepcopy(q) for (s, d), q in selected.items()
                             if s == sid and request.get("device") in (None, "", d)]
                    for p in again:
                        p.index = next_index
                        apply_override(p, request)
                        points.append(p)
                        next_index += 1
                    # Not among the report's points: only a forced session is built anew.
                    if again or not (request.get("device") or request.get("force")):
                        continue
                try:
                    p = caller_point(payload, request, next_index)
                except ValueError as exc:
                    yield {"type": "step", "content": f"⚠️ {exc}"}
                    continue
                same = (targets.get((str(p.session_id), p.device))
                        if sid and not request.get("another") else None)
                if same is not None:
                    apply_override(same, request)
                    continue
                apply_override(p, request)
                points.append(p)
                targets.setdefault((str(p.session_id), p.device), p)
                next_index += 1
        yield {"type": "step", "content": (
            f"Analysing **{len(points)}** window(s) the caller gave." if not html_path else
            f"**{collected['total_sessions']}** sessions in the report; "
            f"**{collected['qualifying_count']}** are battery drains with "
            f"{criteria_text(collected['criteria'])} and a RED top blocker. "
            + (f"Analysing **{len(points)}** session(s) the caller asked for." if session_ids is not None else
               f"Analysing the worst **{len(points)}** (max {max_points}, {per_device} per device).")
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
           f"_Issue time {', '.join(p.get('issue_times') or []) or p['issue_time']} · "
           f"window {p['window_start']} → {p['window_end']} · **{p['status']}**_"]
    if p.get("stop_reason"):
        out.append(f"\n**Analysed to {coverage_text(p)}.** Nothing later in the session was read.")
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
        (f"**Report:** `{result['source']}`  " if result["source"] else
         "**Report:** none (the caller's times alone, without Sleep Study evidence)  "),
        "**Driver log:** " + (", ".join(f"`{d}`" for d in result["driver_logs"]) or "none") + "  ",
        (f"**Sessions:** {result['qualifying_count']} of {result['total_sessions']} qualify "
         f"(battery drain, {criteria_text(c)}, RED top blocker); "
         f"{len(result['points'])} analysed." if result["source"] else
         f"**Windows:** {len(result['points'])} analysed."),
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


# ── Chat before any driver log is read ─────────────────────────────────────────
# A question asked on the NW page before a time point has been analysed has
# only the Sleep Study report to go on. It is answered from the report's
# evidence, not by the log agent: that agent would read the report HTML as if
# it were a driver log.
REPORT_CHAT_PROMPT = """You answer questions about one Windows Sleep Study report, for Modern Standby battery drain on Intel wireless (Wi-Fi / Bluetooth) devices.

Only the Sleep Study report has been read so far; no driver log has been analysed in this conversation. The evidence below is everything the report says about its bad sessions (on battery, DRIPS below 80%): each session's times on the customer machine's clock (its UTC offset is given), its SW / HW DRIPS, its RED top blockers, the scenario the report points to, the OS jobs and wake reasons that woke the wireless device, the network activity by app, and the sessions blocked outside the wireless driver ("system").

Answer from that evidence only, concisely, in the user's language. Quote the numbers it gives; do not invent any. When a question needs the driver log (why the driver stayed in D0, which OS command or packet woke it, firmware, roams, the driver's own wake reason), say what the report shows and that the driver log has not been checked yet: tick the session(s) on the plan card and press Analyse, then ask again. If earlier driver-log conclusions are listed, use them and say they come from the driver log."""


def report_context(html_path: str, findings: Optional[dict] = None, *,
                   drip_threshold: float = DRIP_THRESHOLD, max_points: int = MAX_TIME_POINTS,
                   per_device: int = PER_DEVICE_LIMIT) -> str:
    """
    The Sleep Study report's evidence as text for REPORT_CHAT_PROMPT: the
    bad sessions, each time point's own evidence and the system sessions.
    `findings` (point index -> driver-log conclusion) adds what driver logs
    already concluded, for a conversation analysed before excerpts were kept.
    """
    collected, points, system = _select(html_path, drip_threshold, max_points, per_device)
    out = [f"Report: {os.path.basename(html_path)}",
           f"Bad sessions on battery: {system['bad_total']} "
           f"({system['wireless']} with a wireless RED top blocker, {system['system']} system)."]
    for p in points:
        tz = _fmt_offset(p.utc_offset_minutes) if p.utc_offset_minutes is not None else "customer clock"
        out += ["",
                f"Time point {p.index}: session {p.session_id}, {DEVICE_LABELS.get(p.device, p.device)}, "
                f"{_fmt(p.start)} -> {_fmt(p.end)} ({p.duration}; customer clock {tz})",
                f"SW DRIPS {_pct(p.sw_drip_pct)}, HW DRIPS {_pct(p.hw_drip_pct)}",
                "RED top blockers: " + "; ".join(
                    f"{b.get('name', '')} ({b.get('active_time_pct', 0)}% active)" for b in p.blockers or []),
                f"Sleep Study points to: {html_verdict(p.wake_evidence or {})}"]
        out += [line.strip() for line in wake_evidence_lines(p.wake_evidence or {})]
        out += [line.strip() for line in app_activity_lines(p.wake_evidence or {})]
        if findings and p.index in findings:
            out.append("Driver-log conclusion (already analysed): "
                       + _short_detail(findings[p.index], 900))
    if system.get("groups"):
        out += ["", "System sessions, by top blocker: " + "; ".join(
            f"{g['blocker']} ({len(g['sessions'])} session(s), lowest SW DRIPS {_pct(g['worst_sw'])})"
            for g in system["groups"])]
    return "\n".join(out)


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
