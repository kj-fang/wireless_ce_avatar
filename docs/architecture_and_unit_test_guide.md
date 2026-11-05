# Architecture & Unit Test Guide — Blueprint Layer

This guide explains how the Flask blueprint layer of Intel Wireless CE Avatar is put together, which parts of it make good unit-test targets, and how the stdlib-`unittest` suite under [unit_tests/](../unit_tests/) decides whether a test passes. Per-blueprint details live in the `docs/<blueprint>_bp_analysis.md` files listed at the end.

---

## 1. Blueprint Architecture

### 1.1 Application assembly

[app.py](../app.py) `create_app()` builds one `Flask` app with server-side sessions (`flask_session`, filesystem store under `~/.intelavatar_sessions`), wraps it in a `SocketIO(async_mode='threading')`, and registers twelve feature blueprints:

```
create_app()
 ├─ Flask(__name__) + Session(app) + SocketIO(app)
 ├─ register_blueprint(...)      main, analysis_etl, download, llm, automation, bsod,
 │                               log_parser, log_chatbot, bt_chatbot, nw_analysis, feedback, handsfree
 ├─ download_routes.register_socketio_handlers(socketio)     /progress: start_download, cancel_download
 ├─ log_parser_routes.register_socketio_handlers(socketio)   /progress, /wpp_progress, /sendto-progress
 └─ automation_routes.register_socketio_handlers(socketio)   /progress: trigger_run_latest_etl
__main__:  argparse (--silent-mode, --port, --tray-mode, SendTo paths) → app_config.silent_mode = args.silent_mode
           set_up(socketio) → app_config.driver_manager.run_driver(...)   (Selenium/Chrome UI shell)
```

### 1.2 Layers

```
 Browser (Chrome driven by DriverManager)
    │  HTTP (fetch/forms/SSE)            │  Socket.IO (/progress, /wpp_progress, /sendto-progress)
    ▼                                    ▼
 ┌─────────────────────────── blueprints/<feature>/<feature>_routes.py ───────────────────────────┐
 │ request parsing · session reads/writes · validation · status codes · render_template/jsonify  │
 └──────┬──────────────────────────┬───────────────────────────┬──────────────────────────────────┘
        │ module-level service     │ app_config singleton       │ models.CaseContext
        │ singletons / modules     │ (configs/global_configs.py)│ (session ⇄ dataclass)
        ▼                          ▼                            ▼
 services/*  (analysis_service_wifi/bt/fw, log_parser_service, *_chatbot_service, gather_service,
             feedback_service, history_service, chat_jobs, handsfree/*, case_info_service, llm_service)
        │
        ▼
 utils/* (attachment_download/decompose, etl_utils, helpers, …) · external: IPS/Salesforce, LLM APIs,
 PotatoFarm, Snowflake telemetry, TextAnalysisTool / Explorer (subprocess), network shares, Selenium
```

| Blueprint | Prefix | Responsibility | Route module |
|---|---|---|---|
| main | `/` | Case entry, attachment selection, download result pages, event-log tools | [main_routes.py](../blueprints/main/main_routes.py) |
| analysis_etl | `/analysis_etl` | Dispatch Wi-Fi/BT ETL analysis, async FW analysis | [analysis_etl_routes.py](../blueprints/analysis_etl/analysis_etl_routes.py) |
| download | `/download` | Attachment download worker (SocketIO) + HTTP cancel | [download_routes.py](../blueprints/download/download_routes.py) |
| llm | `/llm` | AI case summary / classification | [llm_routes.py](../blueprints/llm/llm_routes.py) |
| automation | `/automation` | One-click "run latest ETL" (SocketIO) | [automation_routes.py](../blueprints/automation/automation_routes.py) |
| bsod | `/bsod` | Submit BSOD dumps to PotatoFarm | [bsod.py](../blueprints/bsod/bsod.py) |
| log_parser | `/log_parser` | Local/SendTo log upload, parsing, LLM log analysis | [log_parser_routes.py](../blueprints/log_parser/log_parser_routes.py) |
| log_chatbot | `/log_chatbot` | Wi-Fi log agent chat, skills YAML, history | [chatbot_routes.py](../blueprints/chatbot/chatbot_routes.py) (unified `log_chatbot_bp`) |
| bt_chatbot | `/bt_chatbot` | BT log agent chat, skills YAML, history | [chatbot_routes.py](../blueprints/chatbot/chatbot_routes.py) (unified `bt_chatbot_bp`) |
| nw_analysis | `/nw_analysis` | Network/sleep-study analysis agent | [nw_analysis_routes.py](../blueprints/nw_analysis/nw_analysis_routes.py) |
| feedback | `/feedback` | Thumbs/step/skill feedback capture | [feedback_routes.py](../blueprints/feedback/feedback_routes.py) |
| handsfree | `/handsfree` | Handsfree Replyer run control + draft review queue | [handsfree_routes.py](../blueprints/handsfree/handsfree_routes.py) |

