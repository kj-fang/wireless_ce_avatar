# Progress: TAT log viewer

Parent: [README.md](README.md).

| # | Sub-task | Workpad | Depends on | Status | Validated | Commit | Notes |
|---|---|---|---|---|---|---|---|
| 01 | Honor `case_sensitive` | [01_case_sensitivity.md](01_case_sensitivity.md) | — | coded | no | — | Implemented 2026-11-11; manual in-browser validation not yet run. |
| 02 | Honor `showOnlyFilteredLines` | [02_show_only_filtered_lines.md](02_show_only_filtered_lines.md) | — | done | yes | — | Validated 2026-11-10: both modes/matchers, exclude rules in both modes, Ctrl+H toggle both directions (no leak to Chrome History), reveal-from-selection anchoring. |
| 03 | Last-match color precedence | [03_filter_color_precedence.md](03_filter_color_precedence.md) | — | coded | no | — | Implemented 2026-11-11; manual in-browser validation not yet run. |
| 04 | One shared matcher (dedupe) | [04_dedupe_matching_logic.md](04_dedupe_matching_logic.md) | 01, 02, 03 | open | no | — | |
| 05 | In-app filter authoring | [05_in_app_filter_authoring.md](05_in_app_filter_authoring.md) | 04 | done | no | — | Scope cut to add/edit/delete/Save/Save-As (reorder deferred); jumped ahead of the 04 dependency 2026-11-11, same as 07/08/09. Manually validated 2026-11-11; steps 5/6 (zero-rule disabled state, Save round-trip) explicitly skipped by owner's choice, not failed. |
| 06 | Bookmarks / next-match nav | [06_bookmarks_next_match.md](06_bookmarks_next_match.md) | 04 | open | no | — | |
| 07 | Copy selected lines | [07_copy_selected_lines.md](07_copy_selected_lines.md) | — | done | yes | — | Validated 2026-11-10: off-screen selection, Copy, Ctrl+C (after fixes), 500+ line cap split, "Add to question" unaffected. |
| 08 | Default .tat from local folder | [08_default_tat_from_local_folder.md](08_default_tat_from_local_folder.md) | — | done | yes* | — | *5/6 validate steps confirmed; owner explicitly chose to skip the ambiguous-2-files case and accept the risk. |
| 09 | Per-filter hit counts | [09_filter_hit_counts.md](09_filter_hit_counts.md) | — | done | yes | — | Validated 2026-11-10: counts correct and enabled-independent in both the worker (virtualized) and `applyTatRulesToLog` (legacy) implementations. |

Next: 01 and 03 are both coded, pending manual validation. 04 cannot start until both are validated
(02 and 09 are now done).

## Measurements

| Sub-task | Check | Before | After |
|---|---|---|---|
| 01 | Case-sensitive filter hit count on sample log | — | — |
| 02 | Total visible rows with `showOnlyFilteredLines=False` | — | — |
| 03 | Color shown on a line matching two filters | — | — |

## Log

- 2026-11-04: Workpad created. Read `log-preview.js` and `tat_log_preview_worker.js` in full,
  identified 6 gaps vs. real TextAnalysisTool.NET behavior, wrote README.md and all 6 sub-task
  files. No code changed yet.
- 2026-11-04: Owner requested 3 more checks. Confirmed enable/disable of an existing filter already
  works today (no sub-task needed). Added sub-task 07 (copy selected lines to clipboard) — a real
  gap. Add/delete of a filter was already covered by sub-task 05's scope; kept 05 bundled
  (add+edit+delete+reorder+save) rather than splitting it, per the owner's choice.
- 2026-11-04: Implemented sub-task 07. Added a "Copy" button beside "Add to question" in
  `enableKeyLogSelection` (`log-preview.js`), reusing the same selection state and the worker's
  `type: 'selection'` range extraction (not DOM text) so it works for off-screen virtualized rows.
  The selection request now carries a `purpose` (`'add'`/`'copy'`); `tat_log_preview_worker.js`
  applies the existing 200-line/8,000-char cap only to `'add'` and a separate 5,000-line-only cap to
  `'copy'`. Clipboard failures surface in `.key-log-state` instead of failing silently. Decided to
  keep selection/Copy host-only (recorded in 07's doc) since the plain modal already renders real
  text nodes where native copy works. `node --check` passed on both files; manual in-browser
  validation (virtualized off-screen selection, 500+ line copy, Add-to-question still capped at 200)
  still needs to be run before marking this `done`.
- 2026-11-06: Owner asked for a default `.tat` to auto-load from a local folder. Read
  `utils/port_utils.py`, `utils/helpers.py`, and `configs/path_configs.py` for this repo's existing
  local-file and shared-network-folder conventions, and the separate/unrelated "Log Parser"
  feature's own `.tat`-folder convention (`utils/log_parser_file_utils.py`'s
  `get_available_filters`) to avoid confusing the two. Owner chose the existing
  `<Downloads>\IntelAvatar_files\logs` folder (reusing `get_logs_dir()`) over a new dedicated
  folder or the Log Parser's shared network folder. Added sub-task 08 — the first sub-task in this
  workpad needing a backend route, since only the server can read a fixed local path. No code
  changed yet.
