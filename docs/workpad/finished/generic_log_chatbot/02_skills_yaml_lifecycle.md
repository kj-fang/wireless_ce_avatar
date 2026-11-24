# Sub-task 02: Skills file and YAML lifecycle module

Status: open. Folder: [README.md](README.md). Progress: [PROGRESS.md](PROGRESS.md).
Depends on: none.

## Goal

The Generic profile needs its own skills file (so it never falls back to Wi-Fi's
WDI-marker-flavored skills, the exact failure mode Linux's own bundled baseline was built to
prevent — see `utils/linux_skills.yaml:1-14`) and its own dated-YAML lifecycle module (cloud/user
override resolution), independent of Linux's.

## Where

- `utils/generic_skills.yaml` (new) — copy of `utils/linux_skills.yaml` in full (one skill,
  `Whole_Log_Unfiltered`, same `keywords`/`expert_rules` — the "no filter, just the log" floor is
  equally valid as a starting point here). Reword the file's header comment (lines 1-36) so it no
  longer says "Linux skills" / "a dmesg or journalctl capture" / "Replace it with a real Linux
  skill set" — generalize to "Generic skills" / "a log of unknown format" / "a real domain skill
  set once the log's domain is known".
- `utils/generic_skills_yaml_utils.py` (new) — copy of `utils/linux_skills_yaml_utils.py` in
  full, with every `linux`/`Linux`/`LINUX` token replaced by `generic`/`Generic`/`GENERIC`:
  function names stay identical (`get_active_source`, `set_active_source`,
  `parse_dated_filename`, `find_latest_dated_yaml`, `today_dated_filename`,
  `resolve_cloud_skills_dir`, `find_latest_cloud_baseline_yaml`, `find_latest_user_yaml`,
  `find_latest_share_yaml`, `bundled_yaml_path`, `current_active_yaml`,
  `refresh_local_cloud_baseline`, `skills_yaml_status`) — this is the "same 12 names per module"
  contract `AgentRouteProfile.yaml_utils` depends on
  (`blueprints/chatbot/chatbot_routes.py:183-186`). Keep the same shared
  `local_cloud_baseline_dir()`/`local_user_overrides_dir()` import from
  `utils.skills_yaml_utils` (lines 34-40 of the Linux original) — only the filename prefix
  differs, the directories are shared across all domains.
- `configs/path_configs.py` (~line 56, immediately after the `LINUX_SKILLS_YAML_*` block) — add:
  ```python
  GENERIC_SKILLS_YAML_FILENAME = "generic_skills.yaml"
  GENERIC_SKILLS_YAML_DATED_GLOB = "generic_skills_*.yaml"
  GENERIC_SKILLS_YAML_DATED_RE = r"^generic_skills_(\d{4}-\d{2}-\d{2})\.yaml$"
  GENERIC_SKILLS_YAML_DATED_TEMPLATE = "generic_skills_{date}.yaml"
  ```

## Steps

1. Add the four `GENERIC_SKILLS_YAML_*` constants to `configs/path_configs.py`.
2. Write `utils/generic_skills_yaml_utils.py`, importing the new constants from step 1.
3. Write `utils/generic_skills.yaml` next to `generic_skills_yaml_utils.py` (same directory as
   the Linux/BT/Wi-Fi bundled YAMLs — `utils/`), since `bundled_yaml_path()` resolves it as
   `Path(__file__).resolve().parent / GENERIC_SKILLS_YAML_FILENAME`.

## Validate

No automated test covers the Linux skills-YAML lifecycle module either — validate manually,
mirroring what would be checked for Linux:

- `python -c "from utils.generic_skills_yaml_utils import bundled_yaml_path; p = bundled_yaml_path(); assert p.exists(), p; print(p)"`
  succeeds and prints a path ending in `utils/generic_skills.yaml`.
- `python -c "from services.chatbot.engine.system import load_skills_from_yaml; from utils.generic_skills_yaml_utils import bundled_yaml_path; skills = load_skills_from_yaml(str(bundled_yaml_path())); print(list(skills))"`
  prints `['Whole_Log_Unfiltered']` (or whatever the renamed skill is called) with no exception.
- `python -c "from utils.generic_skills_yaml_utils import current_active_yaml; print(current_active_yaml())"`
  returns a `(path, date, source)` tuple without raising (off-VPN it should fall through to the
  bundled file, same as Linux's does per `linux_skills_yaml_utils.py:200-209`).
- Confirm `utils/generic_skills_yaml_utils.py`'s prune loop (mirroring
  `linux_skills_yaml_utils.py:250-259`) only matches `generic_skills_*.yaml` filenames, never
  `linux_skills_*`/`bt_skills_*`/`skills_*`.

## Done when

The three checks above pass and the PROGRESS.md row for 02 is updated.
