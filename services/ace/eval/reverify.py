"""
Nightly re-answer + per-down-voter notification.

After `run-all` learns today's feedback into the playbook AND the regression
review PASSes, this module re-runs the agent (with the UPDATED playbook) on the
SAME cases that got a thumbs-DOWN, then emails each down-voter ONE combined
email whose attached HTML shows the agent re-answering their case(s) exactly as
the chatbot page would — so they can confirm the feedback made the agent better.

Flow:
  1. build_downvote_manifest() — run in the adapt phase; collects vote==-1
     turns (per namespace) into runs/<stamp>/downvotes_<stamp>_<ns>.json.
  2. reverify_and_notify() — run after review PASS; consumes one or more
     manifests (wifi + bt), re-answers each case with attached log against its
     namespace's updated playbook, groups by person across namespaces, and
     sends one combined email per person.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Optional

from services.ace import cli as ace_cli
from services.ace.pipeline import AceRunner
from services import feedback_service

from . import runner as eval_runner
from . import replay_html
from . import notify_email


# Redirect ALL reverify mail here for testing. Set to e.g.
# ["me@intel.com"] to route every per-user email to your own inbox; leave
# None to send to each down-voter's real address.
REVERIFY_REDIRECT_TO: Optional[list[str]] = ["wei-ling.chi@intel.com"]


_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _norm_email(value) -> str:
    if not isinstance(value, str):
        return ""
    out = value.strip().lower()
    return out if _EMAIL_RE.match(out) else ""


def _is_local_case(case_nbr: str) -> bool:
    cn = (case_nbr or "").strip().lower()
    return (not cn) or cn.startswith("local_")


def _parse_iso_ts(value: str) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def _feedback_ts_suffix(value: str) -> str:
    dt = _parse_iso_ts(value)
    if dt is None:
        return "unknown-time"
    return dt.strftime("%Y%m%d_%H%M%S")


def _feedback_ts_label(value: str) -> str:
    dt = _parse_iso_ts(value)
    if dt is None:
        return "unknown time"
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _case_title(entry: dict) -> str:
    case_nbr = str(entry.get("case_nbr") or "").strip()
    ts_label = _feedback_ts_label(str(entry.get("feedback_ts") or ""))
    if _is_local_case(case_nbr):
        return f"Local upload ({entry.get('log_path') or 'unknown log'}) [{ts_label}]"
    base = case_nbr or "unknown case"
    return f"{base} [{ts_label}]"


def _safe_email_filename(email: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", email).strip("._-") or "user"


def _safe_filename_stem(value: str, *, fallback: str = "case") -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", (value or "").strip()).strip("._-")
    return safe or fallback


# ---------------------------------------------------------------------------
# Manifest building (adapt phase)
# ---------------------------------------------------------------------------
def _load_snapshot(feedback_root: Path, prefix: str, cid: str) -> Optional[dict]:
    path = feedback_root / "conversations" / f"{prefix}{cid}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[reverify] snapshot read failed {path}: {e}")
        return None


def _find_attached_log(feedback_root: Path, prefix: str, cid: str,
                       turn_id: str) -> Optional[str]:
    """Locate the opt-in attached log for this turn, if any. Filenames are
    `<safe_turn>__<user>__<original_name>` (see feedback_service)."""
    safe_cid = feedback_service._safe_id(cid)
    safe_turn = feedback_service._safe_id(turn_id, fallback="turn")
    log_dir = feedback_root / "logs" / f"{prefix}{safe_cid}"
    if not log_dir.exists() or not log_dir.is_dir():
        return None
    for f in sorted(log_dir.iterdir()):
        if f.is_file() and f.name.startswith(f"{safe_turn}__"):
            return str(f)
    return None


def _feedback_detail_text(feedback: dict) -> str:
    """A short human summary of the user's original down-vote detail, if any."""
    details = (feedback or {}).get("details") or {}
    if not isinstance(details, dict):
        return ""
    parts: list[str] = []
    if details.get("agent_workflow"):
        parts.append(f"workflow: {details['agent_workflow']}")
    if details.get("correct_conclusion_tag"):
        parts.append(f"correct conclusion: {details['correct_conclusion_tag']}")
    issues = details.get("issues")
    if isinstance(issues, list):
        for it in issues:
            if isinstance(it, dict) and it.get("comment"):
                parts.append(str(it["comment"]))
    return "  \n".join(parts)


