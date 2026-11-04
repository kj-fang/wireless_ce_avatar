# LLM Blueprint — Analysis & Unit Test Documentation

- Route module: [blueprints/llm/llm_routes.py](../blueprints/llm/llm_routes.py)
- Test module: [tests/test_llm_blueprint.py](../tests/test_llm_blueprint.py)
- Shared fixture: [tests/base_test.py](../tests/base_test.py)

## Overview — What LLM Does

The `llm` blueprint (`url_prefix="/llm"`) exposes a single JSON endpoint that produces the AI summary/classification of the current IPS case shown on the "select attachments" page. It rehydrates the full `CaseContext` from the session (including the IPS **Environment Details** form `env_detail`, comments and attachments), calls `app_config.llm_helper.analyze_desc(prompt_path=session['prompt_file_path'], case_context=…, return_usage=True)`, stores the result in `session['ai_ips_analysis']` and (when it is a dict) `session['classification']`, and records feature-usage + attachment-declaration telemetry via `gather_service`. All errors are converted to a `500 {"success": false, "error": …}` JSON response.

## All Routes (1 endpoint)

| Route | Method | Purpose |
|---|---|---|
| `/llm/get_llm_analysis` | GET | Run LLM case analysis, update `classification` / `ai_ips_analysis` in session, record telemetry, return `{success, ai_analysis}` JSON |

## High-Level Flow Diagram

```
GET /llm/get_llm_analysis
  session['classification'] = {"issue_type":"Unclassified","confidence":0,"keywords_found":[]}
  operation_usage = LLM_helper.empty_usage()
  try:
    llm_helper = app_config.llm_helper
      ├─ None ──► ai_analysis = "LLM helper currently not available"   (no telemetry)
      └─ set  ──► _ctx_full = CaseContext.from_session(session.get('case_context') or {}).to_dict()
                  ai_analysis, usage = llm_helper.analyze_desc(prompt_path=session['prompt_file_path'],  ← KeyError if missing
                                                               case_context=_ctx_full, return_usage=True)
                  dict? ──► session['classification'] = ai_analysis['Classification']
    session['ai_ips_analysis'] = ai_analysis
    llm_helper set ──► try: gather.record_feature_usage(status = success if ai_analysis else failed,
                                                         error_code = "" | "empty_result", domain = wifi_or_bt or wifi)
                            gather.record_attachment_declaration(ai_analysis, source, issue, domain)
                       except: pass
    200 application/json {"success": true, "ai_analysis": …}
  except Exception e:
    try gather.record_feature_usage(status="failed", error_code=type(e).__name__) except: pass
    500 {"success": false, "error": str(e)}
```

## Dependencies

```
flask:        Blueprint, session, Response, jsonify
stdlib:       json, time.perf_counter, traceback
services:     services.llm_service.LLM_helper           (empty_usage() static; type hint)
              services.gather_service.record_feature_usage, record_attachment_declaration
app_config:   app_config.llm_helper  (.analyze_desc, .model)
models:       models.models.CaseContext (imported lazily inside the route) .from_session / .to_dict
session keys: case_context (optional), prompt_file_path (required when llm_helper set),
              gather_workflow_id (optional) ; writes classification, ai_ips_analysis
```

## Test Coverage Analysis

