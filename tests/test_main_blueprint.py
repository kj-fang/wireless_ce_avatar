"""Unit tests for the main blueprint (blueprints/main/main_routes.py).

The main blueprint owns the case-entry flow: IPS case number submission,
attachment selection, download-progress page, the download-result page
(log/ETL tables + auto-analysis pick), the BSOD result page and a few
utility endpoints (open folder, Windows event log dump/parse, BT event map).

Everything external is patched at its point of use in ``ROUTES``:
render_template, CaseService, gather_service, helpers, subprocess, the
etl/fw utils, ``open``/``yaml`` for the wireless-topics share, and the
lazily imported ``services.event_log_service`` / ``utils.issue_time_ai``
functions. No network, LLM, subprocess or file write ever happens.

Branch-specific note: on this branch ``app_config.silent_mode`` gates the
*automatic* explorer / TextAnalysisTool launches elsewhere (analysis_etl,
automation, log_parser). ``main.open_path`` is NOT gated - it is an
explicit user click - so it opens explorer even when silent_mode=True.
``test_open_path_ignores_silent_mode`` pins that down.
"""
import json
import sys
import unittest
from datetime import datetime
from unittest.mock import MagicMock, mock_open, patch

import services
from blueprints.main import main_routes
from blueprints.main.main_routes import main_bp
from models.models import CaseContext
from tests.base_test import TEST_CASE_NBR, BaseTestCase

ROUTES = "blueprints.main.main_routes"
ISSUE_TIME_AI = "utils.issue_time_ai"

WORKFLOW_ID = "wf-test-1"
PROMPT_PATH = "prompts/wifi_case_summary.txt"
ZIP_ITEM = ["logs.zip", "https://ips.example/att/1", ["2026-04-14 01:26:00", "uploaded by Partner"]]
TXT_ITEM = ["notes.txt", "https://ips.example/att/2", ["2026-04-13 10:00:00", ""]]
EVT_PATH = r"C:\fake\events\System.evtx"


def _processed_case(**overrides):
    """CaseContext as CaseService.process_case would return it."""
    values = dict(
        case_nbr=TEST_CASE_NBR,
        id="a0B000000TESTID",
        subject="Wi-Fi disconnects after resume",
        description="Wi-Fi drops after resume at 01:26",
        wifi_or_bt="wifi",
        case_download_dir=r"C:\fake\downloads\00123456",
        attachment_list=[ZIP_ITEM, TXT_ITEM],
    )
    values.update(overrides)
    return CaseContext(**values)


class MainTestBase(BaseTestCase):
    blueprint = main_bp

    def flashes(self):
        return self.get_session().get("_flashes", [])

    def assert_redirects_to(self, resp, path):
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], path)


# --------------------------------------------------------------------------
# Structure
# --------------------------------------------------------------------------
class TestMainBlueprintStructure(MainTestBase):

    def test_blueprint_registered_with_url_prefix(self):
        """Verify blueprint registration and all routes with methods.

        Input: the Flask app with ``main_bp`` registered (via setUp).
        Expected: the blueprint is mounted at "/" and exposes every expected
        route with its listed HTTP methods.
        """
        # Arrange
        expected = {
            "/": {"GET", "POST"},
            "/select_attachments": {"GET", "POST"},
            "/download_attachments": {"GET"},
            "/download_result": {"GET"},
            "/download_result_bsod": {"GET"},
            "/open_path": {"POST"},
            "/dump_event_txt": {"POST"},
            "/parse_event_log": {"POST"},
            "/api/bt_event_map": {"GET"},
            "/start_latest_etl_llm": {"POST"},
        }

        # Act
        rules = {r.rule: r.methods for r in self.app.url_map.iter_rules() if r.endpoint.startswith("main.")}

        # Assert
        self.assertIs(self.app.blueprints.get("main"), main_bp)
        self.assertEqual(main_bp.url_prefix, "/")
        self.assertEqual(set(rules), set(expected))
        for rule, methods in expected.items():
            self.assertTrue(methods.issubset(rules[rule]), rule)


# --------------------------------------------------------------------------
# /  (index)
# --------------------------------------------------------------------------
class TestIndex(MainTestBase):

    @patch(f"{ROUTES}._get_wireless_topics", return_value=[{"topic": "T", "explanation": "E"}])
    @patch(f"{ROUTES}.helpers.get_clipboard_case_number", return_value="00999999")
    @patch(f"{ROUTES}.render_template", return_value="index-page")
    def test_get_renders_case_form(self, mock_render, mock_clip, mock_topics):
        """GET / renders the case form with clipboard and topics.

        Input: GET / with a clipboard case number and wireless topics available.
        Expected: 200; index.html is rendered with the clipboard text and topics.
        """
        # Act
        resp = self.client.get("/")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(as_text=True), "index-page")
        mock_render.assert_called_once_with(
            "index.html", clipboard_text="00999999",
            wireless_topics=[{"topic": "T", "explanation": "E"}])

    @patch(f"{ROUTES}.CaseService")
    def test_post_empty_case_number_flashes_and_redirects(self, mock_service):
        """POST / with an empty case number flashes and redirects home.

        Input: POST / with a whitespace case_number.
        Expected: 302 back to "/" with a danger flash; the case service is not
        called.
        """
        # Act
        resp = self.client.post("/", data={"case_number": "   "})

        # Assert
        self.assert_redirects_to(resp, "/")
        self.assertEqual(self.flashes(), [("danger", "❌ No case number provided.")])
        mock_service.process_case.assert_not_called()

    @patch(f"{ROUTES}.gather_service")
    @patch(f"{ROUTES}.CaseService")
    def test_post_invalid_case_flashes_and_redirects(self, mock_service, mock_gather):
        """POST / with an invalid case flashes and redirects home.

        Input: POST / where process_case returns a context with an error_message.
        Expected: 302 back to "/" with the raw case error surfaced as a danger
        flash; no session case context and no workflow start recorded.
        """
        # Arrange
        mock_service.process_case.return_value = _processed_case(error_message="not found")

        # Act
        resp = self.client.post("/", data={"case_number": "00000000"})

        # Assert
        self.assert_redirects_to(resp, "/")
        self.assertEqual(self.flashes()[0], ("danger", "not found"))
        self.assertNotIn("case_context", self.get_session())
        mock_gather.record_workflow_start.assert_not_called()

    @patch(f"{ROUTES}.gather_service")
    @patch(f"{ROUTES}.CaseService")
    def test_post_valid_case_populates_session_and_redirects(self, mock_service, mock_gather):
        """POST / with a valid case populates the session and redirects.

        Input: POST / with a spaced case number that processes successfully.
        Expected: 302 to /select_attachments; the session is cleared then
        repopulated with the case context, prompt path and default flags, and a
        workflow start is recorded with the stripped case number and domain.
        """
        # Arrange
        mock_service.process_case.return_value = _processed_case()
        mock_service.load_case_summary_prompt.return_value = PROMPT_PATH
        mock_gather.new_workflow_id.return_value = WORKFLOW_ID
        with self.client.session_transaction() as sess:
            sess["stale_key"] = "from previous case"

        # Act
        resp = self.client.post("/", data={"case_number": " 00 123 456 "})

        # Assert
        self.assert_redirects_to(resp, "/select_attachments")
        submitted = mock_service.process_case.call_args.kwargs["case_context"]
        self.assertEqual(submitted.case_nbr, TEST_CASE_NBR)  # spaces stripped
        mock_service.load_case_summary_prompt.assert_called_once_with("wifi")
        sess = self.get_session()
        self.assertNotIn("stale_key", sess)  # session.clear()
        self.assertEqual(sess["case_context"]["case_nbr"], TEST_CASE_NBR)
        self.assertEqual(sess["prompt_file_path"], PROMPT_PATH)
        self.assertIs(sess["bsod"], False)
        self.assertIs(sess["latest_etl_llm"], False)
        self.assertIs(sess["debug_mode"], False)
        self.assertEqual(sess["gather_workflow_id"], WORKFLOW_ID)
        mock_gather.record_workflow_start.assert_called_once()
        start_kwargs = mock_gather.record_workflow_start.call_args.kwargs
        self.assertEqual(start_kwargs["workflow_id"], WORKFLOW_ID)
        self.assertEqual(start_kwargs["domain"], "wifi")
        self.assertEqual(start_kwargs["attachment_list"], [ZIP_ITEM, TXT_ITEM])
        mock_gather.record_attachment_selection.assert_not_called()

    @patch(f"{ROUTES}.CaseService")
    def test_post_service_exception_flashes_and_redirects(self, mock_service):
        """POST / with a service exception flashes and redirects home.

        Input: POST / where process_case raises RuntimeError.
        Expected: 302 back to "/" with a generic danger flash.
        """
        # Arrange
        mock_service.process_case.side_effect = RuntimeError("IPS down")

        # Act
        resp = self.client.post("/", data={"case_number": TEST_CASE_NBR})

        # Assert
        self.assert_redirects_to(resp, "/")
        self.assertEqual(self.flashes(), [("danger", "An error occurred while processing the case.")])


