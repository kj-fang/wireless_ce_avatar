# BSOD Blueprint — Analysis & Unit Test Documentation

- Route module: [blueprints/bsod/bsod.py](../blueprints/bsod/bsod.py)
- Test module: [unit_tests/test_bsod_blueprint.py](../unit_tests/test_bsod_blueprint.py)
- Shared fixture: [unit_tests/base_test.py](../unit_tests/base_test.py)

## Overview — What BSOD Does

The `bsod` blueprint (`url_prefix="/bsod"`) submits a Blue-Screen-Of-Death dump-analysis request for the current IPS case to the external PotatoFarm service. It reads the case's dump folder (`session['download_path']`) and the user's e-mail (auto-detected), POSTs them as JSON with the API token from `app_config.key`, and always ends by redirecting back to the BSOD download-result page (`main.download_result_bsod`) with a success or error flash message. The analysis result itself is delivered to the user by e-mail, not by this app.

## All Routes (1 endpoint)

| Route | Method | Purpose |
|---|---|---|
| `/bsod/bsod_submit` | GET, POST | Build `{DUMPS_PATH, EMAIL_LIST}` payload, POST it to `key.potatofarm_url`, flash the outcome and redirect to `main.download_result_bsod` |

## High-Level Flow Diagram

```
 Browser ──GET/POST /bsod/bsod_submit──►┌───────────────────────────────────┐
                                        │ session["case_context"]           │── missing ──► KeyError (500)
                                        │ CaseContext.from_session(...)     │
                                        └────────────────┬──────────────────┘
                                                         ▼
                                        ┌───────────────────────────────────┐
                                        │ helpers.detect_user_email()       │── None ──► 200 "❌ Unable to retrieve email…"
                                        └────────────────┬──────────────────┘
                                                         ▼
                                        ┌───────────────────────────────────┐
                                        │ bsod_nbr = backend_id or case_nbr │  (computed but never used — dead code)
                                        │ dumps_path = session[download_path]│── "" ──► 400 "Session expired…"
                                        └────────────────┬──────────────────┘
                                                         ▼
                                        ┌───────────────────────────────────┐
                                        │ key = app_config.key              │
                                        │ requests.post(key.potatofarm_url, │
                                        │   data=json{DUMPS_PATH,EMAIL_LIST}│
                                        │   headers{X-Api-Token}, proxies   │
                                        │   None, verify=False)             │
                                        └───┬──────────────┬────────────┬───┘
                                    200     │      non-200 │            │ RequestException
                                            ▼              ▼            ▼
                              flash(success)   flash(error,"HTTP n")   flash(error, e)
                                            └──────────────┼────────────┘
                                                           ▼
                                        302 → url_for('main.download_result_bsod')
```

## Dependencies

```
flask:        Blueprint, request, session, redirect, url_for, flash
stdlib:       json
third-party:  requests            (requests.post, requests.exceptions.RequestException)
helpers:      utils.helpers.detect_user_email()
app_config:   app_config.key.potatofarm_api   (X-Api-Token header)
              app_config.key.potatofarm_url   (POST target)
models:       models.models.CaseContext.from_session
session keys: case_context (required), download_path (required)
cross-bp:     url_for('main.download_result_bsod')  → stubbed 'main' blueprint in tests
```

## Test Coverage Analysis

```
STRUCTURE
  ✅ blueprint 'bsod' registered, url_prefix '/bsod', GET+POST rule ── test_blueprint_registered_with_url_prefix

ROUTES — /bsod/bsod_submit
  ✅ success (HTTP 200) → POST payload/headers/proxies/verify, success flash, redirect ── test_submit_success_posts_payload_and_redirects
  ✅ GET method also accepted                                            ── test_submit_via_get_is_also_accepted
  ✅ email not detected → message, API not called                       ── test_missing_email_returns_message_without_calling_api
  ✅ download_path missing → 400 "Session expired"                       ── test_missing_download_path_returns_400
  ✅ non-200 response → error flash with HTTP code, body not leaked      ── test_non_200_response_flashes_error_with_status
  ✅ RequestException → error flash with exception text, redirect       ── test_request_exception_flashes_error_and_redirects
  ✅ case_context missing → unhandled KeyError                           ── test_missing_case_context_raises_key_error

CORE LOGIC
  ⚪ bsod_nbr = backend_id / case_nbr selection ── dead code: computed but never used in the payload; nothing observable to assert

BACKGROUND / SOCKETIO
  ⚪ none ── this blueprint has no SocketIO handlers or threads
```

