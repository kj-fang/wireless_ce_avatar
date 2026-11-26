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
then analyses it; run it soon after the standby. Without --report it also
runs powercfg /sleepstudy. Those steps need administrator rights: from a
prompt that has them they just run; otherwise Windows asks once (UAC) and
only they run elevated, while the decode and the analysis stay with the
person. --no-elevate fails with E_NOT_ADMIN instead (unattended runs).

The simplest use, on the machine that slept:
    IntelAvatar.exe sleepstudy-analyze --collect --open
(--out defaults to %USERPROFILE%\\SleepStudyDriverAnalysis\\<date_time>_<id>;
--open shows report.html when done.)

    (in a checkout: python -m services.sleepstudy_cli <same arguments>)

--report takes the HTML report or the XML one (powercfg /sleepstudy /xml, as
MCS Analyzer generates it); both are read into the same data. It also takes
a .zip (extracted beside it) or a folder holding exactly one report. Without
--logs, the driver log beside the report is used; when there is none, the
report is analysed alone and result.json says missing_driver_log. No report
in it: exit 3, E_NO_SLEEPSTUDY_REPORT; several: E_MULTIPLE_SLEEPSTUDY_REPORTS.

Options for the first form:
    --session-id ID     analyse this session (repeatable); default: the worst
                        bad wireless sessions, as the NW page lists them.
                        Without an issue time, a session is read from its
                        start: whole when its driver log stays under 200,000
                        lines and 12 h, else up to that point (result.json
                        window.covered_until / stop_reason say where)
    --issue-time T      ISO 8601 with an offset; the driver log is cut from
                        5 min before to 10 min after it, inside the session.
                        Repeatable: times whose cuts overlap or lie under
                        5 min apart are analysed as one window (60 min at
                        most; the next window starts where it ended)
    --window-start T / --window-end T
                        the exact window to cut instead (ISO 8601, offset)
    --question TEXT     the caller's focus, added to the agent's question
    --device wifi|bt    whose driver log to read (a session or window the
                        report does not flag as a bad wireless session)
    --device-id ID      the caller's name for the device, e.g. its ACPI path
    --evidence TEXT     a finding of the caller's own (e.g. an ETW trace's
                        "Wi-Fi out of D3 169 s"), for the agent to explain
                        from the driver log (repeatable)
    --force             analyse the session even if it is not a bad wireless one
    --window-start/--window-end (or --issue-time) without --session-id
                        analyse that window: the report session it falls in,
                        or the window alone (e.g. another tool's live capture)
    --case-id ID        echoed in the result
    --dry-run           everything up to the LLM: selection, log matching,
                        time zones, ETL decoding, cuts; no agent call

A job file carries the same, for any number of sessions:
    {
      "schema_version": "1.1",
      "case_id": "CASE-12345",
      "report": "D:\\case\\sleepstudy-report.html",
      "driver_logs": ["D:\\case\\WRT"],
      "sessions": [
        {"session_id": "704",
         "issue_time": "2026-10-25T12:29:07-07:00",
         "window_start": "...", "window_end": "...",
         "issue_description": "Why did Wi-Fi stay in D0?"},
        {"window_start": "2026-11-10T08:40:25Z", "window_end": "2026-11-10T08:49:10Z",
         "device": "wifi", "device_id": "\\_SB.PC00.CNVW",
         "evidence": ["ETW: Wi-Fi out of D3 169 s (32% of standby), 12 entries"]}
      ],
      "options": {"dry_run": false, "collect": false}
    }
    ("collect": true replaces "driver_logs" and makes "report" optional.
     A session is a report session by id -- with "force": true (or a
     "device") analysed even when it is not a bad wireless session -- or a
     window without an id. Schema 1.0 jobs read the same.)

Output, in --out:
    result.json   the contract below (SCHEMA_VERSION), for programs
    report.md     the same results for people (report.html: the same, for a browser)
    excerpts/     the driver-log cut each analysed session was given

Exit code says whether the job ran, not what each session found:
    0  ran; each session's outcome is in result.json (status / code)
    1  unexpected failure          2  invalid input (E_INPUT_INVALID)
    3  report unreadable           4  no LLM configured (E_AGENT_CONFIG)
    5  --collect failed: E_NOT_ADMIN (prompt declined, or --no-elevate),
       E_DRIVER_TRACE_NOT_RUNNING, E_DRIVER_LOG_COPY_FAILED,
       E_SLEEPSTUDY_FAILED, E_ELEVATION_FAILED, E_ELEVATION_TIMEOUT
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

SCHEMA_VERSION = "1.1"
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


def read_evidence_file(path: str) -> list[str]:
    """--evidence-file: a JSON list of strings, or plain text with one finding
    per line (e.g. the event-log suspects another tool already filtered)."""
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            text = fh.read()
    except OSError as exc:
        raise JobError("E_INPUT_INVALID", f"--evidence-file: {exc}", EXIT_INPUT)
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, list):
        return [str(x).strip() for x in data if str(x).strip()]
    if isinstance(data, dict) and isinstance(data.get("evidence"), list):
        return [str(x).strip() for x in data["evidence"] if str(x).strip()]
    return [line.strip() for line in text.splitlines() if line.strip()]


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
        if args.no_elevate:
            job.setdefault("options", {})["no_elevate"] = True
        return job
    if not (args.report or args.collect or args.logs):
        raise JobError("E_INPUT_INVALID",
                       "give --input <job.json>, or --report and/or --logs, or --collect", EXIT_INPUT)
    evidence = list(args.evidence or []) + (read_evidence_file(args.evidence_file) if args.evidence_file else [])
    issue_times = list(args.issue_time or [])
    timing = any((issue_times, args.window_start, args.window_end, args.question,
                  args.device, args.device_id, evidence, args.force))
    ids = args.session_id or []
    if len(issue_times) > 1 and (len(ids) > 1 or args.window_start or args.window_end):
        raise JobError("E_INPUT_INVALID",
                       "several --issue-time take at most one --session-id, and no "
                       "--window-start/--window-end (use a job file for other combinations)", EXIT_INPUT)
    # Without --session-id, a window (or an issue time) is analysed on its own.
    window_only = not ids and bool(issue_times or (args.window_start and args.window_end))
    if timing and len(ids) != 1 and not window_only:
        raise JobError("E_INPUT_INVALID",
                       "--issue-time, --window-start/--window-end, --question, --device, --device-id, "
                       "--evidence and --force need exactly one --session-id, or a window without one "
                       "(use a job file for several)", EXIT_INPUT)
    sessions = [{"session_id": sid} for sid in ids] or ([{}] if window_only else [])
    if timing:
        # One request per issue time, each with what the other options say.
        sessions = [{**sessions[0], "issue_time": at, "window_start": args.window_start,
                     "window_end": args.window_end, "issue_description": args.question,
                     "device": args.device, "device_id": args.device_id,
                     "evidence": evidence, "force": args.force}
                    for at in issue_times or [None]]
    return {"schema_version": SCHEMA_VERSION, "case_id": args.case_id or "", "report": args.report or "",
            "driver_logs": [args.logs] if args.logs else [], "sessions": sessions,
            "options": {"dry_run": bool(args.dry_run), "collect": bool(args.collect),
                        "no_elevate": bool(args.no_elevate)}}


