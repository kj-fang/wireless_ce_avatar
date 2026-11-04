# Download Blueprint — Analysis & Unit Test Documentation

- Route module: [blueprints/download/download_routes.py](../blueprints/download/download_routes.py)
- Test module: [tests/test_download_blueprint.py](../tests/test_download_blueprint.py)
- Shared fixture: [tests/base_test.py](../tests/base_test.py)

## Overview — What Download Does

The `download` blueprint (`url_prefix="/download"`) drives attachment downloads for the current IPS case. Most of its work is SocketIO-driven: `start_download` (namespace `/progress`) launches a background thread that downloads the user's selected attachments through the Selenium-backed `attachment_download.run_dload_threads`, decomposes each ZIP into Wi-Fi / DDD+EVT / BT / FW file lists, stores them in `app_config.set_download_results(case_nbr, …)` and emits `all_attachments_download_done` (or `all_attachments_download_done_bsod` for BSOD dumps). Cancellation is available over SocketIO (`cancel_download`) and over plain HTTP (`POST /download/cancel_download`, fired by `navigator.sendBeacon` when the page unloads). Per-file download telemetry is forwarded to `gather_service`.

## All Routes (1 endpoint)

| Route | Method | Purpose |
|---|---|---|
| `/download/cancel_download` | POST | Cancel all in-flight downloads (`driver_manager.cancel_downloads()`); returns `204 No Content` |

SocketIO events registered by `register_socketio_handlers(socketio)` (not HTTP routes, tested as core logic):

| Event | Namespace | Handler |
|---|---|---|
| `start_download` | `/progress` | `handle_start_download(socketio)` |
| `cancel_download` | `/progress` | `handle_cancel_download(socketio, request.sid)` |

## High-Level Flow Diagram

```
POST /download/cancel_download ──► app_config.driver_manager.cancel_downloads() ──► 204 ''

SocketIO 'cancel_download' ──► handle_cancel_download(socketio, sid)
      driver_manager.cancel_downloads()
      socketio.emit('download_cancelled', {}, namespace='/progress', to=sid)

SocketIO 'start_download' ──► handle_start_download(socketio)
  driver_manager.download_cancel_event.clear()
  session: selected_files, download_path, case_context (→ CaseContext.from_session), bsod, gather_workflow_id
  domain = case_context.wifi_or_bt or 'wifi'
  Thread(target=background_download, daemon=False).start()
        │
        ▼  background_download
  for (file_path, name, already) in attachment_download.run_dload_threads(files, path, socketio,
                                                                          result_callback=record_download_result):
        │     record_download_result(result) ──► gather_service.record_attachment_download_result(...)
        │                                         (exceptions swallowed)
        └─ not bsod ──► attachment_decompose.process_single_zip(file_path, path, already)
                         wifi[name], ddd[name]=ddd+evt, bt[name], fw[name]
        │
        ├─ download_cancel_event.is_set() ──► cleanup_incomplete_downloads(path); return (no emit)
        ├─ not bsod ──► app_config.set_download_results(case_nbr, wifi, ddd, bt, fw)
        │               emit('all_attachments_download_done', {wifi_dict, ddd_dict, bt_dict, fw_dict})
        └─ bsod     ──► emit('all_attachments_download_done_bsod', {'dump_path': last file_path})
```

## Dependencies

```
flask:        Blueprint, request (request.sid in the SocketIO wrapper), session
stdlib:       threading.Thread                  (patched with a synchronous stand-in in tests)
utils:        attachment_download.run_dload_threads, attachment_download.cleanup_incomplete_downloads
              attachment_decompose.process_single_zip
services:     gather_service.record_attachment_download_result   (telemetry)
app_config:   app_config.driver_manager.cancel_downloads()
              app_config.driver_manager.download_cancel_event (.clear / .is_set)
              app_config.set_download_results(case_nbr, wifi=, ddd=, bt=, fw=)
models:       models.models.CaseContext.from_session / to_dict
session keys: selected_files, download_path, case_context, bsod, gather_workflow_id
```

