"""
Review queue + processed-case ledger for Beacon (the handsfree replyer).

Lives on the shared folder (configs.path_configs.HANDSFREE_DIR) so the
analysis server and every reviewer's Avatar see the same queue:
    config.json                    team-wide config (owner, backend, caps, schedule)
    ledger/<case_nbr>.json         {analyzed_at, posted_at, draft_id, ...} per case
    queue/<case_nbr>__<ts>.json    one draft per analyzed case
    logs/<case_nbr>/<file>.log     decoded log the analysis read, for every reviewer
    locks/<case_nbr>.lock          held while one reviewer posts to the case

Draft lifecycle:  pending_review → posted
                              ↘ rejected  ↘ post_failed

Several machines write here: every file is written whole through a uniquely
named temp file + atomic replace, and the ledger is one file per case so two
machines only collide when they touch the same case at the same moment.

Every access to the share is a network round trip (~1 s measured from a
desk PC), so this module touches it as little as possible: one store per
process, the config re-read only when it changed, and the queue list read
incrementally (only new/changed drafts, in parallel) with the summaries also
kept on this PC across restarts.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

VALID_STATUSES = {"pending_review", "approved", "posted", "rejected", "post_failed"}

_LOCK = threading.Lock()


class HandsfreeShareUnavailable(RuntimeError):
    """The shared Handsfree folder cannot be reached or written."""


def describe_share_error(e: BaseException) -> str:
    """What a reviewer can act on, instead of a raw WinError."""
    if isinstance(e, PermissionError):
        return ("access denied — your Windows account needs read AND write "
                "permission on this folder (ask the share owner)")
    if isinstance(e, FileNotFoundError):
        return "path not found — check the network / VPN connection"
    if isinstance(e, TimeoutError):
        return f"{e} — check the network / VPN connection"
    return f"{type(e).__name__}: {e}"


_root_cache: Optional[Path] = None
_store_cache: Optional["HandsfreeStore"] = None
_root_lock = threading.Lock()
_SHARE_PROBE_TIMEOUT_S = 15


def handsfree_root() -> Path:
    """The shared Handsfree folder, checked once per process for both read
    and write access. Raises HandsfreeShareUnavailable instead of falling
    back to a local folder, which would split the queue between machines.
    Only a success is cached, so a later call retries."""
    global _root_cache
    with _root_lock:
        if _root_cache is not None:
            return _root_cache
        from configs.path_configs import HANDSFREE_DIR
        outcome: list = []

        def _probe():
            try:
                root = Path(HANDSFREE_DIR)
                root.mkdir(parents=True, exist_ok=True)
                probe = root / f".probe-{uuid.uuid4().hex[:8]}.tmp"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
                outcome.append(None)
            except Exception as e:
                outcome.append(e)

        # An unreachable SMB host can block for minutes; probe in a thread.
        t = threading.Thread(target=_probe, daemon=True, name="handsfree-share-probe")
        t.start()
        t.join(_SHARE_PROBE_TIMEOUT_S)
        if not outcome or outcome[0] is not None:
            err = outcome[0] if outcome else TimeoutError(
                f"no answer in {_SHARE_PROBE_TIMEOUT_S}s")
            raise HandsfreeShareUnavailable(
                f"Handsfree shared folder unavailable: {HANDSFREE_DIR} — "
                f"{describe_share_error(err)}")
        _root_cache = Path(HANDSFREE_DIR)
        print(f"[handsfree.queue] using shared folder: {_root_cache}")
        return _root_cache


def open_store() -> "HandsfreeStore":
    """The one HandsfreeStore everyone (server, clients, CLI, UI) uses —
    created once per process, so its folder checks and caches are paid once."""
    global _store_cache
    root = handsfree_root()
    with _root_lock:
        if _store_cache is None or _store_cache.root != root:
            _store_cache = HandsfreeStore(root)
        return _store_cache


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# Bump when the slim summary written to handsfree_queue_cache.json gains or
# changes fields: an older cache is then ignored and rebuilt from the files.
_SUMMARY_VERSION = 2


_SHARE_RETRIES = 5          # a reader on another PC holds the file open for ~1 s
_SHARE_RETRY_DELAY_S = 0.2


def _atomic_write(path: Path, text: str) -> None:
    # Unique temp name: two machines replacing the same file must not share a temp.
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(text, encoding="utf-8")
    # Windows refuses to replace a file another process has open (a client's
    # page reading it over SMB: WinError 5/32). Such a reader is gone within
    # a second, so retry briefly; the temp file never stays behind.
    for attempt in range(_SHARE_RETRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == _SHARE_RETRIES - 1:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(_SHARE_RETRY_DELAY_S)


def _unlink_retry(path: Path) -> bool:
    """Delete a file another PC may be reading right now. True when it is
    gone; False when it was still open after the retries (left behind — the
    readers of request records treat such a leftover as stale)."""
    for attempt in range(_SHARE_RETRIES):
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            if attempt == _SHARE_RETRIES - 1:
                return False
            time.sleep(_SHARE_RETRY_DELAY_S)
    return False


def _ts(iso: str) -> float:
    """POSIX seconds of an ISO timestamp from a request record, 0.0 when empty / unreadable."""
    try:
        return datetime.fromisoformat(str(iso)).timestamp() if iso else 0.0
    except ValueError:
        return 0.0


def _safe_case(case_nbr: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9]", "", str(case_nbr or ""))
    if not safe:
        raise ValueError(f"invalid case number: {case_nbr!r}")
    return safe


def decoded_log_dir(rec: dict) -> str:
    """Folder holding the case's decoded log, or "" when the run never got
    past decoding. The shared copy wins: the analysis' own log_path is on the
    machine that ran it, which other reviewers cannot open."""
    log_path = decoded_log_file(rec)
    return os.path.dirname(os.path.normpath(log_path)) if log_path else ""


def decoded_log_file(rec: dict) -> str:
    return str((rec or {}).get("shared_log_path")
               or ((rec or {}).get("analysis") or {}).get("log_path") or "")


class HandsfreeStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.queue_dir = self.root / "queue"
        self.ledger_dir = self.root / "ledger"
        self.locks_dir = self.root / "locks"
        self.logs_dir = self.root / "logs"
        self.requests_dir = self.root / "requests"
        self.config_path = self.root / "config.json"
        for d in (self.queue_dir, self.ledger_dir, self.locks_dir, self.logs_dir,
                  self.requests_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._config_cache: Optional[tuple] = None    # ((mtime_ns, size), dict)
        self._summaries: Optional[dict] = None        # {file name: ((mtime_ns, size), slim)}
        self._summaries_lock = threading.Lock()

    # ------------------------------------------------------------------
    # config
    # ------------------------------------------------------------------
    DEFAULT_CONFIG = {
        "owner_name": "",
        "auto_post": False,            # future: confidence-gated auto posting
        "post_backend": "auto",        # rest | ui | auto (rest first, ui fallback)
        "max_cases_per_run": 3,
        "dry_run": False,              # analyze + queue, posting disabled
        # Opt-in: a case's first reply that asks the customer for missing
        # items (request_logs / request_info / first_response) posts without review.
        "auto_post_first_response": False,
        # Drafts (and their shared logs) older than this are removed by the
        # nightly cleanup; drafts of closed cases go regardless of age.
        "retention_days": 30,
        # Trial / tuning on historical cases: every run's drafts are flagged
        # "trial" and can never be posted, and closed cases of the automatic
        # analysis' list-view source are included instead of skipped.
        "trial_mode": False,
        # Automatic analysis source: the owner's open cases, or an IPS list
        # view (auto_source "list_view" + auto_list_view developer name).
        "auto_source": "owner",
        "auto_list_view": "Handsfree_replier_test_case",
        # Outlook intake (2026-10-22): mail naming an IPS case → the case
        # pipeline; Approve saves the reply as an Outlook draft (never sent).
        "email_watch_enabled": False,
        "email_folder": "Inbox",
        "email_lookback_days": 7,
        "email_max_per_round": 3,
        "email_reply_all": True,
        "email_display_draft": False,  # open the saved draft in Outlook too
        # Verified by a human after describe_comment_fields():
        #   {"body_field": "...", "private_field": "...", "private_value": true}
        "rest_field_map": None,
        # Latest released Wi-Fi driver (services/handsfree/driver_release.py):
        # the server reads the PAE Wi-Fi Dashboard once a day; the Settings
        # override wins when set. The drafts ask a customer on an older
        # branch to validate on this release.
        "latest_driver": "",                   # the report's pick, e.g. "24.90.0.3"
        "latest_driver_manual": "",            # Settings override (full x.y.z.w)
        "latest_driver_checked_at": "",        # last successful fetch (ISO local)
        "latest_driver_last_attempt_at": "",   # last fetch, success or not
        "latest_driver_error": "",             # why the last fetch failed
        "latest_driver_source": "",            # "pbirs report"
        "latest_driver_rows_count": 0,
        "latest_driver_row": {},               # the winning table row
        "latest_driver_report_updated": "",    # the report's "Last Updated: WW .." text
        "latest_driver_refresh_requested_at": "",  # "Refresh now" pressed on a page
    }

    def load_config(self) -> dict:
        """Defaults overlaid with config.json, re-read only when the file's
        mtime/size changed (a stat is far cheaper than a read on the share)."""
        cfg = dict(self.DEFAULT_CONFIG)
        try:
            st = os.stat(self.config_path)
        except FileNotFoundError:
            return cfg
        except OSError as e:
            print(f"[handsfree.queue] config stat failed: {e}")
            return cfg
        key = (st.st_mtime_ns, st.st_size)
        if self._config_cache is None or self._config_cache[0] != key:
            try:
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"[handsfree.queue] config read failed: {e}")
                return cfg
            self._config_cache = (key, data)
        cfg.update(copy.deepcopy(self._config_cache[1]))
        return cfg

    def save_config(self, cfg: dict) -> dict:
        # Load + merge + replace under the one lock: the scheduler thread,
        # a running scan and the /handsfree/config route all write partial
        # updates, and a merge from a stale snapshot would revert the others'
        # fields (trial_mode, dry_run, owner, source).
        with _LOCK:
            merged = {**self.load_config(), **(cfg or {})}
            _atomic_write(self.config_path, json.dumps(merged, indent=2))
            # Prime the read cache with what was just written: two writes
            # inside one mtime tick with equal sizes would otherwise look
            # unchanged to load_config and the second merge would start
            # from the first's stale snapshot.
            try:
                st = os.stat(self.config_path)
                self._config_cache = ((st.st_mtime_ns, st.st_size), copy.deepcopy(merged))
            except OSError:
                self._config_cache = None
        return merged

    # ------------------------------------------------------------------
    # ledger (one file per case)
    # ------------------------------------------------------------------
    def _ledger_file(self, case_nbr: str) -> Path:
        return self.ledger_dir / f"{_safe_case(case_nbr)}.json"

    def is_processed(self, case_nbr: str) -> bool:
        return self._ledger_file(case_nbr).exists()

    def ledger_entry(self, case_nbr: str) -> dict:
        p = self._ledger_file(case_nbr)
        try:
            if p.exists():
                return dict(json.loads(p.read_text(encoding="utf-8")) or {})
        except Exception as e:
            print(f"[handsfree.queue] ledger read failed ({case_nbr}): {e}")
        return {}

    def _update_ledger(self, case_nbr: str, **fields) -> None:
        with _LOCK:
            entry = self.ledger_entry(case_nbr)
            entry.update(fields)
            _atomic_write(self._ledger_file(case_nbr), json.dumps(entry, indent=2))

    def mark_analyzed(self, case_nbr: str, draft_id: str) -> None:
        self._update_ledger(case_nbr, analyzed_at=_now_iso(), draft_id=draft_id)

    def mark_customer_update(self, case_nbr: str, update_iso: str) -> None:
        """Remember the customer comment (IPS CreatedDate) a round was run
        for, so the auto-scan does not re-run the same update."""
        self._update_ledger(case_nbr, last_customer_update=update_iso)

    def mark_posted(self, case_nbr: str, comment_id: str = "",
                    first_response: bool = False, posted_by: str = "") -> None:
        """Record a post. The first customer-facing overview (checklist /
        request reply carrying it) is remembered separately: it is sent at
        most ONCE per case, while analyses may post once per round.
        posted_by: the IPS user the comment was posted as (the reviewer)."""
        fields = {"posted_at": _now_iso(), "comment_id": comment_id}
        if posted_by:
            fields["posted_by"] = posted_by
        if first_response and not self.first_response_posted(case_nbr):
            fields.update({"first_response_posted_at": _now_iso(),
                           "first_response_comment_id": comment_id})
        self._update_ledger(case_nbr, **fields)

    def first_response_posted(self, case_nbr: str) -> bool:
        return bool(self.ledger_entry(case_nbr).get("first_response_posted_at"))

    def mark_skipped(self, case_nbr: str, reason: str,
                     owner_name: str = "") -> None:
        self._update_ledger(case_nbr, skipped_at=_now_iso(), skip_reason=reason,
                            owner_name=owner_name)

    # ------------------------------------------------------------------
    # posting claim (cross-machine)
    # ------------------------------------------------------------------
    # Longer than any post takes (REST seconds, UI fallback ~1 min); an older
    # lock was left by a crashed post.
    CLAIM_STALE_S = 600

    def claim_case(self, case_nbr: str, holder: str) -> Optional[str]:
        """Take the case's posting claim: None when taken, else who holds it.
        O_EXCL creation is atomic on the file server, so two reviewers on
        different machines cannot post to the same case at the same time."""
        path = self.locks_dir / f"{_safe_case(case_nbr)}.lock"
        for _ in range(2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    if time.time() - path.stat().st_mtime > self.CLAIM_STALE_S:
                        path.unlink()
                        continue
                    return path.read_text(encoding="utf-8").strip() or "another reviewer"
                except FileNotFoundError:
                    continue       # released between our two calls: try again
                except OSError:
                    return "another reviewer"
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(holder)
            return None
        return "another reviewer"

    def release_case(self, case_nbr: str) -> None:
        try:
            (self.locks_dir / f"{_safe_case(case_nbr)}.lock").unlink()
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------
    # case requests: a client asks the server to analyze one case
    #   requests/<case>.pending.json  written by the client
    #   requests/<case>.running.json  the server claimed it (atomic rename)
    #   requests/<case>.done.json     finished: ok / error, kept for a week
    # A running record is only believed while it is live: a done record for
    # the case written after it started means the server could not delete
    # it (a client was reading it at that moment), and no result after
    # REQUEST_STALE_RUNNING_S means the server stopped mid-run. Either way
    # the readers drop it, so the page never shows "running" forever and the
    # case can be requested again.
    # ------------------------------------------------------------------
    REQUEST_STATES = ("pending", "running", "done")
    REQUEST_KEEP_DONE_S = 7 * 24 * 3600
    REQUEST_STALE_RUNNING_S = 2 * 3600       # the server gives a run one hour
    REQUEST_LOST_ERROR = "the server stopped while running it (no result after 2 hours)"

    def _request_file(self, case_nbr: str, state: str) -> Path:
        return self.requests_dir / f"{_safe_case(case_nbr)}.{state}.json"

    def _read_request(self, path: Path) -> Optional[dict]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _running_is_live(self, running: dict, done: Optional[dict] = None,
                         pending: Optional[dict] = None) -> bool:
        """A running record counts while no done record (the run finished)
        and no pending record (the case was asked for again) of the case was
        written after it started, and it is younger than REQUEST_STALE_RUNNING_S."""
        started = _ts(running.get("started_at"))
        if done and _ts(done.get("finished_at")) >= started:
            return False
        if pending and _ts(pending.get("requested_at")) >= started:
            return False
        return time.time() - started < self.REQUEST_STALE_RUNNING_S if started else True

    def request_case(self, case_nbr: str, requested_by: str = "") -> dict:
        """Queue one case for the server. A case really running on the
        server is not queued twice: its running record comes back instead.
        A stale running record (see above) is dropped and the case queued."""
        running_path = self._request_file(case_nbr, "running")
        running = self._read_request(running_path)
        if running:
            if self._running_is_live(running, self._read_request(self._request_file(case_nbr, "done")),
                                     self._read_request(self._request_file(case_nbr, "pending"))):
                return running
            _unlink_retry(running_path)
        req = {"case_nbr": case_nbr, "requested_by": requested_by or "",
               "requested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "status": "pending", "host": "", "started_at": "", "finished_at": "",
               "ok": None, "error": "", "draft_id": ""}
        _unlink_retry(self._request_file(case_nbr, "done"))
        _atomic_write(self._request_file(case_nbr, "pending"), json.dumps(req, indent=2))
        return req

    def claim_request(self, case_nbr: str, host: str) -> Optional[dict]:
        """pending -> running, by atomic rename: of two servers only one
        gets it (the loser's source is gone). None when it is gone (taken,
        or withdrawn), or when a stale running file is still held open by a
        reader — the next tick tries again. A stale running file (a pending
        record is newer than it) is overwritten by the rename."""
        pending, running = (self._request_file(case_nbr, "pending"),
                            self._request_file(case_nbr, "running"))
        req = self._read_request(pending)
        if not req:
            return None
        try:
            os.replace(pending, running)
        except OSError:
            return None
        req.update(status="running", host=host,
                   started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        _atomic_write(running, json.dumps(req, indent=2))
        return req

    def unclaim_request(self, case_nbr: str) -> None:
        """running -> pending again (the server could not start it now)."""
        running, pending = (self._request_file(case_nbr, "running"),
                            self._request_file(case_nbr, "pending"))
        req = self._read_request(running)
        if not req:
            return
        req.update(status="pending", host="", started_at="")
        _atomic_write(pending, json.dumps(req, indent=2))
        running.unlink(missing_ok=True)

    def finish_request(self, case_nbr: str, ok: bool, error: str = "",
                       draft_id: str = "") -> dict:
        running = self._request_file(case_nbr, "running")
        req = self._read_request(running) or {"case_nbr": case_nbr, "requested_by": "",
                                               "requested_at": "", "host": "", "started_at": ""}
        req.update(status="done", ok=bool(ok), error=error or "", draft_id=draft_id or "",
                   finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        _atomic_write(self._request_file(case_nbr, "done"), json.dumps(req, indent=2))
        if not _unlink_retry(running):
            # A client's page is reading it over the share right now. The done
            # record is written, so every reader treats this leftover as stale
            # and the next listing removes it; the result is never lost.
            print(f"[handsfree.queue] request {case_nbr}: running record still open by a "
                  "reader, left for the next listing to remove")
        return req

    def list_requests(self) -> list[dict]:
        """Every request on the share: pending and running first, then the
        finished ones of the last week, newest first within each state."""
        out: list[dict] = []
        by_case: dict[str, dict[str, dict]] = {}
        cutoff = time.time() - self.REQUEST_KEEP_DONE_S
        for path in self.requests_dir.glob("*.json"):
            parts = path.name.split(".")
            if len(parts) != 3 or parts[1] not in self.REQUEST_STATES:
                continue
            if parts[1] == "done" and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)      # a week is enough history
                continue
            req = self._read_request(path)
            if req:
                req["status"] = parts[1]
                by_case.setdefault(parts[0], {})[parts[1]] = req
        for safe, recs in by_case.items():
            running = recs.get("running")
            if running and not self._running_is_live(running, recs.get("done"), recs.get("pending")):
                # Stale (see the class notes). Finished or asked for again:
                # the leftover of a delete that failed, the done / pending
                # record already tells the story. Never finished: the server
                # stopped mid-run — record that as the result so the
                # requester sees it instead of "running".
                stale = self.requests_dir / f"{safe}.running.json"
                if not recs.get("done") and not recs.get("pending"):
                    lost = dict(running, status="done", ok=False, error=self.REQUEST_LOST_ERROR,
                                finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
                    try:
                        _atomic_write(self.requests_dir / f"{safe}.done.json", json.dumps(lost, indent=2))
                        recs["done"] = lost
                    except OSError:
                        pass
                if recs.get("done") or recs.get("pending"):
                    _unlink_retry(stale)
                    recs.pop("running")
            out.extend(recs.values())
        order = {"running": 0, "pending": 1, "done": 2}
        out.sort(key=lambda r: (order.get(r["status"], 3),
                                -(float(datetime.fromisoformat(r["requested_at"]).timestamp())
                                  if r.get("requested_at") else 0.0)))
        return out

    def pending_requests(self) -> list[dict]:
        """Oldest first: the order the server serves them."""
        pend = [r for r in self.list_requests() if r["status"] == "pending"]
        pend.sort(key=lambda r: (r.get("requested_at") or "", r.get("case_nbr") or ""))
        return pend

    # ------------------------------------------------------------------
    # queue
    # ------------------------------------------------------------------
    def enqueue(self, *, case_nbr: str, case_id: str, subject: str,
                draft_plain: str, draft_html: str,
                confidence: Optional[int], mode: str,
                analysis: dict, owner_name: str = "",
                trial: bool = False, source: str = "ips",
                shared_log_path: str = "") -> dict:
        draft_id = f"{case_nbr}__{datetime.now().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:6]}"
        record = {
            "draft_id": draft_id,
            "status": "pending_review",
            "trial": bool(trial),              # tuning run: never posted
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "case_nbr": str(case_nbr),
            "case_id": case_id,
            "owner_name": owner_name,
            "subject": subject,
            "mode": mode,                      # full | triage_only
            "source": source,                  # ips | email | ips+email (where the thread came from)
            "confidence": confidence,
            "draft_plain": draft_plain,
            "draft_html": draft_html,
            "post_result": None,
            "shared_log_path": shared_log_path,
            "analysis": analysis,              # full CaseAnalysis dump for the UI
        }
        with _LOCK:
            path = self.queue_dir / f"{draft_id}.json"
            _atomic_write(path, json.dumps(record, indent=2, ensure_ascii=False,
                                           default=str))
            # A real draft supersedes an earlier "skipped" row of the case.
            self._skipped_path(case_nbr).unlink(missing_ok=True)
        # An "error" draft means the case could not even be fetched: it is
        # not an analysis, so it never marks the case processed (the auto
        # scan must retry it; is_processed must not hide it).
        # An email-thread analysis is not an IPS analysis: the IPS ledger
        # (what the IPS auto-scan skips) stays as it was.
        if mode != "error" and source != "email":
            self.mark_analyzed(case_nbr, draft_id)
        return record

    def _skipped_path(self, case_nbr: str) -> Path:
        return self.queue_dir / f"{_safe_case(case_nbr)}__skipped.json"

    def record_skipped(self, *, case_nbr: str, case_id: str, subject: str,
                       reason: str, owner_name: str = "",
                       analysis: Optional[dict] = None) -> None:
        """Queue row for a case the server looked at but did not analyze, so
        reviewers see why. One per case (a re-run overwrites it); never
        postable, and not marked analyzed in the ledger."""
        record = {
            "draft_id": f"{_safe_case(case_nbr)}__skipped",
            "status": "skipped",
            "trial": False,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "case_nbr": str(case_nbr),
            "case_id": case_id,
            "owner_name": owner_name,
            "subject": subject,
            "mode": "unsupported",
            "confidence": None,
            "draft_plain": "",
            "draft_html": "",
            "post_result": None,
            "skip_reason": reason,
            "shared_log_path": "",
            "analysis": analysis or {},
        }
        with _LOCK:
            _atomic_write(self._skipped_path(case_nbr),
                          json.dumps(record, indent=2, ensure_ascii=False, default=str))

    def _draft_path(self, draft_id: str) -> Optional[Path]:
        # draft_id is client-supplied on approve/reject — sanitise.
        if not draft_id or "/" in draft_id or "\\" in draft_id or ".." in draft_id:
            return None
        p = self.queue_dir / f"{draft_id}.json"
        return p if p.exists() else None

    def get(self, draft_id: str) -> Optional[dict]:
        p = self._draft_path(draft_id)
        if p is None:
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[handsfree.queue] draft read failed ({draft_id}): {e}")
            return None

    def update(self, draft_id: str, **fields) -> Optional[dict]:
        with _LOCK:
            rec = self.get(draft_id)
            if rec is None:
                return None
            status = fields.get("status")
            if status is not None and status not in VALID_STATUSES:
                raise ValueError(f"invalid status: {status}")
            rec.update(fields)
            rec["updated_at"] = _now_iso()
            p = self.queue_dir / f"{draft_id}.json"
            _atomic_write(p, json.dumps(rec, indent=2, ensure_ascii=False, default=str))
            return rec

    # Ledger fields that record a POST to IPS: a reset never forgets them (a
    # first response that already went out must not be sent again).
    _LEDGER_KEEP_ON_RESET = ("posted_at", "comment_id",
                             "first_response_posted_at", "first_response_comment_id")

    def reset_case(self, case_nbr: str, *, purge_downloads: bool = False,
                   case_dir: str = "") -> dict:
        """Forget a case's analysis history so it is analyzed afresh (the
        log was wrong, the customer re-uploaded, ...). Drafts that were not
        posted move to queue_archive_reset_<stamp>/ (never deleted); posted
        drafts stay as the audit trail. The ledger keeps only the post
        records. purge_downloads removes decoded logs and extracted capture
        folders under case_dir but keeps the downloaded archives."""
        case_nbr = str(case_nbr)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        archive = self.root / f"queue_archive_reset_{stamp}"
        archived: list[str] = []
        kept: list[str] = []
        with _LOCK:
            for p in sorted(self.queue_dir.glob(f"{case_nbr}__*.json")):
                try:
                    rec = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    rec = {}
                if str(rec.get("case_nbr") or "") not in ("", case_nbr):
                    continue
                if rec.get("status") == "posted":
                    kept.append(p.stem)
                    continue
                archive.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), str(archive / p.name))
                archived.append(p.stem)
            # One ledger file per case (shared folder): keep only the post
            # records, drop the file when nothing is left to keep.
            entry = self.ledger_entry(case_nbr)
            remaining = {k: v for k, v in entry.items() if k in self._LEDGER_KEEP_ON_RESET}
            if remaining:
                _atomic_write(self._ledger_file(case_nbr), json.dumps(remaining, indent=2))
            else:
                self._ledger_file(case_nbr).unlink(missing_ok=True)
        purged: list[str] = []
        if purge_downloads and case_dir and os.path.isdir(case_dir):
            for dirpath, dirnames, filenames in os.walk(case_dir):
                for name in filenames:
                    low = name.lower()
                    if low.endswith(".log") or ".etl" in low:
                        full = os.path.join(dirpath, name)
                        try:
                            os.remove(full)
                            purged.append(full)
                        except OSError:
                            pass
        return {"case_nbr": case_nbr, "archived": archived, "kept_posted": kept,
                "archive_dir": str(archive) if archived else "",
                "ledger_kept": remaining, "purged": purged}

    def list_drafts(self, include_closed: bool = True,
                    limit: int = 100, owner_name: str = "") -> list[dict]:
        """Newest-first draft summaries (analysis payload omitted).
        Raises OSError when the queue folder cannot be listed \u2014 an empty
        list would read as "no drafts" and hide the real problem."""
        owner_filter = (owner_name or "").strip().casefold()
        out: list[dict] = []
        for slim in self._refresh_summaries():
            if owner_filter and str(slim.get("owner_name") or "").casefold() != owner_filter:
                continue
            if not include_closed and slim.get("status") in ("posted", "rejected"):
                continue
            out.append(dict(slim))
            if len(out) >= limit:
                break
        return out

    # ---- summary cache behind list_drafts -------------------------------
    _READ_THREADS = 16      # the share is latency-bound: parallel reads ~4x faster

    def _refresh_summaries(self) -> list[dict]:
        """All drafts' summaries, newest first. One directory listing per call;
        only files whose (mtime, size) changed are read, in parallel."""
        with self._summaries_lock:
            if self._summaries is None:
                self._summaries = self._load_local_summaries()
            entries = [e for e in os.scandir(self.queue_dir)
                       if e.name.endswith(".json") and not e.name.startswith(".")]
            keys = {e.name: (e.stat().st_mtime_ns, e.stat().st_size) for e in entries}
            stale = [e for e in entries
                     if (self._summaries.get(e.name) or (None,))[0] != keys[e.name]]
            if stale:
                with ThreadPoolExecutor(self._READ_THREADS) as ex:
                    for name, slim in ex.map(self._read_summary, stale):
                        if slim is not None:
                            self._summaries[name] = (keys[name], slim)
            gone = set(self._summaries) - set(keys)
            for name in gone:
                del self._summaries[name]
            if stale or gone:
                self._save_local_summaries()
            names = sorted((n for n in keys if n in self._summaries),
                           key=lambda n: keys[n][0], reverse=True)
            return [self._summaries[n][1] for n in names]

    def _read_summary(self, entry) -> tuple:
        try:
            rec = json.loads(Path(entry.path).read_text(encoding="utf-8"))
        except Exception:
            return entry.name, None       # mid-write or corrupt: retried next refresh
        slim = {k: rec.get(k) for k in
                ("draft_id", "status", "created_at", "updated_at",
                 "case_nbr", "case_id", "owner_name", "subject", "mode", "confidence",
                 "post_result", "skip_reason", "auto_posted", "source")}
        slim["trial"] = bool(rec.get("trial"))
        slim["log_dir"] = decoded_log_dir(rec)
        analysis = rec.get("analysis") or {}
        slim["log_path"] = str(analysis.get("log_path") or "") if slim["log_dir"] else ""
        slim["action_owner"] = str(analysis.get("action_owner") or "")
        slim["next_action"] = str(analysis.get("next_action") or "")
        # Where an email-intake draft came from (the page shows the sender).
        email = rec.get("email") or {}
        slim["email"] = ({k: email.get(k, "") for k in
                          ("sender_name", "sender_email", "subject", "received", "thread_size")}
                         if email else None)
        return entry.name, slim

    def _local_summaries_path(self) -> Optional[Path]:
        try:
            from configs.global_configs import app_config
            base = getattr(app_config, "avatarfiles_dir", None)
        except Exception:
            base = None
        return Path(base) / "handsfree_queue_cache.json" if base else None

    def _load_local_summaries(self) -> dict:
        p = self._local_summaries_path()
        try:
            if p and p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                if (data.get("root") == str(self.root)
                        and data.get("version") == _SUMMARY_VERSION):
                    return {n: (tuple(v[0]), v[1]) for n, v in data["items"].items()}
        except Exception as e:
            print(f"[handsfree.queue] local queue cache ignored: {e}")
        return {}

    def _save_local_summaries(self) -> None:
        p = self._local_summaries_path()
        if not p:
            return
        try:
            _atomic_write(p, json.dumps({"root": str(self.root), "version": _SUMMARY_VERSION, "items": {
                n: [list(k), s] for n, (k, s) in self._summaries.items()}}))
        except Exception as e:
            print(f"[handsfree.queue] local queue cache not saved: {e}")

    def all_drafts(self) -> list[dict]:
        """Every draft record (full), unordered — for housekeeping."""
        out: list[dict] = []
        for p in self.queue_dir.glob("*.json"):
            if p.name.startswith("."):
                continue
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                continue
        return out

    def delete_draft(self, draft_id: str) -> None:
        p = self._draft_path(draft_id)
        if p is not None:
            p.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # shared decoded logs
    # ------------------------------------------------------------------
    def share_log(self, case_nbr: str, local_path: str) -> str:
        """Copy the decoded log the analysis read into logs/<case>/ so every
        reviewer can open it; returns the shared path. A copy of the same size
        already there is kept (re-runs of the case do not copy again)."""
        src = Path(local_path)
        dest_dir = self.logs_dir / _safe_case(case_nbr)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        if not (dest.exists() and dest.stat().st_size == src.stat().st_size):
            tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex[:8]}.tmp")
            shutil.copyfile(src, tmp)
            os.replace(tmp, dest)
        return str(dest)

    def case_log_dirs(self) -> list[Path]:
        return [d for d in self.logs_dir.iterdir() if d.is_dir()]

    def delete_case_logs(self, case_nbr: str) -> None:
        shutil.rmtree(self.logs_dir / _safe_case(case_nbr), ignore_errors=True)
