"""
Whether a log still owes us a case number, and remembering the answer.

The answer is remembered per log file rather than per session. The user is
answering a question about the file ("this one is case 01010628", "this one has
no case"), and that answer does not stop being true when the app restarts — or
when a slow request that started before they answered writes the session back
over the top of it.
"""

import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from flask import session

from configs.global_configs import app_config
from models.models import CaseContext
from utils import check_ips_utils, helpers

EXPLICIT = "explicit"
DERIVED_FROM_PATH = "derived_from_path"
SKIPPED = "skipped"
ABSENT = "absent"

_SOURCES = {EXPLICIT, DERIVED_FROM_PATH, SKIPPED, ABSENT}

SESSION_SOURCE_KEY = "case_ref_source"

_ANSWER_FILE_NAME = "ips_answers.json"
_MAX_REMEMBERED_ANSWERS = 500

_answer_lock = threading.Lock()

# Kept outside the session so overlapping requests cannot detach an answer from its log.
_last_prompted_log = ""

# Logs answered in this conversation; cleared so each new conversation reconfirms its case.
_answered_this_session = set()


def _answer_store_path() -> Optional[Path]:
    root = getattr(app_config, "avatarfiles_dir", "") or ""
    if not root:
        # Path() would be ".", which is truthy and would be written to.
        return None
    state_dir = Path(root) / "app_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / _ANSWER_FILE_NAME


def _log_key(log_path) -> str:
    text = str(log_path or "").strip()
    if not text:
        return ""
    # The analysis converts paths to the \\?\ extended-length form to get past
    # MAX_PATH. The prompt records its answer under the path as the user saw
    # it, so without this the same file had two keys and a confirmed skip was
    # invisible to the code that later looked it up by the long form.
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[len("\\\\?\\UNC\\"):]
    elif text.startswith("\\\\?\\"):
        text = text[len("\\\\?\\"):]
    # Send To can hand over an 8.3 short name (INTELA~1) that the analysis
    # later expands, so both spellings must land on one key.
    text = helpers.get_long_path(text)
    try:
        return os.path.normcase(os.path.abspath(text))
    except Exception:
        return os.path.normcase(text)


def _load_answers() -> dict:
    path = _answer_store_path()
    if path is None or not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        # A corrupt store must not block analysis; the worst case is re-asking.
        return {}


def _mark_answered(key: str) -> None:
    """Record that this log has been answered for in the current conversation."""
    if key:
        _answered_this_session.add(key)


def _note_log_path(log_path) -> None:
    global _last_prompted_log
    text = str(log_path or "").strip()
    if text:
        _last_prompted_log = text


def answer_for(log_path) -> dict:
    """The answer already given for this log file, or {}."""
    key = _log_key(log_path)
    if not key:
        return {}
    record = _load_answers().get(key)
    return record if isinstance(record, dict) else {}


