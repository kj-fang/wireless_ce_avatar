"""
AceRunner — the end-to-end pipeline that turns feedback events into playbook
updates.

Two ways to drive it:

  Offline batch  (recommended for first run / periodic adaptation):
      runner = AceRunner(llm_helper, playbooks_dir, feedback_root)
      runner.run_batch(since="2026-05-01T00:00:00")

  Online (per-turn, just after a vote is recorded):
      runner.run_one(conversation_id, turn_id)

Both paths share `_process_turn`, which:

  1. Loads the conversation snapshot + matched feedback record.
  2. Resolves the playbook bullets the agent claimed to apply.
  3. Calls Reflector → reflection JSON.
  4. Calls Curator → operations applied to the in-memory playbooks.
  5. Persists the updated playbooks.

The runner also maintains a tiny cursor file so batch runs are idempotent.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from .playbook import Playbook
from .roles import Reflector, Curator


class AceRunner:
    def __init__(
        self,
        llm,
        playbooks_dir: str | Path,
        feedback_root: str | Path,
        skills: Optional[Iterable[str]] = None,
        max_refine_rounds: int = 1,
    ):
        """
        llm:            an LLM_helper instance (services.llm_service.LLM_helper).
        playbooks_dir:  directory holding JSON playbook files.
        feedback_root:  directory written by services.feedback_service
                        (contains feedback.jsonl, feedback_details.jsonl,
                         conversations/<id>.json).
        skills:         skill names to maintain domain playbooks for. If None,
                        the runner lazily creates one whenever a turn references
                        a new skill.
        """
        self.llm = llm
        self.playbooks_dir = Path(playbooks_dir)
        self.feedback_root = Path(feedback_root)
        self.playbooks_dir.mkdir(parents=True, exist_ok=True)

        self.workflow_pb = Playbook("agent", self.playbooks_dir / "workflow.json")
        self.domain_pbs: dict[str, Playbook] = {}
        for sk in skills or []:
            self._ensure_domain_playbook(sk)

        self.reflector = Reflector(llm, max_refine_rounds=max_refine_rounds)
        self.curator = Curator(llm)
        self._cursor_path = self.playbooks_dir / ".ace_cursor.json"
        self._lock = threading.Lock()

    # ----- domain playbook bookkeeping -----
    def _ensure_domain_playbook(self, skill: str) -> Playbook:
        if skill not in self.domain_pbs:
            safe = skill.replace("/", "_").replace(" ", "_")
            self.domain_pbs[skill] = Playbook(skill, self.playbooks_dir / f"domain_{safe}.json")
        return self.domain_pbs[skill]

    # ----- snapshot / feedback loaders -----
    def _load_snapshot(self, conversation_id: str) -> Optional[dict]:
        path = self.feedback_root / "conversations" / f"{conversation_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[ace.pipeline] failed to read snapshot {path}: {e}")
            return None

    def _iter_feedback_events(self, since_iso: Optional[str] = None) -> Iterable[dict]:
        """
        Walk both feedback.jsonl and feedback_details.jsonl in timestamp order
        and yield each event whose ts > since_iso. The cursor stores the ts
        of the last event we processed, so STRICT `>` makes a re-run with the
        same cursor advance past it instead of reprocessing it.
        """
        events: list[dict] = []
        for fname in ("feedback.jsonl", "feedback_details.jsonl"):
            path = self.feedback_root / fname
            if not path.exists():
                continue
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if since_iso and rec.get("ts", "") <= since_iso:
                        continue
                    events.append(rec)
            except Exception as e:
                print(f"[ace.pipeline] failed to read {path}: {e}")
        events.sort(key=lambda r: r.get("ts", ""))
        return events

    # ----- cursor for idempotent batch runs -----
    def _load_cursor(self) -> str:
        if not self._cursor_path.exists():
            return ""
        try:
            return json.loads(self._cursor_path.read_text(encoding="utf-8")).get("last_ts", "")
        except Exception:
            return ""

    def _save_cursor(self, ts: str) -> None:
        self._cursor_path.write_text(
            json.dumps({"last_ts": ts, "updated_at": datetime.now().astimezone().isoformat()}),
            encoding="utf-8",
        )

    # ----- core: process one turn -----
    def _process_turn(self, conversation_id: str, turn_id: str) -> dict:
        snap = self._load_snapshot(conversation_id)
        if snap is None:
            return {"status": "no_snapshot", "conversation_id": conversation_id, "turn_id": turn_id}

        turn = next((t for t in snap.get("turns", []) if t.get("turn_id") == turn_id), None)
        if turn is None:
            return {"status": "no_turn", "conversation_id": conversation_id, "turn_id": turn_id}

        feedback = turn.get("feedback") or {}
        if not feedback:
            # Untagged turn — nothing for the Reflector to learn from.
            return {"status": "no_feedback", "conversation_id": conversation_id, "turn_id": turn_id}

        case_context = snap.get("issue") or {}

        # Resolve the bullets the agent claimed to apply. The Generator's
        # final report may not exist (older snapshots) or may store
        # `applied_bullet_ids` either in agent_response_full or in the text
        # blob. We accept both.
        applied_ids = self._extract_applied_bullet_ids(turn)
        applied = self._resolve_bullets(applied_ids)

        # 1. Reflect
        reflection = self.reflector.reflect(
            case_context=case_context,
            turn=turn,
            feedback=feedback,
            applied_bullets=applied,
        )

        # 2. Make sure domain playbooks exist for every skill the reflection
        # mentions, so the Curator can write into them.
        for ki in reflection.get("key_insights") or []:
            sk = (ki.get("target_skill") or "").strip()
            if sk:
                self._ensure_domain_playbook(sk)
        # Also create for skills_used in the turn (so positive helpful counts
        # land somewhere even if the reflection didn't propose new bullets).
        for s in turn.get("skills_used") or []:
            sid = (s.get("skill_id") or "").strip()
            if sid:
                self._ensure_domain_playbook(sid)

        # 3. Curate
        curate_result = self.curator.curate(
            reflection=reflection,
            workflow_playbook=self.workflow_pb,
            domain_playbooks=self.domain_pbs,
            turn_id=turn_id,
        )

        # 4. Grow-and-refine (lazy: only when a section overflows)
        self.workflow_pb.refine()
        for pb in self.domain_pbs.values():
            pb.refine()

        # 5. Persist
        self.workflow_pb.save()
        for pb in self.domain_pbs.values():
            pb.save()

        return {
            "status": "ok",
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "reflection": reflection,
            "curate_result": curate_result,
        }

    # ----- public entry points -----
    def run_one(self, conversation_id: str, turn_id: str) -> dict:
        with self._lock:
            return self._process_turn(conversation_id, turn_id)

    def run_batch(self, since: Optional[str] = None, max_turns: Optional[int] = None) -> list[dict]:
        with self._lock:
            since = since or self._load_cursor()
            seen_keys: set[tuple[str, str]] = set()
            results: list[dict] = []
            last_ts = since or ""
            for ev in self._iter_feedback_events(since):
                cid = ev.get("conversation_id")
                tid = ev.get("turn_id")
                if not cid or not tid:
                    continue
                key = (cid, tid)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                results.append(self._process_turn(cid, tid))
                last_ts = ev.get("ts") or last_ts
                if max_turns and len(results) >= max_turns:
                    break
            if last_ts:
                self._save_cursor(last_ts)
            return results

    # ----- generator-side helpers -----
    def render_for_generator(self, skill: str) -> tuple[str, str]:
        """
        Convenience for the agent: return (workflow_text, domain_text) ready
        to be substituted into prompts.GENERATOR_PROMPT.
        """
        domain_pb = self.domain_pbs.get(skill)
        domain_text = domain_pb.render() if domain_pb else "(no playbook yet for this skill)"
        return self.workflow_pb.render(), domain_text

    def render_workflow(self) -> str:
        """Workflow playbook text for injection at the top of the agent system prompt."""
        return self.workflow_pb.render()

    def render_domain(self, skill: str, ensure: bool = True) -> str:
        """
        Domain playbook text for ONE skill. When `ensure=True`, an empty
        playbook is created on first reference so future Curator writes have
        a target.
        """
        if ensure:
            self._ensure_domain_playbook(skill)
        pb = self.domain_pbs.get(skill)
        return pb.render() if pb else ""

    # ----- internal helpers -----
    def _extract_applied_bullet_ids(self, turn: dict) -> list[str]:
        ids: list[str] = []
        full = turn.get("agent_response_full")
        if isinstance(full, dict):
            ids = list(full.get("applied_bullet_ids") or [])
        if not ids:
            # Best-effort fallback: scrape ids out of the response text.
            blob = turn.get("agent_response") or ""
            if isinstance(blob, str):
                import re
                ids = re.findall(r"\b([a-z]{2,5}-\d{5})\b", blob)
        # De-duplicate while preserving order
        seen: set[str] = set()
        out: list[str] = []
        for i in ids:
            if i in seen:
                continue
            seen.add(i)
            out.append(i)
        return out

    def _resolve_bullets(self, ids: list[str]):
        from .playbook import Bullet
        found = []
        for bid in ids:
            b = self.workflow_pb.get(bid)
            if b is None:
                for pb in self.domain_pbs.values():
                    b = pb.get(bid)
                    if b is not None:
                        break
            if b is not None:
                found.append(b)
            else:
                # Synthesize a stub so the reflector can still tag the ID as
                # unknown — useful when bullets have been pruned since the
                # case ran.
                found.append(Bullet(id=bid, section="(missing)", content="(bullet not found — may have been pruned)"))
        return found
