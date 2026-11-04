# Handsfree Blueprint — Analysis & Unit Test Documentation

- Route module: [blueprints/handsfree/handsfree_routes.py](../blueprints/handsfree/handsfree_routes.py)
- Test module: [tests/test_handsfree_blueprint.py](../tests/test_handsfree_blueprint.py)
- Shared fixture: [tests/base_test.py](../tests/base_test.py)
- Services behind it: [services/handsfree/orchestrator.py](../services/handsfree/orchestrator.py), [services/handsfree/queue.py](../services/handsfree/queue.py), [services/handsfree/case_reader.py](../services/handsfree/case_reader.py), [services/handsfree/runner.py](../services/handsfree/runner.py), [services/handsfree/composer.py](../services/handsfree/composer.py)

## Overview — What Handsfree Does

The `handsfree` blueprint (`url_prefix="/handsfree"`) is the control panel for the "Handsfree Replyer": it starts unattended analysis runs over a case owner's open IPS cases (`/check_now`) or a single case (`/run_case`), exposes run status, and manages a review queue of AI-drafted case comments stored by `HandsfreeStore` under `<avatarfiles_dir>/handsfree`. Posting to IPS happens **only** through the explicit `POST /queue/<id>/approve` endpoint (and is disabled entirely when the `dry_run` config flag is set, enforced in the orchestrator). The routes themselves are a thin JSON layer; analysis happens in background threads inside `services.handsfree`.

> **Branch-specific — IPS "Environment Details" (`env_detail`):** on this branch the handsfree reader path ingests the IPS Environment Details Q&A form (`CaseContext.env_detail`, commit 5c1658d9). `HandsfreeRunner.analyze_case` copies it into `CaseAnalysis.env_detail`, `read_case_history(..., env_detail=…)` renders it via `_format_env_detail` into the reader prompt's `{env_block}`, `_env_repro_steps` retracts a reader-flagged "repro steps missing" gap when the form's *Steps to reproduce* answer is filled, and `_env_brief` builds the compact platform summary given to the agent. None of this is visible in the route code, so the test module adds a `TestHandsfreeEnvDetail` class exercising these pure helpers directly.

## All Routes (11 endpoints)

| Route | Method | Purpose |
|---|---|---|
| `/handsfree/` | GET | Render `handsfree.html` with the store config |
| `/handsfree/check_now` | POST | `orchestrator.start_check_now(owner_name)` — background run over the owner's cases |
| `/handsfree/run_case` | POST | `orchestrator.start_case_run(case_nbr)` — background run for one case |
| `/handsfree/status` | GET | `orchestrator.get_run_state()` |
| `/handsfree/queue` | GET | `store.list_drafts(include_closed=all∈{1,true,yes})` → `{"items": [...]}` |
| `/handsfree/queue/<draft_id>` | GET | `store.get(draft_id)` or 404 |
| `/handsfree/queue/<draft_id>` | PUT, POST | Save edited draft text (AI marker enforced, HTML composed) or 400/404 |
| `/handsfree/queue/<draft_id>/approve` | POST | `orchestrator.approve_and_post(draft_id, edited_plain=…)` — the only posting path |
| `/handsfree/queue/<draft_id>/reject` | POST | `orchestrator.reject(draft_id, reason=…)` |
| `/handsfree/config` | GET, POST | Read config / save whitelisted keys |
| `/handsfree/discover_fields` | POST | `orchestrator.discover_rest_fields()`; exceptions → 500 |

## High-Level Flow Diagram

```
                 ┌──────────────── _store() = HandsfreeStore(Path(app_config.avatarfiles_dir) / "handsfree")
                 │
GET  /                    ──► store.load_config() ──► render_template("handsfree.html", config=cfg)
GET  /config              ──► jsonify(store.load_config())
POST /config  {json}      ──► keep keys ∈ {owner_name, post_backend, max_cases_per_run, dry_run,
                                            rest_field_map, ui_locators} ──► jsonify(store.save_config(updates))
GET  /queue?all=          ──► {"items": store.list_drafts(include_closed = all.lower() ∈ {1,true,yes})}
GET  /queue/<id>          ──► store.get(id) ── None ──► 404 {"error":"not found"}
PUT|POST /queue/<id> {draft_plain}
        plain = draft_plain.strip() ── "" ──► 400 {"ok":false,"error":"empty draft"}
        AI_MARKER missing? ──► plain = AI_MARKER + "\n\n" + plain
        store.update(id, draft_plain=plain, draft_html=compose_html(plain)) ── None ──► 404
        ──► {"ok":true,"draft":rec}

POST /check_now  {owner_name} ──► orchestrator.start_check_now(owner_name)      ─┐
POST /run_case   {case_nbr}   ──► orchestrator.start_case_run(case_nbr)          │ background thread:
GET  /status                  ──► orchestrator.get_run_state()                   │ HandsfreeRunner.analyze_case
POST /queue/<id>/approve {draft_plain} ──► orchestrator.approve_and_post(id, edited_plain)   (dry_run gate inside)
POST /queue/<id>/reject  {reason}      ──► orchestrator.reject(id, reason=reason or "")
POST /discover_fields          ──► orchestrator.discover_rest_fields() ── raises ──► 500 {"error":"<Type>: <msg>"}
                                                                                 │
   (service layer, branch-specific)  CaseService.process_case → case_ctx.env_detail ◄┘
        analysis.env_detail = dict(env_detail)
        read_case_history(..., env_detail) ──► _format_env_detail(env) ──► READER_PROMPT {env_block}
        check_case_info: _env_repro_steps(env) filled? ──► drop "repro_steps" from missing_info
        agent context:   _env_brief(env) ──► "Platform: …; Found in build: …; Computer Model: …"
```

