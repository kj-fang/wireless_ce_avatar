"""
SMTP notification helpers for ACE eval/review/triage pipeline.

Designed for server-side, non-interactive runs where Outlook COM is not
available. Defaults match the validated Intel relay setup:
  host=smtp.intel.com, port=25, no auth, no TLS.

Environment variables:
    (Recipients are configured by module globals below, not env vars.)
  ACE_SMTP_FROM         Optional; defaults to current UPN or no-reply value.
  ACE_SMTP_HOST         Optional; default smtp.intel.com.
  ACE_SMTP_PORT         Optional; default 25.
  ACE_SMTP_USE_TLS      Optional; true/false, default false.
  ACE_SMTP_USERNAME     Optional SMTP auth username.
  ACE_SMTP_PASSWORD     Optional SMTP auth password.
"""

from __future__ import annotations

import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape


# --- recipient configuration (edit these directly) -------------------------
# Preferred format: direct list[str].
# Example:
# ACE_NOTIFY_TO = ["alice@intel.com", "bob@intel.com"]
# ACE_NOTIFY_CC = ["team@intel.com"]
ACE_NOTIFY_TO: list[str] = ["wei-ling.chi@intel.com"]
ACE_NOTIFY_CC: list[str] = []


def _parse_recipients(raw: str | None) -> list[str]:
    if not raw:
        return []
    text = raw.replace(";", ",")
    return [p.strip() for p in text.split(",") if p.strip()]


def _clean_recipients(items: list[str] | None) -> list[str]:
    if not items:
        return []
    return [str(p).strip() for p in items if str(p).strip()]


def _env_bool(name: str, default: bool = False) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "y", "on")


def _default_sender() -> str:
    # Keep this dependency local; eval package is also used headless.
    try:
        from utils import helpers
        upn = (helpers.detect_user_email() or "").strip()
        if upn and "@" in upn:
            return upn
    except Exception:
        pass
    return "intelavatar-no-reply@intel.com"


