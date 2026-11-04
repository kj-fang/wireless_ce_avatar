"""Unit tests for the log_parser blueprint (blueprints/log_parser/log_parser_routes.py).

Covers the 13 HTTP routes, the ``before_app_request`` SendTo session pickup
hook, the SocketIO handler functions (called directly), the SendTo
background worker (called synchronously) and the pure helper functions.

Everything non-deterministic is patched at its point of use in the route
module: ``log_parser_service`` / ``file_manager_service`` / ``gather_service``
singletons, ``helpers``, ``os.path`` checks, ``datetime``, the ETL parsers
(``wpp_ddd_parser_run`` / ``bt_decode_via_cli``), tkinter, threading.Thread
and ``render_template``. No parser, network share, GUI or thread really runs.

Branch-specific note: this module has NO direct ``app_config.silent_mode``
check. On this branch the GUI-open gating lives one layer down, inside
``wpp_ddd_parser_run`` (services/etl_parser/wpp_ddd_parser.py) and
``bt_decode_via_cli`` (services/etl_parser/bt_parser.py). Both are patched
here, so silent_mode has no observable effect at the blueprint layer.
"""
import os
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from flask import Blueprint, request, session

import blueprints.log_parser.log_parser_routes as lp
from blueprints.log_parser.log_parser_routes import log_parser_bp
from configs.path_configs import LOAD_PATH_bkup, LOAD_PATH_prim, LOG_PARSER_DIR
from tests.base_test import BaseTestCase

ROUTES = "blueprints.log_parser.log_parser_routes"

SRC_DIR = r"C:\fake\local"
TIMESTAMP = "20260101_120000"
FILTER_DIR = os.path.join(LOG_PARSER_DIR, "filter")
NON_LOCAL = {"REMOTE_ADDR": "10.0.0.5"}


def _long(path):
    return "\\\\?\\" + path


class LogParserTestCase(BaseTestCase):
    """Registers log_parser_bp plus stub 'main', 'log_chatbot', 'bt_chatbot'
    blueprints for the url_for targets used by the routes."""

    blueprint = log_parser_bp
    main_stub_endpoints = ("index", "download_result", "download_result_bsod")

    def setUp(self):
        super().setUp()
        for name in ("log_chatbot", "bt_chatbot"):
            stub = Blueprint(name, __name__)
            stub.add_url_rule(f"/__{name}_stub__/index", endpoint="index",
                              view_func=lambda name=name: f"stub:{name}")
            self.app.register_blueprint(stub)

    def _flashes(self):
        return self.get_session().get("_flashes", [])


# ════════════════════════════════════════════════════════════════════ structure
class TestLogParserBlueprintStructure(LogParserTestCase):

    def test_blueprint_registered_with_url_prefix(self):
        """Verify blueprint registration and all 13 routes with methods.

        Input: the Flask app with ``log_parser_bp`` registered (via setUp).
        Expected: the blueprint is mounted under ``/log_parser`` and exposes the
        13 expected routes, each supporting its listed HTTP methods.
        """
        # Arrange / Act
        rules = {r.rule: r for r in self.app.url_map.iter_rules() if r.endpoint.startswith("log_parser.")}

        # Assert
        self.assertIs(self.app.blueprints.get("log_parser"), log_parser_bp)
        self.assertEqual(log_parser_bp.url_prefix, "/log_parser")
        expected = {
            "/log_parser/log_parser": {"GET", "POST"},
            "/log_parser/load_prompt": {"POST"},
            "/log_parser/upload": {"POST"},
            "/log_parser/pick_local_analysis_file": {"POST"},
            "/log_parser/upload_local_analysis": {"POST"},
            "/log_parser/cancel_local_analysis": {"POST"},
            "/log_parser/open_local_analysis": {"GET"},
            "/log_parser/navigate_existing_browser": {"POST"},
            "/log_parser/verify_sendto_token": {"GET"},
            "/log_parser/get_filter_details": {"POST"},
            "/log_parser/get_all_filter_details": {"POST"},
            "/log_parser/edit_prompt": {"POST"},
            "/log_parser/estimate_tokens": {"POST"},
            "/log_parser/find_best_log": {"POST"},
        }
        self.assertEqual(set(rules), set(expected))
        for rule, methods in expected.items():
            self.assertTrue(methods.issubset(rules[rule].methods), rule)

    def test_register_socketio_handlers_binds_all_events(self):
        """The SocketIO registration binds every handler event.

        Input: a fake socketio whose on() records (event, namespace) pairs.
        Expected: the handlers are bound in order across the wpp/progress/sendto
        namespaces (wpp_events, submit_analysis, chat_message,
        chat_message_with_filter, reset_log_parser_session, start_sendto).
        """
        # Arrange
        fake_socketio = MagicMock()
        registered = []
        fake_socketio.on.side_effect = lambda event, namespace=None: (
            lambda fn: registered.append((event, namespace)) or fn)

        # Act
        lp.register_socketio_handlers(fake_socketio)

        # Assert
        self.assertEqual(registered, [
            ("wpp_events", "/wpp_progress"),
            ("submit_analysis", "/progress"),
            ("chat_message", "/progress"),
            ("chat_message_with_filter", "/progress"),
            ("reset_log_parser_session", "/progress"),
            ("start_sendto", "/sendto-progress"),
        ])


# ═══════════════════════════════════════════════════════════════ /log_parser
@patch(f"{ROUTES}.render_template", return_value="rendered")
@patch(f"{ROUTES}.log_parser_service")
class TestLogParserForm(LogParserTestCase):

    def _configure_service(self, svc):
        svc.set_up.return_value = r"C:\fake\out"
        svc.check_auto_analysis_availability.return_value = (False, None)
        svc.get_available_resources.return_value = (["wifi.tat"], (["p.txt"], ["c.txt"]))
        svc.analysis_result = {"llm_result_html": "<p>x</p>", "log_output_path": r"C:\fake\out\r.txt"}
        svc.prepare_log_file.return_value = r"C:\fake\out\wifi.etl.log"

    def test_renders_form_with_prepared_log(self, mock_svc, mock_render):
        """The form prepares a log from an ETL path and renders the page.

        Input: GET /log_parser?etl_path=... with a seeded download path.
        Expected: 200; the service is set up on the download dir, the log is
        prepared, auto-analysis availability is checked, and log_parser.html is
        rendered with filters/prompts/result; the session stores the log path and
        output dir.
        """
        # Arrange
        self._configure_service(mock_svc)
        self.seed_session(download_path=r"C:\fake\dl")

        # Act
        resp = self.client.get("/log_parser/log_parser?etl_path=C%3A%5Cfake%5Cdl%5Cwifi.etl")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(as_text=True), "rendered")
        mock_svc.set_up.assert_called_once_with(r"C:\fake\dl")
        mock_svc.prepare_log_file.assert_called_once_with(r"C:\fake\dl\wifi.etl", r"C:\fake\out")
        mock_svc.check_auto_analysis_availability.assert_called_once_with(
            {"issue_type": "Unclassified", "confidence": 0, "keywords_found": []})
        args, kwargs = mock_render.call_args
        self.assertEqual(args, ("log_parser.html",))
        self.assertEqual(kwargs["log_path"], r"C:\fake\out\wifi.etl.log")
        self.assertEqual(kwargs["available_filters"], ["wifi.tat"])
        self.assertEqual(kwargs["available_prompts"], ["p.txt"])
        self.assertEqual(kwargs["available_custom_prompts"], ["c.txt"])
        self.assertEqual(kwargs["llm_result_html"], "<p>x</p>")
        self.assertEqual(kwargs["auto_analysis_data"], "{}")
        self.assertIs(kwargs["should_auto_analyze"], False)
        sess = self.get_session()
        self.assertEqual(sess["log_path"], r"C:\fake\out\wifi.etl.log")
        self.assertEqual(sess["logparser_output_dir"], r"C:\fake\out")

    def test_preselect_filter_and_prompt_force_auto_analysis(self, mock_svc, mock_render):
        """Preselected filter and prompt force auto-analysis.

        Input: GET /log_parser?preselect_filter=bt.tat&preselect_prompt=bt.txt.
        Expected: the template receives should_auto_analyze=True and
        auto_analysis_data describing the bt filter/prompt/issue type.
        """
        # Arrange
        self._configure_service(mock_svc)
        self.seed_session(download_path=r"C:\fake\dl")

        # Act
        self.client.get("/log_parser/log_parser?preselect_filter=bt.tat&preselect_prompt=bt.txt")

        # Assert
        kwargs = mock_render.call_args.kwargs
        self.assertIs(kwargs["should_auto_analyze"], True)
        self.assertEqual(kwargs["auto_analysis_data"],
                         {"filter_file": "bt.tat", "prompt_file": "bt.txt", "issue_type": "bt_hci"})

    @patch(f"{ROUTES}.shutil.copy2")
    @patch(f"{ROUTES}.os.path.exists", return_value=True)
    def test_direct_log_file_is_copied_into_output_dir(self, mock_exists, mock_copy, mock_svc, mock_render):
        """A direct log file is copied into the output dir, skipping prepare.

        Input: GET /log_parser with a session latest_etl_path pointing at an
        existing .hci.txt.
        Expected: the file is copied into the output dir, prepare_log_file is not
        called, and the template log_path points at the copied file.
        """
        # Arrange
        self._configure_service(mock_svc)
        self.seed_session(download_path=r"C:\fake\dl", latest_etl_path=r"C:\fake\dl\bt.hci.txt")

        # Act
        self.client.get("/log_parser/log_parser")

        # Assert
        expected = os.path.join(r"C:\fake\out", "bt.hci.txt")
        mock_copy.assert_called_once_with(r"C:\fake\dl\bt.hci.txt", expected)
        mock_svc.prepare_log_file.assert_not_called()
        self.assertEqual(mock_render.call_args.kwargs["log_path"], expected)

    @patch(f"{ROUTES}.os.path.exists", return_value=True)
    def test_local_in_place_uses_source_dir_and_existing_log(self, mock_exists, mock_svc, mock_render):
        """Local in-place mode uses the source dir and existing log.

        Input: GET /log_parser with a local_in_place session carrying an uploaded
        source path.
        Expected: the service is not set up or re-prepared; the template uses the
        existing "<etl>.log" and the output dir is the source dir.
        """
        # Arrange
        self._configure_service(mock_svc)
        self.seed_session(download_path=SRC_DIR, local_in_place=True,
                          uploaded_source_path=os.path.join(SRC_DIR, "wifi.etl"),
                          latest_etl_path=os.path.join(SRC_DIR, "wifi.etl"))

        # Act
        self.client.get("/log_parser/log_parser")

        # Assert
        mock_svc.set_up.assert_not_called()
        mock_svc.prepare_log_file.assert_not_called()
        self.assertEqual(mock_render.call_args.kwargs["log_path"], os.path.join(SRC_DIR, "wifi.etl.log"))
        self.assertEqual(self.get_session()["logparser_output_dir"], SRC_DIR)

    def test_stale_local_in_place_flag_is_reset(self, mock_svc, mock_render):
        """A stale local_in_place flag is reset.

        Input: GET /log_parser with local_in_place set but no uploaded source
        path (stale).
        Expected: the flag is reset to False and the service is set up normally on
        the download dir.
        """
        # Arrange: local_in_place without uploaded_source_path is stale
        self._configure_service(mock_svc)
        self.seed_session(download_path=r"C:\fake\dl", local_in_place=True)

        # Act
        self.client.get("/log_parser/log_parser")

        # Assert
        self.assertIs(self.get_session()["local_in_place"], False)
        mock_svc.set_up.assert_called_once_with(r"C:\fake\dl")


