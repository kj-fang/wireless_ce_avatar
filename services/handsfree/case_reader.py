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

Output ONLY a valid JSON object (no markdown, no code fences):
{{
  "clean_description": "<one-paragraph current issue statement>",
  "issue_times": ["<time exactly as written>", "..."],
  "issue_time_source": "<'description' or 'comment #N' that stated the time>",
  "attachment_name": "<EXACT filename from the ATTACHMENTS list, or '' if none fits>",
  "attachment_reason": "<one sentence: why this attachment matches the issue time>",
  "reasoning": "<2-4 sentences tracing how the comments changed the picture>"
}}

=== SUBJECT ===
{subject}

=== ISSUE DESCRIPTION ===
{description}

=== COMMENTS (chronological; #1 is oldest) ===
{comments_block}

=== ATTACHMENTS (candidate log uploads) ===
{attachments_block}
"""


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
                      comments: Any, attachment_list: Any) -> Optional[dict]:
    """One LLM call. Returns the parsed reader dict, or None on any failure
    (callers fall back to description-only organize_issue_context)."""
    from services.ace.roles import _extract_json

    prompt = READER_PROMPT.format(
        subject=(subject or "").strip()[:500],
        description=(description or "").strip()[:_MAX_DESC_CHARS] or "(empty)",
        comments_block=_format_comments(_normalize_comments(comments)),
        attachments_block=_format_attachments(attachment_list),
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
    return {
        "clean_description": str(res.get("clean_description") or "").strip(),
        "issue_times": times[:3],
        "issue_time_source": str(res.get("issue_time_source") or ""),
        "attachment_name": str(res.get("attachment_name") or "").strip(),
        "attachment_reason": str(res.get("attachment_reason") or ""),
        "reasoning": str(res.get("reasoning") or ""),
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
