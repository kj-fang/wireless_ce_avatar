"""Issue-context application service shared by all chatbot profiles."""

from __future__ import annotations

import re

from flask import session

from models.models import CaseContext
from utils.etl_utils import extract_time_from_description
from utils.issue_time_utils import (
    format_issue_time,
    resolve_issue_time,
)

def organized_issue_context(raw_desc: str, first_ts, last_ts, log_path: str = "",
                            *, llm_client_model, domain: str = "") -> dict:
    """Return the organized issue context (clean description + issue time list).

    Prefers the quick pre-pass cached at the select-attachments step
    (``_issue_ai_quick``) so the whole flow makes a SINGLE LLM call — its
    (possibly undated) times are just re-aligned to the loaded log's date here.
    Falls back to organizing now (e.g. direct chatbot entry with no prior step).

    ``llm_client_model`` is the profile's zero-arg accessor returning
    ``(client, model)``; ``log_path`` is optional because only the profiles that
    resolve time-only logs against the capture file pass it.
    """
    # Imported lazily: utils.issue_time_ai pulls in the LLM stack, which the
    # cheap context helpers above must not drag in at import time.
    from utils.issue_time_ai import organize_issue_context, realign_times_to_log

    quick = session.get("_issue_ai_quick")
    if isinstance(quick, dict) and isinstance(quick.get("data"), dict):
        d = quick["data"]
    else:
        from datetime import datetime

        from models.models import CaseContext
        from services import gather_service

        client, model = llm_client_model()
        started_at = datetime.now()
        d, usage = organize_issue_context(
            raw_desc, first_ts=first_ts, last_ts=last_ts,
            llm_client=client, llm_model=model, return_usage=True,
        )
        session["_issue_ai_quick"] = {"data": d}
        # Cost accounting for the pre-pass. Best-effort: analytics must never
        # break the flow that produced them.
        if int(usage.get("llm_calls") or 0) > 0:
            try:
                issue = CaseContext.from_session(session.get("case_context") or {}).to_dict()
                gather_service.record_feature_usage(
                    workflow_id=session.get("gather_workflow_id", ""),
                    feature_code="issue_time_prepass",
                    model=model or "", usage=usage, issue=issue, domain=domain,
                    trigger="direct_chatbot_context",
                    latency_ms=int((datetime.now() - started_at).total_seconds() * 1000),
                )
            except Exception:
                pass
    return {
        "clean_description": d.get("clean_description") or raw_desc,
        "issue_times": realign_times_to_log(d.get("issue_times") or [], first_ts, last_ts, log_path),
        "interpretation": d.get("interpretation", ""),
    }


def extract_issue_context() -> dict:
    """
    Consolidate issue context from session into a single dict with keys:
      case_nbr, subject, description, issue_type

    Sources (in priority order):
      - session['ai_ips_analysis']  : LLM-generated analysis dict (richest)
      - session['classification']   : issue_type + confidence
      - session['case_context']     : raw Salesforce case fields
    """
    # --- raw case fields ---
    raw_ctx = session.get("case_context", {})
    ctx = CaseContext.from_session(raw_ctx) if raw_ctx else CaseContext()

    # --- classification ---
    classification = session.get("classification", {})
    issue_type = (classification.get("issue_type", "")
                  if isinstance(classification, dict) else "")

    # --- ai_ips_analysis: LLM-generated structured summary ---
    ai_analysis = session.get("ai_ips_analysis", {})
    if not isinstance(ai_analysis, dict):
        ai_analysis = {}

    # Build a rich description: start with the LLM summary if available,
    # fall back to the raw Salesforce description.
    description_parts = []
    if ctx.description:
        description_parts.append(ctx.description)

    # Append key fields from the LLM analysis (skip Classification sub-dict
    # and nested dicts that are not human-readable strings)
    for key, val in ai_analysis.items():
        if key.lower() == "classification":
            continue
        if isinstance(val, str) and val.strip():
            description_parts.append(f"{key}: {val.strip()}")
        elif isinstance(val, list):
            flat = "; ".join(str(v) for v in val if v)
            if flat:
                description_parts.append(f"{key}: {flat}")

    attachment_time = ""

    # Return cached value if already computed this session (avoids re-parsing on every request)
    cached = session.get("_attachment_time_cache")
    if cached is not None:
        attachment_time = cached
    else:
        def _desc_time_to_str(desc: str) -> str:
            """Parse issue time from attachment gray subtitle text and normalize to MM/DD/YYYY-HH:MM:SS."""
            parsed = extract_time_from_description(desc)
            if hasattr(parsed, 'strftime'):
                return parsed.strftime('%m/%d/%Y-%H:%M:%S')
            if isinstance(parsed, str) and parsed.strip():
                # time-only case: keep as HH:MM:SS so agent can still apply segment2 on log date.
                return parsed.strip()
            return ""

        # Step 1: Get the list of selected file names
        selected_files = session.get("selected_files", [])
        selected_names = set()
        for sf in selected_files:
            if isinstance(sf, (list, tuple)) and len(sf) >= 1:
                selected_names.add(sf[0])

        # Step 2: Read from case_context.attachment_list (same data source as the template).
        # Heavy fields like attachment_list are stashed on disk for big
        # cases — go through from_session() so the sidecar is loaded.
        raw_ctx_dict = session.get("case_context", {})
        if isinstance(raw_ctx_dict, dict) and raw_ctx_dict:
            raw_ctx_dict = CaseContext.from_session(raw_ctx_dict).to_dict()
        att_list = raw_ctx_dict.get("attachment_list", []) if isinstance(raw_ctx_dict, dict) else []

        # Step 3: Prefer user-selected attachments; if selected_names is empty, take the first one
        candidates = [item for item in att_list
                      if isinstance(item, (list, tuple)) and len(item) >= 3
                      and (not selected_names or item[0] in selected_names)]
        if not candidates:
            candidates = [item for item in att_list if isinstance(item, (list, tuple)) and len(item) >= 3]

        # Step 4 (PRIMARY): parse from gray subtitle description (item[2][1])
        for item in candidates:
            desc_raw = item[2][1] if isinstance(item[2], (list, tuple)) and len(item[2]) >= 2 else None
            result = _desc_time_to_str(desc_raw)
            if result:
                attachment_time = result
                print(f"[DEBUG] attachment_time from attachment description['{item[0]}']: {attachment_time}")
                break

        # Step 5: Fallback — try directly from selected_files
        if not attachment_time:
            for file_info in selected_files:
                if isinstance(file_info, (list, tuple)) and len(file_info) >= 3:
                    desc_raw = file_info[2][1] if isinstance(file_info[2], (list, tuple)) and len(file_info[2]) >= 2 else None
                    result = _desc_time_to_str(desc_raw)
                    if result:
                        attachment_time = result
                        print(f"[DEBUG] attachment_time from selected_files description['{file_info[0]}']: {attachment_time}")
                        break

        if not attachment_time:
            print(f"[DEBUG] attachment_time: NOT FOUND. selected_names={selected_names}, att_list len={len(att_list)}")

        # Cache in session so subsequent requests in the same flow skip re-parsing
        session["_attachment_time_cache"] = attachment_time

    return {
        "case_nbr":    ctx.case_nbr or "",
        "subject":     ctx.subject or "",
        "description": "\n".join(description_parts),
        "issue_type":  issue_type,
        "attachment_time": attachment_time,
    }

