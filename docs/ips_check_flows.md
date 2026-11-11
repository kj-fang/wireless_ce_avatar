# IPS Case-Number Check — Developer Reference

> **Audience:** developers onboarding to, or doing follow-up work on, the IPS
> case-number feature. This document explains *what the feature does*, *how the
> pieces fit together*, the *detailed runtime flows* (with the relevant
> functions and files), the *impact on existing flows*, and the *invariants* you
> must not break.
>
> **Scope anchor:** function names are the stable reference. Line numbers are
> given as `file:line` for convenience but drift over time — trust the function
> name first. All paths are repo-relative.

---

## 1. Why the feature exists

Every log the tool analyses is billed against an IPS **case number**. A session
that analyses or chats about a log without ever naming a case cannot be traced
back to one afterwards. The feature's single hard requirement is:

> **A case number (or a deliberate, confirmed "this log has no case") is always
> recorded before a log is analysed or discussed.**

Everything else in the feature exists to satisfy that requirement *without
nagging the user*: remembering answers across restarts and conversations,
deriving the case from the folder name when possible, and following a decoded /
extracted log back to the answer given for the file it came from.

The gate lives **on the server** (`blocking_state` → HTTP 428) because a
client-side dialog is only a suggestion: the same conversation can be reached
from several entry points (Send To, local upload, manual set-log, History
restore, a direct API call) and from a restored session.

---

## 2. Core concepts & data model

### 2.1 Sources (how a case number was obtained)

Defined in [`services/check_ips_service.py`](../services/check_ips_service.py) (`:28`):

| Constant | Value | Meaning |
|---|---|---|
| `EXPLICIT` | `"explicit"` | The user typed the number into the dialog. |
| `DERIVED_FROM_PATH` | `"derived_from_path"` | The number is a whole case folder in the log's path. |
| `SKIPPED` | `"skipped"` | The user confirmed the log has no case. |
| `ABSENT` | `"absent"` | The prompt did not set this — e.g. a case that arrived from the case search. |

`_SOURCES` is the validation set. `attach()` rejects anything outside it.

> **Two "source" questions, deliberately different.** `current_source()` (`:196`)
> reports *how the prompt flow recorded it* — this is what `needs_ips` gates on.
> `attributed_source()` (`:209`) reports *how the case was really obtained* for
> telemetry, falling back to the `CaseContext.case_ref_source`. Collapsing these
> two breaks either gating or warehouse attribution. Keep them separate.

### 2.2 Where state lives

There are **three** stores, each with a distinct lifetime. Getting these
straight is the key to understanding the whole feature.

| Store | Lifetime | Holds | Accessed via |
|---|---|---|---|
| **Durable answer file** `ips_answers.json` under `<avatarfiles_dir>/app_state/` | Survives restarts | Per-log answers, per-log case-facts holders, per-case holders | `_load_answers` / `_write_answers` under `_answer_lock` |
| **Flask session** (server-side, written back whole per request) | One browser session | `case_context` (a `CaseContext`), `SESSION_SOURCE_KEY`, `uploaded_source_path`, `sendto_pending_path`, `_UPLOAD_ANSWER_KEY`, carried issue-time keys, `classification` | `flask.session` |
| **Module globals** | Process lifetime | `_answered_this_session` (set of log keys answered this conversation), `_last_prompted_log`, in-memory `_case_facts` lookup cache | module-level |

> **Why a per-log *file* and not just the session?** The user is answering a
> question about a *file* ("this one is case 01010628", "this one has no case").
> That answer does not stop being true when the app restarts, or when a slow
> request that started before they answered writes its stale session back over
> the top of it. The file keyed by log path is the durable record;
> `_apply_stored_answer` (`:245`) puts it back on a session that lost it.

### 2.3 The `ips_answers.json` record shapes

Keys are produced by `_log_key` (`:77`) — see §3.1. Three record shapes share
the file:

**(a) A per-log answer** — written by `remember_answer` (`:158`):
```jsonc
"<log-key>": {
  "case_nbr": "01010628",          // "" for a skip
  "source": "explicit",            // explicit | derived_from_path | skipped
  "at": "2026-11-03T11:22:33+00:00",
  "case_facts": {                   // optional; see (b)
    "description": "...", "attachment_time": "...",
    "issue_time": "...", "issue_times": ["..."]
  }
}
```

