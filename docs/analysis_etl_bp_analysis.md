# Analysis ETL Blueprint — Analysis & Unit Test Documentation

- Route module: [blueprints/analysis_etl/analysis_etl_routes.py](../blueprints/analysis_etl/analysis_etl_routes.py)
- Test module: [tests/test_analysis_etl_blueprint.py](../tests/test_analysis_etl_blueprint.py)
- Shared fixture: [tests/base_test.py](../tests/base_test.py)

## Overview — What Analysis ETL Does

The `analysis_etl` blueprint (`url_prefix="/analysis_etl"`) kicks off offline analysis of a downloaded log artifact. `/process_etl_path` dispatches a Wi-Fi or BT ETL trace to the matching analysis service (`wifi_service.analyze` / `bt_service.analyze`), and `/process_etl_path_fw` starts an asynchronous firmware-dump analysis task. `/cancel_fw_analysis` and `/fw_analysis_result` manage and render those FW tasks. The three service singletons (`WiFiAnalysisService`, `BTAnalysisService`, `FWAnalysisService`) are created at module import time.

> **Branch-specific:** on this branch both `process_etl_path` and `process_etl_path_fw` open Windows Explorer (`subprocess.run(['explorer', <dir>])`) **only when `app_config.silent_mode` is False** (the `--silent-mode` CLI flag added in commit f3818b1b). In silent mode the analysis still runs; only the GUI window is suppressed. Both sides of this gate are tested.

## All Routes (4 endpoints)

| Route | Method | Purpose |
|---|---|---|
| `/analysis_etl/process_etl_path` | GET | Validate `etl_path`, resolve `etl_type` (query param → `case_context.wifi_or_bt`), optionally open Explorer, run Wi-Fi or BT analysis |
| `/analysis_etl/process_etl_path_fw` | GET | Resolve `fw_type`, start async FW analysis (`fw_service.start_async`), optionally open Explorer, return `{ok, task_id, fw_path}` |
| `/analysis_etl/cancel_fw_analysis` | POST | Cancel an FW task by `task_id` (query string or JSON body) |
| `/analysis_etl/fw_analysis_result` | GET | Render `fw_analysis.html` for a completed FW task |

## High-Level Flow Diagram

```
GET /process_etl_path?etl_path=&etl_type=&mode=
  session["case_context"] ──missing──► KeyError (500)
  etl_path = to_long_path(get_long_path(unquote(etl_path)))
  etl_type = (etl_type param OR case_context.wifi_or_bt OR "").strip().lower()
        │
        ├─ etl_path empty / os.path.exists False ──► 200 "❌ Invalid file path: <path>"
        ├─ etl_type ∉ {wifi, bt}                 ──► 400 "❌ Unknown case subcategory"
        ▼
  silent_mode False? ──yes──► subprocess.run(['explorer', dirname(etl_path)])
        │
        ├─ wifi ──► wifi_service.analyze(etl_path)
        └─ bt   ──► bt_service.analyze(etl_path, mode, issue_type=session.classification.issue_type, wifi_or_bt='bt')
        ▼
  200 "🚀 Analysis triggered for: <path>"

GET /process_etl_path_fw?fw_path=&fw_type=
  fw_type = (fw_type param OR case_context.wifi_or_bt OR "").lower()
        ├─ fw_type ∉ {wifi, bt}            ──► 400 {"ok":false,"error":"Unknown case subcategory"}
        ├─ fw_service.start_async → (None, err) ──► 400 {"ok":false,"error":err}
        ▼
  silent_mode False? ──yes──► subprocess.run(['explorer', dirname(fw_path)])
  200 {"ok":true,"task_id","fw_path"}

POST /cancel_fw_analysis   task_id (query → JSON body)
        ├─ none          ──► 400 {"error":"task_id is required"}
        ├─ cancel False  ──► 404 {"error":"task not found or already finished"}
        └─ cancel True   ──► 200 {"ok":true,"task_id"}

GET /fw_analysis_result?task_id=
        ├─ none                    ──► 400 "❌ task_id is required"
        ├─ get_task None           ──► 404 "❌ task not found: <id>"
        ├─ status != completed     ──► 400 "❌ task is not completed yet (status=<s>)"
        └─ completed ──► render_template("fw_analysis.html", fw_path, system_info, system_text, log)
```

## Dependencies

```
flask:        Blueprint, render_template, request, session, jsonify
stdlib:       os (os.path.exists, os.path.dirname), subprocess (subprocess.run), urllib.parse.unquote
helpers:      utils.helpers.get_long_path, utils.helpers.to_long_path   (patched as identity in tests)
services:     wifi_service = WiFiAnalysisService()   (module-level singleton)
              bt_service   = BTAnalysisService()
              fw_service   = FWAnalysisService()     (start_async, cancel_task, get_task)
app_config:   app_config.silent_mode                  (branch-specific GUI gate)
models:       models.models.CaseContext.from_session
session keys: case_context (required by process_etl_path / _fw), classification (optional, BT issue_type)
template:     fw_analysis.html
```

