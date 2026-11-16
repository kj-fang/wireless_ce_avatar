# Sub-task 01: Skill-aware snippet analysis service

Status: done. Folder: [README.md](README.md). Progress: [PROGRESS.md](PROGRESS.md).
Depends on: none.

## Goal

The Avatar fragment path uses configured log keywords as the primary skill signal. Match them case-insensitively against selected rows only, rank by matched keyword specificity and count, and deterministically apply up to three skills before using their `expert_rules`.

## Where

- [`services/chatbot/snippets.py`](../../../services/chatbot/snippets.py) — `build_snippet_request` owns fragment validation, row formatting, limits, and history; `explain_snippet` currently sends the generic Avatar call or the separate Echo call.
- [`services/chatbot/engine/system.py`](../../../services/chatbot/engine/system.py) — the `Skill` data model defines the configured name, description, `keywords`, and `expert_rules` that the injected catalog provides.
- [`tests/test_chatbot_snippets.py`](../../../tests/test_chatbot_snippets.py) — focused service tests cover keyword matching, candidate restriction, rule application, no-match, malformed selector output, and Echo isolation.

## Steps

1. Normalize the supplied skill catalog, including keyword lists, while retaining the existing Echo branch and snippet validation/limits.
2. Match case-insensitive keyword substrings against selected row text only; do not match the filename, question, or omitted rows.
3. Return no-match when nothing hits; otherwise rank by total matched keyword length, then hit count, and apply up to the top three hits without a second model-selection call. Retain the actual matched keyword evidence.
4. Apply selected `expert_rules` with the fragment-only safety constraints and return answer plus skill metadata for the route/UI.
5. Add synthetic tests for matching boundaries, single/multiple candidates, selected rules, metadata, fallback, and Echo isolation.

## Validate

Run the focused service tests:

```powershell
$env:PYTHONPATH = (Get-Location).Path
\.\intel_ava\Scripts\python.exe tests\test_chatbot_snippets.py
```

Confirm tests cover: only selected-row keyword hits apply; question/filename terms do not count; multiple hits are deterministically ranked and capped at three; selected `expert_rules` reach the explanation call; no-match stays general; line references and omitted markers remain; Echo never receives or applies Avatar skills.

## Done when

The 8 focused service tests pass, Avatar analysis applies only keyword-matched configured expertise using the selected fragment, Echo remains unchanged, and the result contract reports applied skill and matched-keyword metadata for sub-task 02.
