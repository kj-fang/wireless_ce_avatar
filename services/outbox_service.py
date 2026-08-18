"""
Durable local event queue for telemetry, backed by SQLite.

Why this exists
---------------
Gather currently writes one JSON file per record straight to the SMB share, and
falls back to a local folder when the share is down. That works, but the queue
state lives in the filesystem: there is no record of what has been delivered,
no retry budget, and no way to tell a partial write from a finished one.

This module makes the queue explicit. An event is written to a local SQLite
database in one transaction and is not the caller's problem again. A background
sender drains it later, whenever the destination happens to be reachable.

What it deliberately does NOT decide
------------------------------------
Where the events eventually go. ``drain()`` takes a *sender* callable, so the
destination is a parameter rather than a design commitment: the share today, an
HTTP ingestion API later, both during a migration. Swapping transport does not
touch any code that produces events.

Delivery contract
-----------------
At-least-once. A batch that is sent but whose acknowledgement is lost will be
sent again after the process restarts. Every event carries a UUID ``event_id``
and ``enqueue`` is INSERT-OR-IGNORE on it, so the *receiver* is responsible for
the matching ``ON CONFLICT (event_id) DO NOTHING``. That pair is what turns
at-least-once transport into exactly-once effect.

Location
--------
Always a local disk path, never a UNC. SQLite's locking depends on the
filesystem honouring advisory locks and on WAL readers sharing a host; SMB
gives neither, and the documented failure mode is a corrupt database rather
than an error. One outbox per machine, per user profile.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

try:
    from configs.global_configs import app_config
except Exception:                                    # pragma: no cover
    app_config = None                                # type: ignore[assignment]

try:
    from configs.version import __version__ as APP_VERSION
except Exception:                                    # pragma: no cover
    APP_VERSION = ""

OUTBOX_SCHEMA_VERSION = 1

# Retry pacing. Capped so a machine that has been offline for a week does not
# come back and immediately hammer the destination, and jittered so twenty
# laptops waking from sleep together do not retry in lockstep.
_BACKOFF_BASE_SEC = 30
_BACKOFF_CAP_SEC = 3600
_MAX_ATTEMPTS = 24

# A queue that never drains must not grow without bound on a user's disk.
_MAX_PENDING = 50_000
# Delivered rows are kept briefly so a duplicate enqueue is still recognised
# and so support can see what left the machine.
_SENT_RETENTION_SEC = 14 * 24 * 3600

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None
_db_path: Optional[Path] = None

STATUS_PENDING = "pending"
STATUS_SENT = "sent"
STATUS_DEAD = "dead"        # attempts exhausted; kept for inspection, never retried

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    event_type      TEXT NOT NULL,
    schema_version  INTEGER NOT NULL,
    environment     TEXT NOT NULL,
    user_name       TEXT NOT NULL,
    app_version     TEXT NOT NULL,
    occurred_at     TEXT NOT NULL,
    enqueued_at     TEXT NOT NULL,
    payload         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error      TEXT NOT NULL DEFAULT '',
    sent_at         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_claimable
    ON events (status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_events_sent_at
    ON events (status, sent_at);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# ---------------------------------------------------------------- location --
def default_db_path() -> Path:
    """Local-disk location of the outbox. Never returns a UNC path."""
    base = getattr(app_config, "avatarfiles_dir", None) if app_config else None
    root = Path(base) if base else Path.home() / "IntelAvatar_files"
    return root / "telemetry" / "outbox.sqlite3"


def _is_unc(p: Path) -> bool:
    s = str(p)
    return s.startswith("\\\\") or s.startswith("//")


# ---------------------------------------------------------------- lifecycle --
def init(db_path: Optional[Path] = None) -> Path:
    """Open (creating if needed) the outbox. Safe to call repeatedly."""
    global _conn, _db_path
    with _lock:
        path = Path(db_path) if db_path else default_db_path()
        if _conn is not None and _db_path == path:
            return path
        if _is_unc(path):
            raise ValueError(
                f"outbox must live on local disk, got a UNC path: {path}. "
                "SQLite locking is not reliable over SMB."
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        conn = sqlite3.connect(str(path), timeout=30, isolation_level=None,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # WAL lets the sender read while a route thread writes. synchronous=NORMAL
        # is the right trade here: a machine that loses power may lose the last
        # few telemetry events, which is not worth an fsync on every enqueue.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(OUTBOX_SCHEMA_VERSION),),
        )
        _conn, _db_path = conn, path
        return path


def close() -> None:
    global _conn, _db_path
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            finally:
                _conn, _db_path = None, None


@contextmanager
def _tx():
    """One immediate transaction. Serialised in-process by the module lock."""
    with _lock:
        if _conn is None:
            init()
        assert _conn is not None
        _conn.execute("BEGIN IMMEDIATE")
        try:
            yield _conn
        except Exception:
            _conn.execute("ROLLBACK")
            raise
        else:
            _conn.execute("COMMIT")


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ------------------------------------------------------------------ produce --
def enqueue(
    event_type: str,
    payload: dict,
    *,
    event_id: Optional[str] = None,
    environment: str = "production",
    schema_version: int = OUTBOX_SCHEMA_VERSION,
    occurred_at: Optional[str] = None,
    user_name: str = "",
    app_version: Optional[str] = None,
) -> Optional[str]:
    """
    Durably record one event and return its id.

    Returns the id whether the row was new or already present — a caller
    retrying with the same ``event_id`` is a no-op, not an error. Returns None
    only if the event could not be stored, and never raises: telemetry must not
    be able to break the feature it is measuring.
    """
    try:
        eid = str(event_id or uuid.uuid4())
        body = json.dumps(payload, ensure_ascii=False, default=str)
        with _tx() as conn:
            pending = conn.execute(
                "SELECT COUNT(*) FROM events WHERE status = ?", (STATUS_PENDING,)
            ).fetchone()[0]
            if pending >= _MAX_PENDING:
                # Shed the oldest rather than the newest: recent events describe
                # the state the machine is actually in.
                conn.execute(
                    "DELETE FROM events WHERE event_id IN ("
                    "  SELECT event_id FROM events WHERE status = ?"
                    "  ORDER BY enqueued_at LIMIT ?)",
                    (STATUS_PENDING, max(1, pending - _MAX_PENDING + 1)),
                )
            conn.execute(
                "INSERT INTO events ("
                " event_id, event_type, schema_version, environment, user_name,"
                " app_version, occurred_at, enqueued_at, payload"
                ") VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(event_id) DO NOTHING",
                (eid, str(event_type), int(schema_version), str(environment),
                 str(user_name), str(app_version if app_version is not None else APP_VERSION),
                 str(occurred_at or _now_iso()), _now_iso(), body),
            )
        return eid
    except Exception as e:                            # pragma: no cover
        print(f"[outbox] enqueue failed ({event_type}): {e}")
        return None


# ------------------------------------------------------------------ consume --
def claim(limit: int = 100) -> list[dict]:
    """Return up to ``limit`` events that are due to be sent, oldest first."""
    with _tx() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE status = ? AND next_attempt_at <= ? "
            "ORDER BY enqueued_at LIMIT ?",
            (STATUS_PENDING, time.time(), int(limit)),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["payload"] = json.loads(d["payload"])
        except Exception:
            pass
        out.append(d)
    return out


def mark_sent(event_ids: Sequence[str]) -> int:
    if not event_ids:
        return 0
    with _tx() as conn:
        cur = conn.executemany(
            "UPDATE events SET status = ?, sent_at = ?, last_error = '' "
            "WHERE event_id = ? AND status = ?",
            [(STATUS_SENT, _now_iso(), e, STATUS_PENDING) for e in event_ids],
        )
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(event_ids)


def mark_failed(event_ids: Sequence[str], error: str = "") -> None:
    """Record a delivery failure and schedule the next attempt."""
    if not event_ids:
        return
    now = time.time()
    with _tx() as conn:
        for eid in event_ids:
            row = conn.execute(
                "SELECT attempts FROM events WHERE event_id = ?", (eid,)
            ).fetchone()
            if row is None:
                continue
            attempts = int(row["attempts"]) + 1
            if attempts >= _MAX_ATTEMPTS:
                conn.execute(
                    "UPDATE events SET status = ?, attempts = ?, last_error = ? "
                    "WHERE event_id = ?",
                    (STATUS_DEAD, attempts, str(error)[:500], eid),
                )
                continue
            delay = min(_BACKOFF_BASE_SEC * (2 ** (attempts - 1)), _BACKOFF_CAP_SEC)
            delay *= 0.5 + random.random()            # jitter: 50–150% of the step
            conn.execute(
                "UPDATE events SET attempts = ?, next_attempt_at = ?, last_error = ? "
                "WHERE event_id = ?",
                (attempts, now + delay, str(error)[:500], eid),
            )


Sender = Callable[[list[dict]], Iterable[str]]
"""Takes a batch of events, returns the ids it durably accepted.

