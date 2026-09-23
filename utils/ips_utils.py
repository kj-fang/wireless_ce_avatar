"""
One definition of what an IPS case number is.

Before this module the format was spelled three different ways — ``^\\d{8}$`` for
the clipboard pickup, a per-segment ``\\d{8}`` for path scraping, and
``0[01]\\d{6}`` in the telemetry collector — and none of the three accepted the
seven-digit form a human actually types.

Every case folder on disk is eight digits, zero-padded, beginning ``00`` or
``01`` (sampled: 00109610, 00981604, 00985610, 00989702, 00990072, 00997200,
00997722, 01004103, 01008992, 01009610, 01009634, 01010628). People drop the
leading zero and say "1010628", so typed input is padded before it is matched.

Typing and guessing are held to different standards on purpose, which is the
same distinction ``case_ref_source`` already draws downstream:

* A number the user typed only has to be eight digits starting with a zero.
  Rejecting a real case mid-analysis is the expensive failure, and the 00/01
  prefix of today's sample is just where the sequence happens to have reached
  — ``services/handsfree/smoke.py`` already uses 07654321 as a case number.
* A number scraped from a path must match the narrower 00/01 form the
  collector uses, because a guess should not be the thing that invents a case.
  It also keeps an 8-digit date such as 05122024 out.
"""

import os
import re
from typing import List

IPS_LENGTH = 8

# What a person may assert. Leading zero only, which is what separates a case
# number from a date such as 20260923.
_TYPED_RE = re.compile(r"^0\d{7}$")

# What may be inferred without being told. Matches the collector's own regex.
_DERIVABLE_RE = re.compile(r"^0[01]\d{6}$")

# Six significant digits at minimum: 997200 is the shortest real case seen,
# and without a floor a stray "0" pads into a perfectly valid-looking 00000000.
_DIGITS_RE = re.compile(r"^\d{6,8}$")
_SEGMENT_RE = re.compile(r"^\d{8}$")
_SEPARATORS_RE = re.compile(r"[\\/]+")

# Punctuation that collects on the ends of a number as it is typed or pasted.
# A trailing full stop is how case 997200. reached the warehouse as a case of
# its own, four seconds apart from 00997200 and with the same subject.
# Only the ends are stripped, and only punctuation: letters are left in place
# so that pasting a sentence which happens to contain digits still fails.
_EDGE_PUNCT_RE = re.compile(r"^[.,;:'\"\-_#()\[\]]+|[.,;:'\"\-_#()\[\]]+$")


def normalise_ips(raw) -> str:
    """Canonical eight-digit form of a typed case number, or "" if it is not one."""
    text = re.sub(r"\s+", "", str(raw or ""))
    text = _EDGE_PUNCT_RE.sub("", text)
    if not _DIGITS_RE.match(text):
        return ""
    padded = text.zfill(IPS_LENGTH)
    return padded if _TYPED_RE.match(padded) else ""


def is_valid_ips(raw) -> bool:
    return bool(normalise_ips(raw))


def derive_ips_candidates(path) -> List[str]:
    """
    Case numbers appearing as whole segments of ``path``, nearest the file first.

    Whole segments only: a folder named ``20260923_01010628_retry`` is a name
    that contains digits, not a case folder, and treating it as one would
    attribute the analysis to a case nobody chose.
    """
    text = str(path or "").strip()
    if not text:
        return []
    segments = [s for s in _SEPARATORS_RE.split(text) if s]
    seen = set()
    out = []
    for segment in reversed(segments):
        if _SEGMENT_RE.match(segment) and _DERIVABLE_RE.match(segment) and segment not in seen:
            seen.add(segment)
            out.append(segment)
    return out


def is_case_folder_segment(segment) -> bool:
    """Whether a single path segment may be read as a case folder unprompted."""
    text = str(segment or "").strip()
    return bool(_SEGMENT_RE.match(text) and _DERIVABLE_RE.match(text))


def derive_ips_from_path(path) -> str:
    """The case number a path sits under, or "" when the path reveals none."""
    candidates = derive_ips_candidates(path)
    return candidates[0] if candidates else ""


def is_synthetic_case_nbr(case_nbr) -> bool:
    """Whether this is a placeholder minted for a case-less local upload."""
    text = str(case_nbr or "").strip().lower()
    return text.startswith("local_upload") or text.startswith("local_bsod")


def resolve_ips_dir(root: str, case_nbr: str) -> str:
    """Path of a case's download folder, using the canonical spelling."""
    canonical = normalise_ips(case_nbr)
    return os.path.join(root, canonical) if canonical else ""