# --------------------------------------------------------------------------
# _get_wireless_topics (core logic behind the index page)
# --------------------------------------------------------------------------
class TestGetWirelessTopics(MainTestBase):
    FALLBACK = [{
        "topic": "Bluetooth Core Specification",
        "explanation": "Defines the technologies required to create interoperable Bluetooth devices.",
    }]

    @patch(f"{ROUTES}.random")
    @patch(f"{ROUTES}.yaml.safe_load")
    @patch(f"{ROUTES}.open", new_callable=mock_open, read_data="", create=True)
    def test_merges_bt_and_wifi_topics_and_limits(self, mock_file, mock_yaml, mock_random):
        """_get_wireless_topics merges BT and Wi-Fi topics and limits them.

        Input: BT and Wi-Fi YAML files (BT uses topic/explanation, Wi-Fi uses
        topic/fact) with a limit of 2.
        Expected: entries from both files are normalized to topic/explanation,
        shuffled, and limited to 2; both files are read.
        """
        # Arrange: BT file uses topic/explanation, WiFi file uses topic/fact
        mock_yaml.side_effect = [
            {"bluetooth_topics": [
                {"topic": "LE Audio", "explanation": "LC3 codec"},
                {"topic": "no explanation"},
            ]},
            {"knowledge_base": {"items": [
                {"topic": "MLO", "fact": "Multi-link operation"},
                {"topic": "320 MHz", "fact": "Wider channels"},
            ]}},
        ]

        # Act
        topics = main_routes._get_wireless_topics(limit=2)

        # Assert
        self.assertEqual(topics, [
            {"topic": "LE Audio", "explanation": "LC3 codec"},
            {"topic": "MLO", "explanation": "Multi-link operation"},
        ])
        self.assertEqual(mock_file.call_count, 2)
        mock_random.shuffle.assert_called_once()

    @patch(f"{ROUTES}.open", side_effect=OSError("share unreachable"), create=True)
    def test_unreachable_share_returns_fallback(self, mock_file):
        """_get_wireless_topics returns the fallback when the share is
        unreachable.

        Input: _get_wireless_topics where open() raises OSError.
        Expected: the single built-in fallback topic list.
        """
        # Act / Assert
        self.assertEqual(main_routes._get_wireless_topics(), self.FALLBACK)

    @patch(f"{ROUTES}.yaml.safe_load", return_value=None)
    @patch(f"{ROUTES}.open", new_callable=mock_open, read_data="", create=True)
    def test_empty_yaml_returns_fallback(self, mock_file, mock_yaml):
        """_get_wireless_topics returns the fallback for empty YAML.

        Input: _get_wireless_topics where safe_load returns None.
        Expected: the single built-in fallback topic list.
        """
        # Act / Assert
        self.assertEqual(main_routes._get_wireless_topics(), self.FALLBACK)