## Test Coverage Analysis

```
STRUCTURE
  ✅ blueprint registered, url_prefix, exact rule set, cancel is POST-only ── test_blueprint_registered_with_url_prefix

ROUTES — /process_etl_path
  ✅ wifi hint → explorer + wifi_service.analyze              ── test_wifi_case_opens_explorer_and_runs_wifi_analysis
  ✅ silent_mode=True → no explorer, analysis still runs     ── test_silent_mode_skips_explorer_but_still_analyzes
  ✅ etl_type=BT overrides wifi hint; classification passed  ── test_etl_type_bt_overrides_wifi_hint_and_passes_classification
  ✅ bt hint, no classification → issue_type=None, mode=""   ── test_bt_hint_without_classification_passes_none_issue_type
  ✅ path does not exist → "Invalid file path"                ── test_nonexistent_path_returns_invalid_message
  ✅ empty path → "Invalid file path" (exists not called)     ── test_empty_path_returns_invalid_message
  ✅ unknown subcategory → 400                                ── test_unknown_subcategory_returns_400
  ✅ missing case_context → KeyError                          ── test_missing_case_context_raises_key_error

ROUTES — /process_etl_path_fw
  ✅ wifi hint → start_async(path,'wifi') + explorer + JSON   ── test_wifi_fw_starts_async_task_and_opens_explorer
  ✅ fw_type=BT param overrides hint                          ── test_fw_type_param_overrides_hint
  ✅ silent_mode=True → no explorer                           ── test_silent_mode_skips_explorer
  ✅ start_async returns (None, err) → 400, no explorer       ── test_start_async_failure_returns_400_without_explorer
  ✅ unknown fw_type → 400, start_async not called            ── test_unknown_fw_type_returns_400

ROUTES — /cancel_fw_analysis
  ✅ task_id from query string                                ── test_cancel_with_query_task_id
  ✅ task_id from JSON body                                   ── test_cancel_with_json_body_task_id
  ✅ no task_id → 400                                          ── test_cancel_without_task_id_returns_400
  ✅ cancel_task False → 404                                   ── test_cancel_unknown_task_returns_404

ROUTES — /fw_analysis_result
  ✅ completed → render_template kwargs                        ── test_completed_task_renders_template
  ✅ no task_id → 400                                           ── test_missing_task_id_returns_400
  ✅ unknown task → 404                                         ── test_unknown_task_returns_404
  ✅ not completed → 400 with status                            ── test_incomplete_task_returns_400_with_status

CORE LOGIC
  ⚪ get_long_path / to_long_path Win32 expansion ── utility code, patched as identity; note: in production
     to_long_path("") returns the absolute CWD, so the `not etl_path` guard never fires there —
     the os.path.exists check is what actually rejects an empty path.
  ⚠️ process_etl_path_fw does not validate fw_path (empty or missing is passed to fw_service.start_async) ── NOT TESTED; validation is delegated to the service

BACKGROUND / SOCKETIO
  ⚪ FW async worker inside FWAnalysisService ── service layer, out of blueprint scope (fw_service mocked)
```



## What the Unit Tests Actually Test

Patches applied at class level (decorators on the test class, injected into every method):

- `TestProcessEtlPath`: `os.path.exists` (default `True`), `wifi_service`, `bt_service`, `subprocess`, `get_long_path` and `to_long_path` (identity side-effect).
- `TestProcessEtlPathFw`: `fw_service`, `subprocess`, `get_long_path` (identity).
- `TestCancelFwAnalysis`: `fw_service`.
- `TestFwAnalysisResult`: `fw_service`, `render_template` (returns `"rendered-fw"`).

Module constants: `ETL_PATH = r"C:\fake\case\WiFi\trace.etl"`, `ETL_DIR = r"C:\fake\case\WiFi"`, `FW_PATH = r"C:\fake\case\FW\fw_dump.bin"`, `FW_DIR = r"C:\fake\case\FW"`. `seed_session()` stores `create_mock_case_context()` (`wifi_or_bt="wifi"`, `case_nbr="00123456"`). `app_config.silent_mode` is `False` unless a test sets it.

### 1. test_blueprint_registered_with_url_prefix

*Class:* `TestAnalysisEtlBlueprintStructure`

**Description:** Checks that `analysis_etl_bp` registers under its own name with `url_prefix='/analysis_etl'`, that it exposes exactly the four expected URL rules, and that `/cancel_fw_analysis` is POST-only. Catches added/removed/renamed routes and method changes.

