"""
ACE Playbook data model.

A playbook is an evolving, itemised list of `Bullet`s organised into sections.
Each bullet carries:
  - a stable id  (e.g. "conn-00042")  used by the Generator to cite it and by
    the Reflector to tag it helpful/harmful;
  - helpful/harmful counters that drive REMOVE decisions;
  - a section tag that the Curator MUST set;
  - free-text content (a single actionable rule).

This module does the LLM-free parts: storage, id allocation, counter updates,
de-duplication, and rendering into a string the Generator can read.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Optional

from .prompts import WORKFLOW_SECTIONS, DOMAIN_SECTIONS

# Dedup threshold: two bullets whose content overlap by this much (ratio in
# [0,1]) are considered duplicates. Tuned to be permissive — false positives
# just merge counters, false negatives leave near-duplicates in the playbook.
DEDUP_RATIO = 0.85

# Soft cap on bullets per section. When exceeded, the lowest-net-score bullets
# are evicted by `refine()`. Kept tight so the Generator reads a compact,
# high-signal playbook instead of a sprawling list.
SECTION_SOFT_CAP = 15

# Aging eviction: a bullet that has NEVER proved helpful, has been marked
# neutral at least this many times, and has not changed in STALE_DAYS days is
# dead weight — refine() drops it so the playbook self-prunes.
NEUTRAL_EVICT_THRESHOLD = 5
STALE_DAYS = 30


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _age_days(ts: str) -> float:
    """Days elapsed since an ISO timestamp; 0.0 when unparseable."""
    if not ts:
        return 0.0
    try:
        dt = datetime.fromisoformat(ts)
    except Exception:
        return 0.0
    now = datetime.now().astimezone()
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return (now - dt).total_seconds() / 86400.0


@dataclass
class Bullet:
    id: str
    section: str
    content: str
    helpful_count: int = 0
    harmful_count: int = 0
    neutral_count: int = 0
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    source_turn_ids: list[str] = field(default_factory=list)

    @property
    def net_score(self) -> int:
        return self.helpful_count - self.harmful_count

    def render(self) -> str:
        # One-line format the Generator sees. Matches the paper convention:
        #   [bullet_id] helpful=H harmful=K :: content
        return (
            f"[{self.id}] helpful={self.helpful_count} "
            f"harmful={self.harmful_count} :: {self.content}"
        )


class Playbook:
    """
    One playbook scope. Two are typically created:

        workflow  scope ("agent")          — id prefix "agent-"
        domain    scope per skill name      — id prefix derived from skill name
                                              (e.g. "conn-" for Connectivity)

    Persisted as a single JSON file at `path`.
    """

    # Short ID prefixes per skill. Anything missing here falls back to a
    # 4-char slug of the skill name.
    _SKILL_PREFIX = {
        "agent":                "agent",
        "Connectivity":         "conn",
        "Roaming":              "roam",
        "BSOD":                 "bsod",
        "Yellow_Bang":          "yb",
        "DSM":                  "dsm",
        "VLP/UHB/AFC":          "vlp",
        "Sensing":              "sens",
        "P2P":                  "p2p",
        "WRDS/WGDS/EWRD/SGOM":  "sar",
        "Assert":               "asrt",
        "PPAG":                 "ppag",
        "TAS":                  "tas",
        "UATS":                 "uats",
        "MLO":                  "mlo",
        "Unclassified":         "misc",
    }

    def __init__(self, scope: str, path: Path):
        """
        scope: "agent" for the workflow playbook, or a skill name (e.g.
               "Connectivity") for a per-skill domain playbook.
        path:  JSON file backing this playbook.
        """
        self.scope = scope
        self.path = Path(path)
        self.bullets: list[Bullet] = []
        self._lock = threading.RLock()
        self._next_seq = 1
        self._loaded_mtime: float = 0.0
        self.load()

    # ----- prefix / id allocation -----
    def _id_prefix(self) -> str:
        if self.scope in self._SKILL_PREFIX:
            return self._SKILL_PREFIX[self.scope]
        # Fallback: slug of the scope (lowercase alnum, first 4 chars)
        slug = re.sub(r"[^a-z0-9]+", "", self.scope.lower())[:4] or "x"
        return slug

    def _allocate_id(self) -> str:
        prefix = self._id_prefix()
        next_id = f"{prefix}-{self._next_seq:05d}"
        self._next_seq += 1
        return next_id

    # ----- allowed sections -----
    def _allowed_sections(self) -> list[str]:
        return WORKFLOW_SECTIONS if self.scope == "agent" else DOMAIN_SECTIONS

    def validate_section(self, section: str) -> str:
        """Map an arbitrary section string to an allowed one; fall back to first."""
        if section in self._allowed_sections():
            return section
        # Best-effort fuzzy match — handles minor curator drift on section names.
        best = max(
            self._allowed_sections(),
            key=lambda s: SequenceMatcher(None, s, section).ratio(),
        )
        return best

    # ----- mutation -----
    def add(self, section: str, content: str, source_turn_id: Optional[str] = None) -> Bullet:
        """Add a bullet. If a near-duplicate exists, increment its helpful_count instead."""
        content = (content or "").strip()
        if not content:
            raise ValueError("Bullet content is empty")
        section = self.validate_section(section)

        with self._lock:
            dup = self._find_duplicate(section, content)
            if dup is not None:
                dup.helpful_count += 1
                dup.updated_at = _now()
                if source_turn_id and source_turn_id not in dup.source_turn_ids:
                    dup.source_turn_ids.append(source_turn_id)
                return dup

            b = Bullet(
                id=self._allocate_id(),
                section=section,
                content=content,
                source_turn_ids=[source_turn_id] if source_turn_id else [],
            )
            self.bullets.append(b)
            return b

    def update(self, bullet_id: str, new_content: str) -> Optional[Bullet]:
        with self._lock:
            b = self.get(bullet_id)
            if b is None:
                return None
            b.content = new_content.strip()
            b.updated_at = _now()
            return b

    def remove(self, bullet_id: str, reason: str = "") -> bool:
        with self._lock:
            for i, b in enumerate(self.bullets):
                if b.id == bullet_id:
                    self.bullets.pop(i)
                    return True
        return False

    def get(self, bullet_id: str) -> Optional[Bullet]:
        for b in self.bullets:
            if b.id == bullet_id:
                return b
        return None

    def increment_counter(self, bullet_id: str, tag: str, weight: int = 1) -> None:
        """tag in {'helpful', 'harmful', 'neutral'}. `weight` scales the bump
        so a detailed, high-confidence feedback submission moves the counter
        more than a bare thumbs vote."""
        b = self.get(bullet_id)
        if b is None:
            return
        step = max(1, int(weight))
        if tag == "helpful":
            b.helpful_count += step
        elif tag == "harmful":
            b.harmful_count += step
        elif tag == "neutral":
            b.neutral_count += step
        b.updated_at = _now()

    def _find_duplicate(self, section: str, content: str) -> Optional[Bullet]:
        for b in self.bullets:
            if b.section != section:
                continue
            ratio = SequenceMatcher(None, b.content.lower(), content.lower()).ratio()
            if ratio >= DEDUP_RATIO:
                return b
        return None

    # ----- grow-and-refine -----
    def refine(self, soft_cap: int = SECTION_SOFT_CAP) -> int:
        """
        Evict bullets that are dead weight:
          - net_score <= -2  (repeatedly harmful), OR
          - never helpful AND marked neutral >= NEUTRAL_EVICT_THRESHOLD times
            AND untouched for STALE_DAYS days (stale noise),
        then cap each section to `soft_cap`, keeping the highest-net-score
        bullets. Returns the number of bullets removed.
        """
        with self._lock:
            removed = 0

            # Drop net-negative and stale-neutral bullets first.
            keep: list[Bullet] = []
            for b in self.bullets:
                if b.net_score <= -2:
                    removed += 1
                    continue
                if (b.helpful_count == 0
                        and b.neutral_count >= NEUTRAL_EVICT_THRESHOLD
                        and _age_days(b.updated_at) >= STALE_DAYS):
                    removed += 1
                    continue
                keep.append(b)
            self.bullets = keep

            # Cap per section.
            by_section: dict[str, list[Bullet]] = {}
            for b in self.bullets:
                by_section.setdefault(b.section, []).append(b)
            new_bullets: list[Bullet] = []
            for section, items in by_section.items():
                if len(items) <= soft_cap:
                    new_bullets.extend(items)
                    continue
                items.sort(key=lambda b: (b.net_score, b.updated_at), reverse=True)
                new_bullets.extend(items[:soft_cap])
                removed += len(items) - soft_cap
            self.bullets = new_bullets
            return removed

    # ----- rendering for the Generator -----
    def render(self, section_filter: Optional[Iterable[str]] = None) -> str:
        """
        Format the playbook as a multi-section block. Empty sections are
        skipped (the Generator's context shouldn't be padded with headers).
        """
        with self._lock:
            allowed = list(section_filter) if section_filter else self._allowed_sections()
            lines: list[str] = []
            for section in allowed:
                items = [b for b in self.bullets if b.section == section]
                if not items:
                    continue
                items.sort(key=lambda b: b.net_score, reverse=True)
                lines.append(f"## {section}")
                for b in items:
                    lines.append(f"  - {b.render()}")
                lines.append("")
            return "\n".join(lines).rstrip() or "(empty playbook)"

    def stats(self) -> dict:
        with self._lock:
            by_section: dict[str, int] = {}
            for b in self.bullets:
                by_section[b.section] = by_section.get(b.section, 0) + 1
            return {
                "scope": self.scope,
                "total_bullets": len(self.bullets),
                "by_section": by_section,
                "next_seq": self._next_seq,
            }

    # ----- persistence -----
    def load(self) -> None:
        if not self.path.exists():
            self._loaded_mtime = 0.0
            return
        with self._lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"[ace.playbook] failed to load {self.path}: {e}")
                return
            self.bullets = [Bullet(**row) for row in data.get("bullets", [])]
            self._next_seq = data.get("next_seq", 1)
            self.scope = data.get("scope", self.scope)
            try:
                self._loaded_mtime = self.path.stat().st_mtime
            except Exception:
                self._loaded_mtime = 0.0

    def reload_if_changed(self) -> bool:
        """Re-read the JSON if another process has written it since our last load.
        Returns True when a reload happened."""
        try:
            mtime = self.path.stat().st_mtime if self.path.exists() else 0.0
        except Exception:
            return False
        if mtime and mtime != self._loaded_mtime:
            print(f"[ace.playbook] reloading {self.path.name} (mtime changed)")
            self.load()
            return True
        return False

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            payload = {
                "scope": self.scope,
                "next_seq": self._next_seq,
                "updated_at": _now(),
                "bullets": [asdict(b) for b in self.bullets],
            }
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
            try:
                self._loaded_mtime = self.path.stat().st_mtime
            except Exception:
                pass