# ═══════════════════════════════════════════════ simple service-delegate routes
class TestLogParserServiceDelegates(LogParserTestCase):

    @patch(f"{ROUTES}.log_parser_service")
    def test_load_prompt_returns_service_content(self, mock_svc):
        """load_prompt returns the service's prompt content.

        Input: POST /load_prompt with a prompt type and file.
        Expected: 200 with {content}; load_prompt_content is called with the type
        and file.
        """
        # Arrange
        mock_svc.load_prompt_content.return_value = "PROMPT BODY"

        # Act
        resp = self.client.post("/log_parser/load_prompt", json={"type": "custom", "file": "a.txt"})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"content": "PROMPT BODY"})
        mock_svc.load_prompt_content.assert_called_once_with("custom", "a.txt")

    @patch(f"{ROUTES}.file_manager_service")
    def test_upload_resets_local_in_place_and_delegates(self, mock_fm):
        """upload resets local_in_place and delegates to the file manager.

        Input: POST /upload with a filter type while local_in_place is set.
        Expected: the upload is delegated to handle_file_upload and the session's
        local_in_place is reset to False.
        """
        # Arrange
        mock_fm.handle_file_upload.return_value = {"success": True, "message": "uploaded"}
        self.seed_session(local_in_place=True)

        # Act
        resp = self.client.post("/log_parser/upload", data={"type": "filter"})

        # Assert
        self.assertEqual(resp.get_json(), {"success": True, "message": "uploaded"})
        self.assertEqual(mock_fm.handle_file_upload.call_args.args[0], "filter")
        self.assertIs(self.get_session()["local_in_place"], False)

    @patch(f"{ROUTES}.file_manager_service")
    def test_edit_prompt_requires_action(self, mock_fm):
        """edit_prompt requires an action.

        Input: POST /edit_prompt with no action.
        Expected: {success: False, "Action is required"}; the file manager is not
        called.
        """
        # Act
        resp = self.client.post("/log_parser/edit_prompt", json={"filename": "a.txt"})

        # Assert
        self.assertEqual(resp.get_json(), {"success": False, "message": "Action is required"})
        mock_fm.handle_prompt_operation.assert_not_called()

    @patch(f"{ROUTES}.file_manager_service")
    def test_edit_prompt_delegates_to_file_manager(self, mock_fm):
        """edit_prompt delegates to the file manager.

        Input: POST /edit_prompt with action/filename/content.
        Expected: handle_prompt_operation is called with those args and its result
        is returned.
        """
        # Arrange
        mock_fm.handle_prompt_operation.return_value = {"success": True, "message": "saved"}

        # Act
        resp = self.client.post("/log_parser/edit_prompt",
                                json={"action": "save", "filename": "a.txt", "content": "hello"})

        # Assert
        self.assertEqual(resp.get_json(), {"success": True, "message": "saved"})
        mock_fm.handle_prompt_operation.assert_called_once_with("save", "a.txt", "hello")


# ═══════════════════════════════════════════════════ /pick_local_analysis_file
class TestPickLocalAnalysisFile(LogParserTestCase):

    def _fake_tk(self, selected):
        fake_tk = MagicMock(name="tkinter")
        fake_tk.filedialog.askopenfilename.return_value = selected
        return fake_tk

    def test_selected_file_is_bound_to_cleared_session(self):
        """A picked file is bound to a freshly cleared session.

        Input: POST /pick_local_analysis_file where the native dialog returns a
        .zip; the session had a stale key.
        Expected: 200 with {source_path, filename}; the session is cleared to hold
        only the picked path and the hidden Tk root is withdrawn/destroyed.
        """
        # Arrange
        fake_tk = self._fake_tk(os.path.join(SRC_DIR, "wifi.zip"))
        self.seed_session(stale_key="old")

        # Act
        with patch.dict("sys.modules", {"tkinter": fake_tk, "tkinter.filedialog": fake_tk.filedialog}):
            resp = self.client.post("/log_parser/pick_local_analysis_file")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"success": True,
                                           "source_path": os.path.join(SRC_DIR, "wifi.zip"),
                                           "filename": "wifi.zip"})
        sess = self.get_session()
        self.assertEqual(sess, {"picked_local_analysis_path": os.path.join(SRC_DIR, "wifi.zip")})
        fake_tk.Tk.return_value.withdraw.assert_called_once_with()
        fake_tk.Tk.return_value.destroy.assert_called()

    def test_no_selection_returns_400(self):
        """Cancelling the picker returns 400.

        Input: POST /pick_local_analysis_file where the dialog returns "".
        Expected: 400 with "No file selected".
        """
        # Arrange
        fake_tk = self._fake_tk("")

        # Act
        with patch.dict("sys.modules", {"tkinter": fake_tk, "tkinter.filedialog": fake_tk.filedialog}):
            resp = self.client.post("/log_parser/pick_local_analysis_file")

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {"success": False, "message": "No file selected"})

    def test_invalid_extension_returns_400(self):
        """Picking a disallowed extension returns 400.

        Input: POST /pick_local_analysis_file where the dialog returns a .docx.
        Expected: 400 with an "Invalid file type" message; no path is bound.
        """
        # Arrange
        fake_tk = self._fake_tk(os.path.join(SRC_DIR, "notes.docx"))

        # Act
        with patch.dict("sys.modules", {"tkinter": fake_tk, "tkinter.filedialog": fake_tk.filedialog}):
            resp = self.client.post("/log_parser/pick_local_analysis_file")

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Invalid file type: notes.docx", resp.get_json()["message"])
        self.assertNotIn("picked_local_analysis_path", self.get_session())

    def test_dialog_exception_returns_500(self):
        """A dialog exception returns 500 and still cleans up.

        Input: POST /pick_local_analysis_file where the dialog raises.
        Expected: 500 with "Native file dialog unavailable: ..."; the Tk root is
        destroyed in the finally block.
        """
        # Arrange
        fake_tk = self._fake_tk(None)
        fake_tk.filedialog.askopenfilename.side_effect = RuntimeError("no display")

        # Act
        with patch.dict("sys.modules", {"tkinter": fake_tk, "tkinter.filedialog": fake_tk.filedialog}):
            resp = self.client.post("/log_parser/pick_local_analysis_file")

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json()["message"], "Native file dialog unavailable: no display")
        fake_tk.Tk.return_value.destroy.assert_called_once_with()  # finally-block cleanup

    def test_tkinter_unavailable_returns_503(self):
        """A missing tkinter returns 503.

        Input: POST /pick_local_analysis_file where importing tkinter raises
        ImportError.
        Expected: 503 with "Native file picker is unavailable in this
        environment.".
        """
        # Arrange: a None entry in sys.modules makes `import tkinter` raise ImportError
        # Act
        with patch.dict("sys.modules", {"tkinter": None}):
            resp = self.client.post("/log_parser/pick_local_analysis_file")

        # Assert
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()["message"], "Native file picker is unavailable in this environment.")


# ═══════════════════════════════════════════════════════ /upload_local_analysis
class TestUploadLocalAnalysisValidation(LogParserTestCase):

    def _post(self, source_path, picked=None):
        if picked is not None:
            with self.client.session_transaction() as sess:
                sess["picked_local_analysis_path"] = picked
        return self.client.post("/log_parser/upload_local_analysis", data={"source_path": source_path})

    def test_missing_source_path_returns_400(self):
        """A missing source_path returns 400.

        Input: POST /upload_local_analysis with an empty source_path.
        Expected: 400 asking to use the native picker.
        """
        # Act
        resp = self._post("", picked=os.path.join(SRC_DIR, "a.log"))

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Please use native picker", resp.get_json()["message"])

    def test_no_picked_path_in_session_returns_400(self):
        """A session with no picked path returns 400.

        Input: POST /upload_local_analysis with a source_path but no picked path
        in session.
        Expected: 400 noting no path is bound to the session.
        """
        # Act
        resp = self._post(os.path.join(SRC_DIR, "a.log"))

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertIn("No path is bound to this session", resp.get_json()["message"])

    def test_source_path_mismatch_returns_400(self):
        """A source_path not matching the session's picked path returns 400.

        Input: POST /upload_local_analysis with a source_path different from the
        session's picked path.
        Expected: 400 with "Invalid source path for this session".
        """
        # Act
        resp = self._post(os.path.join(SRC_DIR, "other.log"), picked=os.path.join(SRC_DIR, "a.log"))

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Invalid source path for this session", resp.get_json()["message"])

    @patch(f"{ROUTES}.os.path.exists", return_value=False)
    def test_missing_file_returns_400(self, mock_exists):
        """A picked file missing on disk returns 400.

        Input: POST /upload_local_analysis where os.path.exists is False.
        Expected: 400 with "Source file not found: ...".
        """
        # Act
        resp = self._post(os.path.join(SRC_DIR, "a.log"), picked=os.path.join(SRC_DIR, "a.log"))

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["message"], f"Source file not found: {os.path.join(SRC_DIR, 'a.log')}")

    @patch(f"{ROUTES}.os.path.isdir", return_value=True)
    @patch(f"{ROUTES}.os.path.exists", return_value=True)
    def test_directory_returns_400(self, mock_exists, mock_isdir):
        """Picking a directory returns 400.

        Input: POST /upload_local_analysis where the picked path is a directory.
        Expected: 400 with "Selected path is a folder".
        """
        # Act
        resp = self._post(os.path.join(SRC_DIR, "folder.zip"), picked=os.path.join(SRC_DIR, "folder.zip"))

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Selected path is a folder", resp.get_json()["message"])

    @patch(f"{ROUTES}.os.path.isdir", return_value=False)
    @patch(f"{ROUTES}.os.path.exists", return_value=True)
    def test_disallowed_extension_returns_400(self, mock_exists, mock_isdir):
        """A disallowed extension returns 400.

        Input: POST /upload_local_analysis with a .docx picked path.
        Expected: 400 with an "Invalid file type" message.
        """
        # Act
        resp = self._post(os.path.join(SRC_DIR, "notes.docx"), picked=os.path.join(SRC_DIR, "notes.docx"))

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Invalid file type: notes.docx", resp.get_json()["message"])

    @patch(f"{ROUTES}.check_ips_service.blocking_state")
    @patch(f"{ROUTES}.check_ips_utils.derive_ips_from_path", return_value="")
    @patch(f"{ROUTES}.os.path.isdir", return_value=False)
    @patch(f"{ROUTES}.os.path.exists", return_value=True)
    def test_blocked_ips_state_returns_428(self, mock_exists, mock_isdir, mock_derive, mock_blocking):
        """An unresolved IPS case number blocks the analysis with 428.

        Input: POST /upload_local_analysis where ``blocking_state`` reports the
        log's case number is still unknown.
        Expected: 428 Precondition Required echoing the blocking payload so the
        client can prompt for the case number and replay.
        """
        # Arrange
        blocked = {"case_nbr": "", "case_ref_source": "absent", "needs_prompt": True}
        mock_blocking.return_value = blocked
        path = os.path.join(SRC_DIR, "wifi.log")

        # Act
        with self.client.session_transaction() as sess:
            sess["picked_local_analysis_path"] = path
        resp = self.client.post("/log_parser/upload_local_analysis", data={"source_path": path})

        # Assert
        self.assertEqual(resp.status_code, 428)
        self.assertEqual(resp.get_json(), blocked)
        mock_blocking.assert_called_once_with(path)


