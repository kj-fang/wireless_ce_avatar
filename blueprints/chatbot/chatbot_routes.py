"""One route module for both full log-analysis agents: Wi-Fi and Bluetooth.

Until now ``blueprints/log_chatbot/log_chatbot_routes.py`` and
``blueprints/bt_chatbot/bt_chatbot_routes.py`` were two 1,500-line files whose
handlers were the same code twice. Of ~1,500 lines each, about 1,080 were
identical; what genuinely differed was a set of values (which ``app_config``
slot holds the app-level agent, which history domain key partitions the saved
conversations, which agent class to instantiate) plus nine behaviour
switches: three the UI config already declared, and six explicit fields on
:class:`AgentRouteProfile`.

This module is the same move the refactor already made for the engine and for
``services/chatbot/shared_routes.py``, applied one layer out:

    the handler bodies are written ONCE, and a profile supplies the data.

:class:`AgentRouteProfile` is that data. :class:`AgentRoutes` builds one
profile's handlers as methods and hands ``handler_map()`` the adapter map it
validates, so a missing handler still fails at import time rather than
404-ing in production.

Reading the differences from ``configs/chatbot_ui.py``
-----------------------------------------------------
Three of the behaviour differences were ALREADY declared as data — the UI
config has driven the frontend with them all along:

    ``ui["issue_time"]["customer_timezone"]``  Wi-Fi logs carry a customer
        timezone that has to be reconciled with the decoder host's clock.
    ``ui["issue_time"]["allow_time_only"]``    Wi-Fi accepts logs with no dates
        (DDD / tracefmt), so a bare ``HH:MM:SS`` is a valid issue time.
    ``ui["issue_time"]["event_refinement"]``   BT anchors AI issue-time
        suggestions on System Event Log Warning/Error rows.

The route layer now reads the same three flags instead of hard-coding the same
decisions a second time, so the page and the endpoints serving it cannot drift
apart. The rest — the values and the remaining behaviour differences — are
fields on :class:`AgentRouteProfile`.

What is NOT here
----------------
``find_best_log`` moved to ``blueprints/log_parser/log_parser_routes.py``. It
picks a capture out of a list of ETL paths, which is log selection, not chatbot
behaviour: it never touches an agent, a conversation or a skill, and the only
caller is download_result's BT auto-pick. It lived in three chatbot route files
in three different states of repair.
"""

from __future__ import annotations

import json
import os
import re
import threading
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime
from types import ModuleType
from typing import Any, Callable, Sequence

from flask import (
    Response,
    copy_current_request_context,
    jsonify,
    render_template,
    request,
    session,
)

from configs.chatbot_ui import BT_UI, LOG_CHATBOT_UI
from configs.global_configs import app_config
from services import feedback_service
from services import check_ips_service
from services import gather_service
from services import history_service
from services.chatbot import job_runtime as chat_jobs
from services.chatbot.engine.bluetooth import BtLogAgentSystem
from services.chatbot.engine.system import WifiLogAgentSystem, load_skills_from_yaml
from services.chatbot.factory import (
    ChatbotBlueprintConfig,
    create_chatbot_blueprint,
    handler_map,
)
from services.chatbot.issue_context import (
    compose_concise_description as _compose_concise_description,
    extract_issue_context as _extract_issue_context,
    organized_issue_context as _organized_issue_context,
    resolved_issue_time_for as _resolved_issue_time_for,
)
from services.chatbot.job_runtime import job_sse as _job_sse
from services.chatbot.session import (
    ensure_feedback_conversation_id as _shared_feedback_conversation_id,
    register_session_agent_store,
    resume_agent_for as _shared_resume_agent_for,
)
from services.chatbot.shared_routes import (
    SharedRouteContext,
    build_shared_handlers,
    llm_client_model as _llm_client_model,
)
from services.skill_editor.controller import (
    SkillEditorContext,
    build_profile_yaml_helpers,
    build_skill_editor_handlers,
)
from services.skill_editor.yaml_service import (
    read_yaml_file as _read_yaml_file,
    sanitise_skill_payload as _sanitise_skill_payload,
    write_yaml_file as _write_yaml_file,
)
from utils import bt_skills_yaml_utils as _bt_yaml_utils
from utils import skills_yaml_utils as _wifi_yaml_utils
from utils.event_log_utils import find_event_log_for_log
from utils.issue_time_ai import (
    build_issue_time_suggestions,
    find_nearest_event_error,
    realign_times_to_log,
)
from utils.issue_time_utils import (
    format_issue_time,
    parse_issue_time_string,
    read_log_time_range,
    validate_issue_time_in_log_range,
)
from utils.timezone_utils import (
    get_effective_timezone,
    taiwan_to_local,
    format_tz_label,
    to_iana_timezone,
)


# Upper bound on System-Event-Log rows pulled to anchor an AI issue-time
# suggestion. build_event_log_digest keeps at most 40 rows (by severity) and
# find_nearest_event_error only needs a representative pool, so this cap keeps
# /suggest_issue_times fast and bounded even on very large .evtx captures.
_EVENT_ANCHOR_MAX = 500


# ==================================================================
# The profile: everything that differs between the two agents
# ==================================================================
@dataclass(frozen=True)
class AgentRouteProfile:
    """One full log-analysis agent's route-layer identity and policy.

    Frozen on purpose, like ``AgentCapabilityPolicy`` in the engine: a profile
    is read at import time to build a blueprint and is never mutated after.
    """

    # ---- identity -------------------------------------------------
    #: Flask blueprint name. Also the ``url_for()`` prefix, so it is part of
    #: the public contract — templates say ``url_for("bt_chatbot.index")``.
    name: str
    #: This profile's block in ``configs/chatbot_ui.py``. Supplies the page
    #: data AND the three issue-time flags documented in the module docstring.
    ui: dict
    #: Agent class instantiated for a session with no app-level agent to clone.
    agent_class: type
    #: ``app_config`` attribute holding the app-level agent built at startup.
    agent_config_attr: str
    #: Which ``utils.*_skills_yaml_utils`` module owns this profile's dated
    #: YAML files. Both expose the same 12 names; BT's differ in filename
    #: pattern and share-folder location.
    yaml_utils: ModuleType
    #: Dated user-override filename prefix (``skills_2026-04-08.yaml``).
    user_yaml_prefix: str
    #: ``filetypes`` for the native open dialog.
    browse_filetypes: Sequence[tuple[str, str]]
    #: Gather's analytics label. NOT the history domain below: history uses ""
    #: for Wi-Fi to preserve the on-disk layout, while analytics rows say
    #: "wifi". Same trap ``SharedRouteContext.gather_domain`` documents.
    gather_domain: str

    # ---- behaviour differences (each one a real divergence) ------
    #: Server-side hard cap on ``issue_time_window_minutes``. The frontend
    #: already caps the slider at the log's own span, so this only bounds a
    #: bypassed / buggy client. Wi-Fi chose 24 h; BT left it effectively open.
    #: TODO(team): one value would do — 1440 is above any real single event.
    window_minutes_cap: int
    #: ``/prepare`` drops the DERIVED issue-context caches (attachment time,
    #: resolved issue time, LLM-organized description) before rebuilding
    #: them, so a second analysis started without "Back to Avatar" cannot
    #: inherit the previous run's. Wi-Fi only.
    #: TODO(team): a BT /prepare is a new analysis too. #165's
    #: check_ips_service already drops these caches whenever the case
    #: number changes, which covers most of BT's exposure; a second BT run
    #: on the SAME case is the part still open. Unverified, so unchanged.
    purge_issue_caches_on_prepare: bool
    #: ``/prepare`` resets the conversation before priming. Wi-Fi only.
    #: TODO(team): BT reaching /prepare is also a new analysis, so this looks
    #: like drift rather than policy — confirm before unifying.
    reset_conversation_on_prepare: bool
    #: ``/prepare`` accepts a path that is ALREADY a log file (``.log`` /
    #: ``.txt``, including ``.hci.txt``) instead of appending ``.log``. BT's
    #: HCI decode writes ``<etl>.hci.txt`` directly; Wi-Fi's wpp_ddd flow
    #: always produces ``<etl>.log``.
    accepts_direct_log_path: bool
    #: ``/history/load`` reads ``_log_has_date()`` even while the conversation's
    #: background analysis is still running. False (BT) skips it, because the
    #: analysis thread mutates the ``_raw_log_cache`` pair that call reads.
    #: TODO(team): the guard is right for both — Wi-Fi just never got it.
    read_log_has_date_while_running: bool
    #: Pre-warm the issue-AI cache during the page render so the page's
    #: /get_issue_context AJAX hits a hot cache instead of spinning on a
    #: synchronous LLM organize. BT only.
    prewarm_issue_ai_on_index: bool

    # ---- derived: read the flags the UI config already declares ----
    @property
    def url_prefix(self) -> str:
        return self.ui["api"]

    @property
    def history_domain(self) -> str:
        """History/job partition key. "" = Wi-Fi (legacy layout), "bt" = BT."""
        return self.ui["feedback_domain"]

    @property
    def customer_timezone(self) -> bool:
        """Reconcile a customer wall clock against the decoder host's clock."""
        return bool(self.ui["issue_time"]["customer_timezone"])

    @property
    def allow_time_only(self) -> bool:
        """Accept logs with no dates (DDD / tracefmt), so ``HH:MM:SS`` is valid."""
        return bool(self.ui["issue_time"]["allow_time_only"])

    @property
    def event_log_anchor(self) -> bool:
        """Anchor AI issue-time suggestions on System Event Log Warn/Err rows."""
        return bool(self.ui["issue_time"]["event_refinement"])

    @property
    def capabilities(self) -> set[str]:
        return {key for key, on in self.ui["features"].items() if on}


