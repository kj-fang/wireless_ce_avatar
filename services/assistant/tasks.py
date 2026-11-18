"""
Scheduled tasks: "at 2 pm, do X".

A task is an instruction in the user's words and a time. When the time comes
the assistant runs the instruction by itself - a fresh agent, the same
connectors, nobody watching - and leaves the result as a conversation in
History plus a notification on the page.

    schedule / list_tasks / cancel      the task list
    start_ticker / tick                 the clock: every 20 s, run what is due
    notify / notifications / mark_read  what the page's bell shows

Where things live: <avatarfiles_dir>/assistant/tasks.json and
notifications.json - this PC only, never the team's shared folder: a task is
one person's instruction and its result can quote their mail.

Limits of a clock inside the app: a task fires only while Avatar is running.
One that came due while it was closed (or more than LATE_S late for any other
reason) is not run behind the user's back hours later - it is marked "missed"
and the page offers Run now / Skip.

A scheduled run cannot schedule or cancel tasks, and can start a Muse
analysis only for a case number written in its own instruction (see
connectors/cases). It never posts and never sends: no connector does.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from services.assistant import DOMAIN, timeutil

TICK_S = 20
LATE_S = 10 * 60               # later than this, a task is "missed" instead of run
RUN_LIMIT_S = 10 * 60          # watchdog for one run
MAX_AHEAD_DAYS = 30
MAX_SCHEDULED = 50
MAX_INSTRUCTION = 2000
KEEP_FINISHED = 60
KEEP_NOTIFICATIONS = 100

OPEN_STATES = ("scheduled", "running", "missed")

_lock = threading.RLock()
_ticker: Optional[threading.Thread] = None
_ticker_stop = threading.Event()


class TaskError(ValueError):
    """A task request that cannot be honoured; `code` is stable for callers."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ---- storage -------------------------------------------------------------------

def _base_dir() -> Path:
    """<avatarfiles_dir>/assistant (tests point this elsewhere)."""
    from configs.global_configs import app_config
    base = getattr(app_config, "avatarfiles_dir", None)
    if not base:
        raise TaskError("no_storage", "the app's data folder is not set up yet")
    return Path(base) / "assistant"


