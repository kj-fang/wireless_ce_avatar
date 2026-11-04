"""Unit tests for the analysis_etl blueprint
(blueprints/analysis_etl/analysis_etl_routes.py).

Branch-specific behavior covered here: on this branch both
``/process_etl_path`` and ``/process_etl_path_fw`` only open Windows Explorer
when ``app_config.silent_mode`` is False (``--silent-mode`` CLI flag). The
fixture defaults silent_mode=False; dedicated tests flip it to True and assert
``subprocess.run`` is NOT called while the analysis still runs.

``get_long_path`` / ``to_long_path`` are patched as identity functions so the
route logic is tested independently of Win32 path expansion.
"""
import unittest
from unittest.mock import patch

from blueprints.analysis_etl.analysis_etl_routes import analysis_etl_bp
from tests.base_test import BaseTestCase

ROUTES = "blueprints.analysis_etl.analysis_etl_routes"

ETL_PATH = r"C:\fake\case\WiFi\trace.etl"
ETL_DIR = r"C:\fake\case\WiFi"
FW_PATH = r"C:\fake\case\FW\fw_dump.bin"
FW_DIR = r"C:\fake\case\FW"


def _identity(path):
    return path


class TestAnalysisEtlBlueprintStructure(BaseTestCase):
    blueprint = analysis_etl_bp

    def test_blueprint_registered_with_url_prefix(self):
        """Verify blueprint registration and route wiring.

        Input: the Flask app with ``analysis_etl_bp`` registered (via setUp).
        Expected: the blueprint is mounted under ``/analysis_etl`` and exposes
        exactly the four ETL routes; cancel_fw_analysis accepts POST but not GET.
        """
        # Arrange / Act
        rules = {r.rule: r.methods for r in self.app.url_map.iter_rules()
                 if r.endpoint.startswith("analysis_etl.")}

        # Assert
        self.assertIs(self.app.blueprints.get("analysis_etl"), analysis_etl_bp)
        self.assertEqual(analysis_etl_bp.url_prefix, "/analysis_etl")
        self.assertEqual(set(rules), {
            "/analysis_etl/process_etl_path",
            "/analysis_etl/process_etl_path_fw",
            "/analysis_etl/cancel_fw_analysis",
            "/analysis_etl/fw_analysis_result",
        })
        self.assertIn("POST", rules["/analysis_etl/cancel_fw_analysis"])
        self.assertNotIn("GET", rules["/analysis_etl/cancel_fw_analysis"])


