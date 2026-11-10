"""Unit tests for the handsfree blueprint
(blueprints/handsfree/handsfree_routes.py).

The routes are a thin JSON layer over ``services.handsfree.orchestrator`` and
the ``HandsfreeStore`` draft queue; both are patched at point of use so no
background run starts and nothing is written anywhere.

The store is the SHARED network folder that the Beacon server and every
client read live (``services.handsfree.queue.open_store``), and the routes
reach it through ``handsfree_routes._store``. That function is what these
tests patch. A test that forgot to would change the team's real settings and
queue, so ``BaseTestCase`` also makes the shared folder unreachable for every
test (see ``TestHandsfreeShareIsNeverOpened``).

Branch-specific: this branch wires the IPS "Environment Details" form
(``CaseContext.env_detail``) into the handsfree reader path (commit 5c1658d9).
That logic lives in the service layer, not in the routes, so
``TestHandsfreeEnvDetail`` exercises the pure helpers the flow relies on:
``case_reader._format_env_detail`` (feeds ``{env_block}`` of the reader
prompt) and ``runner._env_repro_steps`` / ``runner._env_brief`` (retract the
"repro steps missing" gap and build the agent's environment brief).
"""
import unittest
from unittest.mock import Mock, patch

from blueprints.handsfree.handsfree_routes import handsfree_bp
from services.handsfree.case_reader import READER_PROMPT, _format_env_detail
from services.handsfree.composer import AI_MARKER
from services.handsfree.queue import HandsfreeShareUnavailable, HandsfreeStore
from services.handsfree.runner import _env_brief, _env_repro_steps
from unit_tests.base_test import BaseTestCase

ROUTES = "blueprints.handsfree.handsfree_routes"
SCHEDULER = "services.handsfree.scheduler"


class TestHandsfreeBlueprintStructure(BaseTestCase):
    blueprint = handsfree_bp

    def test_blueprint_registered_with_url_prefix(self):
        """Verify blueprint registration and the full route set.

        Input: the Flask app with ``handsfree_bp`` registered (via setUp).
        Expected: the blueprint is mounted under ``/handsfree`` and exposes all
        expected routes; the queue draft route supports GET/PUT/POST and the
        approve route is POST-only.
        """
        # Arrange / Act
        rules = {}
        for r in self.app.url_map.iter_rules():
            if r.endpoint.startswith("handsfree."):
                rules.setdefault(r.rule, set()).update(r.methods)

        # Assert
        self.assertIs(self.app.blueprints.get("handsfree"), handsfree_bp)
        self.assertEqual(handsfree_bp.url_prefix, "/handsfree")
        self.assertEqual(set(rules), {
            "/handsfree/", "/handsfree/check_now", "/handsfree/run_case", "/handsfree/status",
            "/handsfree/queue", "/handsfree/queue/<draft_id>", "/handsfree/queue/<draft_id>/approve",
            "/handsfree/queue/<draft_id>/reject", "/handsfree/config", "/handsfree/discover_fields",
            "/handsfree/auto", "/handsfree/auto/run_now", "/handsfree/list_views",
            "/handsfree/queue/<draft_id>/open_log_folder",
            # server / client roles, reviewer sign-in, email intake, hand-off
            "/handsfree/login", "/handsfree/requests", "/handsfree/latest_driver/refresh",
            "/handsfree/email/run_case", "/handsfree/email/check_now",
            "/handsfree/case/<case_nbr>/reset", "/handsfree/chatbot_handoff",
        })
        self.assertTrue({"GET", "PUT", "POST"}.issubset(rules["/handsfree/queue/<draft_id>"]))
        self.assertNotIn("GET", rules["/handsfree/queue/<draft_id>/approve"])
        self.assertNotIn("GET", rules["/handsfree/latest_driver/refresh"])
        self.assertNotIn("GET", rules["/handsfree/case/<case_nbr>/reset"])


class TestHandsfreeShareIsNeverOpened(BaseTestCase):
    """The safety net under every other class in this module."""

    blueprint = handsfree_bp

    def test_shared_folder_lookup_is_replaced_by_the_guard(self):
        """The function that resolves the shared folder is the guard, not the real one.

        Input: services.handsfree.queue.handsfree_root inside a test.
        Expected: it is BaseTestCase's mock and raises HandsfreeShareUnavailable,
        so open_store() (which every caller goes through) raises too.
        """
        from services.handsfree import queue

        # Assert: checked BEFORE anything is called, so a missing guard fails
        # here instead of opening the share.
        self.assertIsInstance(queue.handsfree_root, Mock)
        with self.assertRaises(HandsfreeShareUnavailable):
            queue.open_store()

    def test_unpatched_store_is_refused_not_opened(self):
        """A route whose store is NOT patched cannot reach the shared folder.

        Input: GET /handsfree/config with nothing patched (a read, so even a
        missing guard could not change anything).
        Expected: 503 with the share-unavailable error from BaseTestCase's
        guard.
        """
        # Act
        resp = self.client.get("/handsfree/config")

        # Assert
        self.assertEqual(resp.status_code, 503)
        self.assertFalse(resp.get_json()["ok"])
        self.assertIn("unit tests never open", resp.get_json()["error"])


