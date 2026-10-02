# Debug checklist data — how to update

`debug_checklist.json` drives the handsfree replyer's customer-facing
first-response checklist (issue domains, Required Log / Required Info /
Initial Triage items, the General Info questions, and sub-categories).
It is **generated** from the CE team's Excel checklist
(`740684_Intel_Windows_Wi-Fi_SW_Issue_Debugging_Checklist_RevX_Y.xlsx`)
by `services/handsfree/checklist_convert.py`. The Excel itself is NOT
committed — this JSON is the runtime source of truth.

## Update workflow

1. **Edit the Excel** (keep the layout conventions below).
2. **Regenerate** from the repo root:

   ```
   python -m services.handsfree.checklist_convert "<path to the xlsx>"
   ```

3. **Review** the diff: `git diff services/handsfree/checklist_data/`
4. **Validate**: `PYTHONUTF8=1 python -m services.handsfree.smoke`
   (S12.* checks assert structure invariants; if your edit legitimately
   changes counts — domains, general questions, per-tab items — update
   the corresponding check in `services/handsfree/smoke.py`).
5. **Restart the app** — the JSON is cached in memory per process.
6. **Commit the JSON diff and push.** Teammates receive checklist
   changes via git, not via the Excel.

## Excel layout conventions the converter expects

- Section headers in **column A**, spelled exactly:
  `Required Log`, `Required Info`, `Initial Triage`.
- **Flat tabs** (WowLAN, Yellow Bang, …): item text in column A,
  example hint in column C. Lines starting with `Note:` render as
  plain notes (no checkbox).
- **Sub-category tabs** (Connectivity, UEFI, P2P): the
  `Issue Type | Description | Check Items | … | Comments` table —
  a row with column A filled starts a sub-category (name = A,
  description = B, first item = C, example = E); rows with only
  column C continue that sub-category's items.
- **Main tab**: Step 1 rows = General Info questions (col B, example
  col D); Step 2 rows = domain name (col B) + description (col C).
  A new domain needs BOTH a Step-2 row and a tab with the same name.

## Gotchas

- **The converter carries policy, not just parsing.** Example:
  per CE decision (2026-09-30), `Air sniffer log` and `OS log` are
  stripped from every tab EXCEPT the Performance domain and P2P's
  Performance sub-category (`_apply_item_policy` /`_DROP_ITEM_RE` in
  `checklist_convert.py`). Re-adding such an item in the Excel will be
  silently stripped again — change the policy in the converter instead.
- **Renames ripple.** If a domain tab or sub-category is renamed,
  update the alias/keyword maps in `services/handsfree/checklist.py`
  (`_DOMAIN_ALIASES`, `resolve_subcategory`) or classification falls
  back to `Others`.
- Keep the xlsx filename's `RevX_Y` suffix — it is recorded as
  `revision` in the JSON for traceability.
