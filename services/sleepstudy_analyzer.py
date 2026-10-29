#!/usr/bin/env python3
"""
Sleep Study Analyzer — AI-Powered Agent
========================================
Analyzes a Windows Sleep Study HTML report to find sessions matching all three criteria:
  1. Sleep session is a DRAIN (OnAc=False — battery in use, not charging)
  2. SW Drip OR HW Drip percentage < 80%
  3. a device is in the Top Offenders list, marked RED (ActivityLevel = 3/high)

The report is read two ways, from one implementation of the criteria:

  - As prose, for a person. `analyze_sleepstudy_stream` summarises each
    qualifying session, pulls the Wi-Fi content out of the Activators and FX
    Devices blocker groups, adds the SRUM App IDs that spend network power,
    and has an LLM write it up. This is what the NW page shows, and it asks
    criterion 3 about Wi-Fi only.

  - As data, for a caller that has to act on it. `collect_sessions` answers
    with the sessions themselves plus one routing field: which device family
    blocked the SoC — wifi, bt, other, or none at all. A caller uses that to
    decide whose driver log to open next, and the per-session issue time to
    decide where in that log to look.

Usage:
    python sleepstudy_analyzer.py [path-to-sleepstudy.html]

If no path is given the script looks for the file next to itself.
"""

import json
import re
import sys
import os
from datetime import datetime
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────────────────────
SEC_TO_US    = 1_000_000      # Duration field is in microseconds
SEC_TO_100NS = 10_000_000     # SwLowPowerStateTime / HwLowPowerStateTime in 100-nanosecond units
ACTIVITY_LEVEL_NAMES = ["neutral", "low", "moderate", "high", "invalid", "invalid"]
DRIP_THRESHOLD = 80.0         # percent — sessions below this are flagged
# RED is level 3 exactly, not "3 or more": ACTIVITY_LEVEL_NAMES calls 4 and 5
# invalid, so a higher number is a measurement that failed, not a worse
# offender. Routing a driver-log investigation off an invalid reading would
# send someone to open a log over a number the report itself distrusts.
RED_ACTIVITY_LEVEL = 3

# The Wi-Fi report is written about Intel's radio: SLEEPSTUDY_SYSTEM_PROMPT
# mandates an "Intel Wi-Fi Impact" heading and a "Please contact Intel Wi-Fi
# team" verdict. So the report path asks for this vendor by name, while
# classify_device() stays vendor-neutral for callers that only want to know
# which device family to route to.
WIFI_REPORT_VENDOR = "Intel"

# Modern Standby session types. Everything else (Type 0, 5, 7, 11 …) is a
# transition the drip percentages do not describe, so the filter skips it.
MS_SESSION_TYPES = {1: "Screen Off", 2: "Sleep"}


# ── Device taxonomy ────────────────────────────────────────────────────────────
# The report already names the device that blocked the SoC, so it can also say
# which team's driver log to open next. Wi-Fi and Bluetooth each have one;
# anything else is still a genuine blocker, it just does not route to a
# wireless log.
#
# `NO_DEVICE` is not a fourth device. It is the answer "no session qualified",
# which is a different outcome from "a session blocked on something that is
# neither Wi-Fi nor Bluetooth": the first needs no further analysis, the
# second needs a log this tool does not own.
DEVICE_WIFI  = "wifi"
DEVICE_BT    = "bt"
DEVICE_OTHER = "other"
NO_DEVICE    = "none"

DEVICE_ORDER = (DEVICE_WIFI, DEVICE_BT, DEVICE_OTHER)

# Matched against a blocker's Name. Word boundaries earn their keep here: a
# bare "BT" substring also matches DEBT and BTW. CNVW and CNVB are the ACPI
# names of the two halves of an Intel CNVi module — W is the Wi-Fi radio, B is
# the Bluetooth one — and a real report names the device that way:
# "Intel(R) Wi-Fi 7 BE201 320MHz (\_SB.PC00.CNVW)".
_DEVICE_PATTERNS = (
    (DEVICE_WIFI, re.compile(r"Wi-?Fi|WLAN|Wireless\s+LAN|CNVW", re.IGNORECASE)),
    (DEVICE_BT,   re.compile(r"Bluetooth|BTH(?:USB|PORT|ENUM)|CNVB|\bBT\b", re.IGNORECASE)),
)


