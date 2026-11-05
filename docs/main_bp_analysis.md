# Main Blueprint — Analysis & Unit Test Documentation

- Route module: [blueprints/main/main_routes.py](../blueprints/main/main_routes.py)
- Test module: [unit_tests/test_main_blueprint.py](../unit_tests/test_main_blueprint.py)
- Shared fixture: [unit_tests/base_test.py](../unit_tests/base_test.py)
- Formatting reference: [docs/bsod_bp_analysis.md](bsod_bp_analysis.md)

## Overview — What Main Does

The `main` blueprint (`url_prefix="/"`) runs the case-entry workflow. It takes an IPS case number, fetches it through `CaseService`, and seeds the session (`case_context`, prompt path, workflow flags, gather-telemetry workflow id). It then lets the user pick attachments, shows the download-progress page (the actual download runs over SocketIO in the `download` blueprint), and builds the download-result page: Wi-Fi/BT/event/FW tables, issue-time filtering, and the auto-analysis ETL pick (newest-by-number, Run-Analysis recovery, AI-time override, BT newest-timestamp). It also hosts the BSOD result page, which feeds `bsod.bsod_submit`, plus utility endpoints: open a folder in Explorer, dump or page a Windows event log, and serve the BT event-ID map.

**Branch-specific:** on this branch `app_config.silent_mode` suppresses *automatic* Explorer/TextAnalysisTool launches in other blueprints. `main` has no `silent_mode` check: its only GUI launch is `/open_path`, which the user triggers with an explicit click, so Explorer opens regardless of `silent_mode`. `test_open_path_ignores_silent_mode` pins this behavior. The IPS Environment Details form (`case_context.env_detail`) is carried through the session as a normal `CaseContext` field. `main` doesn't read it, but it does reach the LLM pre-pass/classifier in the full `to_dict()`.

## All Routes (10 endpoints)

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | Render `index.html` with clipboard case number + random wireless-topic cards (`render_case_form`) |
| `/` | POST | Submit a case number → `CaseService.process_case`, reset + populate session, start gather workflow, redirect to `/select_attachments` (`handle_case_submission`) |
| `/start_latest_etl_llm` | POST | One-click Run Analysis: fetch case, auto-select latest ZIP, classify with LLM, run issue-time pre-pass, return JSON `redirect_url=/download_attachments` |
| `/select_attachments` | GET | Render `select_attachments.html` with the rehydrated case context (or redirect if session expired) |
| `/select_attachments` | POST | Save the selected attachments, set `bsod` / `latest_etl_llm` / `_run_analysis_requested` from `action`, record telemetry, run LLM pre-pass, redirect to `/download_attachments` |
| `/download_attachments` | GET | Resolve local or BSOD-share download path, store `session["download_path"]`, render `attachment_download_progress.html` |
| `/download_result` | GET | Build the download-result page: tables, coex detection, time filter, auto-analysis picks (Wi-Fi/BT/FW), render `download_result.html` |
| `/download_result_bsod` | GET | Render `bsod.html` with case number, user e-mail and BSOD share path |
| `/open_path` | POST | Open an existing local folder in Windows Explorer (204) — **not** gated by `silent_mode` |
| `/dump_event_txt` | POST | Convert a Windows event log to text via `services.event_log_service.dump_event_to_txt` |
| `/parse_event_log` | POST | Page/filter a Windows event log via `services.event_log_service.get_paged_events` |
| `/api/bt_event_map` | GET | Return `configs/bt_event_id_map.json` (or `{}` when missing/malformed) |

(`/` and `/select_attachments` each serve GET and POST; that is 10 URL rules / 12 method-handlers.)

## High-Level Flow Diagram

```
 ┌───────────── GET / ─────────────┐        ┌──────────── POST / ─────────────────────────────┐
 │ helpers.get_clipboard_case_nbr  │        │ case_number stripped of spaces                  │── "" ──► flash danger → 302 /
 │ _get_wireless_topics()          │        │ CaseService.process_case(CaseContext(case_nbr)) │── raises ─► flash danger → 302 /
 │   open+yaml on WIRELESS share   │        │   error_message? ───────────────────────────────│── yes ──► flash danger → 302 /
 │   (OSError/empty → fallback)    │        │ session.clear(); case_context.to_session();     │
 │ render index.html               │        │ prompt_file_path; bsod/latest_etl_llm/debug=F    │
 └─────────────────────────────────┘        │ _start_gather_workflow → gather_service          │
                                            └──────────────────────┬──────────────────────────┘
   POST /start_latest_etl_llm (JSON) ── same lookup ──┐            ▼  302 /select_attachments
     no number/invalid/no zip → 400 JSON              │   ┌──────── /select_attachments ─────────────────────┐
     exception → 500 JSON                             │   │ GET : no session → flash warning → 302 /          │
     pick_latest_zip_attachment → selected_files      │   │       render select_attachments.html              │
     llm_helper.classify_issue (best-effort)          │   │ POST: no session → 302 /                          │
     _prime_issue_ai_cache (best-effort)              │   │       selected_files ⊂ attachment_list            │
     _run_analysis_requested = True                   │   │       gather_service.record_attachment_selection* │
     → {"success":True,"redirect_url":"/download_attachments"}│ _prime_issue_ai_cache* (LLM organize)          │
                                                      │   │       action → bsod / latest_etl_llm / run marker │
                                                      ▼   └───────────────────────┬───────────────────────────┘
                                ┌─────────── GET /download_attachments ───────────▼──────────────┐
                                │ no session → 302 /                                             │
                                │ _resolve_download_path: bsod ? <get_load_path>\<case folder>   │
                                │                         : case_context.case_download_dir       │
                                │ session["download_path"]; render attachment_download_progress │
                                └──────────────┬─────────────────────────────────────────────────┘
                      (SocketIO 'start_download' in download_bp fills app_config.download_results)
                                               ▼
 ┌──────────────────────────── GET /download_result ─────────────────────────────────────────────┐
 │ no session / empty path → flash warning → 302 /                                                │
 │ app_config.get_download_results(case_nbr) → wifi/ddd/bt/fw dicts                               │
 │ files_coexist = wifi items AND bt items → written back to session case_context                 │
 │ get_issue_time_from_selected_files → filter_folders_by_time per ZIP → info / warnings          │
 │ llm_issue_time = session _issue_ai_quick.issue_times[0] or time_mapping fallback               │
 │   → align_issue_time_for_display (LLM-assisted tz alignment)                                    │
 │ run_analysis_pending = session.pop(_run_analysis_requested)                                     │
 │ auto_analysis_etl: coex+bt → None | get_auto_analysis_etl → recovery(max .etl) → AI-time pick   │
 │ auto_analysis_bt : pending & bt → newest _parse_path_timestamp                                  │
 │ ai_pre_selected_etl, chip date anchor, fw info/hints, auto_analysis_fw (popped)                 │
 │ table rows (wifi/bt/event/fw), BT split expansion, BT sizes, latest evt                         │
 │ render download_result.html (22 kwargs + **file_dicts)                                          │
 └────────────────────────────────────────────────────────────────────────────────────────────────┘
 GET /download_result_bsod: no session/empty share → 302 / ; else render bsod.html(case_nbr,email,case_path)
 POST /open_path        : exists → subprocess.run(['explorer', path]) 204 ; else 400 "Invalid path"
 POST /dump_event_txt   : ImportError 503 | invalid path 400 | dump_event_to_txt → 200 | error 500
 POST /parse_event_log  : ImportError 503 | invalid path 400 | get_paged_events(offset≥0,…) → 200 | error 500
 GET  /api/bt_event_map : exists → json file ; missing / malformed → {}
 (* = best-effort, exceptions swallowed)
```

## Dependencies

```
flask:        Blueprint, render_template, request, session, redirect, url_for, flash, jsonify
stdlib:       os, glob, subprocess, time, random, datetime, json (lazy), traceback (lazy), re (lazy)
third-party:  yaml (safe_load for wireless topics)
helpers:      utils.helpers.get_clipboard_case_number, detect_user_email, get_load_path
              utils.etl_utils.get_auto_analysis_etl, get_issue_time_from_selected_files,
                  filter_folders_by_time, extract_timestamp_from_folder, pick_latest_zip_attachment,
                  extract_file_number (lazy)
              utils.fw_utils.load_fw_system_info, infer_fw_parse_type
              utils.issue_time_ai (lazy): organize_issue_context, align_issue_time_for_display,
                  align_issue_datetime_to_customer_frame, pick_etl_by_ai_time,
                  anchor_time_only_to_folder_date
services:     services.case_info_service.CaseService (process_case, load_case_summary_prompt)
              services.gather_service (new_workflow_id, record_workflow_start,
                  record_attachment_selection, record_feature_usage)
              services.llm_service.LLM_helper (empty_usage; instance via app_config.llm_helper)
              services.event_log_service (lazy; dump_event_to_txt, get_paged_events)
cross-bp:     blueprints.log_parser.log_parser_routes._parse_path_timestamp (lazy)
app_config:   llm_helper (classify_issue, client, model), get_download_results(case_nbr),
              etl_exclude_keywords   — NOTE: silent_mode is NOT read by this blueprint
configs:      configs.path_configs.WIRELESS_TOPICS_DIR, LOAD_PATH_prim, LOAD_PATH_bkup (lazy)
              configs/bt_event_id_map.json
models:       models.models.CaseContext (from_session, to_session, to_dict)
session keys: read : case_context, bsod, selected_files, gather_workflow_id, _issue_ai_quick,
                     _run_analysis_requested, auto_analysis_fw, latest_etl_llm (via etl_utils)
              write: case_context, prompt_file_path, bsod, latest_etl_llm, debug_mode,
                     gather_workflow_id, selected_files, classification, _issue_ai_quick,
                     _run_analysis_requested, download_path
templates:    index.html, select_attachments.html, attachment_download_progress.html,
              download_result.html, bsod.html
```

## Test Coverage Analysis

```
STRUCTURE
  ✅ 'main' registered, url_prefix '/', exact 10 rules + methods ── test_blueprint_registered_with_url_prefix

ROUTES
  /  GET
    ✅ renders index.html with clipboard + topics              ── test_get_renders_case_form
  /  POST
    ✅ empty case number → flash + redirect                     ── test_post_empty_case_number_flashes_and_redirects
    ✅ error_message from CaseService → flash + redirect        ── test_post_invalid_case_flashes_and_redirects
    ✅ success → session reset/populated, workflow started      ── test_post_valid_case_populates_session_and_redirects
    ✅ CaseService exception → flash + redirect                 ── test_post_service_exception_flashes_and_redirects
  /start_latest_etl_llm  POST
    ✅ missing case number → 400                                ── test_missing_case_number_returns_400
    ✅ invalid case → 400                                        ── test_invalid_case_returns_400
    ✅ no ZIP attachment → 400, session untouched               ── test_no_zip_attachment_returns_400
    ✅ success → flags, classification, telemetry, JSON         ── test_success_sets_session_classifies_and_returns_redirect
    ✅ llm_helper None → default classification                 ── test_no_llm_helper_keeps_default_classification
    ✅ classify + pre-pass failures swallowed                   ── test_classification_and_prepass_failures_are_best_effort
    ✅ outer exception → 500                                     ── test_service_exception_returns_500
  /select_attachments  GET
    ✅ no session → redirect                                     ── test_get_without_session_redirects_to_index
    ✅ renders form                                              ── test_get_renders_form_with_case_context
  /select_attachments  POST
    ✅ no session → redirect, no LLM                             ── test_post_without_session_redirects_to_index
    ✅ action=analysis → run flags + telemetry                  ── test_post_analysis_action_sets_run_flags
    ✅ action=latest_etl_llm (legacy) → run flags               ── test_post_legacy_latest_etl_llm_action_is_accepted
    ✅ action=bsod → bsod flag, no run marker                   ── test_post_bsod_action_sets_bsod_without_run_marker
    ✅ telemetry + pre-pass failures swallowed                  ── test_post_telemetry_and_prepass_failures_are_best_effort
  /download_attachments  GET
    ✅ no session → redirect                                     ── TestDownloadAttachments.test_without_session_redirects_to_index
    ✅ non-BSOD → case_download_dir                              ── test_non_bsod_uses_case_download_dir
    ✅ BSOD → share\case_nbr                                     ── test_bsod_uses_shared_load_path_and_case_nbr
  /download_result  GET
    ✅ no session → redirect                                     ── TestDownloadResult.test_without_session_redirects_to_index
    ✅ empty download path → redirect                            ── test_empty_download_path_redirects_to_index
    ✅ Wi-Fi only baseline render (kwargs contract)             ── test_wifi_only_renders_tables_without_auto_pick
    ✅ coex + BT hint → files_coexist, Wi-Fi pick skipped       ── test_coex_bt_hint_skips_wifi_auto_pick
    ✅ Run-Analysis recovery pick                                ── test_run_analysis_recovery_picks_highest_numbered_etl
    ✅ AI-time override of newest-by-number                      ── test_ai_issue_time_overrides_newest_by_number_pick
    ✅ attachment time filter success + llm time fallback        ── test_attachment_time_filter_success_and_llm_time_fallback
    ✅ attachment time filter warning                            ── test_attachment_time_filter_warning_is_reported
    ✅ BT auto pick (newest ts) + auto_analysis_fw pop           ── test_bt_run_analysis_picks_newest_timestamped_bt_log
    ❌ NOT TESTED  datetime issue_time → align_issue_datetime_to_customer_frame + strftime fallback
    ❌ NOT TESTED  time-filter exception branch (info/warnings reset to [])
    ❌ NOT TESTED  AI pick == newest-by-number (reason re-tagged 'ai_time' on same path)
    ❌ NOT TESTED  anchor_time_only_to_folder_date changing the chip (patched as identity)
    ❌ NOT TESTED  BT '.etl.split' → '_split*.etl' expansion (needs real files; isfile patched False)
    ❌ NOT TESTED  latest_evt_path selection, _get_latest_fw_system_info / fw_type_hints values
    ❌ NOT TESTED  _extract_first_folder_from_zip folder grouping (fake paths sit outside download_path → '')
  /download_result_bsod  GET
    ✅ no session → redirect                                     ── TestDownloadResultBsod.test_without_session_redirects_to_index
    ✅ unreachable share → redirect                              ── test_unreachable_share_redirects_to_index
    ✅ renders bsod.html                                         ── test_renders_bsod_page
  /open_path  POST
    ✅ existing path → explorer, 204                             ── test_existing_path_opens_explorer
    ✅ missing / non-JSON → 400                                  ── test_missing_or_empty_path_returns_400
    ✅ BRANCH-SPECIFIC: silent_mode=True still opens explorer    ── test_open_path_ignores_silent_mode
  /dump_event_txt  POST
    ✅ ImportError → 503                                         ── TestDumpEventTxt.test_import_error_returns_503
    ✅ invalid path → 400                                        ── TestDumpEventTxt.test_invalid_path_returns_400
    ✅ success → txt_path                                        ── test_success_returns_txt_path
    ✅ service error → 500                                       ── TestDumpEventTxt.test_service_exception_returns_500
  /parse_event_log  POST
    ✅ ImportError → 503                                         ── TestParseEventLog.test_import_error_returns_503
    ✅ invalid path → 400                                        ── TestParseEventLog.test_invalid_path_returns_400
    ✅ success, offset clamped, filters forwarded                ── test_success_passes_clamped_paging_and_filters
    ✅ defaults + service error → 500                            ── test_defaults_and_service_exception
  /api/bt_event_map  GET
    ✅ file present → JSON                                       ── test_returns_json_map
    ✅ file missing → {}                                         ── test_missing_file_returns_empty_object
    ✅ malformed JSON → {}                                       ── test_malformed_json_returns_empty_object

CORE LOGIC
  ✅ _get_wireless_topics merge/filter/limit                    ── test_merges_bt_and_wifi_topics_and_limits
  ✅ _get_wireless_topics OSError fallback                       ── test_unreachable_share_returns_fallback
  ✅ _get_wireless_topics empty-YAML fallback                    ── test_empty_yaml_returns_fallback
  ✅ _resolve_download_path dashed backend_id                    ── test_resolve_download_path_prefers_dashed_backend_id
  ✅ _resolve_download_path no share / None ctx → ''             ── test_resolve_download_path_bsod_without_share_returns_empty
  ✅ _build_merged_table_rows rowspans + .log filter            ── test_rows_carry_zip_rowspan_and_filters
  ✅ _build_event_table_rows evtx filter                         ── test_event_rows_keep_only_system_event_logs
  ✅ _prime_issue_ai_cache organize + usage                      ── test_description_is_organized_and_usage_recorded
  ✅ _prime_issue_ai_cache subtitle fallback                     ── test_empty_description_falls_back_to_attachment_subtitles
  ✅ _prime_issue_ai_cache bail-out                              ── test_no_text_anywhere_bails_without_llm_call
  ✅ _start_gather_workflow (with/without selected_files)        ── test_post_valid_case_populates_session_and_redirects,
                                                                   test_success_sets_session_classifies_and_returns_redirect

BACKGROUND / SOCKETIO
  ⚪ none ── main_bp has no SocketIO handlers or threads (downloads run in download_bp)
```

