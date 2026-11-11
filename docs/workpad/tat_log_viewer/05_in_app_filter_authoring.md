# Sub-task 05: In-app filter authoring

Status: coded (add/edit/delete/Save/Save As only — reorder deferred). Folder: [README.md](README.md).
Progress: [PROGRESS.md](PROGRESS.md).
Originally depends on: 04 (build on the single shared matcher so authoring UI only needs to
produce/consume one rule shape) — owner chose to jump this queue (2026-11-11), same precedent as
07/08/09. Add/edit/delete/Save/Save-As were implemented directly against today's two separate
matchers (`applyTatRulesToLog` and the worker's `processLine`), both of which already accept the
same plain rule shape with no transformation, so 04's eventual dedupe shouldn't need to touch this
authoring code. Reorder is explicitly out of scope for now (owner only asked for
add/delete/edit/save); it still depends on 03 (last-match-wins color precedence) to have any
visible effect, so it's left for whenever 03/04 land.

## Goal

Today a `.tat` filter set can only be loaded from disk via `renderTatFilterOptions`'s file input;
there's no way to add, edit, reorder, delete, or save a filter from inside the chat UI. Real
TextAnalysisTool.NET lets a user build and persist a filter set interactively.

## Where

- `static/chatbot/js/features/log-preview.js`:
  - `renderTatFilterOptions(dialog)` — currently renders a read-only checklist (enable/disable
    toggle, letter badge, EXCL/REGEX badges) per parsed rule; needs add/edit/delete/reorder
    controls.
  - `openKeyLogModal` (~line 606) — owns the dialog chrome; new authoring controls (an "Add filter"
    button, a small edit form) need a place in this layout, including in the `options.host`
    (embedded side-panel) mode.
  - Needs a way to serialize the in-memory rule list back to `.tat` XML for "save" (there is no
    existing serializer — `parseTatFilterRules` only reads; writing is new).

## Reuse from design_review

None — no existing save/export path for this file format anywhere in the repo.

## Steps

1. ~~Add an "Add filter" affordance...~~ Done: an "Add filter" header button opens a shared
   editor bar (`.key-log-filter-editor`) with fields for text, enabled, excluding, regex, and
   optional text/background color (each gated by its own checkbox so "no color" stays
   representable). `case_sensitive` and `description` are intentionally left out of the UI — see
   "Known limitations" below.
2. ~~Add inline edit and delete...~~ Done: each filter row has a trash (delete — splices the rule
   and re-renders) icon button in a 4th "actions" grid column, which calls `event.preventDefault()`
   to stop the row's `<label>` from forwarding a click into toggling the enable checkbox. Edit is a
   double-click anywhere on the row (reopens the same editor bar pre-filled) rather than a second
   icon button — an initial pencil-icon version was tried first but the actions column was too
   narrow for two icon buttons and the overflow visually covered the adjacent Hits number (see
   `PROGRESS.md`'s 2026-11-11 entries); double-click avoids a second icon altogether. The row's
   `<label>` also has a plain `click` listener (not just `dblclick`) that calls
   `event.preventDefault()` for clicks outside the checkbox/actions areas, so neither click of a
   double-click forwards an activation to the enable checkbox — without it, each `click` in a
   double-click sequence independently toggles the checkbox before the `dblclick` handler ever
   runs. A direct click on the checkbox itself (or the letter badge) still toggles normally, since
   that target is excluded from the guard.
3. Reorder (drag handle or up/down buttons) — **deferred**, not implemented. Filter order only
   matters once sub-task 03's last-match-wins color precedence exists; today's first-match-wins
   `applyTatRulesToLog` makes order a no-op anyway.
4. ~~Re-run the matcher...~~ Done: every add/edit/delete calls `renderTatFilterOptions(dialog)` plus
   either `startTatLogScan(dialog)` (virtualized/worker path) or `renderKeyLogContent(dialog)`
   (legacy path) — the same live-refresh pattern the existing enable/disable checkbox already used.
   Regex rules authored/edited in-app have `rule.matcher` set explicitly at submit time, since the
   legacy matcher relies on a pre-compiled `.matcher` (the worker always recompiles its own).
5. ~~Add a "Save .tat" action...~~ Done: `serializeTatFilterRules` builds the XML string (mirroring
   `parseTatFilterRules`'s field names), and `downloadTatFilter` reuses the `showSaveFilePicker`
   (with `Blob`+anchor-click fallback) pattern already established in `prompt-editor.js`. "Save"
   reuses the currently-tracked name (`dialog.__tatName`); "Save As" suggests `<name>-copy.tat`
   without touching the tracked name used for subsequent "Save" presses until the Save-As dialog
   itself completes.

## Known limitations (accepted, not fixed by this change)

- `case_sensitive` is always written as `"n"` and has no UI control, since both matchers always
  match case-insensitively today regardless of this attribute (depends on sub-task 01).
- `description` is always written as `""`; no UI for it.
- A rule's color loaded from an ARGB-with-alpha `.tat` value can't round-trip through the new
  `<input type="color">` editor (6-digit hex only) — editing that rule's color drops alpha.
- Loading a file that already had `description`/`type`/`case_sensitive` set to something
  non-default, then re-saving without touching those fields, will reset them to the defaults above
  (this is `parseTatFilterRules` already dropping those attributes on read, not new).

## Validate

Manual, in-browser (fixtures in `docs/workpad/tat_log_viewer/fixtures/`):

1. Add a new filter via the UI, confirm it highlights matching lines immediately in both the
   virtualized/Preview path and the legacy "View filtered skill logs" path.
2. Add a regex filter, confirm it matches correctly in **both** paths (the `rule.matcher` gotcha
   above — a missing matcher would silently fall back to literal-substring matching in the legacy
   path only).
3. Edit an existing filter's text/color, confirm the view updates live in both paths.
4. Delete a filter, confirm its highlighting and Hits row disappear.
5. Confirm Save/Save As are disabled once all rules are deleted.
6. Save, re-load the downloaded `.tat` file, and confirm the rule set round-trips identically
   (reuse `sample_showfalse.tat`'s 4 filters as a known-count oracle).
7. Save As suggests a `-copy.tat` name and leaves the originally loaded file's name/content
   untouched on disk.
8. Repeat in both the host-embedded workspace pane and the plain modal.
9. Reorder — **not applicable**, deferred (see Steps above).

## Done when

- Add/edit/delete/Save/Save-As all work against a synthetic `.tat`, round-trip is verified, and
  `PROGRESS.md` row 05 is `done` with the manual-check summary in the commit message. Reorder
  remains open and is not required for this row to be marked done.