# --------------------------------------------------------------------------
# /start_latest_etl_llm
# --------------------------------------------------------------------------
class TestStartLatestEtlLlm(MainTestBase):

    def setUp(self):
        super().setUp()
        self.app_config.llm_helper.classify_issue.return_value = {
            "issue_type": "Disconnect", "confidence": 90, "keywords_found": ["drop"]}
        prime = patch(f"{ROUTES}._prime_issue_ai_cache")
        self.mock_prime = prime.start()
        self.addCleanup(prime.stop)

    @patch(f"{ROUTES}.CaseService")
    def test_missing_case_number_returns_400(self, mock_service):
        """start_latest_etl_llm requires a case number.

        Input: POST /start_latest_etl_llm with no case_number.
        Expected: 400 with "No case number provided."; process_case is not called.
        """
        # Act
        resp = self.client.post("/start_latest_etl_llm", data={})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {"success": False, "message": "No case number provided."})
        mock_service.process_case.assert_not_called()

    @patch(f"{ROUTES}.CaseService")
    def test_invalid_case_returns_400(self, mock_service):
        """start_latest_etl_llm returns 400 for an invalid case.

        Input: POST /start_latest_etl_llm where process_case returns an error.
        Expected: 400 surfacing the raw case error message.
        """
        # Arrange
        mock_service.process_case.return_value = _processed_case(error_message="bad")

        # Act
        resp = self.client.post("/start_latest_etl_llm", data={"case_number": TEST_CASE_NBR})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["message"], "bad")

    @patch(f"{ROUTES}.CaseService")
    def test_no_zip_attachment_returns_400(self, mock_service):
        """start_latest_etl_llm returns 400 with no ZIP attachment.

        Input: POST /start_latest_etl_llm where the case has only a .txt
        attachment.
        Expected: 400 with "No ZIP attachment found"; no session case context.
        """
        # Arrange
        mock_service.process_case.return_value = _processed_case(attachment_list=[TXT_ITEM])

        # Act
        resp = self.client.post("/start_latest_etl_llm", data={"case_number": TEST_CASE_NBR})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["message"], "No ZIP attachment found for this case.")
        self.assertNotIn("case_context", self.get_session())

    @patch(f"{ROUTES}.gather_service")
    @patch(f"{ROUTES}.CaseService")
    def test_success_sets_session_classifies_and_returns_redirect(self, mock_service, mock_gather):
        """start_latest_etl_llm sets session, classifies and returns a redirect.

        Input: POST /start_latest_etl_llm for a valid case with a ZIP.
        Expected: 200 with a redirect to /download_attachments; the session is
        cleared then set with the selected ZIP, run/latest flags and
        classification; the issue is classified, selection recorded and the issue
        cache primed.
        """
        # Arrange
        mock_service.process_case.return_value = _processed_case()
        mock_service.load_case_summary_prompt.return_value = PROMPT_PATH
        mock_gather.new_workflow_id.return_value = WORKFLOW_ID
        with self.client.session_transaction() as sess:
            sess["stale_key"] = "x"

        # Act
        resp = self.client.post("/start_latest_etl_llm", data={"case_number": TEST_CASE_NBR})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"success": True, "redirect_url": "/download_attachments"})
        sess = self.get_session()
        self.assertNotIn("stale_key", sess)
        self.assertEqual(sess["selected_files"], [ZIP_ITEM])
        self.assertIs(sess["bsod"], False)
        self.assertIs(sess["latest_etl_llm"], True)
        self.assertIs(sess["_run_analysis_requested"], True)
        self.assertEqual(sess["prompt_file_path"], PROMPT_PATH)
        self.assertEqual(sess["classification"]["issue_type"], "Disconnect")
        self.app_config.llm_helper.classify_issue.assert_called_once()
        mock_gather.record_attachment_selection.assert_called_once()
        self.assertEqual(mock_gather.record_attachment_selection.call_args.kwargs["selected_files"], [ZIP_ITEM])
        self.mock_prime.assert_called_once()

    @patch(f"{ROUTES}.gather_service")
    @patch(f"{ROUTES}.CaseService")
    def test_no_llm_helper_keeps_default_classification(self, mock_service, mock_gather):
        """start_latest_etl_llm keeps the default classification without an LLM.

        Input: POST /start_latest_etl_llm when llm_helper is None.
        Expected: 200 success; the session classification stays Unclassified and
        the cache is primed with no initial usage.
        """
        # Arrange
        self.app_config.llm_helper = None
        mock_service.process_case.return_value = _processed_case()
        mock_service.load_case_summary_prompt.return_value = PROMPT_PATH
        mock_gather.new_workflow_id.return_value = WORKFLOW_ID

        # Act
        resp = self.client.post("/start_latest_etl_llm", data={"case_number": TEST_CASE_NBR})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["success"])
        self.assertEqual(self.get_session()["classification"],
                         {"issue_type": "Unclassified", "confidence": 0, "keywords_found": []})
        self.mock_prime.assert_called_once()
        self.assertIsNone(self.mock_prime.call_args.kwargs["initial_usage"])

    @patch(f"{ROUTES}.gather_service")
    @patch(f"{ROUTES}.CaseService")
    def test_classification_and_prepass_failures_are_best_effort(self, mock_service, mock_gather):
        """start_latest_etl_llm treats classification/prepass failures as
        best-effort.

        Input: POST /start_latest_etl_llm where classify_issue and the prepass
        both raise.
        Expected: 200 success; the classification falls back to Unclassified.
        """
        # Arrange
        mock_service.process_case.return_value = _processed_case()
        mock_service.load_case_summary_prompt.return_value = PROMPT_PATH
        mock_gather.new_workflow_id.return_value = WORKFLOW_ID
        self.app_config.llm_helper.classify_issue.side_effect = RuntimeError("LLM down")
        self.mock_prime.side_effect = RuntimeError("prepass down")

        # Act
        resp = self.client.post("/start_latest_etl_llm", data={"case_number": TEST_CASE_NBR})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["success"])
        self.assertEqual(self.get_session()["classification"]["issue_type"], "Unclassified")

    @patch(f"{ROUTES}.CaseService")
    def test_service_exception_returns_500(self, mock_service):
        """start_latest_etl_llm returns 500 on a service exception.

        Input: POST /start_latest_etl_llm where process_case raises.
        Expected: 500 with a generic error message.
        """
        # Arrange
        mock_service.process_case.side_effect = RuntimeError("IPS down")

        # Act
        resp = self.client.post("/start_latest_etl_llm", data={"case_number": TEST_CASE_NBR})

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(),
                         {"success": False, "message": "An error occurred while processing the case."})


# --------------------------------------------------------------------------
# /select_attachments
# --------------------------------------------------------------------------
class TestSelectAttachments(MainTestBase):

    def setUp(self):
        super().setUp()
        prime = patch(f"{ROUTES}._prime_issue_ai_cache")
        self.mock_prime = prime.start()
        self.addCleanup(prime.stop)

    def _seed_with_attachments(self, **extra):
        with self.client.session_transaction() as sess:
            sess["case_context"] = self.create_mock_case_context(attachment_list=[ZIP_ITEM, TXT_ITEM])
            sess["gather_workflow_id"] = WORKFLOW_ID
            sess.update(extra)

    def test_get_without_session_redirects_to_index(self):
        """select_attachments GET without a session redirects home.

        Input: GET /select_attachments with no case context.
        Expected: 302 to "/" with a "Session expired" warning flash.
        """
        # Act
        resp = self.client.get("/select_attachments")

        # Assert
        self.assert_redirects_to(resp, "/")
        self.assertEqual(self.flashes(), [("warning", "Session expired. Please start again.")])

    @patch(f"{ROUTES}.render_template", return_value="select-page")
    def test_get_renders_form_with_case_context(self, mock_render):
        """select_attachments GET renders the form with the case context.

        Input: GET /select_attachments with a seeded case session.
        Expected: 200; select_attachments.html is rendered with the case context
        and a null ai_analysis.
        """
        # Arrange
        self.seed_session()

        # Act
        resp = self.client.get("/select_attachments")

        # Assert
        self.assertEqual(resp.status_code, 200)
        args, kwargs = mock_render.call_args
        self.assertEqual(args, ("select_attachments.html",))
        self.assertIsNone(kwargs["ai_analysis"])
        self.assertEqual(kwargs["case_context"], self.create_mock_case_context())

    def test_post_without_session_redirects_to_index(self):
        """select_attachments POST without a session redirects home.

        Input: POST /select_attachments with no case context.
        Expected: 302 to "/"; the issue cache is not primed.
        """
        # Act
        resp = self.client.post("/select_attachments", data={"selected_files": ["logs.zip"]})

        # Assert
        self.assert_redirects_to(resp, "/")
        self.mock_prime.assert_not_called()

    @patch(f"{ROUTES}.gather_service")
    def test_post_analysis_action_sets_run_flags(self, mock_gather):
        """The analysis action sets the run/latest flags and records selection.

        Input: POST /select_attachments with the ZIP selected and action
        "analysis".
        Expected: 302 to /download_attachments; the session records the selected
        ZIP, bsod False, latest/run flags True; selection is recorded and the
        cache primed.
        """
        # Arrange
        self._seed_with_attachments()

        # Act
        resp = self.client.post("/select_attachments",
                                data={"selected_files": ["logs.zip"], "action": "analysis"})

        # Assert
        self.assert_redirects_to(resp, "/download_attachments")
        sess = self.get_session()
        self.assertEqual(sess["selected_files"], [ZIP_ITEM])
        self.assertIs(sess["bsod"], False)
        self.assertIs(sess["latest_etl_llm"], True)
        self.assertIs(sess["_run_analysis_requested"], True)
        mock_gather.record_attachment_selection.assert_called_once()
        kwargs = mock_gather.record_attachment_selection.call_args.kwargs
        self.assertEqual(kwargs["workflow_id"], WORKFLOW_ID)
        self.assertEqual(kwargs["selected_files"], [ZIP_ITEM])
        self.assertEqual(kwargs["domain"], "wifi")
        self.mock_prime.assert_called_once()

    @patch(f"{ROUTES}.gather_service")
    def test_post_legacy_latest_etl_llm_action_is_accepted(self, mock_gather):
        """The legacy latest_etl_llm action is still accepted.

        Input: POST /select_attachments with action "latest_etl_llm".
        Expected: the session records latest_etl_llm and the run-analysis flag.
        """
        # Arrange
        self._seed_with_attachments()

        # Act
        self.client.post("/select_attachments",
                         data={"selected_files": ["logs.zip"], "action": "latest_etl_llm"})

        # Assert
        sess = self.get_session()
        self.assertIs(sess["latest_etl_llm"], True)
        self.assertIs(sess["_run_analysis_requested"], True)

    @patch(f"{ROUTES}.gather_service")
    def test_post_bsod_action_sets_bsod_without_run_marker(self, mock_gather):
        """The bsod action sets the bsod flow without a run marker.

        Input: POST /select_attachments with both files selected and action
        "bsod".
        Expected: 302 to /download_attachments; the session records both files,
        bsod True, latest False, and no run-analysis marker.
        """
        # Arrange
        self._seed_with_attachments()

        # Act
        resp = self.client.post("/select_attachments",
                                data={"selected_files": ["logs.zip", "notes.txt"], "action": "bsod"})

        # Assert
        self.assert_redirects_to(resp, "/download_attachments")
        sess = self.get_session()
        self.assertEqual(sess["selected_files"], [ZIP_ITEM, TXT_ITEM])
        self.assertIs(sess["bsod"], True)
        self.assertIs(sess["latest_etl_llm"], False)
        self.assertNotIn("_run_analysis_requested", sess)

    @patch(f"{ROUTES}.gather_service")
    def test_post_telemetry_and_prepass_failures_are_best_effort(self, mock_gather):
        """select_attachments treats telemetry/prepass failures as best-effort.

        Input: POST /select_attachments with action "download" where selection
        recording and the prepass both raise.
        Expected: 302 to /download_attachments; the selected ZIP is still stored.
        """
        # Arrange
        self._seed_with_attachments()
        mock_gather.record_attachment_selection.side_effect = RuntimeError("telemetry down")
        self.mock_prime.side_effect = RuntimeError("LLM down")

        # Act
        resp = self.client.post("/select_attachments",
                                data={"selected_files": ["logs.zip"], "action": "download"})

        # Assert
        self.assert_redirects_to(resp, "/download_attachments")
        self.assertEqual(self.get_session()["selected_files"], [ZIP_ITEM])


