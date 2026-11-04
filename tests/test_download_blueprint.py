"""Unit tests for the download blueprint (blueprints/download/download_routes.py).

The blueprint has one HTTP route (``POST /download/cancel_download``) plus the
SocketIO logic behind the ``start_download`` / ``cancel_download`` events on
the ``/progress`` namespace. The SocketIO handler functions are imported and
called directly inside a test request context. ``threading.Thread`` is patched
so the background worker runs synchronously (deterministic, no real thread),
and the download/decompose helpers are mocked so no browser or file IO runs.
"""
import unittest
from unittest.mock import MagicMock, call, patch

from blueprints.download import download_routes
from blueprints.download.download_routes import (
    download_bp,
    handle_cancel_download,
    handle_start_download,
    register_socketio_handlers,
)
from tests.base_test import TEST_CASE_NBR, BaseTestCase

ROUTES = "blueprints.download.download_routes"

DOWNLOAD_PATH = r"C:\fake\downloads\00123456"
SELECTED_FILES = [["logs.zip", "https://ips.example/logs.zip"]]
ZIP_PATH = r"C:\fake\downloads\00123456\logs.zip"


class _SyncThread:
    """Stand-in for threading.Thread that runs the target on start()."""

    instances = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self.target, self.args, self.kwargs, self.daemon = target, args, kwargs or {}, daemon
        _SyncThread.instances.append(self)

    def start(self):
        self.target(*self.args, **self.kwargs)


class TestDownloadBlueprintStructure(BaseTestCase):
    blueprint = download_bp

    def test_blueprint_registered_with_url_prefix(self):
        """Verify blueprint registration and the cancel route.

        Input: the Flask app with ``download_bp`` registered (via setUp).
        Expected: the blueprint is mounted under ``/download`` and exposes only
        ``/download/cancel_download`` accepting POST (not GET).
        """
        # Arrange / Act
        rules = {r.rule: r.methods for r in self.app.url_map.iter_rules()
                 if r.endpoint.startswith("download.")}

        # Assert
        self.assertIs(self.app.blueprints.get("download"), download_bp)
        self.assertEqual(download_bp.url_prefix, "/download")
        self.assertEqual(set(rules), {"/download/cancel_download"})
        self.assertIn("POST", rules["/download/cancel_download"])
        self.assertNotIn("GET", rules["/download/cancel_download"])

    def test_register_socketio_handlers_binds_progress_events(self):
        """The SocketIO registration binds both progress events.

        Input: a mock socketio passed to register_socketio_handlers.
        Expected: socketio.on is called for "start_download" then
        "cancel_download", both on the "/progress" namespace.
        """
        # Arrange
        socketio = MagicMock()

        # Act
        register_socketio_handlers(socketio)

        # Assert
        self.assertEqual(socketio.on.call_args_list, [
            call("start_download", namespace="/progress"),
            call("cancel_download", namespace="/progress"),
        ])


class TestCancelDownloadRoute(BaseTestCase):
    blueprint = download_bp

    def test_cancel_download_returns_204_and_cancels(self):
        """The cancel route cancels downloads and returns 204.

        Input: POST /download/cancel_download.
        Expected: 204 with an empty body; driver_manager.cancel_downloads is
        called once.
        """
        # Act
        resp = self.client.post("/download/cancel_download")

        # Assert
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(resp.get_data(), b"")
        self.app_config.driver_manager.cancel_downloads.assert_called_once_with()

    def test_cancel_download_rejects_get(self):
        """The cancel route rejects GET.

        Input: GET /download/cancel_download.
        Expected: 405; cancel_downloads is never called.
        """
        # Act
        resp = self.client.get("/download/cancel_download")

        # Assert
        self.assertEqual(resp.status_code, 405)
        self.app_config.driver_manager.cancel_downloads.assert_not_called()


class TestHandleCancelDownload(BaseTestCase):
    blueprint = download_bp

    def test_cancels_and_emits_to_requesting_client(self):
        """The cancel handler cancels downloads and notifies the requester.

        Input: handle_cancel_download(socketio, "sid-123").
        Expected: cancel_downloads runs once and a "download_cancelled" event is
        emitted back only to that client sid on the "/progress" namespace.
        """
        # Arrange
        socketio = MagicMock()

        # Act
        handle_cancel_download(socketio, "sid-123")

        # Assert
        self.app_config.driver_manager.cancel_downloads.assert_called_once_with()
        socketio.emit.assert_called_once_with("download_cancelled", {}, namespace="/progress", to="sid-123")


