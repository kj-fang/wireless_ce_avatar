"""Unit tests for the Beacon (Handsfree) review funnel in
``services.gather_service``: the events written before a post
(``record_handsfree_event`` / ``record_handsfree_listed``), the rollup that
turns them into the overview (``handsfree_funnel``), and the route the pages
report a view through (``POST /handsfree/queue/<draft_id>/viewed``), and the
orchestrator's calls into both (``TestHandsfreeTrackingCallers``, on a queue
in a temp folder).

Nothing here touches the shared Gather folder: the writer tests pin the root
to a temp directory, and ``BaseTestCase`` keeps collection switched off for
the route tests.
"""
import json
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from blueprints.handsfree.handsfree_routes import handsfree_bp
from services import gather_service as gather
from services.handsfree import orchestrator as orch
from services.handsfree.composer import (AI_MARKER, ECHO_SECTION_END, ECHO_SECTION_START,
                                         compose_html)
from services.handsfree.ips_client import IpsClient, PostResult, PostUnsupported
from services.handsfree.queue import HandsfreeStore
from unit_tests.base_test import BaseTestCase

ROUTES = "blueprints.handsfree.handsfree_routes"


def _settle() -> None:
    """Wait for the Gather writer threads started so far."""
    for t in list(threading.enumerate()):
        if t.name.startswith("gather-handsfree"):
            t.join(timeout=10)


