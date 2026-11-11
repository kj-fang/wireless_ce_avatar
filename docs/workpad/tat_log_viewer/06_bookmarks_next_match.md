# Sub-task 06: Bookmarks / next-match navigation

Status: open. Folder: [README.md](README.md). Progress: [PROGRESS.md](PROGRESS.md).
Depends on: 04 (build on the single shared matcher so "next match for filter X" has one source of
truth for which lines match which filter).

## Goal

Real TextAnalysisTool.NET supports jumping between hits of a single filter (F3-style
next/previous) and manually bookmarking arbitrary lines, then cycling through bookmarks. Today the
viewer has no navigation beyond manual scrolling.

## Where

- `static/chatbot/js/features/log-preview.js`:
  - `renderTatFilterOptions` — filter rows are the natural place to add a "jump to next/previous
    match of this filter" control per row.
  - `ensureVirtualLogView` / `requestVisibleLogRows` / `renderVirtualLogRows` — the virtualized
    scroller needs a "scroll to row N" primitive that next-match navigation and bookmark-jump can
    both call; check whether one already exists before adding a new one.
  - `updateKeyLogSelection` / `enableKeyLogSelection` — existing row-selection code is the closest
    precedent for "track a set of row indices and highlight them"; bookmarks are conceptually
    similar (a persisted set of row indices) and should reuse the same selection/highlight styling
    conventions rather than inventing new CSS.
- `static/js/tat_log_preview_worker.js` — for the virtualized path, matched-row indices for a given
  filter need to be queryable from the worker (it already knows per-line matches from `processLine`;
  expose a way to ask "give me the next matched row index after N for filter X").

## Reuse from design_review

None. Reuse within this feature: bookmark highlight styling should extend the existing
selection-highlight CSS/row-marking approach from `enableKeyLogSelection` rather than adding a
parallel one.

## Steps

1. Add a "next match" / "previous match" control per filter row that moves the virtual scroll
   position to the next/previous line matching that filter, wrapping at the ends.
2. For the virtualized path, extend the worker protocol with a message type that returns the
   nearest matching row index before/after a given index for a given filter id, backed by the same
   shared matcher from sub-task 04.
3. Add manual bookmarks: a keyboard shortcut or button to toggle a bookmark on the
   currently-focused/selected row, a visible marker in the gutter, and next/previous bookmark
   navigation independent of any filter.
4. Keep bookmark state in memory only (scoped to the open dialog/session) unless the user asks for
   persistence across reloads — don't add storage machinery speculatively.

## Validate

Manual, in-browser:

1. With a filter matching several scattered lines, click "next match" repeatedly and confirm the
   view scrolls to each match in order, wrapping after the last.
2. Confirm "previous match" does the same in reverse.
3. Toggle a bookmark on a few arbitrary lines, confirm a gutter marker appears, and confirm
   bookmark next/previous cycles through exactly those lines.
4. Repeat in both the legacy (in-memory) and virtualized (streamed) log paths.

## Done when

- Next/previous-match and bookmark navigation both work in both rendering paths, and
  `PROGRESS.md` row 06 is `done` with the manual-check summary in the commit message.
