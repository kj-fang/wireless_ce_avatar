# Testing Reference: Fixtures & Mocks

A plain-language reference for understanding the unit tests in this project
(`unit_tests/test_*_blueprint.py`). Covers what a **test fixture** is, what **mocks**
are, and a line-by-line walkthrough of a real example from
[unit_tests/test_bsod_blueprint.py](../unit_tests/test_bsod_blueprint.py).

---

## 1. What is a test fixture?

A **test fixture** is the fixed, known baseline state and supporting objects that
a test needs in place *before* it runs — so each test starts from a clean,
predictable environment.

In practice a fixture covers three things:

- **Setup** — building what the test needs (objects, mock data, a DB connection,
  a configured app, patched dependencies).
- **The baseline state itself** — the prepared objects/data the test acts on.
- **Teardown** — cleaning up afterward (closing connections, undoing patches) so
  tests don't leak state into each other.

In our `unittest`-based tests, the fixture is mostly the `setUp()` method (and the
shared `BaseTestCase`). Example:

```python
class TestBsodSubmit(BaseTestCase):
    def setUp(self):
        super().setUp()                                  # builds the Flask app + test client
        self.app_config.key.potatofarm_url = API_URL     # known config baseline
        self.app_config.key.potatofarm_api = API_TOKEN
```

The `@patch(...)` decorators that replace `requests.post` and
`detect_user_email` with mocks are **part of the fixture too** — they are
automatically torn down after each test.

### Why fixtures matter

- **Isolation** — every test runs against the same clean baseline, so results
  don't depend on order or on another test's leftovers.
- **Repeatability** — same input state ⇒ same outcome every run.
- **Less duplication** — shared setup lives in one place (`setUp` / `BaseTestCase`)
  instead of being copy-pasted into each test.

### Terminology note

`unittest` calls it `setUp` / `tearDown`; `pytest` uses the word "fixture"
explicitly via `@pytest.fixture`. Same concept. In the flow diagrams under
`docs/unit_test/`, the **"Test fixture"** box means exactly this — the mocked services,
config, and app/client that every test in the file starts from.

---

## 2. What are mocks? (simple terms)

A **mock** is a fake stand-in object that replaces a real dependency during a
test. You tell it how to behave, and it records how it was used — so you can test
your code without triggering the real thing.

Think of it like a **crash-test dummy**: it stands in for a real person so you can
test the car safely, without risking anyone.

### Why use them

Real dependencies are often slow, unpredictable, or have side effects you don't
want in a test:

- `requests.post` → makes a real network call to the PotatoFarm API
- `subprocess.run(["explorer", ...])` → actually opens a File Explorer window
- `detect_user_email` → reads real system/account state
- a database write → actually changes data

A mock replaces these so the test stays fast, isolated, and repeatable — no
network, no popups, no real files.

### What a mock lets you do

1. **Fake a return value** — "pretend the API replied 200":
   ```python
   mock_post.return_value = MagicMock(status_code=200)
   ```
2. **Fake a failure** — "pretend the network blew up":
   ```python
   mock_post.side_effect = ConnectionError("boom")
   ```
3. **Spy on calls** — check your code called it correctly, or didn't call it at all:
   ```python
   mock_post.assert_called_once()     # it was called exactly once
   mock_email.assert_not_called()     # it was never called
   ```

### Quick vocabulary

| Term | Meaning |
|------|---------|
| `MagicMock` | The generic fake object; any attribute/method you call on it just works and is recorded. |
| `return_value` | What the fake returns when called. |
| `side_effect` | Make the fake raise an error (or run custom logic) instead of returning a value. |
| `@patch` | Temporarily replace a real name with a mock during one test, then restore it. |
| `assert_called_once()` | Verify the fake was called exactly one time. |
| `assert_not_called()` | Verify the fake was never called. |
| `call_args` | Inspect the arguments the fake was last called with. |
| "mock" vs "stub" | Loosely: a *stub* just returns canned values; a *mock* also lets you *assert how it was called*. In Python both are usually done with `MagicMock`. |

---

## 3. Line-by-line walkthrough of a real example

From [unit_tests/test_bsod_blueprint.py](../unit_tests/test_bsod_blueprint.py):

```python
@patch(f"{ROUTES}.requests.post")                                       # fake the HTTP call
@patch(f"{ROUTES}.helpers.detect_user_email", return_value=USER_EMAIL)  # fake the email lookup
def test_submit_success_posts_payload_and_redirects(self, mock_email, mock_post):
    mock_post.return_value = MagicMock(status_code=200)   # pretend API said OK
    ...
    mock_post.assert_called_once()                        # verify we called it
```

### What's being tested

The real route [blueprints/bsod/bsod.py](../blueprints/bsod/bsod.py) does roughly
this when someone submits a BSOD dump:

1. Figure out the user's email.
2. Send an HTTP POST to the PotatoFarm API.
3. Based on the reply, flash a message and redirect.