## Dependencies

```
flask:        Blueprint, jsonify, render_template, request
stdlib:       pathlib.Path (inside _store)
services:     services.handsfree.orchestrator  (start_check_now, start_case_run, get_run_state,
                                                approve_and_post, reject, discover_rest_fields)
              services.handsfree.queue.HandsfreeStore (load_config, save_config, list_drafts, get, update)
              services.handsfree.composer.compose_html, AI_MARKER   (imported lazily in save_draft; pure)
app_config:   app_config.avatarfiles_dir   (imported lazily inside _store())
template:     handsfree.html
branch-specific helpers tested directly:
              services.handsfree.case_reader._format_env_detail, READER_PROMPT
              services.handsfree.runner._env_repro_steps, _env_brief
```

## Test Coverage Analysis

```
STRUCTURE
  ✅ blueprint registered, url_prefix, 10 URL rules / 11 endpoints, methods ── test_blueprint_registered_with_url_prefix

ROUTES
  ✅ GET /            → store path + render_template(config)        ── test_index_renders_with_store_config
  ✅ GET /config      → load_config                                 ── test_get_config_returns_store_config
  ✅ POST /config     → whitelist filter                            ── test_post_config_saves_only_allowed_keys
  ✅ POST /config     → non-JSON body → empty update                ── test_post_config_without_json_saves_empty_update
  ✅ POST /check_now  → owner_name / None                           ── test_check_now_passes_owner_name, test_check_now_without_body_passes_none
  ✅ POST /run_case   → case_nbr / None                             ── test_run_case_passes_case_nbr, test_run_case_without_body_passes_none
  ✅ GET /status                                                     ── test_status_returns_run_state
  ✅ POST approve     → edited_plain / None; GET → 405              ── test_approve_passes_edited_plain, test_approve_without_body_passes_none, test_approve_rejects_get
  ✅ POST reject      → reason / default ""                         ── test_reject_passes_reason, test_reject_without_reason_defaults_to_empty
  ✅ POST /discover_fields → success / exception 500                ── test_discover_fields_success, test_discover_fields_exception_returns_500
  ✅ GET /queue       → default include_closed=False                ── test_list_queue_defaults_to_open_drafts
  ✅ GET /queue?all=  → 1/TRUE/yes vs 0/no                          ── test_list_queue_all_flag_variants_include_closed
  ✅ GET /queue/<id>  → found / 404                                 ── test_get_draft_found, test_get_draft_not_found_returns_404
  ✅ PUT /queue/<id>  → marker prepended, HTML composed+escaped     ── test_save_draft_prepends_ai_marker_and_composes_html
  ✅ POST /queue/<id> → existing marker kept                        ── test_save_draft_keeps_existing_marker_via_post
  ✅ PUT /queue/<id>  → blank → 400 / unknown → 404                 ── test_save_draft_empty_returns_400, test_save_draft_unknown_id_returns_404

CORE LOGIC — branch-specific env_detail (service helpers)
  ✅ READER_PROMPT has {env_block}                                  ── test_reader_prompt_has_env_block_placeholder
  ✅ _format_env_detail skips blanks, strips keys                   ── test_format_env_detail_skips_blank_answers
  ✅ _format_env_detail → "(none)" for {}, None, str, all-blank     ── test_format_env_detail_empty_or_invalid_is_none_marker
  ✅ _format_env_detail 300-char truncation, 25-entry cap           ── test_format_env_detail_truncates_long_answers_and_caps_entries
  ✅ _env_repro_steps returns filled answer                         ── test_env_repro_steps_returns_filled_answer
  ✅ _env_repro_steps ignores ""/NA/n/a/None/blank and None input   ── test_env_repro_steps_ignores_placeholder_answers
  ✅ _env_brief keeps only platform/build/OS/model fields           ── test_env_brief_keeps_only_platform_fields
  ✅ _env_brief limit                                               ── test_env_brief_respects_limit

BACKGROUND / SERVICE
  ⚪ orchestrator background threads, HandsfreeRunner.analyze_case (LLM + IPS + agent) ── service layer, orchestrator mocked
  ⚪ dry_run posting gate ── enforced in orchestrator._approve_and_post_locked, not in the route
  ⚪ HandsfreeStore draft_id sanitisation (`/`, `\`, `..`) ── store layer, mocked
  ⚠️ non-string draft_plain (e.g. a number) → AttributeError on .strip() → 500 ── NOT TESTED
```



## What the Unit Tests Actually Test

Route test classes patch `HandsfreeStore` (index/config/queue classes) or `orchestrator` (run classes) at class level; `_store()` therefore returns `mock_store_cls.return_value`. `app_config.avatarfiles_dir` is the fixture's `TEST_AVATARFILES_DIR` (`tests/_fake_avatarfiles`, never created). `AI_MARKER = "[AI-Avatar preliminary analysis]"` and `compose_html` are the real composer objects. `TestHandsfreeEnvDetail` is a plain `unittest.TestCase` — no Flask app, no mocks; it calls the pure service helpers.

### 1. test_blueprint_registered_with_url_prefix

*Class:* `TestHandsfreeBlueprintStructure`

**Description:** Collects all `handsfree.*` rules and verifies name, `url_prefix='/handsfree'`, the exact set of 10 URL rules (11 endpoints — `/queue/<draft_id>` has separate GET and PUT/POST views), that `/queue/<draft_id>` accepts GET+PUT+POST, and that `/approve` does not accept GET (posting only via explicit POST).

