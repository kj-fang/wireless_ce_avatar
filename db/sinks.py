"""
Where accepted events go.

The API talks to an ``EventSink``, never to SQLAlchemy directly. That is what
lets the HTTP contract be developed and tested here, on a machine with no
PostgreSQL, while the SQL itself is verified on the machine that has one.

Two implementations, one contract:

  * ``PostgresSink``  — the real thing. One transaction per batch.
  * ``MemorySink``    — a stand-in with the same *observable* behaviour:
                        same deduplication, same result shape. It is not a
                        database and makes no attempt to be one; it exists so
                        the request/response contract can be exercised.

The stand-in deliberately does NOT emulate SQL. Anything that depends on real
constraints — CHECK violations, foreign keys, the ranked turn-status upsert,
numeric money arithmetic — is invisible to it and must be verified against
PostgreSQL. Passing tests here mean the HTTP layer is right, nothing more.
"""

from __future__ import annotations

from typing import Iterable, Protocol, runtime_checkable

from db.ingest import IngestResult, ingest_batch


@runtime_checkable
class EventSink(Protocol):
    def ingest(self, events: Iterable[dict]) -> IngestResult:
        """Durably accept a batch. Must have committed before returning."""
        ...

    def healthy(self) -> bool:
        ...


class PostgresSink:
    """One transaction per batch: bronze and silver commit together or not at all."""

    def __init__(self, engine) -> None:
        self._engine = engine

    def ingest(self, events: Iterable[dict]) -> IngestResult:
        # engine.begin() commits on clean exit. Returning ids the caller then
        # failed to commit would tell the client to drop events that were never
        # stored, so the commit must happen before this function returns.
        with self._engine.begin() as conn:
            return ingest_batch(conn, list(events))

    def healthy(self) -> bool:
        from sqlalchemy import text
        try:
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False


class MemorySink:
    """
    In-memory stand-in with the same dedup semantics as the real sink.

    Used for local development and for the HTTP contract tests. Keeps every
    event so a test can assert on what the API actually forwarded.
    """

    def __init__(self) -> None:
        self.events: dict[str, dict] = {}
        self.batches: int = 0
        self.fail_with: Exception | None = None

    def ingest(self, events: Iterable[dict]) -> IngestResult:
        if self.fail_with is not None:
            raise self.fail_with
        self.batches += 1
        result = IngestResult()
        for ev in events:
            eid = str(ev.get("event_id") or "")
            etype = str(ev.get("event_type") or "")
            if not eid:
                result.rejected[eid] = "missing event_id"
                continue
            from db.ingest import EVENT_TYPES
            if etype not in EVENT_TYPES:
                result.rejected[eid] = f"unknown event_type: {etype}"
                continue
            if eid in self.events:
                # Same answer the real sink gives for a replayed event: already
                # applied, and the client may stop retrying it.
                result.duplicate.append(eid)
                continue
            self.events[eid] = ev
            result.accepted.append(eid)
        return result

    def healthy(self) -> bool:
        return True
