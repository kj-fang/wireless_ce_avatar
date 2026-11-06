# Feedback Blueprint — Analysis & Unit Test Documentation

- Route module: [blueprints/feedback/feedback_routes.py](../blueprints/feedback/feedback_routes.py)
- Test module: [unit_tests/test_feedback_blueprint.py](../unit_tests/test_feedback_blueprint.py)
- Shared fixture: [unit_tests/base_test.py](../unit_tests/base_test.py)
- Service delegated to: [services/feedback_service.py](../services/feedback_service.py), [services/gather_service.py](../services/gather_service.py)

## Overview — What Feedback Does

The `feedback` blueprint (`url_prefix="/feedback"`) is a sidecar that receives user feedback on chatbot (Wi-Fi / BT log-analysis agent) answers: thumbs up/down per turn, a structured "more feedback" form, per-reasoning-step votes, a "which skill helped" pick and per-skill helpful/wrong chips. It is intentionally decoupled from the chatbot itself: it only validates the request and forwards it to `services.feedback_service` (and, for the detail form, a telemetry record via `services.gather_service`). Every client-supplied `conversation_id` / `turn_id` is checked against the allow-list `_SAFE_ID_RE = ^[A-Za-z0-9_-]{1,80}$` at the route boundary, because those IDs become path components downstream. A debug `GET /feedback/recent` returns the latest events. This blueprint does not use `app_config` (no `silent_mode` gating).

## All Routes (6 endpoints)

| Route | Method | Purpose |
|---|---|---|
| `/feedback/vote` | POST | Record a 👍 (1) / 👎 (-1) / clear (0) for a turn; echoes the session's `yaml_modified` flag and path |
| `/feedback/detail` | POST | Record the structured "more feedback" form (issues, corrections, evidence lines, skill/step verdicts, issue-time fields, optional session-log attach); also emits a `gather_service.record_feedback_submit` event; returns a new `feedback_event_id` |
| `/feedback/step_vote` | POST | Record a per-reasoning-step 👍/👎 (`vote` must be 1 or -1) |
| `/feedback/skill_helpful` | POST | Record which skill drove a correct answer (`skill_id` may contain spaces) |
| `/feedback/skill_assessment` | POST | Set / clear a per-skill `helpful` / `wrong` chip |
| `/feedback/recent` | GET | Debug: last N events (`limit` clamped 1..500, default 50; `domain` = ""/wifi or bt) |

## High-Level Flow Diagram

```
 Client JSON ──POST /feedback/<vote|detail|step_vote|skill_helpful|skill_assessment>──┐
                                                                                     ▼
            ┌───────────────────────────────────────────────────────────────────────────────┐
            │ data = request.get_json(silent=True) or {}   (non-JSON body → {})              │
            │ strip conversation_id / turn_id (/ skill_id)                                   │
            └───────────────────────────────────────┬───────────────────────────────────────┘
          /vote only: int(vote) ∈ {1,-1,0}? ── no ──► 400 "vote must be 1, -1, or 0"   (checked BEFORE ids)
                                                    ▼
            required ids present? ─────────── no ──► 400 "... are required"
                                                    ▼
            _SAFE_ID_RE(conversation_id)? ─── no ──► 400 "conversation_id must match [A-Za-z0-9_-]{1,80}"
            _SAFE_ID_RE(turn_id)? ─────────── no ──► 400 "turn_id must match ..."
                                                    ▼
          route-specific validation:
            /step_vote:  int(step_index), int(vote) ── fail ──► 400 "step_index and vote must be integers"
                         vote ∈ {1,-1} ─────────────── no ───► 400 "vote must be 1 or -1"
            /skill_assessment: assessment ∈ SKILL_ASSESSMENT_VALUES or "" ── no ──► 400
            /detail: vote coerced (invalid → None), evidence str→lines / non-list→[],
                     log_path only if attach_log, domain default "wifi", uuid4 event id
                                                    ▼
            session: chatbot_session_id, yaml_modified(, yaml_modified_path, chatbot_log_path,
                     case_context, gather_workflow_id)
                                                    ▼
            feedback_service.record_*(**kwargs) ──── False ──► /detail: 400 "nothing to record…"
                     │                                         others: 500 "failed to record[ vote]"
                     │ True                         (exception → NOT caught → 500)
                     ▼
            /detail: gather_service.record_feedback_submit(feedback_event_id, ..., issue=CaseContext dict)
                     ▼
            200 {"success": true, ...}

 GET /feedback/recent?limit&domain ── int(limit) (ValueError→50), clamp 1..500
                                   ── feedback_service.get_recent_votes(limit, domain=domain)
                                   ── 200 {"success": true, "domain": domain or "wifi", "events": [...]}
```

## Dependencies

```
flask:        Blueprint, request, jsonify, session
stdlib:       re (_SAFE_ID_RE = ^[A-Za-z0-9_-]{1,80}$), uuid (uuid4 feedback_event_id)
models:       models.models.CaseContext.from_session(...).to_dict()   (/detail → gather issue)
services:     services.feedback_service
                .record_vote / .record_detail / .record_step_vote
                .record_helpful_skill / .record_skill_assessment
                .get_recent_votes / .SKILL_ASSESSMENT_VALUES (= ("helpful", "wrong"))
              services.gather_service.record_feedback_submit
app_config:   not used
session keys: chatbot_session_id, yaml_modified, yaml_modified_path, chatbot_log_path,
              case_context, gather_workflow_id   (all optional)
helpers:      _bad_id_response(field_name) → (json, 400)
```

## Test Coverage Analysis

