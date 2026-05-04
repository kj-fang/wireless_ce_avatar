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
    # Wi-Fi deep-dive (populated only when Wi-Fi is in the top offenders):
    activators: List["ActivatorInfo"] = field(default_factory=list)
    wifi_app_usage: List["WifiAppUsage"] = field(default_factory=list)
    # Per-app SRUM network power consumption (mW), used to attribute Wi-Fi
    # battery drain to specific apps.
    network_app_power: List["NetworkAppPower"] = field(default_factory=list)
    # Wi-Fi FX-device child reasons (name, active_time_s).  These explain why
    # the Wi-Fi adapter stayed active (e.g. OS Wi-Fi data-path wakes, AP
    # protocol offload events).  Used as a fallback root-cause source when
    # the SleepStudy report's Activators group is empty.
    wifi_fx_reasons: List[tuple] = field(default_factory=list)

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


# Wi-Fi-related activator names (case-insensitive substring match).
# These are the Windows components that can keep a network adapter awake.
_WIFI_ACTIVATOR_HINTS = [
    "ncsi", "wlansvc", "wlanext", "netprofm", "nlasvc",
    "wcmsvc", "wfdsconmgr", "wifisense",
    "wu", "wuauserv", "usosvc",          # Windows Update
    "dosvc",                                # Delivery Optimization
    "wpnservice", "wpnuserservice",        # Push Notifications
    "timebrokersvc", "systemeventsbroker",  # Background brokers
    "bits", "backgroundtransferhost",
    "dnscache", "dhcp",
    "mdcoresvc", "securityupdateservice",   # Defender / security
]


def _is_wifi_related_activator(name: str) -> bool:
    n = (name or "").lower()
    return any(h in n for h in _WIFI_ACTIVATOR_HINTS)


@dataclass
class ActivatorInfo:
    name: str
    active_pct: float = 0.0
    time_s: float = 0.0
    level: int = 0
    leaf_reasons: List[str] = field(default_factory=list)  # e.g. ['ActiveInternetProbe.Http']

    def is_wifi_related(self) -> bool:
        return _is_wifi_related_activator(self.name) or any(
            _is_wifi_related_activator(r) for r in self.leaf_reasons
        )


@dataclass
class WifiAppUsage:
    app: str            # cleaned app id / process name
    bytes_sent: int = 0
    bytes_recv: int = 0
    wake_count: int = 0

    @property
    def total_bytes(self) -> int:
        return self.bytes_sent + self.bytes_recv


@dataclass
class NetworkAppPower:
    """Per-app network power consumption from SRUM PowerEstimationData.
    Values are in milliwatts (mW) as reported by the SleepStudy report."""
    app: str
    network_mw: float = 0.0     # NetworkPowerConsumption (mW)
    total_mw: float = 0.0       # TotalPowerConsumption    (mW)
    user: str = ""


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


def _collect_leaf_reasons(blocker: dict, max_items: int = 6) -> List[str]:
    """Walk an activator's `Children` tree and collect names of leaf nodes
    that were actually active (ActiveTime > 0).  Leaves describe *why* the
    activator engaged (e.g. NCSI -> ActiveInternetProbe.Http)."""
    out: List[str] = []
    stack = list(blocker.get("Children") or [])
    while stack and len(out) < max_items:
        node = stack.pop(0)
        if not isinstance(node, dict):
            continue
        children = node.get("Children") or []
        if not children:
            if float(node.get("ActiveTime", 0) or 0) > 0:
                nm = node.get("Name", "")
                if nm and nm not in out:
                    out.append(nm)
        else:
            stack.extend(children)
    return out


_NDIS_HEADER_KEY = "[AppId]"
_PROCESS_PATH_RE = re.compile(r"\\([^\\]+\.exe)$", re.IGNORECASE)


def _clean_app_name(raw: str) -> str:
    """Trim Windows device-path prefixes; keep just the EXE or service name."""
    raw = (raw or "").strip()
    if not raw:
        return raw
    m = _PROCESS_PATH_RE.search(raw)
    if m:
        return m.group(1)
    return raw  # short service names (Dnscache, DoSvc, ...) stay as-is


