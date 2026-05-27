"""
Reflector and Curator roles — the two LLM-driven components of ACE.

Both call the existing `LLM_helper.chat()` for the actual completion so the
agent and ACE share one model client + API plumbing.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from . import prompts
from .playbook import Playbook


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(raw: str) -> dict:
    """
    LLMs sometimes wrap JSON in ```json ... ``` fences or add a stray sentence.
    Pull out the first top-level JSON object and parse it. Raises on failure.
    """
    if not raw:
        raise ValueError("empty LLM response")
    raw = raw.strip()
    # Strip code fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    m = _JSON_OBJECT_RE.search(raw)
    if not m:
        raise ValueError(f"no JSON object found in LLM response: {raw[:200]}")
    return json.loads(m.group(0))


def _truncate_trace(steps: list[dict], max_chars: int = 6000) -> str:
    """
    Steps traces from log_chatbot_service can be huge (full log dumps). Cap
    them so the Reflector prompt stays in the context window. We keep the
    first N chars and a tail of M chars — heads have the skill choices, tails
    have the conclusion.
    """
    if not steps:
        return "(no trace)"
    flat = []
    for s in steps:
        role = (s.get("role") or "").strip()
        # Skip token_usage entries — they add no analytical value.
        if role == "token_usage":
            continue
        content = s.get("content") or ""
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        flat.append(f"[{role}] {content}")
    blob = "\n".join(flat)
    if len(blob) <= max_chars:
        return blob
    head = blob[: int(max_chars * 0.7)]
    tail = blob[-int(max_chars * 0.3):]
    return f"{head}\n... [truncated {len(blob) - max_chars} chars] ...\n{tail}"


def _format_prior_turns(prior_turns: list[dict] | None) -> str:
    """
    Format prior turns (before the reflected turn) into a concise summary.
    Each turn gets: user_message, skills_used, conclusion (first 300 chars),
    and feedback vote if any.
    """
    if not prior_turns:
        return "(this is the first turn in the conversation)"
    lines = []
    for i, t in enumerate(prior_turns, 1):
        user_msg = (t.get("user_message") or "")[:100]
        skills = [s.get("skill_id", "") for s in (t.get("skills_used") or [])]
        skills_str = ", ".join(skills) if skills else "(none)"

        # Extract conclusion from agent_response_full or agent_response
        full = t.get("agent_response_full")
        if isinstance(full, dict):
            conclusion = full.get("root_cause_summary") or full.get("root_cause") or ""
        else:
            conclusion = (t.get("agent_response") or "")

        fb = t.get("feedback")
        fb_str = f"vote={fb['vote']}" if fb else "no feedback"

        lines.append(
            f"  Turn {i} [{t.get('turn_id', '?')[:8]}...] @ {t.get('ts', '?')}\n"
            f"    User: {user_msg}\n"
            f"    Skills: {skills_str}\n"
            f"    Conclusion: {conclusion}\n"
            f"    Feedback: {fb_str}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reflector
# ---------------------------------------------------------------------------

class Reflector:
    """
    Consumes one (turn, feedback) pair and produces a structured reflection.

    Inputs:
        turn        — one entry from a feedback conversation snapshot
                      (services.feedback_service.record_turn shape).
        feedback    — the `feedback` dict that lives inside `turn["feedback"]`
                      (vote, weight, details: {correct_root_cause, ...}).
        applied_bullets — list of Bullet objects the agent claimed to apply.

    Output: dict matching prompts.REFLECTOR_PROMPT's JSON schema.
    """

    def __init__(self, llm, model: Optional[str] = None,
                 max_refine_rounds: int = 1, debug: bool = False):
        self.llm = llm
        self.model = model
        self.max_refine_rounds = max_refine_rounds
        self.debug = debug

    def reflect(
        self,
        *,
        case_context: dict,
        turn: dict,
        feedback: dict,
        applied_bullets: list[Any],
        prior_turns: list[dict] | None = None,
    ) -> dict:
        details = (feedback or {}).get("details") or {}
        vote = (feedback or {}).get("vote", 0)
        agent_workflow_tag = details.get("agent_workflow") or "appropriate"

        # The agent's final structured report (root_cause, conclusion_tag, ...)
        # is stashed by record_turn() as `agent_response_full`.
        final_report = turn.get("agent_response_full") or turn.get("agent_response", "")

        prompt = prompts.fill_reflector_prompt(
            case_context=_safe_json_dump(case_context),
            conversation_history=_format_prior_turns(prior_turns),
            agent_trajectory=_truncate_trace(turn.get("steps_trace") or []),
            agent_final_report=_safe_json_dump(final_report),
            vote=vote,
            agent_workflow_tag=agent_workflow_tag,
            correct_root_cause=details.get("correct_root_cause") or "",
            correct_conclusion_tag=details.get("correct_conclusion_tag") or "",
            correct_skill=details.get("correct_skill") or "",
            correct_approach=details.get("correct_approach") or "",
            evidence_log_lines=_safe_json_dump(details.get("evidence_log_lines") or []),
            helpful_skills=_safe_json_dump(turn.get("helpful_skills") or []),
            step_votes=_safe_json_dump(turn.get("step_votes") or []),
            free_text_issues=_safe_json_dump(details.get("issues") or []),
            applied_bullets="\n".join(b.render() for b in applied_bullets) or "(none)",
        )

        if self.debug:
            print("\n" + "="*80)
            print("[REFLECTOR] PROMPT SENT TO LLM:")
            print("="*80)
            print(prompt)
            print("="*80 + "\n")

        reflection = self._call(prompt)

        if self.debug:
            print("\n" + "-"*80)
            print("[REFLECTOR] LLM RESPONSE (parsed JSON):")
            print("-"*80)
            print(json.dumps(reflection, indent=2, ensure_ascii=False))
            print("-"*80 + "\n")

        # Optional refinement rounds (paper §3, max_refine_rounds=5 by default —
        # we ship with 1 since wifi traces are smaller than AppWorld traces).
        for i in range(max(0, self.max_refine_rounds - 1)):
            refine_prompt = (
                prompt
                + "\n\nYour previous reflection (JSON):\n"
                + json.dumps(reflection, indent=2, ensure_ascii=False)
                + "\n\nRefine it: tighten the root cause, remove vague language, "
                  "ensure every key_insight maps to exactly ONE section. "
                  "Output the refined JSON only."
            )
            if self.debug:
                print(f"\n[REFLECTOR] REFINEMENT ROUND {i+1}")
            reflection = self._call(refine_prompt)
            if self.debug:
                print(json.dumps(reflection, indent=2, ensure_ascii=False))
        return reflection

    def _call(self, prompt: str) -> dict:
        raw = self.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            system_content="You are a careful, evidence-driven WiFi debug reviewer. Output strict JSON only.",
        )
        return _extract_json(raw)


# ---------------------------------------------------------------------------
# Curator
# ---------------------------------------------------------------------------

class Curator:
    """
    Reads ONE reflection + the relevant playbooks, asks the LLM for a delta,
    then applies the delta deterministically.
    """

    def __init__(self, llm, model: Optional[str] = None, token_budget: int = 8000,
                 debug: bool = False):
        self.llm = llm
        self.model = model
        self.token_budget = token_budget
        self.debug = debug

    def curate(
        self,
        *,
        reflection: dict,
        workflow_playbook: Playbook,
        domain_playbooks: dict[str, Playbook],
        turn_id: str = "",
    ) -> dict:
        """
        Returns a summary of what was applied. Side effect: mutates the
        playbooks in memory (caller is responsible for calling .save()).

        Summary shape:
            {
              "operations_proposed": [...],
              "operations_applied":  [...],
              "operations_skipped":  [{"op": ..., "reason": "..."}],
              "counter_updates":     [{"bullet_id": "...", "tag": "..."}]
            }
        """
        # 1. Apply bullet_tags first — they update counters on EXISTING bullets,
        # regardless of what the curator decides about new content.
        counter_updates = self._apply_bullet_tags(
            reflection.get("bullet_tags") or [],
            workflow_playbook,
            domain_playbooks,
        )

        # 2. Render the playbooks for the curator prompt.
        relevant_skills = self._relevant_skills(reflection)
        domain_render = []
        for sk in relevant_skills:
            pb = domain_playbooks.get(sk)
            if pb is None:
                continue
            domain_render.append(f"### Skill: {sk}\n{pb.render()}")
        domain_block = "\n\n".join(domain_render) or "(no domain playbook loaded for the relevant skills)"

        prompt = prompts.fill_curator_prompt(
            reflection_json=json.dumps(reflection, indent=2, ensure_ascii=False),
            workflow_playbook=workflow_playbook.render(),
            domain_playbook=domain_block,
            token_budget=self.token_budget,
        )

        if self.debug:
            print("\n" + "="*80)
            print("[CURATOR] PROMPT SENT TO LLM:")
            print("="*80)
            print(prompt)
            print("="*80 + "\n")

        result = self._call(prompt)

        if self.debug:
            print("\n" + "-"*80)
            print("[CURATOR] LLM RESPONSE (parsed JSON):")
            print("-"*80)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            print("-"*80 + "\n")

        # 3. Apply the operations.
        applied: list[dict] = []
        skipped: list[dict] = []
        for op in result.get("operations") or []:
            ok, reason = self._apply_op(op, workflow_playbook, domain_playbooks, turn_id)
            if ok:
                applied.append(op)
            else:
                skipped.append({"op": op, "reason": reason})

        summary = {
            "operations_proposed": result.get("operations") or [],
            "operations_applied": applied,
            "operations_skipped": skipped,
            "counter_updates": counter_updates,
            "reasoning": result.get("reasoning", ""),
        }

        if self.debug:
            print("\n" + "-"*80)
            print("[CURATOR] APPLY SUMMARY:")
            print("-"*80)
            print(f"  Operations proposed: {len(summary['operations_proposed'])}")
            print(f"  Operations applied:  {len(applied)}")
            for op in applied:
                print(f"    ✓ {op.get('type')} → {op.get('target_playbook','')}/{op.get('section','')}")
            print(f"  Operations skipped:  {len(skipped)}")
            for s in skipped:
                print(f"    ✗ {s['op'].get('type')} — {s['reason']}")
            print(f"  Counter updates:     {len(counter_updates)}")
            for cu in counter_updates:
                print(f"    {cu['bullet_id']} → {cu['tag']}")
            print("-"*80 + "\n")

        return summary

    # ---- helpers ----
    def _relevant_skills(self, reflection: dict) -> list[str]:
        """Skills the reflection mentions (target_skill in key_insights)."""
        seen: list[str] = []
        for ki in reflection.get("key_insights") or []:
            sk = (ki.get("target_skill") or "").strip()
            if sk and sk not in seen:
                seen.append(sk)
        return seen

    def _apply_bullet_tags(self, tags, workflow_pb, domain_pbs) -> list[dict]:
        updates: list[dict] = []
        for t in tags:
            bid = (t.get("id") or "").strip()
            tag = (t.get("tag") or "").strip().lower()
            if not bid or tag not in {"helpful", "harmful", "neutral"}:
                continue
            # Look in workflow first, then every domain playbook.
            target = workflow_pb if workflow_pb.get(bid) else None
            if target is None:
                for pb in domain_pbs.values():
                    if pb.get(bid):
                        target = pb
                        break
            if target is None:
                continue
            target.increment_counter(bid, tag)
            updates.append({"bullet_id": bid, "tag": tag})
        return updates

    def _apply_op(self, op, workflow_pb, domain_pbs, turn_id) -> tuple[bool, str]:
        op_type = (op.get("type") or "").upper()
        if op_type == "ADD":
            scope = (op.get("target_playbook") or "").lower()
            section = (op.get("section") or "").strip()
            content = (op.get("content") or "").strip()
            if not content:
                return False, "empty content"
            if scope == "workflow":
                workflow_pb.add(section, content, source_turn_id=turn_id)
                return True, ""
            if scope == "domain":
                skill = (op.get("target_skill") or "").strip()
                pb = domain_pbs.get(skill)
                if pb is None:
                    return False, f"no domain playbook for skill '{skill}'"
                pb.add(section, content, source_turn_id=turn_id)
                return True, ""
            return False, f"unknown target_playbook '{scope}'"

        if op_type == "UPDATE":
            bid = (op.get("bullet_id") or "").strip()
            new_content = (op.get("new_content") or "").strip()
            if not bid or not new_content:
                return False, "missing bullet_id or new_content"
            if workflow_pb.update(bid, new_content):
                return True, ""
            for pb in domain_pbs.values():
                if pb.update(bid, new_content):
                    return True, ""
            return False, f"bullet not found: {bid}"

        if op_type == "REMOVE":
            bid = (op.get("bullet_id") or "").strip()
            if not bid:
                return False, "missing bullet_id"
            # Safety gate: only allow removal if the bullet is net-negative.
            target_pb = None
            if workflow_pb.get(bid):
                target_pb = workflow_pb
            else:
                for pb in domain_pbs.values():
                    if pb.get(bid):
                        target_pb = pb
                        break
            if target_pb is None:
                return False, f"bullet not found: {bid}"
            b = target_pb.get(bid)
            if b and b.net_score > -1:
                return False, f"refused: bullet {bid} is not net-negative (score={b.net_score})"
            target_pb.remove(bid)
            return True, ""

        return False, f"unknown op type '{op_type}'"

    def _call(self, prompt: str) -> dict:
        raw = self.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            system_content="You are a careful curator. Output strict JSON only.",
        )
        return _extract_json(raw)


# ---------------------------------------------------------------------------
# Patch: Playbook.get-style helper to disambiguate `.get(bullet_id)` from a
# dict-like .get(key, default) — we use the bullet lookup form throughout the
# Curator. The method already exists in playbook.py, this is just a sanity
# helper kept here so roles.py stays self-contained for review.
# ---------------------------------------------------------------------------


def _safe_json_dump(obj) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    except Exception:
        return str(obj)
