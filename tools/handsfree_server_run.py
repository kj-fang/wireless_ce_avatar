"""
One scheduled Handsfree round on the server (run it from Task Scheduler).

    python tools/handsfree_server_run.py --owners-file C:\\path\\owners.txt

1. --sso-check: does Salesforce + esft sign-in work unattended? (the saved
   --sso-login profile if there is one, else the Windows account)
2. If not, --sso-refresh once: a visible Chrome retries the saved profile
   with nobody typing. If that fails too, a person must fix the sign-in.
3. One scan of the owners' new cases / new customer comments (--once).

Each run writes <avatarfiles_dir>/logs/handsfree_server/<timestamp>.log and
overwrites last_status.txt there. Logs older than 30 days are deleted.
Exit code: 0 ok, 1 scan failed, 2 sign-in failed, 3 bad owners file.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOG_RETENTION_DAYS = 30


def _log_dir() -> Path:
    # Same Downloads lookup as utils.helpers.init_download_dir (avatarfiles_dir).
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as key:
        downloads = winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")[0]
    return Path(downloads) / "IntelAvatar_files" / "logs" / "handsfree_server"


def _prune(log_dir: Path) -> None:
    cutoff = time.time() - LOG_RETENTION_DAYS * 86400
    for old in log_dir.glob("*.log"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass


def _note(log, text: str) -> None:
    log.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} {text} ===\n".encode("utf-8"))
    log.flush()


def _cli(log, *args: str) -> int:
    _note(log, "cli " + " ".join(args))
    return subprocess.run([sys.executable, "-m", "services.handsfree.cli", *args],
                          cwd=REPO, stdout=log, stderr=subprocess.STDOUT).returncode


def _round(owners_file: Path, log) -> tuple[int, str]:
    if _cli(log, "--sso-check") != 0:
        if _cli(log, "--sso-refresh") != 0:
            return 2, ("SIGN-IN FAILED - Salesforce/esft sign-in did not complete; "
                       "see the log (a saved-profile machine needs --sso-login)")
    rc = _cli(log, "--owners-file", str(owners_file), "--once")
    return (0, "OK") if rc == 0 else (1, f"scan failed (cli exit {rc})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One scheduled Handsfree round on the server.")
    parser.add_argument("--owners-file", type=Path, required=True,
                        help="UTF-8 text file with one IPS Owner.Name per line.")
    args = parser.parse_args(argv)

    log_dir = _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    _prune(log_dir)
    started = datetime.now()
    log_path = log_dir / f"{started:%Y%m%d_%H%M%S}.log"
    owners_file = args.owners_file.resolve()

    with open(log_path, "ab") as log:
        if not owners_file.is_file():
            code, status = 3, f"owners file not found: {owners_file}"
        else:
            code, status = _round(owners_file, log)
        _note(log, f"finished: {status}")

    (log_dir / "last_status.txt").write_text(
        f"{started:%Y-%m-%d %H:%M} {status}\nlog: {log_path}\n", encoding="utf-8")
    print(f"{status} (log: {log_path})")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