We want to test step 3's logic **without** actually looking up a real email or
hitting a real server. So we fake those two things.

### Line 1 — `@patch(f"{ROUTES}.requests.post")`

- `@patch(...)` is a **decorator** — it wraps the test and says: "for the duration
  of this test, replace this one thing with a fake, then restore it when the test
  ends."
- `ROUTES` is just a string constant, `"blueprints.bsod.bsod"` — the module where
  the route lives. So `f"{ROUTES}.requests.post"` becomes
  `"blueprints.bsod.bsod.requests.post"`.
- **Why target it there and not plain `requests.post`?** You must patch the name
  *where it's actually used*. The route module did `import requests` and later
  calls `requests.post(...)`. We replace *that module's* view of `requests.post`.
  If we patched `requests.post` somewhere else, the route would still call its own
  real one. (This is the single most common mocking gotcha.)

Result: inside this test, whenever the route calls `requests.post(...)`, it hits a
fake instead of the internet.

### Line 2 — `@patch(..., return_value=USER_EMAIL)`

- Same idea: replace the email-lookup function with a fake.
- `return_value=USER_EMAIL` is a shortcut that says "when the code calls this fake,
  just hand back `USER_EMAIL`" (a constant like `"tester@example.com"`).
- So the route *thinks* it successfully found the user's email, without touching
  any real account/system.

### The function signature — mocks come in as arguments

```python
def test_submit_success_posts_payload_and_redirects(self, mock_email, mock_post):
```

This is the part that trips people up: **the decorators hand the fakes into the
test as parameters, in bottom-up order.**

```
@patch(... requests.post ...)        ← top decorator      → last argument  (mock_post)
@patch(... detect_user_email ...)    ← bottom decorator    → first argument (mock_email)
def test_...(self, mock_email, mock_post):
```

Reading the stack from the function *upward*: the decorator closest to the
function fills the first argument after `self`. That's why `mock_email` comes
before `mock_post` even though `requests.post` is written first. These names are
just handles so you can control and inspect each fake.

### `mock_post.return_value = MagicMock(status_code=200)`

- This scripts what the fake HTTP call *returns*.
- A real `requests.post(...)` returns a **response object** that has a
  `.status_code`, `.text`, etc. We fake that response with another `MagicMock` and
  give it `status_code=200`.
- Net effect: when the route runs `resp = requests.post(...)` and then checks
  `if resp.status_code == 200:`, our fake makes that branch true — we're
  simulating a successful API reply.

Compare:
- `test_non_200_response...` sets `status_code=503` to simulate a failure.
- `test_request_exception...` uses `side_effect=ConnectionError("boom")` to
  simulate the network throwing an error.

### The `...` — the actual request (the "Act" step)

In the shortened snippet the `...` stands for the real middle of the test: it sets
up the session, then calls the endpoint through the test client, e.g.:

```python
self.seed_session(download_path=DUMPS_PATH)
resp = self.client.post("/bsod/bsod_submit")
```

This actually triggers the route so it runs its logic against our fakes.

### `mock_post.assert_called_once()`

- A mock doesn't just return canned values — it also **records every call made to
  it**.
- `assert_called_once()` checks: "was the fake `requests.post` called *exactly one
  time*?" If it was called zero times or twice, the test fails.
- This verifies the route actually attempted the API call (it didn't silently skip
  it). The full test goes further and checks *what* it was called with — the URL,
  the JSON body, headers, `verify=False` — using `mock_post.call_args`.

The flip side appears in other tests: `mock_post.assert_not_called()` proves the
route bailed out early (e.g., when the email is missing) and never reached the
API.

### One-sentence summary

> "Replace the email lookup and the HTTP call with fakes; pretend the email exists
> and the API returns 200; run the route; then confirm the route did send exactly
> one API request and redirected correctly — all without any real network or
> system access."

That's the whole point of mocks: you control the inputs (what dependencies return)
and inspect the interactions (how they were called), so you can test *your* code's
behavior in isolation — fast and repeatably.

---

## 4. The Arrange–Act–Assert pattern (how to read any test here)

Every test in this suite follows the same three-part shape:

1. **Arrange** — set up the fixture + mocks + session state (the "given").
2. **Act** — call the endpoint via `self.client.get(...)` / `self.client.post(...)`
   (the "when").
3. **Assert** — check the response (status code, body, flash) and verify which
   mocks were/weren't called (the "then").

The flow diagrams in `docs/unit_test/` visualize this as **scenario / input ➜ expected
response & asserted calls** for each test case.

---

## 5. Related files

- Tests: [unit_tests/test_bsod_blueprint.py](../unit_tests/test_bsod_blueprint.py)
- Route under test: [blueprints/bsod/bsod.py](../blueprints/bsod/bsod.py)
- Flow diagrams: `docs/unit_test/test_bsod_blueprint_flow.html`,
  `docs/unit_test/test_analysis_etl_blueprint_flow.html`