**Code:**
```python
def test_blueprint_registered_with_url_prefix(self):
    # Arrange / Act
    rules = {}
    for r in self.app.url_map.iter_rules():
        if r.endpoint.startswith("handsfree."):
            rules.setdefault(r.rule, set()).update(r.methods)

    # Assert
    self.assertIs(self.app.blueprints.get("handsfree"), handsfree_bp)
    self.assertEqual(handsfree_bp.url_prefix, "/handsfree")
    self.assertEqual(set(rules), {
        "/handsfree/", "/handsfree/check_now", "/handsfree/run_case", "/handsfree/status",
        "/handsfree/queue", "/handsfree/queue/<draft_id>", "/handsfree/queue/<draft_id>/approve",
        "/handsfree/queue/<draft_id>/reject", "/handsfree/config", "/handsfree/discover_fields",
    })
    self.assertTrue({"GET", "PUT", "POST"}.issubset(rules["/handsfree/queue/<draft_id>"]))
    self.assertNotIn("GET", rules["/handsfree/queue/<draft_id>/approve"])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Registered blueprint | `handsfree_bp` | From `BaseTestCase.setUp` |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `app.blueprints['handsfree']` | `handsfree_bp` | `assertIs` |
| `url_prefix` | `'/handsfree'` | `assertEqual` |
| Rule set | 10 rules listed in the test | `assertEqual(set)` |
| `/queue/<draft_id>` methods | ⊇ `{GET, PUT, POST}` | `assertTrue(issubset)` |
| `/approve` methods | no `GET` | `assertNotIn` |

| Pass Condition | Fail Condition |
|---|---|
| Exact route map | Route added/removed/renamed or approve reachable by GET |

### 2. test_index_renders_with_store_config

*Class:* `TestHandsfreeIndexAndConfig`

**Description:** `GET /handsfree/` builds the store at `Path(app_config.avatarfiles_dir) / 'handsfree'` and renders `handsfree.html` with the loaded config.

**Code:**
```python
@patch(f"{ROUTES}.render_template", return_value="handsfree-page")
def test_index_renders_with_store_config(self, mock_render, mock_store_cls):
    # Arrange
    mock_store_cls.return_value.load_config.return_value = {"owner_name": "Tester", "dry_run": True}

    # Act
    resp = self.client.get("/handsfree/")

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_data(as_text=True), "handsfree-page")
    mock_store_cls.assert_called_once_with(Path(TEST_AVATARFILES_DIR) / "handsfree")
    mock_render.assert_called_once_with("handsfree.html", config={"owner_name": "Tester", "dry_run": True})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `HandsfreeStore().load_config` | `{'owner_name': 'Tester', 'dry_run': True}` | Stored config |
| `render_template` | returns `'handsfree-page'` | Patched |
| `app_config.avatarfiles_dir` | `TEST_AVATARFILES_DIR` | Fixture path |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / body | `200` / `'handsfree-page'` | `assertEqual` |
| `HandsfreeStore` | `(Path(TEST_AVATARFILES_DIR) / 'handsfree')` | `assert_called_once_with` |
| `render_template` | `('handsfree.html', config={'owner_name': 'Tester', 'dry_run': True})` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Store rooted correctly, config rendered | Store path or template contract changes |

### 3. test_get_config_returns_store_config

*Class:* `TestHandsfreeIndexAndConfig`

**Description:** `GET /handsfree/config` returns `load_config()` as JSON and never saves.

**Code:**
```python
def test_get_config_returns_store_config(self, mock_store_cls):
    # Arrange
    mock_store_cls.return_value.load_config.return_value = {"max_cases_per_run": 3}

    # Act
    resp = self.client.get("/handsfree/config")

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_json(), {"max_cases_per_run": 3})
    mock_store_cls.return_value.save_config.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `load_config` | `{'max_cases_per_run': 3}` | Stored config |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `200` / `{'max_cases_per_run': 3}` | `assertEqual` |
| `save_config` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| Read-only GET | GET writes config |

### 4. test_post_config_saves_only_allowed_keys

*Class:* `TestHandsfreeIndexAndConfig`

**Description:** `POST /handsfree/config` forwards only whitelisted keys; `auto_post` (a future, confidence-gated auto-posting flag) and unknown keys are dropped, so the UI cannot enable auto-posting.

**Code:**
```python
def test_post_config_saves_only_allowed_keys(self, mock_store_cls):
    # Arrange
    store = mock_store_cls.return_value
    store.save_config.return_value = {"owner_name": "Tester", "dry_run": True}

    # Act
    resp = self.client.post("/handsfree/config", json={
        "owner_name": "Tester", "dry_run": True, "auto_post": True, "evil": "x"})

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_json(), {"owner_name": "Tester", "dry_run": True})
    store.save_config.assert_called_once_with({"owner_name": "Tester", "dry_run": True})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| JSON body | `{'owner_name': 'Tester', 'dry_run': True, 'auto_post': True, 'evil': 'x'}` | Mixed keys |
| `save_config` | returns `{'owner_name': 'Tester', 'dry_run': True}` | Merged config |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `200` / save_config's return | `assertEqual` |
| `save_config` | `({'owner_name': 'Tester', 'dry_run': True})` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Whitelist enforced | `auto_post` or arbitrary keys persisted |

### 5. test_post_config_without_json_saves_empty_update

*Class:* `TestHandsfreeIndexAndConfig`

**Description:** A non-JSON body is parsed with `get_json(silent=True)` → `{}`; the route still calls `save_config({})` (a no-op merge) and returns 200.

