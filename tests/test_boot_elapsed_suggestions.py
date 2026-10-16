"""An unanchored dmesg must never be presented as a calendar issue time."""

import json
from types import SimpleNamespace

from utils import log_time_format
from utils.issue_time_ai import build_issue_time_suggestions, LINUX_WIFI_FOCUS


def _client(answer):
    def create(**kwargs):
        assert "NEVER invent" in kwargs["messages"][0]["content"]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps(answer)))])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_unanchored_dmesg_suggests_only_observed_boot_offset(tmp_path):
    path = tmp_path / "dmesg.log"
    lines = [f"[{i:9.6f}] iwlwifi startup\n" for i in range(30)]
    lines.append("[55.510209] iwlwifi firmware crash\n")
    path.write_text("".join(lines), encoding="utf-8")
    fmt = log_time_format.detect(str(path))
    assert fmt.kind == "relative" and fmt.anchor is None

    result = build_issue_time_suggestions(
        text="Wi-Fi firmware crashed", log_lines=lines, log_format=fmt,
        llm_client=_client({"interpretation": "firmware failure", "suggestions": [
            {"boot_elapsed": "55.5", "confidence": "high", "reason": "firmware crash"},
            {"boot_elapsed": "9999", "confidence": "high", "reason": "made up"},
        ]}), llm_model="fake", focus=LINUX_WIFI_FOCUS,
    )
    assert result["success"] and result["time_basis"] == "boot_elapsed"
    assert len(result["suggestions"]) == 1
    assert result["suggestions"][0]["boot_elapsed"] == "55.510209"
    assert "firmware crash" in result["suggestions"][0]["log_line"]
    assert "issue_time" not in result["suggestions"][0]


def test_unanchored_dmesg_never_falls_back_to_fake_time(tmp_path):
    path = tmp_path / "dmesg.log"
    lines = [f"[{i:9.6f}] kernel startup\n" for i in range(30)]
    path.write_text("".join(lines), encoding="utf-8")
    fmt = log_time_format.detect(str(path))
    result = build_issue_time_suggestions(
        text="failure", log_lines=lines, log_format=fmt,
        llm_client=_client({"suggestions": [{"boot_elapsed": "50000"}]}),
        llm_model="fake",
    )
    assert result["suggestions"] == []
    assert result["needs_user_input"] is True
    assert "full log" in result["message"]