def classify_device(name: str) -> str:
    """
    Name the device family a blocker belongs to: wifi, bt, or other.

    Wi-Fi is tested first on purpose. On a combo module both halves carry the
    same vendor and module string, so a Wi-Fi blocker can read as Bluetooth to
    a looser test — and a report that mislabels it sends the reader to the
    wrong driver log.
    """
    for device, pattern in _DEVICE_PATTERNS:
        if pattern.search(name or ""):
            return device
    return DEVICE_OTHER


# ── System prompt shared by all callers (web + CLI) ────────────────────────────
SLEEPSTUDY_SYSTEM_PROMPT = (
    "You are an expert Windows power-management engineer analysing Sleep Study "
    "reports. The user will provide raw extracted data from a qualifying sleep "
    "session that meets three criteria: (1) drain session, (2) SW or HW Drip "
    "below 80%, (3) Intel Wi-Fi is a RED top offender.\n\n"
    "Output ONLY the following three sections, in this exact order, and NOTHING "
    "else. Do NOT include Session Overview, Battery Drain Summary, DRIP Analysis, "
    "Recommendations, conclusions, or any other section or preamble:\n"
    "1. **Intel Wi-Fi Impact** — how Wi-Fi kept the platform awake: active time %, "
    "blocking buckets, FX Devices / Activators details, wake reasons if available.\n"
    "2. **Network Activity by App** — which App IDs (from SRUM and NDIS) consumed "
    "network power; include network/MBB power in mW, bytes sent/received, and "
    "outgoing wake counts. Present as a short table or bullet list.\n"
    "3. **Root Cause Assessment** — first classify the root cause into ONE of the "
    "three scenarios below, and put the verdict on the FIRST line of this section "
    "in bold:\n"
    "   - **Scenario A: Wi-Fi activity by OS / App** — one or a few App IDs clearly "
    "dominate the network/MBB power consumption (from SRUM or NDIS bytes/wakes) and "
    "are the direct cause of the SW/HW Drip loss. State which App IDs dominate and "
    "by how much (mW, bytes, or wake counts).\n"
    "   - **Scenario B: Wi-Fi activity by remote** — the data shows a continuous "
    "stream of short wake events triggered by `NetWakeReasonTypeDevice` (i.e. the "
    "Wi-Fi NIC was receiving unsolicited inbound network traffic), causing the SW/HW "
    "Drips. Cite the wake-reason metadata and counts that prove this. "
    "**Exception:** if in this sleep session the count of short wakes (0-29 seconds) "
    "triggered by `NetWakeReasonTypeDevice` is **greater than 900**, do NOT report "
    "Scenario B — instead fall through to the \"Please contact Intel Wi-Fi team\" "
    "verdict below.\n"
    "   - **Neither scenario identified** — if neither A nor B is clearly supported "
    "by the data, the FIRST line of the section MUST be exactly: "
    "**\"Please contact Intel Wi-Fi team\"** (no other verdict).\n"
    "   After the verdict line, give a short evidence-based explanation tying the "
    "offending app(s) and/or wake reasons directly to the SW/HW low-power-state loss. "
    "Pick exactly one of the three verdicts; do not list multiple.\n\n"
    "Rules: be concise. Use only the data provided. Do not invent numbers. Do not "
    "emit any heading or section other than the three above."
)


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 1 — DATA LOADING
# ══════════════════════════════════════════════════════════════════════════════

def load_sleepstudy(html_path: str) -> dict:
    """
    Extract the embedded JSON payload from a Windows Sleep Study HTML report.
    Returns the parsed LocalSprData dictionary.
    """
    with open(html_path, "r", encoding="utf-8", errors="replace") as fh:
        raw = fh.read()

    # The entire dataset lives in:  var LocalSprData = { ... };
    scripts = re.findall(r'<script[^>]*>(.*?)</script>', raw, re.DOTALL)
    for script in scripts:
        idx = script.find("var LocalSprData = {")
        if idx == -1:
            continue
        start = idx + len("var LocalSprData = ")
        decoder = json.JSONDecoder()
        data, _ = decoder.raw_decode(script, start)
        return data

    raise ValueError("Could not find LocalSprData JSON payload in the HTML file.")


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 2 — HELPER FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def get_meta_value(session: dict, key: str):
    """Return the Value for a given metadata Key in a session's Metadata.Values list."""
    for entry in session.get("Metadata", {}).get("Values", []):
        if entry.get("Key") == key:
            return entry.get("Value")
    return None


