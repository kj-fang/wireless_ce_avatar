"""Unit tests for the llm blueprint (blueprints/llm/llm_routes.py).

``GET /llm/get_llm_analysis`` asks ``app_config.llm_helper.analyze_desc`` to
summarize/classify the current case, stores the result in the session and
records telemetry through ``gather_service``. The LLM helper is the mocked
``app_config.llm_helper`` (or ``None``), and ``gather_service`` is patched at
point of use, so no LLM or network call is made.
"""
import json
import unittest
from unittest.mock import ANY, patch

from blueprints.llm.llm_routes import llm_bp
from tests.base_test import TEST_CASE_NBR, BaseTestCase

ROUTES = "blueprints.llm.llm_routes"

PROMPT_PATH = r"C:\fake\prompts\wifi_case_summary.txt"
DEFAULT_CLASSIFICATION = {"issue_type": "Unclassified", "confidence": 0, "keywords_found": []}
USAGE = {"llm_calls": 1, "input_tokens": 100, "cache_read_tokens": 0,
         "cache_write_tokens": 0, "output_tokens": 20, "total_tokens": 120}


class TestLlmBlueprintStructure(BaseTestCase):
    blueprint = llm_bp

    def test_blueprint_registered_with_url_prefix(self):
        """Verify blueprint registration and the exposed routes.

        Input: the Flask app with ``llm_bp`` registered (via setUp).
        Expected: the blueprint is mounted under ``/llm`` and exposes the
        analysis route plus the two personal-token endpoints with the right
        HTTP methods.
        """
        # Arrange / Act
        rules = {r.rule: r.methods for r in self.app.url_map.iter_rules()
                 if r.endpoint.startswith("llm.")}

        # Assert
        self.assertIs(self.app.blueprints.get("llm"), llm_bp)
        self.assertEqual(llm_bp.url_prefix, "/llm")
        self.assertEqual(set(rules), {
            "/llm/get_llm_analysis",
            "/llm/personal_token/pending",
            "/llm/personal_token/update",
        })
        self.assertIn("GET", rules["/llm/get_llm_analysis"])
        self.assertIn("GET", rules["/llm/personal_token/pending"])
        self.assertIn("POST", rules["/llm/personal_token/update"])


