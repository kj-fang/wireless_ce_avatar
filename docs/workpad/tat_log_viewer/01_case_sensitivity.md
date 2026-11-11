# Sub-task 01: Honor `case_sensitive`

Status: coded, pending manual validation. Folder: [README.md](README.md). Progress: [PROGRESS.md](PROGRESS.md).
Depends on: none.

## Goal

A `.tat` filter with `case_sensitive="y"` only matches an exact-case substring (or a
case-sensitive regex). Today it behaves identically to `case_sensitive="n"` in both matchers.

Concrete example that fails today: a filter `text="SAE"` with `case_sensitive="y"` currently also
highlights a line containing `sae` or `Sae` — it should not.

## Where

- `static/chatbot/js/features/log-preview.js`:
  - `parseTatFilterRules` (~line 290) — add `caseSensitive` to the parsed rule object from the
    `case_sensitive` attribute.
  - `applyTatRulesToLog`'s `matches()` closure (~line 374) — branch on `rule.caseSensitive`.
- `static/js/tat_log_preview_worker.js`:
  - `compileRules` (~line 38) — same attribute needs to reach the worker; `log-preview.js`'s
    `startTatLogScan` (~line 148) already maps a subset of rule fields into the `postMessage`
    payload — add `caseSensitive` there too.
  - `scanSource`'s `matches()` closure (~line 68) and `processLine`'s `lowerLine` precomputation
    (~line 87) — case-sensitive rules must compare against the original-case `line`, not
    `lowerLine`.
  - Regex rules: compile without the `i` flag when `rule.caseSensitive` is true (both files).

## Reuse from design_review

None — this is the feature's own rule, no other module owns filter matching.

## Steps

1. Parse `case_sensitive` in `parseTatFilterRules`, default `false` (matches TextAnalysisTool.NET's
   own default when the attribute is absent).
2. Thread `caseSensitive` through the `rules` array built in `startTatLogScan` before
   `worker.postMessage`.
3. Update `compileRules` in the worker to keep `caseSensitive` and compile the regex matcher with
   or without `'i'` accordingly.
4. Update both `matches()` implementations to compare case-sensitively when the flag is set
   (compare `line` directly, not `lowerLine`/`rule.searchText.toLowerCase()`).
5. No automated test exists for this file; do not add a Python test.

## Validate

Manual, in-browser (no pytest — this file has no test harness). Fixtures already built:
`docs/workpad/tat_log_viewer/fixtures/sample_case.log` (6 lines) and `sample_case.tat` (3 filters —
`SAE` case-sensitive, `sae` case-insensitive, `ERR\d+` case-sensitive regex, `showOnlyFilteredLines="False"`
so every line renders and is visible for inspection).

1. Load `sample_case.tat` + `sample_case.log` into the preview (both the virtualized "Preview" path
   and the legacy "View filtered skill logs" path).
2. Check the Hits column (sub-task 09) rather than line color — overlapping-rule color precedence
   is sub-task 03's separate, still-open gap and would confound a purely visual read. Expected:
   `SAE` → 1 (only the exact-case line), `sae` → 3 (all three case variants), `ERR\d+` → 1 (only the
   upper-case `ERR42` line, not lower-case `err42`).
3. Toggle the `sae` (case-insensitive) rule off and confirm `SAE`'s count/highlight is unaffected —
   the two rules are independent.
4. Record the counts above in `PROGRESS.md`'s Measurements table once confirmed.

## Done when

- The checklist above holds for both the legacy (`applyTatRulesToLog`) and virtualized
  (worker) paths.
- `PROGRESS.md` row 01 is `done`, with the manual-check summary in the commit message.