**Code:**
```python
def test_post_config_without_json_saves_empty_update(self, mock_store_cls):
    # Arrange
    mock_store_cls.return_value.save_config.return_value = {}

    # Act
    resp = self.client.post("/handsfree/config", data="not json")

    # Assert
    self.assertEqual(resp.status_code, 200)
    mock_store_cls.return_value.save_config.assert_called_once_with({})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Body | `'not json'` | Invalid JSON |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| `save_config` | `({})` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Tolerates bad body | 400/500 on non-JSON |

### 6. test_check_now_passes_owner_name

*Class:* `TestHandsfreeRunRoutes`

**Description:** `owner_name` from the JSON body is passed positionally to `start_check_now`, and its dict is returned as JSON.

**Code:**
```python
def test_check_now_passes_owner_name(self, mock_orch):
    # Arrange
    mock_orch.start_check_now.return_value = {"ok": True, "started": True}

    # Act
    resp = self.client.post("/handsfree/check_now", json={"owner_name": "Tester"})

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_json(), {"ok": True, "started": True})
    mock_orch.start_check_now.assert_called_once_with("Tester")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| JSON body | `{'owner_name': 'Tester'}` | Owner |
| `start_check_now` | `{'ok': True, 'started': True}` | Started |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `200` / `{'ok': True, 'started': True}` | `assertEqual` |
| `start_check_now` | `('Tester')` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Pass-through | Owner dropped or response altered |

### 7. test_check_now_without_body_passes_none

*Class:* `TestHandsfreeRunRoutes`

**Description:** Without a body the route passes `None` (the orchestrator falls back to the configured owner or reports an error); the orchestrator's error dict is returned unchanged.

**Code:**
```python
def test_check_now_without_body_passes_none(self, mock_orch):
    # Arrange
    mock_orch.start_check_now.return_value = {"ok": False, "error": "owner_name required"}

    # Act
    resp = self.client.post("/handsfree/check_now")

    # Assert
    self.assertEqual(resp.get_json(), {"ok": False, "error": "owner_name required"})
    mock_orch.start_check_now.assert_called_once_with(None)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Body | *(none)* | — |
| `start_check_now` | `{'ok': False, 'error': 'owner_name required'}` | Error result |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| JSON | `{'ok': False, 'error': 'owner_name required'}` | `assertEqual` |
| `start_check_now` | `(None)` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| `None` forwarded | Route crashes on missing body |

### 8. test_run_case_passes_case_nbr

*Class:* `TestHandsfreeRunRoutes`

**Description:** `case_nbr` is forwarded to `start_case_run`; the background run is where `env_detail` is fetched and used (see core-logic tests).

**Code:**
```python
def test_run_case_passes_case_nbr(self, mock_orch):
    # Arrange
    mock_orch.start_case_run.return_value = {"ok": True, "case_nbr": "00123456"}

    # Act
    resp = self.client.post("/handsfree/run_case", json={"case_nbr": "00123456"})

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_json(), {"ok": True, "case_nbr": "00123456"})
    mock_orch.start_case_run.assert_called_once_with("00123456")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| JSON body | `{'case_nbr': '00123456'}` | Case |
| `start_case_run` | `{'ok': True, 'case_nbr': '00123456'}` | Started |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `200` / `{'ok': True, 'case_nbr': '00123456'}` | `assertEqual` |
| `start_case_run` | `('00123456')` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Pass-through | case_nbr dropped |

### 9. test_run_case_without_body_passes_none

*Class:* `TestHandsfreeRunRoutes`

**Description:** Missing body → `start_case_run(None)`; validation is the orchestrator's job.

**Code:**
```python
def test_run_case_without_body_passes_none(self, mock_orch):
    # Arrange
    mock_orch.start_case_run.return_value = {"ok": False, "error": "case_nbr required"}

    # Act
    resp = self.client.post("/handsfree/run_case")

    # Assert
    self.assertEqual(resp.get_json()["ok"], False)
    mock_orch.start_case_run.assert_called_once_with(None)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Body | *(none)* | — |
| `start_case_run` | `{'ok': False, 'error': 'case_nbr required'}` | Error result |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| JSON `ok` | `False` | `assertEqual` |
| `start_case_run` | `(None)` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| `None` forwarded | Route crashes |

### 10. test_status_returns_run_state

*Class:* `TestHandsfreeRunRoutes`

**Description:** `GET /status` returns `get_run_state()` verbatim.

**Code:**
```python
def test_status_returns_run_state(self, mock_orch):
    # Arrange
    mock_orch.get_run_state.return_value = {"running": False, "events": []}

    # Act
    resp = self.client.get("/handsfree/status")

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_json(), {"running": False, "events": []})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `get_run_state` | `{'running': False, 'events': []}` | State |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `200` / `{'running': False, 'events': []}` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Pass-through | State altered |

### 11. test_approve_passes_edited_plain

*Class:* `TestHandsfreeRunRoutes`

**Description:** Approve forwards the path's `draft_id` and the body's `draft_plain` as `edited_plain` — the one and only posting path.

**Code:**
```python
def test_approve_passes_edited_plain(self, mock_orch):
    # Arrange
    mock_orch.approve_and_post.return_value = {"ok": True, "posted": True}

    # Act
    resp = self.client.post("/handsfree/queue/d-1/approve", json={"draft_plain": "edited text"})

    # Assert
    self.assertEqual(resp.get_json(), {"ok": True, "posted": True})
    mock_orch.approve_and_post.assert_called_once_with("d-1", edited_plain="edited text")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| URL | `/handsfree/queue/d-1/approve` | Draft d-1 |
| JSON body | `{'draft_plain': 'edited text'}` | Edited text |
| `approve_and_post` | `{'ok': True, 'posted': True}` | Posted |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| JSON | `{'ok': True, 'posted': True}` | `assertEqual` |
| `approve_and_post` | `('d-1', edited_plain='edited text')` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Exact forwarding | Edits lost or wrong draft posted |

### 12. test_approve_without_body_passes_none

*Class:* `TestHandsfreeRunRoutes`

**Description:** Approving without edits passes `edited_plain=None` so the stored draft is posted as-is.

**Code:**
```python
def test_approve_without_body_passes_none(self, mock_orch):
    # Arrange
    mock_orch.approve_and_post.return_value = {"ok": True}

    # Act
    self.client.post("/handsfree/queue/d-1/approve")

    # Assert
    mock_orch.approve_and_post.assert_called_once_with("d-1", edited_plain=None)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Body | *(none)* | — |