## Test Coverage Analysis

```
STRUCTURE
  ✅ blueprint registered, url_prefix, single POST-only rule        ── test_blueprint_registered_with_url_prefix
  ✅ SocketIO events bound on /progress                              ── test_register_socketio_handlers_binds_progress_events

ROUTES — /download/cancel_download
  ✅ POST → 204, driver_manager.cancel_downloads()                   ── test_cancel_download_returns_204_and_cancels
  ✅ GET → 405, nothing cancelled                                    ── test_cancel_download_rejects_get

CORE LOGIC / SOCKETIO HANDLERS
  ✅ handle_cancel_download: cancel + emit to requesting sid         ── test_cancels_and_emits_to_requesting_client
  ✅ start: Wi-Fi decompose → set_download_results + done emit       ── test_wifi_download_decomposes_zip_stores_results_and_emits
  ✅ start: BSOD → no decompose, *_bsod emit with dump_path           ── test_bsod_download_skips_decompose_and_emits_dump_path
  ✅ start: cancelled → cleanup, no results, no emit                  ── test_cancelled_download_cleans_up_and_emits_nothing
  ✅ start: no files → empty dicts stored and emitted                 ── test_no_files_emits_empty_dicts
  ✅ result_callback → gather telemetry kwargs                        ── test_result_callback_records_gather_telemetry
  ✅ result_callback defaults + telemetry exception swallowed         ── test_result_callback_defaults_and_swallows_errors
  ✅ case_context missing → empty CaseContext (case_nbr None)         ── test_missing_case_context_falls_back_to_empty_context

BACKGROUND
  ⚪ real threading / Selenium downloads inside run_dload_threads ── utils layer; Thread replaced by a synchronous
     stand-in and run_dload_threads mocked, so ordering and timing races are not exercised
  ⚪ socketio_start_download / socketio_cancel_download closures ── thin wrappers; registration verified, bodies
     delegate to the handlers tested directly
```



## What the Unit Tests Actually Test

`TestHandleStartDownload` patches at class level: `Thread` → `_SyncThread` (runs `target(*args)` on `start()` and records instances), `attachment_download`, `attachment_decompose`, `gather_service`. Its `setUp` creates `self.socketio = MagicMock()` and sets `driver_manager.download_cancel_event.is_set.return_value = False`. The helper `_run(**session_values)` opens `app.test_request_context("/")`, fills the session with defaults `selected_files=[["logs.zip", "https://ips.example/logs.zip"]]`, `download_path=r"C:\fake\downloads\00123456"`, `case_context=create_mock_case_context()`, `gather_workflow_id="wf-1"` (overridable), then calls `handle_start_download(self.socketio)`. `ZIP_PATH = r"C:\fake\downloads\00123456\logs.zip"`; `TEST_CASE_NBR = "00123456"`.

### 1. test_blueprint_registered_with_url_prefix

*Class:* `TestDownloadBlueprintStructure`

**Description:** Verifies the blueprint name, `url_prefix='/download'`, that `/download/cancel_download` is its only HTTP rule, and that it is POST-only (it is called by `navigator.sendBeacon`, which always POSTs).