- 2026-11-09: Restored the selection size-cap check in `tat_log_preview_worker.js` that had been
  commented out outside this conversation (both `'add'` and `'copy'` were uncapped), and raised
  `COPY_MAX_LINES` from 5,000 to 50,000 per owner request. `ADD_TO_QUESTION_MAX_LINES`/`_MAX_CHARS`
  (200/8,000) left unchanged — flagged that enlarging those requires matching changes in
  `services/chatbot/snippets.py`'s `MAX_SNIPPET_LINES`/`MAX_SNIPPET_CHARS` since the server
  independently re-validates before building the chat prompt; owner has not yet picked a new value.
- 2026-11-09: Added sub-task 09 (per-filter hit counts in the TAT Filter panel's new "Hits" column)
  and implemented it directly: `applyTatRulesToLog` and the worker's `processLine` each tally a
  `ruleHitCounts` array (count independent of enabled state), threaded through to
  `renderTatFilterOptions` via `dialog.__tatHitCounts`. `node --check` passed on both JS files;
  still needs manual in-browser validation (see 09's doc) before marking `done`. Noted this adds a
  third copy of per-line matching logic, to be folded into sub-task 04's shared matcher later.
- 2026-11-09: Owner asked to add a feature for editing an existing filter's keyword text, in
  addition to add/delete. Checked the actual code: none of add, edit, or delete exist yet today —
  only the enable/disable checkbox is implemented (`renderTatFilterOptions`). All three
  (add/edit/delete, plus reorder/save) are already sub-task 05's bundled scope (see its step 2 for
  inline edit specifically) — no new sub-task needed, this is tracked there, not split out. Owner
  chose to hold off implementing until sub-task 04 (dedupe the legacy/worker matchers) is done
  first, per the workpad's original dependency order (05 depends on 04) — unlike sub-tasks 07/08/09,
  which jumped the queue because they had no stated dependency. No code changed.
- 2026-11-09: Owner had agreed to a Ctrl+C shortcut for the sub-task 07 Copy button, but it was
  never actually implemented (conversation moved on before the code was written) — owner reported
  "Ctrl+C doesn't work", which was correct. Added it to `enableKeyLogSelection`: a `keydown`
  listener on the dialog intercepts Ctrl+C/Cmd+C when a click/shift-click row selection is active
  (skipped when focus is in an input/textarea/select), calling the same `copySelection()` path as
  the button. `node --check` passed. Ctrl+H (toggle hide/show unfiltered lines) is still
  undecided/unimplemented — scope discussion paused on terminology, not yet resumed.
- 2026-11-09: Owner chose "Full engine: both views, tied to sub-task 02" for Ctrl+H — rather than a
  fake dim/blur overlay, built the real `showOnlyFilteredLines` engine (sub-task 02) and wired
  Ctrl+H as a runtime toggle on top of it, since both need the same "keep non-matching lines"
  behavior. `parseTatFilterRules` now returns `{rules, showOnlyFilteredLines}` (reads the root
  `<TextAnalysisTool.NET showOnlyFilteredLines="True"|"False">` attribute, default `true`).
  `applyTatRulesToLog` and the worker's `processLine` both now keep every non-excluded line when the
  flag is `false`, rendering unmatched ones plain instead of dropping them (exclude rules still
  always win). Ctrl+H/Cmd+H is handled in `openKeyLogModal`'s shared dialog `keydown` listener
  (works in both host and plain modals, unlike the host-only Copy shortcut), flips
  `dialog.__showOnlyFilteredLines`, and re-renders; it no-ops with no `.tat` loaded or when focus is
  in a form field. `node --check` passed on both JS files; manual in-browser validation (both
  matchers, both attribute values, Ctrl+H toggle, exclude rules still dropping lines) still needs to
  be run before marking row 02 `done`. See [02_show_only_filtered_lines.md](02_show_only_filtered_lines.md)
  for full details.
- 2026-11-09: Owner reported Ctrl+H was instead opening Chrome's History page. Root cause: in
  host-embedded mode (the split workspace pane) the dialog is never opened via `showModal()` (only
  shown with a CSS override), so it never traps focus — the dialog-scoped `keydown` listener missed
  the keystroke whenever focus was elsewhere on the page (e.g. the chat input), letting Chrome's own
  shortcut fire. Fixed by moving Ctrl+H handling to a `document`-level capture-phase listener per
  dialog instance, gated on that dialog's own visibility (`dialog.open` or
  `dialog.offsetParent !== null`). `node --check` passed. See 02's doc for the full root-cause
  writeup.