def _harvest_wifi_ndis(wifi_blocker: dict, sink: List["WifiAppUsage"]) -> None:
    """Walk a Wi-Fi FX-Device blocker's Children, find every `NDIS` node and
    aggregate per-app `[Bytes Sent, Bytes Received, Outgoing Wake Count]`
    entries from its Detailed Blocker Information into `sink`."""
    bucket: dict = {}  # app -> [sent, recv, wakes]
    stack = [wifi_blocker]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        # Recurse first so order is irrelevant.
        stack.extend(node.get("Children") or [])
        if (node.get("Name") or "").upper() != "NDIS":
            continue
        meta = (node.get("Metadata") or {}).get("Values") or []
        for entry in meta:
            if not isinstance(entry, dict):
                continue
            key = entry.get("Key", "")
            val = entry.get("Value", "")
            if not key or key == _NDIS_HEADER_KEY or not isinstance(val, str):
                continue
            parts = [p.strip() for p in val.split(",")]
            if len(parts) < 3:
                continue
            try:
                sent  = int(float(parts[0]))
                recv  = int(float(parts[1]))
                wakes = int(float(parts[2]))
            except ValueError:
                continue
            app = _clean_app_name(key)
            agg = bucket.setdefault(app, [0, 0, 0])
            agg[0] += sent
            agg[1] += recv
            agg[2] += wakes
    for app, (sent, recv, wakes) in bucket.items():
        sink.append(WifiAppUsage(app=app, bytes_sent=sent, bytes_recv=recv, wake_count=wakes))


def _harvest_wifi_fx_reasons(wifi_blocker: dict, sink: List[tuple],
                             max_items: int = 6) -> None:
    """Walk a Wi-Fi FX-Device blocker's Children and collect the most
    significant child nodes (those with the longest ActiveTime) as
    (name, active_time_s) tuples.  These are surfaced in the SW-DRIPS
    explanation when the Activators group is empty.

    Skips NDIS nodes (handled separately by _harvest_wifi_ndis) and the
    bare hardware/component placeholder nodes that lack useful names.
    """
    rows: List[tuple] = []
    stack: List[dict] = list(wifi_blocker.get("Children") or [])
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        name = (node.get("Name") or "").strip()
        active_us = float(node.get("ActiveTime", 0) or 0)
        upper = name.upper()
        if name and active_us > 0 and upper != "NDIS":
            rows.append((name, active_us / 1_000_000.0))
        # Continue walking children to surface deeper reasons (e.g. NCSI leaves).
        stack.extend(node.get("Children") or [])
    # De-dupe by name (sum times) and keep top N by time.
    agg: dict = {}
    for n, t in rows:
        agg[n] = agg.get(n, 0.0) + t
    top = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)[:max_items]
    sink.extend(top)


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

            # Activator deep-dive: capture leaf reasons (e.g. NCSI ->
            # ActiveInternetProbe.Http) so we can correlate them with Wi-Fi.
            if kind == "activator":
                ai = ActivatorInfo(
                    name=b.get("Name", ""),
                    active_pct=float(b.get("ActiveTimePercent", 0) or 0),
                    time_s=active_us / 1_000_000.0,
                    level=int(b.get("ActivityLevel", 0) or 0),
                )
                ai.leaf_reasons = _collect_leaf_reasons(b)
                s.activators.append(ai)

            # Wi-Fi deep-dive: harvest per-app NDIS metadata under each Intel
            # Wi-Fi blocker so we can attribute wake/byte traffic to processes.
            if kind == "fx_device" and _is_wifi(b.get("Name", "")):
                _harvest_wifi_ndis(b, s.wifi_app_usage)
                _harvest_wifi_fx_reasons(b, s.wifi_fx_reasons)

    # Software/SRUM activators (per-app energy estimates).
    srum = scen.get("SrumData") or {}
    pwr  = srum.get("PowerEstimationData") or {}
    # Modern reports expose per-app power under `Values`; older builds used
    # `AppPowerRecords` with an `EnergyConsumption` field.  Handle both.
    for rec in (pwr.get("Values") or []):
        if not isinstance(rec, dict):
            continue
        name = rec.get("AppId") or rec.get("AppName") or ""
        if not name:
            continue
        net_mw   = float(rec.get("NetworkPowerConsumption", 0) or 0)
        total_mw = float(rec.get("TotalPowerConsumption",   0) or 0)
        if net_mw > 0 or total_mw > 0:
            s.network_app_power.append(NetworkAppPower(
                app=_clean_app_name(name),
                network_mw=net_mw,
                total_mw=total_mw,
                user=str(rec.get("UserId", "") or ""),
            ))
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


