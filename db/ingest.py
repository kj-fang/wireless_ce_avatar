"""
Idempotent ingestion: telemetry events -> avatar_bronze_raw_event + silver entities.

Contract
--------
``ingest_batch`` returns the event ids it has durably committed. That is exactly
the shape ``services.outbox_service.drain`` expects from a sender, so the client
outbox can point straight at this with no adapter.

Delivery is at-least-once, so every event in this module arrives twice sooner or
later. Nothing here may depend on being called once:

  * bronze insert is ON CONFLICT (event_id) DO NOTHING. If it reports no row,
    the event was already applied and silver is not touched again.
  * silver upserts are keyed on the client's own UUIDs, which are stable across
    retries.
  * bronze and silver are written in ONE transaction, so silver can never hold
    a row whose source event is missing.

The one upsert that is not last-write-wins is the turn status, which is ranked:
a route reports the outcome it saw, and the usage worker settles tokens
milliseconds later with its own default of "completed". Whichever lands second
must not undo the other.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

from db import models as m

# Namespace for deterministic ids. Fixed forever: change it and every derived
# id changes, which would re-insert the entire backfill.
NAMESPACE = uuid.UUID("6f1d3a52-8b0c-5e7d-9a41-2c8e5b7d0f33")

EVENT_TYPES = (
    "workflow.started",
    "conversation.started",
    "turn.recorded",
    "invocation.recorded",
    "attachment.audited",
    "feedback.submitted",
)

# Silver rows must be written parents-first. A batch that arrives in file order
# puts a conversation before the workflow it references and the foreign key
# fails, so a producer that cannot guarantee order sorts by this instead.
EVENT_ORDER = {t: i for i, t in enumerate(EVENT_TYPES)}

# Download outcomes as the client has spelled them across versions. Records on
# the share carry `already_exists` and `success`, which the column CHECK does
# not accept; the canonical spellings are `already_present` and `succeeded`.
# Mapping happens here rather than in the writer so that historical JSON on the
# share is never rewritten.
_ATTACHMENT_STATUS = {
    "": "not_attempted",
    "not_attempted": "not_attempted",
    "pending": "not_attempted",
    "skipped": "not_attempted",
    "succeeded": "succeeded",
    "success": "succeeded",
    "ok": "succeeded",
    "downloaded": "succeeded",
    "already_present": "already_present",
    "already_exists": "already_present",
    "exists": "already_present",
    "failed": "failed",
    "failure": "failed",
    "error": "failed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
}


def normalise_attachment_status(raw: Any) -> str:
    """
    Translate a client download outcome to the vocabulary the column accepts.

    An unrecognised word raises rather than defaulting. Folding it into
    "not_attempted" would record a file that was in fact downloaded as one that
    was never tried, which is worse than a visible rejection: the rejection is
    retried on the next run once the word is added here, the silent mislabel is
    never noticed.
    """
    s = str(raw or "").strip().lower()
    try:
        return _ATTACHMENT_STATUS[s]
    except KeyError:
        raise ValueError(f"unknown attachment download_status: {s!r}") from None


@dataclass
class IngestResult:
    accepted: list[str] = field(default_factory=list)   # committed now
    duplicate: list[str] = field(default_factory=list)  # already present
    rejected: dict[str, str] = field(default_factory=dict)  # id -> reason

    @property
    def acknowledged(self) -> list[str]:
        """Ids the client may mark sent — a duplicate is a success, not a retry."""
        return self.accepted + self.duplicate


def derive_event_id(*parts: Any) -> uuid.UUID:
    """
    Deterministic event id from stable business fields.

    Used by the legacy importer, where records predate event ids. It must be a
    pure function of the record: a random id per import run would defeat the
    deduplication and re-insert every legacy row on every reconciliation pass.
    """
    key = "|".join("" if p is None else str(p) for p in parts)
    return uuid.uuid5(NAMESPACE, key)


def _as_uuid(value: Any) -> Optional[uuid.UUID]:
    if isinstance(value, uuid.UUID):
        return value
    s = str(value or "").strip()
    if not s:
        return None
    try:
        return uuid.UUID(s)
    except ValueError:
        # Legacy ids that are not UUIDs (SIM-bt-1008992, local_upload_...) still
        # need a stable key rather than being dropped on the floor.
        return uuid.uuid5(NAMESPACE, s)


def _as_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _as_dec(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------- dimensions --
class _DimCache:
    """Per-batch memo so a 100-event batch does not re-query the same user 100x."""

    def __init__(self) -> None:
        self.user: dict[str, int] = {}
        self.case: dict[str, int] = {}
        self.model: dict[str, int] = {}
        self.agent: dict[str, int] = {}
        self.tech: dict[str, int] = {}
        self.feature: dict[str, int] = {}


def _get_user_id(conn: Connection, cache: _DimCache, name: str,
                 seen_at: datetime) -> int:
    name = (name or "unknown").strip() or "unknown"
    if name in cache.user:
        return cache.user[name]
    stmt = (
        pg_insert(m.app_user)
        .values(user_name=name, first_seen=seen_at, last_seen=seen_at)
        .on_conflict_do_update(
            index_elements=[m.app_user.c.user_name],
            # A dimension upsert must widen the window, never reset it: events
            # arrive out of order, so a late backfill must not move last_seen
            # backwards.
            set_={
                "first_seen": func.least(m.app_user.c.first_seen, seen_at),
                "last_seen": func.greatest(m.app_user.c.last_seen, seen_at),
            },
        )
        .returning(m.app_user.c.user_id)
    )
    uid = conn.execute(stmt).scalar_one()
    cache.user[name] = uid
    return uid


def _get_case_id(conn: Connection, cache: _DimCache, case_nbr: str,
                 seen_at: datetime, subject: str = "", issue_type: str = "",
                 technology_id: int = 0) -> Optional[int]:
    case_nbr = (case_nbr or "").strip()
    if not case_nbr:
        return None
    if case_nbr in cache.case:
        return cache.case[case_nbr]
    stmt = (
        pg_insert(m.support_case)
        .values(case_nbr=case_nbr, subject=subject or "",
                issue_type=issue_type or "", technology_id=technology_id,
                first_seen=seen_at, last_seen=seen_at)
        .on_conflict_do_update(
            index_elements=[m.support_case.c.case_nbr],
            set_={
                "first_seen": func.least(m.support_case.c.first_seen, seen_at),
                "last_seen": func.greatest(m.support_case.c.last_seen, seen_at),
                # Only fill a blank; a later event with no subject must not
                # erase one an earlier event supplied.
                "subject": func.coalesce(
                    func.nullif(m.support_case.c.subject, ""), subject or ""),
                "issue_type": func.coalesce(
                    func.nullif(m.support_case.c.issue_type, ""), issue_type or ""),
            },
        )
        .returning(m.support_case.c.case_id)
    )
    cid = conn.execute(stmt).scalar_one()
    cache.case[case_nbr] = cid
    return cid


def _get_model_id(conn: Connection, cache: _DimCache, model_name: str,
                  rate_in: Any = None, rate_out: Any = None,
                  pricing_version: str = "") -> Optional[int]:
    model_name = (model_name or "").strip()
    if not model_name:
        return None
    if model_name in cache.model:
        return cache.model[model_name]
    stmt = (
        pg_insert(m.llm_model)
        .values(model_name=model_name, rate_input_per_mtok=_as_dec(rate_in),
                rate_output_per_mtok=_as_dec(rate_out),
                pricing_version=pricing_version or "")
        .on_conflict_do_update(
            index_elements=[m.llm_model.c.model_name],
            set_={"model_name": model_name},   # no-op update so RETURNING fires
        )
        .returning(m.llm_model.c.model_id)
    )
    mid = conn.execute(stmt).scalar_one()
    cache.model[model_name] = mid
    return mid


def _lookup_id(conn: Connection, cache: dict[str, int], table, code_col,
               id_col, code: str, default: int = 0) -> int:
    code = (code or "").strip().lower()
    if not code:
        return default
    if code in cache:
        return cache[code]
    val = conn.execute(select(id_col).where(code_col == code)).scalar()
    val = default if val is None else val
    cache[code] = val
    return val


def _parent_id(conn: Connection, pk_col, value: Optional[uuid.UUID]):
    """
    Return ``value`` only if that parent row already exists, else None.

    Every one of these references is nullable by design, and an incremental run
    routinely sees a child whose parent file fell outside the scan window —
    a session touched today whose workflow was written three weeks ago. Losing
    the whole event over a link that the schema says is optional trades a
    complete row for no row at all. A later run that does carry the parent fills
    the link in, because the upserts coalesce it.
    """
    if value is None:
        return None
    return conn.execute(select(pk_col).where(pk_col == value)).scalar()


def _agent_id(conn, cache, code):
    return _lookup_id(conn, cache.agent, m.agent, m.agent.c.code,
                      m.agent.c.agent_id, code)


def _tech_id(conn, cache, code):
    return _lookup_id(conn, cache.tech, m.technology, m.technology.c.code,
                      m.technology.c.technology_id, code)


def _feature_id(conn: Connection, cache: _DimCache, code: str) -> Optional[int]:
    code = (code or "").strip()
    if not code:
        return None
    if code in cache.feature:
        return cache.feature[code]
    stmt = (
        pg_insert(m.feature)
        .values(code=code, label=code.replace("_", " ").title())
        .on_conflict_do_update(index_elements=[m.feature.c.code],
                               set_={"code": code})
        .returning(m.feature.c.feature_id)
    )
    fid = conn.execute(stmt).scalar_one()
    cache.feature[code] = fid
    return fid


# ------------------------------------------------------------ silver upserts --
def _upsert_workflow(conn: Connection, cache: _DimCache, ev: dict) -> None:
    p = ev["payload"]
    at = _as_dt(ev["occurred_at"]) or datetime.now(timezone.utc)
    tech = _tech_id(conn, cache, p.get("case_domain") or p.get("wifi_or_bt"))
    uid = _get_user_id(conn, cache, ev.get("user_name", ""), at)
    cid = _get_case_id(conn, cache, p.get("case_nbr", ""), at,
                       p.get("subject", ""), p.get("issue_type", ""), tech)
    stmt = pg_insert(m.workflow).values(
        workflow_id=_as_uuid(p["workflow_id"]), user_id=uid, case_id=cid,
        technology_id=tech, environment=ev["environment"],
        app_version=ev.get("app_version", ""), started_at=at, updated_at=at,
    ).on_conflict_do_update(
        index_elements=[m.workflow.c.workflow_id],
        set_={
            "updated_at": func.greatest(m.workflow.c.updated_at, at),
            "started_at": func.least(m.workflow.c.started_at, at),
            "case_id": func.coalesce(m.workflow.c.case_id, cid),
            # A concrete technology may replace "unknown", never the reverse.
            "technology_id": case(
                (m.workflow.c.technology_id == 0, tech),
                else_=m.workflow.c.technology_id),
        },
    )
    conn.execute(stmt)


def _upsert_conversation(conn: Connection, cache: _DimCache, ev: dict) -> None:
    p = ev["payload"]
    at = _as_dt(ev["occurred_at"]) or datetime.now(timezone.utc)
    tech = _tech_id(conn, cache, p.get("case_domain") or p.get("wifi_or_bt"))
    ag = _agent_id(conn, cache, p.get("agent_domain") or p.get("domain"))
    uid = _get_user_id(conn, cache, ev.get("user_name", ""), at)
    cid = _get_case_id(conn, cache, p.get("case_nbr", ""), at,
                       p.get("subject", ""), p.get("issue_type", ""), tech)
    src = p.get("case_ref_source") or ("explicit" if cid else "absent")
    if cid is None:
        src = "absent"        # the CHECK constraint enforces this pairing
    wf = _parent_id(conn, m.workflow.c.workflow_id, _as_uuid(p.get("workflow_id")))
    stmt = pg_insert(m.conversation).values(
        conversation_id=_as_uuid(p["conversation_id"]),
        workflow_id=wf,
        http_session_id=str(p.get("session_id") or ""),
        user_id=uid, case_id=cid, case_ref_source=src,
        agent_id=ag, technology_id=tech,
        environment=ev["environment"], app_version=ev.get("app_version", ""),
        started_at=at, updated_at=at,
        issue_time=_as_dt(p.get("issue_time")),
        issue_window_minutes=p.get("issue_time_window_minutes"),
    ).on_conflict_do_update(
        index_elements=[m.conversation.c.conversation_id],
        set_={
            "updated_at": func.greatest(m.conversation.c.updated_at, at),
            "started_at": func.least(m.conversation.c.started_at, at),
            "workflow_id": func.coalesce(m.conversation.c.workflow_id, wf),
            "issue_time": func.coalesce(m.conversation.c.issue_time,
                                        _as_dt(p.get("issue_time"))),
            "agent_id": case((m.conversation.c.agent_id == 0, ag),
                             else_=m.conversation.c.agent_id),
            "technology_id": case((m.conversation.c.technology_id == 0, tech),
                                  else_=m.conversation.c.technology_id),
        },
    )
    conn.execute(stmt)


def _upsert_turn(conn: Connection, cache: _DimCache, ev: dict) -> None:
    p = ev["payload"]
    at = _as_dt(ev["occurred_at"]) or datetime.now(timezone.utc)
    usage = p.get("usage") or {}
    status = str(p.get("status") or "started").lower()
    if status not in m.TURN_STATUS_RANK:
        status = "started"
    mid = _get_model_id(conn, cache, p.get("model", ""),
                        p.get("rate_input_per_mtok"),
                        p.get("rate_output_per_mtok"),
                        p.get("pricing_version", ""))
    cost = _as_dec(p.get("cost_usd"))
    unpriced = str(p.get("unpriced_model") or "")
    # The CHECK constraint requires these two to agree; make that true here
    # rather than letting Postgres reject the whole batch.
    if cost is None and not unpriced and p.get("model"):
        unpriced = str(p.get("model"))
    if cost is not None:
        unpriced = ""

    ins = pg_insert(m.turn).values(
        turn_id=_as_uuid(p["turn_id"]),
        conversation_id=_as_uuid(p["conversation_id"]),
        seq=p.get("seq"), status=status,
        error_code=str(p.get("error_code") or ""), model_id=mid,
        input_tokens=_as_int(usage.get("input_tokens")),
        cache_read_tokens=_as_int(usage.get("cache_read_tokens")),
        cache_write_tokens=_as_int(usage.get("cache_write_tokens")),
        output_tokens=_as_int(usage.get("output_tokens")),
        cost_usd=cost, unpriced_model=unpriced,
        rate_input_per_mtok=_as_dec(p.get("rate_input_per_mtok")),
        rate_output_per_mtok=_as_dec(p.get("rate_output_per_mtok")),
        pricing_version=str(p.get("pricing_version") or ""),
        latency_ms=p.get("latency_ms"), started_at=at,
        settled_at=at if cost is not None or usage else None,
    )

    # Rank comparison against the row already stored. The more specific outcome
    # wins whichever thread lands second.
    rank_new = select(m.turn_status.c.rank).where(
        m.turn_status.c.status == ins.excluded.status).scalar_subquery()
    rank_old = select(m.turn_status.c.rank).where(
        m.turn_status.c.status == m.turn.c.status).scalar_subquery()
    keeps_new = rank_new >= rank_old

    stmt = ins.on_conflict_do_update(
        index_elements=[m.turn.c.turn_id],
        set_={
            "status": case((keeps_new, ins.excluded.status), else_=m.turn.c.status),
            "error_code": case((keeps_new, ins.excluded.error_code),
                               else_=m.turn.c.error_code),
            # Tokens are settled once and never revised; GREATEST keeps a
            # zero-valued duplicate from wiping a settled figure.
            "input_tokens": func.greatest(m.turn.c.input_tokens,
                                          ins.excluded.input_tokens),
            "cache_read_tokens": func.greatest(m.turn.c.cache_read_tokens,
                                               ins.excluded.cache_read_tokens),
            "cache_write_tokens": func.greatest(m.turn.c.cache_write_tokens,
                                                ins.excluded.cache_write_tokens),
            "output_tokens": func.greatest(m.turn.c.output_tokens,
                                           ins.excluded.output_tokens),
            "cost_usd": func.coalesce(ins.excluded.cost_usd, m.turn.c.cost_usd),
            "model_id": func.coalesce(ins.excluded.model_id, m.turn.c.model_id),
            # Once anything is priced the row is priced; the marker must clear
            # or the CHECK constraint fails.
            "unpriced_model": case(
                (func.coalesce(ins.excluded.cost_usd,
                               m.turn.c.cost_usd).is_(None),
                 func.coalesce(func.nullif(ins.excluded.unpriced_model, ""),
                               m.turn.c.unpriced_model)),
                else_=""),
            "latency_ms": func.coalesce(ins.excluded.latency_ms,
                                        m.turn.c.latency_ms),
            "settled_at": func.coalesce(m.turn.c.settled_at,
                                        ins.excluded.settled_at),
        },
    )
    conn.execute(stmt)


def _upsert_invocation(conn: Connection, cache: _DimCache, ev: dict) -> None:
    p = ev["payload"]
    at = _as_dt(ev["occurred_at"]) or datetime.now(timezone.utc)
    usage = p.get("usage") or {}
    cost = _as_dec(p.get("cost_usd"))
    unpriced = str(p.get("unpriced_model") or "")
    if cost is None and not unpriced and p.get("model"):
        unpriced = str(p.get("model"))
    if cost is not None:
        unpriced = ""
    stmt = pg_insert(m.ai_invocation).values(
        invocation_id=_as_uuid(p["invocation_id"]),
        workflow_id=_as_uuid(p["workflow_id"]),
        conversation_id=_parent_id(conn, m.conversation.c.conversation_id,
                                   _as_uuid(p.get("conversation_id"))),
        feature_id=_feature_id(conn, cache, p.get("feature_code", "unknown")),
        agent_id=_agent_id(conn, cache, p.get("agent_domain") or p.get("domain")),
        model_id=_get_model_id(conn, cache, p.get("model", "")),
        input_tokens=_as_int(usage.get("input_tokens")),
        cache_read_tokens=_as_int(usage.get("cache_read_tokens")),
        cache_write_tokens=_as_int(usage.get("cache_write_tokens")),
        output_tokens=_as_int(usage.get("output_tokens")),
        cost_usd=cost, unpriced_model=unpriced,
        pricing_version=str(p.get("pricing_version") or ""),
        status=str(p.get("status") or "success"),
        error_code=str(p.get("error_code") or ""),
        latency_ms=p.get("latency_ms"), occurred_at=at,
    ).on_conflict_do_nothing(index_elements=[m.ai_invocation.c.invocation_id])
    # An invocation is a completed fact by the time it is reported; there is no
    # later write that could revise it, so a duplicate is simply ignored.
    conn.execute(stmt)


def _upsert_attachment(conn: Connection, cache: _DimCache, ev: dict) -> None:
    p = ev["payload"]
    at = _as_dt(ev["occurred_at"]) or datetime.now(timezone.utc)
    wf = _as_uuid(p["workflow_id"])

    # The claim is a property of the workflow, not of any one file, so it lands
    # on the workflow row. Only write it when this event actually carries a
    # source: a re-audit that never re-ran the AI would otherwise blank out a
    # verdict an earlier run had established.
    source = str(p.get("declaration_source") or "")
    if source:
        conn.execute(
            m.workflow.update()
            .where(m.workflow.c.workflow_id == wf)
            .values(
                declared_attached=p.get("declared_attached"),
                declaration_source=source,
                declaration_confidence=str(p.get("declaration_confidence") or ""),
                declaration_conflict=bool(p.get("declaration_conflict") or False),
            )
        )

    for item in p.get("files") or []:
        # Deterministic per (workflow, file) so replaying the audit does not
        # duplicate rows.
        aid = derive_event_id("attachment", wf, item.get("name", ""))
        ins = pg_insert(m.attachment_event).values(
            attachment_event_id=aid, workflow_id=wf,
            declared_name=str(item.get("name") or ""),
            log_family=str(item.get("log_family") or ""),
            was_selected=bool(item.get("selected")),
            download_status=normalise_attachment_status(item.get("status")),
            byte_size=item.get("bytes"),
            latency_ms=item.get("latency_ms"),
            attempt_count=item.get("attempt_count"),
            error_code=str(item.get("error_code") or ""),
            occurred_at=at,
        )
        conn.execute(ins.on_conflict_do_update(
            index_elements=[m.attachment_event.c.attachment_event_id],
            set_={
                # Selection is sticky: a re-audit that no longer lists the file
                # must not claim it was never selected.
                "was_selected": m.attachment_event.c.was_selected
                                | ins.excluded.was_selected,
                "download_status": ins.excluded.download_status,
                "log_family": ins.excluded.log_family,
                "byte_size": func.coalesce(ins.excluded.byte_size,
                                             m.attachment_event.c.byte_size),
                "latency_ms": func.coalesce(ins.excluded.latency_ms,
                                              m.attachment_event.c.latency_ms),
                "attempt_count": func.coalesce(ins.excluded.attempt_count,
                                                 m.attachment_event.c.attempt_count),
                "error_code": ins.excluded.error_code,
            },
        ))


def _upsert_feedback(conn: Connection, cache: _DimCache, ev: dict) -> None:
    p = ev["payload"]
    at = _as_dt(ev["occurred_at"]) or datetime.now(timezone.utc)
    uid = _get_user_id(conn, cache, ev.get("user_name", ""), at)
    cid = _get_case_id(conn, cache, p.get("case_nbr", ""), at)
    stmt = pg_insert(m.feedback_event).values(
        feedback_event_id=_as_uuid(p["feedback_event_id"]),
        conversation_id=_parent_id(conn, m.conversation.c.conversation_id,
                                   _as_uuid(p.get("conversation_id"))),
        turn_id=_parent_id(conn, m.turn.c.turn_id, _as_uuid(p.get("turn_id"))),
        workflow_id=_parent_id(conn, m.workflow.c.workflow_id,
                               _as_uuid(p.get("workflow_id"))),
        case_id=cid, user_id=uid, environment=ev["environment"],
        submitted_at=at,
    ).on_conflict_do_nothing(
        index_elements=[m.feedback_event.c.feedback_event_id])
    conn.execute(stmt)


_HANDLERS = {
    "workflow.started": _upsert_workflow,
    "conversation.started": _upsert_conversation,
    "turn.recorded": _upsert_turn,
    "invocation.recorded": _upsert_invocation,
    "attachment.audited": _upsert_attachment,
    "feedback.submitted": _upsert_feedback,
}


# ------------------------------------------------------------------ entry --
def ingest_batch(conn: Connection, events: Iterable[dict]) -> IngestResult:
    """
    Apply a batch of events. One transaction, managed by the caller.

    The caller must commit; returning ids the caller then fails to commit would
    tell the client to drop events that were never stored.
    """
    result = IngestResult()
    cache = _DimCache()
    for ev in events:
        eid = str(ev.get("event_id") or "")
        etype = str(ev.get("event_type") or "")
        if etype not in _HANDLERS:
            result.rejected[eid] = f"unknown event_type: {etype}"
            continue
        # A savepoint per event, so a failure rolls back this event's bronze row
        # along with its half-applied silver writes. Without it the bronze row
        # would survive the batch, the retry would be answered "duplicate", and
        # the event would be lost for good — the worst possible outcome for a
        # design whose whole point is not losing events.
        try:
            with conn.begin_nested():
                occurred = _as_dt(ev.get("occurred_at")) or datetime.now(timezone.utc)
                landed = conn.execute(
                    pg_insert(m.raw_event).values(
                        event_id=_as_uuid(eid), event_type=etype,
                        schema_version=int(ev.get("schema_version") or 1),
                        environment=str(ev.get("environment") or "production"),
                        occurred_at=occurred,
                        received_at=datetime.now(timezone.utc),
                        user_name=str(ev.get("user_name") or ""),
                        app_version=str(ev.get("app_version") or ""),
                        payload=ev.get("payload") or {},
                        source_ref=str(ev.get("source_ref") or ""),
                    ).on_conflict_do_nothing(index_elements=[m.raw_event.c.event_id])
                    .returning(m.raw_event.c.event_id)
                ).scalar()

                if landed is None:
                    # Already applied in an earlier batch. Silver is untouched —
                    # replaying the handler would be safe but pointless work.
                    result.duplicate.append(eid)
                    continue

                _HANDLERS[etype](conn, cache, ev)
            result.accepted.append(eid)
        except Exception as e:
            # One malformed event must not cost the other 99 in the batch.
            result.rejected[eid] = f"{type(e).__name__}: {e}"
            # The savepoint rollback also undid any dimension rows this event
            # created, so their cached ids now point at nothing. Keeping them
            # would hand a stale user_id to the next event and fail its foreign
            # key for reasons that have nothing to do with it.
            cache = _DimCache()
    return result
