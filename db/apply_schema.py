"""Safely initialise the IntelAvatar telemetry schema in PostgreSQL.

The default mode is a read-only preflight.  ``--apply`` is required before the
DDL file is executed, and the command refuses to run if any object it would
create already exists.

Usage:
    python -m db.apply_schema
    python -m db.apply_schema --apply
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from db.config import DatabaseConfigError, database_url, masked_url


DDL_PATH = Path(__file__).with_name("001_initial_schema.sql")

# The objects live in `public` with an avatar_<layer>_ prefix rather than in
# their own schemas, because the role has CREATE in `public` but not at
# database level. `public` here is shared with an unrelated system, so the
# collision check below is against object names, not schema names.
OBJECT_RE = re.compile(r"CREATE (?:TABLE|VIEW|INDEX)\s+(\w+)", re.IGNORECASE)


def planned_objects() -> list[str]:
    return sorted(set(OBJECT_RE.findall(DDL_PATH.read_text(encoding="utf-8"))))


def _connect_kwargs(url) -> dict:
    kwargs = url.translate_connect_args(username="user", database="dbname")
    kwargs.update(dict(url.query))
    kwargs.setdefault("connect_timeout", 10)
    return kwargs


def _preflight(conn, wanted: list[str]):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT current_setting('server_version_num')::int,
                   current_database(), current_user,
                   has_schema_privilege(current_user, 'public', 'CREATE')
        """)
        version_num, database, user, can_create = cur.fetchone()
        # Compare against every relation in public, not just tables: an index
        # shares the same namespace as a table in PostgreSQL, so a clash there
        # would fail the DDL just as hard.
        cur.execute("""
            SELECT c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname = ANY(%s)
            ORDER BY c.relname
        """, (wanted,))
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

    wanted = planned_objects()
    print(f"target: {masked_url(url)}")
    try:
        import psycopg
        with psycopg.connect(**_connect_kwargs(url)) as conn:
            version, database, user, can_create, existing = _preflight(conn, wanted)
            conn.rollback()  # make the default/preflight path explicitly read-only

            print(f"database: {database}")
            print(f"user: {user}")
            print(f"PostgreSQL version number: {version}")
            print(f"can create in public: {bool(can_create)}")
            print(f"objects this file would create: {len(wanted)}")
            print("already present: " +
                  (", ".join(existing) if existing else "none"))

            if version < 120000:
                print("ERROR: PostgreSQL 12+ is required", file=sys.stderr)
                return 1
            if not can_create:
                print("ERROR: current user cannot create objects in public",
                      file=sys.stderr)
                return 1
            if existing:
                print(
                    "ERROR: refusing to initialise because these object names "
                    "already exist in public; inspect ownership and contents "
                    "first",
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
            print(f"schema initialised: {len(wanted)} objects created in public")
            return 0
    except Exception as exc:
        print(f"schema setup failed and was rolled back: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