```
STRUCTURE
  ✅ blueprint registered, url_prefix, single GET rule                 ── test_blueprint_registered_with_url_prefix

ROUTES — /llm/get_llm_analysis
  ✅ dict result → classification + telemetry success (exact kwargs) ── test_dict_analysis_updates_classification_and_records_success
  ✅ string result → default classification kept                      ── test_string_analysis_keeps_default_classification
  ✅ empty result → status "failed", error_code "empty_result"        ── test_empty_analysis_records_failed_status
  ✅ BT case → telemetry domain "bt"                                   ── test_bt_case_records_bt_domain
  ✅ llm_helper None → placeholder text, no telemetry                  ── test_llm_helper_unavailable_returns_placeholder
  ✅ analyze_desc raises → 500 + failure telemetry (exact kwargs)     ── test_llm_exception_returns_500_and_records_failure
  ✅ prompt_file_path missing → 500 KeyError                           ── test_missing_prompt_file_path_returns_500
  ✅ case_context missing → empty context passed to LLM               ── test_missing_case_context_still_calls_llm_with_empty_context
  ✅ telemetry exception swallowed                                     ── test_telemetry_failure_is_swallowed

CORE LOGIC
  ✅ env_detail (IPS Environment Details) forwarded in case_context   ── test_dict_analysis_updates_classification_and_records_success
  ⚪ sidecar rehydration in CaseContext.from_session ── model-layer file IO; tests use a slim in-cookie context
  ⚪ exception inside the except-branch telemetry call ── swallowed by a bare `except: pass`; no observable effect
```



## What the Unit Tests Actually Test

`TestGetLlmAnalysis` patches `gather_service` at class level. `setUp` sets `self.llm = app_config.llm_helper` (a MagicMock) and `self.llm.model = "test-model"`. `_get()` performs `GET /llm/get_llm_analysis` and returns `(resp, json.loads(body))`. Constants: `PROMPT_PATH = r"C:\fake\prompts\wifi_case_summary.txt"`, `DEFAULT_CLASSIFICATION = {"issue_type": "Unclassified", "confidence": 0, "keywords_found": []}`, `USAGE = {"llm_calls": 1, "input_tokens": 100, "cache_read_tokens": 0, "cache_write_tokens": 0, "output_tokens": 20, "total_tokens": 120}`.

### 1. test_blueprint_registered_with_url_prefix

*Class:* `TestLlmBlueprintStructure`

**Description:** Checks the blueprint name, `url_prefix='/llm'`, that `/llm/get_llm_analysis` is its only rule and that it accepts GET (the front end fetches it with GET).

**Code:**
```python
def test_blueprint_registered_with_url_prefix(self):
    # Arrange / Act
    rules = {r.rule: r.methods for r in self.app.url_map.iter_rules()
             if r.endpoint.startswith("llm.")}

    # Assert
    self.assertIs(self.app.blueprints.get("llm"), llm_bp)
    self.assertEqual(llm_bp.url_prefix, "/llm")
    self.assertEqual(set(rules), {"/llm/get_llm_analysis"})
    self.assertIn("GET", rules["/llm/get_llm_analysis"])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Registered blueprint | `llm_bp` | From `BaseTestCase.setUp` |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `app.blueprints['llm']` | `llm_bp` | `assertIs` |
| `url_prefix` | `'/llm'` | `assertEqual` |
| Rule set | `{'/llm/get_llm_analysis'}` | `assertEqual` |
| Methods | contains `GET` | `assertIn` |

| Pass Condition | Fail Condition |
|---|---|
| Single GET rule under `/llm` | Route renamed/added or method changed |

### 2. test_dict_analysis_updates_classification_and_records_success

*Class:* `TestGetLlmAnalysis`

**Description:** Happy path with a structured LLM result. Verifies the LLM receives the prompt path, `return_usage=True` and the full case dict — including the IPS **Environment Details** (`env_detail`) answers — and that the session's `classification` and `ai_ips_analysis` are updated. Both telemetry calls are asserted with exact kwargs (`status='success'`, `error_code=''`, `domain='wifi'`).

**Code:**
```python
def test_dict_analysis_updates_classification_and_records_success(self, mock_gather):
    # Arrange
    classification = {"issue_type": "Disconnect", "confidence": 85, "keywords_found": ["drop"]}
    analysis = {"Classification": classification, "Summary": "Wi-Fi drops on resume"}
    self.llm.analyze_desc.return_value = (analysis, USAGE)
    self.seed_session(prompt_file_path=PROMPT_PATH, gather_workflow_id="wf-7")

    # Act
    resp, body = self._get()

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.mimetype, "application/json")
    self.assertEqual(body, {"success": True, "ai_analysis": analysis})
    kwargs = self.llm.analyze_desc.call_args.kwargs
    self.assertEqual(kwargs["prompt_path"], PROMPT_PATH)
    self.assertIs(kwargs["return_usage"], True)
    self.assertEqual(kwargs["case_context"]["case_nbr"], TEST_CASE_NBR)
    self.assertEqual(kwargs["case_context"]["env_detail"]["Computer Model"], "TestModel X1")
    sess = self.get_session()
    self.assertEqual(sess["classification"], classification)
    self.assertEqual(sess["ai_ips_analysis"], analysis)
    mock_gather.record_feature_usage.assert_called_once_with(
        workflow_id="wf-7", feature_code="select_attachments_ai_summary", model="test-model",
        usage=USAGE, issue=ANY, domain="wifi", trigger="click_ai", status="success",
        latency_ms=ANY, error_code="")
    mock_gather.record_attachment_declaration.assert_called_once_with(
        workflow_id="wf-7", ai_analysis=analysis, source="select_attachments_ai_summary",
        issue=ANY, domain="wifi")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `session` | `case_context=create_mock_case_context()`, `prompt_file_path=PROMPT_PATH`, `gather_workflow_id='wf-7'` | Seeded |
