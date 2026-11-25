"""
Command line for the Sleep Study -> driver log analysis, for tools outside
Avatar (e.g. the Power team's analyzer): one call, a JSON result, no web page,
History or Avatar sign-in.

    IntelAvatar.exe sleepstudy-analyze --report <sleepstudy.html> --logs <capture folder>
                                       --out <output folder> [options]
    IntelAvatar.exe sleepstudy-analyze --collect [--report <sleepstudy.html>] --out <dir> [options]
    IntelAvatar.exe sleepstudy-analyze --input <job.json> --out <output folder>

--collect takes the Intel Wi-Fi driver log from this machine instead of
--logs (services/driver_log_collector.py): it flushes and copies the
driver's always-on autologger (WiFiDriverIHVSession) into <out>/capture,
then analyses it. Run it from an administrator prompt soon after the
standby; without --report it also runs powercfg /sleepstudy.

    (in a checkout: python -m services.sleepstudy_cli <same arguments>)

Options for the first form:
    --session-id ID     analyse this session (repeatable); default: the worst
                        bad wireless sessions, as the NW page lists them
    --issue-time T      ISO 8601 with an offset; the driver log is cut from
                        5 min before to 10 min after it, inside the session
    --window-start T / --window-end T
                        the exact window to cut instead (ISO 8601, offset)
    --question TEXT     the caller's focus, added to the agent's question
    --case-id ID        echoed in the result
    --dry-run           everything up to the LLM: selection, log matching,
                        time zones, ETL decoding, cuts; no agent call

A job file carries the same, for any number of sessions:
    {
      "schema_version": "1.0",
      "case_id": "CASE-12345",
      "report": "D:\\case\\sleepstudy-report.html",
      "driver_logs": ["D:\\case\\WRT"],
      "sessions": [
        {"session_id": "704",
         "issue_time": "2026-10-25T12:29:07-07:00",
         "window_start": "...", "window_end": "...",
         "issue_description": "Why did Wi-Fi stay in D0?"}
      ],
      "options": {"dry_run": false, "collect": false}
    }
    ("collect": true replaces "driver_logs" and makes "report" optional.)

Output, in --out:
    result.json   the contract below (SCHEMA_VERSION), for programs
    report.md     the same results for people
    excerpts/     the driver-log cut each analysed session was given

Exit code says whether the job ran, not what each session found:
    0  ran; each session's outcome is in result.json (status / code)
    1  unexpected failure          2  invalid input (E_INPUT_INVALID)
    3  report unreadable           4  no LLM configured (E_AGENT_CONFIG)
    5  --collect failed: E_NOT_ADMIN, E_DRIVER_TRACE_NOT_RUNNING,
       E_DRIVER_LOG_COPY_FAILED, E_SLEEPSTUDY_FAILED
result.json is written in every case the output folder can be created.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional

SCHEMA_VERSION = "1.0"
TOOL_VERSION = "0.1.0"

EXIT_OK, EXIT_ERROR, EXIT_INPUT, EXIT_REPORT, EXIT_AGENT, EXIT_COLLECT = 0, 1, 2, 3, 4, 5

# Per session: (status, code) for each outcome of the pipeline. A missing
# log, a log without the window, a failed decode and a finished analysis are
# different next steps for the caller, so none of them is folded into another.
STATUS_CODES = {
    "analyzed": ("completed", "OK"),
    "dry_run": ("dry_run", "OK"),
    "no_driver_log": ("no_driver_log", "E_DRIVER_LOG_NOT_FOUND"),
    "not_covered": ("not_covered", "E_LOG_NOT_COVERING_WINDOW"),
    "no_lines": ("empty_window", "E_LOG_EMPTY_WINDOW"),
    "unsupported": ("unsupported", "E_DEVICE_UNSUPPORTED"),
    "agent_failed": ("agent_failed", "E_AGENT_FAILED"),
}


class JobError(Exception):
    """A job that cannot run at all: (code, message, exit code)."""

    def __init__(self, code: str, message: str, exit_code: int):
        super().__init__(message)
        self.code, self.message, self.exit_code = code, message, exit_code


# ── Input ──────────────────────────────────────────────────────────────────────

def parse_time(value, field: str) -> Optional[datetime]:
    """An ISO 8601 time with its offset ("Z" accepted); None when absent."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise JobError("E_INPUT_INVALID", f"{field}: not an ISO 8601 time: {value!r}", EXIT_INPUT)
    if dt.tzinfo is None:
        raise JobError("E_INPUT_INVALID",
                       f"{field}: {value!r} has no time zone; add its offset (e.g. +08:00 or Z)",
                       EXIT_INPUT)
    return dt


