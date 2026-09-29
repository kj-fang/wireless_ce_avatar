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


# ---------------------------------------------------------------------------
# Pre-fill
# ---------------------------------------------------------------------------

def _blank_fills(domain: str) -> dict:
    """{"general_info": [...], "required_log": [...], "required_info": [...]}
    where each entry is {"item", "example", "provided": False, "value": ""}."""
    data = load_checklist()
    dom = data["domains"].get(domain) or data["domains"][FALLBACK_DOMAIN]
    out = {"general_info": [dict(e, provided=False, value="")
                            for e in data["general_info"]]}
    for sec in ("required_log", "required_info"):
        out[sec] = [dict(e, provided=False, value="")
                    for e in dom.get(sec, [])]
    return out


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
    if chosen:
        note = chosen + (" (WRT logs extracted)" if log_ok else " (attached)")
        mark("required_log", r"WRT Log|WPP driver log", note)
    times = getattr(analysis, "issue_times", None) or []
    if times:
        mark("general_info", r"reproduction time|issue reproduction time",
             ", ".join(map(str, times[:3])))
        mark("required_info", r"exact time|time when", ", ".join(map(str, times[:3])))
    env = getattr(analysis, "env_detail", None) or {}
    for q, a in env.items():
        a = str(a or "").strip()
        if not a or a.upper() == "NA":
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

Output ONLY valid JSON: {{"fills": {{"<number>": {{"provided": true, "value": "<answer>"}}, ...}}}}
List only the items that ARE provided.

=== CHECKLIST ITEMS ===
{items_block}

=== CASE CONTENT ===
{case_block}
"""


def llm_fill(llm, fills: dict, case_material: str) -> None:
    """One LLM pass over the still-unfilled items. Mutates `fills`; any
    failure leaves items unfilled (the customer is simply asked again)."""
    todo: list[tuple[str, dict]] = [
        (sec, e) for sec in ("general_info", "required_log", "required_info")
        for e in fills.get(sec, []) if not e["provided"]]
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
    fills = _blank_fills(getattr(analysis, "issue_domain", "") or FALLBACK_DOMAIN)
    deterministic_fills(analysis, fills)
    material = "\n".join(filter(None, [
        str(getattr(analysis, "subject", "") or ""),
        str(getattr(analysis, "clean_description", "") or ""),
        str(getattr(analysis, "description", "") or ""),
        "\n".join(f"{q}: {a}" for q, a in
                  (getattr(analysis, "env_detail", None) or {}).items() if a),
    ]))
    llm_fill(llm, fills, material)
    return fills


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _render_entries(entries: list[dict]) -> list[str]:
    out = []
    for e in entries:
        if str(e["item"]).lower().startswith("note:"):
            out.append(f"  {e['item']}")
        elif e.get("provided"):
            out.append(f"  [x] {e['item']} — provided: {e['value']}")
        else:
            hint = f" ({e['example']})" if e.get("example") else ""
            out.append(f"  [ ] {e['item']} — please provide{hint}")
    return out


def render_checklist_body(domain: str, fills: dict) -> list[str]:
    """The checklist sections shared by first_response and the request modes."""
    data = load_checklist()
    dom = data["domains"].get(domain) or data["domains"][FALLBACK_DOMAIN]
    parts: list[str] = []
    parts.append("=== General Info ===")
    parts += _render_entries(fills.get("general_info", []))
    if fills.get("required_log"):
        parts += ["", f"=== Required Log ({domain}) ==="]
        parts += _render_entries(fills["required_log"])
    if fills.get("required_info"):
        parts += ["", f"=== Required Info ({domain}) ==="]
        parts += _render_entries(fills["required_info"])
    triage = dom.get("initial_triage") or []
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
        "marked [x] we already have from your report; please provide the "
        "items marked [ ] and go through the verification steps.",
        "",
    ]
    parts += render_checklist_body(domain, fills)
    parts += ["",
              "Thank you — we will proceed as soon as the missing items are "
              "available."]
    return parts
