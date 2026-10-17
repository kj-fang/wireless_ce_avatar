"""Large captures must keep streaming; the fast parser keeps old semantics."""

from datetime import datetime, timedelta
import queue
import random
from types import SimpleNamespace

import pytest

from services.chatbot import job_runtime
from services.chatbot.engine.log_scope import LogScopeMixin
from utils import log_time_format


def _format(name):
    shape = next(s for s in log_time_format._SHAPES if s.name == name)
    return log_time_format.LogTimeFormat(name=name, kind=shape.kind, _shape=shape)


def test_wifi_bt_fast_path_matches_strptime_on_valid_and_invalid_timestamps():
    rng = random.Random(44)
    for name, old_pattern in (
        ("wifi_etl", "%m/%d/%Y-%H:%M:%S.%f"),
        ("bt_hci", "%Y/%m/%d %H:%M:%S.%f"),
    ):
        fmt = _format(name)
        for _ in range(1000):
            dt = datetime(rng.randint(1, 9999), rng.randint(1, 12), 1,
                          rng.randrange(24), rng.randrange(60), rng.randrange(60),
                          rng.randrange(1000) * 1000)
            if name == "wifi_etl":
                stamp = (f"{dt.month:02d}/{dt.day:02d}/{dt.year:04d}-"
                         f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}."
                         f"{dt.microsecond // 1000:03d}")
            else:
                stamp = (f"{dt.year:04d}/{dt.month:02d}/{dt.day:02d} "
                         f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}."
                         f"{dt.microsecond // 1000:03d}")
            assert fmt.parse(f"prefix {stamp} suffix") == datetime.strptime(stamp, old_pattern)
        for invalid in (
            "02/30/2026-12:00:00.000" if name == "wifi_etl" else "2026/02/30 12:00:00",
            "12/01/2026-24:00:00.000" if name == "wifi_etl" else "2026/12/01 24:00:00",
        ):
            assert fmt.parse(invalid) is None
    assert _format("bt_hci").parse("2026/10/17 05:06:07.1") == datetime(2026, 10, 17, 5, 6, 7, 100000)
    assert _format("bt_hci").parse("2026/10/17 05:06:07") == datetime(2026, 10, 17, 5, 6, 7)


@pytest.mark.filterwarnings(
    "ignore:Parsing dates involving a day of month without a year specified is ambiguous:DeprecationWarning"
)
def test_linux_shapes_keep_their_existing_time_rules():
    iso = _format("rfc3339")
    iso._utcoffset = timedelta(hours=-7)
    assert iso.parse("2026-02-17T05:27:08.604578+00:00 kernel") == datetime(2026, 2, 16, 22, 27, 8, 604578)
    logcat = _format("logcat")
    logcat.year = logcat.end_year = 2026
    assert logcat.parse("04-18 10:26:16.777 message") == datetime(2026, 4, 18, 10, 26, 16, 777000)
    assert _format("monotonic").parse("[   55.510209] kernel") is None


def test_running_job_emits_heartbeat_then_real_result(monkeypatch):
    job = job_runtime.ChatJob("c", "t", "title", object())
    job.worker_thread = SimpleNamespace(is_alive=lambda: True)

    class OneIdleQueue:
        calls = 0

        def get(self, timeout):
            assert timeout == job_runtime._SSE_HEARTBEAT_SECONDS
            self.calls += 1
            if self.calls == 1:
                raise queue.Empty
            return "done", {"summary": "finished"}

    monkeypatch.setattr(job_runtime, "subscribe", lambda j: (OneIdleQueue(), [], None))
    monkeypatch.setattr(job_runtime, "unsubscribe", lambda j, q: None)
    frames = list(job_runtime.job_sse(job))
    assert frames[0] == ": keep-alive\n\n"
    assert '"type": "done"' in frames[1]
    assert len(frames) == 2


def test_timeout_checks_terminal_state_and_worker_liveness(monkeypatch):
    job = job_runtime.ChatJob("c", "t", "title", object())
    monkeypatch.setattr(job_runtime, "unsubscribe", lambda j, q: None)

    class TimedOutQueue:
        def get(self, timeout):
            raise queue.Empty

    monkeypatch.setattr(job_runtime, "subscribe", lambda j: (TimedOutQueue(), [], None))
    job_runtime.finish_job(job, {"summary": "saved"})
    assert '"type": "done"' in next(job_runtime.job_sse(job))

    job.status = "running"
    job.worker_thread = SimpleNamespace(is_alive=lambda: False)
    assert "worker stopped unexpectedly" in next(job_runtime.job_sse(job)).lower()


def test_large_prescan_publishes_progress_before_completion():
    class Agent(LogScopeMixin):
        DRIVER_ADD_MARKER = "driver add"
        RESET_MARKER = "driver reset"
        SCOPE_FULL_LOG_WHEN_EMPTY = False
        capabilities = SimpleNamespace(merge_wrapped_time_only_logs=False,
                                       full_scope_for_undated_logs=False)
        issue_time = None
        _raw_log_cache = ["ordinary message"] * 500_001

        def _normalize_markers(self, marker):
            return [marker]

        def _line_matches_any(self, line, markers):
            return any(marker in line for marker in markers)

        def _effective_issue_window(self):
            return 5

        def _log_time_format(self):
            return log_time_format.LogTimeFormat()

        def _export_scoped_log_file(self):
            return ""

    steps = []
    Agent()._preprocess_raw_log_context(progress_callback=steps.append)
    assert any("driver markers: 500,000/500,001" in s["content"] for s in steps)
    assert any("merging scoped lines" in s["content"] for s in steps)
    assert all(s["role"] == "agent" for s in steps)