class TestUploadLocalAnalysisProcessing(LogParserTestCase):
    """End-to-end through _process_local_analysis with its I/O patched."""

    def setUp(self):
        super().setUp()
        for target, kwargs in (
            ("os.path.exists", {"return_value": True}),
            ("os.path.isdir", {"return_value": False}),
            ("os.path.getsize", {"return_value": 2048}),
            ("os.makedirs", {}),
        ):
            p = patch(f"{ROUTES}.{target}", **kwargs)
            p.start()
            self.addCleanup(p.stop)
        self.mock_helpers = self._start(f"{ROUTES}.helpers")
        self.mock_helpers.get_long_path.side_effect = lambda p: p
        self.mock_helpers.to_long_path.side_effect = _long
        self.mock_gather = self._start(f"{ROUTES}.gather_service")
        self.mock_gather.new_workflow_id.return_value = "wf-1"
        mock_dt = self._start(f"{ROUTES}.datetime")
        mock_dt.datetime.now.return_value.strftime.return_value = TIMESTAMP
        # The IPS 428 gate guards this route; neutralise it so the analysis
        # path runs. The gate itself is covered by its own test below.
        self._start(f"{ROUTES}.check_ips_service.blocking_state", return_value=None)

    def _start(self, target, **kwargs):
        p = patch(target, **kwargs)
        mocked = p.start()
        self.addCleanup(p.stop)
        return mocked

    def _post(self, filename, **form):
        path = os.path.join(SRC_DIR, filename)
        with self.client.session_transaction() as sess:
            sess["picked_local_analysis_path"] = path
        return self.client.post("/log_parser/upload_local_analysis", data={"source_path": path, **form})

    def test_log_file_routes_to_wifi_chatbot(self):
        """A plain .log routes to the Wi-Fi chatbot.

        Input: POST /upload_local_analysis for a wifi.log.
        Expected: 200 redirecting to the log_chatbot with the long log path; the
        session switches to local in-place mode with a default classification.
        """
        # Act
        resp = self._post("wifi.log")

        # Assert
        body = resp.get_json()
        path = os.path.join(SRC_DIR, "wifi.log")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(body, {"success": True, "uploaded_source_path": path,
                                "redirect": "/__log_chatbot_stub__/index?auto_run=analyze_all",
                                "use_chatbot": True, "log_path": _long(path)})
        self.assertEqual(self.app_config.last_analyzed_log_path, path)
        sess = self.get_session()
        self.assertEqual(sess["download_path"], SRC_DIR)
        self.assertIs(sess["local_in_place"], True)
        self.assertIsNone(sess["latest_etl_path"])
        self.assertEqual(sess["classification"]["issue_type"], "Unclassified")

    def test_hci_txt_routes_to_bt_chatbot(self):
        """A .hci.txt routes to the BT chatbot.

        Input: POST /upload_local_analysis for a bt.hci.txt.
        Expected: 200 redirecting to the bt_chatbot with is_bt=True; the session
        records the latest ETL path.
        """
        # Act
        resp = self._post("bt.hci.txt")

        # Assert
        path = os.path.join(SRC_DIR, "bt.hci.txt")
        self.assertEqual(resp.get_json(), {"success": True, "uploaded_source_path": path,
                                           "redirect": "/__bt_chatbot_stub__/index?auto_run=analyze_all",
                                           "use_chatbot": True, "is_bt": True, "log_path": _long(path)})
        self.assertEqual(self.get_session()["latest_etl_path"], path)

    @patch(f"{ROUTES}.bt_decode_via_cli")
    def test_bt_etl_is_decoded_then_routes_to_bt_chatbot(self, mock_decode):
        """A BT ETL is decoded then routes to the BT chatbot.

        Input: POST /upload_local_analysis for a BT .etl where decode succeeds.
        Expected: bt_decode_via_cli runs; the redirect targets the bt_chatbot
        with the decoded .hci.txt as the long log path and is_bt=True.
        """
        # Arrange
        hci = os.path.join(SRC_DIR, "ibtusb.etl.hci.txt")
        mock_decode.return_value = hci

        # Act
        resp = self._post("ibtusb.etl")

        # Assert
        path = os.path.join(SRC_DIR, "ibtusb.etl")
        mock_decode.assert_called_once_with(SRC_DIR, path)
        body = resp.get_json()
        self.assertEqual(body["redirect"], "/__bt_chatbot_stub__/index?auto_run=analyze_all")
        self.assertEqual(body["log_path"], _long(path + ".hci.txt"))
        self.assertIs(body["is_bt"], True)
        self.assertEqual(self.app_config.last_analyzed_log_path, hci)

    @patch(f"{ROUTES}.bt_decode_via_cli", return_value=None)
    def test_bt_etl_decode_failure_returns_400(self, mock_decode):
        """A BT ETL decode failure returns 400.

        Input: POST /upload_local_analysis for a BT .etl where decode returns
        None.
        Expected: 400 with a "BT HCI decode failed or timed out" message.
        """
        # Act
        resp = self._post("ibtpci.etl")

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {"success": False,
                                           "message": "BT HCI decode failed or timed out for: ibtpci.etl"})

    @patch(f"{ROUTES}.wpp_ddd_parser_run")
    def test_wifi_etl_runs_wpp_parser_then_routes_to_wifi_chatbot(self, mock_wpp):
        """A Wi-Fi ETL runs the WPP parser then routes to the Wi-Fi chatbot.

        Input: POST /upload_local_analysis for a wifi .etl.001.
        Expected: wpp_ddd_parser_run runs on the path; the redirect targets the
        log_chatbot with the long ETL path and the session records the ETL path.
        """
        # Act
        resp = self._post("wifi.etl.001")

        # Assert
        path = os.path.join(SRC_DIR, "wifi.etl.001")
        mock_wpp.assert_called_once_with(path)
        body = resp.get_json()
        self.assertEqual(body["redirect"], "/__log_chatbot_stub__/index?auto_run=analyze_all")
        self.assertEqual(body["etl_path"], _long(path))
        self.assertIs(body["use_chatbot"], True)
        self.assertEqual(self.app_config.last_analyzed_log_path, path + ".log")
        self.assertEqual(self.get_session()["latest_etl_path"], path)

    @patch("utils.fw_utils.infer_fw_parse_type", return_value="bt")
    def test_fw_etl_routes_to_download_result(self, mock_infer):
        """A firmware ETL routes to the download result page.

        Input: POST /upload_local_analysis for a wrt-fw-*.etl.
        Expected: 200 redirecting to main.download_result; fw parse type is
        inferred, download results are stored under an fw key, and the session
        records the fw path, bt case type and workflow id.
        """
        # Act
        resp = self._post("wrt-fw-1.etl")

        # Assert
        path = os.path.join(SRC_DIR, "wrt-fw-1.etl")
        body = resp.get_json()
        self.assertEqual(body, {"success": True, "uploaded_source_path": path,
                                "redirect": "/__main_stub__/download_result"})
        mock_infer.assert_called_once_with(path, "wifi")
        self.app_config.set_download_results.assert_called_once_with(
            f"local_upload_{TIMESTAMP}", wifi={}, ddd={}, bt={}, fw={"wrt-fw-1.etl": [path]})
        sess = self.get_session()
        self.assertEqual(sess["auto_analysis_fw"], path)
        self.assertEqual(sess["case_context"]["wifi_or_bt"], "bt")
        self.assertEqual(sess["gather_workflow_id"], "wf-1")

    @patch(f"{ROUTES}.attachment_decompose.process_single_zip")
    def test_archive_is_extracted_and_routes_to_download_result(self, mock_zip):
        """An archive is extracted and routes to the download result page.

        Input: POST /upload_local_analysis for a .zip that decomposes into
        wifi/ddd/evt/bt/fw files.
        Expected: 200 redirecting to main.download_result; results are stored per
        domain (ddd+evt merged), the case type is bt (any BT file), bsod is False,
        and gather records workflow start and the download result with the byte
        count.
        """
        # Arrange
        mock_zip.return_value = (["w.etl"], ["d.bin"], ["e.evtx"], ["ibtusb.etl"], ["wrt-fw-1.etl"])

        # Act
        resp = self._post("case logs.zip")

        # Assert
        self.assertEqual(resp.get_json()["redirect"], "/__main_stub__/download_result")
        self.assertEqual(mock_zip.call_args.args, (os.path.join(SRC_DIR, "case logs.zip"), SRC_DIR))
        self.assertIs(mock_zip.call_args.kwargs["already_downloaded"], False)
        self.app_config.set_download_results.assert_called_once_with(
            f"local_upload_{TIMESTAMP}",
            wifi={"case logs.zip": ["w.etl"]}, ddd={"case logs.zip": ["d.bin", "e.evtx"]},
            bt={"case logs.zip": ["ibtusb.etl"]}, fw={"case logs.zip": ["wrt-fw-1.etl"]})
        sess = self.get_session()
        self.assertEqual(sess["case_context"]["wifi_or_bt"], "bt")  # any BT file → bt case
        self.assertIs(sess["bsod"], False)
        self.mock_gather.record_workflow_start.assert_called_once()
        self.mock_gather.record_attachment_download_result.assert_called_once()
        self.assertEqual(self.mock_gather.record_attachment_download_result.call_args.kwargs["byte_count"], 2048)

    @patch(f"{ROUTES}.attachment_decompose.process_single_zip", return_value=([], [], [], [], []))
    def test_archive_without_supported_files_returns_400(self, mock_zip):
        """An archive with no supported files returns 400.

        Input: POST /upload_local_analysis for an archive that decomposes to
        nothing.
        Expected: 400 with "No supported analysis files found"; no download
        results are stored.
        """
        # Act
        resp = self._post("empty.7z")

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["message"], "No supported analysis files found in the uploaded file.")
        self.app_config.set_download_results.assert_not_called()

    @patch(f"{ROUTES}._copy_file_with_console_progress")
    def test_dmp_is_copied_to_bsod_share_and_routes_to_bsod_result(self, mock_copy):
        """A .dmp is copied to the BSOD share and routes to the BSOD result.

        Input: POST /upload_local_analysis for a memory.dmp with a reachable BSOD
        share.
        Expected: 200 redirecting to main.download_result_bsod; the dump is copied
        into a timestamped share dir and the session records the bsod flow.
        """
        # Arrange
        self.mock_helpers.get_load_path.return_value = r"\\share\bsod"

        # Act
        resp = self._post("memory.dmp")

        # Assert
        shared_dir = os.path.join(r"\\share\bsod", f"local_bsod_{TIMESTAMP}")
        self.assertEqual(resp.get_json()["redirect"], "/__main_stub__/download_result_bsod")
        self.mock_helpers.get_load_path.assert_called_once_with(LOAD_PATH_prim, LOAD_PATH_bkup)
        self.assertEqual(mock_copy.call_args.args,
                         (os.path.join(SRC_DIR, "memory.dmp"), os.path.join(shared_dir, "memory.dmp")))
        sess = self.get_session()
        self.assertEqual(sess["download_path"], shared_dir)
        self.assertIs(sess["bsod"], True)
        self.assertEqual(sess["case_context"]["backend_id"], f"local_bsod_{TIMESTAMP}")
        self.assertEqual(sess["selected_files"], [("memory.dmp", "memory.dmp", None)])

    @patch(f"{ROUTES}._copy_file_with_console_progress")
    def test_dmp_without_bsod_share_returns_400(self, mock_copy):
        """A .dmp with no BSOD share returns 400.

        Input: POST /upload_local_analysis for a memory.dmp where the share is
        unavailable.
        Expected: 400 noting the BSOD shared folder is unavailable; no copy
        occurs.
        """
        # Arrange
        self.mock_helpers.get_load_path.return_value = None

        # Act
        resp = self._post("memory.dmp")

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["message"], "BSOD shared folder is unavailable. Please try again later.")
        mock_copy.assert_not_called()

    @patch(f"{ROUTES}._process_local_analysis", side_effect=RuntimeError("disk on fire"))
    def test_unexpected_exception_returns_500(self, mock_process):
        """An unexpected processing exception returns 500.

        Input: POST /upload_local_analysis where _process_local_analysis raises
        RuntimeError.
        Expected: 500 with "Failed local analysis flow: ...".
        """
        # Act
        resp = self._post("wifi.log")

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"success": False, "message": "Failed local analysis flow: disk on fire"})

    @patch(f"{ROUTES}._process_local_analysis", side_effect=SystemExit(0))
    def test_system_exit_without_cancel_propagates(self, mock_process):
        """A parser SystemExit without a cancel propagates.

        Input: POST /upload_local_analysis where processing raises SystemExit and
        no cancel was requested.
        Expected: the SystemExit propagates (not swallowed).
        """
        # Act / Assert: a parser's sys.exit is only swallowed when a cancel was requested
        with self.assertRaises(SystemExit):
            self._post("wifi.log")

    def test_system_exit_after_cancel_is_reported_as_cancelled(self):
        """A parser SystemExit after a cancel is reported as cancelled.

        Input: POST /upload_local_analysis where processing sets the cancel event
        then raises SystemExit.
        Expected: 200 with "Upload cancelled by user." and the upload is
        unregistered.
        """
        # Arrange: a parser killed by the cancel thread calls sys.exit(0)
        def _exit_after_cancel(*args, **kwargs):
            kwargs["cancel_event"].set()
            raise SystemExit(0)

        with patch(f"{ROUTES}._process_local_analysis", side_effect=_exit_after_cancel):
            # Act
            resp = self._post("wifi.etl", upload_id="up-3")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["message"], "Upload cancelled by user.")
        self.assertNotIn("up-3", lp._active_local_uploads)

    @patch(f"{ROUTES}._copy_file_with_console_progress")
    def test_is_bsod_flag_forces_bsod_flow_for_non_dmp_file(self, mock_copy):
        """The is_bsod flag forces the BSOD flow for a non-dmp file.

        Input: POST /upload_local_analysis for a .zip with is_bsod="true" and a
        reachable share.
        Expected: the redirect targets download_result_bsod, the file is copied,
        and the session's bsod flag is True.
        """
        # Arrange
        self.mock_helpers.get_load_path.return_value = r"\\share\bsod"

        # Act
        resp = self._post("dump_bundle.zip", is_bsod="true")

        # Assert
        self.assertEqual(resp.get_json()["redirect"], "/__main_stub__/download_result_bsod")
        mock_copy.assert_called_once()
        self.assertIs(self.get_session()["bsod"], True)

    def test_cancelled_upload_returns_cancelled_and_clears_session(self):
        """A cancelled upload reports cancelled and clears the session.

        Input: POST /upload_local_analysis where processing records a cleanup
        path, sets the cancel event, then raises _LocalAnalysisCancelled.
        Expected: 200 with {cancelled: True}; the session download_path is
        removed, child processes are waited on, cleanup paths are deleted, and the
        upload is unregistered.
        """
        # Arrange: worker records a cleanup path, then sees the cancel
        def _cancelled(*args, **kwargs):
            kwargs["cleanup_paths"].append(r"C:\fake\local\partial")
            kwargs["cancel_event"].set()
            raise lp._LocalAnalysisCancelled()

        self.seed_session(download_path="keep-me-not")
        with patch(f"{ROUTES}._process_local_analysis", side_effect=_cancelled), \
                patch(f"{ROUTES}._wait_for_cancel_children_to_exit") as mock_wait, \
                patch(f"{ROUTES}._delete_cancel_cleanup_paths") as mock_delete:
            # Act
            resp = self._post("wifi.zip", upload_id="up-1")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"success": False, "cancelled": True,
                                           "message": "Upload cancelled by user."})
        self.assertNotIn("download_path", self.get_session())
        mock_wait.assert_called_once_with()
        mock_delete.assert_called_once_with([r"C:\fake\local\partial"], "up-1")
        self.assertNotIn("up-1", lp._active_local_uploads)

    def test_value_error_after_cancel_is_reported_as_cancelled(self):
        """A ValueError after a cancel is reported as cancelled.

        Input: POST /upload_local_analysis where processing sets the cancel event
        then raises ValueError.
        Expected: 200 with {cancelled: True}; the upload is unregistered.
        """
        # Arrange
        def _fail_after_cancel(*args, **kwargs):
            kwargs["cancel_event"].set()
            raise ValueError("partial archive")

        with patch(f"{ROUTES}._process_local_analysis", side_effect=_fail_after_cancel):
            # Act
            resp = self._post("wifi.zip", upload_id="up-2")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.get_json()["cancelled"], True)
        self.assertNotIn("up-2", lp._active_local_uploads)


