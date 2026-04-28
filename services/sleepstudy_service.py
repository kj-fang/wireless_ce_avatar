"""
SleepStudy Service
==================
Parse a Windows SleepStudy report (XML preferred, HTML fallback) and return
a Wi-Fi-focused summary: only sessions where Wi-Fi (WLAN) appears in the
top-N offenders are reported.  Used by the log_chatbot agent as a tool.

Generate a report on a target machine with:
    powercfg /sleepstudy /output sleepstudy.xml /xml
    powercfg /sleepstudy /output sleepstudy.html
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


# Patterns that identify a Wi-Fi / WLAN offender by name.
_WIFI_PATTERNS = [
    re.compile(r"\bwlan\b",           re.IGNORECASE),
    re.compile(r"\bwi[-\s]?fi\b",     re.IGNORECASE),
    re.compile(r"\b802\.?11\b",       re.IGNORECASE),
    re.compile(r"\bnetwtw\w*\b",      re.IGNORECASE),  # Intel Wi-Fi driver
    re.compile(r"\bwlansvc\b",        re.IGNORECASE),
    re.compile(r"\bwlanext\b",        re.IGNORECASE),
    re.compile(r"native\s*wifi",      re.IGNORECASE),
    re.compile(r"intel.*wireless",    re.IGNORECASE),
]


def _is_wifi(name: str) -> bool:
    if not name:
        return False
    return any(p.search(name) for p in _WIFI_PATTERNS)


# ---------------------------------------------------------
# Data structures
# ---------------------------------------------------------
@dataclass
class Offender:
    name: str
    energy_mwh: float = 0.0     # software activator energy (mWh) when known
    time_s: float = 0.0         # active time in seconds
    active_pct: float = 0.0     # ActiveTimePercent (0-100) — primary rank score
    level: int = 0              # 0=neutral 1=low 2=moderate 3=high
    kind: str = "software"      # "software" | "hardware" | "fx_device" | "pdc_phase" | "activator"

    def is_wifi(self) -> bool:
        return _is_wifi(self.name)

    def score(self) -> float:
        """Primary ranking metric: prefer ActiveTimePercent, fall back to energy/time."""
        if self.active_pct > 0:
            return self.active_pct
        if self.energy_mwh > 0:
            return self.energy_mwh
        return self.time_s


@dataclass
class Session:
    index: int
    session_id: int = 0
    start: str = ""
    end: str = ""
    duration: str = ""
    duration_s: float = 0.0
    drain_mw: float = 0.0
    drips_pct: float = 0.0
    sw_drips_pct: float = 0.0    # Software low-power-state coverage (DRIPS-SW), 0-100
    hw_drips_pct: float = 0.0    # Hardware low-power-state coverage (DRIPS-HW), 0-100
    energy_change_pct: float = 0.0
    energy_change_mwh: float = 0.0
    activity_level: int = 0
    enter_reason: str = ""
    exit_reason: str = ""
    offenders: List[Offender] = field(default_factory=list)

    def top_offenders(self, n: int) -> List[Offender]:
        return sorted(self.offenders, key=lambda o: o.score(), reverse=True)[:n]

    def wifi_in_top(self, n: int) -> Optional[Offender]:
        for o in self.top_offenders(n):
            if o.is_wifi():
                return o
        return None

    def low_drips(self, threshold_pct: float) -> bool:
        """True when SW DRIPS or HW DRIPS is below `threshold_pct` (0-100)."""
        return self.sw_drips_pct < threshold_pct or self.hw_drips_pct < threshold_pct


@dataclass
class Report:
    system: str = ""
    bios: str = ""
    os_build: str = ""
    sessions: List[Session] = field(default_factory=list)


# ---------------------------------------------------------
# XML parser
# ---------------------------------------------------------
def _to_float(s: Optional[str], default: float = 0.0) -> float:
    if s is None:
        return default
    try:
        return float(re.sub(r"[^0-9.\-]", "", s) or default)
    except Exception:
        return default


def _parse_xml(path: Path) -> Report:
    """Parse powercfg /sleepstudy /xml output."""
    rep = Report()
    tree = ET.parse(str(path))
    root = tree.getroot()

    # System info — element names vary slightly across Windows builds, so
    # match by local-name suffix.
    def _find_text(suffix: str) -> str:
        for el in root.iter():
            tag = el.tag.split("}")[-1].lower()
            if tag == suffix.lower() and el.text:
                return el.text.strip()
        return ""

    rep.system   = _find_text("SystemManufacturer") + " " + _find_text("SystemProductName")
    rep.bios     = _find_text("BIOSVersion") or _find_text("BIOSReleaseDate")
    rep.os_build = _find_text("OSBuild") or _find_text("OSVersion")

    # Sessions
    idx = 0
    for sess_el in root.iter():
        tag = sess_el.tag.split("}")[-1].lower()
        if tag != "session":
            continue
        idx += 1
        s = Session(index=idx)

        for child in sess_el.iter():
            ctag = child.tag.split("}")[-1].lower()
            txt  = (child.text or "").strip()

            if ctag == "starttime":
                s.start = txt
            elif ctag == "endtime":
                s.end = txt
            elif ctag in ("duration", "activetime"):
                if not s.duration:
                    s.duration = txt
            elif ctag in ("drainrate", "drainratemw"):
                s.drain_mw = _to_float(txt)
            elif ctag in ("drips", "lowpowerstateprecent", "lowpowerstatepercent"):
                s.drips_pct = _to_float(txt)
            elif ctag in ("energychange", "energychangepercent"):
                s.energy_change_pct = _to_float(txt)
            elif ctag in ("energychangemwh",):
                s.energy_change_mwh = _to_float(txt)
            elif ctag in ("softwareactivator", "hardwareactivator"):
                kind = "hardware" if ctag.startswith("hardware") else "software"
                name = (
                    child.get("Name")
                    or child.get("name")
                    or child.findtext(".//Name", default="")
                    or ""
                ).strip()
                energy = _to_float(
                    child.get("EnergyChange")
                    or child.get("Energy")
                    or child.findtext(".//EnergyChange", default="0")
                )
                time_s = _to_float(
                    child.get("TimeInS")
                    or child.get("Time")
                    or child.findtext(".//TimeInS", default="0")
                )
                if name:
                    s.offenders.append(
                        Offender(name=name, energy_mwh=energy, time_s=time_s, kind=kind)
                    )

        rep.sessions.append(s)

    return rep


# ---------------------------------------------------------
# HTML parser
# ---------------------------------------------------------
# Modern Windows powercfg /sleepstudy HTML reports embed every session as a
# JSON object literal:  var LocalSprData = { ... };
# The visible tables/charts are rendered later by JavaScript templates, so the
# raw HTML contains no <tr class="SessionRow"> markup to scrape.  We therefore
# extract the JSON, parse it, and walk ScenarioInstances directly.
_LOCALSPRDATA_RE = re.compile(r"\bLocalSprData\s*=\s*(\{)", re.DOTALL)


def _extract_localsprdata(html: str) -> Optional[dict]:
    """Find `LocalSprData = {...}` and return the parsed JSON dict (or None)."""
    import json
    m = _LOCALSPRDATA_RE.search(html)
    if not m:
        return None
    start = m.start(1)
    # Brace-balanced scan, ignoring braces inside JSON strings.
    depth = 0
    in_str = False
    escape = False
    end = -1
    for i in range(start, len(html)):
        ch = html[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return None
    try:
        return json.loads(html[start:end])
    except Exception as e:
        print(f"  [WARN] Could not JSON-parse LocalSprData: {e}")
        return None


def _flatten_blocker_names(blockers: list) -> List[dict]:
    """Return only top-level blocker entries (we ignore nested Children for ranking)."""
    out = []
    for b in blockers or []:
        if isinstance(b, dict) and b.get("Name"):
            out.append(b)
    return out


def _build_session_from_scenario(scen: dict, idx: int) -> Session:
    s = Session(index=idx)
    s.session_id     = int(scen.get("SessionId", idx))
    s.start          = scen.get("EntryTimestampLocal", "") or scen.get("EntryTimestamp", "")
    s.end            = scen.get("ExitTimestampLocal",  "") or scen.get("ExitTimestamp",  "")
    s.activity_level = int(scen.get("ActivityLevel", 0) or 0)
    s.enter_reason   = scen.get("EnterReason", "") or ""
    s.exit_reason    = scen.get("ExitReason",  "") or ""

    # Duration is reported in microseconds.
    dur_us = float(scen.get("Duration", 0) or 0)
    s.duration_s = dur_us / 1_000_000.0
    if s.duration_s > 0:
        h = int(s.duration_s // 3600)
        m = int((s.duration_s % 3600) // 60)
        sec = int(s.duration_s % 60)
        s.duration = f"{h:02d}:{m:02d}:{sec:02d}"

    # SW / HW DRIPS coverage from metadata (values are in 100ns units).
    # DRIPS% = LowPowerStateTime / Duration.
    sw_lp_100ns = 0.0
    hw_lp_100ns = 0.0
    metadata = scen.get("Metadata") or {}
    for entry in (metadata.get("Values") or []):
        if not isinstance(entry, dict):
            continue
        k = entry.get("Key", "")
        v = entry.get("Value", 0)
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if k == "Info.SwLowPowerStateTime":
            sw_lp_100ns = v
        elif k == "Info.HwLowPowerStateTime":
            hw_lp_100ns = v
    if s.duration_s > 0:
        sw_lp_s = sw_lp_100ns / 1e7
        hw_lp_s = hw_lp_100ns / 1e7
        s.sw_drips_pct = max(0.0, min(100.0, (sw_lp_s / s.duration_s) * 100.0))
        s.hw_drips_pct = max(0.0, min(100.0, (hw_lp_s / s.duration_s) * 100.0))
        # Legacy single drips_pct kept for compatibility (use the lower of the two).
        s.drips_pct = min(s.sw_drips_pct, s.hw_drips_pct)

    # Battery delta (mWh) and drain rate (mW).
    entry_cap = float(scen.get("EntryRemainingCapacity", 0) or 0)
    exit_cap  = float(scen.get("ExitRemainingCapacity",  0) or 0)
    full_cap  = float(scen.get("EntryFullChargeCapacity", 0) or 0) or 1.0
    delta_mwh = entry_cap - exit_cap
    s.energy_change_mwh = delta_mwh
    s.energy_change_pct = (delta_mwh / full_cap) * 100.0 if full_cap else 0.0
    if s.duration_s > 0:
        s.drain_mw = (delta_mwh / s.duration_s) * 3600.0  # mWh per hour = mW

    # Offenders: walk the BlockerGroups (Activators, FX Devices, PDC Phases, ...).
    # The container key varies slightly across builds, so accept a few names.
    groups = (
        scen.get("BlockerGroups")
        or scen.get("Blockers")
        or scen.get("BlockerInformation")
        or []
    )
    # Some builds nest groups under a top-level dict.
    if isinstance(groups, dict):
        groups = list(groups.values())

    for g in groups:
        if not isinstance(g, dict):
            continue
        gname = (g.get("Name") or "").strip()
        kind = "fx_device"
        gname_l = gname.lower()
        if "activator" in gname_l:
            kind = "activator"
        elif "pdc" in gname_l or "phase" in gname_l:
            kind = "pdc_phase"
        elif "fx" in gname_l or "device" in gname_l:
            kind = "fx_device"
        elif "processor" in gname_l or "soc" in gname_l:
            kind = "hardware"

        for b in _flatten_blocker_names(g.get("Blockers") or []):
            active_us = float(b.get("ActiveTime", 0) or 0)
            s.offenders.append(Offender(
                name=b.get("Name", ""),
                energy_mwh=float(b.get("EnergyChange", 0) or 0),
                time_s=active_us / 1_000_000.0,
                active_pct=float(b.get("ActiveTimePercent", 0) or 0),
                level=int(b.get("ActivityLevel", 0) or 0),
                kind=kind,
            ))

    # Software/SRUM activators (per-app energy estimates).
    srum = scen.get("SrumData") or {}
    pwr  = srum.get("PowerEstimationData") or {}
    for rec in (pwr.get("AppPowerRecords") or []):
        name = rec.get("AppId") or rec.get("AppName") or ""
        if not name:
            continue
        s.offenders.append(Offender(
            name=name,
            energy_mwh=float(rec.get("EnergyConsumption", 0) or 0) / 1000.0,
            time_s=float(rec.get("InUseTime", 0) or 0) / 1_000_000.0,
            kind="software",
        ))
    return s


def _parse_html(path: Path) -> Report:
    """Parse the standard sleepstudy-report.html produced by powercfg.

    Strategy:
      1. Extract the embedded `LocalSprData = {...}` JSON (modern reports).
      2. Fall back to a best-effort DOM scrape only when JSON is absent.
    """
    rep = Report()
    html = path.read_text(encoding="utf-8", errors="ignore")

    data = _extract_localsprdata(html)
    if data is not None:
        sysinfo = data.get("SystemInformation") or {}
        rep.system = " ".join(filter(None, [
            sysinfo.get("SystemManufacturer", ""),
            sysinfo.get("SystemProductName",  ""),
        ])).strip()
        rep.bios     = sysinfo.get("BIOSVersion", "") or sysinfo.get("BIOSDate", "")
        rep.os_build = sysinfo.get("OSBuild", "")    or sysinfo.get("OSVer", "")

        scenarios = data.get("ScenarioInstances") or []
        for i, scen in enumerate(scenarios, start=1):
            if isinstance(scen, dict):
                rep.sessions.append(_build_session_from_scenario(scen, i))
        return rep

    # ---- Legacy DOM-scrape fallback (older reports / unusual builds) ----
    print("  [WARN] LocalSprData JSON not found; falling back to DOM scrape.")
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) == 2:
            k, v = cells[0].lower(), cells[1]
            if "computer" in k or "model" in k or "manufacturer" in k:
                rep.system = (rep.system + " " + v).strip()
            elif "bios" in k:
                rep.bios = v
            elif "os build" in k or "build" in k:
                if not rep.os_build:
                    rep.os_build = v

    sessions: List[Session] = []
    idx = 0
    text = soup.get_text("\n", strip=True)
    if re.search(r"(wlan|wi-?fi|802\.11|netwtw\w*)", text, re.IGNORECASE):
        idx += 1
        s = Session(index=idx, start="(unparsed report)")
        for m in re.finditer(r"^.{0,120}(wlan|wi-?fi|802\.11|netwtw\w*).{0,120}$",
                             text, re.IGNORECASE | re.MULTILINE):
            s.offenders.append(Offender(name=m.group(0).strip()))
        sessions.append(s)
    rep.sessions = sessions
    return rep


# ---------------------------------------------------------
# Public entry
# ---------------------------------------------------------
def parse_sleepstudy(report_path: str) -> Report:
    p = Path(report_path)
    if not p.exists():
        raise FileNotFoundError(f"SleepStudy report not found: {report_path}")
    suffix = p.suffix.lower()
    if suffix in (".xml",):
        return _parse_xml(p)
    if suffix in (".html", ".htm"):
        return _parse_html(p)
    # Try XML first, then HTML
    try:
        return _parse_xml(p)
    except Exception:
        return _parse_html(p)


def render_wifi_focused(report: Report, top_n: int = 3,
                        wifi_only: bool = True,
                        max_sessions: int = 10,
                        drips_threshold: float = 80.0) -> str:
    """
    Render a compact text summary.

    Filters applied (in order):
      1. SW DRIPS < drips_threshold OR HW DRIPS < drips_threshold
         (skip healthy sessions that already stayed in low-power state).
      2. When wifi_only=True, Wi-Fi must appear in the top-N offenders.
    """
    total = len(report.sessions)
    if total == 0:
        return "SleepStudy report parsed but contained no sessions."

    # Stage 1: low-DRIPS filter (always applied).
    low_drips_sessions: List[Session] = [
        s for s in report.sessions if s.low_drips(drips_threshold)
    ]

    # Stage 2: optional Wi-Fi-in-top-N filter.
    matched: List[Session] = []
    for s in low_drips_sessions:
        if not wifi_only or s.wifi_in_top(top_n) is not None:
            matched.append(s)

    header_lines = [
        "SleepStudy Wi-Fi Involvement Report",
        f"System : {report.system or '(unknown)'}",
        f"BIOS   : {report.bios or '(unknown)'}",
        f"OS     : {report.os_build or '(unknown)'}",
        (f"Filters: SW or HW DRIPS < {drips_threshold:.0f}%"
         + (f"  AND Wi-Fi in top-{top_n} offenders" if wifi_only else "")),
        (f"Sessions: total={total}  low_drips={len(low_drips_sessions)}  "
         f"wifi_implicated={len(matched)}"),
        "",
    ]

    # Always include the explicit list of session IDs that match BOTH filters
    # (low DRIPS + Wi-Fi in top offenders), per user request.
    matched_ids_sorted = sorted({s.session_id or s.index for s in matched})
    if matched_ids_sorted:
        header_lines.append(
            "Session IDs with low DRIPS (<{0:.0f}%) AND Wi-Fi in top-{1} offenders ({2} total):"
            .format(drips_threshold, top_n, len(matched_ids_sorted))
        )
        # Wrap ids 16-per-line for readability.
        ids_str = ", ".join(str(x) for x in matched_ids_sorted)
        header_lines.append("  " + ids_str)
        header_lines.append("")

    if not matched:
        if not low_drips_sessions:
            header_lines.append(
                f"VERDICT: All {total} session(s) reached >= {drips_threshold:.0f}% "
                "DRIPS coverage (SW & HW). No low-power-state regression detected."
            )
        else:
            # Show what *was* draining when DRIPS was low, so the user has signal.
            recur: dict = {}
            for s in low_drips_sessions:
                for o in s.top_offenders(top_n):
                    recur[o.name] = recur.get(o.name, 0.0) + o.score()
            top_recurring = sorted(recur.items(), key=lambda kv: kv[1], reverse=True)[:5]
            recur_txt = "; ".join(f"{n} ({v:.1f})" for n, v in top_recurring) or "n/a"
            header_lines.append(
                f"VERDICT: {len(low_drips_sessions)} low-DRIPS session(s) found, "
                "but Wi-Fi was NOT in the top offenders for any of them."
            )
            header_lines.append(f"Top recurring non-Wi-Fi offenders: {recur_txt}")
        return "\n".join(header_lines)

    out = list(header_lines)
    out.append(f"VERDICT: Wi-Fi appears in top-{top_n} offenders for "
               f"{len(matched)}/{total} session(s).")
    out.append("")

    # Sort matched sessions by drain rate (worst first), cap the list.
    matched.sort(key=lambda s: s.drain_mw, reverse=True)
    _LEVEL = {0: "neutral", 1: "low", 2: "moderate", 3: "high"}
    for s in matched[:max_sessions]:
        wifi_off = s.wifi_in_top(top_n)
        out.append(
            f"[Session #{s.session_id or s.index}] {s.start} -> {s.end}  "
            f"dur={s.duration or f'{s.duration_s:.0f}s'}  "
            f"drain={s.drain_mw:.0f} mW  "
            f"SW-DRIPS={s.sw_drips_pct:.1f}%  HW-DRIPS={s.hw_drips_pct:.1f}%  "
            f"dEnergy={s.energy_change_mwh:.0f} mWh ({s.energy_change_pct:.1f}%)  "
            f"level={_LEVEL.get(s.activity_level, '?')}"
        )
        if s.exit_reason:
            out.append(f"  ExitReason: {s.exit_reason}")
        out.append(f"  Wi-Fi offender: {wifi_off.name}")
        out.append(f"    active={wifi_off.active_pct:.1f}%  "
                   f"time={wifi_off.time_s:.1f}s  "
                   f"energy={wifi_off.energy_mwh:.1f} mWh  "
                   f"({wifi_off.kind}, {_LEVEL.get(wifi_off.level, '?')})")
        out.append(f"  Top {top_n} offenders:")
        for rank, o in enumerate(s.top_offenders(top_n), 1):
            marker = "  <-- Wi-Fi" if o.is_wifi() else ""
            out.append(
                f"    {rank}. {o.name[:48]:<48s} "
                f"active={o.active_pct:>5.1f}%  "
                f"time={o.time_s:>7.1f}s  "
                f"energy={o.energy_mwh:>6.1f} mWh  "
                f"({o.kind}){marker}"
            )
        out.append("")

    if len(matched) > max_sessions:
        out.append(f"... {len(matched) - max_sessions} more Wi-Fi-implicated session(s) omitted.")

    return "\n".join(out)


def analyze_sleepstudy(report_path: str, top_n: int = 3,
                       wifi_only: bool = True,
                       max_sessions: int = 10,
                       drips_threshold: float = 80.0) -> str:
    """High-level convenience wrapper used by the chatbot tool."""
    try:
        report = parse_sleepstudy(report_path)
    except FileNotFoundError as e:
        return f"ERROR: {e}"
    except ET.ParseError as e:
        return f"ERROR: failed to parse XML SleepStudy report: {e}"
    except Exception as e:
        return f"ERROR: failed to parse SleepStudy report: {e}"

    return render_wifi_focused(
        report,
        top_n=top_n,
        wifi_only=wifi_only,
        max_sessions=max_sessions,
        drips_threshold=drips_threshold,
    )
