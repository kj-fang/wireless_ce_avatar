# Automation Blueprint — Analysis & Unit Test Documentation

- Route module: [blueprints/automation/automation_routes.py](../blueprints/automation/automation_routes.py)
- Test module: [unit_tests/test_automation_blueprint.py](../unit_tests/test_automation_blueprint.py)
- Shared fixture: [unit_tests/base_test.py](../unit_tests/base_test.py)

## Overview — What Automation Does

The `automation` blueprint (`url_prefix="/automation"`) implements the one-click "run latest ETL" flow. The HTTP route only renders `run_progress.html`; the page then emits the SocketIO event `trigger_run_latest_etl` (namespace `/progress`), handled by `handle_run_latest_etl()`. That handler picks the newest ZIP attachment of the current case, downloads it via the Selenium driver, decomposes it, filters files by case type (Wi-Fi keeps Wi-Fi+DDD, BT keeps BT) and drops `history` logs, chooses the latest ETL by address folder and `.etl.N` suffix, optionally reveals it in Explorer, and runs `WiFiAnalysisService().analyze(...)` or `BTAnalysisService().analyze(..., mode="AutoFile")`, streaming `stage` progress messages to the browser.

> **Branch-specific:** the `subprocess.run(['explorer', '/select,', latest_etl])` call in both the Wi-Fi and BT branches is gated by `if not app_config.silent_mode:`. Both values are tested for both branches.
>
> **Known bug (documented, not fixed):** when the ZIP contains DDD files, the DDD branch references undefined names `target_etl` (and later `sorted_etls`). The resulting `NameError` is caught by the outer `except` and emitted as `❌ Exception: name 'target_etl' is not defined`; no analysis runs. `test_ddd_files_hit_known_name_error_bug` pins this current behavior so a fix will show up as an intentional test update.

## All Routes (1 endpoint)

| Route | Method | Purpose |
|---|---|---|
| `/automation/run_latest_etl` | GET, POST | Render `run_progress.html` (the page that triggers the SocketIO workflow) |

SocketIO event registered by `register_socketio_handlers(socketio)`:

| Event | Namespace | Handler |
|---|---|---|
| `trigger_run_latest_etl` | `/progress` | `handle_run_latest_etl()` |

## High-Level Flow Diagram

```
GET|POST /automation/run_latest_etl ──► render_template("run_progress.html")

SocketIO trigger_run_latest_etl ──► handle_run_latest_etl()
  session["case_context"] ──missing──► KeyError (before try; nothing emitted)
  emit stage "Case Category: <subcategory>", stage "Attachment list: <case_nbr>"   (ns /progress)
  try:
    emit case_nbr {"name": case_nbr}                                                (ns /progress)
    ├─ no attachment_list          ──► emit stage "❌ No attachment found for this case!"  (NO namespace)
    ├─ no case_nbr / download dir  ──► emit stage "❌ Missing session info"
    ├─ pick_latest_zip_attachment → None ──► emit "❌ No zip file found."
    ├─ download_file → (None|path not on disk) ──► emit "❌ Failed to download <zip>"
    ▼
    process_single_zip(zip_path, dir, already) → wifi, ddd, evt, bt, fw
    'wifi' in wifi_or_bt ? bt=[] : wifi=ddd=[]
    filtered = [p for p in wifi+ddd+bt if 'history' not in p.lower()]
    ├─ empty ──► emit "❌ All files skipped due to 'history'"
    ├─ ddd present ──► uses undefined target_etl ──► NameError ──► except (KNOWN BUG)
    ▼
    latest = sorted(filtered, key=(extract_address_digits, extract_etl_suffix_number), reverse)[0]
    ├─ wifi: silent_mode False? → subprocess.run(['explorer','/select,',latest])
    │        WiFiAnalysisService().analyze(latest) ; emit all_done ; emit "📦 Case <id> processing complete ✅"
    └─ bt:   silent_mode False? → subprocess.run([...]) ; BTAnalysisService().analyze(latest, mode="AutoFile")
             emit all_done ; emit "📦 Case <id> processing complete ✅"
  except Exception e ──► emit stage "❌ Exception: <e>"
```

## Dependencies