def smtp_send_html(
    *,
    subject: str,
    html_body: str,
    to_list: list[str],
    cc_list: list[str] | None = None,
    smtp_host: str = "smtp.intel.com",
    smtp_port: int = 25,
    sender: str | None = None,
    use_tls: bool = False,
    username: str | None = None,
    password: str | None = None,
    timeout_sec: int = 20,
) -> None:
    """Send one HTML email via SMTP relay/auth SMTP."""
    if not to_list:
        raise ValueError("to_list is empty")

    cc = cc_list or []
    sender_addr = sender or _default_sender()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender_addr
    msg["To"] = "; ".join(to_list)
    if cc:
        msg["Cc"] = "; ".join(cc)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    recipients = list(to_list) + list(cc)

    with smtplib.SMTP(smtp_host, int(smtp_port), timeout=timeout_sec) as client:
        if use_tls:
            client.starttls()
        if username:
            client.login(username, password or "")
        client.sendmail(sender_addr, recipients, msg.as_string())


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

    triage_results = (triage_report or {}).get("results") or []
    triage_tally = _tally_actions(triage_results)
    triage_rows = "".join(
        f"<tr><td>{escape(k)}</td><td>{v}</td></tr>"
        for k, v in sorted(triage_tally.items())
    ) or "<tr><td>(none)</td><td>0</td></tr>"

    subject = (
        f"[ACE {namespace}] Review {verdict} - "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    html = f"""
<html><body>
  <p><strong>ACE pipeline notification</strong></p>
  <p>Namespace: <strong>{escape(namespace)}</strong></p>
  <p>Review verdict: <strong>{escape(verdict)}</strong></p>
  <p>Review timestamp (UTC): <strong>{escape(review_ts)}</strong></p>

  <hr>
  <p><strong>Corrupted-bullet triage summary</strong></p>
  <table border="1" cellspacing="0" cellpadding="6">
    <tr><th>Action</th><th>Count</th></tr>
    {triage_rows}
  </table>

  <p style="margin-top:14px;"><strong>Per-bullet actions</strong></p>
  <ul>
    {''.join(f'<li>{escape(str(r.get("bullet_id") or "?"))}: {escape(str(r.get("action") or "unknown"))}</li>' for r in triage_results) or '<li>(none)</li>'}
  </ul>

  <hr>
  <p>This is an automatically generated email from ACE eval pipeline.</p>
</body></html>
"""
    return subject, html


def _resolve_smtp_settings(
    *,
    to_override: str | None = None,
    cc_override: str | None = None,
) -> dict:
    """Resolve recipients + SMTP settings.

    Recipients come from module globals unless one-shot overrides are passed.
    SMTP transport settings remain env-driven.
    """
    to_list = (_parse_recipients(to_override)
               if to_override is not None
               else _clean_recipients(ACE_NOTIFY_TO))
    cc_list = (_parse_recipients(cc_override)
               if cc_override is not None
               else _clean_recipients(ACE_NOTIFY_CC))
    smtp_host = (os.getenv("ACE_SMTP_HOST") or "smtp.intel.com").strip()
    smtp_port = int((os.getenv("ACE_SMTP_PORT") or "25").strip())
    sender = (os.getenv("ACE_SMTP_FROM") or "").strip() or None
    use_tls = _env_bool("ACE_SMTP_USE_TLS", default=False)
    username = (os.getenv("ACE_SMTP_USERNAME") or "").strip() or None
    password = os.getenv("ACE_SMTP_PASSWORD")
    return {
        "to_list": to_list,
        "cc_list": cc_list,
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "sender": sender,
        "use_tls": use_tls,
        "username": username,
        "password": password,
    }


def send_test_from_env(
    *,
    namespace: str = "wifi",
    note: str = "",
    to_override: str | None = None,
    cc_override: str | None = None,
) -> bool:
    """
    Send a standalone SMTP test email using ACE_* environment variables.

    Returns True when sent, False when ACE_NOTIFY_TO is missing.
    """
    cfg = _resolve_smtp_settings(to_override=to_override, cc_override=cc_override)
    to_list = cfg["to_list"]
    if not to_list:
        return False

    sender = cfg["sender"] or _default_sender()
    tls_label = "on" if cfg["use_tls"] else "off"
    auth_label = "on" if cfg["username"] else "off"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    subject = f"[ACE {namespace}] SMTP notify test - {now}"
    html = f"""
<html><body>
  <p><strong>ACE SMTP test email</strong></p>
  <p>Namespace: <strong>{escape(namespace)}</strong></p>
  <p>Timestamp: <strong>{escape(now)}</strong></p>
  <hr>
  <p>SMTP host: <strong>{escape(str(cfg['smtp_host']))}:{cfg['smtp_port']}</strong></p>
  <p>TLS: <strong>{tls_label}</strong></p>
  <p>Auth: <strong>{auth_label}</strong></p>
  <p>Sender: <strong>{escape(sender)}</strong></p>
  <p>To: <strong>{escape('; '.join(to_list))}</strong></p>
  <p>CC: <strong>{escape('; '.join(cfg['cc_list'])) if cfg['cc_list'] else '(none)'}</strong></p>
  <p>Note: <strong>{escape(note) if note else '(none)'}</strong></p>
</body></html>
"""
    smtp_send_html(
        subject=subject,
        html_body=html,
        to_list=to_list,
        cc_list=cfg["cc_list"],
        smtp_host=cfg["smtp_host"],
        smtp_port=cfg["smtp_port"],
        sender=cfg["sender"],
        use_tls=cfg["use_tls"],
        username=cfg["username"],
        password=cfg["password"],
    )
    return True


def notify_from_env(*, namespace: str, review_report: dict, triage_report: dict | None) -> bool:
    """
    Send review/triage notification using env var configuration.

    Returns True when email was sent, False when skipped due to missing
    recipient configuration.
    """
    cfg = _resolve_smtp_settings()
    to_list = cfg["to_list"]
    if not to_list:
        return False

    subject, html = build_review_triage_email(
        namespace=namespace,
        review_report=review_report,
        triage_report=triage_report,
    )
    smtp_send_html(
        subject=subject,
        html_body=html,
        to_list=to_list,
        cc_list=cfg["cc_list"],
        smtp_host=cfg["smtp_host"],
        smtp_port=cfg["smtp_port"],
        sender=cfg["sender"],
        use_tls=cfg["use_tls"],
        username=cfg["username"],
        password=cfg["password"],
    )
    return True
