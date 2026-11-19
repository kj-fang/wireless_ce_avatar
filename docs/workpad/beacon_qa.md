# Beacon: Route Customer Questions to Echo, Debug Requests to the Current Flow

## Goal and Decisions Already Made

The goal is for Beacon to read the **latest comment**, or the **description when there is no comment yet**, and decide whether the customer is asking a question or requesting debug.

- A **question** is answered through the Echo knowledge base.
- A **debug request** follows today's flow unchanged.

This is an investigation first: measure how reliably the two can be distinguished on real cases before wiring the routing.

### Decisions Already Made

- Echo covers **Bluetooth as well as Wi-Fi**, so question routing applies to both radios with no gate.
- A question draft can be **customer-facing**. It is not limited to a private note.

### Recommendation Still Open

Keep question drafts **human-approved and out of auto-post**, because the answer is derived from Echo and Echo content can be Intel-internal.

---

## Classification

Classification should happen in:

`services/handsfree/case_reader.py`

The case reader already:

1. Reads the description.
2. Reads every comment from oldest to newest.
3. Recognises a customer who "asked a question that is still unanswered" in task 6 of `READER_PROMPT`.

### Changes to `READER_PROMPT`

Add one task and two fields to its JSON output:

```json
{
  "request_type": "question | debug | mixed",
  "customer_question": ""
}
```

- `request_type`: `question`, `debug`, or `mixed`.
- `customer_question`: the question copied **verbatim**; empty for debug.

### Classification Source

Judge based on the **latest customer-side comment**, where the author is tagged:

- `Partner`
- `Customer`

When there is no customer comment, judge based on the **description**.

### Request Types

| Type | Meaning | Examples |
|---|---|---|
| `question` | Asks for an explanation, status, support statement, or how-to. Reports no new failure and brings no new logs, time, or repro. | `"Is Wi-Fi 7 MLO supported on this module?"`<br>`"Which driver fixes this?"`<br>`"How do we collect BT traces?"` |
| `debug` | Reports a failure, uploads logs, or answers something Intel asked for. | `"Disconnects at 14:32, logs attached"`<br>`"Here is the dump you requested"` |
| `mixed` | Contains both a question and debug information in one message. Treated as debug. | `"New logs attached. Also, is this fixed in the next release?"` |

### Safety Rules

Apply these rules **in code after the LLM answers**:

1. An unknown or missing `request_type` becomes `debug`, so a bad LLM answer falls into today's flow.
2. A log archive uploaded since the last analysis forces `debug`.
3. BSOD wording in the case forces `debug`.
4. Normalise the value in `read_case_history` the same way `normalize_action_owner` normalises the owner.

---

## Routing and the Question Draft

Route in `_analyze_case` in:

`services/handsfree/runner.py`

Place the routing branch right after the reader and domain resolution, just before `_start_echo_advice`.

### Routing Behavior

| `request_type` | What Beacon Does |
|---|---|
| `question` | Asks Echo synchronously using the customer's question, sets a new mode `question`, and returns before the info gate and log pipeline. |
| `debug` or `mixed` | Follows today's flow unchanged. The existing background Echo advice already receives the latest customer comment. |
| `question`, but Echo is unavailable or returns nothing | Falls back to today's flow. The case is never dropped. |

The classification is performed on every run. A customer who asks a question now and uploads logs later is routed:

- to `question` first, and
- to `debug` on the next run.

This works because a new customer comment already triggers a new run.

### The Draft Must Be Built in Two Parts

Today, everything Echo says is kept away from the customer.

`strip_echo_section` in:

`services/handsfree/composer.py`

removes the block between the Echo markers from every public IPS post and every Outlook reply. If the answer is placed inside those markers, a customer-facing question draft would post empty.

A question draft should therefore contain:

#### 1. Customer-facing body

Use one LLM pass to rewrite Echo's answer as a reply to the customer's question.

The rewrite must remove:

- internal document names,
- case numbers,
- references to other customers,
- citations.

#### 2. Raw Echo answer

Place the original Echo answer underneath the customer-facing body, inside the **existing Echo markers**.

The reviewer can compare the customer-facing rewrite against the raw Echo answer. At post time, the raw Echo section is automatically removed by `strip_echo_section`.

```text
Customer-facing answer

<Echo markers>
Raw Echo answer
</Echo markers>
```

After `strip_echo_section`:

```text
Customer-facing answer
```

### Rewrite Failure

If the rewrite pass fails:

- queue a **private note**,
- state that the customer asked a question,
- include the raw Echo answer.

