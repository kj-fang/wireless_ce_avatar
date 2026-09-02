"""
Headless per-case analysis pipeline for the Handsfree Replyer.

Replays the full avatar user journey without the web UI:

    fetch case (Snowflake/Salesforce) → description triage → pick newest ZIP
    → download → decompose → pick ETL by issue time → decode ETL→.log
    → issue-time organization → agentic root-cause analysis (one run per
      detected incident time, like the UI's multi-time chaining)

Design notes:
  * Every stage records a StageStatus so the review UI can show exactly how
    far a case got and why it stopped.
  * No usable log ⇒ mode="triage_only": the description-level triage
    (analyze_desc) becomes the comment draft (missing-info questions,
    "request logs" recommendations).
  * The ETL decoder (wpp_ddd_parser_run) calls sys.exit() on some failure
    paths and opens TextAnalysisTool/Explorer windows on success — we catch
    SystemExit and suppress the GUI via the AVATAR_HEADLESS_DECODE env flag
    honored in parse_single_binary.
  * The agent run mirrors services/ace/eval/replay.py: fresh
    WifiLogAgentSystem, current_log_path BEFORE prime_with_context,
    chat(use_tools=True) → {"type": "report", "data": {...}}.
"""

from __future__ import annotations

import json
import os
import time
import traceback
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

# Cap agent runs per case: the UI chains one run per detected incident time;
# headless we keep the two most relevant to bound cost.
MAX_INCIDENTS = 2
MAX_AGENT_STEPS = 6
# Cap captured trace rows per incident (for the review UI).
_STEP_CONTENT_CAP = 300
_MAX_STEPS_KEPT = 60
# Every archive type attachment_decompose.extract_archive can open.
_ARCHIVE_EXTS = (".zip", ".rar", ".7z")


def _pick_latest_archive(attachment_list):
    """Newest log archive by upload-timestamp metadata (generalizes
    etl_utils.pick_latest_zip_attachment, which is .zip-only, to every
    archive type the decomposer supports). Falls back to the first archive
    when no timestamp parses."""
    items = [it for it in (attachment_list or [])
             if it and str(it[0]).lower().endswith(_ARCHIVE_EXTS)]
    if not items:
        return None

    def _ts(item):
        try:
            meta = item[2] if len(item) > 2 else None
            raw = str(meta[0]) if meta else ""
        except Exception:
            raw = ""
        from datetime import datetime
        for fmt in ("%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M %p", "%m/%d/%Y",
                    "%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M"):
            try:
                return datetime.strptime(raw.strip(), fmt)
            except Exception:
                continue
        return None

    dated = [(it, t) for it in items if (t := _ts(it)) is not None]
    if dated:
        dated.sort(key=lambda p: p[1], reverse=True)
        return dated[0][0]
    return items[0]


@dataclass
class StageStatus:
    name: str
    ok: bool
    detail: str = ""
    duration_ms: int = 0


@dataclass
class IncidentReport:
    issue_time: str = ""
    user_message: str = ""
    result_type: str = ""            # report | partial_report | text | error
    report: Optional[dict] = None    # submit_final_report args
    report_text: str = ""
    steps: list[dict] = field(default_factory=list)
    error: str = ""

    @property
    def confidence(self) -> Optional[int]:
        if isinstance(self.report, dict):
            v = self.report.get("confidence_score")
            return int(v) if isinstance(v, (int, float)) else None
        return None


@dataclass
class CaseAnalysis:
    case_nbr: str
    case_id: str = ""                # Salesforce 18-char id (for posting)
    ok: bool = False
    mode: str = ""                   # full | triage_only | error
    subject: str = ""
    description: str = ""
    clean_description: str = ""
    issue_type: str = ""
    wifi_or_bt: str = ""
    triage: dict = field(default_factory=dict)       # analyze_desc output
    classification: dict = field(default_factory=dict)
    case_reader: dict = field(default_factory=dict)  # comment-aware reader output
    attachment_time: str = ""
    chosen_attachment: str = ""      # filename the reader picked (or fallback)
    issue_times: list[str] = field(default_factory=list)
    etl_path: str = ""
    log_path: str = ""
    incidents: list[IncidentReport] = field(default_factory=list)
    stages: list[StageStatus] = field(default_factory=list)
    echo_insights: list = field(default_factory=list)  # Echo KB root-cause answers
    time_mismatch: dict = field(default_factory=dict)  # log doesn't cover issue time
    error: str = ""

    @property
    def best_incident(self) -> Optional[IncidentReport]:
        with_reports = [i for i in self.incidents if i.report]
        if not with_reports:
            return None
        return max(with_reports, key=lambda i: i.confidence or 0)

    def to_dict(self) -> dict:
        return json.loads(json.dumps(asdict(self), default=str))