```
flask:        Blueprint, render_template, session
stdlib:       os (os.path.exists, os.path.basename), subprocess.run, re
utils:        utils.etl_utils.extract_address_digits, extract_etl_suffix_number (pure, unpatched in tests)
              utils.etl_utils.pick_latest_zip_attachment
              utils.attachment_download.download_file (Selenium driver download)
              utils.attachment_decompose.process_single_zip
services:     WiFiAnalysisService, BTAnalysisService  (instantiated per run inside the handler)
app_config:   app_config.socketio (.emit), app_config.driver_manager, app_config.silent_mode
models:       models.models.CaseContext.from_session
session keys: case_context (required)
template:     run_progress.html
```

## Test Coverage Analysis

```
STRUCTURE
  ✅ blueprint registered, url_prefix, single GET+POST rule      ── test_blueprint_registered_with_url_prefix
  ✅ SocketIO 'trigger_run_latest_etl' bound on /progress        ── test_register_socketio_handlers_binds_trigger_event

ROUTES — /automation/run_latest_etl
  ✅ GET renders run_progress.html                               ── test_get_renders_progress_page
  ✅ POST renders run_progress.html                              ── test_post_renders_progress_page

CORE LOGIC — handle_run_latest_etl (SocketIO)
  ✅ Wi-Fi: highest .etl.N chosen, BT dropped, explorer + Wi-Fi analysis, emits ── test_wifi_case_picks_highest_suffix_and_runs_wifi_analysis
  ✅ Wi-Fi + silent_mode=True → no explorer                     ── test_wifi_case_silent_mode_skips_explorer
  ✅ BT: Wi-Fi dropped, explorer + BT analysis mode=AutoFile     ── test_bt_case_drops_wifi_files_and_runs_bt_analysis
  ✅ BT + silent_mode=True → no explorer                        ── test_bt_case_silent_mode_skips_explorer
  ✅ history files filtered out                                  ── test_history_files_are_filtered_out
  ✅ only history files → skip message                           ── test_only_history_files_emits_skip_message
  ✅ DDD present → NameError surfaced (KNOWN BUG)                ── test_ddd_files_hit_known_name_error_bug
  ✅ no attachments → error (namespace-less emit)                ── test_no_attachments_emits_error_and_returns
  ✅ no download dir → "Missing session info"                    ── test_missing_download_dir_emits_missing_session_info
  ✅ no ZIP attachment → "No zip file found."                    ── test_no_zip_attachment_emits_error
  ✅ download returns None → "Failed to download"                ── test_failed_download_emits_error
  ✅ downloaded file missing on disk → "Failed to download"      ── test_downloaded_path_missing_on_disk_emits_error
  ✅ unexpected exception → "❌ Exception: <msg>"                ── test_unexpected_exception_is_emitted
  ✅ missing case_context → KeyError, nothing emitted            ── test_missing_case_context_raises_key_error

  ⚠️ "❌ No suitable ETL found." (latest_etl None) ── NOT TESTED: unreachable — an empty filtered list returns earlier,
     so sorted_etls[0] always exists on the non-DDD path
  ⚠️ wifi_or_bt=None → TypeError ('in' on None) caught as "❌ Exception: …" ── NOT TESTED separately; same except
     path as test_unexpected_exception_is_emitted
  ⚪ DDD "prefer newest DDD" selection (extract_number) ── dead in practice because of the NameError bug above

BACKGROUND / SOCKETIO
  ⚪ socketio_run_latest_etl closure ── thin wrapper; registration verified, body delegates to the tested handler
  ⚪ real Selenium download / ZIP extraction / TextAnalysisTool ── utils/service layer, mocked
```



## What the Unit Tests Actually Test

