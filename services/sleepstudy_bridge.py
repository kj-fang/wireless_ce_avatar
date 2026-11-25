"""
The Sleep Study driver-log analysis for other tools: one analysis, three ways in.

Each builds the job JSON of services/sleepstudy_cli.py and runs that command
line in a child process (`IntelAvatar.exe sleepstudy-analyze --input job.json
--out <dir>`, or `python -m services.sleepstudy_cli` from a checkout), then
reads its result.json -- so every caller gets the same analysis and output.

  1. Power team's MCS Analyzer: its etl-trace-request file lists the bad
     sessions it wants traced; the Wi-Fi / Bluetooth ones come here.
       IntelAvatar.exe sleepstudy-bridge from-mcs <etl-trace-request.json> --report <html> [--logs <dir> | --collect]
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
    return {"schema_version": "1.0", "case_id": case_id, "report": report,
            "driver_logs": [logs] if logs else [], "sessions": sessions or [],
            "options": {"collect": collect, "dry_run": dry_run}}


def session_request(session_id="", issue_time="", question="") -> list:
    """The job's sessions list for one optional session."""
    if not str(session_id or "").strip():
        return []
    s = {"session_id": str(session_id).strip()}
    if issue_time:
        s["issue_time"] = issue_time
    if question:
        s["issue_description"] = question
    return [s]


def run_analysis(job: dict, out_dir: Optional[str] = None,
                 on_line: Optional[Callable[[str], None]] = None) -> dict:
    """Run the command line on `job` in a child process; its result.json (written
    even when the job fails), plus _exit_code and _out_dir."""
    from services import sleepstudy_cli as cli
    out_dir = out_dir or cli.default_out_dir()
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
    for s in result.get("sessions") or []:
        finding = s.get("finding") or {}
        lines.append(f"- Session {s.get('session_id')} [{s.get('device')}] {s.get('status')}/{s.get('code')}: "
                     + (finding.get("root_cause_summary") or s.get("message") or ""))
    return "\n".join(lines)


# ── 1. MCS Analyzer's etl-trace-request ───────────────────────────────────────

def job_from_mcs_request(request: dict, report: str, logs: str = "", collect: bool = False,
                         question: str = "", dry_run: bool = False) -> dict:
    """The sessions MCS Analyzer wants traced whose blocker is a Wi-Fi or
    Bluetooth device, as a job; the others (audio, storage...) are not ours."""
    sessions = []
    for target in request.get("target_scenarios") or []:
        families = {DRIVER_FAMILIES.get(str(f).lower()) for f in target.get("device_families") or []}
        if not families - {None} or not str(target.get("session_id") or "").strip():
            continue
        why = ", ".join(request.get("reason") or [])
        sessions += session_request(target["session_id"], question=question or (
            f"MCS Analyzer flagged {', '.join(target.get('blocker_names') or ['this device'])}"
            + (f" ({why})" if why else "")))
    return make_job(report=report, logs=logs, sessions=sessions, collect=collect, dry_run=dry_run,
                    case_id="mcs-" + datetime.now().strftime("%Y%m%d-%H%M%S"))


# ── 2. Avatar's MCP service adapter ───────────────────────────────────────────

async def execute(task, progress) -> dict:
    """
    task.payload: source_path (the Sleep Study report, optional with collect),
    result_dir, fields {session_id, time (ISO 8601 with offset), question,
    logs, collect, dry_run}. `progress(pct, message)` may be sync or async.
    Returns JSON; files are listed as {"file_id", "name", "size", "path"}
    inside result_dir, the shape the service lists.
    """
    payload = task.payload
    fields = payload.get("fields") or {}
    job = make_job(report=payload.get("source_path") or "", logs=fields.get("logs") or "",
                   sessions=session_request(fields.get("session_id"), fields.get("time"), fields.get("question")),
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
            "question": {"type": "string", "description": "What to find out about that session. Needs session_id."},
            "collect": {"type": "boolean", "description": "Collect the Intel Wi-Fi driver log from this machine."},
            "dry_run": {"type": "boolean", "description": "Check log coverage and cut the excerpts only, without the LLM."},
        },
    },
}


def call_tool(args: dict, notify: Optional[Callable[[str], None]] = None) -> dict:
    job = make_job(report=args.get("report") or "", logs=args.get("logs") or "",
                   sessions=session_request(args.get("session_id"), args.get("issue_time"), args.get("question")),
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
        mid, method, params = message.get("id"), message.get("method"), message.get("params") or {}
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

def main(argv: list) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="sleepstudy-bridge", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("mcp", help="serve the analysis as an MCP tool over stdio")
    m = sub.add_parser("from-mcs", help="analyse the Wi-Fi/BT sessions of MCS Analyzer's etl-trace-request")
    m.add_argument("request", help="MCS Analyzer's <basename>-etl-trace-request-<date>.json")
    m.add_argument("--report", default="", help="the Sleep Study report MCS Analyzer read")
    m.add_argument("--logs", default="", help="driver log capture folder")
    m.add_argument("--collect", action="store_true", help="collect the Intel Wi-Fi driver log from this machine")
    m.add_argument("--question", default="")
    m.add_argument("--dry-run", action="store_true")
    m.add_argument("--out")
    a = ap.parse_args(argv)
    if a.cmd == "mcp":
        serve_stdio()
        return 0
    try:
        with open(a.request, encoding="utf-8") as fh:
            request = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"cannot read {a.request}: {exc}", file=sys.stderr)
        return 2
    job = job_from_mcs_request(request, a.report, a.logs, a.collect, a.question, a.dry_run)
    if not job["sessions"]:
        print("No Wi-Fi or Bluetooth session in the trace request: nothing for the driver-log analysis.")
        return 0
    result = run_analysis(job, a.out, on_line=lambda text: print("  ", text, flush=True))
    print(summary_text(result))
    return result["_exit_code"]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