**Code:**
```python
def test_blueprint_registered_with_url_prefix(self):
    # Arrange / Act
    rules = {r.rule: r.methods for r in self.app.url_map.iter_rules()
             if r.endpoint.startswith("download.")}

    # Assert
    self.assertIs(self.app.blueprints.get("download"), download_bp)
    self.assertEqual(download_bp.url_prefix, "/download")
    self.assertEqual(set(rules), {"/download/cancel_download"})
    self.assertIn("POST", rules["/download/cancel_download"])
    self.assertNotIn("GET", rules["/download/cancel_download"])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Registered blueprint | `download_bp` | From `BaseTestCase.setUp` |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `app.blueprints['download']` | `download_bp` | `assertIs` |
| `url_prefix` | `'/download'` | `assertEqual` |
| Rule set | `{'/download/cancel_download'}` | `assertEqual` |
| Methods | has `POST`, not `GET` | `assertIn` / `assertNotIn` |

| Pass Condition | Fail Condition |
|---|---|
| Exactly one POST-only rule under `/download` | Prefix/route/method changed or a route added |

### 2. test_register_socketio_handlers_binds_progress_events

*Class:* `TestDownloadBlueprintStructure`

**Description:** Calls `register_socketio_handlers` with a mock SocketIO and checks it registers `start_download` and `cancel_download` on the `/progress` namespace, in that order. The front end emits these exact event names.

**Code:**
```python
def test_register_socketio_handlers_binds_progress_events(self):
    # Arrange
    socketio = MagicMock()

    # Act
    register_socketio_handlers(socketio)

    # Assert
    self.assertEqual(socketio.on.call_args_list, [
        call("start_download", namespace="/progress"),
        call("cancel_download", namespace="/progress"),
    ])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `socketio` | `MagicMock()` | Records `.on(...)` calls |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `socketio.on.call_args_list` | `[call('start_download', namespace='/progress'), call('cancel_download', namespace='/progress')]` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Both events bound on `/progress` | Event renamed, namespace changed, or registration dropped |

### 3. test_cancel_download_returns_204_and_cancels

*Class:* `TestCancelDownloadRoute`

**Description:** POSTing to the HTTP cancel endpoint calls `app_config.driver_manager.cancel_downloads()` once and returns an empty `204`.

**Code:**
```python
def test_cancel_download_returns_204_and_cancels(self):
    # Act
    resp = self.client.post("/download/cancel_download")

    # Assert
    self.assertEqual(resp.status_code, 204)
    self.assertEqual(resp.get_data(), b"")
    self.app_config.driver_manager.cancel_downloads.assert_called_once_with()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Request | `POST /download/cancel_download` | sendBeacon on page unload |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `204` | `assertEqual` |
| Body | `b''` | `assertEqual` |
| `driver_manager.cancel_downloads` | called once, no args | `assert_called_once_with()` |

| Pass Condition | Fail Condition |
|---|---|
| Downloads cancelled, 204 returned | Not cancelled or non-empty / non-204 response |

### 4. test_cancel_download_rejects_get

*Class:* `TestCancelDownloadRoute`

**Description:** A GET (e.g. a prefetch or a stray link) must not cancel downloads: Flask returns 405 and the driver manager is untouched.

**Code:**
```python
def test_cancel_download_rejects_get(self):
    # Act
    resp = self.client.get("/download/cancel_download")

    # Assert
    self.assertEqual(resp.status_code, 405)
    self.app_config.driver_manager.cancel_downloads.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Request | `GET /download/cancel_download` | Wrong method |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `405` | `assertEqual` |
| `driver_manager.cancel_downloads` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| 405 and nothing cancelled | GET accepted and cancels downloads |

### 5. test_cancels_and_emits_to_requesting_client

*Class:* `TestHandleCancelDownload`

**Description:** `handle_cancel_download` cancels downloads and emits `download_cancelled` only to the requesting client's sid on `/progress` (not broadcast).

