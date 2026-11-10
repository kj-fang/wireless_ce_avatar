"""
Beacon (handsfree replyer) routes — manual trigger + review queue.

v1 rules enforced here and in the orchestrator:
  * Posting happens ONLY via the explicit Approve endpoint.
  * A dry_run config flag disables posting entirely.
"""

from __future__ import annotations

import os
import subprocess

from flask import Blueprint, jsonify, render_template, request, session

from services.handsfree import orchestrator
from services.handsfree.queue import (HandsfreeShareUnavailable, HandsfreeStore,
                                      decoded_log_dir, decoded_log_file,
                                      describe_share_error, open_store)

handsfree_bp = Blueprint("handsfree", __name__, url_prefix="/handsfree")


def _store() -> HandsfreeStore:
    return open_store()


@handsfree_bp.errorhandler(HandsfreeShareUnavailable)
def _share_unavailable(e):
    return jsonify({"ok": False, "error": str(e)}), 503


@handsfree_bp.errorhandler(OSError)
def _file_error(e):
    # Permission or network trouble after the startup check passed.
    where = f" ({e.filename})" if getattr(e, "filename", None) else ""
    return jsonify({"ok": False,
                    "error": f"file access failed{where}: {describe_share_error(e)}"}), 503


@handsfree_bp.route("/")
def index():
    from services.handsfree import scheduler
    try:
        cfg = _store().load_config()
        share_error = ""
    except HandsfreeShareUnavailable as e:
        cfg, share_error = dict(HandsfreeStore.DEFAULT_CONFIG), str(e)
    return render_template("handsfree.html", config=cfg, share_error=share_error,
                           role=scheduler.role())


# ---------- this reviewer: the IPS sign-in of this PC ----------
# The client posts with the Salesforce session of the Windows account that
# runs it, so that session's user IS the reviewer: the queue opens on that
# user's cases and Approve posts under that name. Nothing is stored here
# beyond the resolved identity (login.py).

def _login():
    from services.handsfree import login
    return login


@handsfree_bp.route("/login", methods=["GET"])
def login_status():
    return jsonify(_login().status())


@handsfree_bp.route("/login", methods=["POST"])
def login_start():
    """Start the sign-in in the background: silent (cookie file / Windows
    account SSO) by default; {"interactive": true} opens a visible Chrome
    for the reviewer to sign in, which only makes sense on this PC."""
    body = request.get_json(silent=True) or {}
    interactive = bool(body.get("interactive"))
    if interactive and request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"ok": False, "error": "interactive sign-in: localhost only"}), 403
    return jsonify(_login().start_sign_in(interactive=interactive))


def _signed_in_name() -> str:
    return str(((_login().status().get("identity") or {}).get("name")) or "")


# ---------- run ----------

@handsfree_bp.route("/check_now", methods=["POST"])
def check_now():
    body = request.get_json(silent=True) or {}
    return jsonify(orchestrator.start_check_now(body.get("owner_name")))


@handsfree_bp.route("/run_case", methods=["POST"])
def run_case():
    """Manual trigger for one explicitly chosen IPS case number. On the
    server it runs here; on a client it becomes a request on the share that
    the server serves on its next tick (the draft then shows in the queue)."""
    from services.handsfree import scheduler
    from utils.check_ips_utils import normalise_ips
    body = request.get_json(silent=True) or {}
    if scheduler.can_schedule_here():
        return jsonify(orchestrator.start_case_run(body.get("case_nbr")))
    raw = str(body.get("case_nbr") or "").strip()
    case_nbr = normalise_ips(raw)
    if not case_nbr:
        return jsonify({"ok": False, "error": (f"{raw!r} is not an IPS case number (six to "
                                               "eight digits, e.g. 01009610)") if raw
                                              else "empty case number"})
    import socket
    who = _signed_in_name() or f"{os.environ.get('USERNAME', '?')}@{socket.gethostname()}"
    req = _store().request_case(case_nbr, requested_by=who)
    return jsonify({"ok": True, "requested": True, "request": req})


@handsfree_bp.route("/requests")
def list_requests():
    """The case requests on the share (pending / running on the server /
    finished in the last week), for the page's status line."""
    from services.handsfree import scheduler
    return jsonify({"items": _store().list_requests(), "role": scheduler.role()})


@handsfree_bp.route("/status")
def status():
    return jsonify(orchestrator.get_run_state())