def compute_drip_percentages(session: dict):
    """
    Compute SW Drip % and HW Drip % for a session.
    Both times are stored in 100-nanosecond units; Duration is in microseconds.

    Returns (sw_pct, hw_pct) — either may be None if data is absent.
    """
    duration_us = session.get("Duration", 0)
    if duration_us <= 0:
        return None, None

    duration_s = duration_us / SEC_TO_US
    sw_100ns = get_meta_value(session, "Info.SwLowPowerStateTime")
    hw_100ns = get_meta_value(session, "Info.HwLowPowerStateTime")

    sw_pct = (sw_100ns / SEC_TO_100NS) / duration_s * 100 if sw_100ns is not None else None
    hw_pct = (hw_100ns / SEC_TO_100NS) / duration_s * 100 if hw_100ns is not None else None
    return sw_pct, hw_pct


def format_duration(duration_us: int) -> str:
    total_s = duration_us // SEC_TO_US
    h = total_s // 3600
    m = (total_s % 3600) // 60
    s = total_s % 60
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    if m > 0:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def activity_label(level: int) -> str:
    names = ["neutral", "low", "moderate", "HIGH", "invalid"]
    return names[level] if level < len(names) else "unknown"


def device_filter(devices):
    """
    Freeze a device selection so it survives being used more than once.

    A report has many sessions, and every one of them asks this selection the
    same question. A caller passing a generator or any other one-shot
    iterable would have it emptied by the first session, and every session
    after that would match no device at all -- so a report with two Wi-Fi
    sessions would return one, with nothing reporting that the rest had been
    filtered out by an exhausted iterator rather than by their contents.

    Returns None unchanged, meaning "every family". Otherwise returns a tuple
    in DEVICE_ORDER, so the order the caller happened to pass does not change
    how the criteria read back.
    """
    if devices is None:
        return None
    wanted = set(devices)
    ordered = [device for device in DEVICE_ORDER if device in wanted]
    # Anything unrecognised is kept rather than dropped: silently ignoring a
    # device name would answer a question nobody asked.
    ordered += sorted(wanted.difference(DEVICE_ORDER))
    return tuple(ordered)


def red_top_blockers(session: dict, devices=None, vendor=None) -> list:
    """
    Return the session's RED top blockers, each tagged with the device family
    it belongs to, longest active time first.

    `devices` restricts the result to those families; None accepts every one.
    `vendor` additionally requires that string in the blocker's name, for a
    caller whose report speaks for one vendor.

    An empty list means nothing in this session was red — which is the normal
    case, since most sessions carry no TopBlockers list at all.
    """
    selected = device_filter(devices)
    wanted = set(selected) if selected is not None else None

    hits = []
    for tb in session.get("TopBlockers") or []:
        if (tb.get("ActivityLevel") or 0) != RED_ACTIVITY_LEVEL:
            continue
        name = tb.get("Name", "")
        if vendor is not None and vendor not in name:
            continue
        device = classify_device(name)
        if wanted is not None and device not in wanted:
            continue
        hits.append({"device": device, "blocker": tb})

    hits.sort(key=lambda hit: -(hit["blocker"].get("ActiveTime") or 0))
    return hits


def wifi_in_top_offenders_red(session: dict):
    """
    The question the Wi-Fi report asks: is Intel Wi-Fi a red offender here?

    Returns the raw TopBlockers entries, and an empty list still answers no.
    """
    return [
        hit["blocker"]
        for hit in red_top_blockers(
            session, devices=(DEVICE_WIFI,), vendor=WIFI_REPORT_VENDOR
        )
    ]


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 3 — SESSION FILTER (the three criteria)
# ══════════════════════════════════════════════════════════════════════════════

def filter_qualifying_sessions(sessions: list, devices=(DEVICE_WIFI,), vendor=None) -> list:
    """
    Return sessions that satisfy ALL three criteria:
      1. Drain session  (OnAc = False)
      2. SW Drip < 80 %  OR  HW Drip < 80 %
      3. a RED top offender from one of `devices`
    Only Modern Standby session types (Type 1 = Screen Off, Type 2 = Sleep) are considered.

    `devices` defaults to Wi-Fi alone, because that is the question the Wi-Fi
    report asks and this function answers it for that report. Pass
    `DEVICE_ORDER` — or None — to accept a red blocker from any family, which
    is what a caller routing between teams wants. `vendor` narrows criterion 3
    to one vendor's hardware; the Wi-Fi report passes `WIFI_REPORT_VENDOR`.
    """
    # Frozen before the loop: red_top_blockers() is called once per session,
    # and a one-shot iterable would be empty from the second session on.
    devices = device_filter(devices)

    qualifying = []
    for s in sessions:
        if s.get("OnAc", True):
            continue
        if s.get("Type") not in MS_SESSION_TYPES:
            continue

        sw_pct, hw_pct = compute_drip_percentages(s)
        drip_fail = False
        if sw_pct is not None and sw_pct < DRIP_THRESHOLD:
            drip_fail = True
        if hw_pct is not None and hw_pct < DRIP_THRESHOLD:
            drip_fail = True
        if not drip_fail:
            continue

        if not red_top_blockers(s, devices=devices, vendor=vendor):
            continue

        qualifying.append(s)

    return qualifying


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 4 — WIFI CONTENT EXTRACTOR
# ══════════════════════════════════════════════════════════════════════════════