class TestHandsfreeEventWriters(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gather_funnel_"))
        self._saved = (gather._root_cache, gather._outbox_root, gather._listed_day,
                       set(gather._listed_seen))
        gather._root_cache = gather._ensure_layout(self.tmp)
        gather._outbox_root = None
        gather._listed_day, gather._listed_seen = "", set()
        self._patches = [
            patch.object(gather, "_collection_enabled", return_value=True),
            patch.object(gather, "_maybe_rebuild_aggregates_async"),
            patch.object(gather, "_announce_developer_collection"),
        ]
        for p in self._patches:
            p.start()
        self.events_dir = gather._mode_root(self.tmp) / "handsfree_events"

    def tearDown(self):
        _settle()
        for p in self._patches:
            p.stop()
        (gather._root_cache, gather._outbox_root, gather._listed_day,
         gather._listed_seen) = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _records(self) -> list:
        _settle()
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.events_dir.rglob("*.json"))]

    def test_processed_is_one_record_per_draft(self):
        """A repeated `processed` report of one draft stays one file."""
        for _ in range(2):
            event_id = gather.record_handsfree_event(
                "processed", draft_id="01090001__20261123T101010_abc123", case_nbr="01090001",
                handsfree_role="server",
                details={"outcome": "queued", "mode": "full", "trial": False, "processing_seconds": 42.5,
                         "confidence": None})
            _settle()
        recs = self._records()
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(event_id, "01090001__20261123T101010_abc123__processed")
        self.assertEqual(rec["record_type"], "handsfree_event")
        self.assertEqual(rec["handsfree_event_version"], gather.HANDSFREE_EVENT_VERSION)
        self.assertEqual((rec["kind"], rec["outcome"], rec["mode"], rec["handsfree_role"]),
                         ("processed", "queued", "full", "server"))
        self.assertEqual(rec["processing_seconds"], 42.5)
        self.assertIs(rec["trial"], False)
        self.assertNotIn("confidence", rec)          # None is dropped, not stored
        self.assertTrue(rec["user_name"] and rec["at"] and rec["updated_at"])

    def test_view_open_and_close_merge_into_one_record(self):
        """The page reports a view twice under one id: opened, then closed
        with its duration. One record, the opening time kept."""
        gather.record_handsfree_event("viewed", draft_id="d-1", case_nbr="01090001",
                                      via="home_dashboard", key="v-abc",
                                      details={"draft_status": "pending_review"})
        opened = self._records()[0]
        gather.record_handsfree_event("viewed", draft_id="d-1", case_nbr="01090001",
                                      via="home_dashboard", key="v-abc",
                                      details={"dwell_seconds": 75})
        recs = self._records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["event_id"], "d-1__viewed__v-abc")
        self.assertEqual(recs[0]["at"], opened["at"])
        self.assertEqual(recs[0]["dwell_seconds"], 75)
        self.assertEqual(recs[0]["draft_status"], "pending_review")
        self.assertEqual(recs[0]["via"], "home_dashboard")

    def test_a_closing_report_written_first_does_not_move_the_view_time(self):
        """The opening and the closing report of a view have a writer thread
        each; whichever writes first, the view happened when it was opened."""
        opened = {"event_id": "d-1__viewed__v-9", "kind": "viewed", "draft_id": "d-1",
                  "case_nbr": "01090001", "via": "home_dashboard", "handsfree_role": "client",
                  "at": "2026-11-23T10:00:00+08:00"}
        closed = {**opened, "at": "2026-11-23T10:05:00+08:00", "dwell_seconds": 300}

        gather._do_record_handsfree_event(dict(closed))          # the closing report wins the race
        gather._do_record_handsfree_event(dict(opened))

        recs = self._records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["at"], "2026-11-23T10:00:00+08:00")
        self.assertEqual(recs[0]["dwell_seconds"], 300)

        gather._do_record_handsfree_event(dict(closed))          # and in the usual order it stays put
        self.assertEqual(self._records()[0]["at"], "2026-11-23T10:00:00+08:00")

    def test_removed_is_one_record_per_draft_with_its_reason(self):
        for _ in range(2):
            gather.record_handsfree_event("removed", draft_id="d-1", case_nbr="01090001",
                                          details={"reason": "case_closed"})
            _settle()
        recs = self._records()
        self.assertEqual([(r["event_id"], r["kind"], r["reason"]) for r in recs],
                         [("d-1__removed", "removed", "case_closed")])

    def test_a_post_record_leaves_the_text_work_to_the_writer_thread(self):
        """record_handsfree_post is called while an approval holds the post
        lock: the line diff must not run on the caller's thread."""
        diffed_on = []
        real = gather.summarise_edit

        def _spy(suggested, posted):
            diffed_on.append(threading.current_thread().name)
            return real(suggested, posted)

        suggested = "Hello\nPlease collect logs.\nINTERNAL: firmware bug\n"
        with patch.object(gather, "summarise_edit", side_effect=_spy):
            gather.record_handsfree_post(
                draft_id="d-1", case_nbr="01090001", via="beacon_page", ok=True,
                suggested_plain=suggested, suggested_source="enqueue",
                posted_plain="Hello\nPlease collect logs.\n", echo_stripped=True,
                # the suggestion without the part the system took out, made on the writer thread
                edit_baseline_plain=lambda text: text.replace("INTERNAL: firmware bug\n", ""))
            _settle()

        self.assertEqual(diffed_on, ["gather-handsfree-post"])
        posts = [json.loads(p.read_text(encoding="utf-8"))
                 for p in (gather._mode_root(self.tmp) / "handsfree_posts").rglob("*.json")]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["suggested"]["text"], suggested)
        self.assertEqual(posts[0]["edit"]["baseline"], "suggested_without_echo")
        self.assertIs(posts[0]["edit"]["changed"], False)
        self.assertIs(posts[0]["edit"]["echo_stripped"], True)

    def test_an_update_only_report_completes_a_recorded_view_and_never_starts_one(self):
        """The closing report of a view whose draft has left the queue."""
        gather.record_handsfree_event("viewed", draft_id="ghost", key="v-1", update_only=True,
                                      details={"dwell_seconds": 40})
        gather.record_handsfree_event("viewed", draft_id="d-1", update_only=True,     # no view id
                                      details={"dwell_seconds": 40})
        self.assertEqual(self._records(), [])

        gather.record_handsfree_event("viewed", draft_id="d-1", case_nbr="01090001",
                                      via="beacon_page", key="v-1", details={"mode": "full"})
        _settle()
        gather.record_handsfree_event("viewed", draft_id="d-1", key="v-1", update_only=True,
                                      details={"dwell_seconds": 40})

        recs = self._records()
        self.assertEqual(len(recs), 1)
        self.assertEqual((recs[0]["dwell_seconds"], recs[0]["via"], recs[0]["case_nbr"], recs[0]["mode"]),
                         (40, "beacon_page", "01090001", "full"))

    def test_every_funnel_write_asks_for_a_rollup(self):
        """Not only a processed case: a day of views alone must still reach
        the published numbers (the helper itself spaces rebuilds out)."""
        rebuild = gather._maybe_rebuild_aggregates_async          # the mock from setUp
        gather.record_handsfree_event("viewed", draft_id="d-1", key="v-1")
        gather.record_handsfree_event("edit_saved", draft_id="d-1")
        gather.record_handsfree_listed([{"draft_id": "d-1", "case_nbr": "01090001"}], via="beacon_page")
        _settle()
        self.assertEqual(rebuild.call_count, 3)

    def test_draining_the_outbox_joins_the_days_listed_drafts_instead_of_replacing_them(self):
        """The share has the drafts shown before an outage, the local buffer
        those shown during it: after the drain the day's file names both."""
        share, local = self.tmp / "share", self.tmp / "local"
        name = "listed__20261123.json"

        def listed(root, drafts, updated_at):
            path = gather._mode_root(root) / "handsfree_events" / "ann" / name
            gather._write_json_atomic(path, {"record_type": "handsfree_listed_day", "user_name": "ann",
                                             "drafts": drafts, "updated_at": updated_at})
            return path

        for label, local_stamp, shared_stamp in (("the buffered copy is newer", "2026-11-23T12:00:00+08:00",
                                                  "2026-11-23T09:00:00+08:00"),
                                                 ("the shared copy is newer", "2026-11-23T09:00:00+08:00",
                                                  "2026-11-23T12:00:00+08:00")):
            with self.subTest(label):
                on_share = listed(share, {"A": {"at": "2026-11-23T08:00:00+08:00", "via": "beacon_page"},
                                          "C": {"at": "2026-11-23T08:30:00+08:00", "via": "beacon_page"}},
                                  shared_stamp)
                buffered = listed(local, {"B": {"at": "2026-11-23T11:00:00+08:00", "via": "home_dashboard"},
                                          "C": {"at": "2026-11-23T11:30:00+08:00", "via": "home_dashboard"}},
                                  local_stamp)

                gather._drain_outbox(local, share)

                merged = json.loads(on_share.read_text(encoding="utf-8"))
                self.assertEqual(sorted(merged["drafts"]), ["A", "B", "C"])
                self.assertEqual(merged["drafts"]["C"]["at"], "2026-11-23T08:30:00+08:00")   # first shown
                self.assertEqual(merged["updated_at"], "2026-11-23T12:00:00+08:00")
                self.assertFalse(buffered.exists())

    def test_draining_the_outbox_still_lets_the_later_snapshot_of_an_event_win(self):
        share, local = self.tmp / "share", self.tmp / "local"

        def event(root, dwell, updated_at):
            path = gather._mode_root(root) / "handsfree_events" / "ann" / "d-1__viewed__v-1.json"
            gather._write_json_atomic(path, {"record_type": "handsfree_event", "kind": "viewed",
                                             "dwell_seconds": dwell, "updated_at": updated_at})
            return path

        on_share = event(share, 10, "2026-11-23T09:00:00+08:00")
        event(local, 300, "2026-11-23T12:00:00+08:00")

        gather._drain_outbox(local, share)

        self.assertEqual(json.loads(on_share.read_text(encoding="utf-8"))["dwell_seconds"], 300)

    def test_repeatable_kinds_get_a_record_each_and_bad_input_is_ignored(self):
        gather.record_handsfree_event("edit_saved", draft_id="d-1", via="beacon_page")
        gather.record_handsfree_event("edit_saved", draft_id="d-1", via="somewhere_else")
        self.assertEqual(gather.record_handsfree_event("not_a_kind", draft_id="d-1"), "")
        self.assertEqual(gather.record_handsfree_event("viewed"), "")
        recs = self._records()
        self.assertEqual(len(recs), 2)
        self.assertEqual(sorted(r["via"] for r in recs), ["api", "beacon_page"])

    def test_dwell_is_capped_and_details_are_kept_small(self):
        gather.record_handsfree_event(
            "viewed", draft_id="d-1", key="v-1",
            details={"dwell_seconds": 999999, "reason": "x" * 5000, "nested": {"a": 1}})
        rec = self._records()[0]
        self.assertEqual(rec["dwell_seconds"], gather._MAX_DWELL_SEC)
        self.assertEqual(len(rec["reason"]), gather._MAX_EVENT_TEXT)
        self.assertIsInstance(rec["nested"], str)

    def test_listed_keeps_one_file_per_day_and_reports_a_draft_once(self):
        rows = [{"draft_id": "d-1", "case_nbr": "01090001"}, {"draft_id": "d-2", "case_nbr": "01090002"}]
        self.assertEqual(gather.record_handsfree_listed(rows, via="home_dashboard", handsfree_role="client"), 2)
        self.assertEqual(gather.record_handsfree_listed(rows, via="beacon_page"), 0)      # the next poll
        self.assertEqual(gather.record_handsfree_listed(rows + [{"draft_id": "d-3"}], via="beacon_page"), 1)
        recs = self._records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["record_type"], "handsfree_listed_day")
        self.assertEqual(sorted(recs[0]["drafts"]), ["d-1", "d-2", "d-3"])
        self.assertEqual(recs[0]["drafts"]["d-1"]["via"], "home_dashboard")           # first sighting wins
        self.assertEqual(recs[0]["drafts"]["d-3"]["via"], "beacon_page")

    def test_nothing_is_written_when_collection_is_off(self):
        with patch.object(gather, "_collection_enabled", return_value=False):
            self.assertEqual(gather.record_handsfree_event("processed", draft_id="d-1"), "")
            self.assertEqual(gather.record_handsfree_listed([{"draft_id": "d-1"}]), 0)
        self.assertEqual(self._records(), [])

    def test_stats_carry_the_funnel_built_from_the_written_records(self):
        gather.record_handsfree_event("processed", draft_id="d-1", case_nbr="01090001",
                                      details={"outcome": "queued", "mode": "full"})
        gather.record_handsfree_event("viewed", draft_id="d-1", case_nbr="01090001",
                                      via="beacon_page", key="v-1")
        _settle()
        # Readers list the flat (exe) root; a source run writes under developer/.
        gather._root_cache = gather._mode_root(self.tmp)
        stats = gather._handsfree_stats()
        self.assertEqual(stats["posts"], 0)                       # the existing keys are still there
        self.assertEqual(stats["funnel"]["processed"]["drafts_queued"], 1)
        self.assertEqual(stats["funnel"]["viewed"], 1)
        self.assertEqual(stats["funnel"]["viewed_by_via"], {"beacon_page": 1})