Never queue a **public draft with an empty body**.

---

## Posting Rules

### Public Reply

The new `question` mode must be treated as a **public reply**.

Today, the review page and approve path derive visibility from a list of modes. Add `question` wherever that list is used.

> The approve path has not been traced end to end, so confirm that `question` is ultimately posted as a public comment.

### Auto-post

Keep `question` out of `_CHECKLIST_MODES` in:

`services/handsfree/orchestrator.py`

That list controls what the auto-post option acts on.

Therefore:

```text
question
  -> public reply
  -> human approval required
  -> no auto-post
```

---

## Files to Touch

| File | Change |
|---|---|
| `services/handsfree/case_reader.py` | Add the new prompt task and the `request_type` and `customer_question` fields. Add normalisation in `read_case_history`. |
| `services/handsfree/runner.py` | Add new fields to `CaseAnalysis`. Add the routing branch before `_start_echo_advice`. Add the safety overrides. Add the new `question` mode to the mode comment. |
| `services/handsfree/echo_client.py` | Add a question builder for a customer question, with a radio argument so a BT case says `"Bluetooth"`. The existing builders hard-code `"Wi-Fi"` and need the same argument. Reuse `ask_echo_kb`. |
| `services/handsfree/composer.py` | Add a `question` branch in `compose_plain`: customer-facing body first, then the raw Echo answer inside the existing Echo markers. Add the rewrite pass. |
| `services/handsfree/orchestrator.py` | Add `question` to the modes that suppress the extra first-response checklist draft in `_analyze_and_enqueue`. Leave it out of `_CHECKLIST_MODES`. Confirm the approve path posts it as a public comment. |
| `templates/handsfree.html` | Add a label for the new mode in the mode map. Add `question` to the list that displays the `"public reply to customer"` chip. |
| `services/handsfree/smoke.py` | Add the new smoke checks listed below. |

---

## Investigate First, Then Test

### Important: Use a Scratch Folder

Always set `INTELAVATAR_HANDSFREE_DIR` to a scratch folder first.

Without this setting, the store is the team's **live share**.

All commands should run:

- from the repo root,
- using the project venv `intel_ava`.

Example scratch folder:

```text
C:\Temp\hf_sandbox
```

### Investigation, Before Any Routing Code

#### Step 1: List the Validation Cases

This does not run any case and does not spend tokens.

```bat
cmd /c "set INTELAVATAR_HANDSFREE_DIR=C:\Temp\hf_sandbox&& intel_ava\Scripts\python.exe -X utf8 tools\handsfree_sim.py --list Handsfree_replier_test_case"
```

#### Step 2: Hand-label the Cases

Have an engineer label each listed case based on its **latest customer comment**:

- `question`
- `debug`
- `mixed`

#### Step 3: Add Classification Only

On a branch, add only:

- the new prompt task,
- `request_type`,
- `customer_question`.

Also print `request_type` in the simulator summary. Do **not** add routing yet.

#### Step 4: Run a Sample

Each run:

- requires the corporate network,
- requires Salesforce sign-in,
- spends LLM tokens.

```bat
cmd /c "set INTELAVATAR_HANDSFREE_DIR=C:\Temp\hf_sandbox&& intel_ava\Scripts\python.exe -X utf8 tools\handsfree_sim.py <case number> <case number> --show-drafts"
```

#### Step 5: Compare Against Hand Labels

Compare Beacon's classification against the engineer's hand labels.

The most costly classification error is:

```text
actual: debug
predicted: question
```

This error causes the log analysis to be skipped. Decide whether to continue with routing based on this result.

---

## Smoke Checks

Run the suite with:

```bat
cmd /c "set INTELAVATAR_HANDSFREE_DIR=C:\Temp\hf_sandbox&& intel_ava\Scripts\python.exe -X utf8 -m services.handsfree.smoke"
```

Add the following checks:

- [ ] A missing or unknown `request_type` is treated as `debug`.
- [ ] A new log archive or BSOD wording forces `debug`.
- [ ] A `question` run skips the log pipeline and queues no extra first-response checklist draft.
- [ ] The public text of a question draft keeps the answer and loses the raw Echo block after `strip_echo_section`.
- [ ] A question draft is never auto-posted, even when the auto-post option is on.
- [ ] Echo unavailable falls back to today's flow.
- [ ] A BT case's Echo question says `"Bluetooth"` and not `"Wi-Fi"`.

Use a **fake LLM** and **fake Echo** in these checks, following the pattern used by the existing S-checks.