@patch(f"{ROUTES}.gather_service")
class TestGetLlmAnalysis(BaseTestCase):
    blueprint = llm_bp

    def setUp(self):
        super().setUp()
        self.llm = self.app_config.llm_helper
        self.llm.model = "test-model"

    def _get(self):
        resp = self.client.get("/llm/get_llm_analysis")
        return resp, json.loads(resp.get_data(as_text=True))

    def test_dict_analysis_updates_classification_and_records_success(self, mock_gather):
        """A dict analysis updates the session classification and records success.

        Input: GET /llm/get_llm_analysis where analyze_desc returns a dict with a
        Classification and Summary plus usage, and the session has a prompt path
        and workflow id.
        Expected: 200 JSON {success, ai_analysis}; analyze_desc is called with the
        prompt path, return_usage and the case context; the session stores the
        classification and analysis; gather records a "success" feature usage and
        an attachment declaration tagged domain="wifi".
        """
        # Arrange
        classification = {"issue_type": "Disconnect", "confidence": 85, "keywords_found": ["drop"]}
        analysis = {"Classification": classification, "Summary": "Wi-Fi drops on resume"}
        self.llm.analyze_desc.return_value = (analysis, USAGE)
        self.seed_session(prompt_file_path=PROMPT_PATH, gather_workflow_id="wf-7")

        # Act
        resp, body = self._get()

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/json")
        self.assertEqual(body, {"success": True, "ai_analysis": analysis})
        kwargs = self.llm.analyze_desc.call_args.kwargs
        self.assertEqual(kwargs["prompt_path"], PROMPT_PATH)
        self.assertIs(kwargs["return_usage"], True)
        self.assertEqual(kwargs["case_context"]["case_nbr"], TEST_CASE_NBR)
        self.assertEqual(kwargs["case_context"]["env_detail"]["Computer Model"], "TestModel X1")
        sess = self.get_session()
        self.assertEqual(sess["classification"], classification)
        self.assertEqual(sess["ai_ips_analysis"], analysis)
        mock_gather.record_feature_usage.assert_called_once_with(
            workflow_id="wf-7", feature_code="select_attachments_ai_summary", model="test-model",
            usage=USAGE, issue=ANY, domain="wifi", trigger="click_ai", status="success",
            latency_ms=ANY, error_code="")
        mock_gather.record_attachment_declaration.assert_called_once_with(
            workflow_id="wf-7", ai_analysis=analysis, source="select_attachments_ai_summary",
            issue=ANY, domain="wifi")

    def test_string_analysis_keeps_default_classification(self, mock_gather):
        """A string analysis keeps the default classification.

        Input: GET /llm/get_llm_analysis where analyze_desc returns a plain
        string plus usage.
        Expected: 200; the analysis string is returned, the session keeps the
        default classification, and usage is recorded as "success".
        """
        # Arrange
        self.llm.analyze_desc.return_value = ("plain text summary", USAGE)
        self.seed_session(prompt_file_path=PROMPT_PATH)

        # Act
        resp, body = self._get()

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(body["ai_analysis"], "plain text summary")
        self.assertEqual(self.get_session()["classification"], DEFAULT_CLASSIFICATION)
        self.assertEqual(mock_gather.record_feature_usage.call_args.kwargs["status"], "success")

    def test_empty_analysis_records_failed_status(self, mock_gather):
        """An empty analysis records a failed status.

        Input: GET /llm/get_llm_analysis where analyze_desc returns "" plus usage.
        Expected: 200 with an empty ai_analysis; gather records status "failed"
        with error_code "empty_result".
        """
        # Arrange
        self.llm.analyze_desc.return_value = ("", USAGE)
        self.seed_session(prompt_file_path=PROMPT_PATH)

        # Act
        resp, body = self._get()

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(body, {"success": True, "ai_analysis": ""})
        kwargs = mock_gather.record_feature_usage.call_args.kwargs
        self.assertEqual(kwargs["status"], "failed")
        self.assertEqual(kwargs["error_code"], "empty_result")

    def test_bt_case_records_bt_domain(self, mock_gather):
        """A bt case records telemetry under the bt domain.

        Input: GET /llm/get_llm_analysis with a bt case context.
        Expected: both the feature usage and attachment declaration are tagged
        domain="bt".
        """
        # Arrange
        self.llm.analyze_desc.return_value = ("bt summary", USAGE)
        with self.client.session_transaction() as sess:
            sess["case_context"] = self.create_mock_case_context(wifi_or_bt="bt")
            sess["prompt_file_path"] = PROMPT_PATH

        # Act
        self._get()

        # Assert
        self.assertEqual(mock_gather.record_feature_usage.call_args.kwargs["domain"], "bt")
        self.assertEqual(mock_gather.record_attachment_declaration.call_args.kwargs["domain"], "bt")

    def test_llm_helper_unavailable_returns_placeholder(self, mock_gather):
        """A missing LLM helper returns a placeholder and records nothing.

        Input: GET /llm/get_llm_analysis when app_config.llm_helper is None.
        Expected: 200 with a "LLM helper currently not available" analysis; the
        session stores the default classification and the placeholder; gather
        records neither usage nor a declaration.
        """
        # Arrange
        self.app_config.llm_helper = None
        self.seed_session()

        # Act
        resp, body = self._get()

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(body, {"success": True, "ai_analysis": "LLM helper currently not available"})
        sess = self.get_session()
        self.assertEqual(sess["classification"], DEFAULT_CLASSIFICATION)
        self.assertEqual(sess["ai_ips_analysis"], "LLM helper currently not available")
        mock_gather.record_feature_usage.assert_not_called()
        mock_gather.record_attachment_declaration.assert_not_called()

    def test_llm_exception_returns_500_and_records_failure(self, mock_gather):
        """An LLM exception returns 500 and records a failure.

        Input: GET /llm/get_llm_analysis where analyze_desc raises TimeoutError.
        Expected: 500 with {success: False, error}; gather records a "failed"
        feature usage with zeroed usage and error_code "TimeoutError", no
        declaration, and the session keeps the default classification.
        """
        # Arrange
        self.llm.analyze_desc.side_effect = TimeoutError("LLM timed out")
        self.seed_session(prompt_file_path=PROMPT_PATH, gather_workflow_id="wf-7")

        # Act
        resp, body = self._get()

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(body, {"success": False, "error": "LLM timed out"})
        mock_gather.record_feature_usage.assert_called_once_with(
            workflow_id="wf-7", feature_code="select_attachments_ai_summary", model="test-model",
            usage={"llm_calls": 0, "input_tokens": 0, "cache_read_tokens": 0,
                   "cache_write_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            issue=ANY, domain="wifi", trigger="click_ai", status="failed",
            latency_ms=ANY, error_code="TimeoutError")
        mock_gather.record_attachment_declaration.assert_not_called()
        self.assertEqual(self.get_session()["classification"], DEFAULT_CLASSIFICATION)

    def test_missing_prompt_file_path_returns_500(self, mock_gather):
        """A missing prompt_file_path surfaces as a 500.

        Input: GET /llm/get_llm_analysis with a case context but no
        prompt_file_path in session.
        Expected: 500 whose error mentions prompt_file_path; analyze_desc is not
        called and gather records error_code "KeyError".
        """
        # Arrange: case_context present, prompt_file_path absent -> KeyError inside try
        self.seed_session()

        # Act
        resp, body = self._get()

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertFalse(body["success"])
        self.assertIn("prompt_file_path", body["error"])
        self.llm.analyze_desc.assert_not_called()
        self.assertEqual(mock_gather.record_feature_usage.call_args.kwargs["error_code"], "KeyError")

    def test_missing_case_context_still_calls_llm_with_empty_context(self, mock_gather):
        """A missing case context still calls the LLM with an empty context.

        Input: GET /llm/get_llm_analysis with a prompt path but no case_context.
        Expected: 200; analyze_desc receives an empty case context (case_nbr
        None) and telemetry defaults to domain="wifi".
        """
        # Arrange: no case_context -> from_session({}) -> empty CaseContext
        self.llm.analyze_desc.return_value = ("summary", USAGE)
        with self.client.session_transaction() as sess:
            sess["prompt_file_path"] = PROMPT_PATH

        # Act
        resp, body = self._get()

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self.llm.analyze_desc.call_args.kwargs["case_context"]["case_nbr"])
        self.assertEqual(mock_gather.record_feature_usage.call_args.kwargs["domain"], "wifi")

    def test_telemetry_failure_is_swallowed(self, mock_gather):
        """A telemetry failure does not break the response.

        Input: GET /llm/get_llm_analysis where record_feature_usage raises.
        Expected: 200 with the analysis still returned; the declaration in the
        same try block is skipped (never called).
        """
        # Arrange
        self.llm.analyze_desc.return_value = ("summary", USAGE)
        mock_gather.record_feature_usage.side_effect = RuntimeError("gather down")
        self.seed_session(prompt_file_path=PROMPT_PATH)

        # Act
        resp, body = self._get()

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(body, {"success": True, "ai_analysis": "summary"})
        mock_gather.record_attachment_declaration.assert_not_called()  # same try block, skipped


