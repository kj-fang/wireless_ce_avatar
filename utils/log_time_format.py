"""Detect how a log file writes its timestamps, so an issue time can be matched to it.

Why this module exists
----------------------
Every timestamp-driven feature in the chatbot — the log range shown next to the
issue-time picker, the range check that rejects an out-of-log issue time, and
Segment2 (the +/-N minute slice the agent actually analyses) — used to hard-code
one regex, Wi-Fi's ``MM/DD/YYYY-HH:MM:SS.mmm``. BT's ``YYYY/MM/DD HH:MM:SS`` was
added as a second literal. That works while every log comes from one of our own
decoders.

A Linux capture does not. The same feature has to cope with, measured on real
files from the field:

  * ``2026-02-17T05:27:08.604578+00:00``   rsyslog / journalctl, RFC 3339, and the
                                           offset can CHANGE inside one file (a
                                           machine whose timezone was configured
                                           mid-capture).
  * ``04-18 10:26:16.777``                 Android logcat — month and day, NO year.
  * ``Feb 17 05:27:08``                    traditional syslog — also no year.
  * ``[    0.000000]``                     dmesg — seconds since boot, no wall
                                           clock anywhere in the file.

Against the two hard-coded patterns all four read as "no timestamps at all": the
log range came back empty, so the range check silently passed everything, and
Segment2 fell through to analysing the whole file. Worse, the fallback time-only
scan (a bare ``\\b\\d{1,2}:\\d{2}:\\d{2}``) matched *text inside messages* — on a
dmesg its only 4 hits in 2,000 lines were things like a kernel build date.

So the format is detected per file instead of assumed:

  1. :func:`detect` tries a table of known shapes against a sample of the file
     and takes the one that matches the most lines. Deterministic, no network,
     cached per (path, mtime, size). Every format we ship — Wi-Fi, BT, DDD,
     Linux — is one row in that table.
  2. :func:`detect_with_llm_fallback` is for the rare file nothing in the table
     fits. The model is asked for a *description* of the format (a regex and a
     strptime pattern), never for the answer: whatever it returns is re-run
     against the sample here, and is only used if it parses most of the lines.
  3. When neither works, the format is ``unknown`` and callers fall back to
     whole-file analysis — stated to the user rather than silently.

Rules that are about the Linux world, not about parsing
-------------------------------------------------------
* **No year** (logcat, syslog): the year is inferred from the file's own
  modification time — the newest year that does not place the log's LAST line
  in the future. A log whose first line falls later in the calendar than its
  last crossed New Year, so it starts one year earlier; each line then takes
  the year its month and day belong to, so any line can be read on its own.
  The modification time is when the file was collected, not captured, so the
  year stays an assumption and the sidebar says so.
* **A declared offset** (RFC 3339): every timestamp is normalised to the offset
  the file ENDS on, the machine's latest configuration. That makes the file
  comparable to itself when the offset changes mid-file, and it is the
  default reading of a customer-clock issue time. It is an assumption, not a
  fact — the customer may have reported on an earlier clock — and the sidebar
  says so.
* **Seconds since boot** (dmesg): placing it on a calendar needs proof that
  the evidence belongs to THIS boot. A ``Linux version`` banner is not proof:
  every boot of the same kernel prints the same one (a real kern.log here has
  six identical banners). Two facts together are:

    1. the dmesg's own ``rtc_cmos ...: setting system clock to <UTC>`` line,
       which pins this boot to a UTC instant; and
    2. a sibling log in the same folder with the same full banner, stamped
       within two minutes of that instant, which supplies the local offset.

  The RTC line alone gives UTC only, and nothing in a dmesg names the local
  timezone, so without (2) the file is still reported as having no usable
  time. A guess from the file's mtime is never used.
* **Out of order**: a logcat can interleave kernel lines on a second clock.
  Segment2 keeps only the lines whose own timestamp is in the window, so the
  other clock is not carried along.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Iterator, Optional, Pattern, Tuple

# How much of the file to look at. The head decides the format; the tail is
# needed for the prevailing offset and for the last timestamp.
_SAMPLE_LINES = 400
_TAIL_BYTES = 65536
# A log has blank and continuation lines, so a format does not have to match
# every line to be the right answer -- but it must match a real share of them.
_MIN_HIT_RATIO = 0.10
# ...or this many lines outright, so a long header cannot hide a real format.
_MIN_HITS = 20

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Shape:
    name: str
    label: str
    #: Group 1 must be the timestamp token.
    rx: Pattern[str]
    #: strptime patterns tried against group 1, in order.
    fmts: Tuple[str, ...]
    #: "absolute" = date + time, "time_only" = clock with no date,
    #: "relative" = needs an anchor (dmesg), "epoch" = seconds since 1970.
    kind: str
    has_year: bool = True
    #: The line itself names its UTC offset (group 2).
    declares_offset: bool = False


# Order matters only for ties: our own decoders first, so an existing Wi-Fi or
# BT log keeps the exact format it has always been read with.
_SHAPES: Tuple[_Shape, ...] = (
    _Shape("wifi_etl", "Wi-Fi ETL decode (MM/DD/YYYY)",
           re.compile(r"(\d{2}/\d{2}/\d{4}-\d{2}:\d{2}:\d{2}\.\d{3})"),
           ("%m/%d/%Y-%H:%M:%S.%f",), "absolute"),
    _Shape("bt_hci", "BT HCI decode (YYYY/MM/DD)",
           re.compile(r"(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{1,3})?)"),
           ("%Y/%m/%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S"), "absolute"),
    _Shape("rfc3339", "RFC 3339 (rsyslog / journalctl)",
           re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?)(Z|[+-]\d{2}:\d{2})"),
           (), "absolute", declares_offset=True),
    _Shape("iso_space", "ISO 8601, space separated",
           re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)"),
           ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"), "absolute"),
    _Shape("dmesg_ctime", "dmesg -T (ctime)",
           re.compile(r"^\[([A-Z][a-z]{2} [A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2} \d{4})\]"),
           ("%a %b %d %H:%M:%S %Y",), "absolute"),
    _Shape("logcat", "Android logcat (no year)",
           re.compile(r"^(\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})"),
           ("%m-%d %H:%M:%S.%f",), "absolute", has_year=False),
    _Shape("syslog", "Traditional syslog (no year)",
           re.compile(r"^([A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2})"),
           ("%b %d %H:%M:%S",), "absolute", has_year=False),
    _Shape("epoch", "Unix seconds (journalctl short-unix)",
           re.compile(r"^(\d{10}\.\d{3,6})\s"),
           (), "epoch"),
    _Shape("monotonic", "Seconds since boot (dmesg)",
           re.compile(r"^\[\s*(\d+\.\d+)\]"),
           (), "relative"),
    # DDD / tracefmt / BT HCI without dates: a clock with no date, optionally
    # after a sequence number and inside <...> -- the four entry forms
    # LogScopeMixin._merge_wrapped_lines_if_needed documents. Anchored, so it
    # cannot steal a line that starts with a date or a message that merely
    # contains a clock.
    _Shape("ddd_time_only", "Clock only, no date (DDD / tracefmt / BT HCI)",
           re.compile(r"^\s*<?(?:\d+\s+)?(\d{1,2}:\d{2}:\d{2}(?:\.\d{1,7})?)>?"),
           ("%H:%M:%S.%f", "%H:%M:%S"), "time_only", has_year=False),
)

_UNKNOWN_LABEL = "no recognised timestamps"


# ---------------------------------------------------------------------------
# The detected format
# ---------------------------------------------------------------------------
@dataclass
class LogTimeFormat:
    """One file's timestamp format, and everything needed to read it."""

    name: str = "unknown"
    label: str = _UNKNOWN_LABEL
    kind: str = "unknown"
    has_year: bool = True
    #: "-07:00" / "+00:00" when the file names its offset, else "".
    offset_label: str = ""
    #: The offset the file STARTS on, when it differs from ``offset_label``.
    first_offset_label: str = ""
    #: Year of the FIRST line, for a format that omits it.
    year: int = 0
    #: Year of the last line; one more than ``year`` when the log crosses New Year.
    end_year: int = 0
    #: (month, day) of the first line: a line before it in the calendar is in ``end_year``.
    _first_md: Optional[Tuple[int, int]] = field(default=None, repr=False)
    #: Local wall-clock boot time for a "relative" file, once proven (see above).
    anchor: Optional[datetime] = None
    #: How the anchor was proven, for the UI line.
    anchor_source: str = ""
    #: Why a "relative" file has no anchor, for the UI line.
    anchor_missing: str = ""
    #: The machine suspended during the boot; seconds since boot stop counting
    #: while suspended, so times after it drift from the wall clock.
    suspended: bool = False
    _shape: Optional[_Shape] = field(default=None, repr=False)
    _utcoffset: Optional[timedelta] = field(default=None, repr=False)

    # ---- what callers branch on -------------------------------------------
    @property
    def recognised(self) -> bool:
        return self.kind != "unknown"

    @property
    def has_absolute_time(self) -> bool:
        """Can a timestamp in this file be placed on a calendar?"""
        if self.kind in ("absolute", "epoch"):
            return True
        return self.kind == "relative" and self.anchor is not None

    @property
    def has_date(self) -> bool:
        """What the sidebar means by "this log has dates"."""
        return self.has_absolute_time

    @property
    def supports_windowing(self) -> bool:
        """Whether an issue time can select a slice of this file at all."""
        return self.has_absolute_time or self.kind == "time_only"

    # ---- reading ----------------------------------------------------------
    def parse(self, line: str) -> Optional[datetime]:
        """The line's timestamp, naive, in this file's prevailing frame.

        ``None`` when the line carries no timestamp — or carries one this file
        has no way to place on a calendar (a dmesg with no anchor).
        """
        shape = self._shape
        if shape is None:
            return None
        m = shape.rx.search(line)
        if not m:
            return None
        token = m.group(1)

        if shape.kind == "relative":
            if self.anchor is None:
                return None
            try:
                return self.anchor + timedelta(seconds=float(token))
            except ValueError:
                return None

        if shape.kind == "epoch":
            # Seconds since 1970 are UTC; never the analyst machine's zone.
            try:
                return datetime.fromtimestamp(float(token), timezone.utc).replace(tzinfo=None)
            except (ValueError, OSError, OverflowError):
                return None

        if shape.declares_offset:
            raw = token + ("+00:00" if m.group(2) == "Z" else m.group(2))
            try:
                aware = datetime.fromisoformat(raw)
            except ValueError:
                return None
            # Normalise onto the offset the file ends on: the machine's current
            # configuration, and therefore the clock the customer reads.
            target = self._utcoffset
            if target is None:
                target = aware.utcoffset() or timedelta(0)
            return aware.astimezone(timezone(target)).replace(tzinfo=None)

        # strptime's %f takes at most 6 digits; DDD / tracefmt write 7.
        head, dot, frac = token.partition(".")
        if dot and len(frac) > 6:
            token = f"{head}.{frac[:6]}"
        for fmt in shape.fmts:
            try:
                dt = datetime.strptime(token, fmt)
            except ValueError:
                continue
            if not shape.has_year and self.kind == "absolute":
                return _with_year(dt, self._year_for(dt))
            return dt
        return None

    def _year_for(self, dt: datetime) -> int:
        """The year a yearless stamp belongs to (see the module docstring)."""
        year = self.year or dt.year
        if (self.end_year and self.end_year != year and self._first_md
                and (dt.month, dt.day) < self._first_md):
            return self.end_year
        return year

    def iter_timestamps(self, lines: Iterable[str]) -> Iterator[Tuple[int, datetime]]:
        """Yield ``(line index, timestamp)`` for every line that carries one."""
        for index, line in enumerate(lines):
            dt = self.parse(line)
            if dt is not None:
                yield index, dt

    # ---- what the sidebar shows ------------------------------------------
    def describe(self, first: Optional[datetime] = None,
                 last: Optional[datetime] = None) -> str:
        """What was detected, what was assumed, and what follows from it."""
        no_window = (" The issue-time window cannot be applied, so the whole "
                     "file is analysed with keyword filtering.")
        if not self.recognised:
            return "Detected: no recognised timestamp format." + no_window

        facts = [self.label]
        assumed = []
        if self.offset_label:
            if self.first_offset_label:
                facts.append(f"offset changes {self.first_offset_label} → {self.offset_label}")
            else:
                facts.append(f"offset {self.offset_label}")
            assumed.append(f"the issue time is on the log's latest clock ({self.offset_label})")
        if self.kind == "time_only":
            facts.append("clock only, no dates")
        elif not self._shape.has_year and self.has_absolute_time:
            years = (f"{self.year}–{self.end_year}"
                     if self.end_year and self.end_year != self.year else f"{self.year}")
            assumed.append(f"year {years}, from the file date (the log has no year)")
        if self.kind == "epoch":
            assumed.append("the issue time is in UTC (the log names no timezone)")
        if self.anchor is not None:
            facts.append(f"boot time from {self.anchor_source}")
        if self.suspended and self.anchor is not None:
            assumed.append("times after the first suspend may lag the real clock")

        text = "Detected: " + ", ".join(facts) + "."
        if self.kind == "relative" and self.anchor is None:
            return text + " " + (self.anchor_missing or "No absolute time in the file.") + no_window
        if assumed:
            text += " Assumed: " + "; ".join(assumed) + "."
        if first and last:
            fmt = "%H:%M:%S" if self.kind == "time_only" else "%Y-%m-%d %H:%M"
            text += f" Log covers {first.strftime(fmt)} to {last.strftime(fmt)}."
        return text


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
_cache: dict[tuple, LogTimeFormat] = {}
# Files the LLM was already asked about, whatever it answered.
_llm_tried: set = set()