def _noop(_stage: str, _detail: str = "") -> None:
    pass


class HandsfreeRunner:
    """One instance per run; stateless between cases."""

    def __init__(self, progress_cb: Optional[Callable[[str, str], None]] = None):
        self.progress = progress_cb or _noop

    # ---- stage helper ----
    def _stage(self, analysis: CaseAnalysis, name: str):
        runner = self

        class _Ctx:
            def __enter__(ctx):
                ctx.t0 = time.time()
                runner.progress(name, "started")
                return ctx

            def __exit__(ctx, exc_type, exc, tb):
                dur = int((time.time() - ctx.t0) * 1000)
                if exc is None:
                    analysis.stages.append(StageStatus(name, True, duration_ms=dur))
                    runner.progress(name, "done")
                else:
                    detail = f"{type(exc).__name__}: {exc}"
                    analysis.stages.append(StageStatus(name, False, detail, dur))
                    runner.progress(name, f"failed: {detail}")
                # Never propagate — callers check the returned ok flags.
                return True

        return _Ctx()

    # ------------------------------------------------------------------
    def analyze_case(self, case_nbr: str, *, max_steps: int = MAX_AGENT_STEPS,
                     max_incidents: int = MAX_INCIDENTS) -> CaseAnalysis:
        from configs.global_configs import app_config

        analysis = CaseAnalysis(case_nbr=str(case_nbr))
        case_ctx = None

        # -- 1. fetch case info ------------------------------------------------
        with self._stage(analysis, "fetch_case"):
            from models.models import CaseContext
            from services.case_info_service import CaseService
            case_ctx = CaseService.process_case(CaseContext(case_nbr=str(case_nbr)))
            analysis.case_id = case_ctx.id or ""
            analysis.subject = case_ctx.subject or ""
            analysis.description = case_ctx.description or ""
            analysis.wifi_or_bt = case_ctx.wifi_or_bt or "wifi"
        if case_ctx is None or not (analysis.subject or analysis.description):
            analysis.mode = "error"
            analysis.error = "case fetch failed — no subject/description"
            return analysis

        # -- 2. description triage (always runs; also the no-log fallback) ----
        with self._stage(analysis, "triage"):
            from services.case_info_service import CaseService
            llm = app_config.llm_helper
            prompt_path = CaseService.load_case_summary_prompt(analysis.wifi_or_bt)
            ctx_dict = case_ctx.to_dict()
            triage = llm.analyze_desc(prompt_path, ctx_dict)
            if isinstance(triage, dict):
                analysis.triage = triage
                analysis.classification = triage.get("Classification") or {}
                analysis.issue_type = analysis.classification.get("issue_type", "") or ""

        # BT cases: the agentic analyzer is Wi-Fi; deliver triage only (v1).
        if analysis.wifi_or_bt != "wifi":
            analysis.mode = "triage_only"
            analysis.ok = bool(analysis.triage)
            analysis.error = ("BT case — v1 runs description triage only"
                              if analysis.ok else "triage failed")
            return analysis

        # -- 3. read the case history (description + comments, chronological) --
        # The reader mirrors how an engineer works the case: description
        # first, then comments oldest→newest (later comments supersede),
        # extracting the issue time and nominating the attachment whose log
        # most likely covers it.
        with self._stage(analysis, "read_case_history"):
            from .case_reader import read_case_history
            llm = app_config.llm_helper
            reader = read_case_history(
                llm,
                subject=analysis.subject,
                description=analysis.description,
                comments=case_ctx.comments,
                attachment_list=case_ctx.attachment_list,
            )
            if reader:
                analysis.case_reader = reader
                if reader.get("clean_description"):
                    analysis.clean_description = reader["clean_description"]
                if reader.get("issue_times"):
                    analysis.issue_times = reader["issue_times"][:max_incidents]
                self.progress(
                    "read_case_history",
                    f"issue_times={reader.get('issue_times')} "
                    f"attachment={reader.get('attachment_name') or '(none)'} "
                    f"({reader.get('issue_time_source') or 'no source'})")

        # -- 4. pick the log-archive attachment --------------------------------
        # Reader's nomination first; newest-archive heuristic as fallback.
        # Accept every archive type the decomposer can extract — OEMs upload
        # .7z and .rar captures as often as .zip (case 00993799 was a .7z).
        zip_item = None
        pick_ok = False   # distinguishes "no archive found" from "stage crashed"
        with self._stage(analysis, "pick_zip"):
            from utils.etl_utils import extract_time_from_description
            from .case_reader import find_attachment
            chosen_name = (analysis.case_reader or {}).get("attachment_name") or ""
            if chosen_name:
                zip_item = find_attachment(case_ctx.attachment_list, chosen_name)
                if zip_item is not None and not str(zip_item[0]).lower().endswith(_ARCHIVE_EXTS):
                    self.progress("pick_zip",
                                  f"reader chose non-archive '{zip_item[0]}' — falling back")
                    zip_item = None
            if zip_item is None:
                zip_item = _pick_latest_archive(case_ctx.attachment_list)
                if chosen_name and zip_item is not None:
                    self.progress("pick_zip",
                                  f"fallback to newest archive: {zip_item[0]}")
            if zip_item is not None:
                analysis.chosen_attachment = str(zip_item[0])
                try:
                    subtitle = (zip_item[2] or ["", ""])[1] if len(zip_item) > 2 else ""
                    analysis.attachment_time = extract_time_from_description(subtitle) or ""
                except Exception:
                    pass
            pick_ok = True
        if not pick_ok:
            # pick_zip CRASHED (_stage swallowed the exception) — we don't
            # actually know whether logs are attached, so never ask the
            # customer for them. Triage fallback instead.
            analysis.mode = "triage_only"
            analysis.ok = bool(analysis.triage)
            analysis.error = "archive selection failed"
            return analysis
        if zip_item is None:
            # No archive at all — certainly no WRT logs. Record the check
            # stage explicitly so the stage table tells the story, then draft
            # a request-logs reply (mode "request_logs": human-approved,
            # posted as a PUBLIC comment so the customer sees it).
            with self._stage(analysis, "check_wrt_log"):
                self.progress("check_wrt_log",
                              "no log archive attached — WRT logs missing")
            analysis.mode = "request_logs"
            analysis.ok = True
            analysis.error = ("no WRT log archive (.zip/.rar/.7z) attached — "
                              "drafted a request-logs reply to the customer")
            return analysis

        # -- 5. download -------------------------------------------------------
        downloaded = []   # [file_path, name, already_dload]
        with self._stage(analysis, "download"):
            from utils.attachment_download import run_dload_threads
            for item in run_dload_threads([zip_item], case_ctx.case_download_dir,
                                          socketio=None):
                downloaded.append(item)
        if not downloaded:
            analysis.mode = "triage_only"
            analysis.ok = bool(analysis.triage)
            analysis.error = "attachment download failed"
            return analysis

        # -- 6. decompose ------------------------------------------------------
        wifi_files: list = []
        ddd_files: list = []
        decompose_ok = False
        with self._stage(analysis, "decompose"):
            from utils.attachment_decompose import process_single_zip
            file_path, _name, already = downloaded[0]
            wifi_files, ddd_files, _evt, _bt, _fw = process_single_zip(
                file_path, case_ctx.case_download_dir, already)
            decompose_ok = True
        if not decompose_ok:
            # decompose CRASHED (_stage swallowed the exception): the archive
            # may well contain WRT logs we simply failed to extract — asking
            # the customer to re-upload would be wrong AND public. Empty file
            # lists mean "no logs" ONLY when decompose itself succeeded.
            analysis.mode = "triage_only"
            analysis.ok = bool(analysis.triage)
            analysis.error = "attachment decompose failed"
            return analysis
        # -- 6b. confirm WRT logs exist in the unzipped archive ----------------
        # The archive downloaded and decomposed — now verify it actually
        # contains driver ETL traces (WRT wifi ETLs / DDD). An archive of
        # screenshots or dmesg dumps is not analyzable: ask for WRT logs.
        with self._stage(analysis, "check_wrt_log"):
            if wifi_files or ddd_files:
                self.progress("check_wrt_log",
                              f"ETLs found — wifi(WRT): {len(wifi_files)}, "
                              f"DDD: {len(ddd_files)}")
            else:
                self.progress("check_wrt_log",
                              f"'{analysis.chosen_attachment}' contains no "
                              "WRT/DDD ETL logs")
        if not wifi_files and not ddd_files:
            analysis.mode = "request_logs"
            analysis.ok = True
            analysis.error = (f"attachment '{analysis.chosen_attachment}' contains "
                              "no WRT ETL logs — drafted a request-logs reply")
            return analysis

        # -- 7. issue times -----------------------------------------------------
        # Primary source: the case-history reader (description + comments).
        # Fallback: description-only AI organization, then attachment subtitle.
        with self._stage(analysis, "issue_time"):
            if not analysis.issue_times:
                from utils.issue_time_ai import organize_issue_context
                llm = app_config.llm_helper
                organized = organize_issue_context(
                    analysis.description,
                    llm_client=getattr(llm, "client", None),
                    llm_model=getattr(llm, "model", None),
                ) or {}
                if not analysis.clean_description:
                    analysis.clean_description = (organized.get("clean_description")
                                                  or "").strip()
                times = [str(t).strip() for t in (organized.get("issue_times") or [])
                         if str(t).strip()]
                if not times and analysis.attachment_time:
                    times = [analysis.attachment_time]
                analysis.issue_times = times[:max_incidents]
        if not analysis.clean_description:
            analysis.clean_description = analysis.description or analysis.subject

        # -- 8. pick the ETL ----------------------------------------------------
        etl_path = None
        with self._stage(analysis, "pick_etl"):
            etl_path = self._pick_etl(wifi_files, ddd_files,
                                      analysis.issue_times[0] if analysis.issue_times else "")
            analysis.etl_path = etl_path or ""
        if not etl_path:
            analysis.mode = "triage_only"
            analysis.ok = bool(analysis.triage)
            analysis.error = "could not select an ETL file"
            return analysis

        # -- 9. decode ETL -> .log ----------------------------------------------
        log_path = etl_path + ".log"
        if not os.path.exists(log_path):
            with self._stage(analysis, "decode_etl"):
                self._decode_etl(etl_path)
        if not os.path.exists(log_path):
            analysis.mode = "triage_only"
            analysis.ok = bool(analysis.triage)
            analysis.error = f"ETL decode did not produce {os.path.basename(log_path)}"
            return analysis
        analysis.log_path = log_path

        # -- 9b. does the log actually cover the reported issue time? ----------
        # e.g. case 01025350: issue stated at 17:14, capture covered
        # 17:39–17:44. The agent may still find an assert, but the reviewer
        # (and the customer) must know the log is from a different time.
        try:
            self._check_time_coverage(analysis)
        except Exception as e:
            print(f"[handsfree.runner] time-coverage check failed (non-fatal): {e}")

        # -- 10. agentic analysis (one run per incident time) --------------------
        with self._stage(analysis, "agent_analysis"):
            self._run_agent(analysis, max_steps=max_steps)

        # -- 11. Echo knowledge base: assert / yellow-bang root cause ------------
        # When the agent surfaced a firmware assert (or yellow-bang evidence),
        # resolve the assert to its driver-header entry and ask the Echo
        # knowledge base (MCP, KB-only mode — no credentials) for the known
        # root cause. Echo being down never affects the pipeline: failures are
        # recorded per-insight, and _stage() contains anything unexpected.
        with self._stage(analysis, "echo_kb"):
            from .echo_client import (EchoUnavailable, ask_echo_kb,
                                      collect_echo_insights, find_assert_evidence)
            evidence = find_assert_evidence(analysis)
            if evidence["assert_codes"] or evidence["yellow_bang"]:
                src = {"wrt_log": "from WRT log", "agent_text": "from agent text",
                       None: ""}.get(evidence.get("source"), "")
                self.progress(
                    "echo_kb",
                    f"asking Echo KB — asserts: {evidence['assert_codes'] or 'none'}"
                    + (f" ({src})" if src else "")
                    + f", yellow bang: {evidence['yellow_bang']}")

                def _clip(text: object, limit: int) -> str:
                    s = str(text).strip()
                    return s if len(s) <= limit else s[:limit] + " …[truncated]"

                def _ask_logged(question: str) -> str:
                    # Surface the exchange in the run log (full text stays in
                    # analysis.echo_insights → draft's Analysis details).
                    self.progress("echo_kb", "Q → Echo:\n" + _clip(question, 700))
                    try:
                        answer = ask_echo_kb(question)
                    except EchoUnavailable as e:
                        self.progress("echo_kb", f"Echo unavailable: {_clip(e, 300)}")
                        raise
                    self.progress("echo_kb", "A ← Echo:\n" + _clip(answer, 900))
                    return answer

                analysis.echo_insights = collect_echo_insights(analysis,
                                                               ask=_ask_logged)
                answered = sum(1 for i in analysis.echo_insights if i.get("answer"))
                self.progress(
                    "echo_kb",
                    f"{answered}/{len(analysis.echo_insights)} insight(s) answered")
            else:
                self.progress("echo_kb", "no assert / yellow-bang evidence — skipped")

        got_report = any(i.report for i in analysis.incidents)
        analysis.mode = "full" if got_report else "triage_only"
        analysis.ok = got_report or bool(analysis.triage)
        if not got_report:
            analysis.error = analysis.error or "agent produced no report; triage used instead"
        return analysis

    # ------------------------------------------------------------------
    def _check_time_coverage(self, analysis: CaseAnalysis,
                             grace_minutes: int = 10) -> None:
        """Flag when NO reported issue time falls inside the decoded log's
        time range (±grace). Only full datetimes count — time-only values
        borrow the log's date downstream, so they are inside by construction.
        Sets analysis.time_mismatch and emits a WARNING progress line."""
        from datetime import timedelta
        from utils.issue_time_utils import (format_issue_time,
                                            parse_issue_time_string,
                                            read_log_time_range)

        if not analysis.issue_times or not analysis.log_path:
            return
        first, last = read_log_time_range(analysis.log_path)
        if not first or not last:
            return

        grace = timedelta(minutes=grace_minutes)
        checked: list[str] = []
        covered = False
        for raw in analysis.issue_times:
            dt, time_only = parse_issue_time_string(str(raw))
            if dt is None or time_only:
                continue
            # Resolve customer-vs-log frame when tz anchors are available
            # (system_info.txt / folder timestamp); harmless no-op otherwise.
            try:
                from utils.issue_time_ai import determine_issue_time_frames
                dt = determine_issue_time_frames(
                    dt, [analysis.log_path], first, last).get("log_frame") or dt
            except Exception:
                pass
            checked.append(str(raw))
            if first - grace <= dt <= last + grace:
                covered = True
                break

        if checked and not covered:
            analysis.time_mismatch = {
                "issue_times": checked,
                "log_first": format_issue_time(first, with_ms=False),
                "log_last": format_issue_time(last, with_ms=False),
                "grace_minutes": grace_minutes,
            }
            self.progress(
                "decode_etl",
                f"WARNING: log covers {analysis.time_mismatch['log_first']} – "
                f"{analysis.time_mismatch['log_last']} but the reported issue "
                f"time(s) {', '.join(checked)} are OUTSIDE this window")

    # ------------------------------------------------------------------
    def _pick_etl(self, wifi_files: list, ddd_files: list,
                  issue_time_str: str) -> Optional[str]:
        """AI-time pick first; fallback = newest DDD, then newest Wi-Fi ETL.
        (Deliberately NOT get_auto_analysis_etl — that reads the Flask session.)"""
        from utils.issue_time_ai import pick_etl_by_ai_time

        file_dicts = {"wifi_dict": {"zip": list(wifi_files or [])},
                      "ddd_dict": {"zip": list(ddd_files or [])}}
        if issue_time_str:
            try:
                picked = pick_etl_by_ai_time(file_dicts, issue_time_str)
                if picked:
                    return picked
            except Exception as e:
                print(f"[handsfree.runner] AI-time ETL pick failed: {e}")

        def _newest(paths: list) -> Optional[str]:
            import re as _re
            etls = [p for p in (paths or [])
                    if str(p).lower().endswith(".etl")
                    or _re.search(r"\.etl\.\d+$", str(p), _re.IGNORECASE)]
            if not etls:
                return None
            try:
                from utils.etl_utils import extract_timestamp_from_folder
                def _key(p):
                    ts = extract_timestamp_from_folder(str(p))
                    return (ts is not None, ts, str(p))
                return max(etls, key=_key)
            except Exception:
                return sorted(etls)[-1]

        return _newest(ddd_files) or _newest(wifi_files)

    def _decode_etl(self, etl_path: str) -> None:
        """Run the WPP/DDD decoder headlessly.

        parse_single_binary honors AVATAR_HEADLESS_DECODE=1 to skip opening
        TextAnalysisTool + Explorer. SystemExit (the decoder's failure exits)
        is caught so it cannot kill the app process.
        """
        prev = os.environ.get("AVATAR_HEADLESS_DECODE")
        os.environ["AVATAR_HEADLESS_DECODE"] = "1"
        try:
            from services.etl_parser.wpp_ddd_parser import wpp_ddd_parser_run
            wpp_ddd_parser_run(etl_path)
        except SystemExit as e:
            raise RuntimeError(f"decoder aborted (sys.exit {e.code})") from None
        finally:
            if prev is None:
                os.environ.pop("AVATAR_HEADLESS_DECODE", None)
            else:
                os.environ["AVATAR_HEADLESS_DECODE"] = prev

    def _build_agent(self):
        """Fresh agent per case; borrows client/model/skills (and the ACE
        runner, so learned playbooks apply) from the boot-time base agent."""
        from configs.global_configs import app_config
        from services.log_chatbot_service import WifiLogAgentSystem

        base = getattr(app_config, "log_chatbot_agent", None)
        llm = app_config.llm_helper
        skills = getattr(base, "skills", None) or getattr(llm, "skills", None) or {}
        agent = WifiLogAgentSystem(client=llm.client,
                                   model=getattr(llm, "model", "gpt-4.1"),
                                   skills=skills)
        ace_runner = getattr(base, "ace_runner", None)
        if ace_runner is not None:
            agent.attach_ace(ace_runner)
        return agent

    def _run_agent(self, analysis: CaseAnalysis, *, max_steps: int) -> None:
        agent = self._build_agent()
        agent.current_log_path = analysis.log_path
        agent.prime_with_context(
            case_nbr=analysis.case_nbr,
            subject=analysis.subject,
            description=analysis.clean_description,
            issue_type=analysis.issue_type,
            attachment_time=analysis.attachment_time,
        )

        times = analysis.issue_times or [""]
        for t in times:
            inc = IncidentReport(issue_time=t)
            captured: list[dict] = []

            def _capture(step, _cap=captured):
                if isinstance(step, dict) and len(_cap) < _MAX_STEPS_KEPT:
                    _cap.append({
                        "role": str(step.get("role") or ""),
                        "content": str(step.get("content") or "")[:_STEP_CONTENT_CAP],
                    })

            # Mirror the UI: per-incident issue-time override on the agent.
            if t:
                try:
                    from utils.issue_time_utils import parse_issue_time_string, resolve_issue_time
                    dt, _time_only = parse_issue_time_string(t)
                    if dt is not None:
                        resolved, _src = resolve_issue_time(t, analysis.log_path)
                        agent.issue_time = resolved or dt
                except Exception as e:
                    print(f"[handsfree.runner] issue-time override failed ({t}): {e}")

            inc.user_message = (
                f"{analysis.clean_description} at around {t}" if t
                else analysis.clean_description
            )
            self.progress("agent_analysis", f"incident @ {t or 'unspecified time'}")
            try:
                res = agent.chat(inc.user_message, use_tools=True,
                                 max_steps=max_steps, step_callback=_capture)
                inc.result_type = (res or {}).get("type", "") if isinstance(res, dict) else ""
                data = (res or {}).get("data") if isinstance(res, dict) else None
                if inc.result_type in ("report", "partial_report") and isinstance(data, dict):
                    inc.report = data
                    parts = [str(data.get("root_cause_summary") or "")]
                    if data.get("markdown_summary"):
                        parts.append(str(data["markdown_summary"]))
                    inc.report_text = "\n".join(p for p in parts if p)
                elif data is not None:
                    inc.report_text = data if isinstance(data, str) else json.dumps(data, default=str)
            except Exception as e:
                inc.result_type = "error"
                inc.error = f"{type(e).__name__}: {e}"
                print(f"[handsfree.runner] agent run failed:\n{traceback.format_exc()}")
            inc.steps = captured
            analysis.incidents.append(inc)
