"""
utils/assert_code_utils.py
==========================
Parse Intel Wi-Fi firmware assert-code headers (assertLmac.h / assertUmac.h)
into a searchable lookup dict and expose a single public function for the LLM
tool: ``lookup_assert_code(code)``.

Key design decisions
--------------------
* Lazy init + JSON cache: the first call parses both headers (once per
  process) and writes a cache file next to the headers.  Subsequent calls
  in the same process return the in-memory dict immediately.  App restarts
  reload the fast JSON cache unless one of the .h files is newer than the
  cache (i.e. headers were updated).

* Strip SYSASSERT_CPU_UMAC (0x20000000): this CPU-context flag is injected
  by hardware at assert time but is NOT defined in these header files.  It
  is stripped before lookup automatically.

* All OR-composed values (e.g. ``0x505 | UMAC_ASSERT_START``) are evaluated
  at parse time so every dict key is the final resolved integer in hex form.

Dict entry structure (key = hex string, e.g. "0x100505")
---------------------------------------------------------
{
    "name":        "UMAC_SYSASSERT_505",
    "source":      "umac",          # "lmac" | "umac"
    "resolved":    "0x100505",
    "description": "CNVi HSIF fatal error (STEP)",
    "root_cause":  "",
    "data_fields": ["Data 1: PHY_INTERRUPT_STATUS", ...],
    "domain":      "INFRA"
}
"""

from __future__ import annotations

import json
import re
import sys
import threading
from pathlib import Path
from typing import Optional

# ── Paths ─────────────────────────────────────────────────────────────────────
def _resolve_paths() -> tuple:
    """
    Resolve .h header and cache paths for both dev and frozen (PyInstaller) modes.

    .h files (read-only bundled assets):
      - Dev:    utils/ (next to this file)
      - Frozen: sys._MEIPASS/utils/

    Cache file (writable):
      - Always: Downloads/IntelAvatar_files/assert_codes_cache.json
      This keeps a single consistent location regardless of how the app is run,
      avoids writing into the source tree in dev mode, and survives exe rebuilds
      without needing to re-parse the headers on every fresh install.
      Falls back to utils/ if the Downloads folder cannot be resolved.
    """
    frozen = getattr(sys, 'frozen', False)
    if frozen:
        # Frozen exe: .h files are not bundled (private); cache is bundled read-only.
        utils_dir = Path(sys._MEIPASS) / 'utils'
        cache     = utils_dir / "assert_codes_cache.json"
    else:
        utils_dir = Path(__file__).resolve().parent   # dev: utils/
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
            )
            downloads = winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")[0]
            winreg.CloseKey(key)
            cache_dir = Path(downloads) / "IntelAvatar_files"
            cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            cache_dir = utils_dir   # fallback
        cache = cache_dir / "assert_codes_cache.json"

    lmac = utils_dir / "assertLmac.h"
    umac = utils_dir / "assertUmac.h"
    print(f"[AssertCodes] frozen={frozen}, cache → {cache}")
    return lmac, umac, cache, frozen


_LMAC_H, _UMAC_H, _CACHE_JSON, _FROZEN = _resolve_paths()

# ── CPU-context flag injected at runtime (not defined in these .h files) ──────
# SYSASSERT_CPU_UMAC: prepended by hardware when a UMAC assert fires.
# Example: 0x20100505 = 0x20000000 (CPU flag) | 0x100000 (UMAC_ASSERT_START) | 0x505
_CPU_UMAC_FLAG = 0x20000000

# ── Namespace / flag constants found in the header files ──────────────────────
_KNOWN_CONSTS: dict[str, int] = {
    "RCM_ASSERT_START":     0x400000,   # LMAC sub-CPU: RCM
    "TCM_ASSERT_START":     0x500000,   # LMAC sub-CPU: TCM
    "UMAC_ASSERT_START":    0x100000,   # UMAC namespace marker
    "FSEQ_SYSASSERT_START": 0x200000,   # FSEQ namespace (defined but not used in entries)
    "IML_ASSERT_START":     0xF000,     # ROM / IML namespace marker
}

