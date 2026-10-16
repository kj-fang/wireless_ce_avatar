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
    closed: object = None   # True/False when known (IsClosed), None otherwise

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


# Hosts of Intel's own Salesforce org — the only places the authenticated
# session (Bearer header + sid cookie) may ever be sent: intel.my.salesforce
# .com (REST), intel.file.force.com (rtaImage servlet), intel--c.vf.force.com
# (Visualforce), intel.lightning.force.com. Any other *.force.com /
# *.salesforce.com host is somebody else's tenant — a customer can embed an
# image hosted on their own org and would receive our credentials.
SF_TENANT = "intel"
_TENANT_HOST_RE = re.compile(
    r"^" + re.escape(SF_TENANT) + r"(?:--c)?(?:\.[a-z0-9-]+)?\.(?:force|salesforce)\.com$")


def is_tenant_url(url: str) -> bool:
    """HTTPS URL on one of the org's own Salesforce hosts (no userinfo, no
    other port) — the precondition for attaching the session credentials."""
    m = re.match(r"^https://([a-z0-9.-]+)(?::443)?(?:[/?#]|$)", str(url or "").strip(),
                 re.IGNORECASE)
    return bool(m and _TENANT_HOST_RE.match(m.group(1).lower()))


def build_new_cases_soql(owner_name: str, since_iso: Optional[str] = None,
                         limit: int = 20, created_date: str = "TODAY") -> str:
    """Pure builder (unit-testable, no network).

    since_iso: an ISO-8601 UTC datetime ('2026-07-15T00:00:00Z'). When None,
    uses `created_date` (TODAY by default; YESTERDAY for targeted testing).
    """
    owner = _soql_quote(owner_name)
    if since_iso:
        # SOQL datetime literals are unquoted.
        if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})$",
                        since_iso):
            raise ValueError(f"since_iso is not ISO-8601: {since_iso!r}")
        created_clause = f"CreatedDate >= {since_iso}"
    else:
        date_literal = str(created_date or "TODAY").strip().upper()
        if date_literal not in {"TODAY", "YESTERDAY"}:
            raise ValueError("created_date must be TODAY or YESTERDAY")
        created_clause = f"CreatedDate = {date_literal}"
    return (
        "SELECT Id, CaseNumber, Subject, Status, CreatedDate, Owner.Name "
        "FROM Case "
        f"WHERE Owner.Name = '{owner}' AND {created_clause} "
        "ORDER BY CreatedDate DESC "
        f"LIMIT {int(limit)}"
    )


# Comment author types written by the customer side (Snowflake / REST share
# the picklist): an "update from the customer" is a comment by one of these.
CUSTOMER_AUTHOR_TYPES = ("Partner", "Customer", "Partner - Agent", "Partner - DFAE")


def build_open_cases_soql(owner_name: str, limit: int = 200) -> str:
    """Open cases owned by `owner_name`, most recently modified first."""
    return (
        "SELECT Id, CaseNumber, Subject, Status, CreatedDate, Owner.Name "
        "FROM Case "
        f"WHERE Owner.Name = '{_soql_quote(owner_name)}' AND IsClosed = false "
        "ORDER BY LastModifiedDate DESC "
        f"LIMIT {int(limit)}"
    )


