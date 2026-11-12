"""
Latest released Intel Wi-Fi driver, and the customer's driver against it.

The customer-facing drafts (first response, request_info, request_logs) ask
the customer to validate on the latest released driver when the driver the
issue was seen on is on an older branch. "Branch" is the first two numbers
of the version: 24.90.0.1 and 24.90.0.3 are the same branch, 24.80.x is
older, a customer on a newer branch than the latest release (a Beta) is not
asked.

Source of truth: the PAE Wi-Fi Dashboard on the Power BI Report Server
(REPORT_URL), page "Wi-Fi Kits Map Sustain", table
    WW | Year | Release | WiFi_Core | Version_Quality | TIC | Status | QSR_TA | Distribution | WU
The report's data is an imported PostgreSQL model (no direct access), so the
Beacon SERVER reads the rendered table through the app's SSO Chrome profile
(Windows integrated auth happens in Chrome; Python has no NTLM/Kerberos),
once a day and on demand, and stores the result in the shared config:

    latest_driver                 "24.90.0.3"  the report's pick (pick_latest)
    latest_driver_manual          ""           Settings override; wins when set
    latest_driver_checked_at      ISO local    last SUCCESSFUL fetch
    latest_driver_last_attempt_at ISO local    last fetch, success or not
    latest_driver_error           ""           why the last fetch failed
    latest_driver_source          "pbirs report"
    latest_driver_rows_count      int          rows harvested from the table
    latest_driver_row             {...}        the winning row
    latest_driver_report_updated  "WW 45'2026" the report's "Last Updated" text
    latest_driver_refresh_requested_at  ISO    "Refresh now" pressed on a page

Rule (Charles, 2026-11-09): among rows whose Version_Quality is PV or PV-HF
(not "PV Candidate", not Beta / Alpha / BKC) and whose Year/WW is not after
the current Intel work week, the HIGHEST version wins — by version, not by
week, because an older branch is sometimes re-released later (2024 WW31
23.60.5.14 after 23.70). Status Complete / Cmplete / Progress all count.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import date, datetime, timedelta
from typing import Callable, Optional

REPORT_URL = ("https://pbirs01.intel.com/reports/powerbi/WDA/CustomerEngineering/"
              "PAE%20Wi-Fi%20Dashboard")
REPORT_PAGE = "Wi-Fi Kits Map Sustain"
COLUMNS = ("ww", "year", "release", "wifi_core", "version_quality", "tic",
           "status", "qsr_ta", "distribution", "wu")
REFRESH_EVERY_S = 24 * 3600          # a successful value is good for a day
RETRY_AFTER_FAILURE_S = 2 * 3600     # a failed fetch is retried after two hours

_PV_QUALITY_RE = re.compile(r"^pv(?:[\s_-]?hf)?$", re.IGNORECASE)   # PV, PV-HF; not "PV Candidate"
_VERSION_RE = re.compile(r"(?<![\d.])(\d+)\.(\d+)(?:\.(\d+))?(?:\.(\d+))?(?![\d.])")
_FULL_VERSION_RE = re.compile(r"(?<![\d.])\d+\.\d+\.\d+\.\d+(?![\d.])")

Version = tuple  # (major, minor, build, rev)


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------

def parse_version(text, min_parts: int = 2) -> Optional[Version]:
    """First x.y[.z[.w]] in the text as a 4-int tuple (missing parts are 0);
    None when the text has no version with at least min_parts numbers.
    "MHWFW000813_24.90.0.3" -> (24, 90, 0, 3); "Beta 24.95" -> (24, 95, 0, 0)
    with min_parts=2, None with min_parts=4."""
    for m in _VERSION_RE.finditer(str(text or "")):
        parts = [g for g in m.groups() if g is not None]
        if len(parts) >= max(2, min(int(min_parts), 4)):
            return tuple(int(p) for p in parts) + (0,) * (4 - len(parts))
    return None


def format_version(v: Optional[Version]) -> str:
    return ".".join(str(int(x)) for x in v) if v else ""


def branch_of(v: Optional[Version]) -> Optional[tuple]:
    """The release branch: the first two numbers (24.90.0.3 -> (24, 90))."""
    return (int(v[0]), int(v[1])) if v else None


def compare_branch(customer: str, latest: str) -> str:
    """How the customer's driver relates to the latest release:
    "ok" (same or newer branch), "outdated" (older branch), "unknown" (no
    customer version), "skipped" (no latest release known)."""
    lat = parse_version(latest, min_parts=2)
    if not lat:
        return "skipped"
    cus = parse_version(customer, min_parts=2)
    if not cus:
        return "unknown"
    return "ok" if branch_of(cus) >= branch_of(lat) else "outdated"


# ---------------------------------------------------------------------------
# Intel work week
# ---------------------------------------------------------------------------

def _week_start(d: date) -> date:
    """The Sunday that starts d's week."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


