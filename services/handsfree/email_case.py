"""
An email thread as a case — stage 1 of Beacon's Outlook intake.

Every mail that names an IPS case number forms the thread; the earliest one
is the description, the later ones the comments, their files the uploads.
The result is the same CaseContext the IPS fetch produces, so every later
pipeline stage (triage, reader, checklist, archive pick, decode, analysis,
Echo, draft) runs unchanged on it.

Stage 2 (later): merge_contexts() joins the IPS thread and the email thread
into one context — the reader sees both, the archives of both are
candidates. Keep `source` on the context/analysis/draft so both stages can
tell where a fact came from.

No Outlook/COM here: the messages come from outlook_source (or a fake).
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
INTEL_DOMAINS = ("intel.com",)
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGES_PER_THREAD = 8

_REPLY_PREFIX_RE = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|wg|sv|tr|答复|回复|转发)\s*:\s*)+", re.IGNORECASE)
_BT_RE = re.compile(r"bluetooth|\bbt\b|a2dp|\bhfp\b|le audio|\bhid\b", re.IGNORECASE)
_CACHE_LOCK = threading.Lock()


@dataclass
class EmailMessage:
    entry_id: str
    store_id: str
    folder: str
    subject: str
    sender_name: str
    sender_email: str
    received: str                       # ISO, local clock
    body: str = ""                      # plain text
    is_own: bool = False                # sent by the Beacon user (Sent Items / own address)
    conversation_id: str = ""
    attachments: list = field(default_factory=list)   # [{name, path, size, is_image}]

    @property
    def received_dt(self) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(self.received)
        except Exception:
            return None


# ----------------------------------------------------------------- helpers

def clean_subject(subject: str) -> str:
    """'RE: FW: Case 01031658 drops' -> 'Case 01031658 drops'."""
    return _REPLY_PREFIX_RE.sub("", str(subject or "")).strip()


def author_type(msg: EmailMessage) -> str:
    """The reader's author vocabulary: Intel-side rows are 'Agent', the
    customer side 'Customer' (case_reader.customer_visible_history)."""
    if msg.is_own:
        return "Agent"
    domain = (msg.sender_email or "").rsplit("@", 1)[-1].lower()
    return "Agent" if any(domain == d or domain.endswith("." + d) for d in INTEL_DOMAINS) \
        else "Customer"


def _stamp(msg: EmailMessage) -> str:
    """Upload-time metadata in the IPS attachment format (_pick_latest_archive
    parses '%m/%d/%Y %H:%M')."""
    dt = msg.received_dt
    return dt.strftime("%m/%d/%Y %H:%M") if dt else ""


def _header(msg: EmailMessage) -> str:
    who = msg.sender_name or msg.sender_email or "?"
    if msg.sender_email and msg.sender_name:
        who += f" <{msg.sender_email}>"
    return f"[Email from {who} on {msg.received[:16].replace('T', ' ')} — subject: {msg.subject}]"


def sort_messages(messages) -> list[EmailMessage]:
    return sorted(messages or [], key=lambda m: m.received or "")


def reply_target(messages) -> Optional[EmailMessage]:
    """The mail the reply draft answers: the newest one not written by us
    (else simply the newest)."""
    msgs = sort_messages(messages)
    for m in reversed(msgs):
        if not m.is_own:
            return m
    return msgs[-1] if msgs else None


# ------------------------------------------------------------- the context

def build_case_context(case_nbr: str, messages, download_dir: str):
    """CaseContext (models.models) for the thread. Extra attributes carried
    on the instance: source='email', local_paths {attachment name: path},
    messages, image_files [(message, attachment)]."""
    from models.models import CaseContext

    msgs = sort_messages(messages)
    if not msgs:
        raise ValueError(f"no mail names case {case_nbr}")
    first = msgs[0]
    subject = clean_subject(first.subject) or f"Email thread for case {case_nbr}"
    description = _header(first) + "\n\n" + (first.body or "").strip()
    comments = [[m.received, author_type(m), _header(m) + " " + (m.body or "").strip()]
                for m in msgs[1:]]

    attachment_list: list = []
    local_paths: dict = {}
    image_files: list = []
    for m in msgs:
        for a in m.attachments or []:
            name = str(a.get("name") or "")
            if not name:
                continue
            if a.get("is_image"):
                image_files.append((m, a))
                continue
            if name in local_paths:          # same file name sent twice: keep the first
                continue
            attachment_list.append([name, "", [_stamp(m), f"email from {m.sender_name or m.sender_email}"]])
            local_paths[name] = str(a.get("path") or "")

    text = subject + " " + (first.body or "")[:4000]
    wifi_or_bt = "bt" if _BT_RE.search(text) else "wifi"

    ctx = CaseContext(case_nbr=str(case_nbr), id="", subject=subject, description=description,
                      env_detail={}, subcategory="Email thread", comments=comments,
                      attachment_list=attachment_list, case_download_dir=str(download_dir),
                      wifi_or_bt=wifi_or_bt, case_ref_source="email")
    ctx.source = "email"
    ctx.local_paths = local_paths
    ctx.messages = msgs
    ctx.image_files = image_files
    return ctx


def merge_contexts(ips_ctx, email_ctx):
    """Stage 2 entry point: one context from the IPS case and its email
    thread. Comments interleave by time (email rows tagged), the archives of
    both are candidates (IPS names win on a clash), the mail's files stay
    local. Not wired to a trigger yet."""
    merged = copy.copy(ips_ctx)
    email_rows = [[r[0], r[1], "[email] " + str(r[2])] for r in (email_ctx.comments or [])]
    merged.comments = sorted(list(ips_ctx.comments or []) + email_rows, key=lambda r: str(r[0]))
    merged.description = ((ips_ctx.description or "").rstrip()
                          + "\n\n[Email thread]\n" + (email_ctx.description or ""))
    names = {str(it[0]) for it in (ips_ctx.attachment_list or []) if it}
    merged.attachment_list = list(ips_ctx.attachment_list or []) + [
        it for it in (email_ctx.attachment_list or []) if str(it[0]) not in names]
    merged.source = "ips+email"
    merged.local_paths = dict(getattr(email_ctx, "local_paths", {}) or {})
    merged.messages = list(getattr(email_ctx, "messages", []) or [])
    merged.image_files = list(getattr(email_ctx, "image_files", []) or [])
    return merged


# ------------------------------------------------------------- images

def _load_cache(path: Optional[Path]) -> dict:
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cache(path: Optional[Path], data: dict) -> None:
    if not path:
        return
    with _CACHE_LOCK:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)


def annotate_email_images(llm, ctx, *, cache_path: Optional[Path] = None,
                          progress: Optional[Callable[[str], None]] = None) -> tuple[list, list[dict]]:
    """Describe the image attachments of the thread (vision), one call per
    mail, cached per mail under email:<entry id> (same cache file as the
    IPS comment images). -> (comment rows incl. the synthetic image rows,
    details) in the shape annotate_case_comments returns."""
    from .comment_images import describe_images, sniff_media_type, _ALLOWED_MEDIA
    say = progress or (lambda m: None)
    rows = list(ctx.comments or [])
    details: list[dict] = []
    groups: dict = {}
    for m, a in getattr(ctx, "image_files", []) or []:
        groups.setdefault(m.entry_id, (m, []))[1].append(a)
    if not groups:
        return rows, details
    cached = _load_cache(cache_path)
    budget = MAX_IMAGES_PER_THREAD
    changed = False
    first_id = ctx.messages[0].entry_id if getattr(ctx, "messages", None) else ""
    for entry_id, (m, files) in groups.items():
        key = f"email:{entry_id}"
        author = f"{author_type(m)} ({m.sender_name or m.sender_email})"
        entry = cached.get(key)
        if entry and entry.get("url_count") == len(files):
            descs = entry["descriptions"]
            say(f"{len(files)} image(s) in the mail of {m.received[:10]} from {m.sender_name}: cached")
        else:
            if budget <= 0:
                say(f"image budget ({MAX_IMAGES_PER_THREAD}) exhausted — skipping the mail of {m.received[:10]}")
                continue
            images: list[tuple[str, bytes]] = []
            for a in files[:budget]:
                try:
                    data = Path(a["path"]).read_bytes()[:MAX_IMAGE_BYTES]
                except Exception:
                    continue
                media = sniff_media_type(data, "")
                if media in _ALLOWED_MEDIA:
                    images.append((media, data))
            budget -= len(images)
            if not images:
                say(f"the image(s) attached to the mail of {m.received[:10]} could not be read")
                continue
            say(f"describing {len(images)} image(s) attached to the mail of {m.received[:10]} from {m.sender_name}")
            descs = describe_images(llm, images, author=author, date=m.received[:16],
                                    comment_text=(m.body or "")[:2000])
            if any(descs):
                cached[key] = {"descriptions": descs, "url_count": len(files),
                               "date": m.received, "author": author}
                changed = True
        descs = [d for d in descs if d]
        if not descs:
            continue
        text = (f"[Images attached to the mail of {m.received[:10]} from {m.sender_name or m.sender_email}] "
                + " ".join(f"{i}) {d}" for i, d in enumerate(descs, 1)))
        row = [m.received, author_type(m), text]
        if entry_id == first_id:
            rows.insert(0, row)
        else:
            rows.append(row)
        details.append({"comment_id": entry_id, "date": m.received, "author": author,
                        "count": len(files), "descriptions": descs})
    if changed:
        _save_cache(cache_path, cached)
    if details:
        head = rows[0] if rows and rows[0][0] == (ctx.messages[0].received if ctx.messages else None) else None
        rest = rows[1:] if head else rows
        rows = ([head] if head else []) + sorted(rest, key=lambda r: str(r[0]))
    return rows, details
