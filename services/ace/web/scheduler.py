"""Nightly scheduler for ACE batch adapt runs.

One daemon `threading.Timer` armed for the next occurrence of a fixed
local time (default 23:30). On fire it invokes `run_fn()` and re-arms
for +24h. Persisted `enabled` flag lets `resume_if_enabled()` re-arm
across server restarts.

Missed runs (server offline at the fire time) are dropped silently —
`AceRunner.run_batch()` uses a cursor, so the next successful run
picks up everything since the previous one.
"""

from __future__ import annotations

import json
import threading
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional


class NightlyScheduler:
    def __init__(self, state_path: Path, run_fn: Callable[[], None],
                 hour: int = 23, minute: int = 30):
        self._state_path = Path(state_path)
        self._run_fn = run_fn
        self._hour = int(hour)
        self._minute = int(minute)

        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._next_run: Optional[datetime] = None

        # Persisted fields (loaded from state file if present):
        self._enabled: bool = False
        self._validate: bool = False   # chain eval + gate after the nightly adapt
        self._last_run_iso: Optional[str] = None
        self._last_result: Optional[dict] = None
        self._load()

    # ---------- persistence ----------
    def _load(self) -> None:
        if not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[ace.nightly] failed to read {self._state_path}: {e}")
            return
        self._enabled = bool(data.get("enabled", False))
        self._validate = bool(data.get("validate", False))
        # Allow the state file to override the fire time (useful for dev).
        if "hour" in data:
            self._hour = int(data["hour"])
        if "minute" in data:
            self._minute = int(data["minute"])
        self._last_run_iso = data.get("last_run_iso")
        self._last_result = data.get("last_result")

    def _save(self) -> None:
        payload = {
            "enabled": self._enabled,
            "validate": self._validate,
            "hour": self._hour,
            "minute": self._minute,
            "last_run_iso": self._last_run_iso,
            "last_result": self._last_result,
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(payload, indent=2),
                                        encoding="utf-8")
        except Exception as e:
            print(f"[ace.nightly] failed to write {self._state_path}: {e}")

    # ---------- public API ----------
    @property
    def validate(self) -> bool:
        with self._lock:
            return self._validate

    def start(self, validate: Optional[bool] = None) -> dict:
        with self._lock:
            self._enabled = True
            if validate is not None:
                self._validate = bool(validate)
            self._save()
            self._cancel_timer_locked()
            self._arm_next_locked()
        return self.status()

    def stop(self) -> dict:
        with self._lock:
            self._enabled = False
            self._cancel_timer_locked()
            self._next_run = None
            self._save()
        return self.status()

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": self._enabled,
                "validate": self._validate,
                "hour": self._hour,
                "minute": self._minute,
                "next_run_iso": self._next_run.isoformat() if self._next_run else None,
                "last_run_iso": self._last_run_iso,
                "last_result": self._last_result,
            }

    def resume_if_enabled(self) -> None:
        """Called from `create_app()` at boot. If persisted state says the
        schedule was on, re-arm the timer."""
        with self._lock:
            if self._enabled:
                self._arm_next_locked()

    # ---------- internals ----------
    def _cancel_timer_locked(self) -> None:
        if self._timer is not None:
            try:
                self._timer.cancel()
            except Exception:
                pass
            self._timer = None

    def _compute_next_run(self, now: Optional[datetime] = None) -> datetime:
        now = now or datetime.now()
        target = now.replace(hour=self._hour, minute=self._minute,
                             second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)
        return target

    def _arm_next_locked(self) -> None:
        next_run = self._compute_next_run()
        delay = max(1.0, (next_run - datetime.now()).total_seconds())
        self._next_run = next_run
        t = threading.Timer(delay, self._on_fire)
        t.daemon = True
        self._timer = t
        t.start()
        print(f"[ace.nightly] next run armed for {next_run.isoformat()} "
              f"({delay:.0f}s from now)")

    def _on_fire(self) -> None:
        # Runs in the Timer's own thread.
        started = datetime.now()
        result: dict = {"started_at": started.isoformat()}
        try:
            self._run_fn()
            result["error"] = None
        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"
            print(f"[ace.nightly] run_fn failed:\n{traceback.format_exc()}")

        with self._lock:
            self._last_run_iso = started.isoformat()
            self._last_result = result
            self._save()
            # Re-arm only if still enabled (user may have hit Stop mid-run).
            if self._enabled:
                self._arm_next_locked()
            else:
                self._next_run = None
