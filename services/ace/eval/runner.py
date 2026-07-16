"""
Eval runner: orchestrates case discovery, single-turn chatbot replay,
LLM-as-judge scoring, and report writing.

Run manually:
    python -m services.ace.eval                       # all cases
    python -m services.ace.eval --case <case_id>      # one case
    python -m services.ace.eval --passes 5            # noise control
    python -m services.ace.eval --cases-dir <path>    # custom cases folder

Reuses the same plumbing as `services.ace.cli` (LLM_helper construction,
playbooks dir resolution, skill context provider) without modifying any
existing source.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from services.log_chatbot_service import WifiLogAgentSystem
from services.ace.pipeline import AceRunner
from services.ace import cli as ace_cli  # reuse helpers without modifying

from . import judge as judge_mod


PKG_DIR = Path(__file__).resolve().parent
DEFAULT_CASES_DIR = PKG_DIR / "cases"
DEFAULT_RUNS_DIR = PKG_DIR / "runs"


# ---------------------------------------------------------------------------
# Case loading
# ---------------------------------------------------------------------------
def _is_case_file(p: Path) -> bool:
    if not p.is_file() or p.suffix.lower() != ".json":
        return False
    name = p.name
    # Skip templates and hidden files.
    return not (name.startswith("_") or name.startswith("."))


def load_cases(cases_dir: Path, case_id_filter: Optional[str] = None) -> list[dict]:
    if not cases_dir.exists():
        raise FileNotFoundError(f"Cases dir not found: {cases_dir}")
    cases: list[dict] = []
    for p in sorted(cases_dir.iterdir()):
        if not _is_case_file(p):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[eval] skip unreadable case {p.name}: {e}")
            continue
        data.setdefault("case_id", p.stem)
        data["__source_path"] = str(p)
        if case_id_filter and data.get("case_id") != case_id_filter:
            continue
        cases.append(data)
    return cases


# ---------------------------------------------------------------------------
# Playbook fingerprint
# ---------------------------------------------------------------------------
def _playbook_fingerprint(playbooks_dir: Path) -> dict:
    h = hashlib.sha256()
    files: list[str] = []
    if playbooks_dir.exists():
        for p in sorted(playbooks_dir.glob("*.json")):
            try:
                data = p.read_bytes()
            except Exception:
                continue
            h.update(p.name.encode("utf-8"))
            h.update(b"\0")
            h.update(data)
            files.append(p.name)
    return {
        "playbooks_dir": str(playbooks_dir),
        "files": files,
        "sha256": h.hexdigest(),
    }


# ---------------------------------------------------------------------------
# Chatbot replay (single turn, tools enabled so the agent can read logs)
# ---------------------------------------------------------------------------
def _fresh_agent(llm) -> WifiLogAgentSystem:
    model = getattr(llm, "model", "gpt-4.1")
    return WifiLogAgentSystem(
        client=llm.client,
        model=model,
        skills=llm.skills,
    )


def run_case(llm, ace_runner: AceRunner, case: dict,
             use_tools: bool = True, max_steps: int = 6,
             temperature: float = 0.0) -> dict:
    """Replay one case through a fresh chatbot agent and return the raw answer."""
    agent = _fresh_agent(llm)
    agent.attach_ace(ace_runner)

    log_path = case.get("log_path") or ""
    if log_path:
        # Allow workspace-relative paths in the case file.
        if not Path(log_path).is_absolute():
            log_path = str((Path.cwd() / log_path).resolve())
        agent.current_log_path = log_path

    issue_ctx = case.get("issue_context") or {}
    if issue_ctx:
        agent.prime_with_context(
            case_nbr=str(issue_ctx.get("case_nbr") or ""),
            subject=str(issue_ctx.get("subject") or ""),
            description=str(issue_ctx.get("description") or ""),
            issue_type=str(issue_ctx.get("issue_type") or ""),
            attachment_time=str(issue_ctx.get("attachment_time") or ""),
        )

    user_q = case.get("user_question") or "Please analyze the log and report the root cause."
    try:
        result = agent.chat(
            user_q,
            use_tools=use_tools,
            max_steps=int(max_steps),
            temperature=float(temperature),
        )
    except Exception as e:
        return {
            "status": "agent_error",
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
            "answer": "",
        }

    answer = result.get("data") if isinstance(result, dict) else str(result)
    # `data` for tools mode may be a dict (structured report). Render it as
    # text so the judge can compare against the free-form expected answer.
    if isinstance(answer, dict):
        answer_text = json.dumps(answer, indent=2, ensure_ascii=False)
    else:
        answer_text = str(answer or "")

    return {
        "status": "ok",
        "result_type": (result or {}).get("type") if isinstance(result, dict) else "raw",
        "answer": answer_text,
    }


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def evaluate(cases_dir: Path, runs_dir: Path,
             case_id_filter: Optional[str] = None,
             passes: int = 1,
             judge_temperature: float = 0.2,
             chat_temperature: float = 0.0,
             max_steps: int = 6,
             use_tools: bool = True,
             model: Optional[str] = None,
             judge_model: Optional[list[str] | str] = None) -> dict:
    ace_cli._ensure_avatarfiles_dir()
    cases = load_cases(cases_dir, case_id_filter=case_id_filter)
    if not cases:
        print(f"[eval] no cases found in {cases_dir} "
              f"(filter={case_id_filter!r})")
        return {"status": "no_cases", "cases_dir": str(cases_dir)}

    print(f"[eval] loaded {len(cases)} case(s) from {cases_dir}")

    llm = ace_cli._build_llm(model)
    # _build_llm() intentionally skips skill loading (it's only needed for
    # Reflector/Curator), but the chatbot agent MUST have the same skills
    # the production app uses — otherwise every fetch_filtered_logs(...) call
    # falls through to "skill not found" and the eval scores garbage.
    if not getattr(llm, "skills", None):
        skills_dict = ace_cli._load_active_skills()
        if skills_dict:
            llm.skills = skills_dict
            print(f"[eval] populated llm.skills with {len(skills_dict)} skill(s) "
                  f"from active YAML")
        else:
            print("[eval] WARNING: active skills YAML unreachable — agent will "
                  "fall back to built-in skills and likely fail tool calls")

    # Judge LLM(s). `judge_model` may be a single string, a list of model
    # names (one judgement per model), or None (reuse the chatbot LLM).
    if isinstance(judge_model, str):
        judge_model_list = [judge_model]
    else:
        judge_model_list = list(judge_model or [])

    chat_model_name = getattr(llm, "model", None)
    judge_llms: list = []
    if not judge_model_list:
        judge_llms = [llm]
        print(f"[eval] judge model      : {chat_model_name} "
              f"(shared with chat model, {passes} pass(es))")
    else:
        for m in judge_model_list:
            if m == chat_model_name:
                judge_llms.append(llm)
            else:
                judge_llms.append(ace_cli._build_llm(m))
        names = [getattr(l, "model", None) for l in judge_llms]
        if len(judge_llms) == 1:
            print(f"[eval] judge model      : {names[0]} "
                  f"({passes} pass(es))")
        else:
            print(f"[eval] judge models     : {names} "
                  f"(one pass per model; --passes ignored)")

    playbooks_dir = ace_cli._resolve_playbooks_dir()
    feedback_root = ace_cli._resolve_feedback_root()
    ace_runner = AceRunner(
        llm=llm,
        playbooks_dir=playbooks_dir,
        feedback_root=feedback_root,
        skills=list((llm.skills or {}).keys()) or None,
        skill_context_provider=ace_cli._skill_context_provider,
    )

    pb_fingerprint = _playbook_fingerprint(playbooks_dir)
    print(f"[eval] playbook sha256={pb_fingerprint['sha256'][:12]} "
          f"files={pb_fingerprint['files']}")

    per_case: list[dict] = []
    for i, case in enumerate(cases, 1):
        cid = case.get("case_id")
        print(f"\n[eval] ({i}/{len(cases)}) running case '{cid}' ...")
        run = run_case(
            llm, ace_runner, case,
            use_tools=use_tools,
            max_steps=max_steps,
            temperature=chat_temperature,
        )
        record: dict = {
            "case_id": cid,
            "skill": case.get("skill"),
            "source_path": case.get("__source_path"),
            "agent": {
                "status": run["status"],
                "result_type": run.get("result_type"),
                "answer": run["answer"],
            },
        }
        if run["status"] != "ok":
            record["agent"]["error"] = run.get("error")
            record["judge"] = {"skipped": "agent_failed"}
            print(f"  ✗ agent failed: {run.get('error')}")
            per_case.append(record)
            continue

        if len(judge_llms) > 1:
            print(f"  ✓ agent answered ({len(run['answer'])} chars); "
                  f"judging with {len(judge_llms)} model(s) x1 pass each ...")
            scored = judge_mod.judge_multi(
                judge_llms, case, run["answer"],
                temperature=judge_temperature,
            )
        else:
            print(f"  ✓ agent answered ({len(run['answer'])} chars); judging x{passes} ...")
            scored = judge_mod.judge(
                judge_llms[0], case, run["answer"],
                passes=passes,
                temperature=judge_temperature,
            )
        record["judge"] = scored
        overall = scored.get("mean_overall", 0.0)
        stdev = scored.get("stdev_overall", 0.0)
        print(f"  → overall={overall:.2f}/5 (±{stdev:.2f}) "
              f"facets={scored.get('mean_scores')}")
        per_case.append(record)

    # Aggregate
    scored_cases = [
        c for c in per_case
        if isinstance(c.get("judge"), dict) and "mean_overall" in c["judge"]
    ]
    if scored_cases:
        agg_overall = round(
            sum(c["judge"]["mean_overall"] for c in scored_cases) / len(scored_cases),
            3,
        )
    else:
        agg_overall = 0.0

    # Sum judge token usage across all cases (per-case usage is already an
    # aggregate over that case's passes; here we roll everything up).
    judge_usage_totals = {
        "prompt_tokens":     sum(int(((c.get("judge") or {}).get("usage") or {}).get("prompt_tokens",     0)) for c in per_case),
        "completion_tokens": sum(int(((c.get("judge") or {}).get("usage") or {}).get("completion_tokens", 0)) for c in per_case),
        "total_tokens":      sum(int(((c.get("judge") or {}).get("usage") or {}).get("total_tokens",      0)) for c in per_case),
        "calls":             sum(int(((c.get("judge") or {}).get("usage") or {}).get("calls",             0)) for c in per_case),
    }

    report = {
        "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cases_dir": str(cases_dir),
        "playbook": pb_fingerprint,
        "judge": {
            "passes_per_case": (len(judge_llms) if len(judge_llms) > 1 else passes),
            "judge_temperature": judge_temperature,
            "models": [getattr(l, "model", None) for l in judge_llms],
            "mode": ("multi_model" if len(judge_llms) > 1 else "single_model"),
        },
        "chatbot": {
            "use_tools": use_tools,
            "max_steps": max_steps,
            "chat_temperature": chat_temperature,
        },
        "aggregate": {
            "cases_total": len(per_case),
            "cases_scored": len(scored_cases),
            "mean_overall": agg_overall,
            "judge_usage": judge_usage_totals,
        },
        "cases": per_case,
    }

    runs_dir.mkdir(parents=True, exist_ok=True)
    stamp = report["ts_utc"].replace(":", "").replace("-", "")
    out_path = runs_dir / f"eval_{stamp}.json"
    out_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Also dump each case's raw LLM analyzed result to its own JSON file so
    # it can be inspected / diffed without wading through the aggregate report.
    answers_dir = runs_dir / f"answers_{stamp}"
    answers_dir.mkdir(parents=True, exist_ok=True)
    for c in per_case:
        cid = c.get("case_id") or "unknown"
        safe_cid = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_"
                           for ch in str(cid))
        agent_info = c.get("agent") or {}
        raw_answer = agent_info.get("answer", "")
        # If the agent returned a JSON-serialized dict, keep it as structured
        # JSON; otherwise store the plain text.
        parsed: object
        try:
            parsed = json.loads(raw_answer) if raw_answer else ""
        except (ValueError, TypeError):
            parsed = raw_answer
        answer_record = {
            "case_id": cid,
            "skill": c.get("skill"),
            "source_path": c.get("source_path"),
            "ts_utc": report["ts_utc"],
            "status": agent_info.get("status"),
            "result_type": agent_info.get("result_type"),
            "answer": parsed,
            "answer_raw": raw_answer,
        }
        if agent_info.get("error"):
            answer_record["error"] = agent_info["error"]
        (answers_dir / f"{safe_cid}.json").write_text(
            json.dumps(answer_record, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    print("\n========== EVAL SUMMARY ==========")
    print(f"playbook sha256 : {pb_fingerprint['sha256'][:12]}")
    print(f"cases total     : {len(per_case)}")
    print(f"cases scored    : {len(scored_cases)}")
    print(f"aggregate score : {agg_overall:.2f} / 5")
    ju = judge_usage_totals
    if ju.get("calls"):
        print(f"judge tokens    : {ju['total_tokens']:,} total "
              f"({ju['prompt_tokens']:,} prompt + {ju['completion_tokens']:,} completion) "
              f"across {ju['calls']} call(s)")
    for c in per_case:
        j = c.get("judge") or {}
        if "mean_overall" in j:
            line = (f"  - {c['case_id']:30s} "
                    f"overall={j['mean_overall']:.2f}  "
                    f"facets={j.get('mean_scores')}")
        else:
            line = f"  - {c['case_id']:30s} SKIPPED ({j.get('skipped') or 'unknown'})"
        print(line)
    print(f"\nreport written  : {out_path}")
    print(f"answers written : {answers_dir}")
    print("==================================")
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m services.ace.eval",
        description="Manually evaluate ACE playbook quality against golden cases.",
    )
    p.add_argument("--cases-dir", type=Path, default=DEFAULT_CASES_DIR,
                   help=f"Directory holding *.json case files (default: {DEFAULT_CASES_DIR})")
    p.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR,
                   help=f"Where to write report JSON (default: {DEFAULT_RUNS_DIR})")
    p.add_argument("--case", dest="case_id", default=None,
                   help="Run only this case_id")
    p.add_argument("--passes", type=int, default=1,
                   help="Judge passes per case for noise reduction (default 1)")
    p.add_argument("--judge-temperature", type=float, default=0.2)
    p.add_argument("--chat-temperature", type=float, default=0.0)
    p.add_argument("--max-steps", type=int, default=6,
                   help="Max chatbot tool steps per case (default 6)")
    p.add_argument("--no-tools", action="store_true",
                   help="Disable chatbot tool use (faster, less faithful replay)")
    p.add_argument("--model", default=None,
                   help="Override LLM model for the chatbot replay "
                        "(defaults to the same one the app uses)")
    p.add_argument("--judge-model", nargs="+", default=None, metavar="MODEL",
                   help="One or more LLM model names used by the judge. If "
                        "multiple are given, each model judges every case "
                        "exactly once and --passes is ignored. Defaults to "
                        "--model / the app's LLM.")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        evaluate(
            cases_dir=args.cases_dir,
            runs_dir=args.runs_dir,
            case_id_filter=args.case_id,
            passes=args.passes,
            judge_temperature=args.judge_temperature,
            chat_temperature=args.chat_temperature,
            max_steps=args.max_steps,
            use_tools=not args.no_tools,
            model=args.model,
            judge_model=args.judge_model,
        )
    except Exception as e:
        print(f"[eval] fatal: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
