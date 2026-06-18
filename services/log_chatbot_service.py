"""
Log Chatbot Service
===================
Multi-skill Wi-Fi log analysis agent.

Skills are loaded dynamically from the shared folder:
  <data_dir>/prompt/<prompt_file.py>   – contains SYS_PROMPT string
  <data_dir>/filter/<filter_file.tat>  – TAT XML filter file with keywords

Each classification category defined in classification.py is mapped to its
corresponding prompt and filter files via SKILL_FILE_MAP.
"""

import re
import json
import hashlib
import shutil
import threading
from bisect import bisect_left, bisect_right
import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from utils.softAP_supported_channel import softAP_supported_channel
from pydantic import BaseModel, Field

from utils import helpers
from utils.assert_code_utils import lookup_assert_code
from utils.issue_time_utils import resolve_issue_time, parse_issue_time_string, format_issue_time
from utils.log_parser_preprocess import (
    extract_enabled_keywords_from_filter_file,
    filter_log_by_keywords,
    preprocess_log_for_llm,
    group_similar_logs,
)


# ---------------------------------------------------------
# 0. Local cache sync
# ---------------------------------------------------------
def sync_to_local(remote_dir: str, local_dir: str) -> bool:
    """
    Copy the `prompt/` and `filter/` sub-folders from `remote_dir` to
    `local_dir`.  Only files that are missing or older than the remote
    version are updated (mtime-based).  Returns True on success.
    """
    remote = Path(remote_dir)
    local  = Path(local_dir)

    if not remote.exists():
        print(f"  ⚠️  Remote dir not reachable, cannot sync: {remote_dir}")
        return False

    updated = 0
    errors  = 0
    for sub in ("prompt", "filter"):
        src_dir = remote / sub
        dst_dir = local  / sub
        if not src_dir.exists():
            continue
        dst_dir.mkdir(parents=True, exist_ok=True)
        for src_file in src_dir.iterdir():
            if not src_file.is_file():
                continue
            dst_file = dst_dir / src_file.name
            try:
                if (not dst_file.exists() or
                        src_file.stat().st_mtime > dst_file.stat().st_mtime):
                    shutil.copy2(str(src_file), str(dst_file))
                    updated += 1
            except Exception as e:
                print(f"    ⚠️  Could not copy {src_file.name}: {e}")
                errors += 1

    print(f"  🗂️  Local cache sync: {updated} file(s) updated, {errors} error(s) → {local_dir}")
    return errors == 0


# ---------------------------------------------------------
# 1. Mapping: classification category → (prompt_file, filter_file)
#    None = no dedicated file; fallback keywords/prompt are used.
# ---------------------------------------------------------
SKILL_FILE_MAP: Dict[str, Tuple[Optional[str], Optional[str]]] = {
    "Yellow_Bang":         ("yellow_bang.py",        "BT_YB_LOST.tat"),
    "Connectivity":        ("connectivity.py",        "connectivity.tat"),
    "Roaming":             ("prompt_roaming.py",      "connectivity.tat"),
    "DSM":                 ("Driver_DSM.py",          "Driver_DSM.tat"),
    "VLP/UHB/AFC":         ("BIOS_tool.py",           "BIOS_tool.tat"),
    "Sensing":             ("sensing.py",             "sensing.tat"),
    "P2P":                 (None,                     "P2P.tat"),
    "WRDS/WGDS/EWRD/SGOM": (None,                     "WRDS_EWRD_WGDS_SGOM.tat"),
    "BSOD":                ("BT_YB_Lost.py",          "BT_YB_LOST.tat"),
    "Assert":              ("connectivity.py",         "connectivity.tat"),
    "PPAG":                ("Driver_DSM.py",          "Driver_DSM.tat"),
    "TAS":                 (None,                     None),
    "UATS":                (None,                     None),
    "Unclassified":        (None,                     None),
}

SKILL_DESCRIPTIONS: Dict[str, str] = {
    "Yellow_Bang":         "Diagnose Wi-Fi Yellow Bang / device lost / device drop issues",
    "Connectivity":        "Analyse Wi-Fi connection flow, disconnections, and FW crashes",
    "Roaming":             "Identify roaming events, triggers, and reconnection flows",
    "DSM":                 "Inspect DSM/BIOS UHB allow bitmap and PPAG/WRDS configuration",
    "VLP/UHB/AFC":         "Check UEFI/BIOS DSM table for VLP, UHB, and AFC function settings",
    "Sensing":             "Diagnose Wi-Fi Sensing (WAL/WOA) UEFI/registry configuration",
    "P2P":                 "Analyse Wi-Fi Direct / P2P connection and GO negotiation",
    "WRDS/WGDS/EWRD/SGOM": "Inspect WRDS, EWRD, WGDS, SGOM SAR table reads",
    "BSOD":                "Analyse Blue Screen / FW assert / system crash events",
    "Assert":              "Investigate firmware assertion failures and UMAC/LMAC errors",
    "PPAG":                "Inspect Per-Platform Antenna Gain BIOS configuration",
    "TAS":                 "Analyse TAS-related log events",
    "UATS":                "Analyse UATS-related log events",
    "Unclassified":        "General log scan – no specific skill filter applied",
}

FALLBACK_KEYWORDS: Dict[str, List[str]] = {
    "TAS":          ["TAS"],
    "UATS":         ["UATS"],
    "Unclassified": ["[E]", "error", "assert", "crash", "BSOD"],
    "Default":      ["[E]", "error", "assert"],
}


# ---------------------------------------------------------
# 2. Data structures
# ---------------------------------------------------------
class Skill(BaseModel):
    name: str
    description: str
    keywords: List[str]          # parsed from TAT for fallback use
    exclusive: List[str] = Field(default_factory=list)  # lines containing these terms are removed post-filter
    tat_path: Optional[str]      # path to original .tat file (preferred for filtering)
    expert_rules: str


# ---------------------------------------------------------
# 3. Shared-folder loaders
# ---------------------------------------------------------
def _load_prompt_from_py(py_path: str) -> str:
    """Import a prompt .py file and return its SYS_PROMPT string."""
    try:
        spec = importlib.util.spec_from_file_location("_prompt_mod_", py_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return getattr(mod, "SYS_PROMPT", "")
    except Exception as e:
        print(f"  ⚠️  Could not load prompt '{py_path}': {e}")
        return ""


def _load_keywords_from_tat(tat_path: str) -> List[str]:
    """
    Parse a TextAnalysisTool .tat XML file and return the `text` attribute
    from every filter that is enabled AND is not an exclusion filter.
    Regex filters are skipped (too complex for simple substring matching).
    """
    keywords: List[str] = []
    try:
        tree = ET.parse(tat_path)
        root = tree.getroot()
        for f in root.findall(".//filter"):
            enabled   = f.get("enabled",   "n").lower()
            excluding = f.get("excluding",  "n").lower()
            text      = f.get("text",       "").strip()
            is_regex  = f.get("regex",       "n").lower()
            if enabled == "y" and excluding == "n" and text and is_regex == "n":
                keywords.append(text)
    except Exception as e:
        print(f"  ⚠️  Could not parse filter '{tat_path}': {e}")
    return keywords


def build_skill_file_map(data_dir: str) -> Optional[Dict[str, Tuple[Optional[str], Optional[str]]]]:
    """
    Auto-discover skills by scanning the prompt/ and filter/ sub-folders of
    `data_dir`.  Files are paired by stem name (e.g. yellow_bang.py ↔
    yellow_bang.tat).  Category names are derived from the file stem.
    Returns None when the directory is missing or contains no recognised files,
    signalling the caller to fall back to built-in skills.
    """
    prompt_dir = Path(data_dir) / "prompt"
    filter_dir = Path(data_dir) / "filter"

    prompts = {f.stem: f.name for f in prompt_dir.glob("*.py")} if prompt_dir.exists() else {}
    filters = {f.stem: f.name for f in filter_dir.glob("*.tat")} if filter_dir.exists() else {}
    all_stems = sorted(set(prompts) | set(filters))

    if not all_stems:
        print("  ⚠️  No prompt/filter files found in directory – will use built-in skills.")
        return None

    return {stem: (prompts.get(stem), filters.get(stem)) for stem in all_stems}


def load_skills_from_data_dir(data_dir: str) -> Dict[str, "Skill"]:
    """
    Build the full skill dictionary by reading prompt .py files and
    filter .tat XML files from `data_dir`.
    Uses build_skill_file_map() to auto-discover available files, falling
    back to the hardcoded SKILL_FILE_MAP if the directory is empty.
    """
    data_path  = Path(data_dir)
    prompt_dir = data_path / "prompt"
    filter_dir = data_path / "filter"

    skill_map = build_skill_file_map(data_dir)
    if skill_map is None:
        print("  ⚠️  Falling back to built-in skills.")
        return get_builtin_skills()

    # Build a case-insensitive lookup for SKILL_DESCRIPTIONS so stems like
    # "yellow_bang" still match the hardcoded key "Yellow_Bang".
    desc_lower = {k.lower(): v for k, v in SKILL_DESCRIPTIONS.items()}

    skills: Dict[str, Skill] = {}
    for category, (prompt_file, filter_file) in skill_map.items():
        # prompt / expert_rules
        expert_rules = ""
        if prompt_file and (prompt_dir / prompt_file).exists():
            expert_rules = _load_prompt_from_py(str(prompt_dir / prompt_file))

        # keywords from .tat
        keywords: List[str] = []
        if filter_file and (filter_dir / filter_file).exists():
            keywords = _load_keywords_from_tat(str(filter_dir / filter_file))

        if not keywords:
            keywords = FALLBACK_KEYWORDS.get(category, FALLBACK_KEYWORDS["Default"])

        description = (SKILL_DESCRIPTIONS.get(category)
                       or desc_lower.get(category.lower())
                       or f"Analyse log entries related to {category}.")

        skills[category] = Skill(
            name=category,
            description=description,
            keywords=keywords,
            tat_path=str(filter_dir / filter_file) if filter_file and (filter_dir / filter_file).exists() else None,
            expert_rules=expert_rules or f"Analyse log entries related to {category}.",
        )
        kw_count = len(keywords)
        prompt_ok = "✔ prompt" if expert_rules else "⚠ no prompt"
        print(f"  📌 Skill loaded: {category:<25} {kw_count:>3} keywords  {prompt_ok}")

    return skills


# ---------------------------------------------------------
# 4. Builtin fallback skills (module-level so llm_service can import it)
# ---------------------------------------------------------
def get_builtin_skills() -> Dict[str, "Skill"]:
    """
    Return an empty skill dict by default.
    Skills must be loaded explicitly via skills.yaml or the data directory.
    """
    return {}


# ---------------------------------------------------------
# 4b. Load skills from a YAML file (standalone, no prompt/filter dirs needed)
# ---------------------------------------------------------
def load_skills_from_yaml(yaml_path: str) -> Dict[str, "Skill"]:
    """
    Parse a skills.yaml file and return a Dict[str, Skill].

    Expected YAML structure:
      skill_key:
        name: "display_name"
        description: "short description"
        keywords:
          - "KEYWORD_1"
          - "KEYWORD_2"
        expert_rules: |
          Multi-line expert rules text...
    """
    import yaml

    path = Path(yaml_path)
    if not path.exists():
        raise FileNotFoundError(f"Skills YAML not found: {yaml_path}")

    with open(path, 'r', encoding='utf-8') as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"Invalid YAML structure: expected a dict, got {type(raw).__name__}")

    skills: Dict[str, Skill] = {}
    for key, val in raw.items():
        if not isinstance(val, dict):
            print(f"  ⚠️  Skipping non-dict entry: {key}")
            continue

        name = val.get("name", key)
        description = val.get("description", f"Analyse logs related to {name}.")
        keywords = val.get("keywords", [])
        exclusive = val.get("exclusive", [])
        expert_rules = val.get("expert_rules", "Please analyze the logs.")

        if not isinstance(keywords, list):
            keywords = [str(keywords)]
        if not isinstance(exclusive, list):
            exclusive = [str(exclusive)]

        # expert_rules is stored as a single string in YAML. If a caller
        # (or a hand-edited file) passes a list, fold it down to a numbered
        # string defensively so the agent always sees a plain prompt fragment.
        # Anything else that resolves to None / "" / whitespace falls back to
        # the same default the list-branch uses, so an `expert_rules: null`
        # or bare `expert_rules:` in the YAML doesn't leak the literal
        # string "None" into the agent prompt.
        _DEFAULT_RULES = "Please analyze the logs."
        if isinstance(expert_rules, list):
            items = [str(r).strip() for r in expert_rules if str(r).strip()]
            expert_rules = (
                "\n\n".join(f"{i}. {r}" for i, r in enumerate(items, start=1))
                if items else _DEFAULT_RULES
            )
        elif expert_rules is None:
            expert_rules = _DEFAULT_RULES
        elif not isinstance(expert_rules, str):
            coerced = str(expert_rules).strip()
            expert_rules = coerced if coerced else _DEFAULT_RULES
        elif not expert_rules.strip():
            expert_rules = _DEFAULT_RULES

        skills[key] = Skill(
            name=name,
            description=description,
            keywords=keywords,
            exclusive=[str(x) for x in exclusive if str(x).strip()],
            tat_path=None,
            expert_rules=expert_rules,
        )
        print(f"  📌 YAML Skill loaded: {key:<30} {len(keywords):>3} keywords")

    if not skills:
        raise ValueError(f"No valid skills found in {yaml_path}")

    print(f"✅  {len(skills)} skills loaded from YAML: {yaml_path}")
    return skills