def intel_ww(d: Optional[date] = None) -> tuple[int, int]:
    """(year, work week): weeks start on Sunday, WW1 is the week that holds
    January 1st. 2026-11-09 -> (2026, 46); 2024-12-29 (Sunday) -> (2025, 1)."""
    d = d or date.today()
    if isinstance(d, datetime):
        d = d.date()
    ws = _week_start(d)
    if ws == _week_start(date(d.year + 1, 1, 1)):
        return d.year + 1, 1
    return d.year, (ws - _week_start(date(d.year, 1, 1))).days // 7 + 1


# ---------------------------------------------------------------------------
# picking the latest release from the report rows
# ---------------------------------------------------------------------------

def _int(value) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def pick_latest(rows, today: Optional[date] = None) -> Optional[dict]:
    """The latest released driver among the report rows (dicts keyed like
    COLUMNS, case-insensitive): PV / PV-HF rows up to the current work week,
    highest version wins. None when no row qualifies.
    Returns {"version", "row", "candidates", "excluded": {reason: count}}."""
    now_ww = intel_ww(today)
    excluded = {"quality": 0, "future": 0, "no_week": 0, "no_version": 0}
    best = None
    best_row = None
    candidates = 0
    for raw in rows or []:
        if not isinstance(raw, dict):
            continue
        row = {str(k).strip().lower(): ("" if v is None else str(v).strip())
               for k, v in raw.items()}
        if not _PV_QUALITY_RE.match(row.get("version_quality", "")):
            excluded["quality"] += 1
            continue
        year, ww = _int(row.get("year")), _int(row.get("ww"))
        if year is None or ww is None:
            excluded["no_week"] += 1
            continue
        if (year, ww) > now_ww:
            excluded["future"] += 1
            continue
        v = parse_version(row.get("release"), 4) or parse_version(row.get("tic"), 4)
        if not v:
            excluded["no_version"] += 1
            continue
        candidates += 1
        if best is None or v > best:
            best, best_row = v, row
    if best is None:
        return None
    return {"version": format_version(best), "row": best_row,
            "candidates": candidates, "excluded": excluded}


# ---------------------------------------------------------------------------
# the customer's driver version from a WRT capture
# ---------------------------------------------------------------------------

def read_capture_driver_version(any_path: str) -> str:
    """"Wi-Fi Driver Version" from the system_info.txt of the WRT capture
    that holds any_path (the decoded .log, the .etl, or the capture folder):
    the capture folder and up to three parents are searched, like the time
    zone lookup. "" when there is none or it carries no version."""
    if not any_path:
        return ""
    try:
        from utils.timezone_utils import _search_dirs_from
        dirs = _search_dirs_from(str(any_path))
    except Exception:
        dirs = []
        cur = os.path.dirname(any_path) if os.path.splitext(any_path)[1] else any_path
        for _ in range(4):
            if not cur or cur in dirs:
                break
            dirs.append(cur)
            cur = os.path.dirname(cur)
    for base in dirs:
        path = os.path.join(base, "system_info.txt")
        try:
            if not os.path.isfile(path):
                continue
            with open(path, "r", encoding="utf-8-sig") as fh:
                info = json.load(fh)
            raw = ((info.get("Versions") or {}).get("Wi-Fi Driver Version")
                   if isinstance(info, dict) else "")
            v = parse_version(raw, min_parts=2)
            if v:
                return format_version(v)
        except Exception:
            continue
    return ""


# ---------------------------------------------------------------------------
# shared config
# ---------------------------------------------------------------------------

def effective_latest_driver(cfg: dict) -> tuple[str, str]:
    """(version, source): the Settings override when it is a full version,
    else the report's value; ("", "") when neither is known."""
    cfg = cfg or {}
    manual = parse_version(cfg.get("latest_driver_manual"), min_parts=4)
    if manual:
        return format_version(manual), "manual"
    report = parse_version(cfg.get("latest_driver"), min_parts=2)
    if report:
        return format_version(report), "report"
    return "", ""


