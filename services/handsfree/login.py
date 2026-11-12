"""
IPS sign-in of the reviewer who uses the Handsfree client on this PC.

The client talks to IPS (Salesforce) with the SSO session CaseService keeps
for this Windows account (cookie file, else a silent Chrome SSO, else the
saved --sso-login profile). That session has an identity: the Salesforce
user whose name appears as the author of every comment posted from this
PC. This module resolves that identity once, keeps it for the page, and
lets the page ask for a visible sign-in when the silent one fails.

    status()                 -> {"status": signed_out|signing_in|signed_in|failed, ...}
    start_sign_in(interactive) -> status() right after the worker started
    current_identity()       -> {"user_id", "name", "email", "username"} or raises

Nothing here is a credential store: the sign-in itself is Intel SSO in
Chrome, and only the resulting identity (name, e-mail, user id) is kept.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Optional

_lock = threading.Lock()
_state: dict = {"status": "signed_out", "identity": None, "error": "",
                "checked_at": "", "interactive": False}
_worker: Optional[threading.Thread] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def status() -> dict:
    with _lock:
        out = dict(_state)
        out["identity"] = dict(_state["identity"]) if _state["identity"] else None
        return out


def _whoami() -> dict:
    from .ips_client import IpsClient
    return IpsClient().whoami()


def _sign_in(interactive: bool) -> dict:
    """Resolve the IPS identity, signing in first when there is no usable
    session. interactive=True opens a visible Chrome (Intel SSO) when the
    silent sign-in fails — only meaningful on the reviewer's own PC."""
    try:
        return _whoami()
    except Exception as silent_err:
        if not interactive:
            raise
        print(f"[handsfree.login] silent sign-in failed ({type(silent_err).__name__}: "
              f"{silent_err}); opening a visible Chrome for the reviewer to sign in")
        from services.case_info_service import CaseService
        CaseService.interactive_sso_login()
        return _whoami()


def _run(interactive: bool) -> None:
    try:
        ident = _sign_in(interactive)
        with _lock:
            _state.update(status="signed_in", identity=ident, error="",
                          checked_at=_now())
        print(f"[handsfree.login] signed in to IPS as {ident.get('name')!r} "
              f"<{ident.get('email')}>")
    except Exception as e:
        with _lock:
            _state.update(status="failed", identity=None,
                          error=f"{type(e).__name__}: {e}", checked_at=_now())
        print(f"[handsfree.login] IPS sign-in failed: {type(e).__name__}: {e}")


def start_sign_in(interactive: bool = False) -> dict:
    """Start a sign-in in the background unless one is running. The silent
    variant is what the page runs on load; the interactive one is the
    'Sign in to IPS' button."""
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _state.update(status="signing_in", error="", interactive=bool(interactive))
            _worker = threading.Thread(target=_run, args=(bool(interactive),),
                                       daemon=True, name="handsfree-ips-login")
            _worker.start()
    return status()


def current_identity() -> dict:
    """The identity of the IPS session right now, for an action that posts
    in the reviewer's name. Always re-checked against IPS (one GET): the
    cached page state could be hours old and the session renewed since."""
    ident = _whoami()
    with _lock:
        _state.update(status="signed_in", identity=ident, error="", checked_at=_now())
    return dict(ident)
