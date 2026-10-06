"""
Automatic analysis scheduler: runs orchestrator.run_auto_scan on a cadence.

Config (HandsfreeStore config.json):
    auto_check_enabled   bool
    auto_check_mode      "4h" | "8h" | "nightly"   (nightly = every day 23:00 local)
    auto_check_max_cases int                        cases per round (newest first)
    auto_next_run_at     ISO local                  persisted so a restart resumes
    auto_last_scan_at    ISO UTC                    written by run_auto_scan
    auto_last_result     str                        "<n> case(s) run" / "failed: …"

One daemon thread, started lazily (ensure_started) by the blueprint; it
re-reads the config every tick so Start/Stop/mode changes from the UI need
no restart. Drafts land in the review queue — nothing is ever posted here.
"""

from __future__ import annotations

import threading
import time
import traceback
from datetime import datetime, timedelta
from typing import Optional

MODES = {"4h": "every 4 hours", "8h": "every 8 hours",
         "nightly": "once overnight (23:00)"}
_TICK_S = 30
_NIGHTLY_HOUR = 23

_thread: Optional[threading.Thread] = None
_thread_lock = threading.Lock()
_running = threading.Event()


def next_run_after(mode: str, now: datetime, first: bool = False) -> datetime:
    """When the next round is due, given the mode and the time the previous
    one started (or, with first=True, the moment Start was pressed: interval
    modes run right away, nightly waits for the next 23:00). Pure."""
    if mode == "nightly":
        due = now.replace(hour=_NIGHTLY_HOUR, minute=0, second=0, microsecond=0)
        return due if due > now else due + timedelta(days=1)
    hours = {"4h": 4, "8h": 8}.get(mode, 4)
    return now if first else now + timedelta(hours=hours)


def _store():
    from .orchestrator import _store as orchestrator_store
    return orchestrator_store()


def status(cfg: Optional[dict] = None) -> dict:
    cfg = cfg if cfg is not None else _store().load_config()
    return {
        "enabled": bool(cfg.get("auto_check_enabled")),
        "mode": cfg.get("auto_check_mode") or "nightly",
        "mode_label": MODES.get(cfg.get("auto_check_mode") or "nightly", ""),
        "max_cases": int(cfg.get("auto_check_max_cases") or 10),
        "next_run_at": cfg.get("auto_next_run_at") or "",
        "last_scan_at": cfg.get("auto_last_scan_at") or "",
        "last_result": cfg.get("auto_last_result") or "",
        "running": _running.is_set(),
        "thread_alive": bool(_thread and _thread.is_alive()),
    }


def configure(enabled: bool, mode: str, max_cases: Optional[int] = None) -> dict:
    """Start / stop / re-cadence from the UI. Returns the new status."""
    store = _store()
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode}")
    updates: dict = {"auto_check_enabled": bool(enabled), "auto_check_mode": mode}
    if max_cases is not None:
        updates["auto_check_max_cases"] = max(1, min(50, int(max_cases)))
    if enabled:
        updates["auto_next_run_at"] = next_run_after(
            mode, datetime.now().astimezone(), first=True).isoformat(timespec="seconds")
    else:
        updates["auto_next_run_at"] = ""
    cfg = store.save_config(updates)
    if enabled:
        ensure_started()
    return status(cfg)


def run_once(trigger: str = "manual") -> dict:
    """Run a round now (UI 'Run now'); reschedules the next automatic one."""
    from .orchestrator import run_auto_scan
    store = _store()
    cfg = store.load_config()
    owner = (cfg.get("owner_name") or "").strip()
    if not owner:
        return {"ok": False, "error": "no owner name configured"}
    _running.set()
    try:
        res = run_auto_scan(store, owner, int(cfg.get("auto_check_max_cases") or 10),
                            trigger=trigger)
    finally:
        _running.clear()
    busy = not res.get("ok") and "already in progress" in str(res.get("error", ""))
    if cfg.get("auto_check_enabled") and not busy:
        # (when another run held the lock the due time stays, so the next
        # tick retries instead of skipping a whole cadence)
        store.save_config({"auto_next_run_at": next_run_after(
            cfg.get("auto_check_mode") or "nightly",
            datetime.now().astimezone()).isoformat(timespec="seconds")})
    return res


def _loop() -> None:
    while True:
        try:
            cfg = _store().load_config()
            if cfg.get("auto_check_enabled") and cfg.get("auto_next_run_at"):
                due = datetime.fromisoformat(cfg["auto_next_run_at"])
                if datetime.now().astimezone() >= due:
                    run_once(trigger="scheduled")
        except Exception:
            print(f"[handsfree.scheduler] tick failed:\n{traceback.format_exc()}")
        time.sleep(_TICK_S)


def ensure_started() -> None:
    """Idempotent: start the daemon thread once per process."""
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_loop, daemon=True,
                                   name="handsfree-scheduler")
        _thread.start()