DEVICE_NAMES = {"wifi": "wifi", "wi-fi": "wifi", "wlan": "wifi", "bt": "bt", "bluetooth": "bt"}
MAX_EVIDENCE_LINES, MAX_EVIDENCE_CHARS = 30, 500


def _session_request(s, i: int, warnings: list) -> dict:
    """One entry of the job's sessions, checked: a report session (session_id),
    or a window of the caller's (window_start + window_end, or issue_time).
    What is dropped from its evidence is added to `warnings`."""
    field = f"sessions[{i}]"
    if not isinstance(s, dict):
        raise JobError("E_INPUT_INVALID", f"{field}: must be an object", EXIT_INPUT)
    sid = str(s.get("session_id") or "").strip()
    issue = parse_time(s.get("issue_time"), f"{field}.issue_time")
    lo = parse_time(s.get("window_start"), f"{field}.window_start")
    hi = parse_time(s.get("window_end"), f"{field}.window_end")
    if (lo is None) != (hi is None):
        raise JobError("E_INPUT_INVALID", f"{field}: give both window_start and window_end", EXIT_INPUT)
    if lo is not None and hi <= lo:
        raise JobError("E_INPUT_INVALID", f"{field}: window_end is not after window_start", EXIT_INPUT)
    issue_only = issue is not None and lo is None      # its window comes from the issue time
    if not sid:
        if lo is None and issue is None:
            raise JobError("E_INPUT_INVALID",
                           f"{field}: needs a session_id, or a window (window_start/window_end or issue_time)",
                           EXIT_INPUT)
        if lo is None:          # a window of the caller's from its issue time alone
            from services.sleepstudy_pipeline import ISSUE_TIME_AFTER, ISSUE_TIME_BEFORE
            lo, hi = issue - ISSUE_TIME_BEFORE, issue + ISSUE_TIME_AFTER
    device = str(s.get("device") or "").strip().lower()
    if device and device not in DEVICE_NAMES:
        raise JobError("E_INPUT_INVALID", f"{field}.device: {s.get('device')!r} is not wifi or bt", EXIT_INPUT)
    evidence = s.get("evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    if not isinstance(evidence, list):
        raise JobError("E_INPUT_INVALID", f"{field}.evidence: a string or a list of strings", EXIT_INPUT)
    evidence = [str(e).strip() for e in evidence if str(e).strip()]
    if len(evidence) > MAX_EVIDENCE_LINES:
        warnings.append(f"{field}.evidence: {len(evidence)} lines given, only the first "
                        f"{MAX_EVIDENCE_LINES} are used")
        evidence = evidence[:MAX_EVIDENCE_LINES]
    cut = sum(len(e) > MAX_EVIDENCE_CHARS for e in evidence)
    if cut:
        warnings.append(f"{field}.evidence: {cut} line(s) longer than {MAX_EVIDENCE_CHARS} "
                        "characters were cut to that length")
    evidence = [e[:MAX_EVIDENCE_CHARS] for e in evidence]
    return {"session_id": sid, "issue_time": issue, "window_start": lo, "window_end": hi,
            "question": str(s.get("issue_description") or "").strip(),
            "device": DEVICE_NAMES.get(device, ""), "device_id": str(s.get("device_id") or "").strip(),
            "evidence": evidence, "force": bool(s.get("force")),
            "issue_times": [], "issue_only": issue_only}


def merge_issue_times(sessions: list, warnings: list) -> list:
    """
    The checked requests, with those that give only an issue time merged per
    session and device: issue times whose cuts (ISSUE_TIME_BEFORE / AFTER)
    overlap, or lie under ISSUE_TIME_MERGE_GAP apart, become one window, so
    a burst of events is not analysed once per event. A window stops growing
    at WINDOW_CAP and the next one starts where it ended. A merged request
    keeps every time ("issue_times"), question and evidence line.
    """
    from services.sleepstudy_pipeline import (ISSUE_TIME_AFTER, ISSUE_TIME_BEFORE,
                                              ISSUE_TIME_MERGE_GAP, WINDOW_CAP)

    kept, groups = [], {}
    for i, s in enumerate(sessions):
        if s.pop("issue_only"):
            # No device named: Wi-Fi for a window, the session's own for a report session.
            device = s["device"] or ("" if s["session_id"] else "wifi")
            groups.setdefault((s["session_id"], device), []).append((i, s))
        else:
            kept.append((i, s))
    for members in groups.values():
        segments = []
        for i, s in sorted(members, key=lambda m: m[1]["issue_time"]):
            at = s["issue_time"]
            lo, hi = at - ISSUE_TIME_BEFORE, at + ISSUE_TIME_AFTER
            seg = segments[-1] if segments else None
            # At the cap, a time already inside the window still belongs to it.
            if seg and lo - seg["hi"] < ISSUE_TIME_MERGE_GAP and (hi - seg["lo"] <= WINDOW_CAP or at <= seg["hi"]):
                seg["hi"] = min(max(seg["hi"], hi), seg["lo"] + WINDOW_CAP)
                seg["members"].append((i, s))
            else:
                segments.append({"lo": max(lo, seg["hi"]) if seg else lo, "hi": hi, "members": [(i, s)]})
        for seg in segments:
            parts = [s for _, s in seg["members"]]
            evidence = list(dict.fromkeys(e for s in parts for e in s["evidence"]))
            if len(evidence) > MAX_EVIDENCE_LINES:
                warnings.append(f"issue times {parts[0]['issue_time'].isoformat()} to "
                                f"{parts[-1]['issue_time'].isoformat()}, analysed as one window: "
                                f"{len(evidence)} evidence lines, only the first {MAX_EVIDENCE_LINES} are used")
                evidence = evidence[:MAX_EVIDENCE_LINES]
            kept.append((min(i for i, _ in seg["members"]), {
                **parts[0], "issue_time": None, "issue_times": [s["issue_time"] for s in parts],
                "window_start": seg["lo"], "window_end": seg["hi"],
                "question": " / ".join(dict.fromkeys(s["question"] for s in parts if s["question"])),
                "evidence": evidence,
                "device_id": next((s["device_id"] for s in parts if s["device_id"]), ""),
                "force": any(s["force"] for s in parts)}))
    return [s for _, s in sorted(kept, key=lambda item: item[0])]


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
    warnings: list = []
    sessions = merge_issue_times(
        [_session_request(s, i, warnings) for i, s in enumerate(job.get("sessions") or [])], warnings)
    # A report is needed to find sessions; times alone (an issue time or a
    # window per request) are cut on their own clock. With collect, a missing
    # report is made on the spot (powercfg /sleepstudy).
    times_only = bool(sessions) and not any(s["session_id"] for s in sessions)
    if not report and not collect and not times_only:
        raise JobError("E_INPUT_INVALID",
                       "give a Sleep Study report (--report: .html, .xml, a .zip or folder holding one) "
                       "to pick or name sessions; without one, give the issue time or window "
                       "(--issue-time / --window-start --window-end)", EXIT_INPUT)
    logs = job.get("driver_logs") or []
    if isinstance(logs, str):
        logs = [logs]
    logs = [str(p).strip() for p in logs if str(p).strip()]
    # The report as the NW page takes it: a zip is extracted, a folder looked
    # into; with no driver log given, the one beside the report is used.
    missing_driver_log = False
    if report:
        from services import sleepstudy_input
        try:
            found = sleepstudy_input.resolve(report)
        except sleepstudy_input.InputError as exc:
            message = exc.message + ("".join(f"\n  {c}" for c in exc.candidates) if exc.candidates else "")
            raise JobError(exc.code, message, EXIT_REPORT)
        report = found["report"]
        if not logs and not collect:
            if found["missing_driver_log"]:
                missing_driver_log = True
                warnings.append(f"No driver log beside the report (in {found['search_root']}): the report "
                                "is analysed alone. Give one with --logs, or --collect it.")
            else:
                logs = [found["search_root"]]
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
    return {"case_id": str(job.get("case_id") or ""), "report": report,
            "driver_log": logs[0] if logs else "", "sessions": sessions,
            "dry_run": bool(options.get("dry_run")), "collect": collect,
            "no_elevate": bool(options.get("no_elevate")), "collection": None, "warnings": warnings,
            "missing_driver_log": missing_driver_log}


COLLECTION_FILE = "collection.json"


def _collect_here(out_dir: str, make_report: bool) -> dict:
    """The privileged part of --collect, in this process: powercfg
    /sleepstudy when asked, then flush and copy the driver autologger.
    Raises driver_log_collector.CollectError."""
    from services import driver_log_collector as dlc

    report = dlc.generate_sleepstudy(out_dir) if make_report else ""
    info = dlc.collect_wifi_driver_log(out_dir)
    info["report"] = report
    return info


def _self_command(extra: list[str]) -> tuple[str, list[str], Optional[str]]:
    """(executable, arguments, working folder) that run this CLI again: the
    packaged IntelAvatar.exe, or python -m from the checkout."""
    if getattr(sys, "frozen", False):
        return sys.executable, ["sleepstudy-analyze", *extra], None
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return sys.executable, ["-m", "services.sleepstudy_cli", *extra], repo


def _collect_elevated(out_dir: str, make_report: bool, run_elevated=None) -> dict:
    """Run only the privileged part (--collect-only) with administrator
    rights, through Windows' UAC prompt, and read what it collected. The
    decode and the analysis stay in this process, as the person: an
    elevated process often cannot reach their network share (the PDBs)."""
    from services import driver_log_collector as dlc

    os.makedirs(out_dir, exist_ok=True)
    marker = os.path.join(out_dir, COLLECTION_FILE)
    if os.path.exists(marker):
        os.remove(marker)
    exe, params, cwd = _self_command(["--collect-only", "--out", out_dir]
                                     + (["--make-report"] if make_report else []))
    (run_elevated or dlc.run_elevated)(exe, params, cwd)
    try:
        with open(marker, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        raise dlc.CollectError("E_ELEVATION_FAILED",
                               "the elevated collection ended without writing its result")
    if data.get("error"):
        raise dlc.CollectError(data["error"].get("code", "E_ELEVATION_FAILED"),
                               data["error"].get("message", ""))
    return data


def collect_only(out_dir: str, make_report: bool) -> int:
    """--collect-only (run elevated by _collect_elevated): the privileged
    part alone, its outcome written to collection.json; exit 0 or 5."""
    from services import driver_log_collector as dlc

    try:
        data, code = _collect_here(out_dir, make_report), EXIT_OK
    except dlc.CollectError as exc:
        data, code = {"error": {"code": exc.code, "message": exc.message}}, EXIT_COLLECT
    except Exception as exc:          # noqa: BLE001 -- reported to the caller
        data, code = {"error": {"code": "E_ELEVATION_FAILED", "message": f"{type(exc).__name__}: {exc}"}}, EXIT_ERROR
    os.makedirs(out_dir, exist_ok=True)
    tmp = os.path.join(out_dir, COLLECTION_FILE + ".partial")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, os.path.join(out_dir, COLLECTION_FILE))
    return code


def collect(job: dict, out_dir: str, log=print, run_elevated=None,
            make_report: Optional[bool] = None) -> dict:
    """--collect: the report (made with powercfg when not given, unless
    `make_report` says otherwise) and this machine's Intel Wi-Fi driver log,
    put into the job. From a prompt that is not an administrator's, only that
    part runs elevated, after Windows asks (UAC); --no-elevate refuses
    instead. JobError (exit 5)."""
    from services import driver_log_collector as dlc

    make_report = (not job["report"]) if make_report is None else make_report
    try:
        if dlc.is_admin():
            if make_report:
                log("No report given: running powercfg /sleepstudy…")
            log(f"Collecting the Wi-Fi driver log ({dlc.DRIVER_SESSION} autologger)…")
            info = _collect_here(out_dir, make_report)
        elif job.get("no_elevate"):
            raise dlc.CollectError("E_NOT_ADMIN",
                                   "collecting the Wi-Fi driver log needs administrator rights: run from an "
                                   "administrator prompt, or without --no-elevate to be asked")
        else:
            log("Collecting the driver log needs administrator rights: approve the Windows prompt (UAC)…")
            info = _collect_elevated(out_dir, make_report, run_elevated)
    except dlc.CollectError as exc:
        raise JobError(exc.code, exc.message, EXIT_COLLECT)
    if make_report:
        job["report"] = info["report"]
    for warning in info.get("warnings") or []:
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
        "device_id": p.get("device_id") or "",
        "selection": p.get("selection") or "report",
        "caller_evidence": p.get("caller_evidence") or [],
        "status": status,
        "code": code,
        "message": p.get("message") or "",
        "customer_utc_offset": p.get("utc_offset_label") or "",
        "session": {"start": _iso(p.get("start"), offset), "end": _iso(p.get("end"), offset)},
        "window": {"start": _iso(p.get("window_start"), offset), "end": _iso(p.get("window_end"), offset),
                   "source": p.get("window_source") or "report",
                   "capped": bool(p.get("stop_reason")),
                   # How far the analysis read, and why it stopped short of
                   # the window's end ("line_budget", "time_cap"; "" = all).
                   "covered_until": _iso(p.get("covered_until") or p.get("window_end"), offset),
                   "stop_reason": p.get("stop_reason") or ""},
        "issue_times": [_iso(t, offset) for t in p.get("issue_times") or []],
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


def _report_html(markdown: str) -> str:
    """report.md as a page a browser opens (markdown rendered when the
    package is there, else the text as is)."""
    import html as _html
    try:
        import markdown as _md
        body = _md.markdown(markdown, extensions=["tables", "fenced_code"])
    except Exception:                 # noqa: BLE001 -- the plain text still reads
        body = f"<pre>{_html.escape(markdown)}</pre>"
    return ("<!doctype html><meta charset='utf-8'><title>Sleep Study driver analysis</title>"
            "<style>body{font-family:Segoe UI,Arial,sans-serif;max-width:1000px;margin:24px auto;"
            "padding:0 16px;line-height:1.5;color:#1f2937}table{border-collapse:collapse}"
            "td,th{border:1px solid #d1d5db;padding:4px 8px}pre,code{background:#f3f4f6}"
            "pre{padding:8px;overflow-x:auto;white-space:pre-wrap}</style>" + body)


def write_outputs(out_dir: str, result: dict, markdown: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    tmp = os.path.join(out_dir, "result.json.partial")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, os.path.join(out_dir, "result.json"))
    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as fh:
        fh.write(markdown)
    with open(os.path.join(out_dir, "report.html"), "w", encoding="utf-8") as fh:
        fh.write(_report_html(markdown))


def default_out_dir() -> str:
    """Where results go without --out: a new dated folder under the user's
    profile, with a random suffix so runs started in the same second stay apart."""
    return os.path.join(os.path.expanduser("~"), "SleepStudyDriverAnalysis",
                        f"{datetime.now():%Y%m%d_%H%M%S}_{os.urandom(3).hex()}")


# ── Run ────────────────────────────────────────────────────────────────────────

def run(job: dict, out_dir: str, *, agent_factory=None, point_template=None, log=print) -> dict:
    """Analyse a validated job; returns the result.json contract."""
    from services import sleepstudy_analyzer as ssa
    from services import sleepstudy_pipeline as sp

    job_warnings = list(job.get("warnings") or [])
    for warning in job_warnings:
        log(f"⚠ {warning}")
    payload = {}
    if job["report"]:
        try:
            payload = ssa.load_sleepstudy(job["report"])
        except Exception as exc:          # noqa: BLE001 -- reported to the caller
            raise JobError("E_REPORT_UNREADABLE", f"{type(exc).__name__}: {exc}", EXIT_REPORT)
    raw_by_id = {str(s.get("SessionId")): s for s in payload.get("ScenarioInstances") or []}

    # Report sessions by id; with `force` (or a device named) one that is not a
    # bad wireless session is analysed anyway. Windows without an id are
    # always analysed, attached to the report session they fall in if any.
    keys = ("issue_time", "issue_times", "window_start", "window_end", "question", "evidence", "device_id")
    wanted = [s["session_id"] for s in job["sessions"] if s["session_id"]]
    # By session and device ("": every device of the session), so a request
    # for a session's Wi-Fi does not reach its Bluetooth point. A further
    # request for a target already asked for is a point of its own ("another").
    overrides, caller_points = {}, []
    for s in job["sessions"]:
        request = {**{k: s[k] for k in keys}, "session_id": s["session_id"], "device": s["device"],
                   "force": s["force"]}
        target = (s["session_id"], s["device"])
        if s["session_id"] and any(s[k] for k in keys):
            if target in overrides:
                request["another"] = True
            else:
                overrides[target] = {k: s[k] for k in keys}
        if not s["session_id"] or s["force"] or s["device"] or request.get("another"):
            caller_points.append(request)
    points, final, failure = [], None, None
    for event in sp.run_pipeline(job["report"], job["driver_log"], agent_factory=agent_factory,
                                 out_dir=os.path.join(out_dir, "excerpts"),
                                 point_template=point_template,
                                 session_ids=([(s["session_id"], s["device"]) for s in job["sessions"]
                                               if s["session_id"]] if job["sessions"] else None),
                                 overrides=overrides, caller_points=caller_points):
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
    for sid in dict.fromkeys(wanted):
        if sid not in found:
            code, reason = ineligible_reason(raw_by_id.get(sid), sp.DRIP_THRESHOLD)
            sessions.append({"session_id": sid, "status": "not_eligible" if code != "E_SESSION_NOT_FOUND"
                             else "not_found", "code": code, "message": reason, "finding": None})
    system = (sp.system_sessions(job["report"]) if job["report"] else
              {"bad_total": 0, "wireless": 0, "system": 0, "groups": []})
    reach, reach_md = log_reach(job, payload)
    for line in reach_md.splitlines():
        if line.startswith("- "):
            log(line[2:])
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
        "missing_driver_log": bool(job.get("missing_driver_log")),
        "collection": job.get("collection"),
        "bad_sessions": {"total": system["bad_total"], "wireless": system["wireless"],
                         "system": system["system"],
                         "system_blockers": [{"blocker": g["blocker"], "sessions": g["sessions"],
                                              "lowest_sw_drips_pct": g["worst_sw"]}
                                             for g in system["groups"]]},
        "driver_log_reach": reach,
        "sessions": sessions,
        "warnings": job_warnings + (final or {}).get("warnings", []),
        "excerpt_dir": os.path.join(out_dir, "excerpts"),
        "_markdown": ("".join(f"> ⚠️ {w}\n" for w in job_warnings) + ("\n" if job_warnings else "")
                      + reach_md + (final or {}).get("markdown", "")),
    }