@patch(f"{ROUTES}._store")
class TestHandsfreeIndexAndConfig(BaseTestCase):
    blueprint = handsfree_bp

    @patch(f"{SCHEDULER}.role", return_value="client")
    @patch(f"{ROUTES}.render_template", return_value="handsfree-page")
    def test_index_renders_with_store_config(self, mock_render, _mock_role, mock_store_cls):
        """The index page renders with the store's config.

        Input: GET /handsfree/ where the store loads a config dict.
        Expected: 200; the shared store is opened once and handsfree.html is
        rendered with the loaded config, no share error and this PC's role.
        """
        # Arrange
        mock_store_cls.return_value.load_config.return_value = {"owner_name": "Tester", "dry_run": True}

        # Act
        resp = self.client.get("/handsfree/")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(as_text=True), "handsfree-page")
        mock_store_cls.assert_called_once_with()
        mock_render.assert_called_once_with(
            "handsfree.html", config={"owner_name": "Tester", "dry_run": True},
            share_error="", role="client")

    @patch(f"{SCHEDULER}.role", return_value="server")
    @patch(f"{ROUTES}.render_template", return_value="handsfree-page")
    def test_index_renders_defaults_when_share_is_unavailable(self, mock_render, _mock_role, mock_store_cls):
        """The index page still renders when the shared folder is down.

        Input: GET /handsfree/ where opening the store raises
        HandsfreeShareUnavailable.
        Expected: 200; the page gets the default config and the error text.
        """
        # Arrange
        mock_store_cls.side_effect = HandsfreeShareUnavailable("share is down")

        # Act
        resp = self.client.get("/handsfree/")

        # Assert
        self.assertEqual(resp.status_code, 200)
        mock_render.assert_called_once_with(
            "handsfree.html", config=dict(HandsfreeStore.DEFAULT_CONFIG),
            share_error="share is down", role="server")

    def test_get_config_returns_store_config(self, mock_store_cls):
        """GET /config returns the stored config.

        Input: GET /handsfree/config.
        Expected: 200 with the store config; save_config is not called.
        """
        # Arrange
        mock_store_cls.return_value.load_config.return_value = {"max_cases_per_run": 3}

        # Act
        resp = self.client.get("/handsfree/config")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"max_cases_per_run": 3})
        mock_store_cls.return_value.save_config.assert_not_called()

    def test_post_config_saves_only_allowed_keys(self, mock_store_cls):
        """POST /config persists only allow-listed keys.

        Input: POST /handsfree/config with allowed keys plus extra/unknown keys.
        Expected: 200; save_config is called with only the allowed subset
        (unknown keys dropped).
        """
        # Arrange
        store = mock_store_cls.return_value
        store.save_config.return_value = {"owner_name": "Tester", "dry_run": True}

        # Act
        resp = self.client.post("/handsfree/config", json={
            "owner_name": "Tester", "dry_run": True, "auto_post": True, "evil": "x"})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"owner_name": "Tester", "dry_run": True})
        store.save_config.assert_called_once_with({"owner_name": "Tester", "dry_run": True})

    def test_post_config_without_json_saves_empty_update(self, mock_store_cls):
        """POST /config with a non-JSON body saves an empty update.

        Input: POST /handsfree/config with a plain-text body.
        Expected: 200; save_config is called with {}.
        """
        # Arrange
        mock_store_cls.return_value.save_config.return_value = {}

        # Act
        resp = self.client.post("/handsfree/config", data="not json")

        # Assert
        self.assertEqual(resp.status_code, 200)
        mock_store_cls.return_value.save_config.assert_called_once_with({})

    def test_post_config_normalises_the_latest_driver_override(self, mock_store_cls):
        """The latest-driver override is stored as a full x.y.z.w, or cleared.

        Input: POST /handsfree/config with latest_driver_manual " 24.90.0.3 ",
        then with "".
        Expected: save_config gets "24.90.0.3", then "" (use the report).
        """
        # Arrange
        store = mock_store_cls.return_value
        store.save_config.return_value = {}

        # Act
        self.client.post("/handsfree/config", json={"latest_driver_manual": " 24.90.0.3 "})
        self.client.post("/handsfree/config", json={"latest_driver_manual": ""})

        # Assert
        self.assertEqual([c.args[0] for c in store.save_config.call_args_list],
                         [{"latest_driver_manual": "24.90.0.3"}, {"latest_driver_manual": ""}])

    def test_post_config_rejects_a_partial_latest_driver(self, mock_store_cls):
        """A branch-only override is refused and nothing is saved.

        Input: POST /handsfree/config with latest_driver_manual "24.90".
        Expected: 400 naming the expected form; save_config is not called.
        """
        # Act
        resp = self.client.post("/handsfree/config", json={"latest_driver_manual": "24.90"})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()["ok"])
        self.assertIn("24.90.0.3", resp.get_json()["error"])
        mock_store_cls.return_value.save_config.assert_not_called()

    @patch(f"{SCHEDULER}.status", return_value={"enabled": True})
    @patch(f"{SCHEDULER}.ensure_started")
    @patch(f"{SCHEDULER}.can_schedule_here", return_value=False)
    def test_latest_driver_refresh_leaves_a_request_for_the_server(
            self, _mock_can, mock_ensure, _mock_status, mock_store_cls):
        """"Refresh now" on a client only stamps the shared config.

        Input: POST /handsfree/latest_driver/refresh on a client PC.
        Expected: 200 {ok, **scheduler.status()}; the request time is saved
        and no scheduler is started here.
        """
        # Act
        resp = self.client.post("/handsfree/latest_driver/refresh")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"ok": True, "enabled": True})
        (updates,), _ = mock_store_cls.return_value.save_config.call_args
        self.assertEqual(list(updates), ["latest_driver_refresh_requested_at"])
        self.assertTrue(updates["latest_driver_refresh_requested_at"])
        mock_ensure.assert_not_called()

    @patch(f"{SCHEDULER}.role", return_value="client")
    def test_requests_lists_the_share_requests(self, _mock_role, mock_store_cls):
        """GET /requests returns the case requests and this PC's role.

        Input: GET /handsfree/requests where the store lists one request.
        Expected: 200 with {items, role}.
        """
        # Arrange
        mock_store_cls.return_value.list_requests.return_value = [{"case_nbr": "00123456", "status": "pending"}]

        # Act
        resp = self.client.get("/handsfree/requests")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {
            "items": [{"case_nbr": "00123456", "status": "pending"}], "role": "client"})


