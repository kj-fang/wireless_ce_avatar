"""Unit tests for the automation blueprint
(blueprints/automation/automation_routes.py).

``GET|POST /automation/run_latest_etl`` only renders the progress page; the
real work is the SocketIO handler ``handle_run_latest_etl`` (event
``trigger_run_latest_etl`` on ``/progress``), which these tests import and call
directly inside a test request context with ``app_config.socketio``,
``download_file``, ``process_single_zip``, ``pick_latest_zip_attachment``,
``subprocess``, ``os.path.exists`` and both analysis-service classes mocked.

Branch-specific: on this branch the ``explorer /select,`` launch is gated by
``app_config.silent_mode`` in both the Wi-Fi and BT branches; tests cover both
values.

Known bug documented (not fixed - source is out of scope): when the ZIP holds
DDD files, the handler references the undefined name ``target_etl`` and
``sorted_etls``, raises ``NameError``, and the outer ``except`` turns it into a
"❌ Exception: ..." stage message - no analysis runs on that path.
``extract_address_digits`` / ``extract_etl_suffix_number`` are pure helpers
and are used unpatched.
"""
import os
import unittest
from unittest.mock import MagicMock, call, patch

from blueprints.automation.automation_routes import (
    automation_bp,
    handle_run_latest_etl,
    register_socketio_handlers,
)
from tests.base_test import TEST_CASE_NBR, BaseTestCase

ROUTES = "blueprints.automation.automation_routes"

ZIP_ITEM = ["logs.zip", "https://ips.example/logs.zip", ["2025-01-01", ""]]
ZIP_PATH = r"C:\dl\00123456\logs.zip"
ETL_DIR = os.sep.join(["C:", "dl", "00123456", "logs_20250101"])
WIFI_ETL_1 = os.sep.join([ETL_DIR, "wifi.etl.1"])
WIFI_ETL_3 = os.sep.join([ETL_DIR, "wifi.etl.3"])
BT_ETL_2 = os.sep.join([ETL_DIR, "bt.etl.2"])
BT_ETL_5 = os.sep.join([ETL_DIR, "bt.etl.5"])
HISTORY_ETL = os.sep.join([ETL_DIR, "History_wifi.etl.9"])
DDD_ETL = os.sep.join([ETL_DIR, "ddd_trace_7.etl"])


class TestAutomationBlueprintStructure(BaseTestCase):
    blueprint = automation_bp

    def test_blueprint_registered_with_url_prefix(self):
        """Verify blueprint registration and the single route.

        Input: the Flask app with ``automation_bp`` registered (via setUp).
        Expected: the blueprint is mounted under ``/automation`` and exposes only
        ``/automation/run_latest_etl`` accepting both GET and POST.
        """
        # Arrange / Act
        rules = {r.rule: r.methods for r in self.app.url_map.iter_rules()
                 if r.endpoint.startswith("automation.")}

        # Assert
        self.assertIs(self.app.blueprints.get("automation"), automation_bp)
        self.assertEqual(automation_bp.url_prefix, "/automation")
        self.assertEqual(set(rules), {"/automation/run_latest_etl"})
        self.assertTrue({"GET", "POST"}.issubset(rules["/automation/run_latest_etl"]))

    def test_register_socketio_handlers_binds_trigger_event(self):
        """The SocketIO registration binds the trigger event.

        Input: a mock socketio passed to register_socketio_handlers.
        Expected: socketio.on is called once for "trigger_run_latest_etl" on the
        "/progress" namespace.
        """
        # Arrange
        socketio = MagicMock()

        # Act
        register_socketio_handlers(socketio)

        # Assert
        socketio.on.assert_called_once_with("trigger_run_latest_etl", namespace="/progress")


@patch(f"{ROUTES}.render_template", return_value="progress-page")
class TestRunLatestEtlRoute(BaseTestCase):
    blueprint = automation_bp

    def test_get_renders_progress_page(self, mock_render):
        """GET renders the progress page.

        Input: GET /automation/run_latest_etl.
        Expected: 200 with the rendered body; render_template is called once with
        "run_progress.html".
        """
        # Act
        resp = self.client.get("/automation/run_latest_etl")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(as_text=True), "progress-page")
        mock_render.assert_called_once_with("run_progress.html")

    def test_post_renders_progress_page(self, mock_render):
        """POST renders the same progress page.

        Input: POST /automation/run_latest_etl.
        Expected: 200; render_template is called once with "run_progress.html".
        """
        # Act
        resp = self.client.post("/automation/run_latest_etl")

        # Assert
        self.assertEqual(resp.status_code, 200)
        mock_render.assert_called_once_with("run_progress.html")