def log_reach(job: dict, payload: dict) -> tuple[dict, str]:
    """
    How far back the driver log reaches, per device, with the report's bad
    wireless sessions split into those it can hold, those older than it and,
    while an ETL is undecoded, those before the earliest time known (they
    may be in that ETL): result.json's "driver_log_reach" and the report's
    section about it. The Intel Wi-Fi autologger is circular, so this says
    which past sessions can still be analysed at all.
    """
    from services import sleepstudy_pipeline as sp

    if not job["driver_log"]:
        return {}, ""
    if job["report"]:
        offset = sp.session_utc_offset(None, (payload.get("ReportInformation") or {}).get("UtcOffset"))
        bad = sp.time_points(job["report"], max_points=100000, per_device=100000)
    else:
        # No report: the clock of the caller's own times.
        first = next((s["window_start"] for s in job["sessions"] if s["window_start"] is not None), None)
        offset = int(first.utcoffset().total_seconds() // 60) if first is not None else None
        bad = []
    found = sp.driver_log_reach(job["driver_log"], offset)
    reach, rows = {}, []
    for device, r in found.items():
        older, held, newer, unknown = [], [], [], []
        for p in bad:
            if p["device"] != device:
                continue
            try:
                start = datetime.strptime(p["start"], "%m/%d/%Y %H:%M:%S")
                end = datetime.strptime(p["end"] or p["start"], "%m/%d/%Y %H:%M:%S")
            except ValueError:
                continue
            if end < r["from"]:
                # An undecoded ETL is known by when it was last written, not
                # by its first event: what it holds from before is not known.
                (older if r["exact"] else unknown).append(str(p["session_id"]))
            else:
                (newer if start > r["to"] else held).append(str(p["session_id"]))
        reach[device] = {"from": _iso(sp._fmt(r["from"]), offset), "to": _iso(sp._fmt(r["to"]), offset),
                         "exact": r["exact"], "files": r["files"], "undecoded_etl": r["undecoded_etl"],
                         "bad_sessions_covered": held, "bad_sessions_older": older,
                         "bad_sessions_unknown": unknown, "bad_sessions_newer": newer}
        label = sp.DEVICE_LABELS.get(device, device)
        since = sp._fmt(r["from"]) if r["exact"] else f"at least {sp._fmt(r['from'])}"
        row = (f"- {label} driver log reaches back to {since} (until {sp._fmt(r['to'])}, "
               f"{r['files']} file(s)).")
        if job["report"]:
            row += (f" Bad {label} sessions within its span: {', '.join(held) or 'none'}; "
                    f"older than the log: {', '.join(older) or 'none'}"
                    + (f"; before it, not known until the ETL is decoded: {', '.join(unknown)}" if unknown else "")
                    + (f"; after it ended: {', '.join(newer)}" if newer else ""))
        rows.append(row)
    zone = f" ({sp._fmt_offset(offset)})" if offset is not None else ""
    if not rows:
        md = (f"## Driver log coverage\n\nNo Wi-Fi or Bluetooth driver log was found under "
              f"`{job['driver_log']}`.\n\n")
        return reach, md
    note = ("The Intel Wi-Fi driver autologger is circular: sessions older than this cannot be "
            "analysed from it, so collect soon after the issue. A session within the span can still "
            "fall in a gap between files; its own status below says whether it is logged.")
    if any(not r["exact"] for r in found.values()):
        note += (" \"At least\": some Wi-Fi driver ETLs are not decoded yet, so their start is "
                 "known only after decoding; the log may reach further back.")
    return reach, f"## Driver log coverage{zone}\n\n" + "\n".join(rows) + f"\n\n_{note}_\n\n"


def error_result(code: str, message: str, case_id: str = "") -> dict:
    return {"schema_version": SCHEMA_VERSION, "tool": "sleepstudy-analyze",
            "tool_version": TOOL_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "case_id": case_id, "status": "error", "code": code, "message": message,
            "sessions": [], "warnings": []}


class _Parser(argparse.ArgumentParser):
    """Arguments that cannot be parsed raise JobError instead of exiting, so
    the caller still gets its JSON line; usage goes to stderr as usual."""

    def error(self, message):
        self.print_usage(sys.stderr)
        raise JobError("E_INPUT_INVALID", message, EXIT_INPUT)


def _out_arg(argv) -> str:
    """The output folder of a command line that could not be parsed: its
    --out when one is readable, else the default."""
    argv = list(sys.argv[1:] if argv is None else argv)
    for i, arg in enumerate(argv):
        if arg == "--out" and i + 1 < len(argv):
            return os.path.abspath(argv[i + 1])
        if arg.startswith("--out="):
            return os.path.abspath(arg[len("--out="):])
    return os.path.abspath(default_out_dir())


def build_parser() -> argparse.ArgumentParser:
    ap = _Parser(
        prog="sleepstudy-analyze",
        description="Sleep Study -> Wi-Fi / Bluetooth driver log analysis (JSON in, JSON out).")
    ap.add_argument("--input", help="job file (JSON); replaces --report/--logs/--session-id")
    ap.add_argument("--report", help="Sleep Study report: .html, .xml (powercfg /sleepstudy /xml, MCS Analyzer), or a .zip / folder holding one")
    ap.add_argument("--logs", help="capture folder or driver log (WiFiLog, WifiDriver*.etl, *.hci.txt)")
    ap.add_argument("--out", help="output folder (result.json, report.md/.html, excerpts/); "
                                  "default %%USERPROFILE%%\\SleepStudyDriverAnalysis\\<date_time>_<id>")
    ap.add_argument("--session-id", action="append", help="session to analyse (repeatable)")
    ap.add_argument("--issue-time", action="append",
                    help="ISO 8601 with offset (repeatable; times close together are analysed as one window)")
    ap.add_argument("--window-start", help="ISO 8601 with offset")
    ap.add_argument("--window-end", help="ISO 8601 with offset")
    ap.add_argument("--question", help="the caller's focus for the agent")
    ap.add_argument("--device", help="wifi or bt: whose driver log to read for that session or window")
    ap.add_argument("--device-id", help="the caller's name for the device, e.g. its ACPI path")
    ap.add_argument("--evidence", action="append",
                    help="a finding of the caller's own analysis for the agent to explain (repeatable)")
    ap.add_argument("--evidence-file",
                    help="findings in a file: one per line, or a JSON list (e.g. filtered event-log suspects)")
    ap.add_argument("--force", action="store_true",
                    help="analyse the session even if it is not a bad wireless session")
    ap.add_argument("--case-id", help="echoed in result.json")
    ap.add_argument("--dry-run", action="store_true", help="stop before the LLM")
    ap.add_argument("--collect", action="store_true",
                    help="take the Intel Wi-Fi driver log from this machine; replaces --logs and makes "
                         "--report optional. Not an administrator? Windows asks (UAC) for that step only")
    ap.add_argument("--no-elevate", action="store_true",
                    help="with --collect: fail with E_NOT_ADMIN instead of showing the Windows prompt "
                         "(unattended runs)")
    ap.add_argument("--open", action="store_true", help="open report.html when done")
    # Internal: the elevated half of --collect (see _collect_elevated).
    ap.add_argument("--collect-only", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--make-report", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--version", action="version",
                    version=f"sleepstudy-analyze {TOOL_VERSION} (schema {SCHEMA_VERSION})")
    return ap


def _utf8_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _progress(message: str) -> None:
    """Progress goes to stderr: stdout carries only the final JSON line."""
    print(f"[sleepstudy] {message}", file=sys.stderr, flush=True)


def collect_main(argv=None) -> int:
    """
    IntelAvatar.exe sleepstudy-collect [--out DIR] [--with-report] [--no-elevate] [--open]

    The first step alone: flush and copy this machine's Intel Wi-Fi driver
    autologger into <out>/capture/<host>_<time>_collected (and, with
    --with-report, run powercfg /sleepstudy), for sleepstudy-analyze --logs
    later or elsewhere. Writes <out>/collection.json; stdout's last line is
    JSON: status, capture_dir, files, report, driver_log_reach. Exit 0, or 5
    (E_NOT_ADMIN, E_DRIVER_TRACE_NOT_RUNNING, ...), or 2 for bad arguments.
    """
    return _result_stdout(lambda out: _collect_main(argv, out))


def _collect_main(argv, out) -> int:
    from services import sleepstudy_pipeline as sp

    ap = _Parser(
        prog="sleepstudy-collect",
        description="Collect the Intel Wi-Fi driver log from this machine (step 1 of 2; "
                    "analyse it with sleepstudy-analyze --logs).")
    ap.add_argument("--out", help="output folder; default %%USERPROFILE%%\\SleepStudyDriverAnalysis\\<date_time>_<id>")
    ap.add_argument("--with-report", action="store_true",
                    help="also make a Sleep Study report here (powercfg /sleepstudy, HTML)")
    ap.add_argument("--no-elevate", action="store_true",
                    help="fail with E_NOT_ADMIN instead of showing the Windows (UAC) prompt")
    ap.add_argument("--open", action="store_true", help="open the output folder when done")
    out_dir = ""
    job = {"report": "", "driver_log": "", "collection": None}
    try:
        args = ap.parse_args(argv)
        out_dir = os.path.abspath(args.out or default_out_dir())
        job["no_elevate"] = args.no_elevate
        collect(job, out_dir, log=_progress, make_report=args.with_report)
    except JobError as exc:
        out_dir = out_dir or _out_arg(argv)
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, COLLECTION_FILE), "w", encoding="utf-8") as fh:
            json.dump({"error": {"code": exc.code, "message": exc.message}}, fh, ensure_ascii=False, indent=2)
        print(json.dumps({"status": "error", "code": exc.code, "message": exc.message},
                         ensure_ascii=False), file=out, flush=True)
        return exc.exit_code
    info = job["collection"]
    offset = sp.local_utc_offset()
    reach = {d: {"from": _iso(sp._fmt(r["from"]), offset), "to": _iso(sp._fmt(r["to"]), offset),
                 "exact": r["exact"]}
             for d, r in sp.driver_log_reach(info["capture_dir"], offset).items()}
    info = {**info, "report": job["report"] or "", "driver_log_reach": reach}
    with open(os.path.join(out_dir, COLLECTION_FILE), "w", encoding="utf-8") as fh:
        json.dump(info, fh, ensure_ascii=False, indent=2)
    for device, r in reach.items():
        _progress(f"{'Wi-Fi' if device == 'wifi' else 'Bluetooth'} driver log reaches back to "
                  f"{'' if r['exact'] else 'at least '}{r['from']}")
    _progress("Next: IntelAvatar.exe sleepstudy-analyze --logs \"" + info["capture_dir"] + "\""
              + (f" --report \"{job['report']}\"" if job["report"] else " [--report <sleepstudy.html|.xml>]")
              + " --issue-time <ISO 8601 with offset> --question \"...\"")
    print(json.dumps({"status": "ok", "capture_dir": info["capture_dir"],
                      "files": [f["file"] for f in info["files"]], "report": info["report"],
                      "collection": os.path.join(out_dir, COLLECTION_FILE),
                      "driver_log_reach": reach, "warnings": info.get("warnings") or []},
                     ensure_ascii=False), file=out, flush=True)
    if args.open:
        try:
            os.startfile(out_dir)
        except (AttributeError, OSError):
            pass
    return EXIT_OK