`TestHandleRunLatestEtl` patches at class level (outermost first in the method signature): `pick_latest_zip_attachment` (returns `ZIP_ITEM = ["logs.zip", "https://ips.example/logs.zip", ["2025-01-01", ""]]`), `download_file` (returns `(ZIP_PATH, "logs.zip", False)` with `ZIP_PATH = r"C:\dl\00123456\logs.zip"`), `process_single_zip`, `os.path.exists` (`True`), `subprocess`, `WiFiAnalysisService`, `BTAnalysisService`. `_run(**ctx_overrides)` opens `app.test_request_context("/")`, stores `create_mock_case_context(attachment_list=[ZIP_ITEM], case_download_dir=r"C:\dl\00123456", **overrides)` in the session, calls `handle_run_latest_etl()` and returns `app_config.socketio.emit`. `_messages(emit)` lists the `message` of every `stage` emit. ETL paths are built under `ETL_DIR = C:\dl\00123456\logs_20250101`: `WIFI_ETL_1 = …\wifi.etl.1`, `WIFI_ETL_3 = …\wifi.etl.3`, `BT_ETL_2 = …\bt.etl.2`, `BT_ETL_5 = …\bt.etl.5`, `HISTORY_ETL = …\History_wifi.etl.9`, `DDD_ETL = …\ddd_trace_7.etl`.

### 1. test_blueprint_registered_with_url_prefix

*Class:* `TestAutomationBlueprintStructure`

**Description:** Checks the blueprint name, `url_prefix='/automation'`, that `/automation/run_latest_etl` is its only rule, and that it accepts both GET and POST.

**Code:**
```python
def test_blueprint_registered_with_url_prefix(self):
    # Arrange / Act
    rules = {r.rule: r.methods for r in self.app.url_map.iter_rules()
             if r.endpoint.startswith("automation.")}

    # Assert
    self.assertIs(self.app.blueprints.get("automation"), automation_bp)
    self.assertEqual(automation_bp.url_prefix, "/automation")
    self.assertEqual(set(rules), {"/automation/run_latest_etl"})
    self.assertTrue({"GET", "POST"}.issubset(rules["/automation/run_latest_etl"]))
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Registered blueprint | `automation_bp` | From `BaseTestCase.setUp` |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `app.blueprints['automation']` | `automation_bp` | `assertIs` |
| `url_prefix` | `'/automation'` | `assertEqual` |
| Rule set | `{'/automation/run_latest_etl'}` | `assertEqual` |
| Methods | ⊇ `{'GET','POST'}` | `assertTrue(issubset)` |

| Pass Condition | Fail Condition |
|---|---|
| Single GET+POST rule | Prefix/route/methods changed |

### 2. test_register_socketio_handlers_binds_trigger_event

*Class:* `TestAutomationBlueprintStructure`

**Description:** `register_socketio_handlers` must bind exactly one event, `trigger_run_latest_etl`, on namespace `/progress` — the name the progress page emits.

**Code:**
```python
def test_register_socketio_handlers_binds_trigger_event(self):
    # Arrange
    socketio = MagicMock()

    # Act
    register_socketio_handlers(socketio)

    # Assert
    socketio.on.assert_called_once_with("trigger_run_latest_etl", namespace="/progress")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `socketio` | `MagicMock()` | Records `.on(...)` |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `socketio.on` | `('trigger_run_latest_etl', namespace='/progress')` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Event bound correctly | Event or namespace renamed |

### 3. test_get_renders_progress_page

*Class:* `TestRunLatestEtlRoute`

**Description:** GET renders `run_progress.html` with no context variables.

**Code:**
```python
def test_get_renders_progress_page(self, mock_render):
    # Act
    resp = self.client.get("/automation/run_latest_etl")

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_data(as_text=True), "progress-page")
    mock_render.assert_called_once_with("run_progress.html")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Request | `GET /automation/run_latest_etl` | — |
| `render_template` | returns `'progress-page'` | Patched |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / body | `200` / `'progress-page'` | `assertEqual` |
| `render_template` | `('run_progress.html')` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Template rendered | Template renamed or kwargs added |

### 4. test_post_renders_progress_page

*Class:* `TestRunLatestEtlRoute`

**Description:** POST behaves identically to GET (the route declares both methods).

**Code:**
```python
def test_post_renders_progress_page(self, mock_render):
    # Act
    resp = self.client.post("/automation/run_latest_etl")

    # Assert
    self.assertEqual(resp.status_code, 200)
    mock_render.assert_called_once_with("run_progress.html")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Request | `POST /automation/run_latest_etl` | — |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| `render_template` | `('run_progress.html')` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| POST accepted | 405 on POST |

### 5. test_wifi_case_picks_highest_suffix_and_runs_wifi_analysis