## What the Unit Tests Actually Test

Shared constants (module top of the test file): `ROUTES = "blueprints.main.main_routes"`, `ISSUE_TIME_AI = "utils.issue_time_ai"`, `WORKFLOW_ID = "wf-test-1"`, `PROMPT_PATH = "prompts/wifi_case_summary.txt"`, `ZIP_ITEM = ["logs.zip", "https://ips.example/att/1", ["2026-04-14 01:26:00", "uploaded by Partner"]]`, `TXT_ITEM = ["notes.txt", "https://ips.example/att/2", ["2026-04-13 10:00:00", ""]]`, `EVT_PATH = r"C:\fake\events\System.evtx"`, `WIFI_ETL_1/WIFI_ETL_3 = r"C:\fake\downloads\00123456\logs\20260414\WiFi_1.etl"/"…WiFi_3.etl"`, `WIFI_LOG = r"…\20260414\setup.log"`, `BT_OLD = r"…\logs\old\BT_a.etl"`, `BT_NEW = r"…\logs\new\BT_b.etl"`. `_processed_case(**overrides)` builds the `CaseContext` that the patched `CaseService.process_case` returns (`case_nbr="00123456"`, `wifi_or_bt="wifi"`, `case_download_dir=r"C:\fake\downloads\00123456"`, `attachment_list=[ZIP_ITEM, TXT_ITEM]`). `MainTestBase` adds `flashes()` (reads `_flashes` from the session) and `assert_redirects_to(resp, path)` (302 + exact `Location`).

**— Class `TestMainBlueprintStructure` —**

### 1. test_blueprint_registered_with_url_prefix

**Description:** Verifies `main_bp` is registered under the name `main` with `url_prefix="/"` and exposes exactly the 10 expected URL rules, each with at least its declared HTTP methods. Catches added/removed/renamed routes and narrowed methods that would break templates' `url_for('main.*')` links.

**Code:**
```python
def test_blueprint_registered_with_url_prefix(self):
    # Arrange
    expected = {
        "/": {"GET", "POST"},
        "/select_attachments": {"GET", "POST"},
        "/download_attachments": {"GET"},
        "/download_result": {"GET"},
        "/download_result_bsod": {"GET"},
        "/open_path": {"POST"},
        "/dump_event_txt": {"POST"},
        "/parse_event_log": {"POST"},
        "/api/bt_event_map": {"GET"},
        "/start_latest_etl_llm": {"POST"},
    }

    # Act
    rules = {r.rule: r.methods for r in self.app.url_map.iter_rules() if r.endpoint.startswith("main.")}

    # Assert
    self.assertIs(self.app.blueprints.get("main"), main_bp)
    self.assertEqual(main_bp.url_prefix, "/")
    self.assertEqual(set(rules), set(expected))
    for rule, methods in expected.items():
        self.assertTrue(methods.issubset(rules[rule]), rule)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Registered blueprint | `main_bp` | Registered in `BaseTestCase.setUp` |
| `expected` map | `/`, `/select_attachments` (GET+POST); `/download_attachments`, `/download_result`, `/download_result_bsod`, `/api/bt_event_map` (GET); `/open_path`, `/dump_event_txt`, `/parse_event_log`, `/start_latest_etl_llm` (POST) | Source-of-truth route table |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `app.blueprints["main"]` | `main_bp` | `assertIs` |
| `main_bp.url_prefix` | `"/"` | `assertEqual` |
| Set of `main.*` rules | == the 10 expected rules | `assertEqual` |
| Methods per rule | ⊇ expected methods | `assertTrue(issubset)` |

| Pass Condition | Fail Condition |
|---|---|
| Exact rule set and methods match | A route is added/removed/renamed, prefix changes, or a method is dropped |

**— Class `TestIndex` —**

### 2. test_get_renders_case_form

**Description:** GET `/` must render `index.html` with the clipboard case number and the wireless-topic cards. `helpers.get_clipboard_case_number` and `_get_wireless_topics` are patched so no clipboard or network-share access happens.

**Code:**
```python
@patch(f"{ROUTES}._get_wireless_topics", return_value=[{"topic": "T", "explanation": "E"}])
@patch(f"{ROUTES}.helpers.get_clipboard_case_number", return_value="00999999")
@patch(f"{ROUTES}.render_template", return_value="index-page")
def test_get_renders_case_form(self, mock_render, mock_clip, mock_topics):
    # Act
    resp = self.client.get("/")

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_data(as_text=True), "index-page")
    mock_render.assert_called_once_with(
        "index.html", clipboard_text="00999999",
        wireless_topics=[{"topic": "T", "explanation": "E"}])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| HTTP | `GET /` | Index page |
| `helpers.get_clipboard_case_number` | → `"00999999"` | Patched clipboard |
| `_get_wireless_topics` | → `[{"topic": "T", "explanation": "E"}]` | Patched topic loader |
| `render_template` | → `"index-page"` | Patched |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / body | `200` / `"index-page"` | `assertEqual` |
| `render_template` call | `("index.html", clipboard_text="00999999", wireless_topics=[{"topic": "T", "explanation": "E"}])` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Template and both kwargs passed exactly | Template renamed, kwarg renamed/dropped |

### 3. test_post_empty_case_number_flashes_and_redirects

**Description:** A whitespace-only `case_number` is rejected before any IPS lookup: the user gets a `danger` flash and is redirected back to `/`.

**Code:**
```python
@patch(f"{ROUTES}.CaseService")
def test_post_empty_case_number_flashes_and_redirects(self, mock_service):
    # Act
    resp = self.client.post("/", data={"case_number": "   "})

    # Assert
    self.assert_redirects_to(resp, "/")
    self.assertEqual(self.flashes(), [("danger", "❌ No case number provided.")])
    mock_service.process_case.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| HTTP | `POST /` form `case_number="   "` | Blank after strip |
| `CaseService` | patched MagicMock | Must not be used |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / `Location` | `302` → `/` | `assert_redirects_to` |
| Flashes | `[("danger", "❌ No case number provided.")]` | `assertEqual` |
| `CaseService.process_case` | not called | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| Flash + redirect, no lookup | Lookup attempted with an empty number, or message changes |

### 4. test_post_invalid_case_flashes_and_redirects

**Description:** When `CaseService.process_case` returns a context with `error_message`, the route flashes "Invalid case number…", redirects to `/`, does not store a `case_context`, and does not start a gather workflow.

**Code:**
```python
@patch(f"{ROUTES}.gather_service")
@patch(f"{ROUTES}.CaseService")
def test_post_invalid_case_flashes_and_redirects(self, mock_service, mock_gather):
    # Arrange
    mock_service.process_case.return_value = _processed_case(error_message="not found")

    # Act
    resp = self.client.post("/", data={"case_number": "00000000"})

    # Assert
    self.assert_redirects_to(resp, "/")
    self.assertEqual(self.flashes()[0][0], "danger")
    self.assertIn("Invalid case number", self.flashes()[0][1])
    self.assertNotIn("case_context", self.get_session())
    mock_gather.record_workflow_start.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| HTTP | `POST /` form `case_number="00000000"` | Unknown case |
| `CaseService.process_case` | → `_processed_case(error_message="not found")` | Lookup failure |
| `gather_service` | patched MagicMock | Telemetry |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / `Location` | `302` → `/` | `assert_redirects_to` |
| Flash | category `"danger"`, text contains `"Invalid case number"` | `assertEqual` / `assertIn` |
| Session | no `case_context` | `assertNotIn` |
| `gather_service.record_workflow_start` | not called | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| Error path leaves session untouched | Session populated or workflow started on a failed lookup |

### 5. test_post_valid_case_populates_session_and_redirects

**Description:** Happy path of case submission. Verifies spaces are stripped from the case number, the session is cleared then repopulated (`case_context`, `prompt_file_path`, `bsod`/`latest_etl_llm`/`debug_mode` = `False`, `gather_workflow_id`), a gather workflow is started with the case's domain and attachment list, and the user is redirected to `/select_attachments`.

**Code:**
```python
@patch(f"{ROUTES}.gather_service")
@patch(f"{ROUTES}.CaseService")
def test_post_valid_case_populates_session_and_redirects(self, mock_service, mock_gather):
    # Arrange
    mock_service.process_case.return_value = _processed_case()
    mock_service.load_case_summary_prompt.return_value = PROMPT_PATH
    mock_gather.new_workflow_id.return_value = WORKFLOW_ID
    with self.client.session_transaction() as sess:
        sess["stale_key"] = "from previous case"

    # Act
    resp = self.client.post("/", data={"case_number": " 00 123 456 "})

    # Assert
    self.assert_redirects_to(resp, "/select_attachments")
    submitted = mock_service.process_case.call_args.kwargs["case_context"]
    self.assertEqual(submitted.case_nbr, TEST_CASE_NBR)  # spaces stripped
    mock_service.load_case_summary_prompt.assert_called_once_with("wifi")
    sess = self.get_session()
    self.assertNotIn("stale_key", sess)  # session.clear()
    self.assertEqual(sess["case_context"]["case_nbr"], TEST_CASE_NBR)
    self.assertEqual(sess["prompt_file_path"], PROMPT_PATH)
    self.assertIs(sess["bsod"], False)
    self.assertIs(sess["latest_etl_llm"], False)
    self.assertIs(sess["debug_mode"], False)
    self.assertEqual(sess["gather_workflow_id"], WORKFLOW_ID)
    mock_gather.record_workflow_start.assert_called_once()
    start_kwargs = mock_gather.record_workflow_start.call_args.kwargs
    self.assertEqual(start_kwargs["workflow_id"], WORKFLOW_ID)
    self.assertEqual(start_kwargs["domain"], "wifi")
    self.assertEqual(start_kwargs["attachment_list"], [ZIP_ITEM, TXT_ITEM])
    mock_gather.record_attachment_selection.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| HTTP | `POST /` form `case_number=" 00 123 456 "` | Spaces must be removed |
| Pre-existing session | `stale_key="from previous case"` | Must be cleared |
| `CaseService.process_case` | → `_processed_case()` (`wifi_or_bt="wifi"`, `attachment_list=[ZIP_ITEM, TXT_ITEM]`) | Lookup success |
| `CaseService.load_case_summary_prompt` | → `"prompts/wifi_case_summary.txt"` (`PROMPT_PATH`) | Prompt path |
| `gather_service.new_workflow_id` | → `"wf-test-1"` (`WORKFLOW_ID`) | Workflow id |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / `Location` | `302` → `/select_attachments` | `assert_redirects_to` |
| `process_case(case_context=…).case_nbr` | `"00123456"` | `assertEqual` |
| `load_case_summary_prompt` | called once with `"wifi"` | `assert_called_once_with` |
| Session | no `stale_key`; `case_context.case_nbr="00123456"`; `prompt_file_path=PROMPT_PATH`; `bsod`/`latest_etl_llm`/`debug_mode` is `False`; `gather_workflow_id="wf-test-1"` | `assertNotIn` / `assertEqual` / `assertIs` |
| `record_workflow_start` kwargs | `workflow_id="wf-test-1"`, `domain="wifi"`, `attachment_list=[ZIP_ITEM, TXT_ITEM]` | `assert_called_once` + `assertEqual` |
| `record_attachment_selection` | not called (no selection yet) | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| All session keys and telemetry calls as listed | Session not cleared, key renamed, wrong redirect, or selection recorded prematurely |

### 6. test_post_service_exception_flashes_and_redirects

**Description:** Any exception from `CaseService.process_case` is caught: the user gets the generic `danger` flash and a redirect to `/` instead of a 500.

**Code:**
```python
@patch(f"{ROUTES}.CaseService")
def test_post_service_exception_flashes_and_redirects(self, mock_service):
    # Arrange
    mock_service.process_case.side_effect = RuntimeError("IPS down")

    # Act
    resp = self.client.post("/", data={"case_number": TEST_CASE_NBR})

    # Assert
    self.assert_redirects_to(resp, "/")
    self.assertEqual(self.flashes(), [("danger", "An error occurred while processing the case.")])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| HTTP | `POST /` form `case_number="00123456"` | Valid-looking case |
