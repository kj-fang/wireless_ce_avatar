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

# Skills YAML — unified location for skill definitions
SKILLS_CONFIG_DIR_prim = rf"\\infs089b.iil.intel.com\HOME\WirelessCE\Intel_WirelessCE_Avatar\log_parser_data\skills_config"
SKILLS_CONFIG_DIR_bkup = rf"\\infs089.iil.intel.com\HOME\WirelessCE\Intel_WirelessCE_Avatar\log_parser_data\skills_config"
SKILLS_YAML_FILENAME = "skills.yaml"

# Feedback sidecar — shared training-data layer.
# Each user writes under a per-user subfolder (see feedback_service) so
# concurrent writes from different machines never touch the same file
# (SMB has no reliable cross-machine fcntl). On unreachable share the
# service falls back to the local IntelAvatar_files\feedback directory.
FEEDBACK_DIR_prim = rf"\\infs089b.iil.intel.com\HOME\WirelessCE\Intel_WirelessCE_Avatar\feedback"
FEEDBACK_DIR_bkup = rf"\\infs089.iil.intel.com\HOME\WirelessCE\Intel_WirelessCE_Avatar\feedback"

# Local cache — prompt/ and filter/ are copied here from the remote on first run.
# Using a path relative to this file so it works regardless of install location.
from pathlib import Path as _Path
LOCAL_LOG_PARSER_DATA_DIR = str(_Path(__file__).parent.parent / "data" / "log_parser_data")
LOCAL_SKILLS_YAML = str(_Path(__file__).parent.parent / "data" / "skills.yaml")


