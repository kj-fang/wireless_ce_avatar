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
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
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
#    Mirrors the shared skill folder to local disk so the agent can still
#    boot on cached files if the network share is briefly unreachable.
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
#    Skill is one skill's full definition; AgentCapabilityPolicy is the
#    per-profile switch set every method below reads instead of branching
#    on which chatbot (Wi-Fi/BT/NW) is running.
# ---------------------------------------------------------
class Skill(BaseModel):
    name: str
    description: str
    keywords: List[str]          # parsed from TAT for fallback use
    exclusive: List[str] = Field(default_factory=list)  # lines containing these terms are removed post-filter
    tat_path: Optional[str]      # path to original .tat file (preferred for filtering)
    expert_rules: str


@dataclass(frozen=True)
class AgentCapabilityPolicy:
    """Behavior switches for one chatbot profile.

    The shared engine owns the algorithms; profiles describe which optional
    behavior is active.  This prevents a second copy of the 300-570 line
    preprocessing/tool-loop methods from drifting out of sync.
    """

    profile: str = "wifi"
    disabled_tools: frozenset[str] = frozenset()
    ace_playbooks: bool = True
    cooperative_cancellation: bool = True
    repair_tool_history: bool = True
    recover_invalid_tool_arguments: bool = True
    emit_step_token_usage: bool = False
    emit_fetch_previews: bool = False
    context_issue_time_fallbacks: tuple[str, ...] = ()
    primed_issue_time_source: str = "primed"
    compact_issue_time_notice: bool = False
    show_customer_issue_time: bool = True
    diagnose_history_on_llm_error: bool = True
    merge_wrapped_time_only_logs: bool = True
    scope_time_only_logs: bool = True
    configurable_issue_window: bool = True
    full_scope_for_undated_logs: bool = True


WIFI_AGENT_POLICY = AgentCapabilityPolicy()
NW_AGENT_POLICY = AgentCapabilityPolicy(
    profile="nw",
    disabled_tools=frozenset({"softAP_supported_channel"}),
    ace_playbooks=False,
    cooperative_cancellation=False,
    repair_tool_history=False,
    recover_invalid_tool_arguments=False,
    emit_step_token_usage=True,
    emit_fetch_previews=True,
    context_issue_time_fallbacks=("description", "subject"),
    primed_issue_time_source="attachment_time",
    compact_issue_time_notice=True,
    show_customer_issue_time=False,
    diagnose_history_on_llm_error=False,
    merge_wrapped_time_only_logs=False,
    scope_time_only_logs=False,
    configurable_issue_window=False,
    full_scope_for_undated_logs=False,
)
BT_AGENT_POLICY = AgentCapabilityPolicy(
    profile="bt",
    disabled_tools=frozenset({"lookup_assert_code", "softAP_supported_channel"}),
)


# ---------------------------------------------------------
# 3. Shared-folder loaders
#    Legacy path: one .py (prompt) + one .tat (filter) file per skill, read
#    straight off disk. Superseded by the single-file YAML loader below but
#    kept as the data_dir fallback.
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
#    Currently an empty dict — skills are always loaded explicitly via YAML
#    or data_dir. This is the last-resort return type contract, not a
#    hardcoded skill set.
# ---------------------------------------------------------
def get_builtin_skills() -> Dict[str, "Skill"]:
    """
    Return an empty skill dict by default.
    Skills must be loaded explicitly via skills.yaml or the data directory.
    """
    return {}


# ---------------------------------------------------------
# 4b. Load skills from a YAML file (standalone, no prompt/filter dirs needed)
#     The primary loader in production — one skills.yaml now drives what
#     used to require a matching .py + .tat pair per skill.
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
#    Everything below assembles into WifiLogAgentSystem: behavior lives in
#    the imported mixins, this class supplies shared state, skill lifecycle,
#    and turn-level bookkeeping (token usage, ACE, capability policy).
# ---------------------------------------------------------
from services.chatbot.engine.log_scope import LogScopeMixin
from services.chatbot.engine.skill_analysis import SkillAnalysisMixin
from services.chatbot.engine.conversation import ConversationMixin
from services.chatbot.engine.report_quality import ReportQualityMixin
from services.chatbot.engine.tool_execution import ToolExecutionMixin

class WifiLogAgentSystem(
    LogScopeMixin,
    SkillAnalysisMixin,
    ConversationMixin,
    ReportQualityMixin,
    ToolExecutionMixin,
):
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
    MAX_TOKENS_PER_STEP = 75000          # 3 tools × 16K evidence = ~12K tokens/step; headroom for rules + prompt history
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
    # Budget for a persisted conversation context. Every restored message is
    # re-sent on every follow-up, so this is a recurring token cost, not a
    # one-off disk cost: 120k chars is roughly 30k tokens of grounding.
    MAX_PERSISTED_CONTEXT_CHARS = 120_000

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
    CAPABILITY_POLICY = WIFI_AGENT_POLICY

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

    # The report skeleton that used to live here as REPORT_MARKDOWN_TEMPLATE
    # is now per-profile data: <profile>_report.md under Speclets, with the
    # built-in fallback in engine/speclet_defaults.py. Keeping a class attribute
    # nothing reads would just be a trap for the next person who edits it and
    # wonders why the prompt did not change.

    def __init__(self, client, model: str = "gpt-4.1",
                 data_dir: Optional[str] = None,
                 skills: Optional[Dict[str, "Skill"]] = None):
        self.client = client
        self.model  = model
        self.capabilities = self.CAPABILITY_POLICY
        self.current_log_path: str = ""
        self.conversation_history: List[dict] = []
        # Cooperative cancellation. A background chat job (see
        # services/chatbot/job_runtime.py) sets this event when the user clicks
        # "Stop"; the agentic tools loop polls it between reasoning steps and
        # bails out early. Cleared at the start of every chat turn so a prior
        # stop can't cancel the next one.
        self.cancel_event: threading.Event = threading.Event()
        # Token usage for the CURRENT turn, accumulated across every LLM call
        # (agentic loops make many). Reset at the top of each chat() so it only
        # ever describes one turn; read by the routes after chat() returns and
        # handed to gather_service for cost accounting.
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

    # ------------------------------------------------------------------
    # Per-turn token accounting
    # ------------------------------------------------------------------
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
        """Fold one LLM response's usage into this turn's running total.

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


















    # ------------------------------------------------------------------
    # Step 2 – Skill prompt analysis: LLM sub-call with expert_rules
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Tool Handler: fetch_filtered_logs (returns compact skill-focused payload)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Utility: Quick skill-based analysis (optional one-shot mode)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Tool: query_log_detail — anchor-based context query on assembled log
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Chat: Flexible conversation with optional agentic tools
    # RECOMMENDED for chatbot dialog boxes
    # ------------------------------------------------------------------

    # Generic outcome-signal keywords (not case-specific; covers common WiFi states).
    _OUTCOME_SIGNAL_KEYWORDS = [
        "PROBE_RX", "PROBE_TX", "CONNECTED", "ASSOC_RSP", "AUTH_RSP",
        "RSSI", "RssiAdjustment", "scan is ALLOWED", "scan is DISALLOWED",
        "ALLOWED", "DISALLOWED", "ENABLED", "DISABLED",
        "Update regulatory", "NIC State",
    ]












    # ------------------------------------------------------------------
    # Inject analysis results into conversation_history for follow-up chat
    # ------------------------------------------------------------------

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
