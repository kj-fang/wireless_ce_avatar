# Sub-task 07: Automated unit tests for the generic_chatbot blueprint

Status: open. Folder: [README.md](README.md). Progress: [PROGRESS.md](PROGRESS.md).
Depends on: 04 (the route profile/blueprint must exist to test it).

## Goal

`generic_chatbot_bp` (`blueprints/chatbot/chatbot_routes.py:2182-2201`, `GENERIC_PROFILE`) has zero
automated test coverage today — sub-tasks 01-06 validated it only by hand (Flask test-client smoke
checks run ad hoc, not committed as a test module; see `PROGRESS.md` rows 04-06). Its two siblings
built the same way, `bt_chatbot_bp` and `log_chatbot_bp`, both have thorough dedicated suites
(`unit_tests/test_bt_chatbot_blueprint.py`, 3,173 lines; `test_log_chatbot_blueprint.py`, 2,628
lines) landed in #189, before this workpad existed. `linux_chatbot_bp` has no suite either, but
that gap is pre-existing and out of scope here — this sub-task closes Generic's gap only.

Owner-chosen scope (asked because full BT-style parity is ~3,000 lines of mechanical-but-slow work,
and Linux shipped with no suite at all): **core flows only** — not full parity with BT's suite.
Explicitly in scope: blueprint registration + full route table, `/set_log`, `/chat` (SSE, success
and error paths), `/reset`, `/history/list`, `/history/get`. Explicitly deferred (do not write
these — if a future sub-task wants them, it gets its own number): the skills-editor endpoint
family (`/skills_yaml_status`, `/skills_yaml_use_cloud`, `/skills_yaml_use_user`,
`/refresh_cloud_baseline`, `/load_local_skills_yaml`, `/save_local_skills_yaml`,
`/delete_local_skill`, `/upload_modified_yaml`, `/append_skills_yaml`), the prompts-editor family
(`/prompts/*`), `/suggest_issue_times`, `/browse`, `/browse_yaml`, `/history/stream`,
`/history/delete`, `/history/rename`, `/history/pin`, `/history/load`, `/chat/stop`,
`/back_to_avatar`, `/prepare`, `/get_issue_context`, `/`.

## Where

- New file: `unit_tests/test_generic_chatbot_blueprint.py`.
- Reads from: `blueprints/chatbot/chatbot_routes.py` (`GENERIC_PROFILE`, `generic_chatbot_bp`,
  route handlers — same `AgentRoutes`/factory machinery BT and Wi-Fi use, so the same mocking
  points apply: `_get_or_create_agent`, `_resume_agent_for`, `chat_jobs`, `history_service`,
  `feedback_service`, `gather_service`, `check_ips_service`, `find_event_log_for_log`).
- `configs/chatbot_ui.py:316` (`GENERIC_UI`) — confirms the route table: its `features` dict
  (`sidebar_toggle`, `issue_time`, `feedback`, `history`, `skill_editor`, `skill_append`,
  `prompt_editor`, all `True`) is identical to `BT_UI`'s (`configs/chatbot_ui.py:104-113`), so
  `generic_chatbot_bp` registers the same 38 rules as `bt_chatbot_bp`, just under `/generic_chatbot`
  instead of `/bt_chatbot` — verify this by running the structure test, don't assume it.
- `unit_tests/base_test.py` (`BaseTestCase`) — the shared fixture every blueprint test module uses;
  no changes needed, just subclass it.

## Steps

1. Model the new file directly on `unit_tests/test_bt_chatbot_blueprint.py`'s structure (imports,
   `GenericChatbotTestCase(BaseTestCase)` with `blueprint = generic_chatbot_bp`, the
   `INSTANCE_METHODS` frozenset, `make_agent()`/`sse_events()` helpers, `patch_route`/
   `patch_target`/`patch_services` helpers) — copy the mechanism, not BT-specific values.
2. Swap every BT-specific value for Generic's: `LOG_PATH`/`OTHER_LOG` fixtures use a generic-looking
   path (no `.hci.txt`, since Generic's `browse_filetypes` accepts any extension —
   `chatbot_routes.py:2189`); `domain="generic"` everywhere BT tests assert `domain="bt"`;
   `GENERIC_UI` instead of `BT_UI`; endpoint prefix `generic_chatbot.*`; the view-function lookup
   uses `"generic_chatbot.chat"`.
3. Write `TestGenericChatbotStructure`: blueprint registered under `/generic_chatbot`, and the full
   `EXPECTED_RULES` route-to-methods map (derive it by running the app's `url_map` filtered to
   `generic_chatbot.*`, don't hand-copy BT's table and assume it matches).
4. Write `TestSetLog` covering: missing log_path (400), first load (primes agent, returns
   metadata, tags `domain="generic"`), switching logs (rotation), reloading the same log (history
   preserved vs. restored from `history_service`), optional-metadata failures degrading to
   defaults, agent-creation failure (500).
5. Write `TestChat` covering: empty message (400), IPS gate (428), a streamed step + done event
   with the turn recorded under `domain="generic"`, numeric parameter clamping, no-log-loaded
   error, missing-agent-log recovered from session, worker failure streaming an error and booking
   a failed status, agent-resolution error.
6. Write `TestReset` covering: successful reset, reset failure (500). (`/chat/stop` is out of
   scope per the owner's "core flows only" choice — skip it.)
7. Write `TestHistoryEndpoints` covering only `/history/list` (merge of stored conversations +
   running jobs, ordering, failure case) and `/history/get` (requires `conversation_id`, returns
   the stored turns, 404/empty case). Do not add `/history/stream`, `/delete`, `/rename`, `/pin`,
   `/load` — out of scope.
8. Run the new file in isolation first, then the full suite, to confirm no cross-module fixture
   leakage (`BaseTestCase.setUpClass` rebinds `app_config` process-wide — see its docstring).

## Validate

A real test suite exists for this exact kind of module (`unittest`, run via `unittest discover` or
pytest against `unit_tests/`). Use it — this is not a case where only a manual checklist exists:

```
# Python 3.10 venv (see PROGRESS.md rows 04-05 for why — the repo's own .venv can't install
# requirements.txt's compiled deps on Python 3.14):
C:\Users\kjfang\AppData\Local\Programs\Python\Python310\python.exe -m unittest unit_tests.test_generic_chatbot_blueprint -v
C:\Users\kjfang\AppData\Local\Programs\Python\Python310\python.exe -m unittest discover -s unit_tests -v
```

Both must pass with zero failures/errors before this sub-task is done.

## Done when

The new test file exists, covers exactly the scope listed above (no more, no less — the deferred
list stays deferred until someone opens a new sub-task for it), both commands in Validate pass, and
the `PROGRESS.md` row for 07 is updated (`Status: done`, `Validated: yes`). Only then does
`README.md`'s Status move to `shipped` and the folder move to
`docs/workpad/finished/generic_log_chatbot/`, per the skill's "Ongoing use" step 7.