def _render_wifi_deep_dive(s: Session, indent: str = "  ") -> List[str]:
    """Render activators + per-app NDIS usage + heuristic conclusions for a
    session whose top offenders contain Wi-Fi.  Returns [] when there is no
    deep-dive evidence."""
    if not s.activators and not s.wifi_app_usage and not s.network_app_power:
        return []

    lines: List[str] = []
    conclusions: List[str] = []
    merged: List[WifiAppUsage] = []

    # ---- Activators (filtered to Wi-Fi-related) ----
    wifi_activators = [a for a in s.activators if a.is_wifi_related() and a.time_s > 0]
    wifi_activators.sort(key=lambda a: (a.active_pct, a.time_s), reverse=True)
    if wifi_activators:
        lines.append(f"{indent}Wi-Fi-related Activators (network-keep-alive sources):")
        for a in wifi_activators[:6]:
            reasons = (", ".join(a.leaf_reasons[:4])) if a.leaf_reasons else "-"
            lines.append(
                f"{indent}  - {a.name:<24s} active={a.active_pct:>5.1f}%  "
                f"time={a.time_s:>6.1f}s  reasons=[{reasons}]"
            )

    # ---- Per-app NDIS usage on Wi-Fi adapter ----
    if s.wifi_app_usage:
        # Aggregate duplicates (same app may appear from multiple NDIS nodes).
        agg: dict = {}
        for u in s.wifi_app_usage:
            cur = agg.setdefault(u.app, [0, 0, 0])
            cur[0] += u.bytes_sent
            cur[1] += u.bytes_recv
            cur[2] += u.wake_count
        merged = [WifiAppUsage(app=k, bytes_sent=v[0], bytes_recv=v[1], wake_count=v[2])
                  for k, v in agg.items()]
        # Rank by wake count first (sleep-killer signal), then total bytes.
        merged.sort(key=lambda u: (u.wake_count, u.total_bytes), reverse=True)

        lines.append(
            f"{indent}Wi-Fi NDIS per-process traffic during this session "
            f"(processes that used the Wi-Fi connection):"
        )
        lines.append(
            f"{indent}  {'App / Service':<46s}  {'Wakes':>6s}  {'BytesTx':>9s}  {'BytesRx':>9s}"
        )
        shown = [u for u in merged if u.wake_count > 0 or u.total_bytes > 0][:10]
        for u in shown:
            lines.append(
                f"{indent}  {u.app[:46]:<46s}  {u.wake_count:>6d}  "
                f"{u.bytes_sent:>9d}  {u.bytes_recv:>9d}"
            )

        # Wake-driver: any single app accounting for a large share of wakes.
        total_wakes = sum(u.wake_count for u in merged) or 1
        for u in merged[:3]:
            if u.wake_count >= 10 and u.wake_count / total_wakes >= 0.20:
                conclusions.append(
                    f"`{u.app}` drove {u.wake_count} outgoing wake(s) "
                    f"({u.wake_count*100//total_wakes}% of total) — likely keeping the "
                    "radio active."
                )

    # ---- SRUM per-app NETWORK power consumption (mW) ----
    # Surfaces which apps the OS attributes battery drain on the network
    # subsystem to.  Useful even when NDIS per-process traffic is unavailable.
    net_apps = [a for a in s.network_app_power if a.network_mw > 0]
    net_apps.sort(key=lambda a: a.network_mw, reverse=True)
    if net_apps:
        lines.append(
            f"{indent}SRUM per-app NETWORK power consumption "
            f"(top contributors to Wi-Fi drain):"
        )
        lines.append(
            f"{indent}  {'App / Service':<46s}  {'NetPwr':>8s}  {'TotalPwr':>9s}"
        )
        total_net = sum(a.network_mw for a in net_apps) or 1.0
        for a in net_apps[:10]:
            share = a.network_mw / total_net * 100.0
            lines.append(
                f"{indent}  {a.app[:46]:<46s}  {a.network_mw:>6.0f}mW  "
                f"{a.total_mw:>7.0f}mW  ({share:.0f}% of net)"
            )
        # Promote the dominant network-power consumer to a root-cause hint.
        top_net = net_apps[0]
        if top_net.network_mw >= 5:
            share = top_net.network_mw / total_net * 100.0
            conclusions.append(
                f"`{top_net.app}` consumed {top_net.network_mw:.0f} mW of network "
                f"power ({share:.0f}% of all network power this session) — "
                "likely Wi-Fi sleepstudy power-drain root cause."
            )
        for a in net_apps[1:3]:
            if a.network_mw >= 5:
                conclusions.append(
                    f"`{a.app}` also drew {a.network_mw:.0f} mW of network power."
                )

    # ---- Activator-driven probes & well-known offenders ----
    if any("ncsi" in a.name.lower() for a in wifi_activators):
        conclusions.append(
            "NCSI internet-connectivity probes were active — Windows was "
            "polling the network through Wi-Fi."
        )
    if any(("wu" == a.name.lower() or "usosvc" in a.name.lower()
            or "dosvc" in a.name.lower()) for a in wifi_activators):
        conclusions.append(
            "Windows Update / Delivery Optimization activity detected — "
            "background download/check kept Wi-Fi awake."
        )
    # Specific common offenders by app name (NDIS wake-traffic).
    names = {u.app.lower() for u in merged if u.wake_count > 0}
    if any("teams" in n for n in names):
        conclusions.append("Microsoft Teams was sending presence/keepalive traffic.")
    if any("outlook" in n for n in names):
        conclusions.append("Outlook was syncing mail/calendar over Wi-Fi.")
    if any("monagent" in n or "azure monitor" in n for n in names):
        conclusions.append("Azure Monitor Agent was uploading telemetry.")
    if any("it-servicecontroller" in n or "it-agent" in n for n in names):
        conclusions.append("IT-managed agent was beaconing/check-in over Wi-Fi.")

    if conclusions:
        lines.append(f"{indent}Wi-Fi root-cause hints:")
        for c in conclusions:
            lines.append(f"{indent}  * {c}")

    return lines