*Class:* `TestHandleRunLatestEtl`

**Description:** Main Wi-Fi path. BT files are discarded for a Wi-Fi case; among `wifi.etl.1` and `wifi.etl.3` (same address folder) the higher suffix wins. Verifies the exact download/decompose calls, the Explorer `/select,` launch, the Wi-Fi analysis call, the first three progress emits (with `/progress` namespace), `all_done`, and the completion message using `case_context.id`.

**Code:**
```python
def test_wifi_case_picks_highest_suffix_and_runs_wifi_analysis(self, mock_pick, mock_dl, mock_zip, mock_exists, mock_sub, mock_wifi_cls, mock_bt_cls):
    # Arrange
    mock_zip.return_value = ([WIFI_ETL_1, WIFI_ETL_3], [], [], [BT_ETL_5], [])

    # Act
    emit = self._run()

    # Assert
    mock_pick.assert_called_once_with([ZIP_ITEM])
    mock_dl.assert_called_once_with("logs.zip", ZIP_ITEM[1], r"C:\dl\00123456",
                                    self.app_config.driver_manager, self.app_config.socketio)
    mock_zip.assert_called_once_with(ZIP_PATH, r"C:\dl\00123456", False)
    mock_sub.run.assert_called_once_with(["explorer", "/select,", WIFI_ETL_3])
    mock_wifi_cls.return_value.analyze.assert_called_once_with(WIFI_ETL_3)
    mock_bt_cls.assert_not_called()
    self.assertEqual(emit.call_args_list[:3], [
        call("stage", {"message": "Case Category: Wireless - WiFi"}, namespace="/progress"),
        call("stage", {"message": f"Attachment list: {TEST_CASE_NBR}"}, namespace="/progress"),
        call("case_nbr", {"name": TEST_CASE_NBR}, namespace="/progress"),
    ])
    emit.assert_any_call("all_done", {})
    self.assertIn("📦 Case a0B000000TESTID processing complete ✅", self._messages(emit))
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `process_single_zip` | returns `([WIFI_ETL_1, WIFI_ETL_3], [], [], [BT_ETL_5], [])` | Wi-Fi + BT files |
| Case context | `wifi_or_bt='wifi'`, `subcategory='Wireless - WiFi'`, `id='a0B000000TESTID'` | Defaults |
| `app_config.silent_mode` | `False` | Default |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `pick_latest_zip_attachment` | `([ZIP_ITEM])` | `assert_called_once_with` |
| `download_file` | `('logs.zip', ZIP_ITEM[1], r'C:\dl\00123456', app_config.driver_manager, app_config.socketio)` | `assert_called_once_with` |
| `process_single_zip` | `(ZIP_PATH, r'C:\dl\00123456', False)` | `assert_called_once_with` |
| `subprocess.run` | `['explorer', '/select,', WIFI_ETL_3]` | `assert_called_once_with` |
| `WiFiAnalysisService().analyze` | `(WIFI_ETL_3)` | `assert_called_once_with` |
| `BTAnalysisService` | not instantiated | `assert_not_called` |
| First 3 emits | stage `Case Category: Wireless - WiFi`, stage `Attachment list: 00123456`, `case_nbr {'name': '00123456'}` (all `namespace='/progress'`) | `assertEqual` |
| Emits | `('all_done', {})` and `📦 Case a0B000000TESTID processing complete ✅` | `assert_any_call` / `assertIn` |

| Pass Condition | Fail Condition |
|---|---|
| Latest Wi-Fi ETL analyzed and revealed | Wrong file chosen, BT not dropped, emits changed |

### 6. test_wifi_case_silent_mode_skips_explorer

*Class:* `TestHandleRunLatestEtl`

**Description:** **Branch-specific.** With `silent_mode=True` the Wi-Fi analysis still runs and `all_done` is emitted, but Explorer is not launched.

**Code:**
```python
def test_wifi_case_silent_mode_skips_explorer(self, mock_pick, mock_dl, mock_zip, mock_exists, mock_sub, mock_wifi_cls, mock_bt_cls):
    # Arrange (branch-specific: --silent-mode)
    self.app_config.silent_mode = True
    mock_zip.return_value = ([WIFI_ETL_1], [], [], [], [])

    # Act
    emit = self._run()

    # Assert
    mock_sub.run.assert_not_called()
    mock_wifi_cls.return_value.analyze.assert_called_once_with(WIFI_ETL_1)
    emit.assert_any_call("all_done", {})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `app_config.silent_mode` | `True` | Set by the test |
