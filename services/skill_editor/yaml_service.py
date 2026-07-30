"""Pure skill-editor YAML parsing, validation, and round-trip formatting."""

from __future__ import annotations

import re
import uuid


_CLOUD_YAML_HEADER = (
    "# skill features:\n"
    "#   name: skill name\n"
    "#   description: a brief description of the skill\n"
    "#   keywords: use \"-\" to represent each keyword\n"
    "#   expert_rules: use \"|\" to start a multi-line string\n"
    "\n"
)

_DISABLED_COMMENT_RE = re.compile(
    r"""^\s*\#\s*-\s*(['"])(?P<val>.+?)\1\s*$"""
)
_DISABLED_SKILL_RE = re.compile(r"^([A-Za-z0-9_][^:]*):\s*$")
_DISABLED_LIST_HEADER_RE = re.compile(
    r"^  (keywords|exclusive):\s*$"
)
_DISABLED_DEPTH2_RE = re.compile(r"^  \w+\s*:")

def read_yaml_file(path) -> dict:
    """Load a YAML file as a plain dict. Raises on parse failure."""
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(
            f"Invalid YAML structure: expected a dict, got {type(data).__name__}"
        )
    return data

def write_yaml_file(path, data: dict, disabled_comments: dict | None = None) -> None:
    """
    Write a dict to a YAML file using the same hand-authored layout the
    cloud baseline file uses, so files written from the side-panel editor
    are visually consistent with files maintained by the Wireless CE team.

    Conventions copied from the cloud `skills_<date>.yaml`:
      * Top-of-file schema comment block.
      * Scalar VALUES are double-quoted (mapping KEYS stay unquoted).
      * Lists indent one level deeper than their parent key
        (`  keywords:\\n    - "..."`).
      * Multi-line strings use the literal block scalar `|`.
      * Top-level skills are separated by a blank line.

    ``disabled_comments`` (optional) re-injects commented-out keyword /
    exclusive entries — yaml.safe_load drops comments on load, so this
    parameter is the bridge that keeps cloud-baseline "historically used
    but disabled" entries from disappearing on round-trip.
    Shape: ``{skill_key: {'keywords' | 'exclusive': [str, ...]}}``.
    """
    import yaml
    from pathlib import Path as _P

    class _CloudDumper(yaml.SafeDumper):
        # Track whether we're currently emitting a mapping KEY vs a VALUE
        # so the str representer can quote values without quoting keys.
        pass

    _CloudDumper._cloud_in_key = False  # type: ignore[attr-defined]

    def _str_representer(dumper, value):
        # Multi-line text → literal block style for readability.
        #
        # PyYAML silently FALLS BACK to a double-quoted scalar (with
        # embedded "\n" / "\t" escapes) whenever the input contains
        # characters the literal block style can't represent safely:
        #
        #   * line-internal trailing whitespace → rstrip each line
        #   * tab characters anywhere           → convert to 4 spaces
        #     (cloud-baseline `[ALON \t\t]` cosmetic alignment survives
        #      with spaces and looks the same in a monospace editor)
        #
        # Also append a final "\n" so the emitter uses "|" (clip) instead
        # of "|-" (strip), matching the hand-authored cloud baseline.
        if isinstance(value, str) and "\n" in value:
            value = value.replace("\t", "    ")
            value = "\n".join(line.rstrip() for line in value.split("\n"))
            if not value.endswith("\n"):
                value = value + "\n"
            return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")
        # Plain scalar for keys, quoted for values.
        if getattr(dumper, "_cloud_in_key", False):
            return dumper.represent_scalar("tag:yaml.org,2002:str", value)
        # For value scalars, flatten stray tabs so the YAML emitter never
        # has to fall back to escape-heavy quoting.
        cleaned = value.replace("\t", "    ")
        # Smart quote pick: when the content already contains double
        # quotes (e.g. PDF examples pasted by the user) but no single
        # quotes, use single-quoted YAML so we don't litter the output
        # with `\"...\"` escapes. Default to double-quoted otherwise to
        # match the cloud baseline's hand-authored convention.
        if '"' in cleaned and "'" not in cleaned:
            style = "'"
        else:
            style = '"'
        return dumper.represent_scalar("tag:yaml.org,2002:str", cleaned, style=style)

    _CloudDumper.add_representer(str, _str_representer)

    # Re-implement represent_mapping so KEYs go through the unquoted
    # path while VALUEs get the double-quote treatment.
    def _represent_mapping(self, tag, mapping, flow_style=None):
        value = []
        node = yaml.MappingNode(tag, value, flow_style=flow_style)
        if self.alias_key is not None:
            self.represented_objects[self.alias_key] = node
        best_style = True
        if hasattr(mapping, "items"):
            mapping = list(mapping.items())
        for item_key, item_value in mapping:
            self._cloud_in_key = True
            node_key = self.represent_data(item_key)
            self._cloud_in_key = False
            node_value = self.represent_data(item_value)
            if not (isinstance(node_key, yaml.ScalarNode) and not node_key.style):
                best_style = False
            if not (isinstance(node_value, yaml.ScalarNode) and not node_value.style):
                best_style = False
            value.append((node_key, node_value))
        if flow_style is None:
            if self.default_flow_style is not None:
                node.flow_style = self.default_flow_style
            else:
                node.flow_style = best_style
        return node
    _CloudDumper.represent_mapping = _represent_mapping

    # Indent list items so they sit ONE level deeper than the parent key
    # (i.e. never use indentless sequences).
    def _increase_indent(self, flow=False, indentless=False):
        return yaml.SafeDumper.increase_indent(self, flow, False)
    _CloudDumper.increase_indent = _increase_indent

    def _dump_one(skill_key: str, skill_val) -> str:
        return yaml.dump(
            {skill_key: skill_val},
            Dumper=_CloudDumper,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
            width=1000,
        ).rstrip("\n")

    if isinstance(data, dict):
        blocks = [_dump_one(k, v) for k, v in data.items()]
    else:
        blocks = [yaml.dump(
            data, Dumper=_CloudDumper, allow_unicode=True,
            sort_keys=False, default_flow_style=False, width=1000,
        ).rstrip("\n")]

    content = _CLOUD_YAML_HEADER + "\n\n".join(blocks) + "\n"

    # Re-inject any commented-out keyword / exclusive entries that the
    # caller asked us to preserve (`disabled_comments`). yaml.safe_load
    # drops comments on load, so we scan the cloud baseline / previous
    # user file separately and stitch them back in here.
    if disabled_comments:
        content = inject_disabled_comments(content, disabled_comments)

    p = _P(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(p)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass

def scan_disabled_comments(text: str) -> dict:
    """
    Walk a YAML text and pull out commented-out entries inside each skill's
    ``keywords:`` and ``exclusive:`` block. Returns:

        { skill_key: { 'keywords' | 'exclusive': [str, ...] } }
    """
    if not text:
        return {}
    result: dict = {}
    current_skill = None
    current_list = None
    for line in text.split("\n"):
        m = _DISABLED_SKILL_RE.match(line)
        if m:
            current_skill = m.group(1)
            current_list = None
            continue
        m = _DISABLED_LIST_HEADER_RE.match(line)
        if m:
            current_list = m.group(1)
            continue
        # Any other depth-2 mapping key terminates the current list block
        # so a comment far away isn't misattributed.
        if (current_list is not None
                and _DISABLED_DEPTH2_RE.match(line)
                and not _DISABLED_LIST_HEADER_RE.match(line)):
            current_list = None
            continue
        if current_skill and current_list:
            m = _DISABLED_COMMENT_RE.match(line)
            if m:
                result.setdefault(current_skill, {}) \
                      .setdefault(current_list, []) \
                      .append(m.group("val"))
    return result

def inject_disabled_comments(content: str, disabled: dict) -> str:
    """
    Walk the freshly-rendered YAML text and append ``# - "..."`` comment
    lines AFTER the last list item of each (skill, list_key) block whose
    disabled entries are still meaningful. Lines we know how to recognise:

      * skill header        — column-0 ``key:``  → starts a new skill
      * list header         — depth-2 ``keywords:`` / ``exclusive:``
      * list item           — depth-4 ``- "..."`` (current dumper uses 4)
      * any other depth-2 key — ends the current list block
    """
    if not disabled:
        return content

    lines = content.split("\n")
    # Pass 1: figure out, for each (skill, list_key) we have disabled
    # entries for, the line index AFTER which we should insert comments.
    insertions: dict = {}   # line_idx -> [str, ...]
    current_skill = None
    current_list = None
    last_list_item_idx = -1

    def _commit():
        nonlocal current_list, last_list_item_idx
        if current_skill and current_list:
            entries = disabled.get(current_skill, {}).get(current_list) or []
            if entries and last_list_item_idx >= 0:
                comments = [f'    # - "{v}"' for v in entries]
                insertions.setdefault(last_list_item_idx, []).extend(comments)
        current_list = None
        last_list_item_idx = -1

    for i, line in enumerate(lines):
        if _DISABLED_SKILL_RE.match(line):
            _commit()
            current_skill = _DISABLED_SKILL_RE.match(line).group(1)
            continue
        m_list = _DISABLED_LIST_HEADER_RE.match(line)
        if m_list:
            _commit()
            current_list = m_list.group(1)
            continue
        if (current_list is not None
                and _DISABLED_DEPTH2_RE.match(line)
                and not _DISABLED_LIST_HEADER_RE.match(line)):
            _commit()
            continue
        if current_list is not None and line.startswith("    - "):
            last_list_item_idx = i
    _commit()

    # Pass 2: rebuild text with the comments stitched in.
    if not insertions:
        return content
    out = []
    for i, line in enumerate(lines):
        out.append(line)
        if i in insertions:
            out.extend(insertions[i])
    return "\n".join(out)

def sanitise_skill_payload(skills_dict) -> tuple[dict, str]:
    """
    Validate the per-skill payload sent by the editor. Returns (cleaned, "")
    on success or ({}, error_message) on validation failure. Only known
    fields are persisted; the top-level skill key plus name + description
    must be non-empty strings; lists are coerced.
    """
    if not isinstance(skills_dict, dict) or not skills_dict:
        return ({}, "Request body must contain a non-empty 'skills' object.")

    allowed_keys = {"name", "description", "keywords", "exclusive", "expert_rules"}
    cleaned: dict = {}
    for key, val in skills_dict.items():
        if not isinstance(key, str) or not key.strip():
            return ({}, "Skill ID is required.")
        if not isinstance(val, dict):
            return ({}, f"Skill '{key}' must be an object.")

        row = {k: v for k, v in val.items() if k in allowed_keys}

        name = (row.get("name") or "").strip() if isinstance(row.get("name"), str) else ""
        desc = (row.get("description") or "").strip() if isinstance(row.get("description"), str) else ""
        if not name:
            return ({}, f"Skill '{key}': display name is required.")
        if not desc:
            return ({}, f"Skill '{key}': description is required.")
        row["name"] = name
        row["description"] = desc

        for list_key in ("keywords", "exclusive"):
            if list_key in row:
                v = row[list_key]
                # rstrip ONLY: leading whitespace can be load-matching-
                # critical (the cloud baseline uses entries like
                # " ------- RESUME FLOW" or " [prvDpTlcConfigSendTlcConfigCmd]"
                # where the leading space is part of the literal log
                # prefix). Trailing whitespace is almost always accidental
                # (user typed a trailing space after the keyword) and is
                # still cleaned.
                if isinstance(v, list):
                    raw_items = (str(x).rstrip() for x in v)
                elif v:
                    raw_items = (str(v).rstrip(),)
                else:
                    raw_items = ()
                cleaned_list = [s for s in raw_items if s]
                # Match the cloud baseline: omit the field entirely when
                # it has no entries (no `exclusive: []` placeholder).
                if cleaned_list:
                    row[list_key] = cleaned_list
                else:
                    row.pop(list_key, None)

        # expert_rules is stored in YAML as a single string. The structured
        # editor sends EITHER:
        #
        #   {"preamble": "free-form text", "items": ["1st", "2nd"]}
        #     → joined as:
        #         <preamble>
        #         1. 1st
        #         2. 2nd
        #
        #   "raw string"            (legacy — passed through verbatim)
        #   ["item1", "item2"]      (legacy — flat numbered list, no preamble)
        rules = row.get("expert_rules")
        if isinstance(rules, dict):
            # Items can be either:
            #   * a plain string  → auto-numbered with the next integer
            #   * a dict {prefix, text} → emitted with the original prefix
            #     verbatim (preserves cloud-baseline numbering such as
            #     "2-1.", "2-2.", "3-1." for section sub-steps)
            preamble = str(rules.get("preamble", "") or "")
            raw_items = rules.get("items") or []
            items: list[tuple[str | None, str]] = []
            if isinstance(raw_items, list):
                for entry in raw_items:
                    if isinstance(entry, dict):
                        pref = entry.get("prefix")
                        pref_str = str(pref).strip() if pref is not None else None
                        text = str(entry.get("text", "") or "")
                    else:
                        pref_str = None
                        text = str(entry)
                    if text.strip():
                        items.append((pref_str or None, text))

            # Assign auto-numbered integer prefixes to items that came in
            # without one. The next-int pool starts above the largest
            # explicit integer prefix already in use, so a list mixing
            # "1, 2-1, 2-2, 3" with one fresh entry will yield "4" — not
            # collide with an existing "2".
            max_int = 0
            for pref_str, _ in items:
                if pref_str and pref_str.isdigit():
                    try:
                        max_int = max(max_int, int(pref_str))
                    except ValueError:
                        pass

            parts: list[str] = []
            if preamble.strip():
                parts.append(preamble)
            for pref_str, text in items:
                if not pref_str:
                    max_int += 1
                    pref_str = str(max_int)
                parts.append(f"{pref_str}. {text}")
            joined = "\n".join(parts)
        elif isinstance(rules, list):
            items_str = [str(s).strip() for s in rules if str(s).strip()]
            joined = "\n".join(
                f"{i}. {item}" for i, item in enumerate(items_str, start=1)
            )
        elif isinstance(rules, str):
            joined = rules.strip()
        else:
            joined = ""
        # Expert rules are required. Reject the whole save if any skill
        # would end up with an empty rules block.
        if not joined.strip():
            return ({}, f"Skill '{key}': expert rules are required.")
        # Append a trailing newline whenever there is any content so the
        # str representer sees "\n" and emits the YAML literal block "|"
        # style — even when there's only a single short item. Without
        # this, "1. rfe" would round-trip as `expert_rules: "1. rfe"`
        # (double-quoted), inconsistent with every other skill.
        if not joined.endswith("\n"):
            joined += "\n"
        row["expert_rules"] = joined

        cleaned[key.strip()] = row

    if not cleaned:
        return ({}, "No valid skills found in the request body.")
    return (cleaned, "")
