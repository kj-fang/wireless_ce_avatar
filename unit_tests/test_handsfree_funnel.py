"""Unit tests for the Beacon (Handsfree) review funnel in
``services.gather_service``: the events written before a post
(``record_handsfree_event`` / ``record_handsfree_listed``), the rollup that
turns them into the overview (``handsfree_funnel``), and the route the pages
report a view through (``POST /handsfree/queue/<draft_id>/viewed``).

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
from unittest.mock import patch

from blueprints.handsfree.handsfree_routes import handsfree_bp
from services import gather_service as gather
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


if __name__ == "__main__":
    unittest.main()