# --------------------------------------------------------------------------
# /download_attachments  (+ _resolve_download_path)
# --------------------------------------------------------------------------
class TestDownloadAttachments(MainTestBase):

    def test_without_session_redirects_to_index(self):
        """download_attachments without a session redirects home.

        Input: GET /download_attachments with no case context.
        Expected: 302 to "/" with a "Session expired" warning flash.
        """
        # Act
        resp = self.client.get("/download_attachments")

        # Assert
        self.assert_redirects_to(resp, "/")
        self.assertEqual(self.flashes(), [("warning", "Session expired. Please start again.")])

    @patch(f"{ROUTES}.render_template", return_value="progress-page")
    def test_non_bsod_uses_case_download_dir(self, mock_render):
        """download_attachments (non-BSOD) uses the case download dir.

        Input: GET /download_attachments for a non-BSOD case with selected files.
        Expected: 200; the progress page is rendered with the per-file map and the
        case download dir, which is also stored in the session.
        """
        # Arrange
        self.seed_session(bsod=False, selected_files=[ZIP_ITEM, TXT_ITEM])
        expected_path = self.create_mock_case_context()["case_download_dir"]

        # Act
        resp = self.client.get("/download_attachments")

        # Assert
        self.assertEqual(resp.status_code, 200)
        mock_render.assert_called_once_with(
            "attachment_download_progress.html",
            files_to_download={"logs.zip": 0, "notes.txt": 0},
            download_path=expected_path)
        self.assertEqual(self.get_session()["download_path"], expected_path)

    @patch(f"{ROUTES}.helpers.get_load_path", return_value=r"\\share\BSOD_Dumps")
    @patch(f"{ROUTES}.render_template", return_value="progress-page")
    def test_bsod_uses_shared_load_path_and_case_nbr(self, mock_render, mock_load):
        """download_attachments (BSOD) uses the shared load path and case nbr.

        Input: GET /download_attachments for a BSOD case with a reachable share.
        Expected: the download path is the share path joined with the case number
        and stored in the session.
        """
        # Arrange
        self.seed_session(bsod=True, selected_files=[ZIP_ITEM])

        # Act
        self.client.get("/download_attachments")

        # Assert
        self.assertEqual(mock_render.call_args.kwargs["download_path"], r"\\share\BSOD_Dumps\00123456")
        self.assertEqual(self.get_session()["download_path"], r"\\share\BSOD_Dumps\00123456")

    @patch(f"{ROUTES}.helpers.get_load_path", return_value=r"\\share\BSOD_Dumps")
    def test_resolve_download_path_prefers_dashed_backend_id(self, mock_load):
        """_resolve_download_path prefers a dashed backend id for BSOD.

        Input: _resolve_download_path for a BSOD context with a dashed
        backend_id.
        Expected: the share path joined with the backend id.
        """
        # Arrange
        ctx = CaseContext(case_nbr=TEST_CASE_NBR, backend_id="BSOD-4242")

        # Act / Assert
        self.assertEqual(main_routes._resolve_download_path(ctx, True), r"\\share\BSOD_Dumps\BSOD-4242")

    @patch(f"{ROUTES}.helpers.get_load_path", return_value=None)
    def test_resolve_download_path_bsod_without_share_returns_empty(self, mock_load):
        """_resolve_download_path returns empty without a share (BSOD) or context.

        Input: _resolve_download_path for a BSOD context with no share, and for a
        None context.
        Expected: an empty string in both cases.
        """
        # Act / Assert
        self.assertEqual(main_routes._resolve_download_path(CaseContext(case_nbr=TEST_CASE_NBR), True), "")
        self.assertEqual(main_routes._resolve_download_path(None, False), "")


# --------------------------------------------------------------------------
# /download_result
# --------------------------------------------------------------------------
WIFI_ETL_1 = r"C:\fake\downloads\00123456\logs\20260414\WiFi_1.etl"
WIFI_ETL_3 = r"C:\fake\downloads\00123456\logs\20260414\WiFi_3.etl"
WIFI_LOG = r"C:\fake\downloads\00123456\logs\20260414\setup.log"
BT_OLD = r"C:\fake\downloads\00123456\logs\old\BT_a.etl"
BT_NEW = r"C:\fake\downloads\00123456\logs\new\BT_b.etl"


