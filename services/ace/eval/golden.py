"""
Golden-case registry — the curated set of cases the ACE refine loop runs on.

Stored at `<feedback_root>/golden_cases.json` so it lives next to the
conversation snapshots it references and is shared across users/machines via
the SMB feedback root. Deliberately NOT in the playbooks dir: playbook
snapshots / rollback / mirror-sync must never touch the golden selection.

Durable-log pinning: marking a case golden copies its log into the shared
attached-logs folder (`logs/<prefix><cid>/<tid>__<user>__<name>`, the same
convention feedback_service._enqueue_log_attach uses) when the log currently
only resolves via a machine-local path. That guarantees a golden case stays
replayable from any machine even after the original local file is gone.

Write style mirrors feedback_service: per-file lock, atomic replace,
swallow-and-log errors (a golden bookkeeping failure must never break a run).
"""

from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from .cases import EvalCase, resolve_case_log, _domain_prefix

_GOLDEN_FILENAME = "golden_cases.json"
_LOCK = threading.Lock()

# namespace -> golden-set filename, mirroring feedback_service's domain
# partitioning (_domain_prefix). Kept here rather than computed via
# f"{_domain_prefix(domain)}{_GOLDEN_FILENAME}" so the wifi filename stays
# the exact literal "golden_cases.json" byte-for-byte (no accidental
# reshuffle of the file every machine already has).
_GOLDEN_FILENAMES = {
    "wifi": _GOLDEN_FILENAME,
    "bt": f"bt_{_GOLDEN_FILENAME}",
}


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _current_user() -> str:
    try:
        u = getpass.getuser() or os.environ.get("USERNAME", "") or "anon"
    except Exception:
        u = os.environ.get("USERNAME", "") or "anon"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", u).strip("._-") or "anon"