def _with_year(dt: datetime, year: int) -> datetime:
    try:
        return dt.replace(year=year)
    except ValueError:      # Feb 29 on a non-leap year
        return dt.replace(year=year, day=28)


def _read_sample(log_path: str) -> Tuple[list[str], list[str]]:
    """First ``_SAMPLE_LINES`` lines, and the lines in the last ``_TAIL_BYTES``."""
    head: list[str] = []
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        for index, line in enumerate(f):
            if index >= _SAMPLE_LINES:
                break
            head.append(line)
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - _TAIL_BYTES))
        tail = f.read().splitlines()
    return head, tail


def _pick_shape(head: list[str]) -> Tuple[Optional[_Shape], int]:
    """The shape matching the most sample lines, and that count."""
    lines = [l for l in head if l.strip()]
    if not lines:
        return None, 0
    best: Optional[_Shape] = None
    best_hits = 0
    for shape in _SHAPES:
        hits = sum(1 for l in lines if shape.rx.search(l))
        if hits > best_hits:
            best, best_hits = shape, hits
    if best is None or best_hits < max(1, min(int(len(lines) * _MIN_HIT_RATIO), _MIN_HITS)):
        return None, 0
    return best, best_hits


def _offsets(shape: _Shape, head: list[str],
             tail: list[str]) -> Tuple[Optional[timedelta], str, str]:
    """``(last offset, its label, first label if it differs)`` for a file naming its offset."""
    if not shape.declares_offset:
        return None, "", ""

    def _first(lines) -> str:
        for line in lines:
            m = shape.rx.search(line)
            if m:
                return "+00:00" if m.group(2) == "Z" else m.group(2)
        return ""

    last = _first(reversed(tail)) or _first(reversed(head))
    if not last:
        return None, "", ""
    first = _first(head)
    return _offset_td(last), last, (first if first and first != last else "")


