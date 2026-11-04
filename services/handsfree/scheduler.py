"""
Automatic analysis scheduler: runs orchestrator.run_auto_scan on a cadence.

Config (HandsfreeStore config.json):
    auto_check_enabled   bool
    auto_check_mode      "4h" | "8h" | "nightly"   (nightly = every day 23:00 local)
    auto_check_host      str                        machine that pressed Start: the
                                                    config is shared, only that
                                                    machine runs the rounds
    auto_check_max_cases int                        cases per round (newest first)
    auto_source          "owner" | "list_view"      what a round scans (default owner)
    auto_owners          [str]                      up to MAX_OWNERS IPS Owner.Name
                                                    for the owner source; empty =
                                                    the Settings owner_name
    auto_list_views      [str]                      up to MAX_LIST_VIEWS IPS list view
                                                    developer names; a round runs the
                                                    union of their cases (closed cases
                                                    only in trial mode)
    auto_list_view       str                        legacy single name (first of the list)
    auto_next_run_at     ISO local                  persisted so a restart resumes
    auto_last_scan_at    ISO UTC                    written by run_auto_scan
    auto_last_result     str                        "<n> case(s) run" / "failed: …"
    email_watch_enabled  bool                       also scan an Outlook folder each round
    email_folder         str                        "Inbox" or "Inbox/<subfolder>"
    email_lookback_days  int                        how far back a round looks
    email_max_per_round  int                        cases per round from mail
    email_reply_all      bool                       Reply all (default) or Reply
    email_last_scan_at / email_last_result          written by run_email_scan

One daemon thread per SERVER process (role() == "server": app.py
--handsfree-role server / INTELAVATAR_HANDSFREE_ROLE), started by the
blueprint when it registers and by configure(enabled=True); it re-reads the
config every tick so Start/Stop/mode changes from the UI need no restart. A
client (a reviewer's PC, the default role) never starts it: it sees the
schedule read-only and keeps "Run now". Drafts land in the review queue —
nothing is ever posted here.
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


ROLES = ("client", "server")


def role() -> str:
    """This machine's Handsfree role: app_config.handsfree_role (set by app.py
    from --handsfree-role), else the INTELAVATAR_HANDSFREE_ROLE environment
    variable, else "client". Anything unknown is a client."""
    import os
    try:
        from configs.global_configs import app_config
        value = getattr(app_config, "handsfree_role", "") or ""
    except Exception:
        value = ""
    value = (str(value) or os.environ.get("INTELAVATAR_HANDSFREE_ROLE", "")).strip().lower()
    return value if value in ROLES else "client"


def can_schedule_here() -> bool:
    """Only the server role may start the schedule (and run its rounds)."""
    return role() == "server"


def _this_host() -> str:
    import socket
    return socket.gethostname().casefold()


def _runs_here(cfg: dict) -> bool:
    """The schedule is pinned to the machine that pressed Start (the config
    is shared by every PC); a second server would otherwise run every round
    twice."""
    return str(cfg.get("auto_check_host") or "").casefold() == _this_host()


MAX_LIST_VIEWS = 3
MAX_OWNERS = 10


def _source(cfg: dict) -> str:
    return "list_view" if cfg.get("auto_source") == "list_view" else "owner"


def _clean_views(values) -> list[str]:
    """Strip / drop empties / dedupe (order kept) / cap at MAX_LIST_VIEWS."""
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    for v in values or []:
        name = str(v or "").strip()
        if name and name not in out:
            out.append(name)
    return out[:MAX_LIST_VIEWS]


def _list_views(cfg: dict) -> list[str]:
    # auto_list_view / trial_list_view: where the single name lived before
    # several views could be chosen (2026-10-15 / 2026-10-14).
    views = _clean_views(cfg.get("auto_list_views"))
    if not views:
        views = _clean_views(cfg.get("auto_list_view") or cfg.get("trial_list_view") or "")
    return views


def _list_view(cfg: dict) -> str:
    views = _list_views(cfg)
    return views[0] if views else ""


def _clean_owners(values) -> list[str]:
    """Strip / drop empties / dedupe case-insensitively (order kept) / cap."""
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    seen: set = set()
    for v in values or []:
        name = str(v or "").strip()
        if name and name.casefold() not in seen:
            out.append(name)
            seen.add(name.casefold())
    return out[:MAX_OWNERS]


def _owners(cfg: dict) -> list[str]:
    """The owners a round scans: auto_owners, else the Settings owner_name
    (how every config looked before several owners could be chosen)."""
    owners = _clean_owners(cfg.get("auto_owners"))
    if not owners:
        owners = _clean_owners(cfg.get("owner_name") or "")
    return owners


def scan_target(cfg: dict) -> dict:
    """What a round scans, from the config:
    {"owner": str (first owner), "owners": [str], "list_views": [str],
     "list_view": str (first), "error": str}
    — error set when the chosen source is not configured."""
    if _source(cfg) == "list_view":
        views = _list_views(cfg)
        return {"owner": "", "owners": [], "list_views": views,
                "list_view": views[0] if views else "",
                "error": "" if views else "no IPS list view configured"}
    owners = _owners(cfg)
    return {"owner": owners[0] if owners else "", "owners": owners, "list_views": [],
            "list_view": "", "error": "" if owners else "no owner name configured"}


EMAIL_DEFAULTS = {"email_watch_enabled": False, "email_folder": "Inbox",
                  "email_lookback_days": 7, "email_max_per_round": 3,
                  "email_reply_all": True}


def _int(value, default: int) -> int:
    """int(value); the default only for an absent/blank/non-numeric value
    (0 is a value, clamped by the caller)."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def email_settings(cfg: dict) -> dict:
    """run_email_scan keyword arguments from the config."""
    return {"folder": str(cfg.get("email_folder") or "Inbox").strip() or "Inbox",
            "max_cases": max(1, min(20, int(cfg.get("email_max_per_round") or 3))),
            "lookback_days": max(1, min(60, int(cfg.get("email_lookback_days") or 7))),
            "reply_all": bool(cfg.get("email_reply_all", True))}


