# Sub-task 02: Wire current skills and disclose usage

Status: open. Folder: [README.md](README.md). Progress: [PROGRESS.md](PROGRESS.md).
Depends on: 01.

## Goal

The snippet endpoint must pass the active Avatar agent's log-oriented keywords into the service, and the fragment UI must show both the applied skill(s) and keyword evidence. Keywords in selected log rows are the primary match signal; filenames and the user's question do not count as log evidence. Echo continues to use its existing fragment-only path.

## Where

- [`blueprints/chatbot/chatbot_routes.py`](../../../blueprints/chatbot/chatbot_routes.py) — `AgentRoutes.explain_snippet` retrieves the current conversation's configuration agent via `_get_configuration_agent()` and passes its loaded skill name, description, keywords, and `expert_rules` for Avatar requests only.
- [`services/chatbot/snippets.py`](../../../services/chatbot/snippets.py) — matches keywords case-insensitively against selected rows and deterministically applies up to three hits ranked by keyword specificity and count.
- [`static/chatbot/js/features/snippets.js`](../../../static/chatbot/js/features/snippets.js) — `ChatbotSnippets.send` calls `/explain_snippet`, renders applied skills and matched keyword evidence separately from the answer, and stores fragment conversation history without altering Echo output.
- [`unit_tests/test_log_chatbot_blueprint.py`](../../../unit_tests/test_log_chatbot_blueprint.py) — existing profile route tests are the nearby backend test surface; add coverage for current-agent skill forwarding, no configured skills, and Echo isolation as appropriate.

## Steps

1. In `AgentRoutes.explain_snippet`, obtain skills from the current configuration agent only for Avatar, convert name, description, keywords, and `expert_rules` to the service catalog, and pass them through. Do not source skills from a shared global or another profile.
2. Adapt the endpoint response to return the service's answer and applied-skill metadata while preserving the existing `success` and `answer` fields consumed by current callers.
3. Update the fragment UI to display applied skill names, matched keywords, and concise selection reason (or the explicit no-match/selection-failed state) for Avatar. Keep the existing question, selected-fragment attachment, cancellation, and history behavior intact.
4. Keep Echo's request and response behavior unchanged; verify its answer does not display or claim Avatar skill metadata.
5. Add route-level tests using synthetic skills and mock LLM calls. Manually exercise the desktop viewer for matching skill, no-match, follow-up, and Echo cases.

## Validate

Run focused backend coverage (pytest is not installed in the project virtual environment, so use the existing unittest-compatible tests):

```powershell
$env:PYTHONPATH = (Get-Location).Path
\.\intel_ava\Scripts\python.exe tests\test_chatbot_snippets.py
\.\intel_ava\Scripts\python.exe -m unittest unit_tests.test_log_chatbot_blueprint.TestExplainSnippet
```

Then manually verify in the desktop browser:

- Select a synthetic fragment with exactly one configured keyword hit; Avatar applies that skill without description-based selection and shows the matched keyword and original line references.
- Select a fragment with keywords matching multiple skills; Avatar deterministically applies up to three matching skills, prioritizing longer/more matched keywords, and shows their keyword evidence.
- Put a skill keyword only in the filename or user question, not the selected rows; it must not count as a hit.
- Select a fragment with no relevant keyword; Avatar clearly reports no matching configured skill and still explains only the selected evidence.
- Ask a follow-up in the same fragment conversation; the answer remains scoped to the same selection and reports the expertise actually used.
- Switch to Echo; its current fragment explanation works and does not claim to use Avatar skills.
- Existing 200-line / 8,000-character limits, clear/cancel behavior, and unrelated chat behavior remain intact.

## Done when

Focused backend tests pass and all applicable manual checks pass; the Avatar snippet UI identifies the applied skill(s), while Echo and fragment-only boundaries remain unchanged. Update the sub-task row in `PROGRESS.md`.
