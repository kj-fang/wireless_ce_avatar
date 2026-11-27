"""
What a caller hands in as "the Sleep Study report", resolved to the report
file itself and the folder its driver logs are looked for in. One resolver
for the NW page, its API (which other tools, e.g. an MCP server, call) and
the command line, so all three accept and refuse the same inputs:

  - a Sleep Study report, .html (powercfg /sleepstudy) or .xml (/xml, what
    MCS Analyzer generates): driver logs are looked for in its folder;
  - a .zip: extracted beside it into <name>_extracted, and looked into;
  - a folder: looked into.

A file is a Sleep Study report by its content, never its extension: the HTML
embeds LocalSprData, the XML's root is <SleepStudy>. No report found is an
error (E_NO_SLEEPSTUDY_REPORT), and so is more than one (
E_MULTIPLE_SLEEPSTUDY_REPORTS, with the candidates, for the caller to name
one). No driver log beside the report is not an error: the report can be
analysed alone, so it is only flagged (missing_driver_log).

Standard library only.
"""
from __future__ import annotations

import fnmatch
import os
import zipfile

# Driver logs the analysis reads, by file name (lower case).
DRIVER_LOG_PATTERNS = ("wifilog*.log", "wifidriver*.etl", "wifidriver*.etl.*", "wifidriver*.log",
                       "*.hci.txt")
# A zip larger than this, extracted, is refused (a capture runs to a few GB).
MAX_EXTRACT_BYTES = 8 * 1024 ** 3
MAX_EXTRACT_FILES = 50_000
EXTRACT_MARK = ".sleepstudy_extracted"
_REPORT_SUFFIXES = (".html", ".htm", ".xml")
_HTML_MARK = b"LocalSprData"


class InputError(Exception):
    """A report input the analysis cannot use; `code` for programs."""

    def __init__(self, code: str, message: str, candidates: list | None = None):
        super().__init__(message)
        self.code, self.message, self.candidates = code, message, candidates or []

    def to_dict(self) -> dict:
        return {"success": False, "code": self.code, "error": self.message,
                "candidates": self.candidates}


def report_kind(path: str) -> str:
    """"html" or "xml" when `path` is a Sleep Study report, else ""."""
    if not path.lower().endswith(_REPORT_SUFFIXES) or not os.path.isfile(path):
        return ""
    from services.sleepstudy_xml import is_sleepstudy_xml
    if is_sleepstudy_xml(path):
        return "xml"
    # The HTML report is one large page whose data sits in a script; the
    # marker can be anywhere in it, so it is read in overlapping chunks.
    try:
        with open(path, "rb") as fh:
            tail = b""
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    return ""
                if _HTML_MARK in tail + chunk:
                    return "html"
                tail = chunk[-len(_HTML_MARK):]
    except OSError:
        return ""


def extract_zip(path: str) -> str:
    """Extract `path` beside it into <name>_extracted and return that folder.
    An extraction of the same zip (same size and time) is reused. Entries
    that would land outside the folder are refused, as is a zip that
    extracts to more than MAX_EXTRACT_BYTES."""
    dest = os.path.join(os.path.dirname(os.path.abspath(path)),
                        os.path.splitext(os.path.basename(path))[0] + "_extracted")
    stat = os.stat(path)
    stamp = f"{stat.st_size}:{int(stat.st_mtime)}"
    mark = os.path.join(dest, EXTRACT_MARK)
    try:
        with open(mark, encoding="utf-8") as fh:
            if fh.read().strip() == stamp:
                return dest
    except OSError:
        pass
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_EXTRACT_FILES:
                raise InputError("E_ZIP_TOO_LARGE", f"{os.path.basename(path)} holds {len(infos)} files "
                                 f"(at most {MAX_EXTRACT_FILES} are extracted)")
            total = sum(i.file_size for i in infos)
            if total > MAX_EXTRACT_BYTES:
                raise InputError("E_ZIP_TOO_LARGE", f"{os.path.basename(path)} extracts to "
                                 f"{total / 1024 ** 3:.1f} GB (at most {MAX_EXTRACT_BYTES / 1024 ** 3:.0f} GB)")
            root = os.path.realpath(dest)
            for info in infos:
                target = os.path.realpath(os.path.join(dest, info.filename))
                if os.path.commonpath([root, target]) != root:
                    raise InputError("E_ZIP_UNSAFE",
                                     f"{os.path.basename(path)}: entry {info.filename!r} points outside "
                                     "the extraction folder; not extracted")
            os.makedirs(dest, exist_ok=True)
            zf.extractall(dest)
    except zipfile.BadZipFile as exc:
        raise InputError("E_ZIP_UNREADABLE", f"{os.path.basename(path)} is not a readable zip: {exc}")
    with open(mark, "w", encoding="utf-8") as fh:
        fh.write(stamp)
    return dest


