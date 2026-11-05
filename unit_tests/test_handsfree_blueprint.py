"""Unit tests for the handsfree blueprint
(blueprints/handsfree/handsfree_routes.py).

The routes are a thin JSON layer over ``services.handsfree.orchestrator`` and
the ``HandsfreeStore`` draft queue; both are patched at point of use so no
background run starts and nothing is written under ``avatarfiles_dir``.

Branch-specific: this branch wires the IPS "Environment Details" form
(``CaseContext.env_detail``) into the handsfree reader path (commit 5c1658d9).
That logic lives in the service layer, not in the routes, so
``TestHandsfreeEnvDetail`` exercises the pure helpers the flow relies on:
``case_reader._format_env_detail`` (feeds ``{env_block}`` of the reader
prompt) and ``runner._env_repro_steps`` / ``runner._env_brief`` (retract the
"repro steps missing" gap and build the agent's environment brief).
"""
import unittest
from pathlib import Path
from unittest.mock import patch

from blueprints.handsfree.handsfree_routes import handsfree_bp
from services.handsfree.case_reader import READER_PROMPT, _format_env_detail
from services.handsfree.composer import AI_MARKER
from services.handsfree.runner import _env_brief, _env_repro_steps
from unit_tests.base_test import TEST_AVATARFILES_DIR, BaseTestCase

ROUTES = "blueprints.handsfree.handsfree_routes"


class TestHandsfreeBlueprintStructure(BaseTestCase):
    blueprint = handsfree_bp

    def test_blueprint_registered_with_url_prefix(self):
        """Verify blueprint registration and the full route set.

        Input: the Flask app with ``handsfree_bp`` registered (via setUp).
        Expected: the blueprint is mounted under ``/handsfree`` and exposes all
        expected routes; the queue draft route supports GET/PUT/POST and the
        approve route is POST-only.
        """
        # Arrange / Act
        rules = {}
        for r in self.app.url_map.iter_rules():
            if r.endpoint.startswith("handsfree."):
                rules.setdefault(r.rule, set()).update(r.methods)

        # Assert
        self.assertIs(self.app.blueprints.get("handsfree"), handsfree_bp)
        self.assertEqual(handsfree_bp.url_prefix, "/handsfree")
        self.assertEqual(set(rules), {
            "/handsfree/", "/handsfree/check_now", "/handsfree/run_case", "/handsfree/status",
            "/handsfree/queue", "/handsfree/queue/<draft_id>", "/handsfree/queue/<draft_id>/approve",
            "/handsfree/queue/<draft_id>/reject", "/handsfree/config", "/handsfree/discover_fields",
            "/handsfree/auto", "/handsfree/auto/run_now", "/handsfree/list_views",
            "/handsfree/queue/<draft_id>/open_log_folder",
        })
        self.assertTrue({"GET", "PUT", "POST"}.issubset(rules["/handsfree/queue/<draft_id>"]))
        self.assertNotIn("GET", rules["/handsfree/queue/<draft_id>/approve"])