**Code:**
```python
def test_blueprint_registered_with_url_prefix(self):
    # Arrange / Act
    rules = {r.rule: r.methods for r in self.app.url_map.iter_rules()
             if r.endpoint.startswith("analysis_etl.")}

    # Assert
    self.assertIs(self.app.blueprints.get("analysis_etl"), analysis_etl_bp)
    self.assertEqual(analysis_etl_bp.url_prefix, "/analysis_etl")
    self.assertEqual(set(rules), {
        "/analysis_etl/process_etl_path",
        "/analysis_etl/process_etl_path_fw",
        "/analysis_etl/cancel_fw_analysis",
        "/analysis_etl/fw_analysis_result",
    })
    self.assertIn("POST", rules["/analysis_etl/cancel_fw_analysis"])
    self.assertNotIn("GET", rules["/analysis_etl/cancel_fw_analysis"])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Registered blueprint | `analysis_etl_bp` | Registered in `BaseTestCase.setUp` |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `app.blueprints['analysis_etl']` | `analysis_etl_bp` | `assertIs` |
| `url_prefix` | `'/analysis_etl'` | `assertEqual` |
| Rule set | `process_etl_path`, `process_etl_path_fw`, `cancel_fw_analysis`, `fw_analysis_result` under `/analysis_etl/` | `assertEqual(set)` |
| `cancel_fw_analysis` methods | contains `POST`, not `GET` | `assertIn` / `assertNotIn` |

| Pass Condition | Fail Condition |
|---|---|
| Name, prefix, exact rules and POST-only cancel all match | A route is added/removed/renamed, prefix changes, or cancel accepts GET |

### 2. test_wifi_case_opens_explorer_and_runs_wifi_analysis

*Class:* `TestProcessEtlPath`

**Description:** Happy path for a Wi-Fi case: with no `etl_type` the route falls back to `case_context.wifi_or_bt='wifi'`, checks the path exists, opens Explorer on the containing folder (silent_mode False) and calls `wifi_service.analyze` with the path. BT analysis must not run.

**Code:**
```python
def test_wifi_case_opens_explorer_and_runs_wifi_analysis(self, mock_exists, mock_wifi, mock_bt, mock_sub, *_):
    # Arrange
    self.seed_session()  # wifi_or_bt="wifi"

    # Act
    resp = self.client.get("/analysis_etl/process_etl_path", query_string={"etl_path": ETL_PATH})

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertIn(f"Analysis triggered for: {ETL_PATH}", resp.get_data(as_text=True))
    mock_exists.assert_called_once_with(ETL_PATH)
    mock_sub.run.assert_called_once_with(["explorer", ETL_DIR])
    mock_wifi.analyze.assert_called_once_with(ETL_PATH)
    mock_bt.analyze.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `session['case_context']` | `create_mock_case_context()` | `wifi_or_bt='wifi'` |
| Query `etl_path` | `C:\fake\case\WiFi\trace.etl` | ETL to analyze |
| `os.path.exists` | `True` | Path exists |
| `app_config.silent_mode` | `False` | Fixture default |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| Body | contains `Analysis triggered for: C:\fake\case\WiFi\trace.etl` | `assertIn` |
| `os.path.exists` | called once with `ETL_PATH` | `assert_called_once_with` |
| `subprocess.run` | `['explorer', 'C:\fake\case\WiFi']` | `assert_called_once_with` |
| `wifi_service.analyze` | `(ETL_PATH)` | `assert_called_once_with` |
| `bt_service.analyze` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| Explorer opened on parent folder and Wi-Fi analysis called once | Wrong service, wrong path, Explorer not opened or opened on the file |

### 3. test_silent_mode_skips_explorer_but_still_analyzes

*Class:* `TestProcessEtlPath`

**Description:** **Branch-specific.** With `app_config.silent_mode = True` (the `--silent-mode` flag) the route must not launch Explorer but must still run the Wi-Fi analysis. Guards the `if not app_config.silent_mode:` gate.

**Code:**
```python
def test_silent_mode_skips_explorer_but_still_analyzes(self, mock_exists, mock_wifi, mock_bt, mock_sub, *_):
    # Arrange (branch-specific: --silent-mode)
    self.app_config.silent_mode = True
    self.seed_session()

    # Act
    resp = self.client.get("/analysis_etl/process_etl_path", query_string={"etl_path": ETL_PATH})

    # Assert
    self.assertEqual(resp.status_code, 200)
    mock_sub.run.assert_not_called()
    mock_wifi.analyze.assert_called_once_with(ETL_PATH)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `app_config.silent_mode` | `True` | Set by the test |
| Query `etl_path` | `ETL_PATH` | Existing ETL |
| `session['case_context']` | `create_mock_case_context()` | Wi-Fi hint |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| `subprocess.run` | not called | `assert_not_called` |
| `wifi_service.analyze` | `(ETL_PATH)` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| No GUI launch, analysis still runs | Explorer launched in silent mode, or analysis skipped |

### 4. test_etl_type_bt_overrides_wifi_hint_and_passes_classification

*Class:* `TestProcessEtlPath`

**Description:** Coex case: the Salesforce hint says `wifi` but the caller passes `etl_type=BT`. The explicit param wins (case-insensitive) and the BT service receives `mode`, the `issue_type` from `session['classification']`, and `wifi_or_bt='bt'`.

**Code:**
```python
def test_etl_type_bt_overrides_wifi_hint_and_passes_classification(self, mock_exists, mock_wifi, mock_bt, mock_sub, *_):
    # Arrange: Salesforce hint says wifi, coex caller asks for bt
    self.seed_session(classification={"issue_type": "Audio", "confidence": 90})

    # Act
    resp = self.client.get("/analysis_etl/process_etl_path", query_string={
        "etl_path": ETL_PATH, "etl_type": "BT", "mode": "Manual"})

    # Assert
    self.assertEqual(resp.status_code, 200)
    mock_bt.analyze.assert_called_once_with(ETL_PATH, mode="Manual", issue_type="Audio", wifi_or_bt="bt")
    mock_wifi.analyze.assert_not_called()
    mock_sub.run.assert_called_once_with(["explorer", ETL_DIR])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `session['classification']` | `{'issue_type': 'Audio', 'confidence': 90}` | LLM classification |
