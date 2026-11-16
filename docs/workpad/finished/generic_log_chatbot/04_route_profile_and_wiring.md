# Sub-task 04: Route profile, blueprint, and app registration

Status: open. Folder: [README.md](README.md). Progress: [PROGRESS.md](PROGRESS.md).
Depends on: 01, 02, 03.

## Goal

Wire `GenericLogAgentSystem`, `generic_skills_yaml_utils`, and `GENERIC_UI` into an actual Flask
blueprint, mounted at `/generic_chatbot`, and give the app-level agent instance somewhere to live
(`app_config.generic_chatbot_agent`). This is the layer where a missing piece shows up loudly: the
route factory validates at import time (see Rules in [README.md](README.md)).

## Where

- `blueprints/chatbot/chatbot_routes.py`:
  - Imports (~lines 85, 128, 134) — alongside the existing Linux imports, add:
    `from services.chatbot.engine.generic import GenericLogAgentSystem`,
    `from utils import generic_skills_yaml_utils as _generic_yaml_utils`,
    `from configs.chatbot_ui import ..., GENERIC_UI` (extend the existing import list).
  - After `LINUX_PROFILE` (ends line 2175), add:
    ```python
    GENERIC_PROFILE = AgentRouteProfile(
        name="generic_chatbot",
        ui=GENERIC_UI,
        agent_class=GenericLogAgentSystem,
        agent_config_attr="generic_chatbot_agent",
        yaml_utils=_generic_yaml_utils,
        user_yaml_prefix="generic_skills_",
        browse_filetypes=(("Log files", "*.log *.log.* *.txt *.*"), ("All files", "*.*")),
        gather_domain="generic",
        window_minutes_cap=100000,
        reset_conversation_on_prepare=True,
        direct_log_suffixes=(),
        read_log_has_date_while_running=False,
        issue_time_focus=None,
    )
    ```
  `read_log_has_date_while_running=False` lands on the same value as BT/Linux, but independently:
  this field guards a real race, not a domain choice — while a background analysis job is still
  running, `True` reads `self._raw_log_cache`/`_raw_log_cache_path`
  (`chatbot_routes.py:1604-1608`) concurrently with the background thread that is actively
  mutating that same pair via `_ensure_raw_log_cache()`. The field's own doc comment
  (`chatbot_routes.py:220`) already flags Wi-Fi's `True` as the outlier ("the guard is right for
  both — Wi-Fi just never got it"), not BT/Linux's `False`. Generic shares this hazard only
  because it shares the same background-job machinery every profile built on
  `WifiLogAgentSystem` uses — not because it is imitating Linux — so `False` is independently
  correct here, with the brief cost already accepted elsewhere (the sidebar shows the cheap
  `log_has_date = True` default, `chatbot_routes.py:1572`, until the job finishes and the page
  reloads).
  `reset_conversation_on_prepare=True` is a deliberate departure from Linux's `False`, not an
  oversight: the field's own doc comment (`chatbot_routes.py:202-204`) flags Linux/BT's `False` as
  likely drift rather than policy ("BT reaching /prepare is also a new analysis, so this looks
  like drift... confirm before unifying"). For Generic, pointing the agent at a new log file *is*
  a new analysis — there is no reason to carry forward an already-suspect inherited value.
  - After line 2179 (`linux_chatbot_bp = create_agent_blueprint(LINUX_PROFILE)`), add:
    `generic_chatbot_bp = create_agent_blueprint(GENERIC_PROFILE)`.
- `blueprints/chatbot/__init__.py` — add `GENERIC_PROFILE`, `generic_chatbot_bp` to both the
  `from .chatbot_routes import (...)` block and `__all__`.
- `blueprints/__init__.py` — change
  `from .chatbot import bt_chatbot_bp, linux_chatbot_bp, log_chatbot_bp` to also import
  `generic_chatbot_bp`, and add it to `__all__` (line 27 area).
- `app.py`:
  - Blueprint import line (~line 56) — add `generic_chatbot_bp` to the `from blueprints import
    (...)` list.
  - Registration (~line 229, after `app.register_blueprint(linux_chatbot_bp)`) — add
    `app.register_blueprint(generic_chatbot_bp)`.
- `configs/global_configs.py`:
  - `__init__` (~line 22, after `self.linux_chatbot_agent`) — add
    `self.generic_chatbot_agent: Optional[Any] = None  # GenericLogAgentSystem (generic skills)`.
  - After `set_linux_chatbot_agent` (~line 89) — add:
    ```python
    def set_generic_chatbot_agent(self, agent: Any) -> None:
        self.generic_chatbot_agent = agent
    ```

## Steps

1. Add imports and `GENERIC_PROFILE`/`generic_chatbot_bp` to `chatbot_routes.py`.
2. Update `blueprints/chatbot/__init__.py` exports.
3. Update `blueprints/__init__.py` exports.
4. Update `app.py` import + `register_blueprint` call.
5. Add the agent slot + setter to `configs/global_configs.py`.

## Validate

No dedicated test file exists for the Linux blueprint either (only
`test_bt_chatbot_blueprint.py`, `test_log_chatbot_blueprint.py`, `test_nw_analysis_blueprint.py`
per the existing `unit_tests/` directory) — validate the same way those give confidence for their
profiles, plus an app-level import check:

- `python -c "import app; print('ok')"` (or however this project's entrypoint is normally
  smoke-tested) succeeds with no `RuntimeError` from `handler_map()` and no `ImportError`.
- `python -c "from blueprints.chatbot import GENERIC_PROFILE, generic_chatbot_bp; print(GENERIC_PROFILE.url_prefix, generic_chatbot_bp.name)"`
  prints `/generic_chatbot generic_chatbot`.
- `python -c "from configs.global_configs import app_config; app_config.set_generic_chatbot_agent('x'); assert app_config.generic_chatbot_agent == 'x'"`
  succeeds.
- Optionally, copy `unit_tests/test_bt_chatbot_blueprint.py`'s structure into a new
  `unit_tests/test_generic_chatbot_blueprint.py` for parity — not required (Linux itself has no
  such file), but consistent with the project's existing coverage if time allows.

## Done when

The app starts cleanly with the new blueprint registered, the checks above pass, and the
PROGRESS.md row for 04 is updated.