# ==================================================================
# The profile-specific algorithms, as named functions
# ==================================================================
# Each one is used by exactly one profile today, and none of them branches on
# a profile: they take what they need as arguments. That keeps the handler
# bodies below readable as flow, with the profile's extra work named at the
# point where it happens.


def _time_only_log_last_time(agent) -> str:
    """Last ``HH:MM:SS`` in a DATELESS log, scanning the raw cache backwards.

    ``read_log_time_range``'s regex wants ``MM/DD/YYYY-HH:MM:SS.fff``, so on a
    DDD / tracefmt upload it finds nothing and the sidebar's "Use log's last
    time" button silently does nothing. This is the fallback for those.
    """
    # Force the raw-log cache to load — /set_log otherwise defers it until the
    # first analysis, and this would find [] and no-op.
    try:
        agent._ensure_raw_log_cache()
    except Exception as _e:
        print(f"⚠️  _ensure_raw_log_cache failed for time-only log: {_e}")
    cache = getattr(agent, "_raw_log_cache", None) or []
    _time_re = re.compile(
        r'(?<!\d)(\d{1,2}):(\d{2}):(\d{2})(?:[:.](\d{1,6}))?(?!\d)'
    )
    for _line in reversed(cache):
        _m = _time_re.search(_line or "")
        if not _m:
            continue
        _hh, _mm, _ss = (int(_m.group(i)) for i in (1, 2, 3))
        if not (0 <= _hh <= 23 and 0 <= _mm <= 59 and 0 <= _ss <= 59):
            continue
        _raw_ms = _m.group(4)
        if _raw_ms:
            _ms = int(_raw_ms.ljust(6, "0")[:6]) // 1000
            return f"{_hh:02d}:{_mm:02d}:{_ss:02d}.{_ms:03d}"
        return f"{_hh:02d}:{_mm:02d}:{_ss:02d}"
    return ""


def _event_log_anchor_events(log_path: str, source_filter: str, level_filter: str) -> list:
    """System Event Log Warning/Error rows for the loaded capture.

    On a huge log the rough raw-log browse alone is imprecise, so these
    pre-filtered fault entries strongly anchor the issue time. Best-effort —
    any failure just omits the section from the prompt.
    """
    try:
        evtx_path = find_event_log_for_log(log_path) if log_path else ""
        if not evtx_path:
            return []
        from services import event_log_service
        # Bound the pull so a huge .evtx can't balloon latency/memory:
        # build_event_log_digest keeps at most 40 rows (picked by severity) and
        # find_nearest_event_error only needs a representative pool, so a
        # generous cap is plenty while staying safe on very large captures.
        page = event_log_service.get_paged_events(
            evtx_path, offset=0, limit=_EVENT_ANCHOR_MAX,
            source_filter=source_filter, level_filter=level_filter,
        )
        return page.get("events", []) if isinstance(page, dict) else []
    except Exception as _evt_err:
        print(f"⚠️ issue-time event-log anchor skipped: {_evt_err}")
        return []


def _attach_nearest_errors(payload: dict, event_log_events: list) -> None:
    """Link the sidebar refine picker to the SAME events the AI used.

    Attaches the nearest Error/Critical event to each AI suggestion so the
    frontend can drive "Found a nearby system error" WITHOUT a second
    /parse_event_log fetch. Skipped for user-explicit times and undated
    (time-only) suggestions, where a date-based distance is meaningless.
    """
    if not event_log_events or not isinstance(payload, dict):
        return
    if payload.get("user_explicit"):
        return
    for s in payload.get("suggestions", []) or []:
        try:
            sdt, _ = parse_issue_time_string((s.get("issue_time") or "").strip())
            if sdt and sdt.year >= 2000:
                ne = find_nearest_event_error(event_log_events, sdt)
                if ne:
                    s["nearest_error"] = ne
        except Exception:
            pass


def _sync_customer_issue_time(agent, parsed, is_time_only: bool) -> None:
    """Keep the customer-tz annotation in sync with a new sidebar value.

    The picker always shows the log-frame value (it matches .log content), so
    the same instant on the customer's wall clock is just ``taiwan_to_local``
    at the detected tz. Skipped on time-only values or when no tz is known.
    """
    if not (parsed and not is_time_only and agent.current_log_path):
        return
    try:
        log_tz = get_effective_timezone(agent.current_log_path) or ""
        if log_tz:
            agent.issue_time_tz = log_tz
            agent.issue_time_customer = taiwan_to_local(parsed, log_tz)
        else:
            agent.issue_time_tz = ""
            agent.issue_time_customer = None
    except Exception as _e:
        print(f"[chat] sidebar issue_time customer refresh skipped ({_e})")


def _validate_carried_issue_time(carried_issue_time: str, log_path: str) -> None:
    """Range-check the issue time download_result carried over, into session.

    ``download_result`` has already resolved a time-only case timestamp against
    the selected capture folder. Carry that exact value forward; asking the LLM
    to choose a date again is both redundant and unstable when a log spans
    midnight. The selected log is available here, so this is also the right
    place to reject an impossible / out-of-range value.
    """
    session["_carried_issue_time_present"] = True
    _carried_dt, _range_first, _range_last, _range_error = (
        validate_issue_time_in_log_range(carried_issue_time, log_path)
    )
    if _carried_dt is not None:
        session["_carried_issue_time"] = format_issue_time(_carried_dt)
        session["_carried_issue_time_warning"] = ""
        return
    session["_carried_issue_time"] = ""
    if _range_first and _range_last:
        _range_text = (
            f"{format_issue_time(_range_first)} to "
            f"{format_issue_time(_range_last)}"
        )
        session["_carried_issue_time_warning"] = (
            f"Auto-detected issue time {carried_issue_time} was not used: "
            f"{_range_error} Log range: {_range_text}. Please confirm the issue time."
        )
    else:
        session["_carried_issue_time_warning"] = (
            f"Auto-detected issue time {carried_issue_time} was not used: "
            f"{_range_error} Please confirm the issue time."
        )


def _invalidate_issue_context_caches() -> None:
    """Drop the DERIVED issue-context caches so they get recomputed from the
    current ``selected_files`` / ``case_context``.

    These caches are computed FROM the raw case sources but live independently
    in the session, so they outlive the data they were derived from. Without
    this, starting a SECOND analysis (download_result -> /prepare ->
    /log_chatbot/?auto_run=analyze_all) without first clicking "Back to Avatar"
    makes the new run inherit the PREVIOUS run's attachment time, resolved
    issue time and LLM-organized description. Call this whenever a new analysis
    is entered so the caches are rebuilt from the fresh case data.
    """
    for key in (
        "_attachment_time_cache",      # parsed attachment subtitle time
        "_resolved_issue_time_cache",  # log_path -> resolved issue_time
        "_issue_ai_quick",             # LLM-organized description + issue times
    ):
        session.pop(key, None)


def _clear_carried_issue_time() -> None:
    """Forget the previous run's download_result hand-off.

    A run that carries no issue time must not inherit the last run's, and a
    run that carries one re-validates it from scratch. Kept apart from the
    derived-cache purge above, the same split check_ips_service makes: these
    keys are a time the user picked, not something derived from the case.
    """
    for key in (
        "_carried_issue_time",          # download_result -> chatbot hand-off
        "_carried_issue_time_warning",  # failed hand-off range validation
        "_carried_issue_time_present",  # blocks a second date guess on failure
    ):
        session.pop(key, None)


