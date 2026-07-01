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


def _render_skill_definitions(skill_contexts: Optional[dict]) -> str:
    """Format a per-skill context dict into the prompt block.

    Expected shape:
        {skill_id: {"description": str,
                    "expert_rules": str,
                    "keywords": list[str],
                    "domain_bullets": str}}   # any field optional
    """
    if not skill_contexts:
        return "(no skill metadata available)"
    blocks: list[str] = []
    for sk, ctx in skill_contexts.items():
        if not isinstance(ctx, dict):
            continue
        desc = (ctx.get("description") or "").strip()
        rules = (ctx.get("expert_rules") or "").strip()
        keywords = ctx.get("keywords") or []
        domain_bullets = (ctx.get("domain_bullets") or "").strip()
        lines = [f"### Skill: {sk}"]
        if desc:
            lines.append(f"description: {desc}")
        if keywords:
            kw = ", ".join(str(k) for k in keywords[:20])
            if len(keywords) > 20:
                kw += f", … (+{len(keywords) - 20} more)"
            lines.append(f"keywords: {kw}")
        if rules:
            # Indent so the expert_rules block is visually distinct.
            indented = "\n".join("  " + ln for ln in rules.splitlines())
            lines.append("expert_rules (skill's authoritative voice):")
            lines.append(indented)
        if domain_bullets:
            lines.append("existing domain playbook bullets (style guide):")
            lines.append(domain_bullets)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) if blocks else "(no skill metadata available)"


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


def _looks_like_junk(text: str) -> bool:
    """
    Cheap sanity gate for free-text ground-truth fields (e.g.
    correct_root_cause). A real root cause is a phrase — multiple words or a
    recognisable ALLCAPS_TAG. A short opaque single token like "ejwoi" is
    almost certainly a test/placeholder value and must NOT be handed to the
    Reflector as authoritative ground truth, or it pollutes the playbook.

    Returns True only for clearly-junk input; empty strings are "absent",
    not junk, and return False so the caller can treat them as "no signal".
    """
    t = (text or "").strip()
    if not t:
        return False
    if len(t) >= 12:            # long enough to plausibly be meaningful
        return False
    if any(c.isspace() for c in t):   # multi-token -> looks like a real phrase
        return False
    if re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", t):   # ALLCAPS_TAG style
        return False
    return True                 # short, single opaque token -> junk


