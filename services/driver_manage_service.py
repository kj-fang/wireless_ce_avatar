import os, re, sys
import shutil
import subprocess
import requests
import platform
from tqdm import tqdm
import zipfile
import threading
import time
import signal
import traceback
import warnings
import logging


from selenium import webdriver
from selenium.webdriver.chrome.service import Service

from utils import helpers



class DriverManager:
    def __init__(self, downloads_dir):
        self.all_drivers = []
        self.shutdown_event = threading.Event()
        self.main_driver = None
        

        driver_dir = os.path.join(downloads_dir, "chrome_driver")
        self.chrome_driver_path = self.setup_chromedriver(driver_dir)

    def run_driver(self, socketio, app):
        try:
            signal.signal(signal.SIGINT, self.signal_handler)
            signal.signal(signal.SIGTERM, self.signal_handler)
            
            rand_port = helpers.get_available_port(54000, 60000)
            threading.Timer(1.5, self.open_browser, args=(rand_port, )).start()
            socketio.run(app,  host="0.0.0.0", debug=False, port=rand_port)
        except Exception as e:
            print("❌  An error has occurred：")
            traceback.print_exc() 
            input("	Press Enter to close this window...") 

    def create_download_driver(self, download_path, additional_prefs=None , 
                               additional_args=None, performance_logging=False, headless=True):
        for proxy_var in ["HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"]:
            os.environ.pop(proxy_var, None)
        warnings.simplefilter(action='ignore', category=ResourceWarning)
        logging.basicConfig(level=logging.ERROR)

        options = webdriver.ChromeOptions()
        prefs = {
            'download.default_directory': download_path,
            'profile.default_content_settings.popups': 0,
            'download.prompt_for_download': False,
            'download.directory_upgrade': True, 
            "plugins.always_open_pdf_externally": True,
        }
        if additional_prefs:
            prefs.update(additional_prefs)
        options.add_experimental_option('prefs', prefs)
        performance_args = [
            '--no-sandbox',
            '--disable-dev-shm-usage',
            '--disable-gpu',
            '--disable-extensions',
            '--disable-plugins',
            '--disable-images',
            '--window-size=2560x1440',
            '--log-level=3',
            '--disable-logging',
            '--disable-background-timer-throttling',
            '--disable-backgrounding-occluded-windows',
            '--disable-renderer-backgrounding'
            '--ignore-certificate-errors'
        ]
        if additional_args:
            performance_args.extend(additional_args)
        if headless:
            performance_args.append('--headless=new')
        for arg in performance_args:
            options.add_argument(arg)

        if performance_logging:
            options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

        service = Service(self.chrome_driver_path )
        driver = webdriver.Chrome(service=service, options=options)
        self.all_drivers.append(driver)
        return driver
        

    def setup_chromedriver(self, driver_dir):
        """set ChromeDriver"""
        try:
            for proxy_var in ["HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"]:
                os.environ.pop(proxy_var, None)
            print("🔄 Setting up ChromeDriver...")
            driver_path = self.chrome_driver_init(driver_dir)
            print(f"✅ ChromeDriver installed: {driver_path}")
            return driver_path
        except Exception as e:
            print(f"❌ ChromeDriver setup failed: {e}")
            return None

    def open_browser(self, port):
        for proxy_var in ["HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"]:
            os.environ.pop(proxy_var, None)
        options = webdriver.ChromeOptions()
        options.add_argument('ignore-certificate-errors')
        options.add_argument("--disable-gpu")
        options.add_argument("--log-level=3")
        options.add_experimental_option("detach", True) 
        if not self.chrome_driver_path:
            print("⚠️ No pre-installed driver, downloading now...")
            self.chrome_driver_path = self.setup_chromedriver()
            print(f"✅ ChromeDriver ready: {self.chrome_driver_path}")
        
        self.main_driver = webdriver.Chrome(service=Service(self.chrome_driver_path), options=options)
        self.main_driver.get(f"http://localhost:{port}")
        threading.Thread(target=self.monitor_browser, daemon=True).start()
    
    def get_chrome_version(self):
        """get local chrome version"""
        try:
            # Windows
            result = subprocess.run([
                'reg', 'query', 
                'HKEY_CURRENT_USER\\Software\\Google\\Chrome\\BLBeacon', 
                '/v', 'version'
            ], capture_output=True, text=True)
            
            if result.returncode == 0:
                version = re.search(r'(\d+\.\d+\.\d+\.\d+)', result.stdout)
                if version:
                    return version.group(1)
        except:
            pass
    
    def get_windows_type(self):
        arch = platform.architecture()[0]
        if arch == '64bit':
            return 'win64'
        else:
            return 'win32'
        
    def get_available_chromedriver_version(self, target_version, os_type, proxies):
        """Find the closest available ChromeDriver version"""
        try:
            # Try exact version first
            test_url = f"https://storage.googleapis.com/chrome-for-testing-public/{target_version}/{os_type}/chromedriver-{os_type}.zip"
            test_response = requests.head(test_url, proxies=proxies, timeout=10)
            if test_response.status_code == 200:
                print(f"✅ Found exact version: {target_version}")
                return target_version
        except:
            pass
        
        print(f"⚠️ Exact version {target_version} not found, searching for compatible version...")
        
        try:
            # Get list of available versions from the known-good-versions endpoint
            known_versions_url = "https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json"
            response = requests.get(known_versions_url, proxies=proxies, timeout=15)
            response.raise_for_status()
            data = response.json()
            
            # Parse target version
            target_parts = [int(x) for x in target_version.split('.')]
            target_major = target_parts[0]
            
            # Find all versions with matching major version
            compatible_versions = []
            for version_info in data.get('versions', []):
                version = version_info.get('version', '')
                if not version:
                    continue
                    
                version_parts = [int(x) for x in version.split('.')]
                if version_parts[0] == target_major:
                    # Check if this version has chromedriver available for our OS
                    downloads = version_info.get('downloads', {})
                    chromedriver_downloads = downloads.get('chromedriver', [])
                    if any(d.get('platform') == os_type for d in chromedriver_downloads):
                        compatible_versions.append(version)
            
            if compatible_versions:
                # Use the latest compatible version
                compatible_versions.sort(key=lambda v: [int(x) for x in v.split('.')])
                selected_version = compatible_versions[-1]
                print(f"✅ Found compatible version: {selected_version}")
                return selected_version
            
            # If no compatible version found, try the last-known-good endpoint
            print(f"⚠️ No compatible version found in known-good list, trying last-known-good...")
            lkg_url = f"https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json"
            lkg_response = requests.get(lkg_url, proxies=proxies, timeout=10)
            lkg_response.raise_for_status()
            lkg_data = lkg_response.json()
            
            # Try Stable channel first
            channels_to_try = ['Stable', 'Beta', 'Dev', 'Canary']
            for channel in channels_to_try:
                channel_data = lkg_data.get('channels', {}).get(channel, {})
                version = channel_data.get('version')
                if version:
                    downloads = channel_data.get('downloads', {})
                    chromedriver_downloads = downloads.get('chromedriver', [])
                    if any(d.get('platform') == os_type for d in chromedriver_downloads):
                        print(f"✅ Using {channel} channel version: {version}")
                        return version
                        
        except Exception as e:
            print(f"⚠️ Error finding compatible version: {e}")
        
        # Last resort: return the original version and let it fail
        print(f"⚠️ Could not find compatible version, attempting original: {target_version}")
        return target_version

    def chrome_driver_init(self, driver_dir):
        current_chrome_version = self.get_chrome_version()

        driver_exe_path = fr"{driver_dir}/{current_chrome_version}/chromedriver.exe"
        ## check correct version exists
        if os.path.exists(driver_exe_path):
            print("correct driver exists! ", driver_exe_path)
            return driver_exe_path ## if exist: return 
        # if need download
        # remove
        if os.path.exists(driver_dir):
            print("remove", driver_dir)
            shutil.rmtree(driver_dir)
            os.makedirs(driver_dir)

        os_type = self.get_windows_type()

        proxies = {
            'http': "http://proxy-dmz.intel.com:911",
            'https': "http://proxy-dmz.intel.com:912"
        }
        
        # Find an available ChromeDriver version
        available_version = self.get_available_chromedriver_version(current_chrome_version, os_type, proxies)
        url = fr"https://storage.googleapis.com/chrome-for-testing-public/{available_version}/{os_type}/chromedriver-{os_type}.zip"

        filename = "chromedriver.zip"
        file_path = fr"{driver_dir}/{filename}"
        print("🔄 Download...", url)
        response = requests.get(url, proxies=proxies, stream=True)
        response.raise_for_status()

        total_size = int(response.headers.get('content-length', 0))
        with open(file_path, 'wb') as f, tqdm(
            desc=filename,
            total=total_size,
            unit='B',
            unit_scale=True,
            unit_divisor=1024,
        ) as pbar:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    pbar.update(len(chunk))

        print(f"✅ Download complete: {file_path}")

        with zipfile.ZipFile(file_path, 'r') as zip_ref:
            zip_ref.extractall(driver_dir)

        exe_file = None
        for root, dirs, files in os.walk(driver_dir):
            for file in files:
                if file.endswith('.exe'):
                    exe_file = os.path.join(root, file)
                    break

        version_dir = os.path.join(driver_dir, current_chrome_version)
        os.makedirs(version_dir, exist_ok=True)

        exe_name = os.path.basename(exe_file)
        destination = os.path.join(version_dir, exe_name)
        shutil.move(exe_file, destination)
        print(f"move {exe_name} to {version_dir}")

        for item in os.listdir(driver_dir):
            item_path = os.path.join(driver_dir, item)
            if item != current_chrome_version and os.path.exists(item_path):
                if os.path.isdir(item_path):
                    shutil.rmtree(item_path)
                else:
                    os.remove(item_path)

        return rf"{version_dir}\{exe_name}"
    
    def monitor_browser(self):
        while not self.shutdown_event.is_set():
            if self.is_browser_closed():
                print("🛑 Browser was closed by user.")
                self.shutdown()
                break
            time.sleep(2)

    def is_browser_closed(self):
        try:
            return len(self.main_driver.window_handles) == 0
        except Exception:
            return True

    def shutdown(self):
        print("📴 Received shutdown from client")
        print("all_drivers", self.all_drivers)
        
        for driver in self.all_drivers:
            try:
                print("now closing: ", driver)
                driver.quit()
            except Exception as e:
                print(f"⚠️ Failed to quit driver: {e}")
        self.shutdown_event.set()
        print("All ChromeDriver instances stopped. Shutting down server.")
        os.kill(os.getpid(), signal.SIGINT) 
        sys.exit(0)
        return 'Server shutting down...'
    
    def signal_handler(self, sig, frame):
        print("CTRL+C detected. Shutting down...")
        self.shutdown_event.set()  # notify all sub-thread to stop
        sys.exit(0) 
