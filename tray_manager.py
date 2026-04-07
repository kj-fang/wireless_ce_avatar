"""
Intel Wireless CE Avatar - System Tray Manager

Runs as a separate persistent process (--tray-mode).
Monitors running_avatar.json written by the app instance on startup.
"""

import json
import logging
import glob
import os
import subprocess
import sys
import threading
import time
import traceback
import psutil
import pystray
from PIL import Image
from utils.port_utils import get_user_data_dir


def _base_path() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

 

def _icon_image() -> Image.Image:
    try:
        search_dirs = [
            getattr(sys, '_MEIPASS', ''),
            os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else '',
            os.path.dirname(os.path.abspath(__file__)),
        ]
        for directory in search_dirs:
            if not directory:
                continue
            icon_path = os.path.join(directory, 'icon.ico')
            if os.path.exists(icon_path):
                return Image.open(icon_path)
    except Exception:
        pass
    return Image.new('RGB', (64, 64), (0, 120, 212))


class TrayManager:
    def __init__(self):
        self.base = _base_path()
        self.instance_file = os.path.join(get_user_data_dir(), 'running_avatar.json')
        self.instances = []
        self.icon = None
        self.logger = self._init_log()
        self.logger.info(f'TrayManager initialized | base={self.base} | cwd={os.getcwd()}')

    def _tool_exe_patterns(self) -> list[str]:
        if getattr(sys, 'frozen', False):
            meipass = getattr(sys, '_MEIPASS', '')
            candidates = [
                os.path.join(meipass, 'services', 'driver_download', 'downloadDriver_*.exe'),
            ]
        else:
            candidates = [
                os.path.join(self.base, 'services', 'driver_download', 'downloadDriver_*.exe'),
            ]

        return [pattern for pattern in candidates if pattern]

    def _resolve_tool_exe_path(self) -> str:
        matches = []
        patterns = self._tool_exe_patterns()
        self.logger.info(f'Resolving tool executable from patterns: {patterns}')
        print(patterns)
        for pattern in patterns:
            matches.extend(glob.glob(pattern))
        self.logger.info(f'Tool executable matches: {matches}')
        if not matches:
            return ''
        return max(matches, key=os.path.getmtime)

    def _init_log(self) -> logging.Logger:
        log_path = os.path.join(get_user_data_dir(), 'tray.log')
        logger = logging.getLogger('TrayManager')
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.handlers.clear()

        try:
            file_handler = logging.FileHandler(log_path, encoding='utf-8')
            file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
            logger.addHandler(file_handler)
        except Exception:
            pass

        return logger

    def _is_intelavatar_process(self, pid) -> bool:
        """Return True only if pid belongs to an IntelAvatar app process."""
        try:
            proc = psutil.Process(pid)
            name = (proc.name() or '').lower()
            cmdline = [part.lower() for part in (proc.cmdline() or [])]
            if name == 'intelavatar.exe':
                return True
            if name in ('python.exe', 'pythonw.exe'):
                if any('app.py' in part for part in cmdline):
                    return True
            if any('intelavatar' in part and ('exe' in part or 'app.py' in part) for part in cmdline):
                return True
        except Exception:
            pass
        return False

    def _scan_instances(self) -> list:
        live_instances = []
        try:
            if os.path.exists(self.instance_file):
                with open(self.instance_file, encoding='utf-8') as file_obj:
                    instance = json.load(file_obj)
                pid = instance.get('pid')
                if pid and psutil.pid_exists(pid) and self._is_intelavatar_process(pid):
                    live_instances.append(instance)
                else:
                    os.remove(self.instance_file)
                    self.logger.info(f'Removed stale instance file: {self.instance_file}')
        except Exception as error:
            self.logger.warning(f'Corrupt instance file {self.instance_file}: {error} — removing it.')
            try:
                os.remove(self.instance_file)
            except Exception:
                pass
        return live_instances

    def _launch_instance(self):
        try:
            launch_flags = getattr(subprocess, 'CREATE_NEW_CONSOLE', 0)
            if getattr(sys, 'frozen', False):
                subprocess.Popen([sys.executable], creationflags=launch_flags)
            else:
                app_path = os.path.join(self.base, 'app.py')
                subprocess.Popen([sys.executable, app_path], creationflags=launch_flags, cwd=self.base)
            self.logger.info('Launched new instance')
        except Exception as error:
            self.logger.error(f'Launch failed: {error}')

    def _launch_tool_exe(self):
        exe_path = self._resolve_tool_exe_path()
        if not exe_path:
            self.logger.error(f'Tool executable not found. Tried patterns: {self._tool_exe_patterns()}')
            return

        try:
            subprocess.run(["explorer", exe_path], check=False)
            self.logger.info(f'Launched tool executable: {exe_path}')
        except Exception as error:
            self.logger.error(f'Tool launch failed: {error}')

    def _current_instance(self):
        return self.instances[0] if self.instances else None

    def _stop_instance(self, instance: dict):
        pid = instance.get('pid')
        port = instance.get('port')
        self.logger.info(f'Stopping instance PID={pid}, Port={port}')

        try:
            process = psutil.Process(pid)
            for child in process.children(recursive=True):
                try:
                    # Never terminate the tray manager process from "Stop This Instance"
                    child_cmdline = []
                    try:
                        child_cmdline = child.cmdline() or []
                    except Exception:
                        pass
                    child_cmdline_lower = [part.lower() for part in child_cmdline]
                    is_tray = (
                        '--tray-mode' in child_cmdline_lower
                        or any('tray_manager.py' in part for part in child_cmdline_lower)
                        or (child.pid == os.getpid())
                    )
                    if is_tray:
                        self.logger.info(f'Skipping tray child process PID={child.pid}')
                        continue
                    child.terminate()
                except Exception:
                    pass
            process.terminate()
        except (psutil.NoSuchProcess, TypeError):
            pass
        except Exception as error:
            self.logger.error(f'Stop failed: {error}')

        try:
            if os.path.exists(self.instance_file):
                os.remove(self.instance_file)
        except Exception:
            pass

        self.instances = [item for item in self.instances if item.get('pid') != pid]
        self._push_menu()

    def _quit(self):
        self.logger.info('Tray manager quitting')
        self._stop_current_instance()
        if self.icon:
            self.icon.stop()

    def _stop_current_instance(self):
        instance = self._current_instance()
        if instance:
            self._stop_instance(instance)

    def _build_menu(self) -> pystray.Menu:
        instance = self._current_instance()
        has_instance = instance is not None
        port = instance.get('port', '?') if has_instance else None

        running_label = f'Running IntelAvatar (Port {port})' if has_instance else 'No IntelAvatar running'

        items = [
            pystray.MenuItem(
                'Launch IntelAvatar',
                lambda icon, item: self._launch_instance(),
                enabled=(not has_instance)
            ),
            pystray.MenuItem(running_label, None, enabled=False),
            pystray.MenuItem(
                'Stop Current IntelAvatar',
                lambda icon, item: self._stop_current_instance(),
                enabled=has_instance
            ),
            pystray.MenuItem(
                'Driver Download Tool',
                lambda icon, item: self._launch_tool_exe(),
                enabled=True
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Quit IntelAvatar', lambda icon, item: self._quit()),
        ]

        return pystray.Menu(*items)

    def _push_menu(self):
        if not self.icon:
            return
        try:
            self.icon.menu = self._build_menu()
            self.icon.update_menu()
        except Exception as error:
            self.logger.error(f'Menu update failed: {error}')

    def _monitor(self):
        while True:
            time.sleep(1)
            live_instances = self._scan_instances()
            old_pids = sorted(item.get('pid') for item in self.instances)
            new_pids = sorted(item.get('pid') for item in live_instances)
            if old_pids != new_pids:
                self.instances = live_instances
                self._push_menu()

    def run(self):
        try:
            self.logger.info(f'Tray starting | base={self.base} | instance_file={self.instance_file}')
            self.instances = self._scan_instances()

            monitor_thread = threading.Thread(target=self._monitor, daemon=True)
            monitor_thread.start()

            self.icon = pystray.Icon(
                'IntelAvatar',
                _icon_image(),
                'Intel Wireless CE Avatar',
                self._build_menu(),
            )
            self.logger.info('Tray icon running')
            self.icon.run()
        except Exception as error:
            self.logger.error(f'Tray startup failed: {error}')
            self.logger.error(traceback.format_exc())


if __name__ == '__main__':
    TrayManager().run()