@patch(f"{ROUTES}.orchestrator")
class TestHandsfreeRunRoutes(BaseTestCase):
    blueprint = handsfree_bp

    def test_check_now_passes_owner_name(self, mock_orch):
        """check_now forwards the owner name to the orchestrator.

        Input: POST /handsfree/check_now with an owner_name.
        Expected: 200 echoing the orchestrator result; start_check_now is called
        with the owner name.
        """
        # Arrange
        mock_orch.start_check_now.return_value = {"ok": True, "started": True}

        # Act
        resp = self.client.post("/handsfree/check_now", json={"owner_name": "Tester"})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"ok": True, "started": True})
        mock_orch.start_check_now.assert_called_once_with("Tester")

    def test_check_now_without_body_passes_none(self, mock_orch):
        """check_now passes None when no owner name is given.

        Input: POST /handsfree/check_now with no body.
        Expected: start_check_now is called with None and its error result is
        returned.
        """
        # Arrange
        mock_orch.start_check_now.return_value = {"ok": False, "error": "owner_name required"}

        # Act
        resp = self.client.post("/handsfree/check_now")

        # Assert
        self.assertEqual(resp.get_json(), {"ok": False, "error": "owner_name required"})
        mock_orch.start_check_now.assert_called_once_with(None)

    @patch(f"{SCHEDULER}.can_schedule_here", return_value=True)
    def test_run_case_passes_case_nbr(self, _mock_can, mock_orch):
        """On the server, run_case forwards the case number to the orchestrator.

        Input: POST /handsfree/run_case with a case_nbr on the server role.
        Expected: 200 echoing the result; start_case_run is called with the case
        number.
        """
        # Arrange
        mock_orch.start_case_run.return_value = {"ok": True, "case_nbr": "00123456"}

        # Act
        resp = self.client.post("/handsfree/run_case", json={"case_nbr": "00123456"})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"ok": True, "case_nbr": "00123456"})
        mock_orch.start_case_run.assert_called_once_with("00123456")

    @patch(f"{SCHEDULER}.can_schedule_here", return_value=True)
    def test_run_case_without_body_passes_none(self, _mock_can, mock_orch):
        """On the server, run_case passes None when no case number is given.

        Input: POST /handsfree/run_case with no body on the server role.
        Expected: start_case_run is called with None and its error result is
        returned.
        """
        # Arrange
        mock_orch.start_case_run.return_value = {"ok": False, "error": "case_nbr required"}

        # Act
        resp = self.client.post("/handsfree/run_case")

        # Assert
        self.assertEqual(resp.get_json()["ok"], False)
        mock_orch.start_case_run.assert_called_once_with(None)

    @patch(f"{ROUTES}._signed_in_name", return_value="Reviewer One")
    @patch(f"{ROUTES}._store")
    @patch(f"{SCHEDULER}.can_schedule_here", return_value=False)
    def test_run_case_on_a_client_queues_a_request_for_the_server(
            self, _mock_can, mock_store, _mock_name, mock_orch):
        """On a client, run_case becomes a request on the share.

        Input: POST /handsfree/run_case with a seven-digit case number on the
        client role, signed in as "Reviewer One".
        Expected: 200 {ok, requested, request}; the store queues the
        normalised eight-digit number under the reviewer's name and nothing
        runs on this PC.
        """
        # Arrange
        req = {"case_nbr": "01009610", "status": "pending"}
        mock_store.return_value.request_case.return_value = req

        # Act
        resp = self.client.post("/handsfree/run_case", json={"case_nbr": "1009610"})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"ok": True, "requested": True, "request": req})
        mock_store.return_value.request_case.assert_called_once_with("01009610", requested_by="Reviewer One")
        mock_orch.start_case_run.assert_not_called()

    @patch(f"{ROUTES}._store")
    @patch(f"{SCHEDULER}.can_schedule_here", return_value=False)
    def test_run_case_on_a_client_refuses_a_bad_case_number(self, _mock_can, mock_store, mock_orch):
        """On a client, a bad or empty case number queues nothing.

        Input: POST /handsfree/run_case with "abc", then with no body, on the
        client role.
        Expected: {ok: False} naming the problem; the store is never opened.
        """
        # Act
        bad = self.client.post("/handsfree/run_case", json={"case_nbr": "abc"})
        empty = self.client.post("/handsfree/run_case")

        # Assert
        self.assertFalse(bad.get_json()["ok"])
        self.assertIn("not an IPS case number", bad.get_json()["error"])
        self.assertEqual(empty.get_json(), {"ok": False, "error": "empty case number"})
        mock_store.assert_not_called()
        mock_orch.start_case_run.assert_not_called()

    def test_status_returns_run_state(self, mock_orch):
        """status returns the orchestrator run state.

        Input: GET /handsfree/status.
        Expected: 200 with the run-state dict from get_run_state.
        """
        # Arrange
        mock_orch.get_run_state.return_value = {"running": False, "events": []}

        # Act
        resp = self.client.get("/handsfree/status")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"running": False, "events": []})

    def test_approve_passes_edited_plain(self, mock_orch):
        """approve forwards the edited draft text.

        Input: POST /handsfree/queue/d-1/approve with draft_plain.
        Expected: approve_and_post is called with the draft id and edited_plain,
        and no visibility (the orchestrator then posts Internal to Intel); its
        result is returned.
        """
        # Arrange
        mock_orch.approve_and_post.return_value = {"ok": True, "posted": True}

        # Act
        resp = self.client.post("/handsfree/queue/d-1/approve", json={"draft_plain": "edited text"})

        # Assert
        self.assertEqual(resp.get_json(), {"ok": True, "posted": True})
        mock_orch.approve_and_post.assert_called_once_with("d-1", edited_plain="edited text", visibility=None)

    def test_approve_passes_the_reviewers_visibility(self, mock_orch):
        """approve forwards who the reviewer chose to show the post to.

        Input: POST /handsfree/queue/d-1/approve with visibility "external".
        Expected: approve_and_post is called with visibility="external".
        """
        # Arrange
        mock_orch.approve_and_post.return_value = {"ok": True}

        # Act
        self.client.post("/handsfree/queue/d-1/approve",
                         json={"draft_plain": "edited text", "visibility": "external"})

        # Assert
        mock_orch.approve_and_post.assert_called_once_with(
            "d-1", edited_plain="edited text", visibility="external")

    def test_approve_without_body_passes_none(self, mock_orch):
        """approve passes edited_plain=None and visibility=None with no body.

        Input: POST /handsfree/queue/d-1/approve with no body.
        Expected: approve_and_post is called with edited_plain=None and
        visibility=None.
        """
        # Arrange
        mock_orch.approve_and_post.return_value = {"ok": True}

        # Act
        self.client.post("/handsfree/queue/d-1/approve")

        # Assert
        mock_orch.approve_and_post.assert_called_once_with("d-1", edited_plain=None, visibility=None)

    def test_approve_rejects_get(self, mock_orch):
        """approve rejects GET.

        Input: GET /handsfree/queue/d-1/approve.
        Expected: 405; approve_and_post is never called (posting is POST-only).
        """
        # Act
        resp = self.client.get("/handsfree/queue/d-1/approve")

        # Assert: posting only via explicit POST
        self.assertEqual(resp.status_code, 405)
        mock_orch.approve_and_post.assert_not_called()

    def test_reject_passes_reason(self, mock_orch):
        """reject forwards the rejection reason.

        Input: POST /handsfree/queue/d-1/reject with a reason.
        Expected: reject is called with the draft id and reason.
        """
        # Arrange
        mock_orch.reject.return_value = {"ok": True}

        # Act
        resp = self.client.post("/handsfree/queue/d-1/reject", json={"reason": "wrong case"})

        # Assert
        self.assertEqual(resp.get_json(), {"ok": True})
        mock_orch.reject.assert_called_once_with("d-1", reason="wrong case")

    def test_reject_without_reason_defaults_to_empty(self, mock_orch):
        """reject defaults the reason to empty.

        Input: POST /handsfree/queue/d-1/reject with no reason.
        Expected: reject is called with reason="".
        """
        # Arrange
        mock_orch.reject.return_value = {"ok": True}

        # Act
        self.client.post("/handsfree/queue/d-1/reject")

        # Assert
        mock_orch.reject.assert_called_once_with("d-1", reason="")

    def test_discover_fields_success(self, mock_orch):
        """discover_fields returns the discovered REST fields.

        Input: POST /handsfree/discover_fields.
        Expected: 200 with the fields from discover_rest_fields.
        """
        # Arrange
        mock_orch.discover_rest_fields.return_value = {"fields": ["CommentBody"]}

        # Act
        resp = self.client.post("/handsfree/discover_fields")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"fields": ["CommentBody"]})

    def test_discover_fields_exception_returns_500(self, mock_orch):
        """A discover_fields exception returns 500 with the error type.

        Input: POST /handsfree/discover_fields where discovery raises
        ConnectionError.
        Expected: 500 with {"error": "ConnectionError: IPS unreachable"}.
        """
        # Arrange
        mock_orch.discover_rest_fields.side_effect = ConnectionError("IPS unreachable")

        # Act
        resp = self.client.post("/handsfree/discover_fields")

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"error": "ConnectionError: IPS unreachable"})


