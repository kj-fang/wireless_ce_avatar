# Progress: Skill-aware snippet analysis

Parent: [README.md](README.md).

| # | Sub-task | Workpad | Depends on | Status | Validated | Commit | Notes |
|---|---|---|---|---|---|---|---|
| 01 | Skill-aware snippet analysis service | [01_skill_aware_service.md](01_skill_aware_service.md) | — | done | yes | — | 8 focused service tests and 2 Echo service tests pass, including deterministic keyword hit/no-hit behavior. |
| 02 | Wire current skills and disclose usage | [02_route_and_ui.md](02_route_and_ui.md) | 01 | coded | no | — | 3 route tests, 8 service tests, Python compile, JavaScript syntax, and diagnostics pass; desktop browser checklist remains. |

Next: Complete the manual desktop-browser checklist for 02, then mark it done.

## Log

- 2026-11-13: Workpad created after tracing viewer selection through `ChatbotSnippets`, `/explain_snippet`, and `services/chatbot/snippets.py`. Confirmed the selected-fragment endpoint is separate from `/chat` and currently uses a generic, tool-free Avatar prompt with no active skill catalog.
- 2026-11-13: Implemented 02. The route now forwards only the current Avatar agent's skills and returns selection metadata; the fragment UI displays applied skills or a no-match/selection-failed notice, while Echo remains unchanged. Automated checks passed; desktop browser validation is pending.
- 2026-11-13: Changed skill eligibility to selected-row keyword hits. The route forwards keywords; up to three matching skills are deterministically ranked and applied without a second LLM selection call. The UI shows matched keyword evidence. Added tests proving filename/question text cannot create a hit. Automated checks passed; desktop browser validation remains pending.
