# Sub-task 08: Load a default `.tat` from a local folder

Status: open. Folder: [README.md](README.md). Progress: [PROGRESS.md](PROGRESS.md).
Depends on: none.

## Goal

Today a `.tat` filter must be picked via "Load .tat" (a file-picker dialog) every time the viewer
opens — there is no way to have a default filter set load automatically. The owner wants a default
`.tat` to load from a specific folder on the local machine running the app, so a user who always
analyzes with the same filter set doesn't have to re-browse for it every session.

**This sub-task needs backend help** — unlike every other sub-task in this workpad, which is
front-end only (see README's Rules). A browser can't read an arbitrary local filesystem path
without a user gesture (file picker or drag-drop); only the Flask server, which already runs on
the same machine, can read a fixed local path and hand its contents to the page.

## Agreed location

`<Downloads>\IntelAvatar_files\logs\` — i.e. the existing `logs` folder this app already creates
on every machine (`utils/helpers.py`'s `init_download_dir()`, line ~92), also resolvable via
`utils/port_utils.py`'s `get_logs_dir()`. That folder currently only holds the app's own runtime
log (`avatar.log`, written by `configs/logger_setup.py`) — this sub-task adds a `.tat` file to the
same folder, per the owner's explicit choice, rather than creating a new dedicated folder.

**File name is not fixed.** The owner wants to drop their own `.tat` file in with its original
name intact rather than renaming it to a fixed `default.tat`, so the server picks up *whichever
single* `*.tat` file is present in that folder, under any name, and reports that real name back to
the UI. If the folder holds zero or two-or-more `.tat` files, the result is ambiguous/missing and
auto-load silently no-ops, same as "no default present" — the server does not guess which one to
use.

## Where

- `utils/port_utils.py` — `get_logs_dir()` (line 32) is the existing, already-used accessor for
  this path; reuse it rather than reconstructing the path elsewhere.
- `blueprints/chatbot/chatbot_routes.py` — add a new handler alongside `preview_log` /
  `preview_log_chunk` (~line 1167), e.g. `default_tat_filter(self)`, that lists `*.tat` files (case
  -insensitive extension match) in `get_logs_dir()`. Exactly one: read and return its raw content
  (XML) with the real file name in an `X-Tat-Name` header (URL-encoded, since header values must be
  ASCII). Zero or two-or-more: a 404 JSON `{"success": false}`, same as a missing file — do not
  guess which one is "the" default. Follow `preview_log`'s existing style: check `os.path.isfile`
  before opening, return a typed error on failure, no exceptions escaping.
- `services/chatbot/factory.py` — `ROUTE_SPECS` (~line 35-37): add
  `RouteSpec("/default_tat_filter", "default_tat_filter", ("GET",), "log_preview")`, gated behind
  the same `log_preview` capability flag `preview_log`/`explain_snippet` already use.
- `static/chatbot/js/features/log-preview.js`:
  - `openKeyLogModal` (~line 606-712) — when the dialog is (re)opened and `dialog.__tatRules` is
    not already set (nothing loaded yet this session), fetch `${LOG_PREVIEW_API}/default_tat_filter`
    once; on success, read the real file name back from the `X-Tat-Name` response header
    (`decodeURIComponent`), then run the exact same steps the file-input `change` handler already
    runs (`parseTatFilterRules`, set `dialog.__tatRules`/`dialog.__tatName` to that real name,
    update `filterName`, `renderTatFilterOptions(dialog)`, then
    `startTatLogScan`/`renderKeyLogContent`). On a 404 (missing, or zero/ambiguous `.tat` files) or
    any fetch error, do nothing and leave today's "No .tat file loaded" state — this is a
    convenience default, not a required file, so a missing default must stay silent, not show an
    error.
  - The existing "Load .tat" button and file input must keep working unchanged, so a user can
    still override the default at any time within the same session.

## Steps

1. Add `default_tat_filter` to `chatbot_routes.py`, reusing `get_logs_dir()` and mirroring
   `preview_log`'s error-handling shape.
2. Register it in `factory.py`'s `ROUTE_SPECS` behind the `log_preview` capability.
3. In `log-preview.js`, add the auto-load fetch at dialog-open time, gated on no `.tat` already
   being loaded in that dialog instance.
4. Reuse the same parse/populate path the manual file loader uses — do not duplicate
   `parseTatFilterRules` call logic.
5. Confirm a missing (or ambiguous: zero or 2+ `.tat` files in the folder) default is silent (no
   error banner, no console-visible failure to the user) and a malformed single `.tat` surfaces the
   same "Could not load the .tat filter" message the manual loader already shows for bad XML.

## Validate

Manual, in-browser (no automated test harness for this file):

1. With no `.tat` file present in `<Downloads>\IntelAvatar_files\logs\`: open the log viewer,
   confirm it behaves exactly as today (no filter loaded, no error shown).
2. Drop one valid synthetic `.tat` into that folder, under its own original name (not renamed to
   `default.tat`), reopen the log viewer (fresh dialog / new session), confirm its filters are
   loaded and applied automatically without clicking "Load .tat", and that the filter-name label
   shows that real file name.
3. With the default loaded, click "Load .tat" and pick a different file; confirm it overrides the
   default for the rest of that session.
4. Put an invalid/corrupt XML file as the sole `.tat` in that folder and confirm the existing
   "Could not load the .tat filter" error path is what shows, not a silent failure or a crash.
5. Add a second `.tat` file to the folder (now two present) and confirm auto-load silently no-ops
   (ambiguous — same as missing), then remove one so exactly one remains and confirm it loads
   again.
6. Confirm this works both for the plain modal ("View filtered skill logs") and the embedded
   "advance mode" panel, since `openKeyLogModal` backs both.

## Done when

- A present, unambiguous (exactly one) `.tat` file loads automatically under its own name; auto-
  load stays silent when the folder has zero or 2+ `.tat` files; manual override via "Load .tat"
  still works; invalid XML still surfaces the existing error message; and `PROGRESS.md` row 08 is
  `done` with the manual-check summary (including which path was tested)
  in the commit message.