```
STRUCTURE
  ✅ blueprint 'feedback', url_prefix '/feedback', 5 POST rules + GET /recent ── test_blueprint_registered_with_url_prefix

ROUTES — /feedback/vote
  ✅ success, session context forwarded, ids/domain stripped     ── test_vote_success_records_with_session_context
  ✅ vote "0" accepted, empty-session defaults                     ── test_vote_zero_string_accepted_with_empty_session_defaults
  ✅ vote None / "up" / 2 / -2 → 400                              ── test_vote_invalid_values_return_400
  ✅ non-JSON body → vote check runs first → 400                   ── test_vote_non_json_body_fails_vote_check_first
  ✅ missing / blank ids → 400                                     ── test_vote_missing_ids_return_400
  ✅ unsafe conversation_id / turn_id → 400 (_bad_id_response)     ── test_vote_rejects_unsafe_ids
  ✅ service returns False → 500                                   ── test_vote_service_failure_returns_500
  ✅ service raises → propagates (not caught)                      ── test_vote_service_exception_is_not_caught

ROUTES — /feedback/detail
  ✅ full payload → every record_detail kwarg + gather submit      ── test_detail_success_forwards_all_fields_and_records_gather
  ✅ minimal payload defaults; log ignored without attach_log      ── test_detail_minimal_payload_uses_defaults_and_ignores_log_without_opt_in
  ✅ vote "", "abc", 5, 0, [1] → None                             ── test_detail_invalid_vote_values_are_coerced_to_none
  ✅ evidence string → stripped non-blank lines                    ── test_detail_evidence_string_is_split_into_lines
  ✅ evidence non-list/non-str → []                               ── test_detail_evidence_of_other_type_becomes_empty_list
  ✅ missing ids → 400, nothing recorded                           ── test_detail_missing_ids_return_400
  ✅ unsafe ids → 400                                              ── test_detail_rejects_unsafe_ids
  ✅ service returns False → 400, gather skipped                   ── test_detail_nothing_recorded_returns_400_and_skips_gather
  ⚪ `except Exception: issue = {}` around CaseContext.from_session ── unreachable with a cookie session: from_session drops unknown keys; would need a corrupt sidecar

ROUTES — /feedback/step_vote
  ✅ success, step_index "3" → 3, domain stripped                 ── test_step_vote_success
  ✅ missing ids → 400                                             ── test_step_vote_missing_ids_return_400
  ✅ unsafe ids → 400                                              ── test_step_vote_rejects_unsafe_ids
  ✅ non-integer / missing step_index or vote → 400                ── test_step_vote_non_integer_fields_return_400
  ✅ vote 0 → 400 "vote must be 1 or -1"                          ── test_step_vote_zero_vote_rejected
  ✅ service False → 500                                           ── test_step_vote_service_failure_returns_500

ROUTES — /feedback/skill_helpful
  ✅ success, skill_id with spaces allowed and stripped            ── test_skill_helpful_success_allows_spaces_in_skill_id
  ✅ any of the 3 required fields missing → 400                    ── test_skill_helpful_missing_fields_return_400
  ✅ unsafe ids → 400                                              ── test_skill_helpful_rejects_unsafe_ids
  ✅ service False → 500                                           ── test_skill_helpful_service_failure_returns_500

ROUTES — /feedback/skill_assessment
  ✅ success, " Helpful " → "helpful"                              ── test_skill_assessment_success_normalises_case
  ✅ empty assessment clears chip                                  ── test_skill_assessment_empty_value_clears_chip
  ✅ "redundant" → 400 with allowed values                         ── test_skill_assessment_invalid_value_returns_400
  ✅ skill_id missing → 400                                        ── test_skill_assessment_missing_fields_return_400
  ✅ unsafe ids → 400                                              ── test_skill_assessment_rejects_unsafe_ids
  ✅ service False → 500                                           ── test_skill_assessment_service_failure_returns_500

ROUTES — /feedback/recent
  ✅ defaults: limit 50, domain "" → "wifi" in response            ── test_recent_defaults_to_50_and_wifi
  ✅ limit=10, domain=bt                                            ── test_recent_bt_domain_and_explicit_limit
  ✅ clamp 0→1, -5→1, 9999→500, "abc"→50                          ── test_recent_limit_is_clamped_and_non_numeric_falls_back

CORE LOGIC
  ✅ _SAFE_ID_RE allow-list (traversal, slash, space, 81 chars)    ── all *_rejects_unsafe_ids tests
  ✅ _bad_id_response exact message/status                         ── assert_bad_id helper in the same tests
  ⚪ feedback_service internals (_safe_id, JSONL writes, weighting) ── service layer, mocked here by design

BACKGROUND / SOCKETIO
  ⚪ none ── this blueprint has no SocketIO handlers or threads
```

## What the Unit Tests Actually Test

Shared fixture (`FeedbackTestCase.setUp`, inherited by every class): `blueprint = feedback_bp`; `patch("blueprints.feedback.feedback_routes.feedback_service")` and `patch("blueprints.feedback.feedback_routes.gather_service")` are started for every test; `feedback_service.SKILL_ASSESSMENT_VALUES = ("helpful", "wrong")` (the real value); `record_vote / record_detail / record_step_vote / record_helpful_skill / record_skill_assessment` return `True`; `get_recent_votes` returns `[{"vote": 1}]`.

Module constants: `CONV_ID = "conv_123"`, `TURN_ID = "turn-7"`, `SESSION_ID = "sess-abc"`, `FIXED_UUID = uuid.UUID("12345678-1234-5678-1234-567812345678")`, `UNSAFE_IDS = ["../etc", "a/b", "has space", "x" * 81]`.

Helpers: `post_json(path, payload)` → `self.client.post(path, json=payload)`; `assert_bad_id(resp, field_name)` asserts status `400` and body `{"success": False, "error": f"{field_name} must match [A-Za-z0-9_-]{{1,80}}"}`.

### 1. test_blueprint_registered_with_url_prefix

**Description:** Verifies the blueprint is registered as `feedback` with prefix `/feedback`, that the five write endpoints accept POST and `/feedback/recent` accepts GET. Catches renamed routes or method changes that would silently break the front-end `fetch` calls.

**Code:**
```python
def test_blueprint_registered_with_url_prefix(self):
    # Arrange / Act
    rules = {r.rule: r.methods for r in self.app.url_map.iter_rules()
             if r.endpoint.startswith("feedback.")}

    # Assert
    self.assertIs(self.app.blueprints.get("feedback"), feedback_bp)
    self.assertEqual(feedback_bp.url_prefix, "/feedback")
    for path in ("/feedback/vote", "/feedback/detail", "/feedback/step_vote",
                 "/feedback/skill_helpful", "/feedback/skill_assessment"):
        self.assertIn(path, rules)
        self.assertIn("POST", rules[path])
    self.assertIn("GET", rules["/feedback/recent"])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Registered blueprint | `feedback_bp` | Registered in `BaseTestCase.setUp` |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `app.blueprints["feedback"]` | `feedback_bp` | `assertIs` |
| `url_prefix` | `"/feedback"` | `assertEqual` |
| POST rules | `/feedback/vote`, `/detail`, `/step_vote`, `/skill_helpful`, `/skill_assessment` | `assertIn` |
| GET rule | `/feedback/recent` | `assertIn("GET", ...)` |

| Pass Condition | Fail Condition |
|---|---|
| All six rules present with the right methods | Prefix/name changed, route removed or method changed |

### 2. test_vote_success_records_with_session_context

**Description:** Happy path for `/vote`. Verifies the session-derived context (`chatbot_session_id`, `yaml_modified`, `yaml_modified_path`) is forwarded/echoed, that `conversation_id` and `domain` are whitespace-stripped, and that `record_vote` receives exactly the expected kwargs.

**Code:**
```python
def test_vote_success_records_with_session_context(self):
    # Arrange
    with self.client.session_transaction() as sess:
        sess["chatbot_session_id"] = SESSION_ID
        sess["yaml_modified"] = True
        sess["yaml_modified_path"] = r"C:\fake\skills.yaml"

    # Act
    resp = self.post_json("/feedback/vote", {
        "conversation_id": f" {CONV_ID} ", "turn_id": TURN_ID,
        "vote": 1, "domain": " bt ",
    })

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_json(), {
        "success": True, "yaml_modified": True,
        "yaml_modified_path": r"C:\fake\skills.yaml",
    })
    self.feedback_service.record_vote.assert_called_once_with(
        session_id=SESSION_ID, conversation_id=CONV_ID, turn_id=TURN_ID,
        vote=1, yaml_modified=True, domain="bt",
    )
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `session["chatbot_session_id"]` | `"sess-abc"` | Anonymous chatbot session |
| `session["yaml_modified"]` | `True` | Skill YAML edited earlier |
| `session["yaml_modified_path"]` | `r"C:\fake\skills.yaml"` | Echoed back |
| JSON body | `{"conversation_id": " conv_123 ", "turn_id": "turn-7", "vote": 1, "domain": " bt "}` | Padded values |
| `feedback_service.record_vote` | returns `True` | Mocked |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| JSON | `{"success": True, "yaml_modified": True, "yaml_modified_path": "C:\\fake\\skills.yaml"}` | `assertEqual` |
| `record_vote` kwargs | `session_id="sess-abc", conversation_id="conv_123", turn_id="turn-7", vote=1, yaml_modified=True, domain="bt"` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Exact kwargs once and echoed flags | Stripping removed, session keys renamed, response shape changed |