| Query | `etl_path=ETL_PATH`, `etl_type='BT'`, `mode='Manual'` | Explicit BT request |
| `case_context.wifi_or_bt` | `'wifi'` | Conflicting hint |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| `bt_service.analyze` | `(ETL_PATH, mode='Manual', issue_type='Audio', wifi_or_bt='bt')` | `assert_called_once_with` |
| `wifi_service.analyze` | not called | `assert_not_called` |
| `subprocess.run` | `['explorer', ETL_DIR]` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| BT service called with exact kwargs | Hint overrides the param, casing not normalized, or kwargs differ |

### 5. test_bt_hint_without_classification_passes_none_issue_type

*Class:* `TestProcessEtlPath`

**Description:** BT case with no `etl_type` and no `classification` in session: the route falls back to `wifi_or_bt='bt'`, passes `mode=''` (default) and `issue_type=None` without raising.

**Code:**
```python
def test_bt_hint_without_classification_passes_none_issue_type(self, mock_exists, mock_wifi, mock_bt, mock_sub, *_):
    # Arrange
    with self.client.session_transaction() as sess:
        sess["case_context"] = self.create_mock_case_context(wifi_or_bt="bt")

    # Act
    resp = self.client.get("/analysis_etl/process_etl_path", query_string={"etl_path": ETL_PATH})

    # Assert
    self.assertEqual(resp.status_code, 200)
    mock_bt.analyze.assert_called_once_with(ETL_PATH, mode="", issue_type=None, wifi_or_bt="bt")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `session['case_context']` | `create_mock_case_context(wifi_or_bt='bt')` | BT hint |
| `session['classification']` | *(absent)* | No LLM classification yet |
| Query `etl_path` | `ETL_PATH` | Existing ETL |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| `bt_service.analyze` | `(ETL_PATH, mode='', issue_type=None, wifi_or_bt='bt')` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| BT analysis runs with `issue_type=None` | AttributeError on missing classification, or wrong defaults |

### 6. test_nonexistent_path_returns_invalid_message

*Class:* `TestProcessEtlPath`

**Description:** When `os.path.exists` is False the route returns the plain-text `❌ Invalid file path: <path>` (HTTP 200, as written in the source) and performs no side effects.

**Code:**
```python
def test_nonexistent_path_returns_invalid_message(self, mock_exists, mock_wifi, mock_bt, mock_sub, *_):
    # Arrange
    mock_exists.return_value = False
    self.seed_session()

    # Act
    resp = self.client.get("/analysis_etl/process_etl_path", query_string={"etl_path": ETL_PATH})

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertIn(f"Invalid file path: {ETL_PATH}", resp.get_data(as_text=True))
    mock_sub.run.assert_not_called()
    mock_wifi.analyze.assert_not_called()
    mock_bt.analyze.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `os.path.exists` | `False` | Path missing |
| Query `etl_path` | `ETL_PATH` | Non-existent file |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| Body | contains `Invalid file path: C:\fake\case\WiFi\trace.etl` | `assertIn` |
| `subprocess.run`, `wifi_service.analyze`, `bt_service.analyze` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| Message returned, nothing launched | Explorer opened or analysis started for a missing file |

### 7. test_empty_path_returns_invalid_message

*Class:* `TestProcessEtlPath`

**Description:** No `etl_path` query param: with the path helpers patched as identity, `etl_path` is `''`, so `not etl_path` short-circuits before `os.path.exists`. (In production `to_long_path('')` returns the CWD, so this guard is effectively handled by `os.path.exists` instead — see Test Coverage Analysis.)