def build_downvote_manifest(
    *,
    namespace: str,
    feedback_root,
    playbooks_dir,
    adapt_results: list[dict],
    playbook_changes: list[dict],
    run_stamp: str,
    runs_dir,
) -> Optional[Path]:
    """Collect vote==-1 turns from this adapt batch into a manifest file.

    Returns the manifest path, or None when there are no down-votes."""
    feedback_root = Path(feedback_root)
    prefix = feedback_service._domain_prefix(namespace)
    entries: list[dict] = []

    for r in adapt_results or []:
        if r.get("status") != "ok":
            continue
        cid = r.get("conversation_id")
        tid = r.get("turn_id")
        if not cid or not tid:
            continue
        snap = _load_snapshot(feedback_root, prefix, cid)
        if snap is None:
            continue
        turn = next((t for t in snap.get("turns", [])
                     if t.get("turn_id") == tid), None)
        if turn is None:
            continue
        fb = turn.get("feedback") or {}
        if fb.get("vote") != -1:
            continue
        # Use schema-defined timestamps only, in priority order:
        # 1) feedback.details.ts  (detail-submit event time)
        # 2) feedback.ts          (vote event time)
        # 3) turn.ts              (turn creation time)
        # Stored as `feedback_ts` in the manifest as an internal derived field.
        details = fb.get("details") or {}
        feedback_ts = ""
        if isinstance(details, dict):
            feedback_ts = str(details.get("ts") or "")
        if not feedback_ts:
            feedback_ts = str(fb.get("ts") or "")
        if not feedback_ts:
            feedback_ts = str(turn.get("ts") or "")

        email = _norm_email(snap.get("submitted_by_email"))
        issue = snap.get("issue") or {}
        log_path = snap.get("log_path") or ""
        attached = _find_attached_log(feedback_root, prefix, cid, tid)
        entries.append({
            "namespace": namespace,
            "conversation_id": cid,
            "turn_id": tid,
            "submitted_by": r.get("submitted_by") or snap.get("submitted_by") or "",
            "submitted_by_email": email,
            "case_nbr": issue.get("case_nbr") or "",
            "log_path": log_path,
            "issue": issue,
            "user_message": turn.get("user_message") or "",
            "feedback_ts": feedback_ts,
            "feedback_detail": _feedback_detail_text(fb),
            "has_log": bool(attached),
            "attached_log_path": attached,
        })

    if not entries:
        print(f"[reverify] no down-votes in this {namespace} adapt batch")
        return None

    run_dir = Path(runs_dir) / run_stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "namespace": namespace,
        "stamp": run_stamp,
        "playbooks_dir": str(playbooks_dir),
        "feedback_root": str(feedback_root),
        "playbook_changes": playbook_changes or [],
        "entries": entries,
    }
    out = run_dir / f"downvotes_{run_stamp}_{namespace}.json"
    out.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[reverify] downvote manifest written: {out} ({len(entries)} entr(ies))")
    return out


def find_latest_manifests(runs_dir) -> list[Path]:
    """Newest downvote manifest per namespace under runs/."""
    runs_dir = Path(runs_dir)
    if not runs_dir.exists():
        return []
    latest: dict[str, Path] = {}
    for p in runs_dir.glob("*/downvotes_*_*.json"):
        m = re.match(r"^downvotes_.+_(wifi|bt)\.json$", p.name)
        if not m:
            continue
        ns = m.group(1)
        cur = latest.get(ns)
        if cur is None or p.stat().st_mtime > cur.stat().st_mtime:
            latest[ns] = p
    return list(latest.values())


# ---------------------------------------------------------------------------
# Re-answer + notify
# ---------------------------------------------------------------------------
_NS_CTX_CACHE: dict[str, tuple] = {}


