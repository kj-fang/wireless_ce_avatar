"""Integration tests for the end-to-end SendTo flow.

See data/sendto_flow_integration_test_plan.md for the scenario table and
Mermaid flow diagram this module implements.

Scope: the Flask+SocketIO request flow only -- open_local_analysis ->
start_sendto -> background worker -> sendto_complete/sendto_error -> session
pickup. OS-level shell integration (Explorer shortcut, app.py browser-focus
logic) is out of scope and not covered here.

Follows the conventions in unit_tests/test_log_parser_blueprint.py: fake
paths under SRC_DIR with os.path.exists/os.path.isdir patched at the point of
use (no real files on disk), and SocketIO handlers invoked directly as plain
functions rather than through a real SocketIO test client.
"""
import os
from unittest.mock import MagicMock, patch

from flask import request, session

import blueprints.log_parser.log_parser_routes as lp
from unit_tests.base_test import BaseTestCase

ROUTES = "blueprints.log_parser.log_parser_routes"
SRC_DIR = r"C:\fake\local"
NON_LOCAL = {"REMOTE_ADDR": "10.0.0.5"}


class SendtoFlowTestCase(BaseTestCase):
    """Shared chaining helpers for the four SendTo flow steps."""

    blueprint = lp.log_parser_bp
    main_stub_endpoints = ("index", "download_result", "download_result_bsod")

    def setUp(self):
        super().setUp()
        lp._sendto_session_store.clear()
        self.addCleanup(lp._sendto_session_store.clear)

    # -- step 1: GET /open_local_analysis -------------------------------------
    def _open(self, path, token="unit-test-token", **kw):
        with patch(f"{ROUTES}.os.path.isdir", return_value=False), \
             patch(f"{ROUTES}.os.path.exists", return_value=True), \
             patch(f"{ROUTES}.check_ips_service") as mock_ips, \
             patch(f"{ROUTES}.render_template", return_value="transmission page"):
            mock_ips.current_case_nbr.return_value = "00123456"  # short-circuits the attach block
            mock_ips.prompt_state.return_value = {"required": False}
            return self.client.get("/log_parser/open_local_analysis",
                                    query_string={"token": token, "path": path}, **kw)

    # -- step 2: start_sendto socket handler ----------------------------------
    @staticmethod
    def _start_handler(sio):
        handlers = {}
        sio.on.side_effect = lambda event, namespace=None: (
            lambda fn: handlers.__setitem__(event, fn) or fn)
        lp.register_socketio_handlers(sio)
        return handlers["start_sendto"]

    def _start(self, sio, pending_path, sid="sid-1", needs_ips=False, file_exists=True):
        start = self._start_handler(sio)
        with patch(f"{ROUTES}.check_ips_service.needs_ips", return_value=needs_ips), \
             patch(f"{ROUTES}.os.path.exists", return_value=file_exists), \
             patch(f"{ROUTES}.threading.Thread") as mock_thread, \
             self.app.test_request_context():
            request.sid = sid
            session["sendto_pending_path"] = pending_path
            start()
            pending_after = session.get("sendto_pending_path")
        return pending_after, mock_thread

    # -- step 3: background worker (run synchronously, in-line) ---------------
    def _run_worker(self, sio, pending_path, sid="sid-1",
                     redirect_url="/log_chatbot/?auto_run=analyze_all", process_side_effect=None):
        def _default_process(source_path, source_dir, file_path, original_name, timestamp, progress_cb=None):
            session["uploaded_source_path"] = source_path
            return redirect_url

        with self.app.test_request_context(), \
             patch(f"{ROUTES}.time.sleep"), \
             patch(f"{ROUTES}._process_local_analysis", side_effect=process_side_effect or _default_process):
            lp._run_sendto_in_background(sio, sid, pending_path)

    @staticmethod
    def _token_from_complete(sio):
        for call in sio.emit.call_args_list:
            if call.args[0] == "sendto_complete":
                return call.args[1]["redirect_url"].rsplit("_st=", 1)[1]
        return None

    # -- step 4: pickup on the browser's next request --------------------------
    def _pickup(self, token):
        return self.client.get(f"/log_parser/verify_sendto_token?_st={token}")


