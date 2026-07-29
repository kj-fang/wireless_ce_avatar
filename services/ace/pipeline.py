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
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

from .history import HistoryWriter
from .playbook import Playbook
from .roles import Reflector, Curator


SkillContextProvider = Callable[[str], Optional[dict]]


class AceRunner:
    def __init__(
        self,
        llm,
        playbooks_dir: str | Path,
        feedback_root: str | Path,
        skills: Optional[Iterable[str]] = None,
        max_refine_rounds: int = 1,
        skill_context_provider: Optional[SkillContextProvider] = None,
        history: Optional[HistoryWriter] = None,
        feedback_prefix: str = "",
        exclude_users: Optional[Iterable[str]] = None,
    ):
        """
        llm:            an LLM_helper instance (services.llm_service.LLM_helper).
        playbooks_dir:  directory holding JSON playbook files.
        feedback_root:  directory written by services.feedback_service
                        (contains feedback.jsonl, feedback_details.jsonl,
                         conversations/<id>.json — or, for a non-default
                         domain, the prefixed equivalents; see
                         feedback_prefix below).
        skills:         skill names to maintain domain playbooks for. If None,
                        the runner lazily creates one whenever a turn references
                        a new skill.
        skill_context_provider:
                        Optional callable invoked once per relevant skill before
                        running the Reflector/Curator. Given a skill_id it
                        should return a dict with any of:
                            {"description": str,
                             "expert_rules": str,
                             "keywords": list[str]}
                        These fields are injected into both prompts so newly
                        added bullets match the existing skill voice/style.
                        Return None when the skill is unknown.
        feedback_prefix: filename prefix services.feedback_service uses to
                        partition this domain's feedback stream from the
                        default (wifi) one — "" for wifi, "bt_" for BT. Must
                        match services.feedback_service._domain_prefix() for
                        the same domain, or this runner will silently read
                        (or write the cursor against) the wrong stream.
        exclude_users:  optional collection of submitter identities (e.g.
                        emails / UPNs) whose feedback should be IGNORED — any
                        turn whose feedback.submitted_by (or the snapshot-level
                        submitted_by) matches is skipped, so those users'
                        votes never shape the playbook. Matched
                        case-insensitively after stripping whitespace.
        """
        self.llm = llm
        self.playbooks_dir = Path(playbooks_dir)
        self.feedback_root = Path(feedback_root)
        self.playbooks_dir.mkdir(parents=True, exist_ok=True)
        self.skill_context_provider = skill_context_provider
        self.history = history
        self.feedback_prefix = feedback_prefix
        self.exclude_users = {
            (u or "").strip().lower()
            for u in (exclude_users or [])
            if (u or "").strip()
        }

        self.workflow_pb = Playbook("agent", self.playbooks_dir / "workflow.json")
        self.domain_pbs: dict[str, Playbook] = {}
        for sk in skills or []:
            self._ensure_domain_playbook(sk)

        self.reflector = Reflector(llm, max_refine_rounds=max_refine_rounds)
        self.curator = Curator(llm)
        self._cursor_path = self.playbooks_dir / ".ace_cursor.json"
        self._lock = threading.Lock()

    # ----- domain playbook bookkeeping -----
    @staticmethod
    def _is_plausible_skill_id(skill: str) -> bool:
        """Guard against malformed skill ids. Some feedback snapshots stored an
        entire injected playbook block as `skill_id`; a real skill id is a
        short filename-safe token like 'connection_flow'. Reject anything with
        newlines/control chars or absurd length so it never becomes a bogus
        domain_*.json filename (which crashes save() with OSError 22)."""
        skill = (skill or "").strip()
        if not skill or len(skill) > 64:
            return False
        if any(c in skill for c in "\r\n\t"):
            return False
        return re.match(r"^[\w./+\- ]+$", skill) is not None

    def _ensure_domain_playbook(self, skill: str) -> Optional[Playbook]:
        skill = (skill or "").strip()
        if not self._is_plausible_skill_id(skill):
            print(f"[ace.pipeline] skipping implausible skill id "
                  f"(len={len(skill)}): {skill[:48]!r}\u2026")
            return None
        if skill not in self.domain_pbs:
            safe = skill.replace("/", "_").replace(" ", "_")
            self.domain_pbs[skill] = Playbook(skill, self.playbooks_dir / f"domain_{safe}.json")
        return self.domain_pbs[skill]

    # ----- snapshot / feedback loaders -----
    def _load_snapshot(self, conversation_id: str) -> Optional[dict]:
        path = self.feedback_root / "conversations" / f"{self.feedback_prefix}{conversation_id}.json"
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
            path = self.feedback_root / f"{self.feedback_prefix}{fname}"
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
    def _process_turn(self, conversation_id: str, turn_id: str, progress=None,
                      run_id: Optional[str] = None,
                      run_source: str = "cli") -> dict:
        def _emit(event, **payload):
            if progress is not None:
                try:
                    progress({"phase": "pipeline", "event": event,
                              "conversation_id": conversation_id,
                              "turn_id": turn_id, **payload})
                except Exception:
                    pass

        _emit("turn_start")
        snap = self._load_snapshot(conversation_id)
        if snap is None:
            _emit("turn_end", status="no_snapshot")
            return {"status": "no_snapshot", "conversation_id": conversation_id, "turn_id": turn_id}

        turn = next((t for t in snap.get("turns", []) if t.get("turn_id") == turn_id), None)
        if turn is None:
            _emit("turn_end", status="no_turn")
            return {"status": "no_turn", "conversation_id": conversation_id, "turn_id": turn_id}

        feedback = turn.get("feedback") or {}
        if not feedback:
            # Untagged turn — nothing for the Reflector to learn from.
            _emit("turn_end", status="no_feedback")
            return {"status": "no_feedback", "conversation_id": conversation_id, "turn_id": turn_id}

        submitter = (feedback.get("submitted_by") or snap.get("submitted_by") or "").strip()

        # User-level filter: skip feedback from excluded submitters (e.g. test
        # accounts) so their votes never shape the playbook. Checks the
        # per-turn submitter first, then the conversation-level one.
        if self.exclude_users:
            submitter_lc = submitter.lower()
            if submitter_lc in self.exclude_users:
                _emit("turn_end", status="excluded_user", submitted_by=submitter)
                return {"status": "excluded_user",
                        "conversation_id": conversation_id, "turn_id": turn_id,
                        "submitted_by": submitter}

        case_context = snap.get("issue") or {}

        # Resolve the bullets the agent claimed to apply. The Generator's
        # final report may not exist (older snapshots) or may store
        # `applied_bullet_ids` either in agent_response_full or in the text
        # blob. We accept both.
        applied_ids = self._extract_applied_bullet_ids(turn)
        applied = self._resolve_bullets(applied_ids)
        _emit("bullets_resolved", applied_bullet_ids=applied_ids)

        # 1. Reflect
        skill_contexts = self._collect_skill_contexts(turn, feedback)
        reflection = self.reflector.reflect(
            case_context=case_context,
            turn=turn,
            feedback=feedback,
            applied_bullets=applied,
            skill_contexts=skill_contexts,
            progress=progress,
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

        # Reflection may have introduced new target skills — refresh contexts
        # so the Curator sees them too.
        skill_contexts = self._collect_skill_contexts(turn, feedback, reflection)

        # 3. Curate
        # High-weight feedback (a detailed modal submission) moves bullet
        # counters faster than a bare thumbs vote, so important lessons rise
        # and stale ones fall sooner during refine().
        tag_weight = 2 if (feedback.get("weight") == "high") else 1
        curate_result = self.curator.curate(
            reflection=reflection,
            workflow_playbook=self.workflow_pb,
            domain_playbooks=self.domain_pbs,
            skill_contexts=skill_contexts,
            turn_id=turn_id,
            tag_weight=tag_weight,
            progress=progress,
        )

        # 4. Grow-and-refine (lazy: only when a section overflows)
        self.workflow_pb.refine()
        for pb in self.domain_pbs.values():
            pb.refine()

        # 4b. History — record BEFORE persist so we archive the exact
        # reflection/curator outputs even if save() blows up.
        if self.history is not None:
            try:
                self.history.record_turn(
                    run_id=run_id,
                    run_source=run_source,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    feedback=feedback,
                    applied_bullets=[
                        {"id": b.id, "section": b.section, "content": b.content}
                        for b in applied
                    ],
                    reflection=reflection,
                    curate_result=curate_result,
                )
            except Exception as e:
                print(f"[ace.pipeline] history.record_turn failed: {e}")

        # 5. Persist
        self.workflow_pb.save()
        for pb in self.domain_pbs.values():
            pb.save()

        result = {
            "status": "ok",
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "submitted_by": submitter,
            "reflection": reflection,
            "curate_result": curate_result,
        }
        _emit("turn_end", status="ok")
        return result

    # ----- public entry points -----
    def preview_batch(self, since: Optional[str] = None,
                      max_turns: Optional[int] = None) -> list[dict]:
        """Dry run: list the turns run_batch WOULD process next — honouring the
        cursor, dedup and exclude_users — WITHOUT calling the LLM, writing
        playbooks, or advancing the cursor.

        Fast by design: it classifies each unique turn from the feedback JSONL
        EVENT alone (which already carries `submitted_by`), so it never opens
        the per-turn conversation snapshot on the (possibly remote) share.
        Trade-off: because it doesn't read the snapshot it cannot flag turns
        whose snapshot is missing or carries no feedback — those are rare and a
        real run would simply skip them, so the WOULD-RUN list is a close upper
        bound on what actually runs."""
        with self._lock:
            since = since or self._load_cursor()
            seen_keys: set[tuple[str, str]] = set()
            out: list[dict] = []
            for ev in self._iter_feedback_events(since):
                cid = ev.get("conversation_id")
                tid = ev.get("turn_id")
                if not cid or not tid:
                    continue
                key = (cid, tid)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                submitter = (ev.get("submitted_by") or "").strip()
                status = ("excluded_user"
                          if self.exclude_users and submitter.lower() in self.exclude_users
                          else "would_run")
                out.append({"conversation_id": cid, "turn_id": tid,
                            "submitted_by": submitter, "status": status,
                            "ts": ev.get("ts")})
                if max_turns and len(out) >= max_turns:
                    break
            return out

    def run_one(self, conversation_id: str, turn_id: str, progress=None,
                run_id: Optional[str] = None,
                run_source: str = "cli") -> dict:
        with self._lock:
            return self._process_turn(conversation_id, turn_id,
                                      progress=progress,
                                      run_id=run_id, run_source=run_source)

    def run_batch(self, since: Optional[str] = None, max_turns: Optional[int] = None,
                  progress=None,
                  run_id: Optional[str] = None,
                  run_source: str = "cli") -> list[dict]:
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
                results.append(self._process_turn(cid, tid, progress=progress,
                                                  run_id=run_id,
                                                  run_source=run_source))
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

    # ----- skill metadata helper for Reflector / Curator alignment -----
    def _collect_skill_contexts(self, turn: dict, feedback: dict,
                                 reflection: Optional[dict] = None) -> dict:
        """Resolve skill metadata for every skill this turn touched.

        Combines the skill's static definition (via skill_context_provider, if
        configured) with the current rendered domain playbook bullets so both
        roles see voice + existing style anchored together. Returns an empty
        dict when no skills can be resolved.
        """
        skill_ids: list[str] = []
        seen: set[str] = set()

        def _add(sid):
            sid = (sid or "").strip()
            if sid and sid not in seen:
                seen.add(sid)
                skill_ids.append(sid)

        for s in turn.get("skills_used") or []:
            if isinstance(s, dict):
                _add(s.get("skill_id") or s.get("name"))
            elif isinstance(s, str):
                _add(s)
        for s in turn.get("helpful_skills") or []:
            if isinstance(s, dict):
                _add(s.get("skill_id") or s.get("name"))
            elif isinstance(s, str):
                _add(s)
        if reflection:
            for ki in reflection.get("key_insights") or []:
                _add(ki.get("target_skill"))

        contexts: dict[str, dict] = {}
        for sid in skill_ids:
            ctx: dict = {}
            if self.skill_context_provider is not None:
                try:
                    provided = self.skill_context_provider(sid)
                except Exception as e:
                    print(f"[ace.pipeline] skill_context_provider({sid}) failed: {e}")
                    provided = None
                if isinstance(provided, dict):
                    ctx.update(provided)
            pb = self.domain_pbs.get(sid)
            if pb is not None:
                try:
                    pb.reload_if_changed()
                    ctx["domain_bullets"] = pb.render()
                except Exception:
                    pass
            if ctx:
                contexts[sid] = ctx
        return contexts

    def render_workflow(self, ranked_for_prompt: bool = False) -> str:
        """Workflow playbook text for injection at the top of the agent system prompt."""
        self.workflow_pb.reload_if_changed()
        return self.workflow_pb.render(sort_globally_by_score=ranked_for_prompt)

    def render_domain(
        self,
        skill: str,
        ensure: bool = True,
        ranked_for_prompt: bool = False,
    ) -> str:
        """
        Domain playbook text for ONE skill. When `ensure=True`, an empty
        playbook is created on first reference so future Curator writes have
        a target.
        """
        if ensure:
            self._ensure_domain_playbook(skill)
        pb = self.domain_pbs.get(skill)
        if pb is None:
            return ""
        pb.reload_if_changed()
        return pb.render(sort_globally_by_score=ranked_for_prompt)

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