**Code:**
```python
def test_cancels_and_emits_to_requesting_client(self):
    # Arrange
    socketio = MagicMock()

    # Act
    handle_cancel_download(socketio, "sid-123")

    # Assert
    self.app_config.driver_manager.cancel_downloads.assert_called_once_with()
    socketio.emit.assert_called_once_with("download_cancelled", {}, namespace="/progress", to="sid-123")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `socketio` | `MagicMock()` | Emit sink |
| `client_sid` | `'sid-123'` | Requesting client |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `driver_manager.cancel_downloads` | called once | `assert_called_once_with()` |
| `socketio.emit` | `('download_cancelled', {}, namespace='/progress', to='sid-123')` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Cancel + targeted emit | Broadcast to all clients or emit missing |

### 6. test_wifi_download_decomposes_zip_stores_results_and_emits

*Class:* `TestHandleStartDownload`

**Description:** Main path for a non-BSOD case. Verifies: the cancel event is cleared; exactly one non-daemon worker thread is started; `run_dload_threads` receives the session's files, path, socketio and a callable `result_callback`; each ZIP is decomposed; DDD and EVT lists are merged under `ddd`; results are stored via `set_download_results(case_nbr, …)` and emitted as `all_attachments_download_done`.

**Code:**
```python
def test_wifi_download_decomposes_zip_stores_results_and_emits(self, mock_dl, mock_dec, mock_gather):
    # Arrange
    mock_dl.run_dload_threads.return_value = iter([(ZIP_PATH, "logs.zip", False)])
    mock_dec.process_single_zip.return_value = (["w.etl"], ["d.etl"], ["e.evtx"], ["b.etl"], ["f.bin"])

    # Act
    self._run()

    # Assert
    self.app_config.driver_manager.download_cancel_event.clear.assert_called_once_with()
    self.assertEqual(len(_SyncThread.instances), 1)
    self.assertIs(_SyncThread.instances[0].daemon, False)
    args, kwargs = mock_dl.run_dload_threads.call_args
    self.assertEqual(args, (SELECTED_FILES, DOWNLOAD_PATH, self.socketio))
    self.assertTrue(callable(kwargs["result_callback"]))
    mock_dec.process_single_zip.assert_called_once_with(ZIP_PATH, DOWNLOAD_PATH, False)
    expected = {
        "wifi": {"logs.zip": ["w.etl"]},
        "ddd": {"logs.zip": ["d.etl", "e.evtx"]},  # ddd + evt merged
        "bt": {"logs.zip": ["b.etl"]},
        "fw": {"logs.zip": ["f.bin"]},
    }
    self.app_config.set_download_results.assert_called_once_with(TEST_CASE_NBR, **expected)
    self.socketio.emit.assert_called_once_with(
        "all_attachments_download_done",
        {"wifi_dict": expected["wifi"], "ddd_dict": expected["ddd"],
         "bt_dict": expected["bt"], "fw_dict": expected["fw"]},
        namespace="/progress")
    mock_dl.cleanup_incomplete_downloads.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `run_dload_threads` | returns `iter([(ZIP_PATH, 'logs.zip', False)])` | One downloaded ZIP |
| `process_single_zip` | returns `(['w.etl'], ['d.etl'], ['e.evtx'], ['b.etl'], ['f.bin'])` | Decomposed lists |
| Session | `_run()` defaults | Wi-Fi case, no `bsod` |
| `download_cancel_event.is_set` | `False` | Not cancelled |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `download_cancel_event.clear` | called once | `assert_called_once_with()` |
| Threads started | 1, `daemon=False` | `assertEqual` / `assertIs` |
| `run_dload_threads` args | `(SELECTED_FILES, DOWNLOAD_PATH, socketio)` + callable `result_callback` | `assertEqual` / `assertTrue(callable)` |
| `process_single_zip` | `(ZIP_PATH, DOWNLOAD_PATH, False)` | `assert_called_once_with` |
| `set_download_results` | `('00123456', wifi={'logs.zip': ['w.etl']}, ddd={'logs.zip': ['d.etl', 'e.evtx']}, bt={'logs.zip': ['b.etl']}, fw={'logs.zip': ['f.bin']})` | `assert_called_once_with` |
| `socketio.emit` | `('all_attachments_download_done', {wifi_dict, ddd_dict, bt_dict, fw_dict}, namespace='/progress')` | `assert_called_once_with` |
| `cleanup_incomplete_downloads` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| Results stored and emitted with exact shape | DDD/EVT not merged, keys renamed, daemon thread used, or emit payload changes |

### 7. test_bsod_download_skips_decompose_and_emits_dump_path

*Class:* `TestHandleStartDownload`

**Description:** With `session['bsod']=True` the worker must not decompose ZIPs or store results; it emits `all_attachments_download_done_bsod` with the last downloaded file path as `dump_path`.

