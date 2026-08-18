"""
Telemetry ingestion API.

    uvicorn db.api:app --host 0.0.0.0 --port 8080          # real PostgreSQL
    TELEMETRY_SINK=memory uvicorn db.api:app --reload      # local, no database

One endpoint that matters: ``POST /v1/events``. It takes a batch from a client
outbox and returns the ids it has durably committed.

The response is shaped for the outbox's ``drain(sender)`` contract:
``acknowledged`` is exactly the set the client may mark as sent. A duplicate
counts as acknowledged — the event is already stored, so retrying it forever
would be the bug, not the fix.

Failure stance
--------------
A malformed event is reported per-event in ``rejected``; it never fails the
batch, because one bad event must not block the other ninety-nine behind it.
The endpoint only returns 5xx when the *sink* is unavailable, which is the one
case where the client genuinely should retry the whole batch later.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from db.ingest import EVENT_TYPES
from db.sinks import EventSink, MemorySink
from db.config import database_url

log = logging.getLogger("telemetry.api")

MAX_BATCH = 500
ENVIRONMENTS = ("production", "sim", "format_check", "dev")


# ------------------------------------------------------------------ models --
class EventIn(BaseModel):
    """One telemetry event as the client outbox stores it."""

    event_id: UUID
    event_type: Literal[EVENT_TYPES]  # type: ignore[valid-type]
    schema_version: int = Field(ge=1, le=999)
    environment: Literal["production", "sim", "format_check", "dev"]
    occurred_at: datetime
    user_name: str = Field(min_length=1, max_length=128)
    app_version: str = Field(default="", max_length=64)
    payload: dict[str, Any]
    source_ref: str = Field(default="", max_length=512)

    @field_validator("payload")
    @classmethod
    def _payload_not_empty(cls, v: dict) -> dict:
        if not v:
            raise ValueError("payload must not be empty")
        return v


class BatchIn(BaseModel):
    events: list[EventIn] = Field(min_length=1, max_length=MAX_BATCH)


class BatchOut(BaseModel):
    accepted: list[str]
    duplicate: list[str]
    rejected: dict[str, str]
    acknowledged: list[str]


class Health(BaseModel):
    status: str
    sink: str


# -------------------------------------------------------------- dependencies --
def _build_sink() -> EventSink:
    """Chosen once at import. `memory` is for local work and tests only."""
    kind = os.environ.get("TELEMETRY_SINK", "postgres").lower()
    if kind == "memory":
        log.warning("TELEMETRY_SINK=memory — events are held in RAM and lost on exit")
        return MemorySink()
    dsn = database_url()
    from sqlalchemy import create_engine
    from db.sinks import PostgresSink
    # pool_pre_ping: a pooled connection that died while idle should be
    # replaced silently rather than failing the first batch after a quiet night.
    return PostgresSink(create_engine(dsn, pool_pre_ping=True, pool_size=5,
                                      max_overflow=5, future=True))


_sink: EventSink | None = None


def get_sink() -> EventSink:
    global _sink
    if _sink is None:
        _sink = _build_sink()
    return _sink


def require_identity(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """
    Placeholder for corporate authentication.

    Deliberately minimal and deliberately not silent: with no token configured
    the service refuses to start accepting data rather than defaulting to open.
    Replace the body with the real Intel identity check before deployment; the
    signature is what the routes depend on.
    """
    expected = os.environ.get("TELEMETRY_TOKEN", "")
    if not expected:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "TELEMETRY_TOKEN is not configured; refusing to accept unauthenticated data",
        )
    if authorization != f"Bearer {expected}":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing token")
    return "authenticated"


# --------------------------------------------------------------------- app --
app = FastAPI(
    title="IntelAvatar telemetry ingestion",
    version="1.0.0",
    summary="Accepts batches from the client outbox and lands them in PostgreSQL.",
)


@app.post("/v1/events", response_model=BatchOut,
          status_code=status.HTTP_200_OK)
def post_events(
    batch: BatchIn,
    _: Annotated[str, Depends(require_identity)],
    sink: Annotated[EventSink, Depends(get_sink)],
) -> BatchOut:
    events = [e.model_dump(mode="json") for e in batch.events]
    try:
        result = sink.ingest(events)
    except Exception as e:
        # The sink is down. This is the one case where the client should keep
        # the whole batch and try again — so say so with a 503 and no partial
        # acknowledgement, rather than a 200 the client would read as success.
        log.exception("sink unavailable")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"ingestion unavailable: {type(e).__name__}",
        ) from e
    if result.rejected:
        log.warning("batch had %d rejected event(s): %s",
                    len(result.rejected), list(result.rejected)[:5])
    return BatchOut(
        accepted=result.accepted,
        duplicate=result.duplicate,
        rejected=result.rejected,
        acknowledged=result.acknowledged,
    )


@app.get("/healthz", response_model=Health)
def healthz() -> Health:
    """Liveness only — deliberately does not touch the database."""
    return Health(status="ok", sink=type(get_sink()).__name__)


@app.get("/readyz", response_model=Health)
def readyz(sink: Annotated[EventSink, Depends(get_sink)]) -> Health:
    if not sink.healthy():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "sink unhealthy")
    return Health(status="ready", sink=type(sink).__name__)