def status(cfg: Optional[dict] = None) -> dict:
    cfg = cfg if cfg is not None else _store().load_config()
    em = email_settings(cfg)
    return {
        "email_watch_enabled": bool(cfg.get("email_watch_enabled")),
        "email_folder": em["folder"],
        "email_lookback_days": em["lookback_days"],
        "email_max": em["max_cases"],
        "email_reply_all": em["reply_all"],
        "email_last_scan_at": cfg.get("email_last_scan_at") or "",
        "email_last_result": cfg.get("email_last_result") or "",
        "enabled": bool(cfg.get("auto_check_enabled")),
        "mode": cfg.get("auto_check_mode") or "nightly",
        "mode_label": MODES.get(cfg.get("auto_check_mode") or "nightly", ""),
        "max_cases": int(cfg.get("auto_check_max_cases") or 10),
        "source": _source(cfg),
        "owners": _owners(cfg),
        "max_owners": MAX_OWNERS,
        "list_view": _list_view(cfg),
        "list_views": _list_views(cfg),
        "max_list_views": MAX_LIST_VIEWS,
        "next_run_at": cfg.get("auto_next_run_at") or "",
        "last_scan_at": cfg.get("auto_last_scan_at") or "",
        "last_result": cfg.get("auto_last_result") or "",
        "running": _running.is_set(),
        "thread_alive": bool(_thread and _thread.is_alive()),
        # Where the schedule runs: this machine's role, the machine that
        # pressed Start, and whether that is this one.
        "role": role(),
        "can_schedule": can_schedule_here(),
        "host": cfg.get("auto_check_host") or "",
        "runs_here": _runs_here(cfg),
        "cli_last_run": cli_last_run(),
    }


_CLI_STATUS_MAX_AGE_S = 24 * 3600


def cli_running_message(last_status: str) -> str:
    """Why Start is refused while the Task Scheduler CLI is still active here."""
    return (f"the Task Scheduler CLI (tools/handsfree_server_run.py) ran on this server "
            f"in the last 24 hours ({last_status}): disable that scheduled task first, "
            "then start the schedule here — one scheduler per server")


def scheduled_in_app_here(cfg: dict) -> str:
    """Non-empty when the Beacon page schedules the rounds on THIS machine
    (the CLI round must then stay out of the way): the reason text."""
    if cfg.get("auto_check_enabled") and _runs_here(cfg) and cfg.get("auto_check_host"):
        return (f"the Beacon page schedules the rounds on this server (host "
                f"{cfg.get('auto_check_host')}, {MODES.get(cfg.get('auto_check_mode') or 'nightly', '')}): "
                "stop it there before running the CLI round, or disable this scheduled task "
                "— one scheduler per server")
    return ""


def cli_last_run() -> str:
    """The Task Scheduler CLI's last_status.txt first line (tools/
    handsfree_server_run.py) when it is younger than a day, else "". The
    server page shows it next to an enabled schedule: both running means
    duplicate rounds and a Chrome profile opened twice. Local file only."""
    try:
        from configs.global_configs import app_config
        base = getattr(app_config, "avatarfiles_dir", None)
        if not base:
            return ""
        from pathlib import Path
        p = Path(base) / "logs" / "handsfree_server" / "last_status.txt"
        if time.time() - p.stat().st_mtime > _CLI_STATUS_MAX_AGE_S:
            return ""
        return (p.read_text(encoding="utf-8", errors="replace").splitlines() or [""])[0].strip()
    except Exception:
        return ""


