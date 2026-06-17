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

_logger = logging.getLogger('TrayManager')


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
        _logger.debug(f'[ICON] Searching for icon.ico in dirs: {[d for d in search_dirs if d]}')
        for directory in search_dirs:
            if not directory:
                continue
            icon_path = os.path.join(directory, 'icon.ico')
            if os.path.exists(icon_path):
                _logger.info(f'[ICON] Found icon at: {icon_path}')
                return Image.open(icon_path)
    except Exception as e:
        _logger.warning(f'[ICON] Exception loading icon: {e}')
    _logger.warning('[ICON] icon.ico not found — using fallback colour block')
    return Image.new('RGB', (64, 64), (0, 120, 212))


class TrayManager:
    def __init__(self):
        self.base = _base_path()
        self.instance_file = os.path.join(get_user_data_dir(), 'running_avatar.json')
        self.instances = []
        self.icon = None
        self.logger = self._init_log()
        self.logger.info(f'TrayManager initialized | base={self.base} | cwd={os.getcwd()} | instance_file={self.instance_file} | frozen={getattr(sys, "frozen", False)}')

    #tool path to the driver download tool exe
    def _tool_exe_patterns(self) -> list[str]:
        if getattr(sys, 'frozen', False):
            meipass = getattr(sys, '_MEIPASS', '')
            candidates = []
            if meipass:
                candidates.append(
                    os.path.join(meipass, 'services', 'driver_download', 'downloadDriver_*.exe')
                )
            # In onedir builds, bundled files may be located next to the executable.
            candidates.extend([
                os.path.join(self.base, 'services', 'driver_download', 'downloadDriver_*.exe'),
                os.path.join(self.base, '_internal', 'services', 'driver_download', 'downloadDriver_*.exe'),
            ])
        else:
            candidates = [
                os.path.join(self.base, 'services', 'driver_download', 'downloadDriver_*.exe'),
            ]

        return [pattern for pattern in candidates if pattern]

    #resolve the most recently modified tool exe matching the patterns
    def _resolve_tool_exe_path(self) -> str:
        matches = []
        patterns = self._tool_exe_patterns()
        self.logger.info(f'Resolving tool executable from patterns: {patterns}')
        for pattern in patterns:
            matches.extend(glob.glob(pattern))
        self.logger.info(f'Tool executable matches: {matches}')
        if not matches:
            return ''
        return max(matches, key=os.path.getmtime)

    def _init_log(self) -> logging.Logger:
        logger = logging.getLogger('TrayManager')
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.handlers.clear()

        fmt = logging.Formatter('%(asctime)s [%(levelname)s] [TRAY] %(message)s',
                                datefmt='%Y-%m-%d %H:%M:%S')

        # Write to a dedicated tray.log rather than sharing avatar.log with the
        # main process. On Windows, RotatingFileHandler rotates by renaming the
        # file; if the tray process holds avatar.log open via its own handle,
        # that rename fails and rotation breaks entirely.
        try:
            tray_log = os.path.join(get_user_data_dir(), 'tray.log')
            tray_handler = logging.FileHandler(tray_log, mode='a', encoding='utf-8')
            tray_handler.setFormatter(fmt)
            logger.addHandler(tray_handler)
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
                port = instance.get('port')
                pid_exists = psutil.pid_exists(pid) if pid else False
                is_avatar = self._is_intelavatar_process(pid) if pid_exists else False
                self.logger.debug(f'[SCAN] instance file found | PID={pid} port={port} pid_exists={pid_exists} is_avatar={is_avatar}')
                if pid and pid_exists and is_avatar:
                    live_instances.append(instance)
                else:
                    os.remove(self.instance_file)
                    self.logger.info(f'[SCAN] Removed stale instance file (PID={pid} dead or not avatar): {self.instance_file}')
            else:
                self.logger.debug(f'[SCAN] No instance file at {self.instance_file}')
        except Exception as error:
            self.logger.warning(f'[SCAN] Corrupt instance file {self.instance_file}: {error} — removing it.')
            try:
                os.remove(self.instance_file)
            except Exception:
                pass
        self.logger.debug(f'[SCAN] live_instances count={len(live_instances)}')
        return live_instances

    def _launch_instance(self):
        try:
            launch_flags = getattr(subprocess, 'CREATE_NEW_CONSOLE', 0)
            if getattr(sys, 'frozen', False):
                cmd = [sys.executable]
                self.logger.info(f'[LAUNCH] frozen mode — cmd={cmd}')
                subprocess.Popen(cmd, creationflags=launch_flags)
            else:
                app_path = os.path.join(self.base, 'app.py')
                cmd = [sys.executable, app_path]
                self.logger.info(f'[LAUNCH] dev mode — cmd={cmd} cwd={self.base}')
                subprocess.Popen(cmd, creationflags=launch_flags, cwd=self.base)
            self.logger.info('[LAUNCH] New instance launched successfully')
        except Exception as error:
            self.logger.error(f'[LAUNCH] Launch failed: {error}')

    # launch the driver download tool exe if it exists
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
        self.logger.info(f'[STOP] Stopping instance PID={pid} port={port}')

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
                        self.logger.info(f'[STOP] Skipping tray child process PID={child.pid}')
                        continue
                    self.logger.debug(f'[STOP] Terminating child PID={child.pid}')
                    child.terminate()
                except Exception as e:
                    self.logger.warning(f'[STOP] Error handling child process: {e}')
            self.logger.info(f'[STOP] Terminating main process PID={pid}')
            process.terminate()
        except (psutil.NoSuchProcess, TypeError):
            self.logger.info(f'[STOP] Process PID={pid} already gone')
        except Exception as error:
            self.logger.error(f'[STOP] Stop failed: {error}')

        try:
            if os.path.exists(self.instance_file):
                os.remove(self.instance_file)
        except Exception:
            pass

        self.instances = [item for item in self.instances if item.get('pid') != pid]
        self.logger.info(f'[STOP] Instance PID={pid} removed from list. Remaining: {len(self.instances)}')
        self._push_menu()

    def _quit(self):
        self.logger.info('[QUIT] Tray manager quit requested — stopping current instance and icon')
        self._stop_current_instance()
        if self.icon:
            self.icon.stop()
        self.logger.info('[QUIT] Tray icon stopped')

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
            self.logger.debug('[MENU] _push_menu called but icon is not yet initialised — skipping')
            return
        try:
            self.icon.menu = self._build_menu()
            self.icon.update_menu()
            self.logger.debug(f'[MENU] Menu updated | instances={[i.get("pid") for i in self.instances]}')
        except Exception as error:
            self.logger.error(f'[MENU] Menu update failed: {error}')

    def _monitor(self):
        self.logger.info('[MONITOR] Monitor thread started')
        while True:
            time.sleep(1)
            live_instances = self._scan_instances()
            old_pids = sorted(item.get('pid') for item in self.instances)
            new_pids = sorted(item.get('pid') for item in live_instances)
            if old_pids != new_pids:
                self.logger.info(f'[MONITOR] Instance list changed: {old_pids} → {new_pids}')
                self.instances = live_instances
                self._push_menu()

    def _pid_file(self) -> str:
        return os.path.join(get_user_data_dir(), 'tray_manager.pid')

    def _write_pid(self):
        pid_path = self._pid_file()
        try:
            with open(pid_path, 'w', encoding='utf-8') as f:
                f.write(str(os.getpid()))
            self.logger.info(f'[PID] PID file written: {pid_path} (PID={os.getpid()})')
        except Exception as e:
            self.logger.warning(f'[PID] Failed to write PID file {pid_path}: {e}')

    def _remove_pid(self):
        try:
            pid_path = self._pid_file()
            if os.path.exists(pid_path):
                os.remove(pid_path)
                self.logger.info(f'[PID] PID file removed: {pid_path}')
        except Exception as e:
            self.logger.warning(f'[PID] Failed to remove PID file: {e}')

    def run(self):
        self._write_pid()
        try:
            self.logger.info(f'[RUN] Tray starting | PID={os.getpid()} | base={self.base} | instance_file={self.instance_file}')
            self.instances = self._scan_instances()
            self.logger.info(f'[RUN] Initial instances: {[i.get("pid") for i in self.instances]}')

            monitor_thread = threading.Thread(target=self._monitor, daemon=True)
            monitor_thread.start()

            self.logger.info('[RUN] Creating pystray icon...')
            self.icon = pystray.Icon(
                'IntelAvatar',
                _icon_image(),
                'Intel Wireless CE Avatar',
                self._build_menu(),
            )
            self.logger.info('[RUN] pystray icon created — entering icon.run() (blocking)')
            self.icon.run()
            self.logger.info('[RUN] icon.run() returned')
        except Exception as error:
            self.logger.error(f'[RUN] Tray startup failed: {error}')
            self.logger.error(traceback.format_exc())
        finally:
            self._remove_pid()
            self.logger.info('[RUN] TrayManager.run() finished')


if __name__ == '__main__':
    TrayManager().run()