### 3. test_vote_zero_string_accepted_with_empty_session_defaults

**Description:** `vote` is parsed with `int()`, so the string `"0"` (clear vote) is valid. With no session keys, `session_id` defaults to `""`, `yaml_modified` to `False`, and the echoed path to `""`.

**Code:**
```python
def test_vote_zero_string_accepted_with_empty_session_defaults(self):
    # Arrange: no session keys; vote "0" (clear) as a string

    # Act
    resp = self.post_json("/feedback/vote", {
        "conversation_id": CONV_ID, "turn_id": TURN_ID, "vote": "0",
    })

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_json(), {
        "success": True, "yaml_modified": False, "yaml_modified_path": "",
    })
    self.feedback_service.record_vote.assert_called_once_with(
        session_id="", conversation_id=CONV_ID, turn_id=TURN_ID,
        vote=0, yaml_modified=False, domain="",
    )
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Session | *(empty)* | No chatbot keys |
| JSON body | `{"conversation_id": "conv_123", "turn_id": "turn-7", "vote": "0"}` | String vote |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| JSON | `{"success": True, "yaml_modified": False, "yaml_modified_path": ""}` | `assertEqual` |
| `record_vote` kwargs | `session_id="", ..., vote=0, yaml_modified=False, domain=""` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| `"0"` coerced to `0` and recorded | 0 rejected, or defaults changed |

### 4. test_vote_invalid_values_return_400

**Description:** Covers both `/vote` vote-validation branches: `int()` failure (`None`, `"up"`) and out-of-range integers (`2`, `-2`). Each must return 400 with the same message and no service call.

**Code:**
```python
def test_vote_invalid_values_return_400(self):
    for bad_vote in (None, "up", 2, -2):
        with self.subTest(vote=bad_vote):
            # Act
            resp = self.post_json("/feedback/vote", {
                "conversation_id": CONV_ID, "turn_id": TURN_ID, "vote": bad_vote,
            })

            # Assert
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(resp.get_json()["error"], "vote must be 1, -1, or 0")
    self.feedback_service.record_vote.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `vote` (subTests) | `None`, `"up"`, `2`, `-2` | TypeError, ValueError, out of range ×2 |
| ids | `"conv_123"` / `"turn-7"` | Valid |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` (each) | `assertEqual` |
| `error` | `"vote must be 1, -1, or 0"` | `assertEqual` |
| `record_vote` | not called | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| Every bad vote rejected | Any value accepted or message changed |

### 5. test_vote_non_json_body_fails_vote_check_first

**Description:** A non-JSON body yields `data = {}` (via `get_json(silent=True) or {}`). Because `/vote` validates `vote` before the IDs, the error is the vote message, not "ids required". Pins down the validation order.

**Code:**
```python
def test_vote_non_json_body_fails_vote_check_first(self):
    # Arrange: no JSON at all -> data == {}; vote is validated before ids

    # Act
    resp = self.client.post("/feedback/vote", data="not json")

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertEqual(resp.get_json()["error"], "vote must be 1, -1, or 0")
    self.feedback_service.record_vote.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Body | `data="not json"` (no JSON content type) | `get_json(silent=True)` → `None` |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` | `assertEqual` |
| `error` | `"vote must be 1, -1, or 0"` | `assertEqual` |
| `record_vote` | not called | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| Vote error first | Order swapped (ids error) or route crashes on non-JSON |

### 6. test_vote_missing_ids_return_400

**Description:** With a valid vote, a missing or whitespace-only `conversation_id`/`turn_id` returns 400 "conversation_id and turn_id are required".

**Code:**
```python
def test_vote_missing_ids_return_400(self):
    for payload in ({"turn_id": TURN_ID}, {"conversation_id": CONV_ID},
                    {"conversation_id": "   ", "turn_id": TURN_ID}):
        with self.subTest(payload=payload):
            # Act
            resp = self.post_json("/feedback/vote", {**payload, "vote": 1})

            # Assert
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(resp.get_json()["error"],
                             "conversation_id and turn_id are required")
    self.feedback_service.record_vote.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Payloads (+ `"vote": 1`) | `{"turn_id": "turn-7"}`, `{"conversation_id": "conv_123"}`, `{"conversation_id": "   ", "turn_id": "turn-7"}` | Missing / blank id |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` | `assertEqual` |
| `error` | `"conversation_id and turn_id are required"` | `assertEqual` |
| `record_vote` | not called | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| All three rejected | Blank id accepted (strip removed) |

### 7. test_vote_rejects_unsafe_ids

**Description:** Exercises `_SAFE_ID_RE` / `_bad_id_response` for both IDs with path traversal (`../etc`), a slash, a space and an 81-char string. This is the defense-in-depth path-injection guard.

**Code:**
```python
def test_vote_rejects_unsafe_ids(self):
    for bad in UNSAFE_IDS:
        with self.subTest(conversation_id=bad):
            resp = self.post_json("/feedback/vote",
                                  {"conversation_id": bad, "turn_id": TURN_ID, "vote": 1})
            self.assert_bad_id(resp, "conversation_id")
        with self.subTest(turn_id=bad):
            resp = self.post_json("/feedback/vote",
                                  {"conversation_id": CONV_ID, "turn_id": bad, "vote": 1})
            self.assert_bad_id(resp, "turn_id")
    self.feedback_service.record_vote.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Unsafe ids | `"../etc"`, `"a/b"`, `"has space"`, `"x" * 81` | Each placed in `conversation_id`, then `turn_id` |
| `vote` | `1` | Valid |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` | `assert_bad_id` |
| JSON | `{"success": False, "error": "<field> must match [A-Za-z0-9_-]{1,80}"}` | `assert_bad_id` |
| `record_vote` | not called | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| All 8 combos rejected with the field-specific message | Regex loosened (e.g. allows `/`, `.` or >80 chars) |

### 8. test_vote_service_failure_returns_500

**Description:** When `record_vote` returns `False`, the route answers 500 `{"success": False, "error": "failed to record vote"}`.

**Code:**
```python
def test_vote_service_failure_returns_500(self):
    # Arrange
    self.feedback_service.record_vote.return_value = False

    # Act
    resp = self.post_json("/feedback/vote",
                          {"conversation_id": CONV_ID, "turn_id": TURN_ID, "vote": -1})

    # Assert
    self.assertEqual(resp.status_code, 500)
    self.assertEqual(resp.get_json(), {"success": False, "error": "failed to record vote"})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `record_vote` | returns `False` | Write failed |
| JSON | `{"conversation_id": "conv_123", "turn_id": "turn-7", "vote": -1}` | Valid |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `500` | `assertEqual` |
| JSON | `{"success": False, "error": "failed to record vote"}` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| 500 + exact body | Failure ignored (200) |

### 9. test_vote_service_exception_is_not_caught

**Description:** Documents current behavior: no route wraps the service call in `try/except`, so an exception from `feedback_service` propagates (500 in production). The module docstring claims "failures here never affect chat behaviour" — that holds only because the endpoint is separate, not because errors are handled.