# ═══════════════════════════════════════════════════════ /cancel_local_analysis
class TestCancelLocalAnalysis(LogParserTestCase):

    def test_non_localhost_is_rejected(self):
        """cancel_local_analysis rejects non-localhost callers.

        Input: POST /cancel_local_analysis from a remote address.
        Expected: 403 with "Access denied: localhost only".
        """
        # Act
        resp = self.client.post("/log_parser/cancel_local_analysis", json={"upload_id": "x"},
                                environ_base=NON_LOCAL)

        # Assert
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()["message"], "Access denied: localhost only")

    def test_missing_upload_id_returns_400(self):
        """cancel_local_analysis requires an upload_id.

        Input: POST /cancel_local_analysis with an empty body.
        Expected: 400 with "upload_id is required".
        """
        # Act
        resp = self.client.post("/log_parser/cancel_local_analysis", json={})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["message"], "upload_id is required")

    @patch(f"{ROUTES}.threading.Thread")
    def test_unknown_upload_id_reports_inactive(self, mock_thread):
        """Cancelling an unknown upload reports it inactive.

        Input: POST /cancel_local_analysis for an unregistered upload_id.
        Expected: {success: True, was_active: False}; no killer thread is started.
        """
        # Act
        resp = self.client.post("/log_parser/cancel_local_analysis", json={"upload_id": "nope"})

        # Assert
        self.assertEqual(resp.get_json(), {"success": True, "was_active": False})
        mock_thread.assert_not_called()

    @patch(f"{ROUTES}.threading.Thread")
    def test_active_upload_is_signalled_and_killer_started_once(self, mock_thread):
        """An active upload is signalled and a killer is started once.

        Input: two POST /cancel_local_analysis for the same active upload.
        Expected: both report was_active=True and set the cancel event; a single
        daemon killer thread targeting the child-killer is started once.
        """
        # Arrange
        event = lp._register_local_upload("up-9")
        self.addCleanup(lp._unregister_local_upload, "up-9")

        # Act
        first = self.client.post("/log_parser/cancel_local_analysis", data={"upload_id": "up-9"})
        second = self.client.post("/log_parser/cancel_local_analysis", data={"upload_id": "up-9"})

        # Assert
        self.assertEqual(first.get_json(), {"success": True, "was_active": True})
        self.assertEqual(second.get_json(), {"success": True, "was_active": True})
        self.assertTrue(event.is_set())
        mock_thread.assert_called_once()
        kwargs = mock_thread.call_args.kwargs
        self.assertIs(kwargs["target"], lp._kill_local_analysis_children_until_done)
        self.assertEqual(kwargs["args"], ("up-9",))
        self.assertIs(kwargs["daemon"], True)
        mock_thread.return_value.start.assert_called_once_with()


# ═══════════════════════════════════════════════════════ /open_local_analysis
class TestOpenLocalAnalysis(LogParserTestCase):

    def _get(self, token="unit-test-token", path=os.path.join(SRC_DIR, "wifi.zip"), **kw):
        return self.client.get("/log_parser/open_local_analysis",
                               query_string={"token": token, "path": path}, **kw)

    def _assert_flash_redirect(self, resp, text):
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith(self.main_stub_path("index")))
        category, message = self._flashes()[0]
        self.assertEqual(category, "danger")
        self.assertIn(text, message)

    def test_non_localhost_is_rejected(self):
        """open_local_analysis rejects non-localhost callers.

        Input: GET /open_local_analysis from a remote address.
        Expected: 302 back to main.index with a danger flash about localhost.
        """
        # Act
        resp = self._get(environ_base=NON_LOCAL)

        # Assert
        self._assert_flash_redirect(resp, "only available from localhost")

    def test_invalid_token_is_rejected(self):
        """open_local_analysis rejects an invalid SendTo token.

        Input: GET /open_local_analysis with a wrong token.
        Expected: 302 back to main.index with a danger flash about the token.
        """
        # Act
        resp = self._get(token="wrong-token")

        # Assert
        self._assert_flash_redirect(resp, "Invalid or expired SendTo token")

    def test_missing_path_is_rejected(self):
        """open_local_analysis rejects a missing path.

        Input: GET /open_local_analysis with an empty path.
        Expected: 302 back to main.index with a danger flash about no path.
        """
        # Act
        resp = self._get(path="")

        # Assert
        self._assert_flash_redirect(resp, "No local analysis file path was provided.")

    @patch(f"{ROUTES}.os.path.exists", return_value=False)
    def test_nonexistent_file_is_rejected(self, mock_exists):
        """open_local_analysis rejects a nonexistent file.

        Input: GET /open_local_analysis where os.path.exists is False.
        Expected: 302 back to main.index with a "file not found" danger flash.
        """
        # Act
        resp = self._get()

        # Assert
        self._assert_flash_redirect(resp, "Local analysis file not found")

    @patch(f"{ROUTES}.os.path.isdir", return_value=True)
    @patch(f"{ROUTES}.os.path.exists", return_value=True)
    def test_directory_is_rejected(self, mock_exists, mock_isdir):
        """open_local_analysis rejects a directory.

        Input: GET /open_local_analysis where the path is a directory.
        Expected: 302 back to main.index with a "Selected path is a folder" flash.
        """
        # Act
        resp = self._get()

        # Assert
        self._assert_flash_redirect(resp, "Selected path is a folder")

    @patch(f"{ROUTES}.os.path.isdir", return_value=False)
    @patch(f"{ROUTES}.os.path.exists", return_value=True)
    def test_disallowed_extension_is_rejected(self, mock_exists, mock_isdir):
        """open_local_analysis rejects a disallowed extension.

        Input: GET /open_local_analysis for a readme.pdf.
        Expected: 302 back to main.index with an "Invalid file type" flash.
        """
        # Act
        resp = self._get(path=os.path.join(SRC_DIR, "readme.pdf"))

        # Assert
        self._assert_flash_redirect(resp, "Invalid file type: readme.pdf")

    @patch(f"{ROUTES}.render_template", return_value="transmission page")
    @patch(f"{ROUTES}.os.path.isdir", return_value=False)
    @patch(f"{ROUTES}.os.path.exists", return_value=True)
    def test_valid_request_stores_pending_path_and_renders_page(self, mock_exists, mock_isdir, mock_render):
        """A valid open request stores the pending path and renders the page.

        Input: GET /open_local_analysis with a valid token and existing file.
        Expected: 200 rendering sendto_transmission.html with the filename and
        the IPS prompt state; the session stores the pending path.
        """
        # Arrange
        ips_patch = patch(f"{ROUTES}.check_ips_service")
        mock_ips = ips_patch.start()
        self.addCleanup(ips_patch.stop)
        mock_ips.current_case_nbr.return_value = "00123456"  # short-circuits the attach block
        mock_ips.prompt_state.return_value = {"required": False}

        # Act
        resp = self._get()

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(as_text=True), "transmission page")
        mock_render.assert_called_once_with(
            "sendto_transmission.html", filename="wifi.zip", ips_state={"required": False})
        self.assertEqual(self.get_session()["sendto_pending_path"], os.path.join(SRC_DIR, "wifi.zip"))