**Code:**
```python
def test_empty_path_returns_invalid_message(self, mock_exists, mock_wifi, mock_bt, mock_sub, *_):
    # Arrange
    self.seed_session()

    # Act
    resp = self.client.get("/analysis_etl/process_etl_path")

    # Assert
    self.assertIn("Invalid file path", resp.get_data(as_text=True))
    mock_exists.assert_not_called()  # short-circuits on the empty string
    mock_wifi.analyze.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Query `etl_path` | *(absent)* → `''` | Empty path |
| `get_long_path` / `to_long_path` | identity | Patched |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Body | contains `Invalid file path` | `assertIn` |
| `os.path.exists` | not called | `assert_not_called` |
| `wifi_service.analyze` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| Empty input rejected before any IO | Empty path reaches `exists` or the analysis service |

### 8. test_unknown_subcategory_returns_400

*Class:* `TestProcessEtlPath`

**Description:** With no `etl_type` param and `wifi_or_bt=None`, `etl_type` resolves to `''`, which is not `wifi`/`bt`; the route returns HTTP 400 `❌ Unknown case subcategory` without launching anything.

**Code:**
```python
def test_unknown_subcategory_returns_400(self, mock_exists, mock_wifi, mock_bt, mock_sub, *_):
    # Arrange: no etl_type and no wifi_or_bt hint
    with self.client.session_transaction() as sess:
        sess["case_context"] = self.create_mock_case_context(wifi_or_bt=None)

    # Act
    resp = self.client.get("/analysis_etl/process_etl_path", query_string={"etl_path": ETL_PATH})

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertIn("Unknown case subcategory", resp.get_data(as_text=True))
    mock_sub.run.assert_not_called()
    mock_wifi.analyze.assert_not_called()
    mock_bt.analyze.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `session['case_context']` | `create_mock_case_context(wifi_or_bt=None)` | No hint |
| Query `etl_path` | `ETL_PATH` | Existing ETL |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` | `assertEqual` |
| Body | contains `Unknown case subcategory` | `assertIn` |
| `subprocess.run`, both analyze methods | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| 400 and no side effects | Defaults to a service, or Explorer opened before the type check |

### 9. test_missing_case_context_raises_key_error

*Class:* `TestProcessEtlPath`

**Description:** Documents current behavior: `session['case_context']` is indexed directly, so an empty session raises `KeyError` (500 in production). No analysis must start.

**Code:**
```python
def test_missing_case_context_raises_key_error(self, mock_exists, mock_wifi, mock_bt, mock_sub, *_):
    # Arrange: empty session - route indexes session["case_context"] directly
    # Act / Assert
    with self.assertRaises(KeyError):
        self.client.get("/analysis_etl/process_etl_path", query_string={"etl_path": ETL_PATH})
    mock_wifi.analyze.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Session | *(empty)* | No `case_context` |
| Query `etl_path` | `ETL_PATH` | Valid path |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Exception | `KeyError` | `assertRaises` |
| `wifi_service.analyze` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| `KeyError` propagates | Route starts handling a missing session gracefully (update test + doc) |

### 10. test_wifi_fw_starts_async_task_and_opens_explorer

*Class:* `TestProcessEtlPathFw`

**Description:** FW happy path: with the Wi-Fi hint, `fw_service.start_async(fw_path, 'wifi')` returns a task id, Explorer opens the dump's folder, and the JSON response echoes `task_id` and `fw_path`.

**Code:**
```python
def test_wifi_fw_starts_async_task_and_opens_explorer(self, mock_fw, mock_sub, _):
    # Arrange
    mock_fw.start_async.return_value = ("task-1", None)
    self.seed_session()

    # Act
    resp = self.client.get("/analysis_etl/process_etl_path_fw", query_string={"fw_path": FW_PATH})

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_json(), {"ok": True, "task_id": "task-1", "fw_path": FW_PATH})
    mock_fw.start_async.assert_called_once_with(FW_PATH, "wifi")
    mock_sub.run.assert_called_once_with(["explorer", FW_DIR])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `fw_service.start_async` | returns `('task-1', None)` | Task created |
| Query `fw_path` | `C:\fake\case\FW\fw_dump.bin` | FW dump |
| `case_context.wifi_or_bt` | `'wifi'` | Hint |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| JSON | `{'ok': True, 'task_id': 'task-1', 'fw_path': FW_PATH}` | `assertEqual` |
| `fw_service.start_async` | `(FW_PATH, 'wifi')` | `assert_called_once_with` |
| `subprocess.run` | `['explorer', 'C:\fake\case\FW']` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Task started, folder opened, JSON exact | Wrong type passed, JSON shape changed, Explorer not opened |

### 11. test_fw_type_param_overrides_hint

*Class:* `TestProcessEtlPathFw`

**Description:** The coex FW dropdown sends `fw_type=BT`; it overrides the Wi-Fi hint and is lower-cased before reaching `start_async`.

**Code:**
```python
def test_fw_type_param_overrides_hint(self, mock_fw, mock_sub, _):
    # Arrange
    mock_fw.start_async.return_value = ("task-2", None)
    self.seed_session()

    # Act
    resp = self.client.get("/analysis_etl/process_etl_path_fw",
                           query_string={"fw_path": FW_PATH, "fw_type": "BT"})

    # Assert
    self.assertEqual(resp.get_json()["task_id"], "task-2")
    mock_fw.start_async.assert_called_once_with(FW_PATH, "bt")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Query | `fw_path=FW_PATH`, `fw_type='BT'` | Explicit type |