def _classify_wifi_fx_reason(name: str) -> str:
    """Map a Wi-Fi FX-device child node name to a short human explanation
    of why it would prevent SW-DRIPS.  Returns an empty string when the
    node name carries no actionable insight (e.g. anonymous components)."""
    n = name.lower()
    if "datapathwake" in n or "data path wake" in n:
        return ("OS Wi-Fi data-path wakes (incoming packets / ARP / ND / "
                "keepalive responses) kept Wi-Fi awake")
    if "wol" in n or "wake on lan" in n or "magic packet" in n:
        return "Wake-on-LAN / magic-packet processing kept Wi-Fi awake"
    if "protocol offload" in n or "protocoloffload" in n:
        return ("Protocol-offload events (NS/ARP offload mismatches) forced "
                "Wi-Fi out of low power")
    if "ncsi" in n or "internet probe" in n:
        return "NCSI internet-connectivity probes polled the network via Wi-Fi"
    if "dhcp" in n:
        return "DHCP renewal traffic kept Wi-Fi awake"
    if "wlan" in n and "scan" in n:
        return "WLAN background scanning kept Wi-Fi awake"
    if "rsn" in n or "4-way" in n or "key rotation" in n:
        return "WPA2/WPA3 key-rotation handshakes kept Wi-Fi awake"
    if "os wi-fi jobs" in n or "wifi jobs" in n:
        return "OS-scheduled Wi-Fi jobs (system networking work) kept Wi-Fi awake"
    return ""