# ═══════════════════════════════════════════════════════════════ happy paths
class TestSendtoFlowHappyPath(SendtoFlowTestCase):

    def test_log_file_end_to_end(self):
        """A .log file flows end-to-end from open to session pickup.

        Input: open_local_analysis for a .log, an accepted start_sendto, a
        successful worker run, then the browser's redirect pickup.
        Expected: the pending path travels through each step; the worker's
        session snapshot is restored exactly once at pickup.
        """
        # Arrange
        path = os.path.join(SRC_DIR, "wifi.log")

        # Act: step 1
        resp1 = self._open(path)
        # Assert: step 1
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(self.get_session()["sendto_pending_path"], path)

        # Act: step 2
        sio = MagicMock()
        pending_after, mock_thread = self._start(sio, path)
        # Assert: step 2
        self.assertIsNone(pending_after)
        mock_thread.assert_called_once()
        sio.emit.assert_not_called()

        # Act: step 3
        self._run_worker(sio, path)
        token = self._token_from_complete(sio)
        # Assert: step 3
        self.assertIsNotNone(token)
        self.assertEqual(lp._sendto_session_store[token][1]["uploaded_source_path"], path)

        # Act: step 4
        resp4 = self._pickup(token)
        # Assert: step 4
        self.assertEqual(resp4.status_code, 200)
        self.assertEqual(self.get_session()["uploaded_source_path"], path)
        self.assertNotIn(token, lp._sendto_session_store)

    def test_zip_file_end_to_end(self):
        """A .zip file flows end-to-end from open to session pickup.

        Input: same 4-step chain as the .log scenario, with a .zip path.
        Expected: identical outcome -- the flow is extension-agnostic once
        validation passes.
        """
        # Arrange
        path = os.path.join(SRC_DIR, "wifi.zip")

        # Act / Assert: step 1
        resp1 = self._open(path)
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(self.get_session()["sendto_pending_path"], path)

        # Act / Assert: step 2
        sio = MagicMock()
        pending_after, mock_thread = self._start(sio, path)
        self.assertIsNone(pending_after)
        mock_thread.assert_called_once()

        # Act / Assert: step 3
        self._run_worker(sio, path)
        token = self._token_from_complete(sio)
        self.assertIsNotNone(token)

        # Act / Assert: step 4
        resp4 = self._pickup(token)
        self.assertEqual(resp4.status_code, 200)
        self.assertEqual(self.get_session()["uploaded_source_path"], path)
        self.assertNotIn(token, lp._sendto_session_store)