# ── Module-level in-process cache ─────────────────────────────────────────────
_ASSERT_CODES: Optional[dict] = None
_LOAD_LOCK = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _eval_expr(expr: str) -> Optional[int]:
    """
    Evaluate a C enum value expression such as ``0x505 | UMAC_ASSERT_START``.

    Supports: hex literals (0x / 0X), decimal literals, and known named
    constants from ``_KNOWN_CONSTS``.  Returns None for any expression
    containing an unrecognised symbol (signals caller to skip the entry).
    """
    result = 0
    for part in expr.split("|"):
        part = part.strip()
        if re.match(r'^0[xX][0-9A-Fa-f]+$', part):
            result |= int(part, 16)
        elif re.match(r'^\d+$', part):
            result |= int(part)
        elif part in _KNOWN_CONSTS:
            result |= _KNOWN_CONSTS[part]
        else:
            return None  # unknown symbol → skip this entry
    return result


def _extract_doc_comment(text: str, entry_start: int) -> dict:
    """
    Walk backwards from ``entry_start`` to find the nearest /** ... */ block
    and extract Description, Root cause, Data N, and Domain fields.

    Returns an empty dict when no valid doc comment is found.
    """
    comment_close = text.rfind("*/", 0, entry_start)
    if comment_close == -1:
        return {}
    comment_open = text.rfind("/*", 0, comment_close)
    if comment_open == -1:
        return {}

    # Reject if another enum entry lies between this comment and our entry
    between = text[comment_close + 2 : entry_start]
    if re.search(r'\w+\s*=\s*0[xX0-9]', between):
        return {}

    block = text[comment_open : comment_close + 2]

    # Strip C comment decoration from each line; drop pure comment-marker remnants
    lines: list[str] = []
    for line in block.splitlines():
        stripped = re.sub(r'^\s*/?[\*!]+\s?', '', line).rstrip()
        # Skip lines that are nothing but closing comment markers (e.g. "*/", "/")
        if stripped and not re.fullmatch(r'[*/\s]+', stripped):
            lines.append(stripped)

    result: dict = {
        "description": "",
        "root_cause":  "",
        "data_fields": [],
        "domain":      "",
    }
    current_field: Optional[str] = None
    current_lines: list[str]     = []

    def _flush() -> None:
        if not current_field:
            return
        val = " ".join(current_lines).strip()
        if current_field == "description":
            result["description"] = val
        elif current_field == "root_cause":
            result["root_cause"] = val
        elif current_field == "data":
            if val:
                result["data_fields"].append(val)
        elif current_field == "domain":
            result["domain"] = val

    for line in lines:
        lo = line.lower()
        colon_idx = line.find(":")

        if lo.startswith("description"):
            _flush(); current_field = "description"
            current_lines = [line[colon_idx + 1:].strip()] if colon_idx != -1 else [""]
        elif lo.startswith("root cause"):
            _flush(); current_field = "root_cause"
            current_lines = [line[colon_idx + 1:].strip()] if colon_idx != -1 else [""]
        elif re.match(r'data\s*\d+\s*:', lo):
            _flush(); current_field = "data"
            # Keep the full line (including "Data N:" label) for clarity
            current_lines = [line.strip()]
        elif lo.startswith("domain"):
            _flush(); current_field = "domain"
            current_lines = [line[colon_idx + 1:].strip()] if colon_idx != -1 else [""]
        elif current_field:
            current_lines.append(line)

    _flush()
    return result


def _is_inside_block_comment(text: str, pos: int) -> bool:
    """Return True if ``pos`` falls inside a /* ... */ block comment."""
    last_open  = text.rfind("/*", 0, pos)
    last_close = text.rfind("*/", 0, pos)
    return last_open != -1 and (last_close == -1 or last_open > last_close)