| `process_single_zip` | `([WIFI_ETL_1], [], [], [], [])` | One Wi-Fi ETL |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `subprocess.run` | not called | `assert_not_called` |
| `WiFiAnalysisService().analyze` | `(WIFI_ETL_1)` | `assert_called_once_with` |
| Emit | `('all_done', {})` | `assert_any_call` |

| Pass Condition | Fail Condition |
|---|---|
| Silent analysis | Explorer launched in silent mode |

### 7. test_bt_case_drops_wifi_files_and_runs_bt_analysis

*Class:* `TestHandleRunLatestEtl`

**Description:** BT case: Wi-Fi/DDD lists are cleared, the highest BT suffix (`bt.etl.5`) is chosen, revealed in Explorer and analyzed with `mode='AutoFile'`; the Wi-Fi service is never instantiated.

**Code:**
```python
def test_bt_case_drops_wifi_files_and_runs_bt_analysis(self, mock_pick, mock_dl, mock_zip, mock_exists, mock_sub, mock_wifi_cls, mock_bt_cls):
    # Arrange
    mock_zip.return_value = ([WIFI_ETL_3], [], [], [BT_ETL_2, BT_ETL_5], [])

    # Act
    emit = self._run(wifi_or_bt="bt")

    # Assert
    mock_sub.run.assert_called_once_with(["explorer", "/select,", BT_ETL_5])
    mock_bt_cls.return_value.analyze.assert_called_once_with(BT_ETL_5, mode="AutoFile")
    mock_wifi_cls.assert_not_called()
    emit.assert_any_call("all_done", {})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Case context | `wifi_or_bt='bt'` | Override |
| `process_single_zip` | `([WIFI_ETL_3], [], [], [BT_ETL_2, BT_ETL_5], [])` | Mixed files |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `subprocess.run` | `['explorer', '/select,', BT_ETL_5]` | `assert_called_once_with` |
| `BTAnalysisService().analyze` | `(BT_ETL_5, mode='AutoFile')` | `assert_called_once_with` |
| `WiFiAnalysisService` | not instantiated | `assert_not_called` |
| Emit | `('all_done', {})` | `assert_any_call` |

| Pass Condition | Fail Condition |
|---|---|
| BT ETL analyzed with AutoFile | Wi-Fi file chosen or wrong mode |

### 8. test_bt_case_silent_mode_skips_explorer

*Class:* `TestHandleRunLatestEtl`

**Description:** **Branch-specific.** BT branch with `silent_mode=True`: analysis runs, no Explorer.

**Code:**
```python
def test_bt_case_silent_mode_skips_explorer(self, mock_pick, mock_dl, mock_zip, mock_exists, mock_sub, mock_wifi_cls, mock_bt_cls):
    # Arrange (branch-specific: --silent-mode)
    self.app_config.silent_mode = True
    mock_zip.return_value = ([], [], [], [BT_ETL_2], [])

    # Act
    self._run(wifi_or_bt="bt")

    # Assert
    mock_sub.run.assert_not_called()
    mock_bt_cls.return_value.analyze.assert_called_once_with(BT_ETL_2, mode="AutoFile")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `app_config.silent_mode` | `True` | Set by the test |
| Case context | `wifi_or_bt='bt'` | Override |
| `process_single_zip` | `([], [], [], [BT_ETL_2], [])` | One BT ETL |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `subprocess.run` | not called | `assert_not_called` |
| `BTAnalysisService().analyze` | `(BT_ETL_2, mode='AutoFile')` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Silent BT analysis | Explorer launched in silent mode |

### 9. test_history_files_are_filtered_out

*Class:* `TestHandleRunLatestEtl`

**Description:** `History_wifi.etl.9` has the highest suffix but contains `history` (case-insensitive), so it is filtered and `wifi.etl.1` is analyzed.