class TestHandsfreeFunnelMath(unittest.TestCase):
    """handsfree_funnel is pure: a fixed set of records in, the overview out."""

    T0 = datetime(2026, 11, 2, 9, 0, 0).astimezone()

    def _at(self, **delta) -> str:
        return (self.T0 + timedelta(**delta)).isoformat(timespec="seconds")

    def _processed(self, draft_id, case, *, hours=0, outcome="queued", mode="full", **extra):
        return {"record_type": "handsfree_event", "kind": "processed", "draft_id": draft_id,
                "case_nbr": case, "at": self._at(hours=hours), "outcome": outcome, "mode": mode,
                "source": "ips", "trigger": "auto-scan (scheduled)", "primary": True,
                "processing_seconds": 100, **extra}

    def _viewed(self, draft_id, user, via, *, hours, dwell=None):
        rec = {"record_type": "handsfree_event", "kind": "viewed", "draft_id": draft_id,
               "user_name": user, "via": via, "at": self._at(hours=hours)}
        if dwell is not None:
            rec["dwell_seconds"] = dwell
        return rec

    def _post(self, draft_id, via, *, hours, changed=False, ok=True):
        return {"draft_id": draft_id, "ok": ok, "via": via, "posted_at": self._at(hours=hours),
                "edit": {"changed": changed}}

    def _fixture(self):
        events = [
            # A: shown, opened on both pages, posted after an edit.
            self._processed("A", "01000001"),
            self._viewed("A", "ann", "home_dashboard", hours=2, dwell=60),
            self._viewed("A", "ann", "beacon_page", hours=3, dwell=120),
            {"record_type": "handsfree_event", "kind": "edit_saved", "draft_id": "A", "at": self._at(hours=3)},
            # B: opened and posted exactly as suggested.
            self._processed("B", "01000002", mode="request_logs"),
            self._viewed("B", "bob", "beacon_page", hours=1),
            # C: shown in a queue, never opened -> backlog.
            self._processed("C", "01000003"),
            # D: opened, then rejected.
            self._processed("D", "01000004"),
            self._viewed("D", "ann", "home_dashboard", hours=5),
            {"record_type": "handsfree_event", "kind": "rejected", "draft_id": "D", "at": self._at(hours=6)},
            # E: posted by the server itself - never needed a reviewer.
            self._processed("E", "01000005", mode="first_response", primary=False),
            # F: the server looked at the case and skipped it. G: could not fetch it.
            self._processed("01000006__skipped", "01000006", outcome="skipped", mode="unsupported"),
            self._processed("G", "01000007", outcome="error", mode="error"),
            # H: a trial draft can never be posted.
            self._processed("H", "01000008", trial=True),
            # Z: opened and posted, but queued before tracking existed.
            self._viewed("Z", "ann", "home_dashboard", hours=1),
            {"record_type": "handsfree_listed_day",
             "drafts": {"A": {"at": self._at(hours=1)}, "C": {"at": self._at(hours=1)}}},
        ]
        posts = [
            self._post("A", "beacon_page", hours=4, changed=True),
            self._post("A", "beacon_page", hours=3.5, ok=False),      # a failed attempt is not a post
            self._post("B", "beacon_page", hours=2),
            self._post("E", "auto_post", hours=0),
            self._post("Z", "home_dashboard", hours=2),
        ]
        return posts, events

    def test_overview_counts_rates_and_turnaround(self):
        posts, events = self._fixture()
        now = (self.T0 + timedelta(days=10)).timestamp()
        f = gather.handsfree_funnel(posts, events, now=now)

        # 1. what the server did
        self.assertEqual(f["processed"]["runs"], 7)                    # H (trial) left out
        self.assertEqual(f["processed"]["distinct_cases"], 7)
        self.assertEqual(f["processed"]["by_outcome"], {"queued": 5, "skipped": 1, "error": 1})
        self.assertEqual(f["processed"]["drafts_queued"], 5)
        self.assertEqual(f["processed"]["auto_posted"], 1)
        self.assertEqual(f["processed"]["trial_drafts"], 1)
        self.assertEqual(f["processed"]["by_trigger"], {"auto-scan (scheduled)": 7})

        # 2-5. the drafts that needed a reviewer: A, B, C, D
        self.assertEqual(f["drafts_for_review"], 4)
        self.assertEqual(f["reached"], 4)                              # A, C listed; B, D opened
        self.assertEqual(f["viewed"], 3)
        self.assertEqual(f["viewed_by_via"], {"home_dashboard": 2, "beacon_page": 2})
        self.assertEqual(f["posted"], 2)
        self.assertEqual(f["posted_by_via"], {"beacon_page": 2})
        self.assertEqual((f["posted_as_suggested"], f["posted_after_edit"]), (1, 1))
        self.assertEqual((f["rejected"], f["pending"]), (1, 1))
        self.assertEqual(f["rates"], {
            "reach_rate": 1.0, "view_rate": 0.75, "click_through_rate": 0.75,
            "post_rate": 0.5, "post_rate_of_viewed": 0.667, "edit_rate": 0.5})
        self.assertEqual(f["engagement"]["views"], 4)
        self.assertEqual(f["engagement"]["distinct_reviewers"], 2)
        self.assertEqual(f["engagement"]["mean_dwell_seconds"], 90)
        self.assertEqual(f["engagement"]["edits_saved"], 1)

        # 6. turnaround, in seconds
        self.assertEqual(f["turnaround"]["processing"], {"count": 6, "median_seconds": 100, "p90_seconds": 100})
        self.assertEqual(f["turnaround"]["ready_to_first_view"],      # B 1h, A 2h, D 5h
                         {"count": 3, "median_seconds": 7200, "p90_seconds": 18000})
        self.assertEqual(f["turnaround"]["first_view_to_post"],       # B 1h, A 2h
                         {"count": 2, "median_seconds": 5400, "p90_seconds": 7200})
        self.assertEqual(f["turnaround"]["ready_to_post"],            # B 2h, A 4h
                         {"count": 2, "median_seconds": 10800, "p90_seconds": 14400})

        # 7. server work nobody has opened
        self.assertEqual(f["unseen_backlog"]["count"], 1)
        self.assertEqual(f["unseen_backlog"]["oldest"][0]["draft_id"], "C")
        self.assertEqual(f["unseen_backlog"]["oldest"][0]["age_days"], 10.0)

        self.assertEqual(f["by_week"], {"2026-W45": {"queued": 4, "viewed": 3, "posted": 2, "reviewers": 2}})
        self.assertEqual(f["untracked_drafts"], 1)                     # Z

    def test_a_view_after_the_post_did_not_lead_to_it(self):
        """A posted draft can still be opened ("show posted"): that view counts
        as a view, not as a view that turned into a post."""
        events = [self._processed("A", "01000001"),
                  self._viewed("A", "ann", "beacon_page", hours=5),      # looked at afterwards
                  self._processed("B", "01000002"),
                  self._viewed("B", "ann", "beacon_page", hours=1)]
        posts = [self._post("A", "home_dashboard", hours=2), self._post("B", "beacon_page", hours=1)]

        f = gather.handsfree_funnel(posts, events, now=(self.T0 + timedelta(days=1)).timestamp())

        self.assertEqual((f["viewed"], f["posted"], f["posted_after_view"]), (2, 2, 1))
        self.assertEqual(f["rates"]["post_rate_of_viewed"], 0.5)
        self.assertEqual(f["turnaround"]["first_view_to_post"]["count"], 1)     # B, opened and posted at 1h

    def test_a_removed_draft_is_neither_pending_nor_backlog(self):
        """Cleanup and a case reset take drafts out of every queue. They stay
        in what the server produced and what reviewers were shown, but nobody
        can open them any more."""
        def removed(draft_id, reason, hours):
            return {"record_type": "handsfree_event", "kind": "removed", "draft_id": draft_id,
                    "reason": reason, "at": self._at(hours=hours)}
        events = [
            self._processed("A", "01000001"), removed("A", "case_closed", 30),          # never opened
            self._processed("B", "01000002"), self._viewed("B", "ann", "beacon_page", hours=1),
            removed("B", "case_reset", 2),                                               # opened, then reset
            self._processed("C", "01000003"), self._viewed("C", "ann", "beacon_page", hours=1),
            removed("C", "retention", 800),                                              # posted first: posted
            self._processed("D", "01000004"),                                            # still waiting
            {"record_type": "handsfree_listed_day", "drafts": {"A": {"at": self._at(hours=1)}}},
        ]
        posts = [self._post("C", "beacon_page", hours=2)]

        f = gather.handsfree_funnel(posts, events, now=(self.T0 + timedelta(days=40)).timestamp())

        self.assertEqual((f["drafts_for_review"], f["reached"], f["viewed"]), (4, 3, 2))
        self.assertEqual((f["posted"], f["rejected"], f["removed"], f["pending"]), (1, 0, 2, 1))
        self.assertEqual(f["removed_by_reason"], {"case_closed": 1, "case_reset": 1})
        self.assertEqual([row["draft_id"] for row in f["unseen_backlog"]["oldest"]], ["D"])

    def test_a_fresh_unopened_draft_is_not_backlog_yet(self):
        posts, events = self._fixture()
        f = gather.handsfree_funnel(posts, events, now=(self.T0 + timedelta(days=1)).timestamp())
        self.assertEqual(f["unseen_backlog"]["count"], 0)
        self.assertEqual(f["pending"], 1)

    def test_empty_input_has_no_rates_instead_of_dividing_by_zero(self):
        f = gather.handsfree_funnel([], [])
        self.assertEqual(f["drafts_for_review"], 0)
        self.assertTrue(all(v is None for v in f["rates"].values()))
        self.assertEqual(f["turnaround"]["ready_to_post"]["count"], 0)
        # Odd records are skipped, never raised on.
        gather.handsfree_funnel([None, {"ok": True}], [None, {"kind": "viewed"}, {"record_type": "handsfree_listed_day"}])