### 1.3 Shared state: the `app_config` singleton

[configs/global_configs.py](../configs/global_configs.py) defines `GlobalConfig` and a module-level instance `app_config` that holds `socketio`, `driver_manager`, `llm_helper`, agent backends, `key` (API secrets), directory paths, `sendto_token`, `download_results` and — **on this branch** — `silent_mode`.

Every route module does `from configs.global_configs import app_config`, which **binds a reference to the object at import time**. Consequences for testing:

- Patching only `configs.global_configs.app_config` does not affect modules that were imported earlier — they still hold the real object.
- Any module imported *after* the patch would bind the mock permanently.

[unit_tests/base_test.py](../unit_tests/base_test.py) solves both: it imports every blueprint up front, then in `setUpClass` swaps the singleton for a `MagicMock` and repoints **every** loaded module whose `app_config` attribute `is` the real object (identity match). All patchers are stopped in `tearDownClass`, so the real singleton is restored and test modules are independent of import order (important for the VS Code test runner, which imports test modules before any `setUpClass`).

### 1.4 Session and `CaseContext`

[models/models.py](../models/models.py) `CaseContext` is a dataclass (`case_nbr, id, backend_id, subject, description, env_detail, subcategory, comments, attachment_info, attachment_list, case_download_dir, ips_pdf_path, error_message, wifi_or_bt, files_coexist`). Routes store it in `session["case_context"]` via `to_session()` and read it back with `CaseContext.from_session(...)`. `to_session()` writes heavy fields (`comments`, `attachment_info`, `attachment_list`) to a sidecar JSON in `case_download_dir`; the test fixture patches `to_session` to return `to_dict()` (identical to its no-download-dir behavior) so no file is written.

### 1.5 Branch-specific behavior on this branch

| Feature | Where | Effect | Tested in |
|---|---|---|---|
| `app_config.silent_mode` (`--silent-mode`) | `analysis_etl` (`process_etl_path`, `process_etl_path_fw`), `automation` (`handle_run_latest_etl`, Wi-Fi and BT branches) | `subprocess.run(['explorer', …])` only when `silent_mode` is False; analysis still runs | [analysis_etl](analysis_etl_bp_analysis.md), [automation](automation_bp_analysis.md) |
| `silent_mode` in the service layer | `wpp_ddd_parser_run` / `bt_decode_via_cli` (called by `log_parser`) | GUI opens gated inside the parsers, invisible at the blueprint layer | noted in [log_parser](log_parser_bp_analysis.md) |
| `silent_mode` **not** applied | `main./open_path` | User-clicked "open folder" always launches Explorer | [main](main_bp_analysis.md) |
| IPS Environment Details (`env_detail`) | `services/handsfree/runner.py`, `case_reader.py` | Form answers fed to the reader prompt (`{env_block}`), retract the "repro steps missing" gap, build the agent's platform brief | [handsfree](handsfree_bp_analysis.md) (`TestHandsfreeEnvDetail`) |
| `env_detail` in the LLM case summary | `llm.get_llm_analysis` | Full case dict incl. `env_detail` passed to `analyze_desc` | [llm](llm_bp_analysis.md) |

---

## 2. Good vs Poor Unit-Test Targets

### 2.1 Good targets (deterministic once dependencies are mocked)

| Target | Why it is a good unit-test target | Examples |
|---|---|---|
| Input validation & status codes | Pure branching on request/session data; contract the front end depends on | `cancel_fw_analysis` 400/404, `feedback` id validation, `handsfree` empty draft 400 |
| Dispatch decisions | "Which service is called with which kwargs" is observable with one mock | `process_etl_path` Wi-Fi vs BT, `automation` Wi-Fi vs BT service |
| Feature flags / gates | Boolean in → side effect yes/no | `app_config.silent_mode` explorer gates |
| Response shape | JSON keys and `render_template` kwargs are the UI contract | `fw_analysis_result`, `llm` JSON, `download` emit payloads |
| Session contract | What a route reads/writes in `session` | `llm` → `classification`, `ai_ips_analysis` |
| Pure helpers | No IO at all | `_format_env_detail`, `_env_brief`, `etl_utils` sort keys, bt_chatbot `_pick_preferred` |
| SocketIO handler logic | Callable directly with a request context and a mock `socketio` | `handle_run_latest_etl`, `handle_start_download` |
| Error mapping | Exception in a dependency → user-facing message/status | `bsod` RequestException flash, `discover_fields` 500 |

### 2.2 Poor targets (better served by integration / manual tests)