@patch(f"{ROUTES}._store")
class TestHandsfreeQueueRoutes(BaseTestCase):
    blueprint = handsfree_bp

    @patch(f"{ROUTES}._own_mailbox", return_value="me@intel.com")
    @patch(f"{ROUTES}._signed_in_name", return_value="Reviewer One")
    def test_list_queue_defaults_to_the_reviewers_open_drafts(self, _mock_name, _mock_box, mock_store_cls):
        """Listing the queue defaults to the reviewer's own open drafts.

        Input: GET /handsfree/queue with no query params, signed in to IPS as
        "Reviewer One" on a PC whose mailbox is me@intel.com.
        Expected: 200 with {items}; list_drafts is called with
        include_closed=False, that reviewer as the IPS case owner AND the
        mailbox (an Outlook-intake draft has no IPS owner).
        """
        # Arrange
        mock_store_cls.return_value.list_drafts.return_value = [{"draft_id": "d-1"}]

        # Act
        resp = self.client.get("/handsfree/queue")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"items": [{"draft_id": "d-1"}]})
        mock_store_cls.return_value.list_drafts.assert_called_once_with(
            include_closed=False, owner_name="Reviewer One", mailbox="me@intel.com")

    @patch(f"{ROUTES}._own_mailbox", return_value="")
    @patch(f"{ROUTES}._signed_in_name", return_value="")
    def test_list_queue_without_any_identity_is_empty_and_says_so(self, _mock_name, _mock_box, mock_store_cls):
        """Without an IPS sign-in or a mailbox the own view is empty, not everyone's.

        Input: GET /handsfree/queue with no reviewer signed in and no mail
        address known for this PC.
        Expected: 200 with {items: [], signed_out: True}; the store is not
        read.
        """
        # Act
        resp = self.client.get("/handsfree/queue")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"items": [], "signed_out": True})
        mock_store_cls.return_value.list_drafts.assert_not_called()

    @patch(f"{ROUTES}._own_mailbox", return_value="me@intel.com")
    @patch(f"{ROUTES}._signed_in_name", return_value="")
    def test_list_queue_signed_out_still_lists_the_mailbox_drafts(self, _mock_name, _mock_box, mock_store_cls):
        """Email drafts need no IPS sign-in: they are listed while signed out.

        Input: GET /handsfree/queue with no IPS sign-in on a PC whose mailbox
        is me@intel.com.
        Expected: 200 with the mailbox's drafts and signed_out: True (the
        page still offers the sign-in); list_drafts is filtered by the
        mailbox alone, never unfiltered.
        """
        # Arrange
        mock_store_cls.return_value.list_drafts.return_value = [{"draft_id": "e-1"}]

        # Act
        resp = self.client.get("/handsfree/queue")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"items": [{"draft_id": "e-1"}], "signed_out": True})
        mock_store_cls.return_value.list_drafts.assert_called_once_with(
            include_closed=False, owner_name="", mailbox="me@intel.com")

    @patch(f"{ROUTES}._own_mailbox")
    @patch(f"{ROUTES}._signed_in_name", return_value="")
    def test_list_queue_everyone_lifts_the_owner_filter(self, _mock_name, mock_box, mock_store_cls):
        """?everyone=1 lists every owner's drafts, optionally one owner's.

        Input: GET /handsfree/queue?everyone=1, then with &owner=Ann, with no
        reviewer signed in.
        Expected: list_drafts is called with owner_name "" and then "Ann",
        and no mailbox filter (this PC's mailbox is not even looked up).
        """
        # Arrange
        store = mock_store_cls.return_value
        store.list_drafts.return_value = []

        # Act
        self.client.get("/handsfree/queue", query_string={"everyone": "1"})
        self.client.get("/handsfree/queue", query_string={"everyone": "1", "owner": " Ann "})

        # Assert
        self.assertEqual([c.kwargs for c in store.list_drafts.call_args_list], [
            {"include_closed": False, "owner_name": "", "mailbox": ""},
            {"include_closed": False, "owner_name": "Ann", "mailbox": ""}])
        mock_box.assert_not_called()

    def test_list_queue_all_flag_variants_include_closed(self, mock_store_cls):
        """The ?all flag's truthy variants include closed drafts.

        Input: GET /handsfree/queue?everyone=1&all=<value> for truthy and
        falsy variants.
        Expected: list_drafts is called with include_closed matching the
        truthiness of each value.
        """
        # Arrange
        store = mock_store_cls.return_value
        store.list_drafts.return_value = []

        for value, expected in (("1", True), ("TRUE", True), ("yes", True), ("0", False), ("no", False)):
            with self.subTest(all=value):
                store.list_drafts.reset_mock()

                # Act
                self.client.get("/handsfree/queue", query_string={"everyone": "1", "all": value})

                # Assert
                store.list_drafts.assert_called_once_with(
                    include_closed=expected, owner_name="", mailbox="")

    def test_get_draft_found(self, mock_store_cls):
        """Fetching an existing draft returns it.

        Input: GET /handsfree/queue/d-1 where the store has that draft.
        Expected: 200 with the draft record; get is called with "d-1".
        """
        # Arrange
        rec = {"draft_id": "d-1", "status": "pending", "draft_plain": "text"}
        mock_store_cls.return_value.get.return_value = rec

        # Act
        resp = self.client.get("/handsfree/queue/d-1")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), rec)
        mock_store_cls.return_value.get.assert_called_once_with("d-1")

    def test_get_draft_not_found_returns_404(self, mock_store_cls):
        """Fetching a missing draft returns 404.

        Input: GET /handsfree/queue/missing where the store returns None.
        Expected: 404 with {"error": "not found"}.
        """
        # Arrange
        mock_store_cls.return_value.get.return_value = None

        # Act
        resp = self.client.get("/handsfree/queue/missing")

        # Assert
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json(), {"error": "not found"})

    def test_save_draft_prepends_ai_marker_and_composes_html(self, mock_store_cls):
        """Saving a draft prepends the AI marker and composes HTML.

        Input: PUT /handsfree/queue/d-1 with multi-line, HTML-unsafe draft text.
        Expected: 200; update is called with the trimmed text prefixed by the AI
        marker and an HTML rendering with escaped characters and <br/> breaks.
        """
        # Arrange
        store = mock_store_cls.return_value
        store.update.side_effect = lambda draft_id, **fields: {"draft_id": draft_id, **fields}

        # Act
        resp = self.client.put("/handsfree/queue/d-1", json={"draft_plain": "  Line 1\nLine <2>  "})

        # Assert
        expected_plain = AI_MARKER + "\n\nLine 1\nLine <2>"
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        store.update.assert_called_once_with(
            "d-1", draft_plain=expected_plain,
            draft_html="<p>" + AI_MARKER + "<br/><br/>Line 1<br/>Line &lt;2&gt;</p>")
        self.assertEqual(body["draft"]["draft_plain"], expected_plain)

    def test_save_draft_keeps_existing_marker_via_post(self, mock_store_cls):
        """Saving an already-marked draft (via POST) keeps the single marker.

        Input: POST /handsfree/queue/d-1 with text already carrying the AI marker.
        Expected: 200; update stores the text unchanged (no duplicate marker).
        """
        # Arrange
        plain = AI_MARKER + "\n\nalready marked"
        mock_store_cls.return_value.update.return_value = {"draft_id": "d-1"}

        # Act
        resp = self.client.post("/handsfree/queue/d-1", json={"draft_plain": plain})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_store_cls.return_value.update.call_args.kwargs["draft_plain"], plain)

    def test_save_draft_empty_returns_400(self, mock_store_cls):
        """Saving a blank draft returns 400.

        Input: PUT /handsfree/queue/d-1 with whitespace-only text.
        Expected: 400 with {ok: False, error: "empty draft"}; update is not
        called.
        """
        # Act
        resp = self.client.put("/handsfree/queue/d-1", json={"draft_plain": "   "})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {"ok": False, "error": "empty draft"})
        mock_store_cls.return_value.update.assert_not_called()

    def test_save_draft_unknown_id_returns_404(self, mock_store_cls):
        """Saving to an unknown draft id returns 404.

        Input: PUT /handsfree/queue/missing where update returns None.
        Expected: 404 with {ok: False, error: "not found"}.
        """
        # Arrange
        mock_store_cls.return_value.update.return_value = None

        # Act
        resp = self.client.put("/handsfree/queue/missing", json={"draft_plain": "text"})

        # Assert
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json(), {"ok": False, "error": "not found"})