| `CaseService.process_case` | side effect `RuntimeError("IPS down")` | Backend failure |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / `Location` | `302` → `/` | `assert_redirects_to` |
| Flashes | `[("danger", "An error occurred while processing the case.")]` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Exception converted to flash + redirect | Exception escapes (500) or flash text changes |

**— Class `TestGetWirelessTopics` —**

### 7. test_merges_bt_and_wifi_topics_and_limits

**Description:** Core logic behind the index page. `_get_wireless_topics` reads two YAML files from the shared `WIRELESS_TOPICS_DIR`: BT entries map `topic/explanation`, Wi-Fi 7 entries map `topic/fact`→`explanation`; incomplete entries are dropped, the list is shuffled and truncated to `limit`. `open`, `yaml.safe_load` and `random` are patched so the result is deterministic and the network share is never touched.

**Code:**
```python
@patch(f"{ROUTES}.random")
@patch(f"{ROUTES}.yaml.safe_load")
@patch(f"{ROUTES}.open", new_callable=mock_open, read_data="", create=True)
def test_merges_bt_and_wifi_topics_and_limits(self, mock_file, mock_yaml, mock_random):
    # Arrange: BT file uses topic/explanation, WiFi file uses topic/fact
    mock_yaml.side_effect = [
        {"bluetooth_topics": [
            {"topic": "LE Audio", "explanation": "LC3 codec"},
            {"topic": "no explanation"},
        ]},
        {"knowledge_base": {"items": [
            {"topic": "MLO", "fact": "Multi-link operation"},
            {"topic": "320 MHz", "fact": "Wider channels"},
        ]}},
    ]

    # Act
    topics = main_routes._get_wireless_topics(limit=2)

    # Assert
    self.assertEqual(topics, [
        {"topic": "LE Audio", "explanation": "LC3 codec"},
        {"topic": "MLO", "explanation": "Multi-link operation"},
    ])
    self.assertEqual(mock_file.call_count, 2)
    mock_random.shuffle.assert_called_once()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `open` | `mock_open(read_data="")` | No real share access |
| `yaml.safe_load` side effect | 1st: `{"bluetooth_topics": [{"topic": "LE Audio", "explanation": "LC3 codec"}, {"topic": "no explanation"}]}`; 2nd: `{"knowledge_base": {"items": [{"topic": "MLO", "fact": "Multi-link operation"}, {"topic": "320 MHz", "fact": "Wider channels"}]}}` | BT file, then Wi-Fi file |
| `random` | patched MagicMock (shuffle is a no-op) | Deterministic order |
| `limit` | `2` | Truncation |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Return value | `[{"topic": "LE Audio", "explanation": "LC3 codec"}, {"topic": "MLO", "explanation": "Multi-link operation"}]` | `assertEqual` |
| `open` calls | `2` | `assertEqual(call_count)` |
| `random.shuffle` | called once | `assert_called_once()` |

| Pass Condition | Fail Condition |
|---|---|
| Mapping, filtering, shuffle and limit as expected | Field mapping changes (e.g. `fact` no longer used), incomplete items kept, or limit ignored |

### 8. test_unreachable_share_returns_fallback

**Description:** If the share is unreachable (`open` raises `OSError`), the helper returns the single hard-coded Bluetooth Core Specification card instead of failing the index page.

**Code:**
```python
@patch(f"{ROUTES}.open", side_effect=OSError("share unreachable"), create=True)
def test_unreachable_share_returns_fallback(self, mock_file):
    # Act / Assert
    self.assertEqual(main_routes._get_wireless_topics(), self.FALLBACK)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `open` | side effect `OSError("share unreachable")` | Share offline |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Return value | `FALLBACK` = `[{"topic": "Bluetooth Core Specification", "explanation": "Defines the technologies required to create interoperable Bluetooth devices."}]` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Fallback returned | Exception propagates or a different fallback is returned |

### 9. test_empty_yaml_returns_fallback

**Description:** Empty YAML files (`safe_load` → `None`) yield no topics, so the fallback card is returned.

**Code:**
```python
@patch(f"{ROUTES}.yaml.safe_load", return_value=None)
@patch(f"{ROUTES}.open", new_callable=mock_open, read_data="", create=True)
def test_empty_yaml_returns_fallback(self, mock_file, mock_yaml):
    # Act / Assert
    self.assertEqual(main_routes._get_wireless_topics(), self.FALLBACK)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `open` | `mock_open(read_data="")` | Files readable |
| `yaml.safe_load` | → `None` | Empty files |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Return value | `FALLBACK` = `[{"topic": "Bluetooth Core Specification", "explanation": "Defines the technologies required to create interoperable Bluetooth devices."}]` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Fallback returned | Empty list returned (index would show no card) |

**— Class `TestStartLatestEtlLlm` —**

### 10. test_missing_case_number_returns_400

**Description:** The index-page Run Analysis shortcut requires a case number; with none it returns a JSON 400 and never calls IPS.

**Code:**
```python
@patch(f"{ROUTES}.CaseService")
def test_missing_case_number_returns_400(self, mock_service):
    # Act
    resp = self.client.post("/start_latest_etl_llm", data={})

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertEqual(resp.get_json(), {"success": False, "message": "No case number provided."})
    mock_service.process_case.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `setUp` fixture | `llm_helper.classify_issue` → `{"issue_type": "Disconnect", "confidence": 90, "keywords_found": ["drop"]}`; `_prime_issue_ai_cache` patched (`self.mock_prime`) | Class fixture |
| HTTP | `POST /start_latest_etl_llm` with empty form | No `case_number` |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` | `assertEqual` |
| JSON | `{"success": False, "message": "No case number provided."}` | `assertEqual` |
| `process_case` | not called | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| 400 JSON, no lookup | Lookup attempted or error JSON shape changes |

### 11. test_invalid_case_returns_400

**Description:** A lookup with `error_message` returns a JSON 400 with the "Invalid case number…" message.

**Code:**
```python
@patch(f"{ROUTES}.CaseService")
def test_invalid_case_returns_400(self, mock_service):
    # Arrange
    mock_service.process_case.return_value = _processed_case(error_message="bad")

    # Act
    resp = self.client.post("/start_latest_etl_llm", data={"case_number": TEST_CASE_NBR})

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertEqual(resp.get_json()["message"], "Invalid case number or unable to retrieve data.")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `setUp` fixture | `llm_helper.classify_issue` → `{"issue_type": "Disconnect", "confidence": 90, "keywords_found": ["drop"]}`; `_prime_issue_ai_cache` patched (`self.mock_prime`) | Class fixture |
| HTTP | form `case_number="00123456"` |  |
| `process_case` | → `_processed_case(error_message="bad")` | Lookup failure |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` | `assertEqual` |
| JSON `message` | `"Invalid case number or unable to retrieve data."` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| 400 + message | Proceeds to the download flow on a failed lookup |

### 12. test_no_zip_attachment_returns_400

**Description:** If the case has no `.zip` attachment, the real `pick_latest_zip_attachment` returns `None` and the route returns a JSON 400 **before** clearing/populating the session.

**Code:**
```python
@patch(f"{ROUTES}.CaseService")
def test_no_zip_attachment_returns_400(self, mock_service):
    # Arrange
    mock_service.process_case.return_value = _processed_case(attachment_list=[TXT_ITEM])

    # Act
    resp = self.client.post("/start_latest_etl_llm", data={"case_number": TEST_CASE_NBR})

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertEqual(resp.get_json()["message"], "No ZIP attachment found for this case.")
    self.assertNotIn("case_context", self.get_session())
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `setUp` fixture | `llm_helper.classify_issue` → `{"issue_type": "Disconnect", "confidence": 90, "keywords_found": ["drop"]}`; `_prime_issue_ai_cache` patched (`self.mock_prime`) | Class fixture |
| HTTP | form `case_number="00123456"` |  |
| `process_case` | → `_processed_case(attachment_list=[TXT_ITEM])` | Only `notes.txt` |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` | `assertEqual` |
| JSON `message` | `"No ZIP attachment found for this case."` | `assertEqual` |
| Session | no `case_context` | `assertNotIn` |

| Pass Condition | Fail Condition |
|---|---|
| 400 and session untouched | Session populated or a non-zip selected |

### 13. test_success_sets_session_classifies_and_returns_redirect

**Description:** Happy path of the one-click Run Analysis flow: session cleared and repopulated with the latest ZIP as the only selected file, `latest_etl_llm` and `_run_analysis_requested` set, LLM classification stored, selection recorded in gather telemetry, LLM pre-pass invoked, and the JSON tells the browser to go to `/download_attachments`.

**Code:**
```python
@patch(f"{ROUTES}.gather_service")
@patch(f"{ROUTES}.CaseService")
def test_success_sets_session_classifies_and_returns_redirect(self, mock_service, mock_gather):
    # Arrange
    mock_service.process_case.return_value = _processed_case()
    mock_service.load_case_summary_prompt.return_value = PROMPT_PATH
    mock_gather.new_workflow_id.return_value = WORKFLOW_ID
    with self.client.session_transaction() as sess:
        sess["stale_key"] = "x"

    # Act
    resp = self.client.post("/start_latest_etl_llm", data={"case_number": TEST_CASE_NBR})

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_json(), {"success": True, "redirect_url": "/download_attachments"})
    sess = self.get_session()
    self.assertNotIn("stale_key", sess)
    self.assertEqual(sess["selected_files"], [ZIP_ITEM])
    self.assertIs(sess["bsod"], False)
    self.assertIs(sess["latest_etl_llm"], True)
    self.assertIs(sess["_run_analysis_requested"], True)
    self.assertEqual(sess["prompt_file_path"], PROMPT_PATH)
    self.assertEqual(sess["classification"]["issue_type"], "Disconnect")
    self.app_config.llm_helper.classify_issue.assert_called_once()
    mock_gather.record_attachment_selection.assert_called_once()
    self.assertEqual(mock_gather.record_attachment_selection.call_args.kwargs["selected_files"], [ZIP_ITEM])
    self.mock_prime.assert_called_once()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `setUp` fixture | `llm_helper.classify_issue` → `{"issue_type": "Disconnect", "confidence": 90, "keywords_found": ["drop"]}`; `_prime_issue_ai_cache` patched (`self.mock_prime`) | Class fixture |
| HTTP | form `case_number="00123456"` |  |
| Pre-existing session | `stale_key="x"` | Must be cleared |
| `process_case` | → `_processed_case()` (attachments `[ZIP_ITEM, TXT_ITEM]`) |  |
| `load_case_summary_prompt` | → `PROMPT_PATH` |  |
| `gather_service.new_workflow_id` | → `"wf-test-1"` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `200` / `{"success": True, "redirect_url": "/download_attachments"}` | `assertEqual` |
| Session | no `stale_key`; `selected_files=[ZIP_ITEM]`; `bsod is False`; `latest_etl_llm is True`; `_run_analysis_requested is True`; `prompt_file_path=PROMPT_PATH`; `classification["issue_type"]="Disconnect"` | `assertEqual` / `assertIs` |
| `llm_helper.classify_issue` | called once | `assert_called_once()` |
| `record_attachment_selection` | called once, `selected_files=[ZIP_ITEM]` | `assert_called_once` + `assertEqual` |
| `_prime_issue_ai_cache` | called once | `assert_called_once()` |

| Pass Condition | Fail Condition |
|---|---|
| All flags, classification and calls as listed | Wrong attachment picked, run marker missing, classification not stored, or redirect URL changes |

### 14. test_no_llm_helper_keeps_default_classification

**Description:** With `app_config.llm_helper = None` the route still succeeds, keeps the default `Unclassified` classification, and calls the pre-pass with `initial_usage=None` (no usage accumulator without a helper).

**Code:**
```python
@patch(f"{ROUTES}.gather_service")
@patch(f"{ROUTES}.CaseService")
def test_no_llm_helper_keeps_default_classification(self, mock_service, mock_gather):
    # Arrange
    self.app_config.llm_helper = None
    mock_service.process_case.return_value = _processed_case()
    mock_service.load_case_summary_prompt.return_value = PROMPT_PATH
    mock_gather.new_workflow_id.return_value = WORKFLOW_ID

    # Act
    resp = self.client.post("/start_latest_etl_llm", data={"case_number": TEST_CASE_NBR})

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertTrue(resp.get_json()["success"])
    self.assertEqual(self.get_session()["classification"],
                     {"issue_type": "Unclassified", "confidence": 0, "keywords_found": []})
    self.mock_prime.assert_called_once()
    self.assertIsNone(self.mock_prime.call_args.kwargs["initial_usage"])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `setUp` fixture | `llm_helper.classify_issue` → `{"issue_type": "Disconnect", "confidence": 90, "keywords_found": ["drop"]}`; `_prime_issue_ai_cache` patched (`self.mock_prime`) | Class fixture |
| `app_config.llm_helper` | `None` | LLM unavailable |
| `process_case` / `load_case_summary_prompt` / `new_workflow_id` | `_processed_case()` / `PROMPT_PATH` / `"wf-test-1"` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / `success` | `200` / `True` | `assertEqual` / `assertTrue` |
| `session["classification"]` | `{"issue_type": "Unclassified", "confidence": 0, "keywords_found": []}` | `assertEqual` |
| `_prime_issue_ai_cache` | called once, kwarg `initial_usage=None` | `assert_called_once` / `assertIsNone` |

| Pass Condition | Fail Condition |
|---|---|
| Default classification kept, flow continues | Route crashes on a `None` helper or classification altered |

### 15. test_classification_and_prepass_failures_are_best_effort

**Description:** Both the LLM classification and the issue-time pre-pass are best-effort: when both raise, the route still returns success and keeps the default classification.

**Code:**
```python
@patch(f"{ROUTES}.gather_service")
@patch(f"{ROUTES}.CaseService")
def test_classification_and_prepass_failures_are_best_effort(self, mock_service, mock_gather):
    # Arrange
    mock_service.process_case.return_value = _processed_case()
    mock_service.load_case_summary_prompt.return_value = PROMPT_PATH
    mock_gather.new_workflow_id.return_value = WORKFLOW_ID
    self.app_config.llm_helper.classify_issue.side_effect = RuntimeError("LLM down")
    self.mock_prime.side_effect = RuntimeError("prepass down")

    # Act
    resp = self.client.post("/start_latest_etl_llm", data={"case_number": TEST_CASE_NBR})

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertTrue(resp.get_json()["success"])
    self.assertEqual(self.get_session()["classification"]["issue_type"], "Unclassified")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `setUp` fixture | `llm_helper.classify_issue` → `{"issue_type": "Disconnect", "confidence": 90, "keywords_found": ["drop"]}`; `_prime_issue_ai_cache` patched (`self.mock_prime`) | Class fixture |