| `fw_service.start_async` | returns `('task-2', None)` | Task created |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| JSON `task_id` | `'task-2'` | `assertEqual` |
| `fw_service.start_async` | `(FW_PATH, 'bt')` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Param wins and is lower-cased | Hint wins or `'BT'` passed un-normalized |

### 12. test_silent_mode_skips_explorer

*Class:* `TestProcessEtlPathFw`

**Description:** **Branch-specific.** With `silent_mode=True` the FW task still starts and returns `ok: true`, but Explorer is not launched.

**Code:**
```python
def test_silent_mode_skips_explorer(self, mock_fw, mock_sub, _):
    # Arrange (branch-specific: --silent-mode)
    self.app_config.silent_mode = True
    mock_fw.start_async.return_value = ("task-3", None)
    self.seed_session()

    # Act
    resp = self.client.get("/analysis_etl/process_etl_path_fw", query_string={"fw_path": FW_PATH})

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertTrue(resp.get_json()["ok"])
    mock_sub.run.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `app_config.silent_mode` | `True` | Set by the test |
| `fw_service.start_async` | returns `('task-3', None)` | Task created |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| JSON `ok` | `True` | `assertTrue` |
| `subprocess.run` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| Task started without GUI | Explorer launched in silent mode |

### 13. test_start_async_failure_returns_400_without_explorer

*Class:* `TestProcessEtlPathFw`

**Description:** When `start_async` returns `(None, 'fw file not found')` the route returns that error with HTTP 400 and does not open Explorer (the GUI step happens only after a successful start).

**Code:**
```python
def test_start_async_failure_returns_400_without_explorer(self, mock_fw, mock_sub, _):
    # Arrange
    mock_fw.start_async.return_value = (None, "fw file not found")
    self.seed_session()

    # Act
    resp = self.client.get("/analysis_etl/process_etl_path_fw", query_string={"fw_path": FW_PATH})

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertEqual(resp.get_json(), {"ok": False, "error": "fw file not found"})
    mock_sub.run.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `fw_service.start_async` | returns `(None, 'fw file not found')` | Start failed |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` | `assertEqual` |
| JSON | `{'ok': False, 'error': 'fw file not found'}` | `assertEqual` |
| `subprocess.run` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| Service error surfaced as 400 | Error swallowed, 200 returned, or Explorer opened |

### 14. test_unknown_fw_type_returns_400

*Class:* `TestProcessEtlPathFw`

**Description:** No `fw_type` and `wifi_or_bt=None` → 400 `Unknown case subcategory`; the FW service is never called.

**Code:**
```python
def test_unknown_fw_type_returns_400(self, mock_fw, mock_sub, _):
    # Arrange
    with self.client.session_transaction() as sess:
        sess["case_context"] = self.create_mock_case_context(wifi_or_bt=None)

    # Act
    resp = self.client.get("/analysis_etl/process_etl_path_fw", query_string={"fw_path": FW_PATH})

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertEqual(resp.get_json(), {"ok": False, "error": "Unknown case subcategory"})
    mock_fw.start_async.assert_not_called()
    mock_sub.run.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `session['case_context']` | `create_mock_case_context(wifi_or_bt=None)` | No hint |
| Query `fw_path` | `FW_PATH` | FW dump |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` | `assertEqual` |
| JSON | `{'ok': False, 'error': 'Unknown case subcategory'}` | `assertEqual` |
| `fw_service.start_async`, `subprocess.run` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| Rejected before any service call | Service called with an empty type |

### 15. test_cancel_with_query_task_id

*Class:* `TestCancelFwAnalysis`

**Description:** `task_id` supplied in the query string is passed to `fw_service.cancel_task`; on `True` the route returns `{ok: true, task_id}`.

**Code:**
```python
def test_cancel_with_query_task_id(self, mock_fw):
    # Arrange
    mock_fw.cancel_task.return_value = True

    # Act
    resp = self.client.post("/analysis_etl/cancel_fw_analysis", query_string={"task_id": "task-1"})

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_json(), {"ok": True, "task_id": "task-1"})
    mock_fw.cancel_task.assert_called_once_with("task-1")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Query `task_id` | `'task-1'` | Task to cancel |
| `fw_service.cancel_task` | returns `True` | Cancelled |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| JSON | `{'ok': True, 'task_id': 'task-1'}` | `assertEqual` |
| `fw_service.cancel_task` | `('task-1')` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Cancel called and confirmed | Query param ignored or JSON shape changes |