def extract_wifi_content(session: dict) -> dict:
    """
    For a qualifying session, extract all Wi-Fi-related content from:
      - Activators blocker group
      - FX Devices blocker group
      - SRUM PowerEstimationData (Apps with NetworkPowerConsumption > 0)
    """
    result = {
        "activators_wifi": [],
        "fx_devices_wifi": [],
        "srum_network_apps": [],
        "ndis_app_traffic": [],
    }

    TARGET_GROUPS = {"Activators", "FX Devices"}

    for bg in session.get("BlockerGroups", []):
        group_name = bg.get("Name", "")
        if group_name not in TARGET_GROUPS:
            continue

        for blocker in bg.get("Blockers", []):
            b_name = blocker.get("Name", "")
            is_wifi = "Wi-Fi" in b_name or "WiFi" in b_name or "WLAN" in b_name

            entries_to_check = [(blocker, is_wifi, group_name)]
            for child in blocker.get("Children", []):
                c_name = child.get("Name", "")
                c_is_wifi = "Wi-Fi" in c_name or "WiFi" in c_name or "WLAN" in c_name or is_wifi
                entries_to_check.append((child, c_is_wifi, group_name))
                for grandchild in child.get("Children", []):
                    gc_name = grandchild.get("Name", "")
                    gc_is_wifi = c_is_wifi or "Wi-Fi" in gc_name or "NDIS" in gc_name
                    entries_to_check.append((grandchild, gc_is_wifi, group_name))
                    if "NDIS" in gc_name:
                        meta_vals = grandchild.get("Metadata", {}).get("Values", [])
                        if meta_vals:
                            result["ndis_app_traffic"].extend(_parse_ndis_metadata(meta_vals))

            for entry, wifi_related, grp in entries_to_check:
                if not wifi_related:
                    continue
                info = {
                    "group": grp,
                    "name": entry.get("Name", ""),
                    "activity_level": entry.get("ActivityLevel", 0),
                    "activity_label": activity_label(entry.get("ActivityLevel", 0)),
                    "active_time_pct": entry.get("ActiveTimePercent", 0),
                    "active_time_us": entry.get("ActiveTime", 0),
                    "metadata": entry.get("Metadata", {}).get("Values", []),
                    "blocking_buckets": entry.get("BlockingTimeBuckets", []),
                }
                if grp == "Activators":
                    result["activators_wifi"].append(info)
                else:
                    result["fx_devices_wifi"].append(info)

    # SRUM — network power per app
    srum = session.get("SrumData", {}).get("PowerEstimationData", {})
    for entry in srum.get("Values", []):
        net_pwr = entry.get("NetworkPowerConsumption", 0) or 0
        mbb_pwr = entry.get("MbbPowerConsumption", 0) or 0
        if net_pwr > 0 or mbb_pwr > 0:
            result["srum_network_apps"].append({
                "app_id": entry.get("AppId", "unknown"),
                "user_id": entry.get("UserId", ""),
                "network_power_mw": net_pwr,
                "mbb_power_mw": mbb_pwr,
                "total_power_mw": entry.get("TotalPowerConsumption", 0) or 0,
            })

    result["srum_network_apps"].sort(key=lambda x: -(x["network_power_mw"] + x["mbb_power_mw"]))
    return result


