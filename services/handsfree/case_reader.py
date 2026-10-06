"""
LLM case-history reader.

Reads the IPS case the way an engineer does: Issue Description first, then
every comment in chronological order (later comments supersede earlier ones
— OEMs frequently post updated repro times and fresh log uploads in
comments). Produces:

  * the CURRENT issue statement (clean, single paragraph),
  * the issue time(s) the analysis should anchor on,
  * which attachment most likely contains the driver log covering that time.

Output feeds the runner: chosen attachment → pick_zip; issue_times →
pick_etl + agent prompts.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

# Caps so a comment-heavy case can't blow the prompt.
_MAX_COMMENTS = 30
_MAX_COMMENT_CHARS = 1200
_MAX_ATTACHMENTS = 25
_MAX_DESC_CHARS = 4000

READER_PROMPT = """\
You are an Intel Wi-Fi support engineer reviewing an IPS case before log
analysis. Read the ISSUE DESCRIPTION first, then the COMMENTS strictly in
time order — later comments often supersede the original description (new
repro, corrected time, newer log upload).

Your tasks:
 1. State the CURRENT issue under investigation in one clean paragraph
    (the latest understanding, not necessarily the original description).
 2. Identify the issue occurrence time(s) to anchor log analysis on.
    - Prefer the most recent explicitly reported failure time.
    - Copy times EXACTLY as written in the case (do not convert timezones
      or reformat); include the date when stated.
    - Up to 3 times, most relevant first. Empty list if none is stated.
 3. Choose which ONE attachment most likely contains the driver log that
    covers the issue time. Judge by: the comment that mentions the upload,
    upload timestamp vs issue time (log must be captured AT/AFTER the
    issue), filename hints (date stamps, "log", "trace", platform names),
    and the attachment subtitle. Prefer .zip driver-log captures over
    screenshots/documents.
 4. Assess whether the case gives an engineer enough to analyze, judged
    over the description, all comments AND the ENVIRONMENT DETAILS form
    together (a later comment or a filled form field can fill a gap in the
    description — a filled "Steps to reproduce" form field means repro steps
    are NOT missing; form dates such as "HDD Lock" or "Found In Build" are
    context, not the issue occurrence time). CAUTION: the "Assert Error"
    form field is customer-filled from Windows Event Viewer — its value is
    NOT a driver/firmware assert code; never treat it as one. Report what
    is missing or too vague:
    - "issue_description": no understandable statement of what fails /
      expected vs actual behavior.
    - "issue_time": no failure date/time stated anywhere.
    - "repro_steps": no reproduction steps and no frequency information.
    Report ONLY genuinely missing/unclear items — an item that is stated
    anywhere, even briefly, is NOT missing.
 5. Classify the issue into exactly ONE debugging domain from this list
    (name must match verbatim; use "Others" when nothing fits):
{domains_block}
    When the chosen domain has sub-categories, also pick the ONE
    "issue_subcategory" that fits best (empty string for other domains):
{subcats_block}
 6. From the LATEST state of the thread, state the NEXT ACTION on the case
    in one sentence and who owns it. Comment authors tagged [Partner],
    [Customer], [Partner - ...] are the customer side; [Agent] and [FAE]
    are Intel (the case owner).
    - "action_owner": "intel" when Intel must act next (e.g. the customer
      has provided the requested logs/information or asked a question that
      is still unanswered); "customer" when Intel is waiting on the
      customer (e.g. Intel asked for logs/repro/time and nothing came
      back yet); "unknown" when the thread does not tell.
    - "next_action": e.g. "Intel to analyze the WRT log uploaded in
      comment #4 for the 10:17 failure" or "Customer to provide the
      failure time and WRT logs Intel asked for in comment #2".

Output ONLY a valid JSON object (no markdown, no code fences):
{{
  "clean_description": "<one-paragraph current issue statement>",
  "issue_times": ["<time exactly as written>", "..."],
  "issue_time_source": "<'description' or 'comment #N' that stated the time>",
  "attachment_name": "<EXACT filename from the ATTACHMENTS list, or '' if none fits>",
  "attachment_reason": "<one sentence: why this attachment matches the issue time>",
  "reasoning": "<2-4 sentences tracing how the comments changed the picture>",
  "missing_info": [{{"item": "issue_description|issue_time|repro_steps",
                     "reason": "<one short sentence why it is missing/unclear>"}}],
  "issue_domain": "<one domain name from the list, verbatim>",
  "issue_subcategory": "<sub-category name, or ''>",
  "action_owner": "<'intel' | 'customer' | 'unknown'>",
  "next_action": "<one sentence>"
}}

=== SUBJECT ===
{subject}

