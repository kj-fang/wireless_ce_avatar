# Log Parser Blueprint — Analysis & Unit Test Documentation

- Route module: [blueprints/log_parser/log_parser_routes.py](../blueprints/log_parser/log_parser_routes.py)
- Test module: [tests/test_log_parser_blueprint.py](../tests/test_log_parser_blueprint.py)
- Shared fixture: [tests/base_test.py](../tests/base_test.py)
- Main collaborators: [services/log_parser_service.py](../services/log_parser_service.py), [services/log_parser_file_manage_service.py](../services/log_parser_file_manage_service.py), [services/etl_parser/wpp_ddd_parser.py](../services/etl_parser/wpp_ddd_parser.py), [services/etl_parser/bt_parser.py](../services/etl_parser/bt_parser.py)

## Overview — What Log Parser Does

The `log_parser` blueprint (`url_prefix="/log_parser"`) is the entry point for analysing *local* log files and for the keyword-filter + LLM "log parser" page. It (1) renders the log-parser analysis page and manages filters/prompts, (2) accepts local files from the native file picker (`/pick_local_analysis_file` → `/upload_local_analysis`) or from Windows **Send To** (`/open_local_analysis` + the `start_sendto` Socket.IO event), classifies them (`.log`, `.hci.txt`, BT `ibt*.etl`, FW `wrt-fw-*.etl`, Wi-Fi ETL/DDD, archives, `.dmp`), runs the right parser, and redirects to the Wi-Fi chatbot, BT chatbot, download-result or BSOD pages, (3) supports cooperative cancellation of a running local analysis, and (4) hosts Socket.IO handlers for running the filter+LLM analysis and follow-up chat.

**Branch-specific (`app_config.silent_mode`):** this route module has **no direct `silent_mode` check**. On this branch the GUI-open gating (TextAnalysisTool / explorer) lives one layer down inside `wpp_ddd_parser_run` ([wpp_ddd_parser.py](../services/etl_parser/wpp_ddd_parser.py), `not app_config.silent_mode`) and `bt_decode_via_cli` ([bt_parser.py](../services/etl_parser/bt_parser.py), `if app_config.silent_mode`). Both are patched at their point of use in these tests, so `silent_mode` has no observable effect at the blueprint layer; the mock still defaults it to `False`.

## All Routes (13 endpoints)

| Route | Method | Purpose |
|---|---|---|
| `/log_parser/log_parser` | GET, POST | Render `log_parser.html`: set up output dir, prepare/copy the log, auto-analysis hints, filters & prompts |
| `/log_parser/load_prompt` | POST | Return a prompt file's content via `log_parser_service.load_prompt_content` |
| `/log_parser/upload` | POST | Upload a filter/prompt file via `file_manager_service.handle_file_upload`; resets `local_in_place` |
| `/log_parser/pick_local_analysis_file` | POST | Open a native tkinter file dialog, validate extension, bind the path to a fresh session |
| `/log_parser/upload_local_analysis` | POST | Validate the picker-bound path, run `_process_local_analysis`, return redirect + chatbot hints; supports cancel |
| `/log_parser/cancel_local_analysis` | POST | (localhost) Signal a running upload's cancel event and start a child-process killer thread |
| `/log_parser/open_local_analysis` | GET | (localhost + SendTo token) Validate a Send-To path, store it as pending, render `sendto_transmission.html` |
| `/log_parser/navigate_existing_browser` | POST | (localhost) Navigate the app's existing Chrome to a safe in-app path |
| `/log_parser/verify_sendto_token` | GET | (localhost) Report whether a token matches `app_config.sendto_token` |
| `/log_parser/get_filter_details` | POST | Keywords + enabled state of one `.tat` filter |
| `/log_parser/get_all_filter_details` | POST | Keywords of every `.tat` filter |
| `/log_parser/edit_prompt` | POST | Prompt CRUD via `file_manager_service.handle_prompt_operation` |
| `/log_parser/estimate_tokens` | POST | Token estimate of the session log after filtering/grouping (20,000 limit) |

Non-route entry points also covered: `before_app_request` hook `_pickup_sendto_session`; Socket.IO events `submit_analysis`, `chat_message`, `chat_message_with_filter`, `reset_log_parser_session` (`/progress`), `start_sendto` (`/sendto-progress`), `wpp_events` (`/wpp_progress`, empty); background worker `_run_sendto_in_background`.

## High-Level Flow Diagram

```
 LOCAL FILE PICKER FLOW
 Browser ─POST /pick_local_analysis_file─► tkinter dialog ──none──► 400 / bad ext ─► 400 / ImportError ─► 503 / exc ─► 500
                                            │ ok
                                            ▼
                                session.clear(); session[picked_local_analysis_path]=path ──► 200 {source_path, filename}
 Browser ─POST /upload_local_analysis (source_path[, upload_id, is_bsod])─►
      ├─ empty source_path / no bound path / path ≠ bound path / !exists / is dir / bad ext ──► 400
      ├─ upload_id? ─► _register_local_upload → cancel_event
      ▼
   _process_local_analysis(...)   session: download_path, uploaded_source_path, local_in_place=True, classification
      ├─ .dmp or is_bsod ─► helpers.get_load_path(prim,bkup) ─None─► ValueError(400)
      │                     copy → <share>\local_bsod_<ts>\ ; CaseContext; gather_service.*; bsod=True ─► url main.download_result_bsod
      ├─ .zip/.7z/.rar ──► attachment_decompose.process_single_zip ─empty─► ValueError(400)
      │                     app_config.set_download_results(wifi/ddd+evt/bt/fw); case type bt if bt files ─► url main.download_result
      ├─ .log ───────────► last_analyzed_log_path ─► url log_chatbot.index?auto_run=analyze_all
      ├─ .hci.txt ───────► latest_etl_path ─► url bt_chatbot.index?auto_run=analyze_all
      ├─ ibtpci/ibtusb.etl► bt_decode_via_cli ─None─► ValueError(400) ─► url bt_chatbot.index      [silent_mode gate inside bt_parser]
      ├─ wrt-fw-*.etl ───► infer_fw_parse_type; set_download_results(fw only); auto_analysis_fw ─► url main.download_result
      └─ other (Wi-Fi ETL / dddLog) ► wpp_ddd_parser_run ─► <file>.log ─► url log_chatbot.index   [silent_mode gate inside wpp_ddd_parser]
   except: cancelled / SystemExit|ValueError|Exception after cancel ─► 200 {cancelled:True} + pop session keys
           ValueError ─► 400 · Exception ─► 500 · SystemExit (no cancel) ─► re-raised
   finally: cancelled+cleanup_paths ─► wait children, delete paths; unregister upload_id
      ▼
   200 {success, uploaded_source_path, redirect [, use_chatbot, is_bt, log_path | etl_path]}

 SEND TO FLOW
 Explorer ─► app.py ─► GET /open_local_analysis?token&path ─(non-local/bad token/no path/!exists/dir/bad ext)─► flash danger → main.index
                          │ ok: session[sendto_pending_path]; render sendto_transmission.html
 Browser socket ─ start_sendto ─► no pending / vanished ─► sendto_error
                          │ ok: pop pending; Thread(daemon) ─► _run_sendto_in_background
                          ▼      sendto_progress… (wpp_log forwarded as sendto_wpp_log) → _process_local_analysis(progress_cb)
                          snapshot session → _sendto_session_store[token]; sendto_complete {redirect_url&_st=token}
 Browser ─GET <redirect>?_st=token─► before_app_request _pickup_sendto_session → session.update(snapshot) (TTL 300 s)

 LOG PARSER PAGE + SOCKET.IO
 GET /log_parser ─► set_up(download_path) | in-place source dir ─► copy direct .log/.txt | prepare_log_file ─► render log_parser.html
 submit_analysis ─► validate_analysis_inputs ─✗─► validation_error ─✓─► start_analysis(filter, log, out, llm_helper, prompt, description)
 chat_message / chat_message_with_filter ─► guards ─► log_parser_service.handle_chat_message ─► chat_response {message, message_html}
```

## Dependencies

```
flask:        Blueprint, render_template, request, session, redirect, url_for, flash, jsonify, copy_current_request_context
stdlib:       json, os(.path/.makedirs/.listdir/.getcwd), datetime, hmac, logging, re, shutil, stat, threading, time,
              traceback, uuid, urllib.parse.unquote; tkinter (imported lazily in pick_local_analysis_file)
third-party:  markdown, bleach (_render_safe_markdown_html), werkzeug.utils.secure_filename (unused), psutil (lazy, cancel)
helpers:      utils.helpers (get_long_path, to_long_path, get_load_path, read_log_file)
              utils.attachment_decompose (process_single_zip, ExtractionCancelled)
              utils.log_parser_preprocess (extract_all_keywords_from_filter_file; lazily: extract_enabled_keywords_from_filter_file,
                                           filter_log_by_keywords, preprocess_log_for_llm, group_similar_logs)
              utils.fw_utils.infer_fw_parse_type (lazy)
services:     log_parser_service = LogParserService()      (module singleton)
              file_manager_service = FileManagerService()  (module singleton)
              services.gather_service (new_workflow_id, record_workflow_start, record_attachment_selection,
                                       record_attachment_download_result)
              services.etl_parser.wpp_ddd_parser.wpp_ddd_parser_run   (silent_mode gate lives here)
              services.etl_parser.bt_parser.bt_decode_via_cli          (silent_mode gate lives here)
config:       configs.path_configs: LOG_PARSER_DIR (UNC share), LOAD_PATH_prim, LOAD_PATH_bkup
app_config:   sendto_token, driver_manager.navigate_main_browser, socketio.emit, llm_helper,
              avatarfiles_dir, last_analyzed_log_path (written), set_download_results(...)
models:       models.models.CaseContext (constructor, to_session, to_dict, _SESSION_SIDECAR_NAME)
session keys: read  — picked_local_analysis_path, sendto_pending_path, download_path, local_in_place, uploaded_source_path,
                      classification, latest_etl_path, log_path, logparser_output_dir, case_context
              write — the above plus bsod, latest_etl_llm, selected_files, gather_workflow_id, auto_analysis_fw
cross-bp:     url_for('main.index' | 'main.download_result' | 'main.download_result_bsod'),
              url_for('log_chatbot.index'), url_for('bt_chatbot.index')  → stub blueprints in tests
```

## Test Coverage Analysis

```
STRUCTURE
  ✅ blueprint name/prefix + exact 13 rules & methods ─────────────── test_blueprint_registered_with_url_prefix
  ✅ 6 Socket.IO events bound to correct namespaces ────────────────── test_register_socketio_handlers_binds_all_events

ROUTES
  /log_parser (render_log_parser_form)
    ✅ default: set_up + prepare_log_file + template context ──────── test_renders_form_with_prepared_log
    ✅ preselect_filter + preselect_prompt override ───────────────── test_preselect_filter_and_prompt_force_auto_analysis
    ✅ direct .log/.txt copied with shutil.copy2 ──────────────────── test_direct_log_file_is_copied_into_output_dir
    ✅ local_in_place → source dir + <etl>.log ────────────────────── test_local_in_place_uses_source_dir_and_existing_log
    ✅ stale local_in_place reset ─────────────────────────────────── test_stale_local_in_place_flag_is_reset
    ❌ NOT TESTED: in-place mode where <etl>.log does not exist (log_path=None)
  /load_prompt ✅ test_load_prompt_returns_service_content
  /upload      ✅ test_upload_resets_local_in_place_and_delegates
  /edit_prompt ✅ no action → test_edit_prompt_requires_action · ✅ delegate → test_edit_prompt_delegates_to_file_manager
  /pick_local_analysis_file
    ✅ success binds cleared session ── test_selected_file_is_bound_to_cleared_session
    ✅ no selection 400 ── test_no_selection_returns_400        ✅ bad ext 400 ── test_invalid_extension_returns_400
    ✅ dialog exception 500 ── test_dialog_exception_returns_500 ✅ no tkinter 503 ── test_tkinter_unavailable_returns_503
  /upload_local_analysis — validation
    ✅ no source_path ── test_missing_source_path_returns_400   ✅ no bound path ── test_no_picked_path_in_session_returns_400
    ✅ path mismatch ── test_source_path_mismatch_returns_400   ✅ !exists ── test_missing_file_returns_400
    ✅ directory ── test_directory_returns_400                  ✅ bad ext ── test_disallowed_extension_returns_400
  /upload_local_analysis — _process_local_analysis branches
    ✅ .log ── test_log_file_routes_to_wifi_chatbot             ✅ .hci.txt ── test_hci_txt_routes_to_bt_chatbot
    ✅ BT etl ── test_bt_etl_is_decoded_then_routes_to_bt_chatbot ✅ BT decode fail ── test_bt_etl_decode_failure_returns_400
    ✅ Wi-Fi etl ── test_wifi_etl_runs_wpp_parser_then_routes_to_wifi_chatbot
    ✅ FW etl ── test_fw_etl_routes_to_download_result
    ✅ archive ── test_archive_is_extracted_and_routes_to_download_result
    ✅ empty archive ── test_archive_without_supported_files_returns_400
    ✅ .dmp ── test_dmp_is_copied_to_bsod_share_and_routes_to_bsod_result
    ✅ no BSOD share ── test_dmp_without_bsod_share_returns_400
    ✅ is_bsod flag ── test_is_bsod_flag_forces_bsod_flow_for_non_dmp_file
    ❌ NOT TESTED: FW etl where infer_fw_parse_type returns 'coex'/None (collapses to 'wifi')
  /upload_local_analysis — error / cancel handling
    ✅ Exception → 500 ── test_unexpected_exception_returns_500
    ✅ SystemExit w/o cancel re-raised ── test_system_exit_without_cancel_propagates
    ✅ SystemExit after cancel ── test_system_exit_after_cancel_is_reported_as_cancelled
    ✅ _LocalAnalysisCancelled + cleanup + unregister ── test_cancelled_upload_returns_cancelled_and_clears_session
    ✅ ValueError after cancel ── test_value_error_after_cancel_is_reported_as_cancelled
    ❌ NOT TESTED: generic Exception after cancel (same _cancelled_response as the two above)
  /cancel_local_analysis
    ✅ 403 ── TestCancelLocalAnalysis.test_non_localhost_is_rejected ✅ 400 ── test_missing_upload_id_returns_400
    ✅ unknown id ── test_unknown_upload_id_reports_inactive
    ✅ active id, killer once ── test_active_upload_is_signalled_and_killer_started_once
  /open_local_analysis
    ✅ 403-equivalent ── TestOpenLocalAnalysis.test_non_localhost_is_rejected ✅ token ── test_invalid_token_is_rejected
    ✅ no path ── test_missing_path_is_rejected  ✅ !exists ── test_nonexistent_file_is_rejected
    ✅ dir ── test_directory_is_rejected         ✅ bad ext ── test_disallowed_extension_is_rejected
    ✅ success ── test_valid_request_stores_pending_path_and_renders_page
  /navigate_existing_browser
    ✅ 403 ── TestNavigateExistingBrowser.test_non_localhost_is_rejected ✅ unsafe paths ── test_unsafe_startup_paths_return_400
    ✅ no driver 409 ── test_missing_driver_manager_returns_409          ✅ nav error 409 ── test_navigation_error_returns_409
    ✅ success, fixed origin ── test_success_navigates_with_fixed_localhost_origin
  /verify_sendto_token
    ✅ 403 ── TestVerifySendtoToken.test_non_localhost_is_rejected ✅ match ── test_matching_token
    ✅ mismatch/empty ── test_mismatched_and_empty_tokens
  /get_filter_details
    ✅ no name ── test_get_filter_details_requires_filter_file   ✅ missing ── test_get_filter_details_missing_file
    ✅ success ── test_get_filter_details_success                ✅ error ── test_get_filter_details_read_error
    ⚠️ raw name joined (no basename) ── test_get_filter_details_does_not_strip_directory_components (current behavior, flagged)
  /get_all_filter_details
    ✅ no dir ── test_get_all_filter_details_missing_dir ✅ sorted .tat ── test_get_all_filter_details_lists_sorted_tat_files
    ✅ error ── test_get_all_filter_details_error
  /estimate_tokens
    ✅ no filter ── test_requires_filter_file  ✅ no log ── test_requires_log_path_in_session
    ✅ missing filter + basename ── test_missing_filter_file_returns_400
    ✅ success ── test_success_reports_token_counts  ✅ error 500 ── test_processing_error_returns_500

CORE LOGIC
  ✅ _pickup_sendto_session restore once ── test_valid_token_restores_session_once
  ✅ _pickup_sendto_session TTL purge ── test_expired_tokens_are_purged_and_not_applied
  ✅ _is_allowed_local_analysis_filename ── test_allowed_local_analysis_filenames
  ✅ _is_bt_etl / _is_fw_etl / _infer_local_upload_case_type ── test_bt_and_fw_etl_detection_and_case_type
  ✅ _format_bytes ── test_format_bytes
  ✅ _render_safe_markdown_html (XSS) ── test_render_safe_markdown_strips_scripts
  ✅ _process_local_analysis directory guard ── test_process_local_analysis_rejects_directory_before_touching_session
  ✅ cancel registry (_register/_signal/_unregister/_raise_if_cancelled) ── test_cancel_registry_signal_and_raise
  ⚪ _copy_file_with_console_progress ── real chunked file copy + partial-file delete; needs real files (patched out in route tests)
  ⚪ _force_rmtree / _delete_cancel_cleanup_paths ── destructive filesystem deletion with retries; patched out (no FS writes allowed)
  ⚪ _wait_for_cancel_children_to_exit / _kill_local_analysis_children_until_done ── psutil process polling/termination loops
  ⚪ zip-branch _track_cleanup when extract folder / sidecar don't pre-exist ── exists() patched True; only matters with real FS
  ⚪ progress_cb throttled per-file extraction progress (_extract_progress) ── only with a real progress-emitting extractor

BACKGROUND / SOCKETIO
  ✅ submit_analysis: invalid (given socketio) ── test_submit_analysis_validation_error_emits_on_given_socketio
  ✅ submit_analysis: invalid (app_config fallback) ── test_submit_analysis_validation_error_falls_back_to_app_socketio
  ✅ submit_analysis: start OK ── test_submit_analysis_starts_service_with_case_description
  ✅ submit_analysis: start False ── test_submit_analysis_start_failure_emits_analysis_error
  ✅ submit_analysis: exception ── test_submit_analysis_exception_emits_analysis_error
  ✅ chat_message: empty / no history / no LLM / success / exception ── test_chat_message_* (5 tests)
  ✅ chat_message_with_filter: 3 guards / no LLM / success / exception ── test_filter_chat_* (4 tests)
  ✅ reset_log_parser_session: success / failure ── test_reset_session_success, test_reset_session_failure
  ✅ start_sendto: no pending / vanished / starts worker ── test_start_sendto_* (3 tests)
  ✅ _run_sendto_in_background: success / ValueError / Exception ── test_background_worker_* (3 tests, run synchronously)
  ⚪ wpp_events handler ── empty `pass` (only keeps the namespace open); nothing to assert beyond registration
  ⚪ real daemon thread execution of start_sendto / cancel killer ── threading.Thread patched; worker logic tested synchronously instead
```

## What the Unit Tests Actually Test

