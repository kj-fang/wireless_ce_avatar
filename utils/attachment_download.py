from selenium import webdriver
from selenium.webdriver.chrome.service import Service
#from webdriver_manager.chrome import ChromeDriverManager
import time
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
import json
from configs.global_configs import app_config
from services.driver_manage_service import DriverManager

# data for progress bar
progress_data = {}

def run_dload_threads(att_list, download_path, socketio):
    print("start run_dload_threads")

    global progress_data
    progress_data = {}
    all_file_path = []

    driver_manager = app_config.driver_manager
    print("download_path:", download_path)
    
    with ThreadPoolExecutor(max_workers=15) as executor:
        futures = [executor.submit(download_file, name, url, download_path, driver_manager, socketio) for name, url, _ in att_list]
        for future in as_completed(futures):
            if driver_manager.shutdown_event.is_set():
                print("shutddown!!!", driver_manager.shutdown_event)
                break
            [file_path, name, already_dload] = future.result()
            print("thread done: ", file_path, name)
            if file_path:
                all_file_path.append([file_path, name, already_dload])
                yield [file_path, name, already_dload]

def extract_content_length(logs):
    for entry in logs:
        log = json.loads(entry["message"])["message"]
        if log["method"] == "Network.responseReceived":
            try:
                url = log["params"]["response"]["url"]
                headers = log["params"]["response"]["headers"]
                if "esft.intel.com" in url and "Content-Length" in headers:
                    print("✅ URL:", url)
                    print("📦 Content-Length:", headers["Content-Length"])
                    return int(headers["Content-Length"])
            except Exception as e:
                continue
    return None

def download_file(name, url, download_path, driver_manager: DriverManager, socketio):

    os.makedirs(download_path, exist_ok=True)
    already_dload = False
    file_path = os.path.join(download_path, name)
    temp_path = os.path.join(download_path, name + ".crdownload")
    if (os.path.exists(file_path)):
        print(f"file exist: {file_path}")
        already_dload = True
        return [file_path, name, already_dload]
    if os.path.exists(temp_path):
        print("⚠️ Last download failed. Removing.")
        os.remove(temp_path)

    driver = driver_manager.create_download_driver(download_path, performance_logging=True)
    
    max_retry = 3
    retry = 0

    progress_data[name] = 0
    
    while (retry < max_retry) and not driver_manager.shutdown_event.is_set():
        try:
            driver.get(url)

            time.sleep(5)
            logs = driver.get_log("performance")
            file_size_bytes = extract_content_length(logs)
            if file_size_bytes > 0:
                socketio.emit('file_info', {
                    'name': name,
                    'size': file_size_bytes
                }, namespace='/progress')
            
            print("File path:", file_path)

            pbar = tqdm(total=file_size_bytes, unit='B', unit_scale=True, desc=name)
            while True:
                if driver_manager.shutdown_event.is_set():
                    driver.quit()
                    if driver in driver_manager.all_drivers:
                        driver_manager.all_drivers.remove(driver)
                    return
                
                time.sleep(0.5)
                if os.path.exists(temp_path):
                    initial_size = os.path.getsize(temp_path)
                    pbar.update(initial_size - pbar.n)
                    progress_data[name] = initial_size / file_size_bytes * 100
                    eta_seconds = (pbar.total - pbar.n) / pbar.format_dict['rate']
                    if socketio:
                        socketio.emit('progress_update', {
                            'name': name,
                            'progress': progress_data[name],
                            'eta': eta_seconds if eta_seconds is not None else None
                        }, namespace='/progress')

                elif os.path.exists(file_path):
                    pbar.update(file_size_bytes - pbar.n) 
                    eta_seconds = (pbar.total - pbar.n) / pbar.format_dict['rate'] 
                    pbar.close()
                    print(f"Download done! {name}")
                    progress_data[name] = 100
                    if socketio:
                        socketio.emit('progress_update', {
                            'name': name,
                            'progress': progress_data[name],
                            'eta': int(eta_seconds) if eta_seconds is not None else None
                        }, namespace='/progress')
                    return [file_path, name, already_dload]
                
        except Exception as e:
            print(f"Download failed {e}")
            print(f"Retry download file: {name}")
            retry += 1
        finally:
            
            driver.quit()
            if driver in driver_manager.all_drivers:
                driver_manager.all_drivers.remove(driver)
            print("done")