# ═════════════════════════════════════════════════ /navigate_existing_browser
class TestNavigateExistingBrowser(LogParserTestCase):

    def test_non_localhost_is_rejected(self):
        """navigate_existing_browser rejects non-localhost callers.

        Input: POST /navigate_existing_browser from a remote address.
        Expected: 403; the driver manager is not navigated.
        """
        # Act
        resp = self.client.post("/log_parser/navigate_existing_browser", json={"startup_path": "/"},
                                environ_base=NON_LOCAL)

        # Assert
        self.assertEqual(resp.status_code, 403)
        self.app_config.driver_manager.navigate_main_browser.assert_not_called()

    def test_unsafe_startup_paths_return_400(self):
        """navigate_existing_browser rejects unsafe startup paths.

        Input: POST /navigate_existing_browser with relative, protocol-relative,
        open-redirect and non-string startup paths.
        Expected: each returns 400 with "Invalid startup path"; the browser is not
        navigated.
        """
        for bad in ("relative/path", "//evil.example/x", "/redirect?u=http://evil", 42):
            with self.subTest(startup_path=bad):
                # Act
                resp = self.client.post("/log_parser/navigate_existing_browser", json={"startup_path": bad})

                # Assert
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.get_json()["message"], "Invalid startup path")
        self.app_config.driver_manager.navigate_main_browser.assert_not_called()

    def test_missing_driver_manager_returns_409(self):
        """navigate_existing_browser returns 409 without a driver manager.

        Input: POST /navigate_existing_browser when driver_manager is None.
        Expected: 409 with "Driver manager is not available".
        """
        # Arrange
        self.app_config.driver_manager = None

        # Act
        resp = self.client.post("/log_parser/navigate_existing_browser", json={"startup_path": "/"})

        # Assert
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["message"], "Driver manager is not available")

    def test_navigation_error_returns_409(self):
        """A navigation error returns 409.

        Input: POST /navigate_existing_browser where navigate_main_browser raises.
        Expected: 409 echoing the error.
        """
        # Arrange
        self.app_config.driver_manager.navigate_main_browser.side_effect = RuntimeError("chrome gone")

        # Act
        resp = self.client.post("/log_parser/navigate_existing_browser", json={"startup_path": "/"})

        # Assert
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json(), {"success": False, "message": "chrome gone"})

    def test_success_navigates_with_fixed_localhost_origin(self):
        """A valid navigate uses a fixed localhost origin, ignoring the Host.

        Input: POST /navigate_existing_browser with a spoofed Host header.
        Expected: {success: True}; navigate_main_browser is called with a fixed
        127.0.0.1 origin and the requested startup path.
        """
        # Act
        resp = self.client.post("/log_parser/navigate_existing_browser",
                                json={"startup_path": "/log_parser/open_local_analysis?token=t"},
                                headers={"Host": "attacker.example:5000"})

        # Assert
        self.assertEqual(resp.get_json(), {"success": True})
        self.app_config.driver_manager.navigate_main_browser.assert_called_once_with(
            "http://127.0.0.1:80/", "/log_parser/open_local_analysis?token=t")


# ═══════════════════════════════════════════════════════ /verify_sendto_token
class TestVerifySendtoToken(LogParserTestCase):

    def test_non_localhost_is_rejected(self):
        """verify_sendto_token rejects non-localhost callers.

        Input: GET /verify_sendto_token from a remote address.
        Expected: 403.
        """
        # Act
        resp = self.client.get("/log_parser/verify_sendto_token?token=x", environ_base=NON_LOCAL)

        # Assert
        self.assertEqual(resp.status_code, 403)

    def test_matching_token(self):
        """verify_sendto_token confirms a matching token.

        Input: GET /verify_sendto_token with the correct token.
        Expected: {success, token_match: True} with masked token previews.
        """
        # Act
        resp = self.client.get("/log_parser/verify_sendto_token?token=unit-test-token")

        # Assert
        self.assertEqual(resp.get_json(), {"success": True, "token_match": True,
                                           "provided_token_preview": "unit-tes...",
                                           "expected_token_preview": "unit-tes..."})

    def test_mismatched_and_empty_tokens(self):
        """verify_sendto_token reports mismatched and empty tokens.

        Input: GET /verify_sendto_token with a wrong token, then with none.
        Expected: both report token_match=False with the provided preview
        ("abc" / "").
        """
        # Act
        wrong = self.client.get("/log_parser/verify_sendto_token?token=abc").get_json()
        empty = self.client.get("/log_parser/verify_sendto_token").get_json()

        # Assert
        self.assertIs(wrong["token_match"], False)
        self.assertEqual(wrong["provided_token_preview"], "abc")
        self.assertIs(empty["token_match"], False)
        self.assertEqual(empty["provided_token_preview"], "")


# ═══════════════════════════════════════════════════ filter / token routes
class TestFilterRoutes(LogParserTestCase):

    def test_get_filter_details_requires_filter_file(self):
        """get_filter_details requires a filter file.

        Input: POST /get_filter_details with an empty body.
        Expected: {success: False, "No filter file specified"}.
        """
        # Act
        resp = self.client.post("/log_parser/get_filter_details", json={})

        # Assert
        self.assertEqual(resp.get_json(), {"success": False, "message": "No filter file specified"})

    @patch(f"{ROUTES}.os.path.exists", return_value=False)
    def test_get_filter_details_missing_file(self, mock_exists):
        """get_filter_details reports a missing filter file.

        Input: POST /get_filter_details for a filter file that does not exist.
        Expected: {success: False, "Filter file not found: ..."}; existence is
        checked under the filter dir.
        """
        # Act
        resp = self.client.post("/log_parser/get_filter_details", json={"filter_file": "wifi.tat"})

        # Assert
        self.assertEqual(resp.get_json(), {"success": False, "message": "Filter file not found: wifi.tat"})
        mock_exists.assert_called_once_with(os.path.join(FILTER_DIR, "wifi.tat"))

    @patch(f"{ROUTES}.extract_all_keywords_from_filter_file")
    @patch(f"{ROUTES}.os.path.exists", return_value=True)
    def test_get_filter_details_success(self, mock_exists, mock_extract):
        """get_filter_details returns the extracted keywords.

        Input: POST /get_filter_details for an existing filter file.
        Expected: {success, keywords} from the extractor, which is called with the
        filter path.
        """
        # Arrange
        mock_extract.return_value = [{"keyword": "disconnect", "enabled": True}]

        # Act
        resp = self.client.post("/log_parser/get_filter_details", json={"filter_file": "wifi.tat"})

        # Assert
        self.assertEqual(resp.get_json(), {"success": True,
                                           "keywords": [{"keyword": "disconnect", "enabled": True}]})
        mock_extract.assert_called_once_with(os.path.join(FILTER_DIR, "wifi.tat"))

    @patch(f"{ROUTES}.os.path.exists", return_value=False)
    def test_get_filter_details_does_not_strip_directory_components(self, mock_exists):
        """Document that get_filter_details does not strip directory components.

        Input: POST /get_filter_details with a "..\\" prefixed filter name.
        Expected (current, not endorsed): the raw name is joined to the filter
        dir, so existence is checked on the traversal path.
        """
        # Documents CURRENT behavior, not endorsed: unlike /estimate_tokens
        # (which applies os.path.basename) this route joins the raw name, so
        # a "..\" prefix escapes the filter directory.
        # Act
        self.client.post("/log_parser/get_filter_details", json={"filter_file": r"..\x.tat"})

        # Assert
        mock_exists.assert_called_once_with(os.path.join(FILTER_DIR, r"..\x.tat"))

    @patch(f"{ROUTES}.extract_all_keywords_from_filter_file", side_effect=OSError("locked"))
    @patch(f"{ROUTES}.os.path.exists", return_value=True)
    def test_get_filter_details_read_error(self, mock_exists, mock_extract):
        """get_filter_details surfaces a read error.

        Input: POST /get_filter_details where the extractor raises OSError.
        Expected: {success: False, "Failed to read filter: ..."}.
        """
        # Act
        resp = self.client.post("/log_parser/get_filter_details", json={"filter_file": "wifi.tat"})

        # Assert
        self.assertEqual(resp.get_json(), {"success": False, "message": "Failed to read filter: locked"})

    @patch(f"{ROUTES}.os.path.exists", return_value=False)
    def test_get_all_filter_details_missing_dir(self, mock_exists):
        """get_all_filter_details reports a missing filter directory.

        Input: POST /get_all_filter_details when the filter dir does not exist.
        Expected: {success: False, "Filter directory not found"}.
        """
        # Act
        resp = self.client.post("/log_parser/get_all_filter_details")

        # Assert
        self.assertEqual(resp.get_json(), {"success": False, "message": "Filter directory not found"})

    @patch(f"{ROUTES}.extract_all_keywords_from_filter_file", side_effect=lambda p: [os.path.basename(p)])
    @patch(f"{ROUTES}.os.listdir", return_value=["b.tat", "readme.md", "a.tat"])
    @patch(f"{ROUTES}.os.path.exists", return_value=True)
    def test_get_all_filter_details_lists_sorted_tat_files(self, mock_exists, mock_listdir, mock_extract):
        """get_all_filter_details lists only .tat files, sorted.

        Input: POST /get_all_filter_details where the dir has .tat and other
        files.
        Expected: only the .tat files are returned, sorted, each with its
        extracted keywords.
        """
        # Act
        resp = self.client.post("/log_parser/get_all_filter_details")

        # Assert
        self.assertEqual(resp.get_json(), {"success": True, "filters": [
            {"file": "a.tat", "keywords": ["a.tat"]},
            {"file": "b.tat", "keywords": ["b.tat"]},
        ]})
        mock_listdir.assert_called_once_with(FILTER_DIR)

    @patch(f"{ROUTES}.os.listdir", side_effect=PermissionError("denied"))
    @patch(f"{ROUTES}.os.path.exists", return_value=True)
    def test_get_all_filter_details_error(self, mock_exists, mock_listdir):
        """get_all_filter_details surfaces a directory-read error.

        Input: POST /get_all_filter_details where listdir raises PermissionError.
        Expected: {success: False, "Failed to load filters: ..."}.
        """
        # Act
        resp = self.client.post("/log_parser/get_all_filter_details")

        # Assert
        self.assertEqual(resp.get_json(), {"success": False, "message": "Failed to load filters: denied"})