| `llm_helper.analyze_desc` | returns `({'Classification': {'issue_type': 'Disconnect', 'confidence': 85, 'keywords_found': ['drop']}, 'Summary': 'Wi-Fi drops on resume'}, USAGE)` | Structured result |
| `llm_helper.model` | `'test-model'` | Model name for telemetry |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / mimetype | `200` / `application/json` | `assertEqual` |
| JSON | `{'success': True, 'ai_analysis': analysis}` | `assertEqual` |
| `analyze_desc` kwargs | `prompt_path=PROMPT_PATH`, `return_usage=True`, `case_context['case_nbr']='00123456'`, `case_context['env_detail']['Computer Model']='TestModel X1'` | `assertEqual` / `assertIs` |
| `session['classification']` / `['ai_ips_analysis']` | classification dict / analysis dict | `assertEqual` |
| `gather.record_feature_usage` | `workflow_id='wf-7', feature_code='select_attachments_ai_summary', model='test-model', usage=USAGE, domain='wifi', trigger='click_ai', status='success', error_code=''` | `assert_called_once_with` (issue/latency `ANY`) |
| `gather.record_attachment_declaration` | `workflow_id='wf-7', ai_analysis=analysis, source='select_attachments_ai_summary', domain='wifi'` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Session + telemetry contract exact | env_detail dropped from the LLM input, classification not stored, or telemetry kwargs change |

### 3. test_string_analysis_keeps_default_classification

*Class:* `TestGetLlmAnalysis`

**Description:** A non-dict (plain text) LLM result is returned as-is, but `session['classification']` keeps the `Unclassified` default because only dict results carry a `Classification`. Telemetry still reports success.

**Code:**
```python
def test_string_analysis_keeps_default_classification(self, mock_gather):
    # Arrange
    self.llm.analyze_desc.return_value = ("plain text summary", USAGE)
    self.seed_session(prompt_file_path=PROMPT_PATH)

    # Act
    resp, body = self._get()

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(body["ai_analysis"], "plain text summary")
    self.assertEqual(self.get_session()["classification"], DEFAULT_CLASSIFICATION)
    self.assertEqual(mock_gather.record_feature_usage.call_args.kwargs["status"], "success")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `analyze_desc` | returns `('plain text summary', USAGE)` | Text result |
| `session['prompt_file_path']` | `PROMPT_PATH` | Seeded |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| JSON `ai_analysis` | `'plain text summary'` | `assertEqual` |
| `session['classification']` | `DEFAULT_CLASSIFICATION` | `assertEqual` |
| telemetry `status` | `'success'` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Default classification kept | Route indexes a string (TypeError → 500) |

### 4. test_empty_analysis_records_failed_status

*Class:* `TestGetLlmAnalysis`

**Description:** An empty string result is still a 200 for the UI, but telemetry records `status='failed'` with `error_code='empty_result'`.

**Code:**
```python
def test_empty_analysis_records_failed_status(self, mock_gather):
    # Arrange
    self.llm.analyze_desc.return_value = ("", USAGE)
    self.seed_session(prompt_file_path=PROMPT_PATH)

    # Act
    resp, body = self._get()

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(body, {"success": True, "ai_analysis": ""})
    kwargs = mock_gather.record_feature_usage.call_args.kwargs
    self.assertEqual(kwargs["status"], "failed")
    self.assertEqual(kwargs["error_code"], "empty_result")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `analyze_desc` | returns `('', USAGE)` | Empty result |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `200` / `{'success': True, 'ai_analysis': ''}` | `assertEqual` |