@patch(f"{ROUTES}.BTAnalysisService")
@patch(f"{ROUTES}.WiFiAnalysisService")
@patch(f"{ROUTES}.subprocess")
@patch(f"{ROUTES}.os.path.exists", return_value=True)
@patch(f"{ROUTES}.process_single_zip")
@patch(f"{ROUTES}.download_file", return_value=(ZIP_PATH, "logs.zip", False))
@patch(f"{ROUTES}.pick_latest_zip_attachment", return_value=ZIP_ITEM)
class TestHandleRunLatestEtl(BaseTestCase):
    blueprint = automation_bp

    def _run(self, **ctx_overrides):
        ctx = dict(attachment_list=[ZIP_ITEM], case_download_dir=r"C:\dl\00123456")
        ctx.update(ctx_overrides)
        with self.app.test_request_context("/"):
            from flask import session
            session["case_context"] = self.create_mock_case_context(**ctx)
            handle_run_latest_etl()
        return self.app_config.socketio.emit

    @staticmethod
    def _messages(emit):
        return [c.args[1]["message"] for c in emit.call_args_list
                if c.args and c.args[0] == "stage"]

    def test_wifi_case_picks_highest_suffix_and_runs_wifi_analysis(self, mock_pick, mock_dl, mock_zip, mock_exists, mock_sub, mock_wifi_cls, mock_bt_cls):
        """A wifi case analyzes the highest-suffix ETL and opens Explorer.

        Input: the SocketIO handler with a wifi case; the ZIP yields wifi ETLs
        suffixed 1 and 3 plus an unrelated BT ETL.
        Expected: pick/download/process run in order; Explorer selects wifi.etl.3
        (highest suffix); WiFiAnalysisService.analyze runs on it; BT is untouched;
        the first stage/case_nbr emits and a final "all_done" are sent.
        """
        # Arrange
        mock_zip.return_value = ([WIFI_ETL_1, WIFI_ETL_3], [], [], [BT_ETL_5], [])

        # Act
        emit = self._run()

        # Assert
        mock_pick.assert_called_once_with([ZIP_ITEM])
        mock_dl.assert_called_once_with("logs.zip", ZIP_ITEM[1], r"C:\dl\00123456",
                                        self.app_config.driver_manager, self.app_config.socketio)
        mock_zip.assert_called_once_with(ZIP_PATH, r"C:\dl\00123456", False)
        mock_sub.run.assert_called_once_with(["explorer", "/select,", WIFI_ETL_3])
        mock_wifi_cls.return_value.analyze.assert_called_once_with(WIFI_ETL_3)
        mock_bt_cls.assert_not_called()
        self.assertEqual(emit.call_args_list[:3], [
            call("stage", {"message": "Case Category: Wireless - WiFi"}, namespace="/progress"),
            call("stage", {"message": f"Attachment list: {TEST_CASE_NBR}"}, namespace="/progress"),
            call("case_nbr", {"name": TEST_CASE_NBR}, namespace="/progress"),
        ])
        emit.assert_any_call("all_done", {})
        self.assertIn("📦 Case a0B000000TESTID processing complete ✅", self._messages(emit))

    def test_wifi_case_silent_mode_skips_explorer(self, mock_pick, mock_dl, mock_zip, mock_exists, mock_sub, mock_wifi_cls, mock_bt_cls):
        """Silent mode suppresses Explorer on the wifi path.

        Input: the handler with a wifi case, silent_mode True, one wifi ETL.
        Expected: subprocess.run (Explorer) is not called; analyze still runs on
        the ETL and "all_done" is emitted.
        """
        # Arrange (branch-specific: --silent-mode)
        self.app_config.silent_mode = True
        mock_zip.return_value = ([WIFI_ETL_1], [], [], [], [])

        # Act
        emit = self._run()

        # Assert
        mock_sub.run.assert_not_called()
        mock_wifi_cls.return_value.analyze.assert_called_once_with(WIFI_ETL_1)
        emit.assert_any_call("all_done", {})

    def test_bt_case_drops_wifi_files_and_runs_bt_analysis(self, mock_pick, mock_dl, mock_zip, mock_exists, mock_sub, mock_wifi_cls, mock_bt_cls):
        """A bt case ignores wifi ETLs and analyzes the highest BT ETL.

        Input: the handler with wifi_or_bt="bt"; the ZIP yields one wifi ETL and
        BT ETLs suffixed 2 and 5.
        Expected: Explorer selects bt.etl.5; BTAnalysisService.analyze runs on it
        with mode="AutoFile"; the wifi service is never used; "all_done" emitted.
        """
        # Arrange
        mock_zip.return_value = ([WIFI_ETL_3], [], [], [BT_ETL_2, BT_ETL_5], [])

        # Act
        emit = self._run(wifi_or_bt="bt")

        # Assert
        mock_sub.run.assert_called_once_with(["explorer", "/select,", BT_ETL_5])
        mock_bt_cls.return_value.analyze.assert_called_once_with(BT_ETL_5, mode="AutoFile")
        mock_wifi_cls.assert_not_called()
        emit.assert_any_call("all_done", {})

    def test_bt_case_silent_mode_skips_explorer(self, mock_pick, mock_dl, mock_zip, mock_exists, mock_sub, mock_wifi_cls, mock_bt_cls):
        """Silent mode suppresses Explorer on the bt path.

        Input: the handler with wifi_or_bt="bt", silent_mode True, one BT ETL.
        Expected: subprocess.run (Explorer) is not called; BT analyze still runs
        on the ETL with mode="AutoFile".
        """
        # Arrange (branch-specific: --silent-mode)
        self.app_config.silent_mode = True
        mock_zip.return_value = ([], [], [], [BT_ETL_2], [])

        # Act
        self._run(wifi_or_bt="bt")

        # Assert
        mock_sub.run.assert_not_called()
        mock_bt_cls.return_value.analyze.assert_called_once_with(BT_ETL_2, mode="AutoFile")

    def test_history_files_are_filtered_out(self, mock_pick, mock_dl, mock_zip, mock_exists, mock_sub, mock_wifi_cls, mock_bt_cls):
        """History logs are excluded even when they have the highest suffix.

        Input: the handler with a wifi ETL (suffix 1) and a History_* ETL
        (suffix 9).
        Expected: the History file is skipped; analyze runs on the plain wifi ETL.
        """
        # Arrange: the highest-suffix file is a History log and must be skipped
        mock_zip.return_value = ([WIFI_ETL_1, HISTORY_ETL], [], [], [], [])

        # Act
        self._run()

        # Assert
        mock_wifi_cls.return_value.analyze.assert_called_once_with(WIFI_ETL_1)

    def test_only_history_files_emits_skip_message(self, mock_pick, mock_dl, mock_zip, mock_exists, mock_sub, mock_wifi_cls, mock_bt_cls):
        """When only History logs exist, nothing is analyzed.

        Input: the handler where the ZIP yields only a History_* ETL.
        Expected: a "All files skipped due to 'history'" stage message; no
        analysis and no Explorer launch.
        """
        # Arrange
        mock_zip.return_value = ([HISTORY_ETL], [], [], [], [])

        # Act
        emit = self._run()

        # Assert
        self.assertIn("❌ All files skipped due to 'history'", self._messages(emit))
        mock_wifi_cls.assert_not_called()
        mock_sub.run.assert_not_called()

    def test_ddd_files_hit_known_name_error_bug(self, mock_pick, mock_dl, mock_zip, mock_exists, mock_sub, mock_wifi_cls, mock_bt_cls):
        """Document the known DDD-branch NameError bug.

        Input: the handler where the ZIP yields a wifi ETL plus a DDD file,
        routing into the DDD branch that references undefined ``target_etl``.
        Expected: the NameError is caught and surfaced as a "❌ Exception: ...
        target_etl" stage message; no analysis or Explorer runs.
        """
        # Arrange: KNOWN BUG - DDD branch references undefined `target_etl`
        mock_zip.return_value = ([WIFI_ETL_1], [DDD_ETL], [], [], [])

        # Act
        emit = self._run()

        # Assert: NameError is caught and surfaced; no analysis runs
        messages = self._messages(emit)
        self.assertTrue(any(m.startswith("❌ Exception:") and "target_etl" in m for m in messages), messages)
        mock_wifi_cls.assert_not_called()
        mock_sub.run.assert_not_called()

    def test_no_attachments_emits_error_and_returns(self, mock_pick, mock_dl, mock_zip, mock_exists, mock_sub, mock_wifi_cls, mock_bt_cls):
        """An empty attachment list aborts early with an error.

        Input: the handler with attachment_list=[].
        Expected: a "No attachment found for this case!" stage message (default
        namespace); pick and download are never called.
        """
        # Act
        emit = self._run(attachment_list=[])

        # Assert: note this emit has no namespace (default "/"), unlike the first three
        emit.assert_any_call("stage", {"message": "❌ No attachment found for this case!"})
        mock_pick.assert_not_called()
        mock_dl.assert_not_called()

    def test_missing_download_dir_emits_missing_session_info(self, mock_pick, mock_dl, mock_zip, mock_exists, mock_sub, mock_wifi_cls, mock_bt_cls):
        """A missing download dir aborts with "Missing session info".

        Input: the handler with case_download_dir=None.
        Expected: a "Missing session info" stage message; pick is never called.
        """
        # Act
        emit = self._run(case_download_dir=None)

        # Assert
        emit.assert_any_call("stage", {"message": "❌ Missing session info"})
        mock_pick.assert_not_called()

    def test_no_zip_attachment_emits_error(self, mock_pick, mock_dl, mock_zip, mock_exists, mock_sub, mock_wifi_cls, mock_bt_cls):
        """When no ZIP attachment is found, download is skipped.

        Input: the handler where pick_latest_zip_attachment returns None.
        Expected: a "No zip file found." stage message; download_file is not
        called.
        """
        # Arrange
        mock_pick.return_value = None

        # Act
        emit = self._run()

        # Assert
        emit.assert_any_call("stage", {"message": "❌ No zip file found."})
        mock_dl.assert_not_called()

    def test_failed_download_emits_error(self, mock_pick, mock_dl, mock_zip, mock_exists, mock_sub, mock_wifi_cls, mock_bt_cls):
        """A download returning no path aborts before unzip.

        Input: the handler where download_file returns (None, "logs.zip", False).
        Expected: a "Failed to download logs.zip" stage message; process_single_zip
        is not called.
        """
        # Arrange
        mock_dl.return_value = (None, "logs.zip", False)

        # Act
        emit = self._run()

        # Assert
        emit.assert_any_call("stage", {"message": "❌ Failed to download logs.zip"})
        mock_zip.assert_not_called()

    def test_downloaded_path_missing_on_disk_emits_error(self, mock_pick, mock_dl, mock_zip, mock_exists, mock_sub, mock_wifi_cls, mock_bt_cls):
        """A downloaded path that is absent on disk aborts before unzip.

        Input: the handler where os.path.exists(zip_path) is False.
        Expected: existence is checked on the zip path; a "Failed to download
        logs.zip" stage message is emitted; process_single_zip is not called.
        """
        # Arrange
        mock_exists.return_value = False

        # Act
        emit = self._run()

        # Assert
        mock_exists.assert_called_once_with(ZIP_PATH)
        emit.assert_any_call("stage", {"message": "❌ Failed to download logs.zip"})
        mock_zip.assert_not_called()

    def test_unexpected_exception_is_emitted(self, mock_pick, mock_dl, mock_zip, mock_exists, mock_sub, mock_wifi_cls, mock_bt_cls):
        """Any unexpected exception is caught and surfaced.

        Input: the handler where process_single_zip raises RuntimeError("corrupt
        zip").
        Expected: a "❌ Exception: corrupt zip" stage message; no analysis runs.
        """
        # Arrange
        mock_zip.side_effect = RuntimeError("corrupt zip")

        # Act
        emit = self._run()

        # Assert
        emit.assert_any_call("stage", {"message": "❌ Exception: corrupt zip"})
        mock_wifi_cls.assert_not_called()

    def test_missing_case_context_raises_key_error(self, mock_pick, mock_dl, mock_zip, mock_exists, mock_sub, mock_wifi_cls, mock_bt_cls):
        """A session with no case_context raises KeyError before the try block.

        Input: the handler invoked with an empty session.
        Expected: a KeyError propagates; socketio.emit is never called.
        """
        # Arrange: session read happens before the try block
        # Act / Assert
        with self.app.test_request_context("/"):
            with self.assertRaises(KeyError):
                handle_run_latest_etl()
        self.app_config.socketio.emit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