## What the Unit Tests Actually Test

Fixture used by every test in `TestBsodSubmit`: `blueprint = bsod_bp`, `main_stub_endpoints = ("download_result_bsod",)`, and in `setUp`: `app_config.key.potatofarm_url = "https://potatofarm.example/api/bsod"`, `app_config.key.potatofarm_api = "potato-token"`. Module constants: `USER_EMAIL = "tester@example.com"`, `DUMPS_PATH = r"C:\fake\dumps\00123456"`.

### 1. test_blueprint_registered_with_url_prefix

**Description:** Verifies that registering `bsod_bp` on a fresh app exposes it under the name `bsod` with `url_prefix="/bsod"`, and that `/bsod/bsod_submit` accepts both GET and POST. Catches renamed blueprints, changed prefixes, or narrowed HTTP methods that would break the front end's form/links.

**Code:**
```python
def test_blueprint_registered_with_url_prefix(self):
    # Arrange / Act
    registered = self.app.blueprints.get("bsod")
    rules = {r.rule: r for r in self.app.url_map.iter_rules() if r.endpoint.startswith("bsod.")}

    # Assert
    self.assertIs(registered, bsod_bp)
    self.assertEqual(bsod_bp.url_prefix, "/bsod")
    self.assertIn("/bsod/bsod_submit", rules)
    self.assertTrue({"GET", "POST"}.issubset(rules["/bsod/bsod_submit"].methods))
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Registered blueprint | `bsod_bp` | Registered in `BaseTestCase.setUp` |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `app.blueprints["bsod"]` | `bsod_bp` | `assertIs` |
| `bsod_bp.url_prefix` | `"/bsod"` | `assertEqual` |
| URL rule | `/bsod/bsod_submit` | `assertIn` |
| Allowed methods | ⊇ `{"GET","POST"}` | `assertTrue(issubset)` |

| Pass Condition | Fail Condition |
|---|---|
| Blueprint name, prefix, rule and methods all match | Blueprint renamed, prefix changed, route removed, or GET/POST dropped |

### 2. test_missing_case_context_raises_key_error

**Description:** Documents current behavior: the route reads `session["case_context"]` with item access, so an empty session raises `KeyError` before anything else runs (a 500 in production; Flask's `TESTING=True` propagates it to the test). Also verifies no e-mail lookup or API call happens. This pins down existing behavior rather than endorsing it.

**Code:**
```python
@patch(f"{ROUTES}.requests.post")
@patch(f"{ROUTES}.helpers.detect_user_email", return_value=USER_EMAIL)
def test_missing_case_context_raises_key_error(self, mock_email, mock_post):
    # Arrange: empty session. The route indexes session["case_context"]
    # directly, so this is an unhandled KeyError (a 500 in production).
    # Act / Assert
    with self.assertRaises(KeyError):
        self.client.post("/bsod/bsod_submit")
    mock_email.assert_not_called()
    mock_post.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Session | *(empty)* | No `case_context` key |
| `helpers.detect_user_email` | patched → `"tester@example.com"` | Would succeed if reached |
| `requests.post` | patched MagicMock | Must not be reached |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Exception | `KeyError` | `assertRaises(KeyError)` |
| E-mail lookup | not called | `mock_email.assert_not_called()` |
| API call | not called | `mock_post.assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| `KeyError` propagates and no external call is made | Route starts tolerating a missing session (returns a response) or calls helpers first |

### 3. test_missing_download_path_returns_400

**Description:** With a valid case context but no `download_path` in session, the route must stop with HTTP 400 "Session expired" and never call the PotatoFarm API. This guards against submitting an empty `DUMPS_PATH`.

**Code:**
```python
@patch(f"{ROUTES}.requests.post")
@patch(f"{ROUTES}.helpers.detect_user_email", return_value=USER_EMAIL)
def test_missing_download_path_returns_400(self, mock_email, mock_post):
    # Arrange: case_context present, but no download_path in session
    self.seed_session()

    # Act
    resp = self.client.post("/bsod/bsod_submit")

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertIn("Session expired", resp.get_data(as_text=True))
    mock_post.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `session["case_context"]` | `create_mock_case_context()` | `case_nbr="00123456"`, `backend_id=""` |