class TestEstimateTokens(LogParserTestCase):
    LOG = r"C:\fake\out\wifi.etl.log"

    def test_requires_filter_file(self):
        """estimate_tokens requires a filter file.

        Input: POST /estimate_tokens with a blank filter file.
        Expected: 400 with "No filter file specified".
        """
        # Act
        resp = self.client.post("/log_parser/estimate_tokens", json={"filter_file": "  "})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["message"], "No filter file specified")

    def test_requires_log_path_in_session(self):
        """estimate_tokens requires a log path in session.

        Input: POST /estimate_tokens with a filter file but no session log.
        Expected: 400 with "Log file not found in session".
        """
        # Act
        resp = self.client.post("/log_parser/estimate_tokens", json={"filter_file": "wifi.tat"})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["message"], "Log file not found in session")

    @patch(f"{ROUTES}.os.path.exists", side_effect=lambda p: p == TestEstimateTokens.LOG)
    def test_missing_filter_file_returns_400(self, mock_exists):
        """estimate_tokens reports a missing filter file (basename only).

        Input: POST /estimate_tokens with a "..\\..\\" prefixed filter name and a
        session log.
        Expected: 400 with "Filter file not found: ..."; existence is checked on
        the basename under the filter dir (traversal stripped).
        """
        # Arrange
        self.seed_session(log_path=self.LOG)

        # Act
        resp = self.client.post("/log_parser/estimate_tokens", json={"filter_file": r"..\..\wifi.tat"})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["message"], r"Filter file not found: ..\..\wifi.tat")
        mock_exists.assert_called_with(os.path.join(FILTER_DIR, "wifi.tat"))  # basename only

    @patch(f"{ROUTES}.log_parser_service")
    @patch("utils.log_parser_preprocess.group_similar_logs", return_value=["g1"])
    @patch("utils.log_parser_preprocess.preprocess_log_for_llm", return_value=["p1", "p2"])
    @patch("utils.log_parser_preprocess.filter_log_by_keywords", return_value=["f1", "f2", "f3"])
    @patch("utils.log_parser_preprocess.extract_enabled_keywords_from_filter_file", return_value=["a", "b"])
    @patch("utils.helpers.read_log_file", return_value=["l1", "l2", "l3", "l4"])
    @patch(f"{ROUTES}.os.path.exists", return_value=True)
    def test_success_reports_token_counts(self, mock_exists, mock_read, mock_kw, mock_filter,
                                          mock_pre, mock_group, mock_svc):
        """estimate_tokens reports filtered vs grouped token counts.

        Input: POST /estimate_tokens with a filter file and session log; the
        preprocess pipeline yields filtered/grouped text estimated at 25k/21k.
        Expected: {filtered_tokens, grouped_tokens, token_limit, exceeds_limit,
        keyword_count, filtered_lines} computed from the pipeline; the estimator
        is called on the filtered then grouped text.
        """
        # Arrange
        mock_svc._estimate_tokens.side_effect = [25_000, 21_000]
        self.seed_session(log_path=self.LOG)

        # Act
        resp = self.client.post("/log_parser/estimate_tokens", json={"filter_file": "wifi.tat"})

        # Assert
        self.assertEqual(resp.get_json(), {
            "success": True, "filtered_tokens": 25_000, "grouped_tokens": 21_000,
            "token_limit": 20_000, "exceeds_limit": True, "keyword_count": 2, "filtered_lines": 3,
        })
        mock_read.assert_called_once_with(self.LOG)
        mock_filter.assert_called_once_with(["l1", "l2", "l3", "l4"], ["a", "b"])
        self.assertEqual(mock_svc._estimate_tokens.call_args_list[0].args, ("f1\nf2\nf3",))
        self.assertEqual(mock_svc._estimate_tokens.call_args_list[1].args, ("g1",))

    @patch("utils.helpers.read_log_file", side_effect=UnicodeDecodeError("utf-8", b"", 0, 1, "bad"))
    @patch(f"{ROUTES}.os.path.exists", return_value=True)
    def test_processing_error_returns_500(self, mock_exists, mock_read):
        """estimate_tokens surfaces a processing error as 500.

        Input: POST /estimate_tokens where reading the log raises
        UnicodeDecodeError.
        Expected: 500 with success False.
        """
        # Arrange
        self.seed_session(log_path=self.LOG)

        # Act
        resp = self.client.post("/log_parser/estimate_tokens", json={"filter_file": "wifi.tat"})

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertIs(resp.get_json()["success"], False)


# ═══════════════════════════════════════════ before_app_request session pickup
class TestSendtoSessionPickup(LogParserTestCase):

    def setUp(self):
        super().setUp()
        lp._sendto_session_store.clear()
        self.addCleanup(lp._sendto_session_store.clear)

    def test_valid_token_restores_session_once(self):
        """A valid SendTo token restores the session once.

        Input: GET any route with ?_st=<token> where the store has that token.
        Expected: the stored session values are applied and the token is purged
        from the store (single use).
        """
        # Arrange
        lp._sendto_session_store["tok1"] = (time.time(), {"download_path": SRC_DIR, "bsod": False})

        # Act
        self.client.get("/log_parser/verify_sendto_token?_st=tok1")

        # Assert
        sess = self.get_session()
        self.assertEqual(sess["download_path"], SRC_DIR)
        self.assertIs(sess["bsod"], False)
        self.assertNotIn("tok1", lp._sendto_session_store)

    def test_expired_tokens_are_purged_and_not_applied(self):
        """Expired SendTo tokens are purged and not applied.

        Input: GET a route with ?_st=<token> whose stored entry is older than the
        TTL.
        Expected: nothing is applied to the session and the store is emptied.
        """
        # Arrange
        lp._sendto_session_store["old"] = (time.time() - lp._SENDTO_TOKEN_TTL - 1, {"download_path": "stale"})

        # Act
        self.client.get("/log_parser/verify_sendto_token?_st=old")

        # Assert
        self.assertNotIn("download_path", self.get_session())
        self.assertEqual(lp._sendto_session_store, {})