def remember_answer(log_path, case_nbr: str, source: str) -> None:
    key = _log_key(log_path)
    if not key:
        return
    path = _answer_store_path()
    if path is None:
        return
    try:
        with _answer_lock:
            answers = _load_answers()
            answers[key] = {
                "case_nbr": case_nbr or "",
                "source": source,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            if len(answers) > _MAX_REMEMBERED_ANSWERS:
                oldest = sorted(answers.items(),
                                key=lambda kv: str((kv[1] or {}).get("at", "")))
                answers = dict(oldest[-_MAX_REMEMBERED_ANSWERS:])
            tmp = path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(answers, fh, indent=2)
            os.replace(tmp, path)
    except Exception as e:
        print(f"[ips] could not remember the answer for {key}: {e}")


def current_case_nbr() -> str:
    """The canonical case number already attached to this session, or ""."""
    raw = session.get("case_context") or {}
    if not isinstance(raw, dict) or not raw:
        return ""
    return check_ips_utils.normalise_ips(raw.get("case_nbr"))


def current_source() -> str:
    """How the prompt flow recorded this session's case.

    ABSENT here means "the prompt did not set it", which is what the gating in
    needs_ips keys off: a case that arrived from the case search rather than
    from this prompt counts for any log the user opens. For what to write into
    telemetry, use attributed_source() instead -- these are different questions
    and collapsing them breaks one or the other.
    """
    value = str(session.get(SESSION_SOURCE_KEY) or "").strip()
    return value if value in _SOURCES else ABSENT


def attributed_source() -> str:
    """How the case on this session was really obtained, for telemetry.

    The main case-submission routes put the source on the CaseContext and
    never on the separate session key, so reading only the key reported
    'absent' for a case the user had typed in. That is worse than untidy:
    sync_feedback drops the number whenever the source says absent, so an
    ordinary case lost its attribution entirely on the way to the warehouse.
    """
    value = str(session.get(SESSION_SOURCE_KEY) or "").strip()
    if value in _SOURCES and value != ABSENT:
        return value
    raw = session.get("case_context") or {}
    if isinstance(raw, dict):
        stated = str(raw.get("case_ref_source") or "").strip()
        if stated in _SOURCES:
            return stated
    return value if value in _SOURCES else ABSENT


def _session_answer_is_about(log_path) -> bool:
    """
    Whether the case sitting on the session was given about this log.

    A case that came from the case search rather than from this prompt belongs
    to whatever the user is working on, so it counts for any log they open.
    An answer given through the prompt belongs to one file and one conversation.
    """
    key = _log_key(log_path)
    if not key:
        return True
    if current_source() == ABSENT:
        return True
    return key in _answered_this_session


def _apply_stored_answer(log_path) -> None:
    """
    Put the answer for this log back on a session that lost it.

    Sessions are held server-side and written back whole at the end of every
    request, so a request that started before the user answered overwrites the
    answer on its way out. The file keyed by log path is the durable record.
    """
    record = answer_for(log_path)
    source = record.get("source")
    if source in (SKIPPED, EXPLICIT, DERIVED_FROM_PATH):
        # The restored answer is now this session's answer about this log.
        # Without saying so, _session_answer_is_about disowns it and
        # prompt_state reports 'absent' for a log that was in fact skipped.
        _mark_answered(_log_key(log_path))
    if source == SKIPPED:
        _remember_on_session("", SKIPPED, log_path)
    elif source in (EXPLICIT, DERIVED_FROM_PATH) and record.get("case_nbr"):
        _remember_on_session(str(record["case_nbr"]), source, log_path)


def start_new_session() -> None:
    """Begin a fresh conversation, which confirms the case again."""
    _answered_this_session.clear()
    if current_source() not in (EXPLICIT, DERIVED_FROM_PATH, SKIPPED):
        return
    session[SESSION_SOURCE_KEY] = ABSENT
    raw = session.get("case_context") or {}
    if isinstance(raw, dict) and raw:
        context = CaseContext.from_session(raw)
        if check_ips_utils.is_synthetic_case_nbr(context.case_nbr):
            # A local_upload_/local_bsod_ name is the run's own, and the
            # results page finds its extraction index by it -- the same reason
            # _remember_on_session keeps it on a skip. It is not a case number,
            # so keeping it attributes nothing; only the answer is reset.
            context.case_ref_source = None
        else:
            # A real case's subject, description, backend id and attachments
            # go with its number. Blanking only the number left them behind:
            # the next log's agent was primed from them before its prompt, and
            # the answer then found no previous case to clear -- so a new
            # conversation could still discuss the old case. Rebuild the
            # context, keeping only what is about the log, and clear the agents.
            if context.case_nbr:
                _forget_case_on_agents("")
            context = CaseContext(wifi_or_bt=context.wifi_or_bt,
                                  files_coexist=context.files_coexist)
        session["case_context"] = context.to_session()


_ARCHIVE_EXTS = (".zip", ".7z", ".rar")


def _archive_answer(log_path) -> dict:
    """The answer given for the archive this log was extracted from, if any.

    A local archive is extracted in place, into <its folder>/<stem with spaces
    as underscores>/ (see _process_local_analysis). So a Send To of a zip that
    is not under a case folder is asked about once, and then every log decoded
    out of it is a new log with no case in its path: the chatbot asked again
    with an empty box, and the number just typed had to be typed again. Read
    from the durable store, not the in-process set, because loading a new log
    may start a new conversation and clear that.
    """
    key = _log_key(log_path)
    if not key:
        return {}
    for answered, record in _load_answers().items():
        stem, ext = os.path.splitext(answered)
        if ext.lower() not in _ARCHIVE_EXTS:
            continue
        folder = os.path.normcase(os.path.join(
            os.path.dirname(answered), os.path.basename(stem).replace(" ", "_")))
        if key.startswith(folder + os.sep) and isinstance(record, dict) \
                and record.get("source") in (EXPLICIT, DERIVED_FROM_PATH) \
                and record.get("case_nbr"):
            return record
    return {}


def candidates_for(log_path) -> List[str]:
    """Case numbers worth offering for this log, best guess first."""
    found = check_ips_utils.derive_ips_candidates(log_path)
    from_archive = str(_archive_answer(log_path).get("case_nbr") or "")
    if from_archive and from_archive not in found:
        found.insert(0, from_archive)
    attached = current_case_nbr()
    if attached and attached not in found and _session_answer_is_about(log_path):
        found.insert(0, attached)
    remembered = str(answer_for(log_path).get("case_nbr") or "")
    if remembered and remembered not in found:
        found.insert(0, remembered)
    return found


def candidate_sources(log_path) -> dict:
    """Where each offered candidate came from, so the answer keeps its origin.

    The candidate list mixes numbers read from the path with a remembered or
    session answer, and the client used to treat every one of them as path
    evidence: a number the user had typed in an earlier conversation came back
    as 'derived_from_path', and a path guess picked over a remembered answer
    was recorded as 'explicit'.
    """
    sources = {nbr: DERIVED_FROM_PATH for nbr in check_ips_utils.derive_ips_candidates(log_path)}
    archive = _archive_answer(log_path)
    if archive and archive["case_nbr"] not in sources:
        # Given for the archive this log came out of: keep how it was given.
        sources[archive["case_nbr"]] = archive["source"]
    attached = current_case_nbr()
    if attached and attached not in sources and _session_answer_is_about(log_path):
        sources[attached] = attributed_source()
    record = answer_for(log_path)
    remembered = str(record.get("case_nbr") or "")
    if remembered and record.get("source") in (EXPLICIT, DERIVED_FROM_PATH):
        # The stored answer says how it was given, whatever the path says now.
        sources[remembered] = record["source"]
    return sources


def source_for_answer(case_nbr, claimed: str, log_path) -> str:
    """The source to record for an answer, decided here rather than trusted.

    A remembered answer keeps the source it was given with. Otherwise a claim
    of 'derived_from_path' stands only if the number really is a case folder
    in this log's path; anything else the user supplied is 'explicit'.
    """
    canonical = check_ips_utils.normalise_ips(case_nbr)
    record = answer_for(log_path)
    if canonical and canonical == str(record.get("case_nbr") or "") \
            and record.get("source") in (EXPLICIT, DERIVED_FROM_PATH):
        return record["source"]
    if claimed == DERIVED_FROM_PATH and canonical in check_ips_utils.derive_ips_candidates(log_path):
        return DERIVED_FROM_PATH
    return EXPLICIT


def needs_ips(log_path) -> bool:
    """Whether the modal should block this log."""
    key = _log_key(log_path)
    if key and key in _answered_this_session:
        # Answered in this conversation, so only the session can have lost it.
        _apply_stored_answer(log_path)
        return False
    if key and answer_for(log_path).get("source") == SKIPPED:
        # A confirmed "this log has no case" outlives the conversation and the
        # process. The user answered a question about the file, and that does
        # not stop being true when the app restarts or a new conversation
        # begins -- otherwise the same log is nagged about forever. A
        # remembered case number is deliberately not reapplied here: each new
        # conversation confirms that one again.
        _apply_stored_answer(log_path)
        return False
    if not key:
        return not current_case_nbr() and current_source() != SKIPPED
    if current_source() == ABSENT:
        # A case that came from the case search rather than from this prompt.
        return not current_case_nbr()
    return True


def prompt_state(log_path) -> dict:
    """Everything the client needs to decide whether and how to prompt."""
    _note_log_path(log_path)
    needed = needs_ips(log_path)
    candidates = candidates_for(log_path)
    mine = _session_answer_is_about(log_path)
    return {
        "needs_ips": needed,
        "ips_candidates": candidates,
        # Per-candidate origin, so choosing one records how it was obtained
        # rather than the client guessing from the combined list.
        "ips_candidate_sources": candidate_sources(log_path),
        "suggested_ips": candidates[0] if candidates else "",
        # Reporting a case that was answered about a different log would have
        # the client show this conversation as already attributed.
        "case_nbr": current_case_nbr() if mine else "",
        "case_ref_source": current_source() if mine else ABSENT,
        # A skip is remembered against the path, so the dialog has to be able
        # to name the log it is about to mark as caseless.
        "log_path": str(log_path or ""),
    }


def blocking_state(log_path=None):
    """
    The prompt payload when this session may not start chatting yet, else None.

    The check lives on the server because the requirement is that the case
    number is always recorded, and a client-side dialog is only ever a
    suggestion — the same conversation can be reached from five entry points
    and from a restored session.
    """
    path = log_path if log_path is not None else (
        session.get("chatbot_log_path")
        or _last_prompted_log
        or app_config.last_analyzed_log_path
        or ""
    )
    if not needs_ips(path):
        return None
    state = prompt_state(path)
    state.update({
        "success": False,
        "ips_required": True,
        "log_path": path,
        "error": "Enter the IPS case number for this log before starting the conversation.",
    })
    return state


def _placeholder_is_for(log_path) -> bool:
    """Whether the session's local run is the run this log belongs to.

    A local_upload_/local_bsod_ context is only this log's own when the log is
    the file that was uploaded, a file decoded from it (x.etl -> x.etl.003.log),
    or a file in its extraction folder. Otherwise it is left over from an
    earlier upload, and its subject, attachments and download folder describe
    a different log.
    """
    key = _log_key(log_path)
    uploaded = _log_key(session.get("uploaded_source_path"))
    if not key or not uploaded:
        return False
    if key == uploaded or key.startswith(uploaded + "."):
        return True
    stem, ext = os.path.splitext(uploaded)
    if ext in _ARCHIVE_EXTS:
        folder = os.path.join(os.path.dirname(uploaded),
                              os.path.basename(stem).replace(" ", "_"))
        return key.startswith(folder + os.sep)
    return False


def _remember_on_session(canonical: str, source: str, log_path="") -> str:
    """Write one answer onto the session. Returns the canonical number, or ""."""
    session[SESSION_SOURCE_KEY] = source

    def own_placeholder(previous: str) -> bool:
        # With no log to check against, keep the earlier behaviour.
        return check_ips_utils.is_synthetic_case_nbr(previous) and (
            not log_path or _placeholder_is_for(log_path))

    raw = session.get("case_context") or {}
    have_context = isinstance(raw, dict) and bool(raw)

    if source == SKIPPED:
        if have_context:
            context = CaseContext.from_session(raw)
            previous = str(context.case_nbr or "")
            if own_placeholder(previous):
                # A local_upload_/local_bsod_ run is this log's own run. Its
                # name stays: the results page looks the extraction index up
                # by it, and it is not a case number, so it does not attribute
                # the conversation to anything.
                context.case_ref_source = SKIPPED
            else:
                # A real case on the context belongs to a different log. Its
                # subject, description, attachments and backend id went with
                # it, and /set_log may already have primed the agent from
                # them; keeping them meant a conversation confirmed to have no
                # case could still discuss the previous one.
                if previous:
                    _forget_case_on_agents("")
                context = CaseContext(wifi_or_bt=context.wifi_or_bt,
                                      files_coexist=context.files_coexist,
                                      case_nbr="", case_ref_source=SKIPPED)
            session["case_context"] = context.to_session()
        return ""

    context = CaseContext.from_session(raw) if have_context else CaseContext()
    previous = str(context.case_nbr or "")
    same_case = bool(previous) and check_ips_utils.normalise_ips(previous) == canonical
    # A local_upload_/local_bsod_ placeholder is a name for this log's run,
    # so what hangs off it -- the extraction folder, the attachment list --
    # describes the log being answered about and stays. One left over from an
    # earlier upload does not: it is treated like any other case's context.
    was_placeholder = own_placeholder(previous)

    if previous and not same_case and not was_placeholder:
        # A different real case. Its subject, description, attachments and
        # backend id describe that case, not this one; keeping them meant the
        # next analysis discussed the new case number using the old case's
        # summary. Start from a clean context, keeping only what is about the
        # log rather than the case.
        context = CaseContext(wifi_or_bt=context.wifi_or_bt,
                              files_coexist=context.files_coexist)
        _forget_case_on_agents(canonical)

    context.case_nbr = canonical
    context.case_ref_source = source
    session["case_context"] = context.to_session()

    # The usual case is an agent primed at /set_log with no case at all, and
    # only a *different* case was being handled above -- so after the answer
    # the session and Gather knew the number while the agent answering the
    # chat still had none. Point this session's primed agents at it.
    for agent in _primed_agents():
        ctx = getattr(agent, "issue_context", None)
        if isinstance(ctx, dict) and ctx and not check_ips_utils.normalise_ips(ctx.get("case_nbr")):
            ctx["case_nbr"] = canonical

    # The extracted-file index is keyed by case number, and the results page
    # looks it up by the number on the context. Renaming one without the other
    # leaves that page with nothing to show. A respelling of the same case is
    # a rename, and so is naming a placeholder run -- it is this log's
    # results either way. Moving to a different real case is not.
    if previous and previous != canonical and (same_case or was_placeholder):
        results = app_config.download_results.pop(previous, None)
        if results is not None:
            app_config.download_results[canonical] = results

    return canonical


# The issue-context fields a chatbot agent is primed with that belong to the
# case rather than to the log.
_AGENT_CASE_FIELDS = ("subject", "description", "issue_type", "attachment_time")


# ---------------------------------------------------------------------------
# Filling in the case the user named
# ---------------------------------------------------------------------------
# A case typed into the case search is looked up in IPS and classified; a case
# typed into this prompt was recorded as a bare number. So a Send To or a
# local upload with a perfectly good case number still showed "Unclassified",
# the agent analysed without the customer's description, and Gather stored no
# subject or issue type. enrich_attached_case does the light half of the case
# search for it: the one fact_case row from Snowflake (no PDF, no attachments)
# and the same LLM classifier. Anything that fails leaves the answer exactly
# as recorded -- the lookup is an improvement, never a precondition.
_case_facts: dict = {}              # case number -> {"fields": tuple, "classification": dict}
_case_facts_lock = threading.Lock()
_ENRICH_TIMEOUT_SEC = 20
# A timed-out Future cannot stop a Snowflake call that is already running.
# Reusing one worker and admitting only one lookup at a time keeps a hung
# backend bounded to one thread and zero queued lookups instead of leaking a
# new worker on every case attachment. The slot is released by the worker when
# the backend call really finishes, not when the caller stops waiting.
_case_lookup_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ips-case-lookup")
_case_lookup_slot = threading.BoundedSemaphore(1)


def _lookup_case_facts(canonical: str) -> Optional[dict]:
    """Snowflake row + classification for a case, cached per process."""
    with _case_facts_lock:
        if canonical in _case_facts:
            return _case_facts[canonical]
    key = getattr(app_config, "key", None)
    passwd = getattr(key, "snowflake_passwd", None)
    if not passwd:
        return None
    llm = getattr(app_config, "llm_helper", None)

    if not _case_lookup_slot.acquire(blocking=False):
        print(f"[ips] case lookup for {canonical} skipped: another lookup is still running")
        return None

    def work():
        try:
            from services.case_info_service import CaseService
            fields = CaseService._get_case_info_from_snowflake(canonical, passwd)
            if not fields:
                return None
            _case_id, subject, _env, description, _backend, subcategory = fields
            usage = llm.empty_usage() if llm is not None else None
            classification = None
            if llm is not None:
                classification = llm.classify_issue(
                    {"case_nbr": canonical, "subject": subject or "",
                     "description": description or "", "subcategory": subcategory or ""},
                    usage_accumulator=usage)
            return {"fields": fields, "classification": classification,
                    "usage": usage, "model": getattr(llm, "model", "") if llm else ""}
        finally:
            _case_lookup_slot.release()

    try:
        future = _case_lookup_pool.submit(work)
    except Exception as e:
        _case_lookup_slot.release()
        print(f"[ips] case lookup for {canonical} skipped: {e}")
        return None
    try:
        facts = future.result(timeout=_ENRICH_TIMEOUT_SEC)
    except FutureTimeoutError as e:
        # cancel() succeeds only if the worker has not started. If it is
        # already running, work() owns the slot until the backend returns.
        if future.cancel():
            _case_lookup_slot.release()
        print(f"[ips] case lookup for {canonical} skipped: {e}")
        return None
    except Exception as e:
        print(f"[ips] case lookup for {canonical} skipped: {e}")
        return None
    if facts:
        with _case_facts_lock:
            _case_facts[canonical] = facts
        facts = dict(facts, fresh=True)
    return facts


def cached_classification(case_nbr) -> Optional[dict]:
    """The classification already looked up for this case, without a lookup."""
    canonical = check_ips_utils.normalise_ips(case_nbr)
    with _case_facts_lock:
        facts = _case_facts.get(canonical) if canonical else None
    classification = (facts or {}).get("classification")
    return classification if isinstance(classification, dict) else None


def enrich_attached_case(case_nbr) -> bool:
    """Fill the session's case from IPS and classify it, as the case search does.

    Only fields the context does not already hold are filled, so a case loaded
    through the case search is never overwritten. Returns whether anything was
    applied.
    """
    canonical = check_ips_utils.normalise_ips(case_nbr)
    if not canonical or current_case_nbr() != canonical:
        return False
    facts = _lookup_case_facts(canonical)
    if not facts:
        return False
    # The answer may have changed while the lookup ran.
    if current_case_nbr() != canonical:
        return False

    case_id, subject, env_detail, description, backend_id, subcategory = facts["fields"]
    context = CaseContext.from_session(session.get("case_context") or {})
    context.id = context.id or case_id
    context.subject = context.subject or subject
    context.description = context.description or description
    context.env_detail = context.env_detail or env_detail or {}
    context.backend_id = context.backend_id or backend_id or ""
    context.subcategory = context.subcategory or subcategory
    session["case_context"] = context.to_session()

    classification = facts.get("classification")
    if isinstance(classification, dict) and classification.get("issue_type"):
        session["classification"] = classification

    # The agent reads issue_context on every answer, so updating it is enough;
    # no re-priming, which would reset the conversation.
    for agent in _primed_agents():
        ctx = getattr(agent, "issue_context", None)
        # Primed agents only: an empty context is a boot-time template.
        if not isinstance(ctx, dict) or not ctx \
                or check_ips_utils.normalise_ips(ctx.get("case_nbr")) != canonical:
            continue
        ctx["subject"] = ctx.get("subject") or context.subject or ""
        ctx["description"] = ctx.get("description") or context.description or ""
        if isinstance(classification, dict) and classification.get("issue_type"):
            ctx["issue_type"] = classification["issue_type"]

    # The classification is an LLM call; its cost is recorded the way the
    # case search records its own, so it is not the one untracked spend.
    usage = facts.get("usage") or {}
    if facts.get("fresh") and int(usage.get("llm_calls") or 0) > 0:
        try:
            from services import gather_service
            gather_service.record_feature_usage(
                workflow_id=session.get("gather_workflow_id", ""),
                feature_code="issue_time_prepass",
                model=facts.get("model") or "",
                usage=usage,
                issue=context.to_dict(),
                domain=context.wifi_or_bt or "wifi",
                trigger="ips_prompt_case_lookup",
                status="success",
            )
        except Exception:
            pass
    return True


_CHATBOT_ROUTE_MODULES = (
    "blueprints.log_chatbot.log_chatbot_routes",
    "blueprints.bt_chatbot.bt_chatbot_routes",
    "blueprints.nw_analysis.nw_analysis_routes",
)


def _primed_agents() -> list:
    """Every agent that may be holding this session's case summary.

    The agents on app_config are boot-time templates. What /set_log actually
    primes is each chatbot's per-session clone in its module-level
    _chatbot_instances, keyed by session['chatbot_session_id'] -- the same map
    a finished background job's agent is adopted into. Clearing only the
    templates left the clone the next chat really uses still holding the old
    case. Modules are read from sys.modules rather than imported: one that
    has not been loaded has no agents to clear, and importing it here would
    drag a whole chatbot in from the service layer.
    """
    agents = []
    sid = session.get("chatbot_session_id")
    if sid:
        for module_name in _CHATBOT_ROUTE_MODULES:
            instances = getattr(sys.modules.get(module_name), "_chatbot_instances", None)
            if isinstance(instances, dict) and instances.get(sid) is not None:
                agents.append(instances[sid])
    for name in ("log_chatbot_agent", "bt_chatbot_agent", "nw_analysis_agent"):
        agent = getattr(app_config, name, None)
        if agent is not None and all(agent is not a for a in agents):
            agents.append(agent)
    return agents


def _forget_case_on_agents(canonical: str) -> None:
    """Drop a previous case's summary from any agent primed with it.

    /set_log primes the agent from the session before the prompt is answered,
    so an agent can still be holding the last case's subject and description
    when the user names a different one. Clearing the session alone leaves the
    agent discussing the new case from the old case's notes.
    """
    for agent in _primed_agents():
        ctx = getattr(agent, "issue_context", None)
        if not isinstance(ctx, dict):
            continue
        held = check_ips_utils.normalise_ips(ctx.get("case_nbr"))
        if held == canonical:
            continue
        for key in _AGENT_CASE_FIELDS:
            ctx.pop(key, None)
        ctx["case_nbr"] = canonical
    # The caches derived from the old case: its attachment time, the issue
    # time resolved from that, and the LLM's summary of its description. The
    # _carried_issue_time* keys are left alone -- they are the time the user
    # picked for this log, not something the old case supplied. Kept in step
    # with log_chatbot_routes._invalidate_issue_context_caches.
    for key in ("_attachment_time_cache", "_resolved_issue_time_cache",
                "_issue_ai_quick"):
        session.pop(key, None)


def attach(case_nbr: str, source: str, log_path="") -> str:
    """
    Record the user's answer on the session and against the log file.

    Returns the canonical case number, or "" for a skip. Raises ValueError when
    the answer is not usable, so the route can report it rather than storing a
    case number nobody can trace.
    """
    if source not in _SOURCES:
        raise ValueError(f"unknown case reference source: {source}")

    _note_log_path(log_path)
    key = _log_key(log_path)

    # Marked as answered only once there is an answer. Doing it here, before
    # the validation below, meant that typing something that is not a case
    # number opened the gate: the route reported 400, the set kept the log,
    # and needs_ips then found nothing in the store and returned False. The
    # conversation went ahead with case_ref_source still 'absent' -- the exact
    # silent gap this prompt exists to close -- and because the set is
    # module-level the log was never asked about again for the life of the
    # process.
    if source == SKIPPED:
        _mark_answered(key)
        remember_answer(log_path, "", SKIPPED)
        return _remember_on_session("", SKIPPED, log_path)

    canonical = check_ips_utils.normalise_ips(case_nbr)
    if not canonical:
        raise ValueError("Enter an 8-digit case number, for example 01010628.")

    _mark_answered(key)
    remember_answer(log_path, canonical, source)
    return _remember_on_session(canonical, source, log_path)