def _ns_ctx(namespace: str) -> tuple:
    """Build (and cache) an (llm, AceRunner) for a namespace's updated
    playbook, with the same skills the live agent uses."""
    if namespace in _NS_CTX_CACHE:
        return _NS_CTX_CACHE[namespace]
    llm = ace_cli._build_llm(None)
    skills = ace_cli._load_active_skills(namespace)
    if skills:
        llm.skills = skills
        print(f"[reverify] {namespace}: {len(skills)} skill(s) loaded for replay")
    else:
        print(f"[reverify] WARNING: {namespace} skills unavailable — replay may "
              f"fail tool calls")
    playbooks_dir = ace_cli._resolve_playbooks_dir(namespace)
    feedback_root = ace_cli._resolve_feedback_root()
    ace_runner = AceRunner(
        llm=llm,
        playbooks_dir=playbooks_dir,
        feedback_root=feedback_root,
        skills=list((llm.skills or {}).keys()) or None,
        skill_context_provider=partial(ace_cli._skill_context_provider,
                                        namespace=namespace),
        feedback_prefix=ace_cli._feedback_prefix(namespace),
    )
    _NS_CTX_CACHE[namespace] = (llm, ace_runner)
    return _NS_CTX_CACHE[namespace]


def _final_payload(result) -> Optional[dict]:
    if not isinstance(result, dict):
        return None
    data = result.get("data")
    try:
        json.dumps(data)  # ensure JSON-safe for the embedded replay payload
    except Exception:
        data = str(data)
    return {"type": result.get("type") or "", "data": data}


def _build_section(entry: dict, *, max_steps: int) -> dict:
    domain = "bt" if entry.get("namespace") == "bt" else "wifi"
    section = {
        "title": _case_title(entry),
        "domain": domain,
        "feedback_ts": entry.get("feedback_ts") or "",
        "feedback_detail": entry.get("feedback_detail") or "",
        "reanswered": False,
        "note": "",
        "user_question": entry.get("user_message") or "",
        "steps": [],
        "final": None,
    }
    if not entry.get("has_log"):
        section["note"] = ("因無 log 無法重新驗證：此 case 的 feedback 未附上 log，"
                           "server 無法重新執行分析。")
        return section

    llm, ace_runner = _ns_ctx(entry["namespace"])
    case = {
        "log_path": entry.get("attached_log_path"),
        "issue_context": entry.get("issue") or {},
        "user_question": entry.get("user_message") or "",
    }
    cap = eval_runner.run_case_capture(llm, ace_runner, case, max_steps=max_steps)
    section["reanswered"] = True
    section["user_question"] = cap.get("user_question") or section["user_question"]
    section["steps"] = cap.get("steps") or []
    if cap.get("status") == "ok":
        section["final"] = _final_payload(cap.get("final_result"))
    else:
        section["steps"].append({
            "role": "error",
            "content": f"❌ Re-answer failed: {cap.get('error', 'unknown error')}",
        })
    return section