class GoldenSet:
    """Shared golden registry over one feedback root.

    namespace: "wifi" (default) or "bt" — selects which golden-set file this
    instance reads/writes (golden_cases.json vs bt_golden_cases.json), so a
    BT golden case never lands in WiFi's regression-test basis or vice
    versa. Not wired up anywhere yet (no BT golden cases exist as of
    2026-07 — see EvalHarness/_build_eval_harness in web/server.py, which
    still always builds this with the default "wifi"); this parameter is
    the intended extension point for whenever that changes.
    """

    def __init__(self, feedback_root: Path, namespace: str = "wifi"):
        self.feedback_root = Path(feedback_root)
        self.namespace = namespace if namespace in _GOLDEN_FILENAMES else "wifi"
        self.path = self.feedback_root / _GOLDEN_FILENAMES[self.namespace]
        self._entries: list[dict] = []
        self._loaded_mtime: float = -1.0
        self._load()

    # ---------- persistence ----------
    def _load(self) -> None:
        try:
            if not self.path.exists():
                self._entries = []
                self._loaded_mtime = -1.0
                return
            mtime = self.path.stat().st_mtime
            if mtime == self._loaded_mtime:
                return
            data = json.loads(self.path.read_text(encoding="utf-8"))
            entries = data.get("entries") if isinstance(data, dict) else data
            self._entries = [e for e in (entries or []) if isinstance(e, dict)
                             and e.get("conversation_id")]
            self._loaded_mtime = mtime
        except Exception as e:
            print(f"[ace.eval.golden] load failed ({self.path}): {e}")
            self._entries = self._entries or []

    def _save(self) -> bool:
        try:
            payload = {
                "schema_version": 1,
                "updated_at": _now_iso(),
                "entries": self._entries,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, self.path)
            try:
                self._loaded_mtime = self.path.stat().st_mtime
            except Exception:
                pass
            return True
        except Exception as e:
            print(f"[ace.eval.golden] save failed ({self.path}): {e}")
            return False

    # ---------- queries ----------
    def refresh(self) -> None:
        """Re-read from disk when another machine updated the shared file."""
        with _LOCK:
            self._loaded_mtime = -1.0
            self._load()

    def list(self) -> list[dict]:
        with _LOCK:
            self._load()
            return [dict(e) for e in self._entries]

    def contains(self, conversation_id: str, turn_id: Optional[str] = None) -> bool:
        """turn_id=None matches any golden turn in the conversation. An entry
        with turn_id=None (whole-conversation golden) matches every turn."""
        with _LOCK:
            self._load()
            for e in self._entries:
                if e.get("conversation_id") != conversation_id:
                    continue
                etid = e.get("turn_id")
                if etid is None or turn_id is None or etid == turn_id:
                    return True
        return False

    def key_sets(self) -> tuple[set, set]:
        """One-shot membership snapshot: (exact (cid, tid) pairs,
        whole-conversation cids). Callers checking MANY cases must use this
        instead of contains() — contains() re-stats the shared file per call,
        which over SMB costs one round trip each."""
        with _LOCK:
            self._load()
            pairs: set = set()
            whole: set = set()
            for e in self._entries:
                cid = e.get("conversation_id")
                tid = e.get("turn_id")
                if tid is None:
                    whole.add(cid)
                else:
                    pairs.add((cid, tid))
            return pairs, whole

    def __len__(self) -> int:
        with _LOCK:
            self._load()
            return len(self._entries)

    # ---------- mutation ----------
    def add(self, conversation_id: str, turn_id: Optional[str] = None,
            note: str = "", pin_log: bool = True) -> dict:
        """Mark a case golden. Returns a result dict:
            {added: bool, already: bool, pinned_log: str|None, error: str|None}
        """
        result = {"added": False, "already": False, "pinned_log": None, "error": None}
        if not conversation_id:
            result["error"] = "empty conversation_id"
            return result

        with _LOCK:
            self._load()
            for e in self._entries:
                if (e.get("conversation_id") == conversation_id
                        and e.get("turn_id") == turn_id):
                    result["already"] = True
                    return result
            self._entries.append({
                "conversation_id": conversation_id,
                "turn_id": turn_id,
                "added_by": _current_user(),
                "added_at": _now_iso(),
                "note": (note or "").strip(),
            })
            if not self._save():
                self._entries.pop()
                result["error"] = "failed to write golden registry"
                return result
            result["added"] = True

        if pin_log:
            try:
                pinned = self._pin_log(conversation_id, turn_id)
                result["pinned_log"] = pinned
            except Exception as e:
                # Non-fatal: the golden mark stands; the case just stays
                # dependent on the local path until the user attaches a log.
                print(f"[ace.eval.golden] log pin failed for {conversation_id}: {e}")
        return result

    def remove(self, conversation_id: str, turn_id: Optional[str] = None) -> bool:
        with _LOCK:
            self._load()
            before = len(self._entries)
            self._entries = [
                e for e in self._entries
                if not (e.get("conversation_id") == conversation_id
                        and (turn_id is None or e.get("turn_id") == turn_id))
            ]
            if len(self._entries) == before:
                return False
            return self._save()

    # ---------- durable-log pinning ----------
    def _pin_log(self, conversation_id: str, turn_id: Optional[str]) -> Optional[str]:
        """If the case's log resolves only via a local path, copy it into the
        shared attached-logs folder so the golden case stays replayable from
        any machine. Returns the destination path when a copy was made."""
        snap_path = self.feedback_root / "conversations" / f"{conversation_id}.json"
        if not snap_path.exists():
            # Try the bt_ prefixed name as fallback.
            snap_path = self.feedback_root / "conversations" / f"bt_{conversation_id}.json"
            if not snap_path.exists():
                return None
        snap = json.loads(snap_path.read_text(encoding="utf-8"))
        domain = snap.get("domain") or "wifi"

        # Pick a concrete turn to resolve against: the given one, else the
        # first feedback-carrying turn.
        tid = turn_id
        if tid is None:
            for t in snap.get("turns") or []:
                if t.get("feedback") and t.get("turn_id"):
                    tid = t["turn_id"]
                    break
        if tid is None:
            return None

        res = resolve_case_log(self.feedback_root, snap, tid)
        if res.source != "log_path" or not res.resolved_path:
            return None    # already attached (durable) or nothing to pin

        src = Path(res.resolved_path)
        if not src.is_file():
            return None
        logs_dir = (self.feedback_root / "logs"
                    / f"{_domain_prefix(domain)}{conversation_id}")
        logs_dir.mkdir(parents=True, exist_ok=True)
        dst = logs_dir / f"{tid}__{_current_user()}__{src.name}"
        if not dst.exists():
            shutil.copy2(str(src), str(dst))
        return str(dst)