def _parse_iso(value) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.astimezone()


def is_refresh_due(cfg: dict, now: Optional[datetime] = None) -> bool:
    """Whether the server should fetch the report now: never attempted,
    a page pressed Refresh since the last attempt, the last attempt failed
    more than RETRY_AFTER_FAILURE_S ago, or the last success is older than
    REFRESH_EVERY_S."""
    cfg = cfg or {}
    now = now or datetime.now().astimezone()
    if now.tzinfo is None:
        now = now.astimezone()
    attempt = _parse_iso(cfg.get("latest_driver_last_attempt_at"))
    if attempt is None:
        return True
    requested = _parse_iso(cfg.get("latest_driver_refresh_requested_at"))
    if requested is not None and requested > attempt:
        return True
    if cfg.get("latest_driver_error"):
        return (now - attempt).total_seconds() >= RETRY_AFTER_FAILURE_S
    checked = _parse_iso(cfg.get("latest_driver_checked_at")) or attempt
    return (now - checked).total_seconds() >= REFRESH_EVERY_S


def refresh_latest_driver(store, *, today: Optional[date] = None,
                          fetch: Optional[Callable[[], list]] = None) -> dict:
    """Fetch the report table and store the latest release in the shared
    config. Never raises: on any failure the previous value is kept and only
    the attempt time + error are written. Returns {"ok", "version", "error",
    "rows"}."""
    fetch = fetch or fetch_release_table_via_chrome
    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
    t0 = time.time()
    try:
        result = fetch()
        rows, header = (result if isinstance(result, tuple) else (result, ""))
        picked = pick_latest(rows, today)
        if picked is None:
            raise RuntimeError(f"no PV / PV-HF release row found in {len(rows or [])} row(s)")
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:300]
        store.save_config({"latest_driver_last_attempt_at": now_iso,
                           "latest_driver_error": err})
        print(f"[handsfree.driver_release] latest driver fetch failed after "
              f"{time.time() - t0:.0f}s: {err} - keeping the previous value")
        return {"ok": False, "version": "", "error": err, "rows": 0}
    store.save_config({"latest_driver": picked["version"],
                       "latest_driver_checked_at": now_iso,
                       "latest_driver_last_attempt_at": now_iso,
                       "latest_driver_error": "",
                       "latest_driver_source": "pbirs report",
                       "latest_driver_rows_count": len(rows),
                       "latest_driver_row": picked["row"],
                       "latest_driver_report_updated": str(header or "")[:80]})
    print(f"[handsfree.driver_release] latest released driver {picked['version']} "
          f"({picked['candidates']} PV row(s) of {len(rows)}) in {time.time() - t0:.0f}s")
    return {"ok": True, "version": picked["version"], "error": "", "rows": len(rows)}


# ---------------------------------------------------------------------------
# reading the report table through the SSO Chrome profile (server only)
# ---------------------------------------------------------------------------

_HARVEST_JS = r"""
const want = arguments[0];
const headers = [...document.querySelectorAll('div[role="columnheader"]')]
    .map(c => c.innerText.trim()).filter(Boolean);
const rows = [];
let firstDataRow = null;
for (const r of document.querySelectorAll('div[role="row"]')) {
    let cells = [...r.querySelectorAll('[role="gridcell"]')].map(c => c.innerText.trim());
    if (!cells.length) cells = r.innerText.split('\n').map(s => s.trim());
    // drop the row-selection cell(s): a data row starts with WW then Year
    while (cells.length > 2 && !(/^\d{1,2}$/.test(cells[0]) && /^\d{4}$/.test(cells[1]))) cells.shift();
    if (cells.length >= 3 && /^\d{1,2}$/.test(cells[0]) && /^\d{4}$/.test(cells[1])) {
        rows.push(cells);
        if (!firstDataRow) firstDataRow = r;
    }
}
// The kits-map table virtualises its rows inside its own scroll viewport
// (overflow-y auto, e.g. ".mid-viewport"); other visuals on the page have
// rows too, so walk up from a DATA row, not from the page's first row.
let scroller = null;
for (let el = firstDataRow; el; el = el.parentElement) {
    const oy = getComputedStyle(el).overflowY;
    if (/auto|scroll/.test(oy) && el.scrollHeight > el.clientHeight + 2) { scroller = el; break; }
}
let scrollTop = -1, atEnd = true;
if (scroller) {
    scrollTop = scroller.scrollTop;
    if (want === 'scroll') {
        scroller.scrollTop += Math.floor(scroller.clientHeight * 0.8);
        atEnd = scroller.scrollTop === scrollTop;
    } else {
        atEnd = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2;
    }
}
let updated = '';
for (const el of document.querySelectorAll('div, span')) {
    const t = (el.innerText || '').trim();
    if (/^Last Updated/i.test(t) && t.length < 60) { updated = t; break; }
}
return {headers, rows, hasScroller: !!scroller, atEnd, updated};
"""