class TestEditSummaryBounds(unittest.TestCase):
    """summarise_edit keeps its work bounded on long or repetitive texts and
    says how it compared them."""

    def test_a_small_change_in_a_long_repetitive_text_is_still_compared_exactly(self):
        lines = ["Please collect the WRT logs."] * 1500
        edited = lines[:700] + ["Please also share the AP model."] + lines[701:]

        e = gather.summarise_edit("\n".join(lines), "\n".join(edited))

        self.assertEqual(e["comparison"], "exact")          # only the changed part is diffed
        self.assertEqual((e["removed_lines"], e["added_lines"]),
                         (["Please collect the WRT logs."], ["Please also share the AP model."]))
        self.assertEqual((e["suggested_lines"], e["posted_lines"], e["kept_lines"]), (1500, 1500, 1499))
        self.assertIs(e["changed"], True)
        self.assertEqual(e["similarity"], 0.999)

    def test_a_large_changed_part_is_compared_approximately(self):
        a = "\n".join(f"old line {i}" for i in range(6)) + "\nshared tail"
        b = "\n".join(f"new line {i}" for i in range(6)) + "\nshared tail"

        with patch.object(gather, "_DIFF_EXACT_CELLS", 10):
            e = gather.summarise_edit(a, b)

        self.assertEqual(e["comparison"], "approximate")
        self.assertEqual((e["kept_lines"], e["removed_count"], e["added_count"]), (1, 6, 6))

    def test_lines_past_the_limit_are_checked_for_being_the_same_only(self):
        with patch.object(gather, "_MAX_DIFF_LINES", 5):
            same_start = "\n".join(f"line {i}" for i in range(5))
            e = gather.summarise_edit(same_start + "\nextra one", same_start + "\nextra two")
            unchanged = gather.summarise_edit(same_start + "\nextra", same_start + "\nextra")

        self.assertEqual(e["comparison"], "truncated")
        self.assertIs(e["changed"], True)                   # the difference lies past the limit
        self.assertEqual((e["suggested_lines"], e["posted_lines"], e["kept_lines"]), (6, 6, 5))
        self.assertEqual(e["removed_lines"], [])
        self.assertIs(unchanged["changed"], False)

    def test_equal_and_empty_texts(self):
        self.assertEqual(gather.summarise_edit("a\nb", "a\nb")["similarity"], 1.0)
        self.assertIs(gather.summarise_edit("", "")["changed"], False)
        self.assertEqual(gather.summarise_edit(None, "new")["added_lines"], ["new"])


