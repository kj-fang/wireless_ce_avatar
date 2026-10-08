"""
Images embedded in IPS comments: find, download, describe (vision), inject.

The pipeline reads comments from Snowflake, which keeps the plain text only
— a screenshot pasted into a comment is just blank lines there. The IPS
rich-text body (REST) still holds the image links (Salesforce rtaImage
servlet), the app's Salesforce session can download them, and the Claude
model accepts images. So, per case:

  1. pull the rich-text comments via IpsClient.get_case_comments,
  2. download the embedded images (force.com hosts only, bounded),
  3. one vision call per comment with images -> one description per image,
  4. append a synthetic comment row "[Images embedded in the comment of
     <date> by <author>] 1) ... 2) ..." so the case reader, the checklist
     fill and the customer history see the content.

Descriptions are cached per comment id (<handsfree>/image_descriptions.json)
so a re-run of the case costs no extra vision tokens.
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable, Optional

MAX_IMAGES_PER_CASE = 8
MAX_IMAGE_BYTES = 5 * 1024 * 1024
_ALLOWED_MEDIA = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_ALLOWED_HOSTS = (".force.com", ".salesforce.com")
_CACHE_LOCK = threading.Lock()

_IMG_RE = re.compile(r"""<img\b[^>]*\bsrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def extract_image_urls(rich_html: str) -> list[str]:
    """src of every <img> in a rich-text body, HTML entities decoded."""
    return [html.unescape(u) for u in _IMG_RE.findall(str(rich_html or ""))]


def sniff_media_type(data: bytes, declared: str = "") -> str:
    """Real image type from the magic bytes - the force.com image servlet
    declares image/png for JPEG uploads, and the vision API rejects a
    mismatch. Falls back to the declared type."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return declared


def _host_allowed(url: str) -> bool:
    m = re.match(r"https?://([^/]+)/", url + "/")
    host = (m.group(1) if m else "").lower()
    return any(host.endswith(h) for h in _ALLOWED_HOSTS)


def image_blocks(images: list[tuple[str, bytes]], model: str) -> list[dict]:
    """Message content blocks for the images, in the format the configured
    client expects (Anthropic base64 blocks for claude-*, OpenAI image_url
    data URLs otherwise)."""
    blocks = []
    for n, (media_type, data) in enumerate(images, 1):
        b64 = base64.b64encode(data).decode("ascii")
        blocks.append({"type": "text", "text": f"Image {n}:"})
        if str(model or "").startswith("claude"):
            blocks.append({"type": "image",
                           "source": {"type": "base64", "media_type": media_type,
                                      "data": b64}})
        else:
            blocks.append({"type": "image_url",
                           "image_url": {"url": f"data:{media_type};base64,{b64}"}})
    return blocks


_DESCRIBE_PROMPT = """\
You are reading an Intel Wi-Fi support case. The comment below, written by
{author} on {date}, embeds {n} image(s) (screenshots). For EACH image, in
order, describe concisely what it shows and every fact useful for debugging:
the tool/app or dialog shown, settings and their values, error text or codes,
timestamps, log/file names, versions, test results, and any annotations or
instructions drawn on it. Transcribe text that matters exactly. At most 120
words per image. Do not guess at what is not visible.

Output ONLY valid JSON: {{"images": ["<description of image 1>", "..."]}}