| telemetry `status` / `error_code` | `'failed'` / `'empty_result'` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Empty result flagged in telemetry | Empty result reported as success |

### 5. test_bt_case_records_bt_domain

*Class:* `TestGetLlmAnalysis`

**Description:** For a BT case (`wifi_or_bt='bt'`) both telemetry calls carry `domain='bt'` derived from the rehydrated case context.

**Code:**
```python
def test_bt_case_records_bt_domain(self, mock_gather):
    # Arrange
    self.llm.analyze_desc.return_value = ("bt summary", USAGE)
    with self.client.session_transaction() as sess:
        sess["case_context"] = self.create_mock_case_context(wifi_or_bt="bt")
        sess["prompt_file_path"] = PROMPT_PATH

    # Act
    self._get()

    # Assert
    self.assertEqual(mock_gather.record_feature_usage.call_args.kwargs["domain"], "bt")
    self.assertEqual(mock_gather.record_attachment_declaration.call_args.kwargs["domain"], "bt")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `session['case_context']` | `create_mock_case_context(wifi_or_bt='bt')` | BT case |
| `analyze_desc` | returns `('bt summary', USAGE)` | Text result |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `record_feature_usage` `domain` | `'bt'` | `assertEqual` |
| `record_attachment_declaration` `domain` | `'bt'` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Domain follows the case | Domain hard-coded to `wifi` |

### 6. test_llm_helper_unavailable_returns_placeholder

*Class:* `TestGetLlmAnalysis`

**Description:** With `app_config.llm_helper = None` the route returns the placeholder text as a successful analysis, stores it in `ai_ips_analysis`, keeps the default classification, and records no telemetry.

**Code:**
```python
def test_llm_helper_unavailable_returns_placeholder(self, mock_gather):
    # Arrange
    self.app_config.llm_helper = None
    self.seed_session()

    # Act
    resp, body = self._get()

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(body, {"success": True, "ai_analysis": "LLM helper currently not available"})
    sess = self.get_session()
    self.assertEqual(sess["classification"], DEFAULT_CLASSIFICATION)
    self.assertEqual(sess["ai_ips_analysis"], "LLM helper currently not available")
    mock_gather.record_feature_usage.assert_not_called()
    mock_gather.record_attachment_declaration.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `app_config.llm_helper` | `None` | LLM not configured |
| `session['case_context']` | `create_mock_case_context()` | Seeded |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `200` / `{'success': True, 'ai_analysis': 'LLM helper currently not available'}` | `assertEqual` |
| `session['classification']` | `DEFAULT_CLASSIFICATION` | `assertEqual` |
| `session['ai_ips_analysis']` | `'LLM helper currently not available'` | `assertEqual` |
| Both gather calls | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| Graceful placeholder, no telemetry | AttributeError on `None` or telemetry sent |

### 7. test_llm_exception_returns_500_and_records_failure

*Class:* `TestGetLlmAnalysis`

**Description:** When `analyze_desc` raises `TimeoutError('LLM timed out')`, the route returns `500 {'success': False, 'error': 'LLM timed out'}` and records a failed feature usage with the zero `empty_usage()` dict and `error_code='TimeoutError'`. The attachment declaration is not recorded and the default classification set at entry remains.