| `llm_helper.classify_issue` | side effect `RuntimeError("LLM down")` |  |
| `_prime_issue_ai_cache` | side effect `RuntimeError("prepass down")` |  |
| `process_case` / `load_case_summary_prompt` / `new_workflow_id` | `_processed_case()` / `PROMPT_PATH` / `"wf-test-1"` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / `success` | `200` / `True` | `assertEqual` / `assertTrue` |
| `classification["issue_type"]` | `"Unclassified"` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Failures swallowed | Either failure turns into a 500 |

### 16. test_service_exception_returns_500

**Description:** An exception from `process_case` is caught by the outer handler and returned as a JSON 500 with a generic message.

**Code:**
```python
@patch(f"{ROUTES}.CaseService")
def test_service_exception_returns_500(self, mock_service):
    # Arrange
    mock_service.process_case.side_effect = RuntimeError("IPS down")

    # Act
    resp = self.client.post("/start_latest_etl_llm", data={"case_number": TEST_CASE_NBR})

    # Assert
    self.assertEqual(resp.status_code, 500)
    self.assertEqual(resp.get_json(),
                     {"success": False, "message": "An error occurred while processing the case."})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `setUp` fixture | `llm_helper.classify_issue` → `{"issue_type": "Disconnect", "confidence": 90, "keywords_found": ["drop"]}`; `_prime_issue_ai_cache` patched (`self.mock_prime`) | Class fixture |
| `process_case` | side effect `RuntimeError("IPS down")` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `500` | `assertEqual` |
| JSON | `{"success": False, "message": "An error occurred while processing the case."}` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Generic JSON 500 | Exception escapes or internal error text leaks |

**— Class `TestSelectAttachments` —**

### 17. test_get_without_session_redirects_to_index

**Description:** GET `/select_attachments` with no `case_context` (expired/cleared session) flashes a `warning` and redirects to `/` instead of crashing.

**Code:**
```python
def test_get_without_session_redirects_to_index(self):
    # Act
    resp = self.client.get("/select_attachments")

    # Assert
    self.assert_redirects_to(resp, "/")
    self.assertEqual(self.flashes(), [("warning", "Session expired. Please start again.")])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `setUp` fixture | `_prime_issue_ai_cache` patched (`self.mock_prime`) | Class fixture |
| Session | *(empty)* | No `case_context` |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / `Location` | `302` → `/` | `assert_redirects_to` |
| Flashes | `[("warning", "Session expired. Please start again.")]` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Graceful redirect | KeyError/500 or a different message |

### 18. test_get_renders_form_with_case_context

**Description:** With a session, the form renders `select_attachments.html` with `ai_analysis=None` and the case context rehydrated via `CaseContext.from_session(...).to_dict()` (equal to the seeded dict).

**Code:**
```python
@patch(f"{ROUTES}.render_template", return_value="select-page")
def test_get_renders_form_with_case_context(self, mock_render):
    # Arrange
    self.seed_session()

    # Act
    resp = self.client.get("/select_attachments")

    # Assert
    self.assertEqual(resp.status_code, 200)
    args, kwargs = mock_render.call_args
    self.assertEqual(args, ("select_attachments.html",))
    self.assertIsNone(kwargs["ai_analysis"])
    self.assertEqual(kwargs["case_context"], self.create_mock_case_context())
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `setUp` fixture | `_prime_issue_ai_cache` patched (`self.mock_prime`) | Class fixture |
| Session | `case_context=create_mock_case_context()` |  |
| `render_template` | → `"select-page"` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| `render_template` args | `("select_attachments.html",)` | `assertEqual` |
| kwargs | `ai_analysis=None`, `case_context == create_mock_case_context()` | `assertIsNone` / `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Template + kwargs correct | Template renamed or context not passed through |

### 19. test_post_without_session_redirects_to_index

**Description:** POST without a session also redirects to `/` and never runs the LLM pre-pass.

**Code:**
```python
def test_post_without_session_redirects_to_index(self):
    # Act
    resp = self.client.post("/select_attachments", data={"selected_files": ["logs.zip"]})

    # Assert
    self.assert_redirects_to(resp, "/")
    self.mock_prime.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `setUp` fixture | `_prime_issue_ai_cache` patched (`self.mock_prime`) | Class fixture |
| HTTP | `POST` form `selected_files=["logs.zip"]` |  |
| Session | *(empty)* | No `case_context` |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / `Location` | `302` → `/` | `assert_redirects_to` |
| `_prime_issue_ai_cache` | not called | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| Redirect, no LLM | Pre-pass runs on an empty session |

### 20. test_post_analysis_action_sets_run_flags

**Description:** Submitting with `action=analysis` (the redesigned template's button name) keeps only the selected attachments, sets `bsod=False`, `latest_etl_llm=True` and the `_run_analysis_requested` marker, records the selection in gather telemetry with the session workflow id, runs the pre-pass and redirects to `/download_attachments`.

**Code:**
```python
@patch(f"{ROUTES}.gather_service")
def test_post_analysis_action_sets_run_flags(self, mock_gather):
    # Arrange
    self._seed_with_attachments()

    # Act
    resp = self.client.post("/select_attachments",
                            data={"selected_files": ["logs.zip"], "action": "analysis"})

    # Assert
    self.assert_redirects_to(resp, "/download_attachments")
    sess = self.get_session()
    self.assertEqual(sess["selected_files"], [ZIP_ITEM])
    self.assertIs(sess["bsod"], False)
    self.assertIs(sess["latest_etl_llm"], True)
    self.assertIs(sess["_run_analysis_requested"], True)
    mock_gather.record_attachment_selection.assert_called_once()
    kwargs = mock_gather.record_attachment_selection.call_args.kwargs
    self.assertEqual(kwargs["workflow_id"], WORKFLOW_ID)
    self.assertEqual(kwargs["selected_files"], [ZIP_ITEM])
    self.assertEqual(kwargs["domain"], "wifi")
    self.mock_prime.assert_called_once()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `setUp` fixture | `_prime_issue_ai_cache` patched (`self.mock_prime`) | Class fixture |
| Session (`_seed_with_attachments`) | `case_context=create_mock_case_context(attachment_list=[ZIP_ITEM, TXT_ITEM])`, `gather_workflow_id="wf-test-1"` |  |
| HTTP | `POST` form `selected_files=["logs.zip"]`, `action="analysis"` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / `Location` | `302` → `/download_attachments` | `assert_redirects_to` |
| Session | `selected_files=[ZIP_ITEM]`; `bsod is False`; `latest_etl_llm is True`; `_run_analysis_requested is True` | `assertEqual` / `assertIs` |
| `record_attachment_selection` kwargs | `workflow_id="wf-test-1"`, `selected_files=[ZIP_ITEM]`, `domain="wifi"` | `assert_called_once` + `assertEqual` |
| `_prime_issue_ai_cache` | called once | `assert_called_once()` |

| Pass Condition | Fail Condition |
|---|---|
| Flags, filter and telemetry as listed | `analysis` action no longer triggers auto-run, or selection not filtered |

### 21. test_post_legacy_latest_etl_llm_action_is_accepted

**Description:** The legacy `action=latest_etl_llm` value from older templates must still trigger the auto-run flags.

**Code:**
```python
@patch(f"{ROUTES}.gather_service")
def test_post_legacy_latest_etl_llm_action_is_accepted(self, mock_gather):
    # Arrange
    self._seed_with_attachments()

    # Act
    self.client.post("/select_attachments",
                     data={"selected_files": ["logs.zip"], "action": "latest_etl_llm"})

    # Assert
    sess = self.get_session()
    self.assertIs(sess["latest_etl_llm"], True)
    self.assertIs(sess["_run_analysis_requested"], True)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `setUp` fixture | `_prime_issue_ai_cache` patched (`self.mock_prime`) | Class fixture |
| Session (`_seed_with_attachments`) | `case_context=create_mock_case_context(attachment_list=[ZIP_ITEM, TXT_ITEM])`, `gather_workflow_id="wf-test-1"` |  |
| HTTP | form `selected_files=["logs.zip"]`, `action="latest_etl_llm"` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Session | `latest_etl_llm is True`, `_run_analysis_requested is True` | `assertIs` |

| Pass Condition | Fail Condition |
|---|---|
| Legacy name honored | Legacy name dropped from the accepted set |

### 22. test_post_bsod_action_sets_bsod_without_run_marker

**Description:** `action=bsod` sets `bsod=True`, disables auto-run (`latest_etl_llm=False`) and must not set `_run_analysis_requested`. Both selected attachments are kept.

**Code:**
```python
@patch(f"{ROUTES}.gather_service")
def test_post_bsod_action_sets_bsod_without_run_marker(self, mock_gather):
    # Arrange
    self._seed_with_attachments()

    # Act
    resp = self.client.post("/select_attachments",
                            data={"selected_files": ["logs.zip", "notes.txt"], "action": "bsod"})

    # Assert
    self.assert_redirects_to(resp, "/download_attachments")
    sess = self.get_session()
    self.assertEqual(sess["selected_files"], [ZIP_ITEM, TXT_ITEM])
    self.assertIs(sess["bsod"], True)
    self.assertIs(sess["latest_etl_llm"], False)
    self.assertNotIn("_run_analysis_requested", sess)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `setUp` fixture | `_prime_issue_ai_cache` patched (`self.mock_prime`) | Class fixture |
| Session (`_seed_with_attachments`) | `case_context=create_mock_case_context(attachment_list=[ZIP_ITEM, TXT_ITEM])`, `gather_workflow_id="wf-test-1"` |  |
| HTTP | form `selected_files=["logs.zip", "notes.txt"]`, `action="bsod"` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / `Location` | `302` → `/download_attachments` | `assert_redirects_to` |
| Session | `selected_files=[ZIP_ITEM, TXT_ITEM]`; `bsod is True`; `latest_etl_llm is False`; no `_run_analysis_requested` | `assertEqual` / `assertIs` / `assertNotIn` |

| Pass Condition | Fail Condition |
|---|---|
| BSOD flags only | Auto-run marker set for a BSOD submission |

### 23. test_post_telemetry_and_prepass_failures_are_best_effort

**Description:** Gather telemetry and the LLM pre-pass are both best-effort; when both raise, the selection is still saved and the redirect still happens (here with a plain `action=download`).

**Code:**
```python
@patch(f"{ROUTES}.gather_service")
def test_post_telemetry_and_prepass_failures_are_best_effort(self, mock_gather):
    # Arrange
    self._seed_with_attachments()
    mock_gather.record_attachment_selection.side_effect = RuntimeError("telemetry down")
    self.mock_prime.side_effect = RuntimeError("LLM down")

    # Act
    resp = self.client.post("/select_attachments",
                            data={"selected_files": ["logs.zip"], "action": "download"})

    # Assert
    self.assert_redirects_to(resp, "/download_attachments")
    self.assertEqual(self.get_session()["selected_files"], [ZIP_ITEM])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `setUp` fixture | `_prime_issue_ai_cache` patched (`self.mock_prime`) | Class fixture |
| Session (`_seed_with_attachments`) | `case_context=create_mock_case_context(attachment_list=[ZIP_ITEM, TXT_ITEM])`, `gather_workflow_id="wf-test-1"` |  |
| HTTP | form `selected_files=["logs.zip"]`, `action="download"` |  |
| `gather_service.record_attachment_selection` | side effect `RuntimeError("telemetry down")` |  |
| `_prime_issue_ai_cache` | side effect `RuntimeError("LLM down")` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / `Location` | `302` → `/download_attachments` | `assert_redirects_to` |
| `session["selected_files"]` | `[ZIP_ITEM]` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Failures swallowed | Either failure causes a 500 |

**— Class `TestDownloadAttachments` —**

### 24. test_without_session_redirects_to_index

**Description:** Without a `case_context`, `/download_attachments` flashes a `warning` and redirects to `/`.

**Code:**
```python
def test_without_session_redirects_to_index(self):
    # Act
    resp = self.client.get("/download_attachments")

    # Assert
    self.assert_redirects_to(resp, "/")
    self.assertEqual(self.flashes(), [("warning", "Session expired. Please start again.")])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Session | *(empty)* | No `case_context` |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / `Location` | `302` → `/` | `assert_redirects_to` |
| Flashes | `[("warning", "Session expired. Please start again.")]` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Redirect | 500 on an empty session |

### 25. test_non_bsod_uses_case_download_dir

**Description:** For a non-BSOD case the download path is `case_context.case_download_dir`; the progress page gets a `{name: 0}` map of the selected files, and `session["download_path"]` is stored for the SocketIO download handler.

**Code:**
```python
@patch(f"{ROUTES}.render_template", return_value="progress-page")
def test_non_bsod_uses_case_download_dir(self, mock_render):
    # Arrange
    self.seed_session(bsod=False, selected_files=[ZIP_ITEM, TXT_ITEM])
    expected_path = self.create_mock_case_context()["case_download_dir"]

    # Act
    resp = self.client.get("/download_attachments")

    # Assert
    self.assertEqual(resp.status_code, 200)
    mock_render.assert_called_once_with(
        "attachment_download_progress.html",
        files_to_download={"logs.zip": 0, "notes.txt": 0},
        download_path=expected_path)
    self.assertEqual(self.get_session()["download_path"], expected_path)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Session | `case_context=create_mock_case_context()`, `bsod=False`, `selected_files=[ZIP_ITEM, TXT_ITEM]` |  |
| `render_template` | → `"progress-page"` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| `render_template` call | `("attachment_download_progress.html", files_to_download={"logs.zip": 0, "notes.txt": 0}, download_path=expected_path)` | `assert_called_once_with` |
| `session["download_path"]` | `expected_path` = mock context's `case_download_dir` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Local case dir used and stored | BSOD share used for a normal case, or path not stored in session |

### 26. test_bsod_uses_shared_load_path_and_case_nbr

**Description:** For a BSOD case the path is `<load share>\<case folder>`; with an empty `backend_id` the case number is used as the folder.

**Code:**
```python
@patch(f"{ROUTES}.helpers.get_load_path", return_value=r"\\share\BSOD_Dumps")
@patch(f"{ROUTES}.render_template", return_value="progress-page")
def test_bsod_uses_shared_load_path_and_case_nbr(self, mock_render, mock_load):
    # Arrange
    self.seed_session(bsod=True, selected_files=[ZIP_ITEM])

    # Act
    self.client.get("/download_attachments")

    # Assert
    self.assertEqual(mock_render.call_args.kwargs["download_path"], r"\\share\BSOD_Dumps\00123456")
    self.assertEqual(self.get_session()["download_path"], r"\\share\BSOD_Dumps\00123456")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Session | `case_context=create_mock_case_context()` (`backend_id=""`), `bsod=True`, `selected_files=[ZIP_ITEM]` |  |