@patch(f"{ROUTES}.HandsfreeStore")
class TestHandsfreeIndexAndConfig(BaseTestCase):
    blueprint = handsfree_bp

    @patch(f"{ROUTES}.render_template", return_value="handsfree-page")
    def test_index_renders_with_store_config(self, mock_render, mock_store_cls):
        """The index page renders with the store's config.

        Input: GET /handsfree/ where the store loads a config dict.
        Expected: 200; the store is built from the handsfree dir and
        handsfree.html is rendered with the loaded config.
        """
        # Arrange
        mock_store_cls.return_value.load_config.return_value = {"owner_name": "Tester", "dry_run": True}

        # Act
        resp = self.client.get("/handsfree/")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(as_text=True), "handsfree-page")
        mock_store_cls.assert_called_once_with(Path(TEST_AVATARFILES_DIR) / "handsfree")
        mock_render.assert_called_once_with("handsfree.html", config={"owner_name": "Tester", "dry_run": True})

    def test_get_config_returns_store_config(self, mock_store_cls):
        """GET /config returns the stored config.

        Input: GET /handsfree/config.
        Expected: 200 with the store config; save_config is not called.
        """
        # Arrange
        mock_store_cls.return_value.load_config.return_value = {"max_cases_per_run": 3}

        # Act
        resp = self.client.get("/handsfree/config")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"max_cases_per_run": 3})
        mock_store_cls.return_value.save_config.assert_not_called()

    def test_post_config_saves_only_allowed_keys(self, mock_store_cls):
        """POST /config persists only allow-listed keys.

        Input: POST /handsfree/config with allowed keys plus extra/unknown keys.
        Expected: 200; save_config is called with only the allowed subset
        (unknown keys dropped).
        """
        # Arrange
        store = mock_store_cls.return_value
        store.save_config.return_value = {"owner_name": "Tester", "dry_run": True}

        # Act
        resp = self.client.post("/handsfree/config", json={
            "owner_name": "Tester", "dry_run": True, "auto_post": True, "evil": "x"})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"owner_name": "Tester", "dry_run": True})
        store.save_config.assert_called_once_with({"owner_name": "Tester", "dry_run": True})

    def test_post_config_without_json_saves_empty_update(self, mock_store_cls):
        """POST /config with a non-JSON body saves an empty update.

        Input: POST /handsfree/config with a plain-text body.
        Expected: 200; save_config is called with {}.
        """
        # Arrange
        mock_store_cls.return_value.save_config.return_value = {}

        # Act
        resp = self.client.post("/handsfree/config", data="not json")

        # Assert
        self.assertEqual(resp.status_code, 200)
        mock_store_cls.return_value.save_config.assert_called_once_with({})