class TestDownloadResult(MainTestBase):

    def setUp(self):
        super().setUp()
        self.patches = {}
        for name, kwargs in {
            "render_template": {"return_value": "result-page"},
            "get_issue_time_from_selected_files": {"return_value": {}},
            "get_auto_analysis_etl": {"return_value": None},
            "filter_folders_by_time": {},
            "extract_timestamp_from_folder": {"return_value": None},
            "load_fw_system_info": {"return_value": None},
            "infer_fw_parse_type": {"return_value": "wifi"},
        }.items():
            p = patch(f"{ROUTES}.{name}", **kwargs)
            self.patches[name] = p.start()
            self.addCleanup(p.stop)
        for name in ("align_issue_datetime_to_customer_frame", "align_issue_time_for_display",
                     "pick_etl_by_ai_time", "anchor_time_only_to_folder_date"):
            p = patch(f"{ISSUE_TIME_AI}.{name}")
            self.patches[name] = p.start()
            self.addCleanup(p.stop)
        # identity alignment / anchoring by default
        self.patches["align_issue_time_for_display"].side_effect = lambda t, *a, **k: t
        self.patches["anchor_time_only_to_folder_date"].side_effect = lambda t, folder: t
        self.patches["pick_etl_by_ai_time"].return_value = None
        self.mock_render = self.patches["render_template"]

    def set_results(self, wifi=None, ddd=None, bt=None, fw=None):
        self.app_config.get_download_results.return_value = {
            "wifi": wifi or {}, "ddd": ddd or {}, "bt": bt or {}, "fw": fw or {}}

    def render_kwargs(self):
        args, kwargs = self.mock_render.call_args
        self.assertEqual(args, ("download_result.html",))
        return kwargs

    def test_without_session_redirects_to_index(self):
        """download_result without a session redirects home.

        Input: GET /download_result with no case context.
        Expected: 302 to "/"; the template is not rendered.
        """
        # Act
        resp = self.client.get("/download_result")

        # Assert
        self.assert_redirects_to(resp, "/")
        self.mock_render.assert_not_called()

    @patch(f"{ROUTES}.helpers.get_load_path", return_value=None)
    def test_empty_download_path_redirects_to_index(self, mock_load):
        """download_result with an empty download path redirects home.

        Input: GET /download_result for a BSOD case with no reachable share.
        Expected: 302 to "/" with a "Session expired" warning flash.
        """
        # Arrange: bsod case with no reachable share -> download_path ''
        self.seed_session(bsod=True)

        # Act
        resp = self.client.get("/download_result")

        # Assert
        self.assert_redirects_to(resp, "/")
        self.assertEqual(self.flashes(), [("warning", "Session expired. Please start again.")])

    def test_wifi_only_renders_tables_without_auto_pick(self):
        """A wifi-only result renders tables without an auto pick.

        Input: GET /download_result with wifi results (an ETL plus a .log).
        Expected: 200; the wifi table excludes the .log, no coexist/auto pick,
        empty issue-time/filter fields, and the auto-analysis lookup runs once.
        """
        # Arrange
        self.set_results(wifi={"logs.zip": [WIFI_ETL_1, WIFI_LOG]})
        self.seed_session(selected_files=[ZIP_ITEM])

        # Act
        resp = self.client.get("/download_result")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.app_config.get_download_results.assert_called_once_with(TEST_CASE_NBR)
        kw = self.render_kwargs()
        self.assertEqual(kw["case_path"], self.create_mock_case_context()["case_download_dir"])
        self.assertEqual(kw["wifi_or_bt"], "wifi")
        self.assertIs(kw["files_coexist"], False)
        self.assertIsNone(kw["auto_analysis_etl"])
        self.assertIsNone(kw["auto_analysis_etl_reason"])
        self.assertIsNone(kw["auto_analysis_bt"])
        self.assertEqual(kw["llm_issue_time"], "")
        self.assertEqual(kw["llm_issue_times"], [])
        self.assertEqual(kw["ai_pre_selected_etl"], "")
        self.assertEqual(kw["time_filter_info"], [])
        self.assertEqual(kw["time_filter_warnings"], [])
        self.assertEqual(kw["exclude_keywords"], self.app_config.etl_exclude_keywords)
        self.assertEqual([r["etl_path"] for r in kw["wifi_table_rows"]], [WIFI_ETL_1])  # .log filtered
        self.assertEqual(kw["wifi_dict"], {"logs.zip": [WIFI_ETL_1, WIFI_LOG]})
        self.patches["get_auto_analysis_etl"].assert_called_once()
        self.patches["pick_etl_by_ai_time"].assert_not_called()

    def test_coex_bt_hint_skips_wifi_auto_pick(self):
        """A coexisting BT hint marks files_coexist and skips the wifi auto pick.

        Input: GET /download_result with both wifi and bt results and a bt case
        hint.
        Expected: files_coexist is True, no wifi auto pick is attempted, the
        session records coexistence, and BT file sizes are reported.
        """
        # Arrange
        self.set_results(wifi={"logs.zip": [WIFI_ETL_1]}, bt={"logs.zip": [BT_NEW]})
        with self.client.session_transaction() as sess:
            sess["case_context"] = self.create_mock_case_context(wifi_or_bt="bt")

        # Act
        with patch(f"{ROUTES}.os.path.isfile", return_value=False):
            self.client.get("/download_result")

        # Assert
        kw = self.render_kwargs()
        self.assertIs(kw["files_coexist"], True)
        self.assertIsNone(kw["auto_analysis_etl"])
        self.patches["get_auto_analysis_etl"].assert_not_called()
        self.assertIs(self.get_session()["case_context"]["files_coexist"], True)
        self.assertEqual(kw["bt_file_sizes"], {BT_NEW: 0})

    def test_run_analysis_recovery_picks_highest_numbered_etl(self):
        """Run-analysis recovery picks the highest-numbered ETL.

        Input: GET /download_result where the upstream pick returns None but Run
        Analysis was requested.
        Expected: the highest-numbered ETL is auto-selected with reason
        "latest_by_number"; the run-analysis marker is popped.
        """
        # Arrange: upstream pick returns None but Run Analysis was requested
        self.set_results(wifi={"logs.zip": [WIFI_ETL_1, WIFI_ETL_3, WIFI_LOG]})
        self.seed_session(_run_analysis_requested=True)

        # Act
        self.client.get("/download_result")

        # Assert
        kw = self.render_kwargs()
        self.assertEqual(kw["auto_analysis_etl"], WIFI_ETL_3)
        self.assertEqual(kw["auto_analysis_etl_reason"], "latest_by_number")
        self.assertNotIn("_run_analysis_requested", self.get_session())  # popped once

    def test_ai_issue_time_overrides_newest_by_number_pick(self):
        """An AI issue time overrides the newest-by-number pick.

        Input: GET /download_result where the number pick is WIFI_ETL_3 but the
        AI-time pick is WIFI_ETL_1, with a cached issue time.
        Expected: the AI-time ETL is auto-selected with reason "ai_time", the
        issue time is surfaced, and the display alignment runs.
        """
        # Arrange
        self.set_results(wifi={"logs.zip": [WIFI_ETL_1, WIFI_ETL_3]})
        self.patches["get_auto_analysis_etl"].return_value = WIFI_ETL_3
        self.patches["pick_etl_by_ai_time"].return_value = WIFI_ETL_1
        self.seed_session(_issue_ai_quick={"data": {"issue_times": ["04/14/2026-01:26:00"]}})

        # Act
        self.client.get("/download_result")

        # Assert
        kw = self.render_kwargs()
        self.assertEqual(kw["llm_issue_time"], "04/14/2026-01:26:00")
        self.assertEqual(kw["llm_issue_times"], ["04/14/2026-01:26:00"])
        self.assertEqual(kw["auto_analysis_etl"], WIFI_ETL_1)
        self.assertEqual(kw["auto_analysis_etl_reason"], "ai_time")
        self.assertEqual(kw["ai_pre_selected_etl"], WIFI_ETL_1)
        self.patches["align_issue_time_for_display"].assert_called_once()

    def test_attachment_time_filter_success_and_llm_time_fallback(self):
        """An attachment-time filter succeeds and feeds the LLM time fallback.

        Input: GET /download_result where an issue time is parsed from the
        attachment subtitle and folder filtering succeeds.
        Expected: time_filter_info records the parsed time, no warnings, and the
        LLM issue time falls back to the time mapping.
        """
        # Arrange: issue time parsed from the attachment subtitle
        self.set_results(wifi={"logs.zip": [WIFI_ETL_1, WIFI_ETL_3]})
        self.patches["get_issue_time_from_selected_files"].return_value = {"logs.zip": "14:15:18"}
        self.patches["filter_folders_by_time"].side_effect = lambda d, t: (d, {})
        self.seed_session(selected_files=[ZIP_ITEM])

        # Act
        self.client.get("/download_result")

        # Assert
        kw = self.render_kwargs()
        self.assertEqual(kw["time_filter_info"], [("logs.zip", "14:15:18")])
        self.assertEqual(kw["time_filter_warnings"], [])
        self.assertEqual(kw["llm_issue_time"], "14:15:18")  # fallback from time_mapping
        self.assertEqual(kw["llm_issue_times"], ["14:15:18"])
        self.patches["filter_folders_by_time"].assert_called_once_with(
            {"logs.zip": [WIFI_ETL_1, WIFI_ETL_3]}, "14:15:18")

    def test_attachment_time_filter_warning_is_reported(self):
        """An attachment-time filter warning is reported.

        Input: GET /download_result where folder filtering returns a warning.
        Expected: time_filter_info is empty and time_filter_warnings carries the
        warning.
        """
        # Arrange
        self.set_results(wifi={"logs.zip": [WIFI_ETL_1]})
        self.patches["get_issue_time_from_selected_files"].return_value = {"logs.zip": "14:15:18"}
        self.patches["filter_folders_by_time"].side_effect = lambda d, t: (
            d, {"logs.zip": "All folders are before issue time"})
        self.seed_session(selected_files=[ZIP_ITEM])

        # Act
        self.client.get("/download_result")

        # Assert
        kw = self.render_kwargs()
        self.assertEqual(kw["time_filter_info"], [])
        self.assertEqual(kw["time_filter_warnings"], [("logs.zip", "All folders are before issue time")])

    def test_bt_run_analysis_picks_newest_timestamped_bt_log(self):
        """BT run-analysis picks the newest timestamped BT log.

        Input: GET /download_result for a bt case with Run Analysis requested and
        two BT ETLs of differing folder timestamps, plus an auto-analysis fw path.
        Expected: the newer BT ETL is auto-selected, the fw path is surfaced and
        popped from session, and the BT table lists both ETLs.
        """
        # Arrange
        self.set_results(bt={"logs.zip": [BT_OLD, BT_NEW]})
        with self.client.session_transaction() as sess:
            sess["case_context"] = self.create_mock_case_context(wifi_or_bt="bt")
            sess["_run_analysis_requested"] = True
            sess["auto_analysis_fw"] = r"C:\fake\fw\fw.bin"
        stamps = {BT_OLD: datetime(2026, 4, 13, 9, 0), BT_NEW: datetime(2026, 4, 14, 9, 0)}

        # Act
        with patch("blueprints.log_parser.log_parser_routes._parse_path_timestamp",
                   side_effect=stamps.get), \
             patch(f"{ROUTES}.os.path.isfile", return_value=False):
            self.client.get("/download_result")

        # Assert
        kw = self.render_kwargs()
        self.assertEqual(kw["auto_analysis_bt"], BT_NEW)
        self.assertEqual(kw["auto_analysis_fw"], r"C:\fake\fw\fw.bin")
        self.assertNotIn("auto_analysis_fw", self.get_session())
        self.assertEqual([r["bt_path"] for r in kw["bt_table_rows"]], [BT_OLD, BT_NEW])


