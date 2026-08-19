"""
Replay avatar_bronze_raw_event to settle rows that were never priced.

    python -m db.reprice --dry-run                           # db/.env
    python -m db.reprice                                     # apply
    python -m db.reprice --dsn postgresql+psycopg://...      # override

Why this exists
---------------
A model name the rate table does not recognise is recorded with its token
counts and ``cost_usd = NULL``, never a guessed figure. When the name is later
taught to ``configs.llm_pricing`` — an alias, a new model, a corrected rate key
— those rows can be settled from the usage that bronze already holds.

The one rule
------------
**Only NULL costs are filled. A settled figure is never recomputed.**

That is the whole difference between this and "recalculate spend with today's
rates", which would silently rewrite history and stop the numbers reconciling
with anything anyone quoted last quarter. Filling a gap is not restating a
fact, and the ``WHERE cost_usd IS NULL`` in every statement below is what keeps
the two apart. Running this twice is therefore a no-op.

Real case it was written for: the deployed client reported its model as
``claude-4-6-sonnet`` while the table was keyed ``claude-sonnet-4-6``, leaving
481,001 tokens across 26 invocations unpriced. The alias fixed new records;
this settles the ones already stored.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from typing import Optional

from sqlalchemy import text

from configs.llm_pricing import PRICING_VERSION, cost_for
from db.config import DatabaseConfigError, database_url

# Event types that carry billable usage, and where each lands in silver.
_TARGETS = {
    "turn.recorded": {
        "table": "avatar_silver_turn",
        "pk": "turn_id",
        "payload_id": "turn_id",
        "has_rate_columns": True,
    },
    "invocation.recorded": {
        "table": "avatar_silver_ai_invocation",
        "pk": "invocation_id",
        "payload_id": "invocation_id",
        "has_rate_columns": False,
    },
}


def _model_of(payload: dict) -> str:
    return str(payload.get("model") or payload.get("unpriced_model") or "")


def _plan(conn, event_type: str) -> list[tuple[str, str, dict]]:
    """Return (row_id, model, usage) for unpriced silver rows of this type."""
    t = _TARGETS[event_type]
    # Join bronze to silver rather than trusting either alone: bronze holds the
    # usage as reported, silver says whether the row is still unpriced.
    rows = conn.execute(text(f"""
        SELECT e.payload ->> :pid   AS row_id,
               e.payload            AS payload
        FROM avatar_bronze_raw_event e
        JOIN {t['table']} s ON s.{t['pk']} = (e.payload ->> :pid)::uuid
        WHERE e.event_type = :etype
          AND s.cost_usd IS NULL
    """), {"pid": t["payload_id"], "etype": event_type}).fetchall()

    out = []
    for row_id, payload in rows:
        payload = payload or {}
        model = _model_of(payload)
        usage = payload.get("usage") or {}
        if row_id and model and usage:
            out.append((row_id, model, usage))
    return out


def reprice(conn, *, dry_run: bool = False) -> dict:
    summary = {"examined": 0, "priced": 0, "still_unpriced": 0,
               "usd": Decimal("0"), "tokens": 0, "unknown_models": {}}

    for event_type, t in _TARGETS.items():
        for row_id, model, usage in _plan(conn, event_type):
            summary["examined"] += 1
            settled = cost_for(model, usage)
            if settled is None:
                summary["still_unpriced"] += 1
                summary["unknown_models"][model] = \
                    summary["unknown_models"].get(model, 0) + 1
                continue

            summary["priced"] += 1
            summary["usd"] += Decimal(str(settled["total"]))
            try:
                summary["tokens"] += int(usage.get("total_tokens") or 0)
            except (TypeError, ValueError):
                pass
            if dry_run:
                continue

            params = {
                "rid": row_id,
                "cost": settled["total"],
                "pv": settled["pricing_version"],
            }
            rate_sql = ""
            if t["has_rate_columns"]:
                rate_sql = (", rate_input_per_mtok = :ri"
                            ", rate_output_per_mtok = :ro")
                params["ri"] = settled["rate_input_per_mtok"]
                params["ro"] = settled["rate_output_per_mtok"]

            # `unpriced_model` must be cleared in the same statement: the CHECK
            # constraint requires (cost_usd IS NULL) = (unpriced_model <> ''),
            # so setting one without the other would be rejected.
            conn.execute(text(f"""
                UPDATE {t['table']}
                   SET cost_usd        = :cost,
                       unpriced_model  = '',
                       pricing_version = :pv
                       {rate_sql}
                 WHERE {t['pk']} = CAST(:rid AS uuid)
                   AND cost_usd IS NULL
            """), params)

    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", default=None,
                    help="override db/.env / TELEMETRY_DSN")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        dsn = database_url(args.dsn)
    except DatabaseConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    from sqlalchemy import create_engine
    engine = create_engine(dsn, pool_pre_ping=True, future=True)
    with engine.begin() as conn:
        s = reprice(conn, dry_run=args.dry_run)
        if args.dry_run:
            conn.rollback()

    print(f"pricing_version : {PRICING_VERSION}")
    print(f"unpriced rows   : {s['examined']}")
    print(f"now settled     : {s['priced']}  "
          f"({s['tokens']:,} tokens, ${s['usd']})")
    print(f"still unpriced  : {s['still_unpriced']}")
    for model, n in sorted(s["unknown_models"].items(), key=lambda kv: -kv[1]):
        print(f"    {model}  x{n}   <- add to configs/llm_pricing.py")
    if args.dry_run:
        print("dry run — rolled back")
    return 0


if __name__ == "__main__":
    sys.exit(main())
