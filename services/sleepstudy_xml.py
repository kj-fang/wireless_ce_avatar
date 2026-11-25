"""
Read a Sleep Study XML report (`powercfg /sleepstudy /xml`, what the Power
team's MCS Analyzer generates) into the same shape as the HTML report's
embedded LocalSprData, so the rest of the analysis reads either.

The two hold the same data under different names and units:

    XML                                   LocalSprData (HTML)
    ScenarioInstance Id / Type="Sleep"    SessionId / Type 2 ("Screen Off" 1)
    OsStateInstance (OS timeline: Active, Screen Off, Sleep; no DRIPS)
                                          a session without DRIPS data, as in the HTML
    Ac="1"                                OnAc true
    Duration (100 ns)                     Duration (us)
    TimeStamp / LocalTimestamp (entry)    EntryTimestamp / EntryTimestampLocal ("...Z")
    LowPowerStateTime, HwLowPowerStateTime  Metadata Info.SwLowPowerStateTime / Info.HwLowPowerStateTime
    CsEnterReason="0x101 (Transition To Sleep)"  EnterReason "Transition To Sleep"
    TopBlocker ActivityLevel="high"       TopBlockers ActivityLevel 3
    Devices / Device / Blockers / Blocker + CustomData Data
                                          BlockerGroups "FX Devices" Blockers / Children + Metadata Values
    Activators / Activator                BlockerGroups "Activators"
    SrumData PowerEstimationRecord        SrumData PowerEstimationData Values
    ReportInformation UtcOffset "+8:00"   ReportInformation UtcOffset 480

Only what the analysis reads is carried over; the HTML-only extras
(DripsWakeSources, blocking-time buckets) are absent from the XML.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

_SESSION_TYPES = {"screen off": 1, "sleep": 2}
_ACTIVITY = {"neutral": 0, "info": 0, "low": 1, "moderate": 2, "medium": 2, "high": 3}
_POWER = ("Cpu", "Display", "Disk", "Mbb", "Network", "Soc", "Loss")


def is_sleepstudy_xml(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096).decode("utf-8", "replace")
    except OSError:
        return False
    return "<SleepStudy" in head


def _tag(e) -> str:
    return e.tag.rsplit("}", 1)[-1]


def _kids(e, name: str) -> list:
    return [c for c in e if _tag(c) == name]


def _num(value, kind=int, default=0):
    try:
        return kind(float(value)) if kind is int else kind(value)
    except (TypeError, ValueError):
        return default


def _reason(text) -> str:
    """'0x101 (Transition To Sleep)' -> 'Transition To Sleep'."""
    m = re.search(r"\(([^)]*)\)", text or "")
    return m.group(1) if m else (text or "")


def _offset_minutes(text) -> int | None:
    m = re.fullmatch(r"\s*([+-])?(\d{1,2}):(\d{2})\s*", text or "")
    if not m:
        return None
    minutes = int(m.group(2)) * 60 + int(m.group(3))
    return -minutes if m.group(1) == "-" else minutes


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(text: str):
    try:
        return datetime.strptime((text or "").rstrip("Z")[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def _metadata(e) -> dict:
    values = []
    for cd in _kids(e, "CustomData"):
        values += [{"Key": d.get("Key", ""), "Value": d.get("Value", "")} for d in _kids(cd, "Data")]
    return {"FriendlyName": "Detailed Blocker Information", "Values": values}


def _blocker(e) -> dict:
    """A Device / Activator / Blocker element and the blockers under it."""
    children = []
    for c in e:
        if _tag(c) == "Blocker":
            children.append(_blocker(c))
        elif _tag(c) == "Blockers":
            children += [_blocker(b) for b in _kids(c, "Blocker")]
    return {"Name": e.get("Name", ""), "ActiveTime": _num(e.get("ActiveTime")),
            "ActiveTimePercent": _num(e.get("ScenarioActiveTimePercent"), float, 0.0),
            "ActivityLevel": _ACTIVITY.get(str(e.get("ActivityLevel", "")).lower(), 0),
            "Metadata": _metadata(e), "Children": children}


def _group(instance, wrapper: str, item: str, name: str) -> dict:
    blockers = []
    for w in _kids(instance, wrapper):
        blockers += [_blocker(b) for b in _kids(w, item)]
    return {"Name": name, "Blockers": blockers, "Metadata": {"Values": []}}


def _srum(instance) -> dict:
    records = []
    for srum in _kids(instance, "SrumData"):
        for ped in _kids(srum, "PowerEstimationData"):
            for r in _kids(ped, "PowerEstimationRecord"):
                rec = {"AppId": r.get("AppId", ""), "UserId": r.get("UserId", "")}
                for p in _POWER:
                    rec[f"{p}PowerConsumption"] = _num(r.get(f"{p}PowerConsumption"))
                rec["TotalPowerConsumption"] = sum(rec[f"{p}PowerConsumption"] for p in _POWER)
                records.append(rec)
    return {"PowerEstimationData": {"Values": records}}


def _session(e) -> dict:
    duration_100ns = _num(e.get("Duration"))
    entry_utc, entry_local = _parse(e.get("TimeStamp")), _parse(e.get("LocalTimestamp"))
    length = timedelta(microseconds=duration_100ns // 10)
    s = {
        "Type": _SESSION_TYPES.get(str(e.get("Type", "")).lower(), 0),
        "TypeName": e.get("Type", ""),
        "SessionId": _num(e.get("Id")),
        "ActivityLevel": _ACTIVITY.get(str(e.get("ActivityLevel", "")).lower(), 0),
        "EnterReason": _reason(e.get("CsEnterReason")),
        "ExitReason": _reason(e.get("CsExitReason")),
        "Duration": duration_100ns // 10,
        "OnAc": e.get("Ac") == "1",
        "EntryRemainingCapacity": _num(e.get("EntryRemainingCapacity")),
        "ExitRemainingCapacity": _num(e.get("ExitRemainingCapacity")),
        "EntryFullChargeCapacity": _num(e.get("EntryFullCapacity")),
        "ExitFullChargeCapacity": _num(e.get("ExitFullCapacity")),
    }
    if entry_utc is not None:
        s["EntryTimestamp"], s["ExitTimestamp"] = _stamp(entry_utc), _stamp(entry_utc + length)
    if entry_local is not None:
        s["EntryTimestampLocal"], s["ExitTimestampLocal"] = _stamp(entry_local), _stamp(entry_local + length)
    values = [{"Key": "Info.EnterReason", "Value": s["EnterReason"]},
              {"Key": "Info.ExitReason", "Value": s["ExitReason"]}]
    if e.get("LowPowerStateTime") is not None:
        values.append({"Key": "Info.SwLowPowerStateTime", "Value": _num(e.get("LowPowerStateTime"))})
    if e.get("HwLowPowerStateTime") is not None:
        values.append({"Key": "Info.HwLowPowerStateTime", "Value": _num(e.get("HwLowPowerStateTime"))})
    s["Metadata"] = {"FriendlyName": "Detailed Session Information", "Values": values}
    top = []
    for tbs in _kids(e, "TopBlockers"):
        for tb in _kids(tbs, "TopBlocker"):
            top.append({"Name": tb.get("Name", ""), "ActiveTime": _num(tb.get("ActiveTime")),
                        "ActiveTimePercent": _num(tb.get("ScenarioActiveTimePercent"), float, 0.0),
                        "ActivityLevel": _ACTIVITY.get(str(tb.get("ActivityLevel", "")).lower(), 0),
                        "Type": tb.get("Type", ""), "ScenarioId": s["SessionId"]})
    s["TopBlockers"] = top or None
    s["BlockerGroups"] = [_group(e, "Activators", "Activator", "Activators"),
                          _group(e, "Devices", "Device", "FX Devices")]
    s["SrumData"] = _srum(e)
    return s


def _os_state(e) -> dict:
    """An OsStateInstance: the OS's own timeline (Active, Screen Off, Sleep...),
    with times and reasons but no DRIPS or blockers. The HTML report lists
    these among its sessions too (a Sleep there without DRIPS data)."""
    duration_100ns = _num(e.get("Duration"))
    s = {
        "Type": _SESSION_TYPES.get(str(e.get("Type", "")).lower(), 0),
        "TypeName": e.get("Type", ""),
        "SessionId": _num(e.get("Id")),
        "ActivityLevel": _ACTIVITY.get(str(e.get("ActivityLevel", "")).lower(), 0),
        "EnterReason": e.get("EntryReason", ""),
        "ExitReason": e.get("ExitReason", ""),
        "Duration": duration_100ns // 10,
        "OnAc": e.get("Ac") == "1",
        "EntryRemainingCapacity": _num(e.get("EntryRemainingCapacity")),
        "ExitRemainingCapacity": _num(e.get("ExitRemainingCapacity")),
        "EntryFullChargeCapacity": _num(e.get("EntryFullCapacity")),
        "ExitFullChargeCapacity": _num(e.get("ExitFullCapacity")),
        "Metadata": {"FriendlyName": "Detailed Session Information",
                     "Values": [{"Key": "Info.EnterReason", "Value": e.get("EntryReason", "")},
                                {"Key": "Info.ExitReason", "Value": e.get("ExitReason", "")}]},
        "TopBlockers": None, "BlockerGroups": [],
    }
    for key, attr in (("EntryTimestamp", "EntryTimestamp"), ("EntryTimestampLocal", "LocalTimestamp"),
                      ("ExitTimestamp", "ExitTimestamp"), ("ExitTimestampLocal", "ExitLocalTimestamp")):
        dt = _parse(e.get(attr))
        if dt is not None:
            s[key] = _stamp(dt)
    return s


def load_sleepstudy_xml(path: str) -> dict:
    """The XML report as a LocalSprData-shaped dict. Raises ValueError for an
    XML file that is not a Sleep Study report."""
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ValueError(f"not a readable Sleep Study XML report: {exc}") from exc
    if _tag(root) != "SleepStudy":
        raise ValueError("not a Sleep Study XML report (no <SleepStudy> root)")
    info = {}
    for ri in _kids(root, "ReportInformation"):
        info = {_tag(c): (c.text or "").strip() for c in ri}
    offset = _offset_minutes(info.get("UtcOffset"))
    sessions = []
    for wrapper in _kids(root, "ScenarioInstances"):
        for e in wrapper:
            if _tag(e) == "ScenarioInstance":
                sessions.append(_session(e))
            elif _tag(e) == "OsStateInstance":
                sessions.append(_os_state(e))
    return {
        "ReportInformation": {**info, "UtcOffset": offset if offset is not None else info.get("UtcOffset"),
                              "SourceFormat": "xml"},
        "ScenarioInstances": sessions,
    }