@patch(f"{ROUTES}.orchestrator")
class TestHandsfreeRunRoutes(BaseTestCase):
    blueprint = handsfree_bp

    def test_check_now_passes_owner_name(self, mock_orch):
        """check_now forwards the owner name to the orchestrator.

        Input: POST /handsfree/check_now with an owner_name.
        Expected: 200 echoing the orchestrator result; start_check_now is called
        with the owner name.
        """
        # Arrange
        mock_orch.start_check_now.return_value = {"ok": True, "started": True}

        # Act
        resp = self.client.post("/handsfree/check_now", json={"owner_name": "Tester"})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"ok": True, "started": True})
        mock_orch.start_check_now.assert_called_once_with("Tester")

    def test_check_now_without_body_passes_none(self, mock_orch):
        """check_now passes None when no owner name is given.

        Input: POST /handsfree/check_now with no body.
        Expected: start_check_now is called with None and its error result is
        returned.
        """
        # Arrange
        mock_orch.start_check_now.return_value = {"ok": False, "error": "owner_name required"}

        # Act
        resp = self.client.post("/handsfree/check_now")

        # Assert
        self.assertEqual(resp.get_json(), {"ok": False, "error": "owner_name required"})
        mock_orch.start_check_now.assert_called_once_with(None)

    def test_run_case_passes_case_nbr(self, mock_orch):
        """run_case forwards the case number to the orchestrator.

        Input: POST /handsfree/run_case with a case_nbr.
        Expected: 200 echoing the result; start_case_run is called with the case
        number.
        """
        # Arrange
        mock_orch.start_case_run.return_value = {"ok": True, "case_nbr": "00123456"}

        # Act
        resp = self.client.post("/handsfree/run_case", json={"case_nbr": "00123456"})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"ok": True, "case_nbr": "00123456"})
        mock_orch.start_case_run.assert_called_once_with("00123456")

    def test_run_case_without_body_passes_none(self, mock_orch):
        """run_case passes None when no case number is given.

        Input: POST /handsfree/run_case with no body.
        Expected: start_case_run is called with None and its error result is
        returned.
        """
        # Arrange
        mock_orch.start_case_run.return_value = {"ok": False, "error": "case_nbr required"}

        # Act
        resp = self.client.post("/handsfree/run_case")

        # Assert
        self.assertEqual(resp.get_json()["ok"], False)
        mock_orch.start_case_run.assert_called_once_with(None)

    def test_status_returns_run_state(self, mock_orch):
        """status returns the orchestrator run state.

        Input: GET /handsfree/status.
        Expected: 200 with the run-state dict from get_run_state.
        """
        # Arrange
        mock_orch.get_run_state.return_value = {"running": False, "events": []}

        # Act
        resp = self.client.get("/handsfree/status")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"running": False, "events": []})

    def test_approve_passes_edited_plain(self, mock_orch):
        """approve forwards the edited draft text.

        Input: POST /handsfree/queue/d-1/approve with draft_plain.
        Expected: approve_and_post is called with the draft id and edited_plain;
        its result is returned.
        """
        # Arrange
        mock_orch.approve_and_post.return_value = {"ok": True, "posted": True}

        # Act
        resp = self.client.post("/handsfree/queue/d-1/approve", json={"draft_plain": "edited text"})

        # Assert
        self.assertEqual(resp.get_json(), {"ok": True, "posted": True})
        mock_orch.approve_and_post.assert_called_once_with("d-1", edited_plain="edited text")

    def test_approve_without_body_passes_none(self, mock_orch):
        """approve passes edited_plain=None with no body.

        Input: POST /handsfree/queue/d-1/approve with no body.
        Expected: approve_and_post is called with edited_plain=None.
        """
        # Arrange
        mock_orch.approve_and_post.return_value = {"ok": True}

        # Act
        self.client.post("/handsfree/queue/d-1/approve")

        # Assert
        mock_orch.approve_and_post.assert_called_once_with("d-1", edited_plain=None)

    def test_approve_rejects_get(self, mock_orch):
        """approve rejects GET.

        Input: GET /handsfree/queue/d-1/approve.
        Expected: 405; approve_and_post is never called (posting is POST-only).
        """
        # Act
        resp = self.client.get("/handsfree/queue/d-1/approve")

        # Assert: posting only via explicit POST
        self.assertEqual(resp.status_code, 405)
        mock_orch.approve_and_post.assert_not_called()

    def test_reject_passes_reason(self, mock_orch):
        """reject forwards the rejection reason.

        Input: POST /handsfree/queue/d-1/reject with a reason.
        Expected: reject is called with the draft id and reason.
        """
        # Arrange
        mock_orch.reject.return_value = {"ok": True}

        # Act
        resp = self.client.post("/handsfree/queue/d-1/reject", json={"reason": "wrong case"})

        # Assert
        self.assertEqual(resp.get_json(), {"ok": True})
        mock_orch.reject.assert_called_once_with("d-1", reason="wrong case")

    def test_reject_without_reason_defaults_to_empty(self, mock_orch):
        """reject defaults the reason to empty.

        Input: POST /handsfree/queue/d-1/reject with no reason.
        Expected: reject is called with reason="".
        """
        # Arrange
        mock_orch.reject.return_value = {"ok": True}

        # Act
        self.client.post("/handsfree/queue/d-1/reject")

        # Assert
        mock_orch.reject.assert_called_once_with("d-1", reason="")

    def test_discover_fields_success(self, mock_orch):
        """discover_fields returns the discovered REST fields.

        Input: POST /handsfree/discover_fields.
        Expected: 200 with the fields from discover_rest_fields.
        """
        # Arrange
        mock_orch.discover_rest_fields.return_value = {"fields": ["CommentBody"]}

        # Act
        resp = self.client.post("/handsfree/discover_fields")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"fields": ["CommentBody"]})

    def test_discover_fields_exception_returns_500(self, mock_orch):
        """A discover_fields exception returns 500 with the error type.

        Input: POST /handsfree/discover_fields where discovery raises
        ConnectionError.
        Expected: 500 with {"error": "ConnectionError: IPS unreachable"}.
        """
        # Arrange
        mock_orch.discover_rest_fields.side_effect = ConnectionError("IPS unreachable")

        # Act
        resp = self.client.post("/handsfree/discover_fields")

        # Assert
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"error": "ConnectionError: IPS unreachable"})