**Code:**
```python
def test_vote_service_exception_is_not_caught(self):
    # Arrange: the route has no try/except around the service call, so a
    # raising service surfaces as an unhandled error (500 in production).
    self.feedback_service.record_vote.side_effect = RuntimeError("disk full")

    # Act / Assert
    with self.assertRaises(RuntimeError):
        self.post_json("/feedback/vote",
                       {"conversation_id": CONV_ID, "turn_id": TURN_ID, "vote": 1})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `record_vote` side effect | `RuntimeError("disk full")` | Service blows up |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Exception | `RuntimeError` propagates (TESTING=True) | `assertRaises` |

| Pass Condition | Fail Condition |
|---|---|
| Exception propagates | Route starts catching it (then update test to the new contract) |

### 10. test_detail_success_forwards_all_fields_and_records_gather

**Description:** Full `/detail` payload. Verifies every one of the 23 `record_detail` kwargs: string fields stripped, `vote` `"-1"` → `-1`, `domain " BT "` → `"bt"`, `log_path` taken from the session because `attach_log=True`, and a deterministic `feedback_event_id` from patched `uuid4`. Also verifies the `gather_service.record_feedback_submit` telemetry call including the rehydrated case context and workflow id.

**Code:**
```python
@patch(f"{ROUTES}.uuid.uuid4", return_value=FIXED_UUID)
def test_detail_success_forwards_all_fields_and_records_gather(self, _mock_uuid):
    # Arrange
    self.seed_session(
        chatbot_session_id=SESSION_ID, yaml_modified=True,
        chatbot_log_path=r"C:\fake\session.log", gather_workflow_id="wf-1",
    )
    issues = [{"scope": "overall", "category": "bad_output", "comment": "wrong"}]
    skill_feedback = [{"skill_id": "Connection Flow", "verdict": "wrong"}]
    step_feedback = [{"step_index": 2, "verdict": "helpful"}]

    # Act
    resp = self.post_json("/feedback/detail", {
        "conversation_id": CONV_ID, "turn_id": TURN_ID, "vote": "-1",
        "issues": issues,
        "correct_root_cause": " AP deauth ", "correct_conclusion_tag": " deauth ",
        "correct_skill": " Roaming ", "correct_approach": " check 802.11 mgmt ",
        "evidence_log_lines": ["line 1", "line 2"],
        "agent_workflow": " wf ", "feedback_layer": " skill ",
        "skill_feedback": skill_feedback, "step_feedback": step_feedback,
        "issue_time_problem": " wrong_time ", "correct_issue_time": " 10:00 ",
        "used_issue_time": " 09:00 ", "log_has_date": False,
        "attach_log": True, "domain": " BT ",
    })

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_json(), {"success": True, "feedback_event_id": str(FIXED_UUID)})
    self.feedback_service.record_detail.assert_called_once_with(
        session_id=SESSION_ID, conversation_id=CONV_ID, turn_id=TURN_ID, vote=-1,
        issues=issues, correct_root_cause="AP deauth", correct_conclusion_tag="deauth",
        correct_skill="Roaming", correct_approach="check 802.11 mgmt",
        evidence_log_lines=["line 1", "line 2"], agent_workflow="wf",
        feedback_layer="skill", skill_feedback=skill_feedback, step_feedback=step_feedback,
        issue_time_problem="wrong_time", correct_issue_time="10:00", used_issue_time="09:00",
        log_has_date=False, yaml_modified=True, log_path=r"C:\fake\session.log",
        attach_log=True, domain="bt", feedback_event_id=str(FIXED_UUID),
    )
    self.gather_service.record_feedback_submit.assert_called_once_with(
        feedback_event_id=str(FIXED_UUID), conversation_id=CONV_ID, turn_id=TURN_ID,
        workflow_id="wf-1", session_id=SESSION_ID,
        issue=self.create_mock_case_context(), domain="bt",
    )
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `uuid.uuid4` | patched → `UUID("12345678-1234-5678-1234-567812345678")` | Deterministic event id |
| `session["case_context"]` | `create_mock_case_context()` | Case `00123456` |
| `session["chatbot_session_id"]` | `"sess-abc"` | |
| `session["yaml_modified"]` | `True` | Priority flag |
| `session["chatbot_log_path"]` | `r"C:\fake\session.log"` | Server-side log path |
| `session["gather_workflow_id"]` | `"wf-1"` | Telemetry workflow |
| JSON `vote` / `domain` / `attach_log` / `log_has_date` | `"-1"` / `" BT "` / `True` / `False` | Coercions under test |
| JSON text fields | `" AP deauth "`, `" deauth "`, `" Roaming "`, `" check 802.11 mgmt "`, `" wf "`, `" skill "`, `" wrong_time "`, `" 10:00 "`, `" 09:00 "` | Padded |
| JSON lists | `issues`, `skill_feedback`, `step_feedback`, `evidence_log_lines=["line 1","line 2"]` | Passed through |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `200`, `{"success": True, "feedback_event_id": "12345678-1234-5678-1234-567812345678"}` | `assertEqual` |
| `record_detail` | all kwargs as in the code (stripped strings, `vote=-1`, `domain="bt"`, `log_path=r"C:\fake\session.log"`) | `assert_called_once_with` |
| `record_feedback_submit` | `feedback_event_id=..., conversation_id="conv_123", turn_id="turn-7", workflow_id="wf-1", session_id="sess-abc", issue=create_mock_case_context(), domain="bt"` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Exact forwarding of every field and one telemetry call | Any field dropped/renamed, strip/lower removed, event id mismatch between response and services |

### 11. test_detail_minimal_payload_uses_defaults_and_ignores_log_without_opt_in

**Description:** With only the two IDs, verifies default values: `vote=None`, empty lists, `log_has_date=True`, `attach_log=False`, `domain="wifi"`. Most important: `log_path` stays `""` even though the session has `chatbot_log_path` — the log is shipped only on explicit opt-in. Without a session case context, `issue` is a default `CaseContext().to_dict()`.

**Code:**
```python
@patch(f"{ROUTES}.uuid.uuid4", return_value=FIXED_UUID)
def test_detail_minimal_payload_uses_defaults_and_ignores_log_without_opt_in(self, _mock_uuid):
    # Arrange: a log path exists in session, but attach_log is not set
    with self.client.session_transaction() as sess:
        sess["chatbot_log_path"] = r"C:\fake\session.log"

    # Act
    resp = self.post_json("/feedback/detail", {"conversation_id": CONV_ID, "turn_id": TURN_ID})

    # Assert
    self.assertEqual(resp.status_code, 200)
    kwargs = self.feedback_service.record_detail.call_args.kwargs
    self.assertIsNone(kwargs["vote"])
    self.assertEqual(kwargs["issues"], [])
    self.assertEqual(kwargs["evidence_log_lines"], [])
    self.assertEqual(kwargs["skill_feedback"], [])
    self.assertEqual(kwargs["step_feedback"], [])
    self.assertIs(kwargs["log_has_date"], True)
    self.assertIs(kwargs["attach_log"], False)
    self.assertEqual(kwargs["log_path"], "")
    self.assertEqual(kwargs["domain"], "wifi")
    self.assertEqual(kwargs["session_id"], "")
    gather_kwargs = self.gather_service.record_feedback_submit.call_args.kwargs
    # No case_context in session -> from_session({}) -> default CaseContext
    self.assertEqual(gather_kwargs["issue"], CaseContext().to_dict())
    self.assertEqual(gather_kwargs["workflow_id"], "")
    self.assertEqual(gather_kwargs["domain"], "wifi")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `session["chatbot_log_path"]` | `r"C:\fake\session.log"` | Present but must NOT be used |
| JSON | `{"conversation_id": "conv_123", "turn_id": "turn-7"}` | Minimal |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| `record_detail` kwargs | `vote=None`, `issues=[]`, `evidence_log_lines=[]`, `skill_feedback=[]`, `step_feedback=[]`, `log_has_date=True`, `attach_log=False`, `log_path=""`, `domain="wifi"`, `session_id=""` | `assertIsNone` / `assertEqual` / `assertIs` |
| gather kwargs | `issue=CaseContext().to_dict()`, `workflow_id=""`, `domain="wifi"` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Defaults match and log not attached | Log path leaked without opt-in, or default domain changed |

### 12. test_detail_invalid_vote_values_are_coerced_to_none

**Description:** Unlike `/vote`, `/detail` never rejects a bad vote: `""` → None (explicit), `"abc"` → ValueError → None, `[1]` → TypeError → None, `5` and `0` → not in `(1, -1, None)` → None. All still return 200.

**Code:**
```python
def test_detail_invalid_vote_values_are_coerced_to_none(self):
    for raw_vote in ("", "abc", 5, 0, [1]):
        with self.subTest(vote=raw_vote):
            # Act
            resp = self.post_json("/feedback/detail", {
                "conversation_id": CONV_ID, "turn_id": TURN_ID, "vote": raw_vote,
            })

            # Assert
            self.assertEqual(resp.status_code, 200)
            self.assertIsNone(self.feedback_service.record_detail.call_args.kwargs["vote"])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `vote` (subTests) | `""`, `"abc"`, `5`, `0`, `[1]` | Empty, ValueError, out-of-range ×2, TypeError |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| `record_detail(vote=...)` | `None` | `assertIsNone` |