# ---------- automatic analysis ----------
# The round settings and the schedule live in the shared config, so every
# PC sees them; only the SERVER role (app.py --handsfree-role server) may
# start/stop the schedule and runs its rounds (scheduler.role()). A client
# keeps the settings and "Run now" (a one-off manual round on that PC).

@handsfree_bp.record_once
def _resume_scheduler(_state):
    """Server only: after an app restart, resume a previously enabled
    schedule without waiting for someone to open the page. The thread reads
    the config on every tick, so a not-yet-booted app_config just means a
    skipped tick."""
    try:
        from services.handsfree import scheduler
        if scheduler.can_schedule_here():
            scheduler.ensure_started()
    except Exception as e:
        print(f"[handsfree] scheduler not resumed: {type(e).__name__}: {e}")


@handsfree_bp.route("/auto", methods=["GET", "POST"])
def auto_check():
    from services.handsfree import scheduler
    if request.method == "GET":
        return jsonify(scheduler.status())
    body = request.get_json(silent=True) or {}
    if "enabled" not in body:
        # Settings only (a field edited on the page, e.g. the owners file
        # path): saved and reported back, the schedule itself left alone.
        try:
            cfg = scheduler.save_settings(
                max_cases=body.get("max_cases"), source=body.get("source"),
                list_view=body.get("list_view"), list_views=body.get("list_views"),
                owners=body.get("owners"), owners_file=body.get("owners_file"),
                **_email_settings_of(body))
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        return jsonify(scheduler.status(cfg))
    if not scheduler.can_schedule_here():
        return jsonify({"ok": False, "error": scheduler.CLIENT_CANNOT_SCHEDULE}), 403
    try:
        return jsonify(scheduler.configure(
            enabled=bool(body.get("enabled")),
            mode=str(body.get("mode") or "nightly"),
            max_cases=body.get("max_cases"),
            source=body.get("source"),
            list_view=body.get("list_view"),
            list_views=body.get("list_views"),
            owners=body.get("owners"),
            owners_file=body.get("owners_file"),
            **_email_settings_of(body)))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


_EMAIL_KEYS = ("email_watch", "email_folder", "email_lookback_days", "email_max",
               "email_reply_all")


def _email_settings_of(body: dict) -> dict:
    """The Outlook round settings present in a request body (absent = keep)."""
    return {k: body[k] for k in _EMAIL_KEYS if k in body}


@handsfree_bp.route("/email/run_case", methods=["POST"])
def email_run_case():
    """Manual: the email thread of one typed case number through the pipeline."""
    body = request.get_json(silent=True) or {}
    return jsonify(orchestrator.start_email_case_run(body.get("case_nbr")))


@handsfree_bp.route("/email/check_now", methods=["POST"])
def email_check_now():
    """Scan the Outlook folder right away for mail that names an IPS case
    and analyze those cases (the settings from the page are saved first)."""
    from services.handsfree import scheduler
    body = request.get_json(silent=True) or {}
    scheduler.save_settings(**_email_settings_of(body))
    return jsonify(orchestrator.start_email_scan())


@handsfree_bp.route("/list_views")
def list_views():
    """Case list views visible to the current IPS login, for the automatic-
    analysis source dropdown. Never raises: {"items": [...]} or {"error": ...}."""
    try:
        from services.handsfree.ips_client import IpsClient
        return jsonify({"items": IpsClient().list_case_views()})
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"})


@handsfree_bp.route("/auto/run_now", methods=["POST"])
def auto_run_now():
    """One automatic round right away (same scan the schedule performs),
    with the round settings from the page (source, list view, max cases)
    saved first — the schedule itself is left alone."""
    import threading
    from services.handsfree import scheduler
    if orchestrator._run_lock.locked():
        return jsonify({"ok": False, "error": "a run is already in progress"})
    body = request.get_json(silent=True) or {}
    cfg = scheduler.save_settings(max_cases=body.get("max_cases"),
                                  source=body.get("source"),
                                  list_view=body.get("list_view"),
                                  list_views=body.get("list_views"),
                                  owners=body.get("owners"),
                                  owners_file=body.get("owners_file"),
                                  **_email_settings_of(body))
    target = scheduler.scan_target(cfg)
    if target["error"]:
        return jsonify({"ok": False, "error": target["error"]})
    # Handshake: answer only once the worker holds the run lock and the
    # status says "running" — the page polls right away and would otherwise
    # read the previous run's "done" and stop polling before this one starts.
    started = threading.Event()
    outcome: dict = {}

    def _worker():
        try:
            outcome["res"] = scheduler.run_once(trigger="manual", on_start=started.set)
        finally:
            started.set()
    threading.Thread(target=_worker, daemon=True, name="handsfree-auto-now").start()
    started.wait(timeout=15)
    res = outcome.get("res")
    if res is not None and not res.get("ok"):
        return jsonify({"ok": False, "error": res.get("error") or "run did not start"})
    return jsonify({"ok": True})


