from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

from configs.chatbot_ui import BT_UI, LOG_CHATBOT_UI, WIFI_UI


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = PROJECT_ROOT / "templates"
PROFILES = {
    "bt": BT_UI,
    "wifi": LOG_CHATBOT_UI,
    "nw": WIFI_UI,
}
INLINE_SCRIPT_RE = re.compile(
    r"<script(?P<attrs>\s[^>]*)?>(?P<body>.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
SCRIPT_SRC_RE = re.compile(r"""\bsrc=["'](?P<src>[^"']+)["']""", re.IGNORECASE)


@pytest.mark.parametrize("profile", ["bt", "wifi", "nw"])
def test_rendered_inline_javascript_parses(
    profile: str,
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for rendered JavaScript syntax checks")

    environment = Environment(loader=FileSystemLoader(TEMPLATE_ROOT))
    html = environment.get_template("chatbot/page.html").render(
        ui=PROFILES[profile],
    )
    scripts = []
    for match in INLINE_SCRIPT_RE.finditer(html):
        source_match = SCRIPT_SRC_RE.search(match.group("attrs") or "")
        if source_match:
            public_path = source_match.group("src")
            if not public_path.startswith("/static/"):
                continue
            source_path = PROJECT_ROOT / public_path.removeprefix("/")
            assert source_path.is_file(), public_path
            scripts.append(source_path.read_text(encoding="utf-8"))
        elif match.group("body").strip():
            scripts.append(match.group("body"))
    assert scripts

    rendered_js = tmp_path / f"{profile}.rendered.js"
    rendered_js.write_text(
        "\n;\n".join(scripts),
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, "--check", str(rendered_js)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0, result.stderr