=== COMMENT TEXT ===
{text}
"""


def describe_images(llm, images: list[tuple[str, bytes]], *, author: str,
                    date: str, comment_text: str) -> list[str]:
    """One vision call; a description per image (empty strings on failure)."""
    if not images or llm is None:
        return ["" for _ in images]
    prompt = _DESCRIBE_PROMPT.format(author=author or "unknown", date=date or "?",
                                     n=len(images), text=(comment_text or "")[:2000])
    content = [{"type": "text", "text": prompt}] + image_blocks(
        images, getattr(llm, "model", ""))
    try:
        raw = llm.chat(messages=[{"role": "user", "content": content}],
                       system_content="Output strict JSON only.")
        try:
            from services.ace.roles import _extract_json
            res = _extract_json(raw)
        except Exception:
            res = json.loads(re.search(r"\{.*\}", raw, re.DOTALL).group(0))
        descs = [re.sub(r"\s+", " ", str(d or "")).strip()[:900]
                 for d in (res.get("images") or [])]
    except Exception as e:
        print(f"[handsfree.images] vision call failed: {e}")
        descs = []
    descs = descs[:len(images)] + [""] * max(0, len(images) - len(descs))
    return descs


class _Cache:
    def __init__(self, path: Optional[Path]):
        self.path = path

    def load(self) -> dict:
        try:
            if self.path and self.path.exists():
                return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[handsfree.images] cache read failed: {e}")
        return {}

    def save(self, data: dict) -> None:
        if not self.path:
            return
        try:
            with _CACHE_LOCK:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False),
                               encoding="utf-8")
                os.replace(tmp, self.path)
        except Exception as e:
            print(f"[handsfree.images] cache write failed: {e}")


def _text_of(rich_html: str) -> str:
    t = re.sub(r"<br\s*/?>|</p>", "\n", str(rich_html or ""), flags=re.IGNORECASE)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"[ \t]+", " ", t).strip()


def annotate_case_comments(ips, llm, case_id: str, comments: Any, *,
                           cache_path: Optional[Path] = None,
                           progress: Optional[Callable[[str], None]] = None,
                           skip_marker: str = "") -> tuple[list, list[dict]]:
    """-> (comment rows + synthetic image rows, details). Rows keep the
    [ts, author_type, text] shape the reader expects. `skip_marker`: comments
    containing it (our own AI posts) are not read."""
    say = progress or (lambda m: None)
    rows = list(comments or [])
    details: list[dict] = []
    if not case_id or ips is None:
        return rows, details
    rest = ips.get_case_comments(case_id)          # newest first, <= 50
    cache = _Cache(cache_path)
    cached = cache.load()
    budget = MAX_IMAGES_PER_CASE
    changed = False
    for c in sorted(rest, key=lambda r: r.get("CreatedDate") or ""):
        rich = str(c.get(getattr(ips, "FIELD_RICH_BODY", "Core_IPS_Rich_Comment__c")) or "")
        urls = [u for u in extract_image_urls(rich) if _host_allowed(u)]
        if not urls or (skip_marker and skip_marker in rich):
            continue
        cid = str(c.get("Id") or "")
        author = str(c.get("Core_IPS_Comment_Author_Type__c") or "")
        date = str(c.get("CreatedDate") or "")
        entry = cached.get(cid)
        if entry and entry.get("url_count") == len(urls):
            descs = entry["descriptions"]
            say(f"{len(urls)} image(s) in the {date[:10]} comment by {author}: cached")
        else:
            if budget <= 0:
                say(f"image budget ({MAX_IMAGES_PER_CASE}) exhausted — skipping "
                    f"{len(urls)} image(s) in the {date[:10]} comment")
                continue
            images: list[tuple[str, bytes]] = []
            for u in urls[:budget]:
                got = ips.fetch_binary(u, max_bytes=MAX_IMAGE_BYTES)
                if got:
                    media = sniff_media_type(got[1], got[0])
                    if media in _ALLOWED_MEDIA:
                        images.append((media, got[1]))
            budget -= len(images)
            if not images:
                say(f"could not download the image(s) in the {date[:10]} comment")
                continue
            say(f"describing {len(images)} image(s) in the {date[:10]} comment by {author}")
            descs = describe_images(llm, images, author=author, date=date[:16],
                                    comment_text=_text_of(rich))
            if any(descs):
                cached[cid] = {"descriptions": descs, "url_count": len(urls),
                               "date": date, "author": author}
                changed = True
        descs = [d for d in descs if d]
        if not descs:
            continue
        text = (f"[Images embedded in the comment of {date[:16]} by {author or 'unknown'}] "
                + " ".join(f"{i}) {d}" for i, d in enumerate(descs, 1)))
        rows.append([date, author, text])
        details.append({"comment_id": cid, "date": date, "author": author,
                        "count": len(urls), "descriptions": descs})
    if changed:
        cache.save(cached)
    return rows, details