**Code:**
```python
def test_bsod_download_skips_decompose_and_emits_dump_path(self, mock_dl, mock_dec, mock_gather):
    # Arrange
    mock_dl.run_dload_threads.return_value = iter([(ZIP_PATH, "dump.zip", False)])

    # Act
    self._run(bsod=True)

    # Assert
    mock_dec.process_single_zip.assert_not_called()
    self.app_config.set_download_results.assert_not_called()
    self.socketio.emit.assert_called_once_with(
        "all_attachments_download_done_bsod", {"dump_path": ZIP_PATH}, namespace="/progress")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Session `bsod` | `True` | BSOD flow |
| `run_dload_threads` | returns `iter([(ZIP_PATH, 'dump.zip', False)])` | One dump |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `process_single_zip` | not called | `assert_not_called` |
| `set_download_results` | not called | `assert_not_called` |
| `socketio.emit` | `('all_attachments_download_done_bsod', {'dump_path': ZIP_PATH}, namespace='/progress')` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| BSOD-specific emit only | ZIP decomposed or Wi-Fi event emitted for a BSOD case |

### 8. test_cancelled_download_cleans_up_and_emits_nothing

*Class:* `TestHandleStartDownload`

**Description:** If the cancel event is set when the download loop finishes, the worker removes partial files via `cleanup_incomplete_downloads(download_path)` and returns without storing results or emitting.

**Code:**
```python
def test_cancelled_download_cleans_up_and_emits_nothing(self, mock_dl, mock_dec, mock_gather):
    # Arrange
    mock_dl.run_dload_threads.return_value = iter([(ZIP_PATH, "logs.zip", False)])
    mock_dec.process_single_zip.return_value = ([], [], [], [], [])
    self.app_config.driver_manager.download_cancel_event.is_set.return_value = True

    # Act
    self._run()

    # Assert
    mock_dl.cleanup_incomplete_downloads.assert_called_once_with(DOWNLOAD_PATH)
    self.app_config.set_download_results.assert_not_called()
    self.socketio.emit.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `download_cancel_event.is_set` | `True` | User cancelled |
| `run_dload_threads` | one ZIP | Partial work |
| `process_single_zip` | `([], [], [], [], [])` | Empty lists |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `cleanup_incomplete_downloads` | `(DOWNLOAD_PATH)` | `assert_called_once_with` |
| `set_download_results` | not called | `assert_not_called` |
| `socketio.emit` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| Cleanup only | Results stored / done event emitted after cancel |

### 9. test_no_files_emits_empty_dicts

*Class:* `TestHandleStartDownload`

**Description:** With no selected files the loop never runs; the worker still stores and emits four empty dicts so the UI can leave its progress state.

**Code:**
```python
def test_no_files_emits_empty_dicts(self, mock_dl, mock_dec, mock_gather):
    # Arrange
    mock_dl.run_dload_threads.return_value = iter([])

    # Act
    self._run(selected_files=[])

    # Assert
    self.app_config.set_download_results.assert_called_once_with(
        TEST_CASE_NBR, wifi={}, ddd={}, bt={}, fw={})
    self.socketio.emit.assert_called_once_with(
        "all_attachments_download_done",
        {"wifi_dict": {}, "ddd_dict": {}, "bt_dict": {}, "fw_dict": {}},
        namespace="/progress")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Session `selected_files` | `[]` | Nothing selected |
| `run_dload_threads` | returns `iter([])` | No downloads |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `set_download_results` | `('00123456', wifi={}, ddd={}, bt={}, fw={})` | `assert_called_once_with` |
| `socketio.emit` | `('all_attachments_download_done', {'wifi_dict': {}, 'ddd_dict': {}, 'bt_dict': {}, 'fw_dict': {}}, namespace='/progress')` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Empty results still emitted | UI left waiting (no emit) or error on empty input |

### 10. test_result_callback_records_gather_telemetry

*Class:* `TestHandleStartDownload`

**Description:** The `result_callback` passed to `run_dload_threads` maps a downloader result dict onto `gather_service.record_attachment_download_result` kwargs, including the session's `gather_workflow_id`, the case snapshot, and the domain from `wifi_or_bt`.

**Code:**
```python
def test_result_callback_records_gather_telemetry(self, mock_dl, mock_dec, mock_gather):
    # Arrange: have the fake downloader invoke the callback once
    def fake_run(selected, path, sock, result_callback=None):
        result_callback({"name": "logs.zip", "status": "ok", "bytes": 2048,
                         "latency_ms": 15, "attempt_count": 1})
        return iter([])
    mock_dl.run_dload_threads.side_effect = fake_run

    # Act
    self._run()

    # Assert
    kwargs = mock_gather.record_attachment_download_result.call_args.kwargs
    self.assertEqual(kwargs["workflow_id"], "wf-1")
    self.assertEqual(kwargs["name"], "logs.zip")
    self.assertEqual(kwargs["status"], "ok")
    self.assertEqual(kwargs["byte_count"], 2048)
    self.assertEqual(kwargs["latency_ms"], 15)
    self.assertEqual(kwargs["attempt_count"], 1)
    self.assertEqual(kwargs["error_code"], "")
    self.assertEqual(kwargs["domain"], "wifi")
    self.assertEqual(kwargs["issue"]["case_nbr"], TEST_CASE_NBR)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `run_dload_threads` side effect | calls `result_callback({'name': 'logs.zip', 'status': 'ok', 'bytes': 2048, 'latency_ms': 15, 'attempt_count': 1})` | Fake downloader |