def _offset_td(label: str) -> timedelta:
    sign = -1 if label[0] == "-" else 1
    return sign * timedelta(hours=int(label[1:3]), minutes=int(label[4:6]))


def _infer_years(fmt: LogTimeFormat, shape: _Shape, head: list[str],
                 tail: list[str], log_path: str) -> None:
    """Set ``year`` / ``end_year`` / ``_first_md`` for a format that omits the year.

    The file's own modification time is the anchor: the last line takes the
    newest year that is not in the future. It is the collection time, not the
    capture time, so it is only ever used to choose between whole years — never
    to place an individual timestamp.
    """
    try:
        anchor = datetime.fromtimestamp(os.path.getmtime(log_path))
    except OSError:
        anchor = datetime.now()

    def _stamp(lines) -> Optional[datetime]:
        for line in lines:
            m = shape.rx.search(line)
            if not m:
                continue
            for candidate_fmt in shape.fmts:
                try:
                    return datetime.strptime(m.group(1), candidate_fmt)
                except ValueError:
                    continue
        return None

    first, last = _stamp(head), _stamp(reversed(tail)) or _stamp(reversed(head))
    end_year = anchor.year
    if last is not None:
        # Two days of slack: a capture collected just after midnight on New
        # Year's Day is still last year's log.
        if _with_year(last, anchor.year) > anchor + timedelta(days=2):
            end_year = anchor.year - 1
    start_year = end_year
    if first is not None and last is not None and (first.month, first.day) > (last.month, last.day):
        start_year = end_year - 1
    fmt.year, fmt.end_year = start_year, end_year
    if first is not None:
        fmt._first_md = (first.month, first.day)


