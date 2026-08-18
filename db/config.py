"""Server-side PostgreSQL configuration shared by telemetry jobs.

The desktop client never imports this module.  Operators may provide either a
single ``TELEMETRY_DSN`` or separate ``TELEMETRY_DB_*`` values.  A local
``db/.env`` is loaded when python-dotenv is installed; that file is ignored by
Git and must never be packaged with IntelAvatar.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


ENV_FILE = Path(__file__).with_name(".env")


class DatabaseConfigError(RuntimeError):
    """The server-side database connection is not configured."""


def load_server_env() -> None:
    """Load ``db/.env`` without replacing values set by the service manager."""
    if not ENV_FILE.is_file():
        return
    try:
        from dotenv import load_dotenv
    except ImportError as exc:  # pragma: no cover - depends on deployment env
        raise DatabaseConfigError(
            "db/.env exists but python-dotenv is not installed; run "
            "'pip install -r db/requirements.txt'"
        ) from exc
    load_dotenv(ENV_FILE, override=False)


def _value(primary: str, legacy: str = "") -> str:
    value = os.environ.get(primary, "").strip()
    if not value and legacy:
        value = os.environ.get(legacy, "").strip()
    return value


def database_url(explicit: str | None = None):
    """Return a SQLAlchemy URL while keeping passwords out of string building.

    ``DB_*`` aliases are accepted for compatibility with the reference
    ``services/db_setup_guide`` branch.  New deployments should use the
    ``TELEMETRY_*`` names so unrelated applications cannot alter this job.
    """
    load_server_env()
    from sqlalchemy.engine import URL, make_url

    dsn = (explicit or _value("TELEMETRY_DSN")).strip()
    if dsn:
        return make_url(dsn)

    host = _value("TELEMETRY_DB_HOST", "DB_HOST")
    port_text = _value("TELEMETRY_DB_PORT", "DB_PORT") or "5432"
    database = _value("TELEMETRY_DB_NAME", "DB_NAME")
    user = _value("TELEMETRY_DB_USER", "DB_USER")
    password = _value("TELEMETRY_DB_PASSWORD", "DB_PASS") or None

    missing = [name for name, value in (
        ("TELEMETRY_DB_HOST", host),
        ("TELEMETRY_DB_NAME", database),
        ("TELEMETRY_DB_USER", user),
    ) if not value]
    if missing:
        raise DatabaseConfigError(
            "PostgreSQL is not configured. Copy db/.env.example to db/.env "
            "and set the credentials, or set TELEMETRY_DSN. Missing: "
            + ", ".join(missing)
        )
    try:
        port = int(port_text)
    except ValueError as exc:
        raise DatabaseConfigError("TELEMETRY_DB_PORT must be an integer") from exc

    return URL.create(
        "postgresql+psycopg",
        username=user,
        password=password,
        host=host,
        port=port,
        database=database,
    )


def masked_url(url: Any) -> str:
    """Render a SQLAlchemy URL without exposing its password."""
    return url.render_as_string(hide_password=True)


def gather_source(explicit: str | None = None) -> str | None:
    """Return an optional server-side override for the Gather share."""
    load_server_env()
    return explicit or _value("TELEMETRY_GATHER_SOURCE") or None