**Code:**
```python
def test_history_files_are_filtered_out(self, mock_pick, mock_dl, mock_zip, mock_exists, mock_sub, mock_wifi_cls, mock_bt_cls):
    # Arrange: the highest-suffix file is a History log and must be skipped
    mock_zip.return_value = ([WIFI_ETL_1, HISTORY_ETL], [], [], [], [])

    # Act
    self._run()

    # Assert
    mock_wifi_cls.return_value.analyze.assert_called_once_with(WIFI_ETL_1)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `process_single_zip` | `([WIFI_ETL_1, HISTORY_ETL], [], [], [], [])` | History log present |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `WiFiAnalysisService().analyze` | `(WIFI_ETL_1)` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| History file skipped | History log analyzed |

### 10. test_only_history_files_emits_skip_message

*Class:* `TestHandleRunLatestEtl`

**Description:** If every candidate is a history log, the handler emits the skip message and stops without analysis or Explorer.

**Code:**
```python
def test_only_history_files_emits_skip_message(self, mock_pick, mock_dl, mock_zip, mock_exists, mock_sub, mock_wifi_cls, mock_bt_cls):
    # Arrange
    mock_zip.return_value = ([HISTORY_ETL], [], [], [], [])

    # Act
    emit = self._run()

    # Assert
    self.assertIn("❌ All files skipped due to 'history'", self._messages(emit))
    mock_wifi_cls.assert_not_called()
    mock_sub.run.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `process_single_zip` | `([HISTORY_ETL], [], [], [], [])` | Only history |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Stage messages | contain `❌ All files skipped due to 'history'` | `assertIn` |
| `WiFiAnalysisService` | not instantiated | `assert_not_called` |
| `subprocess.run` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| Skip message, no work | History log analyzed or IndexError |

### 11. test_ddd_files_hit_known_name_error_bug

*Class:* `TestHandleRunLatestEtl`

**Description:** **Known bug pinned.** With a DDD file present the DDD branch references the undefined `target_etl`; the `NameError` is caught and emitted as a `❌ Exception:` stage message mentioning `target_etl`. No analysis or Explorer launch happens. When the bug is fixed this test must be updated to the new expected behavior.

**Code:**
```python
def test_ddd_files_hit_known_name_error_bug(self, mock_pick, mock_dl, mock_zip, mock_exists, mock_sub, mock_wifi_cls, mock_bt_cls):
    # Arrange: KNOWN BUG - DDD branch references undefined `target_etl`
    mock_zip.return_value = ([WIFI_ETL_1], [DDD_ETL], [], [], [])

    # Act
    emit = self._run()

    # Assert: NameError is caught and surfaced; no analysis runs
    messages = self._messages(emit)
    self.assertTrue(any(m.startswith("❌ Exception:") and "target_etl" in m for m in messages), messages)
    mock_wifi_cls.assert_not_called()
    mock_sub.run.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `process_single_zip` | `([WIFI_ETL_1], [DDD_ETL], [], [], [])` | Wi-Fi + DDD |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Stage messages | one starts with `❌ Exception:` and contains `target_etl` | `assertTrue(any(...))` |
| `WiFiAnalysisService` | not instantiated | `assert_not_called` |
| `subprocess.run` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| Current (buggy) behavior reproduced | Bug fixed or behavior changed — update test + doc |

### 12. test_no_attachments_emits_error_and_returns

*Class:* `TestHandleRunLatestEtl`

**Description:** Empty `attachment_list` → emits the no-attachment message and returns before picking/downloading. The assertion matches the call **without** a `namespace` kwarg, documenting that this emit (unlike the first three) goes to the default namespace.

**Code:**
```python
def test_no_attachments_emits_error_and_returns(self, mock_pick, mock_dl, mock_zip, mock_exists, mock_sub, mock_wifi_cls, mock_bt_cls):
    # Act
    emit = self._run(attachment_list=[])

    # Assert: note this emit has no namespace (default "/"), unlike the first three
    emit.assert_any_call("stage", {"message": "❌ No attachment found for this case!"})
    mock_pick.assert_not_called()
    mock_dl.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Case context | `attachment_list=[]` | No attachments |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Emit | `('stage', {'message': '❌ No attachment found for this case!'})` (no namespace) | `assert_any_call` |