- 2026-11-10: Owner asked that Ctrl+H, when a line is selected, reveal the toggled view anchored at
  that line instead of resetting to the top. Virtualized/host path only (selection doesn't exist in
  the legacy/plain modal). The Ctrl+H handler now captures the selected row's stable
  `dataset.lineNumber` into `dialog.__pendingRevealLine` before re-scanning (since
  `startTatLogScan` nulls the selection indices immediately); added a `'locate'` message type —
  `tat_log_preview_worker.js` binary-searches its `lineNumber`-ordered `matchedRows` for the new row
  index, and `log-preview.js` scrolls the virtualized viewport there (skipping the usual
  scroll-to-top reset) once it replies. `node --check` passed on both JS files. See 02's doc for
  details; still needs manual in-browser validation alongside the rest of sub-task 02.
- 2026-11-10: Began manual validation of sub-task 07. The Copy button works correctly (off-screen
  shift-click selection, paste in order, full range). Ctrl+C, however, copied stale/previous
  clipboard content instead — same root cause as the Ctrl+H/Chrome-History bug (sub-task 02): the
  listener was attached to `dialog` (bubble phase), but `enableKeyLogSelection` only runs in
  host-embedded mode, where the dialog never traps focus. Fixed by moving it to a
  `document`-level capture-phase listener gated on the dialog's visibility, same pattern as the
  Ctrl+H fix. That alone wasn't enough — clicking a log row doesn't move keyboard focus away from
  the chat input (`pointerdown` calls `preventDefault()`), and the handler bailed out on any
  input/textarea focus to avoid hijacking normal text-field copy, so Ctrl+C silently no-opped while
  the chat input was still focused. Refined the bail to only skip when that field has its own
  non-empty text selection, not just focus. Owner confirmed Ctrl+C now works. `node --check`
  passed. See 07's doc for the full writeup.
- 2026-11-10: Finished validating sub-task 07 — 500+ line selection: Copy succeeds (50,000-line
  cap), "Add to question" correctly rejects with its existing size-limit message (200-line cap);
  "Add to question" still succeeds normally on a small selection. All 4 validation steps now
  confirmed by the owner. Marked row 07 `done`.
- 2026-11-10: Implemented sub-task 08. Backend: `default_tat_filter` on `AgentRoutes`
  (`blueprints/chatbot/chatbot_routes.py`, beside `preview_log`/`preview_log_chunk`) lists
  `*.tat` files (case-insensitive) in `get_logs_dir()` (`utils/port_utils.py`); returns the raw XML
  with the real file name in an `X-Tat-Name` header when exactly one is found, or a 404
  `{"success": false}` for zero, two-or-more, or any read error — ambiguous stays silent, same as
  missing. Registered in `handlers` and in `factory.py`'s `ROUTE_SPECS` behind the existing
  `log_preview` capability, same as `preview_log`. Frontend: added `loadDefaultTatFilter` in
  `log-preview.js`, called once from `openKeyLogModal` (gated on `!dialog.__tatRules &&
  !dialog.__defaultTatAttempted`, so it fires once per dialog instance and never after a manual
  "Load .tat"). It fetches `${LOG_PREVIEW_API}/default_tat_filter`, silently no-ops on a non-OK
  response or fetch error, and otherwise reads the real name back from `X-Tat-Name`
  (`decodeURIComponent`) and runs the exact same `parseTatFilterRules` → set
  `__tatRules`/`__showOnlyFilteredLines`/`__tatName` → `renderTatFilterOptions` →
  `startTatLogScan`/`renderKeyLogContent` steps the manual file-input handler already uses, so
  malformed XML surfaces the same "Could not load the .tat filter" message. The manual "Load .tat"
  button/file input are untouched.
