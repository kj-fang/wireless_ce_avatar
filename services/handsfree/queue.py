"""
Review queue + processed-case ledger for the Handsfree Replyer.

Layout under <avatarfiles_dir>/handsfree/:
    config.json                    orchestrator config (owner, backend, caps)
    ledger.json                    {case_nbr: {analyzed_at, posted_at, draft_id}}
    queue/<case_nbr>__<ts>.json    one draft per analyzed case

Draft lifecycle:  pending_review → posted
                              ↘ rejected  ↘ post_failed

Same write style as the rest of the app's sidecar stores: per-process lock,
atomic replace, swallow-and-log on the read path.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

VALID_STATUSES = {"pending_review", "approved", "posted", "rejected", "post_failed"}

_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class HandsfreeStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.queue_dir = self.root / "queue"
        self.ledger_path = self.root / "ledger.json"
        self.config_path = self.root / "config.json"
        self.queue_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # config
    # ------------------------------------------------------------------
    DEFAULT_CONFIG = {
        "owner_name": "",
        "auto_post": False,            # future: confidence-gated auto posting
        "post_backend": "auto",        # rest | ui | auto (rest first, ui fallback)
        "max_cases_per_run": 3,
        "dry_run": False,              # analyze + queue, posting disabled
        # Verified by a human after describe_comment_fields():
        #   {"body_field": "...", "private_field": "...", "private_value": true}
        "rest_field_map": None,
    }

    def load_config(self) -> dict:
        cfg = dict(self.DEFAULT_CONFIG)
        try:
            if self.config_path.exists():
                cfg.update(json.loads(self.config_path.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"[handsfree.queue] config read failed: {e}")
        return cfg

    def save_config(self, cfg: dict) -> dict:
        merged = {**self.load_config(), **(cfg or {})}
        with _LOCK:
            tmp = self.config_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(merged, indent=2), encoding="utf-8")
            os.replace(tmp, self.config_path)
        return merged

    # ------------------------------------------------------------------
    # ledger
    # ------------------------------------------------------------------
    def _load_ledger(self) -> dict:
        try:
            if self.ledger_path.exists():
                return json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[handsfree.queue] ledger read failed: {e}")
        return {}

    def _save_ledger(self, ledger: dict) -> None:
        tmp = self.ledger_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
        os.replace(tmp, self.ledger_path)

    def is_processed(self, case_nbr: str) -> bool:
        return str(case_nbr) in self._load_ledger()

    def mark_analyzed(self, case_nbr: str, draft_id: str) -> None:
        with _LOCK:
            ledger = self._load_ledger()
            entry = ledger.get(str(case_nbr), {})
            entry.update({"analyzed_at": _now_iso(), "draft_id": draft_id})
            ledger[str(case_nbr)] = entry
            self._save_ledger(ledger)

    def mark_posted(self, case_nbr: str, comment_id: str = "") -> None:
        with _LOCK:
            ledger = self._load_ledger()
            entry = ledger.get(str(case_nbr), {})
            entry.update({"posted_at": _now_iso(), "comment_id": comment_id})
            ledger[str(case_nbr)] = entry
            self._save_ledger(ledger)

    # ------------------------------------------------------------------
    # queue
    # ------------------------------------------------------------------
    def enqueue(self, *, case_nbr: str, case_id: str, subject: str,
                draft_plain: str, draft_html: str,
                confidence: Optional[int], mode: str,
                analysis: dict) -> dict:
        draft_id = f"{case_nbr}__{datetime.now().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:6]}"
        record = {
            "draft_id": draft_id,
            "status": "pending_review",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "case_nbr": str(case_nbr),
            "case_id": case_id,
            "subject": subject,
            "mode": mode,                      # full | triage_only
            "confidence": confidence,
            "draft_plain": draft_plain,
            "draft_html": draft_html,
            "post_result": None,
            "analysis": analysis,              # full CaseAnalysis dump for the UI
        }
        with _LOCK:
            path = self.queue_dir / f"{draft_id}.json"
            path.write_text(json.dumps(record, indent=2, ensure_ascii=False,
                                       default=str), encoding="utf-8")
        self.mark_analyzed(case_nbr, draft_id)
        return record

    def _draft_path(self, draft_id: str) -> Optional[Path]:
        # draft_id is client-supplied on approve/reject — sanitise.
        if not draft_id or "/" in draft_id or "\\" in draft_id or ".." in draft_id:
            return None
        p = self.queue_dir / f"{draft_id}.json"
        return p if p.exists() else None

    def get(self, draft_id: str) -> Optional[dict]:
        p = self._draft_path(draft_id)
        if p is None:
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[handsfree.queue] draft read failed ({draft_id}): {e}")
            return None

    def update(self, draft_id: str, **fields) -> Optional[dict]:
        with _LOCK:
            rec = self.get(draft_id)
            if rec is None:
                return None
            status = fields.get("status")
            if status is not None and status not in VALID_STATUSES:
                raise ValueError(f"invalid status: {status}")
            rec.update(fields)
            rec["updated_at"] = _now_iso()
            p = self.queue_dir / f"{draft_id}.json"
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(rec, indent=2, ensure_ascii=False,
                                      default=str), encoding="utf-8")
            os.replace(tmp, p)
            return rec

    def list_drafts(self, include_closed: bool = True,
                    limit: int = 100) -> list[dict]:
        """Newest-first draft summaries (analysis payload omitted)."""
        out: list[dict] = []
        try:
            files = sorted(self.queue_dir.glob("*.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            return out
        for p in files[:limit * 2]:
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not include_closed and rec.get("status") in ("posted", "rejected"):
                continue
            slim = {k: rec.get(k) for k in
                    ("draft_id", "status", "created_at", "updated_at",
                     "case_nbr", "case_id", "subject", "mode", "confidence",
                     "post_result")}
            out.append(slim)
            if len(out) >= limit:
                break
        return out