@patch(f"{ROUTES}.orchestrator")
@patch(f"{ROUTES}._signed_in_name", return_value="Ann Reviewer")
@patch(f"{ROUTES}._store")
class TestHandsfreeViewRoute(BaseTestCase):
    blueprint = handsfree_bp

    def test_viewed_records_the_page_the_view_id_and_the_duration(self, mock_store, _name, mock_orch):
        mock_store.return_value.get.return_value = {
            "draft_id": "d-1", "case_nbr": "01090001", "status": "pending_review", "mode": "full"}

        resp = self.client.post("/handsfree/queue/d-1/viewed",
                                json={"view_id": "v-1", "via": "home_dashboard", "dwell_seconds": 30})

        self.assertEqual(resp.get_json(), {"ok": True})
        mock_orch._track_event.assert_called_once_with(
            "viewed", draft_id="d-1", case_nbr="01090001", via="home_dashboard", key="v-1",
            draft_status="pending_review", mode="full", reviewer_name="Ann Reviewer", dwell_seconds=30)

    def test_viewed_takes_the_page_from_the_referer_when_the_body_does_not_say(self, mock_store, _name, mock_orch):
        mock_store.return_value.get.return_value = {"draft_id": "d-1", "case_nbr": "01090001"}

        self.client.post("/handsfree/queue/d-1/viewed", json={"view_id": "v-2"},
                         headers={"Referer": "http://localhost:5000/handsfree/"})

        kwargs = mock_orch._track_event.call_args.kwargs
        self.assertEqual(kwargs["via"], "beacon_page")
        self.assertNotIn("dwell_seconds", kwargs)

    def test_viewed_of_an_unknown_draft_is_404_and_records_nothing(self, mock_store, _name, mock_orch):
        mock_store.return_value.get.return_value = None

        resp = self.client.post("/handsfree/queue/nope/viewed", json={})

        self.assertEqual(resp.status_code, 404)
        mock_orch._track_event.assert_not_called()

    def test_a_closing_report_for_a_draft_that_left_the_queue_completes_its_view(self, mock_store, _name, mock_orch):
        """A case reset archives the draft while it is open; the page's
        closing report must still deliver the time it was open."""
        mock_store.return_value.get.return_value = None

        resp = self.client.post("/handsfree/queue/d-1/viewed",
                                json={"view_id": "v-1", "via": "beacon_page", "dwell_seconds": 75})

        self.assertEqual(resp.get_json(), {"ok": True})
        mock_orch._track_event.assert_called_once_with(
            "viewed", draft_id="d-1", key="v-1", update_only=True, dwell_seconds=75)

    def test_an_opening_report_for_an_unknown_draft_is_still_refused(self, mock_store, _name, mock_orch):
        mock_store.return_value.get.return_value = None

        resp = self.client.post("/handsfree/queue/d-1/viewed", json={"view_id": "v-1"})

        self.assertEqual(resp.status_code, 404)
        mock_orch._track_event.assert_not_called()

    def test_a_case_reset_reports_the_drafts_it_archived(self, mock_store, _name, mock_orch):
        mock_store.return_value.reset_case.return_value = {
            "case_nbr": "01090001", "archived": ["01090001__a", "01090001__skipped"],
            "kept_posted": ["01090001__p"], "archive_dir": "x", "ledger_kept": {}, "purged": []}

        with patch("services.handsfree.bsod.forget_case", return_value=0):
            resp = self.client.post("/handsfree/case/01090001/reset", json={})

        self.assertIs(resp.get_json()["ok"], True)
        mock_orch._track_removed.assert_called_once_with(
            [{"draft_id": "01090001__a", "case_nbr": "01090001"},
             {"draft_id": "01090001__skipped", "case_nbr": "01090001"}], reason="case_reset")

    def test_listing_the_own_queue_reports_open_drafts_only(self, mock_store, _name, mock_orch):
        items = [{"draft_id": "d-1", "status": "pending_review"},
                 {"draft_id": "d-2", "status": "posted"},
                 {"draft_id": "d-3", "status": "pending_review", "trial": True},
                 {"draft_id": "0109__skipped", "status": "skipped"}]
        mock_store.return_value.list_drafts.return_value = items

        with patch(f"{ROUTES}._own_mailbox", return_value=""):
            self.client.get("/handsfree/queue?all=1", headers={"Referer": "http://localhost:5000/"})
            self.client.get("/handsfree/queue?everyone=1")          # browsing others' drafts is not an impression

        mock_orch._track_listed.assert_called_once_with([items[0]], via="home_dashboard")