- 2026-11-10: Owner asked to keep the dropped file's own name instead of requiring a rename to
  `default.tat`. Revised sub-task 08's design (doc and code) before validation started: the backend
  now picks up whichever single `.tat` file sits in the logs folder, under any name, and reports
  that real name to the frontend via an `X-Tat-Name` response header instead of hardcoding
  `"default.tat"` as the display name; zero or 2+ `.tat` files in the folder is treated as
  ambiguous and silently no-ops, same as missing, since the server won't guess which one is "the"
  default. `node --check` passed on `log-preview.js`; `python -I -m py_compile` passed on both
  Python files. Still needs manual in-browser validation (all 6 steps in 08's doc, including the
  new ambiguous-folder case) before marking row 08 `done`.
- 2026-11-10: Owner reported Ctrl+H (sub-task 02) broke again — back to opening Chrome's History
  page. Root cause this time: the Ctrl+H `keydown` handler in `openKeyLogModal` only called
  `event.preventDefault()` *after* confirming `dialog.__tatRules` was populated, which was safe
  before sub-task 08 because loading a `.tat` always required a slow, manual file-picker round
  trip — by the time a user could reach for Ctrl+H, rules were long since loaded. Sub-task 08's
  auto-load fetch happens asynchronously right when the dialog opens, leaving a real (if brief)
  window where `__tatRules` is still empty; a Ctrl+H pressed in that window hit the early return
  and leaked straight to the browser. Fixed by always calling `preventDefault()` once the dialog is
  visible and focus isn't in a form field, and only skipping the actual toggle (not the
  `preventDefault`) when no rules are loaded yet. `node --check` passed.
- 2026-11-10: Ran a `/code-review` pass (effort: high) over the working tree. One finding landed in
  sub-task 08's area: the previous Ctrl+H fix (above) swallowed the keystroke *forever* once no
  rules were loaded — not just during the auto-load race window — permanently blocking the
  browser's native Ctrl+H for as long as the log-viewer dialog stays visible whenever no
  `default.tat` exists and the user never loads one manually. Refined: `loadDefaultTatFilter` now
  wraps its body in `try/finally` and clears a new `dialog.__defaultTatPending` flag (set
  alongside `__defaultTatAttempted` right before the fetch starts) on every exit path — success,
  404, fetch error, or parse error. The Ctrl+H handler only force-swallows the keystroke while
  `__defaultTatPending` is still true; once the fetch has settled with no rules, Ctrl+H falls
  through to the browser again, matching the original (pre-sub-task-08) intent. `node --check`
  passed. The other `/code-review` findings (a `log_scope.py` dict-collision bug, an uncleared
  `check_ips_prompt.js` hover timer, an asymmetric `wpp_ddd_parser.py` cache-corruption guard, a
  `skill_analysis.py` truncation bug, an unsanitized-HTML risk in `echo-chat.js`, a hardcoded
  `"key_logs"` literal in `history_service.py`, and a low-confidence `check_ips_service.py` billing
  race) are all in unrelated areas of the working tree, not sub-task 08 — left untouched, flagged
  here only as a pointer in case they need their own workpad entries later.
