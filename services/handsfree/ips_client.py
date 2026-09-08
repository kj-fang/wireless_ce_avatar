"""
IPS (Salesforce) client for the Handsfree Replyer.

Reuses CaseService's authenticated session machinery:
  - CaseService._get_vf_session()            SSO cookies (cached, 4h file TTL)
  - CaseService._build_salesforce_api_headers()  sid cookie -> Bearer token
  - CaseService._get_salesforce_api_version()

Adds the two capabilities the codebase lacked:
  1. find_new_cases(owner, ...)  — SOQL on the standard Case object.
  2. post_comment(...)           — REST insert into Core_IPS_Case_Comments__c
                                   (the same object the app already reads).

The "Private to Intel" flag's API field name is org-specific; it is
discovered once via describe_comment_fields() and persisted by the
orchestrator config. Until verified, post_comment refuses to run unless an
explicit field_map is supplied (PostUnsupported), so nothing is ever written
with guessed field semantics.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

import requests as _requests


class PostUnsupported(RuntimeError):
    """REST posting is not usable (permission denied / unverified field map)."""


@dataclass
class CaseRef:
    case_nbr: str
    case_id: str            # 18-char Salesforce id
    subject: str = ""
    status: str = ""
    created: str = ""       # ISO from Salesforce
    owner_name: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PostResult:
    ok: bool
    backend: str            # "rest" | "ui"
    comment_id: str = ""
    error: str = ""
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _soql_quote(value: str) -> str:
    """Escape a string for inclusion in a single-quoted SOQL literal."""
    return (value or "").replace("\\", "\\\\").replace("'", "\\'")


def build_new_cases_soql(owner_name: str, since_iso: Optional[str] = None,
                         limit: int = 20) -> str:
    """Pure builder (unit-testable, no network).

    since_iso: an ISO-8601 UTC datetime ('2026-07-15T00:00:00Z'). When None,
    uses the SOQL date literal TODAY (org-timezone day).
    """
    owner = _soql_quote(owner_name)
    if since_iso:
        # SOQL datetime literals are unquoted.
        if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})$",
                        since_iso):
            raise ValueError(f"since_iso is not ISO-8601: {since_iso!r}")
        created_clause = f"CreatedDate >= {since_iso}"
    else:
        created_clause = "CreatedDate = TODAY"
    return (
        "SELECT Id, CaseNumber, Subject, Status, CreatedDate, Owner.Name "
        "FROM Case "
        f"WHERE Owner.Name = '{owner}' AND {created_clause} "
        "ORDER BY CreatedDate DESC "
        f"LIMIT {int(limit)}"
    )


class IpsClient:
    """Thin REST layer over the existing CaseService Salesforce session."""

    COMMENT_OBJECT = "Core_IPS_Case_Comments__c"
    # Fields we know from the app's existing read path.
    FIELD_CASE_LOOKUP = "Core_IPS_Case__c"
    FIELD_RICH_BODY = "Core_IPS_Rich_Comment__c"

    def __init__(self):
        self._api_ver: Optional[str] = None

    # ------------------------------------------------------------------
    # auth plumbing (delegates to CaseService; lazy import keeps this module
    # importable in tests without Flask/selenium)
    # ------------------------------------------------------------------
    def _case_service(self):
        from services.case_info_service import CaseService
        return CaseService

    def _auth(self) -> tuple[_requests.Session, dict, str]:
        cs = self._case_service()
        vf_session = cs._get_vf_session()
        headers = cs._build_salesforce_api_headers(vf_session)
        if self._api_ver is None:
            self._api_ver = cs._get_salesforce_api_version(vf_session, headers)
        return vf_session, headers, self._api_ver

    def _reset_auth(self) -> None:
        """Drop the cached session so the next call re-authenticates (SSO)."""
        cs = self._case_service()
        with cs._vf_session_lock:
            cs._vf_session = None
        try:
            import os
            path = cs._cookie_file_path()
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
        self._api_ver = None

    def _request(self, method: str, path: str, *, params=None, payload=None,
                 timeout: int = 20, retry_auth: bool = True) -> _requests.Response:
        cs = self._case_service()
        vf_session, headers, ver = self._auth()
        url = f"{cs._SF_API_BASE_URL}{path.format(ver=ver)}"
        kwargs = dict(headers={**headers, "Content-Type": "application/json"},
                      params=params, timeout=timeout, proxies=vf_session.proxies)
        if payload is not None:
            kwargs["data"] = json.dumps(payload)
        resp = _requests.request(method, url, **kwargs)
        if resp.status_code == 401 and retry_auth:
            print("[handsfree.ips] 401 — re-authenticating via SSO and retrying once")
            self._reset_auth()
            return self._request(method, path, params=params, payload=payload,
                                 timeout=timeout, retry_auth=False)
        return resp

    # ------------------------------------------------------------------
    # detection
    # ------------------------------------------------------------------
    def find_new_cases(self, owner_name: str, since_iso: Optional[str] = None,
                       limit: int = 20) -> list[CaseRef]:
        """Cases assigned to `owner_name`, created today (or since `since_iso`)."""
        soql = build_new_cases_soql(owner_name, since_iso, limit)
        resp = self._request("GET", "/services/data/v{ver}/query",
                             params={"q": soql})
        resp.raise_for_status()
        out: list[CaseRef] = []
        for rec in resp.json().get("records", []):
            owner = (rec.get("Owner") or {}).get("Name", "")
            out.append(CaseRef(
                case_nbr=rec.get("CaseNumber", ""),
                case_id=rec.get("Id", ""),
                subject=rec.get("Subject") or "",
                status=rec.get("Status") or "",
                created=rec.get("CreatedDate") or "",
                owner_name=owner,
            ))
        return out

    def get_case_comments(self, case_id: str) -> list[dict]:
        """Existing comments on a case. Used to (a) detect our own prior post
        and (b) inspect how human-authored comments populate the visibility /
        type picklists, so a REST-posted comment can mirror them."""
        soql = (
            f"SELECT Id, CreatedDate, {self.FIELD_RICH_BODY}, "
            "Core_IPS_Public__c, Core_IPS_Case_Comment_Type__c, "
            "Core_IPS_Comment_Author_Type__c, Core_IPS_Case_Comment_Source__c "
            f"FROM {self.COMMENT_OBJECT} "
            f"WHERE {self.FIELD_CASE_LOOKUP}='{_soql_quote(case_id)}' "
            "ORDER BY CreatedDate DESC LIMIT 50"
        )
        resp = self._request("GET", "/services/data/v{ver}/query",
                             params={"q": soql})
        resp.raise_for_status()
        return resp.json().get("records", [])

    # ------------------------------------------------------------------
    # field discovery + posting
    # ------------------------------------------------------------------
    def describe_comment_fields(self) -> dict:
        """REST describe of the comment object. Returns
        {"createable": bool, "fields": [{name, label, type, createable}, ...],
         "boolean_candidates": [...], "body_candidates": [...]}.

        The orchestrator shows this to the engineer once so the
        Private-to-Intel flag can be identified and stored in config —
        we never guess a boolean field's meaning.
        """
        resp = self._request(
            "GET", "/services/data/v{ver}/sobjects/" + self.COMMENT_OBJECT + "/describe")
        resp.raise_for_status()
        desc = resp.json()
        fields = [
            {"name": f.get("name"), "label": f.get("label"),
             "type": f.get("type"), "createable": f.get("createable")}
            for f in desc.get("fields", [])
        ]
        boolean_candidates = [
            f for f in fields
            if f["type"] == "boolean" and f["createable"]
            and re.search(r"private|intel|internal|public|visib",
                          f"{f['name']} {f['label']}", re.IGNORECASE)
        ]
        body_candidates = [
            f for f in fields
            if f["createable"] and f["type"] in ("textarea", "string")
            and re.search(r"comment|body|rich", f"{f['name']} {f['label']}",
                          re.IGNORECASE)
        ]
        return {
            "object": self.COMMENT_OBJECT,
            "createable": desc.get("createable", False),
            "fields": fields,
            "boolean_candidates": boolean_candidates,
            "body_candidates": body_candidates,
        }

    @staticmethod
    def build_comment_payload(case_id: str, rich_body: str, *,
                              plain_body: str = "",
                              field_map: Optional[dict] = None,
                              private: bool = True) -> dict:
        """Pure payload builder (unit-testable, no network).

        field_map (from orchestrator config, verified by a human once):
            {"body_field":    "Core_IPS_Rich_Comment__c",
             "plain_field":   "Core_IPS_Comment__c",   # optional second body
             "private_field": "Core_IPS_Public__c",
             "private_value": false,     # value meaning Private-to-Intel —
                                         # NOTE: Core_IPS_Public__c is a
                                         # PUBLIC flag, so private == False
             "extra_fields":  {"Core_IPS_Case_Comment_Source__c": "..."}}
        """
        fm = field_map or {}
        body_field = fm.get("body_field") or IpsClient.FIELD_RICH_BODY
        payload = {IpsClient.FIELD_CASE_LOOKUP: case_id, body_field: rich_body}
        plain_field = fm.get("plain_field")
        if plain_field and plain_body:
            payload[plain_field] = plain_body
        private_field = fm.get("private_field")
        if private:
            if not private_field:
                raise PostUnsupported(
                    "No verified 'Private to Intel' field in config — run field "
                    "discovery (describe) and confirm the field before REST posting."
                )
            payload[private_field] = fm.get("private_value", True)
        else:
            # Public (customer-visible) comment: the flag must be set
            # EXPLICITLY to the inverse of the verified private value —
            # omitting it could silently post with the org default.
            private_value = fm.get("private_value", True)
            if not private_field or not isinstance(private_value, bool):
                raise PostUnsupported(
                    "Cannot guarantee public visibility — no verified boolean "
                    "privacy field in config; refusing to post a customer reply."
                )
            payload[private_field] = not private_value
            # Optional verified overrides for public posts (e.g. the org's
            # public comment-type picklist value).
            pub_extra = fm.get("public_extra_fields")
            if isinstance(pub_extra, dict):
                for k, v in pub_extra.items():
                    payload.setdefault(k, v)
        extra = fm.get("extra_fields")
        if isinstance(extra, dict):
            for k, v in extra.items():
                # Never stamp private-flavored metadata (e.g. comment type
                # 'Private to Intel') onto a public customer reply.
                if not private and isinstance(v, str) and "private" in v.lower():
                    continue
                payload.setdefault(k, v)
        return payload

    def post_comment(self, case_id: str, rich_body: str, *,
                     plain_body: str = "",
                     field_map: Optional[dict] = None,
                     private: bool = True) -> PostResult:
        """Insert a comment via REST. See build_comment_payload for the
        field_map contract. Raises PostUnsupported when the org denies the
        insert or no verified field_map exists — callers then fall back to
        the Selenium UI poster."""
        if not case_id or not rich_body.strip():
            return PostResult(ok=False, backend="rest", error="empty case_id/body")
        payload = self.build_comment_payload(
            case_id, rich_body, plain_body=plain_body,
            field_map=field_map, private=private)

        resp = self._request(
            "POST", "/services/data/v{ver}/sobjects/" + self.COMMENT_OBJECT + "/",
            payload=payload, timeout=30)
        if resp.status_code == 201:
            data = resp.json()
            return PostResult(ok=True, backend="rest",
                              comment_id=data.get("id", ""), detail=data)
        # 400 INVALID_FIELD / 403 insufficient access -> REST path unusable.
        try:
            err_json = resp.json()
        except Exception:
            err_json = {"raw": resp.text[:500]}
        err = f"HTTP {resp.status_code}: {json.dumps(err_json)[:500]}"
        if resp.status_code in (400, 403, 404):
            raise PostUnsupported(err)
        return PostResult(ok=False, backend="rest", error=err, detail={"status": resp.status_code})
