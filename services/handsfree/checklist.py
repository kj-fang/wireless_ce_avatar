"""
Issue-type debug checklist: load, domain resolution, pre-fill, rendering.

Backed by services/handsfree/checklist_data/debug_checklist.json (generated from the CE
team's "Wi-Fi SW Issue Debugging Checklist" xlsx by checklist_convert.py).

Used for the customer-facing FIRST RESPONSE on every case: the identified
domain's Required Log / Required Info items are rendered as a checklist,
pre-filled ([x] + value) for whatever the customer already provided —
deterministic pipeline facts first, one LLM pass for the free-text rest —
and the domain's Initial Triage items become "please verify" steps.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Optional

_DATA_PATH = Path(__file__).parent / "checklist_data" / "debug_checklist.json"
_cache: Optional[dict] = None

FALLBACK_DOMAIN = "Others"

# classification/issue_type wordings seen in the app -> checklist tab names.
_DOMAIN_ALIASES = {
    "yb": "Yellow Bang",
    "yb/lost": "Yellow Bang",
    "yellow bang (yb)": "Yellow Bang",
    "device lost": "Yellow Bang",
    "wowlan": "WowLAN",
    "wake on wlan": "WowLAN",
    "p2p": "P2P (Miracast)",
    "miracast": "P2P (Miracast)",
    "throughput": "Performance",
    "power consumption": "Power Consumption (MS)",
    "sleepstudy": "Power Consumption (MS)",
    "hang": "System Hang",
}


def load_checklist() -> dict:
    global _cache
    if _cache is None:
        _cache = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    return _cache


def domain_names() -> list[str]:
    return list(load_checklist()["domains"].keys())


def resolve_domain(name: str) -> str:
    """Map a free-form issue type / domain wording onto a checklist tab name.
    Unknown or empty -> FALLBACK_DOMAIN."""
    raw = re.sub(r"\s+", " ", str(name or "")).strip()
    if not raw:
        return FALLBACK_DOMAIN
    low = raw.lower()
    domains = load_checklist()["domains"]
    for d in domains:
        if d.lower() == low:
            return d
    if low in _DOMAIN_ALIASES:
        return _DOMAIN_ALIASES[low]
    # substring match either way ("Connectivity issue" -> Connectivity)
    for d in domains:
        if d.lower() in low or low in d.lower():
            return d
    for alias, d in _DOMAIN_ALIASES.items():
        if alias in low:
            return d
    return FALLBACK_DOMAIN


def domain_subcategories(domain: str) -> dict:
    """{name: description} for the domain's sub-categories ({} when flat)."""
    dom = load_checklist()["domains"].get(domain) or {}
    return dict(dom.get("subcategories") or {})


# Domains whose sub-categories are peers with no sensible default (value =
# the noun used in the ask). When the case names none of them the reply
# ASKS which one applies instead of guessing — a wrong guess would request
# another tool's logs.
_SUBCAT_ASK = {"OEM Tools": "OEM tool"}


def resolve_subcategory(domain: str, name: str = "", context_text: str = "") -> str:
    """Pick the applicable sub-category for a domain (e.g. Connectivity ->
    Connectivity/Scan/Roaming). Order: explicit name match -> keyword hit in
    the case text -> the sub-category named like the domain -> first one.
    Returns "" for domains without sub-categories, and for _SUBCAT_ASK
    domains when nothing matched (the reply then asks which one)."""
    subs = domain_subcategories(domain)
    if not subs:
        return ""

    def _word(sub: str, text: str) -> bool:
        return bool(re.search(rf"\b{re.escape(sub.lower())}\b", text))

    raw = re.sub(r"\s+", " ", str(name or "")).strip().lower()
    for sub in subs:
        if not raw:
            break
        # Acronym sub-categories (OEM tools: ANT, DRTU, ...) match as whole
        # words only — "ant" must not hit "constant" / "antenna".
        if sub.isupper():
            if _word(sub, raw):
                return sub
        elif sub.lower() == raw or sub.lower() in raw or raw in sub.lower():
            return sub
    text = str(context_text or "").lower()
    if text:
        # most-specific keyword first (e.g. "roam"/"scan" beat generic connect)
        for sub in subs:
            if sub.lower() == domain.lower():
                continue
            if sub.isupper():
                if _word(sub, text):
                    return sub
                continue
            root = sub.lower().rstrip("gmi")[:4] if len(sub) > 4 else sub.lower()
            if root and root in text:
                return sub
    for sub in subs:
        if sub.lower() == domain.lower():
            return sub
    if domain in _SUBCAT_ASK:
        return ""
    return next(iter(subs))


def subcategory_ask(domain: str, subcat: str) -> str:
    """The 'which one?' question for a _SUBCAT_ASK domain whose sub-category
    could not be identified; "" otherwise."""
    if subcat or domain not in _SUBCAT_ASK:
        return ""
    names = " / ".join(domain_subcategories(domain))
    noun = _SUBCAT_ASK[domain]
    return (f"Which {noun} is the issue with ({names})? — please provide "
            f"(the required logs and verification steps differ per {noun})")