| `session["download_path"]` | *(absent)* | Triggers the guard |
| `helpers.detect_user_email` | `"tester@example.com"` | E-mail check passes |
| `requests.post` | patched MagicMock | Must not be reached |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` | `assertEqual` |
| Body substring | `"Session expired"` | `assertIn` |
| API call | not called | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| 400 + message, no POST | Guard removed (POST with empty path) or status/message changed |

### 4. test_missing_email_returns_message_without_calling_api

**Description:** When `detect_user_email()` returns `None`, the route returns the plain-text message "❌ Unable to retrieve email. Please enter manually." (HTTP 200) and does not call the API. Verifies the e-mail guard runs before the network call.

**Code:**
```python
@patch(f"{ROUTES}.requests.post")
@patch(f"{ROUTES}.helpers.detect_user_email", return_value=None)
def test_missing_email_returns_message_without_calling_api(self, mock_email, mock_post):
    # Arrange
    self.seed_session(download_path=DUMPS_PATH)

    # Act
    resp = self.client.post("/bsod/bsod_submit")

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertIn("Unable to retrieve email", resp.get_data(as_text=True))
    mock_post.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `session["case_context"]` | `create_mock_case_context()` | Valid case |
| `session["download_path"]` | `r"C:\fake\dumps\00123456"` | Valid path |
| `helpers.detect_user_email` | `None` | E-mail detection fails |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| Body substring | `"Unable to retrieve email"` | `assertIn` |
| API call | not called | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| Message returned, no POST | POST sent with `EMAIL_LIST=None`, or message changed |

### 5. test_non_200_response_flashes_error_with_status

**Description:** A non-200 API response must redirect to the BSOD result page with an `error` flash that includes the HTTP status, while the raw response body (`"upstream down"`) stays in the server log only. Verifies the "don't leak upstream detail to the user" rule.

**Code:**
```python
@patch(f"{ROUTES}.requests.post")
@patch(f"{ROUTES}.helpers.detect_user_email", return_value=USER_EMAIL)
def test_non_200_response_flashes_error_with_status(self, mock_email, mock_post):
    # Arrange
    mock_post.return_value = MagicMock(status_code=503, text="upstream down")
    self.seed_session(download_path=DUMPS_PATH)

    # Act
    resp = self.client.post("/bsod/bsod_submit")

    # Assert
    self.assertEqual(resp.status_code, 302)
    self.assertTrue(resp.headers["Location"].endswith(self.main_stub_path("download_result_bsod")))
    category, message = self._flashes()[0]
    self.assertEqual(category, "error")
    self.assertIn("HTTP 503", message)
    self.assertNotIn("upstream down", message)  # body is logged, never shown to the user
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `session["download_path"]` | `r"C:\fake\dumps\00123456"` | Valid |
| `requests.post` return | `MagicMock(status_code=503, text="upstream down")` | Upstream failure |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `302` | `assertEqual` |
| `Location` | ends with `/__main_stub__/download_result_bsod` | `assertTrue(endswith)` |
| Flash category | `"error"` | `assertEqual` |
| Flash message | contains `"HTTP 503"`, not `"upstream down"` | `assertIn` / `assertNotIn` |

| Pass Condition | Fail Condition |
|---|---|
| Error flash with status code only, redirect | Body leaked into flash, success flash shown, or no redirect |

### 6. test_request_exception_flashes_error_and_redirects

**Description:** A `requests` network exception (`ConnectionError("boom")`) is caught, flashed as an `error` with the exception text, and the user is still redirected. Verifies the route never 500s on network failures.

**Code:**
```python
@patch(f"{ROUTES}.requests.post", side_effect=requests.exceptions.ConnectionError("boom"))
@patch(f"{ROUTES}.helpers.detect_user_email", return_value=USER_EMAIL)
def test_request_exception_flashes_error_and_redirects(self, mock_email, mock_post):
    # Arrange
    self.seed_session(download_path=DUMPS_PATH)

    # Act
    resp = self.client.post("/bsod/bsod_submit")

    # Assert
    self.assertEqual(resp.status_code, 302)
    category, message = self._flashes()[0]
    self.assertEqual(category, "error")
    self.assertIn("An error occurred during API call: boom", message)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `requests.post` side effect | `requests.exceptions.ConnectionError("boom")` | Network failure |