def _settings_updates(max_cases=None, source=None, list_view=None,
                      list_views=None, owners=None, email_watch=None,
                      email_folder=None, email_lookback_days=None,
                      email_max=None, email_reply_all=None) -> dict:
    updates: dict = {}
    if max_cases is not None:
        updates["auto_check_max_cases"] = max(1, min(50, int(max_cases)))
    if owners is not None:
        updates["auto_owners"] = _clean_owners(owners)
    if email_watch is not None:
        updates["email_watch_enabled"] = bool(email_watch)
    if email_folder is not None:
        updates["email_folder"] = str(email_folder).strip() or "Inbox"
    if email_lookback_days is not None:
        updates["email_lookback_days"] = max(1, min(60, _int(email_lookback_days, 7)))
    if email_max is not None:
        updates["email_max_per_round"] = max(1, min(20, _int(email_max, 3)))
    if email_reply_all is not None:
        updates["email_reply_all"] = bool(email_reply_all)
    if source is not None:
        updates["auto_source"] = "list_view" if str(source) == "list_view" else "owner"
    if list_views is not None:
        views = _clean_views(list_views)
    elif list_view is not None:
        views = _clean_views(list_view)
    else:
        views = None
    if views is not None:
        updates["auto_list_views"] = views
        updates["auto_list_view"] = views[0] if views else ""   # legacy readers
    return updates


def save_settings(max_cases=None, source=None, list_view=None, list_views=None,
                  owners=None, **email) -> dict:
    """Persist the round settings without touching the schedule (UI 'Run
    now' with edited fields). Returns the config."""
    return _store().save_config(
        _settings_updates(max_cases, source, list_view, list_views, owners, **email))


CLIENT_CANNOT_SCHEDULE = ("this PC runs Beacon as a client: scheduled rounds are started "
                          "on the Handsfree server (start Beacon there with --server / "
                          "app.py --handsfree-role server)")


def configure(enabled: bool, mode: str, max_cases: Optional[int] = None,
              source=None, list_view=None, list_views=None, owners=None,
              **email) -> dict:
    """Start / stop / re-cadence from the UI. Returns the new status.
    Raises ValueError for an unknown mode, for Start on a client machine, or,
    when starting, an unconfigured source (no owner name / no list view)."""
    store = _store()
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode}")
    if enabled and not can_schedule_here():
        raise ValueError(CLIENT_CANNOT_SCHEDULE)
    if enabled:
        recent = cli_last_run()
        if recent:
            raise ValueError(cli_running_message(recent))
    updates: dict = {"auto_check_enabled": bool(enabled), "auto_check_mode": mode}
    updates.update(_settings_updates(max_cases, source, list_view, list_views, owners,
                                     **email))
    if enabled:
        target = scan_target({**store.load_config(), **updates})
        if target["error"]:
            raise ValueError(target["error"])
        updates["auto_next_run_at"] = next_run_after(
            mode, datetime.now().astimezone(), first=True).isoformat(timespec="seconds")
        updates["auto_check_host"] = _this_host()
    else:
        updates["auto_next_run_at"] = ""
    cfg = store.save_config(updates)
    if enabled:
        ensure_started()
    return status(cfg)


def run_once(trigger: str = "manual", on_start=None) -> dict:
    """Run a round now (UI 'Run now'); reschedules the next automatic one.
    on_start() fires once the round holds the run lock (see run_auto_scan)."""
    from .orchestrator import run_auto_scan, run_email_scan
    store = _store()
    cfg = store.load_config()
    target = scan_target(cfg)
    if target["error"]:
        return {"ok": False, "error": target["error"]}
    _running.set()
    try:
        res = run_auto_scan(store, target["owner"],
                            int(cfg.get("auto_check_max_cases") or 10),
                            trigger=trigger, list_views=target["list_views"],
                            owners=target["owners"], on_start=on_start)
        # The Outlook folder is an ADDITIONAL source of the same round.
        if res.get("ok") and cfg.get("email_watch_enabled"):
            res["email"] = run_email_scan(store, trigger=trigger, **email_settings(cfg))
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


def _tick(now: Optional[datetime] = None) -> bool:
    """One scheduler tick: run the due round when this server owns the
    schedule. Returns True when a round ran."""
    cfg = _store().load_config()
    if not (cfg.get("auto_check_enabled") and cfg.get("auto_next_run_at")
            and can_schedule_here() and _runs_here(cfg)):
        return False
    due = datetime.fromisoformat(cfg["auto_next_run_at"])
    if (now or datetime.now().astimezone()) < due:
        return False
    run_once(trigger="scheduled")
    return True


def _loop() -> None:
    while True:
        try:
            from configs.global_configs import app_config
            if not getattr(app_config, "avatarfiles_dir", None):
                time.sleep(_TICK_S)      # app still booting (set_up not run yet)
                continue
            _tick()
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