**(b) A per-log case-facts holder** — written by `remember_case_facts` (`:507`)
for a log that is *covered by someone else's answer* (e.g. a decoded `.etl`
whose case was answered on the archive). It carries `covered_by` pointing at the
owning answer key, and keeps only this capture's own times (`_CASE_FACT_FIELDS`
= description, attachment_time, issue_time, issue_times):
```jsonc
"<decoded-log-key>": {
  "case_nbr": "01010628",
  "covered_by": "<archive-or-upload-key>",
  "at": "...",
  "case_facts": { "description": "...", "issue_times": ["..."] }
}
```

**(c) A per-case holder** — key prefixed `case:` (`_case_level_key`, `:589`),
written by `remember_case_lookup` (`:629`) / `remember_organized` (`:682`). Holds
what belongs to the *case* rather than one capture: the IPS lookup result
(`CASE_LOOKUP_KEY`) and the LLM pre-pass over the description (`ORGANIZED_KEY`).
These are the same answer every time, so caching them here avoids re-paying an
IPS lookup + LLM call on every restart / new conversation / second log of the
same case.

---

## 3. Function reference, grouped by responsibility

All in [`services/check_ips_service.py`](../services/check_ips_service.py) unless noted.

### 3.1 Key normalisation & the durable store
| Function | Line | Responsibility |
|---|---|---|
| `_log_key` | `:77` | Canonical store key for a path. Collapses `\\?\` extended-length, UNC, 8.3 short names (`helpers.get_long_path`), and case. **One physical file → one key.** |
| `_answer_store_path` | `:67` | Path to `ips_answers.json` (returns `None` if no `avatarfiles_dir`, which disables persistence safely). |
| `_load_answers` / `_write_answers` | `:98` / `:146` | Read/write the JSON store; corrupt store degrades to re-asking, never a crash. `_write_answers` enforces `_MAX_REMEMBERED_ANSWERS`. |
| `answer_for` | `:137` | The raw per-log record, or `{}`. |
| `remember_answer` | `:158` | Write a per-log answer; **carries over `case_facts` when re-confirming the same number.** |

### 3.2 Session reflection
| Function | Line | Responsibility |
|---|---|---|
| `current_case_nbr` | `:188` | Canonical number on the session `CaseContext`. |
| `current_source` | `:196` | Gating source (the session key). |
| `attributed_source` | `:209` | Telemetry source (falls back to `CaseContext`). |
| `_session_answer_is_about` | `:229` | Whether the session's case belongs to *this* log. |
| `_apply_stored_answer` | `:245` | Re-apply the durable answer onto a session that lost it. |
| `_remember_on_session` | `:914` | **The session writer.** Writes number+source onto `CaseContext`; clears a *different* prior case and its agents; re-points primed agents; migrates `download_results` on a rename. |

### 3.3 Ownership resolution (the hard part — see §6)
| Function | Line | Responsibility |
|---|---|---|
| `_owning_answer_key` | `:343` | The store key whose answer *covers* this log (itself, its upload, its archive, or a `covered_by` back-reference). |
| `_placeholder_is_for` | `:891` | Whether the session's `local_upload_`/`local_bsod_` run is *this* log's run. |
| `_from_answered_upload` | `:752` | Whether this log came from an upload answered *in this conversation*. |
| `_archive_answer_key` / `_archive_answer` | `:310` / `:337` | Map an extracted log to the archive it came out of. |
| `_case_facts_key` / `_stored_case_facts` | `:396` / `:410` | Where this log's own times live, and reading them back safely. |

### 3.4 The decision / gate
| Function | Line | Responsibility |
|---|---|---|
| `needs_ips` | `:783` | **The decision table.** Whether the modal must block this log. Every branch is logged via `_gate` (`:124`). |
| `prompt_state` | `:838` | Read-only payload the client needs: `needs_ips`, candidates, per-candidate sources, current case/source, log_path. |
| `blocking_state` | `:864` | Server-side gate payload (adds `success:false`, `ips_required:true`, 428 error text) or `None`. |
| `candidates_for` / `candidate_sources` | `:695` / `:710` | Best-guess-first case list and each candidate's true origin. |
| `source_for_answer` | `:735` | Server re-decides the source rather than trusting the client's claim. |

### 3.5 Recording an answer
| Function | Line | Responsibility |
|---|---|---|
| `attach` | `:1413` | **The write entry point.** Validates, marks answered, persists, carries to owning upload, writes session. Raises `ValueError` for a bad number. |
| `_carry_answer_to_owning_upload` | `:1395` | Keep the upload's answer in step when its extracted log is re-answered. |
| `carry_upload_answer` | `:767` | Stash the confirmed source key on the session so it survives the Send-To worker hand-off. |
| `forget_case_derived_session_keys` | `:1384` | Drop the previous file's case-derived session keys on a reused window. |
| `start_new_session` | `:277` | Reset attribution at the start of a conversation (reconfirm the case). |

### 3.6 Remembering & recalling what a case said
| Function | Line | Responsibility |
|---|---|---|
| `remember_case_facts` | `:507` | Store this log's description/times beside the owning answer. |
| `remembered_case_facts` | `:424` | Recall them; both sides must be the same *real* case number. |
| `settled_case_answer` / `restore_settled_answer` | `:462` / `:485` | A log "already been to IPS" (real number + description + issue time); restore it before priming. |
| `remember_case_lookup` / `remembered_case_lookup` | `:629` / `:643` | Per-case IPS lookup cache (disk). |
| `remember_organized` / `remembered_organized` | `:682` / `:666` | Per-case LLM pre-pass cache (disk), keyed on the description digest. |

### 3.7 Enrichment (looking the case up in IPS/Snowflake + LLM)
| Function | Line | Responsibility |
|---|---|---|
| `_lookup_case_facts` | `:1038` | Snowflake row + LLM classification, process-cached, single-worker pool + bounded semaphore + 20s timeout. |
| `enrich_attached_case` | `:1156` | Fill *empty* `CaseContext` fields + classification + primed agents; reports LLM cost. |
| `enrich_attached_case_if_needed` | `:1256` | First-read lookup for a **silently attached** (path-derived) case. |
| `prefetch_case_facts` | `:1220` | Warm the lookup on a daemon thread right after a silent attach. |
| `_apply_case_to_primed_agents` / `_primed_agents` / `_forget_case_on_agents` | `:1137` / `:1312` / `:1330` | Propagate / clear case context on the session's live chatbot agents. |

---

## 4. Detailed runtime flows

### Flow A — Right-click **Send To** → log chatbot (the primary flow)

```
Explorer "Send to IntelAvatar"
  -> app.py _build_startup_path (app.py:122)              [cold start builds URL]
  -> GET /open_local_analysis (log_parser_routes.py:1077)
       - token + path validation
       - session['sendto_pending_path'] = path
       - derived = check_ips_utils.derive_ips_from_path(path)
       - _no_silent_case_because(path, derived)           (log_parser_routes.py:439)
            if OK: attach(derived, DERIVED_FROM_PATH, path)   (:1124)
                   prefetch_case_facts(derived)               (:1126)
       - render sendto_transmission.html
            ips_state = prompt_state(path)                     (:1132)
  -> [client] show dialog iff needs_ips; user may POST /api/ips/resolve
  -> socketio 'start_sendto' (log_parser_routes.py:1547)
       - GATE: if needs_ips(path): emit sendto_error, stop     (:1568)
       - spawn _run_sendto_in_background (:1595)
  -> _process_local_analysis (:549)                            [extract/decode]
       - sets session['uploaded_source_path']
       - carry_upload_answer(source_path)                      (:591)
       - builds chatbot redirect URL
       - snapshot dict(session) into _sendto_session_store[token]
  -> [browser redirect] before_app_request _pickup_sendto_session (:274)
       - forget_case_derived_session_keys()                    (:310)
       - session.update(snapshot)
  -> POST /<profile>/set_log  ChatbotRoutes.set_log (chatbot_routes.py:892)
       - restore_settled_answer(log_path)                      (:924)
       - _extract_issue_context -> prime_with_context
       - response spreads prompt_state(log_path)               (:1055)
  -> [client] GET issue context  get_issue_context (:1869)
       - enrich_attached_case_if_needed()                      (:1873)
       - remembered_case_facts(log_path, case_nbr)             (:1924)
  -> POST /<profile>/chat  (:1185)
       - GATE: blocking_state(active_log) -> 428 if owed       (:1253)
       - otherwise message reaches the LLM