# --------------------------------------------------------------------------
# _build_merged_table_rows (core logic for the result tables)
# --------------------------------------------------------------------------
class TestBuildTableRows(MainTestBase):

    def test_rows_carry_zip_rowspan_and_filters(self):
        """_build_wifi_table_rows carries zip rowspans and filters non-ETLs.

        Input: a dict mapping zips to ETL/.log paths (one zip with only a .log).
        Expected: only ETL rows are emitted, with correct rowspan/show-cell flags
        and the empty zip dropped.
        """
        # Act
        rows = main_routes._build_wifi_table_rows(
            {"a.zip": [WIFI_ETL_1, WIFI_LOG, WIFI_ETL_3], "empty.zip": [WIFI_LOG]})

        # Assert
        self.assertEqual(rows, [
            {"zip_name": "a.zip", "folder_name": "", "zip_rowspan": 2, "folder_rowspan": 2,
             "show_zip_cell": True, "show_folder_cell": True, "etl_path": WIFI_ETL_1},
            {"zip_name": "a.zip", "folder_name": "", "zip_rowspan": 2, "folder_rowspan": 2,
             "show_zip_cell": False, "show_folder_cell": False, "etl_path": WIFI_ETL_3},
        ])

    def test_event_rows_keep_only_system_event_logs(self):
        """_build_event_table_rows keeps only system event logs.

        Input: a dict with an .evtx and an unrelated .etl.
        Expected: only the .evtx path is kept.
        """
        # Act
        rows = main_routes._build_event_table_rows({"a.zip": [EVT_PATH, r"C:\fake\x.etl"]})

        # Assert
        self.assertEqual([r["ddd_path"] for r in rows], [EVT_PATH])