# ---------------------------------------------------------
# 5. Agent System
# ---------------------------------------------------------
class WifiLogAgentSystem:
    """
    Multi-skill Wi-Fi log analysis agent.

    Reuses the OpenAI-compatible client from app_config.llm_helper so that
    ExpertGPT / any configured endpoint is honoured automatically.

    Skills are loaded from the shared data_dir at construction time and
    fall back to built-in hardcoded skills if the folder is unreachable.
    """

    # Hard stop to prevent runaway per-step token spikes.
    # ~500K daily budget guidance (1 token ≈ 4 chars, MAX_TOOL_CALLS_PER_STEP=3):
    #   High volume (10x/day): max_steps=4,  MAX_TOOL_RESULT=3000,  MAX_TOKENS_PER_STEP=15000 → ~50K/analysis
    #   Balanced   (5-7x/day): max_steps=5,  MAX_TOOL_RESULT=6000,  MAX_TOKENS_PER_STEP=25000 → ~70-90K/analysis
    #   Quality    (3-5x/day): max_steps=5,  MAX_TOOL_RESULT=16000, MAX_TOKENS_PER_STEP=40000 → ~100K/analysis
    MAX_TOKENS_PER_STEP = 75000           # 3 tools × 16K evidence = ~12K tokens/step; headroom for rules + prompt history
    # Keep per-tool evidence compact so multi-step prompts do not explode.
    # These are sized to match MAX_TOOL_RESULT_CHARS_IN_MESSAGES (16000):
    #   ~50 chars/line → 16000 ÷ 50 = 320 lines before char limit fires anyway.
    #   MAX_SKILL_FOCUS_CHARS slightly above 16000 to absorb header overhead.
    #   MAX_RECENT_SKILL_HISTORY_LINES = 100: cross-skill hints share the same
    #   16000-char budget leaving ~4000 chars (≈80 lines) for history context.
    MAX_ASSEMBLED_LOG_CHARS_PER_TOOL_CALL = 16000
    MAX_ASSEMBLED_LOG_LINES_PER_TOOL_CALL = 320
    # Keep per-skill payload aligned with the evidence clip limit.
    MAX_SKILL_FOCUS_LINES = 320
    MAX_SKILL_FOCUS_CHARS = 16500  # slightly above clip limit to absorb header text
    MAX_RECENT_SKILL_HISTORY_LINES = 100
    # Additional hard limits for multi-step prompt growth control.
    MAX_TOOL_CONTENT_CHARS_IN_MESSAGES = 1200
    MAX_TOOL_RESULT_CHARS_IN_MESSAGES = 16000  # ~quality: 4000 tokens of evidence per tool call
    MAX_QUERY_DETAIL_OUTPUT_CHARS = 1800
    MAX_DETAIL_CONTEXT_SPAN = 50
    DEFAULT_DETAIL_CONTEXT_SPAN = 20
    MAX_DETAIL_HITS = 2
    # Convergence controls to finish within fixed max steps.
    MAX_TOOL_CALLS_PER_STEP = 3
    FORCE_CONCLUDE_LAST_N_STEPS = 2  # last 2 steps forces conclusion (5-step loop is tighter)
    MAX_SKILL_FETCHES = 6             # max distinct skills the agent may fetch per analysis

    # Segment1 (driver/init context block) is an OPTIONAL part of scoping.
    # The domain-agnostic scoping is the issue-time window (Segment2); the
    # marker-based init block is an optimisation that only some log families
    # (e.g. Wi-Fi/WDI) have a clean lifecycle for. When markers are empty or
    # none match, Segment1 is simply not produced (a no-op, not an error) and
    # Segment2 carries the scope.
    #
    # SCOPE_FULL_LOG_WHEN_EMPTY: when True, if BOTH the marker block and the
    # issue-time window come up empty, scope the entire log instead of leaving
    # the agent with nothing. The downstream skill keyword filter keeps the
    # volume manageable. Default False (Wi-Fi keeps its original behaviour);
    # log families without a reliable init/reset lifecycle set this True so
    # analysis always has something to work on.
    SCOPE_FULL_LOG_WHEN_EMPTY = False

    # Pre-scan markers for Segment1 (driver init block).
    #
    # Class-level so subclasses can target their own log family without
    # touching the scan logic. Each marker may be EITHER a single string OR
    # a list of candidate strings — the scan matches a line if it contains
    # ANY candidate (substring, case-insensitive). Keeping these as data
    # (extend the list, don't edit the loop) lets new driver builds that
    # rename a callback be supported by just adding another candidate.
    #
    # WiFi/WDI defaults below; the Bluetooth subclass overrides with
    # ibtpci-flavoured candidates. Compared after lowercasing, so keep
    # candidates lowercase.
    DRIVER_ADD_MARKER = "os issued driver device add"
    RESET_MARKER = "got command (m1 message) task_dot11_reset"

    @staticmethod
    def _normalize_markers(value) -> List[str]:
        """Coerce a marker class-attr (str | list | None) into a lowercased
        list of non-empty candidate substrings."""
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        out = []
        for v in value:
            s = str(v).strip().lower()
            if s:
                out.append(s)
        return out

    @staticmethod
    def _line_matches_any(line_lower: str, markers: List[str]) -> bool:
        """True if the (already-lowercased) line contains any candidate marker."""
        return any(m in line_lower for m in markers)

    REPORT_MARKDOWN_TEMPLATE = (
        "Your `markdown_summary` format (REQUIRED):\n"
        "  # Executive Summary\n  (1-2 sentences that directly answer the user question)\n\n"
        "  | Aspect | Finding |\n"
        "  |--------|---------|\n"
        "  | Signal | ... |\n"
        "  (Markdown table with data gaps)\n\n"
        "  ## Timeline\n"
        "  - T-Ns: Trigger Event (if confirmed)\n"
        "  - T+0s: Symptom/Observation\n"
        "  - T+Ns: Latest verified state\n\n"
        "  ## Recommendations\n"
        "  **P0 (Urgent):** ...\n"
        "  **P1 (Important):** ...\n"
        "  **P2 (Nice-to-have):** ..."
    )

    def __init__(self, client, model: str = "gpt-4.1",
                 data_dir: Optional[str] = None,
                 skills: Optional[Dict[str, "Skill"]] = None):
        self.client = client
        self.model  = model
        self.current_log_path: str = ""
        self.conversation_history: List[dict] = []
        # Cooperative cancellation. A background chat job (see chat_jobs) sets
        # this event when the user clicks "Stop"; the agentic tools loop polls
        # it between reasoning steps and bails out early. Cleared at the start
        # of every chat turn so a prior stop can't cancel the next one.
        self.cancel_event: threading.Event = threading.Event()
        # Token usage for the CURRENT turn, accumulated across every LLM call
        # (agentic loops make many). Reset at the top of each chat() so it only
        # ever describes one turn; read by the routes after chat() returns and
        # handed to gather_service for cost accounting. See _reset_turn_usage.
        self.last_turn_usage: dict = self._empty_turn_usage()
        self.issue_context: dict = {}  # populated by prime_with_context()
        self.issue_time: Optional[datetime] = None  # populated by prime_with_context() or _chat_with_tools()
        self._issue_time_time_only: bool = False
        # Customer-tz annotation surfaced alongside ``issue_time``. The
        # canonical ``self.issue_time`` is kept in the log's own frame so it
        # matches the .log content for PreScan; ``issue_time_customer`` is
        # the SAME instant viewed from the customer's wall clock (e.g. for
        # a CST customer "01:26 GMT+8" surfaces as "12:26 CST"). Both stay
        # None until ``prime_with_context`` resolves them.
        self.issue_time_customer: Optional[datetime] = None
        self.issue_time_tz: str = ""
        # Half-width (in minutes) of the Segment2 log window captured
        # around issue_time. Default ±5 min; user-adjustable from the
        # chatbot sidebar via the /chat `issue_time_window_minutes` param.
        self.issue_time_window_minutes: int = 5
        # In-memory caches for this agent session
        self._raw_log_cache: List[str] = []
        self._raw_log_cache_path: str = ""
        self._filter_cache_by_skill: Dict[str, dict] = {}
        self._detail_cache: Dict[str, str] = {}
        self._detail_query_seen: set = set()
        self._chat_rules_injected_skills: set = set()
        # Cumulative assembled log store (timestamp-based, no line number persistence)
        self._assembled_entries_by_key: Dict[str, dict] = {}
        self._assembled_entries_no_ts: Dict[str, dict] = {}
        self._assembled_log_text: str = ""
        self._filter_export_counter: int = 0
        # Pre-analysis scan results (populated before skill filtering)
        self._driver_init_count: int = 0
        self._driver_init_lines: List[str] = []
        self._issue_time_window_lines: List[str] = []
        self._scoped_log_lines: List[str] = []  # merged segments for skill filtering

        if skills:
            # Use pre-loaded skills passed in (e.g. from LLM_helper.skills)
            self.skills = skills
            print(f"✅  Log Chatbot Agent using {len(skills)} pre-loaded skills from LLM_helper.")
        elif data_dir and Path(data_dir).exists():
            print(f"🛠  Loading chatbot skills from: {data_dir}")
            self.skills = load_skills_from_data_dir(data_dir)
            print(f"✅  {len(self.skills)} skills loaded from shared folder.")
        else:
            print("⚠️  No skills source available – using built-in fallback skills.")
            self.skills = get_builtin_skills()

        # ACE adaptation hook. When attach_ace() has been called with an
        # AceRunner instance, the agent injects the workflow + domain
        # playbooks into its prompts at generation time, and can also drive
        # post-feedback Reflector/Curator updates. Unattached -> no-op.
        self.ace_runner = None

    def attach_ace(self, runner) -> None:
        """Wire an AceRunner into this agent so it reads/writes playbooks."""
        self.ace_runner = runner
        print(f"🧠 ACE attached to chatbot agent (playbooks_dir={getattr(runner, 'playbooks_dir', '?')})")

    def adapt_from_feedback(self, conversation_id: str, turn_id: str) -> dict:
        """
        Run one Reflector + Curator pass against a voted conversation turn.
        Returns the runner's per-turn summary (or {'status': 'no_ace'} when
        ACE is not attached). Safe to call from any thread — playbook IO is
        lock-guarded inside the runner.
        """
        if self.ace_runner is None:
            return {"status": "no_ace"}
        return self.ace_runner.run_one(conversation_id, turn_id)

    def get_skill_names(self) -> List[str]:
        return list(self.skills.keys())

    def get_skill_descriptions(self) -> List[dict]:
        return [{"name": s.name, "description": s.description}
                for s in self.skills.values()]

    # ------------------------------------------------------------------
    # Step 1 – TAT keyword filter: same pipeline as log_parser_service
    # ------------------------------------------------------------------
    def _ensure_raw_log_cache(self) -> Optional[str]:
        """Load raw log once per current log path and keep it in memory."""
        if not self.current_log_path:
            return "Error: No log file has been set. Please set a log file path first."

        if self._raw_log_cache_path == self.current_log_path and self._raw_log_cache:
            return None

        try:
            self._raw_log_cache = helpers.read_log_file(self.current_log_path)
            self._raw_log_cache_path = self.current_log_path
            # A different file means all derived caches are stale.
            self._filter_cache_by_skill = {}
            self._detail_cache = {}
            self._detail_query_seen = set()
            self._chat_rules_injected_skills = set()
            self._assembled_entries_by_key = {}
            self._assembled_entries_no_ts = {}
            self._assembled_log_text = ""
            self._filter_export_counter = 0
            self._driver_init_count = 0
            self._driver_init_lines = []
            self._issue_time_window_lines = []
            self._scoped_log_lines = []
            # Date-format detection is per-file — invalidate explicitly so a
            # subsequent _log_has_date() call re-runs on the freshly loaded
            # content. (_log_has_date already keys its own cache on the raw
            # cache path, but clearing here makes the invariant impossible
            # to miss when the user loads a WiFi log after a DDD log and
            # expects the sidebar's date-required mode to come back.)
            self._log_has_date_cache = None
            self._log_has_date_cache_path = None
            return None
        except Exception as e:
            return f"Error reading log file: {e}"

    def _log_has_date(self) -> bool:
        """Whether the loaded log's timestamps carry a DATE
        (MM/DD/YYYY-HH:MM:SS.mmm) or are time-only (e.g. DDD / tracefmt logs:
        "HH:MM:SS.fffffff ..." with no date). Cached per log path.

        This splits the pre-scan into two paths: dated logs keep the original
        datetime windowing; time-only logs match by time-of-day instead, so a
        DDD log (no date) doesn't leave the agent with nothing to analyse.

        Cheap by design: it only needs to inspect the first ~2000 lines, so it
        does NOT pull the whole file into memory. When the full cache already
        happens to be loaded it reuses it; otherwise it streams just the head
        of the file. This keeps set_log / page-load fast even for a
        multi-hundred-MB BT .hci.txt (the full read is deferred to the first
        actual analysis).
        """
        if not self.current_log_path:
            return True  # no log → assume dated (original path)

        if getattr(self, "_log_has_date_cache_path", None) == self.current_log_path \
                and getattr(self, "_log_has_date_cache", None) is not None:
            return self._log_has_date_cache

        full_re = re.compile(r'\d{2}/\d{2}/\d{4}-\d{2}:\d{2}:\d{2}\.\d{3}')

        # Prefer the in-memory cache if it's already for this path; else peek
        # the file head via a streaming read (no full load).
        if self._raw_log_cache_path == self.current_log_path and self._raw_log_cache:
            sample = self._raw_log_cache[:2000]
        else:
            sample = []
            try:
                with open(self.current_log_path, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f):
                        if i >= 2000:
                            break
                        sample.append(line)
            except Exception:
                return True  # read error → assume dated (keep original path)

        has_date = any(full_re.search(line) for line in sample)
        self._log_has_date_cache = has_date
        self._log_has_date_cache_path = self.current_log_path
        return has_date

    def _merge_wrapped_lines_if_needed(self) -> None:
        """Some time-only logs (e.g. DDD) wrap a single entry across extra
        physical lines that carry no leading timestamp. Join each such
        continuation line back onto the preceding entry so every logical entry
        is one line — otherwise the orphan fragments break time-windowing and
        skill filtering.

        Surgical + idempotent: only runs for no-date logs (dated logs are
        untouched, including most BT HCI ``.hci.txt`` dumps which carry
        full ``MM/DD/YYYY-HH:MM:SS.mmm`` timestamps), and only rewrites the
        cache when a wrap is actually found.

        An entry line is recognised when it starts with — in any combination
        — an optional sequence number, optional angle-bracket prefix, then a
        ``HH:MM:SS`` time-of-day. Examples that match:
          * ``12:34:56 ...``         (plain DDD)
          * ``0004 12:34:56 ...``    (seq# + space + time)
          * ``<12:34:56> ...``       (BT-style angle-bracket form)
          * ``<12:34:56.789> ...``   (BT-style with ms)
        Anything else is treated as a continuation. False positives are
        cheaper than false negatives here — a misclassified entry-start
        just doesn't get merged, but a misclassified continuation collapses
        distinct entries together.
        """
        if self._log_has_date():
            return
        lines = self._raw_log_cache or []
        # ``<?`` makes the angle-bracket prefix optional, covering both the
        # legacy DDD form (no brackets) and the BT HCI no-date form which
        # wraps the time-of-day in ``<...>``.
        start_re = re.compile(r'^\s*<?(?:\d+\s+)?\d{1,2}:\d{2}:\d{2}')
        merged: List[str] = []
        wrapped = 0
        for line in lines:
            if not line.strip() or not merged or start_re.match(line):
                # Blank lines and entry-start lines pass through unchanged;
                # blanks are never treated as continuations.
                merged.append(line)
            else:
                merged[-1] = merged[-1].rstrip("\r\n") + " " + line.strip()
                wrapped += 1
        if wrapped:
            self._raw_log_cache = merged
            print(f"[PreScan] 🔗 Merged {wrapped} wrapped continuation line(s) "
                  f"in time-only log (now {len(merged)} entries).")

    def get_log_span_minutes(self) -> int:
        """
        Whole-minute span of the log (first → last parseable timestamp). Used
        to bound the sidebar issue-time capture window so the user can't
        request a window wider than the log itself. Returns 0 when the log
        can't be read or has no parseable (dated) timestamps.

        Cheap by design: uses a seek-based first/last read (head + tail only),
        so a multi-hundred-MB BT .hci.txt is NOT pulled into memory just to
        size the sidebar slider. The full read is deferred to first analysis.
        """
        if not self.current_log_path:
            return 0
        try:
            from utils.issue_time_utils import read_log_time_range
            import math as _math
            first_ts, last_ts = read_log_time_range(self.current_log_path)
            if not first_ts or not last_ts:
                return 0
            span_min = (last_ts - first_ts).total_seconds() / 60.0
            return max(0, int(_math.ceil(span_min)))
        except Exception:
            return 0

    def _preprocess_raw_log_context(self) -> None:
        """
        Pre-analysis scan executed on self._raw_log_cache BEFORE any skill
        filtering runs.  Produces two segments that are merged into
        self._scoped_log_lines — ALL subsequent skill filtering operates
        ONLY on these scoped lines, not the full raw cache.

        Segment 1 – Driver init settings
          First "OS issued Driver Device Add"
          → first "Got Command (M1 Message) TASK_DOT11_RESET" AFTER it (inclusive).

        Segment 2 – Event investigation window (one of):
          A) issue_time ±5 min  (if issue_time is available)
          B) last TASK_DOT11_RESET (after first Driver Add) → EOF  (fallback)
        """
        # Markers come from class attributes so subclasses can override
        # (e.g. BtLogAgentSystem uses ibtpci-specific markers). Each may be a
        # single string or a list of candidates — normalise to a list and
        # match ANY candidate, so the scan logic stays generic.
        driver_add_markers = self._normalize_markers(self.DRIVER_ADD_MARKER)
        reset_markers      = self._normalize_markers(self.RESET_MARKER)
        TS_RE = re.compile(r'(\d{2}/\d{2}/\d{4}-\d{2}:\d{2}:\d{2}\.\d{3})')

        # Time-only logs (e.g. DDD) may wrap one entry across extra lines —
        # merge those back first so segments/scoping see whole entries. No-op
        # for dated logs and for logs that don't wrap.
        self._merge_wrapped_lines_if_needed()

        total_lines = len(self._raw_log_cache)

        # ------------------------------------------------------------------
        # Pass 1: single scan — find first ADD, count ADDs, collect all RESETs
        # ------------------------------------------------------------------
        driver_add_indices = []
        reset_indices = []
        for i, line in enumerate(self._raw_log_cache):
            line_lower = line.lower()
            if self._line_matches_any(line_lower, driver_add_markers):
                driver_add_indices.append(i)
                print(f"[PreScan] 🚩 driver-add marker found at line {i+1}")
            if self._line_matches_any(line_lower, reset_markers):
                reset_indices.append(i)

        self._driver_init_count = len(driver_add_indices)

        def _line_ts(idx: int) -> Optional[datetime]:
            m = TS_RE.search(self._raw_log_cache[idx])
            if not m:
                return None
            try:
                return datetime.strptime(m.group(1), "%m/%d/%Y-%H:%M:%S.%f")
            except ValueError:
                return None

        # ------------------------------------------------------------------
        # Segment 1: choose ONE block nearest to issue_time
        # (fallback: first valid block when issue_time is unavailable)
        # ------------------------------------------------------------------
        first_add_idx = None
        seg1_end = None
        self._driver_init_lines = []

        seg1_candidates = []
        for idx, add_idx in enumerate(driver_add_indices):
            next_add_idx = driver_add_indices[idx + 1] if idx + 1 < len(driver_add_indices) else total_lines
            first_reset_idx = next((r for r in reset_indices if add_idx < r < next_add_idx), None)
            if first_reset_idx is not None:
                seg1_candidates.append({
                    "start_idx": add_idx,
                    "end_idx": first_reset_idx + 1,
                    "anchor_ts": _line_ts(first_reset_idx) or _line_ts(add_idx),
                })
            else:
                print(f"[PreScan] ⚠️  No RESET found after ADD at line {add_idx+1} before next ADD.")

        if seg1_candidates:
            chosen = None
            if self.issue_time:
                with_ts = [c for c in seg1_candidates if c.get("anchor_ts") is not None]
                if with_ts:
                    chosen = min(with_ts, key=lambda c: abs((c["anchor_ts"] - self.issue_time).total_seconds()))
            if chosen is None:
                chosen = seg1_candidates[0]

            first_add_idx = chosen["start_idx"]
            seg1_end = chosen["end_idx"]
            self._driver_init_lines = self._raw_log_cache[first_add_idx:seg1_end]
            print(
                f"[PreScan] ✅ Segment1 — Driver init block: "
                f"line {first_add_idx + 1} → line {seg1_end} "
                f"({len(self._driver_init_lines)} lines) | "
                f"driver load occurrences in full log: {self._driver_init_count}"
            )
            if self.issue_time and chosen.get("anchor_ts") is not None:
                print(
                    f"[PreScan]  Segment1 selected by nearest issue_time: "
                    f"{chosen['anchor_ts'].strftime('%m/%d/%Y %H:%M:%S.%f')[:-3]}"
                )

        if first_add_idx is None or seg1_end is None:
            print("[PreScan] ⚠️  No valid Segment1 block found.")

        # ------------------------------------------------------------------
        # Segment 2: event investigation window
        # ------------------------------------------------------------------
        seg2_lines: List[str] = []
        seg2_start_idx: int = -1
        seg2_end_idx:   int = -1

        if self.issue_time and not self._log_has_date():
            # --- 2A (TIME-ONLY logs, e.g. DDD/tracefmt with no date) ---
            # The log carries no date, so match the issue_time's TIME-OF-DAY
            # only (seconds-of-day). issue_time's date part (if any) is ignored.
            _raw_win = self.issue_time_window_minutes
            _win = _raw_win if isinstance(_raw_win, int) and _raw_win >= 0 else 5
            issue_sod = (self.issue_time.hour * 3600 + self.issue_time.minute * 60
                         + self.issue_time.second)
            win_sec = _win * 60
            # Seconds-of-day is cyclic — the time-of-day axis wraps at
            # midnight (86400). Issue times near 00:00 or 23:59 with a
            # symmetric window will produce a lo/hi that crosses midnight
            # (negative lo, or hi >= 86400). Build the predicate so it
            # tests the union of the two valid sub-ranges in that case,
            # so late-night entries that should be in the window are kept.
            # The common case (window comfortably inside one day) falls
            # through to a single-interval comparison.
            SEC_PER_DAY = 86400
            lo_sod, hi_sod = issue_sod - win_sec, issue_sod + win_sec
            if 0 <= lo_sod and hi_sod < SEC_PER_DAY:
                _in_window = lambda sod: lo_sod <= sod <= hi_sod
            else:
                lo_norm = lo_sod % SEC_PER_DAY
                hi_norm = hi_sod % SEC_PER_DAY
                if lo_norm <= hi_norm:
                    _in_window = lambda sod: lo_norm <= sod <= hi_norm
                else:
                    # Window wraps midnight — split into two valid ranges.
                    _in_window = lambda sod: sod >= lo_norm or sod <= hi_norm
            _time_re = re.compile(r'\b(\d{1,2}):(\d{2}):(\d{2})')
            # Track only first/last matching indices instead of every match.
            # On a multi-hour DDD trace a wide capture window can land
            # thousands of hits — accumulating them all into a List[int]
            # spikes memory for no gain since we only ever read the first
            # and the last to slice the cache.
            seg2_first_hit: Optional[int] = None
            seg2_last_hit: Optional[int] = None
            for i, line in enumerate(self._raw_log_cache):
                m = _time_re.search(line)
                if not m:
                    continue
                sod = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                if _in_window(sod):
                    if seg2_first_hit is None:
                        seg2_first_hit = i
                    seg2_last_hit = i
            if seg2_first_hit is not None and seg2_last_hit is not None:
                seg2_start_idx = seg2_first_hit
                seg2_end_idx = seg2_last_hit
                seg2_lines = self._raw_log_cache[seg2_start_idx:seg2_end_idx + 1]
                print(
                    f"[PreScan] ✅ Segment2 (time-only log) — Issue-time window: "
                    f"line {seg2_start_idx + 1} → line {seg2_end_idx + 1} "
                    f"({len(seg2_lines)} lines) | ±{_win} min of "
                    f"{self.issue_time.strftime('%H:%M:%S')} (log has no date)"
                )
            else:
                print(
                    f"[PreScan] ⚠️  No time-only anchors within ±{_win} min of "
                    f"{self.issue_time.strftime('%H:%M:%S')} (log has no date) — falling back."
                )

        elif self.issue_time:
            # --- 2A: issue_time ±N min (timestamp indices + contiguous slice) ---
            # 0 is allowed (capture only the exact issue instant); a None /
            # negative falls back to the default 5.
            _raw_win = self.issue_time_window_minutes
            _win = _raw_win if isinstance(_raw_win, int) and _raw_win >= 0 else 5
            window_start = self.issue_time - timedelta(minutes=_win)
            window_end   = self.issue_time + timedelta(minutes=_win)

            ts_points: List[Tuple[datetime, int]] = []
            for i, line in enumerate(self._raw_log_cache):
                m = TS_RE.search(line)
                if m:
                    try:
                        t = datetime.strptime(m.group(1), "%m/%d/%Y-%H:%M:%S.%f")
                        ts_points.append((t, i))
                    except ValueError:
                        pass

            # --- Sanity check: verify attachment issue_time falls within log's actual time range ---
            if ts_points:
                log_first_ts = ts_points[0][0]
                log_last_ts  = ts_points[-1][0]
                # Allow a 24-hour grace margin (attachment time vs. log may have slight mismatch)
                reasonable = (
                    (log_first_ts - timedelta(hours=24)) <= self.issue_time <= (log_last_ts + timedelta(hours=24))
                )

                # If issue_time came from time-only (HH:MM[:SS]) and date is mismatched,
                # align it to the log date so Segment2 can still use +/-5 minutes.
                if not reasonable and self._issue_time_time_only:
                    aligned = datetime.combine(log_last_ts.date(), self.issue_time.time())
                    print(
                        f"[PreScan] ℹ️  issue_time is time-only; aligning date to log: "
                        f"{self.issue_time.strftime('%m/%d/%Y %H:%M:%S')} -> "
                        f"{aligned.strftime('%m/%d/%Y %H:%M:%S')}"
                    )
                    self.issue_time = aligned
                    window_start = self.issue_time - timedelta(minutes=_win)
                    window_end = self.issue_time + timedelta(minutes=_win)
                    reasonable = (
                        (log_first_ts - timedelta(hours=24)) <= self.issue_time <= (log_last_ts + timedelta(hours=24))
                    )

                if not reasonable:
                    print(
                        f"[PreScan] ⚠️  WARNING: Attachment issue_time "
                        f"{self.issue_time.strftime('%m/%d/%Y %H:%M:%S')} is outside the log time range "
                        f"[{log_first_ts.strftime('%m/%d/%Y %H:%M:%S')} ~ "
                        f"{log_last_ts.strftime('%m/%d/%Y %H:%M:%S')}] — "
                        f"attachment time attached is unreasonable, falling back to Segment1 end → EOF."
                    )
                    # Clear issue_time so Segment2 falls through to 2B fallback
                    self.issue_time = None
                else:
                    ts_values = [x[0] for x in ts_points]
                    left = bisect_left(ts_values, window_start)
                    right = bisect_right(ts_values, window_end) - 1
                    if left <= right:
                        seg2_start_idx = ts_points[left][1]
                        seg2_end_idx = ts_points[right][1]
                        seg2_lines = self._raw_log_cache[seg2_start_idx:seg2_end_idx + 1]

            if seg2_lines and seg2_start_idx >= 0 and seg2_end_idx >= 0:
                print(
                    f"[PreScan] ✅ Segment2 — Issue-time window: "
                    f"line {seg2_start_idx + 1} → line {seg2_end_idx + 1} "
                    f"({len(seg2_lines)} lines, contiguous index slice) | "
                    f"±{_win} min of {self.issue_time.strftime('%m/%d/%Y %H:%M:%S')}"
                )
            elif self.issue_time is not None:
                print(
                    f"[PreScan] ⚠️  No timestamped anchors within ±{_win} min of "
                    f"{self.issue_time.strftime('%m/%d/%Y %H:%M:%S')} — "
                    f"falling back to Segment1 end → EOF."
                )
                # fall through to 2B below

        if not seg2_lines:
            # --- 2B fallback: Segment1 end → EOF ---
            if seg1_end is not None:
                seg2_start = seg1_end  # pick up right where Segment1 left off
                seg2_lines     = self._raw_log_cache[seg2_start:]
                seg2_start_idx = seg2_start
                seg2_end_idx   = total_lines - 1
                print(
                    f"[PreScan] ✅ Segment2 — Segment1 end → EOF: "
                    f"line {seg2_start + 1} → line {total_lines} "
                    f"({len(seg2_lines)} lines)"
                )
            else:
                print("[PreScan] ⚠️  No driver markers found — Segment2 empty.")

        # Don't leave the agent with nothing to analyse when neither a marker
        # block nor an issue-time window matched. This happens for:
        #   * time-only logs (e.g. DDD) with no in-window anchors, and
        #   * any log family that defines no init/reset lifecycle and so opts
        #     into SCOPE_FULL_LOG_WHEN_EMPTY (e.g. the BT agent).
        # The downstream skill keyword filter trims the full scope back down,
        # so scoping everything is a safe fallback rather than a cost blow-up.
        if not seg2_lines and total_lines and (self.SCOPE_FULL_LOG_WHEN_EMPTY or not self._log_has_date()):
            seg2_lines = list(self._raw_log_cache)
            seg2_start_idx = 0
            seg2_end_idx = total_lines - 1
            print(f"[PreScan] ℹ️  No marker block / issue-time window — scoping full log ({total_lines} lines).")

        self._issue_time_window_lines = seg2_lines

        # ------------------------------------------------------------------
        # Merge Segment1 + Segment2 → _scoped_log_lines (de-duplicated)
        # ------------------------------------------------------------------
        # Use an OrderedDict-style approach: keep insertion order, skip dupes.
        seen_ids: set = set()
        merged: List[str] = []
        for line in self._driver_init_lines + seg2_lines:
            lid = id(line)           # same object from _raw_log_cache → same id
            if lid not in seen_ids:
                seen_ids.add(lid)
                merged.append(line)
        self._scoped_log_lines = merged

        overlap = len(self._driver_init_lines) + len(seg2_lines) - len(merged)
        print(
            f"[PreScan] 📦 Scoped segments merged: {len(merged)} lines "
            f"(Seg1: {len(self._driver_init_lines)} + Seg2: {len(seg2_lines)} — overlap: {overlap})"
        )
        if self.issue_time:
            print(f"[PreScan]  Skill filter will use: scoped {len(merged)} lines (±{_win} min window around issue_time)")
        else:
            print(
                f"[PreScan]  Skill filter will use: scoped {len(merged)} lines "
                f"(no issue_time — Segment2 falls back to Segment1 end → EOF)"
            )

        scoped_path = self._export_scoped_log_file()
        if scoped_path:
            print(f"[PreScan] 💾 Scoped lines saved to: {scoped_path}")

    def _export_scoped_log_file(self) -> str:
        """Overwrite scoped.txt in current log folder with latest scoped lines."""
        if not self.current_log_path:
            return ""

        parent_dir = Path(self.current_log_path).parent
        if not parent_dir.exists():
            return ""

        scoped_text = "\n".join(str(line).rstrip("\n") for line in (self._scoped_log_lines or []))
        scoped_file = parent_dir / "scoped.txt"
        try:
            scoped_file.write_text(scoped_text, encoding="utf-8")
            return str(scoped_file)
        except Exception as e:
            print(f"  ⚠️  Scoped export failed: {e}")
            return ""

    def _export_assembled_log_file(self) -> str:
        """
        Increment export index on each call (filterlog1, filterlog2, ...)
        and overwrite the existing file with the same index.
        """
        if not self.current_log_path:
            return ""

        parent_dir = Path(self.current_log_path).parent
        if not parent_dir.exists():
            return ""
        export_text = self._build_assembled_log_report("merged", new_added=0, apply_limits=False)

        # Always advance index; write_text will overwrite same-name files.
        self._filter_export_counter += 1
        candidate = parent_dir / f"filterlog{self._filter_export_counter}.txt"

        try:
            candidate.write_text(export_text, encoding="utf-8")
            return str(candidate)
        except Exception as e:
            print(f"  ⚠️  Export failed: {e}")
            return ""

    def _strip_line_number_prefix(self, line: str) -> str:
        """Remove persisted line-number markers to save memory in caches."""
        out = re.sub(r'^\s*(?:>>>|\s{3})\s*\[Line\s+\d+\]\s*', '', str(line))
        out = re.sub(r'^\s*\[Line\s+\d+\]\s*', '', out)
        return out.strip()

    def _extract_lines_from_filtered_blob(self, filtered_blob: str) -> List[str]:
        """Extract body lines from '[meta]\\n<body>' filtered output."""
        lines = []
        for idx, raw in enumerate((filtered_blob or "").splitlines()):
            text = str(raw).strip()
            if not text:
                continue
            # First line is metadata header from _get_filtered_log_lines.
            if idx == 0 and text.startswith('[') and text.endswith(']'):
                continue
            lines.append(self._strip_line_number_prefix(text))
        return [x for x in lines if x]

    def _normalize_time_message(self, line: str) -> Tuple[Optional[datetime], str, str]:
        """
        Keep only HH:MM:SS + message from a raw filtered line.
        Supports:
          * Full dated timestamps  ``MM/DD/YYYY-HH:MM:SS.mmm`` (WiFi WPP)
          * BT HCI angle-bracket   ``<HH:MM:SS.mmm>``          (ibtpci HCI dump)
          * DDD compact marker     ``<TIME:HH:MM:SS>`` / ``TIME:HH:MM:SS``
        Returns (parsed_ts_or_none, hhmmss_or_na, message_only).

        BT HCI carries no date — a synthetic datetime is built from the
        agent's ``issue_time`` date (or today's date) so the assembled-log
        sort key and time-range header still work.
        """
        raw = self._strip_line_number_prefix(line)

        # Prefer full date timestamp when present for stable chronological sorting.
        # dt_match = re.search(r'(\d{2}/\d{2}/\d{4}-(\d{2}:\d{2}:\d{2})\.\d{3})', raw)
        dt_match = re.search(r'(\d{2}/\d{2}/\d{4}-(\d{2}:\d{2}:\d{2}\.\d{3}))', raw)
        if dt_match:
            ts_full = dt_match.group(1)
            ts_hms = dt_match.group(2)
            ts_dt = None
            try:
                ts_dt = datetime.strptime(ts_full, "%m/%d/%Y-%H:%M:%S.%f")
            except ValueError:
                ts_dt = None
            msg = (raw[:dt_match.start()] + raw[dt_match.end():]).strip(" -:|\t")
        else:
            # BT HCI angle-bracket timestamp ``<HH:MM:SS.mmm>`` (no date).
            # Matched before the legacy ``<TIME:...>`` form because the BT
            # variant has no ``TIME:`` prefix and would otherwise fall
            # through to the "N/A" branch, losing time-axis fidelity.
            hci_match = re.search(r'<(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?>', raw)
            if hci_match:
                h  = int(hci_match.group(1))
                mn = int(hci_match.group(2))
                s  = int(hci_match.group(3))
                ms_str = hci_match.group(4) or "0"
                ms = int(ms_str.ljust(3, "0")[:3])
                ts_hms = f"{h:02d}:{mn:02d}:{s:02d}.{ms:03d}"
                # Synthesise a datetime so the assembled-log sort key and
                # time-range header keep working for time-only BT logs.
                # Prefer the agent's issue_time date; fall back to today.
                _base_date = (self.issue_time.date() if getattr(self, "issue_time", None)
                              else datetime.today().date())
                try:
                    ts_dt = datetime(_base_date.year, _base_date.month, _base_date.day,
                                     h, mn, s, ms * 1000)
                except ValueError:
                    ts_dt = None
                msg = (raw[:hci_match.start()] + raw[hci_match.end():]).strip(" -:|\t")
            else:
                # Fallback: handle logs like <TIME:09:54:13> or TIME:09:54:13
                time_tag_match = re.search(r'(?:<)?TIME:(\d{2}:\d{2}:\d{2})(?:>)?', raw, flags=re.IGNORECASE)
                ts_hms = time_tag_match.group(1) if time_tag_match else "N/A"
                ts_dt = None
                if time_tag_match:
                    msg = (raw[:time_tag_match.start()] + raw[time_tag_match.end():]).strip(" -:|\t")
                else:
                    msg = raw

        # Keep the payload part after function markers when present, e.g. "[func]:### ..."
        if "###" in msg:
            msg = msg[msg.find("###"):]
        elif "]:" in msg:
            # Keep the last [tag] before ]: and prepend it to the payload.
            idx = msg.rfind("]:")
            bstart = msg.rfind("[", 0, idx)
            if bstart >= 0:
                last_tag = msg[bstart:idx + 1]
                payload = msg[idx + 2:].strip(" -:|\t")
                msg = (last_tag + " " + payload).strip()
            else:
                msg = msg[idx + 2:].strip(" -:|\t")

        # Remove leading bracket-like channel tags, keeping the last one with the message.
        msg = re.sub(r'(?:<)?TIME:\d{2}:\d{2}:\d{2}(?:>)?', '', msg, flags=re.IGNORECASE)
        msg = re.sub(r'^(?:\[[^\]]+\]\s*)+(?=\[)', '', msg).strip()
        msg = re.sub(r'\s{2,}', ' ', msg).strip()

        return ts_dt, ts_hms, (msg or raw)

    def _merge_lines_into_assembled_log(self, lines: List[str], skill_name: str) -> Tuple[int, int]:
        """
        Merge new filtered lines into cumulative assembled log by timestamp.
        Storage never keeps line numbers, only timestamp + content + source skill.
        Returns: (new_added_count, total_count)
        """
        added = 0
        for line in lines:
            ts, ts_display, message = self._normalize_time_message(line)
            compact_text = f"<{ts_display}> {message}".strip()
            content_hash = hashlib.md5(compact_text.encode('utf-8')).hexdigest()
            if ts:
                ts_key = ts.strftime("%m/%d/%Y-%H:%M:%S.%f")
                key = f"{ts_key}|{content_hash}"
                if key not in self._assembled_entries_by_key:
                    self._assembled_entries_by_key[key] = {
                        "ts": ts,
                        "text": compact_text,
                        "skills": {skill_name},
                    }
                    added += 1
                else:
                    self._assembled_entries_by_key[key]["skills"].add(skill_name)
            else:
                key = content_hash
                if key not in self._assembled_entries_no_ts:
                    self._assembled_entries_no_ts[key] = {
                        "ts": None,
                        "text": compact_text,
                        "skills": {skill_name},
                    }
                    added += 1
                else:
                    self._assembled_entries_no_ts[key]["skills"].add(skill_name)

        with_ts = sorted(
            self._assembled_entries_by_key.values(),
            key=lambda x: (x["ts"], x["text"])
        )
        no_ts = sorted(
            self._assembled_entries_no_ts.values(),
            key=lambda x: x["text"]
        )
        assembled_lines = [e["text"] for e in with_ts] + [e["text"] for e in no_ts]
        self._assembled_log_text = "\n".join(assembled_lines)
        return added, len(assembled_lines)

    def _build_assembled_log_report(self, trigger_skill: str, new_added: int,
                                    apply_limits: bool = True) -> str:
        """Build assembled-log text for reasoning (limited) or file export (full)."""
        ts_values = [x["ts"] for x in self._assembled_entries_by_key.values() if x.get("ts")]
        first_ts = min(ts_values).strftime('%m/%d/%Y %H:%M:%S') if ts_values else "N/A"
        last_ts = max(ts_values).strftime('%m/%d/%Y %H:%M:%S') if ts_values else "N/A"
        skills_seen = sorted(self._filter_cache_by_skill.keys())

        header = [
            f"Assembled from {len(skills_seen)} skill filter(s): {', '.join(skills_seen) if skills_seen else trigger_skill}",
            f"Trigger skill: {trigger_skill}",
            f"New merged lines this round: {new_added}",
            f"Total assembled lines: {len(self._assembled_log_text.splitlines()) if self._assembled_log_text else 0}",
            f"Time range: {first_ts} → {last_ts}",
            "Storage rule: only timestamp + message are persisted in cache.",
        ]

        body = self._assembled_log_text
        if apply_limits:
            all_lines = self._assembled_log_text.splitlines() if self._assembled_log_text else []
            max_lines = self.MAX_ASSEMBLED_LOG_LINES_PER_TOOL_CALL
            if len(all_lines) > max_lines:
                head_n = max_lines // 2
                tail_n = max_lines - head_n
                kept_lines = all_lines[:head_n] + [
                    f"... ({len(all_lines) - max_lines} lines omitted for token safety) ..."
                ] + all_lines[-tail_n:]
                body = "\n".join(kept_lines)
                header.append(
                    f"⚠ Lines windowed: showing {max_lines}/{len(all_lines)} lines for token safety."
                )

            if len(body) > self.MAX_ASSEMBLED_LOG_CHARS_PER_TOOL_CALL:
                body = body[:self.MAX_ASSEMBLED_LOG_CHARS_PER_TOOL_CALL]
                header.append(
                    f"⚠ Output truncated at {self.MAX_ASSEMBLED_LOG_CHARS_PER_TOOL_CALL} chars for token safety."
                )

        return "[" + " | ".join(header) + "]\n" + body

    def _build_skill_focus_payload(self, skill_name: str, new_added: int, total_count: int) -> str:
        """
        Build a compact, token-efficient payload for current skill analysis.
        Includes only this skill's filtered messages + a tiny cross-skill history glimpse.
        """
        cached = self._filter_cache_by_skill.get(skill_name, {})
        lines = list(cached.get("lines", []) or [])
        line_count = len(lines)

        # Keep head+tail to preserve both early and latest evidence in this skill.
        focus_lines = lines
        if line_count > self.MAX_SKILL_FOCUS_LINES:
            head_n = self.MAX_SKILL_FOCUS_LINES // 2
            tail_n = self.MAX_SKILL_FOCUS_LINES - head_n
            focus_lines = lines[:head_n] + [
                f"... ({line_count - self.MAX_SKILL_FOCUS_LINES} lines omitted) ..."
            ] + lines[-tail_n:]

        # Small cross-skill reminder, not full assembled body.
        other_skills = [k for k in sorted(self._filter_cache_by_skill.keys()) if k != skill_name]
        recent_history = []
        for sk in other_skills[-3:]:
            sk_lines = self._filter_cache_by_skill.get(sk, {}).get("lines", []) or []
            if not sk_lines:
                continue
            sample = sk_lines[-min(5, len(sk_lines)):]
            recent_history.append(f"--- {sk} ({len(sk_lines)} lines cached) ---")
            recent_history.extend(sample)
        if len(recent_history) > self.MAX_RECENT_SKILL_HISTORY_LINES:
            recent_history = recent_history[:self.MAX_RECENT_SKILL_HISTORY_LINES]
            recent_history.append("... (recent skill history truncated) ...")

        parts = [
            f"=== Skill Focus: {skill_name} ===",
            f"Current skill matched lines: {line_count}",
            f"New lines merged this round: {new_added}",
            f"Assembled total lines (stored): {total_count}",
            "",
            "=== Current Skill Evidence (message-only compact view) ===",
            "\n".join(focus_lines) if focus_lines else "(no lines)",
        ]

        if recent_history:
            parts.extend([
                "",
                "=== Recent Cross-Skill Hints (compact) ===",
                "\n".join(recent_history),
            ])

        text = "\n".join(parts)
        if len(text) > self.MAX_SKILL_FOCUS_CHARS:
            text = text[:self.MAX_SKILL_FOCUS_CHARS] + (
                "\n... (skill-focused payload truncated for token safety)"
            )
        return text

    def get_assembled_log_snapshot(self, mode: str = "summary") -> str:
        """
        On-demand assembled-log view for macro analysis.
        mode=summary: metadata only; mode=compact: limited body; mode=full: full body.
        """
        if not (self._assembled_log_text or "").strip():
            return "No assembled log is available yet. Call fetch_filtered_logs(skill_name) first."

        mode = (mode or "summary").strip().lower()
        if mode not in ("summary", "compact", "full"):
            return "Error: mode must be one of: summary, compact, full"

        if mode == "summary":
            return self._build_assembled_log_report("snapshot", new_added=0, apply_limits=False).split("\n", 1)[0]
        if mode == "compact":
            return self._build_assembled_log_report("snapshot", new_added=0, apply_limits=True)
        return self._build_assembled_log_report("snapshot", new_added=0, apply_limits=False)

    def get_final_state_snapshot(self, tail_lines: int = 120) -> str:
        """Return a compact view focused on latest state for end-of-analysis verification."""
        lines = max(20, min(int(tail_lines or 120), 400))
        return self._get_assembled_log_tail(max_lines=lines)

    def _resolve_context_span(self, anchor_text: str, requested_span: int) -> int:
        """Auto-expand detail context for scan/connect style transactions."""
        low = (anchor_text or "").lower()
        span = requested_span if requested_span and requested_span > 0 else self.DEFAULT_DETAIL_CONTEXT_SPAN
        if any(tok in low for tok in ("scan", "connect", "assoc", "roam", "auth", "oid", "wdi_task")):
            span = max(span, 50)
        return min(span, self.MAX_DETAIL_CONTEXT_SPAN)

    def _clip_for_prompt(self, text: str, limit: int = None) -> str:
        """Trim long text before appending into LLM messages."""
        if text is None:
            return ""
        lim = limit or self.MAX_TOOL_CONTENT_CHARS_IN_MESSAGES
        if len(text) <= lim:
            return text
        return text[:lim] + f"\n... (truncated for token safety, kept {lim} chars)"

    def _get_filtered_log_lines(self, skill_name: str, apply_output_limit: bool = True) -> str:
        """
        Apply the TAT keyword filter for `skill_name` to the current log file
        using the same pipeline as LogParserService.process_analysis:
          read_log_file → extract_enabled_keywords_from_filter_file
          → filter_log_by_keywords → preprocess_log_for_llm → group_similar_logs
        Falls back to simple regex matching when no tat_path is available.
        """
        skill = self.skills.get(skill_name)
        if not skill:
            return f"Error: skill '{skill_name}' not found."
        load_err = self._ensure_raw_log_cache()
        if load_err:
            return load_err

        #  scoped lines（Segment1+Segment2）
        log_lines = self._scoped_log_lines

        # --- keyword extraction: prefer TAT file, fall back to in-memory list ---
        if skill.tat_path and Path(skill.tat_path).exists():
            keywords = extract_enabled_keywords_from_filter_file(skill.tat_path)
        else:
            keywords = skill.keywords  # fallback: keywords already loaded from TAT

        if not keywords:
            return "No keywords available for this skill."

        # --- same preprocessing steps as log_parser_service.process_analysis ---
        filtered   = filter_log_by_keywords(log_lines, keywords)
        processed  = preprocess_log_for_llm(filtered)
        grouped    = group_similar_logs(processed)

        # Optional post-filter exclusion from skills.yaml:
        # remove noisy lines that are not useful for diagnosis.
        exclusive_terms = [x for x in (skill.exclusive or []) if str(x).strip()]
        excluded_count = 0
        if grouped and exclusive_terms:
            excl_lower = [x.lower() for x in exclusive_terms]
            kept = []
            for line in grouped:
                line_str = str(line)
                # Match against both original grouped line and parsed message body.
                # If either side hits, drop the entire line.
                _, _, message_only = self._normalize_time_message(line_str)
                raw_low = self._strip_line_number_prefix(line_str).lower()
                msg_low = str(message_only).lower()
                if any((term in raw_low) or (term in msg_low) for term in excl_lower):
                    excluded_count += 1
                    continue
                kept.append(line)
            grouped = kept

        if not grouped:
            return "No log lines matched the keywords for this skill."

        # Build temporal metadata so the agent knows the time span covered
        ts_re = re.compile(r'(\d{2}/\d{2}/\d{4}-\d{2}:\d{2}:\d{2}\.\d{3})')
        first_ts = last_ts = None
        for line in grouped:
            m = ts_re.search(str(line))
            if m:
                try:
                    t = datetime.strptime(m.group(1), "%m/%d/%Y-%H:%M:%S.%f")
                    if first_ts is None or t < first_ts:
                        first_ts = t
                    if last_ts is None or t > last_ts:
                        last_ts = t
                except ValueError:
                    pass

        body_full = "\n".join(str(l) for l in grouped)
        body = body_full

        # Prepend time-range header so the agent sees whether it has
        # full-timeline coverage or only a partial window
        meta_parts = [f"Matched {len(grouped)} lines."]
        if excluded_count > 0:
            meta_parts.append(
                f"Excluded {excluded_count} lines by exclusive keywords."
            )
        if first_ts and last_ts:
            meta_parts.append(
                f"Time range: {first_ts.strftime('%m/%d/%Y %H:%M:%S')} "
                f"→ {last_ts.strftime('%m/%d/%Y %H:%M:%S')}"
            )
            if apply_output_limit and len(body_full) > 15000:
                meta_parts.append(
                    "⚠ Output truncated at 15 KB for display. "
                    "Merged assembled storage still uses full filtered lines."
                )
        header = " | ".join(meta_parts)
        if apply_output_limit and len(body) > 15000:
            body = body[:15000]
        return f"[{header}]\n{body}"

    # ------------------------------------------------------------------
    # Step 2 – Skill prompt analysis: LLM sub-call with expert_rules
    # ------------------------------------------------------------------
    def _analyze_with_skill_prompt(self, skill: Skill, filtered_log: str) -> str:
        """
        Run a focused LLM call using this skill's expert_rules as the system
        prompt and the TAT-filtered log lines as the user message.
        The case issue description (if available) is prepended to give the LLM
        additional context about what problem is being investigated.
        Returns the LLM's analysis text.
        """
        if filtered_log.startswith("Error:") or filtered_log.startswith("No log lines"):
            return filtered_log

        # Build context preamble from stored issue_context
        context_lines = []
        issue_type  = self.issue_context.get("issue_type", "")
        description = self.issue_context.get("description", "")
        subject     = self.issue_context.get("subject", "")
        case_nbr    = self.issue_context.get("case_nbr", "")
        if case_nbr:
            context_lines.append(f"Case: {case_nbr}")
        if subject:
            context_lines.append(f"Subject: {subject}")
        if issue_type:
            context_lines.append(f"Issue type: {issue_type}")
        if description:
            context_lines.append(f"\nIssue description:\n{description}")
        issue_preamble = ("=== Issue Context ===\n" + "\n".join(context_lines) + "\n\n"
                          if context_lines else "")

        messages = [
            {
                "role": "system",
                "content": skill.expert_rules,
            },
            {
                "role": "user",
                "content": (
                    f"{issue_preamble}"
                    f"=== Filtered Log (skill: {skill.name}) ===\n"
                    f"{filtered_log}"
                ),
            },
        ]
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.1,
                max_tokens=2000,
            )
            self._accumulate_turn_usage(getattr(response, "usage", None))
            return response.choices[0].message.content or ""
        except Exception as e:
            return f"Skill analysis error: {e}"

    # ------------------------------------------------------------------
    # Tool Handler: fetch_filtered_logs (returns compact skill-focused payload)
    # ------------------------------------------------------------------
    def fetch_filtered_logs(self, skill_name: str) -> str:
        """
        Fetch filtered logs for a specific skill (agentic tool).
        
        Returns compact skill-focused evidence while still merging full
        filtered lines into assembled storage.
        
        Args:
            skill_name: Skill identifier to apply filter
            
        Returns:
            str: Compact skill-focused payload for low-token reasoning
        """
        skill = self.skills.get(skill_name)
        if not skill:
            return f"Error: skill '{skill_name}' not found."
        
        if skill_name in self._filter_cache_by_skill:
            cached = self._filter_cache_by_skill[skill_name]
            export_path = self._export_assembled_log_file()
            total_count = len(self._assembled_log_text.splitlines()) if self._assembled_log_text else 0
            result = self._build_skill_focus_payload(
                skill_name=skill_name,
                new_added=0,
                total_count=total_count,
            ) + (
                "\n\n=== Cache Info ===\n"
                f"Skill cache hit: {skill_name}\n"
                f"Skill-filtered lines in cache: {cached.get('line_count', 0)}"
            )
            if export_path:
                result += f"\nSaved merged filter log: {export_path}"
            return result

        filtered_lines = self._get_filtered_log_lines(skill_name, apply_output_limit=False)

        # If filtering failed, return error as-is
        if filtered_lines.startswith("Error:") or filtered_lines.startswith("No log lines"):
            return filtered_lines

        body_lines = self._extract_lines_from_filtered_blob(filtered_lines)

        # Collapse burst-repeated lines that differ only in variable fields.
        # For each consecutive run of similar lines:
        #   - compact_lines (LLM payload): keep the first line as sample, then append
        #     a single summary "(×N similar — label: v1, v2, …)" showing only what changed.
        #   - deduped_body_lines (assembled log): keep first + last for detail storage.
        _VAR_RE = re.compile(
            r'(?:[0-9a-fA-F]{2}[:\-]){5}[0-9a-fA-F]{2}'  # MAC address
            r'|\b\d{1,3}(?:\.\d{1,3}){3}\b'               # IPv4 address
            r'|0x[0-9a-fA-F]+'                             # 0x hex literal
            r'|\b[0-9a-fA-F]{4,}\b'                        # bare hex string (4+ hex digits)
            r'|\b\d+\b'                                     # decimal integer
        )

        def _msg_pattern(msg: str) -> str:
            return _VAR_RE.sub('*', msg)

        def _variation_summary(run_lines: list) -> str:
            """Return '(×N similar — label: v1, v2, …)' for a run of similar lines."""
            msgs = [self._normalize_time_message(bl)[2] for bl in run_lines]
            all_tok = [_VAR_RE.findall(m) for m in msgs]
            if not all_tok or not all_tok[0]:
                return f"×{len(msgs)} identical lines"
            first_iters = list(_VAR_RE.finditer(msgs[0]))
            varying = []
            for pos in range(len(first_iters)):
                values = [tl[pos] for tl in all_tok if pos < len(tl)]
                if len(set(values)) <= 1:
                    continue
                # Label: last word before the token in the first message
                label = f"field{pos + 1}"
                before = msgs[0][:first_iters[pos].start()].rstrip(' \t=,(')
                lm = re.search(r'(\w+)\s*$', before)
                if lm:
                    label = lm.group(1)
                # Skip first value (already visible in the sample line); cap at 8
                rest = values[1:]
                val_str = (', '.join(rest) if len(rest) <= 8
                           else ', '.join(rest[:5]) + ', …, ' + rest[-1])
                varying.append(f"{label}: {val_str}")
            if varying:
                return f"(×{len(msgs)} similar — {'; '.join(varying)})"
            return f"(×{len(msgs)} identical lines)"

        deduped_body_lines: list = []
        compact_lines: list = []
        run_start = 0
        while run_start < len(body_lines):
            _, _, msg0 = self._normalize_time_message(body_lines[run_start])
            pat0 = _msg_pattern(msg0)
            run_end = run_start + 1
            while run_end < len(body_lines):
                _, _, msgN = self._normalize_time_message(body_lines[run_end])
                if _msg_pattern(msgN) == pat0:
                    run_end += 1
                else:
                    break
            run_len = run_end - run_start
            if run_len <= 2:
                deduped_body_lines.extend(body_lines[run_start:run_end])
                for line in body_lines[run_start:run_end]:
                    _, ts, msg = self._normalize_time_message(line)
                    compact_lines.append(f"<{ts}> {msg}".strip())
            else:
                # Assembled log: keep first + last
                deduped_body_lines.append(body_lines[run_start])
                deduped_body_lines.append(body_lines[run_end - 1])
                # Compact: sample line + single variation summary
                _, ts0, msg0_txt = self._normalize_time_message(body_lines[run_start])
                compact_lines.append(f"<{ts0}> {msg0_txt}".strip())
                compact_lines.append(f"    {_variation_summary(body_lines[run_start:run_end])}")
            run_start = run_end
        body_lines = deduped_body_lines

        self._filter_cache_by_skill[skill_name] = {
            "skill_name": skill_name,
            "line_count": len(compact_lines),
            "lines": compact_lines,
            "created_at": datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        }

        new_added, total_count = self._merge_lines_into_assembled_log(body_lines, skill_name)
        skill_focus_report = self._build_skill_focus_payload(skill_name, new_added, total_count)
        export_path = self._export_assembled_log_file()

        result = (
            f"=== {skill.name} Filter Applied ===\n"
            f"Skill matched lines: {len(body_lines)}\n"
            f"Assembled total lines after merge: {total_count}\n\n"
            f"=== Skill-Focused Reasoning Payload ===\n"
            f"{skill_focus_report}"
        )
        if export_path:
            result += f"\n\nSaved merged filter log: {export_path}"
        return result

    # ------------------------------------------------------------------
    # Utility: Quick skill-based analysis (optional one-shot mode)
    # ------------------------------------------------------------------
    def analyze_with_skill(self, skill_name: str) -> str:
        """
        Quick one-shot skill analysis: filter logs + analyze with expert rules.
        
        Unlike agentic chat, this directly applies skill expertise without
        multi-step reasoning. Useful for focused, quick analysis when you want
        the skill's expertise applied directly without agent reasoning.
        
        Args:
            skill_name: Which skill's expertise to apply
            
        Returns:
            str: Skill expert's analysis of the filtered logs
            
        Example:
            >>> analysis = agent.analyze_with_skill("Connectivity")
            >>> print(analysis)
            # "Based on the filtered Connectivity logs, the issue appears to be..."
        """
        skill = self.skills.get(skill_name)
        if not skill:
            return f"Error: skill '{skill_name}' not found."
        
        filtered_lines = self._get_filtered_log_lines(skill_name)
        return self._analyze_with_skill_prompt(skill, filtered_lines)

    # ------------------------------------------------------------------
    # Tool: query_log_detail — anchor-based context query on assembled log
    # ------------------------------------------------------------------
    def query_log_detail(self, anchor_text: str = "", anchor_timestamp: str = "",
                         context_span: int = 20, max_hits: int = 3) -> str:
        """
        Query detailed assembled-log context by semantic anchors instead of line numbers.

        Matching strategy:
          - If anchor_timestamp is provided, match lines containing that timestamp
            text (supports exact fragment match).
          - If anchor_text is provided, match lines containing that text
            (case-insensitive).
          - If both are provided, both conditions must match.

        Returns neighboring context around up to `max_hits` anchor matches.
        Source is assembled log only (not raw log).
        """
        anchor_text = (anchor_text or "").strip()
        anchor_timestamp = (anchor_timestamp or "").strip()
        context_span = self._resolve_context_span(anchor_text, context_span)

        if not anchor_text and not anchor_timestamp:
            return "Error: Provide anchor_text and/or anchor_timestamp for detail query."

        assembled_text = self._assembled_log_text or ""
        if not assembled_text.strip():
            return (
                "No assembled log is available yet. "
                "Call fetch_filtered_logs(skill_name) first."
            )

        assembled_sig = hashlib.md5(assembled_text.encode('utf-8')).hexdigest()[:12]
        cache_key = (
            f"text={anchor_text.lower()}|ts={anchor_timestamp}|"
            f"span={context_span}|hits={max_hits}|assembled={assembled_sig}"
        )
        dedup_key = (
            f"text={anchor_text.lower()}|ts={anchor_timestamp}|"
            f"span={context_span}|hits={max_hits}|assembled={assembled_sig}"
        )

        if dedup_key in self._detail_query_seen and cache_key in self._detail_cache:
            cached = self._detail_cache[cache_key]
            first_line = cached.splitlines()[0] if cached else "(no detail)"
            return (
                "Duplicate query skipped: same anchor parameters were already queried on current assembled snapshot.\n"
                f"Previous detail summary: {first_line}\n"
                "Use a different anchor_text/anchor_timestamp or broader snapshot query for new evidence.\n\n"
                "[Detail dedup cache hit]"
            )

        if cache_key in self._detail_cache:
            return self._detail_cache[cache_key] + "\n\n[Detail cache hit]"

        all_lines = assembled_text.splitlines()

        matched_indices = []
        lower_anchor = anchor_text.lower()
        normalized_ts = anchor_timestamp.strip("<>").strip()
        ts_token = f"<{normalized_ts}>" if normalized_ts else ""

        for idx, raw in enumerate(all_lines):
            line = str(raw)
            line_lower = line.lower()
            ts_ok = (
                (not normalized_ts)
                or (normalized_ts in line)
                or (ts_token and ts_token in line)
            )
            txt_ok = (not lower_anchor) or (lower_anchor in line_lower)
            if ts_ok and txt_ok:
                matched_indices.append(idx)

        # HEAD+TAIL sampling: always include earliest AND latest matches
        # to prevent blindspot where only early errors are seen.
        if len(matched_indices) > max_hits:
            head_count = max(1, max_hits // 3)       # ~1/3 from beginning
            tail_count = max_hits - head_count        # ~2/3 from end
            matched_indices = (
                matched_indices[:head_count]
                + matched_indices[-tail_count:]
            )
        elif len(matched_indices) > 0:
            pass  # use all matches as-is

        if not matched_indices:
            return (
                "No matching anchor found in assembled log. "
                f"anchor_text='{anchor_text}', anchor_timestamp='{anchor_timestamp}'."
            )

        sections = []
        for hit_no, idx in enumerate(matched_indices, start=1):
            start_idx = max(0, idx - context_span)
            end_idx = min(len(all_lines), idx + context_span + 1)

            block = []
            for i in range(start_idx, end_idx):
                marker = ">>>" if i == idx else "   "
                block.append(f"{marker} {str(all_lines[i])}")

            sections.append(
                f"=== Detail Hit {hit_no}/{len(matched_indices)} ===\n" + "\n".join(block)
            )

        detail_text = "\n\n".join(sections)
        if len(detail_text) > self.MAX_QUERY_DETAIL_OUTPUT_CHARS:
            detail_text = (
                detail_text[:self.MAX_QUERY_DETAIL_OUTPUT_CHARS]
                + "\n... (detail truncated for token safety)"
            )
        self._detail_cache[cache_key] = detail_text
        self._detail_query_seen.add(dedup_key)
        return detail_text

    # ------------------------------------------------------------------
    # Chat: Flexible conversation with optional agentic tools
    # RECOMMENDED for chatbot dialog boxes
    # ------------------------------------------------------------------
    def chat(self, user_message: str, use_tools: bool = False, max_steps: int = 6,
             temperature: float = 0.2, max_tokens: int = 4000, step_callback=None) -> dict:
        """
        Process user message with flexible LLM call - simple or agentic mode.
        
        Two modes available:
          
          MODE 1: Simple Conversation (use_tools=False, DEFAULT)
            - Direct LLM call, no tools
            - Perfect for free-form Q&A chatbot
            - Faster, fewer tokens
          
          MODE 2: Agentic Reasoning (use_tools=True)
            - LLM can call diagnostic tools
            - Autonomous skill selection and investigation
            - For complex root-cause analysis
        
        Args:
            user_message: User's question or statement
            use_tools: Enable agentic tool mode (default False for simple chat)
            max_steps: Max reasoning iterations when use_tools=True (default 6)
            temperature: Sampling temperature for response generation
            max_tokens: Maximum tokens for direct/simple response generation
            
        Returns:
            dict: {
                "type": "text" | "report" | "error",
                "data": str or dict depending on mode
            }
            
        Examples:
            # Simple chatbot (default, no tools)
            >>> result = agent.chat("What errors are in the log?")
            >>> print(result["data"])  # Direct answer
            
            # With tools for diagnosis
            >>> result = agent.chat(
            ...     "Why does device disconnect?",
            ...     use_tools=True
            ... )
            >>> # Agent may call fetch_filtered_logs, query_log_detail, etc.
        """
        try:
            temperature = float(temperature)
        except Exception:
            temperature = 0.2
        temperature = max(0.0, min(1.0, temperature))

        try:
            max_tokens = int(max_tokens)
        except Exception:
            max_tokens = 4000
        max_tokens = max(256, min(8000, max_tokens))

        # Fresh turn — discard any stop signal left over from a previous turn
        # so the user's new message is never pre-cancelled.
        self.cancel_event.clear()
        # Fresh turn — token counters describe THIS turn only.
        self._reset_turn_usage()

        # Delegate to appropriate implementation
        if use_tools:
            return self._chat_with_tools(user_message, max_steps, temperature=temperature, step_callback=step_callback)
        else:
            return self._chat_simple(user_message, temperature=temperature, max_tokens=max_tokens)

    @staticmethod
    def _empty_turn_usage() -> dict:
        return {
            "llm_calls": 0,
            "input_tokens": 0,        # uncached prompt tokens
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }

    def _reset_turn_usage(self) -> None:
        self.last_turn_usage = self._empty_turn_usage()

    def _accumulate_turn_usage(self, usage) -> None:
        """
        Fold one LLM response's usage into this turn's running total.

        Tolerates a missing/partial usage object — accounting must never be the
        thing that breaks a chat turn, so anything unreadable is counted as 0.
        """
        if usage is None:
            return
        try:
            u = self.last_turn_usage
            if not isinstance(u, dict):
                u = self.last_turn_usage = self._empty_turn_usage()

            def _n(name: str) -> int:
                try:
                    return max(0, int(getattr(usage, name, 0) or 0))
                except (TypeError, ValueError):
                    return 0

            prompt = _n("prompt_tokens")
            completion = _n("completion_tokens")
            # Anthropic naming on our adapter; OpenAI responses simply lack these.
            cache_read = _n("cache_read_input_tokens")
            cache_write = _n("cache_creation_input_tokens")

            u["llm_calls"] += 1
            u["input_tokens"] += prompt
            u["output_tokens"] += completion
            u["cache_read_tokens"] += cache_read
            u["cache_write_tokens"] += cache_write
            u["total_tokens"] += prompt + completion + cache_read + cache_write
        except Exception as e:
            print(f"[TOKEN] usage accumulation skipped: {e}")

    def _chat_simple(self, user_message: str, temperature: float = 0.2,
                     max_tokens: int = 4000) -> dict:
        """
        Simple chat mode: Direct conversation without tools.
        
        Perfect for chatbot UI where users expect immediate, conversational responses.
        """
        if not self.conversation_history:
            # Initialize system message with context on first turn
            log_snippet = ""
            if self.current_log_path:
                try:
                    from utils.helpers import read_log_file
                    lines = read_log_file(self.current_log_path)
                    log_snippet = "\n".join(str(l) for l in lines[:500])
                except Exception:
                    log_snippet = "(unable to read log file)"

            # Build comprehensive system message
            system_msg = (
                "You are a Wi-Fi Troubleshooting Assistant.\n"
                "Answer user questions about the log file concisely and accurately.\n"
            )
            
            # Add case context if available
            if self.issue_context:
                ctx_parts = []
                if self.issue_context.get("case_nbr"):
                    ctx_parts.append(f"Case #: {self.issue_context['case_nbr']}")
                if self.issue_context.get("issue_type"):
                    ctx_parts.append(f"Issue Type: {self.issue_context['issue_type']}")
                if self.issue_context.get("subject"):
                    ctx_parts.append(f"Subject: {self.issue_context['subject']}")
                if self.issue_context.get("description"):
                    ctx_parts.append(f"Description: {self.issue_context['description']}")
                
                if ctx_parts:
                    system_msg += "\n=== CASE CONTEXT ===\n" + "\n".join(ctx_parts) + "\n\n"
            
            # Add log file reference
            if self.current_log_path:
                system_msg += f"Log file: {self.current_log_path}\n"
            
            # Add log snippet for reference
            if log_snippet:
                system_msg += f"\n=== Log Excerpt (first 500 lines) ===\n{log_snippet}\n"

            self.conversation_history.append({
                "role": "system",
                "content": system_msg,
            })

        # Add user message to history
        self.conversation_history.append({"role": "user", "content": user_message})

        try:
            # Simple LLM call (no tools)
            response = self.client.chat.completions.create(
                model=self.model,
                messages=self.conversation_history,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            self._accumulate_turn_usage(getattr(response, "usage", None))
            content = response.choices[0].message.content or ""

            # Add assistant response to history
            self.conversation_history.append({"role": "assistant", "content": content})

            return {"type": "text", "data": content}
        except Exception as e:
            error_msg = f"Chat error: {str(e)}"
            print(f"[ERROR] {error_msg}")
            return {"type": "text", "data": error_msg}

    @staticmethod
    def _msg_role(m):
        return m.get("role") if isinstance(m, dict) else getattr(m, "role", None)

    @staticmethod
    def _msg_tool_call_ids(m):
        """Return the list of tool_call ids on an assistant message
        (works for both plain dicts and the OpenAI SDK message object)."""
        tcs = m.get("tool_calls") if isinstance(m, dict) else getattr(m, "tool_calls", None)
        ids = []
        for tc in (tcs or []):
            tid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
            if tid:
                ids.append(tid)
        return ids

    @staticmethod
    def _msg_tool_call_id(m):
        """tool_call_id of a role:\"tool\" message (dict or SDK object)."""
        return m.get("tool_call_id") if isinstance(m, dict) else getattr(m, "tool_call_id", None)

    def _repair_tool_use_consistency(self) -> None:
        """Rebuild conversation_history so every assistant ``tool_use`` is
        immediately followed by ``tool_result`` message(s) answering ALL of its
        ids — the invariant the (Claude-backed) API enforces.

        It drops only the broken parts, keeping valid context intact:
          * an assistant message whose tool_calls are NOT all answered by the
            tool messages right after it — dropped together with those partial
            tool results;
          * a stray ``role:"tool"`` message with no owning assistant tool_use.

        Called right before every LLM request, so an orphan from ANY source —
        an aborted prior turn, a per-step token-limit bail-out, a mid-history
        injection (e.g. after editing a skill) — can never reach the API and
        trigger a 400 ("tool_use ids ... without tool_result blocks ...").
        """
        hist = self.conversation_history or []
        n = len(hist)
        out = []
        i = 0
        dropped = 0
        while i < n:
            m = hist[i]
            # Identify a tool-use message by its tool_calls, NOT by role: the
            # raw assistant SDK message object does not reliably expose `.role`
            # to our helpers (it can read back as None). Classifying by role
            # would miss it and then wrongly drop its valid tool_results.
            call_ids = set(self._msg_tool_call_ids(m))
            if call_ids:
                # Consume the immediately-following run of tool results and keep
                # ONLY those that match THIS message's ids (exactly once each);
                # extras / duplicates / mismatched ids are dropped.
                j = i + 1
                matched = []
                seen = set()
                while j < n and self._msg_role(hist[j]) == "tool":
                    tid = self._msg_tool_call_id(hist[j])
                    if tid in call_ids and tid not in seen:
                        matched.append(hist[j])
                        seen.add(tid)
                    else:
                        dropped += 1   # extra / duplicate / mismatched tool result
                    j += 1
                if seen == call_ids:
                    out.append(m)
                    out.extend(matched)
                else:
                    # Not every tool_use was answered → drop the message AND its
                    # partial tool results (can't send an unanswered tool_use).
                    dropped += 1 + len(matched)
                i = j
                continue
            if self._msg_role(m) == "tool":
                # A tool result not consumed by a tool-use run above = orphan.
                dropped += 1
                i += 1
                continue
            out.append(m)
            i += 1
        if dropped:
            print(f"[chat] 🧹 Repaired tool_use/tool_result consistency — "
                  f"dropped {dropped} orphan/stray message(s) before sending.")
            self.conversation_history = out

    # Budget for a persisted conversation context. Every restored message is
    # re-sent on every follow-up, so this is a recurring token cost, not a
    # one-off disk cost: 120k chars is roughly 30k tokens of grounding.
    MAX_PERSISTED_CONTEXT_CHARS = 120_000

    @staticmethod
    def _plain_message(m) -> Optional[dict]:
        """Flatten one history entry into a plain, JSON-safe message dict.

        ``conversation_history`` holds a mix of dicts we appended ourselves and
        raw SDK message objects straight off the response, so this reads both
        shapes through attribute-or-key access and keeps only the fields the
        API round-trips: the tool_calls ids and arguments especially, since
        those are what pair an assistant tool_use with its tool results.
        """
        def field(key):
            return m.get(key) if isinstance(m, dict) else getattr(m, key, None)

        role = field("role")
        tool_calls = field("tool_calls") or []
        if not role:
            # A raw assistant SDK object can read back with no role; if it
            # carries tool_calls it is an assistant turn by construction.
            role = "assistant" if tool_calls else None
        if not role:
            return None

        out: dict = {"role": role, "content": field("content")}
        calls = []
        for tc in tool_calls:
            def sub(obj, key):
                return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)
            fn = sub(tc, "function")
            call_id = sub(tc, "id")
            name = sub(fn, "name") if fn is not None else None
            arguments = sub(fn, "arguments") if fn is not None else None
            if not call_id or not name:
                continue
            calls.append({
                "id": call_id,
                "type": sub(tc, "type") or "function",
                "function": {"name": name, "arguments": arguments or "{}"},
            })
        if calls:
            out["tool_calls"] = calls
        tool_call_id = field("tool_call_id")
        if tool_call_id:
            out["tool_call_id"] = tool_call_id
            name = field("name")
            if name:
                out["name"] = name
        # An assistant message with tool_calls legitimately has no content;
        # anything else with neither content nor tool_calls carries nothing.
        if out["content"] is None and "tool_calls" not in out:
            return None
        return out

    def export_conversation_context(self, max_chars: Optional[int] = None) -> list[dict]:
        """Snapshot the model-facing conversation so it can be resumed later.

        This is NOT the UI trace — it is what actually goes to the API on the
        next request: the primed case context, the questions, the assistant
        turns and the tool results they were grounded in.

        Trimmed to a character budget from the OLDEST end, and only ever at a
        group boundary: an assistant tool_use and the tool results answering it
        are kept or dropped together, so a restored context can never open with
        an orphan. The head — the priming message the conversation opened with
        — is always kept; it is small and it is what tells the model which case
        this is.
        """
        budget = self.MAX_PERSISTED_CONTEXT_CHARS if max_chars is None else max_chars
        history = self.conversation_history or []

        # Walk into groups: [assistant-with-tool_calls + its tool results] or
        # [single message]. Grouping mirrors _repair_tool_use_consistency so
        # the two agree on what a severable unit is.
        groups: list[list[dict]] = []
        i, n = 0, len(history)
        while i < n:
            plain = self._plain_message(history[i])
            call_ids = set(self._msg_tool_call_ids(history[i]))
            if call_ids and plain is not None:
                group = [plain]
                j = i + 1
                while j < n and self._msg_role(history[j]) == "tool":
                    tool_plain = self._plain_message(history[j])
                    if tool_plain is not None:
                        group.append(tool_plain)
                    j += 1
                groups.append(group)
                i = j
                continue
            if plain is not None and self._msg_role(history[i]) != "tool":
                groups.append([plain])
            i += 1

        if not groups:
            return []

        def size(group: list[dict]) -> int:
            try:
                return len(json.dumps(group, ensure_ascii=False, default=str))
            except Exception:
                return sum(len(str(msg)) for msg in group)

        head, tail = groups[0], groups[1:]
        total = size(head)
        kept: list[list[dict]] = []
        # Newest-first so the most recent evidence is what survives the budget.
        for group in reversed(tail):
            group_size = size(group)
            if total + group_size > budget:
                break
            kept.append(group)
            total += group_size
        kept.reverse()

        out: list[dict] = list(head)
        for group in kept:
            out.extend(group)
        return out

    def import_conversation_context(self, messages: Any) -> int:
        """Restore a context produced by ``export_conversation_context``.

        Returns how many messages were adopted (0 when there was nothing
        usable, so the caller can fall back to rebuilding from result text).
        The restored list is run through the same repair pass every request
        gets, so a snapshot that was truncated or hand-edited into an invalid
        state degrades to a smaller valid context instead of a 400.
        """
        if not isinstance(messages, list):
            return 0
        restored = [m for m in messages if isinstance(m, dict) and m.get("role")]
        if not restored:
            return 0
        self.conversation_history = restored
        try:
            self._repair_tool_use_consistency()
        except Exception as e:
            print(f"[chat] restored context repair failed: {e}")
        return len(self.conversation_history)

    def _history_skeleton(self) -> str:
        """Compact one-line-per-message view of conversation_history showing
        index + role + tool id(s). Dumped when an LLM request fails so a
        tool_use/tool_result mismatch can be pinpointed by message index and id
        (e.g. the API's "messages.N: tool_use ids ... without tool_result")."""
        lines = []
        for idx, m in enumerate(self.conversation_history or []):
            role = self._msg_role(m)
            ids = self._msg_tool_call_ids(m)
            tcid = self._msg_tool_call_id(m)
            if ids:  # tool-use message (classify by tool_calls, role may be None)
                lines.append(f"  [{idx}] {role or 'assistant?'} tool_use={[s[:12] for s in ids]}")
            elif tcid:
                lines.append(f"  [{idx}] {role or 'tool?'} tool_result={tcid[:12]}")
            else:
                lines.append(f"  [{idx}] {role}")
        return "\n".join(lines) if lines else "  (empty)"

    def _chat_with_tools(self, user_message: str, max_steps: int = 6,
                         temperature: float = 0.1, step_callback=None) -> dict:
        """
        Agentic chat mode: LLM can use diagnostic tools.
        
        For complex analysis where agent needs to investigate multiple skills,
        inspect specific log sections, and provide structured diagnoses.

        This method detects follow-up turns (conversation_history already has
        messages) and appends the new user message so the LLM can continue
        investigating with full context of prior analysis.
        """
        def _emit(step):
            if step_callback:
                step_callback(step)

        tools = self._build_tools()
        final_report = None

        # Self-heal: surgically drop any orphan tool_use/tool_result left by a
        # prior aborted turn (LLM error, tool-arg JSON parse failure, per-step
        # token-limit bail-out, etc.) so the API can't 400 on it. Keeps valid
        # context — unlike a full conversation reset.
        self._repair_tool_use_consistency()

        # Detect first user turn: prime_with_context may have added a system
        # message but no user message yet — treat that as first turn so full
        # initialization (system prompt rebuild, issue-time extraction, log
        # preprocessing) still runs.
        _first_user_turn = not any(
            (isinstance(m, dict) and m.get("role") == "user")
            for m in self.conversation_history
        )
        if not _first_user_turn:
            max_steps = min(max_steps, 4)

        if _first_user_turn:
            # First user turn — rebuild system prompt for agentic mode
            # (replaces any simpler prompt from prime_with_context).
            self.conversation_history = []

            context_section = ""
            if self.issue_context:
                context_parts = []
                if self.issue_context.get("case_nbr"):
                    context_parts.append(f"**Case Number:** {self.issue_context.get('case_nbr')}")
                if self.issue_context.get("issue_type"):
                    context_parts.append(f"**Issue Type:** {self.issue_context.get('issue_type')}")
                if self.issue_context.get("subject"):
                    context_parts.append(f"**Subject:** {self.issue_context.get('subject')}")
                if context_parts:
                    context_section = "\n=== BACKGROUND CONTEXT ===\n" + "\n".join(context_parts) + "\n\n"

            system_content = self._build_analyze_system_prompt(context_section)

            self.conversation_history.append({
                "role": "system",
                "content": system_content,
            })

            # Surface the ACE workflow playbook that was injected into the
            # system prompt, so users can see exactly which learned rules are
            # steering the agent on this turn.
            if self.ace_runner is not None:
                try:
                    _wf_text = self.ace_runner.render_workflow()
                except Exception as _e:
                    _wf_text = ""
                    print(f"[ace] render_workflow (ui emit) failed: {_e}")
                if _wf_text and _wf_text.strip() not in ("", "(empty playbook)"):
                    _emit({
                        "role": "agent",
                        "content": (
                            "🧠 **ACE Workflow Playbook injected** "
                            "(orchestration rules learned from past cases)\n\n"
                            f"```\n{_wf_text}\n```"
                        ),
                    })

            self.conversation_history.append({"role": "user", "content": user_message})

            # --- Issue time extraction ---
            # By the time we get here `self.issue_time` is normally already
            # set: prime_with_context resolved it from attachment_time (and
            # fell back to the log's latest timestamp if needed), and the
            # /chat route may have overridden it with the sidebar value.
            # Only run the LLM-based extractor if everything upstream came
            # back empty — and try the user's message first.
            if self.issue_time:
                time_source = "primed"
            else:
                self.issue_time = self._extract_issue_time(user_message)
                time_source = "user_message"

            if self.issue_time:
                # Add a "customer wall clock" annotation when the issue time
                # frame detection produced a different customer-side value
                # (typical for non-Asia customers — the log shows GMT+8, the
                # customer's screenshot shows their own clock). prime_with_
                # context stamps these on self when it resolves the frames.
                customer_dt = getattr(self, "issue_time_customer", None)
                customer_tz = (getattr(self, "issue_time_tz", "") or "").strip()
                extracted = self.issue_time.strftime('%m/%d/%Y %H:%M:%S')
                has_customer = bool(customer_dt and customer_tz
                                    and customer_dt != self.issue_time)
                if has_customer:
                    # Two clean lines, no nested parentheses: the log-frame
                    # value (what we scan) on top, the customer wall clock below.
                    content = (
                        f"🕒 **Issue Time Extracted:** `{extracted}` — ETL decode-host time (GMT+8)\n"
                        f"👤 **Customer wall clock:** "
                        f"`{customer_dt.strftime('%m/%d/%Y %H:%M:%S')}` — {customer_tz}\n"
                        f"Agent will look for events around this timestamp in filtered logs. "
                        f"_(source: {time_source})_"
                    )
                else:
                    content = (
                        f"🕒 **Issue Time Extracted:** `{extracted}`\n"
                        f"Agent will look for events around this timestamp in filtered logs. "
                        f"_(source: {time_source})_"
                    )
                _emit({"role": "agent", "content": content})

            # --- Raw log preprocessing (scope-narrowing) ---
            load_err = self._ensure_raw_log_cache()
            if load_err:
                _emit({"role": "error", "content": f"❌ Raw log load failed: {load_err}"})
            else:
                self._preprocess_raw_log_context()
                pre_msg_parts = [
                    f"- **Segment1 — Driver init block:** {len(self._driver_init_lines)} lines "
                    f"(driver load occurrences: {self._driver_init_count})",
                    f"- **Segment2 — Event window:** {len(self._issue_time_window_lines)} lines",
                ]
                if self._scoped_log_lines:
                    filter_scope = (
                        f"{len(self._scoped_log_lines)} lines (scoped: Segment1 + issue-time window)"
                        if self.issue_time
                        else f"{len(self._raw_log_cache)} lines (full raw log — no issue time)"
                    )
                    pre_msg_parts.append(f"- **Skill filter input:** {filter_scope}")
                _emit({
                    "role": "agent",
                    "content": "🔍 **Pre-Analysis Scan Complete**\n" + "\n".join(pre_msg_parts),
                })

                if self._scoped_log_lines:
                    seg2_scope = (
                        f"±5 min of issue time {self.issue_time.strftime('%m/%d/%Y %H:%M:%S')}"
                        if self.issue_time
                        else "Segment1 end → EOF"
                    )
                    self.conversation_history.append({
                        "role": "user",
                        "content": (
                            "[PRE-SCAN INFO]\n"
                            f"Skill filtering scope has been narrowed to {len(self._scoped_log_lines)} lines "
                            f"(full raw log: {len(self._raw_log_cache)} lines).\n"
                            f"Segment1 (driver init): {len(self._driver_init_lines)} lines | "
                            f"driver load occurrences: {self._driver_init_count}\n"
                            f"Segment2 ({seg2_scope}): {len(self._issue_time_window_lines)} lines"
                        ),
                    })
        else:
            # Follow-up turn — history already has prior analysis context.
            # Assembled-log caches are still warm so all tools work as normal.
            self.conversation_history.append({"role": "user", "content": user_message})

        # Per-call tracking
        skill_call_counts: dict = {}
        no_match_anchor_counts: dict = {}
        detail_call_counts: dict = {}
        no_progress_rounds: int = 0
        step_token_usages: list = []

        # Sonnet pricing: $3.00/MTok input, $15.00/MTok output
        _INPUT_COST_PER_TOKEN  = 3.00  / 1_000_000
        _OUTPUT_COST_PER_TOKEN = 15.00 / 1_000_000

        def _emit_token_report():
            if not step_token_usages:
                return
            rows = [
                "📊 **Token Usage & Cost Report** *(Standard Sonnet — $3.00/MTok in · $15.00/MTok out)*\n",
                "| Step | Input(tok) | Output(tok) | Total(tok) | Input Cost ($) | Output Cost ($) | Step Cost ($) |",
                "|------|--------|------------|-------|----------------|-----------------|---------------|",
            ]
            total_p = total_c = total_t = 0
            total_cost_in = total_cost_out = 0.0
            for s in step_token_usages:
                cost_in  = s["prompt"]     * _INPUT_COST_PER_TOKEN
                cost_out = s["completion"] * _OUTPUT_COST_PER_TOKEN
                cost_step = cost_in + cost_out
                rows.append(
                    f"| {s['step']} | {s['prompt']:,} | {s['completion']:,} | {s['total']:,} "
                    f"| {cost_in:.4f} | {cost_out:.4f} | {cost_step:.4f} |"
                )
                total_p += s["prompt"]; total_c += s["completion"]; total_t += s["total"]
                total_cost_in += cost_in; total_cost_out += cost_out
            total_cost = total_cost_in + total_cost_out
            rows.append(
                f"| **Total** | **{total_p:,}** | **{total_c:,}** | **{total_t:,}** "
                f"| **{total_cost_in:.4f}** | **{total_cost_out:.4f}** | **{total_cost:.4f}** |"
            )
            rows.append(f"\n💰 **Estimated total cost: ${total_cost:.4f}**")
            _emit({"role": "token_usage", "content": "\n".join(rows)})

        for step_idx in range(max_steps):
            pending_user_nudges: list = []

            # Cooperative stop: the user clicked "Stop" and chat_jobs set our
            # cancel_event. Bail out cleanly BEFORE spending another LLM call —
            # emit the token report and return a short notice.
            if self.cancel_event.is_set():
                _emit({"role": "agent", "content": "⏹️ **Stopped by user.** Analysis halted before completion."})
                _emit_token_report()
                return {"type": "text", "data": "⏹️ Analysis stopped by user."}

            _emit({"role": "agent", "content": f"💭 **Reasoning Step {step_idx + 1}/{max_steps}** — Thinking..."})
            # Force-conclude pressure in final steps
            if step_idx >= max_steps - self.FORCE_CONCLUDE_LAST_N_STEPS:
                pending_user_nudges.append({
                    "role": "user",
                    "content": (
                        "You are in the final steps. Stop gathering new evidence and call "
                        "submit_final_report now using current evidence. If uncertain, state "
                        "uncertainties explicitly in the report."
                    ),
                })

            # Guarantee the request never carries an orphan tool_use/tool_result
            # (created this turn or a prior one) — the #1 cause of the API's
            # "tool_use ids ... without tool_result blocks" 400.
            self._repair_tool_use_consistency()
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=self.conversation_history,
                    tools=tools,
                    tool_choice="auto",
                    temperature=temperature,
                    max_tokens=4096,
                )
            except Exception as e:
                error_msg = f"LLM API error at reasoning step {step_idx}: {str(e)}"
                print(f"[ERROR] {error_msg}")
                # Dump the role + tool-id skeleton so a tool_use/tool_result
                # mismatch can be pinpointed by message index + id.
                print(f"[chat] 🧬 conversation_history skeleton at failure "
                      f"({len(self.conversation_history)} msgs):\n{self._history_skeleton()}")
                return {"type": "error", "data": error_msg}

            message = response.choices[0].message
            self.conversation_history.append(message)

            # Token usage accounting
            usage = getattr(response, 'usage', None)
            if usage:
                print(
                    f"[TOKEN] Chat step {step_idx + 1}: "
                    f"prompt={usage.prompt_tokens} "
                    f"completion={usage.completion_tokens} "
                    f"total={usage.total_tokens}"
                )
                # _emit({
                #     "role": "token_usage",
                #     "content": (
                #         f"📊 **Token Usage (Step {step_idx + 1}):** "
                #         f"Prompt: {usage.prompt_tokens} | "
                #         f"Completion: {usage.completion_tokens} | "
                #         f"Total: {usage.total_tokens}"
                #     ),
                # })
                step_token_usages.append({
                    "step": step_idx + 1,
                    "prompt": usage.prompt_tokens,
                    "completion": usage.completion_tokens,
                    "total": usage.total_tokens,
                })
                self._accumulate_turn_usage(usage)
                if usage.total_tokens > self.MAX_TOKENS_PER_STEP:
                    _emit({
                        "role": "error",
                        "content": (
                            f"🛑 **Stopped:** per-step token limit exceeded at step {step_idx + 1}. "
                            f"total_tokens={usage.total_tokens}, limit={self.MAX_TOKENS_PER_STEP}."
                        ),
                    })
                    _emit_token_report()
                    return {
                        "type": "partial_report",
                        "issue_time": (
                            self.issue_time.strftime('%m/%d/%Y %H:%M:%S')
                            if self.issue_time else None
                        ),
                        "data": {
                            "root_cause_summary": "Analysis stopped due to per-step token limit.",
                            "confidence_score": 20,
                            "recommended_actions": [
                                "Narrow the question scope",
                                "Use simple mode for broad questions",
                            ],
                            "involved_skills": [],
                            "markdown_summary": (
                                "## Partial Result\n"
                                "Analysis stopped because a single reasoning step exceeded the token limit."
                            ),
                        },
                    }
                elif usage.total_tokens > int(self.MAX_TOKENS_PER_STEP * 0.85):
                    pending_user_nudges.append({
                        "role": "user",
                        "content": (
                            "Token budget is getting tight. "
                            "Avoid broad new searches; use current evidence and submit_final_report soon."
                        ),
                    })

            if message.content:
                _emit({"role": "agent", "content": f"🧠 **Thinking:**\n{message.content[:500]}"})

            if message.tool_calls:
                final_report = None
                original_tool_calls = list(message.tool_calls)

                # Tool fan-out cap (tighter in final steps)
                max_calls_this_step = self.MAX_TOOL_CALLS_PER_STEP
                if step_idx >= max_steps - self.FORCE_CONCLUDE_LAST_N_STEPS:
                    max_calls_this_step = 1
                if len(original_tool_calls) > max_calls_this_step:
                    _emit({
                        "role": "agent",
                        "content": (
                            f"🧭 **Tool cap applied:** executing {max_calls_this_step}/"
                            f"{len(original_tool_calls)} tool calls this step."
                        ),
                    })
                else:
                    _emit({"role": "agent", "content": f"🧭 **Tool calls:** {len(original_tool_calls)} this step."})

                tool_calls = original_tool_calls[:max_calls_this_step]
                skipped_tool_calls = original_tool_calls[max_calls_this_step:]

                # In final steps only allow submit_final_report; skip everything else
                if step_idx >= max_steps - self.FORCE_CONCLUDE_LAST_N_STEPS:
                    has_submit = any(tc.function.name == "submit_final_report" for tc in tool_calls)
                    if not has_submit:
                        for skipped_call in original_tool_calls:
                            self.conversation_history.append({
                                "role": "tool",
                                "tool_call_id": skipped_call.id,
                                "name": skipped_call.function.name,
                                "content": (
                                    "Skipped in final-step mode. "
                                    "Call submit_final_report immediately using existing evidence."
                                ),
                            })
                        pending_user_nudges.append({
                            "role": "user",
                            "content": "Call submit_final_report NOW with your current findings.",
                        })
                        self.conversation_history.extend(pending_user_nudges)
                        continue

                # Acknowledge skipped calls so the API sees a tool result for each
                for skipped_call in skipped_tool_calls:
                    self.conversation_history.append({
                        "role": "tool",
                        "tool_call_id": skipped_call.id,
                        "name": skipped_call.function.name,
                        "content": "Skipped (tool cap). Will be retried in a later step if still needed.",
                    })

                for tool_call in tool_calls:
                    try:
                        args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError as e:
                        print(f"[ERROR] Failed to parse tool arguments: {e}")
                        # MUST still answer this tool_call — the assistant
                        # message already carries it, so skipping the
                        # response would leave an orphan tool_use and 400
                        # the very next API call. Reply with an error so
                        # the model can retry / recover.
                        self.conversation_history.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": getattr(tool_call.function, "name", "unknown"),
                            "content": f"Error: could not parse tool arguments as JSON ({e}). "
                                       f"Please re-issue the call with valid JSON arguments.",
                        })
                        continue

                    if tool_call.function.name == "submit_final_report":
                        final_report = args
                        _emit({"role": "agent", "content": "✅ **Conclusion reached!** Generating report."})
                        self.conversation_history.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": "submit_final_report",
                            "content": "Final report received and processed successfully.",
                        })

                    elif tool_call.function.name == "fetch_filtered_logs":
                        skill_label = args.get("skill_name", "")

                        # Skill fetch cap
                        skill_call_counts[skill_label] = skill_call_counts.get(skill_label, 0) + 1
                        distinct = len([k for k, v in skill_call_counts.items() if v >= 1])
                        if distinct > self.MAX_SKILL_FETCHES:
                            msg = (
                                f"Skill fetch limit reached ({self.MAX_SKILL_FETCHES} distinct skills). "
                                "Synthesize findings from already-fetched skills and call submit_final_report."
                            )
                            _emit({"role": "agent", "content": f"⚠️ **Skill cap hit** — {msg}"})
                            self.conversation_history.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "name": tool_call.function.name,
                                "content": msg,
                            })
                            continue

                        _emit({"role": "agent", "content": f"🔍 **Fetching filtered logs** for `{skill_label}`..."})
                        tool_result = self._invoke_tool("fetch_filtered_logs", {"skill_name": skill_label})
                        preview = tool_result[:400].replace('\n', ' ') + "..."
                        # _emit({"role": "tool", "content": f"📄 **Logs loaded** (`{skill_label}`):\n```\n{preview}\n```"})

                        # No-progress detection
                        if "New lines merged this round: 0" in tool_result or "Skill cache hit:" in tool_result:
                            no_progress_rounds += 1
                        else:
                            no_progress_rounds = 0

                        if skill_call_counts.get(skill_label, 0) >= 3:
                            pending_user_nudges.append({
                                "role": "user",
                                "content": (
                                    "Avoid repeatedly querying the same skill unless it adds new information. "
                                    "Cross-check with another perspective or synthesize current findings."
                                ),
                            })
                        if no_progress_rounds >= 2:
                            pending_user_nudges.append({
                                "role": "user",
                                "content": (
                                    "Recent tool calls did not add new evidence. "
                                    "Prioritize contradiction checks, timeline reconciliation, and final synthesis."
                                ),
                            })

                        # Expert rules injection (+ ACE domain playbook for the
                        # same skill, once per case, gated by the same set so
                        # the token-saving omit-on-repeat behaviour applies to
                        # both).
                        skill_obj = self.skills.get(skill_label)
                        expert_rules = getattr(skill_obj, 'expert_rules', '') if skill_obj else ''
                        if expert_rules:
                            if skill_label not in self._chat_rules_injected_skills:
                                rules_section = (
                                    f"=== Expert Rules for {skill_label} ===\n{expert_rules}\n\n"
                                    "=== Rule Usage Instruction ===\n"
                                    "Use these expert rules as investigative clues.\n"
                                    "For each important claim, map each rule clue to concrete log evidence\n"
                                    "and decide: supported, refuted, or uncertain.\n\n"
                                )
                                ace_domain_block = self._build_ace_domain_block(skill_label)
                                if ace_domain_block:
                                    _emit({
                                        "role": "agent",
                                        "content": (
                                            f"🧠 **ACE Domain Playbook injected for `{skill_label}`** "
                                            "(lessons from past cases)\n\n"
                                            f"```\n{ace_domain_block}```"
                                        ),
                                    })
                                rules_section += ace_domain_block
                                self._chat_rules_injected_skills.add(skill_label)
                            else:
                                rules_section = (
                                    f"=== Expert Rules for {skill_label} ===\n"
                                    "(already provided; omitted to save tokens)\n\n"
                                )
                            content = rules_section + self._clip_for_prompt(
                                f"=== Skill-Focused Evidence ({skill_label}) ===\n{tool_result}",
                                limit=self.MAX_TOOL_RESULT_CHARS_IN_MESSAGES,
                            )
                        else:
                            # No expert rules for this skill, but ACE may still
                            # have learned domain bullets — inject them so the
                            # playbook isn't silently dropped on cold skills.
                            ace_block = (
                                self._build_ace_domain_block(skill_label)
                                if skill_label not in self._chat_rules_injected_skills
                                else ""
                            )
                            if ace_block:
                                self._chat_rules_injected_skills.add(skill_label)
                                _emit({
                                    "role": "agent",
                                    "content": (
                                        f"🧠 **ACE Domain Playbook injected for `{skill_label}`** "
                                        "(lessons from past cases)\n\n"
                                        f"```\n{ace_block}```"
                                    ),
                                })
                            content = ace_block + self._clip_for_prompt(
                                tool_result, limit=self.MAX_TOOL_RESULT_CHARS_IN_MESSAGES
                            )

                        self.conversation_history.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.function.name,
                            "content": content,
                        })

                    else:
                        # Other tools: query_log_detail, get_assembled_log_snapshot, etc.
                        skill_label = args.get("skill_name") or tool_call.function.name

                        # Anti-loop for query_log_detail
                        if tool_call.function.name == "query_log_detail":
                            anchor_text = args.get("anchor_text", "")
                            anchor_ts = args.get("anchor_timestamp", "")
                            detail_sig = f"{anchor_text.lower()}|{anchor_ts}"
                            detail_call_counts[detail_sig] = detail_call_counts.get(detail_sig, 0) + 1

                        _emit({"role": "agent", "content": f"🔍 **Invoking** `{skill_label}`..."})
                        tool_result = self._invoke_tool(tool_call.function.name, args)

                        if tool_call.function.name == "query_log_detail":
                            if "No matching anchor found" in tool_result:
                                no_match_anchor_counts[detail_sig] = no_match_anchor_counts.get(detail_sig, 0) + 1
                                if no_match_anchor_counts[detail_sig] >= 2:
                                    pending_user_nudges.append({
                                        "role": "user",
                                        "content": (
                                            "You repeated an anchor query with no matches. "
                                            "Switch to a different anchor or synthesize from existing evidence."
                                        ),
                                    })
                            if detail_call_counts.get(detail_sig, 0) >= 3:
                                pending_user_nudges.append({
                                    "role": "user",
                                    "content": (
                                        "Detail queries are repeating similar anchors. "
                                        "Move from retrieval to judgment: reconcile timeline and conclude."
                                    ),
                                })

                        preview = tool_result[:400].replace('\n', ' ') + "..."
                        _emit({"role": "tool", "content": f"📄 **Result** (`{skill_label}`):\n```\n{preview}\n```"})
                        self.conversation_history.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.function.name,
                            "content": self._clip_for_prompt(tool_result, limit=self.MAX_TOOL_RESULT_CHARS_IN_MESSAGES),
                        })

                if final_report is not None:
                    _emit_token_report()
                    return {
                        "type": "report",
                        "data": final_report,
                        "issue_time": (
                            self.issue_time.strftime('%m/%d/%Y %H:%M:%S')
                            if self.issue_time else None
                        ),
                    }

            else:
                # Agent gave a text answer with no tool calls
                content = message.content or ""
                _emit_token_report()
                return {"type": "text", "data": content}

            # Flush nudges into history so they take effect next step
            self.conversation_history.extend(pending_user_nudges)

        _emit_token_report()
        return {
            "type": "error",
            "data": f"Reached maximum reasoning steps ({max_steps}) without a definitive conclusion. "
                    "Try breaking down the question or asking more specific queries.",
            "issue_time": (
                self.issue_time.strftime('%m/%d/%Y %H:%M:%S')
                if self.issue_time else None
            ),
        }

    def _extract_issue_time(self, issue_description: str) -> Optional[datetime]:
        # Fast path: deterministic regex parsing from issue_description text.
        text = (issue_description or "").strip()
        strict_match = re.search(
            r"(\d{1,2}/\d{1,2}/\d{4})[\s-](\d{1,2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?",
            text,
        )
        if strict_match:
            mmddyyyy = strict_match.group(1)
            hh = strict_match.group(2).zfill(2)
            minute = strict_match.group(3)
            second = strict_match.group(4)
            milli = (strict_match.group(5) or "000").ljust(3, "0")[:3]
            try:
                return datetime.strptime(
                    f"{mmddyyyy}-{hh}:{minute}:{second}.{milli}",
                    "%m/%d/%Y-%H:%M:%S.%f",
                )
            except ValueError:
                pass

        malformed_match = re.search(
            r"(\d{1,2}/\d{1,2}/\d{4})[\s-](\d{1,2}):(\d{2}):(\d{3})",
            text,
        )
        if malformed_match:
            mmddyyyy = malformed_match.group(1)
            hh = malformed_match.group(2).zfill(2)
            minute = malformed_match.group(3)
            sec_triplet = malformed_match.group(4)
            second = sec_triplet[:2]
            milli = (sec_triplet[2:] + "00")[:3]
            try:
                return datetime.strptime(
                    f"{mmddyyyy}-{hh}:{minute}:{second}.{milli}",
                    "%m/%d/%Y-%H:%M:%S.%f",
                )
            except ValueError:
                pass

        # Fallback: let the LLM extract timestamp from free-form issue text.
        prompt = (
            "Extract the exact date and time mentioned in the following user issue description.\n"
            "If a time is found, output ONLY the timestamp in 'MM/DD/YYYY-HH:MM:SS' format "
            "(e.g., 10/28/2025-11:25:50).\n"
            "If no time is mentioned, output 'NONE'.\n\n"
            f"User Description: {issue_description}"
        )
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0
            )
            self._accumulate_turn_usage(getattr(response, "usage", None))
            time_str = response.choices[0].message.content.strip()
            if time_str != "NONE":
                return datetime.strptime(f"{time_str}.000", "%m/%d/%Y-%H:%M:%S.%f")
        except Exception as e:
            print(f"[!] Time extraction failed: {e}")
        return None

    def _precheck_report_quality(self, issue_description: str, report: dict, evidence_tail: str) -> Optional[dict]:
        """Lightweight deterministic guardrails before LLM quality review."""
        issue_text = (issue_description or "").lower()
        report_text = (
            f"{report.get('root_cause_summary', '')}\n{report.get('markdown_summary', '')}"
        ).lower()
        tail_text = (evidence_tail or "").lower()

        asks_direct_question = ("?" in (issue_description or "")) or any(
            t in issue_text for t in ("why", "what", "how", "can", "could", "cannot", "can't")
        )
        answer_markers = (
            "because", "due to", "caused by", "no evidence", "not observed",
            "normal background", "maintenance", "works as expected", "healthy", "stable",
        )
        if asks_direct_question and not any(m in report_text for m in answer_markers):
            return {
                "approved": False,
                "reason": "Final conclusion does not directly answer the user's question.",
                "required_actions": [
                    "Start root_cause_summary with a direct answer to the user question.",
                    "Then provide evidence-based rationale.",
                ],
            }

        severe_claim_markers = (
            "fatal", "critical", "persistent failure", "cannot scan", "can't scan",
            "failed to scan", "crash", "assert", "bsod",
        )
        healthy_tail_markers = (
            "scan is allowed", "connected", "assoc_rsp", "probe_rx", "probe_tx",
            "beacon", "rssi", "allowed (true)",
        )
        hard_fail_tail_markers = (
            "deauth", "task_disconnect", "termination", "bsod", "assert", "fw crash",
        )

        severe_claim = any(m in report_text for m in severe_claim_markers)
        healthy_tail_hits = sum(1 for m in healthy_tail_markers if m in tail_text)
        hard_fail_tail = any(m in tail_text for m in hard_fail_tail_markers)

        if severe_claim and healthy_tail_hits >= 2 and not hard_fail_tail:
            return {
                "approved": False,
                "reason": "Report likely overstates severity: tail evidence suggests normal background maintenance or healthy final state.",
                "required_actions": [
                    "Re-check latest log tail before concluding persistent failure.",
                    "Separate transient/background maintenance from fatal root cause.",
                ],
            }

        return None

    def _review_report_quality(self, issue_description: str, report: dict) -> dict:
        """
        Generic quality gate for final report, avoiding case-specific hardcoding.
        Checks temporal consistency and contradiction risk against compact assembled evidence.
        """
        try:
            evidence = self.get_assembled_log_snapshot(mode="compact")
            evidence_tail = self._get_assembled_log_tail(max_lines=120)
            deterministic = self._precheck_report_quality(issue_description, report, evidence_tail)
            if deterministic:
                return deterministic
            report_text = json.dumps(report, ensure_ascii=False)
            prompt = (
                "You are a diagnostic quality auditor. Evaluate whether the proposed final report "
                "is sufficiently supported by evidence and temporally consistent.\n"
                "Do NOT require domain-specific keywords. Apply generic checks only:\n"
                "1) Claims must be tied to explicit evidence.\n"
                "2) Early failures must be checked against later state to avoid stale conclusions.\n"
                "3) Detect state transitions (e.g., unavailable -> available, fail -> success). "
                "If transition exists, avoid absolute failure conclusions.\n"
                "4) If contradictions or evidence gaps exist, require uncertainty wording.\n"
                "5) Prefer latest confirmed state over earlier transient state.\n"
                "6) Before approving any persistent failure claim, verify latest log tail for success "
                "signals of the same target (for example probe/connected-like evidence).\n"
                "7) Treat explicit gate-status lines like '<feature> is ALLOWED/DISALLOWED/ENABLED/DISABLED' "
                "as high-priority state indicators; prefer the latest state bit.\n"
                "8) Apply hierarchy-of-truth conflict resolution: capability state > physical events > task intent > warning/error.\n"
                "If a lower layer conflicts with a higher layer, reject absolute lower-layer conclusions.\n"
                "9) Confirm that skill rules were used as investigative clues and validated/refuted by logs; "
                "rules are not ground truth by themselves.\n"
                "10) The report MUST directly answer the user's question in the first sentence.\n"
                "11) Distinguish transient/background maintenance behavior from persistent fatal failures.\n"
                "Return strict JSON only with this schema:\n"
                "{\"approved\": true|false, \"reason\": \"...\", \"required_actions\": [\"...\"]}\n\n"
                f"Issue:\n{issue_description}\n\n"
                f"Evidence (compact assembled snapshot):\n{evidence}\n\n"
                f"Latest evidence tail (high priority for final-state checks):\n{evidence_tail}\n\n"
                f"Proposed report JSON:\n{report_text}"
            )

            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=400,
            )
            self._accumulate_turn_usage(getattr(response, "usage", None))
            raw = (response.choices[0].message.content or "").strip()
            parsed = None
            try:
                parsed = json.loads(raw)
            except Exception:
                m = re.search(r'\{[\s\S]*\}', raw)
                if m:
                    parsed = json.loads(m.group(0))
            if isinstance(parsed, dict) and "approved" in parsed:
                parsed.setdefault("reason", "")
                parsed.setdefault("required_actions", [])
                if not isinstance(parsed.get("required_actions"), list):
                    parsed["required_actions"] = [str(parsed.get("required_actions"))]
                return parsed
        except Exception as e:
            print(f"[WARN] Report quality review skipped: {e}")

        return {"approved": True, "reason": "quality gate fallback", "required_actions": []}

    def _get_assembled_log_tail(self, max_lines: int = 120) -> str:
        """Return the latest assembled-log lines for final-state verification."""
        text = (self._assembled_log_text or "").strip()
        if not text:
            return "(no assembled log yet)"
        lines = text.splitlines()
        if len(lines) <= max_lines:
            return "\n".join(lines)
        return "\n".join(lines[-max_lines:])

    # Generic outcome-signal keywords (not case-specific; covers common WiFi states).
    _OUTCOME_SIGNAL_KEYWORDS = [
        "PROBE_RX", "PROBE_TX", "CONNECTED", "ASSOC_RSP", "AUTH_RSP",
        "RSSI", "RssiAdjustment", "scan is ALLOWED", "scan is DISALLOWED",
        "ALLOWED", "DISALLOWED", "ENABLED", "DISABLED",
        "Update regulatory", "NIC State",
    ]

    def _build_outcome_injection(self, tail_count: int = 2000, max_signals: int = 15) -> str:
        """
        Scan the raw log tail for outcome-level signals and return a compact
        auto-injected summary so the model has end-state awareness from step 0.
        Returns empty string if no raw log or no signals found.
        """
        if not self._raw_log_cache:
            err = self._ensure_raw_log_cache()
            if err or not self._raw_log_cache:
                return ""

        tail_lines = self._raw_log_cache[-tail_count:]
        signals = []
        for line in tail_lines:
            line_str = str(line)
            if any(kw in line_str for kw in self._OUTCOME_SIGNAL_KEYWORDS):
                signals.append(line_str.strip())

        if not signals:
            return ""

        # Keep latest N signals to avoid token bloat.
        signals = signals[-max_signals:]
        compact = []
        for s in signals:
            _, ts_display, msg = self._normalize_time_message(s)
            compact.append(f"<{ts_display}> {msg}")

        return (
            "[AUTO-INJECTED: Final Physical State Evidence from log tail]\n"
            "These are outcome-level signals from the end of the log. "
            "Use them to verify whether features eventually succeeded before concluding persistent failure.\n"
            + "\n".join(compact)
        )

    def _build_ace_workflow_block(self) -> str:
        """
        Render the ACE workflow playbook + bullet-citation reminder for the
        agent's system prompt. Returns "" when no AceRunner is attached or
        the workflow playbook has no bullets yet (so we don't waste tokens
        on an empty header on a cold install).
        """
        if self.ace_runner is None:
            print("[ace] workflow block skipped: no AceRunner attached")
            return ""
        try:
            text = self.ace_runner.render_workflow()
        except Exception as e:
            print(f"[ace] render_workflow failed: {e}")
            return ""
        if not text or text.strip() in ("", "(empty playbook)"):
            print("[ace] workflow block skipped: workflow playbook is empty")
            return ""
        n_bullets = sum(1 for ln in text.splitlines() if ln.lstrip().startswith("- "))
        try:
            pb_path = getattr(self.ace_runner.workflow_pb, "path", "?")
        except Exception:
            pb_path = "?"
        print(f"[ace] injecting {n_bullets} workflow bullets into prompt (from {pb_path})")
        return (
            "\n=== ACE Workflow Playbook (orchestration rules learned from past cases) ===\n"
            + text
            + "\nApply the bullets above when they fit. Cite the bullet ids you used in\n"
              "submit_final_report.applied_bullet_ids; cite ids you found misleading in\n"
              "flagged_bullet_ids. Ignore bullets that don't apply.\n"
              "=== End Workflow Playbook ===\n\n"
        )

    def _build_ace_domain_block(self, skill_name: str) -> str:
        """
        Render the ACE domain playbook for one skill. Returns "" when ACE is
        not attached, the playbook is empty, or rendering fails.
        """
        if self.ace_runner is None or not skill_name:
            if self.ace_runner is None:
                print(f"[ace] domain block skipped ({skill_name!r}): no AceRunner attached")
            return ""
        try:
            text = self.ace_runner.render_domain(skill_name, ensure=True)
        except Exception as e:
            print(f"[ace] render_domain({skill_name}) failed: {e}")
            return ""
        if not text or text.strip() in ("", "(empty playbook)"):
            print(f"[ace] domain block skipped ({skill_name!r}): playbook is empty")
            return ""
        n_bullets = sum(1 for ln in text.splitlines() if ln.lstrip().startswith("- "))
        try:
            pb_path = getattr(self.ace_runner.domain_pbs.get(skill_name), "path", "?")
        except Exception:
            pb_path = "?"
        print(f"[ace] injecting {n_bullets} domain bullets for {skill_name!r} into prompt (from {pb_path})")
        return (
            f"=== ACE Domain Playbook for {skill_name} (lessons from past cases) ===\n"
            + text
            + "\n=== End Domain Playbook ===\n\n"
        )

    def _build_analyze_system_prompt(self, context_section: str) -> str:
        """Build the agentic analysis system prompt used by _chat_with_tools."""
        ace_block = self._build_ace_workflow_block()
        return (
            f"{context_section}"
            + ace_block
            + "You are an Elite Wi-Fi Diagnostic Detective. Your GOAL: Find the REAL Root Cause based on evidence.\n"
                        + "Available skills:\n"
                        + "".join(
                            f"  - {s['name']}: {s['description']}\n"
                            for s in self.get_skill_descriptions()
                            if s.get('description')
                        )
                        + "\n"
                        "PHASE 1 (SYMPTOM LOCALIZATION):\n"
                        "   - Call `fetch_filtered_logs` with the most relevant skill to get symptom-focused log evidence.\n"
                        "   - Call `fetch_filtered_logs` with skill `assert_code_analysis` to scan for firmware asserts.\n"
                        "PHASE 2 (SOURCE RETROSPECTIVE - optional):\n"
                        "   - if needed, based on the analysis from PHASE1, use additional skills to get more detail from the logs.\n"
                        "PHASE 3. Call `submit_final_report` to conclude.\n\n"
                        "CRITICAL CONSTRAINTS:\n"
                        "- Max step is 8\n"
                        "- 🛑 NO REPETITION: Do not fetch the same data twice. If Phase 1 keywords are found in Phase 2, ignore them.\n"
                        "- 🛑 IMMEDIATELY call `submit_final_report` after your detail query. Do not over-analyze.\n\n"
                        "Your `markdown_summary` format (REQUIRED):\n"
                        "  # Executive Summary\n  (1-2 sentences about the true root cause found in Phase 2)\n\n"
                        "  | Aspect | Finding |\n"
                        "  |--------|---------|\n"
                        "  | Signal | ... |\n"
                        "  (Markdown table with data gaps)\n\n"
                        "  ## Timeline\n"
                        "  - T-Ns: Trigger Event (The Source)\n"
                        "  - T+0s: Physical Failure begins\n"
                        "  - T+Ns: Final Termination\n\n"
                        "  ## Recommendations\n"
                        "  **P0 (Urgent):** ...\n"
                        "  **P1 (Important):** ...\n"
                        "  **P2 (Nice-to-have):** ..."                
        )

    def _invoke_tool(self, tool_name: str, args: dict) -> str:
        """Centralized tool dispatch used by both chat and analyze flows."""
        if tool_name == "fetch_filtered_logs":
            return self.fetch_filtered_logs(args.get("skill_name", ""))

        if tool_name == "query_log_detail":
            anchor_text = args.get("anchor_text", "")
            anchor_timestamp = args.get("anchor_timestamp", "")
            context_span = self._resolve_context_span(
                anchor_text,
                args.get("context_span", self.DEFAULT_DETAIL_CONTEXT_SPAN),
            )
            max_hits = min(args.get("max_hits", 3), self.MAX_DETAIL_HITS)
            return self.query_log_detail(
                anchor_text=anchor_text,
                anchor_timestamp=anchor_timestamp,
                context_span=context_span,
                max_hits=max_hits,
            )

        if tool_name in ("get_assembled_log_snapshot", "get_final_state_snapshot"):
            return (
                f"{tool_name} is disabled. "
                "Use fetch_filtered_logs(skill_name) to retrieve skill-focused evidence "
                "or query_log_detail(keyword) to search specific events."
            )

        if tool_name == "lookup_assert_code":
            return lookup_assert_code(args.get("code", ""))

        if tool_name == "softAP_supported_channel":
            err = self._ensure_raw_log_cache()
            if err:
                return err
            log_text = "\n".join(self._raw_log_cache)
            if not log_text.strip():
                return "ERROR: Raw log is empty or unavailable."
            return softAP_supported_channel(log_text)

        return f"Unknown tool: {tool_name}"

    def _append_tool_message(self, messages: list, tool_call, content: str) -> None:
        """Append a tool result message in the required protocol format."""
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "name": tool_call.function.name,
            "content": content,
        })

    def _handle_submit_tool_call(self, tool_call, args: dict, issue_description: str,
                                 step_num: int, max_steps: int, messages: list,
                                 pending_user_nudges: list, steps: list, emit_cb) -> Optional[dict]:
        """Handle submit_final_report and return final response dict when accepted."""
        review = self._review_report_quality(issue_description, args)
        if not review.get("approved", True) and step_num < max_steps - 1:
            required_actions = review.get("required_actions", []) or []
            self._append_tool_message(
                messages,
                tool_call,
                (
                    "Rejected by quality gate. "
                    f"Reason: {review.get('reason', 'insufficient support')}."
                ),
            )
            emit_cb({
                "role": "agent",
                "content": (
                    " **Quality gate:** report needs refinement before final submit.\n"
                    f"Reason: {review.get('reason', 'insufficient support')}"
                ),
            })
            pending_user_nudges.append({
                "role": "user",
                "content": (
                    "Refine your diagnosis before submit_final_report. "
                    f"Reason: {review.get('reason', '')}. "
                    f"Required actions: {', '.join(str(x) for x in required_actions) if required_actions else 'perform temporal/contradiction validation with existing evidence.'}"
                ),
            })
            return None

        self._append_tool_message(messages, tool_call, "Final report accepted.")
        emit_cb({"role": "agent", "content": " **Conclusion Reached!** Generating report."})
        self._inject_analysis_into_history(issue_description, steps, args)
        return {
            "type": "report",
            "data": args,
            "steps": steps,
            "issue_time": (
                self.issue_time.strftime('%m/%d/%Y %H:%M:%S')
                if self.issue_time else None
            ),
        }

    def _handle_fetch_tool_call(self, tool_call, args: dict, messages: list,
                                pending_user_nudges: list, expert_rules_injected_skills: set,
                                skill_call_counts: dict, no_progress_rounds: int, emit_cb) -> int:
        """Handle fetch_filtered_logs tool call and return updated no_progress_rounds."""
        skill_name = args.get("skill_name")
        skill_call_counts[skill_name] = skill_call_counts.get(skill_name, 0) + 1

        # Enforce max distinct skill fetches to control token budget.
        distinct_skills_fetched = len([k for k, v in skill_call_counts.items() if v >= 1])
        if distinct_skills_fetched > self.MAX_SKILL_FETCHES:
            msg = (
                f"Skill fetch limit reached ({self.MAX_SKILL_FETCHES} skills). "
                "Synthesize findings from already-fetched skills and call submit_final_report."
            )
            emit_cb({"role": "agent", "content": f"⚠️ **Skill cap hit** — {msg}"})
            self._append_tool_message(messages, tool_call, msg)
            return no_progress_rounds

        emit_cb({"role": "agent", "content": f" **Fetching Filtered Logs** for `{skill_name}`..."})

        tool_result = self._invoke_tool("fetch_filtered_logs", {"skill_name": skill_name})
        skill = self.skills.get(skill_name)
        expert_rules = getattr(skill, 'expert_rules', '') if skill else ''

        line_count = tool_result.count('\n')
        preview = tool_result[:500].replace('\n', ' ') + "..."
        # emit_cb({
        #     "role": "tool",
        #     "content": f" **Logs Loaded** (`{skill_name}`, ~{line_count} lines):\n```\n{preview}\n```"
        # })

        # Expert rules are prepended in full (never clipped); only the evidence
        # section is clipped so the tool_result immediately follows tool_use.
        if skill_name not in expert_rules_injected_skills and expert_rules:
            rules_section = (
                f"=== Expert Rules for {skill_name} ===\n{expert_rules}\n\n"
                "=== Rule Usage Instruction ===\n"
                "Use these expert rules as investigative clues.\n"
                "For each important claim, map each rule clue to concrete log evidence\n"
                "and decide: supported, refuted, or uncertain.\n\n"
            )
            expert_rules_injected_skills.add(skill_name)
        else:
            rules_section = (
                f"=== Expert Rules for {skill_name} ===\n"
                "(already provided; omitted to save tokens)\n\n"
            )

        evidence_content = self._clip_for_prompt(
            f"=== Skill-Focused Evidence ({skill_name}) ===\n{tool_result}",
            limit=self.MAX_TOOL_RESULT_CHARS_IN_MESSAGES,
        )
        self._append_tool_message(
            messages,
            tool_call,
            rules_section + evidence_content,
        )
        emit_cb({
            "role": "debug",
            "content": (
                f"**Token Budget ({skill_name}):** "
                f"rules={len(rules_section)} chars | "
                f"evidence={len(evidence_content)} chars | "
                f"total={len(rules_section) + len(evidence_content)} chars "
                f"(evidence limit={self.MAX_TOOL_RESULT_CHARS_IN_MESSAGES})"
            ),
        })

        if "New lines merged this round: 0" in tool_result or "Skill cache hit:" in tool_result:
            no_progress_rounds += 1
        else:
            no_progress_rounds = 0

        if skill_call_counts.get(skill_name, 0) >= 3:
            pending_user_nudges.append({
                "role": "user",
                "content": (
                    "Avoid repeatedly querying the same evidence view unless it adds new information. "
                    "Cross-check with another perspective or synthesize current findings."
                ),
            })
        if no_progress_rounds >= 2:
            pending_user_nudges.append({
                "role": "user",
                "content": (
                    "Recent tool calls did not add new evidence. "
                    "Prioritize contradiction checks, timeline reconciliation, and final synthesis."
                ),
            })
        return no_progress_rounds

    def _handle_snapshot_tool_call(self, tool_call, args: dict, messages: list, emit_cb) -> None:
        """Handle get_assembled_log_snapshot tool call."""
        mode = args.get("mode", "summary")
        if mode == "full":
            mode = "compact"
        emit_cb({"role": "agent", "content": f" **Requesting assembled snapshot** (mode={mode})"})
        tool_result = self._invoke_tool("get_assembled_log_snapshot", {"mode": mode})
        emit_cb({
            "role": "tool",
            "content": f" **Assembled Snapshot Loaded**:\n```\n{tool_result[:500]}\n```"
        })
        self._append_tool_message(
            messages,
            tool_call,
            self._clip_for_prompt(tool_result, limit=self.MAX_TOOL_RESULT_CHARS_IN_MESSAGES),
        )

    def _handle_final_state_tool_call(self, tool_call, args: dict, messages: list, emit_cb) -> None:
        """Handle get_final_state_snapshot tool call."""
        tail_lines = args.get("tail_lines", 120)
        emit_cb({
            "role": "agent",
            "content": f" **Requesting final-state snapshot** (tail_lines={tail_lines})"
        })
        tool_result = self._invoke_tool("get_final_state_snapshot", {"tail_lines": tail_lines})
        emit_cb({
            "role": "tool",
            "content": f" **Final-State Snapshot Loaded**:\n```\n{tool_result[:500]}\n```"
        })
        self._append_tool_message(
            messages,
            tool_call,
            self._clip_for_prompt(tool_result, limit=self.MAX_TOOL_RESULT_CHARS_IN_MESSAGES),
        )

    def _handle_detail_tool_call(self, tool_call, args: dict, messages: list,
                                 pending_user_nudges: list, no_match_anchor_counts: dict,
                                 detail_call_counts: dict, emit_cb) -> None:
        """Handle query_log_detail tool call and anti-loop nudges."""
        anchor_text = args.get("anchor_text", "")
        anchor_timestamp = args.get("anchor_timestamp", "")
        detail_sig = f"{anchor_text.lower()}|{anchor_timestamp}"
        detail_call_counts[detail_sig] = detail_call_counts.get(detail_sig, 0) + 1
        context_span = self._resolve_context_span(anchor_text, args.get("context_span", self.DEFAULT_DETAIL_CONTEXT_SPAN))
        max_hits = min(args.get("max_hits", 3), self.MAX_DETAIL_HITS)
        emit_cb({
            "role": "agent",
            "content": (
                " **Querying anchor context** "
                f"(text='{anchor_text}', ts='{anchor_timestamp}')"
            )
        })

        tool_result = self._invoke_tool(
            "query_log_detail",
            {
                "anchor_text": anchor_text,
                "anchor_timestamp": anchor_timestamp,
                "context_span": context_span,
                "max_hits": max_hits,
            },
        )

        query_sig = f"{anchor_text.lower()}|{anchor_timestamp}"
        if "No matching anchor found" in tool_result:
            no_match_anchor_counts[query_sig] = no_match_anchor_counts.get(query_sig, 0) + 1
            if no_match_anchor_counts[query_sig] >= 2:
                pending_user_nudges.append({
                    "role": "user",
                    "content": (
                        "You repeated an anchor query with no matches. "
                        "Switch to a different anchor or synthesize conclusions from existing evidence; "
                        "do not loop on the same missing anchor."
                    ),
                })
        if detail_call_counts.get(detail_sig, 0) >= 3:
            pending_user_nudges.append({
                "role": "user",
                "content": (
                    "Detail queries are repeating similar anchors. "
                    "Move from retrieval to judgment: reconcile timeline and contradictions, then conclude."
                ),
            })

        emit_cb({
            "role": "tool",
            "content": f"📄 **Detail Loaded**:\n```\n{tool_result}\n```"
        })
        self._append_tool_message(
            messages,
            tool_call,
            self._clip_for_prompt(tool_result, limit=self.MAX_TOOL_RESULT_CHARS_IN_MESSAGES),
        )

    # ------------------------------------------------------------------
    # Inject analysis results into conversation_history for follow-up chat
    # ------------------------------------------------------------------
    def _inject_analysis_into_history(self, issue_description: str,
                                       steps: list, report: dict) -> None:
        """
        After a chat analysis finishes, inject a clean summary into
        self.conversation_history so follow-up chat() calls have full
        context of the prior analysis.

        IMPORTANT: Only plain text/assistant messages, NO tool_use/tool_result.
        """
        # Build a condensed recap of the agent's thinking process
        # ONLY include agent/tool content, excluding any metadata
        thinking_parts = []
        for s in steps:
            role = s.get("role", "")
            content = s.get("content", "")
            # Only extract raw content, skip any tool_id/name fields
            if role in ("agent", "tool") and content:
                thinking_parts.append(content[:300] if role == "tool" else content)

        thinking_recap = "\n".join(thinking_parts)
        # Cap at 5000 chars
        if len(thinking_recap) > 5000:
            thinking_recap = thinking_recap[:5000] + "\n... (truncated)"

        # Build report summary text
        report_parts = []
        if report.get("root_cause_summary"):
            report_parts.append(f"**Root Cause:** {report['root_cause_summary']}")
        if report.get("confidence_score"):
            report_parts.append(f"**Confidence:** {report['confidence_score']}%")
        if report.get("recommended_actions"):
            actions = "\n".join(f"- {a}" for a in report["recommended_actions"])
            report_parts.append(f"**Recommendations:**\n{actions}")
        if report.get("markdown_summary"):
            report_parts.append(f"\n{report['markdown_summary']}")

        report_text = "\n".join(report_parts)

        # Build CLEAN conversation history: ONLY system/user/assistant roles
        # (NO tool_use, tool_result, or any tool-related fields)
        assistant_summary = (
            f"##  Analysis Complete\n\n"
            f"###  Agent Reasoning Process\n{thinking_recap}\n\n"
            f"###  Report\n{report_text}"
        )

        # CRITICAL: Reset to completely clean history
        self.conversation_history = [
            {
                "role": "system",
                "content": (
                    "You are a Wi-Fi troubleshooting expert.\n"
                    f"Available diagnostic skills: {', '.join(self.skills.keys())}.\n\n"
                    "A comprehensive multi-skill analysis has been completed.\n"
                    "Review the results below and answer user follow-up questions.\n"
                    f"Log file: {self.current_log_path}"
                ),
            },
            {
                "role": "user",
                "content": f"Analyze this issue: {issue_description}",
            },
            {
                "role": "assistant",
                "content": assistant_summary,
            },
        ]

    # ------------------------------------------------------------------
    # Reset conversation
    # ------------------------------------------------------------------
    def reset_conversation(self):
        self.conversation_history = []
        self._detail_cache = {}
        self._detail_query_seen = set()
        self._chat_rules_injected_skills = set()
        self._filter_cache_by_skill = {}
        self._assembled_entries_by_key = {}
        self._assembled_entries_no_ts = {}
        self._assembled_log_text = ""
        # Force date-format re-detection on the next _log_has_date() call.
        # /set_log resets the conversation when the underlying file changes,
        # so wiping this cache here makes the UI mode-switch (date-required
        # vs time-only) reliably follow the actual file format.
        self._log_has_date_cache = None
        self._log_has_date_cache_path = None

    def apply_updated_skills(self, skills) -> None:
        """Swap in edited / reloaded skills WITHOUT discarding the conversation.

        Replacing ``self.skills`` alone is NOT enough for a mid-conversation
        edit to take effect, because two caches would keep serving the old
        version:
          * ``_chat_rules_injected_skills`` — makes the chat loop take the
            "expert rules already provided; omitted to save tokens" path, so an
            edited skill's NEW expert_rules would never be re-injected;
          * ``_filter_cache_by_skill`` — makes ``fetch_filtered_logs`` return
            the previously-filtered lines, so an edited FILTER would never
            re-run.
        Clearing both means the next ``fetch_filtered_logs`` for any skill
        re-applies the latest definition. Conversation history (and the
        assembled-log store) is preserved, so prior analysis context stays and
        no tool_use/tool_result pairing is disturbed.
        """
        self.skills = skills or {}
        self._chat_rules_injected_skills = set()
        self._filter_cache_by_skill = {}

    def prime_with_context(self, case_nbr: str = "", subject: str = "",
                            description: str = "", issue_type: str = "",
                            attachment_time: str = "") -> None:
        """
        Reset conversation and inject the case context as the opening system
        message so the LLM knows what issue it is analysing before the user
        asks the first question.
        """
        self.conversation_history = []
        self._detail_cache = {}
        self._detail_query_seen = set()
        self._chat_rules_injected_skills = set()
        self._filter_cache_by_skill = {}
        self._assembled_entries_by_key = {}
        self._assembled_entries_no_ts = {}
        self._assembled_log_text = ""
        self.issue_context = {
            "case_nbr":   case_nbr,
            "subject":    subject,
            "description": description,
            "issue_type": issue_type,
        }
        # Resolve ``attachment_time`` into the LOG frame so PreScan can
        # match against the raw .log content (the decoder writes the log
        # host's clock — GMT+8 in our deployment). The same priority logic
        # used for the customer-side picker is reused here: trust the
        # input as customer wall-clock first, only flip to "input was
        # already in log frame" when the evidence is strong. Either way we
        # also remember the customer-frame equivalent so the UI / agent
        # context can surface a "what this means on the customer's
        # wall-clock" annotation alongside the log-frame value.
        aligned_attachment_time = attachment_time
        self.issue_time_customer = None
        self.issue_time_tz = ""
        if attachment_time and self.current_log_path:
            try:
                from utils.issue_time_ai import determine_issue_time_frames
                parsed_dt, is_time_only = parse_issue_time_string(attachment_time)
                if parsed_dt and not is_time_only:
                    frames = determine_issue_time_frames(
                        parsed_dt, [self.current_log_path]
                    )
                    log_dt = frames.get("log_frame") or parsed_dt
                    self.issue_time_customer = frames.get("customer_frame")
                    self.issue_time_tz = frames.get("customer_tz") or ""
                    if log_dt != parsed_dt:
                        aligned_attachment_time = format_issue_time(log_dt)
                    print(f"[DEBUG] prime_with_context frames: "
                          f"log={log_dt} customer={self.issue_time_customer} "
                          f"tz={self.issue_time_tz!r} "
                          f"source_frame={frames.get('source_frame')}")
                elif parsed_dt and is_time_only:
                    # Time-only input (e.g. "04:45 PM") carries no date. Per the
                    # locked default (#4) treat the clock as the CUSTOMER wall
                    # clock, anchor its date to the capture day (the log's last
                    # timestamp shifted into the customer tz), then convert
                    # customer -> log so the value lands in the same frame as the
                    # .log content for PreScan. Also stash the customer-frame
                    # equivalent for the UI annotation. Falls through to the raw
                    # resolve_issue_time path (stamps the clock onto the log date,
                    # no shift) when no tz / no log timestamp is available — which
                    # is the correct no-op for a Taiwan-frame customer anyway.
                    from utils.issue_time_utils import read_log_time_range
                    from utils.timezone_utils import (
                        get_effective_timezone, taiwan_to_local, local_to_taiwan,
                    )
                    tz = get_effective_timezone(self.current_log_path)
                    _first_ts, _last_ts = read_log_time_range(self.current_log_path)
                    _ref = _last_ts or _first_ts
                    if tz and _ref:
                        cust_date = (taiwan_to_local(_ref, tz) or _ref).date()
                        cust_dt = datetime.combine(cust_date, parsed_dt.time())
                        log_dt = local_to_taiwan(cust_dt, tz) or cust_dt
                        self.issue_time_customer = cust_dt
                        self.issue_time_tz = tz
                        aligned_attachment_time = format_issue_time(log_dt)
                        print(f"[DEBUG] prime_with_context time-only frames: "
                              f"clock={parsed_dt.time()} customer={cust_dt} "
                              f"log={log_dt} tz={tz!r}")
            except Exception as e:
                print(f"[DEBUG] prime_with_context frame detect skipped ({e})")

        # Resolve issue_time once: parse attachment_time strictly, fall back to
        # the log file's latest timestamp when no usable input exists, and
        # auto-align time-only strings against the log date. After this call
        # `self.issue_time` is the canonical value used everywhere downstream.
        dt, src = resolve_issue_time(aligned_attachment_time, self.current_log_path)
        self.issue_time = dt
        self._issue_time_time_only = (src == "input_time_only")
        print(f"[DEBUG] prime_with_context issue_time={dt} source={src} "
              f"raw='{attachment_time}' log_frame='{aligned_attachment_time}' "
              f"customer_frame='{self.issue_time_customer}'")
        context_parts = []
        if case_nbr:
            context_parts.append(f"Case: {case_nbr}")
        if subject:
            context_parts.append(f"Subject: {subject}")
        if issue_type:
            context_parts.append(f"Classified issue type: {issue_type}")
        if description:
            context_parts.append(f"\nIssue description:\n{description}")

        if context_parts:
            self.conversation_history.append({
                "role": "system",
                "content": (
                    "You are a Wi-Fi troubleshooting assistant with expert-level knowledge.\n"
                    f"Available skills: {', '.join(self.skills.keys())}.\n"
                    "Use fetch_filtered_logs with the most relevant skill(s), then call "
                    "submit_final_report.\n\n"
                    "=== Case Context ===\n"
                    + "\n".join(context_parts)
                )
            })

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _build_tools(self) -> list:
        return [
            {
                "type": "function",
                "function": {
                    "name": "fetch_filtered_logs",
                    "description": (
                        "Filter from the original full log using the specified skill, then merge results "
                        "into a cumulative timestamp-assembled log (line numbers are not persisted). "
                        "Returns a compact skill-focused evidence payload to save tokens."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill_name": {
                                "type": "string",
                                "enum": list(self.skills.keys()),
                                "description": "Which skill's filter to apply (e.g., 'Connectivity', 'Roaming')"
                            }
                        },
                        "required": ["skill_name"]
                    }
                }
            },
            # {
            #     "type": "function",
            #     "function": {
            #         "name": "get_assembled_log_snapshot",
            #         "description": (
            #             "Retrieve assembled-log macro view on demand. "
            #             "Use mode='summary' for metadata only, 'compact' for limited body, "
            #             "or 'full' for complete assembled content."
            #         ),
            #         "parameters": {
            #             "type": "object",
            #             "properties": {
            #                 "mode": {
            #                     "type": "string",
            #                     "enum": ["summary", "compact", "full"],
            #                     "description": "How much assembled content to return.",
            #                     "default": "summary"
            #                 }
            #             },
            #             "required": []
            #         }
            #     }
            # },
            # {
            #     "type": "function",
            #     "function": {
            #         "name": "get_final_state_snapshot",
            #         "description": (
            #             "Retrieve the latest assembled-log tail for end-of-analysis verification. "
            #             "Use this before declaring a persistent failure to check whether later logs show recovery/success."
            #         ),
            #         "parameters": {
            #             "type": "object",
            #             "properties": {
            #                 "tail_lines": {
            #                     "type": "integer",
            #                     "description": "Number of latest lines to inspect. Default 120, range 20-400.",
            #                     "default": 120
            #                 }
            #             },
            #             "required": []
            #         }
            #     }
            # },
            {
                "type": "function",
                "function": {
                    "name": "lookup_assert_code",
                    "description": (
                        "Look up a firmware assert/error code from the Intel Wi-Fi LMAC or UMAC header. "
                        "Accepts the raw code exactly as it appears in the log — flag decomposition is "
                        "handled automatically.\n"
                        "Code formats seen in logs:\n"
                        "  0x20xxxxxx → UMAC assert (0x20000000 CPU flag stripped automatically)\n"
                        "  0x10xxxx   → UMAC namespace (UMAC_ASSERT_START)\n"
                        "  0x40xxxx   → LMAC RCM sub-CPU assert\n"
                        "  0x50xxxx   → LMAC TCM sub-CPU assert\n"
                        "  0x00xxxx   → LMAC direct assert\n"
                        "Call this whenever you see 'assert', 'ASSERT', or a hex code after "
                        "'code=' in the logs."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "code": {
                                "type": "string",
                                "description": (
                                    "Raw assert code from the log, as a hex string "
                                    "e.g. '0x20100505' or '0x34'"
                                )
                            }
                        },
                        "required": ["code"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "softAP_supported_channel",
                    "description": (
                        "Analyze the SoftAP supported channels per country/region from the currently loaded log. "
                        "Takes no arguments — the server reads the full raw log internally. "
                        "Do NOT pass log_text; you do not have the full raw log in context."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": []
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "submit_final_report",
                    "description": (
                        "Call this tool once you have identified the root cause. "
                        "Submits the structured final analysis report."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "root_cause_summary": {
                                "type": "string",
                                "description": "One-sentence root cause summary"
                            },
                            "confidence_score": {
                                "type": "integer",
                                "description": "Confidence 0-100"
                            },
                            "recommended_actions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Bullet-point actions"
                            },
                            "involved_skills": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Skills used in this diagnosis"
                            },
                            "markdown_summary": {
                                "type": "string",
                                "description": "Full Markdown report for engineers"
                            },
                            "applied_bullet_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "ACE playbook bullet ids (e.g. 'conn-00042', 'agent-00007') "
                                    "that you actually relied on for this analysis. "
                                    "Leave empty if no playbook bullets applied."
                                )
                            },
                            "flagged_bullet_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "ACE playbook bullet ids that conflicted with the evidence "
                                    "and should be flagged as harmful in the next reflection."
                                )
                            }
                        },
                        "required": [
                            "root_cause_summary", "confidence_score",
                            "recommended_actions", "involved_skills", "markdown_summary"
                        ]
                    }
                }
            }
        ]