@patch("services.check_ips_service.attach")
@patch(f"{ROUTES}.decoded_log_file", return_value=__file__)      # any file that exists
@patch(f"{ROUTES}._store")
class TestHandsfreeChatbotHandoff(BaseTestCase):
    """"Open in Avatar analysis": the chatbot must match the radio the run analysed."""

    blueprint = handsfree_bp

    def _handoff(self, mock_store, radio):
        mock_store.return_value.get.return_value = {
            "case_nbr": "01009610", "case_id": "500X", "subject": "s",
            "analysis": {"wifi_or_bt": radio, "subject": "s", "clean_description": "d",
                         "incidents": [{"sent_for_analysis": {"issue_time_log": "2026-10-22 10:15:00"}}]}}
        return self.client.post("/handsfree/chatbot_handoff",
                                json={"case_nbr": "01009610", "draft_id": "d-1"})

    def test_bluetooth_draft_opens_the_bluetooth_chatbot(self, mock_store, _mock_log, mock_attach):
        """A Bluetooth draft is handed to /bt_chatbot/, not the Wi-Fi chatbot.

        Input: POST /handsfree/chatbot_handoff for a draft whose analysis says
        wifi_or_bt "bt" (its decoded log is an HCI log).
        Expected: 200; the URL is /bt_chatbot/ with auto_run and the issue
        time, mode "bt", and the Wi-Fi chatbot's process-wide last-analysed
        log is left alone.
        """
        # Act
        resp = self._handoff(mock_store, "bt")

        # Assert
        body = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(body["url"].startswith("/bt_chatbot/?auto_run=analyze_all"))
        self.assertIn("issue_time=2026-10-22", body["url"])
        self.assertEqual(body["mode"], "bt")
        self.assertIsNone(self.app_config.last_analyzed_log_path)
        mock_attach.assert_called_once()

    def test_wifi_draft_opens_the_wifi_chatbot(self, mock_store, _mock_log, _mock_attach):
        """A Wi-Fi draft (and one with no radio recorded) goes to /log_chatbot/.

        Input: POST /handsfree/chatbot_handoff for analyses with wifi_or_bt
        "wifi" and "".
        Expected: 200; the URL is /log_chatbot/, mode "log", and the
        last-analysed log is set for the Wi-Fi chatbot.
        """
        for radio in ("wifi", ""):
            with self.subTest(radio=radio):
                # Act
                resp = self._handoff(mock_store, radio)

                # Assert
                body = resp.get_json()
                self.assertEqual(resp.status_code, 200)
                self.assertTrue(body["url"].startswith("/log_chatbot/?auto_run=analyze_all"))
                self.assertEqual(body["mode"], "log")
                self.assertEqual(self.app_config.last_analyzed_log_path, __file__)