# --------------------------------------------------------------------------
# _prime_issue_ai_cache (core logic for the LLM pre-pass)
# --------------------------------------------------------------------------
class TestPrimeIssueAiCache(MainTestBase):

    def setUp(self):
        super().setUp()
        self.app_config.llm_helper.client = "llm-client"
        self.app_config.llm_helper.model = "gpt-test"

    @patch(f"{ROUTES}.gather_service")
    @patch(f"{ISSUE_TIME_AI}.organize_issue_context")
    def test_description_is_organized_and_usage_recorded(self, mock_organize, mock_gather):
        """_prime_issue_ai_cache organizes the description and records usage.

        Input: _prime_issue_ai_cache for a case with a dated description and a
        workflow id.
        Expected: organize_issue_context runs with the LLM client/model, the
        result is cached in session, and gather records an "issue_time_prepass"
        usage carrying the call/token counts.
        """
        # Arrange
        data = {"clean_description": "drops after resume", "issue_times": ["01:26"]}
        mock_organize.return_value = (data, {"llm_calls": 1, "total_tokens": 42})
        ctx = CaseContext(case_nbr=TEST_CASE_NBR, description="Drops at 01:26", wifi_or_bt="wifi")

        # Act
        with self.app.test_request_context("/"):
            from flask import session
            session["gather_workflow_id"] = WORKFLOW_ID
            main_routes._prime_issue_ai_cache(ctx)
            cached = session["_issue_ai_quick"]

        # Assert
        self.assertEqual(cached, {"data": data})
        mock_organize.assert_called_once_with(
            "Drops at 01:26", first_ts=None, last_ts=None,
            llm_client="llm-client", llm_model="gpt-test", return_usage=True)
        mock_gather.record_feature_usage.assert_called_once()
        kw = mock_gather.record_feature_usage.call_args.kwargs
        self.assertEqual(kw["feature_code"], "issue_time_prepass")
        self.assertEqual(kw["workflow_id"], WORKFLOW_ID)
        self.assertEqual(kw["usage"]["llm_calls"], 1)
        self.assertEqual(kw["usage"]["total_tokens"], 42)

    @patch(f"{ROUTES}.gather_service")
    @patch(f"{ISSUE_TIME_AI}.organize_issue_context")
    def test_empty_description_falls_back_to_attachment_subtitles(self, mock_organize, mock_gather):
        """_prime_issue_ai_cache falls back to attachment subtitles.

        Input: _prime_issue_ai_cache for a case with a blank description but an
        attachment subtitle.
        Expected: organize is called on the subtitle text; usage is not recorded
        when there were no LLM calls.
        """
        # Arrange
        mock_organize.return_value = ({"issue_times": []}, {"llm_calls": 0})
        ctx = CaseContext(case_nbr=TEST_CASE_NBR, description="  ",
                          attachment_list=[ZIP_ITEM, TXT_ITEM, ["short"]])

        # Act
        with self.app.test_request_context("/"):
            main_routes._prime_issue_ai_cache(ctx)

        # Assert
        self.assertEqual(mock_organize.call_args.args[0], "uploaded by Partner")
        mock_gather.record_feature_usage.assert_not_called()  # llm_calls == 0

    @patch(f"{ROUTES}.gather_service")
    @patch(f"{ISSUE_TIME_AI}.organize_issue_context")
    def test_no_text_anywhere_bails_without_llm_call(self, mock_organize, mock_gather):
        """_prime_issue_ai_cache bails without any usable text.

        Input: _prime_issue_ai_cache for a case with no description and only a
        short attachment note.
        Expected: no cache entry is written, organize is not called and no usage
        is recorded.
        """
        # Arrange
        ctx = CaseContext(case_nbr=TEST_CASE_NBR, description="", attachment_list=[TXT_ITEM])

        # Act
        with self.app.test_request_context("/"):
            from flask import session
            main_routes._prime_issue_ai_cache(ctx)
            self.assertNotIn("_issue_ai_quick", session)

        # Assert
        mock_organize.assert_not_called()
        mock_gather.record_feature_usage.assert_not_called()


# --------------------------------------------------------------------------
# /download_result_bsod
# --------------------------------------------------------------------------
class TestDownloadResultBsod(MainTestBase):

    def test_without_session_redirects_to_index(self):
        """download_result_bsod without a session redirects home.

        Input: GET /download_result_bsod with no case context.
        Expected: 302 to "/".
        """
        # Act
        resp = self.client.get("/download_result_bsod")

        # Assert
        self.assert_redirects_to(resp, "/")

    @patch(f"{ROUTES}.helpers.get_load_path", return_value=None)
    def test_unreachable_share_redirects_to_index(self, mock_load):
        """download_result_bsod with an unreachable share redirects home.

        Input: GET /download_result_bsod where the share is unavailable.
        Expected: 302 to "/" with a "Session expired" warning flash.
        """
        # Arrange
        self.seed_session()

        # Act
        resp = self.client.get("/download_result_bsod")

        # Assert
        self.assert_redirects_to(resp, "/")
        self.assertEqual(self.flashes(), [("warning", "Session expired. Please start again.")])

    @patch(f"{ROUTES}.helpers.detect_user_email", return_value="tester@example.com")
    @patch(f"{ROUTES}.helpers.get_load_path", return_value=r"\\share\BSOD_Dumps")
    @patch(f"{ROUTES}.render_template", return_value="bsod-page")
    def test_renders_bsod_page(self, mock_render, mock_load, mock_email):
        """download_result_bsod renders the BSOD page.

        Input: GET /download_result_bsod with a reachable share and a detected
        email.
        Expected: 200; bsod.html is rendered with the case number, email and the
        share path joined with the case number.
        """
        # Arrange
        self.seed_session()

        # Act
        resp = self.client.get("/download_result_bsod")

        # Assert
        self.assertEqual(resp.status_code, 200)
        mock_render.assert_called_once_with(
            "bsod.html", case_nbr=TEST_CASE_NBR, email="tester@example.com",
            case_path=r"\\share\BSOD_Dumps\00123456")


# --------------------------------------------------------------------------
# /open_path
# --------------------------------------------------------------------------
class TestOpenPath(MainTestBase):

    @patch(f"{ROUTES}.subprocess.run")
    @patch(f"{ROUTES}.os.path.exists", return_value=True)
    def test_existing_path_opens_explorer(self, mock_exists, mock_run):
        """open_path opens Explorer for an existing path.

        Input: POST /open_path for an existing folder.
        Expected: 204; existence is checked and Explorer is launched on the path.
        """
        # Act
        resp = self.client.post("/open_path", json={"path": r"C:\fake\folder"})

        # Assert
        self.assertEqual(resp.status_code, 204)
        mock_exists.assert_called_once_with(r"C:\fake\folder")
        mock_run.assert_called_once_with(["explorer", r"C:\fake\folder"])

    @patch(f"{ROUTES}.subprocess.run")
    @patch(f"{ROUTES}.os.path.exists", return_value=False)
    def test_missing_or_empty_path_returns_400(self, mock_exists, mock_run):
        """open_path returns 400 for a missing or empty path.

        Input: POST /open_path for a non-existent path, then with a non-JSON body.
        Expected: both return 400 ("Invalid path") and Explorer is not launched.
        """
        # Act
        resp_missing = self.client.post("/open_path", json={"path": r"C:\nope"})
        resp_empty = self.client.post("/open_path", data="not json")

        # Assert
        self.assertEqual(resp_missing.status_code, 400)
        self.assertEqual(resp_missing.get_data(as_text=True), "Invalid path")
        self.assertEqual(resp_empty.status_code, 400)
        mock_run.assert_not_called()

    @patch(f"{ROUTES}.subprocess.run")
    @patch(f"{ROUTES}.os.path.exists", return_value=True)
    def test_open_path_ignores_silent_mode(self, mock_exists, mock_run):
        """open_path ignores silent mode (explicit user action).

        Input: POST /open_path with app_config.silent_mode True.
        Expected: 204; Explorer still launches because silent mode only gates
        automatic launches.
        """
        # Branch-specific: silent_mode only gates AUTOMATIC GUI launches;
        # this explicit user action still opens explorer.
        # Arrange
        self.app_config.silent_mode = True

        # Act
        resp = self.client.post("/open_path", json={"path": r"C:\fake\folder"})

        # Assert
        self.assertEqual(resp.status_code, 204)
        mock_run.assert_called_once_with(["explorer", r"C:\fake\folder"])


# --------------------------------------------------------------------------
# /dump_event_txt and /parse_event_log (lazy services.event_log_service)
# --------------------------------------------------------------------------
class EventLogTestBase(MainTestBase):

    def use_event_service(self, service):
        """Make ``from services import event_log_service`` return ``service``
        (or raise ImportError when ``service`` is None)."""
        p_mod = patch.dict(sys.modules, {"services.event_log_service": service})
        p_mod.start()
        self.addCleanup(p_mod.stop)
        had_attr = hasattr(services, "event_log_service")
        original = getattr(services, "event_log_service", None)
        if service is None:
            if had_attr:
                delattr(services, "event_log_service")
        else:
            services.event_log_service = service

        def restore():
            if had_attr:
                services.event_log_service = original
            elif hasattr(services, "event_log_service"):
                delattr(services, "event_log_service")
        self.addCleanup(restore)