@patch(f"{ROUTES}.to_long_path", side_effect=_identity)
@patch(f"{ROUTES}.get_long_path", side_effect=_identity)
@patch(f"{ROUTES}.subprocess")
@patch(f"{ROUTES}.bt_service")
@patch(f"{ROUTES}.wifi_service")
@patch(f"{ROUTES}.os.path.exists", return_value=True)
class TestProcessEtlPath(BaseTestCase):
    blueprint = analysis_etl_bp

    def test_wifi_case_opens_explorer_and_runs_wifi_analysis(self, mock_exists, mock_wifi, mock_bt, mock_sub, *_):
        """A wifi case triggers wifi analysis and opens Explorer.

        Input: GET /process_etl_path?etl_path=<etl> with a wifi session and
        silent_mode False.
        Expected: 200 with "Analysis triggered"; the path is existence-checked,
        Explorer opens the containing dir, wifi_service.analyze runs, bt does not.
        """
        # Arrange
        self.seed_session()  # wifi_or_bt="wifi"

        # Act
        resp = self.client.get("/analysis_etl/process_etl_path", query_string={"etl_path": ETL_PATH})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertIn(f"Analysis triggered for: {ETL_PATH}", resp.get_data(as_text=True))
        mock_exists.assert_called_once_with(ETL_PATH)
        mock_sub.run.assert_called_once_with(["explorer", ETL_DIR])
        mock_wifi.analyze.assert_called_once_with(ETL_PATH)
        mock_bt.analyze.assert_not_called()

    def test_silent_mode_skips_explorer_but_still_analyzes(self, mock_exists, mock_wifi, mock_bt, mock_sub, *_):
        """Silent mode suppresses Explorer yet still analyzes.

        Input: GET /process_etl_path?etl_path=<etl> with app_config.silent_mode
        set to True.
        Expected: 200; subprocess.run (Explorer) is NOT called but
        wifi_service.analyze still runs on the path.
        """
        # Arrange (branch-specific: --silent-mode)
        self.app_config.silent_mode = True
        self.seed_session()

        # Act
        resp = self.client.get("/analysis_etl/process_etl_path", query_string={"etl_path": ETL_PATH})

        # Assert
        self.assertEqual(resp.status_code, 200)
        mock_sub.run.assert_not_called()
        mock_wifi.analyze.assert_called_once_with(ETL_PATH)

    def test_etl_type_bt_overrides_wifi_hint_and_passes_classification(self, mock_exists, mock_wifi, mock_bt, mock_sub, *_):
        """An explicit etl_type=BT overrides the session's wifi hint.

        Input: GET /process_etl_path with etl_type="BT", mode="Manual", a wifi
        session hint, and a classification of issue_type "Audio".
        Expected: 200; bt_service.analyze is called with the classification's
        issue_type and wifi_or_bt="bt"; wifi analyze is not called; Explorer opens.
        """
        # Arrange: Salesforce hint says wifi, coex caller asks for bt
        self.seed_session(classification={"issue_type": "Audio", "confidence": 90})

        # Act
        resp = self.client.get("/analysis_etl/process_etl_path", query_string={
            "etl_path": ETL_PATH, "etl_type": "BT", "mode": "Manual"})

        # Assert
        self.assertEqual(resp.status_code, 200)
        mock_bt.analyze.assert_called_once_with(ETL_PATH, mode="Manual", issue_type="Audio", wifi_or_bt="bt")
        mock_wifi.analyze.assert_not_called()
        mock_sub.run.assert_called_once_with(["explorer", ETL_DIR])

    def test_bt_hint_without_classification_passes_none_issue_type(self, mock_exists, mock_wifi, mock_bt, mock_sub, *_):
        """A bt session with no classification passes issue_type=None.

        Input: GET /process_etl_path?etl_path=<etl> with a bt case context and no
        classification in session.
        Expected: 200; bt_service.analyze is called with mode="", issue_type=None,
        wifi_or_bt="bt".
        """
        # Arrange
        with self.client.session_transaction() as sess:
            sess["case_context"] = self.create_mock_case_context(wifi_or_bt="bt")

        # Act
        resp = self.client.get("/analysis_etl/process_etl_path", query_string={"etl_path": ETL_PATH})

        # Assert
        self.assertEqual(resp.status_code, 200)
        mock_bt.analyze.assert_called_once_with(ETL_PATH, mode="", issue_type=None, wifi_or_bt="bt")

    def test_nonexistent_path_returns_invalid_message(self, mock_exists, mock_wifi, mock_bt, mock_sub, *_):
        """A path that does not exist yields an "Invalid file path" message.

        Input: GET /process_etl_path?etl_path=<etl> where os.path.exists is False.
        Expected: 200 with "Invalid file path: <etl>"; no Explorer and no analysis
        (wifi or bt) is performed.
        """
        # Arrange
        mock_exists.return_value = False
        self.seed_session()

        # Act
        resp = self.client.get("/analysis_etl/process_etl_path", query_string={"etl_path": ETL_PATH})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertIn(f"Invalid file path: {ETL_PATH}", resp.get_data(as_text=True))
        mock_sub.run.assert_not_called()
        mock_wifi.analyze.assert_not_called()
        mock_bt.analyze.assert_not_called()

    def test_empty_path_returns_invalid_message(self, mock_exists, mock_wifi, mock_bt, mock_sub, *_):
        """An empty etl_path short-circuits before any existence check.

        Input: GET /process_etl_path with no etl_path query param.
        Expected: "Invalid file path" message; os.path.exists is never called and
        no analysis runs.
        """
        # Arrange
        self.seed_session()

        # Act
        resp = self.client.get("/analysis_etl/process_etl_path")

        # Assert
        self.assertIn("Invalid file path", resp.get_data(as_text=True))
        mock_exists.assert_not_called()  # short-circuits on the empty string
        mock_wifi.analyze.assert_not_called()

    def test_unknown_subcategory_returns_400(self, mock_exists, mock_wifi, mock_bt, mock_sub, *_):
        """An unresolvable wifi/bt subcategory returns 400.

        Input: GET /process_etl_path?etl_path=<etl> with no etl_type and a case
        context whose wifi_or_bt is None.
        Expected: 400 with "Unknown case subcategory"; no Explorer and no analysis.
        """
        # Arrange: no etl_type and no wifi_or_bt hint
        with self.client.session_transaction() as sess:
            sess["case_context"] = self.create_mock_case_context(wifi_or_bt=None)

        # Act
        resp = self.client.get("/analysis_etl/process_etl_path", query_string={"etl_path": ETL_PATH})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Unknown case subcategory", resp.get_data(as_text=True))
        mock_sub.run.assert_not_called()
        mock_wifi.analyze.assert_not_called()
        mock_bt.analyze.assert_not_called()

    def test_missing_case_context_raises_key_error(self, mock_exists, mock_wifi, mock_bt, mock_sub, *_):
        """A session with no case_context raises KeyError.

        Input: GET /process_etl_path?etl_path=<etl> with an empty session.
        Expected: the route indexes session["case_context"] directly, so a
        KeyError propagates; no analysis runs.
        """
        # Arrange: empty session - route indexes session["case_context"] directly
        # Act / Assert
        with self.assertRaises(KeyError):
            self.client.get("/analysis_etl/process_etl_path", query_string={"etl_path": ETL_PATH})
        mock_wifi.analyze.assert_not_called()


