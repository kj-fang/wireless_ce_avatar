"""Log scoping and assembled-evidence behavior for chatbot agents."""

from __future__ import annotations

import hashlib
import re
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

from utils import helpers
from utils.log_parser_preprocess import (
    extract_enabled_keywords_from_filter_file,
    filter_log_by_keywords,
    group_similar_logs,
    preprocess_log_for_llm,
)


class LogScopeMixin:
    """LogScope behavior for the composed agent."""

    def _effective_issue_window(self) -> int:
        """Half-width in minutes for the Segment2 issue-time slice.

        Wi-Fi and BT expose a sidebar slider that writes
        issue_time_window_minutes per turn. NW has no issue-time UI, so its
        policy default is the whole story -- the flag guards a value that
        never arrives today, and would keep guarding it if someone later
        wired NW's /chat to accept the field.

        0 is a legitimate answer (capture only the exact issue instant); a
        None or negative value falls back to the policy default. Both
        Segment2 branches below read this rather than repeating the rule.
        """
        default = self.capabilities.issue_window_minutes_default
        raw = (
            self.issue_time_window_minutes
            if self.capabilities.configurable_issue_window else default
        )
        return raw if isinstance(raw, int) and raw >= 0 else default

    def _ensure_raw_log_cache(self) -> Optional[str]:
        """Load raw log once per current log path and keep it in memory."""
        if not self.current_log_path:
            return "Error: No log file has been set. Please set a log file path first."

        if self._raw_log_cache_path == self.current_log_path and self._raw_log_cache:
            return None

        try:
            self._raw_log_cache = helpers.read_log_file(self.current_log_path)
            self._raw_log_cache_path = self.current_log_path
            # A different file means all derived caches are stale.
            self._filter_cache_by_skill = {}
            self._detail_cache = {}
            self._detail_query_seen = set()
            self._chat_rules_injected_skills = set()
            self._assembled_entries_by_key = {}
            self._assembled_entries_no_ts = {}
            self._assembled_log_text = ""
            self._filter_export_counter = 0
            self._driver_init_count = 0
            self._driver_init_lines = []
            self._issue_time_window_lines = []
            self._scoped_log_lines = []
            # Date-format detection is per-file — invalidate explicitly so a
            # subsequent _log_has_date() call re-runs on the freshly loaded
            # content. (_log_has_date already keys its own cache on the raw
            # cache path, but clearing here makes the invariant impossible
            # to miss when the user loads a WiFi log after a DDD log and
            # expects the sidebar's date-required mode to come back.)
            self._log_has_date_cache = None
            self._log_has_date_cache_path = None
            return None
        except Exception as e:
            return f"Error reading log file: {e}"

    def _log_has_date(self) -> bool:
        """Whether the loaded log's timestamps carry a DATE
        (MM/DD/YYYY-HH:MM:SS.mmm) or are time-only (e.g. DDD / tracefmt logs:
        "HH:MM:SS.fffffff ..." with no date). Cached per log path.

        This splits the pre-scan into two paths: dated logs keep the original
        datetime windowing; time-only logs match by time-of-day instead, so a
        DDD log (no date) doesn't leave the agent with nothing to analyse.

        Cheap by design: it only needs to inspect the first ~2000 lines, so it
        does NOT pull the whole file into memory. When the full cache already
        happens to be loaded it reuses it; otherwise it streams just the head
        of the file. This keeps set_log / page-load fast even for a
        multi-hundred-MB BT .hci.txt (the full read is deferred to the first
        actual analysis).
        """
        if not self.current_log_path:
            return True  # no log → assume dated (original path)

        if getattr(self, "_log_has_date_cache_path", None) == self.current_log_path \
                and getattr(self, "_log_has_date_cache", None) is not None:
            return self._log_has_date_cache

        full_re = re.compile(r'\d{2}/\d{2}/\d{4}-\d{2}:\d{2}:\d{2}\.\d{3}')

        # Prefer the in-memory cache if it's already for this path; else peek
        # the file head via a streaming read (no full load).
        if self._raw_log_cache_path == self.current_log_path and self._raw_log_cache:
            sample = self._raw_log_cache[:2000]
        else:
            sample = []
            try:
                with open(self.current_log_path, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f):
                        if i >= 2000:
                            break
                        sample.append(line)
            except Exception:
                return True  # read error → assume dated (keep original path)

        has_date = any(full_re.search(line) for line in sample)
        self._log_has_date_cache = has_date
        self._log_has_date_cache_path = self.current_log_path
        return has_date

    def _merge_wrapped_lines_if_needed(self) -> None:
        """Some time-only logs (e.g. DDD) wrap a single entry across extra
        physical lines that carry no leading timestamp. Join each such
        continuation line back onto the preceding entry so every logical entry
        is one line — otherwise the orphan fragments break time-windowing and
        skill filtering.

        Surgical + idempotent: only runs for no-date logs (dated logs are
        untouched, including most BT HCI ``.hci.txt`` dumps which carry
        full ``MM/DD/YYYY-HH:MM:SS.mmm`` timestamps), and only rewrites the
        cache when a wrap is actually found.

        An entry line is recognised when it starts with — in any combination
        — an optional sequence number, optional angle-bracket prefix, then a
        ``HH:MM:SS`` time-of-day. Examples that match:
          * ``12:34:56 ...``         (plain DDD)
          * ``0004 12:34:56 ...``    (seq# + space + time)
          * ``<12:34:56> ...``       (BT-style angle-bracket form)
          * ``<12:34:56.789> ...``   (BT-style with ms)
        Anything else is treated as a continuation. False positives are
        cheaper than false negatives here — a misclassified entry-start
        just doesn't get merged, but a misclassified continuation collapses
        distinct entries together.
        """
        if self._log_has_date():
            return
        lines = self._raw_log_cache or []
        # ``<?`` makes the angle-bracket prefix optional, covering both the
        # legacy DDD form (no brackets) and the BT HCI no-date form which
        # wraps the time-of-day in ``<...>``.
        start_re = re.compile(r'^\s*<?(?:\d+\s+)?\d{1,2}:\d{2}:\d{2}')
        merged: List[str] = []
        wrapped = 0
        for line in lines:
            if not line.strip() or not merged or start_re.match(line):
                # Blank lines and entry-start lines pass through unchanged;
                # blanks are never treated as continuations.
                merged.append(line)
            else:
                merged[-1] = merged[-1].rstrip("\r\n") + " " + line.strip()
                wrapped += 1
        if wrapped:
            self._raw_log_cache = merged
            print(f"[PreScan] 🔗 Merged {wrapped} wrapped continuation line(s) "
                  f"in time-only log (now {len(merged)} entries).")

    def get_log_span_minutes(self) -> int:
        """
        Whole-minute span of the log (first → last parseable timestamp). Used
        to bound the sidebar issue-time capture window so the user can't
        request a window wider than the log itself. Returns 0 when the log
        can't be read or has no parseable (dated) timestamps.

        Cheap by design: uses a seek-based first/last read (head + tail only),
        so a multi-hundred-MB BT .hci.txt is NOT pulled into memory just to
        size the sidebar slider. The full read is deferred to first analysis.
        """
        if not self.current_log_path:
            return 0
        try:
            from utils.issue_time_utils import read_log_time_range
            import math as _math
            first_ts, last_ts = read_log_time_range(self.current_log_path)
            if not first_ts or not last_ts:
                return 0
            span_min = (last_ts - first_ts).total_seconds() / 60.0
            return max(0, int(_math.ceil(span_min)))
        except Exception:
            return 0

    def _preprocess_raw_log_context(self) -> None:
        """
        Pre-analysis scan executed on self._raw_log_cache BEFORE any skill
        filtering runs.  Produces two segments that are merged into
        self._scoped_log_lines — ALL subsequent skill filtering operates
        ONLY on these scoped lines, not the full raw cache.

        Segment 1 – Driver init settings
          First "OS issued Driver Device Add"
          → first "Got Command (M1 Message) TASK_DOT11_RESET" AFTER it (inclusive).

        Segment 2 – Event investigation window (one of):
          A) issue_time ±5 min  (if issue_time is available)
          B) last TASK_DOT11_RESET (after first Driver Add) → EOF  (fallback)
        """
        # Markers come from class attributes so subclasses can override
        # (e.g. BtLogAgentSystem uses ibtpci-specific markers). Each may be a
        # single string or a list of candidates — normalise to a list and
        # match ANY candidate, so the scan logic stays generic.
        driver_add_markers = self._normalize_markers(self.DRIVER_ADD_MARKER)
        reset_markers      = self._normalize_markers(self.RESET_MARKER)
        TS_RE = re.compile(r'(\d{2}/\d{2}/\d{4}-\d{2}:\d{2}:\d{2}\.\d{3})')

        # Time-only logs (e.g. DDD) may wrap one entry across extra lines —
        # merge those back first so segments/scoping see whole entries. No-op
        # for dated logs and for logs that don't wrap.
        if self.capabilities.merge_wrapped_time_only_logs:
            self._merge_wrapped_lines_if_needed()

        total_lines = len(self._raw_log_cache)

        # ------------------------------------------------------------------
        # Pass 1: single scan — find first ADD, count ADDs, collect all RESETs
        # ------------------------------------------------------------------
        driver_add_indices = []
        reset_indices = []
        for i, line in enumerate(self._raw_log_cache):
            line_lower = line.lower()
            if self._line_matches_any(line_lower, driver_add_markers):
                driver_add_indices.append(i)
                print(f"[PreScan] 🚩 driver-add marker found at line {i+1}")
            if self._line_matches_any(line_lower, reset_markers):
                reset_indices.append(i)

        self._driver_init_count = len(driver_add_indices)

        def _line_ts(idx: int) -> Optional[datetime]:
            m = TS_RE.search(self._raw_log_cache[idx])
            if not m:
                return None
            try:
                return datetime.strptime(m.group(1), "%m/%d/%Y-%H:%M:%S.%f")
            except ValueError:
                return None

        # ------------------------------------------------------------------
        # Segment 1: choose ONE block nearest to issue_time
        # (fallback: first valid block when issue_time is unavailable)
        # ------------------------------------------------------------------
        first_add_idx = None
        seg1_end = None
        self._driver_init_lines = []

        seg1_candidates = []
        for idx, add_idx in enumerate(driver_add_indices):
            next_add_idx = driver_add_indices[idx + 1] if idx + 1 < len(driver_add_indices) else total_lines
            first_reset_idx = next((r for r in reset_indices if add_idx < r < next_add_idx), None)
            if first_reset_idx is not None:
                seg1_candidates.append({
                    "start_idx": add_idx,
                    "end_idx": first_reset_idx + 1,
                    "anchor_ts": _line_ts(first_reset_idx) or _line_ts(add_idx),
                })
            else:
                print(f"[PreScan] ⚠️  No RESET found after ADD at line {add_idx+1} before next ADD.")

        if seg1_candidates:
            chosen = None
            if self.issue_time:
                with_ts = [c for c in seg1_candidates if c.get("anchor_ts") is not None]
                if with_ts:
                    chosen = min(with_ts, key=lambda c: abs((c["anchor_ts"] - self.issue_time).total_seconds()))
            if chosen is None:
                chosen = seg1_candidates[0]

            first_add_idx = chosen["start_idx"]
            seg1_end = chosen["end_idx"]
            self._driver_init_lines = self._raw_log_cache[first_add_idx:seg1_end]
            print(
                f"[PreScan] ✅ Segment1 — Driver init block: "
                f"line {first_add_idx + 1} → line {seg1_end} "
                f"({len(self._driver_init_lines)} lines) | "
                f"driver load occurrences in full log: {self._driver_init_count}"
            )
            if self.issue_time and chosen.get("anchor_ts") is not None:
                print(
                    f"[PreScan]  Segment1 selected by nearest issue_time: "
                    f"{chosen['anchor_ts'].strftime('%m/%d/%Y %H:%M:%S.%f')[:-3]}"
                )

        if first_add_idx is None or seg1_end is None:
            print("[PreScan] ⚠️  No valid Segment1 block found.")

        # ------------------------------------------------------------------
        # Segment 2: event investigation window
        # ------------------------------------------------------------------
        seg2_lines: List[str] = []
        seg2_start_idx: int = -1
        seg2_end_idx:   int = -1

        if (self.issue_time and self.capabilities.scope_time_only_logs
                and not self._log_has_date()):
            # --- 2A (TIME-ONLY logs, e.g. DDD/tracefmt with no date) ---
            # The log carries no date, so match the issue_time's TIME-OF-DAY
            # only (seconds-of-day). issue_time's date part (if any) is ignored.
            _win = self._effective_issue_window()
            issue_sod = (self.issue_time.hour * 3600 + self.issue_time.minute * 60
                         + self.issue_time.second)
            win_sec = _win * 60
            # Seconds-of-day is cyclic — the time-of-day axis wraps at
            # midnight (86400). Issue times near 00:00 or 23:59 with a
            # symmetric window will produce a lo/hi that crosses midnight
            # (negative lo, or hi >= 86400). Build the predicate so it
            # tests the union of the two valid sub-ranges in that case,
            # so late-night entries that should be in the window are kept.
            # The common case (window comfortably inside one day) falls
            # through to a single-interval comparison.
            SEC_PER_DAY = 86400
            lo_sod, hi_sod = issue_sod - win_sec, issue_sod + win_sec
            if 0 <= lo_sod and hi_sod < SEC_PER_DAY:
                _in_window = lambda sod: lo_sod <= sod <= hi_sod
            else:
                lo_norm = lo_sod % SEC_PER_DAY
                hi_norm = hi_sod % SEC_PER_DAY
                if lo_norm <= hi_norm:
                    _in_window = lambda sod: lo_norm <= sod <= hi_norm
                else:
                    # Window wraps midnight — split into two valid ranges.
                    _in_window = lambda sod: sod >= lo_norm or sod <= hi_norm
            _time_re = re.compile(r'\b(\d{1,2}):(\d{2}):(\d{2})')
            # Track only first/last matching indices instead of every match.
            # On a multi-hour DDD trace a wide capture window can land
            # thousands of hits — accumulating them all into a List[int]
            # spikes memory for no gain since we only ever read the first
            # and the last to slice the cache.
            seg2_first_hit: Optional[int] = None
            seg2_last_hit: Optional[int] = None
            for i, line in enumerate(self._raw_log_cache):
                m = _time_re.search(line)
                if not m:
                    continue
                sod = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                if _in_window(sod):
                    if seg2_first_hit is None:
                        seg2_first_hit = i
                    seg2_last_hit = i
            if seg2_first_hit is not None and seg2_last_hit is not None:
                seg2_start_idx = seg2_first_hit
                seg2_end_idx = seg2_last_hit
                seg2_lines = self._raw_log_cache[seg2_start_idx:seg2_end_idx + 1]
                print(
                    f"[PreScan] ✅ Segment2 (time-only log) — Issue-time window: "
                    f"line {seg2_start_idx + 1} → line {seg2_end_idx + 1} "
                    f"({len(seg2_lines)} lines) | ±{_win} min of "
                    f"{self.issue_time.strftime('%H:%M:%S')} (log has no date)"
                )
            else:
                print(
                    f"[PreScan] ⚠️  No time-only anchors within ±{_win} min of "
                    f"{self.issue_time.strftime('%H:%M:%S')} (log has no date) — falling back."
                )

        elif self.issue_time:
            # --- 2A: issue_time ±N min (timestamp indices + contiguous slice) ---
            _win = self._effective_issue_window()
            window_start = self.issue_time - timedelta(minutes=_win)
            window_end   = self.issue_time + timedelta(minutes=_win)

            ts_points: List[Tuple[datetime, int]] = []
            for i, line in enumerate(self._raw_log_cache):
                m = TS_RE.search(line)
                if m:
                    try:
                        t = datetime.strptime(m.group(1), "%m/%d/%Y-%H:%M:%S.%f")
                        ts_points.append((t, i))
                    except ValueError:
                        pass

            # --- Sanity check: verify attachment issue_time falls within log's actual time range ---
            if ts_points:
                log_first_ts = ts_points[0][0]
                log_last_ts  = ts_points[-1][0]
                # Allow a 24-hour grace margin (attachment time vs. log may have slight mismatch)
                reasonable = (
                    (log_first_ts - timedelta(hours=24)) <= self.issue_time <= (log_last_ts + timedelta(hours=24))
                )

                # If issue_time came from time-only (HH:MM[:SS]) and date is mismatched,
                # align it to the log date so Segment2 can still use +/-5 minutes.
                if not reasonable and self._issue_time_time_only:
                    aligned = datetime.combine(log_last_ts.date(), self.issue_time.time())
                    print(
                        f"[PreScan] ℹ️  issue_time is time-only; aligning date to log: "
                        f"{self.issue_time.strftime('%m/%d/%Y %H:%M:%S')} -> "
                        f"{aligned.strftime('%m/%d/%Y %H:%M:%S')}"
                    )
                    self.issue_time = aligned
                    window_start = self.issue_time - timedelta(minutes=_win)
                    window_end = self.issue_time + timedelta(minutes=_win)
                    reasonable = (
                        (log_first_ts - timedelta(hours=24)) <= self.issue_time <= (log_last_ts + timedelta(hours=24))
                    )

                if not reasonable:
                    print(
                        f"[PreScan] ⚠️  WARNING: Attachment issue_time "
                        f"{self.issue_time.strftime('%m/%d/%Y %H:%M:%S')} is outside the log time range "
                        f"[{log_first_ts.strftime('%m/%d/%Y %H:%M:%S')} ~ "
                        f"{log_last_ts.strftime('%m/%d/%Y %H:%M:%S')}] — "
                        f"attachment time attached is unreasonable, falling back to Segment1 end → EOF."
                    )
                    # Clear issue_time so Segment2 falls through to 2B fallback
                    self.issue_time = None
                else:
                    ts_values = [x[0] for x in ts_points]
                    left = bisect_left(ts_values, window_start)
                    right = bisect_right(ts_values, window_end) - 1
                    if left <= right:
                        seg2_start_idx = ts_points[left][1]
                        seg2_end_idx = ts_points[right][1]
                        seg2_lines = self._raw_log_cache[seg2_start_idx:seg2_end_idx + 1]

            if seg2_lines and seg2_start_idx >= 0 and seg2_end_idx >= 0:
                print(
                    f"[PreScan] ✅ Segment2 — Issue-time window: "
                    f"line {seg2_start_idx + 1} → line {seg2_end_idx + 1} "
                    f"({len(seg2_lines)} lines, contiguous index slice) | "
                    f"±{_win} min of {self.issue_time.strftime('%m/%d/%Y %H:%M:%S')}"
                )
            elif self.issue_time is not None:
                print(
                    f"[PreScan] ⚠️  No timestamped anchors within ±{_win} min of "
                    f"{self.issue_time.strftime('%m/%d/%Y %H:%M:%S')} — "
                    f"falling back to Segment1 end → EOF."
                )
                # fall through to 2B below

        if not seg2_lines:
            # --- 2B fallback: Segment1 end → EOF ---
            if seg1_end is not None:
                seg2_start = seg1_end  # pick up right where Segment1 left off
                seg2_lines     = self._raw_log_cache[seg2_start:]
                seg2_start_idx = seg2_start
                seg2_end_idx   = total_lines - 1
                print(
                    f"[PreScan] ✅ Segment2 — Segment1 end → EOF: "
                    f"line {seg2_start + 1} → line {total_lines} "
                    f"({len(seg2_lines)} lines)"
                )
            else:
                print("[PreScan] ⚠️  No driver markers found — Segment2 empty.")

        # Don't leave the agent with nothing to analyse when neither a marker
        # block nor an issue-time window matched. This happens for:
        #   * time-only logs (e.g. DDD) with no in-window anchors, and
        #   * any log family that defines no init/reset lifecycle and so opts
        #     into SCOPE_FULL_LOG_WHEN_EMPTY (e.g. the BT agent).
        # The downstream skill keyword filter trims the full scope back down,
        # so scoping everything is a safe fallback rather than a cost blow-up.
        expand_undated = (
            self.capabilities.full_scope_for_undated_logs
            and not self._log_has_date()
        )
        if not seg2_lines and total_lines and (self.SCOPE_FULL_LOG_WHEN_EMPTY or expand_undated):
            seg2_lines = list(self._raw_log_cache)
            seg2_start_idx = 0
            seg2_end_idx = total_lines - 1
            print(f"[PreScan] ℹ️  No marker block / issue-time window — scoping full log ({total_lines} lines).")

        self._issue_time_window_lines = seg2_lines

        # ------------------------------------------------------------------
        # Merge Segment1 + Segment2 → _scoped_log_lines (de-duplicated)
        # ------------------------------------------------------------------
        # Use an OrderedDict-style approach: keep insertion order, skip dupes.
        seen_ids: set = set()
        merged: List[str] = []
        for line in self._driver_init_lines + seg2_lines:
            lid = id(line)           # same object from _raw_log_cache → same id
            if lid not in seen_ids:
                seen_ids.add(lid)
                merged.append(line)
        self._scoped_log_lines = merged

        overlap = len(self._driver_init_lines) + len(seg2_lines) - len(merged)
        print(
            f"[PreScan] 📦 Scoped segments merged: {len(merged)} lines "
            f"(Seg1: {len(self._driver_init_lines)} + Seg2: {len(seg2_lines)} — overlap: {overlap})"
        )
        if self.issue_time:
            print(f"[PreScan]  Skill filter will use: scoped {len(merged)} lines (±{_win} min window around issue_time)")
        else:
            print(
                f"[PreScan]  Skill filter will use: scoped {len(merged)} lines "
                f"(no issue_time — Segment2 falls back to Segment1 end → EOF)"
            )

        scoped_path = self._export_scoped_log_file()
        if scoped_path:
            print(f"[PreScan] 💾 Scoped lines saved to: {scoped_path}")

    def _export_scoped_log_file(self) -> str:
        """Overwrite scoped.txt in current log folder with latest scoped lines."""
        if not self.current_log_path:
            return ""

        parent_dir = Path(self.current_log_path).parent
        if not parent_dir.exists():
            return ""

        scoped_text = "\n".join(str(line).rstrip("\n") for line in (self._scoped_log_lines or []))
        scoped_file = parent_dir / "scoped.txt"
        try:
            scoped_file.write_text(scoped_text, encoding="utf-8")
            return str(scoped_file)
        except Exception as e:
            print(f"  ⚠️  Scoped export failed: {e}")
            return ""

    def _export_assembled_log_file(self) -> str:
        """
        Increment export index on each call (filterlog1, filterlog2, ...)
        and overwrite the existing file with the same index.
        """
        if not self.current_log_path:
            return ""

        parent_dir = Path(self.current_log_path).parent
        if not parent_dir.exists():
            return ""
        export_text = self._build_assembled_log_report("merged", new_added=0, apply_limits=False)

        # Always advance index; write_text will overwrite same-name files.
        self._filter_export_counter += 1
        candidate = parent_dir / f"filterlog{self._filter_export_counter}.txt"

        try:
            candidate.write_text(export_text, encoding="utf-8")
            return str(candidate)
        except Exception as e:
            print(f"  ⚠️  Export failed: {e}")
            return ""

    def _strip_line_number_prefix(self, line: str) -> str:
        """Remove persisted line-number markers to save memory in caches."""
        out = re.sub(r'^\s*(?:>>>|\s{3})\s*\[Line\s+\d+\]\s*', '', str(line))
        out = re.sub(r'^\s*\[Line\s+\d+\]\s*', '', out)
        return out.strip()

    def _extract_lines_from_filtered_blob(self, filtered_blob: str) -> List[str]:
        """Extract body lines from '[meta]\\n<body>' filtered output."""
        lines = []
        for idx, raw in enumerate((filtered_blob or "").splitlines()):
            text = str(raw).strip()
            if not text:
                continue
            # First line is metadata header from _get_filtered_log_lines.
            if idx == 0 and text.startswith('[') and text.endswith(']'):
                continue
            lines.append(self._strip_line_number_prefix(text))
        return [x for x in lines if x]

    def _normalize_time_message(self, line: str) -> Tuple[Optional[datetime], str, str]:
        """
        Keep only HH:MM:SS + message from a raw filtered line.
        Supports:
          * Full dated timestamps  ``MM/DD/YYYY-HH:MM:SS.mmm`` (WiFi WPP)
          * BT HCI angle-bracket   ``<HH:MM:SS.mmm>``          (ibtpci HCI dump)
          * DDD compact marker     ``<TIME:HH:MM:SS>`` / ``TIME:HH:MM:SS``
        Returns (parsed_ts_or_none, hhmmss_or_na, message_only).

        BT HCI carries no date — a synthetic datetime is built from the
        agent's ``issue_time`` date (or today's date) so the assembled-log
        sort key and time-range header still work.
        """
        raw = self._strip_line_number_prefix(line)

        # Prefer full date timestamp when present for stable chronological sorting.
        # dt_match = re.search(r'(\d{2}/\d{2}/\d{4}-(\d{2}:\d{2}:\d{2})\.\d{3})', raw)
        dt_match = re.search(r'(\d{2}/\d{2}/\d{4}-(\d{2}:\d{2}:\d{2}\.\d{3}))', raw)
        if dt_match:
            ts_full = dt_match.group(1)
            ts_hms = dt_match.group(2)
            ts_dt = None
            try:
                ts_dt = datetime.strptime(ts_full, "%m/%d/%Y-%H:%M:%S.%f")
            except ValueError:
                ts_dt = None
            msg = (raw[:dt_match.start()] + raw[dt_match.end():]).strip(" -:|\t")
        else:
            # BT HCI angle-bracket timestamp ``<HH:MM:SS.mmm>`` (no date).
            # Matched before the legacy ``<TIME:...>`` form because the BT
            # variant has no ``TIME:`` prefix and would otherwise fall
            # through to the "N/A" branch, losing time-axis fidelity.
            hci_match = re.search(r'<(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?>', raw)
            if hci_match:
                h  = int(hci_match.group(1))
                mn = int(hci_match.group(2))
                s  = int(hci_match.group(3))
                ms_str = hci_match.group(4) or "0"
                ms = int(ms_str.ljust(3, "0")[:3])
                ts_hms = f"{h:02d}:{mn:02d}:{s:02d}.{ms:03d}"
                # Synthesise a datetime so the assembled-log sort key and
                # time-range header keep working for time-only BT logs.
                # Prefer the agent's issue_time date; fall back to today.
                _base_date = (self.issue_time.date() if getattr(self, "issue_time", None)
                              else datetime.today().date())
                try:
                    ts_dt = datetime(_base_date.year, _base_date.month, _base_date.day,
                                     h, mn, s, ms * 1000)
                except ValueError:
                    ts_dt = None
                msg = (raw[:hci_match.start()] + raw[hci_match.end():]).strip(" -:|\t")
            else:
                # Fallback: handle logs like <TIME:09:54:13> or TIME:09:54:13
                time_tag_match = re.search(r'(?:<)?TIME:(\d{2}:\d{2}:\d{2})(?:>)?', raw, flags=re.IGNORECASE)
                ts_hms = time_tag_match.group(1) if time_tag_match else "N/A"
                ts_dt = None
                if time_tag_match:
                    msg = (raw[:time_tag_match.start()] + raw[time_tag_match.end():]).strip(" -:|\t")
                else:
                    msg = raw

        # Keep the payload part after function markers when present, e.g. "[func]:### ..."
        if "###" in msg:
            msg = msg[msg.find("###"):]
        elif "]:" in msg:
            # Keep the last [tag] before ]: and prepend it to the payload.
            idx = msg.rfind("]:")
            bstart = msg.rfind("[", 0, idx)
            if bstart >= 0:
                last_tag = msg[bstart:idx + 1]
                payload = msg[idx + 2:].strip(" -:|\t")
                msg = (last_tag + " " + payload).strip()
            else:
                msg = msg[idx + 2:].strip(" -:|\t")

        # Remove leading bracket-like channel tags, keeping the last one with the message.
        msg = re.sub(r'(?:<)?TIME:\d{2}:\d{2}:\d{2}(?:>)?', '', msg, flags=re.IGNORECASE)
        msg = re.sub(r'^(?:\[[^\]]+\]\s*)+(?=\[)', '', msg).strip()
        msg = re.sub(r'\s{2,}', ' ', msg).strip()

        return ts_dt, ts_hms, (msg or raw)

    def _merge_lines_into_assembled_log(self, lines: List[str], skill_name: str) -> Tuple[int, int]:
        """
        Merge new filtered lines into cumulative assembled log by timestamp.
        Storage never keeps line numbers, only timestamp + content + source skill.
        Returns: (new_added_count, total_count)
        """
        added = 0
        for line in lines:
            ts, ts_display, message = self._normalize_time_message(line)
            compact_text = f"<{ts_display}> {message}".strip()
            content_hash = hashlib.md5(compact_text.encode('utf-8')).hexdigest()
            if ts:
                ts_key = ts.strftime("%m/%d/%Y-%H:%M:%S.%f")
                key = f"{ts_key}|{content_hash}"
                if key not in self._assembled_entries_by_key:
                    self._assembled_entries_by_key[key] = {
                        "ts": ts,
                        "text": compact_text,
                        "skills": {skill_name},
                    }
                    added += 1
                else:
                    self._assembled_entries_by_key[key]["skills"].add(skill_name)
            else:
                key = content_hash
                if key not in self._assembled_entries_no_ts:
                    self._assembled_entries_no_ts[key] = {
                        "ts": None,
                        "text": compact_text,
                        "skills": {skill_name},
                    }
                    added += 1
                else:
                    self._assembled_entries_no_ts[key]["skills"].add(skill_name)

        with_ts = sorted(
            self._assembled_entries_by_key.values(),
            key=lambda x: (x["ts"], x["text"])
        )
        no_ts = sorted(
            self._assembled_entries_no_ts.values(),
            key=lambda x: x["text"]
        )
        assembled_lines = [e["text"] for e in with_ts] + [e["text"] for e in no_ts]
        self._assembled_log_text = "\n".join(assembled_lines)
        return added, len(assembled_lines)

    def _build_assembled_log_report(self, trigger_skill: str, new_added: int,
                                    apply_limits: bool = True) -> str:
        """Build assembled-log text for reasoning (limited) or file export (full)."""
        ts_values = [x["ts"] for x in self._assembled_entries_by_key.values() if x.get("ts")]
        first_ts = min(ts_values).strftime('%m/%d/%Y %H:%M:%S') if ts_values else "N/A"
        last_ts = max(ts_values).strftime('%m/%d/%Y %H:%M:%S') if ts_values else "N/A"
        skills_seen = sorted(self._filter_cache_by_skill.keys())

        header = [
            f"Assembled from {len(skills_seen)} skill filter(s): {', '.join(skills_seen) if skills_seen else trigger_skill}",
            f"Trigger skill: {trigger_skill}",
            f"New merged lines this round: {new_added}",
            f"Total assembled lines: {len(self._assembled_log_text.splitlines()) if self._assembled_log_text else 0}",
            f"Time range: {first_ts} → {last_ts}",
            "Storage rule: only timestamp + message are persisted in cache.",
        ]

        body = self._assembled_log_text
        if apply_limits:
            all_lines = self._assembled_log_text.splitlines() if self._assembled_log_text else []
            max_lines = self.MAX_ASSEMBLED_LOG_LINES_PER_TOOL_CALL
            if len(all_lines) > max_lines:
                head_n = max_lines // 2
                tail_n = max_lines - head_n
                kept_lines = all_lines[:head_n] + [
                    f"... ({len(all_lines) - max_lines} lines omitted for token safety) ..."
                ] + all_lines[-tail_n:]
                body = "\n".join(kept_lines)
                header.append(
                    f"⚠ Lines windowed: showing {max_lines}/{len(all_lines)} lines for token safety."
                )

            if len(body) > self.MAX_ASSEMBLED_LOG_CHARS_PER_TOOL_CALL:
                body = body[:self.MAX_ASSEMBLED_LOG_CHARS_PER_TOOL_CALL]
                header.append(
                    f"⚠ Output truncated at {self.MAX_ASSEMBLED_LOG_CHARS_PER_TOOL_CALL} chars for token safety."
                )

        return "[" + " | ".join(header) + "]\n" + body

    def _build_skill_focus_payload(self, skill_name: str, new_added: int, total_count: int) -> str:
        """
        Build a compact, token-efficient payload for current skill analysis.
        Includes only this skill's filtered messages + a tiny cross-skill history glimpse.
        """
        cached = self._filter_cache_by_skill.get(skill_name, {})
        lines = list(cached.get("lines", []) or [])
        line_count = len(lines)

        # Keep head+tail to preserve both early and latest evidence in this skill.
        focus_lines = lines
        if line_count > self.MAX_SKILL_FOCUS_LINES:
            head_n = self.MAX_SKILL_FOCUS_LINES // 2
            tail_n = self.MAX_SKILL_FOCUS_LINES - head_n
            focus_lines = lines[:head_n] + [
                f"... ({line_count - self.MAX_SKILL_FOCUS_LINES} lines omitted) ..."
            ] + lines[-tail_n:]

        # Small cross-skill reminder, not full assembled body.
        other_skills = [k for k in sorted(self._filter_cache_by_skill.keys()) if k != skill_name]
        recent_history = []
        for sk in other_skills[-3:]:
            sk_lines = self._filter_cache_by_skill.get(sk, {}).get("lines", []) or []
            if not sk_lines:
                continue
            sample = sk_lines[-min(5, len(sk_lines)):]
            recent_history.append(f"--- {sk} ({len(sk_lines)} lines cached) ---")
            recent_history.extend(sample)
        if len(recent_history) > self.MAX_RECENT_SKILL_HISTORY_LINES:
            recent_history = recent_history[:self.MAX_RECENT_SKILL_HISTORY_LINES]
            recent_history.append("... (recent skill history truncated) ...")

        parts = [
            f"=== Skill Focus: {skill_name} ===",
            f"Current skill matched lines: {line_count}",
            f"New lines merged this round: {new_added}",
            f"Assembled total lines (stored): {total_count}",
            "",
            "=== Current Skill Evidence (message-only compact view) ===",
            "\n".join(focus_lines) if focus_lines else "(no lines)",
        ]

        if recent_history:
            parts.extend([
                "",
                "=== Recent Cross-Skill Hints (compact) ===",
                "\n".join(recent_history),
            ])

        text = "\n".join(parts)
        if len(text) > self.MAX_SKILL_FOCUS_CHARS:
            text = text[:self.MAX_SKILL_FOCUS_CHARS] + (
                "\n... (skill-focused payload truncated for token safety)"
            )
        return text

    def get_assembled_log_snapshot(self, mode: str = "summary") -> str:
        """
        On-demand assembled-log view for macro analysis.
        mode=summary: metadata only; mode=compact: limited body; mode=full: full body.
        """
        if not (self._assembled_log_text or "").strip():
            return "No assembled log is available yet. Call fetch_filtered_logs(skill_name) first."

        mode = (mode or "summary").strip().lower()
        if mode not in ("summary", "compact", "full"):
            return "Error: mode must be one of: summary, compact, full"

        if mode == "summary":
            return self._build_assembled_log_report("snapshot", new_added=0, apply_limits=False).split("\n", 1)[0]
        if mode == "compact":
            return self._build_assembled_log_report("snapshot", new_added=0, apply_limits=True)
        return self._build_assembled_log_report("snapshot", new_added=0, apply_limits=False)

    def get_final_state_snapshot(self, tail_lines: int = 120) -> str:
        """Return a compact view focused on latest state for end-of-analysis verification."""
        lines = max(20, min(int(tail_lines or 120), 400))
        return self._get_assembled_log_tail(max_lines=lines)

    def _resolve_context_span(self, anchor_text: str, requested_span: int) -> int:
        """Auto-expand detail context for scan/connect style transactions."""
        low = (anchor_text or "").lower()
        span = requested_span if requested_span and requested_span > 0 else self.DEFAULT_DETAIL_CONTEXT_SPAN
        if any(tok in low for tok in ("scan", "connect", "assoc", "roam", "auth", "oid", "wdi_task")):
            span = max(span, 50)
        return min(span, self.MAX_DETAIL_CONTEXT_SPAN)

    def _clip_for_prompt(self, text: str, limit: int = None) -> str:
        """Trim long text before appending into LLM messages."""
        if text is None:
            return ""
        lim = limit or self.MAX_TOOL_CONTENT_CHARS_IN_MESSAGES
        if len(text) <= lim:
            return text
        return text[:lim] + f"\n... (truncated for token safety, kept {lim} chars)"

    def _get_filtered_log_lines(self, skill_name: str, apply_output_limit: bool = True) -> str:
        """
        Apply the TAT keyword filter for `skill_name` to the current log file
        using the same pipeline as LogParserService.process_analysis:
          read_log_file → extract_enabled_keywords_from_filter_file
          → filter_log_by_keywords → preprocess_log_for_llm → group_similar_logs
        Falls back to simple regex matching when no tat_path is available.
        """
        skill = self.skills.get(skill_name)
        if not skill:
            return f"Error: skill '{skill_name}' not found."
        load_err = self._ensure_raw_log_cache()
        if load_err:
            return load_err

        #  scoped lines（Segment1+Segment2）
        log_lines = self._scoped_log_lines

        # --- keyword extraction: prefer TAT file, fall back to in-memory list ---
        if skill.tat_path and Path(skill.tat_path).exists():
            keywords = extract_enabled_keywords_from_filter_file(skill.tat_path)
        else:
            keywords = skill.keywords  # fallback: keywords already loaded from TAT

        if not keywords:
            return "No keywords available for this skill."

        # --- same preprocessing steps as log_parser_service.process_analysis ---
        filtered   = filter_log_by_keywords(log_lines, keywords)
        processed  = preprocess_log_for_llm(filtered)
        grouped    = group_similar_logs(processed)

        # Optional post-filter exclusion from skills.yaml:
        # remove noisy lines that are not useful for diagnosis.
        exclusive_terms = [x for x in (skill.exclusive or []) if str(x).strip()]
        excluded_count = 0
        if grouped and exclusive_terms:
            excl_lower = [x.lower() for x in exclusive_terms]
            kept = []
            for line in grouped:
                line_str = str(line)
                # Match against both original grouped line and parsed message body.
                # If either side hits, drop the entire line.
                _, _, message_only = self._normalize_time_message(line_str)
                raw_low = self._strip_line_number_prefix(line_str).lower()
                msg_low = str(message_only).lower()
                if any((term in raw_low) or (term in msg_low) for term in excl_lower):
                    excluded_count += 1
                    continue
                kept.append(line)
            grouped = kept

        if not grouped:
            return "No log lines matched the keywords for this skill."

        # Build temporal metadata so the agent knows the time span covered
        ts_re = re.compile(r'(\d{2}/\d{2}/\d{4}-\d{2}:\d{2}:\d{2}\.\d{3})')
        first_ts = last_ts = None
        for line in grouped:
            m = ts_re.search(str(line))
            if m:
                try:
                    t = datetime.strptime(m.group(1), "%m/%d/%Y-%H:%M:%S.%f")
                    if first_ts is None or t < first_ts:
                        first_ts = t
                    if last_ts is None or t > last_ts:
                        last_ts = t
                except ValueError:
                    pass

        body_full = "\n".join(str(l) for l in grouped)
        body = body_full

        # Prepend time-range header so the agent sees whether it has
        # full-timeline coverage or only a partial window
        meta_parts = [f"Matched {len(grouped)} lines."]
        if excluded_count > 0:
            meta_parts.append(
                f"Excluded {excluded_count} lines by exclusive keywords."
            )
        if first_ts and last_ts:
            meta_parts.append(
                f"Time range: {first_ts.strftime('%m/%d/%Y %H:%M:%S')} "
                f"→ {last_ts.strftime('%m/%d/%Y %H:%M:%S')}"
            )
            if apply_output_limit and len(body_full) > 15000:
                meta_parts.append(
                    "⚠ Output truncated at 15 KB for display. "
                    "Merged assembled storage still uses full filtered lines."
                )
        header = " | ".join(meta_parts)
        if apply_output_limit and len(body) > 15000:
            body = body[:15000]
        return f"[{header}]\n{body}"