# ---------- queue ----------

@handsfree_bp.route("/queue")
def list_queue():
    """The signed-in reviewer's own cases. ?everyone=1 lifts that to every
    owner (optionally narrowed by ?owner=); without a sign-in the own-cases
    view is empty and says so."""
    truthy = ("1", "true", "yes")
    include_closed = request.args.get("all", "").lower() in truthy
    everyone = request.args.get("everyone", "").lower() in truthy
    owner_name = request.args.get("owner", "").strip()
    if not everyone:
        me = _signed_in_name()
        if not me:
            return jsonify({"items": [], "signed_out": True})
        owner_name = me
    return jsonify({"items": _store().list_drafts(
        include_closed=include_closed, owner_name=owner_name)})


@handsfree_bp.route("/queue/<draft_id>")
def get_draft(draft_id: str):
    rec = _store().get(draft_id)
    if rec is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(rec)


@handsfree_bp.route("/queue/<draft_id>", methods=["PUT", "POST"])
def save_draft(draft_id: str):
    body = request.get_json(silent=True) or {}
    plain = (body.get("draft_plain") or "").strip()
    if not plain:
        return jsonify({"ok": False, "error": "empty draft"}), 400
    from services.handsfree.composer import compose_html, AI_MARKER
    if AI_MARKER not in plain:
        plain = AI_MARKER + "\n\n" + plain
    rec = _store().update(draft_id, draft_plain=plain,
                          draft_html=compose_html(plain))
    if rec is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"ok": True, "draft": rec})


@handsfree_bp.route("/queue/<draft_id>/approve", methods=["POST"])
def approve(draft_id: str):
    """Body: draft_plain (the reviewer's text) and visibility — "internal"
    (Private to Intel; also what a missing value means) or "external"
    (visible to the customer). The page sends the reviewer's selection on
    every post; the draft's mode never decides it."""
    body = request.get_json(silent=True) or {}
    return jsonify(orchestrator.approve_and_post(
        draft_id, edited_plain=body.get("draft_plain"),
        visibility=body.get("visibility")))


@handsfree_bp.route("/queue/<draft_id>/reject", methods=["POST"])
def reject(draft_id: str):
    body = request.get_json(silent=True) or {}
    return jsonify(orchestrator.reject(draft_id, reason=body.get("reason", "")))