def _explain_sw_drips(s: Session, drips_threshold: float = 80.0) -> str:
    """Build a one-line explanation of why SW-DRIPS coverage was low for this
    session, focused on what kept the system out of the software low-power
    state — preferring Wi-Fi-related causes.

    Source preference:
      1. Wi-Fi-related software activators (NCSI / Wlansvc / DoSvc / ...).
      2. Wi-Fi FX-device child reasons (e.g. OS Wi-Fi DataPathWake) when
         the Activators group is empty (modern reports often omit it).
      3. Top non-Wi-Fi software activator as a last resort.

    Returns \"\" when SW-DRIPS is healthy (>= threshold)."""
    if s.sw_drips_pct >= drips_threshold:
        return ""

    gap = max(0.0, drips_threshold - s.sw_drips_pct)
    prefix = f"SW-DRIPS only {s.sw_drips_pct:.1f}% (gap {gap:.1f}%); "

    # 1) Wi-Fi-related software activators.
    wifi_acts = [a for a in s.activators if a.is_wifi_related() and a.time_s > 0]
    wifi_acts.sort(key=lambda a: (a.active_pct, a.time_s), reverse=True)
    if wifi_acts:
        parts = []
        for a in wifi_acts[:3]:
            reason = f" [{a.leaf_reasons[0]}]" if a.leaf_reasons else ""
            parts.append(f"{a.name} ({a.active_pct:.1f}% active, {a.time_s:.0f}s){reason}")
        return (prefix + "Wi-Fi-related software activator(s) kept the OS out "
                "of DRIPS-SW: " + "; ".join(parts))

    # 2) Wi-Fi FX-device child reasons (OS-side networking work).
    if s.wifi_fx_reasons:
        # Pick the longest-active child that classifies cleanly; fall back to
        # the longest one with its raw name.
        best_classified: Optional[tuple] = None
        best_raw: Optional[tuple] = None
        for name, t_s in s.wifi_fx_reasons:
            if best_raw is None or t_s > best_raw[1]:
                best_raw = (name, t_s)
            cls = _classify_wifi_fx_reason(name)
            if cls and (best_classified is None or t_s > best_classified[2]):
                best_classified = (name, cls, t_s)
        if best_classified:
            name, cls, t_s = best_classified
            extra = []
            for name2, t2 in s.wifi_fx_reasons[1:3]:
                if name2 != name:
                    extra.append(f"{name2} ({t2:.0f}s)")
            extra_txt = ("; also: " + ", ".join(extra)) if extra else ""
            return (prefix + f"{cls} — top child `{name}` was active for "
                    f"{t_s:.0f}s{extra_txt}.")
        if best_raw:
            return (prefix + "Wi-Fi adapter child activity kept SW-DRIPS low: "
                    f"`{best_raw[0]}` was active for {best_raw[1]:.0f}s "
                    "(no specific class match).")

    # 3) Fallback to top non-Wi-Fi activator.
    other_acts = sorted(
        [a for a in s.activators if not a.is_wifi_related() and a.time_s > 0],
        key=lambda a: (a.active_pct, a.time_s), reverse=True,
    )
    if other_acts:
        top = other_acts[0]
        return (prefix + "no Wi-Fi-related activator/FX-child detected — "
                f"non-Wi-Fi software activator `{top.name}` "
                f"({top.active_pct:.1f}% active) was the dominant cause.")

    return (prefix + "no software activator data captured for this session — "
            "OS-side root cause cannot be attributed (likely captured under "
            "the Wi-Fi FX device only).")


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

    # ------------------------------------------------------------------
    # Compact per-session Markdown table.  Surfaced verbatim so the
    # LLM can copy it into its response with all the columns we want
    # (including the new "SW DRIP Cause" column).
    # ------------------------------------------------------------------
    def _short_sw_cause(s: Session) -> str:
        """One-line, table-cell-friendly explanation of the SW-DRIPS cause."""
        full = _explain_sw_drips(s, drips_threshold)
        if not full:
            return "Healthy (>= threshold)"
        # Drop the "SW-DRIPS only X% (gap Y%); " prefix — that info is
        # already in the dedicated SW-DRIPS column.
        cleaned = re.sub(r"^SW-DRIPS only [^;]+;\s*", "", full)
        # Collapse newlines / pipes which would break Markdown tables.
        cleaned = cleaned.replace("|", "/").replace("\n", " ").strip()
        if len(cleaned) > 220:
            cleaned = cleaned[:217] + "..."
        return cleaned or "n/a"

    out.append("Per-session summary (copy this table into the response):")
    out.append("")
    out.append("| Session | Date/Time (UTC) | Duration | SW-DRIPS | "
               "Wi-Fi Active | Wi-Fi Net Power | Exit Reason | SW DRIP Cause |")
    out.append("|---|---|---|---|---|---|---|---|")
    for s in matched[:max_sessions]:
        wifi_off = s.wifi_in_top(top_n)
        wifi_active_txt = (f"{wifi_off.active_pct:.1f}%"
                           if wifi_off else "n/a")
        wifi_net_mw = sum(a.network_mw for a in s.network_app_power)
        wifi_net_txt = f"{wifi_net_mw:.0f} mW" if wifi_net_mw > 0 else "n/a"
        sid = s.session_id or s.index
        date_txt = (s.start or "").replace("|", "/")
        dur_txt = (s.duration or f"{s.duration_s:.0f}s").replace("|", "/")
        exit_txt = (s.exit_reason or "").replace("|", "/").replace("\n", " ")
        if len(exit_txt) > 60:
            exit_txt = exit_txt[:57] + "..."
        out.append(
            f"| {sid} | {date_txt} | {dur_txt} | {s.sw_drips_pct:.1f}% | "
            f"{wifi_active_txt} | {wifi_net_txt} | {exit_txt or 'n/a'} | "
            f"{_short_sw_cause(s)} |"
        )
    out.append("")
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
        sw_reason = _explain_sw_drips(s, drips_threshold)
        if sw_reason:
            out.append(f"  SW-DRIPS reason: {sw_reason}")
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
        # Wi-Fi deep-dive: activators that may be holding Wi-Fi awake +
        # per-process NDIS traffic + heuristic conclusions.
        out.extend(_render_wifi_deep_dive(s, indent="  "))
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