def job_from_args(args) -> dict:
    """The job a command line describes, in the job-file shape."""
    if args.input:
        try:
            with open(args.input, "r", encoding="utf-8-sig") as fh:
                job = json.load(fh)
        except (OSError, ValueError) as exc:
            raise JobError("E_INPUT_INVALID", f"--input: {exc}", EXIT_INPUT)
        if not isinstance(job, dict):
            raise JobError("E_INPUT_INVALID", "--input: the job file must hold a JSON object", EXIT_INPUT)
        if args.dry_run:
            job.setdefault("options", {})["dry_run"] = True
        if args.collect:
            job.setdefault("options", {})["collect"] = True
        return job
    if not args.report and not args.collect:
        raise JobError("E_INPUT_INVALID",
                       "give --input <job.json>, or --report with --logs, or --collect", EXIT_INPUT)
    timing = any((args.issue_time, args.window_start, args.window_end, args.question))
    if timing and len(args.session_id or []) != 1:
        raise JobError("E_INPUT_INVALID",
                       "--issue-time, --window-start/--window-end and --question need exactly one "
                       "--session-id (use a job file for several)", EXIT_INPUT)
    sessions = [{"session_id": sid} for sid in (args.session_id or [])]
    if timing:
        sessions[0].update({"issue_time": args.issue_time, "window_start": args.window_start,
                            "window_end": args.window_end, "issue_description": args.question})
    return {"schema_version": SCHEMA_VERSION, "case_id": args.case_id or "", "report": args.report or "",
            "driver_logs": [args.logs] if args.logs else [], "sessions": sessions,
            "options": {"dry_run": bool(args.dry_run), "collect": bool(args.collect)}}


def validate(job: dict) -> dict:
    """The job checked and normalised; raises JobError (E_INPUT_INVALID)."""
    version = str(job.get("schema_version") or SCHEMA_VERSION)
    if version.split(".")[0] != SCHEMA_VERSION.split(".")[0]:
        raise JobError("E_INPUT_INVALID",
                       f"schema_version {version} is not supported (this tool reads {SCHEMA_VERSION})",
                       EXIT_INPUT)
    options = job.get("options") or {}
    collect = bool(options.get("collect"))
    report = str(job.get("report") or "").strip()
    # With collect, a missing report is made on the spot (powercfg /sleepstudy).
    if (report or not collect) and not os.path.isfile(report):
        raise JobError("E_INPUT_INVALID", f"report not found: {report!r}", EXIT_INPUT)
    logs = job.get("driver_logs") or []
    if isinstance(logs, str):
        logs = [logs]
    logs = [str(p).strip() for p in logs if str(p).strip()]
    if collect and logs:
        raise JobError("E_INPUT_INVALID",
                       "collect takes the driver log from this machine; drop driver_logs / --logs", EXIT_INPUT)
    if len(logs) > 1:
        raise JobError("E_INPUT_INVALID",
                       "driver_logs: this version takes one capture folder (or log file) per job",
                       EXIT_INPUT)
    for path in logs:
        if not os.path.exists(path):
            raise JobError("E_INPUT_INVALID", f"driver log path not found: {path!r}", EXIT_INPUT)
    sessions = []
    for i, s in enumerate(job.get("sessions") or []):
        if not isinstance(s, dict) or str(s.get("session_id") or "").strip() == "":
            raise JobError("E_INPUT_INVALID", f"sessions[{i}]: needs a session_id", EXIT_INPUT)
        sessions.append({
            "session_id": str(s["session_id"]).strip(),
            "issue_time": parse_time(s.get("issue_time"), f"sessions[{i}].issue_time"),
            "window_start": parse_time(s.get("window_start"), f"sessions[{i}].window_start"),
            "window_end": parse_time(s.get("window_end"), f"sessions[{i}].window_end"),
            "question": str(s.get("issue_description") or "").strip(),
        })
    return {"case_id": str(job.get("case_id") or ""), "report": report,
            "driver_log": logs[0] if logs else "", "sessions": sessions,
            "dry_run": bool(options.get("dry_run")), "collect": collect, "collection": None}


