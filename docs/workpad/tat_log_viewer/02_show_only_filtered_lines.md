# Sub-task 02: Honor `showOnlyFilteredLines`

Status: coded. Folder: [README.md](README.md). Progress: [PROGRESS.md](PROGRESS.md).
Depends on: none. Implemented together with a Ctrl+H runtime toggle (owner's choice: "Full engine:
both views, tied to sub-task 02") — see "Ctrl+H" below.

## Goal

The root `<TextAnalysisTool.NET showOnlyFilteredLines="...">` attribute controls whether
non-matching lines are hidden (`True`, today's only behavior) or kept visible with matches merely
highlighted in place (`False`). Right now both matchers always drop non-matching lines regardless
of this attribute.

## Where

- `static/chatbot/js/features/log-preview.js`:
  - `parseTatFilterRules` (~line 290) — currently returns only the `rules` array parsed from
    `<filter>` elements; it needs to also read `showOnlyFilteredLines` off the root
    `<TextAnalysisTool.NET>` element and return it alongside the rules (e.g.
    `{ rules, showOnlyFilteredLines }`), and every call site needs updating for the new shape.
  - `applyTatRulesToLog` (~line 365) — when `showOnlyFilteredLines` is `false`, keep every line in
    the output and only attach color/highlight spans to the ones that match an include rule,
    instead of filtering the `lines` array down to matches only.
  - `renderTatFilteredSections` — must render a kept-but-unmatched line as plain (no color), not
    omit it.
- `static/js/tat_log_preview_worker.js`:
  - The `start` message handler and `processLine` (~line 80) — same branch: when
    `showOnlyFilteredLines` is `false`, every line is sent to the UI (either as a plain row or a
    matched/colored row), not just matched ones.
  - This changes row counts for the virtualized scrollbar/total — `ensureVirtualLogView` /
    `startTatLogScan` in `log-preview.js` read a total-row-count from the worker; verify that count
    still drives the virtual scroller correctly when all lines are kept.

## Reuse from design_review

None.

## Steps

1. Extend `parseTatFilterRules`'s return value to include `showOnlyFilteredLines` (boolean, parsed
   from the root attribute, default `true` to match today's behavior when absent).
2. Update every caller of `parseTatFilterRules` in `log-preview.js` to use the new shape.
3. In `applyTatRulesToLog`, branch: if `showOnlyFilteredLines` is `true`, keep current
   filter-then-render behavior; if `false`, iterate every line, compute its matched rule (or none),
   and always include it in the output with the matched color (or no color).
4. Thread `showOnlyFilteredLines` into the worker's `postMessage` payload in `startTatLogScan`, and
   apply the same keep-all-lines branch in `processLine`.
5. Double-check the exclude (`excluding="y"`) rules still remove a line even when
   `showOnlyFilteredLines` is `false` — exclude rules in TextAnalysisTool.NET always drop a line,
   independent of this attribute; only the "no filter matched" case changes behavior.

## Validate

Manual, in-browser:

1. Synthetic `.tat` with `showOnlyFilteredLines="False"` and one include filter matching ~20% of
   lines in a sample log.
2. Load it and confirm all lines are visible, with only the matching ~20% colored/highlighted.
3. Flip to `showOnlyFilteredLines="True"` (or use a second fixture) and confirm the view reverts to
   showing only the matched lines (today's behavior, unchanged).
4. Confirm an `excluding="y"` filter still removes lines in both modes.
5. Confirm this works in both the legacy (small log, in-memory) and virtualized (large/streamed
   log) paths.

## Ctrl+H

The owner asked for a Ctrl+H shortcut to toggle hide/unhide of unfiltered content, and chose to
build it on the real engine above rather than a fake dim/blur overlay. It flips
`dialog.__showOnlyFilteredLines` and re-renders (`startTatLogScan` for the virtualized path,
`renderKeyLogContent` for legacy). It no-ops when no `.tat` is loaded (nothing to toggle) or when
focus is in an input/textarea/select/contenteditable. This is a runtime override on top of the
file's own `showOnlyFilteredLines` attribute — reloading a `.tat` resets it back to that file's
value.

**Bug found and fixed (2026-11-09):** the first version attached the Ctrl+H handler to the
`dialog` element itself (bubble phase), which only works when focus is somewhere inside the
dialog's subtree. In host-embedded mode (the split workspace pane — `workspace.js`'s
`workspace-raw-viewer`/`workspace-evidence-viewer`), the dialog is never opened with
`showModal()` (see `openKeyLogModal`'s `if (!options.host && !dialog.open) dialog.showModal();`)
— it's shown purely via a CSS override (`workspace.css`: `.workspace-view-host > .key-log-modal {
display: flex; ... }`), so it never traps focus. If the user's focus was anywhere else on the page
(e.g. the chat input) when they pressed Ctrl+H, the keydown never reached the dialog-scoped
listener, so it fell through to Chrome's own History shortcut — reported by the owner as "when I
do CTRL+H, it seems to hit shortcut for history in Chrome." Fixed by moving the Ctrl+H handler to
a `document`-level listener in the capture phase, added once per dialog instance and scoped by
checking that specific dialog's visibility (`dialog.open` for true modal mode, or
`dialog.offsetParent !== null` for host-embedded mode) before acting — so it fires regardless of
where focus currently is on the page, as long as that log viewer is the one actually visible.
`node --check` passed.

**Reveal from selection (2026-11-10):** the owner asked that pressing Ctrl+H while a line (or
range) is selected should reveal the toggled view starting from that line, instead of resetting to
the top of the log and losing context. Since row-level selection (`dataset.rowIndex`,
`dialog.__selectionStart`/`__selectionEnd`) only exists in the virtualized/host-embedded path (the
legacy/plain modal renders real text nodes with no per-row selection — see sub-task 07's "Copy
stays host-only" decision), this only applies there. Implementation: before toggling and
re-scanning, the Ctrl+H handler reads the selected row's stable `dataset.lineNumber` (physical line
number, via the lower of `__selectionStart`/`__selectionEnd`) into `dialog.__pendingRevealLine`,
since `startTatLogScan` immediately nulls out `__selectionStart`/`__selectionEnd` as part of its
reset. The worker's `'ready'` handler skips its usual `scrollTop = 0` reset when a reveal is
pending, and instead sends a new `{type: 'locate', lineNumber}` message; `tat_log_preview_worker.js`
binary-searches its `matchedRows` (always ordered by ascending `lineNumber`) for the first row at or
after that line and replies with its new index; `log-preview.js` then sets `content.scrollTop` from
that index (with a small `TAT_VIRTUAL_REVEAL_MARGIN` of 3 rows so the target line isn't pinned to
the very top edge) and re-requests the visible rows. `node --check` passed on both files.

## Implementation notes (2026-11-09)

- `parseTatFilterRules` now returns `{rules, showOnlyFilteredLines}`, reading the root
  `<TextAnalysisTool.NET showOnlyFilteredLines="True"|"False">` attribute case-insensitively,
  defaulting to `true` when absent (confirmed against a real sample file that filter-level booleans
  are lowercase `y`/`n` but this root attribute is capitalized `True`/`False`).
- `applyTatRulesToLog` takes a `showOnlyFilteredLines` parameter (default `true`): exclude rules
  still always drop a line; when `false`, a line with no matching include rule is still kept, pushed
  with empty `foreground`/`background` so `renderTatFilteredSections` renders it as a plain,
  uncolored span (no code change needed there — it already skips the style when colors are empty).
- The worker's `processLine` mirrors the same branch, keyed off a `showOnlyFilteredLines` flag read
  from the `start` message (`message.showOnlyFilteredLines !== false`); `matchedRows.length` (and
  therefore the virtualized spacer height) naturally grows to include all kept lines.
- `dialog.__showOnlyFilteredLines` defaults to `true` at dialog creation and is **not** reset when
  the modal reopens with new log content (same pattern as `dialog.__tatRules` persisting across
  reopens) — only loading a new `.tat` file resets it to that file's attribute.
- `node --check` passed on both `log-preview.js` and `tat_log_preview_worker.js`.

## Done when

- Both modes render correctly in both matchers, exclude rules still work in both, Ctrl+H toggles
  correctly in both host and plain modals, Ctrl+H with a line selected reveals the toggled view
  anchored at that line (virtualized/host path), and `PROGRESS.md` row 02 is `done` with the
  manual-check summary (including a Ctrl+H check) in the commit message.