@patch(f"{ROUTES}.gather_service")
@patch(f"{ROUTES}.attachment_decompose")
@patch(f"{ROUTES}.attachment_download")
@patch(f"{ROUTES}.Thread", new=_SyncThread)
class TestHandleStartDownload(BaseTestCase):
    blueprint = download_bp

    def setUp(self):
        super().setUp()
        _SyncThread.instances = []
        self.socketio = MagicMock()
        self.app_config.driver_manager.download_cancel_event.is_set.return_value = False

    def _run(self, **session_values):
        values = {
            "selected_files": SELECTED_FILES,
            "download_path": DOWNLOAD_PATH,
            "case_context": self.create_mock_case_context(),
            "gather_workflow_id": "wf-1",
        }
        values.update(session_values)
        with self.app.test_request_context("/"):
            from flask import session
            session.update(values)
            handle_start_download(self.socketio)

    def test_wifi_download_decomposes_zip_stores_results_and_emits(self, mock_dl, mock_dec, mock_gather):
        """A wifi download decomposes the zip, stores results and emits them.

        Input: start_download with a wifi case; the downloader yields one zip and
        decompose returns wifi/ddd/evt/bt/fw file lists.
        Expected: the cancel event is cleared, a (non-daemon) worker runs the zip
        through process_single_zip, results are stored per domain (ddd+evt
        merged), and an "all_attachments_download_done" event carries the dicts;
        no cleanup runs.
        """
        # Arrange
        mock_dl.run_dload_threads.return_value = iter([(ZIP_PATH, "logs.zip", False)])
        mock_dec.process_single_zip.return_value = (["w.etl"], ["d.etl"], ["e.evtx"], ["b.etl"], ["f.bin"])

        # Act
        self._run()

        # Assert
        self.app_config.driver_manager.download_cancel_event.clear.assert_called_once_with()
        self.assertEqual(len(_SyncThread.instances), 1)
        self.assertIs(_SyncThread.instances[0].daemon, False)
        args, kwargs = mock_dl.run_dload_threads.call_args
        self.assertEqual(args, (SELECTED_FILES, DOWNLOAD_PATH, self.socketio))
        self.assertTrue(callable(kwargs["result_callback"]))
        mock_dec.process_single_zip.assert_called_once_with(ZIP_PATH, DOWNLOAD_PATH, False)
        expected = {
            "wifi": {"logs.zip": ["w.etl"]},
            "ddd": {"logs.zip": ["d.etl", "e.evtx"]},  # ddd + evt merged
            "bt": {"logs.zip": ["b.etl"]},
            "fw": {"logs.zip": ["f.bin"]},
        }
        self.app_config.set_download_results.assert_called_once_with(TEST_CASE_NBR, **expected)
        self.socketio.emit.assert_called_once_with(
            "all_attachments_download_done",
            {"wifi_dict": expected["wifi"], "ddd_dict": expected["ddd"],
             "bt_dict": expected["bt"], "fw_dict": expected["fw"]},
            namespace="/progress")
        mock_dl.cleanup_incomplete_downloads.assert_not_called()

    def test_bsod_download_skips_decompose_and_emits_dump_path(self, mock_dl, mock_dec, mock_gather):
        """A BSOD download skips decompose and emits the dump path.

        Input: start_download with bsod=True; the downloader yields one zip.
        Expected: process_single_zip and set_download_results are skipped; an
        "all_attachments_download_done_bsod" event carries the dump path.
        """
        # Arrange
        mock_dl.run_dload_threads.return_value = iter([(ZIP_PATH, "dump.zip", False)])

        # Act
        self._run(bsod=True)

        # Assert
        mock_dec.process_single_zip.assert_not_called()
        self.app_config.set_download_results.assert_not_called()
        self.socketio.emit.assert_called_once_with(
            "all_attachments_download_done_bsod", {"dump_path": ZIP_PATH}, namespace="/progress")

    def test_cancelled_download_cleans_up_and_emits_nothing(self, mock_dl, mock_dec, mock_gather):
        """A cancelled download cleans up partial files and emits nothing.

        Input: start_download where the cancel event is set during processing.
        Expected: cleanup_incomplete_downloads runs on the download path; no
        results are stored and no completion event is emitted.
        """
        # Arrange
        mock_dl.run_dload_threads.return_value = iter([(ZIP_PATH, "logs.zip", False)])
        mock_dec.process_single_zip.return_value = ([], [], [], [], [])
        self.app_config.driver_manager.download_cancel_event.is_set.return_value = True

        # Act
        self._run()

        # Assert
        mock_dl.cleanup_incomplete_downloads.assert_called_once_with(DOWNLOAD_PATH)
        self.app_config.set_download_results.assert_not_called()
        self.socketio.emit.assert_not_called()

    def test_no_files_emits_empty_dicts(self, mock_dl, mock_dec, mock_gather):
        """A download with no files emits empty per-domain dicts.

        Input: start_download with selected_files=[]; the downloader yields
        nothing.
        Expected: empty wifi/ddd/bt/fw dicts are stored and emitted in the
        completion event.
        """
        # Arrange
        mock_dl.run_dload_threads.return_value = iter([])

        # Act
        self._run(selected_files=[])

        # Assert
        self.app_config.set_download_results.assert_called_once_with(
            TEST_CASE_NBR, wifi={}, ddd={}, bt={}, fw={})
        self.socketio.emit.assert_called_once_with(
            "all_attachments_download_done",
            {"wifi_dict": {}, "ddd_dict": {}, "bt_dict": {}, "fw_dict": {}},
            namespace="/progress")

    def test_result_callback_records_gather_telemetry(self, mock_dl, mock_dec, mock_gather):
        """The per-file result callback records download telemetry.

        Input: start_download where the downloader invokes result_callback once
        with a successful result dict.
        Expected: gather records the attachment download result with the
        workflow id, name, status, byte/latency/attempt counts, empty error code,
        domain "wifi" and the case issue.
        """
        # Arrange: have the fake downloader invoke the callback once
        def fake_run(selected, path, sock, result_callback=None):
            result_callback({"name": "logs.zip", "status": "ok", "bytes": 2048,
                             "latency_ms": 15, "attempt_count": 1})
            return iter([])
        mock_dl.run_dload_threads.side_effect = fake_run

        # Act
        self._run()

        # Assert
        kwargs = mock_gather.record_attachment_download_result.call_args.kwargs
        self.assertEqual(kwargs["workflow_id"], "wf-1")
        self.assertEqual(kwargs["name"], "logs.zip")
        self.assertEqual(kwargs["status"], "ok")
        self.assertEqual(kwargs["byte_count"], 2048)
        self.assertEqual(kwargs["latency_ms"], 15)
        self.assertEqual(kwargs["attempt_count"], 1)
        self.assertEqual(kwargs["error_code"], "")
        self.assertEqual(kwargs["domain"], "wifi")
        self.assertEqual(kwargs["issue"]["case_nbr"], TEST_CASE_NBR)

    def test_result_callback_defaults_and_swallows_errors(self, mock_dl, mock_dec, mock_gather):
        """The result callback uses defaults and swallows telemetry errors.

        Input: start_download where the callback gets an empty dict and the
        telemetry call raises.
        Expected: defaults are recorded (empty name, status "failed", attempt 0),
        the error is swallowed, and the flow still emits the completion event.
        """
        # Arrange: empty result dict -> defaults; telemetry raises -> swallowed
        mock_gather.record_attachment_download_result.side_effect = RuntimeError("telemetry down")

        def fake_run(selected, path, sock, result_callback=None):
            result_callback({})
            return iter([])
        mock_dl.run_dload_threads.side_effect = fake_run

        # Act (must not raise)
        self._run()

        # Assert
        kwargs = mock_gather.record_attachment_download_result.call_args.kwargs
        self.assertEqual(kwargs["name"], "")
        self.assertEqual(kwargs["status"], "failed")
        self.assertEqual(kwargs["attempt_count"], 0)
        self.socketio.emit.assert_called_once()  # flow continued after the error

    def test_missing_case_context_falls_back_to_empty_context(self, mock_dl, mock_dec, mock_gather):
        """A missing case context falls back to an empty context.

        Input: start_download with case_context=None and no files.
        Expected: results are stored under case_nbr None and the completion event
        is still emitted.
        """
        # Arrange
        mock_dl.run_dload_threads.return_value = iter([])

        # Act
        self._run(case_context=None)

        # Assert: case_nbr None, still emits
        self.app_config.set_download_results.assert_called_once_with(None, wifi={}, ddd={}, bt={}, fw={})
        self.socketio.emit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