@patch(f"{ROUTES}.get_long_path", side_effect=_identity)
@patch(f"{ROUTES}.subprocess")
@patch(f"{ROUTES}.fw_service")
class TestProcessEtlPathFw(BaseTestCase):
    blueprint = analysis_etl_bp

    def test_wifi_fw_starts_async_task_and_opens_explorer(self, mock_fw, mock_sub, _):
        """A wifi FW dump starts an async task and opens Explorer.

        Input: GET /process_etl_path_fw?fw_path=<fw> with a wifi session;
        fw_service.start_async returns (task-1, None).
        Expected: 200 with {ok, task_id, fw_path}; start_async is called with the
        path and "wifi"; Explorer opens the containing dir.
        """
        # Arrange
        mock_fw.start_async.return_value = ("task-1", None)
        self.seed_session()

        # Act
        resp = self.client.get("/analysis_etl/process_etl_path_fw", query_string={"fw_path": FW_PATH})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"ok": True, "task_id": "task-1", "fw_path": FW_PATH})
        mock_fw.start_async.assert_called_once_with(FW_PATH, "wifi")
        mock_sub.run.assert_called_once_with(["explorer", FW_DIR])

    def test_fw_type_param_overrides_hint(self, mock_fw, mock_sub, _):
        """An explicit fw_type param overrides the session hint.

        Input: GET /process_etl_path_fw with fw_type="BT" over a wifi session;
        start_async returns (task-2, None).
        Expected: response task_id is "task-2"; start_async is called with "bt".
        """
        # Arrange
        mock_fw.start_async.return_value = ("task-2", None)
        self.seed_session()

        # Act
        resp = self.client.get("/analysis_etl/process_etl_path_fw",
                               query_string={"fw_path": FW_PATH, "fw_type": "BT"})

        # Assert
        self.assertEqual(resp.get_json()["task_id"], "task-2")
        mock_fw.start_async.assert_called_once_with(FW_PATH, "bt")

    def test_silent_mode_skips_explorer(self, mock_fw, mock_sub, _):
        """Silent mode suppresses Explorer for FW analysis.

        Input: GET /process_etl_path_fw?fw_path=<fw> with silent_mode True;
        start_async returns (task-3, None).
        Expected: 200 with ok True; subprocess.run (Explorer) is not called.
        """
        # Arrange (branch-specific: --silent-mode)
        self.app_config.silent_mode = True
        mock_fw.start_async.return_value = ("task-3", None)
        self.seed_session()

        # Act
        resp = self.client.get("/analysis_etl/process_etl_path_fw", query_string={"fw_path": FW_PATH})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])
        mock_sub.run.assert_not_called()

    def test_start_async_failure_returns_400_without_explorer(self, mock_fw, mock_sub, _):
        """A start_async error returns 400 and skips Explorer.

        Input: GET /process_etl_path_fw?fw_path=<fw> where start_async returns
        (None, "fw file not found").
        Expected: 400 with {ok: False, error}; Explorer is not opened.
        """
        # Arrange
        mock_fw.start_async.return_value = (None, "fw file not found")
        self.seed_session()

        # Act
        resp = self.client.get("/analysis_etl/process_etl_path_fw", query_string={"fw_path": FW_PATH})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {"ok": False, "error": "fw file not found"})
        mock_sub.run.assert_not_called()

    def test_unknown_fw_type_returns_400(self, mock_fw, mock_sub, _):
        """An unresolvable FW subcategory returns 400.

        Input: GET /process_etl_path_fw?fw_path=<fw> with no fw_type and a case
        context whose wifi_or_bt is None.
        Expected: 400 with {ok: False, error: "Unknown case subcategory"};
        start_async and Explorer are not called.
        """
        # Arrange
        with self.client.session_transaction() as sess:
            sess["case_context"] = self.create_mock_case_context(wifi_or_bt=None)

        # Act
        resp = self.client.get("/analysis_etl/process_etl_path_fw", query_string={"fw_path": FW_PATH})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {"ok": False, "error": "Unknown case subcategory"})
        mock_fw.start_async.assert_not_called()
        mock_sub.run.assert_not_called()