| Pass Condition | Fail Condition |
|---|---|
| Every value coerced to None | Exception escapes or a 0/5 is forwarded |

### 13. test_detail_evidence_string_is_split_into_lines

**Description:** Legacy textarea payloads send `evidence_log_lines` as one string; the route splits it into lines, drops blank/whitespace-only lines and right-strips each.

**Code:**
```python
def test_detail_evidence_string_is_split_into_lines(self):
    # Act
    resp = self.post_json("/feedback/detail", {
        "conversation_id": CONV_ID, "turn_id": TURN_ID,
        "evidence_log_lines": "first line  \n\n   \nsecond line\n",
    })

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(
        self.feedback_service.record_detail.call_args.kwargs["evidence_log_lines"],
        ["first line", "second line"],
    )
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `evidence_log_lines` | `"first line  \n\n   \nsecond line\n"` | String with trailing spaces and blank lines |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| `record_detail(evidence_log_lines=...)` | `["first line", "second line"]` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Two clean lines | Raw string forwarded or blanks kept |

### 14. test_detail_evidence_of_other_type_becomes_empty_list

**Description:** Any `evidence_log_lines` value that is neither `str` nor `list` (here `42`) is replaced with `[]`.

**Code:**
```python
def test_detail_evidence_of_other_type_becomes_empty_list(self):
    # Act
    resp = self.post_json("/feedback/detail", {
        "conversation_id": CONV_ID, "turn_id": TURN_ID, "evidence_log_lines": 42,
    })

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(self.feedback_service.record_detail.call_args.kwargs["evidence_log_lines"], [])
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `evidence_log_lines` | `42` | Wrong type |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| `evidence_log_lines` kwarg | `[]` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| `[]` forwarded | `42` forwarded to the service |

### 15. test_detail_missing_ids_return_400

**Description:** `/detail` requires both IDs; an empty body or either ID alone returns 400, and neither the feedback service nor gather telemetry is called.

**Code:**
```python
def test_detail_missing_ids_return_400(self):
    for payload in ({}, {"conversation_id": CONV_ID}, {"turn_id": TURN_ID}):
        with self.subTest(payload=payload):
            resp = self.post_json("/feedback/detail", payload)
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(resp.get_json()["error"], "conversation_id and turn_id are required")
    self.feedback_service.record_detail.assert_not_called()
    self.gather_service.record_feedback_submit.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Payloads | `{}`, `{"conversation_id": "conv_123"}`, `{"turn_id": "turn-7"}` | Missing ids |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / `error` | `400` / `"conversation_id and turn_id are required"` | `assertEqual` |
| `record_detail`, `record_feedback_submit` | not called | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| All rejected, no writes | Any payload recorded |

### 16. test_detail_rejects_unsafe_ids

**Description:** Same `_SAFE_ID_RE` guard as `/vote`, applied to `/detail` for both IDs.

**Code:**
```python
def test_detail_rejects_unsafe_ids(self):
    for bad in UNSAFE_IDS:
        with self.subTest(conversation_id=bad):
            self.assert_bad_id(self.post_json("/feedback/detail",
                                              {"conversation_id": bad, "turn_id": TURN_ID}),
                               "conversation_id")
        with self.subTest(turn_id=bad):
            self.assert_bad_id(self.post_json("/feedback/detail",
                                              {"conversation_id": CONV_ID, "turn_id": bad}),
                               "turn_id")
    self.feedback_service.record_detail.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Unsafe ids | `"../etc"`, `"a/b"`, `"has space"`, `"x" * 81` | In each id field |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / body | `400`, `{"success": False, "error": "<field> must match [A-Za-z0-9_-]{1,80}"}` | `assert_bad_id` |
| `record_detail` | not called | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| All rejected | Regex check removed from `/detail` |

### 17. test_detail_nothing_recorded_returns_400_and_skips_gather

**Description:** When `record_detail` returns `False` (all fields empty), `/detail` returns **400** (not 500, unlike the other routes) with "nothing to record (all fields empty)", and the gather telemetry event is not emitted.

**Code:**
```python
def test_detail_nothing_recorded_returns_400_and_skips_gather(self):
    # Arrange
    self.feedback_service.record_detail.return_value = False

    # Act
    resp = self.post_json("/feedback/detail", {"conversation_id": CONV_ID, "turn_id": TURN_ID})

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertEqual(resp.get_json(), {"success": False, "error": "nothing to record (all fields empty)"})
    self.gather_service.record_feedback_submit.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `record_detail` | returns `False` | Nothing recorded |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `400` | `assertEqual` |
| JSON | `{"success": False, "error": "nothing to record (all fields empty)"}` | `assertEqual` |
| `record_feedback_submit` | not called | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| 400, no telemetry | Telemetry sent for an empty submission |

### 18. test_step_vote_success

**Description:** Happy path for `/step_vote`: `step_index` `"3"` coerced to `3`, `vote -1`, `domain` stripped, session id forwarded.

**Code:**
```python
def test_step_vote_success(self):
    # Arrange
    with self.client.session_transaction() as sess:
        sess["chatbot_session_id"] = SESSION_ID

    # Act
    resp = self.post_json("/feedback/step_vote", {
        "conversation_id": CONV_ID, "turn_id": TURN_ID,
        "step_index": "3", "vote": -1, "domain": " bt ",
    })

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_json(), {"success": True})
    self.feedback_service.record_step_vote.assert_called_once_with(
        session_id=SESSION_ID, conversation_id=CONV_ID, turn_id=TURN_ID,
        step_index=3, vote=-1, domain="bt",
    )
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `session["chatbot_session_id"]` | `"sess-abc"` | |
| JSON | `{"conversation_id": "conv_123", "turn_id": "turn-7", "step_index": "3", "vote": -1, "domain": " bt "}` | |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `200`, `{"success": True}` | `assertEqual` |
| `record_step_vote` | `session_id="sess-abc", conversation_id="conv_123", turn_id="turn-7", step_index=3, vote=-1, domain="bt"` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Exact kwargs | Coercion/strip removed |