def collect(job: dict, out_dir: str, log=print) -> dict:
    """--collect: the report (made with powercfg when not given) and this
    machine's Intel Wi-Fi driver log, put into the job. JobError (exit 5)."""
    from services import driver_log_collector as dlc

    try:
        if not job["report"]:
            log("No report given: running powercfg /sleepstudy…")
            job["report"] = dlc.generate_sleepstudy(out_dir)
        log(f"Collecting the Wi-Fi driver log ({dlc.DRIVER_SESSION} autologger)…")
        info = dlc.collect_wifi_driver_log(out_dir)
    except dlc.CollectError as exc:
        raise JobError(exc.code, exc.message, EXIT_COLLECT)
    for warning in info["warnings"]:
        log(f"⚠ {warning}")
    log(f"Collected {len(info['files'])} file(s) into {info['capture_dir']}")
    job["driver_log"] = info["capture_dir"]
    job["collection"] = info
    return job


# ── Agents ─────────────────────────────────────────────────────────────────────

def load_agents():
    """(agent factory, point question template) as the NW page uses them:
    Avatar's LLM settings and the NW skills (nw_skills.yaml), set up without
    the web app's browser prewarm. Raises JobError(E_AGENT_CONFIG)."""
    from configs.global_configs import app_config
    from configs.set_up_app import set_up
    from services import sleepstudy_pipeline as sp
    from services.chatbot.engine.speclet_defaults import conversation_speclet

    set_up(None, prewarm=False)
    llm = app_config.llm_helper
    nw_agent = app_config.nw_analysis_agent
    if llm is None or getattr(llm, "client", None) is None or nw_agent is None:
        raise JobError("E_AGENT_CONFIG", "no LLM client is configured (Avatar's key / token)", EXIT_AGENT)
    nw_skills = dict(getattr(nw_agent, "skills", None) or {})

    def factory(device):
        return sp.default_agent_factory(device, wifi_skills=nw_skills or None)
    return factory, conversation_speclet(nw_agent, "sleepstudy_point")


# ── Output ─────────────────────────────────────────────────────────────────────

def _iso(local: str, offset_minutes: Optional[int]) -> str:
    """'MM/DD/YYYY HH:MM:SS' on the customer's clock -> ISO 8601 with its offset."""
    if not local:
        return ""
    try:
        dt = datetime.strptime(local, "%m/%d/%Y %H:%M:%S")
    except ValueError:
        return local
    if offset_minutes is None:
        return dt.isoformat()
    return dt.replace(tzinfo=timezone(timedelta(minutes=offset_minutes))).isoformat()


def session_entry(p: dict) -> dict:
    """One analysed (or not) session in the result.json contract."""
    from services import sleepstudy_pipeline as sp

    status, code = STATUS_CODES.get(p.get("status") or "", ("unknown", "E_ANALYSIS_FAILED"))
    if p.get("status") == "not_covered" and p.get("etl_decode_errors"):
        status, code = "decode_failed", "E_ETL_DECODE_FAILED"
    offset = p.get("utc_offset_minutes")
    agent_data = (p.get("agent_result") or {}).get("data")
    summary = (agent_data.get("root_cause_summary") if isinstance(agent_data, dict) else "") or ""
    finding = None
    if p.get("status") == "analyzed":
        text = summary or p.get("agent_report") or ""
        cause, detail = sp.dominant_cause(text)
        if cause == "Unknown":
            status, code = "no_cause_found", "W_NO_CAUSE_FOUND"
        finding = {
            "cause_category": cause,
            "summary": detail or sp._first_causal_sentence(text),
            "scenario": sp.verdict_scenario(text),
            "sleep_study_agreement": sp.sleepstudy_agreement(text) or "unclear",
            "contributing_factors": sp.contributing_factors(text),
            "root_cause_summary": summary,
            "report": p.get("agent_report") or "",
        }
    return {
        "session_id": str(p.get("session_id")),
        "device": p.get("device"),
        "status": status,
        "code": code,
        "message": p.get("message") or "",
        "customer_utc_offset": p.get("utc_offset_label") or "",
        "session": {"start": _iso(p.get("start"), offset), "end": _iso(p.get("end"), offset)},
        "window": {"start": _iso(p.get("window_start"), offset), "end": _iso(p.get("window_end"), offset),
                   "source": p.get("window_source") or "report",
                   "capped": bool(p.get("window_truncated"))},
        "sleep_study": {
            "sw_drips_pct": p.get("sw_drip_pct"), "hw_drips_pct": p.get("hw_drip_pct"),
            "verdict": p.get("html_verdict") or "",
            "short_device_wakes": p.get("short_device_wakes"),
            "red_top_blockers": [{"name": b.get("name"), "active_pct": b.get("active_time_pct")}
                                 for b in p.get("blockers") or []],
        },
        "driver_log": {
            "path": p.get("driver_log_path") or "",
            "coverage": p.get("coverage") or "none",
            "files": p.get("driver_log_files") or [],
            "time_zone_note": p.get("log_clock_note") or "",
            "etl_decode_errors": p.get("etl_decode_errors") or [],
            "excerpt": p.get("excerpt_path") or "",
            "excerpt_lines": p.get("excerpt_lines") or 0,
        },
        "finding": finding,
    }


