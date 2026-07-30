from __future__ import annotations

from datetime import date
from pathlib import Path

from flask import Flask

from services.skill_editor.controller import (
    SkillEditorContext,
    build_skill_editor_handlers,
)
from services.skill_editor.yaml_service import sanitise_skill_payload


def _context(tmp_path: Path) -> SkillEditorContext:
    yaml_path = tmp_path / "skills_2026-07-30.yaml"
    yaml_path.write_text("Example: {}\n", encoding="utf-8")
    return SkillEditorContext(
        activate_yaml=lambda path: [{"id": "Example"}],
        get_active_source=lambda: "cloud",
        get_or_create_agent=lambda: None,
        latest_cloud_baseline=lambda: (yaml_path, date(2026, 7, 30)),
        latest_user_yaml=lambda: (None, None),
        persist_user_yaml_snapshot=lambda data: yaml_path,
        read_yaml_file=lambda path: {"Example": {}},
        refresh_cloud_baseline=lambda: (yaml_path, date(2026, 7, 30)),
        resolve_cloud_skills_dir=lambda: None,
        sanitise_skill_payload=sanitise_skill_payload,
        set_active_source=lambda source: None,
        skills_yaml_status_payload=lambda: {
            "active_source": "cloud",
            "effective_source": "cloud",
        },
    )


def test_shared_skill_editor_status_and_cloud_switch_contract(
    tmp_path: Path,
) -> None:
    handlers = build_skill_editor_handlers(_context(tmp_path))
    app = Flask(__name__)
    app.secret_key = "contract-test"

    with app.test_request_context("/skills_yaml_status"):
        status_response = handlers["skills_yaml_status"]()
        assert status_response.get_json() == {
            "success": True,
            "active_source": "cloud",
            "effective_source": "cloud",
        }

    with app.test_request_context("/skills_yaml_use_cloud", method="POST"):
        cloud_response = handlers["skills_yaml_use_cloud"]()
        payload = cloud_response.get_json()
        assert payload["success"] is True
        assert payload["active_source"] == "cloud"
        assert payload["filename"] == "skills_2026-07-30.yaml"
        assert payload["skills"] == [{"id": "Example"}]


def test_skill_payload_validation_preserves_matching_whitespace() -> None:
    cleaned, error = sanitise_skill_payload({
        "Roaming": {
            "name": "Roaming",
            "description": "Investigate roam decisions",
            "keywords": [" leading-match  ", ""],
            "exclusive": [],
            "expert_rules": {
                "preamble": "Check the transition.",
                "items": [{"prefix": "2-1", "text": "Inspect candidate grade"}],
            },
        },
    })

    assert error == ""
    assert cleaned["Roaming"]["keywords"] == [" leading-match"]
    assert "exclusive" not in cleaned["Roaming"]
    assert cleaned["Roaming"]["expert_rules"] == (
        "Check the transition.\n"
        "2-1. Inspect candidate grade\n"
    )
