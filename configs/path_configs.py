# -----shared folder path-----

# bsod
#LOAD_PATH_prim = rf"\\pgsfls0101.gar.corp.intel.com\symstore\CMAttachments\JIRA"
LOAD_PATH_bkup = rf"\\elitpts46.ger.corp.intel.com\BSOD_Dumps"
LOAD_PATH_prim = rf"\\elitpts46.ger.corp.intel.com\BSOD_Dumps"


# key
KEY_PATH_prim = rf"\\pgsfls0101.gar.corp.intel.com\symstore\CMAttachments\JIRA\WIFI\Temp\KJ\Intel_WirelessCE_Avatar\key\keys.py"
KEY_PATH_bkup = rf"\\infs089.iil.intel.com\HOME\WirelessCE\Intel_WirelessCE_Avatar\key\keys.py"

# llm 
CLASSIFY_PATH = rf"\\infs089.iil.intel.com\HOME\WirelessCE\Intel_WirelessCE_Avatar\log_parser_data\classification.py"
LOG_PARSER_DIR = rf"\\infs089.iil.intel.com\HOME\WirelessCE\Intel_WirelessCE_Avatar\log_parser_data"

# log_parser_data (skills/prompts/filters for chatbot)
LOG_PARSER_DATA_DIR_prim = rf"\\infs089b.iil.intel.com\HOME\WirelessCE\Intel_WirelessCE_Avatar\log_parser_data"
LOG_PARSER_DATA_DIR_bkup = rf"\\infs089.iil.intel.com\HOME\WirelessCE\Intel_WirelessCE_Avatar\log_parser_data"

# Skills YAML — unified location for skill definitions.
#
# Files are written with an ISO date suffix so multiple revisions can coexist
# in the shared folder (e.g. skills_2026-05-06.yaml). The application always
# picks the most recent dated file on both the cloud and local side; the
# legacy un-dated filename ("skills.yaml") is still accepted for backward
# compatibility so existing deployments keep working until they are migrated.
SKILLS_CONFIG_DIR_prim = rf"\\infs089b.iil.intel.com\HOME\WirelessCE\Intel_WirelessCE_Avatar\log_parser_data\skills_config"
SKILLS_CONFIG_DIR_bkup = rf"\\infs089.iil.intel.com\HOME\WirelessCE\Intel_WirelessCE_Avatar\log_parser_data\skills_config"
SKILLS_YAML_FILENAME = "skills.yaml"                       # legacy un-dated file
SKILLS_YAML_DATED_GLOB = "skills_*.yaml"                   # e.g. skills_2026-05-06.yaml
SKILLS_YAML_DATED_RE = r"^skills_(\d{4}-\d{2}-\d{2})\.yaml$"
SKILLS_YAML_DATED_TEMPLATE = "skills_{date}.yaml"          # date = YYYY-MM-DD

# Bluetooth skills YAML — lives next to skills.yaml in the same shared
# skills_config dir. Loaded at startup by set_up_app to populate the
# bt_chatbot_agent. Falls back to the WiFi skills (skills.yaml) when this
# file is missing.
#
# Naming convention mirrors WiFi (skills_YYYY-MM-DD.yaml) but with a
# `bt_` prefix so both domains can coexist in the same skills_config/
# (cloud, user) sub-folders without name collisions.
BT_SKILLS_YAML_FILENAME = "bt_skills.yaml"                    # legacy un-dated file
BT_SKILLS_YAML_DATED_GLOB = "bt_skills_*.yaml"                # e.g. bt_skills_2026-06-02.yaml
BT_SKILLS_YAML_DATED_RE = r"^bt_skills_(\d{4}-\d{2}-\d{2})\.yaml$"
BT_SKILLS_YAML_DATED_TEMPLATE = "bt_skills_{date}.yaml"       # date = YYYY-MM-DD

# Feedback sidecar — shared training-data layer.
# Each user writes under a per-user subfolder (see feedback_service) so
# concurrent writes from different machines never touch the same file
# (SMB has no reliable cross-machine fcntl). On unreachable share the
# service falls back to the local IntelAvatar_files\feedback directory.
FEEDBACK_DIR_prim = rf"\\infs089b.iil.intel.com\HOME\WirelessCE\Intel_WirelessCE_Avatar\feedback"
FEEDBACK_DIR_bkup = rf"\\infs089.iil.intel.com\HOME\WirelessCE\Intel_WirelessCE_Avatar\feedback"

# Gather — usage-analytics layer.
# On the first Send of each chatbot session a tidy, DB-ingestion-friendly
# record is written here (one JSON per conversation) capturing who used the
# tool, when, which CASE NUMBER they worked on and a short summary of the case
# context + the questions they asked. Lets us tally distinct users, the cases
# they touched and what those cases were about. Same primary/backup/local
# fallback pattern as the feedback sidecar (see gather_service).
#
# Path is intentionally NOT hard-coded as a single literal string: it is
# derived from the feedback share (same WirelessCE host) by swapping the leaf
# folder, so the full SMB UNC never appears verbatim in the repo / logs / a
# grep / a stack trace. An optional environment variable lets an operator
# override the share without committing the override to the repo.
import os as _os