@patch(f"{ROUTES}.fw_service")
class TestCancelFwAnalysis(BaseTestCase):
    blueprint = analysis_etl_bp

    def test_cancel_with_query_task_id(self, mock_fw):
        """Cancel a FW task via a query-string task_id.

        Input: POST /cancel_fw_analysis?task_id=task-1 where cancel_task is True.
        Expected: 200 with {ok: True, task_id}; cancel_task called with "task-1".
        """
        # Arrange
        mock_fw.cancel_task.return_value = True

        # Act
        resp = self.client.post("/analysis_etl/cancel_fw_analysis", query_string={"task_id": "task-1"})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"ok": True, "task_id": "task-1"})
        mock_fw.cancel_task.assert_called_once_with("task-1")

    def test_cancel_with_json_body_task_id(self, mock_fw):
        """Cancel a FW task via a JSON-body task_id.

        Input: POST /cancel_fw_analysis with JSON {task_id: task-9}; cancel_task
        returns True.
        Expected: 200; cancel_task called with "task-9".
        """
        # Arrange
        mock_fw.cancel_task.return_value = True

        # Act
        resp = self.client.post("/analysis_etl/cancel_fw_analysis", json={"task_id": "task-9"})

        # Assert
        self.assertEqual(resp.status_code, 200)
        mock_fw.cancel_task.assert_called_once_with("task-9")

    def test_cancel_without_task_id_returns_400(self, mock_fw):
        """A cancel request with no task_id returns 400.

        Input: POST /cancel_fw_analysis with no task_id (query or body).
        Expected: 400 with {ok: False, error: "task_id is required"}; cancel_task
        is not called.
        """
        # Act
        resp = self.client.post("/analysis_etl/cancel_fw_analysis")

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {"ok": False, "error": "task_id is required"})
        mock_fw.cancel_task.assert_not_called()

    def test_cancel_unknown_task_returns_404(self, mock_fw):
        """Cancelling an unknown/finished task returns 404.

        Input: POST /cancel_fw_analysis with JSON {task_id: gone}; cancel_task
        returns False.
        Expected: 404 with {ok: False, error: "task not found or already
        finished"}.
        """
        # Arrange
        mock_fw.cancel_task.return_value = False

        # Act
        resp = self.client.post("/analysis_etl/cancel_fw_analysis", json={"task_id": "gone"})

        # Assert
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json(), {"ok": False, "error": "task not found or already finished"})