| `helpers.get_load_path` | → `r"\\share\BSOD_Dumps"` | Patched share resolver |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `render_template` kwarg `download_path` | `r"\\share\BSOD_Dumps\00123456"` | `assertEqual` |
| `session["download_path"]` | same | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Share path + case number | Local dir used for BSOD, or wrong folder name |

### 27. test_resolve_download_path_prefers_dashed_backend_id

**Description:** Core logic of `_resolve_download_path`: when `backend_id` contains a dash it is used as the BSOD folder instead of the case number.

**Code:**
```python
@patch(f"{ROUTES}.helpers.get_load_path", return_value=r"\\share\BSOD_Dumps")
def test_resolve_download_path_prefers_dashed_backend_id(self, mock_load):
    # Arrange
    ctx = CaseContext(case_nbr=TEST_CASE_NBR, backend_id="BSOD-4242")

    # Act / Assert
    self.assertEqual(main_routes._resolve_download_path(ctx, True), r"\\share\BSOD_Dumps\BSOD-4242")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `ctx` | `CaseContext(case_nbr="00123456", backend_id="BSOD-4242")` |  |
| `helpers.get_load_path` | → `r"\\share\BSOD_Dumps"` |  |
| `is_bsod` | `True` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Return | `r"\\share\BSOD_Dumps\BSOD-4242"` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| backend_id folder chosen | Case number used despite a dashed backend id |

### 28. test_resolve_download_path_bsod_without_share_returns_empty

**Description:** `_resolve_download_path` returns `''` when the BSOD share is unreachable (`get_load_path` → `None`) and when the context itself is `None`.

**Code:**
```python
@patch(f"{ROUTES}.helpers.get_load_path", return_value=None)
def test_resolve_download_path_bsod_without_share_returns_empty(self, mock_load):
    # Act / Assert
    self.assertEqual(main_routes._resolve_download_path(CaseContext(case_nbr=TEST_CASE_NBR), True), "")
    self.assertEqual(main_routes._resolve_download_path(None, False), "")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `helpers.get_load_path` | → `None` |  |
| Calls | `(CaseContext(case_nbr="00123456"), True)` and `(None, False)` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Both returns | `""` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Empty string for both | Exception or a bogus path |

**— Class `TestDownloadResult` —**

### 29. test_without_session_redirects_to_index

**Description:** Without a `case_context`, `/download_result` redirects to `/` and never renders the template.

**Code:**
```python
def test_without_session_redirects_to_index(self):
    # Act
    resp = self.client.get("/download_result")

    # Assert
    self.assert_redirects_to(resp, "/")
    self.mock_render.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `setUp` fixture | patched in `ROUTES`: `render_template`→`"result-page"`, `get_issue_time_from_selected_files`→`{}`, `get_auto_analysis_etl`→`None`, `filter_folders_by_time`, `extract_timestamp_from_folder`→`None`, `load_fw_system_info`→`None`, `infer_fw_parse_type`→`"wifi"`; patched in `utils.issue_time_ai`: `align_issue_time_for_display` (identity), `anchor_time_only_to_folder_date` (identity), `pick_etl_by_ai_time`→`None`, `align_issue_datetime_to_customer_frame` | Class fixture |
| Session | *(empty)* | No `case_context` |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / `Location` | `302` → `/` | `assert_redirects_to` |
| `render_template` | not called | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| Redirect, no render | Template rendered with no case |

### 30. test_empty_download_path_redirects_to_index

**Description:** A BSOD session whose share is unreachable resolves to an empty download path; the result page flashes "Session expired" and redirects to `/`.

**Code:**
```python
@patch(f"{ROUTES}.helpers.get_load_path", return_value=None)
def test_empty_download_path_redirects_to_index(self, mock_load):
    # Arrange: bsod case with no reachable share -> download_path ''
    self.seed_session(bsod=True)

    # Act
    resp = self.client.get("/download_result")

    # Assert
    self.assert_redirects_to(resp, "/")
    self.assertEqual(self.flashes(), [("warning", "Session expired. Please start again.")])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `setUp` fixture | patched in `ROUTES`: `render_template`→`"result-page"`, `get_issue_time_from_selected_files`→`{}`, `get_auto_analysis_etl`→`None`, `filter_folders_by_time`, `extract_timestamp_from_folder`→`None`, `load_fw_system_info`→`None`, `infer_fw_parse_type`→`"wifi"`; patched in `utils.issue_time_ai`: `align_issue_time_for_display` (identity), `anchor_time_only_to_folder_date` (identity), `pick_etl_by_ai_time`→`None`, `align_issue_datetime_to_customer_frame` | Class fixture |
| Session | `case_context=create_mock_case_context()`, `bsod=True` |  |
| `helpers.get_load_path` | → `None` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / `Location` | `302` → `/` | `assert_redirects_to` |
| Flashes | `[("warning", "Session expired. Please start again.")]` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Redirect | Renders with an empty path |

### 31. test_wifi_only_renders_tables_without_auto_pick

**Description:** Baseline render of `download_result.html` for a Wi-Fi-only case with no issue time and no Run Analysis request. Verifies the results lookup by case number, all pick-related kwargs empty/None, `.log` files filtered out of the Wi-Fi table, and the raw dicts passed through.

**Code:**
```python
def test_wifi_only_renders_tables_without_auto_pick(self):
    # Arrange
    self.set_results(wifi={"logs.zip": [WIFI_ETL_1, WIFI_LOG]})
    self.seed_session(selected_files=[ZIP_ITEM])

    # Act
    resp = self.client.get("/download_result")

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.app_config.get_download_results.assert_called_once_with(TEST_CASE_NBR)
    kw = self.render_kwargs()
    self.assertEqual(kw["case_path"], self.create_mock_case_context()["case_download_dir"])
    self.assertEqual(kw["wifi_or_bt"], "wifi")
    self.assertIs(kw["files_coexist"], False)
    self.assertIsNone(kw["auto_analysis_etl"])
    self.assertIsNone(kw["auto_analysis_etl_reason"])
    self.assertIsNone(kw["auto_analysis_bt"])
    self.assertEqual(kw["llm_issue_time"], "")
    self.assertEqual(kw["llm_issue_times"], [])
    self.assertEqual(kw["ai_pre_selected_etl"], "")
    self.assertEqual(kw["time_filter_info"], [])
    self.assertEqual(kw["time_filter_warnings"], [])
    self.assertEqual(kw["exclude_keywords"], self.app_config.etl_exclude_keywords)
    self.assertEqual([r["etl_path"] for r in kw["wifi_table_rows"]], [WIFI_ETL_1])  # .log filtered
    self.assertEqual(kw["wifi_dict"], {"logs.zip": [WIFI_ETL_1, WIFI_LOG]})
    self.patches["get_auto_analysis_etl"].assert_called_once()
    self.patches["pick_etl_by_ai_time"].assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `setUp` fixture | patched in `ROUTES`: `render_template`→`"result-page"`, `get_issue_time_from_selected_files`→`{}`, `get_auto_analysis_etl`→`None`, `filter_folders_by_time`, `extract_timestamp_from_folder`→`None`, `load_fw_system_info`→`None`, `infer_fw_parse_type`→`"wifi"`; patched in `utils.issue_time_ai`: `align_issue_time_for_display` (identity), `anchor_time_only_to_folder_date` (identity), `pick_etl_by_ai_time`→`None`, `align_issue_datetime_to_customer_frame` | Class fixture |
| Session | `case_context=create_mock_case_context()`, `selected_files=[ZIP_ITEM]` |  |
| `app_config.get_download_results` | → `{"wifi": {"logs.zip": [WIFI_ETL_1, WIFI_LOG]}, "ddd": {}, "bt": {}, "fw": {}}` | `WIFI_LOG` ends in `setup.log` |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| `get_download_results` | called once with `"00123456"` | `assert_called_once_with` |
| kwargs | `case_path=<case_download_dir>`, `wifi_or_bt="wifi"`, `files_coexist=False`, `auto_analysis_etl=None`, `auto_analysis_etl_reason=None`, `auto_analysis_bt=None`, `llm_issue_time=""`, `llm_issue_times=[]`, `ai_pre_selected_etl=""`, `time_filter_info=[]`, `time_filter_warnings=[]`, `exclude_keywords=app_config.etl_exclude_keywords` | `assertEqual` / `assertIs` / `assertIsNone` |
| `wifi_table_rows` paths | `[WIFI_ETL_1]` | `assertEqual` |
| `wifi_dict` | `{"logs.zip": [WIFI_ETL_1, WIFI_LOG]}` | `assertEqual` |
| `get_auto_analysis_etl` / `pick_etl_by_ai_time` | called once / not called | `assert_called_once()` / `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| Template contract matches | Any kwarg renamed/removed, `.log` shown in the ETL table, or AI picker invoked without an issue time |

### 32. test_coex_bt_hint_skips_wifi_auto_pick