class TestDumpEventTxt(EventLogTestBase):

    def test_import_error_returns_503(self):
        """dump_event_txt returns 503 when the event service is unavailable.

        Input: POST /dump_event_txt when importing event_log_service raises.
        Expected: 503 with an "Event log feature unavailable" error.
        """
        # Arrange
        self.use_event_service(None)

        # Act
        resp = self.client.post("/dump_event_txt", json={"path": EVT_PATH})

        # Assert
        self.assertEqual(resp.status_code, 503)
        self.assertIn("Event log feature unavailable", resp.get_json()["error"])

    @patch(f"{ROUTES}.os.path.exists", return_value=False)
    def test_invalid_path_returns_400(self, mock_exists):
        """dump_event_txt returns 400 for an invalid path.

        Input: POST /dump_event_txt where the path does not exist.
        Expected: 400 with "Invalid path"; the service is not called.
        """
        # Arrange
        svc = MagicMock()
        self.use_event_service(svc)

        # Act
        resp = self.client.post("/dump_event_txt", json={"path": EVT_PATH})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {"error": "Invalid path"})
        svc.dump_event_to_txt.assert_not_called()

    @patch(f"{ROUTES}.os.path.exists", return_value=True)
    def test_success_returns_txt_path(self, mock_exists):
        """dump_event_txt returns the generated txt path.

        Input: POST /dump_event_txt for an existing evtx where the service dumps
        it.
        Expected: 200 with {success, txt_path}; the service is called with the
        evtx path.
        """
        # Arrange
        svc = MagicMock()
        svc.dump_event_to_txt.return_value = (r"C:\fake\events\System.txt", 12)
        self.use_event_service(svc)

        # Act
        resp = self.client.post("/dump_event_txt", json={"path": EVT_PATH})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"success": True, "txt_path": r"C:\fake\events\System.txt"})
        svc.dump_event_to_txt.assert_called_once_with(EVT_PATH)

    @patch(f"{ROUTES}.os.path.exists", return_value=True)
    def test_service_exception_returns_500(self, mock_exists):
        """dump_event_txt returns 500 on a service exception.

        Input: POST /dump_event_txt where the dump raises ValueError.
        Expected: 500 echoing the error.
        """
        # Arrange
        svc = MagicMock()
        svc.dump_event_to_txt.side_effect = ValueError("corrupt evtx")
        self.use_event_service(svc)

        # Act
        resp = self.client.post("/dump_event_txt", json={"path": EVT_PATH})

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"error": "corrupt evtx"})


class TestParseEventLog(EventLogTestBase):

    def test_import_error_returns_503(self):
        """parse_event_log returns 503 when the event service is unavailable.

        Input: POST /parse_event_log when importing event_log_service raises.
        Expected: 503.
        """
        # Arrange
        self.use_event_service(None)

        # Act
        resp = self.client.post("/parse_event_log", json={"path": EVT_PATH})

        # Assert
        self.assertEqual(resp.status_code, 503)

    @patch(f"{ROUTES}.os.path.exists", return_value=False)
    def test_invalid_path_returns_400(self, mock_exists):
        """parse_event_log returns 400 for an invalid path.

        Input: POST /parse_event_log with an empty path.
        Expected: 400 with "Invalid path"; the service is not called.
        """
        # Arrange
        svc = MagicMock()
        self.use_event_service(svc)

        # Act
        resp = self.client.post("/parse_event_log", json={"path": ""})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {"error": "Invalid path"})
        svc.get_paged_events.assert_not_called()

    @patch(f"{ROUTES}.os.path.exists", return_value=True)
    def test_success_passes_clamped_paging_and_filters(self, mock_exists):
        """parse_event_log passes clamped paging and filters to the service.

        Input: POST /parse_event_log with a negative offset plus source/level
        filters.
        Expected: 200 echoing the events; get_paged_events is called with the
        offset clamped to 0 and the given limit/filters.
        """
        # Arrange
        svc = MagicMock()
        svc.get_paged_events.return_value = {"events": [{"id": 1}], "total": 1}
        self.use_event_service(svc)

        # Act
        resp = self.client.post("/parse_event_log", json={
            "path": EVT_PATH, "offset": -5, "limit": 50,
            "source_filter": "Netwtw", "level_filter": "error"})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"events": [{"id": 1}], "total": 1})
        svc.get_paged_events.assert_called_once_with(EVT_PATH, 0, 50, "Netwtw", "error")

    @patch(f"{ROUTES}.os.path.exists", return_value=True)
    def test_defaults_and_service_exception(self, mock_exists):
        """parse_event_log uses defaults and surfaces a service exception.

        Input: POST /parse_event_log with only a path where the service raises.
        Expected: 500 echoing the error; get_paged_events is called with the
        default paging/filters (0, 0, "all", "all").
        """
        # Arrange
        svc = MagicMock()
        svc.get_paged_events.side_effect = RuntimeError("parse failed")
        self.use_event_service(svc)

        # Act
        resp = self.client.post("/parse_event_log", json={"path": EVT_PATH})

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"error": "parse failed"})
        svc.get_paged_events.assert_called_once_with(EVT_PATH, 0, 0, "all", "all")


# --------------------------------------------------------------------------
# /api/bt_event_map
# --------------------------------------------------------------------------
class TestBtEventMap(MainTestBase):

    @patch(f"{ROUTES}.open", new_callable=mock_open, read_data=json.dumps({"0x01": "Connect"}), create=True)
    @patch(f"{ROUTES}.os.path.exists", return_value=True)
    def test_returns_json_map(self, mock_exists, mock_file):
        """bt_event_map returns the JSON map from disk.

        Input: GET /api/bt_event_map where the map file exists with JSON.
        Expected: 200 echoing the parsed map; the opened path ends with
        bt_event_id_map.json.
        """
        # Act
        resp = self.client.get("/api/bt_event_map")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"0x01": "Connect"})
        opened_path = mock_file.call_args.args[0]
        self.assertTrue(opened_path.endswith("bt_event_id_map.json"))

    @patch(f"{ROUTES}.open", create=True)
    @patch(f"{ROUTES}.os.path.exists", return_value=False)
    def test_missing_file_returns_empty_object(self, mock_exists, mock_file):
        """bt_event_map returns an empty object when the file is missing.

        Input: GET /api/bt_event_map where the map file does not exist.
        Expected: 200 with {}; the file is never opened.
        """
        # Act
        resp = self.client.get("/api/bt_event_map")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {})
        mock_file.assert_not_called()

    @patch(f"{ROUTES}.open", new_callable=mock_open, read_data="{not json", create=True)
    @patch(f"{ROUTES}.os.path.exists", return_value=True)
    def test_malformed_json_returns_empty_object(self, mock_exists, mock_file):
        """bt_event_map returns an empty object for malformed JSON.

        Input: GET /api/bt_event_map where the file contains invalid JSON.
        Expected: 200 with {}.
        """
        # Act
        resp = self.client.get("/api/bt_event_map")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {})


if __name__ == "__main__":
    unittest.main()
