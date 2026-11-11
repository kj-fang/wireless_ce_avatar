# Sub-task 09: Per-filter hit counts

Status: coded. Folder: [README.md](README.md). Progress: [PROGRESS.md](PROGRESS.md).
Depends on: none (implemented directly against today's duplicated matchers; sub-task 04 should
fold this into the shared matcher once it lands).

## Goal

The TAT Filter panel listed each rule's mod (enable checkbox + letter) and pattern, but gave no
way to see how many log lines a given keyword/regex actually matches — in real
TextAnalysisTool.NET, the filter list's "Hits" column shows this per rule, independent of whether
that rule is currently enabled.

## Where

- `static/chatbot/js/features/log-preview.js`:
  - `applyTatRulesToLog` (~line 395, legacy/in-memory matcher) — now accumulates a `ruleHitCounts`
    array (`rules.forEach` test per candidate line) and returns it alongside `sections`/`text`.
  - `renderKeyLogContent` (~line 514) — legacy branch stores the returned `ruleHitCounts` on
    `dialog.__tatHitCounts` and re-renders the filter panel so counts refresh after every scan.
  - `renderTatFilterOptions` (~line 436) — each rule row now appends a third
    `.key-log-filter-hits` cell reading `dialog.__tatHitCounts[index]`.
  - `startTatLogScan`'s worker `'ready'` handler (~line 119) — stores `message.ruleHitCounts` the
    same way for the virtualized path, and calls `renderTatFilterOptions` after `renderKeyLogContent`.
  - The modal's header markup (`openKeyLogModal`, innerHTML template) gained a `Hits` header cell.
- `static/js/tat_log_preview_worker.js`:
  - `scanSource`/`processLine` now tally the same per-rule counts over every candidate line, testing
    **all** compiled rules (not just the enabled include/exclude subsets used for row matching), so
    a disabled rule's count is still visible.
  - The `'ready'` message now includes `ruleHitCounts`.
- `static/chatbot/css/log-preview.css`: `.key-log-filter-head`/`.key-log-filter-rule` grid gained a
  third `46px` column; added `.key-log-filter-hits-head`/`.key-log-filter-hits` styling
  (right-aligned, tabular numbers).

## Known duplication (same shape as gap #4)

This adds a **third** copy of per-line rule matching (legacy tally, worker tally, plus each
matcher's own existing include/exclude pass) — the same duplication problem sub-task 04 exists to
fix. When 04 lands, fold hit-count tallying into the single shared matcher pass instead of keeping
a separate loop.

## Validate

Manual, in-browser (no automated test harness for this file):

1. Load a synthetic `.tat` with a few filters (mixed enabled/disabled, include/exclude, plain and
   regex) against a sample log.
2. Confirm each rule's "Hits" cell shows a plausible count (matches your own quick grep/count over
   the sample log) and that a **disabled** rule still shows a (non-blank) count.
3. Toggle a rule on/off and confirm its hit count is unchanged (hit count is a property of the
   pattern vs. the log, not of whether it's currently applied).
4. Repeat in the virtualized path (large/streamed log via "advanced mode") and confirm counts match
   the legacy path for the same `.tat`/log pair.

## Done when

- The Hits column renders correct per-rule counts in both matchers, counts are independent of
  enabled state, and `PROGRESS.md` row 09 is `done` with the manual-check summary in the commit
  message.