| `session["download_path"]` | `r"C:\fake\dumps\00123456"` | Valid |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `302` | `assertEqual` |
| Flash | `("error", "An error occurred during API call: boom")` | `assertEqual` / `assertIn` |

| Pass Condition | Fail Condition |
|---|---|
| Exception handled → error flash + redirect | Exception escapes (500) or flash text changes |

### 7. test_submit_success_posts_payload_and_redirects

**Description:** The happy path. Verifies the exact contract with PotatoFarm: positional URL from `key.potatofarm_url`, JSON body `{"DUMPS_PATH", "EMAIL_LIST"}`, `X-Api-Token` header, proxies disabled, TLS verify off; then a `success` flash and redirect to `main.download_result_bsod`.

**Code:**
```python
@patch(f"{ROUTES}.requests.post")
@patch(f"{ROUTES}.helpers.detect_user_email", return_value=USER_EMAIL)
def test_submit_success_posts_payload_and_redirects(self, mock_email, mock_post):
    # Arrange
    mock_post.return_value = MagicMock(status_code=200)
    self.seed_session(download_path=DUMPS_PATH)

    # Act
    resp = self.client.post("/bsod/bsod_submit")

    # Assert
    self.assertEqual(resp.status_code, 302)
    self.assertTrue(resp.headers["Location"].endswith(self.main_stub_path("download_result_bsod")))
    mock_email.assert_called_once_with()
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    self.assertEqual(args, (API_URL,))
    self.assertEqual(json.loads(kwargs["data"]), {"DUMPS_PATH": DUMPS_PATH, "EMAIL_LIST": USER_EMAIL})
    self.assertEqual(kwargs["headers"], {"Content-Type": "application/json", "X-Api-Token": API_TOKEN})
    self.assertEqual(kwargs["proxies"], {"http": None, "https": None})
    self.assertIs(kwargs["verify"], False)
    self.assertEqual(self._flashes()[0][0], "success")
    self.assertIn("Submitted Successfully", self._flashes()[0][1])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `session["case_context"]` | `create_mock_case_context()` | Valid case |
| `session["download_path"]` | `r"C:\fake\dumps\00123456"` | Dump folder |
| `helpers.detect_user_email` | `"tester@example.com"` | Detected e-mail |
| `app_config.key.potatofarm_url` | `"https://potatofarm.example/api/bsod"` | API URL |
| `app_config.key.potatofarm_api` | `"potato-token"` | API token |
| `requests.post` return | `MagicMock(status_code=200)` | Upstream OK |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / `Location` | `302` → `/__main_stub__/download_result_bsod` | `assertEqual` / `endswith` |
| POST positional args | `("https://potatofarm.example/api/bsod",)` | `assertEqual` |
| POST `data` (decoded) | `{"DUMPS_PATH": "C:\\fake\\dumps\\00123456", "EMAIL_LIST": "tester@example.com"}` | `assertEqual` |
| POST `headers` | `{"Content-Type": "application/json", "X-Api-Token": "potato-token"}` | `assertEqual` |
| POST `proxies` / `verify` | `{"http": None, "https": None}` / `False` | `assertEqual` / `assertIs` |
| Flash | category `"success"`, text contains `"Submitted Successfully"` | `assertEqual` / `assertIn` |

| Pass Condition | Fail Condition |
|---|---|
| Exact payload/headers sent once; success flash; redirect | Any payload key renamed, token header changed, proxies re-enabled, or redirect target changed |

### 8. test_submit_via_get_is_also_accepted

**Description:** The route is declared with `methods=['POST', 'GET']`; this verifies a GET still triggers the submission (one API call, 302). Guards against silently dropping GET support used by links.

**Code:**
```python
@patch(f"{ROUTES}.requests.post")
@patch(f"{ROUTES}.helpers.detect_user_email", return_value=USER_EMAIL)
def test_submit_via_get_is_also_accepted(self, mock_email, mock_post):
    # Arrange
    mock_post.return_value = MagicMock(status_code=200)
    self.seed_session(download_path=DUMPS_PATH)

    # Act
    resp = self.client.get("/bsod/bsod_submit")

    # Assert
    self.assertEqual(resp.status_code, 302)
    mock_post.assert_called_once()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| HTTP method | `GET` | Alternative verb |