def _align_times_to_log_frame(log_path, first_ts, last_ts, attachment_time,
                              issue_time_str, issue_times):
    """Shift every surfaced time into the LOG frame, and note the customer one.

    The picker drives PreScan / Segment-2 against the raw .log content, which
    the decoder writes in the log host's clock. Source values are usually
    log-frame strings already, but a customer-typed description ("at 12:26 PM
    CST") lands in customer frame and needs shifting back.
    ``determine_issue_time_frames`` picks which interpretation applies per
    string and returns both frames, so we can also surface a customer-tz
    annotation for the UI.

    Returns ``(attachment_time, issue_time_str, issue_times, customer_tz,
    customer_annotations)``.
    """
    customer_annotations: dict = {}
    customer_tz_for_ui = ""
    if not log_path:
        return (attachment_time, issue_time_str, issue_times,
                customer_tz_for_ui, customer_annotations)
    try:
        from utils.issue_time_ai import determine_issue_time_frames

        def _to_log_frame(s: str) -> str:
            nonlocal customer_tz_for_ui
            if not isinstance(s, str) or not s:
                return s
            parsed, is_time_only = parse_issue_time_string(s)
            if not parsed or is_time_only:
                return s
            # Pass the log content range (GMT+8 engineer frame) as the second
            # anchor so an ATTACH/issue time mistakenly entered in our engineer
            # clock — rather than the customer's packed time — is detected and
            # shifted back to the customer frame.
            frames = determine_issue_time_frames(
                parsed, [log_path],
                log_first_ts=first_ts, log_last_ts=last_ts,
            )
            if frames.get("customer_tz") and not customer_tz_for_ui:
                customer_tz_for_ui = frames["customer_tz"]
            log_dt = frames.get("log_frame") or parsed
            cust_dt = frames.get("customer_frame")
            log_str = format_issue_time(log_dt) if log_dt != parsed else s
            if cust_dt and frames.get("customer_tz"):
                customer_annotations[log_str] = format_issue_time(cust_dt)
            return log_str

        attachment_time = _to_log_frame(attachment_time)
        issue_time_str = _to_log_frame(issue_time_str)
        issue_times = [_to_log_frame(s) for s in issue_times]
    except Exception as e:
        print(f"[get_issue_context] issue-time frame detect skipped ({e})")
    return (attachment_time, issue_time_str, issue_times,
            customer_tz_for_ui, customer_annotations)


def _guard_times_inside_log_range(first_ts, last_ts, attachment_time,
                                  issue_time_str, issue_times, carried_present):
    """Final safety net for every auto-filled source, not only the hand-off.

    An LLM can choose the wrong date when a log spans midnight; never place
    such a value in the picker unless it actually falls inside the selected
    log. Manual entry stays available so the user can correct the time or
    deliberately choose another log.

    Returns ``(attachment_time, issue_time_str, issue_times, blocked, warning)``.
    """
    if not (first_ts and last_ts):
        return attachment_time, issue_time_str, issue_times, False, ""

    def _is_inside_log_range(s: str) -> bool:
        parsed, is_time_only = parse_issue_time_string(s)
        return bool(parsed and not is_time_only and first_ts <= parsed <= last_ts)

    had_auto_candidate = bool(issue_times or attachment_time or issue_time_str)
    issue_times = [s for s in issue_times if _is_inside_log_range(s)]
    attachment_time = attachment_time if _is_inside_log_range(attachment_time) else ""
    if issue_times:
        issue_time_str = issue_times[0]
    elif attachment_time:
        issue_time_str = attachment_time
    elif _is_inside_log_range(issue_time_str):
        # Deterministic log-latest fallback is already safe.
        pass
    elif carried_present or had_auto_candidate:
        return attachment_time, "", issue_times, True, (
            "The auto-detected issue time was not used because it is outside "
            f"the selected log range ({format_issue_time(first_ts)} to "
            f"{format_issue_time(last_ts)}). Please confirm the issue time."
        )
    return attachment_time, issue_time_str, issue_times, False, ""


