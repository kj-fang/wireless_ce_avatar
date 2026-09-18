"""
Email Notify Service
====================
SMTP transport for the Report Ingestion Endpoint's completion emails, ported
from LabHerald Agent Bridge's notify_email.py
(C:\\Python\\Wireless_AI_Agent_Eco-System\\LabHerald-Agent-Bridge), which
already implements this same pattern for a sibling automation. Sends HTML
email directly via smtplib - no local mail client involved.

Recipient resolution follows the `email_list.json` convention documented in
data/CONTEXT.md: an optional file bundled inside the uploaded archive. If
absent or unparsable, NO email is sent (unlike LabHerald, there is no
default-recipient fallback here - see data/CONTEXT.md's email_list.json
entry).
"""

import json
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from avatar_mcp import ingestion_config as cfg

EMAIL_LIST_FILENAME = 'email_list.json'


def find_email_list(root_dir: str) -> str | None:
    """Search `root_dir` (the extracted archive contents) for email_list.json."""
    for dirpath, _dirnames, filenames in os.walk(root_dir):
        if EMAIL_LIST_FILENAME in filenames:
            return os.path.join(dirpath, EMAIL_LIST_FILENAME)
    return None


def resolve_recipients(email_list_path: str) -> tuple[list[str], list[str]]:
    """Read email_list.json's `recipients`/`cc`. Returns ([], []) if missing/invalid."""
    try:
        with open(email_list_path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return [], []

    to_list = data.get('recipients')
    to_list = to_list if isinstance(to_list, list) and to_list else []
    cc_list = data.get('cc')
    cc_list = cc_list if isinstance(cc_list, list) and cc_list else []
    return to_list, cc_list


def send_html_to(*, to_list: list[str], subject: str, html_body: str, cc_list: list[str] | None = None) -> None:
    """Send an HTML email via the configured SMTP relay.

    Raises ValueError if to_list is empty, or the underlying smtplib
    exception if the send fails. Callers are expected to catch and log.
    """
    if not to_list:
        raise ValueError('to_list is empty - refusing to send an email with no recipients')

    cc = cc_list or []
    recipients = list(to_list) + list(cc)

    msg = MIMEMultipart('alternative')
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))
    msg['Subject'] = subject
    msg['From'] = cfg.EMAIL_FROM
    msg['To'] = '; '.join(to_list)
    if cc:
        msg['Cc'] = '; '.join(cc)

    if cfg.EMAIL_DRY_RUN:
        print(f"[DRY RUN] Would send via {cfg.SMTP_HOST}:{cfg.SMTP_PORT} from={cfg.EMAIL_FROM} "
              f"to={to_list} cc={cc} subject={subject!r}")
        return

    with smtplib.SMTP(cfg.SMTP_HOST, cfg.SMTP_PORT, timeout=20) as client:
        if cfg.SMTP_USE_TLS:
            client.starttls()
        if cfg.SMTP_USERNAME:
            client.login(cfg.SMTP_USERNAME, cfg.SMTP_PASSWORD or '')
        client.sendmail(cfg.EMAIL_FROM, recipients, msg.as_string())

    print(f"Email sent via SMTP to: {', '.join(to_list)}" + (f" (cc: {', '.join(cc)})" if cc else ''))


def send_job_completion_email(*, extracted_dir: str, job_id: str, source_filename: str,
                               status: str, result: dict | None, error_message: str | None) -> bool:
    """Look for email_list.json under `extracted_dir`; if found with valid
    recipients, send a completion notification (success or failure).

    Returns True if an email was sent, False if skipped (no email_list.json
    or no valid recipients).
    """
    email_list_path = find_email_list(extracted_dir)
    if not email_list_path:
        return False

    to_list, cc_list = resolve_recipients(email_list_path)
    if not to_list:
        return False

    if status == 'done':
        subject = f'[Avatar Report Ingestion] Analysis complete: {source_filename}'
        root_cause = (result or {}).get('data', {}).get('root_cause_summary', 'N/A') \
            if isinstance((result or {}).get('data'), dict) else 'N/A'
        body = (
            f"<html><body>"
            f"<p>Job <code>{job_id}</code> for <strong>{source_filename}</strong> completed.</p>"
            f"<p><strong>Root cause summary:</strong> {root_cause}</p>"
            f"</body></html>"
        )
    else:
        subject = f'[Avatar Report Ingestion] Analysis FAILED: {source_filename}'
        body = (
            f"<html><body>"
            f"<p>Job <code>{job_id}</code> for <strong>{source_filename}</strong> failed.</p>"
            f"<p><strong>Error:</strong> {error_message or 'unknown error'}</p>"
            f"</body></html>"
        )

    try:
        send_html_to(to_list=to_list, subject=subject, html_body=body, cc_list=cc_list)
        return True
    except Exception as exc:
        print(f'⚠️ [email_notify_service] send failed for job {job_id}: {exc}')
        return False