Shared fixture (`LogParserTestCase`): `blueprint = log_parser_bp`; `main_stub_endpoints = ("index", "download_result", "download_result_bsod")`; extra stub blueprints `log_chatbot` and `bt_chatbot` each exposing `index` at `/__<name>_stub__/index`. Module constants: `SRC_DIR = r"C:\fake\local"`, `TIMESTAMP = "20260101_120000"`, `FILTER_DIR = os.path.join(LOG_PARSER_DIR, "filter")`, `NON_LOCAL = {"REMOTE_ADDR": "10.0.0.5"}`, `_long(p) = "\\\\?\\" + p`. Mock `app_config.sendto_token = "unit-test-token"` (from `BaseTestCase`). `TestLogParserHelpers` is a plain `unittest.TestCase` (no Flask app).

### 1. TestLogParserBlueprintStructure.test_blueprint_registered_with_url_prefix

**Description:** Registers `log_parser_bp` on a fresh app and checks the blueprint name, `url_prefix="/log_parser"`, that the set of URL rules is EXACTLY the 13 expected paths (13 route functions) and that each rule allows its declared methods. Catches added/removed/renamed routes and narrowed methods.

**Code:**
```python
def test_blueprint_registered_with_url_prefix(self):
    # Arrange / Act
    rules = {r.rule: r for r in self.app.url_map.iter_rules() if r.endpoint.startswith("log_parser.")}

    # Assert
    self.assertIs(self.app.blueprints.get("log_parser"), log_parser_bp)
    self.assertEqual(log_parser_bp.url_prefix, "/log_parser")
    expected = {
        "/log_parser/log_parser": {"GET", "POST"},
        "/log_parser/load_prompt": {"POST"},
        "/log_parser/upload": {"POST"},
        "/log_parser/pick_local_analysis_file": {"POST"},
        "/log_parser/upload_local_analysis": {"POST"},
        "/log_parser/cancel_local_analysis": {"POST"},
        "/log_parser/open_local_analysis": {"GET"},
        "/log_parser/navigate_existing_browser": {"POST"},
        "/log_parser/verify_sendto_token": {"GET"},
        "/log_parser/get_filter_details": {"POST"},
        "/log_parser/get_all_filter_details": {"POST"},
        "/log_parser/edit_prompt": {"POST"},
        "/log_parser/estimate_tokens": {"POST"},
    }
    self.assertEqual(set(rules), set(expected))
    for rule, methods in expected.items():
        self.assertTrue(methods.issubset(rules[rule].methods), rule)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Registered blueprint | `log_parser_bp` (+ stub `main`, `log_chatbot`, `bt_chatbot`) | From `LogParserTestCase.setUp` |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `app.blueprints['log_parser']` | `log_parser_bp` | `assertIs` |
| `url_prefix` | `"/log_parser"` | `assertEqual` |
| Rule set | 13 paths listed in `expected` | `assertEqual(set(rules), set(expected))` |
| Methods per rule | e.g. `/log_parser/log_parser` ⊇ `{GET, POST}` | `assertTrue(issubset)` |

| Pass Condition | Fail Condition |
|---|---|
| All names/paths/methods match | Route added, removed, renamed, prefix changed, or method dropped |

### 2. TestLogParserBlueprintStructure.test_register_socketio_handlers_binds_all_events

**Description:** Calls `register_socketio_handlers` with a fake SocketIO whose `.on()` records `(event, namespace)`. Verifies the 6 handlers are bound to the right namespaces in order — a wrong namespace silently disconnects the front end.

**Code:**
```python
def test_register_socketio_handlers_binds_all_events(self):
    # Arrange
    fake_socketio = MagicMock()
    registered = []
    fake_socketio.on.side_effect = lambda event, namespace=None: (
        lambda fn: registered.append((event, namespace)) or fn)

    # Act
    lp.register_socketio_handlers(fake_socketio)

    # Assert
    self.assertEqual(registered, [
        ("wpp_events", "/wpp_progress"),
        ("submit_analysis", "/progress"),
        ("chat_message", "/progress"),
        ("chat_message_with_filter", "/progress"),
        ("reset_log_parser_session", "/progress"),
        ("start_sendto", "/sendto-progress"),
    ])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `socketio` | `MagicMock()` with `on.side_effect` recorder | Captures decorator calls |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Registered events | `wpp_events@/wpp_progress`, `submit_analysis`, `chat_message`, `chat_message_with_filter`, `reset_log_parser_session` @`/progress`, `start_sendto@/sendto-progress` | `assertEqual(registered, [...])` |

| Pass Condition | Fail Condition |
|---|---|
| Exact list of 6 (event, namespace) pairs | Event renamed, namespace changed, handler added/removed |

### 3. TestLogParserForm.test_renders_form_with_prepared_log

**Description:** Default path of `render_log_parser_form`: no in-place flag, an `etl_path` query arg (URL-encoded) → `log_parser_service.set_up(download_path)` creates the output dir and `prepare_log_file` produces the log. Verifies the default classification passed to auto-analysis, every key `render_template` receives, and the session keys written.

**Code:**
```python
def test_renders_form_with_prepared_log(self, mock_svc, mock_render):
    # Arrange
    self._configure_service(mock_svc)
    self.seed_session(download_path=r"C:\fake\dl")

    # Act
    resp = self.client.get("/log_parser/log_parser?etl_path=C%3A%5Cfake%5Cdl%5Cwifi.etl")

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_data(as_text=True), "rendered")
    mock_svc.set_up.assert_called_once_with(r"C:\fake\dl")
    mock_svc.prepare_log_file.assert_called_once_with(r"C:\fake\dl\wifi.etl", r"C:\fake\out")
    mock_svc.check_auto_analysis_availability.assert_called_once_with(
        {"issue_type": "Unclassified", "confidence": 0, "keywords_found": []})
    args, kwargs = mock_render.call_args
    self.assertEqual(args, ("log_parser.html",))
    self.assertEqual(kwargs["log_path"], r"C:\fake\out\wifi.etl.log")
    self.assertEqual(kwargs["available_filters"], ["wifi.tat"])
    self.assertEqual(kwargs["available_prompts"], ["p.txt"])
    self.assertEqual(kwargs["available_custom_prompts"], ["c.txt"])
    self.assertEqual(kwargs["llm_result_html"], "<p>x</p>")
    self.assertEqual(kwargs["auto_analysis_data"], "{}")
    self.assertIs(kwargs["should_auto_analyze"], False)
    sess = self.get_session()
    self.assertEqual(sess["log_path"], r"C:\fake\out\wifi.etl.log")
    self.assertEqual(sess["logparser_output_dir"], r"C:\fake\out")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `log_parser_service` | patched; `set_up`→`C:\fake\out`, `check_auto_analysis_availability`→`(False, None)`, `get_available_resources`→`(["wifi.tat"], (["p.txt"], ["c.txt"]))`, `analysis_result`={`llm_result_html`:`<p>x</p>`, …}, `prepare_log_file`→`C:\fake\out\wifi.etl.log` | `_configure_service` |
| `render_template` | patched → `"rendered"` | Class decorator |
| Session | `case_context`, `download_path=C:\fake\dl` | `seed_session` |
| Query | `etl_path=C%3A%5Cfake%5Cdl%5Cwifi.etl` | Decoded by `unquote` |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / body | `200` / `"rendered"` | `assertEqual` |
| `set_up` | called once with `C:\fake\dl` | `assert_called_once_with` |
| `prepare_log_file` | `(C:\fake\dl\wifi.etl, C:\fake\out)` | `assert_called_once_with` |
| `check_auto_analysis_availability` | `{issue_type: Unclassified, confidence: 0, keywords_found: []}` | `assert_called_once_with` |
| `render_template` args/kwargs | `log_parser.html`; `log_path`, filters/prompts lists, `llm_result_html`, `auto_analysis_data="{}"`, `should_auto_analyze=False` | `assertEqual` / `assertIs` |
| Session | `log_path=C:\fake\out\wifi.etl.log`, `logparser_output_dir=C:\fake\out` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Service calls and template context exactly as listed | Template name/kwargs renamed, default classification changed, `etl_path` not decoded |

### 4. TestLogParserForm.test_preselect_filter_and_prompt_force_auto_analysis

**Description:** BT-LLM flow: when BOTH `preselect_filter` and `preselect_prompt` query args are present they override the service's auto-detection, forcing `should_auto_analyze=True` with an explicit `bt_hci` payload.

**Code:**
```python
def test_preselect_filter_and_prompt_force_auto_analysis(self, mock_svc, mock_render):
    # Arrange
    self._configure_service(mock_svc)
    self.seed_session(download_path=r"C:\fake\dl")

    # Act
    self.client.get("/log_parser/log_parser?preselect_filter=bt.tat&preselect_prompt=bt.txt")

    # Assert
    kwargs = mock_render.call_args.kwargs
    self.assertIs(kwargs["should_auto_analyze"], True)
    self.assertEqual(kwargs["auto_analysis_data"],
                     {"filter_file": "bt.tat", "prompt_file": "bt.txt", "issue_type": "bt_hci"})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `log_parser_service` | patched; `set_up`→`C:\fake\out`, `check_auto_analysis_availability`→`(False, None)`, `get_available_resources`→`(["wifi.tat"], (["p.txt"], ["c.txt"]))`, `analysis_result`={`llm_result_html`:`<p>x</p>`, …}, `prepare_log_file`→`C:\fake\out\wifi.etl.log` | `_configure_service` |
| `render_template` | patched → `"rendered"` | Class decorator |
| Query | `preselect_filter=bt.tat&preselect_prompt=bt.txt` | Override args |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `should_auto_analyze` | `True` | `assertIs` |
| `auto_analysis_data` | `{filter_file: bt.tat, prompt_file: bt.txt, issue_type: bt_hci}` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Override payload passed to template | Override ignored or `issue_type` changed |

### 5. TestLogParserForm.test_direct_log_file_is_copied_into_output_dir

**Description:** A `latest_etl_path` in session that is an existing `.txt` (here a BT `.hci.txt`) is a *direct log*: it is copied with `shutil.copy2` into the output dir instead of being parsed by `prepare_log_file`.

**Code:**
```python
@patch(f"{ROUTES}.shutil.copy2")
@patch(f"{ROUTES}.os.path.exists", return_value=True)
def test_direct_log_file_is_copied_into_output_dir(self, mock_exists, mock_copy, mock_svc, mock_render):
    # Arrange
    self._configure_service(mock_svc)
    self.seed_session(download_path=r"C:\fake\dl", latest_etl_path=r"C:\fake\dl\bt.hci.txt")

    # Act
    self.client.get("/log_parser/log_parser")

    # Assert
    expected = os.path.join(r"C:\fake\out", "bt.hci.txt")
    mock_copy.assert_called_once_with(r"C:\fake\dl\bt.hci.txt", expected)
    mock_svc.prepare_log_file.assert_not_called()
    self.assertEqual(mock_render.call_args.kwargs["log_path"], expected)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `log_parser_service` | patched; `set_up`→`C:\fake\out`, `check_auto_analysis_availability`→`(False, None)`, `get_available_resources`→`(["wifi.tat"], (["p.txt"], ["c.txt"]))`, `analysis_result`={`llm_result_html`:`<p>x</p>`, …}, `prepare_log_file`→`C:\fake\out\wifi.etl.log` | `_configure_service` |
| `render_template` | patched → `"rendered"` | Class decorator |
| Session | `download_path=C:\fake\dl`, `latest_etl_path=C:\fake\dl\bt.hci.txt` | Direct log source |
| `os.path.exists` | patched → `True` | File exists |
| `shutil.copy2` | patched | No real copy |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `shutil.copy2` | `(C:\fake\dl\bt.hci.txt, C:\fake\out\bt.hci.txt)` | `assert_called_once_with` |
| `prepare_log_file` | not called | `assert_not_called` |
| `render_template` `log_path` | `C:\fake\out\bt.hci.txt` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Copied, not parsed | `.txt` no longer treated as a direct log |

### 6. TestLogParserForm.test_local_in_place_uses_source_dir_and_existing_log

**Description:** With a valid in-place local upload (`local_in_place=True` AND `uploaded_source_path` set) the output dir is the source dir (no `set_up`), and the log path is `<etl>.log` if it exists — no parsing/copying.

**Code:**
```python
@patch(f"{ROUTES}.os.path.exists", return_value=True)
def test_local_in_place_uses_source_dir_and_existing_log(self, mock_exists, mock_svc, mock_render):
    # Arrange
    self._configure_service(mock_svc)
    self.seed_session(download_path=SRC_DIR, local_in_place=True,
                      uploaded_source_path=os.path.join(SRC_DIR, "wifi.etl"),
                      latest_etl_path=os.path.join(SRC_DIR, "wifi.etl"))

    # Act
    self.client.get("/log_parser/log_parser")

    # Assert
    mock_svc.set_up.assert_not_called()
    mock_svc.prepare_log_file.assert_not_called()
    self.assertEqual(mock_render.call_args.kwargs["log_path"], os.path.join(SRC_DIR, "wifi.etl.log"))
    self.assertEqual(self.get_session()["logparser_output_dir"], SRC_DIR)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `log_parser_service` | patched; `set_up`→`C:\fake\out`, `check_auto_analysis_availability`→`(False, None)`, `get_available_resources`→`(["wifi.tat"], (["p.txt"], ["c.txt"]))`, `analysis_result`={`llm_result_html`:`<p>x</p>`, …}, `prepare_log_file`→`C:\fake\out\wifi.etl.log` | `_configure_service` |
| `render_template` | patched → `"rendered"` | Class decorator |
| Session | `download_path=C:\fake\local`, `local_in_place=True`, `uploaded_source_path`/`latest_etl_path=C:\fake\local\wifi.etl` | In-place mode |
| `os.path.exists` | patched → `True` | `.log` exists |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `set_up` / `prepare_log_file` | not called | `assert_not_called` |
| `log_path` kwarg | `C:\fake\local\wifi.etl.log` | `assertEqual` |
| Session `logparser_output_dir` | `C:\fake\local` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| In-place branch taken | In-place flag ignored (copies into a new output dir) |

### 7. TestLogParserForm.test_stale_local_in_place_flag_is_reset

**Description:** `local_in_place=True` without `uploaded_source_path` is a stale flag from an earlier flow; the route resets it to `False` and uses the normal `set_up` output dir.

**Code:**
```python
def test_stale_local_in_place_flag_is_reset(self, mock_svc, mock_render):
    # Arrange: local_in_place without uploaded_source_path is stale
    self._configure_service(mock_svc)
    self.seed_session(download_path=r"C:\fake\dl", local_in_place=True)

    # Act
    self.client.get("/log_parser/log_parser")

    # Assert
    self.assertIs(self.get_session()["local_in_place"], False)
    mock_svc.set_up.assert_called_once_with(r"C:\fake\dl")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `log_parser_service` | patched; `set_up`→`C:\fake\out`, `check_auto_analysis_availability`→`(False, None)`, `get_available_resources`→`(["wifi.tat"], (["p.txt"], ["c.txt"]))`, `analysis_result`={`llm_result_html`:`<p>x</p>`, …}, `prepare_log_file`→`C:\fake\out\wifi.etl.log` | `_configure_service` |
| `render_template` | patched → `"rendered"` | Class decorator |
| Session | `download_path=C:\fake\dl`, `local_in_place=True` (no `uploaded_source_path`) | Stale flag |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Session `local_in_place` | `False` | `assertIs` |
| `set_up` | called once with `C:\fake\dl` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Flag reset, normal path | Stale flag honoured |

### 8. TestLogParserServiceDelegates.test_load_prompt_returns_service_content

**Description:** `/load_prompt` forwards `type` and `file` from the JSON body to `log_parser_service.load_prompt_content` and wraps the result as `{"content": ...}`.

**Code:**
```python
@patch(f"{ROUTES}.log_parser_service")
def test_load_prompt_returns_service_content(self, mock_svc):
    # Arrange
    mock_svc.load_prompt_content.return_value = "PROMPT BODY"

    # Act
    resp = self.client.post("/log_parser/load_prompt", json={"type": "custom", "file": "a.txt"})

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_json(), {"content": "PROMPT BODY"})
    mock_svc.load_prompt_content.assert_called_once_with("custom", "a.txt")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `log_parser_service` | patched; `load_prompt_content` → `"PROMPT BODY"` |  |
| JSON | `{type: custom, file: a.txt}` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| JSON | `{content: PROMPT BODY}` | `assertEqual` |
| Service call | `("custom", "a.txt")` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Delegation + wrapping correct | Arg order/keys changed |

### 9. TestLogParserServiceDelegates.test_upload_resets_local_in_place_and_delegates

**Description:** `/upload` (filter/prompt file upload) must clear the `local_in_place` flag, pass the form `type` to `file_manager_service.handle_file_upload`, and return its result as the response.

**Code:**
```python
@patch(f"{ROUTES}.file_manager_service")
def test_upload_resets_local_in_place_and_delegates(self, mock_fm):
    # Arrange
    mock_fm.handle_file_upload.return_value = {"success": True, "message": "uploaded"}
    self.seed_session(local_in_place=True)

    # Act
    resp = self.client.post("/log_parser/upload", data={"type": "filter"})

    # Assert
    self.assertEqual(resp.get_json(), {"success": True, "message": "uploaded"})
    self.assertEqual(mock_fm.handle_file_upload.call_args.args[0], "filter")
    self.assertIs(self.get_session()["local_in_place"], False)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `file_manager_service` | patched; `handle_file_upload` → `{success: True, message: uploaded}` |  |
| Session | `case_context`, `local_in_place=True` |  |
| Form | `type=filter` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| JSON | `{success: True, message: uploaded}` | `assertEqual` |
| First positional arg | `"filter"` | `assertEqual` |
| Session `local_in_place` | `False` | `assertIs` |

| Pass Condition | Fail Condition |
|---|---|
| Flag cleared, result returned | Flag kept, or service result not returned |

### 10. TestLogParserServiceDelegates.test_edit_prompt_requires_action

**Description:** `/edit_prompt` without `action` returns `{success: False, message: "Action is required"}` (HTTP 200) and never touches the file manager.

**Code:**
```python
@patch(f"{ROUTES}.file_manager_service")
def test_edit_prompt_requires_action(self, mock_fm):
    # Act
    resp = self.client.post("/log_parser/edit_prompt", json={"filename": "a.txt"})

    # Assert
    self.assertEqual(resp.get_json(), {"success": False, "message": "Action is required"})
    mock_fm.handle_prompt_operation.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| JSON | `{filename: a.txt}` | No action |
| `file_manager_service` | patched |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| JSON | `{success: False, message: Action is required}` | `assertEqual` |
| `handle_prompt_operation` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| Guard enforced | Operation attempted without action |

### 11. TestLogParserServiceDelegates.test_edit_prompt_delegates_to_file_manager

**Description:** Valid `/edit_prompt` delegates `(action, filename, content)` to `file_manager_service.handle_prompt_operation` and returns its result.