def _truncate_trace(
    steps: list[dict],
    max_chars: int = 6000,
    pinned: set[int] | None = None,
) -> str:
    """
    Steps traces from log_chatbot_service can be huge (full log dumps). Cap
    them so the Reflector prompt stays in the context window.

    Two robustness guarantees the naive head/tail slice did not give us:
      * every row is prefixed with its `#index`, so the Reflector can anchor
        a step_feedback `step_index` to the exact trajectory row (step_index
        is the position into this same list — see
        feedback_service._extract_skills_used), and
      * any row whose index is in `pinned` (the steps the user actually rated)
        is ALWAYS kept, capped per-row, even when it falls in the middle that
        truncation would otherwise drop. Contiguous dropped runs collapse to a
        `... [N steps omitted] ...` marker.
    """
    if not steps:
        return "(no trace)"

    pinned = {i for i in (pinned or set()) if 0 <= i < len(steps)}

    def _render(i: int, cap: int | None = None) -> str:
        s = steps[i]
        role = (s.get("role") or "").strip()
        content = s.get("content") or ""
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        if cap is not None and len(content) > cap:
            content = content[:cap] + f" …[+{len(content) - cap} chars]"
        return f"#{i} [{role}] {content}"

    full_rows = [_render(i) for i in range(len(steps))]
    blob = "\n".join(full_rows)
    if len(blob) <= max_chars:
        return blob

    # Over budget: reserve space for pinned rows first (capped so one giant
    # log dump can't eat the whole budget), then fill head + tail context.
    PIN_CAP = 1500
    n = len(steps)
    keep: dict[int, str] = {i: _render(i, cap=PIN_CAP) for i in pinned}
    used = sum(len(v) + 1 for v in keep.values())
    budget = max(0, max_chars - used)
    head_budget = int(budget * 0.6)
    tail_budget = budget - head_budget

    h, hb = 0, 0
    while h < n and hb + len(full_rows[h]) + 1 <= head_budget:
        keep.setdefault(h, full_rows[h])
        hb += len(full_rows[h]) + 1
        h += 1
    t, tb = n - 1, 0
    while t >= h and tb + len(full_rows[t]) + 1 <= tail_budget:
        keep.setdefault(t, full_rows[t])
        tb += len(full_rows[t]) + 1
        t -= 1

    out: list[str] = []
    prev: int | None = None
    for i in sorted(keep):
        if prev is not None and i > prev + 1:
            out.append(f"... [{i - prev - 1} steps omitted] ...")
        out.append(keep[i])
        prev = i
    return "\n".join(out)


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
                 max_refine_rounds: int = 1):
        self.llm = llm
        self.model = model
        self.max_refine_rounds = max_refine_rounds

    def reflect(
        self,
        *,
        case_context: dict,
        turn: dict,
        feedback: dict,
        applied_bullets: list[Any],
        skill_contexts: Optional[dict] = None,
        progress=None,
    ) -> dict:
        def _emit(event, **payload):
            if progress is not None:
                try:
                    progress({"phase": "reflector", "event": event, **payload})
                except Exception:
                    pass

        details = (feedback or {}).get("details") or {}
        vote = (feedback or {}).get("vote", 0)
        agent_workflow_tag = details.get("agent_workflow") or "appropriate"
        # Submission weight and route gate how the Reflector writes: weight
        # scales counter bumps; route is the user's workflow/skill/both choice.
        weight = (feedback or {}).get("weight") or "low"
        feedback_layer = details.get("feedback_layer") or ""
        # The user-facing router emits "agent" for the workflow layer, but the
        # Reflector prompt's SCOPE BY ROUTE rules (and the insight
        # target_playbook it emits) speak "workflow". Normalise here so the
        # scope directive actually matches — otherwise "agent" hits no rule
        # and the workflow-only routing is silently ignored.
        if feedback_layer == "agent":
            feedback_layer = "workflow"

        # Sanity-gate the free-text ground truth: a short opaque token like
        # "ejwoi" is a test/placeholder value, not a real root cause. Blank it
        # so the Reflector treats it as "absent" instead of writing nonsense
        # into a playbook bullet as if it were authoritative.
        correct_root_cause = details.get("correct_root_cause") or ""
        if _looks_like_junk(correct_root_cause):
            correct_root_cause = ""

        # Pin the trajectory rows the user actually rated so truncation can
        # never drop them (step_index is the position into steps_trace). This
        # keeps step_feedback anchorable even for deep steps in a huge trace.
        pinned_steps: set[int] = set()
        for _row in (details.get("step_feedback") or []):
            _idx = _row.get("step_index") if isinstance(_row, dict) else None
            if isinstance(_idx, int):
                pinned_steps.add(_idx)
        for _row in (details.get("skill_feedback") or []):
            _idx = _row.get("step_index") if isinstance(_row, dict) else None
            if isinstance(_idx, int):
                pinned_steps.add(_idx)

        # The agent's final structured report (root_cause, conclusion_tag, ...)
        # is stashed by record_turn() as `agent_response_full`.
        final_report = turn.get("agent_response_full") or turn.get("agent_response", "")

        prompt = prompts.fill_reflector_prompt(
            case_context=_safe_json_dump(case_context),
            agent_trajectory=_truncate_trace(
                turn.get("steps_trace") or [], pinned=pinned_steps
            ),
            agent_final_report=_safe_json_dump(final_report),
            vote=vote,
            agent_workflow_tag=agent_workflow_tag,
            weight=weight,
            feedback_layer=feedback_layer,
            correct_root_cause=correct_root_cause,
            correct_conclusion_tag=details.get("correct_conclusion_tag") or "",
            helpful_skills=_safe_json_dump(turn.get("helpful_skills") or []),
            step_votes=_safe_json_dump(turn.get("step_votes") or []),
            free_text_issues=_safe_json_dump(details.get("issues") or []),
            skill_assessments=_safe_json_dump(turn.get("skill_assessments") or []),
            skill_feedback=_safe_json_dump(details.get("skill_feedback") or []),
            step_feedback=_safe_json_dump(details.get("step_feedback") or []),
            # Issue-time (analysis anchor) — a wrong time means the agent
            # fetched the wrong log window and missed the real evidence. The
            # Reflector turns issue_time_problem into a generalizable workflow
            # lesson (used/correct are context only, never bulletized).
            issue_time_problem=details.get("issue_time_problem") or "",
            issue_time_used=details.get("used_issue_time") or "",
            issue_time_correct=details.get("correct_issue_time") or "",
            issue_time_has_date=("true" if details.get("log_has_date", True) else "false"),
            applied_bullets="\n".join(b.render() for b in applied_bullets) or "(none)",
            skill_definitions=_render_skill_definitions(skill_contexts),
        )

        _emit("start", prompt_chars=len(prompt), vote=vote, applied_bullet_count=len(applied_bullets))
        reflection = self._call(prompt)
        _emit("draft", reflection=reflection)
        # Optional refinement rounds (paper §3, max_refine_rounds=5 by default —
        # we ship with 1 since wifi traces are smaller than AppWorld traces).
        for i in range(max(0, self.max_refine_rounds - 1)):
            _emit("refine_round", round=i + 1)
            refine_prompt = (
                prompt
                + "\n\nYour previous reflection (JSON):\n"
                + json.dumps(reflection, indent=2, ensure_ascii=False)
                + "\n\nRefine it: tighten the root cause, remove vague language, "
                  "ensure every key_insight maps to exactly ONE section. "
                  "Output the refined JSON only."
            )
            reflection = self._call(refine_prompt)
        _emit("done", reflection=reflection)
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

    def __init__(self, llm, model: Optional[str] = None, token_budget: int = 8000):
        self.llm = llm
        self.model = model
        self.token_budget = token_budget

    def curate(
        self,
        *,
        reflection: dict,
        workflow_playbook: Playbook,
        domain_playbooks: dict[str, Playbook],
        skill_contexts: Optional[dict] = None,
        turn_id: str = "",
        tag_weight: int = 1,
        progress=None,
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
        def _emit(event, **payload):
            if progress is not None:
                try:
                    progress({"phase": "curator", "event": event, **payload})
                except Exception:
                    pass

        _emit("start", turn_id=turn_id)
        # 1. Apply bullet_tags first — they update counters on EXISTING bullets,
        # regardless of what the curator decides about new content.
        counter_updates = self._apply_bullet_tags(
            reflection.get("bullet_tags") or [],
            workflow_playbook,
            domain_playbooks,
            skill_tags=reflection.get("skill_tags") or [],
            weight=tag_weight,
        )
        if counter_updates:
            _emit("counter_updates", updates=counter_updates)

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
            skill_definitions=_render_skill_definitions(skill_contexts),
        )
        _emit("llm_request", prompt_chars=len(prompt), relevant_skills=relevant_skills)
        result = self._call(prompt)
        _emit("llm_response", reasoning=result.get("reasoning", ""),
              operation_count=len(result.get("operations") or []))

        # 3. Apply the operations.
        applied: list[dict] = []
        skipped: list[dict] = []
        for op in result.get("operations") or []:
            ok, reason = self._apply_op(op, workflow_playbook, domain_playbooks, turn_id)
            if ok:
                applied.append(op)
                _emit("op_applied", op=op)
            else:
                skipped.append({"op": op, "reason": reason})
                _emit("op_skipped", op=op, reason=reason)

        summary = {
            "operations_proposed": result.get("operations") or [],
            "operations_applied": applied,
            "operations_skipped": skipped,
            "counter_updates": counter_updates,
            "reasoning": result.get("reasoning", ""),
        }
        _emit("done", summary=summary)
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

    def _apply_bullet_tags(self, tags, workflow_pb, domain_pbs,
                            skill_tags: Optional[list] = None,
                            weight: int = 1) -> list[dict]:
        # Build {skill_id: tag} index from the reflection's skill_tags so we can
        # downgrade a `helpful` bullet whose skill the user (or reflector)
        # flagged as `wrong` — i.e. the skill's OUTPUT was bad. A `redundant`
        # verdict is a WORKFLOW judgment (the skill shouldn't have run this
        # turn) and says nothing about the correctness of that skill's domain
        # bullets, so it must NOT downgrade them.
        skill_tag_map: dict[str, str] = {}
        for st in skill_tags or []:
            sid = (st.get("skill_id") or "").strip()
            stag = (st.get("tag") or "").strip().lower()
            if sid and stag in {"helpful", "redundant", "wrong"}:
                skill_tag_map[sid] = stag

        updates: list[dict] = []
        for t in tags:
            bid = (t.get("id") or "").strip()
            tag = (t.get("tag") or "").strip().lower()
            if not bid or tag not in {"helpful", "harmful", "neutral"}:
                continue
            # Look in workflow first, then every domain playbook. Remember
            # which domain skill owned the bullet so we can downgrade below.
            target = workflow_pb if workflow_pb.get(bid) else None
            owning_skill: Optional[str] = None
            if target is None:
                for sk, pb in domain_pbs.items():
                    if pb.get(bid):
                        target = pb
                        owning_skill = sk
                        break
            if target is None:
                continue
            # Downgrade rule: a domain bullet owned by a skill whose OUTPUT was
            # `wrong` cannot stay `helpful`. `redundant` skills are spared —
            # their domain knowledge may still be correct. Workflow bullets are
            # skill-agnostic so they always pass.
            if tag == "helpful" and owning_skill:
                sk_tag = skill_tag_map.get(owning_skill)
                if sk_tag == "wrong":
                    tag = "neutral"
            target.increment_counter(bid, tag, weight=weight)
            updates.append({"bullet_id": bid, "tag": tag, "weight": weight})
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
