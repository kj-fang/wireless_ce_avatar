"""Unit tests for the bsod blueprint (blueprints/bsod/bsod.py).

The single route POSTs a BSOD-analysis request to the PotatoFarm API and
redirects back to ``main.download_result_bsod`` with a flash message. All
external dependencies (email detection, ``requests.post``, API key) are
mocked at their point of use, so no network call is ever made.
"""
import json
import unittest
from unittest.mock import MagicMock, patch

import requests

from blueprints.bsod.bsod import bsod_bp
from unit_tests.base_test import BaseTestCase

ROUTES = "blueprints.bsod.bsod"

USER_EMAIL = "tester@example.com"
DUMPS_PATH = r"C:\fake\dumps\00123456"
API_URL = "https://potatofarm.example/api/bsod"
API_TOKEN = "potato-token"


class TestBsodBlueprintStructure(BaseTestCase):
    blueprint = bsod_bp

    def test_blueprint_registered_with_url_prefix(self):
        """Verify blueprint registration and the submit route.

        Input: the Flask app with ``bsod_bp`` registered (via setUp).
        Expected: the blueprint is mounted under ``/bsod`` and exposes
        ``/bsod/bsod_submit`` accepting both GET and POST.
        """
        # Arrange / Act
        registered = self.app.blueprints.get("bsod")
        rules = {r.rule: r for r in self.app.url_map.iter_rules() if r.endpoint.startswith("bsod.")}

        # Assert
        self.assertIs(registered, bsod_bp)
        self.assertEqual(bsod_bp.url_prefix, "/bsod")
        self.assertIn("/bsod/bsod_submit", rules)
        self.assertTrue({"GET", "POST"}.issubset(rules["/bsod/bsod_submit"].methods))