| Flow | Why unit tests add little | What the suite does instead |
|---|---|---|
| Selenium/Chrome downloads (`DriverManager`, `run_dload_threads`) | Real browser, timing, network | Mocked; only the orchestration around it is tested |
| Background daemon threads, polling loops, psutil kill loops | Non-deterministic scheduling | `Thread` replaced by a synchronous stand-in where logic matters; otherwise ⚪ out of scope |
| LLM output quality, agent reasoning | Non-deterministic, external | LLM/agent mocked; tests assert we *call* it correctly and *handle* its result/failure |
| tkinter file dialogs (`browse*` routes) | Opens real GUI windows | Dialog patched; route contract only |
| Real ETL parsing / TextAnalysisTool / Explorer | External executables, real files | `subprocess` and parsers patched |
| Network shares, IPS/Salesforce, PotatoFarm, Snowflake | Environment-dependent | Patched at point of use |
| Template rendering / page layout | Needs the browser to judge | `render_template` patched; kwargs asserted |
| Very long page builders (e.g. `main.render_download_result_form`) | Hundreds of branches over real file trees | Main observable branches covered; the rest listed as ❌ NOT TESTED in the doc |

Rule of thumb: **unit-test the decision, mock the effect.**

---

## 3. How a Unit Test Determines Pass or Fail

### 3.1 Arrange → Act → Assert

Every test follows the same three steps (see [unit_tests/test_bsod_blueprint.py](../unit_tests/test_bsod_blueprint.py)):

```python
@patch(f"{ROUTES}.requests.post")                                   # Arrange: replace the effect
@patch(f"{ROUTES}.helpers.detect_user_email", return_value=USER_EMAIL)
def test_submit_success_posts_payload_and_redirects(self, mock_email, mock_post):
    mock_post.return_value = MagicMock(status_code=200)             # Arrange: fix the dependency's answer
    self.seed_session(download_path=DUMPS_PATH)                     # Arrange: fix the input state

    resp = self.client.post("/bsod/bsod_submit")                    # Act: one request

    self.assertEqual(resp.status_code, 302)                         # Assert: observable outcome
    self.assertEqual(json.loads(mock_post.call_args.kwargs["data"]),
                     {"DUMPS_PATH": DUMPS_PATH, "EMAIL_LIST": USER_EMAIL})
```

- **Arrange** fixes *everything* the route can observe: session keys, query/JSON params, `app_config` attributes (e.g. `silent_mode`), and the return value or exception of every patched dependency.
- **Act** performs exactly one request (or one direct handler call).
- **Assert** checks only observable outputs: status code, body substring / JSON fields, redirect `Location`, flash messages, session contents, and **whether and how** each mocked dependency was called (`assert_called_once_with(...)`, `assert_not_called()`).