### 19. test_step_vote_missing_ids_return_400

**Description:** Missing `conversation_id` on `/step_vote` → 400, no service call.

**Code:**
```python
def test_step_vote_missing_ids_return_400(self):
    # Act
    resp = self.post_json("/feedback/step_vote", {"turn_id": TURN_ID, "step_index": 1, "vote": 1})

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertEqual(resp.get_json()["error"], "conversation_id and turn_id are required")
    self.feedback_service.record_step_vote.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| JSON | `{"turn_id": "turn-7", "step_index": 1, "vote": 1}` | No conversation_id |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / `error` | `400` / `"conversation_id and turn_id are required"` | `assertEqual` |
| `record_step_vote` | not called | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| Rejected | Recorded without id |

### 20. test_step_vote_rejects_unsafe_ids

**Description:** `_SAFE_ID_RE` guard on `/step_vote` for both IDs.

**Code:**
```python
def test_step_vote_rejects_unsafe_ids(self):
    base = {"step_index": 1, "vote": 1}
    for bad in UNSAFE_IDS:
        with self.subTest(conversation_id=bad):
            self.assert_bad_id(self.post_json("/feedback/step_vote",
                                              {**base, "conversation_id": bad, "turn_id": TURN_ID}),
                               "conversation_id")
        with self.subTest(turn_id=bad):
            self.assert_bad_id(self.post_json("/feedback/step_vote",
                                              {**base, "conversation_id": CONV_ID, "turn_id": bad}),
                               "turn_id")
    self.feedback_service.record_step_vote.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Unsafe ids | `"../etc"`, `"a/b"`, `"has space"`, `"x" * 81` | |
| Other fields | `step_index=1`, `vote=1` | Valid |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / body | `400` + field-specific message | `assert_bad_id` |
| `record_step_vote` | not called | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| All rejected | Regex removed |

### 21. test_step_vote_non_integer_fields_return_400

**Description:** `step_index` / `vote` must parse as integers: `"x"` (ValueError), `None` (TypeError) and a missing `step_index` all return 400.

**Code:**
```python
def test_step_vote_non_integer_fields_return_400(self):
    for payload in ({"step_index": "x", "vote": 1}, {"step_index": 1, "vote": None},
                    {"vote": 1}):
        with self.subTest(payload=payload):
            resp = self.post_json("/feedback/step_vote",
                                  {"conversation_id": CONV_ID, "turn_id": TURN_ID, **payload})
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(resp.get_json()["error"], "step_index and vote must be integers")
    self.feedback_service.record_step_vote.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Payload variants | `{"step_index": "x", "vote": 1}`, `{"step_index": 1, "vote": None}`, `{"vote": 1}` | Bad / missing ints |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / `error` | `400` / `"step_index and vote must be integers"` | `assertEqual` |
| `record_step_vote` | not called | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| All rejected | Exception escapes or bad value recorded |

### 22. test_step_vote_zero_vote_rejected

**Description:** Unlike `/vote`, a step vote of `0` is invalid → 400 "vote must be 1 or -1".

**Code:**
```python
def test_step_vote_zero_vote_rejected(self):
    # Act: unlike /vote, 0 (clear) is NOT accepted per step
    resp = self.post_json("/feedback/step_vote",
                          {"conversation_id": CONV_ID, "turn_id": TURN_ID, "step_index": 1, "vote": 0})

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertEqual(resp.get_json(), {"success": False, "error": "vote must be 1 or -1"})
    self.feedback_service.record_step_vote.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `vote` | `0` | Not allowed per step |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `400`, `{"success": False, "error": "vote must be 1 or -1"}` | `assertEqual` |
| `record_step_vote` | not called | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| 0 rejected | 0 recorded |

### 23. test_step_vote_service_failure_returns_500

**Description:** `record_step_vote` → `False` yields 500 "failed to record".

**Code:**
```python
def test_step_vote_service_failure_returns_500(self):
    # Arrange
    self.feedback_service.record_step_vote.return_value = False

    # Act
    resp = self.post_json("/feedback/step_vote",
                          {"conversation_id": CONV_ID, "turn_id": TURN_ID, "step_index": 1, "vote": 1})

    # Assert
    self.assertEqual(resp.status_code, 500)
    self.assertEqual(resp.get_json(), {"success": False, "error": "failed to record"})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `record_step_vote` | returns `False` | |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `500`, `{"success": False, "error": "failed to record"}` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| 500 | Failure masked as success |

### 24. test_skill_helpful_success_allows_spaces_in_skill_id

**Description:** `skill_id` is intentionally NOT checked against `_SAFE_ID_RE` (skill names like "Connection Flow" contain spaces); it is only stripped. Verifies the kwargs forwarded to `record_helpful_skill`.

**Code:**
```python
def test_skill_helpful_success_allows_spaces_in_skill_id(self):
    # Arrange
    with self.client.session_transaction() as sess:
        sess["chatbot_session_id"] = SESSION_ID

    # Act
    resp = self.post_json("/feedback/skill_helpful", {
        "conversation_id": CONV_ID, "turn_id": TURN_ID,
        "skill_id": " Connection Flow ", "domain": "bt",
    })

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_json(), {"success": True})
    self.feedback_service.record_helpful_skill.assert_called_once_with(
        session_id=SESSION_ID, conversation_id=CONV_ID, turn_id=TURN_ID,
        skill_id="Connection Flow", domain="bt",
    )
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `session["chatbot_session_id"]` | `"sess-abc"` | |
| JSON | `{"conversation_id": "conv_123", "turn_id": "turn-7", "skill_id": " Connection Flow ", "domain": "bt"}` | Skill with space |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `200`, `{"success": True}` | `assertEqual` |
| `record_helpful_skill` | `session_id="sess-abc", conversation_id="conv_123", turn_id="turn-7", skill_id="Connection Flow", domain="bt"` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Space-containing skill accepted | Regex applied to skill_id |

### 25. test_skill_helpful_missing_fields_return_400

**Description:** All three fields (`conversation_id`, `turn_id`, `skill_id`) are required; dropping any one → 400.

**Code:**
```python
def test_skill_helpful_missing_fields_return_400(self):
    for payload in ({"conversation_id": CONV_ID, "turn_id": TURN_ID},
                    {"conversation_id": CONV_ID, "skill_id": "S"},
                    {"turn_id": TURN_ID, "skill_id": "S"}):
        with self.subTest(payload=payload):
            resp = self.post_json("/feedback/skill_helpful", payload)
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(resp.get_json()["error"],
                             "conversation_id, turn_id, and skill_id are required")
    self.feedback_service.record_helpful_skill.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Payloads | without `skill_id` / without `turn_id` / without `conversation_id` | Each missing one field |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / `error` | `400` / `"conversation_id, turn_id, and skill_id are required"` | `assertEqual` |
| `record_helpful_skill` | not called | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| All rejected | Any partial payload recorded |

### 26. test_skill_helpful_rejects_unsafe_ids

**Description:** `_SAFE_ID_RE` guard on `/skill_helpful` for `conversation_id` and `turn_id` (with a valid `skill_id="S"`).

**Code:**
```python
def test_skill_helpful_rejects_unsafe_ids(self):
    for bad in UNSAFE_IDS:
        with self.subTest(conversation_id=bad):
            self.assert_bad_id(self.post_json("/feedback/skill_helpful",
                                              {"conversation_id": bad, "turn_id": TURN_ID, "skill_id": "S"}),
                               "conversation_id")
        with self.subTest(turn_id=bad):
            self.assert_bad_id(self.post_json("/feedback/skill_helpful",
                                              {"conversation_id": CONV_ID, "turn_id": bad, "skill_id": "S"}),
                               "turn_id")
    self.feedback_service.record_helpful_skill.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Unsafe ids | `"../etc"`, `"a/b"`, `"has space"`, `"x" * 81` | |
