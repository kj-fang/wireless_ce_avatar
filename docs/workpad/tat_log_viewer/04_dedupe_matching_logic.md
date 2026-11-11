# Sub-task 04: One shared matcher (dedupe)

Status: open. Folder: [README.md](README.md). Progress: [PROGRESS.md](PROGRESS.md).
Depends on: 01, 02, 03 (fix correctness in both copies first; this sub-task then deletes one copy,
so it must start from two already-correct, already-converged implementations).

## Goal

`applyTatRulesToLog`'s `matches()`/line-resolution logic in `log-preview.js` and `processLine`'s
`matches()`/line-resolution logic in `tat_log_preview_worker.js` are two independent
implementations of the same rule-matching algorithm. Collapse them into one shared function so a
future fix (or the fixes from 01–03) can't drift apart again.

## Where

- `static/chatbot/js/features/log-preview.js` — `applyTatRulesToLog` (~line 365) owns the legacy
  matcher.
- `static/js/tat_log_preview_worker.js` — `compileRules` (~line 38) and `processLine` (~line 80)
  own the worker matcher. The worker runs in a separate global scope (`self`), so it cannot
  `import` from `log-preview.js` directly without a build step — this repo has no bundler.

## Reuse from design_review

None.

## Steps

1. Decide the shared-module mechanism given there's no bundler: the simplest option is a small,
   dependency-free script (e.g. `static/js/tat_filter_match.js`) that defines the rule-compilation
   and line-matching functions as plain functions attached to a shared namespace (e.g.
   `self.TatFilterMatch` / `window.TatFilterMatch`), loaded via `<script>` in the page template and
   via `importScripts()` in the worker. Confirm this pattern against how other shared vanilla-JS
   utilities in `static/chatbot/js/` are loaded (check `templates/chatbot/page.html` script order)
   before introducing a new loading convention.
2. Move `compileRules`-equivalent rule compilation (regex building respecting `caseSensitive` from
   sub-task 01) and the line-resolution algorithm (respecting `showOnlyFilteredLines` from 02 and
   last-match-wins from 03) into the shared module.
3. Update `applyTatRulesToLog` to call the shared function instead of its own inline `matches()`.
4. Update the worker's `processLine` to call the same shared function.
5. Delete the now-dead duplicate logic from whichever file still has it after the swap.
6. Confirm `parseTatFilterRules`'s output shape (rule object field names) matches what the shared
   module expects, since both callers already produce the same shape from sub-task 01/02's parsing
   changes.

## Validate

Manual, in-browser — this is a refactor, so it's a regression check against the behavior already
validated in 01–03, not new behavior:

1. Re-run the 01, 02, and 03 validation checklists against both the legacy and virtualized paths
   after the refactor; confirm identical results to before the dedupe.
2. Diff the two files to confirm the duplicate `matches()`/line-resolution code is actually gone
   from one of them, not just unused.

## Done when

- Both paths call one shared implementation, the 01–03 checklists still pass, and `PROGRESS.md` row
  04 is `done` with a note on which file now owns the shared logic.