def _parse_ndis_metadata(meta_vals: list) -> list:
    """Parse NDIS blocker metadata into per-app traffic records."""
    if not meta_vals:
        return []
    header = meta_vals[0]
    if "[AppId]" not in str(header.get("Key", "")):
        return []
    results = []
    for entry in meta_vals[1:]:
        app_id = entry.get("Key", "")
        raw_val = entry.get("Value", "")
        try:
            parts = [p.strip() for p in str(raw_val).split(",")]
            sent  = int(parts[0]) if len(parts) > 0 else 0
            recv  = int(parts[1]) if len(parts) > 1 else 0
            wakes = int(parts[2]) if len(parts) > 2 else 0
        except (ValueError, IndexError):
            sent = recv = wakes = 0
        results.append({
            "app_id": app_id,
            "bytes_sent": sent,
            "bytes_received": recv,
            "outgoing_wake_count": wakes,
        })
    return results


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 5 — BUILD SESSION SUMMARY (structured text for agent context)
# ══════════════════════════════════════════════════════════════════════════════

def build_session_summary(session: dict, wifi_content: dict) -> str:
    """Build a detailed text summary used as context for the LLM agent."""
    sid    = session["SessionId"]
    stype  = {1: "Screen Off (Modern Standby)", 2: "Sleep (Connected Standby)"}.get(
        session["Type"], f"Type {session['Type']}")
    start  = session.get("EntryTimestampLocal", "?")
    end    = session.get("ExitTimestampLocal", "?")
    dur    = format_duration(session.get("Duration", 0))
    on_ac  = session.get("OnAc", True)

    sw_pct, hw_pct = compute_drip_percentages(session)
    sw_str = f"{sw_pct:.1f}%" if sw_pct is not None else "N/A"
    hw_str = f"{hw_pct:.1f}%" if hw_pct is not None else "N/A"

    enter_reason = get_meta_value(session, "Info.EnterReason") or "?"
    exit_reason  = get_meta_value(session, "Info.ExitReason")  or "?"

    entry_cap   = get_meta_value(session, "Battery.EntryRemainingCapacity")
    exit_cap    = get_meta_value(session, "Battery.ExitRemainingCapacity")
    full_cap    = get_meta_value(session, "Battery.EntryFullChargeCapacity") or 1
    entry_pct   = round(entry_cap / full_cap * 100, 1) if entry_cap else "?"
    exit_pct    = round(exit_cap  / full_cap * 100, 1) if exit_cap  else "?"
    drained_mwh = (entry_cap - exit_cap) if (entry_cap and exit_cap) else "?"

    top_blockers_info = []
    for tb in session.get("TopBlockers", []):
        level = tb.get("ActivityLevel", 0)
        label = activity_label(level)
        pct   = tb.get("ActiveTimePercent", 0)
        top_blockers_info.append(f"  - [{label}] {tb['Name']} - {pct}% active time")

    lines = [
        f"SESSION ID       : {sid}",
        f"Type             : {stype}",
        f"Power Source     : {'AC (charging)' if on_ac else 'BATTERY (drain)'}",
        f"Start Time       : {start}",
        f"End Time         : {end}",
        f"Duration         : {dur}",
        f"Enter Reason     : {enter_reason}",
        f"Exit Reason      : {exit_reason}",
        "",
        "-- Battery --------------------------------------------",
        f"  Entry capacity : {entry_cap} mWh  ({entry_pct}% of full charge)",
        f"  Exit capacity  : {exit_cap} mWh  ({exit_pct}% of full charge)",
        f"  Drained        : {drained_mwh} mWh",
        "",
        "-- DRIP (Low Power State Time) -----------------------",
        f"  SW Drip        : {sw_str}  {'[BELOW 80%]' if sw_pct is not None and sw_pct < 80 else ''}",
        f"  HW Drip        : {hw_str}  {'[BELOW 80%]' if hw_pct is not None and hw_pct < 80 else ''}",
        "",
        "-- Top Offenders (Top Blockers) ----------------------",
    ] + top_blockers_info + [""]

    lines.append("-- Wi-Fi Content in Blocker Groups --------------------")
    if wifi_content["fx_devices_wifi"]:
        lines.append("  [FX Devices]")
        for entry in wifi_content["fx_devices_wifi"]:
            lines.append(f"    [{entry['activity_label']}] {entry['name']}")
            lines.append(f"      Active: {entry['active_time_pct']}%  ({entry['active_time_us']//1_000_000}s)")
            if entry["blocking_buckets"]:
                bucket_str = ", ".join(
                    f"{b['BucketName']}:{b['Value']}"
                    for b in entry["blocking_buckets"] if b["Value"] > 0
                )
                if bucket_str:
                    lines.append(f"      Blocking buckets: {bucket_str}")
            if entry["metadata"]:
                for mv in entry["metadata"]:
                    lines.append(f"      Metadata: {mv['Key']} = {mv['Value']}")
    else:
        lines.append("  [FX Devices] No Wi-Fi entries found.")

    if wifi_content["activators_wifi"]:
        lines.append("  [Activators]")
        for entry in wifi_content["activators_wifi"]:
            lines.append(f"    [{entry['activity_label']}] {entry['name']}")
            lines.append(f"      Active: {entry['active_time_pct']}%  ({entry['active_time_us']//1_000_000}s)")
            if entry["metadata"]:
                for mv in entry["metadata"]:
                    lines.append(f"      Metadata: {mv['Key']} = {mv['Value']}")
    else:
        lines.append("  [Activators] No Wi-Fi entries found.")

    if wifi_content["ndis_app_traffic"]:
        lines.append("")
        lines.append("-- NDIS Per-App Network Traffic (from FX Devices) ----")
        for app in wifi_content["ndis_app_traffic"]:
            lines.append(
                f"  {app['app_id']}: sent={app['bytes_sent']}B, "
                f"recv={app['bytes_received']}B, wakes={app['outgoing_wake_count']}"
            )

    lines.append("")
    lines.append("-- SRUM: Apps Consuming Network Power ----------------")
    if wifi_content["srum_network_apps"]:
        for app in wifi_content["srum_network_apps"]:
            lines.append(
                f"  App: {app['app_id']}"
                + (f"  (user: {app['user_id']})" if app["user_id"] else "")
            )
            lines.append(
                f"    Network power: {app['network_power_mw']:.2f} mW  |  "
                f"MBB: {app['mbb_power_mw']:.2f} mW  |  "
                f"Total: {app['total_power_mw']:.2f} mW"
            )
    else:
        lines.append("  No SRUM network power consumers found for this session.")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 5b — STRUCTURED RESULT (no Flask, no LLM, no markdown)
