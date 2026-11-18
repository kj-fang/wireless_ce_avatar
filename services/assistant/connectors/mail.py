"""
Mail connectors: the user's own Outlook, read-only.

    list_mail    the mail received since <when>, pre-sorted by what looks urgent
    read_mail    one of those mails in full

Outlook is reached through COM on this PC (services/handsfree/outlook_source).
A COM session belongs to the thread that opened it, so everything Outlook is
asked happens inside one `with Outlook()` on the connector's own thread and
leaves it as plain dicts. One session at a time (_GATE): a chat turn and a
scheduled task must not open two.

Nothing is written: no mail is marked read, moved, flagged, answered or sent,
and the email intake's scan state is not touched (run_email_scan is not used).

What the model sees
    * a short id per mail ("m3") instead of Outlook's 140-character entry id;
      the real id stays in ctx.handles
    * notification mail (IPS, Jira, auto-replies) folded into counts - an IPS
      notification is the signal "the customer commented on case X", its text
      is boilerplate
    * a pre-score with its reasons, as a starting order. The weights are a
      first guess, not learned from anything; the model makes the final call
      and says why.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime
from typing import Optional

from services.assistant import timeutil
from services.assistant.registry import Connector, Ctx, object_schema

MAX_LISTED = 40
MAX_READ = 120                  # mails examined per listing
PREVIEW_CHARS = 220
BODY_CHARS = 4000
GATE_WAIT_S = 40

WEIGHTS = {"external": 40, "names_case": 20, "my_case": 10, "unread": 20, "high_importance": 15,
           "flagged": 10, "to_me": 10, "urgent_words": 10, "digest": -20}
_URGENT_RE = re.compile(
    r"(?i)\b(urgent|asap|escalat\w*|critical|blocker|blocking|showstopper|deadline|"
    r"line[- ]down|stop[- ]ship|by (today|tomorrow|eod|cob))\b|緊急|急件|盡快|儘快|今天內|卡關|嚴重")

_GATE = threading.Lock()


# ---- seams (tests replace these) ---------------------------------------------

def _outlook():
    from services.handsfree.outlook_source import Outlook
    return Outlook()


def _my_case_numbers(owner: str) -> set:
    """Case numbers of the user's Muse drafts - a cheap way to know "this
    mail is about one of MY cases" without another IPS query. Empty when the
    share cannot be read."""
    if not owner:
        return set()
    try:
        from services.handsfree.queue import open_store
        return {str(d.get("case_nbr") or "")
                for d in open_store().list_drafts(include_closed=False, limit=500, owner_name=owner)}
    except Exception:
        return set()


# ---- helpers -------------------------------------------------------------------

def _tokens(name: str) -> set:
    return {t for t in re.split(r"[^a-z0-9]+", str(name or "").casefold()) if len(t) > 1}


def _is_external(sender_email: str) -> bool:
    from services.handsfree.email_case import INTEL_DOMAINS
    domain = str(sender_email or "").rsplit("@", 1)[-1].casefold()
    if "@" not in str(sender_email or ""):
        return False                       # an unresolved Exchange address: internal
    return not any(domain == d or domain.endswith("." + d) for d in INTEL_DOMAINS)


def _addressed_to(row: dict, owner: str, mailbox: str) -> bool:
    """Whether the user is on the To line. Outlook gives display names there
    (asking for addresses can raise its programmatic-access prompt), so the
    signed-in name is compared word by word, and the mailbox as a fallback."""
    to = str(row.get("to") or "")
    if mailbox and mailbox.casefold() in to.casefold():
        return True
    mine = _tokens(owner)
    return bool(mine) and any(mine <= _tokens(entry) for entry in to.split(";"))


def score(row: dict, *, owner: str, mailbox: str, my_cases: set) -> tuple[int, list[str]]:
    """(pre-score, reasons) of one person-written mail. Pure."""
    from services.handsfree.email_case import is_digest
    points, why = 0, []

    def add(key: str, text: str) -> None:
        nonlocal points
        points += WEIGHTS[key]
        why.append(text)

    cases = row.get("case_nbrs") or []
    if _is_external(row.get("sender_email")):
        add("external", "from outside Intel")
    if is_digest(cases):
        add("digest", "status digest naming many cases")
    elif cases:
        add("names_case", "names case " + ", ".join(cases[:3]))
        if my_cases & set(cases):
            add("my_case", "one of your cases")
    if row.get("unread"):
        add("unread", "unread")
    if int(row.get("importance") or 1) >= 2:
        add("high_importance", "marked high importance")
    if row.get("flagged"):
        add("flagged", "flagged")
    if _addressed_to(row, owner, mailbox):
        add("to_me", "addressed to you")
    if _URGENT_RE.search(f"{row.get('subject') or ''} {row.get('preview') or ''}"):
        add("urgent_words", "urgent wording")
    return points, why


def _handle(ctx: Ctx, row: dict) -> str:
    """The short id of a mail in this conversation (the same mail keeps its id)."""
    by_entry = ctx.handles.setdefault("_mail_by_entry", {})
    entry = row.get("entry_id") or ""
    if entry in by_entry:
        return by_entry[entry]
    seq = int(ctx.handles.get("_mail_seq") or 0) + 1
    ctx.handles["_mail_seq"] = seq
    handle = f"m{seq}"
    by_entry[entry] = handle
    ctx.handles[handle] = {"entry_id": entry, "store_id": row.get("store_id") or "",
                           "subject": str(row.get("subject") or "")[:120]}
    return handle


def _who(row: dict) -> str:
    name, email = str(row.get("sender_name") or ""), str(row.get("sender_email") or "")
    return f"{name} <{email}>" if name and "@" in email else (name or email or "?")


class _Busy(RuntimeError):
    pass


def _with_outlook(work):
    """Run work(outlook) inside one Outlook session, one caller at a time."""
    if not _GATE.acquire(timeout=GATE_WAIT_S):
        raise _Busy("Outlook is busy with another request of the assistant; try again shortly")
    try:
        with _outlook() as ol:
            return work(ol)
    finally:
        _GATE.release()


# ---- list_mail -----------------------------------------------------------------

def _list_mail(args: dict, ctx: Ctx) -> dict:
    now = ctx.now()
    try:
        start = timeutil.window_start(args.get("since"), now)
    except ValueError as e:
        return {"error": str(e)}
    folder = str(args.get("folder") or "Inbox").strip() or "Inbox"
    ctx.emit("⏳ Reading Outlook ...")
    try:
        rows = _with_outlook(lambda ol: ol.list_mail(
            folder, start.replace(tzinfo=None), max_items=MAX_READ, preview_chars=PREVIEW_CHARS))
    except Exception as e:
        return {"error": f"Outlook could not be read: {e}"}
    return build_mail_list(rows, ctx, start=start, now=now, folder=folder,
                           my_cases=_my_case_numbers(ctx.owner_name))


def build_mail_list(rows: list[dict], ctx: Ctx, *, start: datetime, now: datetime,
                    folder: str = "Inbox", my_cases: Optional[set] = None) -> dict:
    from services.handsfree.email_case import is_automated
    my_cases = my_cases or set()
    own = (ctx.mailbox or "").casefold()
    people, case_notices, other_auto = [], {}, {}
    from_me = 0
    for row in rows:
        sender = str(row.get("sender_email") or "")
        if own and sender.casefold() == own:
            from_me += 1
            continue
        if is_automated(sender, row.get("sender_name") or "", row.get("subject") or ""):
            cases = row.get("case_nbrs") or []
            if cases and "intel.premier.support" in sender.casefold():
                for nbr in cases[:3]:
                    slot = case_notices.setdefault(nbr, {"case": nbr, "notifications": 0,
                                                         "latest": "", "about": []})
                    slot["notifications"] += 1
                    slot["latest"] = max(slot["latest"], row.get("received") or "")
                    kind = _notice_kind(row.get("subject") or "")
                    if kind and kind not in slot["about"]:
                        slot["about"].append(kind)
            else:
                key = str(row.get("sender_name") or sender or "?")
                slot = other_auto.setdefault(key, {"sender": key, "count": 0})
                slot["count"] += 1
            continue
        points, why = score(row, owner=ctx.owner_name, mailbox=ctx.mailbox, my_cases=my_cases)
        people.append((points, row, why))

    # Highest score first; the newer mail first within a score (stable sorts).
    people.sort(key=lambda t: t[1].get("received") or "", reverse=True)
    people.sort(key=lambda t: t[0], reverse=True)
    listed = []
    for points, row, why in people[:MAX_LISTED]:
        item = {
            "id": _handle(ctx, row),
            "from": _who(row),
            "subject": str(row.get("subject") or "")[:140],
            "received": timeutil.label(row.get("received"), now),
            "pre_score": points,
            "why": why,
            "preview": str(row.get("preview") or "")[:PREVIEW_CHARS],
        }
        if row.get("case_nbrs"):
            item["cases"] = list(row["case_nbrs"][:4])
        if row.get("attachments"):
            item["attachments"] = int(row["attachments"])
        listed.append(item)

    notices = sorted(case_notices.values(), key=lambda s: s["latest"], reverse=True)
    for slot in notices:
        slot["latest"] = timeutil.label(slot["latest"], now)
        slot["one_of_your_cases"] = slot["case"] in my_cases
    result = {
        "folder": folder,
        "since": timeutil.stamp(start),
        "as_of": timeutil.stamp(now),
        "counts": {"from_people": len(people), "listed": len(listed),
                   "ips_notifications": sum(s["notifications"] for s in notices),
                   "other_automatic": sum(s["count"] for s in other_auto.values()),
                   "sent_by_you": from_me},
        "mail": listed,
        "ips_notifications_by_case": notices[:25],
        "other_automatic": sorted(other_auto.values(), key=lambda s: -s["count"])[:10],
        "note": "pre_score is a rough starting order (weights are a first guess). Decide the "
                "real priority from the content and say why. Use read_mail(id) for the full text.",
        "summary": f"{len(people)} mail(s) from people and "
                   f"{sum(s['notifications'] for s in notices)} IPS notification(s) in {folder} "
                   f"since {start.strftime('%m-%d %H:%M')}",
    }
    warnings = []
    if not rows:
        # An empty period is believable at 7 am and suspicious at 4 pm. The
        # usual cause is not an empty mailbox: mail is read from classic
        # Outlook, and a classic Outlook that was only just started (the user
        # works in the new Outlook app, which cannot be automated) has not
        # synchronised yet.
        warnings.append(
            "No mail was found for this period. Mail is read from classic Outlook on this PC; "
            "if it was only just started it may still be synchronising (the new Outlook app is "
            "not read). Tell the engineer this and suggest asking again in a minute.")
    if len(rows) >= MAX_READ:
        warnings.append(f"Only the newest {MAX_READ} mails of the period were examined.")
    if len(people) > len(listed):
        warnings.append(f"Only the {len(listed)} highest-scored of {len(people)} mails are listed.")
    if warnings:
        result["warnings"] = warnings
    return result


def _notice_kind(subject: str) -> str:
    """'IPS has New Private Comment on Case 0103...' -> 'new private comment'."""
    m = re.search(r"(?i)\bips\s+(?:has|-)\s+(.+?)\s+(?:on|for)\s+case\b", subject)
    return m.group(1).strip().lower()[:60] if m else ""


# ---- read_mail -----------------------------------------------------------------

def read_handle(ctx: Ctx, handle: str) -> dict:
    """The full mail behind a short id: a read_item dict, or {"error"}."""
    ref = ctx.handles.get(str(handle or "").strip().lower())
    if not isinstance(ref, dict) or not ref.get("entry_id"):
        return {"error": f"unknown mail id {handle!r}: ids come from list_mail in this conversation"}
    try:
        return _with_outlook(lambda ol: ol.read_item(ref["entry_id"], ref.get("store_id") or "",
                                                     max_chars=BODY_CHARS))
    except Exception as e:
        return {"error": f"Outlook could not open that mail: {e}"}


def _read_mail(args: dict, ctx: Ctx) -> dict:
    mail = read_handle(ctx, args.get("id"))
    if mail.get("error"):
        return mail
    now = ctx.now()
    out = {
        "id": str(args.get("id")).strip().lower(),
        "from": _who(mail),
        "to": str(mail.get("to") or "")[:400],
        "subject": mail.get("subject") or "",
        "received": timeutil.label(mail.get("received"), now),
        "unread": bool(mail.get("unread")),
        "body": mail.get("body") or "",
        "summary": f"mail {args.get('id')}: {str(mail.get('subject') or '')[:80]}",
    }
    if mail.get("cc"):
        out["cc"] = str(mail["cc"])[:300]
    if mail.get("case_nbrs"):
        out["cases"] = list(mail["case_nbrs"][:6])
    if mail.get("attachments"):
        out["attachments"] = int(mail["attachments"])
    if mail.get("body_cut"):
        out["note"] = f"Only the first {BODY_CHARS} characters of the body are shown."
    return out


def connectors() -> list[Connector]:
    return [
        Connector(
            name="list_mail",
            description=(
                "List the mail the engineer received in Outlook since a point in time, as a "
                "pre-sorted list: sender, subject, time, a short preview, the IPS cases it names "
                "and a rough pre-score with its reasons. IPS notification mails are folded into "
                "per-case counts ('2 notifications on case 0103...': the customer or a colleague "
                "commented there). Use it for 'what is in my mail today', 'what should I handle "
                "first'. Read-only: nothing is marked read or changed."),
            parameters=object_schema({
                "since": {"type": "string",
                          "description": "Start of the period in the engineer's local time: today "
                                         "(default), yesterday, 24h, 3d, or an ISO date / date-time."},
                "folder": {"type": "string",
                           "description": "Outlook folder: Inbox (default), a subfolder of the "
                                          "Inbox such as 'Inbox/Customers', or 'Sent Items'."},
            }),
            run=_list_mail, timeout_s=120, untrusted=True, result_chars=16000),
        Connector(
            name="read_mail",
            description=(
                "The full text of one mail from list_mail (first 4000 characters), with its "
                "recipients. Pass the short id list_mail gave (m1, m2, ...). Read-only."),
            parameters=object_schema(
                {"id": {"type": "string", "description": "The mail's short id from list_mail, e.g. m3."}},
                ["id"]),
            run=_read_mail, timeout_s=60, untrusted=True, result_chars=7000),
    ]
