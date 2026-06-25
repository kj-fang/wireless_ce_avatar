from selenium import webdriver
from selenium.webdriver.chrome.service import Service
#from webdriver_manager.chrome import ChromeDriverManager
import time
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
import json
from datetime import datetime
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
            result = future.result()
            if result is None:
                continue
            [file_path, name, already_dload] = result
            print("thread done: ", file_path, name)
            if file_path:
                all_file_path.append([file_path, name, already_dload])
                yield [file_path, name, already_dload]

def extract_content_length(logs):
    max_size = 0
    for entry in logs:
        try:
            log = json.loads(entry["message"])["message"]
        except (KeyError, json.JSONDecodeError, TypeError):
            continue
        if log.get("method") == "Network.responseReceived":
            try:
                url = log["params"]["response"]["url"]
                headers = {k.lower(): v for k, v in log["params"]["response"]["headers"].items()}
                if "esft.intel.com" in url and "content-length" in headers:
                    size = int(headers["content-length"])
                    print("✅ URL:", url)
                    print("📦 Content-Length:", size)
                    if size > max_size:
                        max_size = size
            except Exception as e:
                continue
    return max_size

STALL_TIMEOUT = 45   # If no progress for this many seconds, consider the download stalled

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

    max_retry = 3
    retry = 0

    progress_data[name] = 0
    
    while (retry < max_retry) and not driver_manager.shutdown_event.is_set():
        driver = None
        pbar = None
        if os.path.exists(temp_path):
            print(f"⚠️ Removing stale partial download before retry {retry + 1}.")
            os.remove(temp_path)
        try:
            driver = driver_manager.create_download_driver(download_path, performance_logging=True)
            driver.get(url)

            time.sleep(5)
            logs = driver.get_log("performance")
            file_size_bytes = extract_content_length(logs)
            if not file_size_bytes:
                raise ValueError(f"Could not get Content-Length for {name}, will retry")
            if socketio:
                socketio.emit('file_info', {
                    'name': name,
                    'size': file_size_bytes
                }, namespace='/progress')
            
            print("File path:", file_path)

            pbar = tqdm(total=file_size_bytes, unit='B', unit_scale=True, desc=name)
            last_size = 0
            stall_elapsed = 0
            while True:
                if driver_manager.shutdown_event.is_set():
                    pbar.close()
                    return
                
                time.sleep(0.5)

                if os.path.exists(temp_path):
                    initial_size = os.path.getsize(temp_path)

                    if initial_size > last_size:
                        last_size = initial_size
                        stall_elapsed = 0
                    else:
                        stall_elapsed += 0.5
                        if stall_elapsed >= STALL_TIMEOUT:
                            raise TimeoutError(f"Download stalled for {name}")

                    pbar.update(initial_size - pbar.n)
                    progress_data[name] = initial_size / file_size_bytes * 100
                    rate = pbar.format_dict.get('rate')
                    eta_seconds = (pbar.total - pbar.n) / rate if rate else None
                    if socketio:
                        socketio.emit('progress_update', {
                            'name': name,
                            'progress': progress_data[name],
                            'eta': eta_seconds if eta_seconds is not None else None
                        }, namespace='/progress')

                elif os.path.exists(file_path):
                    pbar.update(file_size_bytes - pbar.n)
                    rate = pbar.format_dict.get('rate')
                    eta_seconds = (pbar.total - pbar.n) / rate if rate else None
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

                else:
                    stall_elapsed += 0.5
                    if stall_elapsed >= STALL_TIMEOUT:
                        raise TimeoutError(f"Download stalled (no file) for {name}")
                
        except Exception as e:
            print(f"Download failed {e}")
            print(f"Retry download file: {name}")
            retry += 1
            if socketio and retry < max_retry:
                if isinstance(e, TimeoutError):
                    retry_reason = 'stall_timeout'
                elif isinstance(e, ValueError):
                    retry_reason = 'missing_content_length'
                else:
                    retry_reason = 'unexpected_error'
                socketio.emit('download_retry', {
                    'name': name,
                    'retry': retry,
                    'max_retry': max_retry,
                    'stall_timeout': STALL_TIMEOUT,
                    'reason': retry_reason,
                    'retry_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }, namespace='/progress')
        finally:
            if pbar is not None:
                pbar.close()
            if driver:
                try:
                    driver.quit()
                except Exception as quit_error:
                    print(f"Driver quit failed for {name}: {quit_error}")
                finally:
                    if driver in driver_manager.all_drivers:
                        driver_manager.all_drivers.remove(driver)
            print("done")

    print(f"❌ All retries failed for {name}")
    if socketio:
        socketio.emit('file_download_failed', {'name': name}, namespace='/progress')
    return [None, name, already_dload]