VALID_TOKEN = "aGVhZGVy.cGF5bG9hZA.c2lnbmF0dXJl"


@patch(f"{ROUTES}._login", return_value="flee5")
class TestPersonalTokenPending(BaseTestCase):
    blueprint = llm_bp

    def _get(self):
        resp = self.client.get("/llm/personal_token/pending")
        return resp, json.loads(resp.get_data(as_text=True))

    def test_pending_true_when_login_flagged(self, _mock_login):
        """A flagged login reports a pending expiry.

        Input: GET /llm/personal_token/pending with the current login present
        in ``app_config.personal_token_expired_pending``.
        Expected: 200 JSON {pending: True, login}.
        """
        # Arrange
        self.app_config.personal_token_expired_pending = {"flee5": True}

        # Act
        resp, body = self._get()

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(body, {"pending": True, "login": "flee5"})

    def test_pending_false_when_not_flagged(self, _mock_login):
        """An unflagged login reports no pending expiry.

        Input: GET /llm/personal_token/pending with an empty pending map.
        Expected: 200 JSON {pending: False, login}.
        """
        # Arrange
        self.app_config.personal_token_expired_pending = {}

        # Act
        resp, body = self._get()

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(body, {"pending": False, "login": "flee5"})

    def test_pending_false_when_no_login(self, mock_login):
        """A missing login reports no pending expiry.

        Input: GET /llm/personal_token/pending when the login cannot be
        resolved.
        Expected: 200 JSON {pending: False, login: ""}.
        """
        # Arrange
        mock_login.return_value = ""
        self.app_config.personal_token_expired_pending = {"flee5": True}

        # Act
        resp, body = self._get()

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(body, {"pending": False, "login": ""})