| `skill_id` | `"S"` | Valid |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / body | `400` + field-specific message | `assert_bad_id` |
| `record_helpful_skill` | not called | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| All rejected | Regex removed |

### 27. test_skill_helpful_service_failure_returns_500

**Description:** `record_helpful_skill` → `False` yields 500 "failed to record".

**Code:**
```python
def test_skill_helpful_service_failure_returns_500(self):
    # Arrange
    self.feedback_service.record_helpful_skill.return_value = False

    # Act
    resp = self.post_json("/feedback/skill_helpful",
                          {"conversation_id": CONV_ID, "turn_id": TURN_ID, "skill_id": "S"})

    # Assert
    self.assertEqual(resp.status_code, 500)
    self.assertEqual(resp.get_json(), {"success": False, "error": "failed to record"})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `record_helpful_skill` | returns `False` | |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `500`, `{"success": False, "error": "failed to record"}` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| 500 | Failure masked |

### 28. test_skill_assessment_success_normalises_case

**Description:** `assessment` is stripped and lower-cased (`" Helpful "` → `"helpful"`) before the allow-list check and forwarding. Verifies `record_skill_assessment` kwargs (note: no `domain` kwarg on this route).

**Code:**
```python
def test_skill_assessment_success_normalises_case(self):
    # Arrange
    with self.client.session_transaction() as sess:
        sess["chatbot_session_id"] = SESSION_ID

    # Act
    resp = self.post_json("/feedback/skill_assessment", {
        "conversation_id": CONV_ID, "turn_id": TURN_ID,
        "skill_id": "Connection Flow", "assessment": " Helpful ",
    })

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_json(), {"success": True})
    self.feedback_service.record_skill_assessment.assert_called_once_with(
        session_id=SESSION_ID, conversation_id=CONV_ID, turn_id=TURN_ID,
        skill_id="Connection Flow", assessment="helpful",
    )
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `session["chatbot_session_id"]` | `"sess-abc"` | |
| JSON | `{"conversation_id": "conv_123", "turn_id": "turn-7", "skill_id": "Connection Flow", "assessment": " Helpful "}` | Mixed case |
| `SKILL_ASSESSMENT_VALUES` | `("helpful", "wrong")` | Set on the mock |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `200`, `{"success": True}` | `assertEqual` |
| `record_skill_assessment` | `session_id="sess-abc", conversation_id="conv_123", turn_id="turn-7", skill_id="Connection Flow", assessment="helpful"` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Normalized value forwarded | Case-sensitive check rejects `"Helpful"` |

### 29. test_skill_assessment_empty_value_clears_chip

**Description:** Omitting `assessment` is valid and forwards `""`, which clears the chip.

**Code:**
```python
def test_skill_assessment_empty_value_clears_chip(self):
    # Act
    resp = self.post_json("/feedback/skill_assessment",
                          {"conversation_id": CONV_ID, "turn_id": TURN_ID, "skill_id": "S"})

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(self.feedback_service.record_skill_assessment.call_args.kwargs["assessment"], "")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| JSON | `{"conversation_id": "conv_123", "turn_id": "turn-7", "skill_id": "S"}` | No assessment |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| `assessment` kwarg | `""` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| Clear accepted | Empty assessment rejected |

### 30. test_skill_assessment_invalid_value_returns_400

**Description:** A value outside `SKILL_ASSESSMENT_VALUES` (`"redundant"` — redundancy is judged AI-side) → 400 with the allowed tuple in the message.

**Code:**
```python
def test_skill_assessment_invalid_value_returns_400(self):
    # Act
    resp = self.post_json("/feedback/skill_assessment", {
        "conversation_id": CONV_ID, "turn_id": TURN_ID, "skill_id": "S", "assessment": "redundant",
    })

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertEqual(resp.get_json()["error"],
                     "assessment must be one of ('helpful', 'wrong') or empty")
    self.feedback_service.record_skill_assessment.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `assessment` | `"redundant"` | Not allowed |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / `error` | `400` / `"assessment must be one of ('helpful', 'wrong') or empty"` | `assertEqual` |
| `record_skill_assessment` | not called | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| Rejected | Arbitrary value recorded |

### 31. test_skill_assessment_missing_fields_return_400

**Description:** `skill_id` missing → 400 "conversation_id, turn_id, and skill_id are required" (required-field check runs before the assessment check).

**Code:**
```python
def test_skill_assessment_missing_fields_return_400(self):
    # Act
    resp = self.post_json("/feedback/skill_assessment",
                          {"conversation_id": CONV_ID, "turn_id": TURN_ID, "assessment": "wrong"})

    # Assert
    self.assertEqual(resp.status_code, 400)
    self.assertEqual(resp.get_json()["error"], "conversation_id, turn_id, and skill_id are required")
    self.feedback_service.record_skill_assessment.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| JSON | `{"conversation_id": "conv_123", "turn_id": "turn-7", "assessment": "wrong"}` | No skill_id |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / `error` | `400` / `"conversation_id, turn_id, and skill_id are required"` | `assertEqual` |
| `record_skill_assessment` | not called | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| Rejected | Recorded without skill |

### 32. test_skill_assessment_rejects_unsafe_ids

**Description:** `_SAFE_ID_RE` guard on `/skill_assessment` for both IDs.

**Code:**
```python
def test_skill_assessment_rejects_unsafe_ids(self):
    for bad in UNSAFE_IDS:
        with self.subTest(conversation_id=bad):
            self.assert_bad_id(self.post_json("/feedback/skill_assessment",
                                              {"conversation_id": bad, "turn_id": TURN_ID, "skill_id": "S"}),
                               "conversation_id")
        with self.subTest(turn_id=bad):
            self.assert_bad_id(self.post_json("/feedback/skill_assessment",
                                              {"conversation_id": CONV_ID, "turn_id": bad, "skill_id": "S"}),
                               "turn_id")
    self.feedback_service.record_skill_assessment.assert_not_called()
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Unsafe ids | `"../etc"`, `"a/b"`, `"has space"`, `"x" * 81` | |
| `skill_id` | `"S"` | Valid |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / body | `400` + field-specific message | `assert_bad_id` |
| `record_skill_assessment` | not called | `assert_not_called()` |

| Pass Condition | Fail Condition |
|---|---|
| All rejected | Regex removed |

### 33. test_skill_assessment_service_failure_returns_500

**Description:** `record_skill_assessment` → `False` yields 500 "failed to record".