def _parse_one_file(path: Path, source: str) -> dict:
    """Parse a single .h file and return a partial lookup dict."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lookup: dict = {}

    # Match active enum entries: IDENTIFIER = EXPR,
    # The regex intentionally does NOT anchor to ^/$ so it captures mid-line
    # expressions, but we post-filter commented-out lines below.
    pattern = re.compile(
        r'[ \t]*([A-Z_][A-Z0-9_]*)[ \t]*=[ \t]*([^,\n]+?),[ \t]*(?://[^\n]*)?(?=\n|$)',
        re.IGNORECASE,
    )

    for m in pattern.finditer(text):
        entry_start = m.start()

        # Skip entries inside block comments
        if _is_inside_block_comment(text, entry_start):
            continue

        # Skip entries on lines that begin with // (line comment)
        line_start = text.rfind("\n", 0, entry_start) + 1
        if text[line_start:entry_start].lstrip().startswith("//"):
            continue

        name = m.group(1)
        expr = m.group(2).strip()

        # Skip flag constants themselves (they are in _KNOWN_CONSTS)
        if name in _KNOWN_CONSTS:
            continue

        resolved = _eval_expr(expr)
        if resolved is None:
            continue

        key = hex(resolved)
        doc = _extract_doc_comment(text, entry_start)
        lookup[key] = {
            "name":        name,
            "source":      source,
            "resolved":    key,
            "description": doc.get("description", ""),
            "root_cause":  doc.get("root_cause",  ""),
            "data_fields": doc.get("data_fields", []),
            "domain":      doc.get("domain",      ""),
        }

    return lookup


def _parse_headers() -> dict:
    """Parse both header files and merge into one lookup dict."""
    lookup: dict = {}
    if _LMAC_H.exists():
        lmac = _parse_one_file(_LMAC_H, "lmac")
        lookup.update(lmac)
        print(f"[AssertCodes] Parsed {_LMAC_H.name}: {len(lmac)} entries.")
    else:
        print(f"[AssertCodes] WARNING: {_LMAC_H} not found — LMAC codes unavailable.")

    if _UMAC_H.exists():
        umac = _parse_one_file(_UMAC_H, "umac")
        lookup.update(umac)
        print(f"[AssertCodes] Parsed {_UMAC_H.name}: {len(umac)} entries. "
              f"Total: {len(lookup)}.")
    else:
        print(f"[AssertCodes] WARNING: {_UMAC_H} not found — UMAC codes unavailable.")

    return lookup


# ── Cache management ──────────────────────────────────────────────────────────

def _load_or_parse() -> dict:
    """
    Return the lookup dict from the JSON cache when it is up-to-date,
    otherwise re-parse the headers and refresh the cache.

    Frozen mode: cache is pre-built and bundled read-only in the exe.
    Load it directly — no mtime check, no writing.
    Dev mode: parse .h files if cache is missing or stale, then write.
    """
    if _FROZEN:
        # Bundled cache is always authoritative for this exe version.
        try:
            data = json.loads(_CACHE_JSON.read_text(encoding="utf-8"))
            print(f"[AssertCodes] Loaded {len(data)} entries from bundled cache.")
            return data
        except Exception as e:
            print(f"[AssertCodes] Bundled cache read failed ({e}) — lookup unavailable.")
            return {}

    # Dev mode: check mtime and re-parse if stale.
    if _CACHE_JSON.exists():
        h_mtimes = [p.stat().st_mtime for p in (_LMAC_H, _UMAC_H) if p.exists()]
        # Some development/test distributions intentionally ship only the
        # generated cache, not the private headers.  In that layout the cache
        # is authoritative; attempting a re-parse would produce {} and erase
        # the usable lookup table.
        if not h_mtimes or _CACHE_JSON.stat().st_mtime >= max(h_mtimes):
            try:
                data = json.loads(_CACHE_JSON.read_text(encoding="utf-8"))
                if data:
                    print(f"[AssertCodes] Loaded {len(data)} entries from cache "
                          f"({_CACHE_JSON.name}).")
                    return data
                print(f"[AssertCodes] Cache is empty, re-parsing headers.")
            except Exception as e:
                print(f"[AssertCodes] Cache read failed ({e}), re-parsing headers.")

    data = _parse_headers()
    try:
        tmp = _CACHE_JSON.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=None),
            encoding="utf-8",
        )
        tmp.replace(_CACHE_JSON)
        print(f"[AssertCodes] Cache written: {_CACHE_JSON.name} "
              f"({len(data)} entries).")
    except Exception as e:
        print(f"[AssertCodes] Cache write failed (non-fatal): {e}")
    return data


def _ensure_loaded() -> dict:
    """Lazy init: parse / load once per process, then return in-memory dict."""
    global _ASSERT_CODES
    if _ASSERT_CODES is None:
        with _LOAD_LOCK:
            if _ASSERT_CODES is None:  # double-checked locking
                _ASSERT_CODES = _load_or_parse()
    return _ASSERT_CODES


# ── Public API ────────────────────────────────────────────────────────────────

def lookup_assert_code(code: str) -> str:
    """
    Look up a firmware assert code and return a human-readable string for the
    LLM tool result.

    Accepts the raw value exactly as it appears in the log:
      - hex string:  ``"0x20100505"``  or  ``"0x34"``
      - decimal str: ``"52"``

    SYSASSERT_CPU_UMAC (0x20000000) is stripped automatically before lookup.
    """
    db = _ensure_loaded()

    # ── Parse input ──────────────────────────────────────────────────────────
    code = code.strip()
    try:
        raw_val = int(code, 16) if code.lower().startswith("0x") else int(code, 0)
    except ValueError:
        return (f"Invalid assert code format: '{code}' — "
                "expected hex (e.g. '0x20100505') or decimal.")

    # ── Strip CPU context flag ───────────────────────────────────────────────
    cpu_flag_present = bool(raw_val & _CPU_UMAC_FLAG)
    resolved_val     = raw_val & ~_CPU_UMAC_FLAG
    resolved_hex     = hex(resolved_val)

    entry = db.get(resolved_hex)

    # ── Build output ─────────────────────────────────────────────────────────
    lines: list[str] = [f"=== Assert Code Lookup: {code} ==="]

    if cpu_flag_present:
        lines.append("CPU flag   : SYSASSERT_CPU_UMAC (0x20000000) stripped "
                     "(indicates UMAC CPU context)")

    lines.append(f"Resolved   : {resolved_hex}")

    if not entry:
        lines += [
            "Result     : NOT FOUND in assertLmac.h / assertUmac.h",
            "",
            "This may be an undocumented or reserved code, or from a newer",
            "firmware version not covered by the current header files.",
        ]
        return "\n".join(lines)

    _SOURCE_LABELS = {
        "lmac": "LMAC (Lower MAC firmware)",
        "umac": "UMAC (Upper MAC firmware)",
    }
    lines.append(f"Layer      : {_SOURCE_LABELS.get(entry['source'], entry['source'])}")
    lines.append(f"Name       : {entry['name']}")
    lines.append("")

    desc = entry.get("description", "").strip()
    lines.append(f"Description: {desc if desc else '(not documented)'}")

    rc = entry.get("root_cause", "").strip()
    if rc:
        lines.append(f"Root cause : {rc}")

    domain = entry.get("domain", "").strip()
    if domain:
        lines.append(f"Domain     : {domain}")

    data_fields = [f for f in entry.get("data_fields", []) if f.strip()]
    if data_fields:
        lines.append("Data fields:")
        for df in data_fields:
            lines.append(f"  {df}")
        lines.append("")
        lines.append("Note: Data 1/2/3 values are reported alongside the assert code in")
        lines.append("      the log. Cross-reference the values above for failure details.")

    return "\n".join(lines)


# ── Eager initialization: build / load cache at import time ──────────────────
_ensure_loaded()
