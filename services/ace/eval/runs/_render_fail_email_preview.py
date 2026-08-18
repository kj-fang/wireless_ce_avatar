from pathlib import Path
from services.ace.eval.email_templates import build_review_triage_email

review_report = {
    "gate_verdict": "FAIL",
    "ts_utc": "2026-08-12T10:20:30+00:00",
    "_feedback_submitter_rows": [
        {"submitter": "alice", "count": 3},
        {"submitter": "bob", "count": 1},
    ],
    "_feedback_submitter_total": 4,
    "_playbook_changes": [
        {
            "bullet_id": "B-1001",
            "change_type": "updated",
            "playbook_label": "domain_connectivity.json",
            "before_text": "Old bullet content",
            "after_text": "New bullet content",
        }
    ],
}

triage_report = {
    "review": "review_20260812T102030+0000.json",
    "results": [
        {"bullet_id": "B-1001", "playbook_file": "domain_connectivity.json", "action": "reverted"},
        {"bullet_id": "B-2001", "playbook_file": "workflow.json", "action": "kept_corrupted"},
    ],
}

killer_report = {
    "results": [
        {
            "bullet_id": "B-1001",
            "verdict": {
                "status": "ok",
                "main_suspect": {"submitted_by": "alice", "confidence": 0.93},
            },
        },
        {
            "bullet_id": "B-2001",
            "verdict": {
                "status": "ok_low_confidence",
                "main_suspect": {"submitted_by": "bob", "confidence": 0.51},
            },
        },
    ]
}

subject, html = build_review_triage_email(
    namespace="wifi",
    review_report=review_report,
    triage_report=triage_report,
    killer_report=killer_report,
)

out = Path("services/ace/eval/runs/_sample_fail_email.html")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(html, encoding="utf-8")
print(subject)
print(out.resolve())
