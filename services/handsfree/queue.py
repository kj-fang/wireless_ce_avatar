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
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

VALID_STATUSES = {"pending_review", "approved", "posted", "rejected", "post_failed"}

_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def decoded_log_dir(rec: dict) -> str:
    """Folder holding the case's decoded WRT log, or "" when the run never
    got past decode_etl (analysis.log_path is only set once the .log exists)."""
    log_path = str(((rec or {}).get("analysis") or {}).get("log_path") or "")
    return os.path.dirname(os.path.normpath(log_path)) if log_path else ""


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

    def ledger_entry(self, case_nbr: str) -> dict:
        return dict(self._load_ledger().get(str(case_nbr)) or {})

    def _update_ledger(self, case_nbr: str, **fields) -> None:
        with _LOCK:
            ledger = self._load_ledger()
            entry = ledger.get(str(case_nbr), {})
            entry.update(fields)
            ledger[str(case_nbr)] = entry
            self._save_ledger(ledger)

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
        with _LOCK:
            ledger = self._load_ledger()
            entry = ledger.get(str(case_nbr), {})
            entry.update({"skipped_at": _now_iso(), "skip_reason": reason,
                          "owner_name": owner_name})
            ledger[str(case_nbr)] = entry
            self._save_ledger(ledger)

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
            path.write_text(json.dumps(record, indent=2, ensure_ascii=False,
                                       default=str), encoding="utf-8")
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
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(rec, indent=2, ensure_ascii=False,
                                      default=str), encoding="utf-8")
            os.replace(tmp, p)
            return rec

    # Ledger fields that record a POST to IPS: a reset never forgets them (a
    # first response that already went out must not be sent again).
    _LEDGER_KEEP_ON_RESET = ("posted_at", "comment_id",
                             "first_response_posted_at", "first_response_comment_id")

    def reset_case(self, case_nbr: str, *, purge_downloads: bool = False,
                   case_dir: str = "") -> dict:
        """Forget a case's analysis history so it is analyzed afresh (the
        log was wrong, the customer re-uploaded, ...). Drafts that were not
        posted move to queue_archive_reset_<stamp>/ (never deleted); posted
        drafts stay as the audit trail. The ledger keeps only the post
        records. purge_downloads removes decoded logs and extracted capture
        folders under case_dir but keeps the downloaded archives."""
        case_nbr = str(case_nbr)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        archive = self.root / f"queue_archive_reset_{stamp}"
        archived: list[str] = []
        kept: list[str] = []
        with _LOCK:
            for p in sorted(self.queue_dir.glob(f"{case_nbr}__*.json")):
                try:
                    rec = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    rec = {}
                if str(rec.get("case_nbr") or "") not in ("", case_nbr):
                    continue
                if rec.get("status") == "posted":
                    kept.append(p.stem)
                    continue
                archive.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), str(archive / p.name))
                archived.append(p.stem)
            ledger = self._load_ledger()
            entry = ledger.get(case_nbr) or {}
            remaining = {k: v for k, v in entry.items() if k in self._LEDGER_KEEP_ON_RESET}
            if remaining:
                ledger[case_nbr] = remaining
            else:
                ledger.pop(case_nbr, None)
            self._save_ledger(ledger)
        purged: list[str] = []
        if purge_downloads and case_dir and os.path.isdir(case_dir):
            for dirpath, dirnames, filenames in os.walk(case_dir):
                for name in filenames:
                    low = name.lower()
                    if low.endswith(".log") or ".etl" in low:
                        full = os.path.join(dirpath, name)
                        try:
                            os.remove(full)
                            purged.append(full)
                        except OSError:
                            pass
        return {"case_nbr": case_nbr, "archived": archived, "kept_posted": kept,
                "archive_dir": str(archive) if archived else "",
                "ledger_kept": remaining, "purged": purged}

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
            slim["log_path"] = str(analysis.get("log_path") or "") if slim["log_dir"] else ""
            slim["action_owner"] = str(analysis.get("action_owner") or "")
            slim["next_action"] = str(analysis.get("next_action") or "")
            out.append(slim)
            if len(out) >= limit:
                break
        return out