@handsfree_bp.route("/queue/<draft_id>/open_log_folder", methods=["POST"])
def open_log_folder(draft_id: str):
    """Open the folder of the case's decoded log in Explorer (the app runs on
    the reviewer's own PC), with the log selected — the shared copy on the
    Handsfree folder, so it works whichever machine ran the analysis. The
    path comes from the stored draft record, never from the request."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"ok": False, "error": "localhost only"}), 403
    rec = _store().get(draft_id)
    if rec is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    folder = decoded_log_dir(rec)
    if not folder:
        return jsonify({"ok": False,
                        "error": "this case has no decoded log"}), 404
    if not os.path.isdir(folder):
        return jsonify({"ok": False,
                        "error": f"folder no longer exists: {folder}"}), 404
    log_path = os.path.normpath(decoded_log_file(rec))
    if os.path.isfile(log_path):
        subprocess.Popen(["explorer", "/select,", log_path])
    else:
        subprocess.Popen(["explorer", folder])
    return jsonify({"ok": True, "folder": folder})


# ---------- per-case history + hand-off to Avatar's manual analysis ----------

def _localhost_only():
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"ok": False, "error": "localhost only"}), 403
    return None


@handsfree_bp.route("/case/<case_nbr>/reset", methods=["POST"])
def reset_case(case_nbr: str):
    """Forget the case's handsfree history (drafts archived, ledger analysis
    marks dropped, BSOD submissions forgotten) so the next run starts fresh,
    e.g. after the customer replaced a wrong log upload. Posted drafts and
    the record of what was posted are kept."""
    denied = _localhost_only()
    if denied:
        return denied
    from services.handsfree import bsod
    from utils import check_ips_utils
    canonical = check_ips_utils.normalise_ips(str(case_nbr or ""))
    if not canonical:
        return jsonify({"ok": False, "error": f"not a case number: {case_nbr}"}), 400
    body = request.get_json(silent=True) or {}
    case_dir = ""
    try:
        from configs.global_configs import app_config
        case_dir = os.path.join(app_config.avatarfiles_dir, canonical)
    except Exception:
        pass
    res = _store().reset_case(canonical, purge_downloads=bool(body.get("purge_downloads")),
                              case_dir=case_dir)
    res["bsod_forgotten"] = bsod.forget_case(canonical)
    res["ok"] = True
    return jsonify(res)


def _handoff_source(case_nbr: str, draft_id: str) -> dict:
    """What the chatbot needs, from the draft record or (while a run is in
    flight) the run state: log_path, subject, description, ... . The log is
    the shared copy when there is one: the analysis' own path is on the
    machine that ran it (the server, in server mode), which a reviewer's PC
    cannot open."""
    store = _store()
    if draft_id:
        rec = store.get(draft_id)
        if rec and str(rec.get("case_nbr") or "") == case_nbr:
            a = rec.get("analysis") or {}
            incs = a.get("incidents") or []
            sent = (incs[0].get("sent_for_analysis") if incs and isinstance(incs[0], dict)
                    else None) or {}
            return {"log_path": decoded_log_file(rec),
                    "case_id": a.get("case_id") or rec.get("case_id") or "",
                    "subject": a.get("subject") or rec.get("subject") or "",
                    "wifi_or_bt": a.get("wifi_or_bt") or "wifi",
                    "issue_type": a.get("issue_type") or "",
                    "description": a.get("clean_description") or a.get("description") or "",
                    "issue_time_log": sent.get("issue_time_log") or ""}
    live = (orchestrator.get_run_state().get("case_details") or {}).get(case_nbr) or {}
    sent = (live.get("sent_for_analysis") or [{}])[0]
    return {"log_path": live.get("log_path") or "", "case_id": live.get("case_id") or "",
            "subject": live.get("subject") or "", "wifi_or_bt": live.get("wifi_or_bt") or "wifi",
            "issue_type": live.get("issue_type") or "",
            "description": live.get("clean_description") or "",
            "issue_time_log": sent.get("issue_time_log") or ""}


# Session keys of a previous Log Chatbot / case the hand-off must not inherit.
_HANDOFF_STALE_KEYS = ("_attachment_time_cache", "_issue_ai_quick", "chatbot_session_id",
                       "feedback_conversation_id", "ai_ips_analysis", "selected_files")


def _handoff_to_log_chatbot(case_nbr: str, src: dict, log_path: str) -> dict:
    """Seed THIS browser session so Avatar's chatbot opens on the case's
    decoded log, primed with the case and the issue time (in the log's
    clock), the way its /prepare route seeds them. The radio the run
    analysed picks the chatbot: a Bluetooth case decoded an HCI log, which
    only /bt_chatbot/ (the BT agent and skills) can analyse; everything else
    goes to the Wi-Fi /log_chatbot/. Both read the same session keys."""
    from urllib.parse import urlencode
    from configs.global_configs import app_config
    from models.models import CaseContext
    from services import check_ips_service
    # 1. the chatbot's case-number gate
    check_ips_service.attach(case_nbr, check_ips_service.EXPLICIT, log_path)
    # 2. the case as the chatbot's issue context (no refetch)
    ctx = CaseContext(case_nbr=case_nbr, id=src["case_id"], subject=src["subject"],
                      description=src["description"], wifi_or_bt=src["wifi_or_bt"],
                      case_download_dir=os.path.join(app_config.avatarfiles_dir, case_nbr),
                      case_ref_source=check_ips_service.EXPLICIT)
    session["case_context"] = ctx.to_session()
    session["classification"] = {"issue_type": src["issue_type"]} if src["issue_type"] else {}
    # 3. the log + the issue time
    for key in _HANDOFF_STALE_KEYS:
        session.pop(key, None)
    session["chatbot_log_path"] = log_path
    session["_carried_issue_time_present"] = bool(src["issue_time_log"])
    session["_carried_issue_time"] = src["issue_time_log"]
    session["_carried_issue_time_warning"] = ""
    is_bt = str(src.get("wifi_or_bt") or "").strip().lower() == "bt"
    if not is_bt:
        # The process-wide "last analysed log" is the Wi-Fi chatbot's fallback
        # and what its sidebar pre-fills from; an HCI log must not land there.
        app_config.last_analyzed_log_path = log_path
    params = {"auto_run": "analyze_all"}
    if src["issue_time_log"]:
        params["issue_time"] = src["issue_time_log"]
    prefix = "/bt_chatbot/" if is_bt else "/log_chatbot/"
    return {"ok": True, "mode": "bt" if is_bt else "log", "url": prefix + "?" + urlencode(params),
            "log_path": log_path, "issue_time": src["issue_time_log"]}


NO_LOG_TO_ANALYZE = "no log to analysis"


@handsfree_bp.route("/chatbot_handoff", methods=["POST"])
def chatbot_handoff():
    """Hand the case over to Avatar's Log Chatbot for a manual deep dive, in
    THIS browser session: the chatbot opens on the case's decoded log (the
    shared copy first), primed with the case and the issue time, and the
    issue is pre-filled in the chat. A case whose run never decoded a log
    (waiting on the customer, request-info, decode failed, or the file is
    gone) answers 404 "no log to analysis" — the page shows that as the
    button's tooltip and keeps it disabled. Returns the URL to open; the page
    opens it in a new tab from the click handler."""
    denied = _localhost_only()
    if denied:
        return denied
    from utils import check_ips_utils
    body = request.get_json(silent=True) or {}
    case_nbr = check_ips_utils.normalise_ips(str(body.get("case_nbr") or ""))
    if not case_nbr:
        return jsonify({"ok": False, "error": "case number required"}), 400
    src = _handoff_source(case_nbr, str(body.get("draft_id") or ""))
    log_path = src["log_path"]
    if not log_path or not os.path.isfile(log_path):
        return jsonify({"ok": False, "error": NO_LOG_TO_ANALYZE}), 404
    try:
        res = _handoff_to_log_chatbot(case_nbr, src, log_path)
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500
    return jsonify(res), (200 if res.get("ok") else 502)


# ---------- config + REST field discovery ----------

@handsfree_bp.route("/config", methods=["GET", "POST"])
def config():
    store = _store()
    if request.method == "GET":
        return jsonify(store.load_config())
    body = request.get_json(silent=True) or {}
    allowed = {"owner_name", "post_backend", "max_cases_per_run",
               "dry_run", "rest_field_map", "ui_locators", "trial_mode",
               "email_reply_all", "email_display_draft", "auto_post_first_response",
               "latest_driver_manual"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if "latest_driver_manual" in updates:
        # Settings override of the latest released driver: empty (use the
        # report) or a full x.y.z.w, stored normalised.
        from services.handsfree.driver_release import format_version, parse_version
        raw = str(updates["latest_driver_manual"] or "").strip()
        if raw:
            v = parse_version(raw, min_parts=4)
            if not v:
                return jsonify({"ok": False,
                                "error": f"latest driver must look like 24.90.0.3 (got {raw!r})"}), 400
            updates["latest_driver_manual"] = format_version(v)
        else:
            updates["latest_driver_manual"] = ""
    return jsonify(store.save_config(updates))


@handsfree_bp.route("/latest_driver/refresh", methods=["POST"])
def latest_driver_refresh():
    """Ask the Beacon server to re-read the PAE Wi-Fi Dashboard now. Works
    from any PC: the request is a timestamp in the shared config that the
    server's next scheduler tick (<= 30 s) honours; no Chrome runs here."""
    from datetime import datetime
    from services.handsfree import scheduler
    store = _store()
    store.save_config({"latest_driver_refresh_requested_at":
                       datetime.now().astimezone().isoformat(timespec="seconds")})
    if scheduler.can_schedule_here():
        scheduler.ensure_started()
    return jsonify({"ok": True, **scheduler.status()})


@handsfree_bp.route("/discover_fields", methods=["POST"])
def discover_fields():
    try:
        return jsonify(orchestrator.discover_rest_fields())
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
