"""
Empty the telemetry tables and the sync watermark, so a load can be redone.

    python -m db.reset_telemetry                 # report only, touches nothing
    python -m db.reset_telemetry --apply         # truncate + reset watermark
    python -m db.reset_telemetry --watermark-only --apply

Why this exists
---------------
A first load that ends with rejected events leaves silver holding a subset of
the share, and the numbers on top of it are wrong in a way no query reveals.
Reloading over the top does not fix it: bronze already holds those event ids,
so a re-run is answered "duplicate" and silver is never revisited. The only way
back to a clean load is to drop bronze as well, which is what this does.

What it deliberately does not touch
-----------------------------------
The database is shared. Every table named here is verified to start with
``avatar_`` before a single statement runs, so a typo cannot reach anything
else, and nothing is dropped — the schema and its grants survive.

Reference data (technology, agent, feature, turn_status) is also left alone.
Those rows were inserted by the schema, not by ingestion; truncating them would
silently delete the seed values and every later lookup would resolve to
"unknown".
"""

from __future__ import annotations

import argparse
import sys

from db.config import DatabaseConfigError, database_url, masked_url

# Order is irrelevant — they go in one TRUNCATE — but the grouping documents
# what is being thrown away.
TARGET_TABLES = (
    # bronze: the audit trail and the incremental cursor
    "avatar_bronze_raw_event",
    "avatar_bronze_sync_state",
    # silver entities
    "avatar_silver_feedback_event",
    "avatar_silver_attachment_event",
    "avatar_silver_ai_invocation",
    "avatar_silver_turn",
    "avatar_silver_conversation",
    "avatar_silver_workflow",
    # silver dimensions that ingestion creates rows in; identities restart so a
    # reload produces the same keys it would have on a fresh database
    "avatar_silver_app_user",
    "avatar_silver_support_case",
    "avatar_silver_llm_model",
    "avatar_silver_log_file",
)

# Seeded by 001_initial_schema.sql. Listed so the exclusion is a decision on
# the page rather than an omission.
PRESERVED_TABLES = (
    "avatar_silver_technology",
    "avatar_silver_agent",
    "avatar_silver_feature",
    "avatar_silver_turn_status",
)


def _counts(conn, tables) -> dict[str, int]:
    from sqlalchemy import text
    out: dict[str, int] = {}
    for t in tables:
        exists = conn.execute(
            text("SELECT to_regclass(:t)"), {"t": t}).scalar()
        out[t] = (conn.execute(text(f"SELECT count(*) FROM {t}")).scalar()
                  if exists else -1)
    return out


def _report(label: str, counts: dict[str, int]) -> None:
    print(f"\n{label}")
    for t, n in counts.items():
        print(f"  {t:<38} {'(missing)' if n < 0 else n}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", default=None, help="override db/.env / TELEMETRY_DSN")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete; without it this only reports")
    ap.add_argument("--watermark-only", action="store_true",
                    help="reset the incremental cursor, keep the data")
    args = ap.parse_args()

    # A prefix a caller cannot override. Everything else in this database
    # belongs to someone else.
    bad = [t for t in TARGET_TABLES if not t.startswith("avatar_")]
    if bad:
        print(f"refusing to run: non-telemetry table in target list: {bad}",
              file=sys.stderr)
        return 2

    try:
        dsn = database_url(args.dsn)
    except DatabaseConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    from sqlalchemy import create_engine, text
    print(f"database: {masked_url(dsn)}")
    engine = create_engine(dsn, pool_pre_ping=True, future=True)

    targets = ("avatar_bronze_sync_state",) if args.watermark_only else TARGET_TABLES

    with engine.begin() as conn:
        before = _counts(conn, targets)
        _report("before:", before)
        print("\npreserved (reference data, never truncated):")
        for t in PRESERVED_TABLES:
            print(f"  {t}")

        if not args.apply:
            print("\nreport only — re-run with --apply to delete")
            return 0

        present = [t for t, n in before.items() if n >= 0]
        if not present:
            print("\nnothing to do: no telemetry tables found")
            return 0

        # One statement, one transaction: either the whole set is empty or none
        # of it is. A half-cleared database is the state this tool exists to
        # get out of.
        conn.execute(text(
            f"TRUNCATE {', '.join(present)} RESTART IDENTITY"))
        _report("after:", _counts(conn, targets))

    print("\ndone — next `python -m db.sync_share` reads the share from scratch")
    return 0


if __name__ == "__main__":
    sys.exit(main())