| `pick_latest_zip_attachment`, `download_file` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| Early return with message | Proceeds to download, or emit signature changes |

### 13. test_missing_download_dir_emits_missing_session_info

*Class:* `TestHandleRunLatestEtl`

**Description:** `case_download_dir=None` → `❌ Missing session info`; no ZIP is picked.

**Code:**
```python
def test_missing_download_dir_emits_missing_session_info(self, mock_pick, mock_dl, mock_zip, mock_exists, mock_sub, mock_wifi_cls, mock_bt_cls):
    # Act
    emit = self._run(case_download_dir=None)

    # Assert
    emit.assert_any_call("stage", {"message": "❌ Missing session info"})
    mock_pick.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Case context | `case_download_dir=None` | No download dir |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Emit | `('stage', {'message': '❌ Missing session info'})` | `assert_any_call` |
| `pick_latest_zip_attachment` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| Guard fires | Download attempted without a target dir |

### 14. test_no_zip_attachment_emits_error

*Class:* `TestHandleRunLatestEtl`

**Description:** `pick_latest_zip_attachment` returning `None` → `❌ No zip file found.`; nothing downloaded.

**Code:**
```python
def test_no_zip_attachment_emits_error(self, mock_pick, mock_dl, mock_zip, mock_exists, mock_sub, mock_wifi_cls, mock_bt_cls):
    # Arrange
    mock_pick.return_value = None

    # Act
    emit = self._run()

    # Assert
    emit.assert_any_call("stage", {"message": "❌ No zip file found."})
    mock_dl.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `pick_latest_zip_attachment` | returns `None` | No ZIPs |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Emit | `('stage', {'message': '❌ No zip file found.'})` | `assert_any_call` |
| `download_file` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| Guard fires | Unpacking `None` raises / download attempted |

### 15. test_failed_download_emits_error

*Class:* `TestHandleRunLatestEtl`

**Description:** `download_file` returning `(None, ...)` → `❌ Failed to download logs.zip`; decompose never runs.

**Code:**
```python
def test_failed_download_emits_error(self, mock_pick, mock_dl, mock_zip, mock_exists, mock_sub, mock_wifi_cls, mock_bt_cls):
    # Arrange
    mock_dl.return_value = (None, "logs.zip", False)

    # Act
    emit = self._run()

    # Assert
    emit.assert_any_call("stage", {"message": "❌ Failed to download logs.zip"})
    mock_zip.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `download_file` | returns `(None, 'logs.zip', False)` | Download failed |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Emit | `('stage', {'message': '❌ Failed to download logs.zip'})` | `assert_any_call` |
| `process_single_zip` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| Guard fires | Decompose on `None` |

### 16. test_downloaded_path_missing_on_disk_emits_error

*Class:* `TestHandleRunLatestEtl`

**Description:** A returned path that does not exist on disk is treated as a failed download.

**Code:**
```python
def test_downloaded_path_missing_on_disk_emits_error(self, mock_pick, mock_dl, mock_zip, mock_exists, mock_sub, mock_wifi_cls, mock_bt_cls):
    # Arrange
    mock_exists.return_value = False

    # Act
    emit = self._run()

    # Assert
    mock_exists.assert_called_once_with(ZIP_PATH)
    emit.assert_any_call("stage", {"message": "❌ Failed to download logs.zip"})
    mock_zip.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `os.path.exists` | `False` | File vanished |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `os.path.exists` | `(ZIP_PATH)` | `assert_called_once_with` |
| Emit | `('stage', {'message': '❌ Failed to download logs.zip'})` | `assert_any_call` |
| `process_single_zip` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| Existence checked | Missing file passed to decompose |

### 17. test_unexpected_exception_is_emitted

*Class:* `TestHandleRunLatestEtl`

**Description:** Any exception inside the `try` (here from `process_single_zip`) is reported to the UI as `❌ Exception: <message>`.