def _sibling_share(base: str, leaf: str) -> str:
    """Return ``<parent-of-base>\\<leaf>`` — used so the Gather UNC is derived
    from the feedback UNC at import time instead of being written out in
    full anywhere in the source."""
    return rf"{base.rsplit(chr(92), 1)[0]}\{leaf}"


_GATHER_LEAF = "Gather"
GATHER_DIR_prim = _os.environ.get("INTELAVATAR_GATHER_DIR") \
    or _sibling_share(FEEDBACK_DIR_prim, _GATHER_LEAF)
GATHER_DIR_bkup = _os.environ.get("INTELAVATAR_GATHER_DIR_BKUP") \
    or _sibling_share(FEEDBACK_DIR_bkup, _GATHER_LEAF)

# ACE playbook cloud sync (WiFi/general).
#
# Default users only READ the playbooks, so this share is treated as a simple
# cache source: on every boot each machine pulls the latest playbook from here
# into <avatarfiles_dir>/ace_playbooks/local/ (falling back to the last local
# version when the share is unreachable). The rare machine that runs ACE
# reflection best-effort pushes its updated copy back here, archiving the
# previous version under ace_playbook/history/. See services/ace/sync_utils.py.
_ACE_PLAYBOOK_LEAF = "ace_playbook"
ACE_PLAYBOOK_DIR_prim = _os.environ.get("INTELAVATAR_ACE_PLAYBOOK_DIR") \
    or _sibling_share(FEEDBACK_DIR_prim, _ACE_PLAYBOOK_LEAF)
ACE_PLAYBOOK_DIR_bkup = _os.environ.get("INTELAVATAR_ACE_PLAYBOOK_DIR_BKUP") \
    or _sibling_share(FEEDBACK_DIR_bkup, _ACE_PLAYBOOK_LEAF)

# ACE playbook cloud sync (BT).
#
# Kept as a SEPARATE share leaf (not just a filename prefix like the BT
# skills YAML) because playbooks accumulate bullets from live reflection —
# mixing BT's and WiFi's log-analysis styles into one workflow.json/
# domain_*.json set would let each domain's reflected bullets pollute the
# other's playbook. Same pull/push cache semantics as the WiFi share above.
_ACE_PLAYBOOK_BT_LEAF = "ace_playbook_bt"
ACE_PLAYBOOK_BT_DIR_prim = _os.environ.get("INTELAVATAR_ACE_PLAYBOOK_BT_DIR") \
    or _sibling_share(FEEDBACK_DIR_prim, _ACE_PLAYBOOK_BT_LEAF)
ACE_PLAYBOOK_BT_DIR_bkup = _os.environ.get("INTELAVATAR_ACE_PLAYBOOK_BT_DIR_BKUP") \
    or _sibling_share(FEEDBACK_DIR_bkup, _ACE_PLAYBOOK_BT_LEAF)

# Local cache — prompt/ and filter/ are copied here from the remote on first run.
# Using a path relative to this file so it works regardless of install location.
from pathlib import Path as _Path
LOCAL_LOG_PARSER_DATA_DIR = str(_Path(__file__).parent.parent / "data" / "log_parser_data")

# Fixed "Downloads" base override.
#
# When set to a non-empty path, helpers.init_download_dir() uses this instead
# of looking up the per-user "Downloads" shell folder from the registry. Use
# this on a server / service account where the HKCU Downloads folder is
# unreliable or points at the wrong profile, so <base>\IntelAvatar_files\...
# (and therefore ace_playbooks) always lands in a predictable location.
# Leave as "" to keep the original per-user registry behaviour.
DOWNLOADS_DIR = r"C:\Users\admin\Downloads"

# ACE trainer mode.
#
# True on a dedicated central ACE-training server: this machine is the SOLE
# writer of the playbook shares, so it must NOT pull playbooks from the share
# at boot (a pull could overwrite its locally-trained, canonical playbooks
# with a staler copy). Pushing after an adapt/nightly run is unaffected.
# Leave False on ordinary client installs, which pull the latest playbook at
# every boot and never train. See services/ace/sync_utils.py:sync_at_boot.
ACE_TRAINER_MODE = True

# Local skill YAML cache.
#
# Primary location: <IntelAvatar_files>/skills_config/  — same root the rest of
# the app uses for case-number downloads, so users can find / edit / replace
# their skill configs in one familiar place.
# That path is only known after helpers.init_download_dir() runs, so
# skills_yaml_utils.local_skills_dir() resolves it at runtime via
# app_config.avatarfiles_dir. This module-level constant is the fallback used
# when avatarfiles_dir is not yet set (e.g. during import-time helpers).
LOCAL_SKILLS_DIR_NAME = "skills_config"
LOCAL_SKILLS_YAML = str(_Path(__file__).parent.parent / "data" / "skills.yaml")