| `approve_and_post` | `{'ok': True}` | — |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `approve_and_post` | `('d-1', edited_plain=None)` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| `None` forwarded | Empty string posted instead |

### 13. test_approve_rejects_get

*Class:* `TestHandsfreeRunRoutes`

**Description:** A GET on the approve URL returns 405 and never posts — enforcing the 'posting only via explicit Approve POST' rule from the module docstring.

**Code:**
```python
def test_approve_rejects_get(self, mock_orch):
    # Act
    resp = self.client.get("/handsfree/queue/d-1/approve")

    # Assert: posting only via explicit POST
    self.assertEqual(resp.status_code, 405)
    mock_orch.approve_and_post.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Request | `GET /handsfree/queue/d-1/approve` | Wrong method |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `405` | `assertEqual` |
| `approve_and_post` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| No posting via GET | GET triggers a post |

### 14. test_reject_passes_reason

*Class:* `TestHandsfreeRunRoutes`

**Description:** Reject forwards `draft_id` and `reason`.

**Code:**
```python
def test_reject_passes_reason(self, mock_orch):
    # Arrange
    mock_orch.reject.return_value = {"ok": True}

    # Act
    resp = self.client.post("/handsfree/queue/d-1/reject", json={"reason": "wrong case"})

    # Assert
    self.assertEqual(resp.get_json(), {"ok": True})
    mock_orch.reject.assert_called_once_with("d-1", reason="wrong case")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| JSON body | `{'reason': 'wrong case'}` | Reason |
| `reject` | `{'ok': True}` | — |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| JSON | `{'ok': True}` | `assertEqual` |
| `reject` | `('d-1', reason='wrong case')` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Exact forwarding | Reason dropped |

### 15. test_reject_without_reason_defaults_to_empty

*Class:* `TestHandsfreeRunRoutes`

**Description:** No body → `reason=''` (default in `body.get('reason', '')`).

**Code:**
```python
def test_reject_without_reason_defaults_to_empty(self, mock_orch):
    # Arrange
    mock_orch.reject.return_value = {"ok": True}

    # Act
    self.client.post("/handsfree/queue/d-1/reject")

    # Assert
    mock_orch.reject.assert_called_once_with("d-1", reason="")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Body | *(none)* | — |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `reject` | `('d-1', reason='')` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Empty default | `None` passed |

### 16. test_discover_fields_success

*Class:* `TestHandsfreeRunRoutes`

**Description:** `discover_rest_fields()` result returned as JSON with 200.

**Code:**
```python
def test_discover_fields_success(self, mock_orch):
    # Arrange
    mock_orch.discover_rest_fields.return_value = {"fields": ["CommentBody"]}

    # Act
    resp = self.client.post("/handsfree/discover_fields")

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_json(), {"fields": ["CommentBody"]})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `discover_rest_fields` | `{'fields': ['CommentBody']}` | Field list |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `200` / `{'fields': ['CommentBody']}` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Pass-through | Result altered |

### 17. test_discover_fields_exception_returns_500

*Class:* `TestHandsfreeRunRoutes`

**Description:** Any exception becomes `500 {'error': '<ExceptionType>: <message>'}` — the only route with its own error handling.

**Code:**
```python
def test_discover_fields_exception_returns_500(self, mock_orch):
    # Arrange
    mock_orch.discover_rest_fields.side_effect = ConnectionError("IPS unreachable")

    # Act
    resp = self.client.post("/handsfree/discover_fields")

    # Assert
    self.assertEqual(resp.status_code, 500)
    self.assertEqual(resp.get_json(), {"error": "ConnectionError: IPS unreachable"})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `discover_rest_fields` | side effect `ConnectionError('IPS unreachable')` | IPS down |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `500` / `{'error': 'ConnectionError: IPS unreachable'}` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Formatted 500 | Unhandled exception |

### 18. test_list_queue_defaults_to_open_drafts

*Class:* `TestHandsfreeQueueRoutes`

**Description:** Without `?all`, only open drafts are listed and wrapped in `{'items': …}`.

**Code:**
```python
def test_list_queue_defaults_to_open_drafts(self, mock_store_cls):
    # Arrange
    mock_store_cls.return_value.list_drafts.return_value = [{"draft_id": "d-1"}]

    # Act
    resp = self.client.get("/handsfree/queue")

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_json(), {"items": [{"draft_id": "d-1"}]})
    mock_store_cls.return_value.list_drafts.assert_called_once_with(include_closed=False)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `list_drafts` | `[{'draft_id': 'd-1'}]` | One draft |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `200` / `{'items': [{'draft_id': 'd-1'}]}` | `assertEqual` |
| `list_drafts` | `(include_closed=False)` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Open drafts by default | Closed drafts included by default |

### 19. test_list_queue_all_flag_variants_include_closed

*Class:* `TestHandsfreeQueueRoutes`