def _rows_to_dicts(rows: list) -> list[dict]:
    """Map harvested cell lists (WW first, the row-selection cell already
    dropped) onto COLUMNS by position - the kits-map table's column order."""
    out = []
    for cells in rows:
        d = {name: val for name, val in zip(COLUMNS, cells)}
        if d.get("release") or d.get("tic"):
            out.append(d)
    return out


def fetch_release_table_via_chrome(url: str = REPORT_URL, page: str = REPORT_PAGE,
                                   timeout_s: int = 120) -> tuple[list[dict], str]:
    """Open the report in headless Chrome (the app's Intel-SSO profile, so
    Windows integrated auth just happens), switch into the report iframe,
    make sure the kits-map page is shown, scroll the virtualised table and
    harvest every row. Returns (rows, "Last Updated ..." text). Raises on
    anything that would make the result untrustworthy (sign-in page, wrong
    page, no rows): the caller keeps the previous value then."""
    from configs.global_configs import app_config
    from services.case_info_service import CaseService
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    if not CaseService._sso_lock.acquire(timeout=timeout_s):
        raise RuntimeError("the SSO Chrome profile is busy (another sign-in or round)")
    driver = None
    driver_manager = app_config.driver_manager
    try:
        profile = CaseService._sso_profile_dir()
        driver = driver_manager.create_download_driver(
            os.path.join(app_config.avatarfiles_dir, "_pbirs_tmp"),
            headless=True,
            user_data_dir=profile if os.path.isdir(profile) else None,
            additional_args=["--auth-server-allowlist=*.intel.com",
                             "--auth-negotiate-delegate-allowlist=*.intel.com"],
        )
        driver.set_page_load_timeout(timeout_s)
        driver.get(url)
        wait = WebDriverWait(driver, min(timeout_s, 90))
        try:
            frame = wait.until(EC.presence_of_element_located(
                (By.CSS_SELECTOR, 'iframe[src*="/powerbi/?id="]')))
        except Exception:
            title = (driver.title or "")[:80]
            raise RuntimeError(
                f"the report did not render (page title {title!r}, url {driver.current_url[:120]}) - "
                "Windows integrated auth may not have completed in Chrome")
        driver.switch_to.frame(frame)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'div[role="row"]')))
        # The report opens on its first page; make sure it is the kits map.
        try:
            tabs = driver.find_elements(
                By.XPATH, f"//*[normalize-space(text())={json.dumps(page)}]")
            for tab in tabs:
                if tab.is_displayed():
                    tab.click()
                    time.sleep(1.5)
                    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'div[role="row"]')))
                    break
        except Exception:
            pass          # not clickable / already active: the header check below decides
        seen: dict[tuple, list] = {}
        headers: list = []
        updated = ""
        idle = 0
        for _ in range(80):
            res = driver.execute_script(_HARVEST_JS, "read")
            headers = res.get("headers") or headers
            updated = res.get("updated") or updated
            before = len(seen)
            for cells in res.get("rows") or []:
                seen.setdefault(tuple(cells), list(cells))
            if not res.get("hasScroller"):
                break
            if len(seen) == before:
                idle += 1
                if idle >= 3 or res.get("atEnd"):
                    break
            else:
                idle = 0
            step = driver.execute_script(_HARVEST_JS, "scroll")
            if step.get("atEnd") and len(seen) == before:
                break
            time.sleep(0.4)
        rows = _rows_to_dicts(list(seen.values()))
        if not rows:
            raise RuntimeError("no table rows found on the report page")
        head_l = " ".join(headers).lower()
        if headers and not ("release" in head_l and "quality" in head_l):
            raise RuntimeError(f"unexpected table (header {headers[:6]}) - not the kits map")
        return rows, updated
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
            try:
                if driver in getattr(driver_manager, "all_drivers", []):
                    driver_manager.all_drivers.remove(driver)
            except Exception:
                pass
        CaseService._sso_lock.release()