```

**Key subtlety:** the file *asked about* (archive/`.etl`) is not the file the
chatbot *opens* (extracted/decoded `.log`). `uploaded_source_path` records the
former; `_owning_answer_key` (§6) ties the latter back to it so the same work is
never asked about twice.

### Flow B — Local upload (drag/drop or picker)

Same spine as Send-To but through `upload_local_analysis` (`log_parser_routes.py:888`):
silent-attach + `prefetch_case_facts` (`:943`/`:945`), then `blocking_state(source_path)`
→ 428 (`:948`). `_process_local_analysis` sets the `local_upload_`/`local_bsod_`
placeholder context when the upload has no case of its own.

### Flow C — Manual set-log / open existing analysis

`open_local_analysis` (`:1077`) for a path already on disk, and `set_log` for a
file the user points the chatbot at. `restore_settled_answer` (`:924`) refills a
previously settled case *before priming* so the agent (not just the sidebar)
sees the remembered description and time.

### Flow D — The gate (enforcement)

`blocking_state(log_path)` (`:864`) → `needs_ips` → 428. Enforced at:
- `chatbot_routes.py:1253` (`chat`, against the agent's real `current_log_path`)
- `nw_analysis_routes.py:390` (sleepstudy, *before* the SSE stream opens)
- `nw_analysis_routes.py:516` (NW `chat`)
- `log_parser_routes.py:948` (`upload_local_analysis`)
- `log_parser_routes.py:1568` (`start_sendto`, Socket.IO — not covered by the
  HTTP 428 interception, so repeated here)

### Flow E — Conversation reset (reconfirm)

`start_new_session` (`:277`) clears `_answered_this_session`, resets attribution
to `ABSENT`, and rebuilds the context keeping only log-level fields. Wired at:
- `services/chatbot/shared_routes.py:109`
- `chatbot_routes.py:2180` (blueprint `on_reset`)
- `nw_analysis_routes.py:901` (also rotates `nw_conversation_id`)

### Flow F — Attribution / telemetry read path

- `feedback_service.py:323-332`, `:462`: `current_case_nbr` / `attributed_source`
  (empty case for a skip) → warehouse.
- `gather_service.py:527-531`: adds `case_ref_source` to the issue payload.

### Flow G — Enrichment / lookup

A **typed** number is enriched by the resolve route. A **path-derived** number is
attached silently, so nothing looked it up — `prefetch_case_facts` (`:1220`) warms
it on a daemon thread, and `enrich_attached_case_if_needed` (`:1256`) does the
first real read in `get_issue_context`. Both ultimately go through
`_lookup_case_facts` (`:1038`), which is cached and timeout-bounded.

---

## 5. Impact map — what existing flows this touches

`check_ips_service` is imported by **six** modules. This is the regression blast
radius:

| File | Existing flow at risk | IPS calls (lines) |
|---|---|---|
| `blueprints/log_parser/log_parser_routes.py` | Send-To, local upload, open_local_analysis, find_best_log | `310, 448, 454, 490-517, 591, 595, 943-945, 1124-1126, 1132, 1568` |
| `blueprints/chatbot/chatbot_routes.py` | set_log, chat (428), get_issue_context, reset | `486, 924, 1055, 1253, 1873, 1924, 2020, 2180` |
| `blueprints/nw_analysis/nw_analysis_routes.py` | NW set_log, sleepstudy stream, NW chat, reset | `333, 360, 390, 516, 901` |
| `services/chatbot/shared_routes.py` | conversation reset | `109` |
| `services/feedback_service.py` | case attribution to warehouse | `323, 330, 331, 332, 462` |
| `services/gather_service.py` | telemetry/usage records | `527, 529, 531` |

Templates involved: `templates/sendto_transmission.html` (reads `ips_state`),
plus the chatbot UI dialog/notice.

---

## 6. Deep dive: `_owning_answer_key` (ownership resolution)

This is the function most follow-up bugs cluster around. Given a log path, it
returns the store key whose answer covers it, checked in this order (`:343`):

1. **The upload, when answered this conversation** — `covered_by_upload AND
   _from_answered_upload`. Mirrors `_apply_stored_answer`'s ordering so the just-
   given answer wins over an older one the decoded log still carries.
2. **The log's own record** — but only if it is a real answer, *not* a bare
   `covered_by` facts-holder.
3. **The upload** — `covered_by_upload` (placeholder matches this log).
4. **The `covered_by` back-reference** — follow the holder's pointer when the
   named record is still a real answer. *(Added in `59b8061a`: lets a decoded
   `.etl` find its answer again after a restart, when there is no
   `uploaded_source_path` and it sits under no archive folder.)*
5. **The archive** — `_archive_answer_key`.

Every branch logs an `[ips] ... is covered by ...` line.

---

## 7. Invariants & gotchas (do not break these)

1. **Mark answered only *after* a valid answer.** `attach` marks the log answered
   only once validation passes (`:1413`). Marking before validation opens the
   gate for a typo: route returns 400, the module-level set keeps the log, and
   `needs_ips` then returns `False` forever (process lifetime). This is the exact
   silent gap the feature closes.
2. **`skip` requires `confirmed === true` (the literal boolean).** `bool("false")`
   is `True`; the route checks identity (`blueprints/check_ips/check_ips_routes.py`).
3. **A remembered *number* is not auto-reapplied; a `SKIPPED` is.** Each new
   conversation reconfirms a real case, but a confirmed "no case" persists across
   restarts (see `needs_ips`, `:783`).
4. **Keep `current_source()` and `attributed_source()` distinct** (§2.1).
5. **Gate against the agent's real log**, not the shared session path — chat uses
   `active_agent.current_log_path` because `prepare()` can move the agent without
   touching `session['chatbot_log_path']`.
6. **Session writes from background threads are unreliable.** The Send-To worker
   snapshots the session into a module store and lets the next HTTP request adopt
   it (`_pickup_sendto_session`). Don't assume `session[...] = x` in a thread
   persists.
7. **`_log_key` must stay canonical.** Any new path form (short names, UNC,
   extended-length) that is not folded here will create a second key and make a
   confirmed answer invisible.
8. **Synthetic placeholders are not case numbers.** `normalise_ips` reads
   `local_upload_`/`local_bsod_` as no case; code that must not inherit the
   previous file's facts relies on this (e.g. `remembered_case_facts`).

---

## 8. Extension points (adding a new entry path)

If you add a new way to open a log for analysis/chat, you must:

1. **Advertise**: return `prompt_state(log_path)` so the client can prompt.
2. **Gate**: call `blocking_state(log_path)` and return `428` when it is truthy
   (or, for a non-HTTP path like Socket.IO, call `needs_ips` and refuse).
3. **Attribute**: ensure the case reaches `feedback_service`/`gather_service`
   via the session `CaseContext` (not only the session key).
4. If the opened file is *derived* from the file asked about, set
   `session['uploaded_source_path']` so `_owning_answer_key` can bridge them.

---

## 9. Known verification gaps (as of this writing)

- **CI does not run tests** — `.github/workflows/build.yml` only builds + an
  optional linter. There is no automated gate on behavior before merge.
- **No automated IPS coverage.** The test referenced by commit `d6b7b0b9`
  (`tests/test_ips_case_facts_memory.py`) is **not** in the tree or git history.
- The repo's existing tests use `unittest` with an AST-extract pattern (see
  `tests/test_log_chatbot_preview_route.py`), which is a good model for unit-
  testing these route/service functions without importing the whole app.

Recommended test layers: state-machine unit tests over a temp answer store +
mocked session; route/gating integration tests with Flask `test_client`
(especially the 428 behavior and the `/api/ips/*` contracts); and a CI pytest
gate so the suite blocks the merge.
