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
    _SF_API_BASE_URL = "https://intel.my.salesforce.com"

    @staticmethod
    def _cookie_file_path() -> str:
        return os.path.join(app_config.avatarfiles_dir, CaseService._VF_COOKIE_FILENAME)

    @staticmethod
    def _save_cookies(vf_session: _requests.Session):
        """Persist cookies to a per-user local file for reuse across app restarts."""
        cookies_data = {
            "saved_at": time.time(),
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
            print(f"  [VF Session] Restored cookies from file (age {age_s/3600:.1f}h)")
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

        CaseService._prepare_case_download_dir(case_context)
        case_fields = CaseService._get_case_info_from_snowflake(case_context.case_nbr, key.snowflake_passwd)

        if case_fields is not None:
            case_context = CaseService._process_snowflake_data(case_context, case_fields, key)
        else:
            case_context.id, case_context.ips_pdf_path = CaseService._download_pdf_by_simulation(
                case_context.case_nbr,
                case_context.case_download_dir,
            )
            if case_context.id is None:
                session.clear()
                case_context.error_message = "Error downloading PDF"
                return case_context
            case_context = case_utils.parse_pdf_for_all_info(case_context.ips_pdf_path, case_context)

        CaseService._finalize_case_context(case_context)
        case_context.attachment_list = case_utils.parse_pdf_for_attachments(case_context.ips_pdf_path, case_context.attachment_info)
        return case_context

    @staticmethod
    def _prepare_case_download_dir(case_context: CaseContext) -> None:
        case_context.case_download_dir = os.path.join(app_config.avatarfiles_dir, case_context.case_nbr)
        os.makedirs(case_context.case_download_dir, exist_ok=True)
        print("-----download_path:-----", case_context.case_download_dir)

    @staticmethod
    def _apply_case_fields(case_context: CaseContext, case_fields) -> CaseContext:
        (
            case_context.id,
            case_context.subject,
            case_context.env_detail,
            case_context.description,
            case_context.backend_id,
            case_context.subcategory,
        ) = case_fields
        return case_context

    @staticmethod
    def _finalize_case_context(case_context: CaseContext) -> None:
        sub = (case_context.subcategory or "").lower()
        has_wifi = "wifi" in sub
        has_bt = "bt" in sub
        if has_wifi and has_bt:
            case_context.wifi_or_bt = "wifi_bt"
        elif has_wifi:
            case_context.wifi_or_bt = "wifi"
        else:
            case_context.wifi_or_bt = "bt"
    

    @staticmethod
    def load_case_summary_prompt(wifi_or_bt):
        # Mixed Wi-Fi + BT cases share the Wi-Fi summary prompt (no dedicated
        # prompt_wifi_bt.py template ships with the repo).
        key = (wifi_or_bt or "").lower()
        if key == "wifi_bt":
            key = "wifi"
        prompt_filename = f"prompt_{key}.py"
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
        # Some Snowflake rows have a NULL comment text — comm[2] then comes
        # back as None. Both `"Download link" in None` and `None.replace(...)`
        # raise — the former throws exactly the cryptic
        # `argument of type 'NoneType' is not iterable` error that case
        # 00984509 hit. Coerce None to "" so a single bad row never
        # poisons the whole case-processing pipeline.
        for comm in (comments or []):
            comm = list(comm)
            text = comm[2] if comm[2] is not None else ""
            if "Download link" in text:
                att = text.split(' \xa0 \xa0 ')
                if len(att) >= 2:
                    att_info[att[-2]] = [comm[0], att[-1]]
            comm[2] = text.replace('\xa0', ' ').replace('\n', ' ').strip()
            processed_comments.append(comm)

        return processed_comments, att_info
    
    @staticmethod
    def _is_valid_pdf(resp) -> bool:
        """Return True only if the response is a real PDF (Content-Type + magic bytes)."""
        content_type = resp.headers.get("Content-Type", "")
        return "application/pdf" in content_type or resp.content[:4] == b"%PDF"

    @staticmethod
    def _supplement_attachment_info_from_api(case_id, att_info):
        """Query Salesforce REST API for case comments and supplement att_info
        with entries not found in Snowflake (handles replication lag)."""
        # Defensive: cases where Snowflake returned no comments leave
        # att_info as an empty dict, but the upstream contract isn't
        # enforced at the type level — if a caller ever passes None we
        # would die at `filename in att_info` with the cryptic
        # `argument of type 'NoneType' is not iterable`. Treat None
        # exactly like {} so the supplementing pass still runs.
        if att_info is None:
            print(f"  [SF API] att_info was None for case {case_id}; treating as empty dict")
            att_info = {}
        try:
            vf_session = CaseService._get_vf_session()
            print(f"  [SF API] Supplementing attachment info for case {case_id}...")

            auth_headers = CaseService._build_salesforce_api_headers(vf_session)
            api_ver = CaseService._get_salesforce_api_version(vf_session, auth_headers)

            t = time.time()
            resp = CaseService._query_salesforce_case_comments(case_id, vf_session, api_ver, auth_headers)

            # Re-auth once if the sid backing the bearer token has expired.
            if resp.status_code == 401:
                print("  [SF API] Session invalid for REST API, re-authenticating...")
                CaseService._invalidate_vf_session()
                vf_session = CaseService._get_vf_session()
                auth_headers = CaseService._build_salesforce_api_headers(vf_session)
                api_ver = CaseService._get_salesforce_api_version(vf_session, auth_headers)
                resp = CaseService._query_salesforce_case_comments(case_id, vf_session, api_ver, auth_headers)

            resp.raise_for_status()
            records = resp.json().get("records", [])
            print(f"  [SF API] Got {len(records)} comment record(s) in {time.time() - t:.2f}s")

            for rec in records:
                rich_text = rec.get("Core_IPS_Rich_Comment__c") or ""
                created_date = rec.get("CreatedDate")

                urls = CaseService._extract_attachment_urls_from_rich_text(rich_text)
                if not urls:
                    continue

                for url in urls:
                    filename = CaseService._extract_filename_from_attachment_url(url)
                    if not filename or filename in att_info:
                        continue

                    desc = CaseService._extract_attachment_desc_from_rich_text(rich_text, filename)

                    att_info[filename] = [created_date, desc]
                    print(f"  [SF API] Supplemented '{filename}': '{desc}'")

        except Exception as e:
            print(f"  [SF API] Could not supplement attachment info: {e}")

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
        print(f"  [PDF] Session ready, sending request to: {pdf_url}")
        resp = vf_session.get(pdf_url, timeout=60, allow_redirects=True)
        print(f"  [PDF] Response: status={resp.status_code}, content-type={resp.headers.get('Content-Type','?')}, size={len(resp.content)}")

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
        case_context = CaseService._apply_case_fields(case_context, case_fields)
        
        with ThreadPoolExecutor(max_workers=2) as executor:
            comment_future = executor.submit(CaseService._get_case_comments_from_snowflake, case_context.id, key.snowflake_passwd)
            pdf_future = executor.submit(CaseService._download_pdf_by_url, case_context.id, case_context.case_download_dir)
            
            case_context.comments, case_context.attachment_info = comment_future.result(timeout=100)
            case_context.ips_pdf_path = pdf_future.result(timeout=100)

        CaseService._supplement_attachment_info_from_api(case_context.id, case_context.attachment_info)
            
        return case_context

    @staticmethod
    def _build_salesforce_api_headers(vf_session: _requests.Session) -> dict:
        sid_cookie = next(
            (
                cookie.value
                for cookie in vf_session.cookies
                if cookie.name == "sid" and cookie.domain == "intel.my.salesforce.com"
            ),
            None,
        )
        if not sid_cookie:
            raise RuntimeError("No Salesforce sid cookie found for intel.my.salesforce.com")
        return {"Authorization": f"Bearer {sid_cookie}"}

    @staticmethod
    def _get_salesforce_api_version(vf_session: _requests.Session, auth_headers: dict) -> str:
        resp = _requests.get(
            f"{CaseService._SF_API_BASE_URL}/services/data/",
            headers=auth_headers,
            timeout=15,
            proxies=vf_session.proxies,
        )
        resp.raise_for_status()
        return resp.json()[-1]["version"]

    @staticmethod
    def _query_salesforce_case_comments(case_id, vf_session: _requests.Session, api_ver: str, auth_headers: dict):
        soql = (
            f"SELECT CreatedDate, Core_IPS_Rich_Comment__c "
            f"FROM Core_IPS_Case_Comments__c "
            f"WHERE Core_IPS_Case__c='{case_id}'"
        )
        return _requests.get(
            f"{CaseService._SF_API_BASE_URL}/services/data/v{api_ver}/query",
            headers=auth_headers,
            params={"q": soql},
            timeout=15,
            proxies=vf_session.proxies,
        )

    @staticmethod
    def _extract_attachment_urls_from_rich_text(rich_text: str):
        import re
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(rich_text, "html.parser")
        urls = [
            anchor["href"]
            for anchor in soup.find_all("a", href=True)
            if "https://esft.intel.com/sftservices/download" in anchor["href"]
        ]
        if urls:
            return urls
        return re.findall(r'https://esft\.intel\.com/sftservices/download/[^"\'<>\s]+', rich_text)

    @staticmethod
    def _extract_filename_from_attachment_url(url: str) -> str:
        from urllib.parse import urlparse, parse_qs, unquote

        qs = parse_qs(urlparse(url).query)
        return unquote(qs.get("FileName", [""])[0])

    @staticmethod
    def _extract_attachment_desc_from_rich_text(rich_text: str, filename: str) -> str:
        import re
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(rich_text, "html.parser")
        text_lines = [line.strip() for line in soup.get_text(separator='\n').split('\n') if line.strip()]

        for idx, line in enumerate(text_lines):
            if filename not in line:
                continue
            if line.startswith(filename):
                parts = [part.strip() for part in re.split(r'(?:\s*\xa0\s*){2,}|\s{4,}', line) if part.strip()]
                for part in reversed(parts):
                    if part != filename:
                        return part
            if line != filename:
                return line
            if idx + 1 < len(text_lines):
                return text_lines[idx + 1]

        return os.path.splitext(filename)[0]
    
 
    
    
    