class TestHandsfreeEnvDetail(unittest.TestCase):
    """Branch-specific: IPS Environment Details (env_detail) in the reader path."""

    def test_reader_prompt_has_env_block_placeholder(self):
        """The reader prompt exposes the env-block placeholder.

        Input: the READER_PROMPT template.
        Expected: it contains "{env_block}", the slot the env detail is formatted
        into.
        """
        # Assert: read_case_history() formats env_detail into this slot
        self.assertIn("{env_block}", READER_PROMPT)

    def test_format_env_detail_skips_blank_answers(self):
        """_format_env_detail skips blank answers and trims keys.

        Input: an env dict with a blank answer and a padded key.
        Expected: only non-blank entries remain, with keys trimmed, one per line.
        """
        # Arrange
        env = {"Computer Model": "TestModel X1", "Steps to reproduce": "   ", " OS ": "Windows 11"}

        # Act
        block = _format_env_detail(env)

        # Assert
        self.assertEqual(block, "Computer Model: TestModel X1\nOS: Windows 11")

    def test_format_env_detail_empty_or_invalid_is_none_marker(self):
        """_format_env_detail returns "(none)" for empty/invalid input.

        Input: empty dict, None, a non-dict, and a dict of only blank answers.
        Expected: each yields the "(none)" marker.
        """
        for env in ({}, None, "not a dict", {"Platform": ""}):
            with self.subTest(env=env):
                self.assertEqual(_format_env_detail(env), "(none)")

    def test_format_env_detail_truncates_long_answers_and_caps_entries(self):
        """_format_env_detail truncates long answers and caps entry count.

        Input: 30 entries where one answer is 400 chars long.
        Expected: at most 25 lines (_MAX_ENV_ENTRIES); the long answer is
        truncated to 300 chars with a "…[truncated]" suffix.
        """
        # Arrange
        env = {f"Q{i}": "x" for i in range(30)}
        env["Q0"] = "a" * 400

        # Act
        lines = _format_env_detail(env).splitlines()

        # Assert
        self.assertEqual(len(lines), 25)  # _MAX_ENV_ENTRIES
        self.assertEqual(lines[0], "Q0: " + "a" * 300 + " …[truncated]")

    def test_env_repro_steps_returns_filled_answer(self):
        """_env_repro_steps returns the trimmed repro-steps answer.

        Input: an env dict with a padded "Steps to reproduce" answer.
        Expected: the trimmed answer is returned.
        """
        # Arrange / Act / Assert
        self.assertEqual(
            _env_repro_steps({"Computer Model": "X1", "Steps to reproduce": " 1. Sleep 2. Resume "}),
            "1. Sleep 2. Resume")

    def test_env_repro_steps_ignores_placeholder_answers(self):
        """_env_repro_steps ignores placeholder answers.

        Input: placeholder answers ("", "NA", "n/a", "None", blank) and None.
        Expected: each yields an empty string.
        """
        for answer in ("", "NA", "n/a", "None", "   "):
            with self.subTest(answer=answer):
                self.assertEqual(_env_repro_steps({"Steps to reproduce": answer}), "")
        self.assertEqual(_env_repro_steps(None), "")

    def test_env_brief_keeps_only_platform_fields(self):
        """_env_brief keeps only the platform-relevant fields.

        Input: an env dict mixing platform fields, a placeholder OS answer and an
        ignored note.
        Expected: a "; "-joined brief of only the recognized platform fields,
        trimmed.
        """
        # Arrange
        env = {"Platform": "Lunar Lake", "Found in build": "  23.40  ", "Notes": "ignored",
               "Operating System": "n/a", "Computer Model": "TestModel X1"}

        # Act
        brief = _env_brief(env)

        # Assert
        self.assertEqual(brief, "Platform: Lunar Lake; Found in build: 23.40; Computer Model: TestModel X1")

    def test_env_brief_respects_limit(self):
        """_env_brief respects the character limit.

        Input: an over-long platform value, and a small explicit limit.
        Expected: the output is capped at the default 300 chars, and the explicit
        limit truncates mid-field.
        """
        self.assertEqual(len(_env_brief({"Platform": "p" * 500})), 300)
        self.assertEqual(_env_brief({"Platform": "Lunar Lake"}, limit=8), "Platform")


if __name__ == "__main__":
    unittest.main()
