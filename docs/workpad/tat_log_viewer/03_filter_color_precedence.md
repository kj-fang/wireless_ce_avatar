# Sub-task 03: Last-match color precedence

Status: coded, pending manual validation. Folder: [README.md](README.md). Progress: [PROGRESS.md](PROGRESS.md).
Depends on: none.

## Goal

When a line matches more than one enabled include filter, TextAnalysisTool.NET applies filters in
file order and lets a **later** filter's color override an **earlier** one's. Today both matchers
use `Array.find`, so the **first** matching filter wins instead.

Concrete example that fails today: filter `a` (listed first) colors a line yellow; filter `c`
(listed third, more specific) should recolor it red for lines matching both — today it stays
yellow.

## Where

- `static/chatbot/js/features/log-preview.js`:
  - `applyTatRulesToLog` (~line 395): `includeRules.find(rule => matches(line, rule))` — replace
    with an approach that walks all enabled include rules in order and keeps the *last* match
    (e.g. `reduce`, or iterate forward and keep overwriting a `winner` variable instead of
    returning on first hit).
- `static/js/tat_log_preview_worker.js`:
  - `processLine` (~line 88): same `includeRules.find(...)` pattern — apply the same fix.

## Reuse from design_review

None.

## Steps

1. In both files, change the include-rule resolution from "first match wins" to "last match wins"
   by iterating `includeRules` in their existing (file) order and overwriting the matched rule each
   time a later rule also matches, instead of stopping at the first hit.
2. Keep exclude-rule semantics unchanged — an exclude match should still win outright and drop the
   line regardless of order (verify this is already true in both files; it is not part of this
   gap).
3. Keep this change minimal and identical in shape in both files, since sub-task 04 will delete one
   copy — don't let the two implementations diverge in approach.

## Validate

Manual, in-browser. Fixtures already built: `docs/workpad/tat_log_viewer/fixtures/sample_precedence.tat`
(two enabled include filters — `WARN` yellow listed first, `WARN_CRIT` red listed second,
`showOnlyFilteredLines="True"`) and `sample_precedence.log` (3 lines).

1. Load `sample_precedence.tat` + `sample_precedence.log` into the preview.
2. Confirm line 2 (`WARN_CRIT: temperature high`, which matches both filters) renders in **red**
   (the later filter's color), not yellow.
3. Confirm line 1 (`WARN: minor glitch`, which matches only the first filter) still renders
   **yellow**.
4. Repeat in both the legacy ("View filtered skill logs") and virtualized ("Preview") rendering
   paths.
5. Confirm exclude rules are unaffected: reuse `sample_showfalse.tat`/`sample_showtrue.tat` (sub-task
   02's fixtures) and confirm `EXCLUDE_C` lines are still dropped regardless of filter order.

## Done when

- Last-match-wins color resolution is confirmed in both matchers and `PROGRESS.md` row 03 is `done`
  with the manual-check summary in the commit message.