**Code:**
```python
def test_llm_exception_returns_500_and_records_failure(self, mock_gather):
    # Arrange
    self.llm.analyze_desc.side_effect = TimeoutError("LLM timed out")
    self.seed_session(prompt_file_path=PROMPT_PATH, gather_workflow_id="wf-7")

    # Act
    resp, body = self._get()

    # Assert
    self.assertEqual(resp.status_code, 500)
    self.assertEqual(body, {"success": False, "error": "LLM timed out"})
    mock_gather.record_feature_usage.assert_called_once_with(
        workflow_id="wf-7", feature_code="select_attachments_ai_summary", model="test-model",
        usage={"llm_calls": 0, "input_tokens": 0, "cache_read_tokens": 0,
               "cache_write_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        issue=ANY, domain="wifi", trigger="click_ai", status="failed",
        latency_ms=ANY, error_code="TimeoutError")
    mock_gather.record_attachment_declaration.assert_not_called()
    self.assertEqual(self.get_session()["classification"], DEFAULT_CLASSIFICATION)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `analyze_desc` | side effect `TimeoutError('LLM timed out')` | LLM failure |
| `session` | `prompt_file_path=PROMPT_PATH`, `gather_workflow_id='wf-7'` | Seeded |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `500` / `{'success': False, 'error': 'LLM timed out'}` | `assertEqual` |
| `record_feature_usage` | `workflow_id='wf-7', model='test-model', usage=<all zeros>, domain='wifi', trigger='click_ai', status='failed', error_code='TimeoutError'` | `assert_called_once_with` |
| `record_attachment_declaration` | not called | `assert_not_called` |
| `session['classification']` | `DEFAULT_CLASSIFICATION` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| 500 with error + failure telemetry | Exception escapes, 200 returned, or wrong error_code |

### 8. test_missing_prompt_file_path_returns_500

*Class:* `TestGetLlmAnalysis`

**Description:** `session['prompt_file_path']` is read with item access inside the `try`, so a missing key becomes a 500 whose `error` mentions the key; the LLM is never called and telemetry records `error_code='KeyError'`.

**Code:**
```python
def test_missing_prompt_file_path_returns_500(self, mock_gather):
    # Arrange: case_context present, prompt_file_path absent -> KeyError inside try
    self.seed_session()

    # Act
    resp, body = self._get()

    # Assert
    self.assertEqual(resp.status_code, 500)
    self.assertFalse(body["success"])
    self.assertIn("prompt_file_path", body["error"])
    self.llm.analyze_desc.assert_not_called()
    self.assertEqual(mock_gather.record_feature_usage.call_args.kwargs["error_code"], "KeyError")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `session` | `case_context` only | No `prompt_file_path` |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `500` | `assertEqual` |
| JSON | `success` False; `error` contains `'prompt_file_path'` | `assertFalse` / `assertIn` |
| `analyze_desc` | not called | `assert_not_called` |
| telemetry `error_code` | `'KeyError'` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Handled as a JSON 500 | Unhandled KeyError or LLM called without a prompt |

### 9. test_missing_case_context_still_calls_llm_with_empty_context

*Class:* `TestGetLlmAnalysis`

**Description:** Unlike most blueprints, this route uses `session.get('case_context') or {}`, so a missing case context yields an empty `CaseContext` (all `None`) and the LLM is still called; telemetry falls back to `domain='wifi'`.