def reverify_and_notify(
    *,
    manifests: list,
    runs_dir,
    run_stamp: str,
    dry_run: bool = False,
    max_steps: int = 6,
) -> dict:
    """Re-answer down-voted cases and email each down-voter one combined email.

    manifests: list of manifest dicts OR paths to downvotes_*.json.
    """
    loaded: list[dict] = []
    for m in manifests or []:
        if isinstance(m, dict):
            loaded.append(m)
            continue
        p = Path(m)
        if not p.exists():
            print(f"[reverify] manifest not found: {p}")
            continue
        try:
            loaded.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"[reverify] manifest read failed {p}: {e}")

    # namespace -> bullet before/after changes (for the email body).
    ns_changes: dict[str, list] = {}
    for man in loaded:
        ns = man.get("namespace") or "wifi"
        ns_changes.setdefault(ns, [])
        ns_changes[ns].extend(man.get("playbook_changes") or [])

    # Group entries by recipient email (across namespaces). When a redirect
    # override is active (testing), entries whose snapshot predates the
    # submitted_by_email field are still processed under their submitter name
    # since the mail goes to the override inbox anyway.
    people: dict[str, dict] = {}
    skipped_no_email = 0
    for man in loaded:
        for entry in man.get("entries") or []:
            email = _norm_email(entry.get("submitted_by_email"))
            if email:
                key = email
                recipient = email
                display = email
            elif REVERIFY_REDIRECT_TO:
                display = (entry.get("submitted_by") or "unknown").strip() or "unknown"
                key = f"noemail:{display}"
                recipient = None
            else:
                skipped_no_email += 1
                print(f"[reverify] skip (no email): conv={entry.get('conversation_id')} "
                      f"case={entry.get('case_nbr')}")
                continue
            bundle = people.setdefault(
                key, {"sections": [], "namespaces": set(),
                      "display": display, "recipient": recipient})
            section = _build_section(entry, max_steps=max_steps)
            bundle["sections"].append(section)
            bundle["namespaces"].add(entry.get("namespace") or "wifi")

    out_dir = Path(runs_dir) / run_stamp / "reverify"
    out_dir.mkdir(parents=True, exist_ok=True)

    sent = 0
    written = 0
    for key, bundle in people.items():
        sections = bundle["sections"]
        display = bundle["display"]
        replay_sections = [s for s in sections if s.get("reanswered")]
        no_log_case_count = sum(1 for s in sections if not s.get("reanswered"))
        cases = [{"title": s["title"], "domain": s["domain"],
                  "reanswered": s["reanswered"], "attachment_name": None}
                 for s in sections]
        has_attachment = bool(replay_sections)
        safe = _safe_email_filename(display)
        attachment_names: list[str] = []
        attachments = None
        if has_attachment:
            attachments = []
            used_names: set[str] = set()
            case_idx = 0
            for i, section in enumerate(sections):
                if not section.get("reanswered"):
                    continue
                case_idx += 1
                html = replay_html.render_replay_html(
                    person_email=display,
                    sections=[section],
                )
                ts_suffix = _feedback_ts_suffix(str(section.get("feedback_ts") or ""))
                stem = _safe_filename_stem(str(section.get("title") or ""), fallback=f"case_{case_idx}")
                base_name = f"agent_reanswer_{safe}_{stem}_{ts_suffix}.html"
                attachment_name = base_name
                if attachment_name in used_names:
                    attachment_name = (
                        f"agent_reanswer_{safe}_{stem}_{ts_suffix}_{case_idx}.html"
                    )
                used_names.add(attachment_name)

                html_path = out_dir / attachment_name
                html_path.write_text(html, encoding="utf-8")
                written += 1
                attachments.append((attachment_name, html.encode("utf-8"), "html"))
                attachment_names.append(attachment_name)
                cases[i]["attachment_name"] = attachment_name

        namespace_changes = [
            {"namespace": ns, "changes": ns_changes.get(ns, [])}
            for ns in sorted(bundle["namespaces"])
        ]
        body = replay_html.render_user_email(
            person_email=display,
            cases=cases,
            namespace_changes=namespace_changes,
            attachment_names=attachment_names,
            has_attachment=has_attachment,
            no_log_case_count=no_log_case_count,
        )
        subject = f"Avatar Feedback Reverify - {datetime.now().strftime('%Y-%m-%d')} ({len(sections)} case)"
        to_list = (list(REVERIFY_REDIRECT_TO) if REVERIFY_REDIRECT_TO
                   else ([bundle["recipient"]] if bundle["recipient"] else []))
        if not to_list:
            print(f"[reverify] skip send (no recipient): {display}")
            continue

        if dry_run:
            if has_attachment:
                print(f"[reverify] DRY-RUN would email {to_list} — {len(sections)} case(s); "
                      f"attachments: {len(attachment_names)}")
            else:
                print(f"[reverify] DRY-RUN would email {to_list} — {len(sections)} case(s); "
                      "no replay attachment (no attached logs).")
            continue
        try:
            notify_email.send_html_to(
                to_list=to_list,
                subject=subject,
                html_body=body,
                attachments=attachments,
            )
            sent += 1
            if has_attachment:
                print(f"[reverify] emailed {to_list} — {len(sections)} case(s) with replay attachment")
            else:
                print(f"[reverify] emailed {to_list} — {len(sections)} case(s), no replay attachment")
        except Exception as e:
            print(f"[reverify] email FAILED for {to_list}: {e}")

    summary = {
        "people": len(people),
        "emails_sent": sent,
        "html_written": written,
        "skipped_no_email": skipped_no_email,
        "dry_run": dry_run,
        "out_dir": str(out_dir),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return summary