def ineligible_reason(raw: Optional[dict], drip_threshold: float) -> tuple[str, str]:
    """(code, reason) for a requested session the analysis does not take."""
    from services import sleepstudy_analyzer as ssa

    if raw is None:
        return "E_SESSION_NOT_FOUND", "the report has no session with this id"
    if raw.get("Type") not in ssa.MS_SESSION_TYPES:
        return "E_SESSION_NOT_ELIGIBLE", "not a Modern Standby session"
    if raw.get("OnAc", True):
        return "E_SESSION_NOT_ELIGIBLE", "on AC (charging); only battery sessions are analysed"
    sw, hw = ssa.compute_drip_percentages(raw)
    if not any(v is not None and v < drip_threshold for v in (sw, hw)):
        return "E_SESSION_NOT_ELIGIBLE", f"DRIPS not below {drip_threshold:.0f}%"
    return "E_SESSION_NOT_ELIGIBLE", "no Wi-Fi or Bluetooth RED top blocker; a platform (system) session"


def write_outputs(out_dir: str, result: dict, markdown: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    tmp = os.path.join(out_dir, "result.json.partial")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, os.path.join(out_dir, "result.json"))
    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as fh:
        fh.write(markdown)


# ── Run ────────────────────────────────────────────────────────────────────────

def run(job: dict, out_dir: str, *, agent_factory=None, point_template=None, log=print) -> dict:
    """Analyse a validated job; returns the result.json contract."""
    from services import sleepstudy_analyzer as ssa
    from services import sleepstudy_pipeline as sp

    try:
        payload = ssa.load_sleepstudy(job["report"])
    except Exception as exc:          # noqa: BLE001 -- reported to the caller
        raise JobError("E_REPORT_UNREADABLE", f"{type(exc).__name__}: {exc}", EXIT_REPORT)
    raw_by_id = {str(s.get("SessionId")): s for s in payload.get("ScenarioInstances") or []}

    wanted = [s["session_id"] for s in job["sessions"]]
    overrides = {s["session_id"]: {k: s[k] for k in ("issue_time", "window_start", "window_end", "question")}
                 for s in job["sessions"] if any(s[k] for k in ("issue_time", "window_start", "window_end", "question"))}
    points, final, failure = [], None, None
    for event in sp.run_pipeline(job["report"], job["driver_log"], agent_factory=agent_factory,
                                 out_dir=os.path.join(out_dir, "excerpts"),
                                 point_template=point_template, session_ids=wanted or None,
                                 overrides=overrides):
        kind = event["type"]
        if kind == "step":
            log(event["content"])
        elif kind == "point":
            points.append(event["data"])
        elif kind == "done":
            final = event["data"]
        elif kind == "error":
            failure = event["content"]
    if failure is not None:
        if failure.startswith("ValueError:"):
            raise JobError("E_INPUT_INVALID", failure.split(":", 1)[1].strip(), EXIT_INPUT)
        raise JobError("E_ANALYSIS_FAILED", failure, EXIT_ERROR)

    sessions = [session_entry(p) for p in points]
    found = {str(p.get("session_id")) for p in points}
    for sid in wanted:
        if sid not in found:
            code, reason = ineligible_reason(raw_by_id.get(sid), sp.DRIP_THRESHOLD)
            sessions.append({"session_id": sid, "status": "not_eligible" if code != "E_SESSION_NOT_FOUND"
                             else "not_found", "code": code, "message": reason, "finding": None})
    system = sp.system_sessions(job["report"])
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "sleepstudy-analyze",
        "tool_version": TOOL_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "case_id": job["case_id"],
        "status": "ok",
        "code": "OK",
        "message": "",
        "dry_run": job["dry_run"],
        "report": job["report"],
        "driver_log": job["driver_log"],
        "collection": job.get("collection"),
        "bad_sessions": {"total": system["bad_total"], "wireless": system["wireless"],
                         "system": system["system"],
                         "system_blockers": [{"blocker": g["blocker"], "sessions": g["sessions"],
                                              "lowest_sw_drips_pct": g["worst_sw"]}
                                             for g in system["groups"]]},
        "sessions": sessions,
        "warnings": (final or {}).get("warnings", []),
        "excerpt_dir": os.path.join(out_dir, "excerpts"),
        "_markdown": (final or {}).get("markdown", ""),
    }


