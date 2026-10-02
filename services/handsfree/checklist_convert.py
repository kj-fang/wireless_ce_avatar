"""
Converter: CE "Wi-Fi SW Issue Debugging Checklist" xlsx -> committed JSON.

Stdlib-only (zipfile + ElementTree) so neither the converter nor the app
needs openpyxl. Run whenever the team revs the checklist, commit the diff:

    python -m services.handsfree.checklist_convert "<path to checklist.xlsx>" \
        [-o services/handsfree/checklist_data/debug_checklist.json]

The xlsx itself is NOT committed; services/handsfree/checklist_data/debug_checklist.json
is the runtime source of truth (see services/handsfree/checklist.py).

Sheet structure (verified against Rev1_0):
  * Main tab: "Step1" block rows = general-info questions (col B, example in
    col D); "Step2" block rows = domain name (col B) + description (col C).
  * One tab per domain: sections delimited by col-A cells exactly equal to
    "Required Log" / "Required Info" / "Initial Triage"; items in col A with
    example hints in col C. "Check Items" header rows are skipped; preamble
    lines before the first section land in the domain's "notes".
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

_M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

_SECTION_NAMES = {"required log": "required_log",
                  "required info": "required_info",
                  "initial triage": "initial_triage"}
# Tabs that are not issue domains.
_NON_DOMAIN_SHEETS = {"revisions", "main", "sheet1"}


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _load_sheets(path: str) -> list[tuple[str, list[dict]]]:
    """[(sheet_name, rows)] where each row is {col_letter: text}."""
    z = zipfile.ZipFile(path)
    try:
        sst = ["".join(t.text or "" for t in si.iter(_M + "t"))
               for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(_M + "si")]
    except KeyError:
        sst = []
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    relmap = {r.get("Id"): r.get("Target") for r in rels}

    out = []
    for s in wb.find(_M + "sheets"):
        target = relmap[s.get(_R + "id")]
        ws = ET.fromstring(z.read("xl/" + target.lstrip("/")))
        rows = []
        for row in ws.iter(_M + "row"):
            cells: dict = {}
            for c in row.iter(_M + "c"):
                t = c.get("t")
                v = c.find(_M + "v")
                if t == "s" and v is not None:
                    val = sst[int(v.text)]
                elif t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter(_M + "t"))
                else:
                    val = v.text if v is not None else ""
                val = _norm_ws(val)
                if val:
                    cells[re.match(r"[A-Z]+", c.get("r")).group(0)] = val
            if cells:
                rows.append(cells)
        out.append((s.get("name"), rows))
    return out


def _parse_main(rows: list[dict]) -> tuple[list[dict], dict]:
    """-> (general_info items, {domain_name: description})."""
    general: list[dict] = []
    domains: dict = {}
    block = None
    for r in rows:
        a = r.get("A", "")
        if a.lower().startswith("step1"):
            block = "general"
            continue
        if a.lower().startswith("step2"):
            block = "domains"
            continue
        if a.lower().startswith("step3"):
            block = None
            continue
        b = r.get("B", "")
        if not b or b.lower() == "check items":
            continue
        if block == "general":
            general.append({"item": b, "example": r.get("D", "")})
        elif block == "domains":
            domains[b] = r.get("C", "")
    return general, domains


def _parse_domain(rows: list[dict]) -> dict:
    """Two section layouts exist:

    * FLAT (most tabs): items in col A, example hint in col C.
    * SUB-CATEGORY (Connectivity, P2P): header row 'Issue Type |
      Description | Check Items | ...'; a row with col A starts a
      sub-category (name=A, description=B, first item=C, example=E);
      rows with only col C continue the current sub-category's items.
    """
    d = {"required_log": [], "required_info": [], "initial_triage": [],
         "notes": [], "subcategories": {}}
    section = None
    layout = "flat"
    cur_subcat = ""
    for r in rows:
        a, b = r.get("A", ""), r.get("B", "")
        c, e = r.get("C", ""), r.get("E", "")
        key = _SECTION_NAMES.get(a.lower()) if a else None
        if key:
            section, layout, cur_subcat = key, "flat", ""
            continue
        if a.lower() == "check items":
            continue
        if a.lower() == "issue type":       # sub-category header row
            layout = "subcat"
            continue
        if section is None:
            if a:
                d["notes"].append(a)
            continue
        if layout == "subcat":
            if a:                            # new sub-category
                cur_subcat = a
                d["subcategories"].setdefault(a, b)
                if c:
                    d[section].append({"item": c, "example": e,
                                       "subcat": a})
            elif c:
                d[section].append({"item": c, "example": e,
                                   "subcat": cur_subcat})
        elif a:
            d[section].append({"item": a, "example": c})
    for k in ("notes", "subcategories"):
        if not d[k]:
            del d[k]
    return d


# CE-team decision (2026-09-30): Air sniffer log and OS log are NOT requested
# from customers — except for performance debugging, where they matter
# (P2P's Performance sub-category and the Performance domain tab keep them).
# Applied at convert time so re-running against a new checklist revision
# preserves the policy. Initial-triage guidance notes are never touched.
_DROP_ITEM_RE = re.compile(r"(?i)^(\d+\)\s*)?(air sniffer log|os log)\b")


def _apply_item_policy(data: dict) -> dict:
    for dom_name, dom in data["domains"].items():
        if dom_name == "Performance":
            continue
        for sec in ("required_log", "required_info"):
            dom[sec] = [e for e in dom.get(sec, [])
                        if e.get("subcat") == "Performance"
                        or not _DROP_ITEM_RE.match(e["item"])]
    return data


def convert(xlsx_path: str) -> dict:
    sheets = _load_sheets(xlsx_path)
    by_name = {name: rows for name, rows in sheets}
    if "Main" not in by_name:
        raise SystemExit("no 'Main' sheet — is this the debugging checklist?")
    general, domain_desc = _parse_main(by_name["Main"])

    revision = ""
    m = re.search(r"Rev(\d+[_\.]\d+)", Path(xlsx_path).name)
    if m:
        revision = "Rev" + m.group(1).replace(".", "_")

    domains: dict = {}
    for name, rows in sheets:
        if name.lower() in _NON_DOMAIN_SHEETS:
            continue
        parsed = _parse_domain(rows)
        if not any(parsed.get(k) for k in _SECTION_NAMES.values()):
            continue    # a stray empty tab
        # Main's Step-2 key may differ slightly from the tab name
        # (e.g. tab "WowLAN" vs Step-2 "Wowlan") — match case-insensitively.
        desc = next((v for k, v in domain_desc.items()
                     if k.lower() == name.lower()), "")
        parsed["description"] = desc
        domains[name] = parsed

    return _apply_item_policy({"revision": revision,
                               "generated_from": Path(xlsx_path).name,
                               "general_info": general,
                               "domains": domains})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("xlsx")
    ap.add_argument("-o", "--out",
                    default=str(Path(__file__).parent / "checklist_data" / "debug_checklist.json"))
    args = ap.parse_args(argv)
    data = convert(args.xlsx)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    print(f"wrote {out}  (domains: {len(data['domains'])}, "
          f"general_info: {len(data['general_info'])}, rev: {data['revision']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