def _entry_in_subcat(entry: dict, subcat: str) -> bool:
    return entry.get("subcat") in (None, "", subcat)


# ---------------------------------------------------------------------------
# Pre-fill
# ---------------------------------------------------------------------------

def _blank_fills(domain: str, subcat: str = "") -> dict:
    """{"general_info": [...], "required_log": [...], "required_info": [...]}
    where each entry is {"item", "example", "provided": False, "value": ""}.
    For sub-categorized domains only the chosen sub-category's items are
    included (entries without a subcat tag always are)."""
    data = load_checklist()
    dom = data["domains"].get(domain) or data["domains"][FALLBACK_DOMAIN]
    out = {"general_info": [dict(e, provided=False, value="")
                            for e in data["general_info"]]}
    for sec in ("required_log", "required_info"):
        out[sec] = [dict(e, provided=False, value="")
                    for e in dom.get(sec, []) if _entry_in_subcat(e, subcat)]
    return out


# Form answers that mean "not provided" (same set the runner rejects).
_PLACEHOLDER_ANSWERS = {"na", "n/a", "none"}


def deterministic_fills(analysis, fills: dict) -> None:
    """Mark items the PIPELINE itself can vouch for. Mutates `fills`."""
    def mark(section: str, pattern: str, value: str):
        rx = re.compile(pattern, re.IGNORECASE)
        for e in fills.get(section, []):
            if rx.search(e["item"]) and not e["provided"] and value:
                e["provided"] = True
                e["value"] = value

    chosen = getattr(analysis, "chosen_attachment", "") or ""
    log_ok = bool(getattr(analysis, "log_path", "") or "")
    # mode request_logs = the pipeline itself found the logs missing or the
    # archive unreadable — never tick the item that same reply asks for.
    if chosen and getattr(analysis, "mode", "") != "request_logs":
        note = chosen + (" (WRT logs extracted)" if log_ok else " (attached)")
        mark("required_log", r"WRT Log|WPP driver log", note)
    # issue_times may be the runner's last-resort fallback to the attachment
    # upload time — that is not a customer-stated reproduction time. Domain
    # "exact time" questions (e.g. WowLAN wake-trigger time) are a different
    # fact from the failure time: left to the LLM pass.
    att_time = str(getattr(analysis, "attachment_time", "") or "")
    times = [str(t) for t in (getattr(analysis, "issue_times", None) or [])
             if str(t) != att_time]
    if times:
        mark("general_info", r"reproduction time|issue reproduction time",
             ", ".join(times[:3]))
    env = getattr(analysis, "env_detail", None) or {}
    for q, a in env.items():
        a = str(a or "").strip()
        if not a or a.lower() in _PLACEHOLDER_ANSWERS:
            continue
        if re.search(r"steps to reproduce", str(q), re.IGNORECASE):
            mark("general_info", r"reproduction steps", a[:200])
        if re.search(r"frequency", str(q), re.IGNORECASE):
            mark("general_info", r"reproduction rate", a[:100])


_FILL_PROMPT = """\
You are reviewing an Intel Wi-Fi support case to pre-fill a debug checklist
for the customer. For EVERY numbered item below, decide whether the case
content ALREADY answers it. Only mark provided=true when the case clearly
states the answer; copy the answer concisely (<=160 chars). Do not guess.
Customer comments are listed oldest first — a later comment supersedes the
original description.

Output ONLY valid JSON: {{"fills": {{"<number>": {{"provided": true, "value": "<answer>"}}, ...}}}}
List only the items that ARE provided.

=== CHECKLIST ITEMS ===
{items_block}

=== CASE CONTENT ===
{case_block}
"""


_WRT_ITEM_RE = re.compile(r"WRT Log|WPP driver log", re.IGNORECASE)


def llm_fill(llm, fills: dict, case_material: str, *,
             lock_wrt_items: bool = False) -> None:
    """One LLM pass over the still-unfilled items. Mutates `fills`; any
    failure leaves items unfilled (the customer is simply asked again).
    lock_wrt_items: the pipeline established that no usable WRT/WPP log was
    provided (request_logs) — the text pass must not tick those items from
    an attachment label, or the reply would contradict itself."""
    todo: list[tuple[str, dict]] = [
        (sec, e) for sec in ("general_info", "required_log", "required_info")
        for e in fills.get(sec, []) if not e["provided"]
        and not (lock_wrt_items and _WRT_ITEM_RE.search(e["item"]))]
    if not todo or llm is None:
        return
    items_block = "\n".join(f"{i + 1}. {e['item']}" for i, (_, e) in enumerate(todo))
    prompt = _FILL_PROMPT.format(items_block=items_block,
                                 case_block=case_material[:6000])
    try:
        from services.ace.roles import _extract_json  # noqa: F401 (heavy)
    except Exception:
        _extract_json = None
    try:
        raw = llm.chat(messages=[{"role": "user", "content": prompt}],
                       system_content="Output strict JSON only.")
        if _extract_json is not None:
            res = _extract_json(raw)
        else:
            res = json.loads(re.search(r"\{.*\}", raw, re.DOTALL).group(0))
        for num, info in (res.get("fills") or {}).items():
            try:
                idx = int(num) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(todo) and isinstance(info, dict) and info.get("provided"):
                val = str(info.get("value") or "").strip()[:160]
                if val:
                    todo[idx][1]["provided"] = True
                    todo[idx][1]["value"] = val
    except Exception as e:
        print(f"[handsfree.checklist] llm fill skipped: {e}")