@patch(f"{ROUTES}._login", return_value="flee5")
class TestUpdatePersonalToken(BaseTestCase):
    blueprint = llm_bp

    def _post(self, payload):
        resp = self.client.post("/llm/personal_token/update", json=payload)
        return resp, json.loads(resp.get_data(as_text=True))

    def test_invalid_token_format_returns_400(self, _mock_login):
        """A malformed token is rejected before any file is touched.

        Input: POST /llm/personal_token/update with a token missing the three
        base64url segments.
        Expected: 400 JSON {ok: False, reason: "invalid_format"}.
        """
        # Act
        resp, body = self._post({"token": "not-a-jwt"})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(body["ok"])
        self.assertEqual(body["reason"], "invalid_format")

    def test_empty_token_returns_400(self, _mock_login):
        """A missing token field is rejected as invalid format.

        Input: POST /llm/personal_token/update with an empty body.
        Expected: 400 JSON {ok: False, reason: "invalid_format"}.
        """
        # Act
        resp, body = self._post({})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(body["reason"], "invalid_format")

    def test_no_login_returns_500(self, mock_login):
        """An unresolved login aborts the refresh.

        Input: POST /llm/personal_token/update with a valid token but no
        resolvable Windows login.
        Expected: 500 JSON {ok: False, reason: "no_login"}.
        """
        # Arrange
        mock_login.return_value = ""

        # Act
        resp, body = self._post({"token": VALID_TOKEN})

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(body["reason"], "no_login")

    def test_missing_avatarfiles_dir_returns_500(self, _mock_login):
        """A missing local files directory aborts the refresh.

        Input: POST /llm/personal_token/update when ``avatarfiles_dir`` is not
        initialised.
        Expected: 500 JSON {ok: False, reason: "no_avatarfiles_dir"}.
        """
        # Arrange
        self.app_config.avatarfiles_dir = None

        # Act
        resp, body = self._post({"token": VALID_TOKEN})

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(body["reason"], "no_avatarfiles_dir")

    @patch("configs.set_up_app.configure_llm_personal_token")
    @patch(f"{ROUTES}._update_keys_module_personal_token", return_value={})
    @patch(f"{ROUTES}._snapshot_and_write", return_value=None)
    def test_success_hot_swaps_and_clears_pending(
            self, mock_write, mock_update_keys, mock_configure, _mock_login):
        """A valid token is written, hot-swapped and clears the pending flag.

        Input: POST /llm/personal_token/update with a valid token, helper/key
        modules present and no cached case context.
        Expected: 200 JSON {ok: True}; the local file is written, the keys map
        updated, the live helper reconfigured and the pending flag cleared.
        """
        # Arrange
        self.app_config.personal_token_expired_pending = {"flee5": True}

        # Act
        resp, body = self._post({"token": VALID_TOKEN})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(body, {"ok": True})
        mock_write.assert_called_once()
        mock_update_keys.assert_called_once()
        mock_configure.assert_called_once()
        self.assertNotIn("flee5", self.app_config.personal_token_expired_pending)

    @patch(f"{ROUTES}._restore")
    @patch(f"{ROUTES}._snapshot_and_write", side_effect=OSError("disk full"))
    def test_local_write_failure_returns_500(self, mock_write, mock_restore, _mock_login):
        """A local write failure aborts before the hot-swap.

        Input: POST /llm/personal_token/update where the local user_keys write
        raises.
        Expected: 500 JSON {ok: False, reason: "local_write_failed"}.
        """
        # Act
        resp, body = self._post({"token": VALID_TOKEN})

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(body["reason"], "local_write_failed")
        mock_restore.assert_not_called()

    @patch(f"{ROUTES}._restore_keys_module_personal_token")
    @patch(f"{ROUTES}._restore")
    @patch("configs.set_up_app.configure_llm_personal_token", side_effect=RuntimeError("swap boom"))
    @patch(f"{ROUTES}._update_keys_module_personal_token", return_value={})
    @patch(f"{ROUTES}._snapshot_and_write", return_value="old content")
    def test_hot_swap_failure_rolls_back_and_returns_500(
            self, mock_write, mock_update_keys, mock_configure,
            mock_restore, mock_restore_keys, _mock_login):
        """A hot-swap failure rolls back the written files and reports 500.

        Input: POST /llm/personal_token/update where ``configure_llm_personal_token``
        raises after the files were written.
        Expected: 500 JSON {ok: False, reason: "hot_swap_failed"}; both the keys
        map and the local file are restored.
        """
        # Act
        resp, body = self._post({"token": VALID_TOKEN})

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(body["reason"], "hot_swap_failed")
        mock_restore_keys.assert_called_once()
        mock_restore.assert_called_once()


if __name__ == "__main__":
    unittest.main()