# ==================================================================
# The handlers, written once
# ==================================================================
class AgentRoutes:
    """One profile's complete route layer.

    Every handler is written once, as a method, and reads its profile's data
    from ``self.profile`` — the two route files this module replaces satisfied
    the same contract with two copies of this code.

    ``handlers`` is what ``handler_map()`` validates against the profile's
    enabled capabilities. ``get_agent`` is the per-session accessor those
    handlers use, which the factory's own three use cases (/reset, /skills,
    /browse_yaml) also need.

    Methods rather than closures, so a handler can be imported, patched and
    named in a stack trace like any other method, and a test can reach the
    session store as ``routes.session_agents`` instead of digging through
    ``__closure__``.
    """

    def __init__(self, profile: AgentRouteProfile):
        self.profile = profile
        # Per-profile server-side store: session_id -> agent instance. One dict
        # per profile, NOT one shared dict: both profiles key it by the same
        # Flask session id, so sharing it would hand the BT page the Wi-Fi agent.
        self.session_agents: dict = {}
        register_session_agent_store(profile.name, self.session_agents)
        self.get_agent = self._get_or_create_agent
        self.handlers = self._build_handlers()

    # ------------------------------------------------------------------
    # Feedback sidecar helpers (anonymous, side-car, never blocks chat)
    # ------------------------------------------------------------------
    def _ensure_feedback_conversation_id(self, *, rotate: bool = False) -> str:
        """
        Return the current feedback conversation_id, creating one if missing
        or if `rotate=True` (e.g. on set_log / prepare — a new log = new case).
        Stored in Flask session so it persists across requests.
        """
        return _shared_feedback_conversation_id(rotate=rotate)

    def _get_or_create_agent(self, skip_prime: bool = False):
        """
        Return a per-session agent for this profile.
        Borrows client/model from the app-level agent in
        ``app_config.<agent_config_attr>``, initialised at app startup
        (set_up_app.py -> set_up()).

        skip_prime: if True, skip the auto prime_with_context on new session creation.
                    Use this when the caller will immediately call prime_with_context itself.
        """
        sid = session.get("chatbot_session_id")
        if not sid or sid not in self.session_agents:
            sid = str(uuid.uuid4())
            session["chatbot_session_id"] = sid

        if sid not in self.session_agents:
            base = getattr(app_config, self.profile.agent_config_attr, None)
            if base is None:
                # Fallback: try to build from llm_helper directly
                llm_helper = app_config.llm_helper
                if llm_helper is None or llm_helper.client is None:
                    raise RuntimeError(
                        "Log Chatbot Agent is not available. "
                        "The app may not have an API key configured."
                    )
                base = self.profile.agent_class(
                    client=llm_helper.client,
                    model=getattr(llm_helper, "model", "gpt-4.1"),
                    skills=getattr(llm_helper, "skills", None),
                )
            # Create a fresh per-session instance sharing the same client +
            # skills. type(base) rather than profile.agent_class so a subclass's
            # own overrides survive the clone — that is how BtLogAgentSystem
            # keeps SCOPE_FULL_LOG_WHEN_EMPTY and its empty DRIVER_ADD_MARKER /
            # RESET_MARKER.
            agent = type(base)(
                client=base.client,
                model=base.model,
                skills=base.skills,   # reuse pre-loaded skills, no disk re-read
            )
            # Inherit ACE runner from the boot-time base agent so playbook
            # blocks are injected into per-session prompts. Each profile has its
            # own sync namespace — see configs/set_up_app.py.
            ace_runner = getattr(base, "ace_runner", None)
            if ace_runner is not None:
                agent.attach_ace(ace_runner)
            # Auto-populate the log path so a freshly-(re)created per-session
            # agent still knows which log to use. Two sources, in order:
            #   1. session["chatbot_log_path"] — set by set_log when the user
            #      loads a log DIRECTLY. For BT that is the only record of it:
            #      set_log does NOT touch app_config.last_analyzed_log_path.
            #   2. app_config.last_analyzed_log_path — the LogParser -> chatbot
            #      hand-off path, and what the sidebar pre-fills from.
            # Reading the session key first is what lets the agent survive an
            # in-memory wipe of session_agents (Flask debug auto-reload, worker
            # restart) and a browser-back / bfcache re-run where prepare() and
            # set_log() don't re-execute: without it, a directly-loaded log
            # produced "No log file loaded" on the next /chat even though the
            # user had already set it. The session value wins so a per-session
            # log can't be clobbered by another tab or case that moved the
            # process-global on.
            restored_log = (session.get("chatbot_log_path")
                            or app_config.last_analyzed_log_path or "")
            if restored_log:
                agent.current_log_path = restored_log

            # Prime with session issue context so every new session is context-aware
            # (skipped when caller will immediately call prime_with_context itself)
            if not skip_prime:
                try:
                    ctx = _extract_issue_context()
                    if any(ctx.values()):
                        agent.prime_with_context(**ctx)
                except Exception:
                    pass  # session may not have case context (standalone chatbot)
            self.session_agents[sid] = agent

        return self.session_agents[sid]

    def _export_agent_context(self, agent) -> list:
        """Snapshot the agent's model-facing conversation. Never raises.

        Persisting the context is a convenience — without it a conversation
        still resumes, just from result text. The caller runs inside the turn's
        worker try/except, where an exception would mark an already-successful
        turn as failed, so this swallows its own errors rather than costing the
        user a finished analysis.
        """
        try:
            return agent.export_conversation_context()
        except Exception as e:
            print(f"[history] context export failed: {e}")
            return []

    def _resume_agent_for(self, conversation_id: str):
        """
        Return the agent to use for ``conversation_id``.

        A finished background analysis keeps its own (detached) agent, which
        holds the full tool-grounded conversation history. When the user
        continues that same conversation we adopt that agent — far higher
        fidelity than rebuilding context from saved text. Running jobs are NOT
        adopted (their agent is busy on a background thread); the caller falls
        back to a fresh session agent.
        """
        return _shared_resume_agent_for(
            conversation_id,
            self.session_agents,
            self._get_or_create_agent,
        )

    def _get_llm_client_model(self):
        return _llm_client_model(self.profile.agent_config_attr)

    def _issue_context_organized(self, raw_desc: str, first_ts, last_ts,
                                 log_path: str = "") -> dict:
        return _organized_issue_context(
            raw_desc, first_ts, last_ts, log_path,
            llm_client_model=self._get_llm_client_model,
            domain=self.profile.gather_domain,
        )

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------
    def index(self):
        suggested_log = app_config.last_analyzed_log_path or ""
        issue_desc = ""
        ctx: dict = {}
        try:
            ctx = _extract_issue_context()
            issue_desc = ctx.get("description", "")
        except Exception:
            ctx = {}

        # Pre-warm the issue-AI cache BEFORE rendering so the page's
        # /get_issue_context AJAX hits a hot cache and returns instantly —
        # otherwise it does a synchronous LLM organize on page load and the
        # sidebar "spins" while the user waits.
        #
        # In the normal button flow prepare() already warmed _issue_ai_quick,
        # so this is a cheap cache hit. It only does real work when the page is
        # reached WITHOUT going through prepare() (direct nav, browser back, or
        # a stale last_analyzed_log_path) — exactly the path that was slow. The
        # LLM wait (if any) now happens during the page-render request (browser
        # shows its native loading bar) instead of as a post-load spinner.
        # Best-effort: never let a warm failure block the page.
        if self.profile.prewarm_issue_ai_on_index:
            try:
                if suggested_log and not session.get("_issue_ai_quick"):
                    _first_ts, _last_ts = read_log_time_range(suggested_log)
                    self._issue_context_organized(
                        ctx.get("description", "") or "", _first_ts, _last_ts)
            except Exception as _warm_err:
                print(f"⚠️ {self.profile.name} index pre-warm skipped: {_warm_err}")

        return render_template(
            "chatbot/page.html",
            ui=self.profile.ui,
            suggested_log=suggested_log,
            issue_description=issue_desc,
        )

    # ------------------------------------------------------------------
    # API: set log file path
    # ------------------------------------------------------------------
    def set_log(self):
        data = request.get_json(silent=True) or {}
        log_path = data.get("log_path", "").strip()
        if not log_path:
            return jsonify({"success": False, "error": "log_path is required"}), 400

        try:
            # Capture the previous log_path + conv_id BEFORE we rotate, so the
            # client can show a "log switched, chat cleared" toast and offer
            # undo within a short window. `rotated` is True only when this
            # genuinely replaces a different log (not the first load).
            prev_log_path = (session.get("chatbot_log_path") or "").strip()
            rotated = bool(prev_log_path) and prev_log_path != log_path
            prev_conv_id = (session.get("feedback_conversation_id") or "") if rotated else ""

            # Re-loading the SAME file is not a new case. It used to be treated
            # as one anyway — a fresh conversation id and a wiped agent — so
            # anything that incidentally re-loaded the log (the path field
            # losing focus, a draft restore, returning to the live session)
            # silently split a case into another one-turn conversation and made
            # the next question start from nothing. Continue the thread when the
            # file has not changed.
            same_log = bool(prev_log_path) and prev_log_path == log_path

            agent = self._get_or_create_agent(skip_prime=True)
            agent.current_log_path = log_path
            if not same_log:
                agent.reset_conversation()      # fresh conversation for a new file
            ctx = _extract_issue_context()      # re-extract context in case session was updated after agent creation
            # Always prime: prime_with_context falls back to the log file's latest
            # timestamp when ctx has no usable issue time, so the sidebar always
            # gets an issue_time to display (covers the no-session entry path).
            # It also clears conversation_history, so on a same-file re-load the
            # thread is put back afterwards — priming is wanted for its caches and
            # issue-time resolution, not for its side effect on the conversation.
            preserved_history = list(agent.conversation_history or []) if same_log else []
            agent.prime_with_context(**ctx)

            session["chatbot_log_path"] = log_path

            # Sidecar: a NEW log file = a new conversation. Rotate the id (and
            # eagerly create the snapshot file so issue context is captured even
            # if the user never sends a message); keep it for a same-file re-load
            # so the next turn appends to the conversation already on screen.
            new_conv_id = self._ensure_feedback_conversation_id(rotate=not same_log)

            if same_log:
                if preserved_history:
                    agent.conversation_history = preserved_history
                else:
                    # The per-conversation agent is detached from the session
                    # slot at the start of every tools run, so by now this is
                    # usually a fresh instance with nothing to preserve. Fall
                    # back to the conversation's stored context for the same
                    # continuity a History click gets.
                    try:
                        stored = history_service.get_context(
                            new_conv_id, domain=self.profile.history_domain)
                        if stored:
                            agent.import_conversation_context(stored)
                    except Exception as _e:
                        print(f"[set_log] context restore skipped: {_e}")
            feedback_service.ensure_conversation(
                conversation_id=new_conv_id,
                session_id=session.get("chatbot_session_id", ""),
                issue=ctx,
                log_path=log_path,
                domain=self.profile.history_domain,
            )

            # Whole-minute span of the log so the sidebar can cap the
            # issue-time capture window at the log's actual length. 0 means
            # "unknown" (no parseable timestamps) — client falls back to a
            # generic cap.
            try:
                log_span_minutes = agent.get_log_span_minutes()
            except Exception:
                log_span_minutes = 0

            # Whether this log carries dates. Time-only logs (e.g. DDD) let the
            # sidebar leave the date fields blank and match Segment2 by
            # time-of-day. Computed before log_last_time because the time-only
            # fallback below needs to know which format to look for.
            try:
                log_has_date = agent._log_has_date()
            except Exception:
                log_has_date = True

            # Log's last parseable timestamp — offered in the "no issue time"
            # prompt as a one-click anchor ("Use log's last time").
            log_last_time = ""
            try:
                _first_ts, _last_ts = read_log_time_range(log_path)
                if _last_ts:
                    log_last_time = format_issue_time(_last_ts)
                elif self.profile.allow_time_only and log_has_date is False:
                    log_last_time = _time_only_log_last_time(agent)
            except Exception as _e:
                print(f"⚠️  log_last_time lookup failed: {_e}")
                log_last_time = ""

            # Locate associated System Event log (.evtx / .evt)
            try:
                evtx_path = find_event_log_for_log(log_path)
            except Exception:
                evtx_path = ""

            return jsonify({
                "success": True,
                "message": f"Log file set: {log_path}",
                "skills": agent.get_skill_descriptions(),
                "issue_time": format_issue_time(agent.issue_time),
                "log_span_minutes": log_span_minutes,
                "log_last_time": log_last_time,
                "log_has_date": log_has_date,
                "evtx_path": evtx_path,
                **check_ips_service.prompt_state(log_path),
                # Hints for the client to clear chat history + show the toast.
                "rotated": rotated,
                "previous_log_path": prev_log_path if rotated else "",
                "previous_conversation_id": prev_conv_id if rotated else "",
                "new_conversation_id": new_conv_id,
            })
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    # ------------------------------------------------------------------
    # API: suggest issue time(s) via LLM
    # ------------------------------------------------------------------
    def suggest_issue_times(self):
        """Suggest issue time(s) from the user's typed description + a rough browse
        of the loaded log. User-first (explicit times bypass the LLM). The frontend
        must obtain the user's consent before calling this route. The heavy lifting
        lives in ``utils.issue_time_ai``; this handler just marshals request/agent
        state in and jsonifies the result out."""
        data = request.get_json(silent=True) or {}
        text = (data.get("text") or "").strip()
        # The page's event-log dropdowns are forwarded so the AI uses the SAME
        # Warn+Err selection the user sees (level defaults to 'warning_error',
        # source defaults to 'all' so we don't silently exclude the relevant bus).
        source_filter = str(data.get("source_filter") or "all").strip() or "all"
        level_filter = str(data.get("level_filter") or "warning_error").strip() or "warning_error"
        try:
            agent = self._get_or_create_agent()
            log_path = agent.current_log_path or ""
            first_ts, last_ts = read_log_time_range(log_path) if log_path else (None, None)

            # Cached raw log lines feed the rough-browse digest (best-effort).
            log_lines = []
            try:
                if not agent._ensure_raw_log_cache():
                    log_lines = agent._raw_log_cache or []
            except Exception:
                log_lines = []

            event_log_events = (
                _event_log_anchor_events(log_path, source_filter, level_filter)
                if self.profile.event_log_anchor else []
            )

            # An empty description is allowed — the AI can still infer the issue
            # time from the log alone (a description just improves accuracy). Only
            # block when there's truly nothing to analyze (no text AND no log).
            if not text and not log_lines:
                return jsonify({"success": False,
                                "error": "Type a problem description or load a log first."}), 400

            extra: dict = {}
            if self.profile.allow_time_only:
                # Detect whether the loaded log carries dates or is time-only
                # (DDD / tracefmt). Threading this into
                # build_issue_time_suggestions makes the LLM prompt + the
                # returned suggestion shape honest about it: time-only logs
                # yield time-only suggestions with no fabricated date
                # placeholder.
                try:
                    extra["log_has_date"] = agent._log_has_date()
                except Exception:
                    extra["log_has_date"] = None
            if self.profile.customer_timezone:
                # The log's first/last timestamps stay in the log frame (decoder
                # host clock) — the same frame the LLM sees in the digest. So no
                # shift is needed before the model call and only the tz LABEL
                # travels, for the prompt to name the clock it is reading.
                extra["log_frame_first_ts"] = None
                extra["log_frame_last_ts"] = None
                _tz_name = get_effective_timezone(log_path) if log_path else ""
                extra["tz_label"] = format_tz_label(_tz_name) if _tz_name else ""
            if self.profile.event_log_anchor:
                extra["event_log_events"] = event_log_events

            payload = build_issue_time_suggestions(
                text=text,
                log_lines=log_lines,
                first_ts=first_ts,
                last_ts=last_ts,
                llm_client=getattr(agent, "client", None),
                llm_model=getattr(agent, "model", None),
                **extra,
            )
            _attach_nearest_errors(payload, event_log_events)
            return jsonify(payload), (200 if payload.get("success") else 503)
        except Exception as e:
            traceback.print_exc()
            return jsonify({"success": False, "error": str(e)}), 500

    # ------------------------------------------------------------------
    # API: chat
    #
    # This is the HTTP adapter around one turn: parse the request, apply the
    # issue-time policy, open the background job, and account for the turn in
    # Gather / feedback / history. It used to be written twice because those
    # four things looked profile-specific; the only parts that actually are
    # turned out to be the window cap, the customer-tz refresh and the two
    # domain labels, so it is now written once.
    #
    # What the worker eventually calls — agent.chat(), i.e.
    # ConversationMixin.chat in services/chatbot/engine/conversation.py — has
    # been one shared implementation since the engine refactor. Same name, two
    # layers: seeing agent.chat() in here is not what makes this shared.
    # ------------------------------------------------------------------
    def chat(self):
        data = request.get_json(silent=True) or {}
        user_message = (data.get("message") or "").strip()
        if not user_message:
            return jsonify({"success": False, "error": "message is required"}), 400

        # 428 Precondition Required: the log has no case number yet. The client
        # opens the prompt and replays this request once it has one. Gated on
        # the log this profile's session agent actually holds: prepare() moves
        # the agent to a new log without touching session['chatbot_log_path'],
        # so the no-argument form could check a stale, already-answered path.
        # (#165 made the same call for NW.)
        sid = session.get("chatbot_session_id", "")
        active_agent = self.session_agents.get(sid) if sid else None
        active_log = str(getattr(active_agent, "current_log_path", "") or "")
        blocked = check_ips_service.blocking_state(active_log or None)
        if blocked:
            return jsonify(blocked), 428

        try:
            temperature = float(data.get("temperature", 0.2))
        except Exception:
            temperature = 0.2
        temperature = max(0.0, min(1.0, temperature))

        try:
            max_steps = int(data.get("max_steps", 6))
        except Exception:
            max_steps = 6
        max_steps = max(1, min(12, max_steps))

        # Parent-message id: client-generated UUID stamped on every iteration
        # of a single Send click. When the user types one message that yields
        # multiple incident analyses (multi-time chained calls), every
        # resulting turn shares this id, so downstream ETL can recover the
        # co-firing relationship from the bronze layer.
        parent_message_id = (data.get("parent_message_id") or "").strip()

        # Issue-time window (minutes before/after issue_time captured for the
        # Segment2 log slice). Sidebar-adjustable; default ±5. Allowed range
        # is 0..log-span; the frontend enforces the log-span cap, here we
        # just clamp to the profile's hard bound so a stray value can't blow
        # up the pre-scan. 0 is valid (capture only the exact issue instant).
        issue_time_window_minutes = None
        if "issue_time_window_minutes" in data:
            try:
                issue_time_window_minutes = max(0, min(
                    self.profile.window_minutes_cap,
                    int(data.get("issue_time_window_minutes")),
                ))
            except (TypeError, ValueError):
                issue_time_window_minutes = None

        try:
            # Resolve the conversation first so we can adopt a finished job's
            # agent (full tool-grounded history) when the user continues a
            # just-analysed thread WITHOUT going through the History sidebar's
            # /history/load — otherwise the run_chat_with_tools background job
            # detaches the agent from the session slot and a bare
            # _get_or_create_agent() would hand the very next follow-up a
            # brand-new, context-less agent.
            conversation_id = self._ensure_feedback_conversation_id()
            agent = self._resume_agent_for(conversation_id)
            if issue_time_window_minutes is not None:
                agent.issue_time_window_minutes = issue_time_window_minutes
            # Backstop: if the resolved agent lost its log path (e.g. a fresh
            # agent rebuilt on a browser-back re-run where prepare()/set_log()
            # didn't run), recover it from the same sources
            # _get_or_create_agent() uses — which are also the ones the sidebar
            # reads — before the guard below, so a valid in-session log isn't
            # reported as missing.
            if not agent.current_log_path:
                agent.current_log_path = (
                    session.get("chatbot_log_path")
                    or app_config.last_analyzed_log_path or ""
                )
            if not agent.current_log_path:
                def _no_log():
                    yield f"data: {json.dumps({'type': 'error', 'content': 'No log file loaded. Please set a log file first.'})}\n\n"
                return Response(_no_log(), mimetype="text/event-stream")

            # ------------------------------------------------------------------
            # Issue Time: the sidebar field is the SINGLE source of truth.
            # When the frontend includes the `issue_time` key, override whatever
            # was pre-populated by prime_with_context (e.g. attachment_time).
            # An empty string with `issue_time_cleared=True` means "user explicitly
            # chose no time" — clear the agent's issue_time AND any backup sources.
            # An empty string WITHOUT that flag means the sidebar had only time
            # fields filled (time-only, no date) — keep the sentinel so pre-scan
            # can align the date to the log file range.
            # ------------------------------------------------------------------
            if "issue_time" in data:
                raw_it = (data.get("issue_time") or "").strip()
                explicitly_cleared = bool(data.get("issue_time_cleared", False))
                if raw_it:
                    # Full datetime from sidebar — override agent's issue_time
                    if isinstance(agent.issue_context, dict):
                        agent.issue_context.pop("attachment_time", None)
                    parsed, is_time_only = parse_issue_time_string(raw_it)
                    agent.issue_time = parsed
                    agent._issue_time_time_only = is_time_only
                    if self.profile.customer_timezone:
                        _sync_customer_issue_time(agent, parsed, is_time_only)
                elif explicitly_cleared:
                    # Explicit "no time": clear agent state and neutralise
                    # description/subject so the fallback chain can't re-extract one.
                    agent.issue_time = None
                    if isinstance(agent.issue_context, dict):
                        agent.issue_context.pop("attachment_time", None)
                        for k in ("description", "subject"):
                            v = agent.issue_context.get(k)
                            if isinstance(v, str) and v:
                                # Strip recognisable timestamp fragments.
                                v = re.sub(r'\d{1,2}/\d{1,2}/\d{4}[\s-]\d{1,2}:\d{2}:\d{2}(?:\.\d{1,3})?', '', v)
                                v = re.sub(r'\d{4}[-/]\d{1,2}[-/]\d{1,2}[\sT]\d{1,2}:\d{2}:\d{2}', '', v)
                                v = re.sub(r'\b\d{1,2}:\d{2}:\d{2}(?:\.\d{1,3})?\b', '', v)
                                agent.issue_context[k] = re.sub(r'\s+', ' ', v).strip()
                # else: empty but not explicitly cleared (time-only in sidebar, date blank)
                # → keep agent.issue_time as-is (sentinel 0001-01-01) so pre-scan aligns date

            # ------------------------------------------------------------------
            # Feedback sidecar: identify this turn so the frontend can attach
            # 👍/👎 to it, and so the conversation snapshot can record skill
            # invocations. Both IDs are anonymous (no auth).
            # ------------------------------------------------------------------
            session_id = session.get("chatbot_session_id", "")
            turn_id = str(uuid.uuid4())
            turn_started_at = datetime.now()
            try:
                _issue_ctx_for_snapshot = _extract_issue_context()
            except Exception:
                _issue_ctx_for_snapshot = {}

            feedback_service.begin_turn(
                conversation_id,
                issue=_issue_ctx_for_snapshot,
                log_path=getattr(agent, "current_log_path", "") or "",
            )

            # Usage analytics: on every Send, capture the entry session (user name,
            # date, CASE NUMBER + case summary) and the asked question into the
            # shared Gather folder for later DB ingestion. Non-blocking; never
            # raises, so it can't affect the chat path.
            try:
                gather_service.record_send(
                    conversation_id=conversation_id,
                    workflow_id=session.get("gather_workflow_id", ""),
                    session_id=session_id,
                    user_message=user_message,
                    issue=_issue_ctx_for_snapshot,
                    log_path=getattr(agent, "current_log_path", "") or "",
                    issue_time=format_issue_time(agent.issue_time),
                    issue_time_window_minutes=getattr(agent, "issue_time_window_minutes", None),
                    domain=self.profile.gather_domain,
                    turn_id=turn_id,
                )
            except Exception:
                pass
            collected_steps: list = []

            # Register a background job that OWNS this analysis, then detach the
            # agent from the session slot. The run keeps going — and stays
            # uncorrupted — even if the user switches to another conversation
            # mid-analysis (any later session use just creates a fresh agent).
            # The job buffers every step so a reconnecting client can replay +
            # follow it via /history/stream.
            job = chat_jobs.start_job(
                conversation_id=conversation_id,
                turn_id=turn_id,
                title=user_message,
                agent=agent,
                domain=self.profile.history_domain,
            )
            if session_id:
                self.session_agents.pop(session_id, None)

            def step_cb(step):
                try:
                    if isinstance(step, dict):
                        # Stamp how far into the turn this step arrived. The
                        # live card times itself from the browser clock; a
                        # replay months later cannot, so the offset travels
                        # with the step into history. The copy keeps the
                        # object published to live subscribers untouched.
                        elapsed_ms = int(
                            (datetime.now() - turn_started_at).total_seconds() * 1000)
                        collected_steps.append({**step, "ts_ms": elapsed_ms})
                except Exception:
                    pass
                chat_jobs.publish_step(job, step)

            @copy_current_request_context
            def run_chat_with_tools():
                try:
                    result = agent.chat(
                        user_message,
                        max_steps=max_steps,
                        temperature=temperature,
                        step_callback=step_cb,
                    )
                    # Cost accounting: token counts only exist once the LLM has
                    # finished, so this is a second Gather write on top of the
                    # record_send() that opened this turn.
                    try:
                        gather_service.record_usage(
                            conversation_id=conversation_id,
                            workflow_id=session.get("gather_workflow_id", ""),
                            model=getattr(agent, "model", "") or "",
                            usage=getattr(agent, "last_turn_usage", None),
                            issue=_issue_ctx_for_snapshot,
                            domain=self.profile.gather_domain,
                            turn_id=turn_id,
                            latency_ms=int((datetime.now() - turn_started_at).total_seconds() * 1000),
                        )
                    except Exception:
                        pass
                    # Persist BEFORE signalling done so any subscriber that
                    # refreshes its history list on 'done' already sees this
                    # turn. feedback is vote-gated; history always persists.
                    feedback_service.record_turn(
                        session_id=session_id,
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        user_message=user_message,
                        agent_result=result,
                        steps=collected_steps,
                        mode="tools",
                        duration_ms=int((datetime.now() - turn_started_at).total_seconds() * 1000),
                        issue=_issue_ctx_for_snapshot,
                        log_path=getattr(agent, "current_log_path", "") or "",
                        parent_message_id=parent_message_id,
                        domain=self.profile.history_domain,
                    )
                    history_service.record_turn(
                        conversation_id=conversation_id,
                        session_id=session_id,
                        turn_id=turn_id,
                        user_message=user_message,
                        agent_result=result,
                        mode="tools",
                        issue=_issue_ctx_for_snapshot,
                        log_path=getattr(agent, "current_log_path", "") or "",
                        issue_time=format_issue_time(agent.issue_time),
                        domain=self.profile.history_domain,
                        # Keep the reasoning trace too, so reopening this
                        # conversation shows how the answer was reached and
                        # not only what it was.
                        steps=collected_steps,
                        # And the model-facing conversation, so a follow-up
                        # asked tomorrow is answered by something that still
                        # has the evidence, not just the conclusions.
                        agent_context=self._export_agent_context(agent),
                    )
                    chat_jobs.finish_job(job, result)
                except Exception as exc:
                    error_tb = traceback.format_exc()
                    print(f"❌ Chat-with-tools thread error:\n{error_tb}")
                    try:
                        gather_service.record_turn_status(
                            conversation_id=conversation_id,
                            turn_id=turn_id,
                            status="failed",
                            workflow_id=session.get("gather_workflow_id", ""),
                            issue=_issue_ctx_for_snapshot,
                            domain=self.profile.gather_domain,
                            latency_ms=int((datetime.now() - turn_started_at).total_seconds() * 1000),
                            error_code=type(exc).__name__,
                        )
                    except Exception:
                        pass
                    chat_jobs.fail_job(job, str(exc))

            t = threading.Thread(target=run_chat_with_tools, daemon=True)
            t.start()

            # The original request streams the job exactly like a reconnect
            # would (replay buffered steps, then follow to done/error).
            return Response(
                _job_sse(job),
                mimetype="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        except Exception as e:
            error_traceback = traceback.format_exc()
            print(f"❌ Chatbot error:\n{error_traceback}")
            try:
                if conversation_id and turn_id:
                    gather_service.record_turn_status(
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        status="failed",
                        workflow_id=session.get("gather_workflow_id", ""),
                        issue=locals().get("_issue_ctx_for_snapshot") or {},
                        domain=self.profile.gather_domain,
                        error_code=type(e).__name__,
                    )
            except Exception:
                pass

            def generate_error():
                yield f"data: {json.dumps({'type': 'error', 'content': str(e)}, ensure_ascii=False)}\n\n"

            return Response(generate_error(), mimetype="text/event-stream")

    # ------------------------------------------------------------------
    # API: conversation history
    #
    # chat_stop and the history list/stream/get/delete/rename/pin endpoints come
    # from services.chatbot.shared_routes (see the context built below) — they
    # only ever differed by the history domain key. history_load stayed in the
    # profile modules because the resume path reads that key in more places; it
    # is now written once here too.
    # ------------------------------------------------------------------
    def history_load(self):
        """
        Resume a saved conversation: re-point the session at its id, restore the
        log file + issue context into the per-session agent (so follow-up
        questions keep working), rebuild the agent's textual conversation history,
        and return the stored turns for the frontend to re-render.
        """
        data = request.get_json(silent=True) or {}
        conversation_id = (data.get("conversation_id") or "").strip()
        if not conversation_id:
            return jsonify({"success": False, "error": "conversation_id is required"}), 400

        # A first analysis still in flight has no disk file yet — fall back to
        # its in-memory job so the sidebar's ⏳ entry is still openable.
        # chat_jobs is a single registry shared by every profile, so reject
        # anything not tagged with this one's domain — otherwise the other
        # bot's conversation id would adopt its job and its agent here.
        job = chat_jobs.get_job(conversation_id)
        if job is not None and getattr(job, "domain", "") != self.profile.history_domain:
            job = None
        # with_steps: the client re-renders each saved turn's reasoning trace, the
        # same card the live stream drew while the turn was running.
        conv = history_service.get_conversation(
            conversation_id, domain=self.profile.history_domain, with_steps=True)
        if conv is None and job is None:
            return jsonify({"success": False, "error": "Conversation not found"}), 404

        try:
            # Re-point BOTH sidecars at this conversation so new turns + feedback
            # continue appending here instead of spawning a fresh conversation.
            session["feedback_conversation_id"] = conversation_id

            conv = conv or {}
            feedback_service.remember_conversation_case(conversation_id, conv)
            running = bool(job is not None and job.status == "running")
            issue = conv.get("issue") if isinstance(conv.get("issue"), dict) else {}
            turns = conv.get("turns") or []

            log_path = (conv.get("log_path") or "").strip()

            # Prefer adopting the conversation's in-memory agent (it holds the full
            # tool-grounded history) over rebuilding context from saved text.
            adopted = False
            if job is not None and getattr(job, "agent", None) is not None:
                agent = job.agent
                adopted = True
                if not log_path:
                    log_path = (getattr(agent, "current_log_path", "") or "").strip()
                # Don't pull a RUNNING job's agent into the session slot — it's busy
                # on a background thread. Reinstate only finished ones for follow-ups.
                if not running:
                    sid = session.get("chatbot_session_id")
                    if not sid:
                        sid = str(uuid.uuid4())
                        session["chatbot_session_id"] = sid
                    self.session_agents[sid] = agent
            else:
                agent = self._get_or_create_agent(skip_prime=True)
                agent.reset_conversation()

            log_exists = bool(log_path) and os.path.exists(log_path)

            skills = []
            log_has_date = True
            log_span_minutes = 0
            if log_exists:
                if not adopted:
                    agent.current_log_path = log_path
                    # Prime context (also resets conversation_history) BEFORE we
                    # rebuild the textual turn history below.
                    allowed = {"case_nbr", "subject", "description", "issue_type", "attachment_time"}
                    try:
                        agent.prime_with_context(**{k: v for k, v in issue.items()
                                                    if k in allowed and isinstance(v, str)})
                    except Exception as _e:
                        print(f"[history] prime_with_context skipped: {_e}")
                # get_skill_descriptions() only reads self.skills (set once at
                # construction, never reassigned mid-run) and
                # get_log_span_minutes() does its own independent file read keyed
                # off current_log_path — both are safe to call even while the
                # job's background thread is still analysing.
                try:
                    skills = agent.get_skill_descriptions()
                except Exception:
                    skills = []
                try:
                    log_span_minutes = agent.get_log_span_minutes()
                except Exception:
                    log_span_minutes = 0
                # _log_has_date() reads self._raw_log_cache / _raw_log_cache_path,
                # which the background analysis thread actively mutates via
                # _ensure_raw_log_cache() while running=True. Profiles that skip
                # it for a live job avoid reading that pair mid-write; the cheap
                # default (True) just means the sidebar briefly uses the original
                # datetime windowing until the job finishes and the page reloads.
                if self.profile.read_log_has_date_while_running or not running:
                    try:
                        log_has_date = agent._log_has_date()
                    except Exception:
                        log_has_date = True

            # Restore the issue time the conversation was anchored on.
            issue_time_str = (conv.get("issue_time") or "").strip()
            if adopted:
                # The adopted agent already carries the right issue_time; just
                # surface it to the client when the snapshot didn't record one.
                if not issue_time_str:
                    try:
                        issue_time_str = format_issue_time(agent.issue_time) or ""
                    except Exception:
                        issue_time_str = ""
            elif issue_time_str:
                try:
                    parsed, is_time_only = parse_issue_time_string(issue_time_str)
                    agent.issue_time = parsed
                    agent._issue_time_time_only = is_time_only
                except Exception:
                    pass

            # Restore the agent's conversation ONLY when we didn't adopt a live
            # agent (which already holds the real history).
            #
            # Preferred: the stored model-facing context — the same messages the
            # agent last sent, tool results included — so a follow-up is answered
            # by something that still has the evidence. It is applied AFTER
            # prime_with_context, which resets conversation_history along with the
            # agent's caches; the stored context already carries its own priming
            # head, so replacing wholesale avoids a duplicate one.
            #
            # Fallback: the pre-existing rebuild from result text. Plain
            # user/assistant pairs, no tool_use blocks, so the tool loop's pairing
            # invariants stay intact. Conversations saved before contexts were
            # stored land here, and behave exactly as they did before.
            context_restored = 0
            if not adopted:
                stored_context = history_service.get_context(
                    conversation_id, domain=self.profile.history_domain)
                if stored_context:
                    try:
                        context_restored = agent.import_conversation_context(stored_context)
                    except Exception as _e:
                        print(f"[history] context restore failed: {_e}")
                        context_restored = 0
                if not context_restored:
                    for turn in turns:
                        um = (turn.get("user_message") or "").strip()
                        if um:
                            agent.conversation_history.append({"role": "user", "content": um})
                        at = history_service.assistant_text_from_result(turn.get("result"))
                        if at:
                            agent.conversation_history.append({"role": "assistant", "content": at})

            if log_exists:
                session["chatbot_log_path"] = log_path

            return jsonify({
                "success": True,
                "conversation_id": conversation_id,
                "title": conv.get("title") or (job.title if job else "") or "Conversation",
                "turns": turns,
                # How grounded the resumed agent is, so the UI can say so rather
                # than leaving the user to guess whether a follow-up will still
                # know what the analysis found. 0 = rebuilt from result text.
                "context_restored": context_restored,
                # Live-analysis hand-off: when running, the client renders these
                # buffered steps and then opens /history/stream to follow the rest.
                "running": running,
                "running_user_message": (job.title if running else ""),
                "steps": list(job.steps) if (job is not None and running) else [],
                "log_path": log_path,
                "log_exists": log_exists,
                "issue_time": issue_time_str,
                "log_has_date": log_has_date,
                "log_span_minutes": log_span_minutes,
                "skills": skills,
            })
        except Exception as e:
            traceback.print_exc()
            return jsonify({"success": False, "error": str(e)}), 500

    # ------------------------------------------------------------------
    # API: prepare chatbot from download_result (set log path + case context)
    # ------------------------------------------------------------------
    def prepare(self):
        """
        Called from download_result when the user clicks the analysis button.
        1. Derives the log path from the given etl_path.
        2. Sets the agent's current_log_path.
        3. Primes conversation history with case description + classification.
        Returns {"success": True} — JS then redirects to this profile's page.
        """
        data = request.get_json(silent=True) or {}
        etl_path = data.get("etl_path", "").strip()
        carried_issue_time = str(data.get("issue_time") or "").strip()
        if not etl_path:
            return jsonify({"success": False, "error": "etl_path is required"}), 400

        # A profile whose decode writes the log directly (BT HCI produces
        # <etl>.hci.txt, with no separate .log) accepts a path that IS already a
        # log file. Mirrors the log_parser entry handling. Everyone else follows
        # the wpp_ddd convention and appends ".log".
        lower = etl_path.lower()
        if (self.profile.accepts_direct_log_path and os.path.exists(etl_path)
                and (lower.endswith(".log") or lower.endswith(".txt"))):
            log_path = etl_path
        else:
            log_path = etl_path + ".log"
        if not os.path.exists(log_path):
            # A profile that can only ever derive "<etl>.log" says so; one that
            # also accepts a direct path can't promise which extension it wanted.
            missing = "Log file" if self.profile.accepts_direct_log_path else ".log file"
            return jsonify({"success": False,
                            "error": f"{missing} not found: {log_path}"}), 404

        try:
            if self.profile.purge_issue_caches_on_prepare:
                # Entering a NEW analysis from download_result. Purge the derived
                # issue-context caches FIRST so the context below is rebuilt from
                # this run's selected_files / case_context — not a previous run's
                # leftovers. (Fixes stale attachment time / description when a
                # second analysis is started without going through "Back to
                # Avatar".)
                _invalidate_issue_context_caches()
            # download_result already resolved the issue time against the
            # capture the user picked. Carry that exact value over, checked
            # against this log's range, instead of re-guessing it. Both
            # profiles: BT's decoded .hci.txt carries the same dated
            # timestamps, so the range check works for it too.
            _clear_carried_issue_time()
            if carried_issue_time:
                _validate_carried_issue_time(carried_issue_time, log_path)

            # Pull consolidated issue context from all session sources
            ctx = _extract_issue_context()

            # Update shared last_analyzed_log_path so the chatbot index page pre-fills it
            app_config.last_analyzed_log_path = log_path

            # Get/create per-session agent and prime it
            # skip_prime=True: we call prime_with_context explicitly below (after setting log path)
            agent = self._get_or_create_agent(skip_prime=True)
            agent.current_log_path = log_path
            if self.profile.reset_conversation_on_prepare:
                agent.reset_conversation()      # fresh conversation for a new file
            agent.prime_with_context(**ctx)

            # Run the token-frugal LLM issue-time + description organize NOW, on the
            # button click (deterministic pre-filter trims noise first). Cached in
            # session so the chatbot page's /get_issue_context reuses it rather than
            # calling the LLM a second time. Never let it fail the prepare step.
            try:
                _first_ts, _last_ts = read_log_time_range(log_path)
                self._issue_context_organized(ctx.get("description", "") or "", _first_ts, _last_ts)
            except Exception as _org_err:
                print(f"⚠️ Chatbot prepare: issue-context organize skipped: {_org_err}")

            # Sidecar: prepare = entering a new analysis = new conversation.
            new_conv_id = self._ensure_feedback_conversation_id(rotate=True)
            feedback_service.ensure_conversation(
                conversation_id=new_conv_id,
                session_id=session.get("chatbot_session_id", ""),
                issue=ctx,
                log_path=log_path,
                domain=self.profile.history_domain,
            )

            return jsonify({"success": True})
        except Exception as e:
            error_traceback = traceback.format_exc()
            print(f"❌ Chatbot prepare error:\n{error_traceback}")
            return jsonify({"success": False, "error": str(e)}), 500

    # ------------------------------------------------------------------
    # API: consolidated issue context for the sidebar
    # ------------------------------------------------------------------
    def get_issue_context(self):
        try:
            ctx = _extract_issue_context()
            attachment_time = ctx.get("attachment_time", "")
        except Exception:
            ctx = {}
            attachment_time = ""

        log_path = session.get("chatbot_log_path") or app_config.last_analyzed_log_path or ""
        first_ts, last_ts = read_log_time_range(log_path) if log_path else (None, None)

        if attachment_time:
            # Date a clock-only attachment_time up front, so the log-range
            # check below can judge it instead of discarding it as undated.
            # With customer_timezone the bare clock ("16:45:00", the customer
            # wall clock from the attachment subtitle) is also anchored to the
            # customer capture date and converted to the log frame — otherwise
            # the picker, which prefers attachment_time, would mix frames (e.g.
            # 06/03 16:45 instead of log-frame 06/03 05:45). Without it the
            # clock is stamped onto the log's date, the same treatment the
            # organizer gives that profile's issue times. Full datetimes pass
            # through unchanged (handled by _align_times_to_log_frame).
            _aligned_at = realign_times_to_log(
                [attachment_time], first_ts, last_ts,
                log_path if self.profile.customer_timezone else "")
            if _aligned_at:
                attachment_time = _aligned_at[0]

        # Smart pass: let the LLM organize the raw case Issue Description into a
        # clean problem statement + (possibly multiple) issue time points. Cached
        # per-description so repeat fetches don't re-call the LLM; falls back to
        # the regex extractor + concise composer when no LLM is configured.
        # log_path travels only for profiles that reconcile customer time:
        # realign_times_to_log uses it to detect the customer's timezone and
        # capture date, nothing else.
        organized = self._issue_context_organized(
            ctx.get("description", "") or "", first_ts, last_ts,
            log_path if self.profile.customer_timezone else "")
        clean_desc = organized.get("clean_description") or _compose_concise_description(ctx)
        issue_times = organized.get("issue_times") or []

        # A case-number hand-off is authoritative for this transition. It either
        # supplies the already-resolved, range-checked value from download_result,
        # or deliberately supplies no value after validation failed. In the latter
        # case do not silently fall back to a fresh LLM/attachment/log-latest guess.
        carried_present = bool(session.get("_carried_issue_time_present"))
        carried_issue_time = (session.get("_carried_issue_time") or "").strip()
        carried_warning = (session.get("_carried_issue_time_warning") or "").strip()
        if carried_present:
            issue_times = [carried_issue_time] if carried_issue_time else []
            attachment_time = carried_issue_time

        # Back-compat single issue_time: prefer the first organized time, else the
        # previous attachment_time / log-latest resolution (cached by log_path).
        if issue_times:
            issue_time_str = issue_times[0]
        elif carried_present:
            issue_time_str = ""
        elif self.profile.customer_timezone:
            # attachment_time is already frame-corrected above; use it directly.
            # When absent, fall back to the cached log-latest resolution.
            issue_time_str = attachment_time or _resolved_issue_time_for(log_path, attachment_time)
        else:
            issue_time_str = _resolved_issue_time_for(log_path, attachment_time)

        payload = {
            "description": clean_desc,
            "attachment_time": attachment_time,
            "issue_time": issue_time_str,
            "issue_times": issue_times,
            "interpretation": organized.get("interpretation", ""),
        }

        if self.profile.customer_timezone:
            (attachment_time, issue_time_str, issue_times,
             customer_tz_for_ui, customer_annotations) = _align_times_to_log_frame(
                log_path, first_ts, last_ts, attachment_time, issue_time_str, issue_times)
            payload.update({
                "attachment_time": attachment_time,
                "issue_time": issue_time_str,
                "issue_times": issue_times,
                # Customer-tz annotation: same instant viewed from the customer's
                # wall clock. The picker shows the log-frame value (matches .log
                # content) and surfaces this map underneath so the engineer also
                # sees what time it was on the customer's side. tz label is the
                # detected system_info / sidecar value; empty string when nothing
                # could be detected (chatbot then hides the annotation row).
                "customer_tz": customer_tz_for_ui,
                "customer_annotations": customer_annotations,
                # IANA id for the customer tz (e.g. "America/Los_Angeles") so the
                # browser can recompute the customer wall clock DST-correctly for
                # any date typed into the picker. Empty when only a fixed offset
                # is known — the frontend then falls back to the label's
                # standard offset.
                "customer_iana": to_iana_timezone(customer_tz_for_ui) if customer_tz_for_ui else "",
            })

        # Final safety net, for every profile: never put an auto-filled time
        # in the picker unless it falls inside the selected log (an LLM can
        # pick the wrong date when a log spans midnight). Deliberately not
        # tied to customer_timezone, which is a UI display flag.
        (attachment_time, issue_time_str, issue_times,
         range_blocked, range_warning) = _guard_times_inside_log_range(
            first_ts, last_ts, attachment_time, issue_time_str, issue_times,
            carried_present)
        payload.update({
            "attachment_time": attachment_time,
            "issue_time": issue_time_str,
            "issue_times": issue_times,
            "log_first_time": format_issue_time(first_ts),
            "log_last_time": format_issue_time(last_ts),
            # Why the picker was left empty: a hand-off that failed
            # validation, or every auto-filled time fell outside the log.
            "issue_time_blocked": bool(
                (carried_present and not carried_issue_time) or range_blocked
            ),
            "issue_time_warning": carried_warning or range_warning,
        })
        return jsonify(payload)

    # ==================================================================
    # Skills YAML lifecycle — dated filenames (skills_YYYY-MM-DD.yaml)
    # ==================================================================
    #
    # The endpoints below implement the SVG v2 "Skill" column: detect cloud
    # revisions newer than the local cache, let the user opt in to replace the
    # local copy, edit individual skills through a structured side panel, and
    # upload a user-tuned local file back to the share folder.
    #
    # The disabled-comment scan that backs them lives in
    # services/skill_editor/yaml_service.py. It exists because the cloud
    # baseline marks "historically used but currently disabled" entries as
    # comments (`# - "Got Command"`), which yaml.safe_load discards — so a
    # round-trip through the editor would lose them. Its skill-header regex is
    # anchored at [A-Za-z0-9_] and widened to accept the real skill IDs in this
    # codebase that contain "/" ("VLP/UHB/AFC", "WRDS/WGDS/EWRD/SGOM" — see
    # each engine module's SKILL_FILE_MAP).
    def _build_handlers(self) -> dict[str, Callable[..., Any]]:
        profile, yu = self.profile, self.profile.yaml_utils
        _yaml_helpers = build_profile_yaml_helpers(
            user_local_dir=yu.local_user_overrides_dir,
            today_yaml_filename=yu.today_dated_filename,
            user_yaml_prefix=profile.user_yaml_prefix,
            latest_cloud_baseline=yu.find_latest_cloud_baseline_yaml,
            latest_user_yaml=yu.find_latest_user_yaml,
            write_yaml_file=_write_yaml_file,
            load_skills_from_yaml=load_skills_from_yaml,
            get_agent=self._get_or_create_agent,
            agent_config_attr=profile.agent_config_attr,
        )

        skill_editor_handlers = build_skill_editor_handlers(SkillEditorContext(
            activate_yaml=_yaml_helpers["activate_yaml"],
            get_active_source=yu.get_active_source,
            get_or_create_agent=self._get_or_create_agent,
            latest_cloud_baseline=yu.find_latest_cloud_baseline_yaml,
            latest_user_yaml=yu.find_latest_user_yaml,
            persist_user_yaml_snapshot=_yaml_helpers["persist_user_yaml_snapshot"],
            read_yaml_file=_read_yaml_file,
            refresh_cloud_baseline=yu.refresh_local_cloud_baseline,
            resolve_cloud_skills_dir=yu.resolve_cloud_skills_dir,
            sanitise_skill_payload=_sanitise_skill_payload,
            set_active_source=yu.set_active_source,
            skills_yaml_status_payload=yu.skills_yaml_status,
        ))
        shared_handlers = build_shared_handlers(SharedRouteContext(
            domain=profile.history_domain,
            agent_config_attr=profile.agent_config_attr,
            get_agent=self._get_or_create_agent,
            session_agents=self.session_agents,
            browse_filetypes=profile.browse_filetypes,
            load_skills_from_yaml=load_skills_from_yaml,
            gather_domain=profile.gather_domain,
        ))

        return {
            "chat": self.chat,
            "get_issue_context": self.get_issue_context,
            "history_load": self.history_load,
            "index": self.index,
            "prepare": self.prepare,
            "set_log": self.set_log,
            "suggest_issue_times": self.suggest_issue_times,
            **shared_handlers,
            **skill_editor_handlers,
        }


def create_agent_blueprint(profile: AgentRouteProfile):
    """Build one profile's Blueprint from its route layer + the route contract.

    ``get_agent`` has to be THIS profile's session-agent accessor, because the
    factory's own three use cases (/reset, /skills, /browse_yaml) call it — and
    each profile keeps its own session store, so the two blueprints must not
    share one.
    """
    capabilities = profile.capabilities
    routes = AgentRoutes(profile)
    return create_chatbot_blueprint(ChatbotBlueprintConfig(
        name=profile.name,
        import_name=__name__,
        url_prefix=profile.url_prefix,
        capabilities=capabilities,
        get_agent=routes.get_agent,
        handlers=handler_map(routes.handlers, capabilities),
        on_reset=check_ips_service.start_new_session,
    ))


# ==================================================================
# The two profiles
# ==================================================================
WIFI_PROFILE = AgentRouteProfile(
    name="log_chatbot",
    ui=LOG_CHATBOT_UI,
    agent_class=WifiLogAgentSystem,
    agent_config_attr="log_chatbot_agent",
    yaml_utils=_wifi_yaml_utils,
    user_yaml_prefix="skills_",
    browse_filetypes=(("Log files", "*.log"), ("All files", "*.*")),
    gather_domain="wifi",
    window_minutes_cap=1440,
    purge_issue_caches_on_prepare=True,
    reset_conversation_on_prepare=True,
    accepts_direct_log_path=False,
    read_log_has_date_while_running=True,
    prewarm_issue_ai_on_index=False,
)

BT_PROFILE = AgentRouteProfile(
    name="bt_chatbot",
    ui=BT_UI,
    agent_class=BtLogAgentSystem,
    agent_config_attr="bt_chatbot_agent",
    yaml_utils=_bt_yaml_utils,
    user_yaml_prefix="bt_skills_",
    browse_filetypes=(("hci.txt files", "*.hci.txt"), ("All files", "*.*")),
    gather_domain="bt",
    window_minutes_cap=100000,
    purge_issue_caches_on_prepare=False,
    reset_conversation_on_prepare=False,
    accepts_direct_log_path=True,
    read_log_has_date_while_running=False,
    prewarm_issue_ai_on_index=True,
)

log_chatbot_bp = create_agent_blueprint(WIFI_PROFILE)
bt_chatbot_bp = create_agent_blueprint(BT_PROFILE)
