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
    return {
        "disconnection_analysis": Skill(
            name="disconnection_analysis",
            description="Used to analysis the reason for unexpected disconnection",
            keywords=["TASK_DISCONNECT", "DeAuth", "CONNECTED - to:", "TASK_Connect", "TASK_Disconnect", "DEAUTH_REQ", "reason:", "MIC failure", "timeout"],
            tat_path=None,
            expert_rules=(
                """1. List the key configuration and log lines in each of the Key steps categories below, and display all the log information contained in the log example provided by the user under Key steps and log example. Always include timestamps and the exact log text for any item you cite. Key steps categories to include:
                   1-1 Authentication and Association (AUTH_REQ/AUTH_RSP, ASSOC_REQ/ASSOC_RSP, EAPOL messages, 4-way handshake lines).
                   1-2 Connection Establishment (WDI_IND_ASSOC_RESULT or similar success/failure lines)
                   1-3 Disconnection procedure
                   2. If observed disconnection procedure at any kind, try to analysis the disconnection root cause"""
            )
        ),
        "Resume_connection_setup_time_analysis": Skill(
            name="Resume_connection_setup_time_analysis",
            description="Used to analysis the time need to setup wifi connection after PC resume",
            keywords=["TASK_DISCONNECT", "DeAuth", "CONNECTED - to", "TASK_Connect", "DEAUTH_REQ", "reason:", "RESUME FLOW STARTED", "timeout"],
            tat_path=None,
            expert_rules=(
                """1. List the key configuration and log lines in each of the Key steps categories below. Always include timestamps and the exact log text.
                   2. Each Network resume time is calculated by the time difference between keyword "RESUME FLOW STARTED" and following "CONNECTED - to"
                   3. Calculate every network resume time in the log, summarize the network resume time static by showing Average, list all the resume time > 3 sec."""
            )
        ),
        "Remote_disconnection": Skill(
            name="Remote_disconnection",
            description="Used to diagnose the reason while disconnection is not initiate by OS",
            keywords=["MISSED BEACONS", "DeAuth", "TASK_Disconnect", "ROAM_DECISION"],
            tat_path=None,
            expert_rules=(
                """1. List the key configuration and log lines. Always include timestamps and the exact log text.
                   2. If Consecutive missed beacons (19) crossed the extended threshold of 19, it means the RF signal is weak, therefore disconnection reason is Weak signal. Deauth_req would be sent to AP after.
                   3. If Deauth is received from air, then the disconnection was initiated by AP router."""
            )
        ),
    }


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
    # Analyze ALL skills and synthesise a summary
    # ------------------------------------------------------------------
    def analyze_all(self, issue_description: str = "Perform full log analysis") -> dict:
        """
        Run all skills against the log and produce a consolidated report.
        """
        per_skill_results = {}
        for skill_name in self.skills:
            filtered = self.fetch_filtered_logs(skill_name)
            per_skill_results[skill_name] = filtered

        # Build one-shot synthesis prompt
        combined_context = "\n\n".join(
            f"--- Skill: {k} ---\n{v}" for k, v in per_skill_results.items()
        )

        synthesis_messages = [
            {
                "role": "system",
                "content": (
                    "You are a senior Wi-Fi engineer. You are given filtered log excerpts "
                    "from multiple diagnostic skills. Synthesise a complete root-cause analysis.\n"
                    "Return a structured JSON with keys:\n"
                    "  root_cause_summary, confidence_score (0-100), "
                    "recommended_actions (list), skill_findings (dict of skill→findings), "
                    "markdown_summary."
                )
            },
            {
                "role": "user",
                "content": (
                    f"Issue: {issue_description}\n\n"
                    f"Multi-skill log analysis data:\n{combined_context[:30000]}"
                )
            }
        ]

        response = self.client.chat.completions.create(
            model=self.model,
            messages=synthesis_messages,
            temperature=0.2,
            max_tokens=3000,
        )
        raw = response.choices[0].message.content
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if json_match:
            try:
                return {"type": "report", "data": json.loads(json_match.group(0))}
            except json.JSONDecodeError:
                pass
        return {"type": "text", "data": raw}

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
                    "name": "fetch_filtered_logs",
                    "description": (
                        "Filter the Wi-Fi log file and retrieve lines relevant to a specific "
                        "diagnostic skill, together with expert debugging rules."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "skill_name": {
                                "type": "string",
                                "enum": list(self.skills.keys()),
                                "description": "Which skill/filter to apply"
                            }
                        },
                        "required": ["skill_name"]
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
            }
        ]
