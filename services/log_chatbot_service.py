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
import shutil
import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path
from collections import deque
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from pydantic import BaseModel

from utils import helpers
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
        expert_rules = val.get("expert_rules", "Please analyze the logs.")

        if not isinstance(keywords, list):
            keywords = [str(keywords)]

        skills[key] = Skill(
            name=name,
            description=description,
            keywords=keywords,
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

    def __init__(self, client, model: str = "gpt-4.1",
                 data_dir: Optional[str] = None,
                 skills: Optional[Dict[str, "Skill"]] = None):
        self.client = client
        self.model  = model
        self.current_log_path: str = ""
        self.conversation_history: List[dict] = []
        self.issue_context: dict = {}  # populated by prime_with_context()
        self.issue_time: Optional[datetime] = None  # populated by analyze_all()

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

    def get_skill_names(self) -> List[str]:
        return list(self.skills.keys())

    def get_skill_descriptions(self) -> List[dict]:
        return [{"name": s.name, "description": s.description}
                for s in self.skills.values()]

    # ------------------------------------------------------------------
    # Step 1 – TAT keyword filter: same pipeline as log_parser_service
    # ------------------------------------------------------------------
    def _get_filtered_log_lines(self, skill_name: str) -> str:
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
        if not self.current_log_path:
            return "Error: No log file has been set. Please set a log file path first."

        try:
            log_lines = helpers.read_log_file(self.current_log_path)
        except Exception as e:
            return f"Error reading log file: {e}"

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

        if not grouped:
            return "No log lines matched the keywords for this skill."

        return "\n".join(str(l) for l in grouped)[:15000]

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
        ai_summary   = self.issue_context.get("ai_summary", "")
        if case_nbr:
            context_lines.append(f"Case: {case_nbr}")
        if subject:
            context_lines.append(f"Subject: {subject}")
        if issue_type:
            context_lines.append(f"Issue type: {issue_type}")
        if description:
            context_lines.append(f"\nIssue description:\n{description}")
        if ai_summary:
            context_lines.append(f"\nAI summary:\n{ai_summary}")
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
            return response.choices[0].message.content or ""
        except Exception as e:
            return f"Skill analysis error: {e}"

    # ------------------------------------------------------------------
    # Combined helper used by analyze_all (filter → analyse in one call)
    # ------------------------------------------------------------------
    def fetch_filtered_logs(self, skill_name: str) -> str:
        """Filter the log then analyse with the skill's expert prompt."""
        skill = self.skills.get(skill_name)
        if not skill:
            return f"Error: skill '{skill_name}' not found."
        filtered = self._get_filtered_log_lines(skill_name)
        return self._analyze_with_skill_prompt(skill, filtered)

    # ------------------------------------------------------------------
    # Tool: query_log_detail — read specific log line + context
    # ------------------------------------------------------------------
    def query_log_detail(self, line_number: int, context_lines: int = 25) -> str:
        """
        Query the full content of a specific log line plus context lines.
        Returns the target line + context_lines before and after.
        Default: ±25 lines to capture full event sequences (e.g. beacon
        loss escalation from initial threshold to extended threshold).
        """
        if not self.current_log_path:
            return "Error: No log file loaded."
        
        try:
            from utils.helpers import read_log_file
            all_lines = read_log_file(self.current_log_path)
        except Exception as e:
            return f"Error reading log file: {e}"
        
        if line_number < 1 or line_number > len(all_lines):
            return f"Error: Line {line_number} out of range (log has {len(all_lines)} lines)."
        
        # Convert to 0-based index
        idx = line_number - 1
        start_idx = max(0, idx - context_lines)
        end_idx = min(len(all_lines), idx + context_lines + 1)
        
        result_lines = []
        for i in range(start_idx, end_idx):
            marker = ">>>" if i == idx else "   "
            result_lines.append(f"{marker} [Line {i + 1}] {all_lines[i]}")
        
        return "\n".join(result_lines)

    # ------------------------------------------------------------------
    # Simple chat (no tool loop — plain LLM conversation)
    # ------------------------------------------------------------------
    def simple_chat(self, user_message: str) -> dict:
        """
        Process one user message with a straightforward LLM call.
        Maintains conversation history for follow-ups, but does NOT
        invoke tools / agentic reasoning.  Used by the /chat endpoint
        so users can ask free-form questions after the initial
        skill-agent analysis.
        """
        if not self.conversation_history:
            # Read a snippet of the log for context (first 500 lines)
            log_snippet = ""
            if self.current_log_path:
                try:
                    from utils.helpers import read_log_file
                    lines = read_log_file(self.current_log_path)
                    log_snippet = "\n".join(str(l) for l in lines[:500])
                except Exception:
                    log_snippet = "(unable to read log file)"

            system_msg = (
                "You are a Wi-Fi troubleshooting assistant. "
                "Answer the user's questions about the log file concisely.\n"
            )
            if self.issue_context:
                ctx_parts = [f"{k}: {v}" for k, v in self.issue_context.items() if v]
                if ctx_parts:
                    system_msg += "Case context:\n" + "\n".join(ctx_parts) + "\n"
            if log_snippet:
                system_msg += f"\n--- Log excerpt (first 500 lines) ---\n{log_snippet}\n"

            self.conversation_history.append({
                "role": "system",
                "content": system_msg,
            })

        self.conversation_history.append({"role": "user", "content": user_message})

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=self.conversation_history,
                temperature=0.2,
                max_tokens=4000,
            )
            content = response.choices[0].message.content or ""
            self.conversation_history.append({"role": "assistant", "content": content})
            return {"type": "text", "data": content}
        except Exception as e:
            return {"type": "text", "data": f"LLM error: {e}"}

    # ------------------------------------------------------------------
    # Single-question chat (maintains conversation history)
    # ------------------------------------------------------------------
    def chat(self, user_message: str, max_steps: int = 6) -> dict:
        """Process one user message using the agent loop."""
        if not self.conversation_history:
            self.conversation_history.append({
                "role": "system",
                "content": (
                    "You are a Wi-Fi troubleshooting assistant with logical reasoning.\n"
                    f"Available skills: {', '.join(self.skills.keys())}.\n"
                    "Use fetch_filtered_logs with the most relevant skill to gather "
                    "evidence, then call submit_final_report.\n"
                    "Use conversation history for follow-up questions."
                )
            })

        self.conversation_history.append({"role": "user", "content": user_message})

        tools = self._build_tools()
        final_report = None

        for _ in range(max_steps):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=self.conversation_history,
                tools=tools,
                tool_choice="auto",
                temperature=0.1,
            )
            message = response.choices[0].message
            self.conversation_history.append(message)

            if message.tool_calls:
                # Process ALL tool calls and append their responses BEFORE
                # returning, so every tool_call_id is answered in history.
                final_report = None
                for tool_call in message.tool_calls:
                    args = json.loads(tool_call.function.arguments)
                    if tool_call.function.name == "submit_final_report":
                        final_report = args
                        # Provide a required tool response for this call_id
                        self.conversation_history.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": "submit_final_report",
                            "content": "Report submitted.",
                        })
                    elif tool_call.function.name == "fetch_filtered_logs":
                        skill_name = args.get("skill_name")
                        skill = self.skills.get(skill_name)
                        # Step 1: apply TAT keyword filter
                        filtered_lines = self._get_filtered_log_lines(skill_name)
                        # Step 2: analyse filtered lines with the skill's expert prompt
                        tool_result = (
                            self._analyze_with_skill_prompt(skill, filtered_lines)
                            if skill else filtered_lines
                        )
                        self.conversation_history.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": "fetch_filtered_logs",
                            "content": tool_result,
                        })
                    elif tool_call.function.name == "query_log_detail":
                        line_number = args.get("line_number")
                        context_lines = args.get("context_lines", 5)
                        tool_result = self.query_log_detail(line_number, context_lines)
                        self.conversation_history.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": "query_log_detail",
                            "content": tool_result,
                        })

                if final_report is not None:
                    return {"type": "report", "data": final_report}
            else:
                # Plain text answer (no tool call)
                content = message.content or ""
                return {"type": "text", "data": content}

        return {
            "type": "error",
            "data": "Reached maximum reasoning steps without a conclusion."
        }

    # ------------------------------------------------------------------
    # Time-aware log filtering (ported from AI_analysis_log_CFE.py)
    # ------------------------------------------------------------------
    def _parse_log_timestamp(self, log_line: str) -> Optional[datetime]:
        """Extract timestamp matching format: MM/DD/YYYY-HH:MM:SS.mmm"""
        time_pattern = re.compile(r'(\d{2}/\d{2}/\d{4}-\d{2}:\d{2}:\d{2}\.\d{3})')
        match = time_pattern.search(log_line)
        if match:
            try:
                return datetime.strptime(match.group(1), "%m/%d/%Y-%H:%M:%S.%f")
            except ValueError:
                return None
        return None

    def _detect_physical_disconnect_time(
        self, reference_time: Optional[datetime], window_minutes: int = 5
    ) -> Optional[datetime]:
        """
        Scan the raw log file for the first physical disconnect event
        near `reference_time`.  Returns the timestamp of that event,
        which may differ from the user-reported time (e.g. beacon loss
        at 11:25:49 vs. browser noticing at 11:26:25).

        Searches for:
          DISCONNECT, DEAUTH, TASK_DISCONNECT, TERMINATION
        """
        if not self.current_log_path:
            return None

        DISCONNECT_RE = re.compile(
            r'DISCONNECT|DEAUTH|TASK_DISCONNECT|TERMINATION',
            re.IGNORECASE,
        )
        ts_re = re.compile(r'(\d{2}/\d{2}/\d{4}-\d{2}:\d{2}:\d{2}\.\d{3})')

        if reference_time:
            window_start = reference_time - timedelta(minutes=window_minutes)
            window_end   = reference_time + timedelta(minutes=1)
        else:
            window_start = window_end = None

        try:
            with open(self.current_log_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    clean = line.strip()
                    if not clean:
                        continue
                    m_ts = ts_re.search(clean)
                    if not m_ts:
                        continue
                    try:
                        line_time = datetime.strptime(m_ts.group(1), "%m/%d/%Y-%H:%M:%S.%f")
                    except ValueError:
                        continue
                    # Skip lines outside the time window
                    if window_start and (line_time < window_start or line_time > window_end):
                        continue
                    # Check for disconnect keyword
                    if DISCONNECT_RE.search(clean):
                        return line_time
        except Exception as e:
            print(f"[!] Physical disconnect scan failed: {e}")
        return None

    def _extract_issue_time(self, issue_description: str) -> Optional[datetime]:
        """Use LLM to extract the exact issue time from the user's description."""
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
            time_str = response.choices[0].message.content.strip()
            if time_str != "NONE":
                return datetime.strptime(f"{time_str}.000", "%m/%d/%Y-%H:%M:%S.%f")
        except Exception as e:
            print(f"[!] Time extraction failed: {e}")
        return None

    # ------------------------------------------------------------------
    # Tool 1: scan_overview — quick keyword statistics (no log content)
    # ------------------------------------------------------------------
    def scan_overview(self, skill_name: str) -> str:
        """
        Scan the log with ALL skill keywords in the ±5min window,
        return ONLY a statistical summary — no actual log lines.
        This lets the LLM see the keyword distribution and decide
        which keywords to focus on in fetch_focused_logs.
        """
        print(f"   [Scan Overview] Scanning keyword stats for skill: '{skill_name}'...")
        skill = self.skills.get(skill_name)
        if not skill:
            return f"Error: Skill '{skill_name}' not found."
        if not self.current_log_path:
            return "Error: No log file loaded."

        keywords = skill.keywords
        if getattr(skill, 'tat_path', None) and Path(skill.tat_path).exists():
            from utils.log_parser_preprocess import extract_enabled_keywords_from_filter_file
            keywords = extract_enabled_keywords_from_filter_file(skill.tat_path)

        if not keywords:
            return "No keywords available for this skill."

        time_re = re.compile(r'(\d{2}/\d{2}/\d{4}-\d{2}:\d{2}:\d{2}\.\d{3})')
        kw_counts_disconnect = {kw: 0 for kw in keywords}  # ±5 min around disconnect
        kw_counts_init       = {kw: 0 for kw in keywords}  # boot → first 30 min of log
        first_ts = last_ts = None
        log_start_time: Optional[datetime] = None

        # Window 1: ±5 min around the physical disconnect
        if self.issue_time:
            w1_start = self.issue_time - timedelta(minutes=5)
            w1_end   = self.issue_time + timedelta(minutes=1)
        else:
            w1_start = w1_end = None

        try:
            with open(self.current_log_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    clean = line.strip()
                    if not clean:
                        continue
                    m = time_re.search(clean)
                    line_time: Optional[datetime] = None
                    if m:
                        if first_ts is None:
                            first_ts = m.group(1)
                            try:
                                log_start_time = datetime.strptime(m.group(1), "%m/%d/%Y-%H:%M:%S.%f")
                            except ValueError:
                                pass
                        last_ts = m.group(1)
                        try:
                            line_time = datetime.strptime(m.group(1), "%m/%d/%Y-%H:%M:%S.%f")
                        except ValueError:
                            pass

                    # Window 2: boot → log_start + 30 min (connection init period)
                    in_init = (
                        log_start_time is not None and line_time is not None and
                        line_time <= log_start_time + timedelta(minutes=30)
                    )
                    # Window 1: ±5 min around disconnect
                    in_disconnect = (
                        w1_start is not None and line_time is not None and
                        w1_start <= line_time <= w1_end
                    )

                    for kw in keywords:
                        if re.search(re.escape(kw), clean, re.IGNORECASE):
                            if in_disconnect:
                                kw_counts_disconnect[kw] += 1
                            if in_init:
                                kw_counts_init[kw] += 1
                            break  # count each line once per window check
        except FileNotFoundError:
            return f"Error: Log file not found at {self.current_log_path}"

        total_disconnect = sum(kw_counts_disconnect.values())
        total_init       = sum(kw_counts_init.values())

        # Build summary — two sections so LLM understands where events live
        w1_label = (
            f"{w1_start.strftime('%H:%M:%S')} ~ {w1_end.strftime('%H:%M:%S')}"
            if w1_start else "full log"
        )
        init_end_label = (
            f"{(log_start_time + timedelta(minutes=30)).strftime('%H:%M:%S')}"
            if log_start_time else "N/A"
        )
        summary_lines = [
            f"=== Skill: {skill_name} — Keyword Scan Overview ===",
            f"Log span: {first_ts or 'N/A'} → {last_ts or 'N/A'}",
            f"",
            f"── Window A: Near-Disconnect ({w1_label}) ── {total_disconnect} matched lines",
        ]
        for kw, cnt in sorted(kw_counts_disconnect.items(), key=lambda x: -x[1]):
            if cnt:
                bar = "█" * min(cnt // 5, 30)
                summary_lines.append(f"  {kw:30s} → {cnt:5d}  {bar}")
        summary_lines.append(f"")
        summary_lines.append(
            f"── Window B: Connection-Init (boot ~ {init_end_label}) ── {total_init} matched lines"
        )
        for kw, cnt in sorted(kw_counts_init.items(), key=lambda x: -x[1]):
            if cnt:
                bar = "█" * min(cnt // 5, 30)
                summary_lines.append(f"  {kw:30s} → {cnt:5d}  {bar}")

        summary_lines.append(f"\n=== Expert Rules ===")
        summary_lines.append(skill.expert_rules)
        summary_lines.append(f"\n=== Instruction ===")
        summary_lines.append(
            "Use Window A keywords to diagnose the disconnect event. "
            "Use Window B keywords (if any appear there but NOT in Window A) as clues "
            "for root causes set during connection init — call fetch_focused_logs with "
            "those keywords and seconds_before=7200 to retrieve the full init context."
        )

        return "\n".join(summary_lines)

    # ------------------------------------------------------------------
    # Tool 2: fetch_focused_logs — LLM specifies which keywords & time range
    # ------------------------------------------------------------------
    def fetch_focused_logs(self, skill_name: str, focus_keywords: List[str],
                           seconds_before: int = 60, seconds_after: int = 10) -> str:
        """
        Fetch actual log lines using ONLY the keywords the LLM chose,
        within a narrow time window the LLM specified.
        This mimics how an engineer works: find the anchor, look back for cause.
        """
        print(f"   [Focused Fetch] skill='{skill_name}', keywords={focus_keywords}, "
              f"window=-{seconds_before}s/+{seconds_after}s")
        skill = self.skills.get(skill_name)
        if not skill:
            return f"Error: Skill '{skill_name}' not found."
        if not self.current_log_path:
            return "Error: No log file loaded."
        if not focus_keywords:
            return "Error: No focus_keywords provided. Specify which keywords to search for."

        pattern = re.compile("|".join(map(re.escape, focus_keywords)), re.IGNORECASE)
        filtered_lines = []
        buffer = deque(maxlen=2)
        after_counter = 0

        time_filter = self.issue_time is not None
        if time_filter:
            start_time = self.issue_time - timedelta(seconds=seconds_before)
            end_time = self.issue_time + timedelta(seconds=seconds_after)
            print(f"   [Time Filter] {start_time.strftime('%H:%M:%S')} to {end_time.strftime('%H:%M:%S')}")

        try:
            with open(self.current_log_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line_num, line in enumerate(f, 1):
                    clean = line.strip()
                    if not clean:
                        continue
                    if time_filter:
                        line_time = self._parse_log_timestamp(clean)
                        if line_time:
                            if line_time < start_time:
                                continue
                            if line_time > end_time:
                                break
                    if pattern.search(clean):
                        while buffer:
                            filtered_lines.append(buffer.popleft())
                        filtered_lines.append(f"[Line {line_num}] {clean}")
                        after_counter = 2
                    else:
                        if after_counter > 0:
                            filtered_lines.append(f"[Line {line_num}] {clean}")
                            after_counter -= 1
                        else:
                            buffer.append(f"[Line {line_num}] {clean}")
        except FileNotFoundError:
            return f"Error: Log file not found at {self.current_log_path}"

        unique = list(dict.fromkeys(filtered_lines))

        if not unique:
            return (f"=== Expert Rules ===\n{skill.expert_rules}\n\n"
                    f"=== Focused Logs ({', '.join(focus_keywords)}) ===\n"
                    f"No matching logs found in the specified time window.")

        # Compress and apply budget
        compressed = self._compress_repetitive_lines(unique)
        TOTAL_BUDGET = 25000
        expert_str = f"=== Expert Rules ===\n{skill.expert_rules}\n\n"
        logs_budget = TOTAL_BUDGET - len(expert_str) - 200
        logs_str = "\n".join(compressed)
        if len(logs_str) > logs_budget:
            logs_str = logs_str[:logs_budget] + "\n... (truncated)"

        return (f"{expert_str}"
                f"=== Focused Logs ({', '.join(focus_keywords)}, "
                f"-{seconds_before}s/+{seconds_after}s) ===\n"
                f"{len(unique)} lines matched\n\n{logs_str}")

    @staticmethod
    def _compress_simple(lines: List[str], similarity_threshold: int = 5) -> List[str]:
        """
        Simple compression: Collapse consecutive lines with same pattern.
        Does NOT lose any critical logs — just compresses repetitive noise.
        """
        if not lines or len(lines) < similarity_threshold:
            return lines

        # Pattern to extract the meaningful part (strip line number and timestamp)
        PREFIX_PATTERN = re.compile(r'^\[Line \d+\]\s*\d{2}/\d{2}/\d{4}-\d{2}:\d{2}:\d{2}\.\d{3}\s*')
        
        def get_core(line: str) -> str:
            """Extract core message by removing line number and timestamp"""
            return PREFIX_PATTERN.sub('', line)
        
        result = []
        i = 0
        while i < len(lines):
            current_core = re.sub(r'-?\d+', '#', get_core(lines[i]))[:80]  # Normalize numbers
            
            # Count consecutive lines with same pattern
            j = i + 1
            while j < len(lines):
                next_core = re.sub(r'-?\d+', '#', get_core(lines[j]))[:80]
                if next_core != current_core:
                    break
                j += 1
            
            run_length = j - i
            if run_length >= similarity_threshold:
                # Collapse: show first, middle hint, last
                result.append(lines[i])
                result.append(f"    ... [×{run_length-2} similar] ...")
                result.append(lines[j-1])
                i = j
            else:
                # Keep all if not many repeats
                result.extend(lines[i:j])
                i = j
        
        return result

    def _scan_log(self, pattern: re.Pattern, start_time: Optional[datetime],
                  end_time: Optional[datetime]) -> list:
        """Scan the log file for keyword matches within a time window."""
        filtered_lines = []
        buffer = deque(maxlen=2)
        after_counter = 0

        try:
            with open(self.current_log_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line_num, line in enumerate(f, 1):
                    clean_line = line.strip()
                    if not clean_line:
                        continue

                    if start_time or end_time:
                        line_time = self._parse_log_timestamp(clean_line)
                        if line_time:
                            if start_time and line_time < start_time:
                                continue
                            if end_time and line_time > end_time:
                                break

                    if pattern.search(clean_line):
                        while buffer:
                            filtered_lines.append(buffer.popleft())
                        filtered_lines.append(f"[Line {line_num}] {clean_line}")
                        after_counter = 2
                    else:
                        if after_counter > 0:
                            filtered_lines.append(f"[Line {line_num}] {clean_line}")
                            after_counter -= 1
                        else:
                            buffer.append(f"[Line {line_num}] {clean_line}")
        except FileNotFoundError:
            return [f"Error: Log file not found at {self.current_log_path}"]

        return list(dict.fromkeys(filtered_lines))

    @staticmethod
    def _compress_repetitive_lines(lines: List[str], similarity_threshold: int = 3) -> List[str]:
        """
        Detect consecutive lines that share the same 'signature' and collapse
        them into a summary line.  This prevents verbose roaming/scan loops
        from eating the entire token budget.

        CRITICAL: Lines containing state-change keywords (e.g., 'crossed',
        'threshold', 'DISCONNECT', 'DEAUTH') are NEVER compressed — they
        represent key diagnostic events that must be visible to the LLM.
        """
        if not lines:
            return lines

        # Keywords that mark a line as diagnostically critical — never compress
        CRITICAL_KEYWORDS = re.compile(
            r'crossed the|DISCONNECT|DEAUTH|DEAUTH_REQ|TERMINATION|'
            r'CONNECTED - to|TASK_DISCONNECT|RESUME FLOW|for the first time',
            re.IGNORECASE
        )

        SIG_RE = re.compile(
            r'^\[Line \d+\]\s*'
            r'\d{2}/\d{2}/\d{4}-'
            r'\d{2}:\d{2}:\d{2}\.\d{3}\s*'
        )
        TS_RE = re.compile(r'(\d{2}/\d{2}/\d{4}-\d{2}:\d{2}:\d{2})')

        def get_sig(line: str) -> str:
            """
            Return a normalized signature for compression comparison.
            Strips timestamp/line-number prefix and replaces variable numbers
            with '#' so lines like 'rssi changed to -53' and 'rssi changed to -54'
            are treated as the same pattern.
            """
            stripped = SIG_RE.sub('', line).strip()
            # Normalize numbers (including negative) to '#' for comparison
            normalized = re.sub(r'-?\d+', '#', stripped)
            return normalized[:100]

        def get_ts(line: str) -> str:
            m = TS_RE.search(line)
            return m.group(1) if m else ""

        def is_critical(line: str) -> bool:
            """True if this line contains a state-change keyword that must not be compressed."""
            return bool(CRITICAL_KEYWORDS.search(line))

        result = []
        i = 0
        while i < len(lines):
            # Never compress a critical line
            if is_critical(lines[i]):
                result.append(lines[i])
                i += 1
                continue

            sig = get_sig(lines[i])
            if len(sig) < 10:
                result.append(lines[i])
                i += 1
                continue

            # Count consecutive non-critical lines that share this signature
            j = i + 1
            while j < len(lines) and not is_critical(lines[j]) and get_sig(lines[j]) == sig:
                j += 1

            run_length = j - i
            if run_length >= similarity_threshold:
                first_ts = get_ts(lines[i])
                last_ts = get_ts(lines[j - 1])
                label = sig[:60].rstrip()
                result.append(lines[i])
                result.append(
                    f"    ... [×{run_length - 2} similar entries: "
                    f"`{label}`, {first_ts} → {last_ts}] ..."
                )
                result.append(lines[j - 1])
            else:
                result.extend(lines[i:j])
            i = j

        return result

    # ------------------------------------------------------------------
    # Analyze ALL skills — Agentic Reasoning Loop
    # ------------------------------------------------------------------
    def analyze_all(self, issue_description: str = "Perform full log analysis",
                     step_callback=None, issue_context: dict = None) -> dict:
        """
        Agentic loop: LLM autonomously decides which skills to invoke,
        gathers evidence step by step, then produces a structured report.
        
        Args:
            issue_description: concise problem statement (used for time extraction + skill selection)
            step_callback: optional callable(step_dict) — called immediately
                           each time a new step is produced, enabling real-time
                           streaming to the frontend via SSE.
            issue_context: optional dict with complete background context
                          (case_nbr, subject, description, issue_type, ai_summary)
                          injected into system prompt for LLM to use when writing reports
        
        Returns dict with 'type', 'data' (report), and 'steps' (decision trail).
        """
        # Store context for potential use by submit_final_report
        self.issue_context = issue_context or {}
        steps = []

        def _emit(step):
            steps.append(step)
            if step_callback:
                step_callback(step)
        _emit({
            "role": "system",
            "content": f"🎯 **Starting Full Multi-Skill Agent Analysis**\n"
                       f"**Issue:** {issue_description}\n"
                       f"**Log:** `{self.current_log_path}`"
        })

        # Step 1: Extract issue time using LLM (user-reported time)
        user_reported_time = self._extract_issue_time(issue_description)

        # Step 1b: Detect the PHYSICAL disconnect time from the log file
        # (e.g. beacon loss at 11:25:49 vs browser disconnect at 11:26:25)
        physical_time = self._detect_physical_disconnect_time(user_reported_time)

        # Use physical time for filtering if found; fall back to user time
        issue_time = physical_time or user_reported_time
        self.issue_time = issue_time
        # Store physical_disconnect_time for report display
        self.physical_disconnect_time = physical_time

        if physical_time and user_reported_time:
            _emit({
                "role": "agent",
                "content": (
                    f"🕒 **Physical Disconnect Time:** `{physical_time.strftime('%m/%d/%Y %H:%M:%S')}`\n"
                    f"(User-reported: `{user_reported_time.strftime('%m/%d/%Y %H:%M:%S')}`, "
                    f"delta: {abs((user_reported_time - physical_time).total_seconds()):.0f}s)\n"
                    f"Log filtering will focus ±5 minutes around the physical event."
                )
            })
        elif physical_time:
            _emit({
                "role": "agent",
                "content": f"🕒 **Physical Disconnect Time:** `{physical_time.strftime('%m/%d/%Y %H:%M:%S')}`\n"
                           f"Log filtering will focus ±5 minutes around this time."
            })
        elif user_reported_time:
            _emit({
                "role": "agent",
                "content": f"🕒 **User-Reported Time:** `{user_reported_time.strftime('%m/%d/%Y %H:%M:%S')}`\n"
                           f"(No physical disconnect event found in log near this time.)\n"
                           f"Log filtering will focus ±5 minutes around this time."
            })
        else:
            _emit({
                "role": "agent",
                "content": "⚠️ Could not extract a specific time from the description. "
                           "Will scan the entire log file."
            })

        # Step 2: Build tools and system prompt
        tools = self._build_tools()
        
        # Inject complete context as background data for LLM's report writing
        context_section = ""
        if issue_context:
            case_nbr = issue_context.get("case_nbr", "")
            subject = issue_context.get("subject", "")
            raw_desc = issue_context.get("description", "")
            issue_type = issue_context.get("issue_type", "")
            ai_summary = issue_context.get("ai_summary", "")
            
            context_parts = []
            if case_nbr:
                context_parts.append(f"**Case Number:** {case_nbr}")
            if issue_type:
                context_parts.append(f"**Issue Type:** {issue_type}")
            if subject:
                context_parts.append(f"**Subject:** {subject}")
            if ai_summary:
                context_parts.append(f"**Quick Summary:** {ai_summary}")
            if raw_desc and raw_desc != (subject or ""):
                # Only include if different from subject
                desc_preview = raw_desc[:300] + ("..." if len(raw_desc) > 300 else "")
                context_parts.append(f"**Full Description:** {desc_preview}")
            
            if context_parts:
                context_section = "\n=== BACKGROUND CONTEXT (for report writing) ===\n" + "\n".join(context_parts) + "\n\n"

        # Build Step 4 instruction from Issue_summary.Symptom (via issue_context["ai_summary"])
        # directly — no roundtrip through description string.
        symptom_text = (issue_context or {}).get("ai_summary", "").strip()
        if symptom_text:
            step4_instruction = (
                f"  Step 4 — 🕵️ HYPOTHESIS TESTING (Context-Driven 2nd Pass):\n"
                f"           Symptom context: \"{symptom_text[:300]}\"\n"
                "           Review the BACKGROUND CONTEXT above. If it mentions specific anomalies,\n"
                "           tools, or configuration changes, those are your PRIMARY SUSPECTS.\n"
                "           You MUST call `fetch_focused_logs` a SECOND TIME with these rules:\n"
                "           a) Use the EXACT anomaly/tool names from context as `focus_keywords`.\n"
                "              DO NOT reuse symptom keywords (MISSED BEACONS, DEAUTH, etc.).\n"
                "           b) Set `seconds_before=7200` (2 hours) to catch setup-stage events.\n"
                "           If context has NO specific tools/anomalies, SKIP to Step 5.\n"
            )
        else:
            step4_instruction = "  Step 4 — No symptom context found. SKIP directly to Step 5.\n"

        messages = [
            {
                "role": "system",
                "content": (
                    f"{context_section}"
                    "You are an Elite Wi-Fi Diagnostic Detective. Your GOAL: Find the REAL Root Cause based on evidence.\n"
                    f"Available skills: {', '.join(self.skills.keys())}.\n\n"
                    "OPTIMIZED WORKFLOW (STRICT TWO-PHASE INVESTIGATION):\n"
                    "1. Call `scan_overview` on ONLY the 1 most relevant skill.\n"
                    "2. PHASE 1 (SYMPTOM LOCALIZATION): \n"
                    "   - Identify the exact timestamp when the reported failure occurred in the logs.\n"
                    "   - Call `fetch_focused_logs` using keywords derived from the immediate physical symptoms.\n"
                    "3. PHASE 2 (SOURCE RETROSPECTIVE - MANDATORY):\n"
                    "   - Analyze the 'User Issue' below. Identify any specific software tools, configuration parameters, or anomaly names mentioned as context.\n"
                    "   - You MUST execute a SECOND `fetch_focused_logs` specifically to find the 'Trigger Event'.\n"
                    "   - 🛑 KEYWORD RULE: Use the unique terms extracted from the 'User Issue' as your keywords. DO NOT reuse physical symptom keywords from Phase 1.\n"
                    "   - 🛑 TIME JUMP: Always set `seconds_before=7200` (2 hours) for this fetch to capture connection-stage or setup events that occurred long before the failure.\n"
                    "4. Call `query_log_detail` on the transition point between the Trigger Event and the Physical Failure.\n"
                    "5. 🧠 CAUSAL SYNTHESIS:\n"
                    "   - You MUST explain the chain of causality: How did the [Trigger Event] lead to the [Physical Failure]?\n"
                    "   - State the root cause in your own words based on raw evidence, not just the provided expert rules.\n"
                    "6. ⚠️ DOMAIN ACCURACY: Ensure your analysis adheres to the physical characteristics of the frequency band (2.4GHz/5GHz/6GHz) currently in use.\n"
                    "7. Call `submit_final_report` to conclude.\n\n"
                    "CRITICAL CONSTRAINTS:\n"
                    "- Max step 8.\n"
                    "- 🛑 NO REPETITION: Do not fetch the same data twice. If Phase 1 keywords are found in Phase 2, ignore them.\n"
                    "- 🛑 DO NOT SCROLL: You may call `query_log_detail` strictly ONLY ONCE. Set `context_lines=30` to see the full escalation chain in one shot.\n"
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
            },
            {"role": "user", "content": f"User Issue: {issue_description}"}
        ]
        max_steps = 8
        # Global accumulated log: merge all focused fetches, dedup by line number
        global_log_lines = []  # list of "[Line XXXXX] ..." strings
        global_log_seen = set()  # track line numbers already added

        def _merge_into_global(tool_output: str) -> str:
            """
            Extract log lines from tool output, merge new ones into global_log,
            return the updated global log as a single string for LLM context.
            """
            new_count = 0
            for line in tool_output.split('\n'):
                # Only merge actual log lines (start with [Line)
                if line.startswith('[Line '):
                    # Extract line number for dedup
                    m = re.match(r'\[Line (\d+)\]', line)
                    if m:
                        ln = m.group(1)
                        if ln not in global_log_seen:
                            global_log_seen.add(ln)
                            global_log_lines.append(line)
                            new_count += 1
            # Sort by line number for chronological order
            global_log_lines.sort(key=lambda l: int(re.match(r'\[Line (\d+)\]', l).group(1)) if re.match(r'\[Line (\d+)\]', l) else 0)
            return f"[Global Log: {len(global_log_lines)} unique lines, {new_count} new from this fetch]"

        # Step 3: Agentic reasoning loop
        for step_num in range(max_steps):
            _emit({
                "role": "agent",
                "content": f"💭 **Reasoning Step {step_num + 1}/{max_steps}** — "
                           f"Sending context to LLM..."
            })

            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    temperature=0.1,
                )
            except Exception as e:
                _emit({"role": "error", "content": f"❌ LLM API Error: {e}"})
                return {
                    "type": "error",
                    "data": {"error": str(e)},
                    "steps": steps,
                }

            message = response.choices[0].message
            messages.append(message)

            # If LLM returned plain text (thinking aloud), record it FULLY (no truncation)
            if message.content:
                _emit({
                    "role": "agent",
                    "content": f"🧠 **LLM Reasoning:**\n{message.content[:500]}"
                })

            if message.tool_calls:
                for tool_call in message.tool_calls:
                    args = json.loads(tool_call.function.arguments)

                    if tool_call.function.name == "submit_final_report":
                        _emit({
                            "role": "agent",
                            "content": "✅ **Conclusion Reached!** Generating final structured report."
                        })
                        # Inject analysis results into conversation_history
                        # so subsequent chat() calls have full context
                        self._inject_analysis_into_history(
                            issue_description, steps, args
                        )
                        return {
                            "type": "report",
                            "data": args,
                            "steps": steps,
                            "physical_disconnect_time": (
                                self.physical_disconnect_time.strftime('%m/%d/%Y %H:%M:%S')
                                if getattr(self, 'physical_disconnect_time', None)
                                else None
                            ),
                        }

                    elif tool_call.function.name == "scan_overview":
                        skill_name = args.get("skill_name")
                        _emit({
                            "role": "agent",
                            "content": f"📊 **Scanning** keyword statistics for `{skill_name}`..."
                        })

                        tool_result = self.scan_overview(skill_name)

                        line_count = tool_result.count('\n')
                        preview = tool_result[:500].replace('\n', ' ') + "..."
                        _emit({
                            "role": "tool",
                            "content": f"📄 **Scan Overview** (`{skill_name}`, {line_count} lines):\n"
                                       f"```\n{preview}\n```"
                        })

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.function.name,
                            "content": tool_result,
                        })

                    elif tool_call.function.name == "fetch_focused_logs":
                        skill_name = args.get("skill_name")
                        focus_kw = args.get("focus_keywords", [])
                        sec_before = args.get("seconds_before", 60)
                        sec_after = args.get("seconds_after", 10)
                        _emit({
                            "role": "agent",
                            "content": f"🔍 **Focused Fetch** `{skill_name}` — "
                                       f"keywords: {focus_kw}, window: -{sec_before}s/+{sec_after}s"
                        })

                        tool_result = self.fetch_focused_logs(
                            skill_name, focus_kw, sec_before, sec_after
                        )

                        # Merge new log lines into global_log (dedup by line number)
                        merge_summary = _merge_into_global(tool_result)

                        line_count = tool_result.count('\n')
                        preview = tool_result[:500].replace('\n', ' ') + "..."
                        _emit({
                            "role": "tool",
                            "content": f"📄 **Focused Logs** (`{skill_name}`, {line_count} lines):\n"
                                       f"```\n{preview}\n```\n{merge_summary}"
                        })

                        # Build unified context: Expert Rules + merged global log
                        skill = self.skills.get(skill_name)
                        expert_rules = getattr(skill, 'expert_rules', '') if skill else ''
                        global_log_text = "\n".join(global_log_lines)

                        # Budget: keep global log within 25K chars
                        if len(global_log_text) > 25000:
                            global_log_text = global_log_text[:25000] + "\n... (truncated)"

                        unified_content = (
                            f"=== Expert Rules ===\n{expert_rules}\n\n"
                            f"=== Merged Log (all fetches combined, {len(global_log_lines)} unique lines) ===\n"
                            f"{global_log_text}"
                        )

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.function.name,
                            "content": unified_content,
                        })

                    elif tool_call.function.name == "query_log_detail":
                        line_number = args.get("line_number")
                        context_lines = args.get("context_lines", 5)
                        _emit({
                            "role": "agent",
                            "content": f"🔎 **Querying log detail** — "
                                       f"Line {line_number} (±{context_lines} lines context)"
                        })

                        tool_result = self.query_log_detail(line_number, context_lines)

                        _emit({
                            "role": "tool",
                            "content": f"📄 **Log Detail** (Line {line_number}):\n"
                                       f"```\n{tool_result[:500]}...\n```"
                        })

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.function.name,
                            "content": tool_result,
                        })
            else:
                # No tool call — nudge LLM to conclude
                messages.append({
                    "role": "user",
                    "content": "Please call `submit_final_report` to output the final results."
                })

        # Exhausted all steps without conclusion
        _emit({
            "role": "error",
            "content": f"⚠️ Reached maximum reasoning steps ({max_steps}) without explicit conclusion. "
                       f"Generating partial report from {len(global_log_lines)} log lines collected."
        })
        
        # Generate partial report from collected evidence
        partial_report = {
            "root_cause_summary": {
                                "type": "string",
                                "description": (
                                    "A crisp, definitive engineering verdict . "
                                    "DO NOT use storytelling language (e.g., 'A leading to B'). "
                                    "You MUST explicitly state the FINAL OUTCOME and include HARD METRICS "
                                    
                                )
                            },
            "confidence_score": 30,
            "recommended_actions": [
                "Review the collected log evidence in detail",
                "Consider enabling verbose driver logging for deeper analysis",
                "Contact WiFi team with this diagnostic bundle"
            ],
            "involved_skills": list(self.skills.keys()),
            "markdown_summary": (
                f"## Partial Diagnosis (Step Limit Reached)\n\n"
                f"**Evidence Collected:** {len(global_log_lines)} log lines from {len(self.skills)} skills\n\n"
                f"### Collected Logs:\n"
                f"```\n"
                f"{chr(10).join(global_log_lines[-50:])}  # Last 50 lines\n"
                f"```\n\n"
                f"**Note:** Agent exhausted reasoning steps before reaching final conclusion. "
                f"Increase analysis scope or manually review the logs above."
            )
        }
        
        return {
            "type": "partial_report",
            "data": partial_report,
            "steps": steps,
            "physical_disconnect_time": (
                self.physical_disconnect_time.strftime('%m/%d/%Y %H:%M:%S')
                if getattr(self, 'physical_disconnect_time', None)
                else None
            ),
        }

    # ------------------------------------------------------------------
    # Inject analyze_all results into conversation_history for chat()
    # ------------------------------------------------------------------
    def _inject_analysis_into_history(self, issue_description: str,
                                       steps: list, report: dict) -> None:
        """
        After analyze_all() finishes, inject a clean summary into
        self.conversation_history so subsequent chat() calls have full
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
            f"## ✅ Analysis Complete\n\n"
            f"### 🧠 Agent Reasoning Process\n{thinking_recap}\n\n"
            f"### 📊 Report\n{report_text}"
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

    def prime_with_context(self, case_nbr: str = "", subject: str = "",
                            description: str = "", issue_type: str = "", ai_summary: str = "") -> None:
        """
        Reset conversation and inject the case context as the opening system
        message so the LLM knows what issue it is analysing before the user
        asks the first question.
        """
        self.conversation_history = []
        self.issue_context = {
            "case_nbr":   case_nbr,
            "subject":    subject,
            "description": description,
            "issue_type": issue_type,
            "ai_summary": ai_summary
        }
        context_parts = []
        if case_nbr:
            context_parts.append(f"Case: {case_nbr}")
        if subject:
            context_parts.append(f"Subject: {subject}")
        if issue_type:
            context_parts.append(f"Classified issue type: {issue_type}")
        if description:
            context_parts.append(f"\nIssue description:\n{description}")
        if ai_summary:
            context_parts.append(f"\nAI summary:\n{ai_summary}")

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
                    "name": "scan_overview",
                    "description": (
                        "Quick scan: returns keyword match STATISTICS (counts per keyword) "
                        "and Expert Rules for a skill — NO actual log lines. "
                        "Use this FIRST to understand the data distribution, then call "
                        "fetch_focused_logs with the most relevant keywords."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill_name": {
                                "type": "string",
                                "enum": list(self.skills.keys()),
                                "description": "Which skill to scan"
                            }
                        },
                        "required": ["skill_name"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "fetch_focused_logs",
                    "description": (
                        "Fetch actual log lines using ONLY the keywords you specify, "
                        "within a narrow time window you choose. "
                        "Use this AFTER scan_overview to zoom into the most relevant evidence. "
                        "Pick diagnostic keywords (e.g., MISSED BEACONS, DEAUTH) over "
                        "high-volume noise keywords (e.g., OSC with 700+ lines)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill_name": {
                                "type": "string",
                                "enum": list(self.skills.keys()),
                                "description": "Which skill's Expert Rules to include"
                            },
                            "focus_keywords": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Specific keywords to search for. Choose based on "
                                    "scan_overview results — prefer low-count diagnostic "
                                    "keywords over high-count noise keywords."
                                )
                            },
                            "seconds_before": {
                                "type": "integer",
                                "description": "How many seconds before the issue time to scan (default: 60). CRITICAL: Increase to 7200 to look 2 hours back if instructed by Phase 2.",
                                "default": 60
                            },
                            "seconds_after": {
                                "type": "integer",
                                "description": "How many seconds after the issue time to scan (default: 10)"
                            }
                        },
                        "required": ["skill_name", "focus_keywords"]
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
                            }
                        },
                        "required": [
                            "root_cause_summary", "confidence_score",
                            "recommended_actions", "involved_skills", "markdown_summary"
                        ]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "query_log_detail",
                    "description": (
                        "Query a specific log line PLUS surrounding context lines. "
                        "CRITICAL: Wi-Fi events escalate across 20-50 lines (e.g. missed beacons: "
                        "initial threshold → extended threshold → disconnect). "
                        "Always use context_lines=30 to see the FULL event sequence. "
                        "NEVER use less than 15."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "line_number": {
                                "type": "integer",
                                "description": "The line number to query (e.g., 1520)"
                            },
                            "context_lines": {
                                "type": "integer",
                                "description": "How many lines before/after to show. Default 25. Use 30+ for disconnect events to see full escalation chain.",
                                "default": 25
                            }
                        },
                        "required": ["line_number"]
                    }
                }
            }
        ]