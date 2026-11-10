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
OL_FOLDER_SENT = 5
OL_MAIL_ITEM = 43
STATE_FILE = "email_state.json"
MAX_BODY_CHARS = 20000          # the case number is near the top anyway
SCAN_EXAMINE_FACTOR = 20        # items a scan may pass over per mail it may read

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


def update_state(root, change) -> dict:
    """One read-modify-write of the scan state: `change(state)` edits the
    state that is on the share NOW, and the result is written back. The
    state file is shared — the server's scheduled scan and a reviewer's
    manual scan on another PC both record processed mail in it — so the
    whole transaction runs under _LOCK (this process's threads) and
    queue.shared_file_lock (the machines), and is written through a unique
    temp file. Without that, one side's write replaces the other's marks and
    mail that was already handled is analysed again. Returns the new state."""
    from .queue import _atomic_write, read_json_for_update, shared_file_lock
    with _LOCK:
        p = state_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        with shared_file_lock(p):
            state = read_json_for_update(p, what="email scan state")
            state.setdefault("processed", {})
            state.setdefault("last_scan_at", "")
            change(state)
            _atomic_write(p, json.dumps(state, indent=2))
            return state


def save_state(root, state: dict) -> None:
    """Replace the whole state. Only for a caller that owns it outright (a
    test, a reset); a running scan records its marks with update_state."""
    def _replace(current: dict) -> None:
        current.clear()
        current.update(state)
    update_state(root, _replace)


def set_last_scan(root, stamp: str) -> None:
    """Record when the last scan started, leaving every processed mark alone."""
    update_state(root, lambda state: state.__setitem__("last_scan_at", str(stamp)))