class _FakeIps:
    """Stands in for IPS: `outcome` is what the next REST post does."""
    FIELD_RICH_BODY = IpsClient.FIELD_RICH_BODY
    outcome = "ok"
    closed_cases: set = set()

    def post_comment(self, case_id, rich_body, **kw):
        if _FakeIps.outcome == "unsupported":
            raise PostUnsupported("no verified field map for a public reply")
        if _FakeIps.outcome == "timeout":
            raise TimeoutError("read timed out")
        if _FakeIps.outcome == "http500":
            return PostResult(ok=False, backend="rest", error="HTTP 500: boom", detail={"status": 500})
        return PostResult(ok=True, backend="rest", comment_id="a0X0001", detail={})

    def get_case_comments(self, case_id):
        return []

    def comment_author(self, comment_id):
        return {}

    def _fill_case_states(self, refs):
        for ref in refs:
            ref.closed = ref.case_nbr in _FakeIps.closed_cases


class TestHandsfreeTrackingCallers(unittest.TestCase):
    """What the orchestrator reports, and when. The queue is a real store in a
    temp folder; IPS is a fake; nothing reaches the Gather share (collection
    is off unless a test stands in for the writer)."""

    BODY = AI_MARKER + "\n\nHello,\n\nPlease collect WRT logs.\n\nThank you\n"
    ECHO_BODY = (AI_MARKER + "\n\nAnalysis: the AP rejected the association.\n\n"
                 + ECHO_SECTION_START + "\nINTERNAL: firmware bug HSD-123\n" + ECHO_SECTION_END + "\n")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="handsfree_callers_"))
        self.store = HandsfreeStore(self.tmp / "handsfree")
        self.store.save_config({"dry_run": False, "post_backend": "auto"})
        _FakeIps.outcome, _FakeIps.closed_cases = "ok", set()
        self._patches = [
            patch.object(orch, "_store", return_value=self.store),
            patch.object(orch, "IpsClient", _FakeIps),
            patch.object(orch, "_signed_in_identity",
                         return_value={"user_id": "005A", "name": "Ann Reviewer", "email": "a@x"}),
            patch.object(gather, "_collection_enabled", return_value=False),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _draft(self, case_nbr, body=None, **extra):
        body = self.BODY if body is None else body
        return self.store.enqueue(case_nbr=case_nbr, case_id="500" + case_nbr, subject="s",
                                  draft_plain=body, draft_html=compose_html(body), confidence=60,
                                  mode="full", analysis={"first_response_done": True}, **extra)

    # ------------------------------------------------------------ failed posts
    def test_a_public_post_refused_or_failed_by_rest_is_recorded_as_a_failed_post(self):
        for nbr, outcome in (("01100010", "unsupported"), ("01100011", "http500")):
            with self.subTest(outcome=outcome):
                _FakeIps.outcome = outcome
                draft = self._draft(nbr)
                with patch.object(orch, "_track_post") as track:
                    res = orch.approve_and_post(draft["draft_id"], visibility="external")

                self.assertIs(res["ok"], False)
                track.assert_called_once()
                self.assertIs(track.call_args.kwargs["ok"], False)
                self.assertEqual(track.call_args.kwargs["rec"]["status"], "post_failed")
                self.assertEqual(track.call_args.kwargs["rec"]["post_result"]["backend"], "rest")

    def test_a_rest_transport_error_is_recorded_as_a_failed_post_and_still_raised(self):
        """A timeout may have gone through: no retry, the error surfaces as
        before, the draft is left as it was - but the attempt is on record."""
        _FakeIps.outcome = "timeout"
        draft = self._draft("01100012")

        with patch.object(orch, "_track_post") as track:
            with self.assertRaises(TimeoutError):
                orch.approve_and_post(draft["draft_id"], visibility="internal")

        track.assert_called_once()
        self.assertIs(track.call_args.kwargs["ok"], False)
        self.assertEqual(track.call_args.kwargs["rec"]["post_result"],
                         {"ok": False, "backend": "rest", "error": "TimeoutError: read timed out"})
        after = self.store.get(draft["draft_id"])
        self.assertEqual((after["status"], after["post_result"]), ("pending_review", None))

    def test_a_draft_refused_as_a_second_first_response_leaves_the_queue_in_the_usage_numbers(self):
        """The system marks it posted / rejected without posting anything:
        not a post, not a reviewer's rejection, and no longer pending."""
        self.store.mark_posted("01100013", comment_id="a0X0009", first_response=True)

        def queued(mode):
            return self.store.enqueue(case_nbr="01100013", case_id="50001100013", subject="s",
                                      draft_plain=self.BODY, draft_html=compose_html(self.BODY),
                                      confidence=None, mode=mode, analysis={"first_response_done": False})

        for mode, status, reason in (("first_response", "posted", "first_response_already_sent"),
                                     ("request_logs", "rejected", "overview_already_sent")):
            with self.subTest(mode=mode):
                draft = queued(mode)
                with patch.object(orch, "_track_event") as event, patch.object(orch, "_track_post") as post:
                    res = orch.approve_and_post(draft["draft_id"], visibility="external", via="beacon_page")

                self.assertIs(res["ok"], False)
                self.assertEqual(self.store.get(draft["draft_id"])["status"], status)
                post.assert_not_called()                                 # nothing reached a backend
                event.assert_called_once()
                self.assertEqual(event.call_args.args, ("removed",))
                self.assertEqual((event.call_args.kwargs["draft_id"], event.call_args.kwargs["reason"],
                                  event.call_args.kwargs["via"]),
                                 (draft["draft_id"], reason, "beacon_page"))

    # --------------------------------------------------------------- _track_post
    def test_track_post_uses_the_draft_it_is_given_and_does_nothing_when_collection_is_off(self):
        store = MagicMock()
        rec = {"draft_id": "d-1", "case_nbr": "01090001", "draft_plain": "text",
               "post_result": {"ok": True, "backend": "rest"}}

        with patch.object(gather, "record_handsfree_post") as record:
            orch._track_post(store, "d-1", ok=True)                      # collection is off
            record.assert_not_called()
            store.get.assert_not_called()

            with patch.object(gather, "_collection_enabled", return_value=True):
                orch._track_post(store, "d-1", ok=True, rec=rec)
            store.get.assert_not_called()                                # no second read of the share
            self.assertEqual(record.call_args.kwargs["posted_plain"], "text")
            self.assertIs(record.call_args.kwargs["echo_stripped"], False)
            self.assertIsNone(record.call_args.kwargs["edit_baseline_plain"])

    def test_a_retry_after_a_failed_public_post_still_knows_the_system_removed_the_echo_section(self):
        draft = self._draft("01100020", body=self.ECHO_BODY)
        with patch.object(gather, "_collection_enabled", return_value=True), \
                patch.object(gather, "record_handsfree_post") as record:
            _FakeIps.outcome = "http500"
            first = orch.approve_and_post(draft["draft_id"], visibility="external")
            _FakeIps.outcome = "ok"
            second = orch.approve_and_post(draft["draft_id"], visibility="external")   # no further edit

        self.assertEqual((first["ok"], second["ok"]), (False, True))
        self.assertIs(self.store.get(draft["draft_id"])["echo_stripped_by_system"], True)
        failed, posted = (call.kwargs for call in record.call_args_list)
        self.assertEqual((failed["ok"], posted["ok"]), (False, True))
        for kwargs in (failed, posted):
            self.assertIs(kwargs["echo_stripped"], True)
            self.assertIn("HSD-123", kwargs["suggested_plain"])
            self.assertNotIn("HSD-123", kwargs["posted_plain"])
        content = gather._post_content(**{k: posted[k] for k in (
            "suggested_plain", "suggested_source", "posted_plain", "echo_stripped", "edit_baseline_plain")})
        self.assertEqual(content["edit"]["baseline"], "suggested_without_echo")
        self.assertIs(content["edit"]["changed"], False)               # not the reviewer's edit

    def test_text_that_goes_out_with_the_echo_section_was_not_stripped(self):
        """After a failed public post the reviewer puts the section back and
        posts inside Intel: the section went out, so nothing was removed."""
        rec = {"draft_id": "d-1", "case_nbr": "01090001", "draft_plain": self.ECHO_BODY,
               "suggested_plain": self.ECHO_BODY, "echo_stripped_by_system": True,
               "post_result": {"ok": True, "backend": "rest"}}
        with patch.object(gather, "_collection_enabled", return_value=True), \
                patch.object(gather, "record_handsfree_post") as record:
            orch._track_post(MagicMock(), "d-1", ok=True, rec=rec)

        self.assertIs(record.call_args.kwargs["echo_stripped"], False)
        self.assertIsNone(record.call_args.kwargs["edit_baseline_plain"])

    # ------------------------------------------------------------ skipped cases
    def test_a_case_skipped_during_a_trial_run_is_reported_as_trial(self):
        analysis = SimpleNamespace(mode="unsupported", error="BT case", case_id="500X", subject="s",
                                   to_dict=lambda: {})
        runner = MagicMock()
        runner.return_value.analyze_case.return_value = analysis
        for label, trial_arg, config_trial, expected in (
                ("the run's captured flag", True, False, True),
                ("a manual run reads the setting", None, True, True),
                ("a normal run", None, False, False)):
            with self.subTest(label):
                self.store.save_config({"trial_mode": config_trial})
                with patch.object(orch, "HandsfreeRunner", runner), \
                        patch.object(orch, "_case_owner", return_value=""), \
                        patch.object(orch, "_track_event") as track:
                    self.assertIsNone(orch._analyze_and_enqueue(self.store, "01100030", trial=trial_arg))

                track.assert_called_once()
                self.assertEqual(track.call_args.args, ("processed",))
                self.assertEqual(track.call_args.kwargs["outcome"], "skipped")
                self.assertIs(track.call_args.kwargs["trial"], expected)
                # One record per RUN, not per case: the queue's single
                # "<case>__skipped" row id would let a later run overwrite it.
                self.assertRegex(track.call_args.kwargs["draft_id"], r"^01100030__skipped_\d{8}T\d{6}$")

    # ----------------------------------------------------------------- removals
    def test_cleanup_reports_the_open_drafts_it_removes_with_the_reason(self):
        closed_open = self._draft("01100040")
        closed_posted = self._draft("01100040")
        self.store.update(closed_posted["draft_id"], status="posted")
        old = self._draft("01100041")
        path = self.store.queue_dir / f"{old['draft_id']}.json"
        aged = json.loads(path.read_text(encoding="utf-8"))
        aged["created_at"] = (datetime.now().astimezone() - timedelta(days=60)).isoformat(timespec="seconds")
        path.write_text(json.dumps(aged), encoding="utf-8")
        fresh = self._draft("01100042")
        _FakeIps.closed_cases = {"01100040"}

        with patch.object(orch, "_track_event") as track:
            res = orch.cleanup_queue(self.store)

        self.assertEqual(res["drafts_removed"], 3)
        self.assertIsNotNone(self.store.get(fresh["draft_id"]))
        reported = {call.kwargs["draft_id"]: (call.args[0], call.kwargs["reason"])
                    for call in track.call_args_list}
        self.assertEqual(reported, {closed_open["draft_id"]: ("removed", "case_closed"),
                                    old["draft_id"]: ("removed", "retention")})

    def test_track_removed_leaves_out_rows_that_were_never_an_open_draft(self):
        rows = [{"draft_id": "01100050__a", "case_nbr": "01100050"},
                {"draft_id": "01100050__skipped", "case_nbr": "01100050"},
                {"draft_id": "01100050__r", "case_nbr": "01100050", "status": "rejected"},
                {"case_nbr": "01100050"}]
        with patch.object(orch, "_track_event") as track:
            orch._track_removed(rows, reason="case_reset")

        track.assert_called_once()
        self.assertEqual(track.call_args.kwargs["draft_id"], "01100050__a")
        self.assertEqual(track.call_args.kwargs["reason"], "case_reset")


if __name__ == "__main__":
    unittest.main()