**Code:**
```python
def test_skill_assessment_service_failure_returns_500(self):
    # Arrange
    self.feedback_service.record_skill_assessment.return_value = False

    # Act
    resp = self.post_json("/feedback/skill_assessment", {
        "conversation_id": CONV_ID, "turn_id": TURN_ID, "skill_id": "S", "assessment": "wrong",
    })

    # Assert
    self.assertEqual(resp.status_code, 500)
    self.assertEqual(resp.get_json(), {"success": False, "error": "failed to record"})
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `record_skill_assessment` | returns `False` | |
| `assessment` | `"wrong"` | Valid |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `500`, `{"success": False, "error": "failed to record"}` | `assertEqual` |

| Pass Condition | Fail Condition |
|---|---|
| 500 | Failure masked |

### 34. test_recent_defaults_to_50_and_wifi

**Description:** With no query params, `/recent` asks the service for 50 events with `domain=""` and reports `"domain": "wifi"` in the response.

**Code:**
```python
def test_recent_defaults_to_50_and_wifi(self):
    # Act
    resp = self.client.get("/feedback/recent")

    # Assert
    self.assertEqual(resp.status_code, 200)
    self.assertEqual(resp.get_json(), {"success": True, "domain": "wifi", "events": [{"vote": 1}]})
    self.feedback_service.get_recent_votes.assert_called_once_with(50, domain="")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Query | *(none)* | Defaults |
| `get_recent_votes` | returns `[{"vote": 1}]` | |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status / JSON | `200`, `{"success": True, "domain": "wifi", "events": [{"vote": 1}]}` | `assertEqual` |
| Service call | `get_recent_votes(50, domain="")` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| Defaults applied | Default limit/domain changed |

### 35. test_recent_bt_domain_and_explicit_limit

**Description:** `?limit=10&domain=bt` is passed straight through and echoed as `"domain": "bt"`.

**Code:**
```python
def test_recent_bt_domain_and_explicit_limit(self):
    # Act
    resp = self.client.get("/feedback/recent?limit=10&domain=bt")

    # Assert
    self.assertEqual(resp.get_json()["domain"], "bt")
    self.feedback_service.get_recent_votes.assert_called_once_with(10, domain="bt")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| Query | `limit=10`, `domain=bt` | |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| `domain` | `"bt"` | `assertEqual` |
| Service call | `get_recent_votes(10, domain="bt")` | `assert_called_once_with` |

| Pass Condition | Fail Condition |
|---|---|
| BT stream requested | Domain ignored |

### 36. test_recent_limit_is_clamped_and_non_numeric_falls_back

**Description:** `limit` is clamped to `1..500` and a non-numeric value falls back to 50 (ValueError branch).

**Code:**
```python
def test_recent_limit_is_clamped_and_non_numeric_falls_back(self):
    for raw, expected in (("0", 1), ("-5", 1), ("9999", 500), ("abc", 50)):
        with self.subTest(limit=raw):
            # Arrange
            self.feedback_service.get_recent_votes.reset_mock()

            # Act
            resp = self.client.get(f"/feedback/recent?limit={raw}")

            # Assert
            self.assertEqual(resp.status_code, 200)
            self.feedback_service.get_recent_votes.assert_called_once_with(expected, domain="")
```

**Inputs:**

| Input | Value | Description |
|---|---|---|
| `limit` (subTests) | `"0"`, `"-5"`, `"9999"`, `"abc"` | Below min, negative, above max, non-numeric |

**Expected Output:**

| Expected Output | Value | Assertion |
|---|---|---|
| Status | `200` | `assertEqual` |
| Service limit | `1`, `1`, `500`, `50` respectively | `assert_called_once_with(expected, domain="")` |

| Pass Condition | Fail Condition |
|---|---|
| Clamped / fallback values | Clamp removed or ValueError escapes |

## Test Execution Flow

```
setUpClass (BaseTestCase)
  ├─ capture real app_config
  ├─ patch configs.global_configs.app_config → MagicMock
  └─ repoint every module whose app_config IS the real object
     (feedback_routes itself doesn't import app_config — no effect here)
        │
        ▼
for each test method:
  setUp
    ├─ BaseTestCase: reset mock app_config (silent_mode=False ...), patch CaseContext.to_session,
    │                fresh Flask app (TESTING, SECRET_KEY, WTF_CSRF_ENABLED=False),
    │                register feedback_bp (no 'main' stub needed — no redirects), client, app context
    └─ FeedbackTestCase: patch ROUTES.feedback_service + ROUTES.gather_service,
                         SKILL_ASSESSMENT_VALUES=("helpful","wrong"), record_* → True,
                         get_recent_votes → [{"vote": 1}]
        │
        ▼
  test body: optional session seed (chatbot_session_id / yaml_modified / chatbot_log_path /
             case_context / gather_workflow_id), optional @patch uuid.uuid4
             Arrange → Act (client.post json= / client.get) → Assert (status, JSON, service kwargs)
        │
        ▼
  tearDown / cleanups: pop app context, stop service + to_session patches ──► PASS / FAIL
        │
        ▼
tearDownClass: stop all app_config patchers
```

## Coverage Summary

| Metric | Value |
|---|---|
| Total Routes | 6 |
| Routes Tested | 6 |
| Total Tests | 36 (1 structure + 35 route; many use `subTest` for variant inputs) |
| Actual Coverage (%) | 100% of routes; every validation / failure branch in the route module except one defensive `except` |
| Out-of-scope items | 2 (`/detail` `except Exception: issue = {}` around `CaseContext.from_session` — not reachable from a cookie session; `feedback_service` / `gather_service` internals — service layer) |

## What Would Make These Tests Fail?

| Source change | ✅ PASS | ❌ FAIL |
|---|---|---|
| Loosen `_SAFE_ID_RE` (allow `/`, `.`, spaces or >80 chars) | non-id tests | all 5 `*_rejects_unsafe_ids` tests |
| Change `_bad_id_response` wording or status | non-id tests | all `*_rejects_unsafe_ids` tests |
| `/vote`: validate ids before vote | all others | `test_vote_non_json_body_fails_vote_check_first` |
| `/vote`: stop accepting 0 | all others | `test_vote_zero_string_accepted_with_empty_session_defaults` |
| `/vote`: drop `yaml_modified_path` from response | all others | `test_vote_success_records_with_session_context`, `test_vote_zero_string_accepted_with_empty_session_defaults` |
| `/vote`: wrap service call in try/except | all others | `test_vote_service_exception_is_not_caught` |
| `/detail`: always send `chatbot_log_path` regardless of `attach_log` | all others | `test_detail_minimal_payload_uses_defaults_and_ignores_log_without_opt_in` |
| `/detail`: change default domain from `"wifi"` | all others | `test_detail_minimal_payload_uses_defaults_and_ignores_log_without_opt_in` |
| `/detail`: remove `.strip()` / `.lower()` on any field, or rename a kwarg | all others | `test_detail_success_forwards_all_fields_and_records_gather` |
| `/detail`: drop legacy string-evidence splitting | all others | `test_detail_evidence_string_is_split_into_lines` |
| `/detail`: return 500 instead of 400 when nothing recorded, or emit gather anyway | all others | `test_detail_nothing_recorded_returns_400_and_skips_gather` |
| `/detail`: reject bad votes instead of coercing to None | all others | `test_detail_invalid_vote_values_are_coerced_to_none` |
| `/step_vote`: accept 0 | all others | `test_step_vote_zero_vote_rejected` |
| `/skill_helpful`: apply `_SAFE_ID_RE` to `skill_id` | all others | `test_skill_helpful_success_allows_spaces_in_skill_id` |
| `/skill_assessment`: remove `.lower()` or add `"redundant"` to allowed values | all others | `test_skill_assessment_success_normalises_case` / `test_skill_assessment_invalid_value_returns_400` |
| `/recent`: change clamp bounds or default limit | all others | `test_recent_defaults_to_50_and_wifi`, `test_recent_limit_is_clamped_and_non_numeric_falls_back` |
| Change `url_prefix` from `/feedback` | — | every test |