def find_reports(folder: str) -> list[str]:
    """Every Sleep Study report under `folder`, by content."""
    found = []
    for root, _dirs, files in os.walk(folder):
        for name in sorted(files):
            path = os.path.join(root, name)
            if report_kind(path):
                found.append(path)
    return found


def find_driver_logs(folder: str, limit: int = 20) -> list[str]:
    """Up to `limit` driver logs under `folder`, by file name."""
    found = []
    for root, _dirs, files in os.walk(folder):
        for name in sorted(files):
            low = name.lower()
            if any(fnmatch.fnmatch(low, pattern) for pattern in DRIVER_LOG_PATTERNS):
                found.append(os.path.join(root, name))
                if len(found) >= limit:
                    return found
    return found


def resolve(path: str) -> dict:
    """
    The report and the driver-log folder for what a caller gave:
      report        the Sleep Study report file
      kind          "html" or "xml"
      search_root   where its driver logs are looked for (the report's folder,
                    or the zip's / folder's root)
      extracted_to  the folder a zip was extracted into ("" otherwise)
      driver_logs   driver logs found under search_root (at most 20)
      missing_driver_log  True when there is none
    Raises InputError.
    """
    path = (path or "").strip().strip('"')
    if not path:
        raise InputError("E_NO_SLEEPSTUDY_REPORT", "no Sleep Study report given (.html, .xml, .zip or a folder)")
    if not os.path.exists(path):
        raise InputError("E_NO_SLEEPSTUDY_REPORT", f"not found: {path}")
    path = os.path.abspath(path)
    extracted = ""
    if os.path.isfile(path) and path.lower().endswith(".zip"):
        extracted = folder = extract_zip(path)
    elif os.path.isdir(path):
        folder = path
    else:
        kind = report_kind(path)
        if not kind:
            raise InputError("E_NO_SLEEPSTUDY_REPORT",
                             f"{os.path.basename(path)} is not a Sleep Study report: give the HTML "
                             "(powercfg /sleepstudy) or XML (/xml) report, or a zip or folder holding one")
        root = os.path.dirname(path)
        logs = find_driver_logs(root)
        return {"report": path, "kind": kind, "search_root": root, "extracted_to": "",
                "driver_logs": logs, "missing_driver_log": not logs}
    reports = find_reports(folder)
    where = os.path.basename(path)
    if not reports:
        raise InputError("E_NO_SLEEPSTUDY_REPORT",
                         f"{where} holds no Sleep Study report (no HTML with LocalSprData, no XML "
                         "<SleepStudy>)")
    if len(reports) > 1:
        raise InputError("E_MULTIPLE_SLEEPSTUDY_REPORTS",
                         f"{where} holds {len(reports)} Sleep Study reports; name the one to analyse",
                         candidates=reports)
    logs = find_driver_logs(folder)
    return {"report": reports[0], "kind": report_kind(reports[0]), "search_root": folder,
            "extracted_to": extracted, "driver_logs": logs, "missing_driver_log": not logs}