# ══════════════════════════════════════════════════════════════════════════════
#  Everything above this point produces prose for a person to read. A caller
#  that has to act on the result — pick a device, open that device's log, fill
#  in an issue time — needs the same findings as data instead. This section is
#  that view: plain dicts, no request, no session, no LLM, so one implementation
#  serves the web route, the CLI, a test, and a tool server.

# The chatbot asks for an issue time as MM/DD/YYYY HH:MM:SS. The report writes
# EntryTimestampLocal as ISO-8601 with a trailing Z even though the value is
# already local, so the Z is dropped rather than honoured: reading it as UTC
# would shift every time by the machine's offset and point the next agent at
# the wrong stretch of the driver log.
_TS_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f")


def parse_local_timestamp(value: str):
    """Parse a report timestamp into a naive datetime, or None if unreadable.

    Windows counts in 100-nanosecond ticks, so a report can write seven
    fractional digits where %f reads at most six. The reports to hand carry
    no fraction at all, but one that did would fail to parse entirely and
    leave issue_time unset -- the caller would then be asked to type in a
    time the report had already stated. Extra digits are finer than a
    microsecond and nothing downstream can use them, so they are dropped
    rather than allowed to lose the whole timestamp.
    """
    text = (value or "").strip().rstrip("Z")
    head, dot, frac = text.partition(".")
    if dot and len(frac) > 6:
        text = f"{head}.{frac[:6]}"
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def to_issue_time(value: str):
    """Render a report timestamp in the MM/DD/YYYY HH:MM:SS the chatbot expects."""
    parsed = parse_local_timestamp(value)
    return parsed.strftime("%m/%d/%Y %H:%M:%S") if parsed else None


def summarize_blocker(hit: dict) -> dict:
    """Flatten one `red_top_blockers` hit into plain fields."""
    tb = hit["blocker"]
    level = tb.get("ActivityLevel") or 0
    return {
        "name": tb.get("Name", ""),
        "device": hit["device"],
        "activity_level": level,
        "activity_label": activity_label(level),
        "active_time_us": tb.get("ActiveTime") or 0,
        "active_time_pct": tb.get("ActiveTimePercent") or 0,
    }