A test **passes** when every assertion holds and no unexpected exception escapes. It **fails** when an assertion is false (`AssertionError`) and **errors** when an unexpected exception propagates (Flask's `TESTING=True` re-raises route exceptions instead of returning a 500, which is how tests like `test_missing_case_context_raises_key_error` assert current crash behavior explicitly with `assertRaises`).

### 3.2 Why mocking makes the output deterministic

A route's output is a function of its inputs **and** of everything it talks to. Real dependencies (LLM, network, clock, filesystem, threads, GUI) change between runs; replacing them with `MagicMock`s that return fixed values turns the route into a pure function of the test's Arrange step:

```
 real:   output = f(request, session, LLM(t), network(t), disk(t), threads(t))   → varies
 mocked: output = f(request, session, fixed, fixed, fixed, synchronous)        → identical every run
```

Patching **at the point of use** (`patch("blueprints.<bp>.<module>.<name>")`) matters because each route module holds its own reference to imported names; patching the original definition would miss references that were already bound. The fixture applies the same principle to `app_config`.

Hermeticity checklist the suite follows: no real network, LLM, subprocess, Selenium, GUI dialog or filesystem write; `CaseContext.to_session` sidecar writes are patched; `threading.Thread` is replaced or patched wherever a route starts one.

*Known exception outside the tests' control:* importing the application (any blueprint) executes `utils/assert_code_utils.py`, which at import time writes `assert_codes_cache.json` into the user's Downloads folder (`IntelAvatar_files`). This is the app's own import-time behavior, not something a test does; it cannot be avoided without changing source.

### 3.3 Tests verify a contract, not full correctness

A green suite means: *given these inputs and these dependency answers, the blueprint makes these decisions and produces these outputs.* It does **not** mean:

- the services, parsers, LLM prompts or agent produce correct analyses;
- the real browser, SocketIO transport, templates or network shares work;
- behavior is correct for inputs the tests did not enumerate;
- current behavior is *desired* behavior — several tests deliberately pin **known bugs** (e.g. automation's DDD `NameError`, log_parser's `/get_filter_details` path traversal, `KeyError` on a missing session) so a fix shows up as an intentional test change. These are labeled in the test docstrings and in each doc's coverage section.

Each blueprint doc's "What Would Make These Tests Fail?" table shows exactly which source changes the contract catches, and the ⚪ / ⚠️ / ❌ items in its "Test Coverage Analysis" show what it does not.

---

## 4. Test Framework Reference

### 4.1 Running

```powershell
# whole suite (-b buffers the app's print() output; success = final "OK" / exit code 0)
& .\venv\Scripts\python.exe -m unittest discover -s unit_tests -b
# one blueprint, verbose
& .\venv\Scripts\python.exe -m unittest -b unit_tests.test_analysis_etl_blueprint -v
```

`unittest` writes progress to **stderr**, and error-path tests make the app print tracebacks; red text or "Traceback" lines are not failures by themselves — only the final `FAILED (...)` line / non-zero exit code is.

### 4.2 Fixture (`unit_tests/base_test.py`)

```
BaseTestCase.setUpClass
  ├─ real = configs.global_configs.app_config
  ├─ patch('configs.global_configs.app_config', MagicMock)
  └─ for every module in sys.modules with vars(module)['app_config'] is real → patch.object(module, 'app_config', mock)
BaseTestCase.setUp
  ├─ _configure_app_config(mock): socketio / llm_helper / key / driver_manager = MagicMock(),
  │     silent_mode=False, project_root / avatarfiles_dir / driver_dir = unit_tests/_fake_* (never created),
  │     sendto_token='unit-test-token', agents=None
  ├─ patch CaseContext.to_session → to_dict()   (no sidecar file)
  ├─ Flask app: TESTING=True, SECRET_KEY, WTF_CSRF_ENABLED=False
  ├─ register cls.blueprint; register stub Blueprint('main') for cls.main_stub_endpoints
  └─ test client + app context
Helpers: create_mock_case_context(**overrides) (built via CaseContext(...).to_dict(), so field drift fails loudly),
         seed_session(**extra), get_session(), register_main_stub(*endpoints), main_stub_path(endpoint)
BaseTestCase.tearDown / tearDownClass: pop context; stop every patcher
```

### 4.3 Conventions

- One `unit_tests/test_<blueprint>_blueprint.py` per blueprint; `ROUTES = 'blueprints.<bp>.<module>'` at the top.
- First test class asserts registration and `url_prefix`.
- Routes that `redirect(url_for('main.<endpoint>'))` get a stub `main` blueprint so `url_for` resolves without importing the real main routes' dependencies.
- SocketIO handlers are imported and called directly inside `app.test_request_context()`.
- Branch-specific behavior is called out in the test docstring and in the blueprint doc.

---

## 5. Per-Blueprint Documents

| Blueprint | Doc | Test module |
|---|---|---|
| main | [main_bp_analysis.md](main_bp_analysis.md) | [test_main_blueprint.py](../unit_tests/test_main_blueprint.py) |
| analysis_etl | [analysis_etl_bp_analysis.md](analysis_etl_bp_analysis.md) | [test_analysis_etl_blueprint.py](../unit_tests/test_analysis_etl_blueprint.py) |
| download | [download_bp_analysis.md](download_bp_analysis.md) | [test_download_blueprint.py](../unit_tests/test_download_blueprint.py) |
| llm | [llm_bp_analysis.md](llm_bp_analysis.md) | [test_llm_blueprint.py](../unit_tests/test_llm_blueprint.py) |
| automation | [automation_bp_analysis.md](automation_bp_analysis.md) | [test_automation_blueprint.py](../unit_tests/test_automation_blueprint.py) |
| bsod | [bsod_bp_analysis.md](bsod_bp_analysis.md) | [test_bsod_blueprint.py](../unit_tests/test_bsod_blueprint.py) |
| log_parser | [log_parser_bp_analysis.md](log_parser_bp_analysis.md) | [test_log_parser_blueprint.py](../unit_tests/test_log_parser_blueprint.py) |
| log_chatbot | [log_chatbot_bp_analysis.md](log_chatbot_bp_analysis.md) | [test_log_chatbot_blueprint.py](../unit_tests/test_log_chatbot_blueprint.py) |
| bt_chatbot | [bt_chatbot_bp_analysis.md](bt_chatbot_bp_analysis.md) | [test_bt_chatbot_blueprint.py](../unit_tests/test_bt_chatbot_blueprint.py) |
| nw_analysis | [nw_analysis_bp_analysis.md](nw_analysis_bp_analysis.md) | [test_nw_analysis_blueprint.py](../unit_tests/test_nw_analysis_blueprint.py) |
| feedback | [feedback_bp_analysis.md](feedback_bp_analysis.md) | [test_feedback_blueprint.py](../unit_tests/test_feedback_blueprint.py) |
| handsfree | [handsfree_bp_analysis.md](handsfree_bp_analysis.md) | [test_handsfree_blueprint.py](../unit_tests/test_handsfree_blueprint.py) |