def _read(name: str) -> list:
    try:
        data = json.loads((_base_dir() / name).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write(name: str, rows: list) -> None:
    folder = _base_dir()
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / (name + ".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(folder / name)


def _now() -> datetime:
    return datetime.now().astimezone()


def _update_task(task_id: str, **fields) -> Optional[dict]:
    with _lock:
        rows = _read("tasks.json")
        for row in rows:
            if row.get("id") == task_id:
                row.update(fields)
                _write("tasks.json", rows)
                return dict(row)
    return None


# ---- when ----------------------------------------------------------------------

_PERIODS = {"am": 0, "a.m.": 0, "上午": 0, "早上": 0, "凌晨": 0, "清晨": 0,
            "pm": 12, "p.m.": 12, "下午": 12, "晚上": 12, "傍晚": 12, "中午": 12, "noon": 12}
_DAYS = {"today": 0, "今天": 0, "今日": 0, "tonight": 0, "今晚": 0,
         "tomorrow": 1, "明天": 1, "明日": 1, "後天": 2, "后天": 2}


def parse_when(text: str, now: datetime) -> datetime:
    """A typed time as an aware local datetime.

        2pm, 2:30 pm, 14:00, 下午2點, 下午 2 點半, 晚上8點15分
        tomorrow 9am, 明天 9:00, 後天下午3點
        in 30 minutes, in 2 hours, 30分鐘後, 2小時後, 半小時後
        2026-11-19T14:00, 2026-11-19 14:00

    Raises TaskError('unreadable_time') when it cannot be read. A clock time
    with no day is today; when that is already past the result is returned as
    it is and schedule() refuses it - "2 pm" said at 3 pm is not silently
    moved to tomorrow."""
    raw = str(text or "").strip()
    low = raw.lower()
    if not low:
        raise TaskError("unreadable_time", "no time given")

    m = re.fullmatch(r"(?:in\s+)?(\d{1,4})\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours)"
                     r"(?:\s+from\s+now)?", low)
    if m:
        amount = int(m.group(1))
        return now + (timedelta(hours=amount) if m.group(2).startswith("h")
                      else timedelta(minutes=amount))
    m = re.fullmatch(r"(\d{1,4})\s*(分鐘|分钟|分|個小時|个小时|小時|小时)\s*(?:之)?(?:後|后)", low)
    if m:
        amount = int(m.group(1))
        return now + (timedelta(hours=amount) if "小" in m.group(2) else timedelta(minutes=amount))
    if re.fullmatch(r"半\s*(個|个)?\s*(小時|小时)\s*(?:之)?(?:後|后)", low):
        return now + timedelta(minutes=30)

    if re.match(r"^\d{4}-\d{2}-\d{2}", low):
        try:
            return timeutil.as_local(datetime.fromisoformat(raw.replace("Z", "+00:00")
                                                            .replace("z", "+00:00")), now)
        except ValueError:
            raise TaskError("unreadable_time", f"cannot read {raw!r} as a date and time") from None

    day_offset = 0
    rest = low
    for word, offset in sorted(_DAYS.items(), key=lambda kv: -len(kv[0])):
        if word in rest:
            day_offset = offset
            rest = rest.replace(word, " ", 1)
            break
    shift: Optional[int] = None
    period_word = ""
    for word, add in sorted(_PERIODS.items(), key=lambda kv: -len(kv[0])):
        if word in rest:
            shift, period_word = add, word
            rest = rest.replace(word, " ", 1)
            break
    rest = rest.replace("at", " ").strip()
    m = (re.fullmatch(r"(\d{1,2})\s*(?:點|点|時|时)\s*(?:(\d{1,2})\s*分?|(半))?", rest)
         or re.fullmatch(r"(\d{1,2})(?:\s*[:：]\s*(\d{2}))?()", rest))
    if not m:
        if period_word in ("noon", "中午") and not rest:
            m = re.fullmatch(r"(\d{1,2})()()", "12")
        else:
            raise TaskError("unreadable_time",
                            f"cannot read {raw!r} as a time: try '2pm', '14:30', "
                            "'下午2點', 'in 30 minutes', or an ISO date-time")
    hour = int(m.group(1))
    minute = 30 if m.group(3) else int(m.group(2) or 0)
    if shift is not None:
        if hour > 12 or hour == 0:
            pass                                    # already a 24-hour value
        elif shift == 12:
            hour = hour if hour == 12 else hour + 12
        elif hour == 12:
            hour = 0                                # 12 am
    if hour > 23 or minute > 59:
        raise TaskError("unreadable_time", f"{raw!r} is not a time of day")
    base = now + timedelta(days=day_offset)
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


# ---- the task list -------------------------------------------------------------

def schedule(instruction: str, run_at: datetime, now: Optional[datetime] = None) -> dict:
    """Add a task. Raises TaskError: empty_instruction, instruction_too_long,
    time_in_past, too_far_ahead, too_many_tasks."""
    now = now or _now()
    instruction = " ".join(str(instruction or "").split())
    if not instruction:
        raise TaskError("empty_instruction", "the task has no instruction")
    if len(instruction) > MAX_INSTRUCTION:
        raise TaskError("instruction_too_long",
                        f"the instruction is longer than {MAX_INSTRUCTION} characters")
    run_at = timeutil.as_local(run_at, now).replace(microsecond=0)
    if run_at <= now:
        raise TaskError("time_in_past",
                        f"{timeutil.stamp(run_at)} is already past (it is {timeutil.stamp(now)}). "
                        "Give a later time, or say 'tomorrow'.")
    if run_at > now + timedelta(days=MAX_AHEAD_DAYS):
        raise TaskError("too_far_ahead", f"tasks can be scheduled at most {MAX_AHEAD_DAYS} days ahead")
    task = {"id": uuid.uuid4().hex[:8], "instruction": instruction,
            "run_at": run_at.isoformat(timespec="seconds"),
            "created_at": now.isoformat(timespec="seconds"),
            "status": "scheduled", "started_at": "", "finished_at": "",
            "result_preview": "", "error": "", "conversation_id": ""}
    with _lock:
        rows = _read("tasks.json")
        if sum(1 for r in rows if r.get("status") == "scheduled") >= MAX_SCHEDULED:
            raise TaskError("too_many_tasks", f"there are already {MAX_SCHEDULED} scheduled tasks")
        rows.append(task)
        _write("tasks.json", _trimmed(rows))
    return dict(task)


def _trimmed(rows: list) -> list:
    """Every open task, and the newest KEEP_FINISHED finished ones."""
    open_rows = [r for r in rows if r.get("status") in OPEN_STATES]
    finished = sorted((r for r in rows if r.get("status") not in OPEN_STATES),
                      key=lambda r: r.get("finished_at") or r.get("run_at") or "")
    return open_rows + finished[-KEEP_FINISHED:]


def list_tasks(include_finished: bool = True) -> list[dict]:
    """Open tasks soonest first, then (optionally) finished ones newest first."""
    with _lock:
        rows = _read("tasks.json")
    open_rows = sorted((r for r in rows if r.get("status") in OPEN_STATES),
                       key=lambda r: r.get("run_at") or "")
    if not include_finished:
        return open_rows
    finished = sorted((r for r in rows if r.get("status") not in OPEN_STATES),
                      key=lambda r: r.get("finished_at") or r.get("run_at") or "", reverse=True)
    return open_rows + finished


def get_task(task_id: str) -> Optional[dict]:
    return next((t for t in list_tasks() if t.get("id") == str(task_id or "")), None)


def cancel(task_id: str) -> bool:
    """Cancel a task that has not started (scheduled or missed)."""
    with _lock:
        task = get_task(task_id)
        if task is None or task.get("status") not in ("scheduled", "missed"):
            return False
        _update_task(task["id"], status="cancelled",
                     finished_at=_now().isoformat(timespec="seconds"))
        return True


# ---- running -------------------------------------------------------------------

def _run_instruction(task: dict) -> tuple[dict, list, object]:
    """Run one task's instruction with a fresh headless agent.
    (result, steps, agent). Tests replace this."""
    from services.assistant import runner
    agent = runner.new_agent(headless=True)
    steps: list = []
    started = time.monotonic()

    def on_step(step: dict) -> None:
        steps.append(dict(step, ts_ms=int((time.monotonic() - started) * 1000)))

    box: dict = {}

    def work() -> None:
        try:
            box["result"] = agent.chat(task["instruction"], max_steps=8, step_callback=on_step)
        except Exception as e:
            box["error"] = f"{type(e).__name__}: {e}"

    worker = threading.Thread(target=work, daemon=True, name=f"assistant-task-{task['id']}")
    worker.start()
    worker.join(RUN_LIMIT_S)
    if worker.is_alive():
        agent.cancel_event.set()
        worker.join(15)
        raise TimeoutError(f"the task did not finish within {RUN_LIMIT_S // 60} minutes")
    if "error" in box:
        raise RuntimeError(box["error"])
    return box["result"], steps, agent


def run_task(task_id: str) -> Optional[dict]:
    """Run a task now (the ticker's work, and the page's "Run now"). Returns
    the finished task, or None when it is not in a state to run."""
    with _lock:
        task = get_task(task_id)
        if task is None or task.get("status") not in ("scheduled", "missed"):
            return None
        task = _update_task(task["id"], status="running",
                            started_at=_now().isoformat(timespec="seconds"))
    title = "⏰ " + task["instruction"]
    try:
        result, steps, agent = _run_instruction(task)
    except Exception as e:
        error = str(e)[:500]
        done = _update_task(task["id"], status="failed", error=error,
                            finished_at=_now().isoformat(timespec="seconds"))
        notify("task_failed", title[:120], f"The scheduled task failed: {error}",
               task_id=task["id"])
        return done
    conversation_id = str(uuid.uuid4())
    # The preview is shown as plain text: drop table rules and Markdown marks.
    text = re.sub(r"(?m)^[\s|:\-]{3,}$", "", str((result or {}).get("data") or ""))
    text = re.sub(r"[*_`#>|\\]+", " ", text)
    try:
        from services import history_service
        history_service.record_turn(
            conversation_id=conversation_id, session_id=conversation_id,
            turn_id=str(uuid.uuid4()), user_message=title, agent_result=result,
            mode="assistant-task", domain=DOMAIN, steps=steps,
            agent_context=agent.export_conversation_context() if agent is not None else None)
    except Exception as e:
        print(f"[assistant.tasks] result of task {task['id']} could not be saved: {e}")
        conversation_id = ""
    done = _update_task(task["id"], status="done", error="", conversation_id=conversation_id,
                        result_preview=" ".join(text.split())[:300],
                        finished_at=_now().isoformat(timespec="seconds"))
    notify("task_done", title[:120], " ".join(text.split())[:300], task_id=task["id"],
           conversation_id=conversation_id, internal=bool((result or {}).get("internal")))
    return done


def skip(task_id: str) -> bool:
    """Drop a missed task without running it."""
    with _lock:
        task = get_task(task_id)
        if task is None or task.get("status") != "missed":
            return False
        _update_task(task["id"], status="skipped",
                     finished_at=_now().isoformat(timespec="seconds"))
        return True


def recover(now: Optional[datetime] = None) -> None:
    """At start-up: a task that was running when the app stopped did not
    finish; say so instead of leaving it "running" for ever."""
    now = now or _now()
    with _lock:
        for task in list_tasks(include_finished=False):
            if task.get("status") == "running":
                _update_task(task["id"], status="failed",
                             error="the app was closed while this task was running",
                             finished_at=now.isoformat(timespec="seconds"))
                notify("task_failed", ("⏰ " + task["instruction"])[:120],
                       "The app was closed while this task was running.", task_id=task["id"])


def tick(now: Optional[datetime] = None, *, run: Optional[Callable[[str], object]] = None) -> list[str]:
    """One beat of the clock: start every task that is due, mark as missed
    the ones that are more than LATE_S late. Returns the ids started. Each
    task runs on its own thread (`run` replaces that in tests)."""
    now = now or _now()
    started: list[str] = []
    with _lock:
        for task in list_tasks(include_finished=False):
            if task.get("status") != "scheduled":
                continue
            due = timeutil.parse_stamp(task.get("run_at"))
            if due is None or due > now:
                continue
            if (now - due).total_seconds() > LATE_S:
                _update_task(task["id"], status="missed")
                notify("task_missed", ("⏰ " + task["instruction"])[:120],
                       f"This task was due at {timeutil.label(due, now)} but Avatar was not "
                       "running then. Run it now, or skip it.", task_id=task["id"])
                continue
            started.append(task["id"])
    for task_id in started:
        if run is not None:
            run(task_id)
        else:
            threading.Thread(target=run_task, args=(task_id,), daemon=True,
                             name=f"assistant-task-run-{task_id}").start()
    return started


def start_ticker() -> bool:
    """Start the clock thread once per process. True when this call started it."""
    global _ticker
    with _lock:
        if _ticker is not None and _ticker.is_alive():
            return False
        _ticker_stop.clear()

        def loop() -> None:
            try:
                recover()
            except Exception as e:
                print(f"[assistant.tasks] recover failed: {e}")
            while not _ticker_stop.wait(TICK_S):
                try:
                    tick()
                except Exception as e:
                    print(f"[assistant.tasks] tick failed: {type(e).__name__}: {e}")

        _ticker = threading.Thread(target=loop, daemon=True, name="assistant-task-ticker")
        _ticker.start()
        return True


def ticker_running() -> bool:
    return _ticker is not None and _ticker.is_alive()


def stop_ticker() -> None:
    _ticker_stop.set()


# ---- notifications ---------------------------------------------------------------

def notify(kind: str, title: str, text: str = "", *, task_id: str = "",
           conversation_id: str = "", internal: bool = False) -> dict:
    note = {"id": uuid.uuid4().hex[:10], "at": _now().isoformat(timespec="seconds"),
            "kind": kind, "title": str(title or "")[:200], "text": str(text or "")[:400],
            "task_id": task_id, "conversation_id": conversation_id,
            "internal": bool(internal), "read": False}
    try:
        with _lock:
            rows = _read("notifications.json")
            rows.append(note)
            _write("notifications.json", rows[-KEEP_NOTIFICATIONS:])
    except Exception as e:
        print(f"[assistant.tasks] notification not saved: {e}")
    return note


def notifications(limit: int = 30) -> dict:
    """{"items": newest first, "unread": count}"""
    with _lock:
        rows = _read("notifications.json")
    rows.reverse()                       # of two written in the same second, the later first
    rows.sort(key=lambda r: r.get("at") or "", reverse=True)
    return {"items": rows[:limit], "unread": sum(1 for r in rows if not r.get("read"))}


def mark_read(ids=None) -> int:
    """Mark the given notifications (or all, when ids is None) as read."""
    wanted = None if ids is None else {str(i) for i in ids}
    changed = 0
    with _lock:
        rows = _read("notifications.json")
        for row in rows:
            if not row.get("read") and (wanted is None or row.get("id") in wanted):
                row["read"] = True
                changed += 1
        if changed:
            _write("notifications.json", rows)
    return changed
