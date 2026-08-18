"""Read-only PostgreSQL connectivity, privilege, and size preflight.

Usage:
    python -m db.check_postgres
    python -m db.check_postgres --dsn postgresql+psycopg://...

No CREATE, INSERT, UPDATE, or DELETE statement is issued by this command.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import create_engine, text

from db.config import DatabaseConfigError, database_url, masked_url


def _pretty_bytes(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"


def inspect(conn) -> dict:
    server = conn.execute(text("""
        SELECT current_database(), current_user, current_schema(),
               current_setting('server_version'),
               current_setting('server_version_num')::int,
               pg_database_size(current_database()),
               has_database_privilege(current_user, current_database(), 'CREATE'),
               has_schema_privilege(current_user, 'public', 'CREATE')
    """)).one()

    schemas = conn.execute(text("""
        SELECT wanted.schema_name,
               EXISTS (SELECT 1 FROM pg_namespace n
                       WHERE n.nspname = wanted.schema_name) AS exists,
               COALESCE((
                   SELECT SUM(pg_total_relation_size(c.oid))::bigint
                   FROM pg_class c
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = wanted.schema_name
                     AND c.relkind IN ('r', 'm')
               ), 0) AS bytes
        FROM (VALUES ('bronze'), ('silver'), ('gold')) AS wanted(schema_name)
        ORDER BY wanted.schema_name
    """)).all()

    tables = conn.execute(text("""
        SELECT schemaname, relname,
               pg_total_relation_size(
                   format('%I.%I', schemaname, relname)::regclass
               )::bigint AS bytes
        FROM pg_stat_user_tables
        WHERE schemaname IN ('bronze', 'silver', 'gold')
        ORDER BY bytes DESC
        LIMIT 15
    """)).all()

    return {"server": server, "schemas": schemas, "tables": tables}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=None,
                        help="override db/.env / TELEMETRY_DSN")
    args = parser.parse_args()

    try:
        url = database_url(args.dsn)
    except DatabaseConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    print(f"target: {masked_url(url)}")
    try:
        engine = create_engine(url, pool_pre_ping=True,
                               connect_args={"connect_timeout": 10})
        with engine.connect() as conn:
            result = inspect(conn)
    except Exception as exc:
        print(f"connection/preflight failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if "engine" in locals():
            engine.dispose()

    server = result["server"]
    print(f"database: {server[0]}")
    print(f"user: {server[1]}")
    print(f"current schema: {server[2]}")
    print(f"PostgreSQL: {server[3]}")
    print(f"database size: {_pretty_bytes(server[5])} ({server[5]} bytes)")
    print(f"can create schema in database: {bool(server[6])}")
    print(f"can create table in public: {bool(server[7])}")
    if server[4] < 120000:
        print("ERROR: telemetry schema requires PostgreSQL 12+", file=sys.stderr)
        return 1

    print("telemetry schemas:")
    for name, exists, size in result["schemas"]:
        state = "exists" if exists else "missing"
        print(f"  {name:<8} {state:<7} {_pretty_bytes(size)}")
    if result["tables"]:
        print("largest telemetry tables:")
        for schema, table, size in result["tables"]:
            print(f"  {schema}.{table}: {_pretty_bytes(size)}")
    else:
        print("largest telemetry tables: none (schema not initialized)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
