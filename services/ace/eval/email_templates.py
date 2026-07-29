"""HTML/email template builders for ACE notifications."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_JINJA_ENV = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(enabled_extensions=("html", "xml"), default=True),
)
_REVIEW_TRIAGE_TEMPLATE = _JINJA_ENV.get_template("review_triage_email.html")


def _tally_actions(results: list[dict]) -> dict[str, int]:
    tally: dict[str, int] = {}
    for row in results:
        action = str(row.get("action") or "unknown")
        tally[action] = tally.get(action, 0) + 1
    return tally


def build_review_triage_email(
    *,
    namespace: str,
    review_report: dict,
    triage_report: dict | None,
) -> tuple[str, str]:
    """Build (subject, html) for manager notification."""
    verdict = str(review_report.get("gate_verdict") or "UNKNOWN")
    review_ts = str(review_report.get("ts_utc") or "")
    has_triage = triage_report is not None

    triage_results = (triage_report or {}).get("results") or []
    triage_tally = _tally_actions(triage_results)

    total_count = len(triage_results)
    reverted_count = triage_tally.get("reverted", 0)
    removed_count = triage_tally.get("removed", 0)
    kept_count = (
        triage_tally.get("kept_corrupted", 0)
        + triage_tally.get("kept_no_previous", 0)
    )
    failed_count = (
        triage_tally.get("revert_failed", 0)
        + triage_tally.get("remove_failed", 0)
    )

    verdict_upper = verdict.upper()
    if verdict_upper == "PASS":
        badge_bg = "#e8f7ef"
        badge_fg = "#176b3a"
        verdict_hint = "No regression detected by review gate."
    elif verdict_upper == "FAIL":
        badge_bg = "#fdeaea"
        badge_fg = "#9e1c1c"
        verdict_hint = "Regression detected. Triage actions were applied."
    else:
        badge_bg = "#f2f4f8"
        badge_fg = "#374151"
        verdict_hint = "Review verdict was not recognized."

    action_rows = [
        {"action": k, "count": v}
        for k, v in sorted(triage_tally.items())
    ]
    if not action_rows:
        action_rows = [{"action": "(none)", "count": 0}]

    detail_rows = [
        {
            "bullet_id": str(r.get("bullet_id") or "?"),
            "playbook_file": str(r.get("playbook_file") or "-"),
            "action": str(r.get("action") or "unknown"),
        }
        for r in triage_results
    ]
    if not detail_rows:
        detail_rows = [{"bullet_id": "(none)", "playbook_file": "-", "action": "-"}]

    changed_bullet_rows = []
    for row in (review_report.get("_playbook_changes") or []):
        changed_bullet_rows.append({
            "bullet_id": str(row.get("bullet_id") or "?"),
            "change_type": str(row.get("change_type") or "updated"),
            "playbook_label": str(row.get("playbook_label") or "-"),
            "before_text": str(row.get("before_text") or "(none)"),
            "after_text": str(row.get("after_text") or "(none)"),
        })
    if not changed_bullet_rows:
        changed_bullet_rows = [{
            "bullet_id": "(none)",
            "change_type": "none",
            "playbook_label": "-",
            "before_text": "(none)",
            "after_text": "(none)",
        }]

    feedback_submitters = [
        str(s).strip()
        for s in (review_report.get("_feedback_submitters") or [])
        if str(s).strip()
    ]
    feedback_submitter_count = len(feedback_submitters)

    subject = (
        f"[ACE {namespace}] Review {verdict_upper} | "
        f"{datetime.now().strftime('%Y-%m-%d')}"
    )

    html = _REVIEW_TRIAGE_TEMPLATE.render(
        namespace=namespace,
        verdict_upper=verdict_upper,
        review_ts=review_ts,
        badge_bg=badge_bg,
        badge_fg=badge_fg,
        verdict_hint=verdict_hint,
        total_count=total_count,
        reverted_count=reverted_count,
        removed_count=removed_count,
        kept_count=kept_count,
        failed_count=failed_count,
        review_source=str((triage_report or {}).get("review") or "-"),
        action_rows=action_rows,
        detail_rows=detail_rows,
        has_triage=has_triage,
        changed_bullet_rows=changed_bullet_rows,
        feedback_submitters=feedback_submitters,
        feedback_submitter_count=feedback_submitter_count,
    )
    return subject, html