def summarize_session_data(session: dict, devices=None) -> dict:
    """
    Describe one session as data: when it ran, how badly it dripped, and which
    device family its worst red blocker belongs to.
    """
    sw_pct, hw_pct = compute_drip_percentages(session)
    hits = red_top_blockers(session, devices=devices)
    duration_us = session.get("Duration") or 0
    start = session.get("EntryTimestampLocal")

    return {
        "session_id": session.get("SessionId"),
        "type": session.get("Type"),
        "type_label": MS_SESSION_TYPES.get(session.get("Type"), "Other"),
        "on_ac": bool(session.get("OnAc")),
        "start": start,
        "end": session.get("ExitTimestampLocal"),
        "issue_time": to_issue_time(start),
        "duration_us": duration_us,
        "duration": format_duration(duration_us),
        "sw_drip_pct": sw_pct,
        "hw_drip_pct": hw_pct,
        # The longest-running red blocker decides the device, so a caller never
        # has to guess which of several entries to route on.
        "device": hits[0]["device"] if hits else NO_DEVICE,
        "blockers": [summarize_blocker(hit) for hit in hits],
    }


def _drip_rank(summary: dict) -> float:
    """Sort key: the worse of the two drip percentages, missing values last."""
    known = [v for v in (summary["sw_drip_pct"], summary["hw_drip_pct"]) if v is not None]
    return min(known) if known else float("inf")