# ═══════════════════════════════════════════════════════ SocketIO handlers
class TestSocketHandlers(LogParserTestCase):

    def setUp(self):
        super().setUp()
        p = patch(f"{ROUTES}.log_parser_service")
        self.svc = p.start()
        self.addCleanup(p.stop)
        self.socketio = MagicMock(name="socketio_arg")

    # -- handle_submit_analysis ------------------------------------------------
    def test_submit_analysis_validation_error_emits_on_given_socketio(self):
        """submit_analysis emits a validation error on the given socketio.

        Input: handle_submit_analysis with an invalid filter and an explicit
        socketio; validation fails.
        Expected: a "validation_error" is emitted on that socketio and the service
        is not started.
        """
        # Arrange
        self.svc.validate_analysis_inputs.return_value = (False, "Pick a filter")
        with self.app.test_request_context():
            session["log_path"] = r"C:\fake\out\a.log"
            # Act
            lp.handle_submit_analysis({"filter_file": None, "prompt_content": "x"}, self.socketio)

        # Assert
        self.svc.validate_analysis_inputs.assert_called_once_with(r"C:\fake\out\a.log", None, "x")
        self.socketio.emit.assert_called_once_with("validation_error", {"message": "Pick a filter"},
                                                   namespace="/progress")
        self.svc.start_analysis.assert_not_called()

    def test_submit_analysis_validation_error_falls_back_to_app_socketio(self):
        """submit_analysis falls back to the app socketio on validation error.

        Input: handle_submit_analysis with no explicit socketio; validation
        fails.
        Expected: the validation error is emitted on app_config.socketio.
        """
        # Arrange
        self.svc.validate_analysis_inputs.return_value = (False, "No log")
        with self.app.test_request_context():
            # Act
            lp.handle_submit_analysis({})

        # Assert
        self.app_config.socketio.emit.assert_called_once_with("validation_error", {"message": "No log"})

    def test_submit_analysis_starts_service_with_case_description(self):
        """submit_analysis starts the service with the case description.

        Input: handle_submit_analysis with a valid filter, prompt and a seeded
        case session.
        Expected: start_analysis is called with the filter path, log, output dir,
        llm helper, prompt and the case description; no error is emitted.
        """
        # Arrange
        self.svc.validate_analysis_inputs.return_value = (True, "")
        self.svc.start_analysis.return_value = True
        with self.app.test_request_context():
            session["log_path"] = r"C:\fake\out\a.log"
            session["logparser_output_dir"] = r"C:\fake\out"
            session["case_context"] = self.create_mock_case_context()
            # Act
            lp.handle_submit_analysis({"filter_file": "wifi.tat", "prompt_content": "Analyze"}, self.socketio)

        # Assert
        self.svc.start_analysis.assert_called_once_with(
            os.path.join(FILTER_DIR, "wifi.tat"), r"C:\fake\out\a.log", r"C:\fake\out",
            self.app_config.llm_helper, "Analyze",
            "Wi-Fi drops every time the laptop resumes from Modern Standby.")
        self.socketio.emit.assert_not_called()

    def test_submit_analysis_start_failure_emits_analysis_error(self):
        """submit_analysis emits an analysis error when start fails.

        Input: handle_submit_analysis where start_analysis returns False.
        Expected: an "analysis_error" with "Failed to start analysis" is emitted.
        """
        # Arrange
        self.svc.validate_analysis_inputs.return_value = (True, "")
        self.svc.start_analysis.return_value = False
        with self.app.test_request_context():
            session["logparser_output_dir"] = r"C:\fake\out"
            # Act
            lp.handle_submit_analysis({"filter_file": "wifi.tat"}, self.socketio)

        # Assert
        self.socketio.emit.assert_called_once_with("analysis_error", {"message": "Failed to start analysis"},
                                                   namespace="/progress")

    def test_submit_analysis_exception_emits_analysis_error(self):
        """submit_analysis emits an analysis error on an unexpected exception.

        Input: handle_submit_analysis where logparser_output_dir is missing,
        raising a KeyError inside the try.
        Expected: an "analysis_error" whose message mentions the failure and
        logparser_output_dir.
        """
        # Arrange: logparser_output_dir missing → session[...] KeyError inside the try
        self.svc.validate_analysis_inputs.return_value = (True, "")
        with self.app.test_request_context():
            # Act
            lp.handle_submit_analysis({"filter_file": "wifi.tat"}, self.socketio)

        # Assert
        event, payload = self.socketio.emit.call_args.args
        self.assertEqual(event, "analysis_error")
        self.assertIn("Failed to start analysis: ", payload["message"])
        self.assertIn("logparser_output_dir", payload["message"])

    # -- handle_chat_message ---------------------------------------------------
    def test_chat_message_empty_is_rejected(self):
        """chat_message rejects an empty message.

        Input: handle_chat_message with a whitespace message.
        Expected: a "chat_error" with "Empty message" is emitted.
        """
        # Act
        lp.handle_chat_message({"message": "   "}, self.socketio)

        # Assert
        self.socketio.emit.assert_called_once_with("chat_error", {"message": "Empty message"}, namespace="/progress")

    def test_chat_message_requires_prior_analysis(self):
        """chat_message requires a prior analysis.

        Input: handle_chat_message when the service has no conversation history.
        Expected: a "chat_error" asking to run analysis first is emitted.
        """
        # Arrange
        self.svc.conversation_history = []

        # Act
        lp.handle_chat_message({"message": "why?"}, self.socketio)

        # Assert
        self.socketio.emit.assert_called_once_with(
            "chat_error", {"message": "Please run analysis first before chatting."}, namespace="/progress")

    def test_chat_message_without_llm_helper(self):
        """chat_message reports a missing LLM helper (app socketio fallback).

        Input: handle_chat_message with no explicit socketio and llm_helper None,
        after a prior analysis.
        Expected: a "chat_error" about the unavailable LLM helper is emitted on
        app_config.socketio.
        """
        # Arrange
        self.svc.conversation_history = [{"role": "system"}]
        self.app_config.llm_helper = None

        # Act
        lp.handle_chat_message({"message": "why?"})

        # Assert: socketio=None → falls back to app_config.socketio
        self.app_config.socketio.emit.assert_called_once_with(
            "chat_error", {"message": "LLM helper is not available."}, namespace="/progress")

    def test_chat_message_success_emits_markdown_html(self):
        """chat_message emits the answer with rendered markdown HTML.

        Input: handle_chat_message after an analysis where the service returns a
        markdown answer.
        Expected: a "chat_response" carrying the raw message and its rendered HTML.
        """
        # Arrange
        self.svc.conversation_history = [{"role": "system"}]
        self.svc.handle_chat_message.return_value = "**Root cause**: roaming"

        # Act
        lp.handle_chat_message({"message": " why? "}, self.socketio)

        # Assert
        self.svc.handle_chat_message.assert_called_once_with("why?", self.app_config.llm_helper)
        event, payload = self.socketio.emit.call_args.args
        self.assertEqual(event, "chat_response")
        self.assertEqual(payload["message"], "**Root cause**: roaming")
        self.assertIn("<strong>Root cause</strong>", payload["message_html"])

    def test_chat_message_llm_exception_emits_chat_error(self):
        """chat_message emits a chat error on an LLM exception.

        Input: handle_chat_message where the service raises TimeoutError.
        Expected: a "chat_error" with "Chat failed: ..." is emitted.
        """
        # Arrange
        self.svc.conversation_history = [{"role": "system"}]
        self.svc.handle_chat_message.side_effect = TimeoutError("llm timeout")

        # Act
        lp.handle_chat_message({"message": "why?"}, self.socketio)

        # Assert
        self.socketio.emit.assert_called_once_with("chat_error", {"message": "Chat failed: llm timeout"},
                                                   namespace="/progress")

    # -- handle_chat_message_with_filter ---------------------------------------
    def test_filter_chat_validation_errors(self):
        """filter chat rejects empty message, no keywords, or no raw log.

        Input: handle_chat_message_with_filter across three invalid combinations.
        Expected: each emits the matching "chat_error" and the service chat is
        never called.
        """
        cases = [
            ({"message": "", "keywords": ["a"]}, ["x"], "Empty message"),
            ({"message": "hi", "keywords": []}, ["x"], "No filter keywords selected"),
            ({"message": "hi", "keywords": ["a"]}, [], "Please run analysis first. No raw log available."),
        ]
        for data, raw, expected in cases:
            with self.subTest(expected=expected):
                # Arrange
                self.socketio.reset_mock()
                self.svc.raw_log_lines = raw

                # Act
                lp.handle_chat_message_with_filter(data, self.socketio)

                # Assert
                self.socketio.emit.assert_called_once_with("chat_error", {"message": expected},
                                                           namespace="/progress")
        self.svc.handle_chat_message.assert_not_called()

    def test_filter_chat_without_llm_helper(self):
        """filter chat reports a missing LLM helper.

        Input: handle_chat_message_with_filter with raw log present but
        llm_helper None.
        Expected: a "chat_error" about the unavailable LLM helper is emitted.
        """
        # Arrange
        self.svc.raw_log_lines = ["l1"]
        self.app_config.llm_helper = None

        # Act
        lp.handle_chat_message_with_filter({"message": "hi", "keywords": ["a"]}, self.socketio)

        # Assert
        self.socketio.emit.assert_called_once_with("chat_error", {"message": "LLM helper is not available."},
                                                   namespace="/progress")

    @patch("utils.log_parser_preprocess.group_similar_logs", return_value=["g1"])
    @patch("utils.log_parser_preprocess.preprocess_log_for_llm", return_value=["p1"])
    @patch("utils.log_parser_preprocess.filter_log_by_keywords", return_value=["f1"])
    def test_filter_chat_refilters_and_sends_enhanced_prompt(self, mock_filter, mock_pre, mock_group):
        """filter chat refilters the log and sends an enhanced prompt.

        Input: handle_chat_message_with_filter with raw log, keywords and no
        prior history.
        Expected: the log is re-filtered/preprocessed/grouped, a default system
        prompt is installed, and the enhanced prompt (naming the keywords and
        grouped logs) is sent; a "chat_response" with rendered HTML is emitted.
        """
        # Arrange: no conversation history yet → a default system prompt is installed
        self.svc.raw_log_lines = ["l1", "l2"]
        self.svc.conversation_history = []
        self.svc.handle_chat_message.return_value = "ok"

        # Act
        lp.handle_chat_message_with_filter({"message": "explain", "keywords": ["disconnect", "roam"]},
                                           self.socketio)

        # Assert
        mock_filter.assert_called_once_with(["l1", "l2"], ["disconnect", "roam"])
        mock_pre.assert_called_once_with(["f1"])
        mock_group.assert_called_once_with(["p1"])
        sent, helper = self.svc.handle_chat_message.call_args.args
        self.assertIs(helper, self.app_config.llm_helper)
        self.assertIn("following keywords: disconnect, roam", sent)
        self.assertIn("['g1']", sent)
        self.assertTrue(sent.endswith("explain"))
        self.assertEqual(self.svc.chat_system_prompt, "You are an expert log analyzer.")
        self.socketio.emit.assert_called_once_with(
            "chat_response", {"message": "ok", "message_html": "<p>ok</p>"}, namespace="/progress")

    @patch("utils.log_parser_preprocess.filter_log_by_keywords", side_effect=ValueError("bad regex"))
    def test_filter_chat_exception_emits_chat_error(self, mock_filter):
        """filter chat emits a chat error on a processing exception.

        Input: handle_chat_message_with_filter where filtering raises ValueError.
        Expected: a "chat_error" with "Chat failed: ..." is emitted.
        """
        # Arrange
        self.svc.raw_log_lines = ["l1"]

        # Act
        lp.handle_chat_message_with_filter({"message": "hi", "keywords": ["("]}, self.socketio)

        # Assert
        self.socketio.emit.assert_called_once_with("chat_error", {"message": "Chat failed: bad regex"},
                                                   namespace="/progress")

    # -- handle_reset_log_parser_session ---------------------------------------
    def test_reset_session_success(self):
        """reset_session resets the parser and acks the client.

        Input: handle_reset_log_parser_session for a client sid.
        Expected: the service is reset and a "session_reset" success is emitted to
        that sid.
        """
        # Act
        lp.handle_reset_log_parser_session("sid-1", self.socketio)

        # Assert
        self.svc.reset_log_parser.assert_called_once_with()
        self.socketio.emit.assert_called_once_with("session_reset", {"success": True},
                                                   namespace="/progress", to="sid-1")

    def test_reset_session_failure(self):
        """reset_session reports a failure to the client.

        Input: handle_reset_log_parser_session where reset raises RuntimeError.
        Expected: a "session_reset" failure with the error is emitted to the sid
        on app_config.socketio.
        """
        # Arrange
        self.svc.reset_log_parser.side_effect = RuntimeError("busy")

        # Act
        lp.handle_reset_log_parser_session("sid-1")

        # Assert
        self.app_config.socketio.emit.assert_called_once_with(
            "session_reset", {"success": False, "message": "Failed to reset session: busy"},
            namespace="/progress", to="sid-1")


