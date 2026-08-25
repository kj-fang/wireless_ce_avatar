"""Render sample versions of the ACE notification emails for visual review.

Run this file directly (or double-click it via a .pyw/batch wrapper) from the
repo root to render both the manager review-triage email and the per-user
reverify email with mock data, then open them in the default browser:

    python services/ace/eval/preview_emails.py
"""

from __future__ import annotations

import sys
import webbrowser
from pathlib import Path

# Allow running this file directly (python services/ace/eval/preview_emails.py)
# without needing to invoke it as a module.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.ace.eval import email_templates, replay_html

_OUT_DIR = Path(__file__).resolve().parent / "runs" / "preview"


def _sample_review_triage_email() -> tuple[str, str]:
    review_report = {
        "gate_verdict": "FAIL",
        "ts_utc": "2026-08-25T02:15:00Z",
        "_feedback_submitter_rows": [
            {"submitter": "alice@example.com", "count": 3},
            {"submitter": "bob@example.com", "count": 1},
        ],
        "_feedback_submitter_total": 4,
        "_playbook_changes": [
            {"bullet_id": "B-101", "change_type": "added", "playbook_label": "wifi_roaming.json"},
            {"bullet_id": "B-102", "change_type": "updated", "playbook_label": "wifi_scan.json"},
            {"bullet_id": "B-103", "change_type": "removed", "playbook_label": "wifi_legacy.json"},
        ],
    }
    triage_report = {
        "review": "auto-triage",
        "results": [
            {"bullet_id": "B-201", "playbook_file": "wifi_roaming.json", "action": "reverted"},
            {"bullet_id": "B-202", "playbook_file": "wifi_scan.json", "action": "removed"},
            {"bullet_id": "B-203", "playbook_file": "wifi_legacy.json", "action": "kept_no_previous"},
        ],
    }
    killer_report = {
        "results": [
            {
                "bullet_id": "B-201",
                "verdict": {
                    "status": "confirmed",
                    "main_suspect": {"submitted_by": "alice@example.com", "confidence": 0.92},
                },
            },
        ],
    }
    return email_templates.build_review_triage_email(
        namespace="wifi",
        review_report=review_report,
        triage_report=triage_report,
        killer_report=killer_report,
    )


def _sample_reverify_email() -> str:
    long_summary = (
        "workflow: roaming_analysis  \n correct conclusion: driver_issue  \n "
        "The bot missed the actual root cause and blamed AP config instead of the driver."
    )
    return replay_html.render_user_email(
        person_email="jane.doe@example.com",
        cases=[
            {
                "title": "Local upload (very_long_original_filename_from_customer_upload_2026.zip) "
                         "[2026-08-20 10:00:00]",
                "title_full": r"C:\Users\jane.doe\Downloads\case_uploads\very_long_original_filename"
                              r"_from_customer_upload_2026.zip",
                "domain": "wifi",
                "reanswered": True,
                "attachment_name": "agent_reanswer_jane_doe_case_12345_20260825.html",
                "feedback_detail": long_summary,
            },
            {
                "title": "Case #67890 - BT audio glitch",
                "title_full": "",
                "domain": "bt",
                "reanswered": False,
                "attachment_name": None,
                "feedback_detail": "",
            },
        ],
        namespace_changes=[],
        attachment_names=["agent_reanswer_jane_doe_case_12345_20260825.html"],
        has_attachment=True,
        no_log_case_count=1,
        generated_at="2026-08-25 10:30:00",
    )


def build_preview() -> Path:
    """Render both sample emails into one combined HTML file and return its path."""
    _, review_html = _sample_review_triage_email()
    reverify_html = _sample_reverify_email()

    combined = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>ACE Email Previews</title></head>
<body style="margin:0;padding:24px;background:#eef1f5;font-family:Segoe UI,Arial,sans-serif;">
  <h1 style="font-size:18px;color:#1f2937;">1. Review Triage Email (manager notification)</h1>
  <div style="background:#fff;border:1px solid #d9e2ec;border-radius:8px;padding:16px;margin-bottom:32px;">
    {review_html}
  </div>

  <h1 style="font-size:18px;color:#1f2937;">2. Reverify User Email (per feedback submitter)</h1>
  <div style="background:#fff;border:1px solid #d9e2ec;border-radius:8px;padding:16px;">
    {reverify_html}
  </div>
</body>
</html>
"""
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _OUT_DIR / "email_previews.html"
    out_path.write_text(combined, encoding="utf-8")
    return out_path


if __name__ == "__main__":
    path = build_preview()
    print(f"Preview written to: {path}")
    webbrowser.open(path.as_uri())