Anything not returned is retried. Raising is also fine — the whole batch is
then retried — but returning the accepted subset lets one poison event fail
without holding up the rest of the batch.
"""


def drain(sender: Sender, *, batch: int = 100, max_batches: int = 20) -> dict:
    """
    Hand pending events to ``sender`` until it stops accepting or the queue empties.

    The sender decides the destination; this function only owns the retry
    bookkeeping. Never raises.
    """
    sent = failed = batches = 0
    for _ in range(max_batches):
        events = claim(batch)
        if not events:
            break
        batches += 1
        ids = [e["event_id"] for e in events]
        try:
            accepted = list(sender(events) or [])
        except Exception as e:
            mark_failed(ids, f"{type(e).__name__}: {e}")
            failed += len(ids)
            break
        ok = [i for i in ids if i in set(accepted)]
        bad = [i for i in ids if i not in set(accepted)]
        if ok:
            mark_sent(ok)
            sent += len(ok)
        if bad:
            mark_failed(bad, "not acknowledged by sender")
            failed += len(bad)
            break
    purged = purge()
    return {"sent": sent, "failed": failed, "batches": batches, "purged": purged}


# ---------------------------------------------------------------- housekeep --
def purge(older_than_sec: int = _SENT_RETENTION_SEC) -> int:
    """Drop delivered rows past the retention window. Dead rows are kept."""
    cutoff = datetime.fromtimestamp(time.time() - older_than_sec, timezone.utc)
    cutoff_iso = cutoff.astimezone().isoformat(timespec="seconds")
    with _tx() as conn:
        cur = conn.execute(
            "DELETE FROM events WHERE status = ? AND sent_at != '' AND sent_at < ?",
            (STATUS_SENT, cutoff_iso),
        )
        return cur.rowcount or 0


def stats() -> dict:
    """Queue depth by status, plus the oldest pending event — for a health line."""
    with _tx() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) n FROM events GROUP BY status"
        ).fetchall()
        oldest = conn.execute(
            "SELECT MIN(enqueued_at) FROM events WHERE status = ?", (STATUS_PENDING,)
        ).fetchone()[0]
        dead = conn.execute(
            "SELECT COUNT(*) FROM events WHERE status = ?", (STATUS_DEAD,)
        ).fetchone()[0]
    by = {r["status"]: r["n"] for r in rows}
    return {
        "db_path": str(_db_path or ""),
        "pending": by.get(STATUS_PENDING, 0),
        "sent": by.get(STATUS_SENT, 0),
        "dead": dead,
        "oldest_pending": oldest or "",
        "schema_version": OUTBOX_SCHEMA_VERSION,
    }