- 2026-11-10: Owner confirmed 2 of 08's 6 validate-checklist items: (1) a `.tat` dropped under its
  own real name into `<Downloads>\IntelAvatar_files\logs\` auto-loads on reopen and the filter-name
  label shows that real name, and (2) Ctrl+H toggles correctly post-fix, no longer leaking to
  Chrome's History. Not yet confirmed: no-`.tat`-present behavior, manual "Load .tat" override,
  corrupt-XML error path, the ambiguous-two-`.tat`-files no-op, and the plain-modal ("View filtered
  skill logs") path specifically. Kept row 08 `coded` (not `done`) per this workpad's own rule that
  a sub-task is done only once its full Validate checklist holds — marked `partial` in the
  Validated column rather than overclaiming.
- 2026-11-10: Owner confirmed 2 more of 08's validate steps: (3) with no `.tat` present in
  `<Downloads>\IntelAvatar_files\logs\` the viewer behaves exactly as before auto-load existed (no
  filter, no error), and (4) manually picking a different file via "Load .tat" while a default is
  already loaded correctly overrides it for the session. 4/6 confirmed now. Remaining: corrupt-XML
  error path, the ambiguous-two-`.tat`-files no-op, and the plain-modal ("View filtered skill logs")
  path specifically.
- 2026-11-10: Owner confirmed 2 more of 08's validate steps: (5) a corrupt/invalid XML file as the
  sole `.tat` surfaces the existing "Could not load the .tat filter." error path, not a silent
  failure or crash, and (6) the plain modal ("View filtered skill logs") auto-loads a default the
  same way the advanced-mode panel does. 5/6 confirmed now. Remaining: the ambiguous-two-`.tat`-files
  case (two files present → silent no-op; drop back to one → loads again).
- 2026-11-10: Began manual validation of sub-tasks 02 and 09 together (same TAT Filter panel
  screen). Built reusable fixtures under `docs/workpad/tat_log_viewer/fixtures/`: `sample.log` (25
  lines with known, exact hit counts) and `sample_showfalse.tat` / `sample_showtrue.tat` (same 4
  filters — one enabled include, one **disabled** include, one exclude, one regex include — differing
  only in the root `showOnlyFilteredLines` attribute). Using the virtualized/"Preview" path: loaded
  `sample_showfalse.tat` and owner confirmed all of: the 22/25 kept-line count and exact line
  ordering (all 3 excluded lines correctly dropped, pasted and diffed line-by-line), the status line
  text, correct per-line coloring (enabled include rules colored, disabled include rule left plain),
  and the Hits column (`MATCH_A=5`, `DISABLED_B=2` i.e. a disabled rule still counts, `EXCLUDE_C=3`,
  `ERR\d+=2`), including that toggling a rule's enabled checkbox leaves its Hits count unchanged.
  This confirms 02's validate items 1+2 and 09's validate items 1+2+3, in the virtualized path only.
  Remaining for 02: flip to `showOnlyFilteredLines="True"` (item 3), exclude rule still removing
  lines in True mode (item 4b), the legacy (small/in-memory) matcher path (item 5), and the Ctrl+H
  runtime toggle + reveal-from-selection behavior (separate "Done when" criteria). Remaining for 09:
  repeat/confirm counts match in the legacy path (item 4).
- 2026-11-10: Loaded `sample_showtrue.tat` (same filters, `showOnlyFilteredLines="True"`) over the
  same preview; owner confirmed exactly 7 lines shown (5 `MATCH_A` + 2 `ERR\d+`), reverting to
  today's default "only matched lines" behavior. Confirms 02's validate item 3, and item 4 (exclude
  rules still remove lines) is now confirmed in both modes. Remaining for 02: the legacy
  (small/in-memory) matcher path (item 5), and the Ctrl+H runtime toggle + reveal-from-selection
  behavior.
- 2026-11-10: Owner explicitly chose to skip the remaining ambiguous-two-`.tat`-files validate step
  and accept the risk on that one untested path, rather than run it. Marked row 08 `done` (5/6
  Validate items confirmed: happy-path auto-load under the real file name, Ctrl+H toggling correctly
  post-fix, no-`.tat`-present behaves exactly as before auto-load existed, manual "Load .tat" still
  overrides the default for the session, corrupt/invalid XML surfaces the existing "Could not load
  the .tat filter." error, and the plain modal auto-loads the same as the advanced-mode panel).
  Known gap, not yet exercised: if the logs folder ever ends up with two-or-more `.tat` files at
  once, the code (`default_tat_filter` in `chatbot_routes.py`) is written to 404/no-op rather than
  guess, but this has not been manually confirmed in-browser.
- 2026-11-10: Finished validating the legacy (non-worker) code path — asked the chatbot a real
  question to get a response card with the "View filtered skill logs" footer button, then loaded
  `sample_showfalse.tat` and `sample_showtrue.tat` there. Owner confirmed: the kept-line count is
  unchanged from the unfiltered snippet when `showOnlyFilteredLines="False"`, the Hits column shows a
  count (including `0`) for every rule with no blanks/errors, and switching to the `"True"` variant
  correctly drops non-matching lines. This exercises `applyTatRulesToLog` in `log-preview.js`
  directly (the legacy modal never goes through the worker), which is a genuinely separate
  implementation from the one validated earlier in the virtualized/Preview path. This was the last
  remaining item for both sub-tasks. Marked row 02 `done` (all of: both modes/matchers, exclude
  rules in both modes, Ctrl+H toggle in both directions with no leak to Chrome's History, and
  reveal-from-selection anchoring, confirmed) and row 09 `done` (Hits column correct and
  enabled-independent, confirmed in both the worker and the legacy `applyTatRulesToLog`
  implementations). Fixtures used throughout: `docs/workpad/tat_log_viewer/fixtures/sample.log`,
  `sample_large.log`, `sample_showfalse.tat`, `sample_showtrue.tat` — kept in the repo in case 04's
  dedupe work wants the same known-count fixtures for regression-checking the merged matcher.
- 2026-11-10: With `sample_showfalse.tat` loaded (22-line view), owner pressed Ctrl+H with focus in
  both the log viewer and the chat input box and confirmed it flips to the 7-line matched-only view
  each time (no leak to Chrome's History page), and flips back to the 22-line view on a second
  press. Confirms the "Ctrl+H toggles correctly" part of 02's Done-when criteria. Remaining for 02:
  reveal-from-selection (Ctrl+H with a line selected should anchor the toggled view at that line
  instead of resetting to the top) and the legacy (small/in-memory) matcher path (item 5). Remaining
  for 09: legacy path (item 4).
- 2026-11-10: Built `sample_large.log` (same 25-line pattern repeated 30x = 750 lines, under
  `docs/workpad/tat_log_viewer/fixtures/`) since the original 25-line sample was short enough to fit
  in the viewport without scrolling, making reveal-from-selection unobservable. Owner selected a
  `MATCH_A` row around line 317 (well below the fold) with `sample_showfalse.tat` loaded, pressed
  Ctrl+H twice, and confirmed the view stayed anchored near that line on both the toggle-to-matched
  and toggle-back transitions, instead of resetting to the top. Confirms the last remaining
  "Done when" criterion for 02 in the virtualized path. Remaining for 02: the legacy
  (small/in-memory) matcher path (item 5). Remaining for 09: legacy path (item 4).
- 2026-11-11: Owner asked for two of sub-task 05's five bundled behaviors now: add/edit/delete of a
  filter's keyword, and Save/Save As — explicitly deferring reorder, and explicitly choosing to
  jump ahead of the stated 04 dependency (same precedent as 07/08/09). Planned and implemented in
  `static/chatbot/js/features/log-preview.js` and `static/chatbot/css/log-preview.css`:
  - New `serializeTatFilterRules`/`colorToTatHex`/`escapeXmlAttr` build `.tat` XML from the
    in-memory rule list, mirroring `parseTatFilterRules`'s exact field names so a saved file reloads
    identically. `case_sensitive` is always written `"n"` and `description` always `""` (no UI for
    either — `case_sensitive` would be a no-op control until sub-task 01 lands).
  - New `downloadTatFilter` reuses the existing `showSaveFilePicker`-with-`Blob`-fallback pattern
    already used by `prompt-editor.js`. "Save" re-suggests the tracked `dialog.__tatName`; "Save As"
    suggests `<name>-copy.tat`.
  - New `openFilterEditor`/`closeFilterEditor`/`submitFilterEditor` drive a single shared editor bar
    (`.key-log-filter-editor`, added to the dialog's static template) used for both Add (header
    button) and Edit (new pencil icon per row); a new trash icon per row deletes. Both icon buttons
    call `event.preventDefault()` since they're nested inside the row's `<label>`, which would
    otherwise forward-activate the enable checkbox on click. A regex rule authored/edited in-app has
    `rule.matcher` set explicitly at submit time — the legacy matcher (`applyTatRulesToLog`) relies
    on a pre-compiled `.matcher` and would otherwise silently fall back to literal-substring
    matching for new/edited regex rules (the worker always recompiles its own, so this gap is
    legacy-path-only).
  - Every add/edit/delete re-renders the filter list and re-runs whichever matcher is active
    (`startTatLogScan` for the virtualized/worker path, `renderKeyLogContent` for the legacy path) —
    the same live-refresh pattern the existing enable/disable checkbox already used.
  - CSS: filter-row grid extended from 3 to 4 columns (`44px minmax(0,1fr) 46px 40px`) for the new
    per-row actions cell; added styles for the actions cell and the new editor bar.
  - No changes needed in `tat_log_preview_worker.js` — it already recompiles matchers from whatever
    rules `startTatLogScan` sends each time.
  - `node --check` passed on `log-preview.js`. Updated 05's doc (steps marked done/deferred, a new
    "Known limitations" section, revised Validate checklist) and this row to `coded`. Manual
    in-browser validation (per 05's doc) still needs to be run before marking row 05 `done`.
- 2026-11-11: Began manual validation of 05. Owner added a `MATCH_A` filter via the Preview/
  virtualized panel and reported the Hits number "was gone" (blank) — but highlighting/filtering
  worked correctly, toggling the row's enable checkbox (which re-triggers a full rescan) didn't fix
  it, and no console errors appeared. Since the worker computes hit counts and matched-row
  inclusion from the exact same per-line match check, a real computation bug was ruled out by
  reasoning alone; the owner then directly inspected the DOM (`.key-log-filter-rule` row text) and
  found the actual cause: the Hits number was present in the DOM the whole time, just visually
  covered by the new edit (pencil) icon button. Root cause: the new 4th grid column
  (`.key-log-filter-actions`, added this session for the edit/delete icons) was sized `40px`, too
  narrow for its own content (two `16px` icon buttons + `2px` gap + `4px`+`4px` padding = `42px`),
  so with default CSS Grid `overflow: visible` the overflow spilled left on top of the adjacent
  Hits column. Fixed by widening that grid column from `40px` to `48px` in
  `static/chatbot/css/log-preview.css` (`.key-log-filter-head, .key-log-filter-rule`'s
  `grid-template-columns`) — CSS-only fix, no JS changes. Validate step 1 (Add) now confirmed.
  Remaining: steps 2-8 of 05's Validate checklist.
- 2026-11-11: Owner reported the Hits number was still covered by the edit icon even after the
  `48px` widening (two `16px` icon buttons simply don't fit comfortably in a column this narrow no
  matter the exact width chosen) and asked to replace the edit icon with double-click-to-edit
  instead. Removed the pencil button entirely from `renderTatFilterOptions` in `log-preview.js`;
  the actions column now holds only the trash/delete button. Added a `dblclick` listener on the row
  `<label>` that opens the same editor (`openFilterEditor(dialog, index)`), skipping when the
  double-click lands on the checkbox/letter (`.key-log-filter-mod`) or the delete button
  (`.key-log-filter-actions`) so their own click behavior is undisturbed; added
  `label.title = 'Double-click to edit'` for discoverability. With only one icon button left, the
  actions grid column in `log-preview.css` was shrunk from `48px` back down to `28px`, giving the
  Hits column its full `46px` back. `node --check` passed. Updated 05's doc (step 2) to describe
  double-click-to-edit instead of a pencil icon.
- 2026-11-11: Owner then reported double-clicking a row to edit it also toggled the row's enable
  checkbox, which was confusing. Root cause: each row is a `<label>` wrapping the enable checkbox,
  and every native `click` inside a `<label>` (on any non-control area) forwards a synthetic click
  to its associated control by default — a double-click fires two separate `click` events before
  the single `dblclick` event, so the checkbox was toggled twice (flickering) before the
  `dblclick` handler's own `openFilterEditor` call ever ran; the `dblclick` handler's
  `event.preventDefault()` only suppressed the `dblclick` event's own default action (text
  selection), not the two already-dispatched `click` events' forwarding. Fixed by adding a plain
  `click` listener on the row `<label>` (alongside the existing `dblclick` listener) in
  `renderTatFilterOptions` that calls `event.preventDefault()` for clicks outside the
  checkbox/letter (`.key-log-filter-mod`) or actions (`.key-log-filter-actions`) areas — this
  suppresses the label's forwarding behavior for both clicks of a double-click on the
  pattern/hits area, while a direct click on the checkbox itself still toggles normally since
  that target is excluded from the guard. `node --check` passed. Updated 05's doc (step 2) to
  describe this guard.
- 2026-11-11: Retested after the click-guard fix. Validate step 1 (Add) reconfirmed clean (no
  checkbox flicker on double-click). Validate step 2 (regex rule): owner added `ERR\d+` as a regex
  filter and confirmed it correctly matched `ERR42`/`ERR99` lines (and not plain `ERR` without a
  digit) in both the Preview/virtualized panel and the legacy "View filtered skill logs" modal —
  the `rule.matcher` gotcha (legacy path needing an explicit compiled matcher at submit time, since
  only the worker recompiles its own) did not regress. Validate step 3 (Edit): owner double-clicked
  an existing rule, confirmed the editor opened pre-filled, changed its text/color, and confirmed
  the view updated live. Validate step 4 (Delete): owner deleted a rule and confirmed its
  highlighting and Hits row both disappeared. Validate step 5 (Save/Save-As disabled with zero
  rules) was skipped by owner's choice, not verified. Owner asked for distinct Save/Save-As icons
  (both used the same `save` lucide icon before, differing only by tooltip text) — changed "Save
  As"'s icon from `save` to `copy-plus` (fallback glyph `📋`) in `openKeyLogModal`, leaving "Save"
  as `save`/`💾`. `node --check` passed. Validate step 6 (Save round-trip) was skipped by owner's
  choice, not verified. Validate step 7 (Save As naming + original untouched): owner loaded
  `sample_showfalse.tat`, edited a rule, clicked "Save As", confirmed it suggested
  `sample_showfalse-copy.tat` rather than overwriting the original name, and confirmed the original
  file on disk was unaffected. Validate step 8 (host-embedded pane vs plain modal parity): owner
  confirmed add/edit/delete/double-click/Save/Save-As all behave the same in the host-embedded
  workspace pane as in the plain modal. All of 05's Validate checklist is now exercised except
  steps 5 (Save/Save-As disabled with zero rules) and 6 (Save round-trip), both explicitly skipped
  by owner's choice rather than failing.
- 2026-11-11: Owner chose to mark row 05 `done` now rather than first running the skipped step 6
  (Save round-trip), despite the doc's "Done when" section naming round-trip verification
  explicitly — accepted as a known gap, not silently glossed over. Row 05 moved from `coded` to
  `done`. Reorder (05's one explicitly out-of-scope behavior) remains open per the doc's own
  "Done when" carve-out and is not tracked as a gap.
- 2026-11-11: Implemented sub-task 01 (honor `case_sensitive`) in both matchers.
  `parseTatFilterRules` now reads the `case_sensitive` attribute into `rule.caseSensitive`
  (`static/chatbot/js/features/log-preview.js`). `applyTatRulesToLog`'s `matches()` compares the
  original-case `line`/`rule.text` when the flag is set, instead of always lowercasing both sides;
  the legacy regex compile (`new RegExp(rule.text, ...)`) drops the `'i'` flag when case-sensitive.
  Threaded `caseSensitive` through `startTatLogScan`'s worker payload. In the worker
  (`static/js/tat_log_preview_worker.js`), `compileRules` keeps `caseSensitive`, compiles
  `searchText` in original case when set (lowercased otherwise, as before), and compiles the regex
  matcher without `'i'` when case-sensitive; `processLine`'s `matches()` compares against the
  original-case `line` instead of `lowerLine` for case-sensitive rules. Two incidental fixes needed
  to avoid regressing sub-task 05's in-app authoring once this field exists: (1)
  `serializeTatFilterRules` had hardcoded `case_sensitive="n"` on every save — now writes
  `rule.caseSensitive`, so a loaded `case_sensitive="y"` filter survives a Save/Save-As round-trip
  instead of silently flipping to `"n"`; (2) `submitFilterEditor` rebuilt the rule object from
  scratch with no `caseSensitive` field at all — editing an existing case-sensitive rule via
  double-click would have silently dropped it to `false`, so it now carries the old rule's
  `caseSensitive` forward (there is still no in-app UI control to set it, per the doc's original
  scope). Built `fixtures/sample_case.log` (6 lines) and `fixtures/sample_case.tat` (3 filters:
  `SAE` case-sensitive, `sae` case-insensitive, `ERR\d+` case-sensitive regex) with hand-computed
  expected Hits counts (`SAE=1`, `sae=3`, `ERR\d+=1`) — using the Hits column (sub-task 09) rather
  than displayed line color, since overlapping-rule color precedence (sub-task 03) is a separate,
  still-open gap and would confound a purely visual check. `node --check` passed on both JS files.
  Row 01 marked `coded`, not `done` — manual in-browser validation (per 01's doc, using the new
  fixtures) still needs to be run.
- 2026-11-11: Implemented sub-task 03 (last-match-wins color precedence) in both matchers. Both
  `applyTatRulesToLog` (`log-preview.js`) and the worker's `processLine`
  (`tat_log_preview_worker.js`) replaced `includeRules.find(rule => matches(...))` (first match
  wins) with a forward loop over `includeRules` that keeps overwriting a `matchingInclude`/`include`
  variable on every later match, so the last enabled include rule (in file order) that matches a
  line determines its color — matching real TextAnalysisTool.NET's "later filter overrides earlier"
  behavior. Exclude-rule resolution (`excludeRules.some(...)`, checked before include resolution) is
  untouched — an exclude match still drops the line regardless of filter order, as before. Both
  implementations changed in the same minimal shape, per the doc's note that sub-task 04 will later
  delete one copy. Built `fixtures/sample_precedence.log` (3 lines) and `sample_precedence.tat` (two
  enabled include filters, `WARN` yellow listed first then `WARN_CRIT` red listed second, one line
  matching both, one matching only the first) for a clean visual check — line 2 should render red
  (the later filter), not yellow. `node --check` passed on both JS files. Row 03 marked `coded`, not
  `done` — manual in-browser validation (per 03's doc) still needs to be run.