### 16. test_cancel_with_json_body_task_id

*Class:* `TestCancelFwAnalysis`

**Description:** When the query string has no `task_id`, the route falls back to the JSON body (`navigator.sendBeacon`-style callers).

**Code:**
```python
def test_cancel_with_json_body_task_id(self, mock_fw):
    # Arrange
    mock_fw.cancel_task.return_value = True

    # Act
    resp = self.client.post("/analysis_etl/cancel_fw_analysis", json={"task_id": "task-9"})

    # Assert
    self.assertEqual(resp.status_code, 200)
    mock_fw.cancel_task.assert_called_once_with("task-9")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| JSON body | `{'task_id': 'task-9'}` | Body-supplied id |
| `fw_service.cancel_task` | returns `True` | Cancelled |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| `fw_service.cancel_task` | `('task-9')` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Body fallback works | Body ignored → 400 |

### 17. test_cancel_without_task_id_returns_400

*Class:* `TestCancelFwAnalysis`

**Description:** Neither query nor body has `task_id` → 400 `task_id is required`; the service is not called.

**Code:**
```python
def test_cancel_without_task_id_returns_400(self, mock_fw):
    # Act
    resp = self.client.post("/analysis_etl/cancel_fw_analysis")

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertEqual(resp.get_json(), {"ok": False, "error": "task_id is required"})
    mock_fw.cancel_task.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Request | POST with no query and no body | Missing id |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` | `assertEqual` |
| JSON | `{'ok': False, 'error': 'task_id is required'}` | `assertEqual` |
| `fw_service.cancel_task` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| Validation rejects the request | Service called with an empty id |

### 18. test_cancel_unknown_task_returns_404

*Class:* `TestCancelFwAnalysis`

**Description:** `cancel_task` returning `False` (unknown or finished task) maps to 404 with a fixed error message.

**Code:**
```python
def test_cancel_unknown_task_returns_404(self, mock_fw):
    # Arrange
    mock_fw.cancel_task.return_value = False

    # Act
    resp = self.client.post("/analysis_etl/cancel_fw_analysis", json={"task_id": "gone"})

    # Assert
    self.assertEqual(resp.status_code, 404)
    self.assertEqual(resp.get_json(), {"ok": False, "error": "task not found or already finished"})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| JSON body | `{'task_id': 'gone'}` | Unknown id |
| `fw_service.cancel_task` | returns `False` | Not cancellable |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `404` | `assertEqual` |
| JSON | `{'ok': False, 'error': 'task not found or already finished'}` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| 404 with message | 200 returned for an unknown task |

### 19. test_completed_task_renders_template

*Class:* `TestFwAnalysisResult`

**Description:** A completed FW task is rendered via `fw_analysis.html` with exactly the task's `fw_path` and the `system_info`, `system_text`, `log` fields from its `result`.

**Code:**
```python
def test_completed_task_renders_template(self, mock_fw, mock_render):
    # Arrange
    mock_fw.get_task.return_value = {
        "status": "completed",
        "fw_path": FW_PATH,
        "result": {"system_info": {"fw": "89.1"}, "system_text": "sys text", "log": "log text"},
    }

    # Act
    resp = self.client.get("/analysis_etl/fw_analysis_result", query_string={"task_id": "task-1"})

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_data(as_text=True), "rendered-fw")
    mock_fw.get_task.assert_called_once_with("task-1")
    mock_render.assert_called_once_with(
        "fw_analysis.html", fw_path=FW_PATH, system_info={"fw": "89.1"},
        system_text="sys text", log="log text")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Query `task_id` | `'task-1'` | Completed task |
| `fw_service.get_task` | `{'status': 'completed', 'fw_path': FW_PATH, 'result': {'system_info': {'fw': '89.1'}, 'system_text': 'sys text', 'log': 'log text'}}` | Task record |
| `render_template` | returns `'rendered-fw'` | Patched |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / body | `200` / `'rendered-fw'` | `assertEqual` |
| `fw_service.get_task` | `('task-1')` | `assert_called_once_with` |
| `render_template` | `('fw_analysis.html', fw_path=FW_PATH, system_info={'fw': '89.1'}, system_text='sys text', log='log text')` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Template rendered with exact kwargs | Template name or any kwarg changes |

### 20. test_missing_task_id_returns_400

*Class:* `TestFwAnalysisResult`

**Description:** No `task_id` query param → 400 `❌ task_id is required`; the service is not queried.

**Code:**
```python
def test_missing_task_id_returns_400(self, mock_fw, mock_render):
    # Act
    resp = self.client.get("/analysis_etl/fw_analysis_result")

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertIn("task_id is required", resp.get_data(as_text=True))
    mock_fw.get_task.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Query `task_id` | *(absent)* | Missing id |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` | `assertEqual` |
| Body | contains `task_id is required` | `assertIn` |
| `fw_service.get_task` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| Rejected before lookup | Lookup attempted with empty id |

### 21. test_unknown_task_returns_404

*Class:* `TestFwAnalysisResult`

**Description:** `get_task` returning `None` → 404 `❌ task not found: nope`; nothing is rendered.

**Code:**
```python
def test_unknown_task_returns_404(self, mock_fw, mock_render):
    # Arrange
    mock_fw.get_task.return_value = None

    # Act
    resp = self.client.get("/analysis_etl/fw_analysis_result", query_string={"task_id": "nope"})

    # Assert
    self.assertEqual(resp.status_code, 404)
    self.assertIn("task not found: nope", resp.get_data(as_text=True))
    mock_render.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Query `task_id` | `'nope'` | Unknown id |