=== ENVIRONMENT DETAILS (structured Q&A form filled by the customer) ===
{env_block}

=== ISSUE DESCRIPTION ===
{description}

=== COMMENTS (chronological; #1 is oldest) ===
{comments_block}

=== ATTACHMENTS (candidate log uploads) ===
{attachments_block}
"""


ACTION_OWNER_LABELS = {"intel": "Intel (case owner)", "customer": "customer"}


def normalize_action_owner(raw: Any) -> str:
    """'intel' | 'customer' | '' (unknown) from the reader's free-form value
    ('Intel', 'case owner', 'OEM', 'partner' ... all tolerated)."""
    low = str(raw or "").strip().lower()
    if any(k in low for k in ("intel", "agent", "case owner", "fae")):
        return "intel"
    if any(k in low for k in ("customer", "partner", "oem", "odm")):
        return "customer"
    return ""


def _normalize_comments(comments: Any) -> list[dict]:
    """Coerce case_ctx.comments ([dtm, author_type, text] rows, possibly
    unsorted / stringy) into sorted dicts. Unknown shapes degrade to text."""
    rows: list[dict] = []
    if isinstance(comments, str):
        if comments.strip():
            rows.append({"ts": "", "author": "", "text": comments.strip()})
        return rows
    for c in (comments or []):
        try:
            if isinstance(c, (list, tuple)) and len(c) >= 3:
                ts, author, text = c[0], c[1], c[2]
            elif isinstance(c, dict):
                ts = c.get("ts") or c.get("created") or ""
                author = c.get("author") or ""
                text = c.get("text") or c.get("comment") or ""
            else:
                ts, author, text = "", "", str(c)
            text = (str(text) if text is not None else "").strip()
            if not text:
                continue
            rows.append({"ts": str(ts or ""), "author": str(author or ""),
                         "text": text})
        except Exception:
            continue
    # Chronological ascending; rows without a timestamp keep their position
    # after the dated ones (stable sort on empty string sorts first — push
    # them last instead so the "latest supersedes" instruction stays sound).
    dated = [r for r in rows if r["ts"]]
    undated = [r for r in rows if not r["ts"]]
    dated.sort(key=lambda r: r["ts"])
    return dated + undated


def _format_comments(rows: list[dict]) -> str:
    if not rows:
        return "(no comments)"
    rows = rows[-_MAX_COMMENTS:]      # keep the most recent N
    out = []
    for i, r in enumerate(rows, 1):
        text = r["text"]
        if len(text) > _MAX_COMMENT_CHARS:
            text = text[:_MAX_COMMENT_CHARS] + " …[truncated]"
        who = f" [{r['author']}]" if r["author"] else ""
        ts = f" ({r['ts']})" if r["ts"] else ""
        out.append(f"#{i}{ts}{who}: {text}")
    return "\n".join(out)


# Comment author types (CORE_IPS_COMMENT_AUTHOR_TYPE_TXT) written by the
# customer side: "Partner", "Partner - Agent", "Partner - DFAE", "Customer".
# Intel-side rows ("Agent", "FAE", "Backend Integration", "System") may be
# Private-to-Intel — the comment rows carry no visibility flag, so only an
# allowlisted customer author proves a comment is customer-visible.
_CUSTOMER_AUTHOR_PREFIXES = ("partner", "customer")
_MAX_CUSTOMER_COMMENTS = 12
_MAX_CUSTOMER_COMMENT_CHARS = 400


def customer_visible_history(comments: Any) -> str:
    """Customer-authored comments only, chronological, bounded — the one
    slice of the comment history that is safe to quote in a PUBLIC reply.
    Unknown / empty author types are excluded (fail closed)."""
    rows = [r for r in _normalize_comments(comments)
            if r["author"].strip().lower().startswith(_CUSTOMER_AUTHOR_PREFIXES)]
    out = []
    for r in rows[-_MAX_CUSTOMER_COMMENTS:]:
        text = r["text"]
        if len(text) > _MAX_CUSTOMER_COMMENT_CHARS:
            text = text[:_MAX_CUSTOMER_COMMENT_CHARS] + " …[truncated]"
        out.append((f"({r['ts']}) " if r["ts"] else "") + text)
    return "\n".join(out)


_MAX_ENV_ENTRIES = 25
_MAX_ENV_VALUE_CHARS = 300


def _format_env_detail(env: Any) -> str:
    """Render the IPS Environment Details Q&A dict as 'Q: A' lines.
    Empty/whitespace responses are skipped (the form always lists every
    question; only filled answers carry information)."""
    if not isinstance(env, dict) or not env:
        return "(none)"
    out = []
    for q, a in list(env.items())[:_MAX_ENV_ENTRIES]:
        a = str(a or "").strip()
        if not a:
            continue
        if len(a) > _MAX_ENV_VALUE_CHARS:
            a = a[:_MAX_ENV_VALUE_CHARS] + " …[truncated]"
        out.append(f"{str(q).strip()}: {a}")
    return "\n".join(out) or "(none)"


def _format_attachments(attachment_list: Any) -> str:
    if not attachment_list:
        return "(no attachments)"
    out = []
    for item in list(attachment_list)[:_MAX_ATTACHMENTS]:
        try:
            name = str(item[0])
            meta = item[2] if len(item) > 2 else ["", ""]
            ts = str(meta[0]) if meta and len(meta) > 0 else ""
            subtitle = str(meta[1]) if meta and len(meta) > 1 else ""
        except Exception:
            continue
        out.append(f"- {name}"
                   + (f" | uploaded: {ts}" if ts else "")
                   + (f" | note: {subtitle}" if subtitle else ""))
    return "\n".join(out) or "(no attachments)"


def read_case_history(llm, *, subject: str, description: str,
                      comments: Any, attachment_list: Any,
                      env_detail: Any = None) -> Optional[dict]:
    """One LLM call. Returns the parsed reader dict, or None on any failure
    (callers fall back to description-only organize_issue_context)."""
    from services.ace.roles import _extract_json

    from .checklist import load_checklist, resolve_domain

    checklist = load_checklist()
    domains_block = "\n".join(
        f"    - {name}" + (f": {d.get('description', '')}" if d.get("description") else "")
        for name, d in checklist["domains"].items())
    subcats_block = "\n".join(
        f"      * {name}: " + "; ".join(
            f"{sub} ({desc})" if desc else sub
            for sub, desc in d["subcategories"].items())
        for name, d in checklist["domains"].items()
        if d.get("subcategories")) or "      (none)"
    prompt = READER_PROMPT.format(
        subject=(subject or "").strip()[:500],
        env_block=_format_env_detail(env_detail),
        description=(description or "").strip()[:_MAX_DESC_CHARS] or "(empty)",
        comments_block=_format_comments(_normalize_comments(comments)),
        attachments_block=_format_attachments(attachment_list),
        domains_block=domains_block,
        subcats_block=subcats_block,
    )
    try:
        raw = llm.chat(
            messages=[{"role": "user", "content": prompt}],
            system_content=("You are a meticulous Wi-Fi support engineer. "
                            "Output strict JSON only."),
        )
        res = _extract_json(raw)
    except Exception as e:
        print(f"[handsfree.case_reader] reader failed: {e}")
        return None

    times = [str(t).strip() for t in (res.get("issue_times") or []) if str(t).strip()]

    # Normalize the completeness assessment: known items only, deduped,
    # reasons capped. Tolerates bare-string entries ("repro_steps").
    known_items = ("issue_description", "issue_time", "repro_steps")
    missing: list[dict] = []
    seen: set = set()
    for entry in (res.get("missing_info") or []):
        if isinstance(entry, str):
            entry = {"item": entry, "reason": ""}
        if not isinstance(entry, dict):
            continue
        item = str(entry.get("item") or "").strip().lower()
        if item not in known_items or item in seen:
            continue
        seen.add(item)
        missing.append({"item": item,
                        "reason": str(entry.get("reason") or "").strip()[:200]})

    return {
        "action_owner": normalize_action_owner(res.get("action_owner")),
        "next_action": re.sub(r"\s+", " ", str(res.get("next_action") or "")).strip()[:300],
        "clean_description": str(res.get("clean_description") or "").strip(),
        "issue_times": times[:3],
        "issue_time_source": str(res.get("issue_time_source") or ""),
        "attachment_name": str(res.get("attachment_name") or "").strip(),
        "attachment_reason": str(res.get("attachment_reason") or ""),
        "reasoning": str(res.get("reasoning") or ""),
        "missing_info": missing,
        "issue_domain": resolve_domain(res.get("issue_domain")),
        "issue_subcategory": str(res.get("issue_subcategory") or "").strip(),
    }


def find_attachment(attachment_list: Any, name: str):
    """Locate the reader-chosen attachment entry by filename (exact, then
    case-insensitive, then substring either way). None when not found."""
    if not name or not attachment_list:
        return None
    items = list(attachment_list)
    for it in items:
        if str(it[0]) == name:
            return it
    low = name.lower()
    for it in items:
        if str(it[0]).lower() == low:
            return it
    for it in items:
        n = str(it[0]).lower()
        if low in n or n in low:
            return it
    return None