| Session `gather_workflow_id` | `'wf-1'` | Telemetry workflow |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| kwargs `workflow_id`, `name`, `status` | `'wf-1'`, `'logs.zip'`, `'ok'` | `assertEqual` |
| kwargs `byte_count`, `latency_ms`, `attempt_count` | `2048`, `15`, `1` | `assertEqual` |
| kwargs `error_code`, `domain` | `''`, `'wifi'` | `assertEqual` |
| kwargs `issue['case_nbr']` | `'00123456'` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Exact kwargs mapping | `bytes`→`byte_count` mapping broken or workflow id lost |

### 11. test_result_callback_defaults_and_swallows_errors

*Class:* `TestHandleStartDownload`

**Description:** An empty result dict produces the documented defaults (`name=''`, `status='failed'`, `attempt_count=0`), and a telemetry exception is swallowed so the download flow still completes and emits.

**Code:**
```python
def test_result_callback_defaults_and_swallows_errors(self, mock_dl, mock_dec, mock_gather):
    # Arrange: empty result dict -> defaults; telemetry raises -> swallowed
    mock_gather.record_attachment_download_result.side_effect = RuntimeError("telemetry down")

    def fake_run(selected, path, sock, result_callback=None):
        result_callback({})
        return iter([])
    mock_dl.run_dload_threads.side_effect = fake_run

    # Act (must not raise)
    self._run()

    # Assert
    kwargs = mock_gather.record_attachment_download_result.call_args.kwargs
    self.assertEqual(kwargs["name"], "")
    self.assertEqual(kwargs["status"], "failed")
    self.assertEqual(kwargs["attempt_count"], 0)
    self.socketio.emit.assert_called_once()  # flow continued after the error
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `run_dload_threads` side effect | calls `result_callback({})` | Empty result |
| `record_attachment_download_result` | raises `RuntimeError('telemetry down')` | Telemetry failure |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| kwargs `name`, `status`, `attempt_count` | `''`, `'failed'`, `0` | `assertEqual` |
| `socketio.emit` | called once | `assert_called_once()` |
| Exception | none raised | test completes |

| Pass Condition | Fail Condition |
|---|---|
| Defaults applied, error swallowed | Telemetry failure aborts the download flow |

### 12. test_missing_case_context_falls_back_to_empty_context

*Class:* `TestHandleStartDownload`

**Description:** `session['case_context']` is `None`; `CaseContext.from_session(None or {})` yields an empty context, so results are stored under `case_nbr=None` and the done event is still emitted (no `KeyError`, unlike the HTTP routes in other blueprints).

**Code:**
```python
def test_missing_case_context_falls_back_to_empty_context(self, mock_dl, mock_dec, mock_gather):
    # Arrange
    mock_dl.run_dload_threads.return_value = iter([])

    # Act
    self._run(case_context=None)

    # Assert: case_nbr None, still emits
    self.app_config.set_download_results.assert_called_once_with(None, wifi={}, ddd={}, bt={}, fw={})
    self.socketio.emit.assert_called_once()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Session `case_context` | `None` | No case loaded |