**Code:**
```python
@patch(f"{ROUTES}.file_manager_service")
def test_edit_prompt_delegates_to_file_manager(self, mock_fm):
    # Arrange
    mock_fm.handle_prompt_operation.return_value = {"success": True, "message": "saved"}

    # Act
    resp = self.client.post("/log_parser/edit_prompt",
                            json={"action": "save", "filename": "a.txt", "content": "hello"})

    # Assert
    self.assertEqual(resp.get_json(), {"success": True, "message": "saved"})
    mock_fm.handle_prompt_operation.assert_called_once_with("save", "a.txt", "hello")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| JSON | `{action: save, filename: a.txt, content: hello}` |  |
| `handle_prompt_operation` | → `{success: True, message: saved}` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| JSON | `{success: True, message: saved}` | `assertEqual` |
| Service call | `("save", "a.txt", "hello")` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Delegation correct | Args reordered/dropped |

### 12. TestPickLocalAnalysisFile.test_selected_file_is_bound_to_cleared_session

**Description:** Native picker success: the chosen path is normalized, the WHOLE session is cleared (stale keys removed), and only `picked_local_analysis_path` is stored — binding the later `/upload_local_analysis` to this path. Also checks the hidden Tk root is withdrawn and destroyed.

**Code:**
```python
def test_selected_file_is_bound_to_cleared_session(self):
    # Arrange
    fake_tk = self._fake_tk(os.path.join(SRC_DIR, "wifi.zip"))
    self.seed_session(stale_key="old")

    # Act
    with patch.dict("sys.modules", {"tkinter": fake_tk, "tkinter.filedialog": fake_tk.filedialog}):
        resp = self.client.post("/log_parser/pick_local_analysis_file")

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_json(), {"success": True,
                                       "source_path": os.path.join(SRC_DIR, "wifi.zip"),
                                       "filename": "wifi.zip"})
    sess = self.get_session()
    self.assertEqual(sess, {"picked_local_analysis_path": os.path.join(SRC_DIR, "wifi.zip")})
    fake_tk.Tk.return_value.withdraw.assert_called_once_with()
    fake_tk.Tk.return_value.destroy.assert_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `sys.modules` | `patch.dict` → `tkinter`=MagicMock, `tkinter.filedialog`=its `.filedialog` | No real Tk window |
| `askopenfilename` | → `C:\fake\local\wifi.zip` |  |
| Session | `case_context`, `stale_key=old` | Must be wiped |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| JSON | `{success: True, source_path: C:\fake\local\wifi.zip, filename: wifi.zip}` | `assertEqual` |
| Session | exactly `{picked_local_analysis_path: C:\fake\local\wifi.zip}` | `assertEqual` |
| Tk root | `withdraw()` once, `destroy()` called | `assert_called_once_with` / `assert_called` |

| Pass Condition | Fail Condition |
|---|---|
| Session reset + path bound | Session not cleared or path not stored |

### 13. TestPickLocalAnalysisFile.test_no_selection_returns_400

**Description:** Cancelling the dialog (empty string) returns 400 `No file selected`.

**Code:**
```python
def test_no_selection_returns_400(self):
    # Arrange
    fake_tk = self._fake_tk("")

    # Act
    with patch.dict("sys.modules", {"tkinter": fake_tk, "tkinter.filedialog": fake_tk.filedialog}):
        resp = self.client.post("/log_parser/pick_local_analysis_file")

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertEqual(resp.get_json(), {"success": False, "message": "No file selected"})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `sys.modules` | `patch.dict` → `tkinter`=MagicMock, `tkinter.filedialog`=its `.filedialog` | No real Tk window |
| `askopenfilename` | → `""` | User cancelled |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` | `assertEqual` |
| JSON | `{success: False, message: No file selected}` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| 400 returned | Empty selection accepted |

### 14. TestPickLocalAnalysisFile.test_invalid_extension_returns_400

**Description:** A selected file with an unsupported extension is rejected with 400 and the session is not bound.

**Code:**
```python
def test_invalid_extension_returns_400(self):
    # Arrange
    fake_tk = self._fake_tk(os.path.join(SRC_DIR, "notes.docx"))

    # Act
    with patch.dict("sys.modules", {"tkinter": fake_tk, "tkinter.filedialog": fake_tk.filedialog}):
        resp = self.client.post("/log_parser/pick_local_analysis_file")

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertIn("Invalid file type: notes.docx", resp.get_json()["message"])
    self.assertNotIn("picked_local_analysis_path", self.get_session())
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `sys.modules` | `patch.dict` → `tkinter`=MagicMock, `tkinter.filedialog`=its `.filedialog` | No real Tk window |
| `askopenfilename` | → `C:\fake\local\notes.docx` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` | `assertEqual` |
| Message | contains `Invalid file type: notes.docx` | `assertIn` |
| Session | no `picked_local_analysis_path` | `assertNotIn` |

| Pass Condition | Fail Condition |
|---|---|
| Rejected, nothing bound | Unsupported file bound to session |

### 15. TestPickLocalAnalysisFile.test_dialog_exception_returns_500

**Description:** An exception from the dialog is caught → 500 with the error text; the `finally` block still destroys the Tk root exactly once (the success-path `destroy` was never reached).

**Code:**
```python
def test_dialog_exception_returns_500(self):
    # Arrange
    fake_tk = self._fake_tk(None)
    fake_tk.filedialog.askopenfilename.side_effect = RuntimeError("no display")

    # Act
    with patch.dict("sys.modules", {"tkinter": fake_tk, "tkinter.filedialog": fake_tk.filedialog}):
        resp = self.client.post("/log_parser/pick_local_analysis_file")

    # Assert
    self.assertEqual(resp.status_code, 500)
    self.assertEqual(resp.get_json()["message"], "Native file dialog unavailable: no display")
    fake_tk.Tk.return_value.destroy.assert_called_once_with()  # finally-block cleanup
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `sys.modules` | `patch.dict` → `tkinter`=MagicMock, `tkinter.filedialog`=its `.filedialog` | No real Tk window |
| `askopenfilename` | side effect `RuntimeError("no display")` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `500` | `assertEqual` |
| Message | `Native file dialog unavailable: no display` | `assertEqual` |
| Tk root `destroy` | called once | `assert_called_once_with()` |

| Pass Condition | Fail Condition |
|---|---|
| 500 + cleanup | Exception escapes or Tk root leaked |

### 16. TestPickLocalAnalysisFile.test_tkinter_unavailable_returns_503

**Description:** If `tkinter` cannot be imported (headless Python), the route returns 503 with a clear message instead of crashing.

**Code:**
```python
def test_tkinter_unavailable_returns_503(self):
    # Arrange: a None entry in sys.modules makes `import tkinter` raise ImportError
    # Act
    with patch.dict("sys.modules", {"tkinter": None}):
        resp = self.client.post("/log_parser/pick_local_analysis_file")

    # Assert
    self.assertEqual(resp.status_code, 503)
    self.assertEqual(resp.get_json()["message"], "Native file picker is unavailable in this environment.")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `sys.modules` | `{"tkinter": None}` | Forces `ImportError` |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `503` | `assertEqual` |
| Message | `Native file picker is unavailable in this environment.` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| 503 returned | ImportError propagates |

### 17. TestUploadLocalAnalysisValidation.test_missing_source_path_returns_400

**Description:** Empty `source_path` form field → 400 asking the user to use the native picker.

**Code:**
```python
def test_missing_source_path_returns_400(self):
    # Act
    resp = self._post("", picked=os.path.join(SRC_DIR, "a.log"))

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertIn("Please use native picker", resp.get_json()["message"])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Form `source_path` | `""` |  |
| Session `picked_local_analysis_path` | `C:\fake\local\a.log` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` | `assertEqual` |
| Message | contains `Please use native picker` | `assertIn` |

| Pass Condition | Fail Condition |
|---|---|
| 400 | Empty path processed |

### 18. TestUploadLocalAnalysisValidation.test_no_picked_path_in_session_returns_400

**Description:** A `source_path` without a picker-bound session path is refused (prevents posting arbitrary local paths).

**Code:**
```python
def test_no_picked_path_in_session_returns_400(self):
    # Act
    resp = self._post(os.path.join(SRC_DIR, "a.log"))

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertIn("No path is bound to this session", resp.get_json()["message"])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Form `source_path` | `C:\fake\local\a.log` |  |
| Session | *(no picked path)* |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` | `assertEqual` |
| Message | contains `No path is bound to this session` | `assertIn` |

| Pass Condition | Fail Condition |
|---|---|
| 400 | Unbound path processed |

### 19. TestUploadLocalAnalysisValidation.test_source_path_mismatch_returns_400

**Description:** The posted path must equal (normcase/normpath) the picker-bound path; a different file is refused.

**Code:**
```python
def test_source_path_mismatch_returns_400(self):
    # Act
    resp = self._post(os.path.join(SRC_DIR, "other.log"), picked=os.path.join(SRC_DIR, "a.log"))

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertIn("Invalid source path for this session", resp.get_json()["message"])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Form `source_path` | `C:\fake\local\other.log` |  |
| Session picked path | `C:\fake\local\a.log` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` | `assertEqual` |
| Message | contains `Invalid source path for this session` | `assertIn` |

| Pass Condition | Fail Condition |
|---|---|
| 400 | Path substitution allowed |

### 20. TestUploadLocalAnalysisValidation.test_missing_file_returns_400

**Description:** Bound path that no longer exists → 400 with the full path in the message.

**Code:**
```python
@patch(f"{ROUTES}.os.path.exists", return_value=False)
def test_missing_file_returns_400(self, mock_exists):
    # Act
    resp = self._post(os.path.join(SRC_DIR, "a.log"), picked=os.path.join(SRC_DIR, "a.log"))

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertEqual(resp.get_json()["message"], f"Source file not found: {os.path.join(SRC_DIR, 'a.log')}")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Paths | both `C:\fake\local\a.log` |  |
| `os.path.exists` | → `False` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` | `assertEqual` |
| Message | `Source file not found: C:\fake\local\a.log` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| 400 | Missing file processed |

### 21. TestUploadLocalAnalysisValidation.test_directory_returns_400

**Description:** A folder (even one named `*.zip`) is rejected by `_reject_if_directory`.

**Code:**
```python
@patch(f"{ROUTES}.os.path.isdir", return_value=True)
@patch(f"{ROUTES}.os.path.exists", return_value=True)
def test_directory_returns_400(self, mock_exists, mock_isdir):
    # Act
    resp = self._post(os.path.join(SRC_DIR, "folder.zip"), picked=os.path.join(SRC_DIR, "folder.zip"))

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertIn("Selected path is a folder", resp.get_json()["message"])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Paths | both `C:\fake\local\folder.zip` |  |
| `os.path.exists` / `isdir` | `True` / `True` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` | `assertEqual` |
| Message | contains `Selected path is a folder` | `assertIn` |

| Pass Condition | Fail Condition |
|---|---|
| 400 | Directory processed |

### 22. TestUploadLocalAnalysisValidation.test_disallowed_extension_returns_400

**Description:** Existing regular file with an unsupported extension → 400 `Invalid file type`.

**Code:**
```python
@patch(f"{ROUTES}.os.path.isdir", return_value=False)
@patch(f"{ROUTES}.os.path.exists", return_value=True)
def test_disallowed_extension_returns_400(self, mock_exists, mock_isdir):
    # Act
    resp = self._post(os.path.join(SRC_DIR, "notes.docx"), picked=os.path.join(SRC_DIR, "notes.docx"))

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertIn("Invalid file type: notes.docx", resp.get_json()["message"])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Paths | both `C:\fake\local\notes.docx` |  |
| `os.path.exists` / `isdir` | `True` / `False` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` | `assertEqual` |
| Message | contains `Invalid file type: notes.docx` | `assertIn` |

| Pass Condition | Fail Condition |
|---|---|
| 400 | Unsupported file processed |

### 23. TestUploadLocalAnalysisProcessing.test_log_file_routes_to_wifi_chatbot

**Description:** A `.log` upload needs no parsing: `_process_local_analysis` sets in-place session state, records `app_config.last_analyzed_log_path`, and the response tells the client to use the Wi-Fi chatbot with the long-path form of the log.

