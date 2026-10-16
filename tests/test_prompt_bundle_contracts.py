"""One-file agent switches all three prompt documents for one profile."""

from flask import Flask

from services.chatbot.engine.speclet_defaults import default_speclet
from services.chatbot.prompt_editor import PromptEditorContext, build_prompt_editor_handlers
from utils import speclets_utils as sp


def test_bundle_save_switch_and_per_part_edit(tmp_path, monkeypatch):
    shared, user = tmp_path / "shared", tmp_path / "user"
    shared.mkdir()
    user.mkdir()
    monkeypatch.setattr(sp, "local_speclets_dir", lambda: shared)
    monkeypatch.setattr(sp, "local_user_speclets_dir", lambda: user)

    (shared / "linux_prompt.md").write_text("old shared prompt", encoding="utf-8")
    (shared / "linux_report.md").write_text("old shared report", encoding="utf-8")
    (shared / "linux_suggest_time.md").write_text("old shared time", encoding="utf-8")
    for part in sp.BUNDLED_KINDS:
        sp.write_user_speclet("linux", part, f"stale {part}")
        sp.set_source("linux", part, sp.SOURCE_LOCAL)

    handlers = build_prompt_editor_handlers(PromptEditorContext("linux", default_speclet))
    app = Flask(__name__)
    bundle = "## prompt\n\nLinux Bluetooth\n\n## report\n\nBT report\n\n## suggest_time\n\nBT cause time"
    with app.test_request_context(json={"doc": "linux_agent", "text": bundle}):
        response = handlers["prompts_save"]()
        assert response.get_json()["success"]
    for part, expected in zip(sp.BUNDLED_KINDS, ("Linux Bluetooth", "BT report", "BT cause time")):
        assert sp.resolve("linux", part) == (expected, sp.SOURCE_LOCAL)
        assert sp.from_bundle("linux", part)
        assert sp.get_speclet("linux", part) == expected
        assert sp.get_source("linux", part) == sp.SOURCE_SHARED
    assert sp.resolve("wifi", "prompt") == (None, sp.SOURCE_BUILTIN)

    with app.test_request_context(json={"doc": "linux_report", "text": "revised report"}):
        assert handlers["prompts_save"]().get_json()["success"]
    assert "revised report" in (user / "linux_agent.md").read_text(encoding="utf-8")
    assert sp.resolve("linux", "report") == ("revised report", sp.SOURCE_LOCAL)

    (shared / "linux_agent.md").write_text(
        "## prompt\n\nshared bundle\n\n## report\n\nshared report\n\n## suggest_time\n\nshared time",
        encoding="utf-8",
    )
    with app.test_request_context(json={"doc": "linux_agent", "source": sp.SOURCE_SHARED}):
        assert handlers["prompts_use_source"]().get_json()["success"]
    assert sp.resolve("linux", "prompt") == ("shared bundle", sp.SOURCE_SHARED)
    assert sp.resolve("linux", "report") == ("shared report", sp.SOURCE_SHARED)
    assert sp.resolve("linux", "suggest_time") == ("shared time", sp.SOURCE_SHARED)

    with app.test_request_context(json={"doc": "linux_report", "text": "one part only"}):
        assert handlers["prompts_save"]().get_json()["success"]
    assert sp.resolve("linux", "report") == ("one part only", sp.SOURCE_LOCAL)
    assert sp.resolve("linux", "prompt") == ("shared bundle", sp.SOURCE_SHARED)


def test_bundle_editor_requires_all_sections_and_offers_builtin(tmp_path, monkeypatch):
    shared, user = tmp_path / "shared", tmp_path / "user"
    shared.mkdir()
    user.mkdir()
    monkeypatch.setattr(sp, "local_speclets_dir", lambda: shared)
    monkeypatch.setattr(sp, "local_user_speclets_dir", lambda: user)
    app = Flask(__name__)
    for profile in sp.PROFILES:
        handlers = build_prompt_editor_handlers(PromptEditorContext(profile, default_speclet))
        with app.test_request_context(query_string={"doc": f"{profile}_agent"}):
            document = handlers["prompts_get"]().get_json()
            assert set(sp.split_bundle(document["builtin"])) == set(sp.BUNDLED_KINDS)
        with app.test_request_context(json={"doc": f"{profile}_agent",
                                            "text": "## prompt\nOnly one part"}):
            response, status = handlers["prompts_save"]()
            assert status == 400
            assert "report" in response.get_json()["error"]
            assert not (user / f"{profile}_agent.md").exists()


def test_wifi_bt_linux_bundle_swaps_stay_in_their_own_profile(tmp_path, monkeypatch):
    shared, user = tmp_path / "shared", tmp_path / "user"
    shared.mkdir()
    user.mkdir()
    monkeypatch.setattr(sp, "local_speclets_dir", lambda: shared)
    monkeypatch.setattr(sp, "local_user_speclets_dir", lambda: user)
    app = Flask(__name__)
    for profile in ("wifi", "bt", "linux"):
        handlers = build_prompt_editor_handlers(PromptEditorContext(profile, default_speclet))
        bundle = sp.join_bundle({part: f"{profile} {part}" for part in sp.BUNDLED_KINDS})
        with app.test_request_context(json={"doc": f"{profile}_agent", "text": bundle}):
            assert handlers["prompts_save"]().get_json()["success"]
    for profile in ("wifi", "bt", "linux"):
        for part in sp.BUNDLED_KINDS:
            assert sp.get_speclet(profile, part) == f"{profile} {part}"
            assert sp.from_bundle(profile, part)
