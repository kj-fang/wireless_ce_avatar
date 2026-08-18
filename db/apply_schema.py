"""Safely initialise the IntelAvatar telemetry schema in PostgreSQL.

The default mode is a read-only preflight.  ``--apply`` is required before the
DDL file is executed, and the command refuses to touch a database where any of
the target schema names already exist.

Usage:
    python -m db.apply_schema
    python -m db.apply_schema --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from db.config import DatabaseConfigError, database_url, masked_url


DDL_PATH = Path(__file__).with_name("001_initial_schema.sql")
TARGET_SCHEMAS = ("bronze", "silver", "gold")


def _connect_kwargs(url) -> dict:
    kwargs = url.translate_connect_args(username="user", database="dbname")
    kwargs.update(dict(url.query))
    kwargs.setdefault("connect_timeout", 10)
    return kwargs


def _preflight(conn) -> tuple[int, str, str, bool, list[str]]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT current_setting('server_version_num')::int,
                   current_database(), current_user,
                   has_database_privilege(
                       current_user, current_database(), 'CREATE'
                   )
        """)
        version_num, database, user, can_create = cur.fetchone()
        cur.execute("""
            SELECT nspname
            FROM pg_namespace
            WHERE nspname = ANY(%s)
            ORDER BY nspname
        """, (list(TARGET_SCHEMAS),))
        existing = [row[0] for row in cur.fetchall()]
    return version_num, database, user, can_create, existing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=None,
                        help="override db/.env / TELEMETRY_DSN")
    parser.add_argument("--apply", action="store_true",
                        help="execute the DDL after all safety checks pass")
    args = parser.parse_args()

    try:
        url = database_url(args.dsn)
    except DatabaseConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    print(f"target: {masked_url(url)}")
    try:
        import psycopg
        with psycopg.connect(**_connect_kwargs(url)) as conn:
            version, database, user, can_create, existing = _preflight(conn)
            conn.rollback()  # make the default/preflight path explicitly read-only

            print(f"database: {database}")
            print(f"user: {user}")
            print(f"PostgreSQL version number: {version}")
            print(f"can create schema in database: {bool(can_create)}")
            print("existing target schemas: " +
                  (", ".join(existing) if existing else "none"))

            if version < 120000:
                print("ERROR: PostgreSQL 12+ is required", file=sys.stderr)
                return 1
            if not can_create:
                print("ERROR: current user cannot create schemas in this database",
                      file=sys.stderr)
                return 1
            if existing:
                print(
                    "ERROR: refusing to initialise because target schema names "
                    "already exist; inspect ownership and contents first",
                    file=sys.stderr,
                )
                return 1
            if not args.apply:
                print("preflight only — nothing written; rerun with --apply to initialise")
                return 0

            ddl = DDL_PATH.read_text(encoding="utf-8")
            with conn.cursor() as cur:
                cur.execute(ddl)
            conn.commit()
            print("schema initialised: bronze, silver, gold")
            return 0
    except Exception as exc:
        print(f"schema setup failed and was rolled back: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
