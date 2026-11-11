# Sub-task 07: Copy selected lines to the clipboard

Status: done. Folder: [README.md](README.md). Progress: [PROGRESS.md](PROGRESS.md).
Depends on: none.

## Goal

Row selection already exists (click / shift-click / double-click), but the only action on a
selection today is "Add to question" (feeds the lines into the chat prompt). There is no way to
copy selected lines to the system clipboard for pasting elsewhere (an email, a bug report, a
separate editor).

This is not just a convenience gap: in the **virtualized** view, only the currently-rendered rows
(visible + overscan) exist in the DOM — everything else is a blank spacer. So native browser
select-and-copy silently only grabs whatever happens to be rendered, not the full selected range,
once a selection spans off-screen rows.

## Where

- `static/chatbot/js/features/log-preview.js`:
  - `enableKeyLogSelection` (~line 204) — owns `dialog.__selectionStart` / `__selectionEnd` and the
    existing "Add to question" button (`addSelection` closure, ~line 214). Add a sibling "Copy"
    button here that reuses the same selection state.
  - `openKeyLogModal` (~line 702-703) — `enableKeyLogSelection` is currently only called when
    `options.host` is set (the embedded "advance mode" side panel). The plain modal opened via
    "View filtered skill logs" has no selection UI at all today. Decide whether Copy (and
    selection generally) should also be wired up there, or stay host-only for now.
- `static/js/tat_log_preview_worker.js`:
  - The `type: 'selection'` handler (~line 20-35) already extracts exact row text for an index
    range, independent of what's currently rendered — this is the mechanism Copy must reuse for
    the virtualized path, not DOM text extraction.
  - That handler enforces a **200-line / 8,000-character cap** — this cap exists to bound what
    gets sent into a chat prompt (mirrors `services/chatbot/snippets.py`'s
    `MAX_SNIPPET_LINES`/`MAX_SNIPPET_CHARS`). A pure clipboard copy has no reason to share it.
  - The legacy (non-virtualized) path (`applyTatRulesToLog` / `renderTatFilteredSections`) renders
    real text nodes in a `<pre>`, so native browser select+copy already technically works there —
    this sub-task's new work is specifically for the virtualized path.

## Steps

1. Add a "Copy" button next to "Add to question" in `enableKeyLogSelection`, disabled under the
   same condition (no active selection).
2. On click, request the selected row range from the worker the same way `addSelection` does, but
   add a **separate, more generous cap** (e.g. a few thousand lines) only to avoid freezing the UI
   on an accidental whole-log selection — do not reuse the 200-line/8,000-char chat-prompt cap,
   since it's unrelated to clipboard copy.
3. Join the returned rows' `text` (already includes original line endings) and write it via
   `navigator.clipboard.writeText(...)`.
4. If the clipboard write fails (non-secure context, permission denied), show the error in the
   existing `.key-log-state` status line rather than failing silently.
5. Decide, and record the decision here once made, whether to also enable selection/Copy in the
   plain (non-host) modal, not just the embedded "advance mode" panel.

   **Decision:** stay host-only. `enableKeyLogSelection`/virtualization are only ever wired up when
   `openKeyLogModal` is called with `options.host` (from `openTatPreviewFromSidebar`, always called
   with a host per `workspace.js:39`). The plain modal (`attachKeyLogsToLastCard`'s "View filtered
   skill logs" button) always renders through the legacy `applyTatRulesToLog`/`renderTatFilteredSections`
   path into a real `<pre>` with real text nodes — native browser select+copy already works there
   today, so there is no virtualized-DOM gap to fix in that modal.

## Validate

Manual, in-browser (no automated test harness for this file):

1. In the virtualized view, start a selection, scroll so the end of the range is off-screen, then
   shift-click to extend the selection past what's rendered.
2. Click Copy; paste into a plain text editor and confirm every line in the range is present, in
   order — including the off-screen ones (proves it didn't just copy the rendered DOM).
3. Confirm "Add to question" still works unaffected by this change (same selection, separate
   button, separate cap).
4. Select 500+ lines (beyond "Add to question"'s 200-line cap) and confirm Copy still succeeds
   while "Add to question" still correctly rejects it with its existing size-limit message.

**Bug found and fixed (2026-11-10):** Ctrl+C copied stale/previous clipboard content instead of
the selected log lines, while the Copy button worked correctly. Same root cause as the Ctrl+H
History-shortcut bug (see sub-task 02): the Ctrl+C listener was attached to `dialog` (bubble
phase), but `enableKeyLogSelection` only ever runs in host-embedded mode (per the "stay host-only"
decision above), where the dialog is never opened via `showModal()` and so never traps focus. If
focus was elsewhere on the page when Ctrl+C was pressed, the keydown never reached the
dialog-scoped listener, `preventDefault()` never ran, and Chrome's native copy fired instead
(copying whatever, if anything, was already selected as plain text — explaining the stale
clipboard). Fixed the same way as Ctrl+H: moved the listener to `document` in the capture phase,
gated on that dialog's own visibility (`dialog.open` or `dialog.offsetParent !== null`). `node
--check` passed.

That alone wasn't enough: clicking a log row calls `event.preventDefault()` on `pointerdown` (so
the row div doesn't steal focus), so if keyboard focus was left in the chat input (or another text
field) from earlier, it stays there after selecting rows by click. The handler originally bailed
out on any `input`/`textarea`/`select`/`contenteditable` target to avoid hijacking normal text-field
copy, which meant Ctrl+C silently did nothing (confirmed: the owner still had the chat input
focused after selecting rows), and the browser's native copy ran instead, copying whatever was
previously on the clipboard. Refined the bail condition to only skip when that field actually has
a non-empty text selection of its own (`target.selectionStart !== target.selectionEnd` for
input/textarea, `!document.getSelection().isCollapsed` for contenteditable) — an idle cursor with no
selection no longer blocks the log-copy shortcut. Confirmed working by the owner after this change.
`node --check` passed.

## Done when

- Copy reliably captures the full selected range (including off-screen virtualized rows), clipboard
  failures surface as a visible error instead of failing silently, "Add to question" is unaffected,
  and `PROGRESS.md` row 07 is `done` with the manual-check summary in the commit message.

**Validated (2026-11-10):** off-screen shift-click selection renders correctly; Copy button
captures the full range in order via the worker's `'selection'` path (not DOM text); Ctrl+C works
after the two fixes above; a 500+ line selection is accepted by Copy (50,000-line cap) but rejected
by "Add to question" (200-line cap) with its existing size-limit message; "Add to question" still
succeeds normally on a small selection, unaffected by the Copy/Ctrl+C changes.
