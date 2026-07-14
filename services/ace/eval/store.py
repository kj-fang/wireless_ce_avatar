"""
EvalStore — persisted eval reports under <playbooks_dir>/history/evals/.

Layout:
    history/evals/
        YYYY-MM-DD/
            YYYYMMDDTHHMMSSZ__<run_id>.json

Reports ride along with the rest of history/ in the additive SMB sync
(services.ace.sync mirrors playbook JSONs but copies history/ additively),
so past evals are shared across machines automatically.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


class EvalStore:
    def __init__(self, root: Path, retention_days: int = 90):
        self._root = Path(root)
        self._retention_days = int(retention_days)
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"[ace.eval.store] failed to create {self._root}: {e}")

    def save(self, report: dict) -> Optional[Path]:
        now = datetime.now()
        day_dir = self._root / now.strftime("%Y-%m-%d")
        run_id = report.get("run_id") or "run"
        name = f"{now.strftime('%Y%m%dT%H%M%SZ')}__{run_id}.json"
        path = day_dir / name
        try:
            day_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, indent=2, ensure_ascii=False,
                                       default=str), encoding="utf-8")
            return path
        except Exception as e:
            print(f"[ace.eval.store] save failed: {e}")
            return None

    def list_reports(self) -> list[dict]:
        """Newest-first summaries (no per-case detail)."""
        out: list[dict] = []
        if not self._root.exists():
            return out
        for day_dir in sorted(self._root.iterdir(), reverse=True):
            if not day_dir.is_dir():
                continue
            try:
                datetime.strptime(day_dir.name, "%Y-%m-%d")
            except ValueError:
                continue
            for f in sorted(day_dir.glob("*.json"), reverse=True):
                try:
                    rep = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                out.append({
                    "date": day_dir.name,
                    "name": f.name,
                    "run_id": rep.get("run_id"),
                    "source": rep.get("source"),
                    "started_at": rep.get("started_at"),
                    "finished_at": rep.get("finished_at"),
                    "before_ref": (rep.get("before") or {}).get("ref"),
                    "summary": rep.get("summary"),
                    "gate": rep.get("gate"),
                })
        return out

    def read_report(self, date: str, name: str) -> Optional[dict]:
        for part in (date, name):
            if "/" in part or "\\" in part or ".." in part:
                return None
        target = self._root / date / name
        try:
            target = target.resolve()
            if self._root.resolve() not in target.parents:
                return None
            if not target.exists():
                return None
            return json.loads(target.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[ace.eval.store] read_report({date}/{name}) failed: {e}")
            return None

    def latest(self) -> Optional[dict]:
        reports = self.list_reports()
        if not reports:
            return None
        first = reports[0]
        return self.read_report(first["date"], first["name"])

    def prune(self) -> dict:
        cutoff = datetime.now() - timedelta(days=self._retention_days)
        removed = 0
        try:
            for day_dir in self._root.iterdir():
                if not day_dir.is_dir():
                    continue
                try:
                    day = datetime.strptime(day_dir.name, "%Y-%m-%d")
                except ValueError:
                    continue
                if day < cutoff:
                    try:
                        shutil.rmtree(day_dir)
                        removed += 1
                    except Exception as e:
                        print(f"[ace.eval.store] prune failed for {day_dir}: {e}")
        except Exception as e:
            print(f"[ace.eval.store] prune failed: {e}")
        return {"eval_dirs_removed": removed}
