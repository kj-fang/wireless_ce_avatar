"""
Eval runner: orchestrates case discovery, single-turn chatbot replay,
LLM-as-judge scoring, and report writing.

Run manually:
    python -m services.ace.eval                       # all cases
    python -m services.ace.eval --case <case_id>      # one case
    python -m services.ace.eval --passes 5            # noise control
    python -m services.ace.eval --cases-dir <path>    # custom cases folder
    python -m services.ace.eval --review              # chain eval -> review
    python -m services.ace.eval --auto-fix            # chain eval -> review -> corrupted_bullet -y

Output layout:
    runs/<stamp>/eval_<stamp>.json
    runs/<stamp>/answers_<stamp>.json
    runs/<stamp>/review_<stamp>.json

Each invocation creates a fresh stamp folder under runs/ so the judge,
review, and auto-fix artifacts stay grouped by run.

Reuses the same plumbing as `services.ace.cli` (LLM_helper construction,
playbooks dir resolution, skill context provider) without modifying any
existing source.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from services.log_chatbot_service import WifiLogAgentSystem
from services.ace.pipeline import AceRunner
from services.ace import cli as ace_cli  # reuse helpers without modifying

from . import judge as judge_mod
from . import golden_set_sync


PKG_DIR = Path(__file__).resolve().parent
EVAL_LOOKUP_JSON = PKG_DIR / "playbook_to_golden_set.json"
RECENT_PLAYBOOK_HOURS = 3


def _current_user_downloads() -> Path:
    """Return the current Windows user's real Downloads folder.

    Deliberately bypasses `path_configs.DOWNLOADS_DIR` — that override is
    pinned to the training server's account (`C:\\Users\\admin\\...`) so
    the running app + CLI always target the canonical trainer directory.
    The eval, however, is a per-user dev tool that must land in the
    profile actually running it (e.g. `C:\\Users\\<me>\\Downloads`),
    otherwise it hits `PermissionError` on any box whose user isn't
    `admin`. We query the HKCU shell-folders key directly, falling back
    to `%USERPROFILE%\\Downloads` if the registry read fails.
    """
    try:
        import winreg  # local import: keeps module importable on non-Windows
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        ) as key:
            path, _ = winreg.QueryValueEx(
                key, "{374DE290-123F-4565-9164-39C4925E467B}"
            )
            return Path(path)
    except Exception:
        return Path(os.path.expandvars(r"%USERPROFILE%\Downloads"))


def _avatarfiles_dir() -> Path:
    """`<current user Downloads>\\IntelAvatar_files`, per-machine.

    Unlike `helpers.init_download_dir()`, this ignores the
    `path_configs.DOWNLOADS_DIR` override so the eval always uses the
    logged-in user's Downloads folder — see `_current_user_downloads`.
    """
    root = _current_user_downloads() / "IntelAvatar_files"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _default_cases_dir() -> Path:
    """Local mirror of the golden-set, under `<avatarfiles_dir>\\golden_set`."""
    return _avatarfiles_dir() / "golden_set"


def _default_playbooks_dir() -> Path:
    """WiFi playbook working dir — mirrors the app's
    `<avatarfiles_dir>\\ace_playbooks\\local` layout but rooted at the
    current user's Downloads folder (not the trainer override), for the
    same reason `_avatarfiles_dir` bypasses `DOWNLOADS_DIR`.
    """
    d = _avatarfiles_dir() / "ace_playbooks" / "local"
    d.mkdir(parents=True, exist_ok=True)
    return d


# Golden-set cases live on the shared server, but running the eval directly
# off the SMB share is painfully slow. We keep a local mirror on disk and
# sync (server → local, newer-mtime wins) at the start of every run.
SERVER_CASES_DIR = Path(r"\\infs089b.iil.intel.com\HOME\WirelessCE\Intel_WirelessCE_Avatar\golden_set")
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
        # Non-recursive glob — deliberately skips the `history/` subfolder
        # that lives under the shared ace_playbook directory.
        for p in sorted(playbooks_dir.glob("*.json")):
            if not p.is_file():
                continue
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


def _run_stamp_dir(runs_dir: Path, stamp: str) -> Path:
    return runs_dir / stamp

# ---------------------------------------------------------------------------
# Selective-run helpers (skip cases whose playbook wasn't touched recently)
# ---------------------------------------------------------------------------
def _load_playbook_lookup(path: Path = EVAL_LOOKUP_JSON) -> dict:
    """Return the `mapping` dict from the playbook -> golden-set lookup JSON.

    Returns an empty dict if the file is missing or unparseable, in which
    case the caller falls back to "run everything".
    """
    if not path.exists():
        print(f"[eval] WARNING: lookup table not found: {path} "
              f"— selective run disabled, will run all cases")
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[eval] WARNING: cannot parse {path.name}: {e} "
              f"— selective run disabled")
        return {}
    return data.get("mapping") or {}


def _recent_playbooks(playbooks_dir: Path, *, hours: int) -> list[str]:
    """Return lowercased filename stems of playbook JSONs whose top-level
    `updated_at` is within the last `hours` hours. Non-recursive."""
    if not playbooks_dir.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    recent: list[str] = []
    for p in sorted(playbooks_dir.glob("*.json")):
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        ts_raw = data.get("updated_at")
        if not ts_raw:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_raw))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            recent.append(p.stem.lower())
    return recent


def _triggered_categories(recent_stems: list[str], lookup: dict) -> object:
    """Map recently-updated playbook stems to golden-set categories.

    Returns the sentinel string ``"all"`` when any updated playbook is
    marked as global (``"all"`` in the lookup), otherwise a set of
    lowercased category names. Empty set means "nothing triggered".
    An empty lookup falls back to ``"all"`` (safe default: run everything).
    """
    if not lookup:
        return "all"
    lut = {str(k).lower(): v for k, v in lookup.items()}
    categories: set[str] = set()
    for stem in recent_stems:
        entry = lut.get(stem)
        if entry is None:
            print(f"[eval]   playbook '{stem}' has no lookup entry — ignored")
            continue
        if isinstance(entry, str) and entry.lower() == "all":
            return "all"
        if isinstance(entry, list):
            for c in entry:
                categories.add(str(c).lower())
    return categories


def _case_category(case: dict) -> str:
    """Golden-set category for a case (lowercased), derived strictly from
    the case JSON's filename.

    Convention: the filename prefix before the first underscore is the
    category (e.g. `Connectivity_1.json` -> `connectivity`,
    `SoftAP_flow_2.json` -> `softap`). Files with no underscore fall back
    to the full stem. The `skill` field inside the case is intentionally
    ignored — filenames are the single source of truth so the mapping
    stays predictable.
    """
    src = case.get("__source_path") or ""
    stem = Path(src).stem
    if "_" in stem:
        return stem.split("_", 1)[0].lower()
    return stem.lower()


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


def _resolve_case_log_path(log_path: str, case: dict) -> str:
    """Resolve a case's log file.

    An absolute path is used as-is. A relative path is resolved against the
    folder the case JSON was loaded from (e.g. the shared golden_set folder),
    so the log is pulled from the SAME place as the case — not the process CWD.
    We try, in order, the log next to the case file (by basename), then the
    relative path preserved under that folder, and finally fall back to CWD
    (legacy behaviour for a local repo-relative layout). The first path that
    exists wins; if none exist we return the share-based candidate so the
    downstream error message points at the shared location.
    """
    p = Path(log_path)
    if p.is_absolute():
        return str(p)

    src = case.get("__source_path")
    case_dir = Path(src).parent if src else None

    candidates: list[Path] = []
    if case_dir is not None:
        candidates.append(case_dir / p.name)   # log next to the case json (share)
        candidates.append(case_dir / p)        # relative path kept under the share
    candidates.append((Path.cwd() / p))        # legacy: repo-relative from CWD

    for c in candidates:
        if c.exists():
            return str(c.resolve())
    return str(candidates[0].resolve() if candidates else p)


def run_case(llm, ace_runner: AceRunner, case: dict,
             use_tools: bool = True, max_steps: int = 6,
             temperature: float = 0.0) -> dict:
    """Replay one case through a fresh chatbot agent and return the raw answer."""
    agent = _fresh_agent(llm)
    agent.attach_ace(ace_runner)

    log_path = case.get("log_path") or ""
    if log_path:
        # Resolve relative paths against the case JSON's own directory first
        # (so `"log_path": "Connectivity_1.log"` picks up the sibling file in
        # the cases folder), then fall back to CWD for backwards compatibility.
        if not Path(log_path).is_absolute():
            src = case.get("__source_path")
            candidates = []
            if src:
                candidates.append((Path(src).parent / log_path).resolve())
            candidates.append((Path.cwd() / log_path).resolve())
            resolved = next((c for c in candidates if c.exists()), candidates[0])
            log_path = str(resolved)
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
             judge_model: Optional[list[str] | str] = None,
             sync_golden_set: bool = True,
             run_all: bool = False) -> dict:
    # Pin `app_config.avatarfiles_dir` to the CURRENT user's IntelAvatar_files
    # folder BEFORE any ACE helper reads it. `ace_cli._ensure_avatarfiles_dir`
    # would otherwise defer to `helpers.init_download_dir()`, which honors
    # `path_configs.DOWNLOADS_DIR` — that override is pinned to the training
    # server's `C:\Users\admin` and any dev box hits PermissionError. Once
    # this is set every downstream helper (skills YAML loader, feedback
    # root, playbook sync) rehomes to `<current user Downloads>\...`.
    try:
        from configs.global_configs import app_config
        app_config.set_avatarfiles_dir(str(_avatarfiles_dir()))
    except Exception as e:
        print(f"[eval] WARNING: could not pin avatarfiles_dir: {e}")
    ace_cli._ensure_avatarfiles_dir()

    # Pull the latest skills YAML from the share into the local `cloud/`
    # mirror. Without this, a fresh machine that has never booted the main
    # Avatar app has no skills on disk and the agent falls back to
    # built-in stubs ("No skills source available"). Best-effort — if the
    # share is unreachable we still try whatever is cached locally.
    try:
        from utils import skills_yaml_utils as _sy
        _sy.set_active_source("cloud")
        refreshed_path, _refreshed_date = _sy.refresh_local_cloud_baseline()
        if refreshed_path is not None:
            print(f"[eval] refreshed local skills YAML → {refreshed_path}")
        else:
            print("[eval] skills YAML share unreachable — using existing local mirror")
    except Exception as e:
        print(f"[eval] WARNING: skills YAML refresh failed: {e}")
    # Drop any cached empty result so `_load_active_skills` re-scans now that
    # `avatarfiles_dir` and the local mirror are populated.
    try:
        ace_cli._SKILLS_CACHE.clear()
    except Exception:
        pass

    # Sync the golden-set from the shared server down to the local cache
    # before we start reading cases. Only trigger this when the caller is
    # pointing at the default local mirror — an explicit --cases-dir is
    # respected verbatim so users can point at ad-hoc folders.
    default_cases_dir = _default_cases_dir()
    is_default_local = (
        Path(cases_dir).resolve() == default_cases_dir.resolve()
    )
    if is_default_local:
        if sync_golden_set:
            cases_dir = golden_set_sync.sync_golden_set(
                SERVER_CASES_DIR, default_cases_dir,
            )
        else:
            # --no-sync still needs the local folder to exist so
            # load_cases() doesn't blow up on a fresh machine.
            golden_set_sync._ensure_local_dir(default_cases_dir)
            cases_dir = default_cases_dir

    cases = load_cases(cases_dir, case_id_filter=case_id_filter)
    if not cases:
        print(f"[eval] no cases found in {cases_dir} "
              f"(filter={case_id_filter!r})")
        return {"status": "no_cases", "cases_dir": str(cases_dir)}

    print(f"[eval] loaded {len(cases)} case(s) from {cases_dir}")

    # ------------------------------------------------------------------
    # Selective run: keep only cases whose category is tied to a playbook
    # that was updated within RECENT_PLAYBOOK_HOURS. `--all` (run_all=True)
    # or an explicit --case filter bypasses this entirely.
    # ------------------------------------------------------------------
    playbooks_dir = _default_playbooks_dir()
    if not playbooks_dir.exists():
        print(f"[eval] WARNING: playbook dir not reachable: {playbooks_dir} "
              f"— check the Avatar app has run at least once on this machine")

    filter_info: dict = {
        "enabled": not run_all and case_id_filter is None,
        "window_hours": RECENT_PLAYBOOK_HOURS,
        "updated_playbooks": [],
        "triggered_categories": None,
        "cases_selected": [c.get("case_id") for c in cases],
        "cases_skipped": [],
    }
    if filter_info["enabled"]:
        lookup = _load_playbook_lookup()
        recent = _recent_playbooks(playbooks_dir,
                                   hours=RECENT_PLAYBOOK_HOURS)
        filter_info["updated_playbooks"] = recent
        triggered = _triggered_categories(recent, lookup)
        if triggered == "all":
            filter_info["triggered_categories"] = "all"
            print(f"[eval] recent playbook update triggers ALL categories "
                  f"(window={RECENT_PLAYBOOK_HOURS}h, updated={recent})")
        else:
            filter_info["triggered_categories"] = sorted(triggered)
            kept: list[dict] = []
            selected_ids: list = []
            skipped_ids: list = []
            for c in cases:
                cat = _case_category(c)
                if cat in triggered:
                    kept.append(c)
                    selected_ids.append(c.get("case_id"))
                else:
                    skipped_ids.append(c.get("case_id"))
            filter_info["cases_selected"] = selected_ids
            filter_info["cases_skipped"] = skipped_ids
            if not kept:
                print(f"[eval] no cases triggered "
                      f"(window={RECENT_PLAYBOOK_HOURS}h, "
                      f"updated_playbooks={recent}, "
                      f"triggered_categories={sorted(triggered)})")
                return {
                    "status": "no_cases_triggered",
                    "cases_dir": str(cases_dir),
                    "filter": filter_info,
                }
            print(f"[eval] {len(kept)}/{len(cases)} case(s) triggered by "
                  f"recent playbook update(s) {recent} -> "
                  f"categories={sorted(triggered)}")
            cases = kept
    elif run_all:
        print(f"[eval] --all set: bypassing recent-playbook filter "
              f"(window={RECENT_PLAYBOOK_HOURS}h)")

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
        "filter": filter_info,
        "cases": per_case,
    }

    stamp = report["ts_utc"].replace(":", "").replace("-", "")
    run_dir = _run_stamp_dir(runs_dir, stamp)
    run_dir.mkdir(parents=True, exist_ok=True)
    out_path = run_dir / f"eval_{stamp}.json"
    out_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Consolidated per-case answer file, stored alongside the eval report.
    answers_path = run_dir / f"answers_{stamp}.json"
    answers_records: list[dict] = []
    for c in per_case:
        cid = c.get("case_id") or "unknown"
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
        answers_records.append(answer_record)

    answers_report = {
        "ts_utc": report["ts_utc"],
        "eval_path": str(out_path),
        "cases": answers_records,
    }
    answers_path.write_text(
        json.dumps(answers_report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    report["run_dir"] = str(run_dir)
    report["eval_path"] = str(out_path)
    report["answers_path"] = str(answers_path)
    out_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
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
    print(f"answers written : {answers_path}")
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
    # `--cases-dir` defaults to None here so we can lazily resolve
    # <Downloads>\IntelAvatar_files\golden_set for the *current* Windows
    # user (see main()). Hard-coding the default at import time would bake
    # in whatever username generated the argparse help text.
    p.add_argument("--cases-dir", type=Path, default=None,
                   help="Directory holding *.json case files "
                        "(default: <Downloads>/IntelAvatar_files/golden_set)")
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
    p.add_argument("--review", action="store_true",
                   help="After the eval finishes, run services.ace.eval.review "
                        "on the eval report that was just written.")
    p.add_argument("--auto-fix", action="store_true",
                   help="After --review, run services.ace.eval.corrupted_bullet "
                        "with -y to auto-revert (or remove if no snapshot "
                        "exists) every bullet the reviewer flagged as "
                        "harmful. Implies --review.")
    p.add_argument("--no-sync", action="store_true",
                   help="Skip syncing the golden-set from the shared server. "
                        "Use the existing local cache as-is. Only takes effect "
                        "when --cases-dir is left at the default local path.")
    p.add_argument("--all", dest="run_all", action="store_true",
            help="Force-run every case, bypassing the "
                    f"recent-playbook-update filter "
                    f"(default window: last {RECENT_PLAYBOOK_HOURS}h).")
    return p


def _chain_review_and_fix(
    report: dict,
    runs_dir: Path,
    model: Optional[str],
    do_review: bool,
    do_auto_fix: bool,
) -> int:
    """
    Optionally chain `services.ace.eval.review` and
    `services.ace.eval.corrupted_bullet -y` after the eval finishes.
    Returns a process exit code (0 = ok, non-zero = failure or review FAIL).
    """
    if not (do_review or do_auto_fix):
        return 0
    if report.get("status") == "no_cases" or "ts_utc" not in report:
        print("[eval] skipping --review/--auto-fix: no eval report produced.")
        return 0

    eval_path_raw = report.get("eval_path")
    if eval_path_raw:
        eval_path = Path(eval_path_raw)
    else:
        run_dir = Path(report.get("run_dir") or runs_dir)
        stamp = report["ts_utc"].replace(":", "").replace("-", "")
        eval_path = run_dir / stamp / f"eval_{stamp}.json"
    if not eval_path.is_file():
        print(f"[eval] cannot chain --review: eval file missing: {eval_path}",
              file=sys.stderr)
        return 1

    from . import review as review_mod
    print("\n[eval] ---- chaining review ----")
    review_report = review_mod.review(eval_path, model=model)

    if not do_auto_fix:
        return 0 if review_report.get("gate_verdict") == "PASS" else 2

    r_stamp = review_report["ts_utc"].replace(":", "").replace("-", "")
    review_path = eval_path.parent / f"review_{r_stamp}.json"
    if not review_path.is_file():
        print(f"[eval] cannot chain --auto-fix: review file missing: "
              f"{review_path}", file=sys.stderr)
        return 1

    from . import corrupted_bullet as cb_mod
    print("\n[eval] ---- chaining corrupted-bullet triage (auto, -y) ----")
    cb_mod.process(review_path, auto_revert=True, auto_remove=True)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    # Resolve the default cases dir lazily so the current Windows user's
    # Downloads folder is picked up (rather than whatever was baked in at
    # argparse-help time).
    cases_dir = args.cases_dir if args.cases_dir is not None else _default_cases_dir()
    try:
        report = evaluate(
            cases_dir=cases_dir,
            runs_dir=args.runs_dir,
            case_id_filter=args.case_id,
            passes=args.passes,
            judge_temperature=args.judge_temperature,
            chat_temperature=args.chat_temperature,
            max_steps=args.max_steps,
            use_tools=not args.no_tools,
            model=args.model,
            judge_model=args.judge_model,
            sync_golden_set=not args.no_sync,
            run_all=args.run_all,
        )
        return _chain_review_and_fix(
            report,
            runs_dir=args.runs_dir,
            model=args.model,
            do_review=args.review,
            do_auto_fix=args.auto_fix,
        )
    except Exception as e:
        print(f"[eval] fatal: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