**Code:**
```python
def test_log_file_routes_to_wifi_chatbot(self):
    # Act
    resp = self._post("wifi.log")

    # Assert
    body = resp.get_json()
    path = os.path.join(SRC_DIR, "wifi.log")
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(body, {"success": True, "uploaded_source_path": path,
                            "redirect": "/__log_chatbot_stub__/index?auto_run=analyze_all",
                            "use_chatbot": True, "log_path": _long(path)})
    self.assertEqual(self.app_config.last_analyzed_log_path, path)
    sess = self.get_session()
    self.assertEqual(sess["download_path"], SRC_DIR)
    self.assertIs(sess["local_in_place"], True)
    self.assertIsNone(sess["latest_etl_path"])
    self.assertEqual(sess["classification"]["issue_type"], "Unclassified")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Class `setUp` patches | `os.path.exists`→`True`, `os.path.isdir`→`False`, `os.path.getsize`→`2048`, `os.makedirs`; `helpers` (`get_long_path` identity, `to_long_path`→`\\?\`+path); `gather_service` (`new_workflow_id`→`wf-1`); `datetime` (timestamp `20260101_120000`) | All at `ROUTES` |
| Form / session | `source_path` and `picked_local_analysis_path` = `C:\fake\local\<file>` | `_post` helper |
| File | `wifi.log` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| JSON | `{success: True, uploaded_source_path, redirect: /__log_chatbot_stub__/index?auto_run=analyze_all, use_chatbot: True, log_path: \\?\C:\fake\local\wifi.log}` | `assertEqual` |
| `app_config.last_analyzed_log_path` | `C:\fake\local\wifi.log` | `assertEqual` |
| Session | `download_path=C:\fake\local`, `local_in_place=True`, `latest_etl_path=None`, classification `Unclassified` | `assertEqual` / `assertIs` / `assertIsNone` |

| Pass Condition | Fail Condition |
|---|---|
| Exact response + state | Redirect target, response keys or session state changed |

### 24. TestUploadLocalAnalysisProcessing.test_hci_txt_routes_to_bt_chatbot

**Description:** A decoded BT HCI log (`.hci.txt`) routes to the BT chatbot (`is_bt: True`) and is stored as `latest_etl_path`.

**Code:**
```python
def test_hci_txt_routes_to_bt_chatbot(self):
    # Act
    resp = self._post("bt.hci.txt")

    # Assert
    path = os.path.join(SRC_DIR, "bt.hci.txt")
    self.assertEqual(resp.get_json(), {"success": True, "uploaded_source_path": path,
                                       "redirect": "/__bt_chatbot_stub__/index?auto_run=analyze_all",
                                       "use_chatbot": True, "is_bt": True, "log_path": _long(path)})
    self.assertEqual(self.get_session()["latest_etl_path"], path)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Class `setUp` patches | `os.path.exists`→`True`, `os.path.isdir`→`False`, `os.path.getsize`→`2048`, `os.makedirs`; `helpers` (`get_long_path` identity, `to_long_path`→`\\?\`+path); `gather_service` (`new_workflow_id`→`wf-1`); `datetime` (timestamp `20260101_120000`) | All at `ROUTES` |
| Form / session | `source_path` and `picked_local_analysis_path` = `C:\fake\local\<file>` | `_post` helper |
| File | `bt.hci.txt` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| JSON | `{…, redirect: /__bt_chatbot_stub__/index?auto_run=analyze_all, use_chatbot: True, is_bt: True, log_path: \\?\…bt.hci.txt}` | `assertEqual` |
| Session `latest_etl_path` | `C:\fake\local\bt.hci.txt` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| BT chatbot response | Treated as Wi-Fi log |

### 25. TestUploadLocalAnalysisProcessing.test_bt_etl_is_decoded_then_routes_to_bt_chatbot

**Description:** `ibtusb*.etl` is decoded by `bt_decode_via_cli(source_dir, file)`; the HCI output becomes `last_analyzed_log_path`, and the response points the BT chatbot at `<etl>.hci.txt`. (On this branch the CLI's own GUI-open is gated by `silent_mode` inside `bt_parser` — patched out here.)

**Code:**
```python
@patch(f"{ROUTES}.bt_decode_via_cli")
def test_bt_etl_is_decoded_then_routes_to_bt_chatbot(self, mock_decode):
    # Arrange
    hci = os.path.join(SRC_DIR, "ibtusb.etl.hci.txt")
    mock_decode.return_value = hci

    # Act
    resp = self._post("ibtusb.etl")

    # Assert
    path = os.path.join(SRC_DIR, "ibtusb.etl")
    mock_decode.assert_called_once_with(SRC_DIR, path)
    body = resp.get_json()
    self.assertEqual(body["redirect"], "/__bt_chatbot_stub__/index?auto_run=analyze_all")
    self.assertEqual(body["log_path"], _long(path + ".hci.txt"))
    self.assertIs(body["is_bt"], True)
    self.assertEqual(self.app_config.last_analyzed_log_path, hci)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Class `setUp` patches | `os.path.exists`→`True`, `os.path.isdir`→`False`, `os.path.getsize`→`2048`, `os.makedirs`; `helpers` (`get_long_path` identity, `to_long_path`→`\\?\`+path); `gather_service` (`new_workflow_id`→`wf-1`); `datetime` (timestamp `20260101_120000`) | All at `ROUTES` |
| Form / session | `source_path` and `picked_local_analysis_path` = `C:\fake\local\<file>` | `_post` helper |
| File | `ibtusb.etl` |  |
| `bt_decode_via_cli` | → `C:\fake\local\ibtusb.etl.hci.txt` | Patched at `ROUTES` |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Decoder call | `(C:\fake\local, C:\fake\local\ibtusb.etl)` | `assert_called_once_with` |
| JSON `redirect` / `log_path` / `is_bt` | BT chatbot URL / `\\?\…ibtusb.etl.hci.txt` / `True` | `assertEqual` / `assertIs` |
| `last_analyzed_log_path` | `C:\fake\local\ibtusb.etl.hci.txt` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Decoded and routed | Decoder skipped, wrong args, or Wi-Fi parser used |

### 26. TestUploadLocalAnalysisProcessing.test_bt_etl_decode_failure_returns_400

**Description:** If the BT decoder returns nothing, `_process_local_analysis` raises `ValueError` → 400 with the original filename.

**Code:**
```python
@patch(f"{ROUTES}.bt_decode_via_cli", return_value=None)
def test_bt_etl_decode_failure_returns_400(self, mock_decode):
    # Act
    resp = self._post("ibtpci.etl")

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertEqual(resp.get_json(), {"success": False,
                                       "message": "BT HCI decode failed or timed out for: ibtpci.etl"})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Class `setUp` patches | `os.path.exists`→`True`, `os.path.isdir`→`False`, `os.path.getsize`→`2048`, `os.makedirs`; `helpers` (`get_long_path` identity, `to_long_path`→`\\?\`+path); `gather_service` (`new_workflow_id`→`wf-1`); `datetime` (timestamp `20260101_120000`) | All at `ROUTES` |
| Form / session | `source_path` and `picked_local_analysis_path` = `C:\fake\local\<file>` | `_post` helper |
| File | `ibtpci.etl` |  |
| `bt_decode_via_cli` | → `None` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` | `assertEqual` |
| JSON | `{success: False, message: BT HCI decode failed or timed out for: ibtpci.etl}` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| 400 with message | Silent success with no log |

### 27. TestUploadLocalAnalysisProcessing.test_wifi_etl_runs_wpp_parser_then_routes_to_wifi_chatbot

**Description:** Any other allowed file (here a Wi-Fi `wifi.etl.001`) falls through to `wpp_ddd_parser_run(file)`; the parser writes `<file>.log`, which becomes `last_analyzed_log_path`. Response carries `etl_path`. (Branch note: the parser's TextAnalysisTool launch is gated by `app_config.silent_mode` inside `wpp_ddd_parser` — patched out here.)

**Code:**
```python
@patch(f"{ROUTES}.wpp_ddd_parser_run")
def test_wifi_etl_runs_wpp_parser_then_routes_to_wifi_chatbot(self, mock_wpp):
    # Act
    resp = self._post("wifi.etl.001")

    # Assert
    path = os.path.join(SRC_DIR, "wifi.etl.001")
    mock_wpp.assert_called_once_with(path)
    body = resp.get_json()
    self.assertEqual(body["redirect"], "/__log_chatbot_stub__/index?auto_run=analyze_all")
    self.assertEqual(body["etl_path"], _long(path))
    self.assertIs(body["use_chatbot"], True)
    self.assertEqual(self.app_config.last_analyzed_log_path, path + ".log")
    self.assertEqual(self.get_session()["latest_etl_path"], path)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Class `setUp` patches | `os.path.exists`→`True`, `os.path.isdir`→`False`, `os.path.getsize`→`2048`, `os.makedirs`; `helpers` (`get_long_path` identity, `to_long_path`→`\\?\`+path); `gather_service` (`new_workflow_id`→`wf-1`); `datetime` (timestamp `20260101_120000`) | All at `ROUTES` |
| Form / session | `source_path` and `picked_local_analysis_path` = `C:\fake\local\<file>` | `_post` helper |
| File | `wifi.etl.001` |  |
| `wpp_ddd_parser_run` | patched |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Parser call | `(C:\fake\local\wifi.etl.001)` | `assert_called_once_with` |
| JSON `redirect` / `etl_path` / `use_chatbot` | Wi-Fi chatbot URL / `\\?\…wifi.etl.001` / `True` | `assertEqual` / `assertIs` |
| `last_analyzed_log_path` | `C:\fake\local\wifi.etl.001.log` | `assertEqual` |
| Session `latest_etl_path` | `C:\fake\local\wifi.etl.001` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Parsed and routed | Parser not called or `.log` suffix logic changed |

### 28. TestUploadLocalAnalysisProcessing.test_fw_etl_routes_to_download_result

**Description:** `wrt-fw-*.etl` builds a local FW case: `infer_fw_parse_type(file, 'wifi')` decides bt/wifi, download results contain only the FW file, `auto_analysis_fw` is set and the user lands on `main.download_result`. FW responses carry no chatbot keys.

**Code:**
```python
@patch("utils.fw_utils.infer_fw_parse_type", return_value="bt")
def test_fw_etl_routes_to_download_result(self, mock_infer):
    # Act
    resp = self._post("wrt-fw-1.etl")

    # Assert
    path = os.path.join(SRC_DIR, "wrt-fw-1.etl")
    body = resp.get_json()
    self.assertEqual(body, {"success": True, "uploaded_source_path": path,
                            "redirect": "/__main_stub__/download_result"})
    mock_infer.assert_called_once_with(path, "wifi")
    self.app_config.set_download_results.assert_called_once_with(
        f"local_upload_{TIMESTAMP}", wifi={}, ddd={}, bt={}, fw={"wrt-fw-1.etl": [path]})
    sess = self.get_session()
    self.assertEqual(sess["auto_analysis_fw"], path)
    self.assertEqual(sess["case_context"]["wifi_or_bt"], "bt")
    self.assertEqual(sess["gather_workflow_id"], "wf-1")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Class `setUp` patches | `os.path.exists`→`True`, `os.path.isdir`→`False`, `os.path.getsize`→`2048`, `os.makedirs`; `helpers` (`get_long_path` identity, `to_long_path`→`\\?\`+path); `gather_service` (`new_workflow_id`→`wf-1`); `datetime` (timestamp `20260101_120000`) | All at `ROUTES` |
| Form / session | `source_path` and `picked_local_analysis_path` = `C:\fake\local\<file>` | `_post` helper |
| File | `wrt-fw-1.etl` |  |
| `utils.fw_utils.infer_fw_parse_type` | → `"bt"` | Imported inside the function |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| JSON | `{success: True, uploaded_source_path, redirect: /__main_stub__/download_result}` | `assertEqual` |
| `infer_fw_parse_type` | `(C:\fake\local\wrt-fw-1.etl, "wifi")` | `assert_called_once_with` |
| `app_config.set_download_results` | `(local_upload_20260101_120000, wifi={}, ddd={}, bt={}, fw={wrt-fw-1.etl: [path]})` | `assert_called_once_with` |
| Session | `auto_analysis_fw=path`, `case_context.wifi_or_bt=bt`, `gather_workflow_id=wf-1` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| FW case built | FW file routed to chatbot or results malformed |

### 29. TestUploadLocalAnalysisProcessing.test_archive_is_extracted_and_routes_to_download_result

**Description:** `.zip` uploads are extracted via `attachment_decompose.process_single_zip(file, source_dir, already_downloaded=False, …)`; categorised files are stored with `set_download_results` (evt files merged into ddd), case type is `bt` because BT files exist, gather telemetry is recorded with the file size.

**Code:**
```python
@patch(f"{ROUTES}.attachment_decompose.process_single_zip")
def test_archive_is_extracted_and_routes_to_download_result(self, mock_zip):
    # Arrange
    mock_zip.return_value = (["w.etl"], ["d.bin"], ["e.evtx"], ["ibtusb.etl"], ["wrt-fw-1.etl"])

    # Act
    resp = self._post("case logs.zip")

    # Assert
    self.assertEqual(resp.get_json()["redirect"], "/__main_stub__/download_result")
    self.assertEqual(mock_zip.call_args.args, (os.path.join(SRC_DIR, "case logs.zip"), SRC_DIR))
    self.assertIs(mock_zip.call_args.kwargs["already_downloaded"], False)
    self.app_config.set_download_results.assert_called_once_with(
        f"local_upload_{TIMESTAMP}",
        wifi={"case logs.zip": ["w.etl"]}, ddd={"case logs.zip": ["d.bin", "e.evtx"]},
        bt={"case logs.zip": ["ibtusb.etl"]}, fw={"case logs.zip": ["wrt-fw-1.etl"]})
    sess = self.get_session()
    self.assertEqual(sess["case_context"]["wifi_or_bt"], "bt")  # any BT file → bt case
    self.assertIs(sess["bsod"], False)
    self.mock_gather.record_workflow_start.assert_called_once()
    self.mock_gather.record_attachment_download_result.assert_called_once()
    self.assertEqual(self.mock_gather.record_attachment_download_result.call_args.kwargs["byte_count"], 2048)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Class `setUp` patches | `os.path.exists`→`True`, `os.path.isdir`→`False`, `os.path.getsize`→`2048`, `os.makedirs`; `helpers` (`get_long_path` identity, `to_long_path`→`\\?\`+path); `gather_service` (`new_workflow_id`→`wf-1`); `datetime` (timestamp `20260101_120000`) | All at `ROUTES` |
| Form / session | `source_path` and `picked_local_analysis_path` = `C:\fake\local\<file>` | `_post` helper |
| File | `case logs.zip` | Space in name |
| `process_single_zip` | → `([w.etl], [d.bin], [e.evtx], [ibtusb.etl], [wrt-fw-1.etl])` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| JSON `redirect` | `/__main_stub__/download_result` | `assertEqual` |
| Extractor positional / `already_downloaded` | `(C:\fake\local\case logs.zip, C:\fake\local)` / `False` | `assertEqual` / `assertIs` |
| `set_download_results` | `wifi={…:[w.etl]}, ddd={…:[d.bin, e.evtx]}, bt={…:[ibtusb.etl]}, fw={…:[wrt-fw-1.etl]}` | `assert_called_once_with` |
| Session | `case_context.wifi_or_bt=bt`, `bsod=False` | `assertEqual` / `assertIs` |
| `gather_service` | `record_workflow_start` once; `record_attachment_download_result` once with `byte_count=2048` | `assert_called_once` / `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Archive categorised | Categories mis-mapped, case type wrong, telemetry missing |

### 30. TestUploadLocalAnalysisProcessing.test_archive_without_supported_files_returns_400

**Description:** An archive with no recognised files → `ValueError` → 400, and no download results are stored.

**Code:**
```python
@patch(f"{ROUTES}.attachment_decompose.process_single_zip", return_value=([], [], [], [], []))
def test_archive_without_supported_files_returns_400(self, mock_zip):
    # Act
    resp = self._post("empty.7z")

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertEqual(resp.get_json()["message"], "No supported analysis files found in the uploaded file.")
    self.app_config.set_download_results.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Class `setUp` patches | `os.path.exists`→`True`, `os.path.isdir`→`False`, `os.path.getsize`→`2048`, `os.makedirs`; `helpers` (`get_long_path` identity, `to_long_path`→`\\?\`+path); `gather_service` (`new_workflow_id`→`wf-1`); `datetime` (timestamp `20260101_120000`) | All at `ROUTES` |
| Form / session | `source_path` and `picked_local_analysis_path` = `C:\fake\local\<file>` | `_post` helper |
| File | `empty.7z` |  |
| `process_single_zip` | → `([], [], [], [], [])` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` | `assertEqual` |
| Message | `No supported analysis files found in the uploaded file.` | `assertEqual` |
| `set_download_results` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| 400, nothing stored | Empty case created |

### 31. TestUploadLocalAnalysisProcessing.test_dmp_is_copied_to_bsod_share_and_routes_to_bsod_result

**Description:** `.dmp` → local BSOD case: resolve the BSOD share via `helpers.get_load_path(LOAD_PATH_prim, LOAD_PATH_bkup)`, copy the dump into `<share>\local_bsod_<ts>\`, point `download_path` at the share folder and redirect to `main.download_result_bsod`.

**Code:**
```python
@patch(f"{ROUTES}._copy_file_with_console_progress")
def test_dmp_is_copied_to_bsod_share_and_routes_to_bsod_result(self, mock_copy):
    # Arrange
    self.mock_helpers.get_load_path.return_value = r"\\share\bsod"

    # Act
    resp = self._post("memory.dmp")

    # Assert
    shared_dir = os.path.join(r"\\share\bsod", f"local_bsod_{TIMESTAMP}")
    self.assertEqual(resp.get_json()["redirect"], "/__main_stub__/download_result_bsod")
    self.mock_helpers.get_load_path.assert_called_once_with(LOAD_PATH_prim, LOAD_PATH_bkup)
    self.assertEqual(mock_copy.call_args.args,
                     (os.path.join(SRC_DIR, "memory.dmp"), os.path.join(shared_dir, "memory.dmp")))
    sess = self.get_session()
    self.assertEqual(sess["download_path"], shared_dir)
    self.assertIs(sess["bsod"], True)
    self.assertEqual(sess["case_context"]["backend_id"], f"local_bsod_{TIMESTAMP}")
    self.assertEqual(sess["selected_files"], [("memory.dmp", "memory.dmp", None)])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Class `setUp` patches | `os.path.exists`→`True`, `os.path.isdir`→`False`, `os.path.getsize`→`2048`, `os.makedirs`; `helpers` (`get_long_path` identity, `to_long_path`→`\\?\`+path); `gather_service` (`new_workflow_id`→`wf-1`); `datetime` (timestamp `20260101_120000`) | All at `ROUTES` |
| Form / session | `source_path` and `picked_local_analysis_path` = `C:\fake\local\<file>` | `_post` helper |
| File | `memory.dmp` |  |
| `helpers.get_load_path` | → `\\share\bsod` |  |
| `_copy_file_with_console_progress` | patched | No copy |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| JSON `redirect` | `/__main_stub__/download_result_bsod` | `assertEqual` |
| `get_load_path` | `(LOAD_PATH_prim, LOAD_PATH_bkup)` | `assert_called_once_with` |
| Copy args | `(C:\fake\local\memory.dmp, \\share\bsod\local_bsod_20260101_120000\memory.dmp)` | `assertEqual` |
| Session | `download_path` = share dir, `bsod=True`, `case_context.backend_id=local_bsod_20260101_120000`, `selected_files=[(memory.dmp, memory.dmp, None)]` | `assertEqual` / `assertIs` |

| Pass Condition | Fail Condition |
|---|---|
| BSOD case built | Share/copy target or session keys changed |

### 32. TestUploadLocalAnalysisProcessing.test_dmp_without_bsod_share_returns_400

**Description:** If no BSOD share is reachable, the upload fails with 400 and nothing is copied.

**Code:**
```python
@patch(f"{ROUTES}._copy_file_with_console_progress")
def test_dmp_without_bsod_share_returns_400(self, mock_copy):
    # Arrange
    self.mock_helpers.get_load_path.return_value = None

    # Act
    resp = self._post("memory.dmp")

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertEqual(resp.get_json()["message"], "BSOD shared folder is unavailable. Please try again later.")
    mock_copy.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Class `setUp` patches | `os.path.exists`→`True`, `os.path.isdir`→`False`, `os.path.getsize`→`2048`, `os.makedirs`; `helpers` (`get_long_path` identity, `to_long_path`→`\\?\`+path); `gather_service` (`new_workflow_id`→`wf-1`); `datetime` (timestamp `20260101_120000`) | All at `ROUTES` |
| Form / session | `source_path` and `picked_local_analysis_path` = `C:\fake\local\<file>` | `_post` helper |
| File | `memory.dmp` |  |
| `helpers.get_load_path` | → `None` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` | `assertEqual` |
| Message | `BSOD shared folder is unavailable. Please try again later.` | `assertEqual` |
| Copy | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| 400, no copy | Copy attempted into `None` path |

### 33. TestUploadLocalAnalysisProcessing.test_unexpected_exception_returns_500

**Description:** Any non-ValueError failure (no cancel) → 500 `Failed local analysis flow: <err>`.

**Code:**
```python
@patch(f"{ROUTES}._process_local_analysis", side_effect=RuntimeError("disk on fire"))
def test_unexpected_exception_returns_500(self, mock_process):
    # Act
    resp = self._post("wifi.log")

    # Assert
    self.assertEqual(resp.status_code, 500)
    self.assertEqual(resp.get_json(), {"success": False, "message": "Failed local analysis flow: disk on fire"})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Class `setUp` patches | `os.path.exists`→`True`, `os.path.isdir`→`False`, `os.path.getsize`→`2048`, `os.makedirs`; `helpers` (`get_long_path` identity, `to_long_path`→`\\?\`+path); `gather_service` (`new_workflow_id`→`wf-1`); `datetime` (timestamp `20260101_120000`) | All at `ROUTES` |
| Form / session | `source_path` and `picked_local_analysis_path` = `C:\fake\local\<file>` | `_post` helper |
| `_process_local_analysis` | side effect `RuntimeError("disk on fire")` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `500` | `assertEqual` |
| JSON | `{success: False, message: Failed local analysis flow: disk on fire}` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| 500 | Exception escapes |

### 34. TestUploadLocalAnalysisProcessing.test_system_exit_without_cancel_propagates

**Description:** Documents current behavior: parser helpers call `sys.exit(0)` on failure; without a cancel request the route re-raises `SystemExit` (the test client surfaces it). Only cancelled uploads swallow it.

**Code:**
```python
@patch(f"{ROUTES}._process_local_analysis", side_effect=SystemExit(0))
def test_system_exit_without_cancel_propagates(self, mock_process):
    # Act / Assert: a parser's sys.exit is only swallowed when a cancel was requested
    with self.assertRaises(SystemExit):
        self._post("wifi.log")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Class `setUp` patches | `os.path.exists`→`True`, `os.path.isdir`→`False`, `os.path.getsize`→`2048`, `os.makedirs`; `helpers` (`get_long_path` identity, `to_long_path`→`\\?\`+path); `gather_service` (`new_workflow_id`→`wf-1`); `datetime` (timestamp `20260101_120000`) | All at `ROUTES` |
| Form / session | `source_path` and `picked_local_analysis_path` = `C:\fake\local\<file>` | `_post` helper |
| `_process_local_analysis` | side effect `SystemExit(0)` |  |
| `upload_id` | *(none)* | No cancel event |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Exception | `SystemExit` | `assertRaises` |

| Pass Condition | Fail Condition |
|---|---|
| Propagates | Swallowed into a response |

### 35. TestUploadLocalAnalysisProcessing.test_system_exit_after_cancel_is_reported_as_cancelled

**Description:** When the cancel thread kills a parser subprocess, the parser helper calls `sys.exit(0)`; with the cancel event set, the route converts that `SystemExit` into the normal cancelled response and unregisters the id.

**Code:**
```python
def test_system_exit_after_cancel_is_reported_as_cancelled(self):
    # Arrange: a parser killed by the cancel thread calls sys.exit(0)
    def _exit_after_cancel(*args, **kwargs):
        kwargs["cancel_event"].set()
        raise SystemExit(0)

    with patch(f"{ROUTES}._process_local_analysis", side_effect=_exit_after_cancel):
        # Act
        resp = self._post("wifi.etl", upload_id="up-3")

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_json()["message"], "Upload cancelled by user.")
    self.assertNotIn("up-3", lp._active_local_uploads)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Class `setUp` patches | `os.path.exists`→`True`, `os.path.isdir`→`False`, `os.path.getsize`→`2048`, `os.makedirs`; `helpers` (`get_long_path` identity, `to_long_path`→`\\?\`+path); `gather_service` (`new_workflow_id`→`wf-1`); `datetime` (timestamp `20260101_120000`) | All at `ROUTES` |
| Form / session | `source_path` and `picked_local_analysis_path` = `C:\fake\local\<file>` | `_post` helper |
| Form `upload_id` | `up-3` |  |
| `_process_local_analysis` | sets event, raises `SystemExit(0)` |  |
| File | `wifi.etl` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| Message | `Upload cancelled by user.` | `assertEqual` |
| Registry | `up-3` removed | `assertNotIn` |

| Pass Condition | Fail Condition |
|---|---|
| Cancelled response | SystemExit kills the request |

### 36. TestUploadLocalAnalysisProcessing.test_is_bsod_flag_forces_bsod_flow_for_non_dmp_file

**Description:** The `is_bsod=true` form flag forces the BSOD copy-to-share flow even for a non-`.dmp` file (here a `.zip`).

**Code:**
```python
@patch(f"{ROUTES}._copy_file_with_console_progress")
def test_is_bsod_flag_forces_bsod_flow_for_non_dmp_file(self, mock_copy):
    # Arrange
    self.mock_helpers.get_load_path.return_value = r"\\share\bsod"

    # Act
    resp = self._post("dump_bundle.zip", is_bsod="true")

    # Assert
    self.assertEqual(resp.get_json()["redirect"], "/__main_stub__/download_result_bsod")
    mock_copy.assert_called_once()
    self.assertIs(self.get_session()["bsod"], True)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Class `setUp` patches | `os.path.exists`→`True`, `os.path.isdir`→`False`, `os.path.getsize`→`2048`, `os.makedirs`; `helpers` (`get_long_path` identity, `to_long_path`→`\\?\`+path); `gather_service` (`new_workflow_id`→`wf-1`); `datetime` (timestamp `20260101_120000`) | All at `ROUTES` |
| Form / session | `source_path` and `picked_local_analysis_path` = `C:\fake\local\<file>` | `_post` helper |
| File / form | `dump_bundle.zip`, `is_bsod=true` |  |
| `helpers.get_load_path` | → `\sharesod` |  |
| `_copy_file_with_console_progress` | patched |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| JSON `redirect` | `/__main_stub__/download_result_bsod` | `assertEqual` |
| Copy | called once | `assert_called_once` |
| Session `bsod` | `True` | `assertIs` |

| Pass Condition | Fail Condition |
|---|---|
| BSOD flow taken | Archive extracted instead |

### 37. TestUploadLocalAnalysisProcessing.test_cancelled_upload_returns_cancelled_and_clears_session

**Description:** Cooperative cancel: the worker registers a cleanup path, the cancel event is set and `_LocalAnalysisCancelled` is raised. The route returns `cancelled: True` (HTTP 200), pops all cancelable session keys, waits for child processes, deletes registered paths and unregisters the upload id.

**Code:**
```python
def test_cancelled_upload_returns_cancelled_and_clears_session(self):
    # Arrange: worker records a cleanup path, then sees the cancel
    def _cancelled(*args, **kwargs):
        kwargs["cleanup_paths"].append(r"C:\fake\local\partial")
        kwargs["cancel_event"].set()
        raise lp._LocalAnalysisCancelled()

    self.seed_session(download_path="keep-me-not")
    with patch(f"{ROUTES}._process_local_analysis", side_effect=_cancelled), \
            patch(f"{ROUTES}._wait_for_cancel_children_to_exit") as mock_wait, \
            patch(f"{ROUTES}._delete_cancel_cleanup_paths") as mock_delete:
        # Act
        resp = self._post("wifi.zip", upload_id="up-1")

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_json(), {"success": False, "cancelled": True,
                                       "message": "Upload cancelled by user."})
    self.assertNotIn("download_path", self.get_session())
    mock_wait.assert_called_once_with()
    mock_delete.assert_called_once_with([r"C:\fake\local\partial"], "up-1")
    self.assertNotIn("up-1", lp._active_local_uploads)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Class `setUp` patches | `os.path.exists`→`True`, `os.path.isdir`→`False`, `os.path.getsize`→`2048`, `os.makedirs`; `helpers` (`get_long_path` identity, `to_long_path`→`\\?\`+path); `gather_service` (`new_workflow_id`→`wf-1`); `datetime` (timestamp `20260101_120000`) | All at `ROUTES` |
| Form / session | `source_path` and `picked_local_analysis_path` = `C:\fake\local\<file>` | `_post` helper |
| Form `upload_id` | `up-1` | Registers a cancel event |
| `_process_local_analysis` | appends `C:\fake\local\partial`, sets event, raises `_LocalAnalysisCancelled` |  |
| `_wait_for_cancel_children_to_exit` / `_delete_cancel_cleanup_paths` | patched | No psutil / no deletes |
| Session | `download_path=keep-me-not` | Must be popped |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `200` / `{success: False, cancelled: True, message: Upload cancelled by user.}` | `assertEqual` |
| Session `download_path` | absent | `assertNotIn` |
| Wait / delete | `()` / `([C:\fake\local\partial], "up-1")` | `assert_called_once_with` |
| Registry | `up-1` removed | `assertNotIn` |

| Pass Condition | Fail Condition |
|---|---|
| Full cancel cleanup | Session keys left, cleanup skipped or id leaked |

### 38. TestUploadLocalAnalysisProcessing.test_value_error_after_cancel_is_reported_as_cancelled

**Description:** A `ValueError` raised after the user cancelled (e.g. half-extracted archive) is reported as a cancel, not a 400.

**Code:**
```python
def test_value_error_after_cancel_is_reported_as_cancelled(self):
    # Arrange
    def _fail_after_cancel(*args, **kwargs):
        kwargs["cancel_event"].set()
        raise ValueError("partial archive")

    with patch(f"{ROUTES}._process_local_analysis", side_effect=_fail_after_cancel):
        # Act
        resp = self._post("wifi.zip", upload_id="up-2")

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertIs(resp.get_json()["cancelled"], True)
    self.assertNotIn("up-2", lp._active_local_uploads)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Class `setUp` patches | `os.path.exists`→`True`, `os.path.isdir`→`False`, `os.path.getsize`→`2048`, `os.makedirs`; `helpers` (`get_long_path` identity, `to_long_path`→`\\?\`+path); `gather_service` (`new_workflow_id`→`wf-1`); `datetime` (timestamp `20260101_120000`) | All at `ROUTES` |
| Form / session | `source_path` and `picked_local_analysis_path` = `C:\fake\local\<file>` | `_post` helper |
| Form `upload_id` | `up-2` |  |
| `_process_local_analysis` | sets event, raises `ValueError("partial archive")` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| `cancelled` | `True` | `assertIs` |
| Registry | `up-2` removed | `assertNotIn` |

| Pass Condition | Fail Condition |
|---|---|
| Reported as cancelled | Reported as 400 error |

### 39. TestCancelLocalAnalysis.test_non_localhost_is_rejected

**Description:** `/cancel_local_analysis` is localhost-only; a remote address gets 403.

**Code:**
```python
def test_non_localhost_is_rejected(self):
    # Act
    resp = self.client.post("/log_parser/cancel_local_analysis", json={"upload_id": "x"},
                            environ_base=NON_LOCAL)

    # Assert
    self.assertEqual(resp.status_code, 403)
    self.assertEqual(resp.get_json()["message"], "Access denied: localhost only")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `environ_base` | `{REMOTE_ADDR: 10.0.0.5}` |  |
| JSON | `{upload_id: x}` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `403` | `assertEqual` |
| Message | `Access denied: localhost only` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| 403 | Remote cancel accepted |

### 40. TestCancelLocalAnalysis.test_missing_upload_id_returns_400

**Description:** Cancel without an `upload_id` (JSON or form) → 400.

**Code:**
```python
def test_missing_upload_id_returns_400(self):
    # Act
    resp = self.client.post("/log_parser/cancel_local_analysis", json={})

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertEqual(resp.get_json()["message"], "upload_id is required")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| JSON | `{}` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` | `assertEqual` |
| Message | `upload_id is required` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| 400 | Empty id accepted |

### 41. TestCancelLocalAnalysis.test_unknown_upload_id_reports_inactive

**Description:** Cancelling an unknown id succeeds with `was_active: False` and no killer thread is started.

**Code:**
```python
@patch(f"{ROUTES}.threading.Thread")
def test_unknown_upload_id_reports_inactive(self, mock_thread):
    # Act
    resp = self.client.post("/log_parser/cancel_local_analysis", json={"upload_id": "nope"})

    # Assert
    self.assertEqual(resp.get_json(), {"success": True, "was_active": False})
    mock_thread.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| JSON | `{upload_id: nope}` |  |
| `threading.Thread` | patched |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| JSON | `{success: True, was_active: False}` | `assertEqual` |
| Thread | not created | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| Inactive reported | Killer started for unknown id |

### 42. TestCancelLocalAnalysis.test_active_upload_is_signalled_and_killer_started_once

**Description:** For a registered upload the cancel event is set and ONE daemon thread running `_kill_local_analysis_children_until_done(upload_id)` is started, even when cancel is clicked twice (`killer_started` guard). Form-encoded `upload_id` is accepted.

**Code:**
```python
@patch(f"{ROUTES}.threading.Thread")
def test_active_upload_is_signalled_and_killer_started_once(self, mock_thread):
    # Arrange
    event = lp._register_local_upload("up-9")
    self.addCleanup(lp._unregister_local_upload, "up-9")

    # Act
    first = self.client.post("/log_parser/cancel_local_analysis", data={"upload_id": "up-9"})
    second = self.client.post("/log_parser/cancel_local_analysis", data={"upload_id": "up-9"})

    # Assert
    self.assertEqual(first.get_json(), {"success": True, "was_active": True})
    self.assertEqual(second.get_json(), {"success": True, "was_active": True})
    self.assertTrue(event.is_set())
    mock_thread.assert_called_once()
    kwargs = mock_thread.call_args.kwargs
    self.assertIs(kwargs["target"], lp._kill_local_analysis_children_until_done)
    self.assertEqual(kwargs["args"], ("up-9",))
    self.assertIs(kwargs["daemon"], True)
    mock_thread.return_value.start.assert_called_once_with()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Registry | `_register_local_upload("up-9")` | Cleaned up via `addCleanup` |
| Form | `upload_id=up-9` (posted twice) |  |
| `threading.Thread` | patched |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Both responses | `{success: True, was_active: True}` | `assertEqual` |
| Event | set | `assertTrue(event.is_set())` |
| Thread | created once: `target=_kill_local_analysis_children_until_done`, `args=("up-9",)`, `daemon=True`; `start()` once | `assert_called_once` / `assertIs` / `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Single killer | Duplicate killers or event not set |

### 43. TestOpenLocalAnalysis.test_non_localhost_is_rejected

**Description:** SendTo entry point is localhost-only: remote callers get a `danger` flash and a redirect to `main.index`.

**Code:**
```python
def test_non_localhost_is_rejected(self):
    # Act
    resp = self._get(environ_base=NON_LOCAL)

    # Assert
    self._assert_flash_redirect(resp, "only available from localhost")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `environ_base` | `{REMOTE_ADDR: 10.0.0.5}` |  |
| Query | `token=unit-test-token`, `path=C:\fake\local\wifi.zip` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / Location | `302` → `/__main_stub__/index` | `_assert_flash_redirect` |
| Flash | `danger`, contains `only available from localhost` | `assertEqual` / `assertIn` |

| Pass Condition | Fail Condition |
|---|---|
| Rejected | Remote request processed |

### 44. TestOpenLocalAnalysis.test_invalid_token_is_rejected

**Description:** A token that does not match `app_config.sendto_token` (constant-time compare) → danger flash + redirect to index.

**Code:**
```python
def test_invalid_token_is_rejected(self):
    # Act
    resp = self._get(token="wrong-token")

    # Assert
    self._assert_flash_redirect(resp, "Invalid or expired SendTo token")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Query (default) | `token=unit-test-token`, `path=C:\fake\local\wifi.zip` | `_get` helper; `app_config.sendto_token=unit-test-token` |
| Query `token` | `wrong-token` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / Location | `302` → `/__main_stub__/index` |  |
| Flash | `danger`, `Invalid or expired SendTo token` | `assertIn` |

| Pass Condition | Fail Condition |
|---|---|
| Rejected | Wrong token accepted |

### 45. TestOpenLocalAnalysis.test_missing_path_is_rejected

**Description:** Valid token but empty `path` → danger flash + redirect.

**Code:**
```python
def test_missing_path_is_rejected(self):
    # Act
    resp = self._get(path="")

    # Assert
    self._assert_flash_redirect(resp, "No local analysis file path was provided.")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Query (default) | `token=unit-test-token`, `path=C:\fake\local\wifi.zip` | `_get` helper; `app_config.sendto_token=unit-test-token` |
| Query `path` | `""` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Flash | `danger`, `No local analysis file path was provided.` | `assertIn` |

| Pass Condition | Fail Condition |
|---|---|
| Rejected | Empty path stored |

### 46. TestOpenLocalAnalysis.test_nonexistent_file_is_rejected

**Description:** Path that does not exist → danger flash + redirect.

**Code:**
```python
@patch(f"{ROUTES}.os.path.exists", return_value=False)
def test_nonexistent_file_is_rejected(self, mock_exists):
    # Act
    resp = self._get()

    # Assert
    self._assert_flash_redirect(resp, "Local analysis file not found")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Query (default) | `token=unit-test-token`, `path=C:\fake\local\wifi.zip` | `_get` helper; `app_config.sendto_token=unit-test-token` |
| `os.path.exists` | → `False` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Flash | `danger`, `Local analysis file not found` | `assertIn` |

| Pass Condition | Fail Condition |
|---|---|
| Rejected | Missing file stored |

### 47. TestOpenLocalAnalysis.test_directory_is_rejected

**Description:** A folder sent via SendTo is rejected with the folder message (surfaced to the user as a flash, per `app._build_startup_path` design).

**Code:**
```python
@patch(f"{ROUTES}.os.path.isdir", return_value=True)
@patch(f"{ROUTES}.os.path.exists", return_value=True)
def test_directory_is_rejected(self, mock_exists, mock_isdir):
    # Act
    resp = self._get()

    # Assert
    self._assert_flash_redirect(resp, "Selected path is a folder")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Query (default) | `token=unit-test-token`, `path=C:\fake\local\wifi.zip` | `_get` helper; `app_config.sendto_token=unit-test-token` |
| `os.path.exists` / `isdir` | `True` / `True` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Flash | `danger`, `Selected path is a folder` | `assertIn` |

| Pass Condition | Fail Condition |
|---|---|
| Rejected | Folder accepted |

### 48. TestOpenLocalAnalysis.test_disallowed_extension_is_rejected

**Description:** Unsupported file type via SendTo → danger flash + redirect.

**Code:**
```python
@patch(f"{ROUTES}.os.path.isdir", return_value=False)
@patch(f"{ROUTES}.os.path.exists", return_value=True)
def test_disallowed_extension_is_rejected(self, mock_exists, mock_isdir):
    # Act
    resp = self._get(path=os.path.join(SRC_DIR, "readme.pdf"))

    # Assert
    self._assert_flash_redirect(resp, "Invalid file type: readme.pdf")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Query (default) | `token=unit-test-token`, `path=C:\fake\local\wifi.zip` | `_get` helper; `app_config.sendto_token=unit-test-token` |
| Query `path` | `C:\fake\local\readme.pdf` |  |
| `os.path.exists` / `isdir` | `True` / `False` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Flash | `danger`, `Invalid file type: readme.pdf` | `assertIn` |

| Pass Condition | Fail Condition |
|---|---|
| Rejected | Unsupported file accepted |

### 49. TestOpenLocalAnalysis.test_valid_request_stores_pending_path_and_renders_page

**Description:** Valid SendTo request: path stored as `sendto_pending_path` (processing starts later over Socket.IO) and the transmission page is rendered with the filename.

**Code:**
```python
@patch(f"{ROUTES}.render_template", return_value="transmission page")
@patch(f"{ROUTES}.os.path.isdir", return_value=False)
@patch(f"{ROUTES}.os.path.exists", return_value=True)
def test_valid_request_stores_pending_path_and_renders_page(self, mock_exists, mock_isdir, mock_render):
    # Act
    resp = self._get()

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_data(as_text=True), "transmission page")
    mock_render.assert_called_once_with("sendto_transmission.html", filename="wifi.zip")
    self.assertEqual(self.get_session()["sendto_pending_path"], os.path.join(SRC_DIR, "wifi.zip"))
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Query (default) | `token=unit-test-token`, `path=C:\fake\local\wifi.zip` | `_get` helper; `app_config.sendto_token=unit-test-token` |
| `os.path.exists` / `isdir` | `True` / `False` |  |
| `render_template` | → `"transmission page"` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / body | `200` / `transmission page` | `assertEqual` |
| `render_template` | `("sendto_transmission.html", filename="wifi.zip")` | `assert_called_once_with` |
| Session `sendto_pending_path` | `C:\fake\local\wifi.zip` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Page rendered, path pending | Processing starts synchronously or path not stored |

### 50. TestNavigateExistingBrowser.test_non_localhost_is_rejected

**Description:** Remote callers cannot drive the app's Chrome: 403 and no navigation.

**Code:**
```python
def test_non_localhost_is_rejected(self):
    # Act
    resp = self.client.post("/log_parser/navigate_existing_browser", json={"startup_path": "/"},
                            environ_base=NON_LOCAL)

    # Assert
    self.assertEqual(resp.status_code, 403)
    self.app_config.driver_manager.navigate_main_browser.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `environ_base` | `{REMOTE_ADDR: 10.0.0.5}` |  |
| JSON | `{startup_path: /}` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `403` | `assertEqual` |
| `navigate_main_browser` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| 403 | Remote navigation |

### 51. TestNavigateExistingBrowser.test_unsafe_startup_paths_return_400

**Description:** Open-redirect guard: `startup_path` must be a str starting with a single `/` and contain no `://`. Four bad values (relative, protocol-relative, embedded URL, non-string) are each rejected; the browser is never navigated.

**Code:**
```python
def test_unsafe_startup_paths_return_400(self):
    for bad in ("relative/path", "//evil.example/x", "/redirect?u=http://evil", 42):
        with self.subTest(startup_path=bad):
            # Act
            resp = self.client.post("/log_parser/navigate_existing_browser", json={"startup_path": bad})

            # Assert
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(resp.get_json()["message"], "Invalid startup path")
    self.app_config.driver_manager.navigate_main_browser.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| JSON `startup_path` (subTests) | `relative/path`, `//evil.example/x`, `/redirect?u=http://evil`, `42` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` each | `assertEqual` |
| Message | `Invalid startup path` | `assertEqual` |
| `navigate_main_browser` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| All 4 rejected | Any unsafe value navigates |

### 52. TestNavigateExistingBrowser.test_missing_driver_manager_returns_409

**Description:** If `app_config.driver_manager` is `None` (browser not started) → 409.

**Code:**
```python
def test_missing_driver_manager_returns_409(self):
    # Arrange
    self.app_config.driver_manager = None

    # Act
    resp = self.client.post("/log_parser/navigate_existing_browser", json={"startup_path": "/"})

    # Assert
    self.assertEqual(resp.status_code, 409)
    self.assertEqual(resp.get_json()["message"], "Driver manager is not available")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `app_config.driver_manager` | `None` |  |
| JSON | `{startup_path: /}` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `409` | `assertEqual` |
| Message | `Driver manager is not available` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| 409 | AttributeError / 500 |

### 53. TestNavigateExistingBrowser.test_navigation_error_returns_409

**Description:** Selenium failure while navigating → 409 with the error message.

**Code:**
```python
def test_navigation_error_returns_409(self):
    # Arrange
    self.app_config.driver_manager.navigate_main_browser.side_effect = RuntimeError("chrome gone")

    # Act
    resp = self.client.post("/log_parser/navigate_existing_browser", json={"startup_path": "/"})

    # Assert
    self.assertEqual(resp.status_code, 409)
    self.assertEqual(resp.get_json(), {"success": False, "message": "chrome gone"})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `navigate_main_browser` | side effect `RuntimeError("chrome gone")` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `409` | `assertEqual` |
| JSON | `{success: False, message: chrome gone}` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| 409 | Exception escapes |

### 54. TestNavigateExistingBrowser.test_success_navigates_with_fixed_localhost_origin

**Description:** Success path. Even with a spoofed `Host: attacker.example:5000` header the base URL is built from `127.0.0.1` + `SERVER_PORT` (80 under the test client), never from the Host header.

**Code:**
```python
def test_success_navigates_with_fixed_localhost_origin(self):
    # Act
    resp = self.client.post("/log_parser/navigate_existing_browser",
                            json={"startup_path": "/log_parser/open_local_analysis?token=t"},
                            headers={"Host": "attacker.example:5000"})

    # Assert
    self.assertEqual(resp.get_json(), {"success": True})
    self.app_config.driver_manager.navigate_main_browser.assert_called_once_with(
        "http://127.0.0.1:80/", "/log_parser/open_local_analysis?token=t")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| JSON | `{startup_path: /log_parser/open_local_analysis?token=t}` |  |
| Header | `Host: attacker.example:5000` | Spoof attempt |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| JSON | `{success: True}` | `assertEqual` |
| `navigate_main_browser` | `("http://127.0.0.1:80/", "/log_parser/open_local_analysis?token=t")` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Fixed origin used | Host header trusted |

### 55. TestVerifySendtoToken.test_non_localhost_is_rejected

**Description:** Token verification is localhost-only.

**Code:**
```python
def test_non_localhost_is_rejected(self):
    # Act
    resp = self.client.get("/log_parser/verify_sendto_token?token=x", environ_base=NON_LOCAL)

    # Assert
    self.assertEqual(resp.status_code, 403)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `environ_base` | `{REMOTE_ADDR: 10.0.0.5}` |  |
| Query | `token=x` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `403` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| 403 | Token oracle exposed remotely |

### 56. TestVerifySendtoToken.test_matching_token

**Description:** Correct token → `token_match: True` with 8-char previews of both tokens.

**Code:**
```python
def test_matching_token(self):
    # Act
    resp = self.client.get("/log_parser/verify_sendto_token?token=unit-test-token")

    # Assert
    self.assertEqual(resp.get_json(), {"success": True, "token_match": True,
                                       "provided_token_preview": "unit-tes...",
                                       "expected_token_preview": "unit-tes..."})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Query | `token=unit-test-token` | Matches mock `sendto_token` |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| JSON | `{success: True, token_match: True, provided_token_preview: unit-tes..., expected_token_preview: unit-tes...}` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Exact JSON | Full token leaked or match wrong |

### 57. TestVerifySendtoToken.test_mismatched_and_empty_tokens

**Description:** Wrong token and missing token both report `token_match: False`; short tokens are previewed verbatim.

**Code:**
```python
def test_mismatched_and_empty_tokens(self):
    # Act
    wrong = self.client.get("/log_parser/verify_sendto_token?token=abc").get_json()
    empty = self.client.get("/log_parser/verify_sendto_token").get_json()

    # Assert
    self.assertIs(wrong["token_match"], False)
    self.assertEqual(wrong["provided_token_preview"], "abc")
    self.assertIs(empty["token_match"], False)
    self.assertEqual(empty["provided_token_preview"], "")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Query | `token=abc`; then no token |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `token_match` | `False` both | `assertIs` |
| Previews | `abc` / `""` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| False for both | Empty token matches |

### 58. TestFilterRoutes.test_get_filter_details_requires_filter_file

**Description:** No `filter_file` → `{success: False, message: No filter file specified}` (HTTP 200).

**Code:**
```python
def test_get_filter_details_requires_filter_file(self):
    # Act
    resp = self.client.post("/log_parser/get_filter_details", json={})

    # Assert
    self.assertEqual(resp.get_json(), {"success": False, "message": "No filter file specified"})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| JSON | `{}` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| JSON | `{success: False, message: No filter file specified}` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Guard | Lookup with empty name |

### 59. TestFilterRoutes.test_get_filter_details_missing_file

**Description:** The filter is looked up under `LOG_PARSER_DIR\filter\<name>`; a missing file returns a not-found message. Asserting the `exists` argument pins the path layout and guarantees no real network-share access.

**Code:**
```python
@patch(f"{ROUTES}.os.path.exists", return_value=False)
def test_get_filter_details_missing_file(self, mock_exists):
    # Act
    resp = self.client.post("/log_parser/get_filter_details", json={"filter_file": "wifi.tat"})

    # Assert
    self.assertEqual(resp.get_json(), {"success": False, "message": "Filter file not found: wifi.tat"})
    mock_exists.assert_called_once_with(os.path.join(FILTER_DIR, "wifi.tat"))
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| JSON | `{filter_file: wifi.tat}` |  |
| `os.path.exists` | → `False` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| JSON | `{success: False, message: Filter file not found: wifi.tat}` | `assertEqual` |
| `exists` arg | `os.path.join(LOG_PARSER_DIR, "filter", "wifi.tat")` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Not-found | Path layout changed |

### 60. TestFilterRoutes.test_get_filter_details_success

**Description:** Existing filter → keywords from `extract_all_keywords_from_filter_file`.

**Code:**
```python
@patch(f"{ROUTES}.extract_all_keywords_from_filter_file")
@patch(f"{ROUTES}.os.path.exists", return_value=True)
def test_get_filter_details_success(self, mock_exists, mock_extract):
    # Arrange
    mock_extract.return_value = [{"keyword": "disconnect", "enabled": True}]

    # Act
    resp = self.client.post("/log_parser/get_filter_details", json={"filter_file": "wifi.tat"})

    # Assert
    self.assertEqual(resp.get_json(), {"success": True,
                                       "keywords": [{"keyword": "disconnect", "enabled": True}]})
    mock_extract.assert_called_once_with(os.path.join(FILTER_DIR, "wifi.tat"))
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| JSON | `{filter_file: wifi.tat}` |  |
| `os.path.exists` | `True` |  |
| `extract_all_keywords_from_filter_file` | → `[{keyword: disconnect, enabled: True}]` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| JSON | `{success: True, keywords: [...]}` | `assertEqual` |
| Extractor arg | `<FILTER_DIR>\wifi.tat` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Keywords returned | Wrong file parsed |

### 61. TestFilterRoutes.test_get_filter_details_does_not_strip_directory_components

**Description:** ⚠️ Documents CURRENT behavior, not endorsed: `/get_filter_details` joins the raw `filter_file` under the filter dir (no `basename`, unlike `/estimate_tokens`), so `..\x.tat` resolves outside it. Exposure is limited (localhost app, keyword extraction only), but this is a path-traversal smell.

**Code:**
```python
@patch(f"{ROUTES}.os.path.exists", return_value=False)
def test_get_filter_details_does_not_strip_directory_components(self, mock_exists):
    # Documents CURRENT behavior, not endorsed: unlike /estimate_tokens
    # (which applies os.path.basename) this route joins the raw name, so
    # a "..\" prefix escapes the filter directory.
    # Act
    self.client.post("/log_parser/get_filter_details", json={"filter_file": r"..\x.tat"})

    # Assert
    mock_exists.assert_called_once_with(os.path.join(FILTER_DIR, r"..\x.tat"))
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| JSON | `{filter_file: ..\x.tat}` | Traversal |
| `os.path.exists` | → `False` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `exists` arg | `os.path.join(FILTER_DIR, "..\x.tat")` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Raw name joined (current behavior) | Route starts sanitising with `basename` — update test + doc |

### 62. TestFilterRoutes.test_get_filter_details_read_error

**Description:** Extractor exception → `Failed to read filter: <err>` (HTTP 200, `success: False`).

**Code:**
```python
@patch(f"{ROUTES}.extract_all_keywords_from_filter_file", side_effect=OSError("locked"))
@patch(f"{ROUTES}.os.path.exists", return_value=True)
def test_get_filter_details_read_error(self, mock_exists, mock_extract):
    # Act
    resp = self.client.post("/log_parser/get_filter_details", json={"filter_file": "wifi.tat"})

    # Assert
    self.assertEqual(resp.get_json(), {"success": False, "message": "Failed to read filter: locked"})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `extract_all_keywords_from_filter_file` | side effect `OSError("locked")` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| JSON | `{success: False, message: Failed to read filter: locked}` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Handled | Exception escapes |

### 63. TestFilterRoutes.test_get_all_filter_details_missing_dir

**Description:** Filter directory absent → `Filter directory not found`.

**Code:**
```python
@patch(f"{ROUTES}.os.path.exists", return_value=False)
def test_get_all_filter_details_missing_dir(self, mock_exists):
    # Act
    resp = self.client.post("/log_parser/get_all_filter_details")

    # Assert
    self.assertEqual(resp.get_json(), {"success": False, "message": "Filter directory not found"})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `os.path.exists` | → `False` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| JSON | `{success: False, message: Filter directory not found}` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Handled | listdir on missing dir |

### 64. TestFilterRoutes.test_get_all_filter_details_lists_sorted_tat_files

**Description:** Only `.tat` files are returned, sorted by name, each with its keywords.

**Code:**
```python
@patch(f"{ROUTES}.extract_all_keywords_from_filter_file", side_effect=lambda p: [os.path.basename(p)])
@patch(f"{ROUTES}.os.listdir", return_value=["b.tat", "readme.md", "a.tat"])
@patch(f"{ROUTES}.os.path.exists", return_value=True)
def test_get_all_filter_details_lists_sorted_tat_files(self, mock_exists, mock_listdir, mock_extract):
    # Act
    resp = self.client.post("/log_parser/get_all_filter_details")

    # Assert
    self.assertEqual(resp.get_json(), {"success": True, "filters": [
        {"file": "a.tat", "keywords": ["a.tat"]},
        {"file": "b.tat", "keywords": ["b.tat"]},
    ]})
    mock_listdir.assert_called_once_with(FILTER_DIR)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `os.listdir` | → `[b.tat, readme.md, a.tat]` |  |
| Extractor | → `[basename]` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| JSON | `{success: True, filters: [{file: a.tat, keywords: [a.tat]}, {file: b.tat, keywords: [b.tat]}]}` | `assertEqual` |
| `listdir` arg | `<LOG_PARSER_DIR>\filter` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Sorted .tat only | Non-.tat included or unsorted |

### 65. TestFilterRoutes.test_get_all_filter_details_error

**Description:** `os.listdir` failure → `Failed to load filters: <err>`.

**Code:**
```python
@patch(f"{ROUTES}.os.listdir", side_effect=PermissionError("denied"))
@patch(f"{ROUTES}.os.path.exists", return_value=True)
def test_get_all_filter_details_error(self, mock_exists, mock_listdir):
    # Act
    resp = self.client.post("/log_parser/get_all_filter_details")

    # Assert
    self.assertEqual(resp.get_json(), {"success": False, "message": "Failed to load filters: denied"})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `os.listdir` | side effect `PermissionError("denied")` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| JSON | `{success: False, message: Failed to load filters: denied}` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Handled | Exception escapes |

### 66. TestEstimateTokens.test_requires_filter_file

**Description:** Whitespace-only `filter_file` → 400.

**Code:**
```python
def test_requires_filter_file(self):
    # Act
    resp = self.client.post("/log_parser/estimate_tokens", json={"filter_file": "  "})

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertEqual(resp.get_json()["message"], "No filter file specified")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| JSON | `{filter_file: "  "}` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` |  |
| Message | `No filter file specified` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| 400 | Blank accepted |

### 67. TestEstimateTokens.test_requires_log_path_in_session

**Description:** No `log_path` in session (analysis page not opened) → 400.

**Code:**
```python
def test_requires_log_path_in_session(self):
    # Act
    resp = self.client.post("/log_parser/estimate_tokens", json={"filter_file": "wifi.tat"})

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertEqual(resp.get_json()["message"], "Log file not found in session")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| JSON | `{filter_file: wifi.tat}` |  |
| Session | *(empty)* |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` |  |
| Message | `Log file not found in session` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| 400 | Estimation on nothing |

### 68. TestEstimateTokens.test_missing_filter_file_returns_400

**Description:** Missing filter → 400. Also proves path-traversal safety: only `os.path.basename` of the supplied name is joined to the filter dir.

**Code:**
```python
@patch(f"{ROUTES}.os.path.exists", side_effect=lambda p: p == TestEstimateTokens.LOG)
def test_missing_filter_file_returns_400(self, mock_exists):
    # Arrange
    self.seed_session(log_path=self.LOG)

    # Act
    resp = self.client.post("/log_parser/estimate_tokens", json={"filter_file": r"..\..\wifi.tat"})

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertEqual(resp.get_json()["message"], r"Filter file not found: ..\..\wifi.tat")
    mock_exists.assert_called_with(os.path.join(FILTER_DIR, "wifi.tat"))  # basename only
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Session `log_path` | `C:\fake\out\wifi.etl.log` |  |
| JSON | `{filter_file: ..\..\wifi.tat}` | Traversal attempt |
| `os.path.exists` | `True` only for the log path |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` |  |
| Message | `Filter file not found: ..\..\wifi.tat` | `assertEqual` |
| Last `exists` arg | `<FILTER_DIR>\wifi.tat` | `assert_called_with` |

| Pass Condition | Fail Condition |
|---|---|
| 400, basename only | Traversal path probed |

### 69. TestEstimateTokens.test_success_reports_token_counts

**Description:** Full pipeline read → enabled keywords → filter → preprocess → group, with `_estimate_tokens` on filtered and grouped text. `exceeds_limit` compares grouped tokens to the 20,000 limit.

**Code:**
```python
@patch(f"{ROUTES}.log_parser_service")
@patch("utils.log_parser_preprocess.group_similar_logs", return_value=["g1"])
@patch("utils.log_parser_preprocess.preprocess_log_for_llm", return_value=["p1", "p2"])
@patch("utils.log_parser_preprocess.filter_log_by_keywords", return_value=["f1", "f2", "f3"])
@patch("utils.log_parser_preprocess.extract_enabled_keywords_from_filter_file", return_value=["a", "b"])
@patch("utils.helpers.read_log_file", return_value=["l1", "l2", "l3", "l4"])
@patch(f"{ROUTES}.os.path.exists", return_value=True)
def test_success_reports_token_counts(self, mock_exists, mock_read, mock_kw, mock_filter,
                                      mock_pre, mock_group, mock_svc):
    # Arrange
    mock_svc._estimate_tokens.side_effect = [25_000, 21_000]
    self.seed_session(log_path=self.LOG)

    # Act
    resp = self.client.post("/log_parser/estimate_tokens", json={"filter_file": "wifi.tat"})

    # Assert
    self.assertEqual(resp.get_json(), {
        "success": True, "filtered_tokens": 25_000, "grouped_tokens": 21_000,
        "token_limit": 20_000, "exceeds_limit": True, "keyword_count": 2, "filtered_lines": 3,
    })
    mock_read.assert_called_once_with(self.LOG)
    mock_filter.assert_called_once_with(["l1", "l2", "l3", "l4"], ["a", "b"])
    self.assertEqual(mock_svc._estimate_tokens.call_args_list[0].args, ("f1\nf2\nf3",))
    self.assertEqual(mock_svc._estimate_tokens.call_args_list[1].args, ("g1",))
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Session `log_path` | `C:\fake\out\wifi.etl.log` |  |
| `utils.helpers.read_log_file` | → `[l1..l4]` |  |
| `extract_enabled_keywords_from_filter_file` | → `[a, b]` |  |
| `filter_log_by_keywords` / `preprocess_log_for_llm` / `group_similar_logs` | → `[f1,f2,f3]` / `[p1,p2]` / `[g1]` |  |
| `log_parser_service._estimate_tokens` | side effect `[25000, 21000]` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| JSON | `{success: True, filtered_tokens: 25000, grouped_tokens: 21000, token_limit: 20000, exceeds_limit: True, keyword_count: 2, filtered_lines: 3}` | `assertEqual` |
| Calls | `read_log_file(log)`, `filter_log_by_keywords([l1..l4], [a, b])`, `_estimate_tokens("f1\nf2\nf3")`, `_estimate_tokens("g1")` | `assert_called_once_with` / `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Exact numbers | Limit/aggregation changed |

### 70. TestEstimateTokens.test_processing_error_returns_500

**Description:** Any failure inside the pipeline (here a decode error reading the log) → 500 `success: False`.

**Code:**
```python
@patch("utils.helpers.read_log_file", side_effect=UnicodeDecodeError("utf-8", b"", 0, 1, "bad"))
@patch(f"{ROUTES}.os.path.exists", return_value=True)
def test_processing_error_returns_500(self, mock_exists, mock_read):
    # Arrange
    self.seed_session(log_path=self.LOG)

    # Act
    resp = self.client.post("/log_parser/estimate_tokens", json={"filter_file": "wifi.tat"})

    # Assert
    self.assertEqual(resp.status_code, 500)
    self.assertIs(resp.get_json()["success"], False)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `utils.helpers.read_log_file` | side effect `UnicodeDecodeError` |  |
| `os.path.exists` | `True` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `500` | `assertEqual` |
| `success` | `False` | `assertIs` |

| Pass Condition | Fail Condition |
|---|---|
| 500 | Exception escapes |

### 71. TestSendtoSessionPickup.test_valid_token_restores_session_once

**Description:** `_pickup_sendto_session` (a `before_app_request` hook, so it runs for every route): a valid `?_st=` token merges the stored snapshot into the session and is consumed (one-time).

**Code:**
```python
def test_valid_token_restores_session_once(self):
    # Arrange
    lp._sendto_session_store["tok1"] = (time.time(), {"download_path": SRC_DIR, "bsod": False})

    # Act
    self.client.get("/log_parser/verify_sendto_token?_st=tok1")

    # Assert
    sess = self.get_session()
    self.assertEqual(sess["download_path"], SRC_DIR)
    self.assertIs(sess["bsod"], False)
    self.assertNotIn("tok1", lp._sendto_session_store)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `_sendto_session_store` | `{tok1: (now, {download_path: C:\fake\local, bsod: False})}` |  |
| Request | `GET /log_parser/verify_sendto_token?_st=tok1` | Any route works |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Session | `download_path=C:\fake\local`, `bsod=False` | `assertEqual` / `assertIs` |
| Store | `tok1` removed | `assertNotIn` |

| Pass Condition | Fail Condition |
|---|---|
| Restored once | Not restored or token reusable |

### 72. TestSendtoSessionPickup.test_expired_tokens_are_purged_and_not_applied

**Description:** Entries older than `_SENDTO_TOKEN_TTL` (300 s) are purged on any request and are not applied even if their token is presented.

**Code:**
```python
def test_expired_tokens_are_purged_and_not_applied(self):
    # Arrange
    lp._sendto_session_store["old"] = (time.time() - lp._SENDTO_TOKEN_TTL - 1, {"download_path": "stale"})

    # Act
    self.client.get("/log_parser/verify_sendto_token?_st=old")

    # Assert
    self.assertNotIn("download_path", self.get_session())
    self.assertEqual(lp._sendto_session_store, {})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Store | `{old: (now - TTL - 1, {download_path: stale})}` |  |
| Request | `?_st=old` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Session | no `download_path` | `assertNotIn` |
| Store | `{}` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Purged, not applied | Expired snapshot applied |

### 73. TestSocketHandlers.test_submit_analysis_validation_error_emits_on_given_socketio

**Description:** `handle_submit_analysis` validates `(log_path, filter, prompt)`; on failure it emits `validation_error` on the passed SocketIO (`/progress`) and does not start analysis.

**Code:**
```python
def test_submit_analysis_validation_error_emits_on_given_socketio(self):
    # Arrange
    self.svc.validate_analysis_inputs.return_value = (False, "Pick a filter")
    with self.app.test_request_context():
        session["log_path"] = r"C:\fake\out\a.log"
        # Act
        lp.handle_submit_analysis({"filter_file": None, "prompt_content": "x"}, self.socketio)

    # Assert
    self.svc.validate_analysis_inputs.assert_called_once_with(r"C:\fake\out\a.log", None, "x")
    self.socketio.emit.assert_called_once_with("validation_error", {"message": "Pick a filter"},
                                               namespace="/progress")
    self.svc.start_analysis.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `log_parser_service` | patched in `setUp` |  |
| `socketio` arg | `MagicMock(name="socketio_arg")` |  |
| Session | `log_path=C:\fake\out\a.log` | `test_request_context` |
| data | `{filter_file: None, prompt_content: x}` |  |
| `validate_analysis_inputs` | → `(False, "Pick a filter")` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Validate call | `(C:\fake\out\a.log, None, "x")` | `assert_called_once_with` |
| Emit | `("validation_error", {message: Pick a filter}, namespace="/progress")` | `assert_called_once_with` |
| `start_analysis` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| Emitted, not started | Analysis started on invalid input |

### 74. TestSocketHandlers.test_submit_analysis_validation_error_falls_back_to_app_socketio

**Description:** Without a `socketio` argument the handler emits on `app_config.socketio` — note: WITHOUT a namespace in this branch (inconsistent with the other branch, documented as-is).

**Code:**
```python
def test_submit_analysis_validation_error_falls_back_to_app_socketio(self):
    # Arrange
    self.svc.validate_analysis_inputs.return_value = (False, "No log")
    with self.app.test_request_context():
        # Act
        lp.handle_submit_analysis({})

    # Assert
    self.app_config.socketio.emit.assert_called_once_with("validation_error", {"message": "No log"})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `log_parser_service` | patched in `setUp` |  |
| data | `{}` |  |
| `validate_analysis_inputs` | → `(False, "No log")` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `app_config.socketio.emit` | `("validation_error", {message: No log})` (no namespace) | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Fallback emitter used | Namespace added or wrong emitter |

### 75. TestSocketHandlers.test_submit_analysis_starts_service_with_case_description

**Description:** Valid input → `start_analysis(<FILTER_DIR>\wifi.tat, log_path, output_dir, app_config.llm_helper, prompt, case description)`. The case description comes from `session['case_context']['description']`. No error emitted.

**Code:**
```python
def test_submit_analysis_starts_service_with_case_description(self):
    # Arrange
    self.svc.validate_analysis_inputs.return_value = (True, "")
    self.svc.start_analysis.return_value = True
    with self.app.test_request_context():
        session["log_path"] = r"C:\fake\out\a.log"
        session["logparser_output_dir"] = r"C:\fake\out"
        session["case_context"] = self.create_mock_case_context()
        # Act
        lp.handle_submit_analysis({"filter_file": "wifi.tat", "prompt_content": "Analyze"}, self.socketio)

    # Assert
    self.svc.start_analysis.assert_called_once_with(
        os.path.join(FILTER_DIR, "wifi.tat"), r"C:\fake\out\a.log", r"C:\fake\out",
        self.app_config.llm_helper, "Analyze",
        "Wi-Fi drops every time the laptop resumes from Modern Standby.")
    self.socketio.emit.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `log_parser_service` | patched in `setUp` |  |
| `socketio` arg | `MagicMock(name="socketio_arg")` |  |
| Session | `log_path`, `logparser_output_dir=C:\fake\out`, `case_context=create_mock_case_context()` |  |
| data | `{filter_file: wifi.tat, prompt_content: Analyze}` |  |
| Service | validate `(True, "")`, start → `True` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `start_analysis` | `(<FILTER_DIR>\wifi.tat, C:\fake\out\a.log, C:\fake\out, app_config.llm_helper, "Analyze", "Wi-Fi drops every time the laptop resumes from Modern Standby.")` | `assert_called_once_with` |
| Emit | none | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| Exact service call | Arg order / description source changed |

### 76. TestSocketHandlers.test_submit_analysis_start_failure_emits_analysis_error

**Description:** `start_analysis` returning `False` → `analysis_error` `Failed to start analysis`.

**Code:**
```python
def test_submit_analysis_start_failure_emits_analysis_error(self):
    # Arrange
    self.svc.validate_analysis_inputs.return_value = (True, "")
    self.svc.start_analysis.return_value = False
    with self.app.test_request_context():
        session["logparser_output_dir"] = r"C:\fake\out"
        # Act
        lp.handle_submit_analysis({"filter_file": "wifi.tat"}, self.socketio)

    # Assert
    self.socketio.emit.assert_called_once_with("analysis_error", {"message": "Failed to start analysis"},
                                               namespace="/progress")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `log_parser_service` | patched in `setUp` |  |
| `socketio` arg | `MagicMock(name="socketio_arg")` |  |
| Service | validate OK; start → `False` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Emit | `("analysis_error", {message: Failed to start analysis}, namespace="/progress")` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Emitted | Silent failure |

### 77. TestSocketHandlers.test_submit_analysis_exception_emits_analysis_error

**Description:** Exceptions inside the try (here `session['logparser_output_dir']` KeyError because the form page was never opened) are caught and emitted as `analysis_error` with the error text.

**Code:**
```python
def test_submit_analysis_exception_emits_analysis_error(self):
    # Arrange: logparser_output_dir missing → session[...] KeyError inside the try
    self.svc.validate_analysis_inputs.return_value = (True, "")
    with self.app.test_request_context():
        # Act
        lp.handle_submit_analysis({"filter_file": "wifi.tat"}, self.socketio)

    # Assert
    event, payload = self.socketio.emit.call_args.args
    self.assertEqual(event, "analysis_error")
    self.assertIn("Failed to start analysis: ", payload["message"])
    self.assertIn("logparser_output_dir", payload["message"])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `log_parser_service` | patched in `setUp` |  |
| `socketio` arg | `MagicMock(name="socketio_arg")` |  |
| Session | *(no `logparser_output_dir`)* | Triggers KeyError |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Event | `analysis_error` | `assertEqual` |
| Message | starts `Failed to start analysis: ` and mentions `logparser_output_dir` | `assertIn` |

| Pass Condition | Fail Condition |
|---|---|
| Emitted | Exception escapes the socket handler |

### 78. TestSocketHandlers.test_chat_message_empty_is_rejected

**Description:** Whitespace-only chat message → `chat_error` `Empty message`.

**Code:**
```python
def test_chat_message_empty_is_rejected(self):
    # Act
    lp.handle_chat_message({"message": "   "}, self.socketio)

    # Assert
    self.socketio.emit.assert_called_once_with("chat_error", {"message": "Empty message"}, namespace="/progress")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `log_parser_service` | patched in `setUp` |  |
| `socketio` arg | `MagicMock(name="socketio_arg")` |  |
| data | `{message: "   "}` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Emit | `("chat_error", {message: Empty message}, namespace="/progress")` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Rejected | Sent to LLM |

### 79. TestSocketHandlers.test_chat_message_requires_prior_analysis

**Description:** Chat before any analysis (empty `conversation_history`) is refused.

**Code:**
```python
def test_chat_message_requires_prior_analysis(self):
    # Arrange
    self.svc.conversation_history = []

    # Act
    lp.handle_chat_message({"message": "why?"}, self.socketio)

    # Assert
    self.socketio.emit.assert_called_once_with(
        "chat_error", {"message": "Please run analysis first before chatting."}, namespace="/progress")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `log_parser_service` | patched in `setUp` |  |
| `socketio` arg | `MagicMock(name="socketio_arg")` |  |
| `conversation_history` | `[]` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Emit | `chat_error` `Please run analysis first before chatting.` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Refused | LLM called without context |

### 80. TestSocketHandlers.test_chat_message_without_llm_helper

**Description:** `app_config.llm_helper=None` → `chat_error` `LLM helper is not available.`; with no `socketio` argument it falls back to `app_config.socketio`.

**Code:**
```python
def test_chat_message_without_llm_helper(self):
    # Arrange
    self.svc.conversation_history = [{"role": "system"}]
    self.app_config.llm_helper = None

    # Act
    lp.handle_chat_message({"message": "why?"})

    # Assert: socketio=None → falls back to app_config.socketio
    self.app_config.socketio.emit.assert_called_once_with(
        "chat_error", {"message": "LLM helper is not available."}, namespace="/progress")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `log_parser_service` | patched in `setUp` |  |
| `conversation_history` | `[{role: system}]` |  |
| `app_config.llm_helper` | `None` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `app_config.socketio.emit` | `("chat_error", {message: LLM helper is not available.}, namespace="/progress")` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Refused | AttributeError |

### 81. TestSocketHandlers.test_chat_message_success_emits_markdown_html

**Description:** The trimmed message is sent to `log_parser_service.handle_chat_message`; the reply is emitted raw and as sanitised Markdown HTML.

**Code:**
```python
def test_chat_message_success_emits_markdown_html(self):
    # Arrange
    self.svc.conversation_history = [{"role": "system"}]
    self.svc.handle_chat_message.return_value = "**Root cause**: roaming"

    # Act
    lp.handle_chat_message({"message": " why? "}, self.socketio)

    # Assert
    self.svc.handle_chat_message.assert_called_once_with("why?", self.app_config.llm_helper)
    event, payload = self.socketio.emit.call_args.args
    self.assertEqual(event, "chat_response")
    self.assertEqual(payload["message"], "**Root cause**: roaming")
    self.assertIn("<strong>Root cause</strong>", payload["message_html"])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `log_parser_service` | patched in `setUp` |  |
| `socketio` arg | `MagicMock(name="socketio_arg")` |  |
| data | `{message: " why? "}` |  |
| `handle_chat_message` | → `**Root cause**: roaming` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Service call | `("why?", app_config.llm_helper)` | `assert_called_once_with` |
| Emit event / message | `chat_response` / raw reply | `assertEqual` |
| `message_html` | contains `<strong>Root cause</strong>` | `assertIn` |

| Pass Condition | Fail Condition |
|---|---|
| Response emitted | Untrimmed or unrendered |

### 82. TestSocketHandlers.test_chat_message_llm_exception_emits_chat_error

**Description:** LLM exceptions become `chat_error` `Chat failed: <err>`.

**Code:**
```python
def test_chat_message_llm_exception_emits_chat_error(self):
    # Arrange
    self.svc.conversation_history = [{"role": "system"}]
    self.svc.handle_chat_message.side_effect = TimeoutError("llm timeout")

    # Act
    lp.handle_chat_message({"message": "why?"}, self.socketio)

    # Assert
    self.socketio.emit.assert_called_once_with("chat_error", {"message": "Chat failed: llm timeout"},
                                               namespace="/progress")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `log_parser_service` | patched in `setUp` |  |
| `socketio` arg | `MagicMock(name="socketio_arg")` |  |
| `handle_chat_message` | side effect `TimeoutError("llm timeout")` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Emit | `("chat_error", {message: Chat failed: llm timeout}, namespace="/progress")` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Handled | Exception escapes |

### 83. TestSocketHandlers.test_filter_chat_validation_errors

**Description:** Three guards of `handle_chat_message_with_filter` (subTests): empty message, no keywords, no raw log lines. Each emits exactly one `chat_error`; the LLM is never called.

**Code:**
```python
def test_filter_chat_validation_errors(self):
    cases = [
        ({"message": "", "keywords": ["a"]}, ["x"], "Empty message"),
        ({"message": "hi", "keywords": []}, ["x"], "No filter keywords selected"),
        ({"message": "hi", "keywords": ["a"]}, [], "Please run analysis first. No raw log available."),
    ]
    for data, raw, expected in cases:
        with self.subTest(expected=expected):
            # Arrange
            self.socketio.reset_mock()
            self.svc.raw_log_lines = raw

            # Act
            lp.handle_chat_message_with_filter(data, self.socketio)

            # Assert
            self.socketio.emit.assert_called_once_with("chat_error", {"message": expected},
                                                       namespace="/progress")
    self.svc.handle_chat_message.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `log_parser_service` | patched in `setUp` |  |
| `socketio` arg | `MagicMock(name="socketio_arg")` |  |
| Cases | `({message:"",keywords:[a]}, raw=[x])`, `({message:hi,keywords:[]}, [x])`, `({message:hi,keywords:[a]}, raw=[])` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Emit per case | `Empty message` / `No filter keywords selected` / `Please run analysis first. No raw log available.` | `assert_called_once_with` |
| `handle_chat_message` | never called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| All 3 guarded | Any guard missing |

### 84. TestSocketHandlers.test_filter_chat_without_llm_helper

**Description:** No LLM helper → `chat_error`.

**Code:**
```python
def test_filter_chat_without_llm_helper(self):
    # Arrange
    self.svc.raw_log_lines = ["l1"]
    self.app_config.llm_helper = None

    # Act
    lp.handle_chat_message_with_filter({"message": "hi", "keywords": ["a"]}, self.socketio)

    # Assert
    self.socketio.emit.assert_called_once_with("chat_error", {"message": "LLM helper is not available."},
                                               namespace="/progress")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `log_parser_service` | patched in `setUp` |  |
| `socketio` arg | `MagicMock(name="socketio_arg")` |  |
| `raw_log_lines` | `[l1]` |  |
| `app_config.llm_helper` | `None` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Emit | `chat_error` `LLM helper is not available.` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Refused | AttributeError |

### 85. TestSocketHandlers.test_filter_chat_refilters_and_sends_enhanced_prompt

**Description:** Raw log lines are re-filtered with the user's keywords, preprocessed and grouped, then wrapped into an enhanced prompt ending with the user's instruction. With empty history a default system prompt is installed first.

**Code:**
```python
@patch("utils.log_parser_preprocess.group_similar_logs", return_value=["g1"])
@patch("utils.log_parser_preprocess.preprocess_log_for_llm", return_value=["p1"])
@patch("utils.log_parser_preprocess.filter_log_by_keywords", return_value=["f1"])
def test_filter_chat_refilters_and_sends_enhanced_prompt(self, mock_filter, mock_pre, mock_group):
    # Arrange: no conversation history yet → a default system prompt is installed
    self.svc.raw_log_lines = ["l1", "l2"]
    self.svc.conversation_history = []
    self.svc.handle_chat_message.return_value = "ok"

    # Act
    lp.handle_chat_message_with_filter({"message": "explain", "keywords": ["disconnect", "roam"]},
                                       self.socketio)

    # Assert
    mock_filter.assert_called_once_with(["l1", "l2"], ["disconnect", "roam"])
    mock_pre.assert_called_once_with(["f1"])
    mock_group.assert_called_once_with(["p1"])
    sent, helper = self.svc.handle_chat_message.call_args.args
    self.assertIs(helper, self.app_config.llm_helper)
    self.assertIn("following keywords: disconnect, roam", sent)
    self.assertIn("['g1']", sent)
    self.assertTrue(sent.endswith("explain"))
    self.assertEqual(self.svc.chat_system_prompt, "You are an expert log analyzer.")
    self.socketio.emit.assert_called_once_with(
        "chat_response", {"message": "ok", "message_html": "<p>ok</p>"}, namespace="/progress")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `log_parser_service` | patched in `setUp` |  |
| `socketio` arg | `MagicMock(name="socketio_arg")` |  |
| `raw_log_lines` / `conversation_history` | `[l1, l2]` / `[]` |  |
| data | `{message: explain, keywords: [disconnect, roam]}` |  |
| Preprocess mocks | filter→`[f1]`, preprocess→`[p1]`, group→`[g1]` | `utils.log_parser_preprocess.*` |
| `handle_chat_message` | → `ok` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Pipeline calls | `filter([l1,l2],[disconnect,roam])`, `preprocess([f1])`, `group([p1])` | `assert_called_once_with` |
| Prompt | contains `following keywords: disconnect, roam` and `['g1']`, ends with `explain` | `assertIn` / `assertTrue(endswith)` |
| `chat_system_prompt` | `You are an expert log analyzer.` | `assertEqual` |
| Emit | `("chat_response", {message: ok, message_html: <p>ok</p>}, namespace="/progress")` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Enhanced prompt sent | Keywords/log missing from prompt |

### 86. TestSocketHandlers.test_filter_chat_exception_emits_chat_error

**Description:** Errors in re-filtering → `chat_error` `Chat failed: <err>`.

**Code:**
```python
@patch("utils.log_parser_preprocess.filter_log_by_keywords", side_effect=ValueError("bad regex"))
def test_filter_chat_exception_emits_chat_error(self, mock_filter):
    # Arrange
    self.svc.raw_log_lines = ["l1"]

    # Act
    lp.handle_chat_message_with_filter({"message": "hi", "keywords": ["("]}, self.socketio)

    # Assert
    self.socketio.emit.assert_called_once_with("chat_error", {"message": "Chat failed: bad regex"},
                                               namespace="/progress")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `log_parser_service` | patched in `setUp` |  |
| `socketio` arg | `MagicMock(name="socketio_arg")` |  |
| `filter_log_by_keywords` | side effect `ValueError("bad regex")` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Emit | `chat_error` `Chat failed: bad regex` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Handled | Exception escapes |

### 87. TestSocketHandlers.test_reset_session_success

**Description:** Reset calls `log_parser_service.reset_log_parser()` and acknowledges to the requesting sid only.

**Code:**
```python
def test_reset_session_success(self):
    # Act
    lp.handle_reset_log_parser_session("sid-1", self.socketio)

    # Assert
    self.svc.reset_log_parser.assert_called_once_with()
    self.socketio.emit.assert_called_once_with("session_reset", {"success": True},
                                               namespace="/progress", to="sid-1")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `log_parser_service` | patched in `setUp` |  |
| `socketio` arg | `MagicMock(name="socketio_arg")` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Service | `reset_log_parser()` once | `assert_called_once_with` |
| Emit | `("session_reset", {success: True}, namespace="/progress", to="sid-1")` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Acknowledged | Broadcast to all clients |

### 88. TestSocketHandlers.test_reset_session_failure

**Description:** Reset failure is reported (`success: False`) via the `app_config.socketio` fallback.

**Code:**
```python
def test_reset_session_failure(self):
    # Arrange
    self.svc.reset_log_parser.side_effect = RuntimeError("busy")

    # Act
    lp.handle_reset_log_parser_session("sid-1")

    # Assert
    self.app_config.socketio.emit.assert_called_once_with(
        "session_reset", {"success": False, "message": "Failed to reset session: busy"},
        namespace="/progress", to="sid-1")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `log_parser_service` | patched in `setUp` |  |
| `reset_log_parser` | side effect `RuntimeError("busy")` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Emit | `("session_reset", {success: False, message: Failed to reset session: busy}, namespace="/progress", to="sid-1")` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Reported | Exception escapes |

### 89. TestSendtoSocketFlow.test_start_sendto_without_pending_path_emits_error

**Description:** The `start_sendto` Socket.IO handler (nested in `register_socketio_handlers`) with no pending path emits `sendto_error` to the caller's sid.

**Code:**
```python
def test_start_sendto_without_pending_path_emits_error(self):
    # Arrange
    sio = MagicMock()
    start = self._handlers(sio)["start_sendto"]
    with self.app.test_request_context():
        request.sid = "sid-1"
        # Act
        start()

    # Assert
    sio.emit.assert_called_once_with(
        "sendto_error", {"message": "No pending file in session. Please use Send To again."},
        namespace="/sendto-progress", to="sid-1")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `register_socketio_handlers` | called with `MagicMock` socketio; handlers captured by event name | `_handlers` helper |
| Context | `test_request_context()`, `request.sid=sid-1`, empty session |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Emit | `("sendto_error", {message: No pending file in session. Please use Send To again.}, namespace="/sendto-progress", to="sid-1")` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Emitted | Worker started with no path |

### 90. TestSendtoSocketFlow.test_start_sendto_with_vanished_file_emits_error

**Description:** Pending file deleted between page load and socket connect → `sendto_error` `File no longer exists`.

**Code:**
```python
@patch(f"{ROUTES}.os.path.exists", return_value=False)
def test_start_sendto_with_vanished_file_emits_error(self, mock_exists):
    # Arrange
    sio = MagicMock()
    start = self._handlers(sio)["start_sendto"]
    with self.app.test_request_context():
        request.sid = "sid-1"
        session["sendto_pending_path"] = os.path.join(SRC_DIR, "wifi.zip")
        # Act
        start()

    # Assert
    sio.emit.assert_called_once_with(
        "sendto_error", {"message": f"File no longer exists: {os.path.join(SRC_DIR, 'wifi.zip')}"},
        namespace="/sendto-progress", to="sid-1")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `register_socketio_handlers` | called with `MagicMock` socketio; handlers captured by event name | `_handlers` helper |
| Session | `sendto_pending_path=C:\fake\local\wifi.zip` |  |
| `os.path.exists` | `False` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Emit | `sendto_error` `File no longer exists: C:\fake\local\wifi.zip` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Emitted | Worker started |

### 91. TestSendtoSocketFlow.test_start_sendto_pops_pending_path_and_starts_daemon_worker

**Description:** Valid pending path: popped from session (refresh can't re-trigger) and a daemon thread is started for `_run_sendto_in_background` (Thread patched — nothing runs).

**Code:**
```python
@patch(f"{ROUTES}.threading.Thread")
@patch(f"{ROUTES}.os.path.exists", return_value=True)
def test_start_sendto_pops_pending_path_and_starts_daemon_worker(self, mock_exists, mock_thread):
    # Arrange
    sio = MagicMock()
    start = self._handlers(sio)["start_sendto"]
    with self.app.test_request_context():
        request.sid = "sid-1"
        session["sendto_pending_path"] = os.path.join(SRC_DIR, "wifi.zip")
        # Act
        start()
        pending_after = session.get("sendto_pending_path")

    # Assert
    self.assertIsNone(pending_after)
    mock_thread.assert_called_once()
    self.assertIs(mock_thread.call_args.kwargs["daemon"], True)
    mock_thread.return_value.start.assert_called_once_with()
    sio.emit.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `register_socketio_handlers` | called with `MagicMock` socketio; handlers captured by event name | `_handlers` helper |
| Session | `sendto_pending_path=C:\fake\local\wifi.zip` |  |
| `os.path.exists` | `True` |  |
| `threading.Thread` | patched |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Session after | no `sendto_pending_path` | `assertIsNone` |
| Thread | created once, `daemon=True`, `start()` once | `assert_called_once` / `assertIs` |
| Emit | none | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| Worker started | Path not consumed or non-daemon |

### 92. TestSendtoSocketFlow.test_background_worker_success_snapshots_session_and_emits_complete

**Description:** Runs `_run_sendto_in_background` synchronously. Verifies: progress events, the temporary `emit` monkey-patch forwards `wpp_log` to `sendto_wpp_log` and is ALWAYS restored, the session snapshot is stored under a uuid token, and `sendto_complete` carries the redirect URL with `&_st=<token>` appended.

**Code:**
```python
@patch(f"{ROUTES}.uuid.uuid4")
@patch(f"{ROUTES}.time.sleep")
def test_background_worker_success_snapshots_session_and_emits_complete(self, mock_sleep, mock_uuid):
    # Arrange
    mock_uuid.return_value.hex = "tok123"
    sio = MagicMock()
    original_emit = sio.emit
    lp._sendto_session_store.clear()
    self.addCleanup(lp._sendto_session_store.clear)

    def _fake_process(source_path, source_dir, file_path, original_name, timestamp, progress_cb=None):
        session["download_path"] = source_dir
        sio.emit("wpp_log", {"data": "parsing"}, namespace="/wpp_progress")  # forwarded
        return "/log_chatbot/?auto_run=analyze_all"

    with self.app.test_request_context(), \
            patch(f"{ROUTES}._process_local_analysis", side_effect=_fake_process):
        # Act
        lp._run_sendto_in_background(sio, "sid-1", os.path.join(SRC_DIR, "wifi.etl"))

    # Assert
    self.assertIs(sio.emit, original_emit)  # monkey-patched emit restored
    emitted = [(c.args[0], c.args[1]) for c in original_emit.call_args_list]
    self.assertIn(("sendto_progress", {"pct": 5, "msg": "File validated: wifi.etl"}), emitted)
    self.assertIn(("sendto_progress",
                   {"pct": 10, "msg": "Wi-Fi ETL file detected. Starting WPP/DDD parser…"}), emitted)
    self.assertIn(("sendto_wpp_log", {"data": "parsing"}), emitted)
    self.assertEqual(emitted[-1], ("sendto_complete",
                                   {"redirect_url": "/log_chatbot/?auto_run=analyze_all&_st=tok123"}))
    self.assertEqual(lp._sendto_session_store["tok123"][1], {"download_path": SRC_DIR})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `time.sleep` / `uuid.uuid4().hex` | patched / `tok123` |  |
| `_process_local_analysis` | fake: sets `session[download_path]`, emits `wpp_log {data: parsing}`, returns `/log_chatbot/?auto_run=analyze_all` |  |
| Source path | `C:\fake\local\wifi.etl` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `sio.emit` after run | original method restored | `assertIs` |
| Progress | `{pct: 5, msg: File validated: wifi.etl}`, `{pct: 10, msg: Wi-Fi ETL file detected. Starting WPP/DDD parser…}` | `assertIn` |
| Forwarded | `("sendto_wpp_log", {data: parsing})` | `assertIn` |
| Last event | `sendto_complete {redirect_url: /log_chatbot/?auto_run=analyze_all&_st=tok123}` | `assertEqual` |
| Store | `tok123` → `{download_path: C:\fake\local}` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| All events + snapshot | emit not restored, token missing, wrong separator |

### 93. TestSendtoSocketFlow.test_background_worker_value_error_emits_sendto_error

**Description:** Validation errors from processing become `sendto_error` with the raw message, addressed to the client sid.

**Code:**
```python
@patch(f"{ROUTES}.time.sleep")
def test_background_worker_value_error_emits_sendto_error(self, mock_sleep):
    # Arrange
    sio = MagicMock()
    with self.app.test_request_context(), \
            patch(f"{ROUTES}._process_local_analysis", side_effect=ValueError("No supported files")):
        # Act
        lp._run_sendto_in_background(sio, "sid-1", os.path.join(SRC_DIR, "a.zip"))

    # Assert
    self.assertEqual(sio.emit.call_args.args, ("sendto_error", {"message": "No supported files"}))
    self.assertEqual(sio.emit.call_args.kwargs, {"namespace": "/sendto-progress", "to": "sid-1"})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `_process_local_analysis` | side effect `ValueError("No supported files")` |  |
| Source | `C:\fake\local\a.zip` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Last emit | `("sendto_error", {message: No supported files})`, kwargs `{namespace: /sendto-progress, to: sid-1}` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Emitted | Exception escapes thread |

### 94. TestSendtoSocketFlow.test_background_worker_unexpected_error_emits_processing_failed

**Description:** Unexpected errors become `sendto_error` `Processing failed: <err>`; the `.dmp` pre-step message is also verified.

**Code:**
```python
@patch(f"{ROUTES}.time.sleep")
def test_background_worker_unexpected_error_emits_processing_failed(self, mock_sleep):
    # Arrange
    sio = MagicMock()
    with self.app.test_request_context(), \
            patch(f"{ROUTES}._process_local_analysis", side_effect=OSError("share offline")):
        # Act
        lp._run_sendto_in_background(sio, "sid-1", os.path.join(SRC_DIR, "memory.dmp"))

    # Assert
    emitted = [(c.args[0], c.args[1]) for c in sio.emit.call_args_list]
    self.assertIn(("sendto_progress",
                   {"pct": 10, "msg": "BSOD dump detected. Copying to shared folder…"}), emitted)
    self.assertEqual(emitted[-1], ("sendto_error", {"message": "Processing failed: share offline"}))
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `_process_local_analysis` | side effect `OSError("share offline")` |  |
| Source | `C:\fake\local\memory.dmp` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Progress | `{pct: 10, msg: BSOD dump detected. Copying to shared folder…}` | `assertIn` |
| Last emit | `sendto_error {message: Processing failed: share offline}` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Emitted | Exception escapes thread |

### 95. TestLogParserHelpers.test_allowed_local_analysis_filenames

**Description:** Table test of `_is_allowed_local_analysis_filename` — the single whitelist shared by picker, upload and SendTo. Note `dddLog_<n>.bin` is case-sensitive.

**Code:**
```python
def test_allowed_local_analysis_filenames(self):
    allowed = ["a.zip", "a.7z", "A.RAR", "x.log", "bt.hci.txt", "w.etl", "w.etl.003",
               "dddLog_12.bin", "memory.DMP", r"C:\dir\w.etl"]
    rejected = ["", "a.txt", "dddlog_12.bin", "dddLog_x.bin", "a.etlx", "notes.docx", None]
    for name in allowed:
        with self.subTest(allowed=name):
            self.assertTrue(lp._is_allowed_local_analysis_filename(name))
    for name in rejected:
        with self.subTest(rejected=name):
            self.assertFalse(lp._is_allowed_local_analysis_filename(name))
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Allowed | `a.zip, a.7z, A.RAR, x.log, bt.hci.txt, w.etl, w.etl.003, dddLog_12.bin, memory.DMP, C:\dir\w.etl` | subTests |
| Rejected | `"", a.txt, dddlog_12.bin, dddLog_x.bin, a.etlx, notes.docx, None` | subTests |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Return | `True` / `False` respectively | `assertTrue` / `assertFalse` |

| Pass Condition | Fail Condition |
|---|---|
| Whitelist exact | Extension added/removed |

### 96. TestLogParserHelpers.test_bt_and_fw_etl_detection_and_case_type

**Description:** `_is_bt_etl` (ibtpci/ibtusb prefix + `.etl`), `_is_fw_etl` (`wrt-fw-`/`wrt-fw~` 8.3 name + `.etl`), and `_infer_local_upload_case_type` (any BT file → `bt`).

**Code:**
```python
def test_bt_and_fw_etl_detection_and_case_type(self):
    self.assertTrue(lp._is_bt_etl(r"C:\x\IbtUsb_1.etl"))
    self.assertTrue(lp._is_bt_etl("ibtpci.etl"))
    self.assertFalse(lp._is_bt_etl("ibtusb.etl.001"))
    self.assertTrue(lp._is_fw_etl("wrt-fw-123.etl"))
    self.assertTrue(lp._is_fw_etl("WRT-FW~1.ETL"))
    self.assertFalse(lp._is_fw_etl("wrt-fw-123.log"))
    self.assertEqual(lp._infer_local_upload_case_type(["ibtusb.etl"]), "bt")
    self.assertEqual(lp._infer_local_upload_case_type([]), "wifi")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Names | `IbtUsb_1.etl`, `ibtpci.etl`, `ibtusb.etl.001`, `wrt-fw-123.etl`, `WRT-FW~1.ETL`, `wrt-fw-123.log` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Results | T, T, F, T, T, F; case type `bt` / `wifi` | `assertTrue` / `assertFalse` / `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| All match | Routing heuristics changed |

### 97. TestLogParserHelpers.test_format_bytes

**Description:** `_format_bytes` unit scaling used in upload progress messages.

**Code:**
```python
def test_format_bytes(self):
    self.assertEqual(lp._format_bytes(512), "512.0B")
    self.assertEqual(lp._format_bytes(2048), "2.0KB")
    self.assertEqual(lp._format_bytes(5 * 1024 ** 3), "5.0GB")
    self.assertEqual(lp._format_bytes(3 * 1024 ** 4), "3.0TB")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Values | `512`, `2048`, `5 GiB`, `3 TiB` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Return | `512.0B`, `2.0KB`, `5.0GB`, `3.0TB` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Exact strings | Format changed |

### 98. TestLogParserHelpers.test_render_safe_markdown_strips_scripts

**Description:** `_render_safe_markdown_html` renders Markdown then bleaches it: `<script>` removed, `<strong>` and `<a href>` kept. Guards the LLM reply → browser HTML path against XSS.

**Code:**
```python
def test_render_safe_markdown_strips_scripts(self):
    html = lp._render_safe_markdown_html("**bold** <script>alert(1)</script> [x](http://a)")
    self.assertIn("<strong>bold</strong>", html)
    self.assertNotIn("<script", html)
    self.assertIn('<a href="http://a">x</a>', html)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Text | `**bold** <script>alert(1)</script> [x](http://a)` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| HTML | contains `<strong>bold</strong>` and `<a href="http://a">x</a>`, not `<script` | `assertIn` / `assertNotIn` |

| Pass Condition | Fail Condition |
|---|---|
| Sanitised | Script tag survives |

### 99. TestLogParserHelpers.test_process_local_analysis_rejects_directory_before_touching_session

**Description:** `_process_local_analysis` validates directory input FIRST — it raises before needing a request context (this test runs with no Flask context at all).

**Code:**
```python
@patch(f"{ROUTES}.os.path.isdir", return_value=True)
def test_process_local_analysis_rejects_directory_before_touching_session(self, mock_isdir):
    with self.assertRaises(ValueError) as ctx:
        lp._process_local_analysis(SRC_DIR, SRC_DIR, SRC_DIR, "local", TIMESTAMP)
    self.assertIn("Selected path is a folder", str(ctx.exception))
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `os.path.isdir` | `True` |  |
| Args | `(C:\fake\local, C:\fake\local, C:\fake\local, "local", TIMESTAMP)` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Exception | `ValueError` containing `Selected path is a folder` | `assertRaises` / `assertIn` |

| Pass Condition | Fail Condition |
|---|---|
| Raises early | Session touched first (RuntimeError) |

### 100. TestLogParserHelpers.test_cancel_registry_signal_and_raise

**Description:** Cancel registry primitives: register → signal sets the event → `_raise_if_cancelled` raises; after unregister, signalling returns `False`; `None` or an unset event are no-ops.

**Code:**
```python
def test_cancel_registry_signal_and_raise(self):
    event = lp._register_local_upload("reg-1")
    try:
        self.assertTrue(lp._signal_cancel_local_upload("reg-1"))
        self.assertTrue(event.is_set())
        with self.assertRaises(lp._LocalAnalysisCancelled):
            lp._raise_if_cancelled(event)
    finally:
        lp._unregister_local_upload("reg-1")
    self.assertFalse(lp._signal_cancel_local_upload("reg-1"))
    lp._raise_if_cancelled(None)  # no-op
    lp._raise_if_cancelled(threading.Event())  # not set → no-op
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Upload id | `reg-1` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Signal | `True`, event set; after unregister `False` | `assertTrue` / `assertFalse` |
| Raise | `_LocalAnalysisCancelled`; none for `None`/unset | `assertRaises` |

| Pass Condition | Fail Condition |
|---|---|
| Registry semantics | Leak or no-op broken |

## Test Execution Flow

```
setUpClass (BaseTestCase)
  ├─ capture real app_config; patch configs.global_configs.app_config → MagicMock
  └─ repoint every module whose app_config IS the real object
     (incl. blueprints.log_parser.log_parser_routes, services.log_parser_service, services.etl_parser.wpp_ddd_parser)
        │
        ▼
for each test method:
  setUp (BaseTestCase)
    ├─ reset mock app_config (silent_mode=False, sendto_token='unit-test-token', socketio/llm_helper/driver_manager mocks)
    ├─ patch CaseContext.to_session → to_dict (no sidecar writes)
    ├─ fresh Flask app (TESTING, SECRET_KEY, WTF_CSRF_ENABLED=False) + register log_parser_bp
    └─ stub Blueprint('main'): index, download_result, download_result_bsod
  setUp (LogParserTestCase)
    └─ stub Blueprint('log_chatbot').index, Blueprint('bt_chatbot').index
  setUp (class-specific)
    ├─ TestUploadLocalAnalysisProcessing: patch os.path.exists/isdir/getsize, os.makedirs, helpers,
    │                                    gather_service, datetime (fixed timestamp)
    ├─ TestSocketHandlers: patch log_parser_service
    └─ TestSendtoSessionPickup: clear _sendto_session_store
        │
        ▼
  test body: per-test @patch (render_template, services, parsers, tkinter via sys.modules, threading.Thread …)
             seed session (seed_session / session_transaction / test_request_context)
             Arrange → Act (client.get/post · direct handler call) → Assert
        │
        ▼
  tearDown: pop app context, stop per-test patchers (addCleanup) ──► PASS / FAIL
        │
        ▼
tearDownClass: stop all app_config patchers (real singleton restored)
```

## Coverage Summary

| Metric | Value |
|---|---|
| Total Routes | 13 HTTP routes (+1 `before_app_request` hook, 6 Socket.IO events, 1 background worker) |
| Routes Tested | 13 / 13 (+ hook, 5/6 events with logic, worker) |
| Total Tests | 100 (2 structure · 5 page · 4 delegate · 5 picker · 6 upload-validation · 16 upload-processing · 4 cancel · 7 open_local_analysis · 5 navigate · 3 verify-token · 8 filter · 5 estimate · 2 session-pickup · 16 socket handlers · 6 SendTo socket/worker · 6 pure helpers) |
| Actual Coverage (%) | 100% of routes; every validation/error branch of every route except 3 listed ❌ items (in-place missing `.log`, FW `coex`/`None` type, generic Exception after cancel) |
| Out-of-scope items | 7 ⚪ (file-copy loop, force-rmtree/cleanup, psutil child polling/kill, cleanup tracking with real FS, extraction progress throttle, empty `wpp_events` handler, real daemon-thread execution) |

## What Would Make These Tests Fail?

| Source change | ✅ PASS | ❌ FAIL |
|---|---|---|
| Add/rename/remove a route or change its methods | all others | `test_blueprint_registered_with_url_prefix` |
| Rename a Socket.IO event or namespace | all others | `test_register_socketio_handlers_binds_all_events` |
| Drop the "source_path must equal picker-bound path" check | all others | `test_source_path_mismatch_returns_400`, `test_no_picked_path_in_session_returns_400` |
| Allow a new extension (e.g. `.txt`) or drop `.rar` | route tests | `test_allowed_local_analysis_filenames` (+ `test_invalid_extension_returns_400` if `.docx`) |
| Change `.log` redirect from `log_chatbot.index` | all others | `test_log_file_routes_to_wifi_chatbot` |
| Stop merging `evt_files` into `ddd` for archives | all others | `test_archive_is_extracted_and_routes_to_download_result` |
| Infer case type from `wifi_files` instead of `bt_files` | all others | `test_archive_is_extracted_and_routes_to_download_result` |
| Remove the BT-decode-failure `ValueError` | all others | `test_bt_etl_decode_failure_returns_400` |
| Change BSOD share folder naming `local_bsod_<ts>` | all others | `test_dmp_is_copied_to_bsod_share_and_routes_to_bsod_result` |
| Ignore `is_bsod` form flag | all others | `test_is_bsod_flag_forces_bsod_flow_for_non_dmp_file` |
| Stop popping session keys / unregistering on cancel | all others | `test_cancelled_upload_returns_cancelled_and_clears_session`, `test_value_error_after_cancel_…`, `test_system_exit_after_cancel_…` |
| Swallow `SystemExit` unconditionally | all others | `test_system_exit_without_cancel_propagates` |
| Remove the `killer_started` guard | all others | `test_active_upload_is_signalled_and_killer_started_once` |
| Accept a missing/incorrect SendTo token | all others | `test_invalid_token_is_rejected`, `test_mismatched_and_empty_tokens` |
| Build navigate URL from `request.host_url` | all others | `test_success_navigates_with_fixed_localhost_origin` |
| Loosen `startup_path` validation (allow `//` or `://`) | all others | `test_unsafe_startup_paths_return_400` |
| Add `os.path.basename` to `/get_filter_details` (fixing the traversal smell) | all others | `test_get_filter_details_does_not_strip_directory_components` (intended — update test + doc) |
| Change `TOKEN_LIMIT` from 20,000 | all others | `test_success_reports_token_counts` |
| Remove bleach sanitising of LLM replies | all others | `test_render_safe_markdown_strips_scripts` |
| Stop restoring `socketio.emit` after SendTo | all others | `test_background_worker_success_snapshots_session_and_emits_complete` |
| Make `_sendto_session_store` tokens reusable or drop TTL purge | all others | `test_valid_token_restores_session_once`, `test_expired_tokens_are_purged_and_not_applied` |
| Add a namespace to the `app_config.socketio` fallback emit in `handle_submit_analysis` | all others | `test_submit_analysis_validation_error_falls_back_to_app_socketio` |
| Gate `wpp_ddd_parser_run` / `bt_decode_via_cli` on `silent_mode` inside the route | — (parsers are patched) | none — would need a new test; `silent_mode` gating is currently in the service layer only |

### Known issues / surprising behavior surfaced by these tests (documented, not fixed)

- **No `silent_mode` gate in this module** — GUI launches are suppressed (or not) inside the patched ETL parsers; see Overview.
- **`/get_filter_details` path traversal smell** — raw `filter_file` joined under `LOG_PARSER_DIR\filter` (no `basename`, unlike `/estimate_tokens`). Pinned by `test_get_filter_details_does_not_strip_directory_components`.
- **`handle_submit_analysis` fallback emits without a namespace** when no `socketio` argument is given (all other handlers pass `namespace='/progress'`).
- **`SystemExit` from parser helpers propagates** out of `/upload_local_analysis` unless a cancel was requested (a 500 / killed request in production).
- **Import-time `SyntaxWarning: invalid escape sequence '\-'`** from the `_force_rmtree` docstring (line 133); harmless but noisy.
- Unused imports: `importlib.resources.path`, `werkzeug.utils.secure_filename`.