@patch(f"{ROUTES}.HandsfreeStore")
class TestHandsfreeQueueRoutes(BaseTestCase):
    blueprint = handsfree_bp

    def test_list_queue_defaults_to_open_drafts(self, mock_store_cls):
        """Listing the queue defaults to open drafts only.

        Input: GET /handsfree/queue with no query params.
        Expected: 200 with {items}; list_drafts is called with
        include_closed=False.
        """
        # Arrange
        mock_store_cls.return_value.list_drafts.return_value = [{"draft_id": "d-1"}]

        # Act
        resp = self.client.get("/handsfree/queue")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"items": [{"draft_id": "d-1"}]})
        mock_store_cls.return_value.list_drafts.assert_called_once_with(include_closed=False, owner_name="")

    def test_list_queue_all_flag_variants_include_closed(self, mock_store_cls):
        """The ?all flag's truthy variants include closed drafts.

        Input: GET /handsfree/queue?all=<value> for truthy and falsy variants.
        Expected: list_drafts is called with include_closed matching the
        truthiness of each value.
        """
        # Arrange
        store = mock_store_cls.return_value
        store.list_drafts.return_value = []

        for value, expected in (("1", True), ("TRUE", True), ("yes", True), ("0", False), ("no", False)):
            with self.subTest(all=value):
                store.list_drafts.reset_mock()

                # Act
                self.client.get("/handsfree/queue", query_string={"all": value})

                # Assert
                store.list_drafts.assert_called_once_with(include_closed=expected, owner_name="")

    def test_get_draft_found(self, mock_store_cls):
        """Fetching an existing draft returns it.

        Input: GET /handsfree/queue/d-1 where the store has that draft.
        Expected: 200 with the draft record; get is called with "d-1".
        """
        # Arrange
        rec = {"draft_id": "d-1", "status": "pending", "draft_plain": "text"}
        mock_store_cls.return_value.get.return_value = rec

        # Act
        resp = self.client.get("/handsfree/queue/d-1")

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), rec)
        mock_store_cls.return_value.get.assert_called_once_with("d-1")

    def test_get_draft_not_found_returns_404(self, mock_store_cls):
        """Fetching a missing draft returns 404.

        Input: GET /handsfree/queue/missing where the store returns None.
        Expected: 404 with {"error": "not found"}.
        """
        # Arrange
        mock_store_cls.return_value.get.return_value = None

        # Act
        resp = self.client.get("/handsfree/queue/missing")

        # Assert
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json(), {"error": "not found"})

    def test_save_draft_prepends_ai_marker_and_composes_html(self, mock_store_cls):
        """Saving a draft prepends the AI marker and composes HTML.

        Input: PUT /handsfree/queue/d-1 with multi-line, HTML-unsafe draft text.
        Expected: 200; update is called with the trimmed text prefixed by the AI
        marker and an HTML rendering with escaped characters and <br/> breaks.
        """
        # Arrange
        store = mock_store_cls.return_value
        store.update.side_effect = lambda draft_id, **fields: {"draft_id": draft_id, **fields}

        # Act
        resp = self.client.put("/handsfree/queue/d-1", json={"draft_plain": "  Line 1\nLine <2>  "})

        # Assert
        expected_plain = AI_MARKER + "\n\nLine 1\nLine <2>"
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        store.update.assert_called_once_with(
            "d-1", draft_plain=expected_plain,
            draft_html="<p>" + AI_MARKER + "<br/><br/>Line 1<br/>Line &lt;2&gt;</p>")
        self.assertEqual(body["draft"]["draft_plain"], expected_plain)

    def test_save_draft_keeps_existing_marker_via_post(self, mock_store_cls):
        """Saving an already-marked draft (via POST) keeps the single marker.

        Input: POST /handsfree/queue/d-1 with text already carrying the AI marker.
        Expected: 200; update stores the text unchanged (no duplicate marker).
        """
        # Arrange
        plain = AI_MARKER + "\n\nalready marked"
        mock_store_cls.return_value.update.return_value = {"draft_id": "d-1"}

        # Act
        resp = self.client.post("/handsfree/queue/d-1", json={"draft_plain": plain})

        # Assert
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_store_cls.return_value.update.call_args.kwargs["draft_plain"], plain)

    def test_save_draft_empty_returns_400(self, mock_store_cls):
        """Saving a blank draft returns 400.

        Input: PUT /handsfree/queue/d-1 with whitespace-only text.
        Expected: 400 with {ok: False, error: "empty draft"}; update is not
        called.
        """
        # Act
        resp = self.client.put("/handsfree/queue/d-1", json={"draft_plain": "   "})

        # Assert
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {"ok": False, "error": "empty draft"})
        mock_store_cls.return_value.update.assert_not_called()

    def test_save_draft_unknown_id_returns_404(self, mock_store_cls):
        """Saving to an unknown draft id returns 404.

        Input: PUT /handsfree/queue/missing where update returns None.
        Expected: 404 with {ok: False, error: "not found"}.
        """
        # Arrange
        mock_store_cls.return_value.update.return_value = None

        # Act
        resp = self.client.put("/handsfree/queue/missing", json={"draft_plain": "text"})

        # Assert
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json(), {"ok": False, "error": "not found"})


