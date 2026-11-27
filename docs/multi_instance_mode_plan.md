# Multi-Instance Mode — Design Discussion & Implementation Plan

## Problem

Need to run multiple Avatar instances at the same time (e.g. to test different
IPS cases in parallel) without them interfering with each other. Today the app
actively enforces a single-instance model, so this doesn't work out of the box.

Scope: starts as a QA/testing capability, but may become a formally supported
feature later — the design below is chosen to be easy to promote to that later
without a rewrite.

## Current single-instance architecture (facts gathered)

- `utils/instance_utils.py`: `check_already_running()` + `app_start.lock`
  actively block a second launch. `running_avatar.json` holds exactly one
  instance record (pid/port/url). `ensure_tray_manager()` is a singleton
  (`tray_manager.pid`).
- `utils/port_utils.py`: shared paths all live under
  `Downloads\IntelAvatar_files\` — `app_state\` (lock/instance/port-config
  files) and `logs\avatar.log`.
- `app.py`: already supports `--port` to pick a port explicitly, and the
  port-occupied check later in `__main__` already resolves "is this port
  owned by an IntelAvatar process" on a **per-port** basis (used to redirect
  SendTo to the right existing window). The *early* `check_already_running()`
  check is the one that is NOT port-aware and blocks regardless of `--port`.
- `app.py` `create_app()`: Flask session uses a hardcoded `secret_key` and a
  shared session directory `~/.intelavatar_sessions` with the default
  (non-port-scoped) cookie name. Browser cookies are not port-scoped on
  `127.0.0.1`, so two instances on different ports would collide on the same
  session cookie/files — this is the mechanism that would leak case context
  (per [IPS_CASE_NUMBER_FLOW.md](../IPS_CASE_NUMBER_FLOW.md)) between
  instances testing different cases.
- `configs/logger_setup.py`: all instances log to one shared, rotating
  `avatar.log`.
- `ensure_startup_shortcut()` / `ensure_sendto_shortcut()`: write to a single
  shared Windows "Startup" / "Send To" shortcut; the last instance to start
  overwrites the token, breaking SendTo for earlier instances.
- `configs/global_configs.py`: `sendto_token` is per-process, fixed
  (`dev-debug-token-12345`) in dev mode and random in packaged builds — no
  code changes needed here, just something to be aware of.

## Decisions

| # | Topic | Decision |
|---|---|---|
| 1 | Scope | QA-first, but designed so it can become a supported feature later |
| 2 | Instance identity | The **port number** (already guaranteed unique; no new user input needed) |
| 3 | Session isolation | Cookie name **and** session file directory both include the port (e.g. cookie `session_48596`, dir `~/.intelavatar_sessions/48596/`) |
| 4 | Single-instance guard | Keep default behavior; add opt-in **CLI flag** `--allow-multi-instance` to bypass `check_already_running()` |
| 5 | Tray manager | Skipped entirely under the flag (`ensure_tray_manager()` not called) — revisit multi-instance tray UI only if this becomes a real feature |
| 6 | Log files | Under the flag, log filename includes the port (e.g. `avatar_48596.log`) instead of shared `avatar.log` |
| 7 | Startup/SendTo shortcuts | Skipped entirely under the flag (`ensure_startup_shortcut()` / `ensure_sendto_shortcut()` not called), to avoid one test instance clobbering the shared shortcut. SendTo between instances is done via CLI instead: `python app.py --port <target> --allow-multi-instance --sendto-token <token> <file>`. **`--allow-multi-instance` is required on this call too** — without it, the unconditional `check_already_running()` runs first and redirects to whichever instance is registered in the shared `running_avatar.json`, ignoring `--port` entirely. Only with the flag does execution reach the per-port "reuse existing browser" logic further down in `app.py` that actually honors `--port`. |
| 8 | Startup lock | `acquire_app_start_lock()` keyed by port under the flag (e.g. `app_start_48596.lock`) instead of one global lock, so concurrent instances on different ports don't false-positive each other as "still starting up" |
| 9 | `port_config.json` (persisted machine-default port) | Left shared as-is — already only written when `--port` is *not* explicitly passed, so it's a non-issue as long as every multi-instance launch passes `--port` explicitly |

## Planned implementation

1. `app.py`: add `--allow-multi-instance` CLI flag.
2. `utils/instance_utils.py`:
   - `check_already_running()` / early-exit block in `app.py`: skip when the
     flag is set.
   - `acquire_app_start_lock()` / `release_app_start_lock()` /
     `_app_start_lock_path()`: accept a `port` and use a per-port lock
     filename when the flag is set.
   - `ensure_tray_manager()`, `ensure_startup_shortcut()`,
     `ensure_sendto_shortcut()`: not called when the flag is set.
3. `configs/logger_setup.py` / `utils/port_utils.py`: allow the log filename
   to be parameterized by port, used only when the flag is set.
4. `app.py` `create_app()`: parameterize the session cookie name and
   `SESSION_FILE_DIR` by port, used only when the flag is set.
5. No changes needed to the per-port "reuse existing instance" logic later in
   `app.py` — it already does the right thing once the early singleton guard
   is out of the way.

## Open follow-ups (not blocking, deferred)

- If multi-instance becomes a real supported feature: redesign tray manager
  to track and display multiple instances (listed in the original discussion
  as option (A), deferred here as option (C): skip tray entirely for now).
- `services/driver_manage_service.py` ChromeDriver download/setup directory
  is shared across instances (read-mostly cache); a race only matters if two
  instances start at the exact same moment *and* a chromedriver version
  upgrade/download is needed. Not addressed in this round — flagged as a
  known, low-probability risk.
