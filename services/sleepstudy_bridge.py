"""
The Sleep Study driver-log analysis for other tools: one analysis, three ways in.

Each builds the job JSON of services/sleepstudy_cli.py and runs that command
line in a child process (`IntelAvatar.exe sleepstudy-analyze --input job.json
--out <dir>`, or `python -m services.sleepstudy_cli` from a checkout), then
reads its result.json -- so every caller gets the same analysis and output.

  1. Power team's MCS Analyzer:
     a. its ETW investigation plan (the ETW input MCS documents; its
        etl-trace-request sidecar is read the same) lists the bad Sleep Study
        sessions it wants traced; the Wi-Fi / Bluetooth ones come here, with
        the device's ACPI path. Without --report, the report the plan names
        (source_sleepstudy) is used, from that path or from beside the plan.
          IntelAvatar.exe sleepstudy-bridge from-mcs <etw-investigation-plan.json> [--report <xml|html>] [--logs <dir> | --collect]
     b. its etl-findings describe its live ETW capture: the standby Windows
        confirmed in it (Kernel-Power 506/507) becomes the window, and its
        measurements (Wi-Fi time out of D3, Netwaw ISR/DPC counts) become
        evidence the driver-log agent has to explain. Run right after MCS, on
        the same machine, so the driver log still holds the capture:
          IntelAvatar.exe sleepstudy-bridge from-mcs-etw <etl-findings.json> --collect
  2. Avatar's MCP service (Streamable HTTP): the adapter its task manager calls,
       async execute(task, progress) -> dict
  3. MCP over stdio, for any MCP client (MCS Analyzer already drives
     Microsoft's ETW MCP server this way; VS Code too):
       IntelAvatar.exe sleepstudy-bridge mcp
     stdout is the protocol channel: app.py routes here before it prints.

Standard library only; JSON-RPC 2.0, one message per line.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from typing import Callable, Optional

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "sleepstudy-driver", "version": "0.1.0"}
# MCS Analyzer's device family names -> the devices this analysis has a driver log for.
DRIVER_FAMILIES = {"wi-fi": "wifi", "wifi": "wifi", "wlan": "wifi", "bluetooth": "bt"}


# ── What every way in shares ──────────────────────────────────────────────────

def make_job(report: str = "", logs: str = "", sessions: Optional[list] = None, case_id: str = "",
             collect: bool = False, dry_run: bool = False) -> dict:
    # Paths are made absolute here, against the caller's folder: from a
    # checkout the child process runs in the repository folder instead.
    return {"schema_version": "1.1", "case_id": case_id,
            "report": os.path.abspath(report) if report else "",
            "driver_logs": [os.path.abspath(logs)] if logs else [], "sessions": sessions or [],
            "options": {"collect": collect, "dry_run": dry_run}}


def session_request(session_id="", issue_time="", question="", *, window_start="", window_end="",
                    device="", device_id="", evidence=None, force=False) -> list:
    """The job's sessions list for one optional session or window ([] when
    neither is given: the report's own bad sessions are analysed). A window
    with one end missing is passed on, for the command line to refuse."""
    s = {"session_id": str(session_id or "").strip(), "issue_time": issue_time,
         "window_start": window_start, "window_end": window_end, "issue_description": question,
         "device": device, "device_id": device_id, "evidence": list(evidence or []), "force": force}
    s = {k: v for k, v in s.items() if v}
    if not (s.get("session_id") or s.get("issue_time") or window_start or window_end):
        return []
    return [s]


def run_analysis(job: dict, out_dir: Optional[str] = None,
                 on_line: Optional[Callable[[str], None]] = None) -> dict:
    """Run the command line on `job` in a child process; its result.json (written
    even when the job fails), plus _exit_code and _out_dir."""
    from services import sleepstudy_cli as cli
    out_dir = os.path.abspath(out_dir or cli.default_out_dir())
    os.makedirs(out_dir, exist_ok=True)
    job_path = os.path.join(out_dir, "job.json")
    with open(job_path, "w", encoding="utf-8") as fh:
        json.dump(job, fh, indent=2)
    exe, params, cwd = cli._self_command(["--input", job_path, "--out", out_dir])
    proc = subprocess.Popen([exe, *params], cwd=cwd, env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for line in proc.stdout:              # never inherited: under `mcp` our stdout is the protocol
        if on_line and line.startswith("[sleepstudy]"):
            on_line(line[len("[sleepstudy]"):].strip())
    code = proc.wait()
    try:
        with open(os.path.join(out_dir, "result.json"), encoding="utf-8") as fh:
            result = json.load(fh)
    except (OSError, ValueError):
        result = {"status": "failed", "code": "E_NO_RESULT",
                  "message": f"the analysis exited {code} without writing result.json"}
    result["_exit_code"], result["_out_dir"] = code, out_dir
    return result


def summary_text(result: dict) -> str:
    """What an LLM reads back: the job, then one line per session, finding first."""
    lines = [f"Sleep Study driver analysis: {result.get('status')} ({result.get('code', 'OK')}); "
             f"output in {result.get('_out_dir')}"]
    if result.get("message"):
        lines.append(result["message"])
    for device, r in (result.get("driver_log_reach") or {}).items():
        lines.append(f"{'Wi-Fi' if device == 'wifi' else 'Bluetooth'} driver log reaches back to "
                     f"{'' if r.get('exact') else 'at least '}{r.get('from')}; bad sessions older than it: "
                     f"{', '.join(r.get('bad_sessions_older') or []) or 'none'}"
                     + (f"; before it, not known until the ETL is decoded: {', '.join(r['bad_sessions_unknown'])}"
                        if r.get("bad_sessions_unknown") else ""))
    for s in result.get("sessions") or []:
        finding = s.get("finding") or {}
        lines.append(f"- Session {s.get('session_id')} [{s.get('device')}] {s.get('status')}/{s.get('code')}: "
                     + (finding.get("root_cause_summary") or s.get("message") or ""))
    return "\n".join(lines)


# ── 1. MCS Analyzer's investigation plan ──────────────────────────────────────

def report_of_plan(plan: dict, plan_path: str) -> str:
    """The Sleep Study report an MCS investigation plan was derived from (its
    source_sleepstudy): at the path the plan gives, else beside the plan,
    where a copied run folder has it; "" when it is at neither."""
    source = str(plan.get("source_sleepstudy") or "").strip()
    if not source:
        return ""
    beside = os.path.join(os.path.dirname(os.path.abspath(plan_path)),
                          os.path.basename(source.replace("\\", "/")))
    return next((os.path.abspath(p) for p in (source, beside) if os.path.isfile(p)), "")


def job_from_mcs_request(request: dict, report: str, logs: str = "", collect: bool = False,
                         question: str = "", dry_run: bool = False) -> dict:
    """The sessions MCS Analyzer wants traced (its investigation plan or its
    etl-trace-request: the same target_scenarios) whose blocker is a Wi-Fi or
    Bluetooth device, as a job; the others (audio, storage...) are not ours."""
    sessions = []
    why = ", ".join(request.get("reason") or [])
    for target in request.get("target_scenarios") or []:
        sid = str(target.get("session_id") or "").strip()
        devices = [d for d in (DRIVER_FAMILIES.get(str(f).lower()) for f in target.get("device_families") or []) if d]
        if not sid or not devices:
            continue
        for device in dict.fromkeys(devices):
            mine = [t for t in target.get("target_devices") or []
                    if DRIVER_FAMILIES.get(str(t.get("family") or "").lower()) == device]
            evidence = [f"MCS Analyzer flagged {t.get('name')} ({t.get('sleepstudy_type') or 'blocker'})"
                        for t in mine] or [f"MCS Analyzer flagged {', '.join(target.get('blocker_names') or ['this device'])}"]
            if why:
                evidence.append(f"MCS Analyzer reasons: {why}")
            if target.get("drain_mwh") is not None:
                evidence.append(f"Battery drained {target['drain_mwh']} mWh in this session (MCS Analyzer)")
            sessions += session_request(sid, question=question, device=device,
                                        device_id=(mine[0].get("acpi_path") if mine else "") or "",
                                        evidence=evidence)
    return make_job(report=report, logs=logs, sessions=sessions, collect=collect, dry_run=dry_run,
                    case_id="mcs-" + datetime.now().strftime("%Y%m%d-%H%M%S"))


# ── 1b. MCS Analyzer's etl-findings (its live ETW capture) ────────────────────

_WIRELESS_DEVICE = (("wifi", re.compile(r"(?i)wi-?fi|wlan|\\CNVW\b")),
                    ("bt", re.compile(r"(?i)bluetooth")))
# Intel wireless kernel drivers, as MCS Analyzer names them in its ISR/DPC counts.
_WIRELESS_DRIVER = (("wifi", re.compile(r"(?i)^netw[a-z]{2}\d*\.sys$")),
                    ("bt", re.compile(r"(?i)^ibt[a-z]*\.sys$")))


def _family(name: str, table) -> str:
    return next((device for device, rx in table if rx.search(name or "")), "")


def _utc(text: str) -> str:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%H:%M:%S") if text else "?"


def job_from_mcs_etw(findings: dict, report: str = "", logs: str = "", collect: bool = False,
                     question: str = "", device: str = "", dry_run: bool = False) -> dict:
    """
    MCS Analyzer's live ETW capture (its etl-findings JSON) as a job: one
    request per Intel wireless device MCS saw active, over the standby that
    Windows confirmed inside the capture (Kernel-Power 506/507), carrying
    MCS's measurements (time out of D3, ISR/DPC per driver) as evidence for
    the agent to explain from the driver log. `device` asks for one device
    even when MCS saw nothing of it.
    """
    power = (findings.get("capture_context") or {}).get("power_events") or {}
    standby = [s for s in power.get("confirmed_sleep_sessions_within_capture") or []
               if s.get("started_utc") and s.get("ended_utc")]
    if not standby:
        return make_job(report=report, logs=logs, collect=collect, dry_run=dry_run)
    lo = min(s["started_utc"] for s in standby)
    hi = max(s["ended_utc"] for s in standby)
    activity = findings.get("standby_device_activity") or {}
    devices = activity.get("devices_out_of_d3") or []
    total = activity.get("standby_seconds")

    evidence_common = []
    for s in sorted(standby, key=lambda s: s["started_utc"]):
        secs = s.get("session_seconds") or 0
        drips = s.get("drips_seconds")
        pct = f" ({drips / secs * 100:.0f}%)" if drips is not None and secs else ""
        evidence_common.append(
            f"MCS ETW: Windows-confirmed standby {_utc(s['started_utc'])}-{_utc(s['ended_utc'])} UTC, "
            f"{secs:.0f} s, DRIPS {drips or 0:.0f} s{pct}, HW DRIPS {s.get('hardware_drips_seconds') or 0:.0f} s, "
            f"exit: {s.get('exit_reason') or '?'}")
    others = [d for d in devices if not _family(f"{d.get('device')} {d.get('acpi_path')}", _WIRELESS_DEVICE)]
    if others:
        evidence_common.append("MCS ETW: other devices out of D3 in standby: " + "; ".join(
            f"{d.get('device')} {d.get('seconds')} s" for d in others[:3]))

    wanted = {device} if device else set()
    per_device = {}
    for d in devices:
        fam = _family(f"{d.get('device')} {d.get('acpi_path')}", _WIRELESS_DEVICE)
        if fam:
            wanted.add(fam)
            per_device.setdefault(fam, []).append(d)
    drivers = {}
    for kind in ("interrupts_by_driver", "dpcs_by_driver"):
        for row in activity.get(kind) or []:
            fam = _family(str(row.get("driver") or ""), _WIRELESS_DRIVER)
            if fam:
                wanted.add(fam)
                drivers.setdefault((fam, row["driver"]), {})[kind] = row.get("count")

    sessions = []
    for fam in sorted(wanted):
        evidence = list(evidence_common)
        for d in per_device.get(fam, []):
            evidence.append(f"MCS ETW: {d.get('device')} ({d.get('acpi_path')}) out of D3 (in {d.get('state')}) "
                            f"{d.get('seconds')} s = {d.get('percent_of_standby')}% of "
                            f"{total or '?'} s standby, {d.get('entries')} entries")
        for (f, name), counts in sorted(drivers.items()):
            if f == fam:
                evidence.append(f"MCS ETW: {name} during standby: {counts.get('interrupts_by_driver', 0)} "
                                f"interrupts, {counts.get('dpcs_by_driver', 0)} DPCs")
        if not per_device.get(fam) and not any(f == fam for f, _ in drivers):
            evidence.append(f"MCS ETW saw no {'Wi-Fi' if fam == 'wifi' else 'Bluetooth'} device out of D3 "
                            "and no interrupts/DPCs from its driver in standby")
        acpi = next((d.get("acpi_path") for d in per_device.get(fam, []) if d.get("acpi_path")), "")
        sessions += session_request(window_start=lo, window_end=hi, question=question, device=fam,
                                    device_id=acpi, evidence=evidence, force=True)
    return make_job(report=report, logs=logs, sessions=sessions, collect=collect, dry_run=dry_run,
                    case_id="mcs-etw-" + datetime.now().strftime("%Y%m%d-%H%M%S"))


# ── 2. Avatar's MCP service adapter ───────────────────────────────────────────

async def execute(task, progress) -> dict:
    """
    task.payload: source_path (the Sleep Study report, optional with collect),
    result_dir, fields {session_id, time (ISO 8601 with offset), question,
    window_start, window_end, device, device_id, evidence, force, logs,
    collect, dry_run}. `progress(pct, message)` may be sync or async.
    Returns JSON; files are listed as {"file_id", "name", "size", "path"}
    inside result_dir, the shape the service lists.
    """
    payload = task.payload
    fields = payload.get("fields") or {}
    job = make_job(report=payload.get("source_path") or "", logs=fields.get("logs") or "",
                   sessions=session_request(fields.get("session_id"), fields.get("time"), fields.get("question"),
                                            window_start=fields.get("window_start") or "",
                                            window_end=fields.get("window_end") or "",
                                            device=fields.get("device") or "", device_id=fields.get("device_id") or "",
                                            evidence=fields.get("evidence"), force=bool(fields.get("force"))),
                   case_id=str(getattr(task, "task_id", "") or ""),
                   collect=bool(fields.get("collect")), dry_run=bool(fields.get("dry_run")))
    loop = asyncio.get_running_loop()
    steps = [0]

    def on_line(message: str):                       # on the worker thread
        steps[0] += 1
        pending = progress(min(90, 10 + 10 * steps[0]), message)
        if asyncio.iscoroutine(pending):
            asyncio.run_coroutine_threadsafe(pending, loop)

    result = await asyncio.to_thread(run_analysis, job, payload.get("result_dir"), on_line)
    files = []
    for name in ("report.html", "report.md", "result.json"):
        path = os.path.join(result["_out_dir"], name)
        if os.path.exists(path):
            files.append({"file_id": str(len(files)), "name": name, "size": os.path.getsize(path), "path": path})
    return {"status": result.get("status"), "summary": summary_text(result), "result": result, "files": files}


# ── 3. MCP over stdio ─────────────────────────────────────────────────────────

TOOL = {
    "name": "analyze_sleepstudy_driver_log",
    "description": (
        "Find why Intel Wi-Fi or Bluetooth kept a Windows laptop out of Modern Standby (low DRIPS), "
        "from a Sleep Study report and the Intel driver log. Picks the battery sessions with low DRIPS "
        "and a RED Wi-Fi/BT blocker (or the session_id given), cuts the driver log to each session's "
        "window on the customer clock, and returns a root cause per session. Takes a few minutes. "
        "collect=true takes the driver log (and the report) from this machine; Windows asks for admin once."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "report": {"type": "string", "description": "Sleep Study report .html (powercfg /sleepstudy). Optional with collect."},
            "logs": {"type": "string", "description": "Capture folder with the driver log (WifiDriver*.etl, WRT WiFiLog, BT .hci.txt)."},
            "session_id": {"type": "string", "description": "Analyse this session only."},
            "issue_time": {"type": "string", "description": "When the issue happened, ISO 8601 with offset (2026-11-25T16:00:00+08:00). Needs session_id."},
            "question": {"type": "string", "description": "What to find out about that session or window."},
            "window_start": {"type": "string", "description": "Analyse this window instead of a session (ISO 8601 with offset), e.g. another tool's live capture; with window_end."},
            "window_end": {"type": "string", "description": "End of that window (ISO 8601 with offset)."},
            "device": {"type": "string", "enum": ["wifi", "bt"], "description": "Whose driver log to read for that session or window."},
            "device_id": {"type": "string", "description": "The device's ACPI path or name, as the caller knows it."},
            "evidence": {"type": "array", "items": {"type": "string"},
                         "description": "Findings of the caller's own analysis of that standby (e.g. ETW: Wi-Fi out of D3 169 s), for the driver log to explain."},
            "force": {"type": "boolean", "description": "Analyse session_id even if it is not a bad wireless session."},
            "collect": {"type": "boolean", "description": "Collect the Intel Wi-Fi driver log from this machine."},
            "dry_run": {"type": "boolean", "description": "Check log coverage and cut the excerpts only, without the LLM."},
        },
    },
}


def call_tool(args: dict, notify: Optional[Callable[[str], None]] = None) -> dict:
    job = make_job(report=args.get("report") or "", logs=args.get("logs") or "",
                   sessions=session_request(args.get("session_id"), args.get("issue_time"), args.get("question"),
                                            window_start=args.get("window_start") or "",
                                            window_end=args.get("window_end") or "",
                                            device=args.get("device") or "", device_id=args.get("device_id") or "",
                                            evidence=args.get("evidence"), force=bool(args.get("force"))),
                   collect=bool(args.get("collect")), dry_run=bool(args.get("dry_run")), case_id="mcp")
    result = run_analysis(job, on_line=notify)
    return {"content": [{"type": "text", "text": summary_text(result)}],
            "structuredContent": result, "isError": result.get("status") != "ok"}


def serve_stdio(stdin=None, stdout=None) -> None:
    channel = stdout or sys.stdout
    stdin = stdin or sys.stdin
    sys.stdout = sys.stderr                  # a stray print() must not land in the protocol

    def send(message: dict):
        channel.write(json.dumps(message, ensure_ascii=False) + "\n")
        channel.flush()

    for raw in stdin:
        if not raw.strip():
            continue
        try:
            message = json.loads(raw)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        if not isinstance(message, dict):    # null, a list (batches are not served), a bare value
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}})
            continue
        mid, method, params = message.get("id"), message.get("method"), message.get("params")
        if not isinstance(params, dict):
            params = {}
        if mid is None:                      # notifications: initialized, cancelled...
            continue
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                      "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [TOOL]}
        elif method == "tools/call" and params.get("name") == TOOL["name"]:
            token = (params.get("_meta") or {}).get("progressToken")
            steps = [0]

            def notify(text: str):
                if token is not None:
                    steps[0] += 1
                    send({"jsonrpc": "2.0", "method": "notifications/progress",
                          "params": {"progressToken": token, "progress": steps[0], "message": text}})
            try:
                result = call_tool(params.get("arguments") or {}, notify)
            except Exception as exc:         # noqa: BLE001 -- a tool error goes back to the client
                result = {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}], "isError": True}
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method: {method}"}})
            continue
        send({"jsonrpc": "2.0", "id": mid, "result": result})


# ── Command line ──────────────────────────────────────────────────────────────

def _utf8_streams() -> None:
    """Streams another program reads or writes are in the ANSI code page on
    Windows unless set: MCP is UTF-8, and progress carries arrows and emoji."""
    for stream, errors in ((sys.stdin, None), (sys.stdout, "replace"), (sys.stderr, "replace")):
        try:
            stream.reconfigure(encoding="utf-8", errors=errors)
        except (AttributeError, ValueError):
            pass


def main(argv: list) -> int:
    import argparse
    _utf8_streams()
    ap = argparse.ArgumentParser(prog="sleepstudy-bridge", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("mcp", help="serve the analysis as an MCP tool over stdio")
    m = sub.add_parser("from-mcs", help="analyse the Wi-Fi/BT sessions of MCS Analyzer's ETW investigation plan")
    m.add_argument("request", metavar="plan",
                   help="MCS Analyzer's <basename>-etw-investigation-plan-<date>.json "
                        "(its etl-trace-request sidecar is read the same)")
    m.add_argument("--report", default="",
                   help="the Sleep Study report MCS Analyzer read (.html or .xml); default: the one the plan "
                        "names (source_sleepstudy), at that path or beside the plan")
    m.add_argument("--logs", default="", help="driver log capture folder")
    m.add_argument("--collect", action="store_true", help="collect the Intel Wi-Fi driver log from this machine")
    m.add_argument("--question", default="")
    m.add_argument("--dry-run", action="store_true")
    m.add_argument("--out")
    e = sub.add_parser("from-mcs-etw",
                       help="analyse the driver log over MCS Analyzer's live ETW capture (its etl-findings)")
    e.add_argument("request", metavar="etl_findings", help="MCS Analyzer's <basename>-etl-findings-<date>.json")
    e.add_argument("--report", default="", help="a Sleep Study report holding the capture (made by --collect if absent)")
    e.add_argument("--logs", default="", help="driver log capture folder")
    e.add_argument("--collect", action="store_true", help="collect the Intel Wi-Fi driver log from this machine")
    e.add_argument("--device", choices=("wifi", "bt"), default="",
                   help="analyse this device even if MCS saw nothing of it")
    e.add_argument("--question", default="")
    e.add_argument("--dry-run", action="store_true")
    e.add_argument("--out")
    a = ap.parse_args(argv)
    if a.cmd == "mcp":
        serve_stdio()
        return 0
    try:
        with open(a.request, encoding="utf-8-sig") as fh:
            request = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"cannot read {a.request}: {exc}", file=sys.stderr)
        return 2
    if a.cmd == "from-mcs-etw":
        job = job_from_mcs_etw(request, a.report, a.logs, a.collect, a.question, a.device, a.dry_run)
        nothing = ("No Intel Wi-Fi/Bluetooth activity in MCS Analyzer's capture (no device out of D3, "
                   "no driver interrupts/DPCs), or no confirmed standby: nothing for the driver-log "
                   "analysis. Add --device wifi to look anyway.")
    else:
        report = a.report or report_of_plan(request, a.request)
        job = job_from_mcs_request(request, report, a.logs, a.collect, a.question, a.dry_run)
        nothing = "No Wi-Fi or Bluetooth session in the plan: nothing for the driver-log analysis."
    if not job["sessions"]:
        print(nothing)
        return 0
    if a.cmd == "from-mcs" and not a.report:
        if job["report"]:
            print(f"Sleep Study report named by the plan: {job['report']}")
        elif request.get("source_sleepstudy") and not a.collect:
            print(f"The plan's Sleep Study report is not at {request['source_sleepstudy']} nor beside "
                  "the plan: pass it with --report.", file=sys.stderr)
    for s in job["sessions"]:
        where = s.get("session_id") or f"{s.get('window_start')} -> {s.get('window_end')}"
        print(f"  {s.get('device') or 'wifi'} {s.get('device_id') or ''} @ {where}")
        for line in s.get("evidence") or []:
            print(f"      · {line}")
    result = run_analysis(job, a.out, on_line=lambda text: print("  ", text, flush=True))
    print(summary_text(result))
    return result["_exit_code"]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