def mark_processed(root, entry_id: str, case_nbr: str = "", draft_id: str = "",
                   complete: bool = False) -> None:
    """Remember what was handled of this mail. Work is tracked per
    (mail, case): `case_nbr` records one case of the mail as done.
    `complete` closes the mail as a whole — a mail that is skipped (no case
    number, automated, a digest) is remembered that way so it is not
    re-listed every round."""
    def _mark(state: dict) -> None:
        entry = state["processed"].setdefault(str(entry_id), {"case_nbrs": [], "draft_ids": []})
        entry.setdefault("case_nbrs", [])
        entry.setdefault("draft_ids", [])
        if case_nbr and case_nbr not in entry["case_nbrs"]:
            entry["case_nbrs"].append(case_nbr)
        if draft_id:
            entry["draft_ids"].append(draft_id)
        if complete:
            entry["complete"] = True
        entry["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    update_state(root, _mark)


def mark_complete(root, entry_ids) -> None:
    """Close several mails as a whole in ONE transaction (see closed_ids)."""
    ids = [str(e) for e in (entry_ids or []) if e]
    if not ids:
        return

    def _close(state: dict) -> None:
        for entry_id in ids:
            entry = state["processed"].setdefault(entry_id, {"case_nbrs": [], "draft_ids": []})
            entry["complete"] = True
    update_state(root, _close)


def closed_ids(state: dict) -> set:
    """The mails nothing is left to do for: skipped ones, and mails whose
    every case has run. A scan passes over them without reading them."""
    return {str(k) for k, v in (state.get("processed") or {}).items()
            if isinstance(v, dict) and v.get("complete")}


def is_processed(state: dict, entry_id: str) -> bool:
    """The mail has been seen at all. A mail naming several cases can be
    seen and still have work left: see pending_cases."""
    return str(entry_id) in (state.get("processed") or {})


def pending_cases(state: dict, entry_id: str, case_nbrs) -> list[str]:
    """The cases of this mail that still have to run: all of them for a mail
    not seen yet, none for a mail closed as a whole, otherwise the ones not
    recorded as done. So a case that was capped out of a round or failed is
    retried even though another case of the same mail succeeded."""
    wanted = [str(c) for c in (case_nbrs or [])]
    entry = (state.get("processed") or {}).get(str(entry_id))
    if entry is None:
        return wanted
    if entry.get("complete"):
        return []
    done = {str(c) for c in (entry.get("case_nbrs") or [])}
    return [c for c in wanted if c not in done]


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
             own_email: str = "", skip_ids=()) -> list[EmailRef]:
        """Mail received since `since` (newest first), own mail skipped, with
        the IPS case numbers each one names.

        `max_items` bounds the work of one scan: the mails that are READ
        (subject + body, the slow part). `skip_ids` are the Outlook ids of
        mail an earlier round closed for good (closed_ids): they are passed
        over without being read and without using up the budget. So a busy
        folder cannot keep an unprocessed mail out of reach behind 300 newer
        ones that were already handled — each round reaches further down
        the window until everything in it has been looked at."""
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
        skip = {str(s) for s in (skip_ids or ())}
        out: list[EmailRef] = []
        count = int(getattr(items, "Count", 0) or 0)
        read = 0
        # Passing over an item is one cheap property read; the ceiling only
        # guards a folder whose date filter could not be applied.
        for i in range(1, min(count, max(max_items, 1) * SCAN_EXAMINE_FACTOR) + 1):
            if read >= max_items:
                break
            try:
                item = items.Item(i)
            except Exception:
                continue
            if int(getattr(item, "Class", OL_MAIL_ITEM) or OL_MAIL_ITEM) != OL_MAIL_ITEM:
                continue
            received = _to_datetime(getattr(item, "ReceivedTime", None))
            if received is not None and received < since.replace(tzinfo=None):
                continue
            if skip and str(getattr(item, "EntryID", "") or "") in skip:
                continue                         # closed in an earlier round
            sender_email = _sender_email(item)
            if own and sender_email.casefold() == own:
                continue
            read += 1
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


    # -- the thread of a case (stage 1) ------------------------------------

    def _named_folder(self, name: str):
        if str(name or "").strip().casefold() in ("sent items", "sent"):
            try:
                return self._ns.GetDefaultFolder(OL_FOLDER_SENT)
            except Exception as e:
                raise OutlookUnavailable(f"Sent Items could not be opened ({e})") from e
        return self.folder(name)

    def thread(self, case_nbr: str, *, folders=("Inbox", "Sent Items"),
               max_items: int = 200, own_email: str = "") -> list:
        """Every mail that names the case (eight-digit or without the leading
        zeros) in the given folders, oldest first, as EmailMessage objects
        (attachments not yet saved: see save_attachments)."""
        from .email_case import EmailMessage, is_automated, is_digest
        nbr = str(case_nbr)
        forms = [nbr] + ([nbr.lstrip("0")] if nbr.lstrip("0") != nbr else [])
        dasl = "@SQL=" + " OR ".join(
            f"\"urn:schemas:httpmail:subject\" LIKE '%{f}%' OR "
            f"\"urn:schemas:httpmail:textdescription\" LIKE '%{f}%'" for f in forms)
        own = (own_email or "").strip().casefold()
        out: list = []
        seen: set = set()
        for fname in folders:
            try:
                folder = self._named_folder(fname)
            except OutlookUnavailable:
                continue
            store_id = str(getattr(folder, "StoreID", "") or "")
            is_sent = str(fname).strip().casefold() in ("sent items", "sent")
            items = folder.Items
            try:
                items.Sort("[ReceivedTime]", True)
            except Exception:
                pass
            restricted = True
            try:
                items = items.Restrict(dasl)
            except Exception:
                restricted = False          # scan by hand below
            count = int(getattr(items, "Count", 0) or 0)
            limit = max_items if restricted else max_items * 5
            for i in range(1, min(count, limit) + 1):
                try:
                    item = items.Item(i)
                except Exception:
                    continue
                if int(getattr(item, "Class", OL_MAIL_ITEM) or OL_MAIL_ITEM) != OL_MAIL_ITEM:
                    continue
                subject = str(getattr(item, "Subject", "") or "")
                try:
                    body = str(getattr(item, "Body", "") or "")[:MAX_BODY_CHARS]
                except Exception:
                    body = ""
                nbrs = extract_case_numbers(subject, body)
                if nbr not in nbrs or is_digest(nbrs):
                    continue
                entry_id = str(item.EntryID)
                if entry_id in seen:
                    continue
                seen.add(entry_id)
                received = _to_datetime(getattr(item, "ReceivedTime", None)
                                        or getattr(item, "SentOn", None))
                sender_email = _sender_email(item)
                sender_name = str(getattr(item, "SenderName", "") or "")
                if is_automated(sender_email, sender_name, subject):
                    continue                     # IPS/Jira/robot mail, auto-replies
                # A mail to oneself sits in Sent Items AND the Inbox: one copy.
                dup_key = (subject.strip().casefold(), sender_email.casefold(),
                           received.strftime("%Y-%m-%dT%H:%M") if received else "")
                if dup_key in seen:
                    continue
                seen.add(dup_key)
                out.append(EmailMessage(
                    entry_id=entry_id, store_id=store_id, folder=str(fname),
                    subject=subject,
                    sender_name=sender_name,
                    sender_email=sender_email,
                    received=received.isoformat(timespec="seconds") if received else "",
                    body=body,
                    is_own=is_sent or (bool(own) and sender_email.casefold() == own),
                    conversation_id=str(getattr(item, "ConversationID", "") or "")))
        return sorted(out, key=lambda m: m.received or "")

    def save_attachments(self, msg, dest_dir: str) -> list[dict]:
        """Save the mail's file attachments under dest_dir, in a folder of
        this mail's own (message_dir_name): mails of one thread reuse file
        names all the time (image001.png, a second WRT.zip), and a shared
        folder would hand a later mail the earlier mail's bytes. Skips
        attached mails / OLE objects; keeps a file this mail already saved.
        Fills and returns msg.attachments: [{name, path, size, is_image}]."""
        from .email_case import IMAGE_EXTS
        dest_dir = os.path.join(dest_dir, message_dir_name(msg))
        try:
            item = (self._ns.GetItemFromID(msg.entry_id, msg.store_id) if msg.store_id
                    else self._ns.GetItemFromID(msg.entry_id))
            atts = item.Attachments
            count = int(getattr(atts, "Count", 0) or 0)
        except Exception as e:
            raise OutlookUnavailable(
                f"attachments of the mail '{msg.subject[:50]}' could not be read ({e})") from e
        out: list[dict] = []
        used: set = set()
        if count:
            os.makedirs(dest_dir, exist_ok=True)
        for i in range(1, count + 1):
            try:
                att = atts.Item(i)
                if int(getattr(att, "Type", 1) or 1) in (5, 6):      # olEmbeddeditem, olOLE
                    continue
                name = str(getattr(att, "FileName", "") or f"attachment_{i}")
                safe = re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or f"attachment_{i}"
                safe = unique_name(safe, used)       # two files of ONE mail with one name
                path = os.path.join(dest_dir, safe)
                if not os.path.exists(path):
                    att.SaveAsFile(path)
                size = os.path.getsize(path) if os.path.exists(path) else 0
                out.append({"name": safe, "path": path, "size": size,
                            "is_image": safe.lower().endswith(IMAGE_EXTS)})
            except Exception as e:
                print(f"[handsfree.outlook] attachment {i} of '{msg.subject[:40]}' skipped: {e}")
        msg.attachments = out
        return out


def message_dir_name(msg) -> str:
    """Folder name for ONE mail's attachments inside the thread folder: when
    it was received plus a short hash of its Outlook id, e.g.
    20261022T101500_3fa9c1d2. Stable across rounds, so a saved file is kept."""
    import hashlib
    stamp = re.sub(r"[^0-9T]", "", str(getattr(msg, "received", "") or ""))[:15] or "undated"
    tag = hashlib.sha1(str(getattr(msg, "entry_id", "") or "").encode("utf-8")).hexdigest()[:8]
    return f"{stamp}_{tag}"


def unique_name(name: str, used: set) -> str:
    """`name`, or "(2) name", "(3) name"… when it was handed out before
    (compared case-insensitively, as the file system does). The counter goes
    in FRONT so every extension — .zip, .tar.gz, .7z.001 — stays intact.
    Records the result in `used`."""
    candidate, n = name, 1
    while candidate.casefold() in used:
        n += 1
        candidate = f"({n}) {name}"
    used.add(candidate.casefold())
    return candidate


def reply_fragment(body_html: str) -> str:
    """The analysis as it goes above the quoted mail in the reply."""
    return ('<div style="font-family: Calibri, Arial, sans-serif; font-size: 11pt;">'
            + body_html + '</div><br/>')