def collect_sessions(html_path: str, devices=DEVICE_ORDER) -> dict:
    """
    Load a Sleep Study report and return its qualifying sessions as data.

    `device` is the one-line routing answer — wifi, bt, other or none — and
    `sessions` lists every qualifying session worst drip first, so a report
    holding both a Wi-Fi session and a Bluetooth one is not flattened into a
    single verdict. `warnings` carries what a reader needs before trusting the
    result: a report with no sessions in it, or a timestamp this parser could
    not read.
    """
    # Used by the filter, by every session summary and by the criteria block
    # below, so it is frozen once here rather than consumed by the first use.
    devices = device_filter(devices)

    payload = load_sleepstudy(html_path)
    sessions = payload.get("ScenarioInstances") or []
    warnings = []

    if not sessions:
        warnings.append("The report contains no sleep sessions.")

    qualifying = filter_qualifying_sessions(sessions, devices=devices)
    summaries = [summarize_session_data(s, devices=devices) for s in qualifying]
    summaries.sort(key=_drip_rank)

    undated = [s["session_id"] for s in summaries if not s["issue_time"]]
    if undated:
        warnings.append(
            "Could not read the entry timestamp of session(s) "
            + ", ".join(str(i) for i in undated)
            + " — the issue time has to be filled in by hand."
        )

    return {
        "source": html_path,
        "total_sessions": len(sessions),
        "qualifying_count": len(summaries),
        "sessions": summaries,
        # `none` means the report is clean against the three criteria: there is
        # no log to open next, which is not the same as a blocker this tool
        # cannot attribute to Wi-Fi or Bluetooth (that one reports as `other`).
        "device": summaries[0]["device"] if summaries else NO_DEVICE,
        "devices": sorted({s["device"] for s in summaries}),
        "criteria": {
            "drip_threshold_pct": DRIP_THRESHOLD,
            "red_activity_level": RED_ACTIVITY_LEVEL,
            "devices": list(devices) if devices is not None else list(DEVICE_ORDER),
        },
        "warnings": warnings,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 6 — UNIFIED ANALYSIS GENERATOR (shared by web route + CLI)
# ══════════════════════════════════════════════════════════════════════════════

def analyze_sleepstudy_stream(html_path: str, llm_call=None):
    """
    Run the full pipeline and yield event dicts:

      {"type": "step",  "content": <markdown string>}      - progress message
      {"type": "done",  "data":    <final markdown string>} - terminal success event
      {"type": "error", "content": <error message>}        - terminal failure event

    `llm_call` is an optional callable with signature
        llm_call(system_prompt: str, user_message: str) -> str
    Pass `None` to skip LLM and emit only the raw extracted data.
    """
    try:
        yield {"type": "step", "content": f"Loading sleepstudy report: `{html_path}`"}
        payload  = load_sleepstudy(html_path)
        sessions = payload.get("ScenarioInstances", [])
        yield {"type": "step", "content": f"Loaded **{len(sessions)}** sessions from report."}

        yield {"type": "step", "content": (
            "Applying session filters:\n"
            "1. Drain (battery, not AC)\n"
            f"2. SW Drip < {DRIP_THRESHOLD}% **OR** HW Drip < {DRIP_THRESHOLD}%\n"
            "3. Intel Wi-Fi is a RED top offender (ActivityLevel = 3)"
        )}

        # The Wi-Fi report names Intel in its headings and in its fallback
        # verdict, so it asks criterion 3 about Intel Wi-Fi specifically.
        qualifying = filter_qualifying_sessions(
            sessions, devices=(DEVICE_WIFI,), vendor=WIFI_REPORT_VENDOR
        )
        yield {"type": "step", "content": f"Qualifying sessions: **{len(qualifying)}**"}

        if not qualifying:
            yield {
                "type": "done",
                "data": (
                    "No sleep sessions match all three criteria. "
                    "No Wi-Fi-related drain issues to report."
                ),
            }
            return

        combined_reports = []
        for i, sess in enumerate(qualifying, 1):
            sid = sess["SessionId"]
            sw_pct, hw_pct = compute_drip_percentages(sess)
            start = sess.get("EntryTimestampLocal", "?")
            dur   = format_duration(sess.get("Duration", 0))

            yield {"type": "step", "content": (
                f"### Session {sid}  ({i}/{len(qualifying)})\n"
                f"- Start: `{start}`\n"
                f"- Duration: `{dur}`\n"
                f"- SW Drip: **{sw_pct:.1f}%**, HW Drip: **{hw_pct:.1f}%**"
            )}

            yield {"type": "step", "content":
                f"Extracting Wi-Fi content (Activators / FX Devices / SRUM) for session {sid}..."}
            wifi_content = extract_wifi_content(sess)
            yield {"type": "step", "content": (
                f"Session {sid} extraction: "
                f"FX Devices Wi-Fi={len(wifi_content['fx_devices_wifi'])}, "
                f"Activators Wi-Fi={len(wifi_content['activators_wifi'])}, "
                f"NDIS apps={len(wifi_content['ndis_app_traffic'])}, "
                f"SRUM consumers={len(wifi_content['srum_network_apps'])}"
            )}

            summary = build_session_summary(sess, wifi_content)
            yield {"type": "step", "content": f"```\n{summary}\n```"}

            if llm_call is None:
                ai_report = "_LLM client not configured - showing raw extracted data only._"
            else:
                yield {"type": "step", "content": f"Calling LLM for session {sid} report..."}
                try:
                    user_msg = (
                        f"Please analyse Session ID {sid} from the Sleep Study report:\n\n"
                        f"```\n{summary}\n```"
                    )
                    ai_report = llm_call(SLEEPSTUDY_SYSTEM_PROMPT, user_msg) or ""
                except Exception as llm_err:
                    ai_report = f"_LLM call failed: {llm_err}_"

            section = (
                f"## AI Report - Session {sid}\n"
                f"_Start: {start} | Duration: {dur} | "
                f"SW Drip: {sw_pct:.1f}% | HW Drip: {hw_pct:.1f}%_\n\n"
                f"{ai_report}"
            )
            combined_reports.append(section)
            yield {"type": "step", "content": section}

        final_text = (
            f"# Sleepstudy Analysis Report\n"
            f"**Source:** `{html_path}`\n"
            f"**Qualifying sessions analysed:** {len(qualifying)} of {len(sessions)}\n\n"
            + "\n\n---\n\n".join(combined_reports)
        )
        yield {"type": "done", "data": final_text}

    except Exception as exc:
        yield {"type": "error", "content": str(exc)}


# ══════════════════════════════════════════════════════════════════════════════
#  STEP 7 — CLI ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def run_cli(html_path: str, llm_call=None) -> str:
    """
    Drive the generator from the command line, printing progress and returning
    the final markdown text. `llm_call` defaults to None (no LLM).
    """
    final_text = ""
    for event in analyze_sleepstudy_stream(html_path, llm_call=llm_call):
        kind = event["type"]
        if kind == "step":
            print(event["content"])
            print("-" * 60)
        elif kind == "done":
            final_text = event["data"]
            print("\n=== FINAL REPORT ===\n")
            print(final_text)
        elif kind == "error":
            print(f"ERROR: {event['content']}", file=sys.stderr)
    return final_text


if __name__ == "__main__":
    if len(sys.argv) > 1:
        html_file = sys.argv[1]
    else:
        here = Path(__file__).parent
        candidates = list(here.glob("*.html"))
        if not candidates:
            print("Usage: python sleepstudy_analyzer.py <path-to-sleepstudy.html>")
            sys.exit(1)
        html_file = str(candidates[0])
        print(f"Auto-detected: {html_file}")

    if not os.path.isfile(html_file):
        print(f"Error: file not found - {html_file}")
        sys.exit(1)

    final = run_cli(html_file, llm_call=None)
    if final:
        out_path = Path(html_file).stem + "_analysis_report.md"
        Path(out_path).write_text(final, encoding="utf-8")
        print(f"\nReport saved to: {out_path}")