def _result_stdout(work):
    """Run `work(out)` with everything printed going to stderr (progress, and
    whatever the modules it loads print), so stdout carries only the JSON
    line `work` writes to `out`: a caller can read stdout as the result."""
    _utf8_streams()
    out, sys.stdout = sys.stdout, sys.stderr
    try:
        return work(out)
    finally:
        sys.stdout = out


def main(argv=None) -> int:
    return _result_stdout(lambda out: _main(argv, out))


def _publish_failure(out, out_dir: str, result: dict, code: int) -> int:
    """A job that did not run: result.json and the report in `out_dir`, the
    JSON line on stdout; returns `code`."""
    try:
        write_outputs(out_dir, result, f"# Sleep Study driver analysis\n\nFailed: {result['code']} {result['message']}\n")
    except OSError:
        pass
    print(json.dumps({"status": "error", "code": result["code"], "message": result["message"],
                      "result": os.path.join(out_dir, "result.json")},
                     ensure_ascii=False), file=out, flush=True)
    return code


def _main(argv, out) -> int:
    try:
        args = build_parser().parse_args(argv)
    except JobError as exc:
        return _publish_failure(out, _out_arg(argv), error_result(exc.code, exc.message), exc.exit_code)
    out_dir = os.path.abspath(args.out or default_out_dir())
    if args.collect_only:
        return collect_only(out_dir, args.make_report)
    case_id = args.case_id or ""
    try:
        job = validate(job_from_args(args))
        case_id = job["case_id"]
        if job["collect"]:
            collect(job, out_dir, log=_progress)
        factory = template = None
        if not job["dry_run"]:
            factory, template = load_agents()
        result = run(job, out_dir, agent_factory=factory, point_template=template, log=_progress)
        markdown = (f"# Sleep Study driver analysis\n\n"
                    f"_sleepstudy-analyze {TOOL_VERSION} · schema {SCHEMA_VERSION}"
                    + (f" · case {case_id}" if case_id else "") + "_\n\n"
                    + result.pop("_markdown", ""))
        write_outputs(out_dir, result, markdown)
        counts = {}
        for s in result["sessions"]:
            counts[s["status"]] = counts.get(s["status"], 0) + 1
        print(json.dumps({"status": "ok", "result": os.path.join(out_dir, "result.json"),
                          "report": os.path.join(out_dir, "report.html"),
                          "sessions": counts,
                          "missing_driver_log": result.get("missing_driver_log", False),
                          "findings": [{"session_id": s["session_id"], "device": s.get("device"),
                                        "code": s["code"],
                                        "summary": (s.get("finding") or {}).get("summary") or s.get("message") or ""}
                                       for s in result["sessions"]],
                          "driver_log_reach": {d: {"from": r["from"], "exact": r["exact"],
                                                   "bad_sessions_older": r["bad_sessions_older"],
                                                   "bad_sessions_unknown": r["bad_sessions_unknown"]}
                                               for d, r in (result.get("driver_log_reach") or {}).items()}},
                         ensure_ascii=False), file=out, flush=True)
        if args.open:
            try:
                os.startfile(os.path.join(out_dir, "report.html"))
            except (AttributeError, OSError):
                pass
        return EXIT_OK
    except JobError as exc:
        result, code = error_result(exc.code, exc.message, case_id), exc.exit_code
    except Exception as exc:          # noqa: BLE001 -- reported to the caller
        traceback.print_exc()
        result, code = error_result("E_ANALYSIS_FAILED", f"{type(exc).__name__}: {exc}", case_id), EXIT_ERROR
    return _publish_failure(out, out_dir, result, code)


if __name__ == "__main__":
    sys.exit(main())
