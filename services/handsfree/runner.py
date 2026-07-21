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
            analysis.error = "" if analysis.ok else "triage failed"
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

        # -- 4. pick the ZIP attachment ----------------------------------------
        # Reader's nomination first; newest-ZIP heuristic as fallback.
        zip_item = None
        with self._stage(analysis, "pick_zip"):
            from utils.etl_utils import pick_latest_zip_attachment, extract_time_from_description
            from .case_reader import find_attachment
            chosen_name = (analysis.case_reader or {}).get("attachment_name") or ""
            if chosen_name:
                zip_item = find_attachment(case_ctx.attachment_list, chosen_name)
                if zip_item is not None and not str(zip_item[0]).lower().endswith(".zip"):
                    self.progress("pick_zip",
                                  f"reader chose non-zip '{zip_item[0]}' — falling back")
                    zip_item = None
            if zip_item is None:
                zip_item = pick_latest_zip_attachment(case_ctx.attachment_list)
                if chosen_name and zip_item is not None:
                    self.progress("pick_zip",
                                  f"fallback to newest ZIP: {zip_item[0]}")
            if zip_item is not None:
                analysis.chosen_attachment = str(zip_item[0])
                try:
                    subtitle = (zip_item[2] or ["", ""])[1] if len(zip_item) > 2 else ""
                    analysis.attachment_time = extract_time_from_description(subtitle) or ""
                except Exception:
                    pass
        if zip_item is None:
            analysis.mode = "triage_only"
            analysis.ok = bool(analysis.triage)
            analysis.error = "" if analysis.ok else "no ZIP attachment and triage failed"
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
        with self._stage(analysis, "decompose"):
            from utils.attachment_decompose import process_single_zip
            file_path, _name, already = downloaded[0]
            wifi_files, ddd_files, _evt, _bt, _fw = process_single_zip(
                file_path, case_ctx.case_download_dir, already)
        if not wifi_files and not ddd_files:
            analysis.mode = "triage_only"
            analysis.ok = bool(analysis.triage)
            analysis.error = "no Wi-Fi/DDD ETL files found in the attachment"
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

        # -- 10. agentic analysis (one run per incident time) --------------------
        with self._stage(analysis, "agent_analysis"):
            self._run_agent(analysis, max_steps=max_steps)

        got_report = any(i.report for i in analysis.incidents)
        analysis.mode = "full" if got_report else "triage_only"
        analysis.ok = got_report or bool(analysis.triage)
        if not got_report:
            analysis.error = analysis.error or "agent produced no report; triage used instead"
        return analysis

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