# ═══════════════════════════════════════════════════════════════ error paths
class TestSendtoFlowErrorPaths(SendtoFlowTestCase):

    def test_bad_token_blocks_flow_at_open(self):
        """An invalid SendTo token blocks the flow at open_local_analysis.

        Input: GET open_local_analysis with a wrong token.
        Expected: 302 redirect to main.index with a token flash; no pending
        path is stored, so later steps never happen.
        """
        # Act
        resp = self._open(os.path.join(SRC_DIR, "wifi.log"), token="wrong-token")

        # Assert
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith(self.main_stub_path("index")))
        self.assertNotIn("sendto_pending_path", self.get_session())

    def test_non_localhost_blocks_flow_at_open(self):
        """A non-localhost caller blocks the flow at open_local_analysis.

        Input: GET open_local_analysis from a remote address.
        Expected: 302 redirect to main.index with a localhost flash; no
        pending path is stored.
        """
        # Act
        resp = self._open(os.path.join(SRC_DIR, "wifi.log"), environ_base=NON_LOCAL)

        # Assert
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith(self.main_stub_path("index")))
        self.assertNotIn("sendto_pending_path", self.get_session())

    def test_file_vanished_before_start_sendto_emits_error(self):
        """A file deleted after open_local_analysis blocks start_sendto.

        Input: open_local_analysis succeeds (file exists), then start_sendto
        is invoked after the file has vanished (os.path.exists now False).
        Expected: open stores the pending path; start_sendto emits
        sendto_error naming the missing file, starts no worker thread, and
        leaves the pending path in session (retryable).
        """
        # Arrange
        path = os.path.join(SRC_DIR, "wifi.log")
        self._open(path)
        self.assertEqual(self.get_session()["sendto_pending_path"], path)

        # Act
        sio = MagicMock()
        pending_after, mock_thread = self._start(sio, path, file_exists=False)

        # Assert
        mock_thread.assert_not_called()
        sio.emit.assert_called_once_with(
            "sendto_error", {"message": f"File no longer exists: {path}"},
            namespace="/sendto-progress", to="sid-1")
        self.assertEqual(pending_after, path)

    def test_background_exception_emits_error_and_skips_pickup(self):
        """A worker exception emits sendto_error and nothing reaches the store.

        Input: open + start succeed; _process_local_analysis raises
        ValueError.
        Expected: sendto_error is emitted (no sendto_complete); the session
        store stays empty, so no token exists to pick up afterwards.
        """
        # Arrange
        path = os.path.join(SRC_DIR, "corrupt.zip")
        self._open(path)
        sio = MagicMock()
        self._start(sio, path)

        # Act
        self._run_worker(sio, path, process_side_effect=ValueError("No supported files found"))

        # Assert
        self.assertEqual(sio.emit.call_args.args, ("sendto_error", {"message": "No supported files found"}))
        self.assertEqual(sio.emit.call_args.kwargs, {"namespace": "/sendto-progress", "to": "sid-1"})
        self.assertEqual(lp._sendto_session_store, {})

    def test_ttl_expired_token_is_purged_at_pickup(self):
        """A SendTo token older than the TTL is purged, not applied, at pickup.

        Input: a worker run stores a session snapshot under a token; the
        stored entry is aged past _SENDTO_TOKEN_TTL before the browser's
        redirect pickup request.
        Expected: the pickup applies nothing from the snapshot and the store
        entry is purged.
        """
        # Arrange
        path = os.path.join(SRC_DIR, "wifi.log")
        self._open(path)
        sio = MagicMock()
        self._start(sio, path)
        self._run_worker(sio, path)
        token = self._token_from_complete(sio)
        ts, data = lp._sendto_session_store[token]
        lp._sendto_session_store[token] = (ts - lp._SENDTO_TOKEN_TTL - 1, data)

        # Act
        resp = self._pickup(token)

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("uploaded_source_path", self.get_session())
        self.assertNotIn(token, lp._sendto_session_store)


# ══════════════════════════════════════════ worker file-type message dispatch
class TestSendtoWorkerFileTypeDispatch(SendtoFlowTestCase):
    """Only step 3 (the worker) in isolation, across the file types not already
    exercised by the .log/.zip end-to-end chains above.

    _process_local_analysis is faked for every row, so this only proves the
    worker picks the right "detected" branch/message for each filename -- the
    real per-type processing (WPP/DDD parsing, HCI decode, BSOD copy) lives
    inside _process_local_analysis and is out of scope here (see plan notes).
    """

    CASES = [
        ("memory.dmp", "BSOD dump detected. Copying to shared folder\u2026"),
        ("bt.hci.txt", "Bluetooth log detected. Preparing Bluetooth chatbot\u2026"),
        ("ibtusb1.etl", "Bluetooth ETL detected. Starting HCI decode\u2026"),
        ("wrt-fw-1.etl", "FW ETL detected. Preparing\u2026"),
        ("wifi.etl", "Wi-Fi ETL file detected. Starting WPP/DDD parser\u2026"),
    ]

    def test_worker_emits_the_matching_detection_message(self):
        """Each file type triggers its own sendto_progress detection message.

        Input: _run_sendto_in_background for a representative filename of
        each dispatch branch (.dmp, .hci.txt, BT ETL, FW ETL, generic ETL).
        Expected: a pct=10 sendto_progress carrying that branch's message.
        """
        for filename, expected_msg in self.CASES:
            with self.subTest(filename=filename):
                # Arrange
                sio = MagicMock()

                # Act
                self._run_worker(sio, os.path.join(SRC_DIR, filename))

                # Assert
                emitted = [c.args[1] for c in sio.emit.call_args_list if c.args[0] == "sendto_progress"]
                self.assertIn({"pct": 10, "msg": expected_msg}, emitted)
