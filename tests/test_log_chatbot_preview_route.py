"""Tests for the read-only active-log preview endpoint."""

import ast
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from flask import Flask, Response, jsonify, request


class PreviewLogRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1] / "blueprints" / "log_chatbot" / "log_chatbot_routes.py"
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        routes = [node for node in tree.body
                  if isinstance(node, ast.FunctionDef)
                  and node.name in {"preview_log", "preview_log_chunk"}]
        for route in routes:
            route.decorator_list = []
        cls.namespace = {
            "os": os,
            "Response": Response,
            "jsonify": jsonify,
            "request": request,
            "url_for": lambda endpoint: f"/log_chatbot/{endpoint.rsplit('.', 1)[-1]}",
            "LOG_PREVIEW_CHUNK_BYTES": 1024 * 1024,
        }
        exec(compile(ast.Module(body=routes, type_ignores=[]), str(source), "exec"), cls.namespace)
        cls.app = Flask(__name__)

    def test_metadata_identifies_active_log_and_chunk_endpoint(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "selected.log"
            source = "01\t10/28/2026 10:11:12\r\n02\tEvent\twith tabs\n"
            path.write_text(source, encoding="utf-8", newline="")
            agent = type("Agent", (), {"current_log_path": str(path)})()
            self.namespace["_get_or_create_agent"] = lambda: agent
            with self.app.test_request_context("/"):
                response = self.app.make_response(self.namespace["preview_log"]())

        self.assertTrue(response.json["success"])
        self.assertEqual(response.json["name"], "selected.log")
        self.assertEqual(response.json["size"], len(source.encode("utf-8")))
        self.assertEqual(response.json["chunk_size"], 1024 * 1024)
        self.assertEqual(response.json["chunk_url"], "/log_chatbot/preview_log_chunk")

    def test_chunk_endpoint_returns_raw_bytes_and_offsets(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "selected.log"
            source = ("01\t10/28/2026 10:11:12\r\n02\tEvent\twith tabs\n" * 40000)
            path.write_text(source, encoding="utf-8", newline="")
            agent = type("Agent", (), {"current_log_path": str(path)})()
            self.namespace["_get_or_create_agent"] = lambda: agent
            with self.app.test_request_context("/?offset=0"):
                first = self.namespace["preview_log_chunk"]()
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.data, source.encode("utf-8")[:1024 * 1024])
            self.assertEqual(first.headers["X-Next-Offset"], str(1024 * 1024))
            self.assertEqual(first.headers["X-Log-Eof"], "false")
            with self.app.test_request_context(f"/?offset={1024 * 1024}"):
                second = self.namespace["preview_log_chunk"]()
            self.assertEqual(second.data, source.encode("utf-8")[1024 * 1024:2 * 1024 * 1024])

    def test_rejects_missing_or_unavailable_selected_log(self):
        agent = type("Agent", (), {"current_log_path": ""})()
        self.namespace["_get_or_create_agent"] = lambda: agent
        with self.app.test_request_context("/?offset=0"):
            missing = self.app.make_response(self.namespace["preview_log"]())
            missing_chunk = self.namespace["preview_log_chunk"]()
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing_chunk.status_code, 400)

        agent.current_log_path = os.path.join(os.path.abspath(os.sep), "missing-preview-log.log")
        with self.app.test_request_context("/?offset=0"):
            unavailable = self.app.make_response(self.namespace["preview_log"]())
        self.assertEqual(unavailable.status_code, 404)

    def test_chunk_endpoint_rejects_invalid_offsets(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "selected.log"
            path.write_text("one\ntwo\n", encoding="utf-8")
            agent = type("Agent", (), {"current_log_path": str(path)})()
            self.namespace["_get_or_create_agent"] = lambda: agent
            with self.app.test_request_context("/?offset=-1"):
                negative = self.namespace["preview_log_chunk"]()
            with self.app.test_request_context("/?offset=not-a-number"):
                invalid = self.namespace["preview_log_chunk"]()
            with self.app.test_request_context("/?offset=100"):
                beyond_end = self.namespace["preview_log_chunk"]()
        self.assertEqual(negative.status_code, 416)
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(beyond_end.status_code, 416)


if __name__ == "__main__":
    unittest.main()