| `session["download_path"]` | `r"C:\fake\dumps\00123456"` | Valid |
| `requests.post` return | `MagicMock(status_code=200)` | Upstream OK |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `302` | `assertEqual` |
| API call | exactly once | `assert_called_once()` |

| Pass Condition | Fail Condition |
|---|---|
| GET behaves like POST | GET returns 405 or skips the API call |

## Test Execution Flow

```
setUpClass (BaseTestCase)
  ├─ capture real app_config
  ├─ patch configs.global_configs.app_config → MagicMock
  └─ repoint every module whose app_config IS the real object (incl. blueprints.bsod.bsod)
        │
        ▼
for each test method:
  setUp
    ├─ reset mock app_config (silent_mode=False, key/socketio/llm_helper/driver_manager mocks)
    ├─ patch CaseContext.to_session (no sidecar file writes)
    ├─ fresh Flask app (TESTING, SECRET_KEY, WTF_CSRF_ENABLED=False)
    ├─ register bsod_bp
    ├─ register stub Blueprint('main') with endpoint 'download_result_bsod'   (TestBsodSubmit only)
    ├─ TestBsodSubmit.setUp: key.potatofarm_url / key.potatofarm_api
    └─ test client + push app context
        │
        ▼
  test body: @patch requests.post / helpers.detect_user_email
             seed_session(case_context, download_path)
             Arrange → Act (client.get/post) → Assert
        │
        ▼
  tearDown: pop app context, stop to_session patch ──► PASS / FAIL
        │
        ▼
tearDownClass: stop all app_config patchers (real singleton restored)
```

## Coverage Summary

| Metric | Value |
|---|---|
| Total Routes | 1 |
| Routes Tested | 1 |
| Total Tests | 8 (1 structure + 7 route) |
| Actual Coverage (%) | 100% of routes; 6/6 observable branches (success, GET, no e-mail, no download_path, non-200, RequestException) + missing-session behavior |
| Out-of-scope items | 1 (unused `bsod_nbr` computation — dead code) |

## What Would Make These Tests Fail?

| Source change | ✅ PASS | ❌ FAIL |
|---|---|---|
| Rename payload key `DUMPS_PATH` / `EMAIL_LIST` | all others | `test_submit_success_posts_payload_and_redirects` |
| Change `X-Api-Token` header or stop disabling proxies / `verify=False` | all others | `test_submit_success_posts_payload_and_redirects` |
| Redirect to a different endpoint than `main.download_result_bsod` | structure, missing-session, no-e-mail, no-download_path tests | success, GET, non-200, RequestException tests (`url_for` raises `BuildError` against the stub `main`) |
| Remove the `download_path` guard | all others | `test_missing_download_path_returns_400` |
| Include `response.text` in the user flash | all others | `test_non_200_response_flashes_error_with_status` |
| Stop catching `RequestException` | all others | `test_request_exception_flashes_error_and_redirects` |
| Use `session.get("case_context")` and handle `None` | all others | `test_missing_case_context_raises_key_error` |
| Drop `GET` from `methods` | all others | `test_submit_via_get_is_also_accepted`, `test_blueprint_registered_with_url_prefix` |
| Change `url_prefix` from `/bsod` | — | every test (structure assertion fails; route tests get 404 / no KeyError) |
| Use `bsod_nbr` in the payload | — | `test_submit_success_posts_payload_and_redirects` (payload equality) |
