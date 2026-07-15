"""
Token-free test doubles for the eval harness (`cli eval --smoke`).

FakeAgentClient duck-types the OpenAI client surface the agent uses
(`client.chat.completions.create(...)`) and plays a fixed two-step script:
step 1 fetches a skill's filtered logs, step 2 submits a final report with
constructor-provided args. FakeJudgeLLM duck-types `LLM_helper.chat(...)`.

Both let the FULL production replay/scoring/gate code paths run end-to-end
with zero LLM tokens.
"""

from __future__ import annotations

import json
from types import SimpleNamespace


def _make_tool_call(name: str, args: dict, tc_id: str):
    return SimpleNamespace(
        id=tc_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


def _make_response(*tool_calls, content=None, finish_reason="tool_calls"):
    message = SimpleNamespace(
        content=content,
        tool_calls=list(tool_calls) if tool_calls else None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50, total_tokens=150),
    )


class FakeAgentClient:
    """Scripted OpenAI-shaped client that lets a REAL WifiLogAgentSystem run
    end-to-end without an LLM.

    Dispatch rules per create() call:
      * `tools` kwarg present  → agentic loop step. The fake alternates
        fetch → submit per replay (a fresh conversation starts each replay,
        detected by the absence of any prior tool-result message).
      * no `tools` kwarg       → auxiliary call (quality reviewer,
        issue-time extraction). Returns `{"approved": true, ...}` as plain
        text, which the quality gate parses and everything else ignores.

    The report submitted depends on the playbook marker: when the system
    prompt contains `marker` (seeded only into the AFTER playbooks by the
    smoke fixture), `report_with_marker` is submitted; otherwise
    `report_without_marker`. This doubles as a live check that ACE playbook
    injection actually reaches the prompt.
    """

    def __init__(self, report_without_marker: dict, report_with_marker: dict,
                 marker: str = "SMOKE_AFTER_MARKER",
                 skill_name: str = "Connectivity"):
        self._report_plain = dict(report_without_marker)
        self._report_marked = dict(report_with_marker)
        self._marker = marker
        self._skill_name = skill_name
        self._tc_seq = 0
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _system_text(self, messages) -> str:
        for m in messages or []:
            if isinstance(m, dict) and m.get("role") == "system":
                return str(m.get("content") or "")
        return ""

    def _has_tool_result(self, messages) -> bool:
        return any(isinstance(m, dict) and m.get("role") == "tool"
                   for m in (messages or []))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        self._tc_seq += 1
        messages = kwargs.get("messages") or []

        if "tools" not in kwargs or not kwargs.get("tools"):
            # Auxiliary call (quality reviewer / time extraction).
            return _make_response(
                content='{"approved": true, "reason": "fake", "required_actions": []}',
                finish_reason="stop",
            )

        if not self._has_tool_result(messages):
            # First agentic step of a replay → fetch a skill.
            return _make_response(_make_tool_call(
                "fetch_filtered_logs", {"skill_name": self._skill_name},
                tc_id=f"tc_fetch_{self._tc_seq}"))

        report = (self._report_marked
                  if self._marker in self._system_text(messages)
                  else self._report_plain)
        return _make_response(_make_tool_call(
            "submit_final_report", report, tc_id=f"tc_submit_{self._tc_seq}"))


class FakeAgentLLM:
    """LLM_helper stand-in for replay: carries .client and .model."""

    def __init__(self, client: FakeAgentClient):
        self.client = client
        self.model = "fake-model"

    def chat(self, messages, system_content=None) -> str:
        raise RuntimeError("FakeAgentLLM.chat should not be called in replay")


class FakeJudgeLLM:
    """Duck-types LLM_helper.chat(messages, system_content) -> canned judge
    JSON. The winner is expressed in before/after terms; the Judge class
    randomizes A/B ordering, so we answer based on which report text appears
    where (the fake inspects the prompt for the marker strings)."""

    def __init__(self, winner: str = "after",
                 match_winner: float = 0.9, match_loser: float = 0.2):
        assert winner in ("before", "after", "tie")
        self.winner = winner
        self.match_winner = match_winner
        self.match_loser = match_loser
        self.model = "fake-judge"
        self.client = object()
        self.calls: list[dict] = []

    def chat(self, messages, system_content=None) -> str:
        self.calls.append({"messages": messages, "system_content": system_content})
        prompt = messages[-1]["content"] if messages else ""
        # The replay fakes embed the label into root_cause_summary via the
        # smoke fixture ("[BEFORE]" / "[AFTER]" markers). Locate which slot
        # (A or B) got the winner.
        winner_slot = "tie"
        if self.winner != "tie":
            marker = f"[{self.winner.upper()}]"
            a_start = prompt.find("=== REPORT A ===")
            b_start = prompt.find("=== REPORT B ===")
            if a_start != -1 and b_start != -1:
                a_text = prompt[a_start:b_start]
                winner_slot = "A" if marker in a_text else "B"
        if winner_slot == "tie":
            ma = mb = (self.match_winner + self.match_loser) / 2
        elif winner_slot == "A":
            ma, mb = self.match_winner, self.match_loser
        else:
            ma, mb = self.match_loser, self.match_winner
        return json.dumps({
            "winner": winner_slot,
            "root_cause_match_a": ma,
            "root_cause_match_b": mb,
            "evidence_quality_a": ma,
            "evidence_quality_b": mb,
            "inferred_tag_a": "",
            "inferred_tag_b": "",
            "rationale": f"fake judge: winner={self.winner}",
        })