def error_result(code: str, message: str, case_id: str = "") -> dict:
    return {"schema_version": SCHEMA_VERSION, "tool": "sleepstudy-analyze",
            "tool_version": TOOL_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "case_id": case_id, "status": "error", "code": code, "message": message,
            "sessions": [], "warnings": []}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="sleepstudy-analyze",
        description="Sleep Study -> Wi-Fi / Bluetooth driver log analysis (JSON in, JSON out).")
    ap.add_argument("--input", help="job file (JSON); replaces --report/--logs/--session-id")
    ap.add_argument("--report", help="Sleep Study HTML report")
    ap.add_argument("--logs", help="capture folder or driver log (WiFiLog, WifiDriver*.etl, *.hci.txt)")
    ap.add_argument("--out", required=True, help="output folder (result.json, report.md, excerpts/)")
    ap.add_argument("--session-id", action="append", help="session to analyse (repeatable)")
    ap.add_argument("--issue-time", help="ISO 8601 with offset")
    ap.add_argument("--window-start", help="ISO 8601 with offset")
    ap.add_argument("--window-end", help="ISO 8601 with offset")
    ap.add_argument("--question", help="the caller's focus for the agent")
    ap.add_argument("--case-id", help="echoed in result.json")
    ap.add_argument("--dry-run", action="store_true", help="stop before the LLM")
    ap.add_argument("--collect", action="store_true",
                    help="take the Intel Wi-Fi driver log from this machine (administrator prompt); "
                         "replaces --logs, and makes --report optional")
    ap.add_argument("--version", action="version",
                    version=f"sleepstudy-analyze {TOOL_VERSION} (schema {SCHEMA_VERSION})")
    return ap


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    out_dir = os.path.abspath(args.out)
    case_id = args.case_id or ""
    try:
        job = validate(job_from_args(args))
        case_id = job["case_id"]
        progress = lambda m: print(f"[sleepstudy] {m}", flush=True)
        if job["collect"]:
            collect(job, out_dir, log=progress)
        factory = template = None
        if not job["dry_run"]:
            factory, template = load_agents()
        result = run(job, out_dir, agent_factory=factory, point_template=template,
                     log=lambda m: print(f"[sleepstudy] {m}", flush=True))
        markdown = (f"# Sleep Study driver analysis\n\n"
                    f"_sleepstudy-analyze {TOOL_VERSION} · schema {SCHEMA_VERSION}"
                    + (f" · case {case_id}" if case_id else "") + "_\n\n"
                    + result.pop("_markdown", ""))
        write_outputs(out_dir, result, markdown)
        counts = {}
        for s in result["sessions"]:
            counts[s["status"]] = counts.get(s["status"], 0) + 1
        print(json.dumps({"status": "ok", "result": os.path.join(out_dir, "result.json"),
                          "sessions": counts}, ensure_ascii=False), flush=True)
        return EXIT_OK
    except JobError as exc:
        result, code = error_result(exc.code, exc.message, case_id), exc.exit_code
    except Exception as exc:          # noqa: BLE001 -- reported to the caller
        traceback.print_exc()
        result, code = error_result("E_ANALYSIS_FAILED", f"{type(exc).__name__}: {exc}", case_id), EXIT_ERROR
    try:
        write_outputs(out_dir, result, f"# Sleep Study driver analysis\n\nFailed: {result['code']} {result['message']}\n")
    except OSError:
        pass
    print(json.dumps({"status": "error", "code": result["code"], "message": result["message"]},
                     ensure_ascii=False), file=sys.stderr, flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