class TestBsodSubmit(BaseTestCase):
    blueprint = bsod_bp
    main_stub_endpoints = ("download_result_bsod",)

    def setUp(self):
        super().setUp()
        self.app_config.key.potatofarm_url = API_URL
        self.app_config.key.potatofarm_api = API_TOKEN

    def _flashes(self):
        return self.get_session().get("_flashes", [])

    @patch(f"{ROUTES}.requests.post")
    @patch(f"{ROUTES}.helpers.detect_user_email", return_value=USER_EMAIL)
    def test_submit_success_posts_payload_and_redirects(self, mock_email, mock_post):
        """Happy path: posts the BSOD payload and redirects with success flash.

        Input: POST /bsod/bsod_submit with a detected user email and a session
        download_path; the API returns HTTP 200.
        Expected: 302 back to download_result_bsod; requests.post is called with
        the API URL, JSON {DUMPS_PATH, EMAIL_LIST}, the api-token header, no
        proxies, verify=False; a "success" flash with "Submitted Successfully".
        """
        # Arrange
        mock_post.return_value = MagicMock(status_code=200)
        self.seed_session(download_path=DUMPS_PATH)

        # Act
        resp = self.client.post("/bsod/bsod_submit")

        # Assert
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith(self.main_stub_path("download_result_bsod")))
        mock_email.assert_called_once_with()
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(args, (API_URL,))
        self.assertEqual(json.loads(kwargs["data"]), {"DUMPS_PATH": DUMPS_PATH, "EMAIL_LIST": USER_EMAIL})
        self.assertEqual(kwargs["headers"], {"Content-Type": "application/json", "X-Api-Token": API_TOKEN})
        self.assertEqual(kwargs["proxies"], {"http": None, "https": None})
        self.assertIs(kwargs["verify"], False)
        self.assertEqual(self._flashes()[0][0], "success")
        self.assertIn("Submitted Successfully", self._flashes()[0][1])

    @patch(f"{ROUTES}.requests.post")
    @patch(f"{ROUTES}.helpers.detect_user_email", return_value=USER_EMAIL)
    def test_submit_via_get_is_also_accepted(self, mock_email, mock_post):
        """The submit endpoint also works over GET.

        Input: GET /bsod/bsod_submit with a valid email and download_path; API
        returns 200.
        Expected: 302 redirect; requests.post is still called once.
        """
        # Arrange
        mock_post.return_value = MagicMock(status_code=200)
        self.seed_session(download_path=DUMPS_PATH)

        # Act
        resp = self.client.get("/bsod/bsod_submit")

        # Assert
        self.assertEqual(resp.status_code, 302)
        mock_post.assert_called_once()

    @patch(f"{ROUTES}.requests.post")
    @patch(f"{ROUTES}.helpers.detect_user_email", return_value=None)
    def test_missing_email_returns_message_without_calling_api(self, mock_email, mock_post):
        """A missing user email blocks the API call.

        Input: POST /bsod/bsod_submit where detect_user_email returns None.
        Expected: 200 with "Unable to retrieve email"; requests.post is never
        called.
        """
        # Arrange
        self.seed_session(download_path=DUMPS_PATH)

        # Act
        resp = self.client.post("/bsod/bsod_submit")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Unable to retrieve email", resp.get_data(as_text=True))
        mock_post.assert_not_called()

    @patch(f"{ROUTES}.requests.post")
    @patch(f"{ROUTES}.helpers.detect_user_email", return_value=USER_EMAIL)
    def test_missing_download_path_returns_400(self, mock_email, mock_post):
        """A session without download_path returns 400.

        Input: POST /bsod/bsod_submit with a valid email but no download_path in
        session.
        Expected: 400 with "Session expired"; requests.post is never called.
        """
        # Arrange: case_context present, but no download_path in session
        self.seed_session()

        # Act
        resp = self.client.post("/bsod/bsod_submit")

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Session expired", resp.get_data(as_text=True))
        mock_post.assert_not_called()

    @patch(f"{ROUTES}.requests.post")
    @patch(f"{ROUTES}.helpers.detect_user_email", return_value=USER_EMAIL)
    def test_non_200_response_flashes_error_with_status(self, mock_email, mock_post):
        """A non-200 API response flashes an error naming the status.

        Input: POST /bsod/bsod_submit where the API returns HTTP 503 with a body.
        Expected: 302 back to download_result_bsod; an "error" flash containing
        "HTTP 503" but not the response body (body is logged, never shown).
        """
        # Arrange
        mock_post.return_value = MagicMock(status_code=503, text="upstream down")
        self.seed_session(download_path=DUMPS_PATH)

        # Act
        resp = self.client.post("/bsod/bsod_submit")

        # Assert
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith(self.main_stub_path("download_result_bsod")))
        category, message = self._flashes()[0]
        self.assertEqual(category, "error")
        self.assertIn("HTTP 503", message)
        self.assertNotIn("upstream down", message)  # body is logged, never shown to the user

    @patch(f"{ROUTES}.requests.post", side_effect=requests.exceptions.ConnectionError("boom"))
    @patch(f"{ROUTES}.helpers.detect_user_email", return_value=USER_EMAIL)
    def test_request_exception_flashes_error_and_redirects(self, mock_email, mock_post):
        """A network exception flashes an error and still redirects.

        Input: POST /bsod/bsod_submit where requests.post raises
        ConnectionError("boom").
        Expected: 302 redirect; an "error" flash with "An error occurred during
        API call: boom".
        """
        # Arrange
        self.seed_session(download_path=DUMPS_PATH)

        # Act
        resp = self.client.post("/bsod/bsod_submit")

        # Assert
        self.assertEqual(resp.status_code, 302)
        category, message = self._flashes()[0]
        self.assertEqual(category, "error")
        self.assertIn("An error occurred during API call: boom", message)

    @patch(f"{ROUTES}.requests.post")
    @patch(f"{ROUTES}.helpers.detect_user_email", return_value=USER_EMAIL)
    def test_missing_case_context_raises_key_error(self, mock_email, mock_post):
        """An empty session raises KeyError before any work.

        Input: POST /bsod/bsod_submit with no case_context in session.
        Expected: the route indexes session["case_context"] directly, so a
        KeyError propagates (500 in production); neither email detection nor the
        API call runs.
        """
        # Arrange: empty session. The route indexes session["case_context"]
        # directly, so this is an unhandled KeyError (a 500 in production).
        # Act / Assert
        with self.assertRaises(KeyError):
            self.client.post("/bsod/bsod_submit")
        mock_email.assert_not_called()
        mock_post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