**Code:**
```python
def test_unexpected_exception_is_emitted(self, mock_pick, mock_dl, mock_zip, mock_exists, mock_sub, mock_wifi_cls, mock_bt_cls):
    # Arrange
    mock_zip.side_effect = RuntimeError("corrupt zip")

    # Act
    emit = self._run()

    # Assert
    emit.assert_any_call("stage", {"message": "❌ Exception: corrupt zip"})
    mock_wifi_cls.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `process_single_zip` | side effect `RuntimeError('corrupt zip')` | Extraction failure |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Emit | `('stage', {'message': '❌ Exception: corrupt zip'})` | `assert_any_call` |
| `WiFiAnalysisService` | not instantiated | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| Error surfaced, handler survives | Exception propagates to SocketIO |

### 18. test_missing_case_context_raises_key_error

*Class:* `TestHandleRunLatestEtl`

**Description:** The session read happens before the `try`, so a missing `case_context` raises `KeyError` and nothing is emitted. Documents current behavior.

**Code:**
```python
def test_missing_case_context_raises_key_error(self, mock_pick, mock_dl, mock_zip, mock_exists, mock_sub, mock_wifi_cls, mock_bt_cls):
    # Arrange: session read happens before the try block
    # Act / Assert
    with self.app.test_request_context("/"):
        with self.assertRaises(KeyError):
            handle_run_latest_etl()
    self.app_config.socketio.emit.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Session | *(empty)* | No `case_context` |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Exception | `KeyError` | `assertRaises` |
| `app_config.socketio.emit` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| `KeyError` raised before any emit | Handler starts tolerating a missing session (update test + doc) |

## Test Execution Flow

```
setUpClass ── patch app_config; rebind blueprints.automation.automation_routes.app_config (identity match)
     │
     ▼
setUp ── reset mock (socketio / driver_manager MagicMocks, silent_mode=False) ── patch CaseContext.to_session
      ── fresh Flask app ── register automation_bp ── client ── push app ctx
     │
     ├─ structure tests ── url_map / register_socketio_handlers(MagicMock())
     ├─ route tests     ── @patch render_template → client.get/post → assert template
     └─ handler tests   ── class @patch pick/download/process_single_zip/os.path.exists/subprocess/services
                          Arrange mock returns (+ silent_mode=True where tested)
                          Act: _run(**ctx) → test_request_context → session["case_context"] → handle_run_latest_etl()
                          Assert: emit calls / stage messages, explorer call, which service analyze() ran
     │
     ▼
tearDown ──► PASS / FAIL ── tearDownClass stops patchers
```

## Coverage Summary

| Metric | Value |
|---|---|
| Total Routes | 1 HTTP route (+ 1 SocketIO event) |
| Routes Tested | 1 / 1 (SocketIO handler also tested) |
| Total Tests | 18 (2 structure + 2 route + 14 handler logic) |
| Actual Coverage (%) | 100% of routes; every reachable handler branch incl. both `silent_mode` gates and the known DDD bug |
| Out-of-scope items | 5 (unreachable "No suitable ETL found", `wifi_or_bt=None` TypeError variant, dead DDD selection, wrapper closure, real Selenium/ZIP/analysis) |

## What Would Make These Tests Fail?

| Source change | ✅ PASS | ❌ FAIL |
|---|---|---|
| Fix the DDD `target_etl` bug | all others | `test_ddd_files_hit_known_name_error_bug` (update to the fixed behavior) |
| Remove a `silent_mode` gate | non-silent tests | `test_wifi_case_silent_mode_skips_explorer` / `test_bt_case_silent_mode_skips_explorer` |
| Change sort order (e.g. pick lowest `.etl.N`) | guard tests | Wi-Fi and BT happy-path tests |
| Drop the `history` filter | other tests | `test_history_files_are_filtered_out`, `test_only_history_files_emits_skip_message` |
| Stop clearing BT files for Wi-Fi cases (or vice versa) | guard tests | Wi-Fi / BT happy-path tests |
| Change BT analysis `mode="AutoFile"` | Wi-Fi tests | both BT tests |
| Add `namespace="/progress"` to the "No attachment" emit | other tests | `test_no_attachments_emits_error_and_returns` |
| Remove the `os.path.exists(zip_path)` check | other tests | `test_downloaded_path_missing_on_disk_emits_error` |
| Remove the outer `try/except` | happy paths | `test_unexpected_exception_is_emitted`, `test_ddd_files_hit_known_name_error_bug` |
| Rename the SocketIO event or template | handler tests | structure / route tests |
