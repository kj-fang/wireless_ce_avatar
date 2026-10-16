"""
Review queue + processed-case ledger for the Handsfree Replyer.

Lives on the shared folder (configs.path_configs.HANDSFREE_DIR) so the
analysis server and every reviewer's Avatar see the same queue:
    config.json                    team-wide config (owner, backend, caps, schedule)
    ledger/<case_nbr>.json         {analyzed_at, posted_at, draft_id, ...} per case
    queue/<case_nbr>__<ts>.json    one draft per analyzed case
    locks/<case_nbr>.lock          held while one reviewer posts to the case

Draft lifecycle:  pending_review → posted
                              ↘ rejected  ↘ post_failed

Several machines write here: every file is written whole through a uniquely
named temp file + atomic replace, and the ledger is one file per case so two
machines only collide when they touch the same case at the same moment.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

VALID_STATUSES = {"pending_review", "approved", "posted", "rejected", "post_failed"}

_LOCK = threading.Lock()


class HandsfreeShareUnavailable(RuntimeError):
    """The shared Handsfree folder cannot be reached or written."""


_root_cache: Optional[Path] = None
_root_lock = threading.Lock()
_SHARE_PROBE_TIMEOUT_S = 10


def handsfree_root() -> Path:
    """The shared Handsfree folder. Raises HandsfreeShareUnavailable instead
    of falling back to a local folder, which would split the queue between
    machines. Only a success is cached, so a later call retries."""
    global _root_cache
    with _root_lock:
        if _root_cache is not None:
            return _root_cache
        from configs.path_configs import HANDSFREE_DIR
        outcome: list = []

        def _probe():
            try:
                Path(HANDSFREE_DIR).mkdir(parents=True, exist_ok=True)
                outcome.append(None)
            except Exception as e:
                outcome.append(e)

        # An unreachable SMB host can block for minutes; probe in a thread.
        t = threading.Thread(target=_probe, daemon=True, name="handsfree-share-probe")
        t.start()
        t.join(_SHARE_PROBE_TIMEOUT_S)
        if not outcome or outcome[0] is not None:
            reason = f"{outcome[0]}" if outcome else f"no answer in {_SHARE_PROBE_TIMEOUT_S}s"
            raise HandsfreeShareUnavailable(
                f"Handsfree shared folder unreachable: {HANDSFREE_DIR} ({reason})")
        _root_cache = Path(HANDSFREE_DIR)
        print(f"[handsfree.queue] using shared folder: {_root_cache}")
        return _root_cache


def open_store() -> "HandsfreeStore":
    """The one HandsfreeStore everyone (server, clients, CLI, UI) uses."""
    return HandsfreeStore(handsfree_root())


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_write(path: Path, text: str) -> None:
    # Unique temp name: two machines replacing the same file must not share a temp.
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _safe_case(case_nbr: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9]", "", str(case_nbr or ""))
    if not safe:
        raise ValueError(f"invalid case number: {case_nbr!r}")
    return safe


def decoded_log_dir(rec: dict) -> str:
    """Folder holding the case's decoded WRT log, or "" when the run never
    got past decode_etl (analysis.log_path is only set once the .log exists)."""
    log_path = str(((rec or {}).get("analysis") or {}).get("log_path") or "")
    return os.path.dirname(os.path.normpath(log_path)) if log_path else ""


class HandsfreeStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.queue_dir = self.root / "queue"
        self.ledger_dir = self.root / "ledger"
        self.locks_dir = self.root / "locks"
        self.config_path = self.root / "config.json"
        for d in (self.queue_dir, self.ledger_dir, self.locks_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # config
    # ------------------------------------------------------------------
    DEFAULT_CONFIG = {
        "owner_name": "",
        "auto_post": False,            # future: confidence-gated auto posting
        "post_backend": "auto",        # rest | ui | auto (rest first, ui fallback)
        "max_cases_per_run": 3,
        "dry_run": False,              # analyze + queue, posting disabled
        # Trial / tuning on historical cases: every run's drafts are flagged
        # "trial" and can never be posted, and closed cases of the automatic
        # analysis' list-view source are included instead of skipped.
        "trial_mode": False,
        # Automatic analysis source: the owner's open cases, or an IPS list
        # view (auto_source "list_view" + auto_list_view developer name).
        "auto_source": "owner",
        "auto_list_view": "Handsfree_replier_test_case",
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
        # Load + merge + replace under the one lock: the scheduler thread,
        # a running scan and the /handsfree/config route all write partial
        # updates, and a merge from a stale snapshot would revert the others'
        # fields (trial_mode, dry_run, owner, source).
        with _LOCK:
            merged = {**self.load_config(), **(cfg or {})}
            _atomic_write(self.config_path, json.dumps(merged, indent=2))
        return merged

    # ------------------------------------------------------------------
    # ledger (one file per case)
    # ------------------------------------------------------------------
    def _ledger_file(self, case_nbr: str) -> Path:
        return self.ledger_dir / f"{_safe_case(case_nbr)}.json"

    def is_processed(self, case_nbr: str) -> bool:
        return self._ledger_file(case_nbr).exists()

    def ledger_entry(self, case_nbr: str) -> dict:
        p = self._ledger_file(case_nbr)
        try:
            if p.exists():
                return dict(json.loads(p.read_text(encoding="utf-8")) or {})
        except Exception as e:
            print(f"[handsfree.queue] ledger read failed ({case_nbr}): {e}")
        return {}

    def _update_ledger(self, case_nbr: str, **fields) -> None:
        with _LOCK:
            entry = self.ledger_entry(case_nbr)
            entry.update(fields)
            _atomic_write(self._ledger_file(case_nbr), json.dumps(entry, indent=2))

    def mark_analyzed(self, case_nbr: str, draft_id: str) -> None:
        self._update_ledger(case_nbr, analyzed_at=_now_iso(), draft_id=draft_id)

    def mark_customer_update(self, case_nbr: str, update_iso: str) -> None:
        """Remember the customer comment (IPS CreatedDate) a round was run
        for, so the auto-scan does not re-run the same update."""
        self._update_ledger(case_nbr, last_customer_update=update_iso)

    def mark_posted(self, case_nbr: str, comment_id: str = "",
                    first_response: bool = False) -> None:
        """Record a post. The first customer-facing overview (checklist /
        request reply carrying it) is remembered separately: it is sent at
        most ONCE per case, while analyses may post once per round."""
        fields = {"posted_at": _now_iso(), "comment_id": comment_id}
        if first_response and not self.first_response_posted(case_nbr):
            fields.update({"first_response_posted_at": _now_iso(),
                           "first_response_comment_id": comment_id})
        self._update_ledger(case_nbr, **fields)

    def first_response_posted(self, case_nbr: str) -> bool:
        return bool(self.ledger_entry(case_nbr).get("first_response_posted_at"))

    def mark_skipped(self, case_nbr: str, reason: str,
                     owner_name: str = "") -> None:
        self._update_ledger(case_nbr, skipped_at=_now_iso(), skip_reason=reason,
                            owner_name=owner_name)

    # ------------------------------------------------------------------
    # posting claim (cross-machine)
    # ------------------------------------------------------------------
    # Longer than any post takes (REST seconds, UI fallback ~1 min); an older
    # lock was left by a crashed post.
    CLAIM_STALE_S = 600

    def claim_case(self, case_nbr: str, holder: str) -> Optional[str]:
        """Take the case's posting claim: None when taken, else who holds it.
        O_EXCL creation is atomic on the file server, so two reviewers on
        different machines cannot post to the same case at the same time."""
        path = self.locks_dir / f"{_safe_case(case_nbr)}.lock"
        for _ in range(2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    if time.time() - path.stat().st_mtime > self.CLAIM_STALE_S:
                        path.unlink()
                        continue
                    return path.read_text(encoding="utf-8").strip() or "another reviewer"
                except FileNotFoundError:
                    continue       # released between our two calls: try again
                except OSError:
                    return "another reviewer"
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(holder)
            return None
        return "another reviewer"

    def release_case(self, case_nbr: str) -> None:
        try:
            (self.locks_dir / f"{_safe_case(case_nbr)}.lock").unlink()
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------
    # queue
    # ------------------------------------------------------------------
    def enqueue(self, *, case_nbr: str, case_id: str, subject: str,
                draft_plain: str, draft_html: str,
                confidence: Optional[int], mode: str,
                analysis: dict, owner_name: str = "",
                trial: bool = False) -> dict:
        draft_id = f"{case_nbr}__{datetime.now().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:6]}"
        record = {
            "draft_id": draft_id,
            "status": "pending_review",
            "trial": bool(trial),              # tuning run: never posted
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "case_nbr": str(case_nbr),
            "case_id": case_id,
            "owner_name": owner_name,
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
            _atomic_write(path, json.dumps(record, indent=2, ensure_ascii=False,
                                           default=str))
        # An "error" draft means the case could not even be fetched: it is
        # not an analysis, so it never marks the case processed (the auto
        # scan must retry it; is_processed must not hide it).
        if mode != "error":
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
            _atomic_write(p, json.dumps(rec, indent=2, ensure_ascii=False, default=str))
            return rec

    def list_drafts(self, include_closed: bool = True,
                    limit: int = 100, owner_name: str = "") -> list[dict]:
        """Newest-first draft summaries (analysis payload omitted)."""
        out: list[dict] = []
        owner_filter = (owner_name or "").strip().casefold()
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
            if owner_filter and str(rec.get("owner_name") or "").casefold() != owner_filter:
                continue
            if not include_closed and rec.get("status") in ("posted", "rejected"):
                continue
            slim = {k: rec.get(k) for k in
                    ("draft_id", "status", "created_at", "updated_at",
                     "case_nbr", "case_id", "owner_name", "subject", "mode", "confidence",
                     "post_result")}
            slim["trial"] = bool(rec.get("trial"))
            slim["log_dir"] = decoded_log_dir(rec)
            analysis = rec.get("analysis") or {}
            slim["action_owner"] = str(analysis.get("action_owner") or "")
            slim["next_action"] = str(analysis.get("next_action") or "")
            out.append(slim)
            if len(out) >= limit:
                break
        return out