**Code:**
```python
def test_missing_case_context_still_calls_llm_with_empty_context(self, mock_gather):
    # Arrange: no case_context -> from_session({}) -> empty CaseContext
    self.llm.analyze_desc.return_value = ("summary", USAGE)
    with self.client.session_transaction() as sess:
        sess["prompt_file_path"] = PROMPT_PATH

    # Act
    resp, body = self._get()

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertIsNone(self.llm.analyze_desc.call_args.kwargs["case_context"]["case_nbr"])
    self.assertEqual(mock_gather.record_feature_usage.call_args.kwargs["domain"], "wifi")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `session` | `prompt_file_path` only | No `case_context` |
| `analyze_desc` | returns `('summary', USAGE)` | Text result |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| `analyze_desc` `case_context['case_nbr']` | `None` | `assertIsNone` |
| telemetry `domain` | `'wifi'` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Empty context tolerated | KeyError / AttributeError on missing context |

### 10. test_telemetry_failure_is_swallowed

*Class:* `TestGetLlmAnalysis`

**Description:** If `record_feature_usage` raises, the analysis response is still 200; because both telemetry calls share one `try`, the declaration call is skipped.

**Code:**
```python
def test_telemetry_failure_is_swallowed(self, mock_gather):
    # Arrange
    self.llm.analyze_desc.return_value = ("summary", USAGE)
    mock_gather.record_feature_usage.side_effect = RuntimeError("gather down")
    self.seed_session(prompt_file_path=PROMPT_PATH)

    # Act
    resp, body = self._get()

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(body, {"success": True, "ai_analysis": "summary"})
    mock_gather.record_attachment_declaration.assert_not_called()  # same try block, skipped
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `analyze_desc` | returns `('summary', USAGE)` | Text result |
| `record_feature_usage` | side effect `RuntimeError('gather down')` | Telemetry down |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `200` / `{'success': True, 'ai_analysis': 'summary'}` | `assertEqual` |
| `record_attachment_declaration` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| Telemetry failure invisible to the user | Telemetry error turns into a 500 |

## Test Execution Flow

```
setUpClass ── patch app_config; rebind blueprints.llm.llm_routes.app_config (identity match)
     │
     ▼
setUp ── reset mock (llm_helper=MagicMock) ── patch CaseContext.to_session ── fresh app ── register llm_bp
      ── client + app ctx ── TestGetLlmAnalysis: self.llm = app_config.llm_helper; self.llm.model = "test-model"
     │
     ▼
test (class @patch gather_service):
   Arrange: analyze_desc.return_value / side_effect; seed_session(prompt_file_path, gather_workflow_id) or llm_helper=None
   Act:     resp, body = self._get()                     (GET /llm/get_llm_analysis)
   Assert:  status, JSON body, analyze_desc kwargs, session['classification'/'ai_ips_analysis'], gather calls
     │
     ▼
tearDown ──► PASS / FAIL ── tearDownClass stops patchers
```

## Coverage Summary

| Metric | Value |
|---|---|
| Total Routes | 1 |
| Routes Tested | 1 |
| Total Tests | 10 (1 structure + 9 route) |
| Actual Coverage (%) | 100% of routes; all branches (helper None/set, dict/str/empty result, LLM exception, missing prompt, missing context, telemetry failure, BT domain) |
| Out-of-scope items | 2 (sidecar rehydration file IO, exception inside the except-branch telemetry) |

## What Would Make These Tests Fail?

| Source change | ✅ PASS | ❌ FAIL |
|---|---|---|
| Stop passing the full case dict (e.g. drop `env_detail`) to `analyze_desc` | other tests | `test_dict_analysis_updates_classification_and_records_success` |
| Store classification for non-dict results | dict test | `test_string_analysis_keeps_default_classification` (TypeError → 500) |
| Report empty results as success | other tests | `test_empty_analysis_records_failed_status` |
| Hard-code telemetry domain to `wifi` | Wi-Fi tests | `test_bt_case_records_bt_domain` |
| Send telemetry when `llm_helper` is `None` | other tests | `test_llm_helper_unavailable_returns_placeholder` |
| Remove the outer `try/except` | success tests | `test_llm_exception_returns_500_and_records_failure`, `test_missing_prompt_file_path_returns_500` |
| Use `session['case_context']` (item access) | other tests | `test_missing_case_context_still_calls_llm_with_empty_context` |
| Let telemetry exceptions propagate | other tests | `test_telemetry_failure_is_swallowed` |
| Rename `feature_code` / `source` strings | non-telemetry tests | dict-result and exception tests |