**Description:** Table-driven (`subTest`): `all` values `1`, `TRUE`, `yes` → `include_closed=True`; `0`, `no` → `False` (case-insensitive membership in `{1, true, yes}`).

**Code:**
```python
def test_list_queue_all_flag_variants_include_closed(self, mock_store_cls):
    # Arrange
    store = mock_store_cls.return_value
    store.list_drafts.return_value = []

    for value, expected in (("1", True), ("TRUE", True), ("yes", True), ("0", False), ("no", False)):
        with self.subTest(all=value):
            store.list_drafts.reset_mock()

            # Act
            self.client.get("/handsfree/queue", query_string={"all": value})

            # Assert
            store.list_drafts.assert_called_once_with(include_closed=expected)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Query `all` | `'1'`, `'TRUE'`, `'yes'`, `'0'`, `'no'` | Flag variants |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `list_drafts` | `include_closed=True` ×3, `False` ×2 | `assert_called_once_with` per subTest |

| Pass Condition | Fail Condition |
|---|---|
| All variants parsed | Case sensitivity or accepted values change |

### 20. test_get_draft_found

*Class:* `TestHandsfreeQueueRoutes`

**Description:** Existing draft returned verbatim.

**Code:**
```python
def test_get_draft_found(self, mock_store_cls):
    # Arrange
    rec = {"draft_id": "d-1", "status": "pending", "draft_plain": "text"}
    mock_store_cls.return_value.get.return_value = rec

    # Act
    resp = self.client.get("/handsfree/queue/d-1")

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_json(), rec)
    mock_store_cls.return_value.get.assert_called_once_with("d-1")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `store.get` | `{'draft_id': 'd-1', 'status': 'pending', 'draft_plain': 'text'}` | Draft |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `200` / the record | `assertEqual` |
| `store.get` | `('d-1')` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Record returned | Record altered |

### 21. test_get_draft_not_found_returns_404

*Class:* `TestHandsfreeQueueRoutes`

**Description:** `store.get` → `None` maps to `404 {'error': 'not found'}`.

**Code:**
```python
def test_get_draft_not_found_returns_404(self, mock_store_cls):
    # Arrange
    mock_store_cls.return_value.get.return_value = None

    # Act
    resp = self.client.get("/handsfree/queue/missing")

    # Assert
    self.assertEqual(resp.status_code, 404)
    self.assertEqual(resp.get_json(), {"error": "not found"})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `store.get` | `None` | Missing / sanitised-away id |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `404` / `{'error': 'not found'}` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| 404 | `null` with 200 |

### 22. test_save_draft_prepends_ai_marker_and_composes_html

*Class:* `TestHandsfreeQueueRoutes`

**Description:** PUT trims the text, prepends the mandatory `AI_MARKER` + blank line, and stores both the plain text and the HTML from the real `compose_html` (escaped `<2>` → `&lt;2&gt;`, newlines → `<br/>`). Guarantees every posted comment is labeled as AI-generated.

**Code:**
```python
def test_save_draft_prepends_ai_marker_and_composes_html(self, mock_store_cls):
    # Arrange
    store = mock_store_cls.return_value
    store.update.side_effect = lambda draft_id, **fields: {"draft_id": draft_id, **fields}

    # Act
    resp = self.client.put("/handsfree/queue/d-1", json={"draft_plain": "  Line 1\nLine <2>  "})

    # Assert
    expected_plain = AI_MARKER + "\n\nLine 1\nLine <2>"
    self.assertEqual(resp.status_code, 200)
    body = resp.get_json()
    self.assertTrue(body["ok"])
    store.update.assert_called_once_with(
        "d-1", draft_plain=expected_plain,
        draft_html="<p>" + AI_MARKER + "<br/><br/>Line 1<br/>Line &lt;2&gt;</p>")
    self.assertEqual(body["draft"]["draft_plain"], expected_plain)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| JSON body | `{'draft_plain': '  Line 1\nLine <2>  '}` | Unmarked text |
| `store.update` | echoes `{'draft_id': id, **fields}` | Side effect |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / `ok` | `200` / `True` | `assertEqual` / `assertTrue` |
| `store.update` | `('d-1', draft_plain=AI_MARKER + '\n\nLine 1\nLine <2>', draft_html='<p>' + AI_MARKER + '<br/><br/>Line 1<br/>Line &lt;2&gt;</p>')` | `assert_called_once_with` |
| JSON `draft.draft_plain` | marked text | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Marker + escaped HTML stored | Unmarked or unescaped draft stored |

### 23. test_save_draft_keeps_existing_marker_via_post

*Class:* `TestHandsfreeQueueRoutes`

**Description:** If the marker is already present the text is stored unchanged (no double marker); also verifies POST works like PUT.

**Code:**
```python
def test_save_draft_keeps_existing_marker_via_post(self, mock_store_cls):
    # Arrange
    plain = AI_MARKER + "\n\nalready marked"
    mock_store_cls.return_value.update.return_value = {"draft_id": "d-1"}

    # Act
    resp = self.client.post("/handsfree/queue/d-1", json={"draft_plain": plain})

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(mock_store_cls.return_value.update.call_args.kwargs["draft_plain"], plain)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| JSON body | `{'draft_plain': AI_MARKER + '\n\nalready marked'}` | Marked text |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| `update` `draft_plain` | unchanged input | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| No double marker | Marker duplicated |

### 24. test_save_draft_empty_returns_400

*Class:* `TestHandsfreeQueueRoutes`

**Description:** Whitespace-only text → `400 {'ok': False, 'error': 'empty draft'}` and no store write.

**Code:**
```python
def test_save_draft_empty_returns_400(self, mock_store_cls):
    # Act
    resp = self.client.put("/handsfree/queue/d-1", json={"draft_plain": "   "})

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertEqual(resp.get_json(), {"ok": False, "error": "empty draft"})
    mock_store_cls.return_value.update.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| JSON body | `{'draft_plain': '   '}` | Blank |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `400` / `{'ok': False, 'error': 'empty draft'}` | `assertEqual` |
