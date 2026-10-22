"""
Outlook intake for Beacon (the handsfree replyer): mail that names an IPS
case goes through the case pipeline, and the reviewed reply is saved as an
Outlook DRAFT (never sent).

Local Outlook desktop over COM (pywin32, pinned in requirements.txt). All
COM access lives in this module; the import is lazy so the package — and
the smoke test, which injects a fake application — load without Outlook.

Gotchas (2026-10-22):
- COM is apartment-threaded: every thread that touches Outlook calls
  CoInitialize first (the scheduler and the route workers are threads).
- Outlook must be running and signed in to a profile; otherwise Dispatch
  starts it headless and MAPI may fail — reported as OutlookUnavailable.
- Reading addresses / creating replies can trip Outlook's programmatic-
  access guard when the antivirus status is "invalid"; that surfaces as a
  COM error here, also OutlookUnavailable.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

OL_FOLDER_INBOX = 6
OL_MAIL_ITEM = 43
STATE_FILE = "email_state.json"
MAX_BODY_CHARS = 20000          # the case number is near the top anyway

_LOCK = threading.Lock()


class OutlookUnavailable(RuntimeError):
    """Outlook could not be reached or refused the operation; the message
    is meant for the reviewer (shown on the page)."""


@dataclass
class EmailRef:
    entry_id: str
    store_id: str
    subject: str
    sender_name: str
    sender_email: str
    received: str                      # ISO, local clock
    conversation_id: str = ""
    case_nbrs: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- case nbrs

# "case 01031658", "Case#1031658", "IPS 01031658", "IPS-01031658", "#01031658"
_CASE_RE = re.compile(r"(?i)(?:\bcase\b|\bips\b|#)\s*[:#\-–]?\s*(\d{6,8})\b")
# bare eight-digit IPS numbers (they start with 0) anywhere in the text
_BARE_RE = re.compile(r"(?<!\d)0\d{7}(?!\d)")


def _normalise(raw: str) -> str:
    try:
        from utils.check_ips_utils import normalise_ips
        return normalise_ips(raw)
    except Exception:
        digits = re.sub(r"\D", "", str(raw or ""))
        return digits.zfill(8) if 6 <= len(digits) <= 8 else ""


def extract_case_numbers(subject: str, body: str = "") -> list[str]:
    """IPS case numbers named in the mail, subject first, in order of
    appearance, normalised to eight digits, duplicates removed."""
    out: list[str] = []
    for text in (subject or "", (body or "")[:MAX_BODY_CHARS]):
        hits = [(m.start(), m.group(1)) for m in _CASE_RE.finditer(text)]
        hits += [(m.start(), m.group(0)) for m in _BARE_RE.finditer(text)]
        for _pos, raw in sorted(hits):
            nbr = _normalise(raw)
            if nbr and nbr not in out:
                out.append(nbr)
    return out


# ------------------------------------------------------------- scan state

def state_path(root) -> Path:
    return Path(root) / STATE_FILE


def load_state(root) -> dict:
    try:
        data = json.loads(state_path(root).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("processed", {})
            data.setdefault("last_scan_at", "")
            return data
    except Exception:
        pass
    return {"last_scan_at": "", "processed": {}}


def save_state(root, state: dict) -> None:
    with _LOCK:
        p = state_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, p)


def mark_processed(root, entry_id: str, case_nbr: str = "", draft_id: str = "") -> None:
    """Remember that this mail was handled (per case it named); a mail with
    no case number is remembered too so it is not re-listed every round."""
    state = load_state(root)
    entry = state["processed"].setdefault(str(entry_id), {"case_nbrs": [], "draft_ids": []})
    if case_nbr and case_nbr not in entry["case_nbrs"]:
        entry["case_nbrs"].append(case_nbr)
    if draft_id:
        entry["draft_ids"].append(draft_id)
    entry["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_state(root, state)


def is_processed(state: dict, entry_id: str) -> bool:
    return str(entry_id) in (state.get("processed") or {})


# ------------------------------------------------------------------- COM

def _com():
    try:
        import pythoncom                       # noqa: F401
        import win32com.client                 # noqa: F401
    except ImportError as e:                   # pragma: no cover
        raise OutlookUnavailable("pywin32 is not installed in this Python "
                                 "(pip install pywin32)") from e
    import pythoncom
    import win32com.client
    return pythoncom, win32com.client


def _to_datetime(value) -> Optional[datetime]:
    """pywintypes datetime / datetime / string -> naive local datetime."""
    if value is None:
        return None
    try:
        return datetime(value.year, value.month, value.day,
                        value.hour, value.minute, value.second)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def _sender_email(item) -> str:
    """SMTP address of the sender; Exchange items hide it behind the
    legacy EX address, so ask the Exchange user object first."""
    try:
        if str(getattr(item, "SenderEmailType", "") or "").upper() == "EX":
            sender = item.Sender
            if sender is not None:
                exch = sender.GetExchangeUser()
                if exch is not None and getattr(exch, "PrimarySmtpAddress", ""):
                    return str(exch.PrimarySmtpAddress)
    except Exception:
        pass
    try:
        return str(getattr(item, "SenderEmailAddress", "") or "")
    except Exception:
        return ""


def _restrict_stamp(since: datetime) -> str:
    # Outlook's Restrict parses the date in the user's locale; the US form
    # with a 12-hour clock is the one every locale accepts in practice.
    return since.strftime("%m/%d/%Y %I:%M %p")


class Outlook:
    """One Outlook session; use as a context manager inside the thread that
    does the work. `factory` (class attribute) lets the smoke inject a fake
    application object instead of COM."""

    factory = None          # callable -> application object, or None (= COM)

    def __init__(self):
        self._app = None
        self._ns = None
        self._com_inited = False

    def __enter__(self) -> "Outlook":
        factory = type(self).factory      # off the class: never bound to self
        if factory is not None:
            self._app = factory()
        else:
            pythoncom, client = _com()
            pythoncom.CoInitialize()
            self._com_inited = True
            try:
                self._app = client.Dispatch("Outlook.Application")
            except Exception as e:
                raise OutlookUnavailable(
                    f"Outlook is not available on this PC ({type(e).__name__}: {e})") from e
        try:
            self._ns = self._app.GetNamespace("MAPI")
        except Exception as e:
            raise OutlookUnavailable(
                "Outlook's MAPI session could not be opened — is Outlook running "
                f"and signed in? ({type(e).__name__}: {e})") from e
        return self

    def __exit__(self, *_exc) -> None:
        self._ns = None
        self._app = None
        if self._com_inited:
            try:
                import pythoncom
                pythoncom.CoUninitialize()
            except Exception:
                pass
            self._com_inited = False

    # -- folders -----------------------------------------------------------

    def folder(self, path: str = "Inbox"):
        """'Inbox' (default) or a path under it like 'Inbox/Beacon' or
        'Beacon' (both mean the Inbox subfolder named Beacon)."""
        parts = [p.strip() for p in re.split(r"[\\/]", str(path or "Inbox")) if p.strip()]
        try:
            folder = self._ns.GetDefaultFolder(OL_FOLDER_INBOX)
        except Exception as e:
            raise OutlookUnavailable(
                f"the Inbox could not be opened ({type(e).__name__}: {e})") from e
        if parts and parts[0].casefold() == "inbox":
            parts = parts[1:]
        for name in parts:
            found = None
            try:
                subs = folder.Folders
                count = int(getattr(subs, "Count", 0) or 0)
                for i in range(1, count + 1):
                    sub = subs.Item(i)
                    if str(sub.Name).casefold() == name.casefold():
                        found = sub
                        break
            except Exception as e:
                raise OutlookUnavailable(
                    f"Outlook folder {path!r}: {type(e).__name__}: {e}") from e
            if found is None:
                raise OutlookUnavailable(
                    f"Outlook folder not found: {path!r} (no subfolder {name!r} under "
                    f"{folder.Name})")
            folder = found
        return folder

    # -- scan --------------------------------------------------------------

    def scan(self, folder_path: str, since: datetime, *, max_items: int = 300,
             own_email: str = "") -> list[EmailRef]:
        """Mail received since `since` (newest first), own mail skipped, with
        the IPS case numbers each one names."""
        folder = self.folder(folder_path)
        store_id = str(getattr(folder, "StoreID", "") or "")
        items = folder.Items
        try:
            items.Sort("[ReceivedTime]", True)
        except Exception:
            pass
        try:
            items = items.Restrict(f"[ReceivedTime] >= '{_restrict_stamp(since)}'")
        except Exception:
            pass                                 # filtered by hand below
        own = (own_email or "").strip().casefold()
        out: list[EmailRef] = []
        count = int(getattr(items, "Count", 0) or 0)
        for i in range(1, min(count, max_items) + 1):
            try:
                item = items.Item(i)
            except Exception:
                continue
            if int(getattr(item, "Class", OL_MAIL_ITEM) or OL_MAIL_ITEM) != OL_MAIL_ITEM:
                continue
            received = _to_datetime(getattr(item, "ReceivedTime", None))
            if received is not None and received < since.replace(tzinfo=None):
                continue
            sender_email = _sender_email(item)
            if own and sender_email.casefold() == own:
                continue
            subject = str(getattr(item, "Subject", "") or "")
            try:
                body = str(getattr(item, "Body", "") or "")[:MAX_BODY_CHARS]
            except Exception:
                body = ""
            out.append(EmailRef(
                entry_id=str(item.EntryID), store_id=store_id, subject=subject,
                sender_name=str(getattr(item, "SenderName", "") or ""),
                sender_email=sender_email,
                received=received.isoformat(timespec="seconds") if received else "",
                conversation_id=str(getattr(item, "ConversationID", "") or ""),
                case_nbrs=extract_case_numbers(subject, body)))
        return out

    # -- reply draft -------------------------------------------------------

    def save_reply_draft(self, ref: dict, html_fragment: str, *,
                         reply_all: bool = True, display: bool = False) -> dict:
        """Create a reply to the mail in `ref` with `html_fragment` above the
        quoted thread and SAVE it to Drafts. Returns {entry_id, to, subject}.
        Nothing is ever sent from here."""
        entry_id = str(ref.get("entry_id") or "")
        store_id = str(ref.get("store_id") or "")
        try:
            item = (self._ns.GetItemFromID(entry_id, store_id) if store_id
                    else self._ns.GetItemFromID(entry_id))
        except Exception as e:
            raise OutlookUnavailable(
                "the original mail could not be opened in Outlook (moved or deleted?) "
                f"({type(e).__name__}: {e})") from e
        try:
            reply = item.ReplyAll() if reply_all else item.Reply()
            reply.HTMLBody = html_fragment + str(getattr(reply, "HTMLBody", "") or "")
            reply.Save()
            if display:
                try:
                    reply.Display()
                except Exception:
                    pass
            return {"entry_id": str(getattr(reply, "EntryID", "") or ""),
                    "to": str(getattr(reply, "To", "") or ""),
                    "subject": str(getattr(reply, "Subject", "") or "")}
        except Exception as e:
            raise OutlookUnavailable(
                f"Outlook refused to create the reply draft ({type(e).__name__}: {e}) — "
                "if Outlook showed a programmatic-access warning, allow it and approve again"
            ) from e


def reply_fragment(body_html: str) -> str:
    """The analysis as it goes above the quoted mail in the reply."""
    return ('<div style="font-family: Calibri, Arial, sans-serif; font-size: 11pt;">'
            + body_html + '</div><br/>')
