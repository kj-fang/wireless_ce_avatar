"""
Skill YAML import helper.

Shared between the Wi-Fi (log_chatbot / nw_analysis) and Bluetooth
(bt_chatbot) blueprints for the "Add Skills from YAML" side-panel action.
Handles only the pure-Python parts:

  * parse the source YAML the user picked,
  * keep only the five recognised skill fields
    (name, description, keywords, exclusive, expert_rules),
  * drop skills that are missing the required `name` or `expert_rules`,
  * merge into an existing skills dict with overwrite-on-duplicate.

Disk I/O and agent-refresh remain in the blueprint routes so each
blueprint can keep using its own hand-tuned YAML writer / agent state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


_ALLOWED_FIELDS = ("name", "description", "keywords", "exclusive", "expert_rules")
_REQUIRED_FIELDS = ("name", "description", "expert_rules") 


def _clean_field(key: str, value: Any) -> Any:
    """Normalise a single field: coerce lists, keep strings, drop None."""
    if key in ("keywords", "exclusive"):
        if value is None:
            return []
        if isinstance(value, list):
            out = [str(x) for x in value if str(x).strip() != ""]
        else:
            s = str(value).strip()
            out = [s] if s else []
        return out
    if key == "expert_rules":
        if value is None:
            return ""
        text = str(value)
        # PyYAML emits `|` block scalars only when the value ends in "\n".
        if text and not text.endswith("\n"):
            text += "\n"
        return text
    if key in ("name", "description"):
        return str(value).strip() if value is not None else ""
    return value


def _filter_skill(raw: Any) -> tuple[dict | None, str]:
    """
    Return (cleaned_dict, "") on success or (None, reason) if the skill
    lacks required fields.
    """
    if not isinstance(raw, dict):
        return (None, "not a mapping")

    cleaned: dict = {}
    for k in _ALLOWED_FIELDS:
        if k not in raw:
            continue
        v = _clean_field(k, raw[k])
        # Drop empty list fields (no `exclusive: []` placeholder).
        if k in ("keywords", "exclusive") and not v:
            continue
        cleaned[k] = v

    for req in _REQUIRED_FIELDS:
        val = cleaned.get(req)
        if not val or (isinstance(val, str) and not val.strip()):
            return (None, f"missing required field '{req}'")

    return (cleaned, "")


def load_and_filter_source(source_yaml_path: str | Path) -> tuple[dict, dict]:
    """
    Parse `source_yaml_path` and return (valid_skills, skipped).

    `valid_skills` is `{skill_key: {name, description?, keywords?, exclusive?, expert_rules}}`.
    `skipped` is `{skill_key: reason_str}` for entries that failed validation.
    Raises `FileNotFoundError` or `ValueError` on unusable input.
    """
    import yaml

    path = Path(source_yaml_path)
    if not path.exists():
        raise FileNotFoundError(f"YAML file not found: {source_yaml_path}")
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if raw is None:
        raise ValueError("YAML file is empty.")
    if not isinstance(raw, dict):
        raise ValueError(
            f"Invalid YAML structure: expected a mapping of skills, "
            f"got {type(raw).__name__}."
        )

    valid: dict = {}
    skipped: dict = {}
    for key, val in raw.items():
        if not isinstance(key, str) or not key.strip():
            skipped[str(key)] = "skill key is empty or non-string"
            continue
        cleaned, reason = _filter_skill(val)
        if cleaned is None:
            skipped[key] = reason
            continue
        valid[key.strip()] = cleaned

    return (valid, skipped)


def merge_overwrite(existing: dict, incoming: dict) -> tuple[dict, list[str], list[str]]:
    """
    Merge `incoming` into `existing`. Duplicate keys are OVERWRITTEN.
    Returns (merged_dict, added_keys, overwritten_keys).
    """
    merged = dict(existing) if isinstance(existing, dict) else {}
    added: list[str] = []
    overwritten: list[str] = []
    for k, v in incoming.items():
        if k in merged:
            overwritten.append(k)
        else:
            added.append(k)
        merged[k] = v
    return (merged, added, overwritten)