**Description:** Coex: when both Wi-Fi and BT items exist, `files_coexist` becomes `True` (and is persisted back into the session's case_context). With a BT hint, the Wi-Fi newest-by-number pick is skipped entirely. BT file sizes default to 0 when the file isn't on disk (`os.path.isfile` patched to `False`).

**Code:**
```python
def test_coex_bt_hint_skips_wifi_auto_pick(self):
    # Arrange
    self.set_results(wifi={"logs.zip": [WIFI_ETL_1]}, bt={"logs.zip": [BT_NEW]})
    with self.client.session_transaction() as sess:
        sess["case_context"] = self.create_mock_case_context(wifi_or_bt="bt")

    # Act
    with patch(f"{ROUTES}.os.path.isfile", return_value=False):
        self.client.get("/download_result")

    # Assert
    kw = self.render_kwargs()
    self.assertIs(kw["files_coexist"], True)
    self.assertIsNone(kw["auto_analysis_etl"])
    self.patches["get_auto_analysis_etl"].assert_not_called()
    self.assertIs(self.get_session()["case_context"]["files_coexist"], True)
    self.assertEqual(kw["bt_file_sizes"], {BT_NEW: 0})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `setUp` fixture | patched in `ROUTES`: `render_template`→`"result-page"`, `get_issue_time_from_selected_files`→`{}`, `get_auto_analysis_etl`→`None`, `filter_folders_by_time`, `extract_timestamp_from_folder`→`None`, `load_fw_system_info`→`None`, `infer_fw_parse_type`→`"wifi"`; patched in `utils.issue_time_ai`: `align_issue_time_for_display` (identity), `anchor_time_only_to_folder_date` (identity), `pick_etl_by_ai_time`→`None`, `align_issue_datetime_to_customer_frame` | Class fixture |
| Session | `case_context=create_mock_case_context(wifi_or_bt="bt")` |  |
| `get_download_results` | → wifi `{"logs.zip": [WIFI_ETL_1]}`, bt `{"logs.zip": [BT_NEW]}` |  |
| `os.path.isfile` | → `False` | No real FS |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| kwargs | `files_coexist is True`, `auto_analysis_etl is None`, `bt_file_sizes={BT_NEW: 0}` | `assertIs` / `assertIsNone` / `assertEqual` |
| `get_auto_analysis_etl` | not called | `assert_not_called()` |
| `session["case_context"]["files_coexist"]` | `True` | `assertIs` |

| Pass Condition | Fail Condition |
|---|---|
| Coex detected, Wi-Fi pick skipped | Wi-Fi ETL auto-launched for a BT coex case |

### 33. test_run_analysis_recovery_picks_highest_numbered_etl

**Description:** When Run Analysis was requested (`_run_analysis_requested`) but the upstream pick returns `None`, the recovery path picks the highest-numbered `.etl` itself (via the real `extract_file_number`), tags it `latest_by_number`, and pops the one-shot marker from the session.

**Code:**
```python
def test_run_analysis_recovery_picks_highest_numbered_etl(self):
    # Arrange: upstream pick returns None but Run Analysis was requested
    self.set_results(wifi={"logs.zip": [WIFI_ETL_1, WIFI_ETL_3, WIFI_LOG]})
    self.seed_session(_run_analysis_requested=True)

    # Act
    self.client.get("/download_result")

    # Assert
    kw = self.render_kwargs()
    self.assertEqual(kw["auto_analysis_etl"], WIFI_ETL_3)
    self.assertEqual(kw["auto_analysis_etl_reason"], "latest_by_number")
    self.assertNotIn("_run_analysis_requested", self.get_session())  # popped once
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `setUp` fixture | patched in `ROUTES`: `render_template`→`"result-page"`, `get_issue_time_from_selected_files`→`{}`, `get_auto_analysis_etl`→`None`, `filter_folders_by_time`, `extract_timestamp_from_folder`→`None`, `load_fw_system_info`→`None`, `infer_fw_parse_type`→`"wifi"`; patched in `utils.issue_time_ai`: `align_issue_time_for_display` (identity), `anchor_time_only_to_folder_date` (identity), `pick_etl_by_ai_time`→`None`, `align_issue_datetime_to_customer_frame` | Class fixture |
| Session | `case_context=create_mock_case_context()`, `_run_analysis_requested=True` |  |
| `get_download_results` | → wifi `{"logs.zip": [WIFI_ETL_1, WIFI_ETL_3, WIFI_LOG]}` |  |
| `get_auto_analysis_etl` | → `None` | Upstream picked nothing |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `auto_analysis_etl` | `WIFI_ETL_3` (`…\WiFi_3.etl`) | `assertEqual` |
| `auto_analysis_etl_reason` | `"latest_by_number"` | `assertEqual` |
| Session | `_run_analysis_requested` removed | `assertNotIn` |

| Pass Condition | Fail Condition |
|---|---|
| Recovery pick + marker popped | No auto-launch after a requested Run Analysis, or marker not consumed |

### 34. test_ai_issue_time_overrides_newest_by_number_pick

**Description:** With an AI issue time cached by the pre-pass, `pick_etl_by_ai_time` overrides the newest-by-number pick; the reason becomes `ai_time` and the same path is used as the download pre-selection.

**Code:**
```python
def test_ai_issue_time_overrides_newest_by_number_pick(self):
    # Arrange
    self.set_results(wifi={"logs.zip": [WIFI_ETL_1, WIFI_ETL_3]})
    self.patches["get_auto_analysis_etl"].return_value = WIFI_ETL_3
    self.patches["pick_etl_by_ai_time"].return_value = WIFI_ETL_1
    self.seed_session(_issue_ai_quick={"data": {"issue_times": ["04/14/2026-01:26:00"]}})

    # Act
    self.client.get("/download_result")

    # Assert
    kw = self.render_kwargs()
    self.assertEqual(kw["llm_issue_time"], "04/14/2026-01:26:00")
    self.assertEqual(kw["llm_issue_times"], ["04/14/2026-01:26:00"])
    self.assertEqual(kw["auto_analysis_etl"], WIFI_ETL_1)
    self.assertEqual(kw["auto_analysis_etl_reason"], "ai_time")
    self.assertEqual(kw["ai_pre_selected_etl"], WIFI_ETL_1)
    self.patches["align_issue_time_for_display"].assert_called_once()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `setUp` fixture | patched in `ROUTES`: `render_template`→`"result-page"`, `get_issue_time_from_selected_files`→`{}`, `get_auto_analysis_etl`→`None`, `filter_folders_by_time`, `extract_timestamp_from_folder`→`None`, `load_fw_system_info`→`None`, `infer_fw_parse_type`→`"wifi"`; patched in `utils.issue_time_ai`: `align_issue_time_for_display` (identity), `anchor_time_only_to_folder_date` (identity), `pick_etl_by_ai_time`→`None`, `align_issue_datetime_to_customer_frame` | Class fixture |
| Session | `case_context=create_mock_case_context()`, `_issue_ai_quick={"data": {"issue_times": ["04/14/2026-01:26:00"]}}` |  |
| `get_download_results` | → wifi `{"logs.zip": [WIFI_ETL_1, WIFI_ETL_3]}` |  |
| `get_auto_analysis_etl` | → `WIFI_ETL_3` | Newest by number |
| `pick_etl_by_ai_time` | → `WIFI_ETL_1` | AI-time pick |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| kwargs | `llm_issue_time="04/14/2026-01:26:00"`, `llm_issue_times=["04/14/2026-01:26:00"]`, `auto_analysis_etl=WIFI_ETL_1`, `auto_analysis_etl_reason="ai_time"`, `ai_pre_selected_etl=WIFI_ETL_1` | `assertEqual` |
| `align_issue_time_for_display` | called once | `assert_called_once()` |

| Pass Condition | Fail Condition |
|---|---|
| AI pick wins | Newest-by-number kept despite the AI pick, or reason not updated |

### 35. test_attachment_time_filter_success_and_llm_time_fallback

**Description:** An issue time parsed from the attachment subtitle drives `filter_folders_by_time` for that ZIP; with no warnings it is listed in `time_filter_info`. Because the AI cache is empty, the same time becomes the `llm_issue_time` fallback.

**Code:**
```python
def test_attachment_time_filter_success_and_llm_time_fallback(self):
    # Arrange: issue time parsed from the attachment subtitle
    self.set_results(wifi={"logs.zip": [WIFI_ETL_1, WIFI_ETL_3]})
    self.patches["get_issue_time_from_selected_files"].return_value = {"logs.zip": "14:15:18"}
    self.patches["filter_folders_by_time"].side_effect = lambda d, t: (d, {})
    self.seed_session(selected_files=[ZIP_ITEM])

    # Act
    self.client.get("/download_result")

    # Assert
    kw = self.render_kwargs()
    self.assertEqual(kw["time_filter_info"], [("logs.zip", "14:15:18")])
    self.assertEqual(kw["time_filter_warnings"], [])
    self.assertEqual(kw["llm_issue_time"], "14:15:18")  # fallback from time_mapping
    self.assertEqual(kw["llm_issue_times"], ["14:15:18"])
    self.patches["filter_folders_by_time"].assert_called_once_with(
        {"logs.zip": [WIFI_ETL_1, WIFI_ETL_3]}, "14:15:18")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `setUp` fixture | patched in `ROUTES`: `render_template`→`"result-page"`, `get_issue_time_from_selected_files`→`{}`, `get_auto_analysis_etl`→`None`, `filter_folders_by_time`, `extract_timestamp_from_folder`→`None`, `load_fw_system_info`→`None`, `infer_fw_parse_type`→`"wifi"`; patched in `utils.issue_time_ai`: `align_issue_time_for_display` (identity), `anchor_time_only_to_folder_date` (identity), `pick_etl_by_ai_time`→`None`, `align_issue_datetime_to_customer_frame` | Class fixture |
| Session | `case_context=create_mock_case_context()`, `selected_files=[ZIP_ITEM]` |  |
| `get_download_results` | → wifi `{"logs.zip": [WIFI_ETL_1, WIFI_ETL_3]}` |  |
| `get_issue_time_from_selected_files` | → `{"logs.zip": "14:15:18"}` |  |
| `filter_folders_by_time` | side effect `lambda d, t: (d, {})` | No warnings |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `time_filter_info` / `time_filter_warnings` | `[("logs.zip", "14:15:18")]` / `[]` | `assertEqual` |
| `llm_issue_time` / `llm_issue_times` | `"14:15:18"` / `["14:15:18"]` | `assertEqual` |
| `filter_folders_by_time` | called once with `({"logs.zip": [WIFI_ETL_1, WIFI_ETL_3]}, "14:15:18")` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Filter applied once; fallback time used | Filter applied to ZIPs/dicts without a time, or fallback removed |

### 36. test_attachment_time_filter_warning_is_reported

**Description:** When the filter returns a warning for a ZIP, it is surfaced in `time_filter_warnings` and that ZIP is *not* listed as successfully filtered.

**Code:**
```python
def test_attachment_time_filter_warning_is_reported(self):
    # Arrange
    self.set_results(wifi={"logs.zip": [WIFI_ETL_1]})
    self.patches["get_issue_time_from_selected_files"].return_value = {"logs.zip": "14:15:18"}
    self.patches["filter_folders_by_time"].side_effect = lambda d, t: (
        d, {"logs.zip": "All folders are before issue time"})
    self.seed_session(selected_files=[ZIP_ITEM])

    # Act
    self.client.get("/download_result")

    # Assert
    kw = self.render_kwargs()
    self.assertEqual(kw["time_filter_info"], [])
    self.assertEqual(kw["time_filter_warnings"], [("logs.zip", "All folders are before issue time")])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `setUp` fixture | patched in `ROUTES`: `render_template`→`"result-page"`, `get_issue_time_from_selected_files`→`{}`, `get_auto_analysis_etl`→`None`, `filter_folders_by_time`, `extract_timestamp_from_folder`→`None`, `load_fw_system_info`→`None`, `infer_fw_parse_type`→`"wifi"`; patched in `utils.issue_time_ai`: `align_issue_time_for_display` (identity), `anchor_time_only_to_folder_date` (identity), `pick_etl_by_ai_time`→`None`, `align_issue_datetime_to_customer_frame` | Class fixture |
| Session | `case_context=create_mock_case_context()`, `selected_files=[ZIP_ITEM]` |  |
| `get_download_results` | → wifi `{"logs.zip": [WIFI_ETL_1]}` |  |
| `get_issue_time_from_selected_files` | → `{"logs.zip": "14:15:18"}` |  |
| `filter_folders_by_time` | → `(d, {"logs.zip": "All folders are before issue time"})` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `time_filter_info` | `[]` | `assertEqual` |
| `time_filter_warnings` | `[("logs.zip", "All folders are before issue time")]` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Warning surfaced | Warning dropped, or ZIP also reported as success |

### 37. test_bt_run_analysis_picks_newest_timestamped_bt_log

**Description:** For a BT case with Run Analysis pending, the BT path with the newest folder timestamp (via `log_parser_routes._parse_path_timestamp`, patched) is auto-selected. Also verifies `auto_analysis_fw` is popped from the session and passed through, and the BT table rows keep both paths.

**Code:**
```python
def test_bt_run_analysis_picks_newest_timestamped_bt_log(self):
    # Arrange
    self.set_results(bt={"logs.zip": [BT_OLD, BT_NEW]})
    with self.client.session_transaction() as sess:
        sess["case_context"] = self.create_mock_case_context(wifi_or_bt="bt")
        sess["_run_analysis_requested"] = True
        sess["auto_analysis_fw"] = r"C:\fake\fw\fw.bin"
    stamps = {BT_OLD: datetime(2026, 4, 13, 9, 0), BT_NEW: datetime(2026, 4, 14, 9, 0)}

    # Act
    with patch("blueprints.log_parser.log_parser_routes._parse_path_timestamp",
               side_effect=stamps.get), \
         patch(f"{ROUTES}.os.path.isfile", return_value=False):
        self.client.get("/download_result")

    # Assert
    kw = self.render_kwargs()
    self.assertEqual(kw["auto_analysis_bt"], BT_NEW)
    self.assertEqual(kw["auto_analysis_fw"], r"C:\fake\fw\fw.bin")
    self.assertNotIn("auto_analysis_fw", self.get_session())
    self.assertEqual([r["bt_path"] for r in kw["bt_table_rows"]], [BT_OLD, BT_NEW])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `setUp` fixture | patched in `ROUTES`: `render_template`→`"result-page"`, `get_issue_time_from_selected_files`→`{}`, `get_auto_analysis_etl`→`None`, `filter_folders_by_time`, `extract_timestamp_from_folder`→`None`, `load_fw_system_info`→`None`, `infer_fw_parse_type`→`"wifi"`; patched in `utils.issue_time_ai`: `align_issue_time_for_display` (identity), `anchor_time_only_to_folder_date` (identity), `pick_etl_by_ai_time`→`None`, `align_issue_datetime_to_customer_frame` | Class fixture |
| Session | `case_context=create_mock_case_context(wifi_or_bt="bt")`, `_run_analysis_requested=True`, `auto_analysis_fw=r"C:\fake\fw\fw.bin"` |  |
| `get_download_results` | → bt `{"logs.zip": [BT_OLD, BT_NEW]}` |  |
| `_parse_path_timestamp` | `BT_OLD`→`datetime(2026, 4, 13, 9, 0)`, `BT_NEW`→`datetime(2026, 4, 14, 9, 0)` | Patched in `blueprints.log_parser.log_parser_routes` |
| `os.path.isfile` | → `False` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `auto_analysis_bt` | `BT_NEW` | `assertEqual` |
| `auto_analysis_fw` | `r"C:\fake\fw\fw.bin"` | `assertEqual` |
| Session | `auto_analysis_fw` removed | `assertNotIn` |
| `bt_table_rows` paths | `[BT_OLD, BT_NEW]` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Newest BT log chosen; fw value consumed | Older log chosen, or fw value not popped |

**— Class `TestBuildTableRows` —**

### 38. test_rows_carry_zip_rowspan_and_filters

**Description:** Core logic of the merged result tables. `_build_wifi_table_rows` drops `.log` entries, skips ZIPs that end up empty, and emits rowspan metadata so the template merges the ZIP/folder cells (first row shows the cells, later rows don't). No `download_path` → `folder_name=""`.

**Code:**
```python
def test_rows_carry_zip_rowspan_and_filters(self):
    # Act
    rows = main_routes._build_wifi_table_rows(
        {"a.zip": [WIFI_ETL_1, WIFI_LOG, WIFI_ETL_3], "empty.zip": [WIFI_LOG]})

    # Assert
    self.assertEqual(rows, [
        {"zip_name": "a.zip", "folder_name": "", "zip_rowspan": 2, "folder_rowspan": 2,
         "show_zip_cell": True, "show_folder_cell": True, "etl_path": WIFI_ETL_1},
        {"zip_name": "a.zip", "folder_name": "", "zip_rowspan": 2, "folder_rowspan": 2,
         "show_zip_cell": False, "show_folder_cell": False, "etl_path": WIFI_ETL_3},
    ])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `wifi_dict` | `{"a.zip": [WIFI_ETL_1, WIFI_LOG, WIFI_ETL_3], "empty.zip": [WIFI_LOG]}` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Rows | two rows for `a.zip` (`zip_rowspan=2`, `folder_rowspan=2`; `show_zip_cell`/`show_folder_cell` True then False) with `etl_path` `WIFI_ETL_1`, `WIFI_ETL_3`; nothing for `empty.zip` | `assertEqual` (full list) |

| Pass Condition | Fail Condition |
|---|---|
| Exact row dicts | Row shape/keys change or filters regress |

### 39. test_event_rows_keep_only_system_event_logs

**Description:** `_build_event_table_rows` keeps only `System.evtx` / `RawEventViewerSystemLogs.evt` files from the DDD dict.

**Code:**
```python
def test_event_rows_keep_only_system_event_logs(self):
    # Act
    rows = main_routes._build_event_table_rows({"a.zip": [EVT_PATH, r"C:\fake\x.etl"]})

    # Assert
    self.assertEqual([r["ddd_path"] for r in rows], [EVT_PATH])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `ddd_dict` | `{"a.zip": [EVT_PATH, r"C:\fake\x.etl"]}` | `EVT_PATH=r"C:\fake\events\System.evtx"` |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Row `ddd_path`s | `[EVT_PATH]` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Only the evtx row | ETLs leak into the event table |

**— Class `TestPrimeIssueAiCache` —**

### 40. test_description_is_organized_and_usage_recorded

**Description:** Core logic of `_prime_issue_ai_cache` (called directly inside `app.test_request_context`). With a description, `organize_issue_context` is called with the helper's client/model, its data is cached at `session["_issue_ai_quick"]`, and because an LLM call happened (`llm_calls=1`) the merged usage is recorded under feature `issue_time_prepass`.

**Code:**
```python
@patch(f"{ROUTES}.gather_service")
@patch(f"{ISSUE_TIME_AI}.organize_issue_context")
def test_description_is_organized_and_usage_recorded(self, mock_organize, mock_gather):
    # Arrange
    data = {"clean_description": "drops after resume", "issue_times": ["01:26"]}
    mock_organize.return_value = (data, {"llm_calls": 1, "total_tokens": 42})
    ctx = CaseContext(case_nbr=TEST_CASE_NBR, description="Drops at 01:26", wifi_or_bt="wifi")

    # Act
    with self.app.test_request_context("/"):
        from flask import session
        session["gather_workflow_id"] = WORKFLOW_ID
        main_routes._prime_issue_ai_cache(ctx)
        cached = session["_issue_ai_quick"]

    # Assert
    self.assertEqual(cached, {"data": data})
    mock_organize.assert_called_once_with(
        "Drops at 01:26", first_ts=None, last_ts=None,
        llm_client="llm-client", llm_model="gpt-test", return_usage=True)
    mock_gather.record_feature_usage.assert_called_once()
    kw = mock_gather.record_feature_usage.call_args.kwargs
    self.assertEqual(kw["feature_code"], "issue_time_prepass")
    self.assertEqual(kw["workflow_id"], WORKFLOW_ID)
    self.assertEqual(kw["usage"]["llm_calls"], 1)
    self.assertEqual(kw["usage"]["total_tokens"], 42)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `setUp` fixture | `llm_helper.client="llm-client"`, `llm_helper.model="gpt-test"` | Class fixture |
| `ctx` | `CaseContext(case_nbr="00123456", description="Drops at 01:26", wifi_or_bt="wifi")` |  |
| `session["gather_workflow_id"]` | `"wf-test-1"` |  |
| `organize_issue_context` | → `({"clean_description": "drops after resume", "issue_times": ["01:26"]}, {"llm_calls": 1, "total_tokens": 42})` | Patched in `utils.issue_time_ai` |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `session["_issue_ai_quick"]` | `{"data": data}` | `assertEqual` |
| `organize_issue_context` call | `("Drops at 01:26", first_ts=None, last_ts=None, llm_client="llm-client", llm_model="gpt-test", return_usage=True)` | `assert_called_once_with` |
| `record_feature_usage` kwargs | `feature_code="issue_time_prepass"`, `workflow_id="wf-test-1"`, `usage["llm_calls"]=1`, `usage["total_tokens"]=42` | `assert_called_once` + `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Cache populated, usage recorded | Cache key renamed, wrong LLM args, or usage not merged |

### 41. test_empty_description_falls_back_to_attachment_subtitles

**Description:** With a blank description, non-empty attachment subtitles (`item[2][1]`) are used as the text; malformed entries (fewer than 3 elements) are skipped. With `llm_calls=0` no usage is recorded.

**Code:**
```python
@patch(f"{ROUTES}.gather_service")
@patch(f"{ISSUE_TIME_AI}.organize_issue_context")
def test_empty_description_falls_back_to_attachment_subtitles(self, mock_organize, mock_gather):
    # Arrange
    mock_organize.return_value = ({"issue_times": []}, {"llm_calls": 0})
    ctx = CaseContext(case_nbr=TEST_CASE_NBR, description="  ",
                      attachment_list=[ZIP_ITEM, TXT_ITEM, ["short"]])

    # Act
    with self.app.test_request_context("/"):
        main_routes._prime_issue_ai_cache(ctx)

    # Assert
    self.assertEqual(mock_organize.call_args.args[0], "uploaded by Partner")
    mock_gather.record_feature_usage.assert_not_called()  # llm_calls == 0
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `setUp` fixture | `llm_helper.client="llm-client"`, `llm_helper.model="gpt-test"` | Class fixture |
| `ctx` | `description="  "`, `attachment_list=[ZIP_ITEM, TXT_ITEM, ["short"]]` | Only `ZIP_ITEM` has a subtitle |
| `organize_issue_context` | → `({"issue_times": []}, {"llm_calls": 0})` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Text passed to `organize_issue_context` | `"uploaded by Partner"` | `assertEqual` |
| `record_feature_usage` | not called | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| Subtitle fallback used | Fallback removed, or usage recorded with zero calls |

### 42. test_no_text_anywhere_bails_without_llm_call

**Description:** With no description and no usable subtitle, the function bails out: no LLM call, no cache entry, no usage record.

**Code:**
```python
@patch(f"{ROUTES}.gather_service")
@patch(f"{ISSUE_TIME_AI}.organize_issue_context")
def test_no_text_anywhere_bails_without_llm_call(self, mock_organize, mock_gather):
    # Arrange
    ctx = CaseContext(case_nbr=TEST_CASE_NBR, description="", attachment_list=[TXT_ITEM])

    # Act
    with self.app.test_request_context("/"):
        from flask import session
        main_routes._prime_issue_ai_cache(ctx)
        self.assertNotIn("_issue_ai_quick", session)

    # Assert
    mock_organize.assert_not_called()
    mock_gather.record_feature_usage.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `setUp` fixture | `llm_helper.client="llm-client"`, `llm_helper.model="gpt-test"` | Class fixture |
| `ctx` | `description=""`, `attachment_list=[TXT_ITEM]` (empty subtitle) |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `session` | no `_issue_ai_quick` | `assertNotIn` |
| `organize_issue_context` / `record_feature_usage` | not called | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| Early return | LLM called with empty text |

**— Class `TestDownloadResultBsod` —**

### 43. test_without_session_redirects_to_index

**Description:** Without a `case_context`, `/download_result_bsod` redirects to `/`.

**Code:**
```python
def test_without_session_redirects_to_index(self):
    # Act
    resp = self.client.get("/download_result_bsod")

    # Assert
    self.assert_redirects_to(resp, "/")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Session | *(empty)* | No `case_context` |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / `Location` | `302` → `/` | `assert_redirects_to` |

| Pass Condition | Fail Condition |
|---|---|
| Redirect | 500 on an empty session |

### 44. test_unreachable_share_redirects_to_index

**Description:** `/download_result_bsod` always resolves the BSOD share path; if the share is unreachable it flashes "Session expired" and redirects to `/`.

**Code:**
```python
@patch(f"{ROUTES}.helpers.get_load_path", return_value=None)
def test_unreachable_share_redirects_to_index(self, mock_load):
    # Arrange
    self.seed_session()

    # Act
    resp = self.client.get("/download_result_bsod")

    # Assert
    self.assert_redirects_to(resp, "/")
    self.assertEqual(self.flashes(), [("warning", "Session expired. Please start again.")])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Session | `case_context=create_mock_case_context()` |  |
| `helpers.get_load_path` | → `None` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / `Location` | `302` → `/` | `assert_redirects_to` |
| Flashes | `[("warning", "Session expired. Please start again.")]` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Redirect | Renders with an empty path |

### 45. test_renders_bsod_page

**Description:** Happy path: renders `bsod.html` with the case number, detected e-mail and the share-based case path (feeds the `bsod.bsod_submit` form).

**Code:**
```python
@patch(f"{ROUTES}.helpers.detect_user_email", return_value="tester@example.com")
@patch(f"{ROUTES}.helpers.get_load_path", return_value=r"\\share\BSOD_Dumps")
@patch(f"{ROUTES}.render_template", return_value="bsod-page")
def test_renders_bsod_page(self, mock_render, mock_load, mock_email):
    # Arrange
    self.seed_session()

    # Act
    resp = self.client.get("/download_result_bsod")

    # Assert
    self.assertEqual(resp.status_code, 200)
    mock_render.assert_called_once_with(
        "bsod.html", case_nbr=TEST_CASE_NBR, email="tester@example.com",
        case_path=r"\\share\BSOD_Dumps\00123456")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Session | `case_context=create_mock_case_context()` |  |
| `helpers.get_load_path` | → `r"\\share\BSOD_Dumps"` |  |
| `helpers.detect_user_email` | → `"tester@example.com"` |  |
| `render_template` | → `"bsod-page"` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| `render_template` call | `("bsod.html", case_nbr="00123456", email="tester@example.com", case_path=r"\\share\BSOD_Dumps\00123456")` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Exact template call | Kwarg renamed or wrong path |

**— Class `TestOpenPath` —**

### 46. test_existing_path_opens_explorer

**Description:** An existing path is opened with `subprocess.run(['explorer', path])` and the route returns 204 No Content.

**Code:**
```python
@patch(f"{ROUTES}.subprocess.run")
@patch(f"{ROUTES}.os.path.exists", return_value=True)
def test_existing_path_opens_explorer(self, mock_exists, mock_run):
    # Act
    resp = self.client.post("/open_path", json={"path": r"C:\fake\folder"})

    # Assert
    self.assertEqual(resp.status_code, 204)
    mock_exists.assert_called_once_with(r"C:\fake\folder")
    mock_run.assert_called_once_with(["explorer", r"C:\fake\folder"])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| HTTP | `POST /open_path` JSON `{"path": r"C:\fake\folder"}` |  |
| `os.path.exists` | → `True` |  |
| `subprocess.run` | patched | Nothing launches |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `204` | `assertEqual` |
| `os.path.exists` | called once with the path | `assert_called_once_with` |
| `subprocess.run` | `(["explorer", r"C:\fake\folder"])` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Explorer invoked once | Different command or status |

### 47. test_missing_or_empty_path_returns_400

**Description:** A non-existent path and a non-JSON body (→ empty path) both return 400 "Invalid path" and never launch explorer.

**Code:**
```python
@patch(f"{ROUTES}.subprocess.run")
@patch(f"{ROUTES}.os.path.exists", return_value=False)
def test_missing_or_empty_path_returns_400(self, mock_exists, mock_run):
    # Act
    resp_missing = self.client.post("/open_path", json={"path": r"C:\nope"})
    resp_empty = self.client.post("/open_path", data="not json")

    # Assert
    self.assertEqual(resp_missing.status_code, 400)
    self.assertEqual(resp_missing.get_data(as_text=True), "Invalid path")
    self.assertEqual(resp_empty.status_code, 400)
    mock_run.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Requests | JSON `{"path": r"C:\nope"}`; then `data="not json"` |  |
| `os.path.exists` | → `False` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / body (1st) | `400` / `"Invalid path"` | `assertEqual` |
| Status (2nd) | `400` | `assertEqual` |
| `subprocess.run` | not called | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| Both rejected | Explorer launched for an invalid path |

### 48. test_open_path_ignores_silent_mode

**Description:** **Branch-specific.** `app_config.silent_mode` gates only *automatic* GUI launches (analysis_etl, automation, log_parser). `/open_path` is an explicit user click and is not gated, so explorer opens even with `silent_mode=True`. Pins that contract so any future change is deliberate.

**Code:**
```python
@patch(f"{ROUTES}.subprocess.run")
@patch(f"{ROUTES}.os.path.exists", return_value=True)
def test_open_path_ignores_silent_mode(self, mock_exists, mock_run):
    # Branch-specific: silent_mode only gates AUTOMATIC GUI launches;
    # this explicit user action still opens explorer.
    # Arrange
    self.app_config.silent_mode = True

    # Act
    resp = self.client.post("/open_path", json={"path": r"C:\fake\folder"})

    # Assert
    self.assertEqual(resp.status_code, 204)
    mock_run.assert_called_once_with(["explorer", r"C:\fake\folder"])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `app_config.silent_mode` | `True` | Branch-specific flag |
| HTTP | JSON `{"path": r"C:\fake\folder"}` |  |
| `os.path.exists` | → `True` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `204` | `assertEqual` |
| `subprocess.run` | `(["explorer", r"C:\fake\folder"])` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Explorer still opens | A silent_mode guard is added to this route |

**— Class `TestDumpEventTxt` —**

### 49. test_import_error_returns_503

**Description:** If `services.event_log_service` can't be imported (optional Windows dependency missing), `/dump_event_txt` returns 503 with an explanatory error.

**Code:**
```python
def test_import_error_returns_503(self):
    # Arrange
    self.use_event_service(None)

    # Act
    resp = self.client.post("/dump_event_txt", json={"path": EVT_PATH})

    # Assert
    self.assertEqual(resp.status_code, 503)
    self.assertIn("Event log feature unavailable", resp.get_json()["error"])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `use_event_service(svc)` | Puts `svc` into `sys.modules["services.event_log_service"]` and onto the `services` package (or removes it so the lazy import raises `ImportError` when `svc` is `None`); restored in cleanup | Lazy-import control |
| Service | `None` → `ImportError` |  |
| HTTP | `POST /dump_event_txt` JSON `{"path": EVT_PATH}` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `503` | `assertEqual` |
| JSON `error` | contains `"Event log feature unavailable"` | `assertIn` |

| Pass Condition | Fail Condition |
|---|---|
| 503 on missing module | ImportError escapes as 500 |

### 50. test_invalid_path_returns_400

**Description:** A non-existent path returns 400 `{"error": "Invalid path"}` and the service is not called.

**Code:**
```python
@patch(f"{ROUTES}.os.path.exists", return_value=False)
def test_invalid_path_returns_400(self, mock_exists):
    # Arrange
    svc = MagicMock()
    self.use_event_service(svc)

    # Act
    resp = self.client.post("/dump_event_txt", json={"path": EVT_PATH})

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertEqual(resp.get_json(), {"error": "Invalid path"})
    svc.dump_event_to_txt.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `use_event_service(svc)` | Puts `svc` into `sys.modules["services.event_log_service"]` and onto the `services` package (or removes it so the lazy import raises `ImportError` when `svc` is `None`); restored in cleanup | Lazy-import control |
| Service | `MagicMock()` |  |
| `os.path.exists` | → `False` |  |
| HTTP | JSON `{"path": EVT_PATH}` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `400` / `{"error": "Invalid path"}` | `assertEqual` |
| `dump_event_to_txt` | not called | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| Rejected before service | Service called with an invalid path |

### 51. test_success_returns_txt_path

**Description:** `dump_event_txt` converts the event log via `event_log_service.dump_event_to_txt(path)` and returns the generated `txt_path`.

**Code:**
```python
@patch(f"{ROUTES}.os.path.exists", return_value=True)
def test_success_returns_txt_path(self, mock_exists):
    # Arrange
    svc = MagicMock()
    svc.dump_event_to_txt.return_value = (r"C:\fake\events\System.txt", 12)
    self.use_event_service(svc)

    # Act
    resp = self.client.post("/dump_event_txt", json={"path": EVT_PATH})

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_json(), {"success": True, "txt_path": r"C:\fake\events\System.txt"})
    svc.dump_event_to_txt.assert_called_once_with(EVT_PATH)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `use_event_service(svc)` | Puts `svc` into `sys.modules["services.event_log_service"]` and onto the `services` package (or removes it so the lazy import raises `ImportError` when `svc` is `None`); restored in cleanup | Lazy-import control |
| `os.path.exists` | → `True` |  |
| `dump_event_to_txt` | → `(r"C:\fake\events\System.txt", 12)` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `200` / `{"success": True, "txt_path": r"C:\fake\events\System.txt"}` | `assertEqual` |
| `dump_event_to_txt` | called once with `EVT_PATH` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| JSON contract | Key renamed or service not called |

### 52. test_service_exception_returns_500

**Description:** A service error is returned as a JSON 500 carrying the exception text.

**Code:**
```python
@patch(f"{ROUTES}.os.path.exists", return_value=True)
def test_service_exception_returns_500(self, mock_exists):
    # Arrange
    svc = MagicMock()
    svc.dump_event_to_txt.side_effect = ValueError("corrupt evtx")
    self.use_event_service(svc)

    # Act
    resp = self.client.post("/dump_event_txt", json={"path": EVT_PATH})

    # Assert
    self.assertEqual(resp.status_code, 500)
    self.assertEqual(resp.get_json(), {"error": "corrupt evtx"})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `use_event_service(svc)` | Puts `svc` into `sys.modules["services.event_log_service"]` and onto the `services` package (or removes it so the lazy import raises `ImportError` when `svc` is `None`); restored in cleanup | Lazy-import control |
| `os.path.exists` | → `True` |  |
| `dump_event_to_txt` | side effect `ValueError("corrupt evtx")` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `500` / `{"error": "corrupt evtx"}` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Error JSON | Exception escapes |

**— Class `TestParseEventLog` —**

### 53. test_import_error_returns_503

**Description:** Same optional-dependency guard for `/parse_event_log`: an `ImportError` becomes a 503.

**Code:**
```python
def test_import_error_returns_503(self):
    # Arrange
    self.use_event_service(None)

    # Act
    resp = self.client.post("/parse_event_log", json={"path": EVT_PATH})

    # Assert
    self.assertEqual(resp.status_code, 503)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `use_event_service(svc)` | Puts `svc` into `sys.modules["services.event_log_service"]` and onto the `services` package (or removes it so the lazy import raises `ImportError` when `svc` is `None`); restored in cleanup | Lazy-import control |
| Service | `None` → `ImportError` |  |
| HTTP | `POST /parse_event_log` JSON `{"path": EVT_PATH}` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `503` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| 503 on missing module | ImportError escapes as 500 |

### 54. test_invalid_path_returns_400

**Description:** An empty path returns 400 `{"error": "Invalid path"}` and `get_paged_events` is not called.

**Code:**
```python
@patch(f"{ROUTES}.os.path.exists", return_value=False)
def test_invalid_path_returns_400(self, mock_exists):
    # Arrange
    svc = MagicMock()
    self.use_event_service(svc)

    # Act
    resp = self.client.post("/parse_event_log", json={"path": ""})

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertEqual(resp.get_json(), {"error": "Invalid path"})
    svc.get_paged_events.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `use_event_service(svc)` | Puts `svc` into `sys.modules["services.event_log_service"]` and onto the `services` package (or removes it so the lazy import raises `ImportError` when `svc` is `None`); restored in cleanup | Lazy-import control |
| Service | `MagicMock()` |  |
| `os.path.exists` | → `False` |  |
| HTTP | JSON `{"path": ""}` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `400` / `{"error": "Invalid path"}` | `assertEqual` |
| `get_paged_events` | not called | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| Rejected before service | Service called with an invalid path |

### 55. test_success_passes_clamped_paging_and_filters

**Description:** `parse_event_log` clamps a negative `offset` to 0 and forwards `limit`, `source_filter` and `level_filter` positionally to `event_log_service.get_paged_events`, returning its result as JSON.

**Code:**
```python
@patch(f"{ROUTES}.os.path.exists", return_value=True)
def test_success_passes_clamped_paging_and_filters(self, mock_exists):
    # Arrange
    svc = MagicMock()
    svc.get_paged_events.return_value = {"events": [{"id": 1}], "total": 1}
    self.use_event_service(svc)

    # Act
    resp = self.client.post("/parse_event_log", json={
        "path": EVT_PATH, "offset": -5, "limit": 50,
        "source_filter": "Netwtw", "level_filter": "error"})

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_json(), {"events": [{"id": 1}], "total": 1})
    svc.get_paged_events.assert_called_once_with(EVT_PATH, 0, 50, "Netwtw", "error")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `use_event_service(svc)` | Puts `svc` into `sys.modules["services.event_log_service"]` and onto the `services` package (or removes it so the lazy import raises `ImportError` when `svc` is `None`); restored in cleanup | Lazy-import control |
| `os.path.exists` | → `True` |  |
| HTTP JSON | `{"path": EVT_PATH, "offset": -5, "limit": 50, "source_filter": "Netwtw", "level_filter": "error"}` |  |
| `get_paged_events` | → `{"events": [{"id": 1}], "total": 1}` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `200` / `{"events": [{"id": 1}], "total": 1}` | `assertEqual` |
| `get_paged_events` | `(EVT_PATH, 0, 50, "Netwtw", "error")` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Clamped offset + forwarded filters | Negative offset passed through or argument order changes |

### 56. test_defaults_and_service_exception

**Description:** With only `path` given, the defaults are `offset=0, limit=0, source_filter="all", level_filter="all"`; a service exception becomes a JSON 500.

**Code:**
```python
@patch(f"{ROUTES}.os.path.exists", return_value=True)
def test_defaults_and_service_exception(self, mock_exists):
    # Arrange
    svc = MagicMock()
    svc.get_paged_events.side_effect = RuntimeError("parse failed")
    self.use_event_service(svc)

    # Act
    resp = self.client.post("/parse_event_log", json={"path": EVT_PATH})

    # Assert
    self.assertEqual(resp.status_code, 500)
    self.assertEqual(resp.get_json(), {"error": "parse failed"})
    svc.get_paged_events.assert_called_once_with(EVT_PATH, 0, 0, "all", "all")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `use_event_service(svc)` | Puts `svc` into `sys.modules["services.event_log_service"]` and onto the `services` package (or removes it so the lazy import raises `ImportError` when `svc` is `None`); restored in cleanup | Lazy-import control |
| `os.path.exists` | → `True` |  |
| HTTP JSON | `{"path": EVT_PATH}` |  |
| `get_paged_events` | side effect `RuntimeError("parse failed")` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `500` / `{"error": "parse failed"}` | `assertEqual` |
| `get_paged_events` | `(EVT_PATH, 0, 0, "all", "all")` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Defaults + error JSON | A default changes or the exception escapes |

**— Class `TestBtEventMap` —**

### 57. test_returns_json_map

**Description:** `/api/bt_event_map` reads `configs/bt_event_id_map.json` (resolved relative to the blueprint) and returns it as JSON. `open` is patched with `mock_open` so the real file isn't read.

**Code:**
```python
@patch(f"{ROUTES}.open", new_callable=mock_open, read_data=json.dumps({"0x01": "Connect"}), create=True)
@patch(f"{ROUTES}.os.path.exists", return_value=True)
def test_returns_json_map(self, mock_exists, mock_file):
    # Act
    resp = self.client.get("/api/bt_event_map")

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_json(), {"0x01": "Connect"})
    opened_path = mock_file.call_args.args[0]
    self.assertTrue(opened_path.endswith("bt_event_id_map.json"))
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `os.path.exists` | → `True` |  |
| `open` | `mock_open(read_data='{"0x01": "Connect"}')` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `200` / `{"0x01": "Connect"}` | `assertEqual` |
| Opened path | ends with `bt_event_id_map.json` | `assertTrue(endswith)` |

| Pass Condition | Fail Condition |
|---|---|
| Map returned | Path or response shape changes |

### 58. test_missing_file_returns_empty_object

**Description:** If the JSON file is missing the route returns `{}` (200) without opening anything.

**Code:**
```python
@patch(f"{ROUTES}.open", create=True)
@patch(f"{ROUTES}.os.path.exists", return_value=False)
def test_missing_file_returns_empty_object(self, mock_exists, mock_file):
    # Act
    resp = self.client.get("/api/bt_event_map")

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_json(), {})
    mock_file.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `os.path.exists` | → `False` |  |
| `open` | patched MagicMock |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `200` / `{}` | `assertEqual` |
| `open` | not called | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| Empty object | Error status, or file opened |

### 59. test_malformed_json_returns_empty_object

**Description:** Malformed JSON is caught and `{}` is returned with 200, so the BT UI degrades gracefully.

**Code:**
```python
@patch(f"{ROUTES}.open", new_callable=mock_open, read_data="{not json", create=True)
@patch(f"{ROUTES}.os.path.exists", return_value=True)
def test_malformed_json_returns_empty_object(self, mock_exists, mock_file):
    # Act
    resp = self.client.get("/api/bt_event_map")

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_json(), {})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `os.path.exists` | → `True` |  |
| `open` | `mock_open(read_data="{not json")` |  |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `200` / `{}` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Empty object | 500 on bad JSON |

## Test Execution Flow

```
setUpClass (BaseTestCase)
  ├─ capture real app_config
  ├─ patch configs.global_configs.app_config → MagicMock
  └─ repoint every module whose app_config IS the real object (incl. blueprints.main.main_routes)
        │
        ▼
for each test method:
  setUp (BaseTestCase)
    ├─ reset mock app_config (silent_mode=False, llm_helper/socketio/key/driver_manager mocks,
    │   etl_exclude_keywords, fake project/avatarfiles/driver paths)
    ├─ patch CaseContext.to_session → to_dict (no sidecar file writes)
    ├─ fresh Flask app (TESTING, SECRET_KEY, WTF_CSRF_ENABLED=False)
    ├─ register main_bp      (no stub needed — redirects target main_bp itself)
    └─ test client + push app context
  class-level setUp extras
    ├─ TestStartLatestEtlLlm : classify_issue return value, patch _prime_issue_ai_cache
    ├─ TestSelectAttachments : patch _prime_issue_ai_cache
    ├─ TestDownloadResult    : patch render_template + etl/fw utils in ROUTES,
    │                          patch utils.issue_time_ai pickers/aligners
    ├─ TestPrimeIssueAiCache : llm_helper.client / .model
    └─ EventLogTestBase      : use_event_service() swaps services.event_log_service per test
        │
        ▼
  test body: @patch at point of use (ROUTES.*, utils.issue_time_ai.*)
             seed session (seed_session / session_transaction)
             Arrange → Act (client.get/post, or direct helper call in test_request_context)
             → Assert (status, Location, flashes, JSON, render_template kwargs, mock calls)
        │
        ▼
  tearDown: pop app context, cleanups stop per-test patches ──► PASS / FAIL
        │
        ▼
tearDownClass: stop all app_config patchers (real singleton restored)
```

## Coverage Summary

| Metric | Value |
|---|---|
| Total Routes | 10 URL rules (12 method handlers: `/` and `/select_attachments` serve GET + POST) |
| Routes Tested | 10 / 10 (12 / 12 handlers) |
| Total Tests | 59 (1 structure + 48 route + 10 core-logic) |
| Actual Coverage (%) | 100% of routes and handlers; every success + validation/error branch of every handler except the 7 `download_result` sub-branches listed below |
| Out-of-scope / not-tested items | 7 (all inside `render_download_result_form`: datetime issue-time alignment, time-filter exception reset, AI-pick-equals-newest retag, non-identity chip anchoring, BT `.etl.split` expansion, latest-evt / FW system-info values, first-folder grouping) |

## What Would Make These Tests Fail?

| Source change | ✅ PASS | ❌ FAIL |
|---|---|---|
| Rename/remove any route or change `url_prefix` | — | `test_blueprint_registered_with_url_prefix` (+ the route's own tests: 404/405) |
| Stop stripping spaces from the case number | other index tests | `test_post_valid_case_populates_session_and_redirects` |
| Remove `session.clear()` before repopulating | other tests | `test_post_valid_case_populates_session_and_redirects`, `test_success_sets_session_classifies_and_returns_redirect` |
| Rename a session key (`prompt_file_path`, `latest_etl_llm`, `_run_analysis_requested`, `download_path`, …) | unrelated tests | the session-assertion tests for that key (index POST, start_latest_etl_llm success, select_attachments POST, download_attachments) |
| Let a classification/pre-pass/telemetry exception propagate | happy paths | `test_classification_and_prepass_failures_are_best_effort`, `test_post_telemetry_and_prepass_failures_are_best_effort` |
| Drop the legacy `latest_etl_llm` action alias | `test_post_analysis_action_sets_run_flags` | `test_post_legacy_latest_etl_llm_action_is_accepted` |
| Set `_run_analysis_requested` for BSOD submissions | other select tests | `test_post_bsod_action_sets_bsod_without_run_marker` |
| Use the case number even when `backend_id` contains `-` | other path tests | `test_resolve_download_path_prefers_dashed_backend_id` |
| Rename a `download_result.html` kwarg (e.g. `auto_analysis_etl_reason`) | redirect tests | `test_wifi_only_renders_tables_without_auto_pick` and the pick tests that read it |
| Run the Wi-Fi auto pick for coex + BT cases | Wi-Fi tests | `test_coex_bt_hint_skips_wifi_auto_pick` |
| Remove the Run-Analysis recovery pick | AI/BT tests | `test_run_analysis_recovery_picks_highest_numbered_etl` |
| Prefer newest-by-number over the AI-time pick | recovery test | `test_ai_issue_time_overrides_newest_by_number_pick` |
| Stop falling back to the attachment time for `llm_issue_time` | warning test | `test_attachment_time_filter_success_and_llm_time_fallback` |
| Report a warned ZIP as successfully filtered | success-filter test | `test_attachment_time_filter_warning_is_reported` |
| Pick the oldest BT log, or stop popping `auto_analysis_fw` | Wi-Fi tests | `test_bt_run_analysis_picks_newest_timestamped_bt_log` |
| Show `.log` files in the Wi-Fi ETL table | event-row test | `test_rows_carry_zip_rowspan_and_filters`, `test_wifi_only_renders_tables_without_auto_pick` |
| Change the `_issue_ai_quick` cache shape or the pre-pass feature code | bail-out test | `test_description_is_organized_and_usage_recorded` |
| Remove the attachment-subtitle fallback in `_prime_issue_ai_cache` | description test | `test_empty_description_falls_back_to_attachment_subtitles` |
| **Add a `silent_mode` guard to `/open_path`** | `test_existing_path_opens_explorer` | `test_open_path_ignores_silent_mode` |
| Launch explorer without checking `os.path.exists` | success test | `test_missing_or_empty_path_returns_400` |
| Make `services.event_log_service` a hard import (no 503) | path/success tests | both `test_import_error_returns_503` |
| Stop clamping a negative `offset` / change filter defaults | invalid-path tests | `test_success_passes_clamped_paging_and_filters`, `test_defaults_and_service_exception` |
| Return 500 on malformed `bt_event_id_map.json` | `test_returns_json_map` | `test_malformed_json_returns_empty_object` |
| Change the wireless-topic YAML field mapping (`fact` → other) | fallback tests | `test_merges_bt_and_wifi_topics_and_limits` |
