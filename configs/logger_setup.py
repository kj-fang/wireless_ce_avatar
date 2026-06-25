"""
logger_setup.py
---------------
Redirect sys.stdout through a tee-stream so every regular print() call is
*also* written to a rotating log file at:
    <Downloads>\\IntelAvatar_files\\logs\\avatar.log
    where <Downloads> is resolved from the Windows shell registry
    (CSIDL_DOWNLOADS), falling back to ~/Downloads on non-Windows.

Design:
- sys.stdout  → _TeeStream → terminal (unchanged) + log file
- sys.stderr  → NOT tee'd; tracebacks are captured via sys.excepthook instead
- werkzeug logger → file handler added directly (clean, single timestamp, no ANSI)

Progress-bar prints (those that contain \\r, e.g. bt_parser's overwrite lines)
are forwarded to the terminal as usual but skipped in the log file.
"""

import logging
import os
import re
import sys
import threading
import traceback as _tb_mod
from utils.port_utils import get_logs_dir
from logging.handlers import RotatingFileHandler

# Module-level logger — name "avatar" keeps it isolated from Flask/werkzeug.
_logger = logging.getLogger("avatar")

# Strip ANSI escape codes (colour / cursor sequences) before writing to log.
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[mGKHF]')

# Keyword-based level detection for print()s that lack emoji prefixes.
# Matched case-insensitively against the cleaned line text.
_ERROR_KW_RE = re.compile(
    r'\b(error|exception|traceback|failed|failure|crash)\b', re.IGNORECASE
)
_WARN_KW_RE = re.compile(
    r'\b(warning|warn)\b', re.IGNORECASE
)


class _AnsiStrippingFormatter(logging.Formatter):
    """Formatter that removes ANSI escape sequences from the final output."""
    def format(self, record):
        return _ANSI_RE.sub('', super().format(record))


class _TeeStream:
    """sys.stdout replacement that mirrors output to terminal + log file.

    Rules:
    - Everything is always forwarded to the original stream (terminal).
    - Complete lines (ending with \\n) that do NOT contain a bare \\r are
      also sent to the file logger so progress-bar overwrite sequences
      (\\r…) are excluded from the log.
    """

    def __init__(self, original_stream, file_logger):
        self._stream = original_stream
        self._logger = file_logger
        self._buf = ""
        self._lock = threading.Lock()

    def write(self, text):
        # Always forward to terminal unchanged.
        self._stream.write(text)

        with self._lock:
            self._buf += text

            # Guard against unbounded buffer growth from progress-bar writes
            # that never emit a newline (e.g. print(..., end='', flush=True)).
            # If the buffer is large and has no newline, discard it.
            if len(self._buf) > 4096 and "\n" not in self._buf:
                self._buf = ""
                return

            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                # Strip a trailing CR so CRLF line endings ("\r\n") are treated
                # as normal lines rather than being silently discarded.
                if line.endswith("\r"):
                    line = line[:-1]
                # Skip progress-bar lines: those that use a *leading* \r to
                # overwrite the current terminal line.
                if "\r" in line:
                    continue
                clean = _ANSI_RE.sub('', line.rstrip())
                if not clean:
                    continue
                if any(clean.startswith(p) for p in ("❌", "🚨", "[ERROR]")):
                    self._logger.error(clean)
                elif any(clean.startswith(p) for p in ("⚠️", "[WARNING]")):
                    self._logger.warning(clean)
                elif _ERROR_KW_RE.search(clean):
                    self._logger.error(clean)
                elif _WARN_KW_RE.search(clean):
                    self._logger.warning(clean)
                else:
                    self._logger.info(clean)

    def flush(self):
        self._stream.flush()

    def fileno(self):
        return self._stream.fileno()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def setup_file_logging() -> str:
    """Configure the rotating log file and install the stdout tee.

    Returns the path to the log file so callers can display it on startup.
    Call this once, as early as possible in app.py.
    """
    if isinstance(sys.stdout, _TeeStream):
        return ""

    log_path = os.path.join(get_logs_dir(), "avatar.log")

    _fmt = _AnsiStrippingFormatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Single shared handler — both loggers write through the same
    # RotatingFileHandler instance so there is only one file descriptor
    # and one rotation counter, avoiding rotation races on Windows.
    _file_handler = RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    _file_handler.setLevel(logging.DEBUG)
    _file_handler.setFormatter(_fmt)

    # ── avatar logger (captures print() via stdout tee) ──────────────────
    _logger.setLevel(logging.DEBUG)
    _logger.addHandler(_file_handler)
    _logger.propagate = False

    # ── stdout tee ───────────────────────────────────────────────────────
    sys.stdout = _TeeStream(sys.stdout, _logger)

    # ── sys.excepthook — captures unhandled exception tracebacks ─────────
    # We do NOT tee stderr because werkzeug's StreamHandler also writes to
    # sys.stderr (raw messages without a logging-format prefix), which would
    # cause every werkzeug line to appear twice in the log (once via the
    # werkzeug handler below, once via the tee).  excepthook is the clean
    # way to get tracebacks without touching stderr at all.
    _orig_excepthook = sys.excepthook

    def _excepthook(exc_type, exc_value, exc_tb):
        for chunk in _tb_mod.format_exception(exc_type, exc_value, exc_tb):
            for subline in chunk.splitlines():
                clean = _ANSI_RE.sub('', subline.rstrip())
                if clean:
                    _logger.error(clean)
        _orig_excepthook(exc_type, exc_value, exc_tb)

    sys.excepthook = _excepthook

    # ── threading.excepthook — captures unhandled exceptions in background threads
    # sys.excepthook only fires for the main thread; thread crashes are routed
    # through threading.excepthook which is separate.
    _orig_thread_excepthook = threading.excepthook

    def _thread_excepthook(args):
        _logger.error("Unhandled exception in thread: %s", args.thread.name if args.thread else "unknown")
        for chunk in _tb_mod.format_exception(args.exc_type, args.exc_value, args.exc_traceback):
            for subline in chunk.splitlines():
                clean = _ANSI_RE.sub('', subline.rstrip())
                if clean:
                    _logger.error(clean)
        _orig_thread_excepthook(args)

    threading.excepthook = _thread_excepthook

    # ── werkzeug logger (HTTP request logs + startup messages) ───────────
    # Attach our handler directly so werkzeug lines are written once, with a
    # clean single timestamp, and with ANSI codes stripped by the formatter.
    logging.getLogger("werkzeug").addHandler(_file_handler)

    _logger.info("=" * 60)
    _logger.info("IntelAvatar session started — log: %s", log_path)
    _logger.info("=" * 60)

    return log_path