# ═══════════════════════════════════════════════════════ SendTo socket flow
class TestSendtoSocketFlow(LogParserTestCase):

    def _handlers(self, fake_socketio):
        handlers = {}
        fake_socketio.on.side_effect = lambda event, namespace=None: (
            lambda fn: handlers.__setitem__(event, fn) or fn)
        lp.register_socketio_handlers(fake_socketio)
        return handlers

    def test_start_sendto_without_pending_path_emits_error(self):
        """start_sendto errors when no pending path is in session.

        Input: the start_sendto handler with no sendto_pending_path.
        Expected: a "sendto_error" asking to use Send To again is emitted to the
        sid.
        """
        # Arrange
        sio = MagicMock()
        start = self._handlers(sio)["start_sendto"]
        with self.app.test_request_context():
            request.sid = "sid-1"
            # Act
            start()

        # Assert
        sio.emit.assert_called_once_with(
            "sendto_error", {"message": "No pending file in session. Please use Send To again."},
            namespace="/sendto-progress", to="sid-1")

    @patch(f"{ROUTES}.os.path.exists", return_value=False)
    def test_start_sendto_with_vanished_file_emits_error(self, mock_exists):
        """start_sendto errors when the pending file has vanished.

        Input: the start_sendto handler with a pending path whose file no longer
        exists.
        Expected: a "sendto_error" naming the missing file is emitted to the sid.
        """
        # Arrange
        sio = MagicMock()
        start = self._handlers(sio)["start_sendto"]
        with self.app.test_request_context():
            request.sid = "sid-1"
            session["sendto_pending_path"] = os.path.join(SRC_DIR, "wifi.zip")
            # Act
            start()

        # Assert
        sio.emit.assert_called_once_with(
            "sendto_error", {"message": f"File no longer exists: {os.path.join(SRC_DIR, 'wifi.zip')}"},
            namespace="/sendto-progress", to="sid-1")

    @patch(f"{ROUTES}.check_ips_service.needs_ips", return_value=True)
    @patch(f"{ROUTES}.threading.Thread")
    @patch(f"{ROUTES}.os.path.exists", return_value=True)
    def test_start_sendto_needs_ips_emits_error_and_keeps_pending_path(self, mock_exists, mock_thread, mock_needs):
        """start_sendto refuses and keeps the pending path when a case is needed.

        Input: the start_sendto handler with an existing pending file whose IPS
        case number is still unknown.
        Expected: a "sendto_error" asking for the case number is emitted, the
        pending path is kept, and no worker thread is started.
        """
        # Arrange
        sio = MagicMock()
        start = self._handlers(sio)["start_sendto"]
        with self.app.test_request_context():
            request.sid = "sid-1"
            session["sendto_pending_path"] = os.path.join(SRC_DIR, "wifi.zip")
            # Act
            start()
            pending_after = session.get("sendto_pending_path")

        # Assert
        self.assertEqual(pending_after, os.path.join(SRC_DIR, "wifi.zip"))
        mock_thread.assert_not_called()
        self.assertEqual(sio.emit.call_args.args[0], "sendto_error")
        self.assertIn("IPS case number", sio.emit.call_args.args[1]["message"])

    @patch(f"{ROUTES}.threading.Thread")
    @patch(f"{ROUTES}.os.path.exists", return_value=True)
    def test_start_sendto_pops_pending_path_and_starts_daemon_worker(self, mock_exists, mock_thread):
        """start_sendto pops the pending path and starts a daemon worker.

        Input: the start_sendto handler with an existing pending file that does
        not need an IPS case number.
        Expected: the pending path is removed from session, a daemon worker thread
        is started, and no error is emitted.
        """
        # Arrange
        sio = MagicMock()
        start = self._handlers(sio)["start_sendto"]
        with patch(f"{ROUTES}.check_ips_service.needs_ips", return_value=False), \
                self.app.test_request_context():
            request.sid = "sid-1"
            session["sendto_pending_path"] = os.path.join(SRC_DIR, "wifi.zip")
            # Act
            start()
            pending_after = session.get("sendto_pending_path")

        # Assert
        self.assertIsNone(pending_after)
        mock_thread.assert_called_once()
        self.assertIs(mock_thread.call_args.kwargs["daemon"], True)
        mock_thread.return_value.start.assert_called_once_with()
        sio.emit.assert_not_called()

    @patch(f"{ROUTES}.uuid.uuid4")
    @patch(f"{ROUTES}.time.sleep")
    def test_background_worker_success_snapshots_session_and_emits_complete(self, mock_sleep, mock_uuid):
        """The sendto worker snapshots the session and emits completion.

        Input: _run_sendto_in_background for a wifi .etl where processing sets a
        download path and forwards a wpp log, returning a redirect URL.
        Expected: progress events are emitted (including a forwarded wpp log), the
        monkey-patched emit is restored, a token is appended to the redirect URL,
        and the session snapshot is stored under that token.
        """
        # Arrange
        mock_uuid.return_value.hex = "tok123"
        sio = MagicMock()
        original_emit = sio.emit
        lp._sendto_session_store.clear()
        self.addCleanup(lp._sendto_session_store.clear)

        def _fake_process(source_path, source_dir, file_path, original_name, timestamp, progress_cb=None):
            session["download_path"] = source_dir
            sio.emit("wpp_log", {"data": "parsing"}, namespace="/wpp_progress")  # forwarded
            return "/log_chatbot/?auto_run=analyze_all"

        with self.app.test_request_context(), \
                patch(f"{ROUTES}._process_local_analysis", side_effect=_fake_process):
            # Act
            lp._run_sendto_in_background(sio, "sid-1", os.path.join(SRC_DIR, "wifi.etl"))

        # Assert
        self.assertIs(sio.emit, original_emit)  # monkey-patched emit restored
        emitted = [(c.args[0], c.args[1]) for c in original_emit.call_args_list]
        self.assertIn(("sendto_progress", {"pct": 5, "msg": "File validated: wifi.etl"}), emitted)
        self.assertIn(("sendto_progress",
                       {"pct": 10, "msg": "Wi-Fi ETL file detected. Starting WPP/DDD parser…"}), emitted)
        self.assertIn(("sendto_wpp_log", {"data": "parsing"}), emitted)
        self.assertEqual(emitted[-1], ("sendto_complete",
                                       {"redirect_url": "/log_chatbot/?auto_run=analyze_all&_st=tok123"}))
        self.assertEqual(lp._sendto_session_store["tok123"][1], {"download_path": SRC_DIR})

    @patch(f"{ROUTES}.time.sleep")
    def test_background_worker_value_error_emits_sendto_error(self, mock_sleep):
        """The sendto worker emits a sendto_error on a ValueError.

        Input: _run_sendto_in_background where processing raises ValueError.
        Expected: a "sendto_error" with the message is emitted to the sid.
        """
        # Arrange
        sio = MagicMock()
        with self.app.test_request_context(), \
                patch(f"{ROUTES}._process_local_analysis", side_effect=ValueError("No supported files")):
            # Act
            lp._run_sendto_in_background(sio, "sid-1", os.path.join(SRC_DIR, "a.zip"))

        # Assert
        self.assertEqual(sio.emit.call_args.args, ("sendto_error", {"message": "No supported files"}))
        self.assertEqual(sio.emit.call_args.kwargs, {"namespace": "/sendto-progress", "to": "sid-1"})

    @patch(f"{ROUTES}.time.sleep")
    def test_background_worker_unexpected_error_emits_processing_failed(self, mock_sleep):
        """The sendto worker emits processing-failed on an unexpected error.

        Input: _run_sendto_in_background for a .dmp where processing raises
        OSError.
        Expected: a BSOD-copy progress event then a final "sendto_error" with
        "Processing failed: ...".
        """
        # Arrange
        sio = MagicMock()
        with self.app.test_request_context(), \
                patch(f"{ROUTES}._process_local_analysis", side_effect=OSError("share offline")):
            # Act
            lp._run_sendto_in_background(sio, "sid-1", os.path.join(SRC_DIR, "memory.dmp"))

        # Assert
        emitted = [(c.args[0], c.args[1]) for c in sio.emit.call_args_list]
        self.assertIn(("sendto_progress",
                       {"pct": 10, "msg": "BSOD dump detected. Copying to shared folder…"}), emitted)
        self.assertEqual(emitted[-1], ("sendto_error", {"message": "Processing failed: share offline"}))


# ═══════════════════════════════════════════════════════ pure helpers
class TestLogParserHelpers(unittest.TestCase):

    def test_allowed_local_analysis_filenames(self):
        """_is_allowed_local_analysis_filename accepts/rejects by extension.

        Input: lists of allowed and rejected filenames (incl. ddd naming rules).
        Expected: allowed names return True and rejected ones (incl. None) False.
        """
        allowed = ["a.zip", "a.7z", "A.RAR", "x.log", "bt.hci.txt", "w.etl", "w.etl.003",
                   "dddLog_12.bin", "memory.DMP", r"C:\dir\w.etl"]
        rejected = ["", "a.txt", "dddlog_12.bin", "dddLog_x.bin", "a.etlx", "notes.docx", None]
        for name in allowed:
            with self.subTest(allowed=name):
                self.assertTrue(lp._is_allowed_local_analysis_filename(name))
        for name in rejected:
            with self.subTest(rejected=name):
                self.assertFalse(lp._is_allowed_local_analysis_filename(name))

    def test_bt_and_fw_etl_detection_and_case_type(self):
        """BT/FW ETL detection and case-type inference.

        Input: various ETL names passed to _is_bt_etl / _is_fw_etl and a list to
        _infer_local_upload_case_type.
        Expected: BT/FW names are detected correctly and the case type is "bt"
        when a BT file is present, else "wifi".
        """
        self.assertTrue(lp._is_bt_etl(r"C:\x\IbtUsb_1.etl"))
        self.assertTrue(lp._is_bt_etl("ibtpci.etl"))
        self.assertFalse(lp._is_bt_etl("ibtusb.etl.001"))
        self.assertTrue(lp._is_fw_etl("wrt-fw-123.etl"))
        self.assertTrue(lp._is_fw_etl("WRT-FW~1.ETL"))
        self.assertFalse(lp._is_fw_etl("wrt-fw-123.log"))
        self.assertEqual(lp._infer_local_upload_case_type(["ibtusb.etl"]), "bt")
        self.assertEqual(lp._infer_local_upload_case_type([]), "wifi")

    def test_format_bytes(self):
        """_format_bytes renders human-readable sizes.

        Input: byte counts across B/KB/GB/TB ranges.
        Expected: the matching one-decimal unit string.
        """
        self.assertEqual(lp._format_bytes(512), "512.0B")
        self.assertEqual(lp._format_bytes(2048), "2.0KB")
        self.assertEqual(lp._format_bytes(5 * 1024 ** 3), "5.0GB")
        self.assertEqual(lp._format_bytes(3 * 1024 ** 4), "3.0TB")

    def test_render_safe_markdown_strips_scripts(self):
        """_render_safe_markdown_html renders markdown but strips scripts.

        Input: markdown text containing a <script> tag and a link.
        Expected: bold/link are rendered as HTML while the script tag is removed.
        """
        html = lp._render_safe_markdown_html("**bold** <script>alert(1)</script> [x](http://a)")
        self.assertIn("<strong>bold</strong>", html)
        self.assertNotIn("<script", html)
        self.assertIn('<a href="http://a">x</a>', html)

    @patch(f"{ROUTES}.os.path.isdir", return_value=True)
    def test_process_local_analysis_rejects_directory_before_touching_session(self, mock_isdir):
        """_process_local_analysis rejects a directory before touching session.

        Input: _process_local_analysis called with a directory path.
        Expected: a ValueError "Selected path is a folder" is raised.
        """
        with self.assertRaises(ValueError) as ctx:
            lp._process_local_analysis(SRC_DIR, SRC_DIR, SRC_DIR, "local", TIMESTAMP)
        self.assertIn("Selected path is a folder", str(ctx.exception))

    def test_cancel_registry_signal_and_raise(self):
        """The cancel registry signals, raises and tolerates missing entries.

        Input: register an upload, signal cancel, and call _raise_if_cancelled.
        Expected: signalling sets the event and returns True; _raise_if_cancelled
        raises for a set event; after unregister, signalling returns False and
        _raise_if_cancelled is a no-op for None/unset events.
        """
        event = lp._register_local_upload("reg-1")
        try:
            self.assertTrue(lp._signal_cancel_local_upload("reg-1"))
            self.assertTrue(event.is_set())
            with self.assertRaises(lp._LocalAnalysisCancelled):
                lp._raise_if_cancelled(event)
        finally:
            lp._unregister_local_upload("reg-1")
        self.assertFalse(lp._signal_cancel_local_upload("reg-1"))
        lp._raise_if_cancelled(None)  # no-op
        lp._raise_if_cancelled(threading.Event())  # not set → no-op


if __name__ == "__main__":
    unittest.main()