_BANNER = re.compile(r"(Linux version \S+ .*?)\s*$")
_RTC = re.compile(r"^\[\s*(\d+\.\d+)\].*setting system clock to "
                  r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}) UTC")
_SUSPEND = re.compile(r"PM: suspend entry")
_SIBLING_HINTS = ("kern", "syslog", "messages", "journal")
# rsyslog stamps the boot banner when it starts reading the kernel buffer, a
# few seconds to a minute after the RTC set.
_SAME_BOOT_TOLERANCE = timedelta(minutes=2)
_DMESG_SCAN_BYTES = 64 * 1024 * 1024


def _find_boot_anchor(log_path: str) -> Tuple[Optional[datetime], str, str, bool]:
    """``(local boot time, source, why missing, suspended)`` for a dmesg.

    See the module docstring: the RTC line pins the boot in UTC, and a sibling
    log with the same full banner stamped within ``_SAME_BOOT_TOLERANCE`` of it
    proves the boot and supplies the local offset. Anything less is reported
    as missing rather than guessed.
    """
    banner = rtc_mono = rtc_utc = None
    suspended = False
    read = 0
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                read += len(line)
                if banner is None:
                    m = _BANNER.search(line)
                    if m:
                        banner = m.group(1)
                if rtc_utc is None:
                    m = _RTC.search(line)
                    if m:
                        rtc_mono = float(m.group(1))
                        rtc_utc = datetime.fromisoformat(m.group(2))
                if not suspended and _SUSPEND.search(line):
                    suspended = True
                if read > _DMESG_SCAN_BYTES:
                    break
    except (OSError, ValueError):
        return None, "", "The file could not be read.", False

    if rtc_utc is None:
        return None, "", ("No 'setting system clock' line, so this boot cannot be "
                           "placed on a calendar (a kernel banner alone does not "
                           "identify a boot)."), suspended
    boot_utc = rtc_utc - timedelta(seconds=rtc_mono)
    utc_text = boot_utc.strftime("%Y-%m-%d %H:%M:%S")
    missing = (f"The boot started at {utc_text} UTC, but no log in the same folder "
               f"comes from that boot, so the local timezone is unknown.")
    if banner is None:
        return None, "", missing, suspended

    folder = os.path.dirname(os.path.abspath(log_path))
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return None, "", missing, suspended
    matches = []
    for name in names:
        candidate = os.path.join(folder, name)
        if (os.path.abspath(candidate) == os.path.abspath(log_path)
                or not os.path.isfile(candidate)
                or not any(h in name.lower() for h in _SIBLING_HINTS)):
            continue
        sibling = detect(candidate)
        if not sibling._shape or not sibling._shape.declares_offset:
            continue
        try:
            with open(candidate, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if banner not in line:
                        continue
                    m = sibling._shape.rx.search(line)
                    if not m:
                        continue
                    raw = m.group(1) + ("+00:00" if m.group(2) == "Z" else m.group(2))
                    stamp_utc = datetime.fromisoformat(raw).astimezone(timezone.utc)
                    gap = abs(stamp_utc.replace(tzinfo=None) - boot_utc)
                    if gap <= _SAME_BOOT_TOLERANCE:
                        matches.append((gap, name, sibling))
        except (OSError, ValueError):
            continue
    if not matches:
        return None, "", missing, suspended
    matches.sort(key=lambda x: x[0])
    if len(matches) > 1 and matches[1][0] - matches[0][0] < timedelta(seconds=10):
        return None, "", (f"The boot started at {utc_text} UTC, and more than one boot "
                          f"in the folder's logs fits it, so it cannot be told apart."), suspended
    _, name, sibling = matches[0]
    offset = sibling._utcoffset or timedelta(0)
    return (boot_utc + offset, f"{name} (same kernel banner, boot within "
            f"{int(matches[0][0].total_seconds())} s of the RTC line)", "", suspended)


def detect(log_path: str) -> LogTimeFormat:
    """Detect ``log_path``'s timestamp format. Cached per (path, mtime, size)."""
    if not log_path or not os.path.exists(log_path):
        return LogTimeFormat()
    try:
        stat = os.stat(log_path)
        key = (os.path.abspath(log_path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return LogTimeFormat()
    hit = _cache.get(key)
    if hit is not None:
        return hit

    try:
        head, tail = _read_sample(log_path)
    except OSError as e:
        print(f"[log_time_format] cannot sample {log_path}: {e}")
        return LogTimeFormat()

    # The tail too: a decode can open with hundreds of header lines, and the
    # old literal check scanned 2,000 lines before calling a Wi-Fi log dateless.
    shape, hits = _pick_shape(head)
    if shape is None:
        shape, hits = _pick_shape(tail)
    if shape is None:
        fmt = LogTimeFormat()
    else:
        offset, offset_label, first_offset_label = _offsets(shape, head, tail)
        fmt = LogTimeFormat(
            name=shape.name, label=shape.label, kind=shape.kind,
            has_year=shape.has_year, offset_label=offset_label,
            first_offset_label=first_offset_label,
            _shape=shape, _utcoffset=offset,
        )
        if shape.kind == "absolute" and not shape.has_year:
            _infer_years(fmt, shape, head, tail, log_path)
        if shape.kind == "relative":
            # Cached first: the sibling search calls detect() on its neighbours.
            _cache[key] = fmt
            (fmt.anchor, fmt.anchor_source,
             fmt.anchor_missing, fmt.suspended) = _find_boot_anchor(log_path)
        print(f"[log_time_format] {os.path.basename(log_path)}: {shape.name} "
              f"({hits}/{len(head) + len(tail)} sample lines)"
              + (f", offset {offset_label}" if offset_label else "")
              + (f", year {fmt.year}" if fmt.year else "")
              + (f", boot from {fmt.anchor_source}" if fmt.anchor else ""))
    _cache[key] = fmt
    return fmt


# ---------------------------------------------------------------------------
# LLM fallback: the model describes the format, this module checks the claim
# ---------------------------------------------------------------------------
_LLM_PROMPT = """You are given the first lines of a log file. Describe the format of
its line timestamps so a program can parse them.

Answer with JSON only:
{"regex": "<Python regex, group 1 = the timestamp token, anchored with ^ if the
timestamp starts the line>", "strptime": "<Python strptime pattern for group 1>",
"has_year": true|false}

If the lines carry no timestamp at all, answer {"regex": null}.

LINES:
"""


def llm_format_probe(
    llm_client, llm_model: str,
    on_usage: Optional[Callable[[dict], None]] = None,
) -> Callable[[str], Optional[dict]]:
    """Build a probe that asks one LLM call to describe a sample's timestamps.

    The client is injected the same way the issue-time AI takes it, so this
    module stays importable with no app config and no network. ``on_usage``
    receives the call's token usage in the shape ``gather_service`` records.
    """
    def probe(sample: str) -> Optional[dict]:
        # Lazy: issue_time_ai imports issue_time_utils, which imports this module.
        from utils.issue_time_ai import parse_json_loose

        if llm_client is None:
            return None
        try:
            response = llm_client.chat.completions.create(
                model=llm_model or "gpt-4.1",
                messages=[{"role": "system", "content": _LLM_PROMPT},
                          {"role": "user", "content": sample}],
                temperature=0,
                max_tokens=200,
            )
        except Exception as e:  # noqa: BLE001 - never break a page over this
            print(f"[log_time_format] LLM format probe failed: {e}")
            return None
        if on_usage is not None:
            try:
                on_usage(_usage_dict(getattr(response, "usage", None)))
            except Exception:
                pass
        return parse_json_loose(response.choices[0].message.content or "") or None
    return probe


def _usage_dict(usage) -> dict:
    def _n(*names: str) -> int:
        for name in names:
            value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
            if value is not None:
                try:
                    return max(0, int(value))
                except (TypeError, ValueError):
                    return 0
        return 0

    prompt, output = _n("prompt_tokens", "input_tokens"), _n("completion_tokens", "output_tokens")
    return {"llm_calls": 1, "input_tokens": prompt, "output_tokens": output,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "total_tokens": prompt + output}


def detect_with_llm_fallback(
    log_path: str,
    probe: Optional[Callable[[str], Optional[dict]]] = None,
) -> LogTimeFormat:
    """:func:`detect`, and for an unrecognised file one LLM-described attempt.

    The model only ever proposes; the proposal is then re-run against the same
    sample here and kept only if it actually parses most of the lines. A
    description that does not hold up is discarded, leaving the file
    unrecognised — which callers report rather than paper over.

    ``probe`` comes from :func:`llm_format_probe`. Without one this is just
    :func:`detect`, so the deterministic path never depends on a configured
    key, and the result is cached per file either way: at most one call per log.
    """
    fmt = detect(log_path)
    if fmt.recognised or not log_path or not os.path.exists(log_path):
        return fmt
    try:
        stat = os.stat(log_path)
        key = (os.path.abspath(log_path), stat.st_mtime_ns, stat.st_size)
        head, tail = _read_sample(log_path)
    except OSError:
        return fmt

    if probe is None or key in _llm_tried:
        return fmt
    # Marked before the call: a failed or rejected answer is not retried.
    _llm_tried.add(key)
    described = probe("".join(head[:60]))
    if not described or not described.get("regex"):
        return fmt
    try:
        rx = re.compile(described["regex"])
    except re.error as e:
        print(f"[log_time_format] LLM regex rejected: {e}")
        return fmt
    pattern = str(described.get("strptime") or "")
    has_year = bool(described.get("has_year", True))
    shape = _Shape("llm", f"described by the model ({pattern or 'regex only'})",
                   rx, (pattern,) if pattern else (), "absolute", has_year=has_year)

    # Validate the claim before trusting it: it must find AND parse real
    # timestamps on most of the sample.
    candidate = LogTimeFormat(name=shape.name, label=shape.label, kind=shape.kind,
                              has_year=has_year, _shape=shape)
    if shape.kind == "absolute" and not has_year:
        _infer_years(candidate, shape, head, tail, log_path)
    lines = [l for l in head if l.strip()]
    parsed = sum(1 for l in lines if candidate.parse(l) is not None)
    if not lines or parsed < len(lines) * 0.6:
        print(f"[log_time_format] LLM description parsed only {parsed}/{len(lines)} "
              f"sample lines — discarded")
        return fmt
    print(f"[log_time_format] {os.path.basename(log_path)}: using LLM-described "
          f"format ({parsed}/{len(lines)} sample lines)")
    _cache[key] = candidate
    return candidate


def clear_cache() -> None:
    """Forget detected formats (tests, and a file rewritten in place)."""
    _cache.clear()
    _llm_tried.clear()