| `store.update` | not called | `assert_not_called` |

| Pass Condition | Fail Condition |
|---|---|
| Blank rejected | Marker-only draft saved |

### 25. test_save_draft_unknown_id_returns_404

*Class:* `TestHandsfreeQueueRoutes`

**Description:** `store.update` → `None` (unknown id) maps to `404 {'ok': False, 'error': 'not found'}`.

**Code:**
```python
def test_save_draft_unknown_id_returns_404(self, mock_store_cls):
    # Arrange
    mock_store_cls.return_value.update.return_value = None

    # Act
    resp = self.client.put("/handsfree/queue/missing", json={"draft_plain": "text"})

    # Assert
    self.assertEqual(resp.status_code, 404)
    self.assertEqual(resp.get_json(), {"ok": False, "error": "not found"})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| JSON body | `{'draft_plain': 'text'}` | Valid text |
| `store.update` | `None` | Unknown id |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `404` / `{'ok': False, 'error': 'not found'}` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| 404 | 200 with `draft: null` |

### 26. test_reader_prompt_has_env_block_placeholder

*Class:* `TestHandsfreeEnvDetail`

**Description:** **Branch-specific.** The reader prompt template must contain `{env_block}`, the slot `read_case_history` fills with the formatted Environment Details. Removing it would silently drop `env_detail` from the LLM input.

**Code:**
```python
def test_reader_prompt_has_env_block_placeholder(self):
    # Assert: read_case_history() formats env_detail into this slot
    self.assertIn("{env_block}", READER_PROMPT)
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `READER_PROMPT` | module constant | Real template |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Substring | `'{env_block}'` | `assertIn` |

| Pass Condition | Fail Condition |
|---|---|
| Slot present | Template no longer receives env_detail |

### 27. test_format_env_detail_skips_blank_answers

*Class:* `TestHandsfreeEnvDetail`

**Description:** **Branch-specific.** The IPS form lists every question; only filled answers carry information. Blank answers are skipped and question keys are stripped, producing `Q: A` lines.

**Code:**
```python
def test_format_env_detail_skips_blank_answers(self):
    # Arrange
    env = {"Computer Model": "TestModel X1", "Steps to reproduce": "   ", " OS ": "Windows 11"}

    # Act
    block = _format_env_detail(env)

    # Assert
    self.assertEqual(block, "Computer Model: TestModel X1\nOS: Windows 11")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `env` | `{'Computer Model': 'TestModel X1', 'Steps to reproduce': '   ', ' OS ': 'Windows 11'}` | Form answers |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Return | `'Computer Model: TestModel X1\nOS: Windows 11'` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Exact block | Blank answers included or keys unstripped |

### 28. test_format_env_detail_empty_or_invalid_is_none_marker

*Class:* `TestHandsfreeEnvDetail`

**Description:** **Branch-specific.** `{}`, `None`, a non-dict, and an all-blank form all render as `(none)` so the prompt never contains an empty section.

**Code:**
```python
def test_format_env_detail_empty_or_invalid_is_none_marker(self):
    for env in ({}, None, "not a dict", {"Platform": ""}):
        with self.subTest(env=env):
            self.assertEqual(_format_env_detail(env), "(none)")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `env` | `{}`, `None`, `'not a dict'`, `{'Platform': ''}` | subTests |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Return | `'(none)'` for each | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Uniform marker | Exception or empty string |

### 29. test_format_env_detail_truncates_long_answers_and_caps_entries

*Class:* `TestHandsfreeEnvDetail`

**Description:** **Branch-specific.** Prompt-size guards: at most 25 entries (`_MAX_ENV_ENTRIES`) and answers longer than 300 chars (`_MAX_ENV_VALUE_CHARS`) are cut with ` …[truncated]`.

**Code:**
```python
def test_format_env_detail_truncates_long_answers_and_caps_entries(self):
    # Arrange
    env = {f"Q{i}": "x" for i in range(30)}
    env["Q0"] = "a" * 400

    # Act
    lines = _format_env_detail(env).splitlines()

    # Assert
    self.assertEqual(len(lines), 25)  # _MAX_ENV_ENTRIES
    self.assertEqual(lines[0], "Q0: " + "a" * 300 + " …[truncated]")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `env` | 30 keys `Q0..Q29` = `'x'`, with `Q0 = 'a' * 400` | Oversized form |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Line count | `25` | `assertEqual` |
| First line | `'Q0: ' + 'a'*300 + ' …[truncated]'` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Caps enforced | Unbounded prompt growth |

### 30. test_env_repro_steps_returns_filled_answer

*Class:* `TestHandsfreeEnvDetail`

**Description:** **Branch-specific.** A filled *Steps to reproduce* answer is returned stripped; the runner uses its truthiness to retract a reader-flagged `repro_steps` gap.

**Code:**
```python
def test_env_repro_steps_returns_filled_answer(self):
    # Arrange / Act / Assert
    self.assertEqual(
        _env_repro_steps({"Computer Model": "X1", "Steps to reproduce": " 1. Sleep 2. Resume "}),
        "1. Sleep 2. Resume")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `env` | `{'Computer Model': 'X1', 'Steps to reproduce': ' 1. Sleep 2. Resume '}` | Filled form |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Return | `'1. Sleep 2. Resume'` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Answer found | Gap not retracted for a filled form |

