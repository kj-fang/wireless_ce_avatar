"""
Email Notify Service
====================
SMTP transport for the Report Ingestion Endpoint's completion emails. 
Sends HTML email directly via smtplib - no local mail client involved.

Recipient resolution follows the `email_list.json` convention documented in
data/CONTEXT.md: an optional file bundled inside the uploaded archive. If
absent or unparsable, NO email is sent (there is no default-recipient 
fallback here - see data/CONTEXT.md's email_list.json entry).
"""

import json
import os
import smtplib
from datetime import datetime
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


def _build_analysis_section_html(result: dict | None) -> str:
    """Build the 'IntelAvatar Analysis' HTML block, mirroring LabHerald's
    email_draft.py::_build_analysis_section_html but reading from the
    in-memory job result dict instead of an on-disk llm_report JSON.
    """
    data = (result or {}).get('data')
    if not isinstance(data, dict):
        return ''

    html = '<hr><p><strong>IntelAvatar Analysis</strong></p>'
    html += f"<p><strong>Root Cause</strong>: {data.get('root_cause_summary', 'N/A')}</p>"
    confidence = data.get('confidence_score')
    if confidence is not None:
        html += f'<p><strong>Confidence</strong>: {confidence}%</p>'
    actions = data.get('recommended_actions') or []
    if actions:
        html += '<p><strong>Recommended Actions:</strong><ul>'
        html += ''.join(f'<li>{action}</li>' for action in actions)
        html += '</ul></p>'
    html += '<hr>'
    return html


def _build_html_body(source_filename: str, job_id: str, timestamp: str, status: str,
                      result: dict | None, error_message: str | None) -> str:
    """Build the completion email body, adapted from LabHerald's
    email_draft.py::_build_html_body (no archived-folder link - this system
    deletes uploads immediately instead of moving them to a Completed dir).
    """
    if status == 'done':
        intro = f'<p>The Agent Admin Robot completed analysis at <strong>{timestamp}</strong> for:</p>'
        status_html = _build_analysis_section_html(result)
    else:
        intro = f'<p>The Agent Admin Robot FAILED to analyze the following at <strong>{timestamp}</strong>:</p>'
        status_html = f"<p><strong>Error</strong>: {error_message or 'unknown error'}</p>"

    return f"""\
            <html><body>
            <p>Hi,</p>
            {intro}
            <ul><li>{source_filename}</li></ul>
            <p>Job ID: <code>{job_id}</code></p>
            {status_html}
            <p>Please review the IntelAvatar submissions in shared folder if needed.</p>
            <p>Best regards,<br>Agent Admin Robot</p>
            </body></html>
        """


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

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if status == 'done':
        subject = f'[Agent Admin Robot] Processed Archive: {source_filename} \u2013 {now}'
    else:
        subject = f'[Agent Admin Robot] Analysis FAILED: {source_filename} \u2013 {now}'
    body = _build_html_body(source_filename, job_id, now, status, result, error_message)

    try:
        send_html_to(to_list=to_list, subject=subject, html_body=body, cc_list=cc_list)
        return True
    except Exception as exc:
        print(f'⚠️ [email_notify_service] send failed for job {job_id}: {exc}')
        return False
