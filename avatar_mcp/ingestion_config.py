"""
Ingestion Config
================
Centralized configuration for the MCP Report Ingestion Endpoint (Streamable
HTTP). Self-contained from the main Avatar app's config
(configs/global_configs.py) - see data/adr/0003 for why this is a separate
entry point.

Settings are loaded from ingestion_config.json (next to this file, or next to
the packaged EXE). If it doesn't exist, it is created with placeholder
defaults on first run - fill in real API keys / SMTP settings before use.
"""

import json
import os
import sys

if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(BASE_DIR, 'ingestion_config.json')

_DEFAULTS: dict = {
    # Network
    'host': '0.0.0.0',
    'port': 8443,
    'public_url': 'http://avatar-ingestion.local:8443',  # used only for OAuth-shaped metadata; not dialed

    # Auth: caller_id -> static API key. Callers send `Authorization: Bearer <key>`.
    'api_keys': {
        'ci-runner-01': 'CHANGE-ME-generate-with-secrets.token_urlsafe-32',
    },

    # Upload / job limits
    'max_upload_bytes': 5 * 1024 ** 3,       # 5 GiB safety cap
    'max_concurrent_analyses': 3,
    'job_result_ttl_seconds': 3600,          # 1 hour

    # Where uploaded archives are staged before/while analyzed
    'upload_staging_dir': '',                # empty -> a temp dir is used

    # SMTP (see services/email_notify_service.py; ported from LabHerald Agent Bridge)
    'email_from': 'agent-admin-robot@intel.com',
    'smtp_host': 'smtp.intel.com',
    'smtp_port': 25,
    'smtp_use_tls': False,
    'smtp_username': '',
    'smtp_password': '',
    'email_dry_run': False,
}

if not os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as _f:
        json.dump(_DEFAULTS, _f, indent=4, ensure_ascii=False)
    _cfg = _DEFAULTS.copy()
else:
    with open(CONFIG_FILE, 'r', encoding='utf-8') as _f:
        _cfg = json.load(_f)
    # Fill in any keys added in later versions without overwriting existing ones.
    for _k, _v in _DEFAULTS.items():
        _cfg.setdefault(_k, _v)

HOST: str = _cfg['host']
PORT: int = _cfg['port']
PUBLIC_URL: str = _cfg['public_url']
API_KEYS: dict = _cfg['api_keys']
MAX_UPLOAD_BYTES: int = _cfg['max_upload_bytes']
MAX_CONCURRENT_ANALYSES: int = _cfg['max_concurrent_analyses']
JOB_RESULT_TTL_SECONDS: int = _cfg['job_result_ttl_seconds']
UPLOAD_STAGING_DIR: str = _cfg['upload_staging_dir'] or None

EMAIL_FROM: str = _cfg['email_from']
SMTP_HOST: str = _cfg['smtp_host']
SMTP_PORT: int = _cfg['smtp_port']
SMTP_USE_TLS: bool = _cfg['smtp_use_tls']
SMTP_USERNAME: str = _cfg['smtp_username']
SMTP_PASSWORD: str = _cfg['smtp_password']
EMAIL_DRY_RUN: bool = _cfg['email_dry_run']
