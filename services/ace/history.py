"""Run history for ACE — turn-level reflection/curation logs + full playbook
snapshots, both auto-pruned after N days.

Layout under <playbooks_dir>/history/:

    turns/
        YYYY-MM-DD.jsonl        one JSON per line, one line per processed turn
    snapshots/
        YYYY-MM-DD/
            YYYYMMDDTHHMMSSZ__<job_id>/
                meta.json
                workflow.json   verbatim copies of live playbooks
                domain_*.json

Design notes
------------
* Snapshots are plain file copies. Restoring is `cp snapshots/…/foo.json
  <playbooks_dir>/foo.json`. No delta format.
* Retention prune walks by day-directory mtime — cheap even at N=30.
* All write paths swallow their own exceptions after logging: a failure to
  archive must NEVER block the ACE run itself.
"""

from __future__ import annotations

import json
import shutil
import threading
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


# Playbook-directory dotfiles we do NOT want copied into snapshots.
_SNAPSHOT_SKIP_NAMES = {".ace_cursor.json", ".ace_nightly.json"}


class HistoryWriter:
    def __init__(self, root: Path, retention_days: int = 30):
        self._root = Path(root)
        self._turns_dir = self._root / "turns"
        self._snapshots_dir = self._root / "snapshots"
        self._retention_days = int(retention_days)
        self._write_lock = threading.Lock()
        try:
            self._turns_dir.mkdir(parents=True, exist_ok=True)
            self._snapshots_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"[ace.history] failed to create {self._root}: {e}")

    # ---------- writers ----------
    def record_turn(self, *, run_id: Optional[str], run_source: str,
                    conversation_id: str, turn_id: str,
                    feedback: dict, applied_bullets: list[dict],
                    reflection: dict, curate_result: dict) -> None:
        record = {
            "ts": datetime.now().astimezone().isoformat(),
            "run_id": run_id,
            "run_source": run_source,
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "feedback": feedback,
            "applied_bullet_ids": [b.get("id") for b in (applied_bullets or [])
                                   if isinstance(b, dict)],
            "applied_bullets": applied_bullets,
            "reflection": reflection,
            "curate_result": curate_result,
        }
        path = self._turns_dir / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        line = json.dumps(record, ensure_ascii=False, default=str)
        try:
            with self._write_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception as e:
            print(f"[ace.history] record_turn failed for {conversation_id}/{turn_id}: {e}")

    def snapshot_playbooks(self, playbooks_dir: Path, *,
                           run_id: str, source: str, meta: dict) -> Optional[str]:
        """Copy every *.json in playbooks_dir into a new dated dir and write
        meta.json. Returns the run-directory name, or None on failure."""
        now = datetime.now()
        day_dir = self._snapshots_dir / now.strftime("%Y-%m-%d")
        run_dir_name = f"{now.strftime('%Y%m%dT%H%M%SZ')}__{run_id}"
        run_dir = day_dir / run_dir_name
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"[ace.history] failed to create snapshot dir {run_dir}: {e}")
            return None

        copied: list[str] = []
        try:
            for src in sorted(Path(playbooks_dir).glob("*.json")):
                if src.name in _SNAPSHOT_SKIP_NAMES:
                    continue
                shutil.copy2(src, run_dir / src.name)
                copied.append(src.name)
        except Exception as e:
            print(f"[ace.history] snapshot copy failed: {e}")

        meta_out = dict(meta or {})
        meta_out.update({
            "run_id": run_id,
            "source": source,
            "snapshot_at": now.astimezone().isoformat(),
            "files": copied,
        })
        try:
            (run_dir / "meta.json").write_text(
                json.dumps(meta_out, indent=2, default=str), encoding="utf-8")
        except Exception as e:
            print(f"[ace.history] failed to write meta.json in {run_dir}: {e}")

        return run_dir_name

    # ---------- pruning ----------
    def prune(self) -> dict:
        cutoff = datetime.now() - timedelta(days=self._retention_days)
        stats = {"turns_removed": 0, "snapshot_dirs_removed": 0}

        # Turn JSONLs — by filename (date) so we don't rely on mtime of files
        # that may have been touched by recent appends.
        try:
            for f in self._turns_dir.glob("*.jsonl"):
                try:
                    d = datetime.strptime(f.stem, "%Y-%m-%d")
                except ValueError:
                    continue
                if d < cutoff:
                    try:
                        f.unlink()
                        stats["turns_removed"] += 1
                    except Exception as e:
                        print(f"[ace.history] prune: failed to remove {f}: {e}")
        except Exception as e:
            print(f"[ace.history] prune (turns) failed: {e}")

        # Snapshot day-dirs — by directory-name date.
        try:
            for d in self._snapshots_dir.iterdir():
                if not d.is_dir():
                    continue
                try:
                    day = datetime.strptime(d.name, "%Y-%m-%d")
                except ValueError:
                    continue
                if day < cutoff:
                    try:
                        shutil.rmtree(d)
                        stats["snapshot_dirs_removed"] += 1
                    except Exception as e:
                        print(f"[ace.history] prune: failed to rmtree {d}: {e}")
        except Exception as e:
            print(f"[ace.history] prune (snapshots) failed: {e}")

        return stats

    # ---------- readers for the UI ----------
    def list_turn_dates(self) -> list[str]:
        if not self._turns_dir.exists():
            return []
        dates = []
        for f in self._turns_dir.glob("*.jsonl"):
            try:
                datetime.strptime(f.stem, "%Y-%m-%d")
            except ValueError:
                continue
            dates.append(f.stem)
        return sorted(dates, reverse=True)

    def read_turns(self, date: str, offset: int = 0, limit: int = 200) -> list[dict]:
        path = self._turns_dir / f"{date}.jsonl"
        if not path.exists():
            return []
        out: list[dict] = []
        try:
            with path.open("r", encoding="utf-8") as fh:
                for i, line in enumerate(fh):
                    if i < offset:
                        continue
                    if len(out) >= limit:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
        except Exception as e:
            print(f"[ace.history] read_turns({date}) failed: {e}")
        return out

    def list_snapshots(self) -> list[dict]:
        """Return newest-first list of snapshot runs, each with meta + file list."""
        if not self._snapshots_dir.exists():
            return []
        out: list[dict] = []
        try:
            for day_dir in sorted(self._snapshots_dir.iterdir(), reverse=True):
                if not day_dir.is_dir():
                    continue
                try:
                    datetime.strptime(day_dir.name, "%Y-%m-%d")
                except ValueError:
                    continue
                for run_dir in sorted(day_dir.iterdir(), reverse=True):
                    if not run_dir.is_dir():
                        continue
                    meta: dict = {}
                    mp = run_dir / "meta.json"
                    if mp.exists():
                        try:
                            meta = json.loads(mp.read_text(encoding="utf-8"))
                        except Exception:
                            pass
                    files = sorted(p.name for p in run_dir.glob("*.json")
                                   if p.name != "meta.json")
                    out.append({
                        "date": day_dir.name,
                        "run_dir": run_dir.name,
                        "source": meta.get("source"),
                        "run_id": meta.get("run_id"),
                        "snapshot_at": meta.get("snapshot_at"),
                        "totals": meta.get("totals"),
                        "files": files,
                    })
        except Exception as e:
            print(f"[ace.history] list_snapshots failed: {e}\n{traceback.format_exc()}")
        return out

    def read_snapshot_file(self, date: str, run_dir: str,
                           filename: str) -> Optional[str]:
        """Return raw file text, or None if the path escapes the snapshot root
        or the file is missing."""
        # Reject any path separators to prevent traversal.
        for part in (date, run_dir, filename):
            if "/" in part or "\\" in part or ".." in part:
                return None
        target = self._snapshots_dir / date / run_dir / filename
        try:
            target = target.resolve()
            if self._snapshots_dir.resolve() not in target.parents:
                return None
            if not target.exists():
                return None
            return target.read_text(encoding="utf-8")
        except Exception as e:
            print(f"[ace.history] read_snapshot_file({date}/{run_dir}/{filename}) failed: {e}")
            return None