def resolved_issue_time_for(log_path: str, attachment_time: str) -> str:
    """
    Session-level cache for the canonical sidebar-prefill issue_time.
    Mirrors `_attachment_time_cache`: keyed by log_path so reloading a
    different log naturally invalidates. Lets /get_issue_context return
    the same value prime_with_context resolved without re-reading the
    log file's first/last timestamps every time.
    """
    cache = session.get("_resolved_issue_time_cache") or {}
    cache_key = log_path or "__nolog__"
    if cache_key in cache:
        return cache[cache_key]
    dt, _ = resolve_issue_time(attachment_time, log_path)
    formatted = format_issue_time(dt)
    cache[cache_key] = formatted
    session["_resolved_issue_time_cache"] = cache
    return formatted

def extract_disconnect_time(*text_sources: str) -> str:
    """
    Search multiple text sources for the most precise disconnect/event
    timestamp.  Returns a string like ' at around 10/28/2025-11:25:49'
    or '' if nothing found.

    Tries several common formats:
      MM/DD/YYYY-HH:MM:SS(.mmm)
      MM/DD/YYYY HH:MM:SS
      YYYY-MM-DD HH:MM:SS
      YYYY/MM/DD HH:MM:SS
    """
    patterns = [
        r'(\d{1,2}/\d{1,2}/\d{4}[\s-]\d{1,2}:\d{2}:\d{2}(?:\.\d{1,3})?)',
        r'(\d{4}[-/]\d{1,2}[-/]\d{1,2}[\sT]\d{1,2}:\d{2}:\d{2})',
    ]
    for src in text_sources:
        if not src:
            continue
        for pat in patterns:
            m = re.search(pat, src)
            if m:
                return f" at around {m.group(1)}"
    return ""

def compose_concise_description(ctx: dict = None) -> str:
    """
    Auto-compose the most effective issue description for auto-analysis via chat.

        Format: "<problem statement> <timestamp>"
        e.g. "6G Weak Signal disconnected at around 10/28/2025-11:25:49"
    """
    try:
        if ctx is None:
            ctx = extract_issue_context()
    except Exception:
        return "Perform full multi-skill log analysis"

    subject = ctx.get("subject", "")
    desc_raw = ctx.get("description", "")
    attachment_time = ctx.get("attachment_time", "")
    # Prefer attachment time from selected file; fall back to text extraction
    if attachment_time:
        time_hint = f" at around {attachment_time}"
    else:
        time_hint = extract_disconnect_time(subject, desc_raw)

    # Best: clean subject line — strip ALL leading [tag] groups
    if subject:
        clean = re.sub(r'^(\[.*?\]\s*)+', '', subject).strip()    # remove ALL [xxx] tags
        clean = re.sub(r'\s*\(F/R.*?\)\s*$', '', clean).strip()   # remove (F/R：1/1u,40/200C)
        if clean:
            return f"{clean}{time_hint}"

    # Last resort: first 200 chars of description
    if desc_raw:
        return desc_raw[:200].strip() + time_hint

    return "Perform full multi-skill log analysis"
