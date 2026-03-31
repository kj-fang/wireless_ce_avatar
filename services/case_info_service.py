import os
import json
import threading
import requests as _requests
from flask import Blueprint, render_template, request, session, redirect, url_for, flash
from concurrent.futures import ThreadPoolExecutor
import shutil
import time

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC

from models.models import CaseContext
from configs.global_configs import app_config

from utils import case_utils
from services.snowflake_service import snowflake_query
from utils.case_utils import parse_html_table



class CaseService:

    # Persistent requests session authenticated to intel--c.vf.force.com
    _vf_session: _requests.Session = None
    _vf_session_lock = threading.Lock()
    _VF_COOKIE_FILENAME = "sf_session_cookies.json"

    # Field name on Core_IPS_Case_Comments__c that holds the esft download URL.
    # Run probe_comment_fields(case_id) once to discover it, then set this constant.
    _COMMENT_DOWNLOAD_FIELD: str = None  # e.g. "Core_Download_Link__c"
    _SF_API_VERSION: str = None          # cached, e.g. "59.0"
    _sf_bearer_token: str = None         # the sid value confirmed to work with the REST API
    _sf_bearer_domain: str = None        # the cookie domain that produced the working sid

    @staticmethod
    def _cookie_file_path() -> str:
        return os.path.join(app_config.avatarfiles_dir, CaseService._VF_COOKIE_FILENAME)

    @staticmethod
    def _save_cookies(vf_session: _requests.Session):
        """Persist cookies to a per-user local file for reuse across app restarts."""
        cookies_data = {
            "saved_at": time.time(),
            "sf_bearer_domain": CaseService._sf_bearer_domain,
            "cookies": [
                {"name": c.name, "value": c.value, "domain": c.domain}
                for c in vf_session.cookies
            ],
        }
        try:
            with open(CaseService._cookie_file_path(), "w") as f:
                json.dump(cookies_data, f)
            print(f"  [VF Session] Cookies saved to {CaseService._cookie_file_path()}")
        except Exception as e:
            print(f"  [VF Session] Could not save cookies: {e}")

    @staticmethod
    def _load_cookies_from_file() -> _requests.Session | None:
        """Try to restore a session from the saved cookie file.
        Returns a Session if cookies are still valid, else None.
        """
        path = CaseService._cookie_file_path()
        if not os.path.exists(path):
            return None
        _SESSION_TTL = 4 * 3600  # 4 hours in seconds
        try:
            with open(path) as f:
                data = json.load(f)

            # Support old format (plain list) and new format (dict with saved_at + cookies)
            if isinstance(data, list):
                cookie_list, saved_at = data, 0.0
            else:
                cookie_list = data.get("cookies", [])
                saved_at = data.get("saved_at", 0.0)

            age_s = time.time() - saved_at
            if age_s > _SESSION_TTL:
                print(f"  [VF Session] Saved cookies are {age_s/3600:.1f}h old — will re-authenticate")
                return None

            vf_session = _requests.Session()
            for c in cookie_list:
                vf_session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))
            # Explicit proxy — env vars may have been popped by create_download_driver()
            vf_session.proxies = {
                "http": "http://proxy-dmz.intel.com:911",
                "https": "http://proxy-dmz.intel.com:912",
            }
            # Restore the known working bearer domain so _sf_rest_get never needs a full scan
            saved_domain = data.get("sf_bearer_domain") if isinstance(data, dict) else None
            if saved_domain and CaseService._sf_bearer_domain is None:
                CaseService._sf_bearer_domain = saved_domain
                print(f"  [VF Session] Restored bearer domain: {saved_domain!r}")
            print(f"  [VF Session] Restored cookies from file (age {age_s/3600:.1f}h) ✅")
            return vf_session
        except Exception as e:
            print(f"  [VF Session] Could not restore cookies: {e}")
            return None

    @staticmethod
    def _get_vf_session() -> _requests.Session:
        """Return a cached requests.Session with VF-domain cookies.
        Order of preference:
          1. In-memory cached session (fastest, ~0s)
          2. Saved cookie file from previous run (fast if not expired, ~1s)
          3. Full SSO via headless Chrome (slow, ~10-15s, only when needed)
        """
        # Fast path: check in-memory cache under lock
        with CaseService._vf_session_lock:
            if CaseService._vf_session is not None:
                return CaseService._vf_session

        # Try restoring from file — fast, no Chrome needed
        restored = CaseService._load_cookies_from_file()
        if restored is not None:
            with CaseService._vf_session_lock:
                # Another thread may have populated it while we were loading from file
                if CaseService._vf_session is None:
                    CaseService._vf_session = restored
                return CaseService._vf_session

        # Slow path: full SSO via Chrome — do NOT hold the lock during this
        # (holds 13+ seconds and blocks any concurrent PDF download threads)
        t = time.time()
        driver_manager = app_config.driver_manager
        driver = driver_manager.create_download_driver(
            os.path.join(app_config.avatarfiles_dir, "_vf_auth_tmp")
        )
        try:
            # Step 1: Trigger SSO — VF root always redirects to Lightning after login
            driver.get("https://intel--c.vf.force.com")
            # Step 2: Wait for SSO to finish on any Salesforce domain
            WebDriverWait(driver, 90).until(
                lambda d: "force.com" in d.current_url and "login" not in d.current_url
            )
            print(f"  [VF Session] SSO complete, landed: {driver.current_url}")

            # Step 3: Use CDP Network.getAllCookies to get ALL cookies for ALL domains
            all_cookies = driver.execute_cdp_cmd("Network.getAllCookies", {})["cookies"]
            vf_cookies = [c for c in all_cookies if "force.com" in c.get("domain", "")]
            print(f"  [VF Session] Collected {len(vf_cookies)} force.com cookies via CDP")

            vf_session = _requests.Session()
            for c in vf_cookies:
                vf_session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))
            vf_session.headers["User-Agent"] = driver.execute_script("return navigator.userAgent")
            # Explicitly set corporate proxy — create_download_driver() permanently pops
            # HTTP_PROXY/HTTPS_PROXY from os.environ, so trust_env=True finds nothing after this.
            vf_session.proxies = {
                "http": "http://proxy-dmz.intel.com:911",
                "https": "http://proxy-dmz.intel.com:912",
            }

            CaseService._save_cookies(vf_session)
            print(f"  [VF Session] Authenticated via SSO in {time.time() - t:.2f}s")
        finally:
            driver.quit()
            if driver in driver_manager.all_drivers:
                driver_manager.all_drivers.remove(driver)

        # Store result under lock
        with CaseService._vf_session_lock:
            if CaseService._vf_session is None:
                CaseService._vf_session = vf_session
        return CaseService._vf_session

    @staticmethod
    def process_case(case_context: CaseContext) -> CaseContext:
        
        key = app_config.key
        if not case_context.case_nbr:
            return case_context  
        ## create download folder for ips case
        case_context.case_download_dir = os.path.join(f"{app_config.avatarfiles_dir}\{case_context.case_nbr}")
        os.makedirs(case_context.case_download_dir, exist_ok=True)
        print("-----download_path:-----", case_context.case_download_dir)


        case_fields = CaseService._get_case_info_from_snowflake(case_context.case_nbr, key.snowflake_passwd)

        if  case_fields is not None:

            (case_context.id, 
            case_context.subject,   
            case_context.env_detail, 
            case_context.description, 
            case_context.backend_id, 
            case_context.subcategory) = case_fields

            with ThreadPoolExecutor(max_workers=2) as executor:
                comment_future = executor.submit(CaseService._get_case_comments_from_snowflake,  case_context.id, key.snowflake_passwd)
                att_future = executor.submit(CaseService._get_attachment_links_from_api, case_context.id)
                case_context.comments, _ = comment_future.result(timeout=100)
                case_context.attachment_info = att_future.result(timeout=30)
            # Build attachment_list directly from API data — no PDF needed
            case_context.attachment_list = [
                [name, url, [created_at, name]]
                for name, (created_at, url) in case_context.attachment_info.items()
            ]
            case_context.wifi_or_bt = "wifi" if "wifi" in case_context.subcategory.lower() else "bt"
            return case_context

        else: # snowflake failed, try parse from pdf
            case_context.id, case_context.ips_pdf_path  = CaseService._download_pdf_by_simulation(case_context.case_nbr, case_context.case_download_dir)
            if case_context.id == None:
                session.clear()
                case_context.error_message = f"Error downloading PDF"
                return case_context
            case_context = case_utils.parse_pdf_for_all_info(case_context.ips_pdf_path , case_context)

        case_context.wifi_or_bt = "wifi" if "wifi" in case_context.subcategory.lower()  else "bt"
        
        case_context.attachment_list = case_utils.parse_pdf_for_attachments(case_context.ips_pdf_path, case_context.attachment_info)

        return case_context
    

    @staticmethod
    def load_case_summary_prompt(wifi_or_bt):

        prompt_filename = f"prompt_{wifi_or_bt.lower()}.py"
        target_prompt = os.path.join(app_config.prompt_dir, prompt_filename)

        if not os.path.exists(target_prompt):

            source_prompt = os.path.join(app_config.project_root, "utils", "summary_prompt_templates",prompt_filename)   
            if os.path.exists(source_prompt):
                shutil.copy(source_prompt, target_prompt)
                print(f"✅ Copied prompt.py to {target_prompt}")
            else:
                print(f"❌ Source prompt.py not found at {source_prompt}") 
           
        return target_prompt
    
    @staticmethod
    def _get_case_info_from_snowflake(case_nbr, passwd):
        sql_query = f"""
        SELECT CASE_ID, SUBJECT_TXT, ENV_DETAIL_DSC, ISS_CASE_DESCRIPTION_DSC, 
               BACKEND_ID, CORE_ISSUE_SUBCATEGORY_EXTERNAL_TXT 
        FROM SALES_MARKETING.sales_support_premier_analysis.fact_case 
        WHERE CASE_NBR={case_nbr}
        """
        schema = "sales_support_premier_analysis.fact_case"
        row = snowflake_query(passwd, sql_query, schema, fetch_mode="one")
        
        if not row:
            return None
            
        case_id, subject, env_detail, description, backend_id, subcategory = row
        return case_id, subject, parse_html_table(env_detail), description, backend_id, subcategory
    
    @staticmethod
    def _get_case_comments_from_snowflake(case_id, passwd):
        sql_query = f"""
        SELECT CORE_IPS_CREATED_DTM, CORE_IPS_COMMENT_AUTHOR_TYPE_TXT, CORE_IPS_CASE_COMMENT_TXT 
        FROM SALES_MARKETING.SALES_SUPPORT_PREMIER_ANALYSIS.DIM_CORE_IPS_CASE_COMMENTS 
        WHERE CORE_IPS_CASE_ID='{case_id}'
        """
        schema = "sales_support_premier_analysis.DIM_CORE_IPS_CASE_COMMENTS"
        comments = snowflake_query(passwd, sql_query, schema, fetch_mode="all")
        
        att_info = {}
        processed_comments = []
        
        for comm in comments:
            comm = list(comm)
            if "Download link" in comm[2]:
                att = comm[2].split(' \xa0 \xa0 ')
                att_info[att[-2]] = [comm[0], att[-1]]
            comm[2] = comm[2].replace('\xa0', ' ').replace('\n', ' ').strip()
            processed_comments.append(comm)
            
        return processed_comments, att_info
    
    @staticmethod
    def _sf_rest_get(url: str, **kwargs) -> _requests.Response:
        """GET a Salesforce REST API URL, injecting the correct Bearer token.
        Tries all available sid cookies (non-VF domains first) until one is accepted,
        then caches that token for subsequent calls.
        """
        vf_session = CaseService._get_vf_session()
        headers_base = {"Accept": "application/json"}

        def _is_invalid_session(resp: _requests.Response) -> bool:
            try:
                body = resp.json()
                if isinstance(body, list):
                    return any(e.get("errorCode") == "INVALID_SESSION_ID" for e in body)
            except Exception:
                pass
            return False

        # Fast path: use the already-confirmed working token
        if CaseService._sf_bearer_token:
            resp = vf_session.get(url, headers={**headers_base, "Authorization": f"Bearer {CaseService._sf_bearer_token}"}, **kwargs)
            if not _is_invalid_session(resp):
                return resp
            print("  [SF API] Cached bearer token expired, re-discovering...")
            CaseService._sf_bearer_token = None

        # If we know which domain worked before, try it first with the fresh sid value
        if CaseService._sf_bearer_domain:
            for c in vf_session.cookies:
                if c.name == "sid" and c.domain == CaseService._sf_bearer_domain:
                    resp = vf_session.get(url, headers={**headers_base, "Authorization": f"Bearer {c.value}"}, **kwargs)
                    if not _is_invalid_session(resp):
                        print(f"  [SF API] sid from known domain {CaseService._sf_bearer_domain!r} accepted ✅")
                        CaseService._sf_bearer_token = c.value
                        return resp
                    print(f"  [SF API] Known domain {CaseService._sf_bearer_domain!r} sid also expired, falling back to full scan...")
                    CaseService._sf_bearer_domain = None
                    break

        # Full scan: collect all sid cookies; try non-VF domains first
        sid_candidates = []
        for c in vf_session.cookies:
            if c.name == "sid":
                domain = c.domain or ""
                is_vf = "vf.force.com" in domain
                sid_candidates.append((is_vf, domain, c.value))
        sid_candidates.sort(key=lambda x: x[0])  # non-VF (False) before VF (True)

        if not sid_candidates:
            raise RuntimeError("[SF API] No 'sid' cookie found in VF session — cannot call REST API")

        last_resp = None
        for _, domain, sid in sid_candidates:
            resp = vf_session.get(url, headers={**headers_base, "Authorization": f"Bearer {sid}"}, **kwargs)
            last_resp = resp
            if _is_invalid_session(resp):
                print(f"  [SF API] sid from {domain!r} → INVALID_SESSION_ID, trying next...")
                continue
            print(f"  [SF API] sid from {domain!r} accepted ✅")
            CaseService._sf_bearer_token = sid
            CaseService._sf_bearer_domain = domain
            return resp

        print(f"  [SF API] All {len(sid_candidates)} sid cookie(s) rejected. Last response: {last_resp.text[:300]}")
        return last_resp

    @staticmethod
    def _get_sf_api_version() -> str:
        """Return the latest Salesforce REST API version (cached)."""
        if CaseService._SF_API_VERSION is None:
            resp = CaseService._sf_rest_get("https://intel--c.vf.force.com/services/data/", timeout=10)
            resp.raise_for_status()
            CaseService._SF_API_VERSION = resp.json()[-1]["version"]
            print(f"  [SF API] Using API version: {CaseService._SF_API_VERSION}")
        return CaseService._SF_API_VERSION

    @staticmethod
    def probe_comment_fields(case_id: str):
        """
        One-time diagnostic: print all fields on Core_IPS_Case_Comments__c
        that look like they contain a download URL.

        Call from a debug route or the Python console:
            CaseService.probe_comment_fields("500Ho00001izeWEIAY")

        Then set CaseService._COMMENT_DOWNLOAD_FIELD = "<field name found>"
        """
        from urllib.parse import urlparse
        api_ver = CaseService._get_sf_api_version()

        soql = (
            f"SELECT FIELDS(ALL) FROM Core_IPS_Case_Comments__c "
            f"WHERE Core_IPS_Case__c='{case_id}' LIMIT 5"
        )
        resp = CaseService._sf_rest_get(
            f"https://intel--c.vf.force.com/services/data/v{api_ver}/query",
            params={"q": soql},
            timeout=15,
        )
        resp.raise_for_status()
        records = resp.json().get("records", [])
        if not records:
            print("  [probe] No comment records found for this case ID.")
            print("  [probe] Raw response:", resp.text[:500])
            return

        print(f"  [probe] Found {len(records)} record(s). Scanning fields for URLs...")
        for rec in records[:1]:  # inspect first record only
            for field, value in sorted(rec.items()):
                if not value or field == "attributes":
                    continue
                s = str(value)
                if "esft.intel.com" in s or "http" in s.lower():
                    print(f"  [probe] *** URL FIELD: {field!r} = {s[:200]}")
                elif "link" in field.lower() or "url" in field.lower() or "file" in field.lower() or "download" in field.lower():
                    print(f"  [probe]     CANDIDATE: {field!r} = {s[:200]}")
        print("  [probe] Done. Set CaseService._COMMENT_DOWNLOAD_FIELD = '<field name above>'")

    @staticmethod
    def _discover_download_field(case_id: str) -> str:
        """Auto-discover and cache the field on Core_IPS_Case_Comments__c that holds the esft URL."""
        api_ver = CaseService._get_sf_api_version()
        soql = (
            f"SELECT FIELDS(ALL) FROM Core_IPS_Case_Comments__c "
            f"WHERE Core_IPS_Case__c='{case_id}' LIMIT 1"
        )
        resp = CaseService._sf_rest_get(
            f"https://intel--c.vf.force.com/services/data/v{api_ver}/query",
            params={"q": soql},
            timeout=15,
        )
        resp.raise_for_status()
        records = resp.json().get("records", [])
        if not records:
            raise RuntimeError(f"No Core_IPS_Case_Comments__c records found for case {case_id}")
        for field, value in records[0].items():
            if value and "esft.intel.com" in str(value):
                print(f"  [SF API] Auto-discovered download field: {field!r}")
                CaseService._COMMENT_DOWNLOAD_FIELD = field
                return field
        raise RuntimeError(
            "Could not auto-discover download URL field on Core_IPS_Case_Comments__c. "
            f"Record fields: {list(records[0].keys())}"
        )

    @staticmethod
    def _get_attachment_links_from_api(case_id: str) -> dict:
        """
        Query Salesforce REST API for all comments on the case and extract
        esft.intel.com download URLs from the rich-text comment HTML.
        Returns dict: filename -> [created_at, download_url]
        (same shape as att_info produced by _get_case_comments_from_snowflake)
        """
        import re
        from urllib.parse import urlparse, parse_qs, unquote

        api_ver = CaseService._get_sf_api_version()

        # Core_IPS_Rich_Comment__c is a LongTextArea/Html field — cannot be used in WHERE,
        # so fetch all comments and filter in Python.
        soql = (
            f"SELECT CreatedDate, Core_IPS_Rich_Comment__c "
            f"FROM Core_IPS_Case_Comments__c "
            f"WHERE Core_IPS_Case__c='{case_id}'"
        )
        t = time.time()
        resp = CaseService._sf_rest_get(
            f"https://intel--c.vf.force.com/services/data/v{api_ver}/query",
            params={"q": soql},
            timeout=15,
        )
        resp.raise_for_status()
        records = resp.json().get("records", [])
        print(f"  [SF API] Got {len(records)} comment record(s) in {time.time() - t:.2f}s")

        att_info = {}
        filename_count = {}
        for rec in records:
            rich_text = rec.get("Core_IPS_Rich_Comment__c") or ""
            urls = re.findall(r'https://esft\.intel\.com/sftservices/download/[^"\'<>\s]+', rich_text)
            for url in urls:
                qs = parse_qs(urlparse(url).query)
                filename = unquote(qs.get("FileName", [""])[0])
                if not filename:
                    continue
                # Handle duplicate filenames the same way the PDF parser does
                if filename in filename_count:
                    filename_count[filename] += 1
                    base, ext = os.path.splitext(filename)
                    unique_name = f"{base}_{filename_count[filename]}{ext}"
                else:
                    filename_count[filename] = 1
                    unique_name = filename
                att_info[unique_name] = [rec.get("CreatedDate"), url]

        print(f"  [SF API] Parsed {len(att_info)} attachment URL(s): {list(att_info.keys())}")
        return att_info

    @staticmethod
    def _is_valid_pdf(resp) -> bool:
        """Return True only if the response is a real PDF (Content-Type + magic bytes)."""
        content_type = resp.headers.get("Content-Type", "")
        return "application/pdf" in content_type or resp.content[:4] == b"%PDF"

    @staticmethod
    def _download_pdf_by_url(case_id, download_path):
        """Download PDF via cached VF-domain requests session — no Chrome driver after first auth."""
        pdf_url = f"https://intel--c.vf.force.com/apex/Core_IPS_Case_ExportPDF_LEX?id={case_id}"
        downloaded_pdf_path = os.path.join(download_path, 'Core_IPS_Case_ExportPDF_LEX.pdf')

        if os.path.exists(downloaded_pdf_path):
            print("⚠️ Existing PDF found. Removing old one.")
            os.remove(downloaded_pdf_path)

        t = time.time()
        print(f"  [PDF] Getting VF session...")
        vf_session = CaseService._get_vf_session()
        print(f"  [PDF] Session ready, sending request to: {pdf_url}, consume: {time.time() - t:.2f}s, ")
        resp = vf_session.get(pdf_url, timeout=60, allow_redirects=True)
        print(f"  [PDF] Response: status={resp.status_code}, consume: {time.time() - t:.2f}s, content-type={resp.headers.get('Content-Type','?')}, size={len(resp.content)}")

        # Retry once if response is not a valid PDF (login redirect, error page, etc.)
        if not CaseService._is_valid_pdf(resp):
            print(f"  [PDF] Response is not a PDF, re-authing...")
            CaseService._invalidate_vf_session()
            vf_session = CaseService._get_vf_session()
            resp = vf_session.get(pdf_url, timeout=60, allow_redirects=True)
            print(f"  [PDF] Retry response: status={resp.status_code}, content-type={resp.headers.get('Content-Type','?')}, size={len(resp.content)}")

        if not CaseService._is_valid_pdf(resp):
            raise RuntimeError(
                f"Failed to download PDF for case {case_id}: "
                f"status={resp.status_code}, content-type={resp.headers.get('Content-Type','?')}"
            )

        with open(downloaded_pdf_path, 'wb') as f:
            f.write(resp.content)
        print(f"  [PDF] Download via requests: {time.time() - t:.2f}s  ({len(resp.content):,} bytes)")
        return downloaded_pdf_path
    
    @staticmethod
    def _invalidate_vf_session():
        """Clear in-memory session and delete the cookie file so next call does a fresh SSO."""
        with CaseService._vf_session_lock:
            CaseService._vf_session = None
        cookie_path = CaseService._cookie_file_path()
        if os.path.exists(cookie_path):
            try:
                os.remove(cookie_path)
            except Exception:
                pass
        CaseService._sf_bearer_token = None
        CaseService._sf_bearer_domain = None
        CaseService._SF_API_VERSION = None
        print("  [VF Session] Session invalidated, will re-authenticate on next request")

    @staticmethod
    def _download_pdf_by_simulation(case_nbr, download_path):
        driver_manager = app_config.driver_manager
        driver = driver_manager.create_download_driver(download_path)

        try:
            case_list_url = "https://intel.lightning.force.com/lightning/o/Case/list?filterName=Core_AllCases"
            driver.get(case_list_url)
            search_button = WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.XPATH, "//button[@aria-label='Search']"))
            )
            search_button.click()

            search_box = WebDriverWait(driver, 20).until(
                EC.element_to_be_clickable((By.XPATH, "//input[@placeholder='Search...']"))
            )
            search_box.clear()
            search_box.send_keys(case_nbr)
            search_box.send_keys(Keys.ENTER)

            a_tag = WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.XPATH, f'//a[@title="{case_nbr}"]'))
            )
            href = a_tag.get_attribute("href")
            case_id = href.split("/r/")[1].split("/")[0]

            pdf_url = f"https://intel--c.vf.force.com/apex/Core_IPS_Case_ExportPDF_LEX?id={case_id}"
            pdf_path = CaseService._download_pdf_common(pdf_url, download_path, driver)

            return case_id, pdf_path

        except Exception as e:
            print(f"link of case {case_nbr} not found: {e}")
            flash(f"link of case {case_nbr} not found: {e}", "danger")
            return None, None
        finally:
            driver.quit()
            if driver in driver_manager.all_drivers:
                driver_manager.all_drivers.remove(driver)
    
    @staticmethod
    def _download_pdf_common(pdf_url, download_path, driver=None):
        start_time = time.time()
        
        if driver is None:
            driver_manager = app_config.driver_manager
            driver = driver_manager.create_download_driver(download_path)
            should_quit = True
        else:
            should_quit = False
        
        downloaded_pdf_path = os.path.join(download_path, 'Core_IPS_Case_ExportPDF_LEX.pdf')
        if os.path.exists(downloaded_pdf_path):
            print("⚠️ Existing PDF found. Removing old one.")
            os.remove(downloaded_pdf_path)
        
        try:
            driver.get(pdf_url)
            
            while True:
                time.sleep(0.1)
                if os.path.exists(downloaded_pdf_path):
                    initial_size = os.path.getsize(downloaded_pdf_path)
                    time.sleep(0.1)
                    current_size = os.path.getsize(downloaded_pdf_path)
                    if initial_size == current_size:
                        print("Download pdf done!")
                        break
                        
            return downloaded_pdf_path
            
        finally:
            if should_quit:
                driver.quit()
                if driver in driver_manager.all_drivers:
                    driver_manager.all_drivers.remove(driver)
            print(f"(download pdf) time total: {(time.time() - start_time):.2f}秒")
    
    @staticmethod
    def _process_snowflake_data(case_context, case_fields, key):
        (case_context.id, case_context.subject, case_context.env_detail, 
         case_context.description, case_context.backend_id, case_context.subcategory) = case_fields
        
        with ThreadPoolExecutor(max_workers=2) as executor:
            comment_future = executor.submit(CaseService._get_case_comments_from_snowflake, case_context.id, key.snowflake_passwd)
            pdf_future = executor.submit(CaseService._download_pdf_by_url, case_context.id, case_context.case_download_dir)
            
            case_context.comments, case_context.attachment_info = comment_future.result(timeout=100)
            case_context.ips_pdf_path = pdf_future.result(timeout=100)
            
        return case_context
    
 
    
    
    