| `fw_service.get_task` | returns `None` | Not found |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `404` | `assertEqual` |
| Body | contains `task not found: nope` | `assertIn` |
| `render_template` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| 404 with id echoed | Template rendered or 500 on `None` |

### 22. test_incomplete_task_returns_400_with_status

*Class:* `TestFwAnalysisResult`

**Description:** A task whose status is not `completed` (here `running`) returns 400 and echoes the status; the result template is not rendered.

**Code:**
```python
def test_incomplete_task_returns_400_with_status(self, mock_fw, mock_render):
    # Arrange
    mock_fw.get_task.return_value = {"status": "running"}

    # Act
    resp = self.client.get("/analysis_etl/fw_analysis_result", query_string={"task_id": "task-1"})

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertIn("status=running", resp.get_data(as_text=True))
    mock_render.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Query `task_id` | `'task-1'` | Running task |
| `fw_service.get_task` | `{'status': 'running'}` | Not finished |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` | `assertEqual` |
| Body | contains `status=running` | `assertIn` |
| `render_template` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| 400 with status | Partial result rendered (KeyError on `result`) |

## Test Execution Flow

```
setUpClass (BaseTestCase)
  └─ patch configs.global_configs.app_config → MagicMock; repoint
     blueprints.analysis_etl.analysis_etl_routes.app_config (identity match) to the same mock
        │
        ▼
for each test class (Structure / ProcessEtlPath / ProcessEtlPathFw / CancelFwAnalysis / FwAnalysisResult):
  class-level @patch decorators wrap every test method
  (os.path.exists, wifi_service, bt_service, fw_service, subprocess, get_long_path, to_long_path, render_template)
        │
        ▼
  setUp: reset mock app_config (silent_mode=False) → patch CaseContext.to_session →
         fresh Flask app → register analysis_etl_bp → test client → push app context
        │                                   (no 'main' stub needed: no url_for('main.*'))
        ▼
  test: seed_session(...) [+ app_config.silent_mode=True where tested]
        Arrange → Act (client.get / client.post) → Assert (status, body/JSON, mock calls)
        │
        ▼
  tearDown ──► PASS / FAIL
        │
        ▼
tearDownClass: stop all app_config patchers
```

## Coverage Summary

| Metric | Value |
|---|---|
| Total Routes | 4 |
| Routes Tested | 4 |
| Total Tests | 22 (1 structure + 21 route) |
| Actual Coverage (%) | 100% of routes; every validation/success branch in the route bodies, including both sides of both `silent_mode` gates |
| Out-of-scope items | 3 (Win32 long-path helpers, FW async worker, `fw_path` validation delegated to the service) |

## What Would Make These Tests Fail?

| Source change | ✅ PASS | ❌ FAIL |
|---|---|---|
| Remove the `if not app_config.silent_mode:` gate in `process_etl_path` | FW / cancel / result tests | `test_silent_mode_skips_explorer_but_still_analyzes` |
| Remove the gate in `process_etl_path_fw` | `process_etl_path` tests | `test_silent_mode_skips_explorer` |
| Open Explorer before the existence / type checks | success-path tests | `test_nonexistent_path_returns_invalid_message`, `test_unknown_subcategory_returns_400` |
| Let `case_context.wifi_or_bt` override the `etl_type` / `fw_type` param | wifi-hint tests | `test_etl_type_bt_overrides_wifi_hint_and_passes_classification`, `test_fw_type_param_overrides_hint` |
| Stop passing `issue_type` / `wifi_or_bt` kwargs to `bt_service.analyze` | Wi-Fi tests | both BT `process_etl_path` tests |
| Open Explorer before checking `start_async`'s result | other FW tests | `test_start_async_failure_returns_400_without_explorer` |
| Drop the JSON-body fallback in `cancel_fw_analysis` | query-string cancel | `test_cancel_with_json_body_task_id` |
| Return 200 for an unknown cancel / result task | success tests | `test_cancel_unknown_task_returns_404`, `test_unknown_task_returns_404` |
| Rename a `render_template` kwarg or the template | other tests | `test_completed_task_renders_template` |
| Add / rename a route or make cancel accept GET | route tests | `test_blueprint_registered_with_url_prefix` |