def build_fills(analysis, llm=None) -> dict:
    """Deterministic pipeline facts first, then one LLM pass for the rest."""
    domain = getattr(analysis, "issue_domain", "") or FALLBACK_DOMAIN
    subcat = resolve_subcategory(
        domain, getattr(analysis, "issue_subcategory", ""),
        " ".join([str(getattr(analysis, "clean_description", "") or ""),
                  str(getattr(analysis, "description", "") or ""),
                  str(getattr(analysis, "subject", "") or "")]))
    try:
        analysis.issue_subcategory = subcat
    except Exception:
        pass
    fills = _blank_fills(domain, subcat)
    deterministic_fills(analysis, fills)
    # The filled values post PUBLICLY, so the fill pass may only read what
    # the customer wrote: subject, description, the Environment Details form
    # and customer-authored comments. NOT clean_description — the reader
    # synthesizes it from ALL comments, Private-to-Intel ones included.
    history = str(getattr(analysis, "customer_history", "") or "")
    material = "\n".join(filter(None, [
        str(getattr(analysis, "subject", "") or ""),
        str(getattr(analysis, "description", "") or "")[:2500],
        "\n".join(f"{q}: {a}" for q, a in
                  (getattr(analysis, "env_detail", None) or {}).items() if a)[:1000],
        ("=== CUSTOMER COMMENTS (oldest first) ===\n" + history) if history else "",
    ]))
    llm_fill(llm, fills, material,
             lock_wrt_items=getattr(analysis, "mode", "") == "request_logs")
    return fills


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _render_entries(entries: list[dict]) -> list[str]:
    """Checked items first (easier to scan), then the asks, notes last."""
    checked, unchecked, notes = [], [], []
    for e in entries:
        if str(e["item"]).lower().startswith("note:"):
            notes.append(f"  {e['item']}")
        elif e.get("provided"):
            checked.append(f"  [✓] {e['item']} — provided: {e['value']}")
        else:
            hint = f" ({e['example']})" if e.get("example") else ""
            unchecked.append(f"  [ ] {e['item']} — please provide{hint}")
    return checked + unchecked + notes


def render_checklist_body(domain: str, fills: dict, subcat: str = "") -> list[str]:
    """The checklist sections shared by first_response and the request modes."""
    data = load_checklist()
    dom = data["domains"].get(domain) or data["domains"][FALLBACK_DOMAIN]
    label = f"{domain} — {subcat}" if subcat and subcat != domain else domain
    parts: list[str] = []
    parts.append("=== General Info ===")
    parts += _render_entries(fills.get("general_info", []))
    if fills.get("required_log"):
        parts += ["", f"=== Required Log ({label}) ==="]
        parts += _render_entries(fills["required_log"])
    info_lines = _render_entries(fills.get("required_info", []))
    ask = subcategory_ask(domain, subcat)
    if ask:
        info_lines.insert(0, f"  [ ] {ask}")
    if info_lines:
        parts += ["", f"=== Required Info ({label}) ==="] + info_lines
    triage = [e for e in (dom.get("initial_triage") or [])
              if _entry_in_subcat(e, subcat)]
    if triage:
        parts += ["", "=== Please verify (initial triage) ==="]
        for e in triage:
            hint = f" ({e['example']})" if e.get("example") else ""
            parts.append(f"  - {e['item']}{hint}")
    return parts


def render_first_response(analysis, fills: dict) -> list[str]:
    domain = getattr(analysis, "issue_domain", "") or FALLBACK_DOMAIN
    data = load_checklist()
    desc = (data["domains"].get(domain) or {}).get("description", "")
    subject = getattr(analysis, "subject", "") or ""
    parts = [
        "Hello,",
        "",
        "Thank you for reporting this issue"
        + (f" ({subject})" if subject else "") + ".",
        "",
        f"Based on the report, we are treating this as a {domain} issue"
        + (f" ({desc})" if desc else "") + ".",
        "To speed up debugging, please review the checklist below: items "
        "marked [✓] we already have from your report; please provide "
        "the items marked [ ] and go through the verification steps.",
        "",
    ]
    parts += render_checklist_body(domain, fills,
                                   getattr(analysis, "issue_subcategory", ""))
    parts += ["",
              "Thank you — we will proceed as soon as the missing items are "
              "available."]
    return parts
