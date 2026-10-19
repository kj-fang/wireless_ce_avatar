"""
BSOD branch of the Handsfree Replyer — Avatar's BSOD flow, unattended.

Avatar's manual flow (index page "BSOD" toggle -> select attachments ->
download -> bsod.html -> bsod_submit): the uploads are downloaded, NOT
decomposed, to the BSOD share (configs.path_configs.LOAD_PATH_prim/bkup,
one folder per case) and the folder path plus the user's e-mail are POSTed
to the PotatoFarm BSOD parser (keys.potatofarm_url, X-Api-Token
keys.potatofarm_api). PotatoFarm e-mails the analysis; nothing comes back
synchronously.

Handsfree does the same without a human (Charles, 2026-10-14): a case
classified BSOD whose attachments include a kernel/memory dump needs NO
issue time (the dump carries the crash), the dump uploads go to the share
and PotatoFarm is triggered once per upload set. <handsfree>/
bsod_submissions.json remembers what was submitted (and which downloads
failed), so later rounds only submit new uploads and never spam the parser.

PotatoFarm also writes its results back INTO the case folder a few minutes
later (seen 2026-10-14 on 01032878: ~6 min for a 3.35 GB zip holding a
16 GB MEMORY.DMP): bsod_parse_output.json (one entry per dump: bugcheck,
failure bucket, is_wifi_driver_fault / is_bt_driver_fault, driver build,
stacks, JIRA decision), bsod_report.html and windbg_outputs/. The runner
waits for that file (config bsod_result_wait_min, default 10) and the draft
carries the parsed verdict, so the BSOD analysis continues in handsfree.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

_BSOD_TEXT_RE = re.compile(
    r"(?i)\bBSOD\b|blue ?screen|bug ?check|stop code|kernel dump|memory\.dmp|minidump")
# Upload names that can carry the crash dump (archived or not).
_DUMP_NAME_RE = re.compile(
    r"(?i)\.(dmp|mdmp|hdmp|kdmp)\b|minidump|memory|\bdump|bsod|crash")
# Upload names that are driver/log captures, never a dump.
_LOG_NAME_RE = re.compile(r"(?i)wrt|\blog|trace|\betl\b|wpp|sniffer|autologger")
_ARCHIVE_EXTS = (".zip", ".rar", ".7z")
_LOCK = threading.Lock()


def quick_hint(subject: str = "", subcategory: str = "", description: str = "") -> bool:
    """Cheap pre-triage signal (used to skip the WRT prefetch: a BSOD case's
    newest archive is a multi-GB memory dump, not a WRT capture)."""
    return bool(_BSOD_TEXT_RE.search(
        " ".join(str(x or "")[:2000] for x in (subject, subcategory, description))))


def is_bsod_case(*, issue_domain: str = "", issue_type: str = "", subject: str = "",
                 description: str = "", subcategory: str = "") -> bool:
    """BSOD by the checklist domain (reader / IPS subcategory / triage), the
    triage issue type, or BSOD wording in the case."""
    if str(issue_domain or "").strip().lower() == "bsod":
        return True
    if "bsod" in str(issue_type or "").lower():
        return True
    return quick_hint(subject, subcategory, description)


def find_dump_attachments(attachment_list) -> list:
    """Uploads that can carry the crash dump: names that say dump / memory /
    minidump / .dmp (01032878: SKU15_0x9F_MEMORY.zip). When none is named
    like that, every archive that is not obviously a log capture (OEMs zip
    MEMORY.DMP under arbitrary names); a WRT-looking archive is never a dump."""
    items = [it for it in (attachment_list or []) if it and str(it[0]).strip()]
    named = [it for it in items if _DUMP_NAME_RE.search(str(it[0]))]
    if named:
        return named
    return [it for it in items
            if str(it[0]).lower().endswith(_ARCHIVE_EXTS)
            and not _LOG_NAME_RE.search(str(it[0]))]


def share_case_dir(case_nbr: str, backend_id: str = "") -> str:
    """The folder Avatar's manual flow downloads to
    (main_routes._resolve_download_path): <BSOD share>\\<backend id or case
    number>. "" when no share is reachable."""
    from configs.path_configs import LOAD_PATH_prim, LOAD_PATH_bkup
    from utils import helpers
    root = helpers.get_load_path(LOAD_PATH_prim, LOAD_PATH_bkup)
    if not root:
        return ""
    folder = backend_id if "-" in str(backend_id or "") else str(case_nbr)
    return rf"{root}\{folder}"


def handsfree_config() -> dict:
    try:
        from configs.global_configs import app_config
        from .queue import HandsfreeStore
        return HandsfreeStore(Path(app_config.avatarfiles_dir) / "handsfree").load_config()
    except Exception as e:
        print(f"[handsfree.bsod] config unavailable: {e}")
        return {}


def resolve_email(config: dict | None) -> str:
    """Where PotatoFarm mails the result: handsfree config `bsod_email`, else
    the Windows login's UPN (what Avatar's bsod.html pre-fills)."""
    email = str((config or {}).get("bsod_email") or "").strip()
    if email:
        return email
    from utils import helpers
    return (helpers.detect_user_email() or "").strip()


def submit_to_potatofarm(dumps_path: str, email: str, key=None) -> dict:
    """POST the dump folder to the PotatoFarm BSOD parser — the same payload
    blueprints/bsod/bsod.py sends. Returns {"ok", "status", "detail"}; never
    raises."""
    import requests
    from configs.global_configs import app_config
    key = key or getattr(app_config, "key", None)
    url = getattr(key, "potatofarm_url", "") or ""
    token = getattr(key, "potatofarm_api", "") or ""
    if not url or not token:
        return {"ok": False, "status": 0,
                "detail": "PotatoFarm URL / API token missing from keys"}
    payload = {"DUMPS_PATH": dumps_path, "EMAIL_LIST": email}
    print(f"[handsfree.bsod] PotatoFarm submit: {json.dumps(payload)}")
    try:
        resp = requests.post(url, data=json.dumps(payload),
                             headers={"Content-Type": "application/json",
                                      "X-Api-Token": token},
                             proxies={"http": None, "https": None},
                             verify=False, timeout=60)
    except Exception as e:
        return {"ok": False, "status": 0, "detail": f"{type(e).__name__}: {e}"}
    return {"ok": resp.status_code == 200, "status": resp.status_code,
            "detail": (resp.text or "")[:300]}


# ---------------------------------------------------------------------------
# Submission record (so a later round does not resubmit the same uploads)
# ---------------------------------------------------------------------------

def _submissions_path() -> Path:
    from configs.global_configs import app_config
    return Path(app_config.avatarfiles_dir) / "handsfree" / "bsod_submissions.json"


def _load() -> dict:
    try:
        p = _submissions_path()
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[handsfree.bsod] submissions read failed: {e}")
    return {}


def submissions(case_nbr: str) -> list[dict]:
    """[{submitted_at, attachments, dumps_path, email, status}] oldest first."""
    return list(_load().get(str(case_nbr)) or [])


def submitted_names(case_nbr: str) -> set[str]:
    return {str(n) for s in submissions(case_nbr) for n in (s.get("attachments") or [])}


MAX_DOWNLOAD_ROUNDS = 2


def failed_names(case_nbr: str) -> dict[str, int]:
    """{upload name: rounds in which its download failed}."""
    out: dict[str, int] = {}
    for s_ in submissions(case_nbr):
        for n in s_.get("failed") or []:
            out[str(n)] = out.get(str(n), 0) + 1
    return out


def names_to_skip(case_nbr: str) -> set[str]:
    """Uploads a round must not fetch again: already submitted, or their
    download failed MAX_DOWNLOAD_ROUNDS times (a stalled multi-GB memory
    dump would otherwise cost hours every round — the reviewer fetches it
    manually or asks for a smaller upload)."""
    skip = submitted_names(case_nbr)
    skip |= {n for n, c in failed_names(case_nbr).items() if c >= MAX_DOWNLOAD_ROUNDS}
    return skip


def forget_case(case_nbr: str) -> int:
    """Drop a case's submission record (handsfree 'reset case'). Returns the
    number of entries removed."""
    with _LOCK:
        data = _load()
        entries = data.pop(str(case_nbr), None) or []
        if entries:
            p = _submissions_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, p)
        return len(entries)


def record_submission(case_nbr: str, attachments: list, dumps_path: str,
                      email: str, status, failed: list | None = None) -> None:
    with _LOCK:
        data = _load()
        data.setdefault(str(case_nbr), []).append({
            "submitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "attachments": [str(a) for a in attachments],
            "failed": [str(a) for a in (failed or [])],
            "dumps_path": dumps_path, "email": email, "status": status})
        p = _submissions_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, p)


# ---------------------------------------------------------------------------
# PotatoFarm results (written into the case folder on the share)
# ---------------------------------------------------------------------------

RESULT_JSON = "bsod_parse_output.json"
RESULT_HTML = "bsod_report.html"


def result_mtime(dumps_path: str) -> float:
    """mtime of the parser's JSON in the folder, 0 when absent — read BEFORE
    a submission so the wait below looks for a NEWER file, independent of
    clock skew between this PC and the share."""
    try:
        return (Path(dumps_path) / RESULT_JSON).stat().st_mtime
    except OSError:
        return 0.0


def _first_stack(d: dict, limit: int = 8) -> list[str]:
    for st in d.get("stacks") or []:
        if isinstance(st, dict):
            for _core, frames in st.items():
                return [str(f) for f in (frames or [])][:limit]
    return []


def read_results(dumps_path: str, newer_than: float = 0.0) -> list[dict]:
    """Parsed PotatoFarm verdicts, one per dump in the folder; [] when the
    parser has not written (a newer) bsod_parse_output.json yet."""
    p = Path(dumps_path) / RESULT_JSON
    try:
        if not p.exists() or (newer_than and p.stat().st_mtime <= newer_than):
            return []
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:
        print(f"[handsfree.bsod] result read failed: {e}")
        return []
    out = []
    for key, d in (data or {}).items():
        if not isinstance(d, dict):
            continue
        out.append({
            "dump": str(key),
            "bugcheck_code": str(d.get("bugcheck_code") or ""),
            "bugcheck_str": str(d.get("bugcheck_str") or "").split("#")[0].strip(),
            "failure_bucket_id": str(d.get("failure_bucket_id") or ""),
            "wifi_fault": bool(d.get("is_wifi_driver_fault")),
            "bt_fault": bool(d.get("is_bt_driver_fault")),
            "driver_on_stack": bool(d.get("is_driver_on_stack")),
            "driver_version": str(d.get("version") or ""),
            "driver_branch": str(d.get("drv_branch") or ""),
            "build_id": str(d.get("build_id") or ""),
            "process_name": str(d.get("process_name") or ""),
            "dump_time": str(d.get("dump_time") or ""),
            "os_version": str(d.get("os_version") or ""),
            "miniport": str(d.get("miniport_name") or ""),
            "verifier": bool(d.get("is_verifier_enabled")),
            "fw_asserts": d.get("num_of_fw_asserts"),
            "dbg_asserts": d.get("num_of_dbg_asserts"),
            "corrupted": bool(d.get("is_dump_corrupted")),
            "mini_dump": bool(d.get("is_mini_dump")),
            "jira_id": d.get("jira_id"),
            "jira_decision": str(d.get("jira_decision") or ""),
            "stack": _first_stack(d),
        })
    return out


def wait_for_results(dumps_path: str, newer_than: float, max_wait_s: float,
                     poll_s: float = 30.0, progress=None) -> list[dict]:
    """Poll the folder until the parser's JSON is (re)written or the wait
    runs out. Always checks at least once."""
    deadline = time.time() + max(0.0, float(max_wait_s))
    while True:
        res = read_results(dumps_path, newer_than)
        if res or time.time() >= deadline:
            return res
        if progress:
            progress(f"waiting for PotatoFarm results in {dumps_path} "
                     f"({int(max(0, deadline - time.time()) // 60)} min left)")
        time.sleep(min(poll_s, max(0.0, deadline - time.time())))


def assessment(results: list[dict]) -> str:
    """One deterministic sentence from the parser verdicts."""
    if not results:
        return ""
    blamed = [r for r in results if r.get("wifi_fault") or r.get("bt_fault")]
    if blamed:
        who = sorted({label for r in blamed
                      for label, flag in (("Wi-Fi driver", r.get("wifi_fault")),
                                          ("BT driver", r.get("bt_fault")))
                      if flag})
        return (f"PotatoFarm blames the Intel {' / '.join(who)} in "
                f"{len(blamed)}/{len(results)} dump(s) — driver issue to debug.")
    on_stack = [r for r in results if r.get("driver_on_stack")]
    if on_stack:
        return (f"Intel driver on the crash stack in {len(on_stack)}/{len(results)} "
                "dump(s) but not blamed by the parser — review the stacks.")
    return (f"None of the {len(results)} parsed dump(s) implicates the Intel Wi-Fi / "
            "BT driver (not on the stack, not blamed) — likely not a WLAN driver issue; "
            "share the bucket with the OEM / OS owner.")