def build_customer_updates_soql(case_ids: list[str], since_iso: str,
                                limit: Optional[int] = None) -> str:
    """Customer-side comments on the given cases created after `since_iso`
    (ISO-8601 UTC). Bounded by case ids AND time so the huge comment table
    stays selective. No LIMIT by default: the caller pages through every
    result (a truncated result would silently drop the cases whose updates
    sort last, and the scan window would advance past them)."""
    if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})$",
                    since_iso or ""):
        raise ValueError(f"since_iso is not ISO-8601: {since_iso!r}")
    ids = ", ".join(f"'{_soql_quote(i)}'" for i in case_ids)
    authors = ", ".join(f"'{_soql_quote(a)}'" for a in CUSTOMER_AUTHOR_TYPES)
    return (
        "SELECT Core_IPS_Case__c, CreatedDate, Core_IPS_Comment_Author_Type__c "
        "FROM Core_IPS_Case_Comments__c "
        f"WHERE Core_IPS_Case__c IN ({ids}) AND CreatedDate > {since_iso} "
        f"AND Core_IPS_Comment_Author_Type__c IN ({authors}) "
        "ORDER BY CreatedDate DESC"
        + (f" LIMIT {int(limit)}" if limit else "")
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
    
    def _query_cases(self, soql: str) -> list[CaseRef]:
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

    def find_new_cases(self, owner_name: str, since_iso: Optional[str] = None,
                       limit: int = 20,
                       created_date: str = "TODAY") -> list[CaseRef]:
        """Cases assigned to `owner_name`, created on a SOQL date literal or since `since_iso`."""
        return self._query_cases(
            build_new_cases_soql(owner_name, since_iso, limit, created_date))

    def list_case_views(self, developer_name: Optional[str] = None) -> list[dict]:
        """Case list views visible to the current login, as
        [{"developer_name", "label", "id"}] in Salesforce order. Pages through
        /sobjects/Case/listviews; with `developer_name` it stops at the first
        page containing that view (the lookup list_view_cases needs)."""
        out: list[dict] = []
        url: Optional[str] = "/services/data/v{ver}/sobjects/Case/listviews"
        params: Optional[dict] = {"limit": 200}
        while url:
            resp = self._request("GET", url, params=params)
            resp.raise_for_status()
            d = resp.json()
            for lv in d.get("listviews", []):
                if not lv.get("id"):
                    continue
                out.append({"developer_name": str(lv.get("developerName") or ""),
                            "label": str(lv.get("label") or lv.get("developerName") or ""),
                            "id": str(lv.get("id"))})
                if developer_name and out[-1]["developer_name"] == developer_name:
                    return out
            url, params = d.get("nextRecordsUrl"), None
        return out

    def list_view_cases(self, list_view_name: str, limit: int = 200) -> list[CaseRef]:
        """Cases of a Salesforce Case list view (by developer name), open or
        closed — the list-view source of the automatic analysis."""
        lv_id = next((v["id"] for v in self.list_case_views(list_view_name)
                      if v["developer_name"] == list_view_name), None)
        if not lv_id:
            raise LookupError(f"list view not found: {list_view_name}")
        resp = self._request(
            "GET", "/services/data/v{ver}/sobjects/Case/listviews/" + lv_id + "/results",
            params={"limit": int(limit)})
        resp.raise_for_status()
        out: list[CaseRef] = []
        for rec in resp.json().get("records", []):
            cols = {c.get("fieldNameOrPath"): c.get("value")
                    for c in rec.get("columns", [])}
            if not cols.get("CaseNumber"):
                continue
            out.append(CaseRef(
                case_nbr=str(cols.get("CaseNumber")),
                case_id=str(cols.get("Id") or ""),
                subject=str(cols.get("Subject") or ""),
                status=str(cols.get("Status") or ""),
                created=str(cols.get("CreatedDate") or ""),
                owner_name=str(cols.get("Owner.Name") or ""),
            ))
        self._fill_case_states(out)
        return out

    def _fill_case_states(self, refs: list) -> None:
        """Open/closed state (+ Status, CreatedDate) of list-view cases: a
        list view only returns the columns it displays, so query the Case
        object for them. Raises when the query fails: the closed-case gate
        of a non-trial round relies on IsClosed (Status may be a column the
        view does not show), so an unknown state must fail the round rather
        than pass as open."""
        by_id = {r.case_id: r for r in refs if r.case_id}
        ids = list(by_id)
        for start in range(0, len(ids), 150):
            chunk = ", ".join(f"'{_soql_quote(i)}'" for i in ids[start:start + 150])
            soql = ("SELECT Id, IsClosed, Status, CreatedDate, Owner.Name "
                    f"FROM Case WHERE Id IN ({chunk})")
            try:
                resp = self._request("GET", "/services/data/v{ver}/query",
                                     params={"q": soql}, timeout=60)
                resp.raise_for_status()
                records = resp.json().get("records", [])
            except Exception as e:
                raise RuntimeError("case state lookup failed (open/closed unknown — "
                                   f"round aborted): {type(e).__name__}: {e}") from e
            for rec in records:
                ref = by_id.get(str(rec.get("Id") or ""))
                if ref is None:
                    continue
                ref.closed = bool(rec.get("IsClosed"))
                ref.status = str(rec.get("Status") or ref.status)
                ref.created = str(rec.get("CreatedDate") or ref.created)
                ref.owner_name = str((rec.get("Owner") or {}).get("Name") or ref.owner_name)

    def find_open_cases(self, owner_name: str, limit: int = 200) -> list[CaseRef]:
        """All open cases assigned to `owner_name` (auto-scan candidates)."""
        return self._query_cases(build_open_cases_soql(owner_name, limit))

    def find_customer_updates(self, case_ids: list[str],
                              since_iso: str) -> dict[str, str]:
        """{case_id: CreatedDate of the LATEST customer comment after
        since_iso} for the cases that have one. Chunked: SOQL IN-lists of a
        few hundred ids stay within the query length limit."""
        latest: dict[str, str] = {}
        ids = [i for i in case_ids if i]
        for start in range(0, len(ids), 150):
            soql = build_customer_updates_soql(ids[start:start + 150], since_iso)
            # Every page: Salesforce returns at most ~2000 rows per response,
            # and the cases whose comments sort last must not go missing.
            url: Optional[str] = "/services/data/v{ver}/query"
            params: Optional[dict] = {"q": soql}
            while url:
                resp = self._request("GET", url, params=params, timeout=60)
                resp.raise_for_status()
                d = resp.json()
                for rec in d.get("records", []):
                    cid = rec.get("Core_IPS_Case__c") or ""
                    ts = rec.get("CreatedDate") or ""
                    if cid and ts > latest.get(cid, ""):
                        latest[cid] = ts
                url = None if d.get("done", True) else d.get("nextRecordsUrl")
                params = None
        return latest

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

    def fetch_binary(self, url: str, max_bytes: int = 5 * 1024 * 1024,
                     max_redirects: int = 3) -> Optional[tuple[str, bytes]]:
        """Download a file the comment body links to (rich-text images on
        the force.com image servlet) with the Salesforce session. Returns
        (content_type, bytes) or None (non-200, non-image, too big).

        The URL comes from customer-authored rich text: credentials are only
        attached to HTTPS URLs on the org's own hosts (is_tenant_url), and
        redirects are followed by hand so every hop is re-checked before the
        headers go out again."""
        if not is_tenant_url(url):
            print(f"[handsfree.ips] image URL refused (not an {SF_TENANT} Salesforce "
                  f"host over HTTPS): {str(url)[:120]}")
            return None
        vf_session, headers, _ver = self._auth()
        try:
            for _hop in range(max_redirects + 1):
                resp = _requests.get(url, headers=headers, cookies=vf_session.cookies,
                                     proxies=vf_session.proxies, timeout=30, stream=True,
                                     allow_redirects=False)
                if resp.status_code in (301, 302, 303, 307, 308):
                    from urllib.parse import urljoin
                    url = urljoin(url, resp.headers.get("Location") or "")
                    if not is_tenant_url(url):
                        print(f"[handsfree.ips] image redirect refused: {url[:120]}")
                        return None
                    continue
                if resp.status_code != 200:
                    return None
                ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if not ctype.startswith("image/"):
                    return None
                data = b""
                for chunk in resp.iter_content(65536):
                    data += chunk
                    if len(data) > max_bytes:
                        return None
                return ctype, data
            return None
        except Exception as e:
            print(f"[handsfree.ips] image download failed: {e}")
            return None

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
             "extra_fields":  {"Core_IPS_Case_Comment_Source__c": "..."},
             # values for PUBLIC posts; required for every extra_fields
             # entry whose value is private-flavored ("Private to Intel"):
             "public_extra_fields":
                 {"Core_IPS_Case_Comment_Type__c": "Public to Customers"}}
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
            # Verified values for public posts (the org's public comment-
            # type picklist value). Required, not optional: seen live (case
            # 01032011) that the Public flag alone inserts a comment IPS
            # shows nowhere — the picklist is what the comment feed goes by.
            pub_extra = fm.get("public_extra_fields")
            if not isinstance(pub_extra, dict) or not pub_extra:
                raise PostUnsupported(
                    "No verified public comment type in config — add "
                    "rest_field_map.public_extra_fields (e.g. "
                    "Core_IPS_Case_Comment_Type__c: 'Public to Customers'); "
                    "refusing to post a customer reply that IPS would not display.")
            for k, v in pub_extra.items():
                payload.setdefault(k, v)
        extra = fm.get("extra_fields")
        if isinstance(extra, dict):
            unset = []
            for k, v in extra.items():
                # Never stamp private-flavored metadata (e.g. comment type
                # 'Private to Intel') onto a public customer reply.
                if not private and isinstance(v, str) and "private" in v.lower():
                    if k not in payload:
                        unset.append(k)
                    continue
                payload.setdefault(k, v)
            if unset:
                # A field that marks private posts as private needs an
                # explicit public value too. Seen live (case 01032011): with
                # Core_IPS_Case_Comment_Type__c left empty the insert
                # succeeds, but IPS shows the comment nowhere — the picklist,
                # not the Public flag, is what the IPS comment feed goes by.
                raise PostUnsupported(
                    "No public value configured for " + ", ".join(unset)
                    + " — add it under rest_field_map.public_extra_fields "
                    "(e.g. Core_IPS_Case_Comment_Type__c: 'Public to "
                    "Customers'); refusing to post a customer reply that "
                    "IPS would not display.")
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
