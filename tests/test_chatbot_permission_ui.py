"""Permission errors and dateless issue-time hints must stay truthful."""

import shutil
import subprocess
from pathlib import Path

import pytest

from services.chatbot.engine.conversation import _llm_error_for_user


ROOT = Path(__file__).resolve().parents[1]
ISSUE_TIME_JS = ROOT / "static/chatbot/js/strategies/issue-time-base.js"


def test_forbidden_error_is_actionable_without_leaking_provider_details():
    class ForbiddenError(Exception):
        status_code = 403

    error = ForbiddenError("Permission denied; groups: admin, confidential")
    message = _llm_error_for_user(error, 0, "configured-model")

    assert "HTTP 403" in message
    assert "configured-model" in message
    assert "administrator" in message
    assert "groups" not in message
    assert "confidential" not in message


def test_other_llm_errors_keep_the_existing_diagnostic():
    message = _llm_error_for_user(ValueError("invalid request"), 2, "model")
    assert message == "LLM API error at reasoning step 2: invalid request"


def test_time_only_hint_requires_a_clock_axis():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for JavaScript behavior checks")

    script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const source = fs.readFileSync(process.argv[1], 'utf8');
const hint = {style: {display: 'none'}};
const context = {
    window: {},
    document: {getElementById: id => id === 'it-nodate-hint' ? hint : null},
};
vm.runInNewContext(source + '\nglobalThis.Strategy = IssueTimeStrategyBase;', context);
const strategy = new context.Strategy({api: '/linux_chatbot'});
let axis;
strategy._applyDateFieldState = () => {};
strategy._applyTimeAxisState = value => { axis = value; };

context.window.__logHasDate = false;
context.window.__logTimeAxis = false;
strategy.refreshIssueTimeDateOptional();
assert.strictEqual(hint.style.display, 'none'); // unanchored dmesg
assert.strictEqual(axis, false);

context.window.__logTimeAxis = true;
strategy.refreshIssueTimeDateOptional();
assert.strictEqual(hint.style.display, 'block'); // time-only clock log
assert.strictEqual(axis, true);

context.window.__logHasDate = true;
strategy.refreshIssueTimeDateOptional();
assert.strictEqual(hint.style.display, 'none'); // dated log
"""
    result = subprocess.run(
        [node, "-e", script, str(ISSUE_TIME_JS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stderr
