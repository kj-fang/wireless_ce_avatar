import os
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

    @staticmethod
    def process_case(case_context: CaseContext) -> CaseContext:
        
        key = app_config.key
        if not case_context.case_nbr:
            return case_context  
        ## create download folder for ips case
        case_context.case_download_dir = os.path.join(f"{app_config.avatarfiles_dir}\{case_context.case_nbr}")
        os.makedirs(case_context.case_download_dir, exist_ok=True)
        print("-----download_path:-----", case_context.case_download_dir)


        case_fields = CaseService._get_case_info_from_snowflake(case_context.case_nbr[2:], key.snowflake_passwd)

        if  case_fields is not None:

            (case_context.id, 
            case_context.subject, 
            case_context.env_detail, 
            case_context.description, 
            case_context.backend_id, 
            case_context.subcategory) = case_fields

            with ThreadPoolExecutor(max_workers=2) as executor:
                comment_future = executor.submit(CaseService._get_case_comments_from_snowflake,  case_context.id, key.snowflake_passwd)
                pdf_future = executor.submit(CaseService._download_pdf_by_url,  case_context.id, case_context.case_download_dir)
                case_context.comments, case_context.attachment_info = comment_future.result(timeout=100)
                case_context.ips_pdf_path = pdf_future.result(timeout=100)

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
    def _download_pdf_by_url(case_id, download_path):
        pdf_url = f"https://intel--c.vf.force.com/apex/Core_IPS_Case_ExportPDF_LEX?id={case_id}"
        return CaseService._download_pdf_common(pdf_url, download_path)
    
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
    
 
    
    
    