@patch(f"{ROUTES}.render_template", return_value="rendered-fw")
@patch(f"{ROUTES}.fw_service")
class TestFwAnalysisResult(BaseTestCase):
    blueprint = analysis_etl_bp

    def test_completed_task_renders_template(self, mock_fw, mock_render):
        """A completed FW task renders the result template.

        Input: GET /fw_analysis_result?task_id=task-1 where get_task returns a
        completed task with fw_path and a result dict.
        Expected: 200 with the rendered output; render_template is called with
        fw_analysis.html and the system_info/system_text/log from the result.
        """
        # Arrange
        mock_fw.get_task.return_value = {
            "status": "completed",
            "fw_path": FW_PATH,
            "result": {"system_info": {"fw": "89.1"}, "system_text": "sys text", "log": "log text"},
        }

        # Act
        resp = self.client.get("/analysis_etl/fw_analysis_result", query_string={"task_id": "task-1"})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(as_text=True), "rendered-fw")
        mock_fw.get_task.assert_called_once_with("task-1")
        mock_render.assert_called_once_with(
            "fw_analysis.html", fw_path=FW_PATH, system_info={"fw": "89.1"},
            system_text="sys text", log="log text")

    def test_missing_task_id_returns_400(self, mock_fw, mock_render):
        """A result request with no task_id returns 400.

        Input: GET /fw_analysis_result with no task_id.
        Expected: 400 with "task_id is required"; get_task is not called.
        """
        # Act
        resp = self.client.get("/analysis_etl/fw_analysis_result")

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertIn("task_id is required", resp.get_data(as_text=True))
        mock_fw.get_task.assert_not_called()

    def test_unknown_task_returns_404(self, mock_fw, mock_render):
        """Requesting an unknown task returns 404.

        Input: GET /fw_analysis_result?task_id=nope where get_task returns None.
        Expected: 404 with "task not found: nope"; the template is not rendered.
        """
        # Arrange
        mock_fw.get_task.return_value = None

        # Act
        resp = self.client.get("/analysis_etl/fw_analysis_result", query_string={"task_id": "nope"})

        # Assert
        self.assertEqual(resp.status_code, 404)
        self.assertIn("task not found: nope", resp.get_data(as_text=True))
        mock_render.assert_not_called()

    def test_incomplete_task_returns_400_with_status(self, mock_fw, mock_render):
        """Requesting a not-yet-complete task returns 400 with its status.

        Input: GET /fw_analysis_result?task_id=task-1 where get_task returns a
        task still "running".
        Expected: 400 mentioning "status=running"; the template is not rendered.
        """
        # Arrange
        mock_fw.get_task.return_value = {"status": "running"}

        # Act
        resp = self.client.get("/analysis_etl/fw_analysis_result", query_string={"task_id": "task-1"})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertIn("status=running", resp.get_data(as_text=True))
        mock_render.assert_not_called()


if __name__ == "__main__":
    unittest.main()