### 31. test_env_repro_steps_ignores_placeholder_answers

*Class:* `TestHandsfreeEnvDetail`

**Description:** **Branch-specific.** Placeholder answers (`''`, `NA`, `n/a`, `None`, blanks — case-insensitive) and a `None` form return `''`, so the 'repro steps missing' gap is kept.

**Code:**
```python
def test_env_repro_steps_ignores_placeholder_answers(self):
    for answer in ("", "NA", "n/a", "None", "   "):
        with self.subTest(answer=answer):
            self.assertEqual(_env_repro_steps({"Steps to reproduce": answer}), "")
    self.assertEqual(_env_repro_steps(None), "")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `env['Steps to reproduce']` | `''`, `'NA'`, `'n/a'`, `'None'`, `'   '`; plus `env=None` | subTests |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Return | `''` for each | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Placeholders ignored | `NA` treated as real repro steps |

### 32. test_env_brief_keeps_only_platform_fields

*Class:* `TestHandsfreeEnvDetail`

**Description:** **Branch-specific.** The agent brief keeps only platform / build / OS / frequency / tested-hardware / computer-model fields with real answers, whitespace-collapsed and `; `-joined in form order.

**Code:**
```python
def test_env_brief_keeps_only_platform_fields(self):
    # Arrange
    env = {"Platform": "Lunar Lake", "Found in build": "  23.40  ", "Notes": "ignored",
           "Operating System": "n/a", "Computer Model": "TestModel X1"}

    # Act
    brief = _env_brief(env)

    # Assert
    self.assertEqual(brief, "Platform: Lunar Lake; Found in build: 23.40; Computer Model: TestModel X1")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `env` | `{'Platform': 'Lunar Lake', 'Found in build': '  23.40  ', 'Notes': 'ignored', 'Operating System': 'n/a', 'Computer Model': 'TestModel X1'}` | Mixed form |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Return | `'Platform: Lunar Lake; Found in build: 23.40; Computer Model: TestModel X1'` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Exact brief | Irrelevant or placeholder fields leak in |

### 33. test_env_brief_respects_limit

*Class:* `TestHandsfreeEnvDetail`

**Description:** **Branch-specific.** The brief is truncated to `limit` characters (default 300).

**Code:**
```python
def test_env_brief_respects_limit(self):
    self.assertEqual(len(_env_brief({"Platform": "p" * 500})), 300)
    self.assertEqual(_env_brief({"Platform": "Lunar Lake"}, limit=8), "Platform")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `env` | `{'Platform': 'p' * 500}` / `{'Platform': 'Lunar Lake'}` with `limit=8` | Long / custom limit |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Length | `300` | `assertEqual` |
| Return | `'Platform'` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Limit applied | Unbounded brief |

## Test Execution Flow

```
setUpClass (BaseTestCase subclasses) ── patch app_config (configs.global_configs + identity-rebound modules)
     │                                   _store() imports app_config lazily → receives the mock
     ▼
setUp ── reset mock (avatarfiles_dir = tests/_fake_avatarfiles) ── patch CaseContext.to_session
      ── fresh Flask app ── register handsfree_bp ── client ── app ctx
     │
     ├─ store-backed tests: class @patch HandsfreeStore → store = mock_store_cls.return_value
     ├─ run tests:          class @patch orchestrator
     │     Arrange return values → Act client.get/post/put → Assert status, JSON, forwarded args
     │
     └─ TestHandsfreeEnvDetail (plain unittest.TestCase, no app/mocks)
           call _format_env_detail / _env_repro_steps / _env_brief / READER_PROMPT → assertEqual
     │
     ▼
tearDown ──► PASS / FAIL ── tearDownClass stops patchers
```

## Coverage Summary

| Metric | Value |
|---|---|
| Total Routes | 11 endpoints (10 URL rules) |
| Routes Tested | 11 / 11 |
| Total Tests | 33 (1 structure + 24 route + 8 env_detail core-logic) |
| Actual Coverage (%) | 100% of routes and route branches; branch-specific env_detail helpers covered directly |
| Out-of-scope items | 4 (background runner/orchestrator, dry_run gate in orchestrator, store id sanitisation, non-string `draft_plain`) |

## What Would Make These Tests Fail?

| Source change | ✅ PASS | ❌ FAIL |
|---|---|---|
| Remove `{env_block}` from `READER_PROMPT` | all route tests | `test_reader_prompt_has_env_block_placeholder` |
| Include blank env answers / drop truncation | route tests | `_format_env_detail` tests |
| Treat `NA` / `n/a` as real repro steps | route tests | `test_env_repro_steps_ignores_placeholder_answers` |
| Widen `_ENV_BRIEF_KEY_RE` (e.g. include Notes) | route tests | `test_env_brief_keeps_only_platform_fields` |
| Add `auto_post` to the config whitelist | other tests | `test_post_config_saves_only_allowed_keys` |
| Stop prepending `AI_MARKER` on save | other tests | `test_save_draft_prepends_ai_marker_and_composes_html` |
| Allow GET on `/approve` | other tests | `test_approve_rejects_get`, `test_blueprint_registered_with_url_prefix` |
| Change `?all` parsing | other tests | `test_list_queue_all_flag_variants_include_closed` |
| Remove the try/except in `discover_fields` | other tests | `test_discover_fields_exception_returns_500` |
| Root the store somewhere other than `<avatarfiles_dir>/handsfree` | other tests | `test_index_renders_with_store_config` |
| Return 200 for unknown drafts | other tests | `test_get_draft_not_found_returns_404`, `test_save_draft_unknown_id_returns_404` |