| `run_dload_threads` | returns `iter([])` | No downloads |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `set_download_results` | `(None, wifi={}, ddd={}, bt={}, fw={})` | `assert_called_once_with` |
| `socketio.emit` | called once | `assert_called_once()` |

| Pass Condition | Fail Condition |
|---|---|
| Graceful fallback | Exception on missing case context |

## Test Execution Flow

```
setUpClass ── patch app_config (module + blueprints.download.download_routes, utils.attachment_download, … identity-rebound)
     │
     ▼
setUp ── reset mock app_config (driver_manager / socketio MagicMocks, silent_mode=False)
      ── patch CaseContext.to_session ── fresh Flask app ── register download_bp ── client ── push app ctx
      ── (TestHandleStartDownload) _SyncThread.instances=[]; self.socketio=MagicMock(); cancel_event.is_set=False
     │
     ├─ HTTP tests:     client.post/get('/download/cancel_download') → assert status + driver_manager calls
     ├─ cancel handler: handle_cancel_download(MagicMock(), 'sid-123') → assert emit(to=sid)
     └─ start handler:  _run(**session) → test_request_context → session.update → handle_start_download
                        → _SyncThread.start() runs background_download synchronously
                        → assert run_dload_threads / process_single_zip / set_download_results / emit / gather
     │
     ▼
tearDown ──► PASS / FAIL ── tearDownClass stops all patchers
```

## Coverage Summary

| Metric | Value |
|---|---|
| Total Routes | 1 HTTP route (+ 2 SocketIO events) |
| Routes Tested | 1 / 1 (both SocketIO handlers also tested) |
| Total Tests | 12 (2 structure + 2 route + 8 handler logic) |
| Actual Coverage (%) | 100% of routes; all branches of `handle_start_download` / `background_download` (Wi-Fi, BSOD, cancelled, empty, telemetry ok/error, missing context) |
| Out-of-scope items | 2 (real threading/Selenium in `run_dload_threads`; SocketIO wrapper closures) |

## What Would Make These Tests Fail?

| Source change | ✅ PASS | ❌ FAIL |
|---|---|---|
| Stop merging `evt_files` into `ddd` | BSOD, cancel, telemetry tests | `test_wifi_download_decomposes_zip_stores_results_and_emits` |
| Rename `all_attachments_download_done` or its payload keys | BSOD / cancel tests | Wi-Fi, no-files, missing-context tests |
| Remove the `download_cancel_event.is_set()` check | other tests | `test_cancelled_download_cleans_up_and_emits_nothing` |
| Decompose ZIPs for BSOD cases | Wi-Fi tests | `test_bsod_download_skips_decompose_and_emits_dump_path` |
| Make the worker thread `daemon=True` | other tests | `test_wifi_download_decomposes_zip_stores_results_and_emits` |
| Let telemetry exceptions propagate | other tests | `test_result_callback_defaults_and_swallows_errors` |
| Rename `bytes`→`byte_count` mapping or drop `gather_workflow_id` | other tests | `test_result_callback_records_gather_telemetry` |
| Broadcast `download_cancelled` (drop `to=sid`) | other tests | `test_cancels_and_emits_to_requesting_client` |
| Allow GET on `/cancel_download` | POST test | `test_cancel_download_rejects_get`, `test_blueprint_registered_with_url_prefix` |
| Rename SocketIO events / namespace | handler tests | `test_register_socketio_handlers_binds_progress_events` |
| Index `session['case_context']` directly | other tests | `test_missing_case_context_falls_back_to_empty_context` (KeyError) |
