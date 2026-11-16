# Progress: generic log chatbot (split Linux / Generic)

Parent: [README.md](README.md).

| # | Sub-task | Workpad | Depends on | Status | Validated | Commit | Notes |
|---|---|---|---|---|---|---|---|
| 01 | Engine class, capability policy, default prompts | [01_engine_and_prompts.md](01_engine_and_prompts.md) | — | done | yes | — | Also fixed `default_speclet()`'s own hardcoded profile tuple (separate from `PROFILES`) to use `PROFILES`, otherwise the "agent" bundle lookup would silently return `""` for `generic`. |
| 02 | Skills file and YAML lifecycle module | [02_skills_yaml_lifecycle.md](02_skills_yaml_lifecycle.md) | — | done | yes | — | |
| 03 | Chat domain row and UI config | [03_domain_and_ui_config.md](03_domain_and_ui_config.md) | — | done | yes | — | Also reworded the stale `linux`-as-"Generic Log Analysis Agent" comment near `CASE_TECHNOLOGY_AGENTS` to stay accurate post-split. |
| 04 | Route profile, blueprint, and app registration | [04_route_profile_and_wiring.md](04_route_profile_and_wiring.md) | 01, 02, 03 | done | yes | — | Validated with the system Python 3.10 venv (`C:\Users\kjfang\AppData\Local\Programs\Python\Python310\python.exe`), which has the full dependency set — the repo's own `.venv` (Python 3.14) is missing most of `requirements.txt` and can't install `py7zr`'s deps on 3.14. |
| 05 | App-startup agent instantiation | [05_app_startup_instantiation.md](05_app_startup_instantiation.md) | 01, 02, 04 | done | yes | — | Validated by calling `set_up(socketio)` directly (Python 3.10 venv): prints `✅ 1 Generic skills loaded from cloud YAML: .../utils/generic_skills.yaml` then `📄 Generic Chatbot Agent loaded`, and `app_config.generic_chatbot_agent` is non-`None` after, mirroring Linux's boot block exactly. |
| 06 | Home page button split | [06_home_page_button.md](06_home_page_button.md) | 04 | done | yes | — | Validated with Flask's test client (Python 3.10 venv): home page now has five shortcut links (added Generic, relabeled Linux's), and `/linux_chatbot/` / `/generic_chatbot/` both return 200 with the expected page titles. |
| 07 | Automated unit tests for the generic_chatbot blueprint | [07_unit_tests.md](07_unit_tests.md) | 04 | done | yes | — | Scope: core flows only (registration/routes, `/set_log`, `/chat` SSE, `/reset`, history list/get), mirroring `unit_tests/test_bt_chatbot_blueprint.py`'s pattern but not its full breadth. New file `unit_tests/test_generic_chatbot_blueprint.py`, 24 tests, all pass (Python 3.10 venv). One finding along the way: GENERIC_UI's `windows_event_log` is `False` (like Linux, unlike BT/Wi-Fi), so `/set_log` never calls `find_event_log_for_log` and `evtx_path` is always `""` — the test asserts that instead of assuming BT's shape. |

Next: all seven sub-tasks are done. README.md's Status should move to `shipped` and the folder
should move to `docs/workpad/finished/generic_log_chatbot/` per the skill's "Ongoing use" step 7.

## Log

- 2026-11-13: Workpad created from the approved plan at
  `C:\Users\kjfang\.claude\plans\add-a-new-feature-hidden-kahn.md`, seeded by reading
  `services/chatbot/engine/{system,linux,speclet_defaults}.py`,
  `blueprints/chatbot/chatbot_routes.py`, `configs/{chatbot_ui,chat_domains,path_configs,
  global_configs,set_up_app}.py`, `utils/linux_skills_yaml_utils.py`, `utils/speclets_utils.py`,
  `utils/issue_time_ai.py`, `blueprints/__init__.py`, `templates/index.html`, and `app.py`.
- 2026-11-13: All six sub-tasks done and validated. Final end-to-end check: `create_app()`'s test
  client returns 200 for both `/linux_chatbot/` (title "Linux Log Chatbot") and `/generic_chatbot/`
  (title "Generic Log Analysis Agent"), and calling `set_up(socketio)` directly loads both agents
  (`app_config.linux_chatbot_agent` / `.generic_chatbot_agent` both non-`None`) with the Generic
  boot block falling back correctly to the bundled `utils/generic_skills.yaml` when the cloud share
  is unreachable. Every diff reviewed via `git diff` is additive only, except the two sanctioned
  Linux-side edits (its display title and the `CASE_TECHNOLOGY_AGENTS` comment). Status moved to
  `shipped`; folder moves to `docs/workpad/finished/generic_log_chatbot/` per the skill's "Ongoing
  use" step 7. No commits were made — all changes remain in the working tree on
  `feature/generic_log_chatbot` for the user to review and commit.
  Environment note: the repo's own `.venv` (Python 3.14) is missing most of `requirements.txt` and
  cannot install `py7zr`'s compiled deps on 3.14 at all; all `import app` / `set_up` validation in
  this workpad used the system Python 3.10 install instead
  (`C:\Users\kjfang\AppData\Local\Programs\Python\Python310\python.exe`), which already has the
  full dependency set.
- 2026-11-13: Owner flagged that the workpad was closed out prematurely: no automated test
  coverage exists for `generic_chatbot` yet, only the manual/import checks above. Reopened —
  moved the folder back from `docs/workpad/finished/generic_log_chatbot/` to
  `docs/workpad/generic_log_chatbot/`, README.md Status reverted to `open`, and added sub-task 07
  (see [07_unit_tests.md](07_unit_tests.md)) scoped to "core flows only" per the owner's choice.
- 2026-11-13: Sub-task 07 done. Added `unit_tests/test_generic_chatbot_blueprint.py` (structure,
  `/set_log`, `/chat`, `/reset`, `/history/list`+`/history/get` — 24 tests), modeled on
  `test_bt_chatbot_blueprint.py`'s mocking pattern. Validated both ways listed in the sub-task's
  Validate section (Python 3.10 venv): the new module alone (24/24 pass) and the full
  `unittest discover -s unit_tests` suite (`PYTHONIOENCODING=utf-8` needed — a pre-existing cp1252
  console-encoding issue on this Windows Python unrelated to this change; without it, an unrelated
  module's import-time `print()` of a non-ASCII arrow crashes discovery before any test runs). Full
  suite: 659 tests, 1 pre-existing unrelated failure (`test_main_blueprint.TestIndex
  .test_get_renders_case_form`, about a `dashboard_now` kwarg added by the Handsfree dashboard work
  already on this branch — confirmed independent of this change by running it in isolation with the
  new file absent from the run). All sub-tasks are now done; moving README.md's Status to `shipped`
  and the folder to `docs/workpad/finished/generic_log_chatbot/` next.