class TestHandsfreeEnvDetail(unittest.TestCase):
    """Branch-specific: IPS Environment Details (env_detail) in the reader path."""

    def test_reader_prompt_has_env_block_placeholder(self):
        """The reader prompt exposes the env-block placeholder.

        Input: the READER_PROMPT template.
        Expected: it contains "{env_block}", the slot the env detail is formatted
        into.
        """
        # Assert: read_case_history() formats env_detail into this slot
        self.assertIn("{env_block}", READER_PROMPT)

    def test_format_env_detail_skips_blank_answers(self):
        """_format_env_detail skips blank answers and trims keys.

        Input: an env dict with a blank answer and a padded key.
        Expected: only non-blank entries remain, with keys trimmed, one per line.
        """
        # Arrange
        env = {"Computer Model": "TestModel X1", "Steps to reproduce": "   ", " OS ": "Windows 11"}

        # Act
        block = _format_env_detail(env)

        # Assert
        self.assertEqual(block, "Computer Model: TestModel X1\nOS: Windows 11")

    def test_format_env_detail_empty_or_invalid_is_none_marker(self):
        """_format_env_detail returns "(none)" for empty/invalid input.

        Input: empty dict, None, a non-dict, and a dict of only blank answers.
        Expected: each yields the "(none)" marker.
        """
        for env in ({}, None, "not a dict", {"Platform": ""}):
            with self.subTest(env=env):
                self.assertEqual(_format_env_detail(env), "(none)")

    def test_format_env_detail_truncates_long_answers_and_caps_entries(self):
        """_format_env_detail truncates long answers and caps entry count.

        Input: 30 entries where one answer is 400 chars long.
        Expected: at most 25 lines (_MAX_ENV_ENTRIES); the long answer is
        truncated to 300 chars with a "…[truncated]" suffix.
        """
        # Arrange
        env = {f"Q{i}": "x" for i in range(30)}
        env["Q0"] = "a" * 400

        # Act
        lines = _format_env_detail(env).splitlines()

        # Assert
        self.assertEqual(len(lines), 25)  # _MAX_ENV_ENTRIES
        self.assertEqual(lines[0], "Q0: " + "a" * 300 + " …[truncated]")

    def test_env_repro_steps_returns_filled_answer(self):
        """_env_repro_steps returns the trimmed repro-steps answer.

        Input: an env dict with a padded "Steps to reproduce" answer.
        Expected: the trimmed answer is returned.
        """
        # Arrange / Act / Assert
        self.assertEqual(
            _env_repro_steps({"Computer Model": "X1", "Steps to reproduce": " 1. Sleep 2. Resume "}),
            "1. Sleep 2. Resume")

    def test_env_repro_steps_ignores_placeholder_answers(self):
        """_env_repro_steps ignores placeholder answers.

        Input: placeholder answers ("", "NA", "n/a", "None", blank) and None.
        Expected: each yields an empty string.
        """
        for answer in ("", "NA", "n/a", "None", "   "):
            with self.subTest(answer=answer):
                self.assertEqual(_env_repro_steps({"Steps to reproduce": answer}), "")
        self.assertEqual(_env_repro_steps(None), "")

    def test_env_brief_keeps_only_platform_fields(self):
        """_env_brief keeps only the platform-relevant fields.

        Input: an env dict mixing platform fields, a placeholder OS answer and an
        ignored note.
        Expected: a "; "-joined brief of only the recognized platform fields,
        trimmed.
        """
        # Arrange
        env = {"Platform": "Lunar Lake", "Found in build": "  23.40  ", "Notes": "ignored",
               "Operating System": "n/a", "Computer Model": "TestModel X1"}

        # Act
        brief = _env_brief(env)

        # Assert
        self.assertEqual(brief, "Platform: Lunar Lake; Found in build: 23.40; Computer Model: TestModel X1")

    def test_env_brief_respects_limit(self):
        """_env_brief respects the character limit.

        Input: an over-long platform value, and a small explicit limit.
        Expected: the output is capped at the default 300 chars, and the explicit
        limit truncates mid-field.
        """
        self.assertEqual(len(_env_brief({"Platform": "p" * 500})), 300)
        self.assertEqual(_env_brief({"Platform": "Lunar Lake"}, limit=8), "Platform")


if __name__ == "__main__":
    unittest.main()
