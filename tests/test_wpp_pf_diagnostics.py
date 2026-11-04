"""Focused PF request tests without initializing the application's services."""

import ast
import logging
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import requests


class PotatoFarmDiagnosticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1] / "services" / "etl_parser" / "wpp_ddd_parser.py"
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        parser = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Parser")
        method = next(node for node in parser.body
                      if isinstance(node, ast.FunctionDef) and node.name == "__send_rest_call_to_pf")
        parser.body = [method]
        cls.namespace = {}
        exec(compile(ast.Module(body=[parser], type_ignores=[]), str(source), "exec"), cls.namespace)

    def setUp(self):
        self.logger = logging.getLogger("test_wpp_pf_diagnostics")
        self.get = Mock()
        self.emit_error = Mock()
        self.namespace.update(
            log=self.logger,
            requests=SimpleNamespace(get=self.get, RequestException=requests.RequestException),
            POTATO_FARM_SITE_LIST=["primary.invalid", "fallback.invalid"],
            emit_error=self.emit_error,
            WppParserError=RuntimeError,
        )
        self.parser = self.namespace["Parser"]()
        self.parser.wifi_driver_job_name = "WIFI_DRV"

    @staticmethod
    def response(status, body, content_type="application/json"):
        response = requests.Response()
        response.status_code = status
        response.reason = {200: "OK", 401: "Unauthorized", 404: "Not Found"}.get(status, "Server Error")
        response.url = "https://primary.invalid/api/external/get_build"
        response.headers["Content-Type"] = content_type
        response._content = body.encode("utf-8")
        return response

    def call_pf(self):
        return self.parser._Parser__send_rest_call_to_pf("api/external/get_build?jenkins_build_id=136616&strict")

    def test_http_failure_logs_status_and_uses_fallback_without_logging_secrets(self):
        self.get.side_effect = [
            self.response(401, "private response body"),
            self.response(200, '{"drv_branch": "main"}'),
        ]
        with self.assertLogs(self.logger, level="ERROR") as captured:
            result = self.call_pf()
        output = "\n".join(captured.output)
        self.assertIn("401", output)
        self.assertIn("Unauthorized", output)
        self.assertIn("primary.invalid", output)
        self.assertNotIn("private response body", output)
        token = self.get.call_args.kwargs["headers"]["X-Api-Token"]
        self.assertNotIn(token, output)
        self.assertEqual(result, {"drv_branch": "main"})
        self.assertEqual(self.get.call_count, 2)
        self.assertTrue(self.get.call_args.args[0].startswith("https://fallback.invalid/"))
        self.emit_error.assert_not_called()

    def test_network_failure_logs_exception_type_and_uses_fallback(self):
        self.get.side_effect = [
            requests.Timeout("request timed out"),
            self.response(200, '{"drv_branch": "main"}'),
        ]
        with self.assertLogs(self.logger, level="ERROR") as captured:
            self.assertEqual(self.call_pf(), {"drv_branch": "main"})
        self.assertIn("Timeout", "\n".join(captured.output))

    def test_non_json_logs_response_metadata_and_uses_fallback(self):
        self.get.side_effect = [
            self.response(200, "<html>private login page</html>", "text/html"),
            self.response(200, '{"drv_branch": "main"}'),
        ]
        with self.assertLogs(self.logger, level="ERROR") as captured:
            self.call_pf()
        output = "\n".join(captured.output)
        self.assertIn("JSON", output)
        self.assertIn("text/html", output)
        self.assertIn("200", output)
        self.assertNotIn("private login page", output)

    def test_empty_driver_builds_logs_problem_and_uses_fallback(self):
        self.get.side_effect = [
            self.response(200, '{"builds": []}'),
            self.response(200, '{"drv_branch": "main"}'),
        ]
        with self.assertLogs(self.logger, level="ERROR") as captured:
            self.assertEqual(self.call_pf(), {"drv_branch": "main"})
        self.assertIn("WIFI_DRV", "\n".join(captured.output))

    def test_selects_latest_matching_driver_build(self):
        self.get.return_value = self.response(200, """{"builds": [
            {"jenkins_job": "WIFI_DRV", "submission_date": "2026-10-01"},
            {"jenkins_job": "OTHER", "submission_date": "2026-11-03"},
            {"jenkins_job": "WIFI_DRV", "submission_date": "2026-11-02"}
        ]}""")
        self.assertEqual(self.call_pf()["submission_date"], "2026-11-02")
        self.get.assert_called_once()

    def test_malformed_build_response_logs_problem_and_uses_fallback(self):
        for body in ('{"builds": null}', '{"builds": [{}]}', 'null'):
            with self.subTest(body=body):
                self.get.side_effect = [
                    self.response(200, body),
                    self.response(200, '{"drv_branch": "main"}'),
                ]
                with self.assertLogs(self.logger, level="ERROR") as captured:
                    self.assertEqual(self.call_pf(), {"drv_branch": "main"})
                self.assertIn("invalid build response", "\n".join(captured.output))

    def test_unexpected_programming_error_is_not_hidden_as_network_failure(self):
        self.get.side_effect = RuntimeError("unexpected failure")
        with self.assertRaisesRegex(RuntimeError, "unexpected failure"):
            self.call_pf()
        self.get.assert_called_once()
        self.emit_error.assert_not_called()

    def test_all_sites_fail_emits_fatal_error_and_raises(self):
        self.get.side_effect = [
            self.response(404, "private body"),
            self.response(503, "private body"),
        ]
        with self.assertLogs(self.logger, level="ERROR") as captured:
            with self.assertRaises(RuntimeError) as raised:
                self.call_pf()
        output = "\n".join(captured.output)
        self.assertIn("404", output)
        self.assertIn("503", output)
        self.emit_error.assert_called_once()
        self.assertTrue(self.emit_error.call_args.kwargs["fatal"])
        self.assertEqual(self.emit_error.call_args.args[0], str(raised.exception))


if __name__ == "__main__":
    unittest.main()
