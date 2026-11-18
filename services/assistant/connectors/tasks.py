"""
Task connectors: "at 2 pm, do X" (services/assistant/tasks holds the list and
the clock).

    schedule_task   store an instruction to run later
    list_tasks      what is scheduled, what ran
    cancel_task     cancel one that has not started

The model turns the user's "2 pm" / "in half an hour" into an absolute local
time (the system prompt gives it the current time and UTC offset); the server
checks it and answers with the time it actually stored.

Scheduling and cancelling change something, so they follow the rule for
actions: once other people's writing (mail, case comments) is in front of the
model in this conversation, the action is not run but proposed, and the user
confirms it with a click - a mail must not be able to plant a task. A
scheduled run itself gets neither connector.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from services.assistant import actions, tasks, timeutil
from services.assistant.registry import Connector, Ctx, object_schema

SCHEDULE_KIND = "schedule_task"
CANCEL_KIND = "cancel_task"


def _view(task: dict, now: datetime) -> dict:
    out = {"id": task.get("id"), "status": task.get("status"),
           "run_at": timeutil.stamp(timeutil.parse_stamp(task.get("run_at")) or now),
           "instruction": task.get("instruction")}
    if task.get("finished_at"):
        out["finished"] = timeutil.label(task["finished_at"], now)
    if task.get("result_preview"):
        out["result_preview"] = task["result_preview"]
    if task.get("error"):
        out["error"] = task["error"]
    return out


def _do_schedule(params: dict) -> dict:
    try:
        task = tasks.schedule(params.get("instruction", ""),
                              datetime.fromisoformat(str(params.get("run_at"))))
    except tasks.TaskError as e:
        return {"error": str(e), "code": e.code}
    except (TypeError, ValueError):
        return {"error": "the stored time could not be read"}
    when = timeutil.parse_stamp(task["run_at"])
    return {"scheduled": True, "task_id": task["id"], "run_at": timeutil.stamp(when),
            "instruction": task["instruction"],
            "message": f"Scheduled for {timeutil.stamp(when)}: {task['instruction']}",
            "note": "It runs only if Avatar is open at that time; otherwise it is kept as "
                    "'missed' and offered again."}


def _do_cancel(params: dict) -> dict:
    task = tasks.get_task(params.get("task_id", ""))
    if task is None:
        return {"error": f"there is no task {params.get('task_id')!r}"}
    if not tasks.cancel(task["id"]):
        return {"error": f"task {task['id']} is {task.get('status')} and can no longer be cancelled"}
    return {"cancelled": True, "task_id": task["id"],
            "message": f"Cancelled: {task['instruction']}"}


actions.register(SCHEDULE_KIND, _do_schedule)
actions.register(CANCEL_KIND, _do_cancel)


def _schedule_task(args: dict, ctx: Ctx) -> dict:
    now = ctx.now()
    instruction = " ".join(str(args.get("instruction") or "").split())
    if not instruction:
        return {"error": "the task has no instruction"}
    if args.get("in_minutes") is not None:
        try:
            minutes = int(args["in_minutes"])
        except (TypeError, ValueError):
            return {"error": "in_minutes must be a whole number"}
        if minutes < 1:
            return {"error": "in_minutes must be at least 1", "code": "time_in_past"}
        run_at = now + timedelta(minutes=minutes)
    elif args.get("run_at"):
        try:
            run_at = tasks.parse_when(str(args["run_at"]), now)
        except tasks.TaskError as e:
            return {"error": str(e), "code": e.code}
    else:
        return {"error": "give run_at (a local date-time) or in_minutes"}
    run_at = timeutil.as_local(run_at, now).replace(microsecond=0)
    if run_at <= now:
        return {"error": f"{timeutil.stamp(run_at)} is already past (it is {timeutil.stamp(now)}). "
                         "Ask the engineer for a later time; do not move it to another day yourself.",
                "code": "time_in_past"}
    params = {"instruction": instruction, "run_at": run_at.isoformat(timespec="seconds")}
    if ctx.untrusted_seen:
        action = actions.propose(
            SCHEDULE_KIND, f"Schedule for {timeutil.stamp(run_at)}: {instruction}", params,
            confirm_label="Schedule")
        ctx.pending_actions.append(action)
        return {"needs_confirmation": True, "run_at": timeutil.stamp(run_at),
                "message": "Not scheduled yet. Mail or case text was read in this conversation, so "
                           "a 'Schedule' button is shown under your answer for the engineer to "
                           "confirm. Tell them to click it; do not say it is scheduled."}
    return _do_schedule(params)


def _list_tasks(args: dict, ctx: Ctx) -> dict:
    now = ctx.now()
    rows = tasks.list_tasks(include_finished=True)
    open_rows = [_view(t, now) for t in rows if t.get("status") in tasks.OPEN_STATES]
    finished = [_view(t, now) for t in rows if t.get("status") not in tasks.OPEN_STATES][:10]
    return {"now": timeutil.stamp(now), "open": open_rows, "recent": finished,
            "summary": f"{len(open_rows)} open task(s), {len(finished)} recent"}


def _cancel_task(args: dict, ctx: Ctx) -> dict:
    task = tasks.get_task(str(args.get("task_id") or "").strip())
    if task is None:
        return {"error": f"there is no task {args.get('task_id')!r}; use list_tasks for the ids"}
    if ctx.untrusted_seen:
        action = actions.propose(CANCEL_KIND, f"Cancel the task: {task['instruction']}",
                                 {"task_id": task["id"]}, confirm_label="Cancel task")
        ctx.pending_actions.append(action)
        return {"needs_confirmation": True,
                "message": "Not cancelled yet: a 'Cancel task' button is shown under your answer "
                           "for the engineer to confirm."}
    return _do_cancel({"task_id": task["id"]})


def connectors() -> list[Connector]:
    return [
        Connector(
            name="schedule_task",
            description=(
                "Schedule something for the assistant to do later by itself, e.g. 'at 2 pm list "
                "the cases updated since this morning'. Store the instruction as a complete, "
                "self-contained sentence in the engineer's language (the later run sees only "
                "this text - include case numbers, names and what to report). The run can look "
                "things up and ask Echo; it cannot post or send anything. Work out the absolute "
                "time from the current local time in your instructions. If the time the engineer "
                "said is already past today, ask them - never move it to another day yourself."),
            parameters=object_schema({
                "instruction": {"type": "string",
                                "description": "What to do at that time, self-contained."},
                "run_at": {"type": "string",
                           "description": "When, in the engineer's local time, as ISO "
                                          "YYYY-MM-DDTHH:MM (preferred) or a plain time such as "
                                          "'2pm' or 'tomorrow 9:00'."},
                "in_minutes": {"type": "integer",
                               "description": "Alternative to run_at: this many minutes from now."},
            }, ["instruction"]),
            run=_schedule_task, kind="act", headless_ok=False),
        Connector(
            name="list_tasks",
            description=("The scheduled tasks (with their ids and times), the missed ones, and "
                         "the last finished ones with a preview of their result."),
            parameters=object_schema(),
            run=_list_tasks),
        Connector(
            name="cancel_task",
            description="Cancel a scheduled or missed task by its id (from list_tasks).",
            parameters=object_schema(
                {"task_id": {"type": "string", "description": "The task's id from list_tasks."}},
                ["task_id"]),
            run=_cancel_task, kind="act", headless_ok=False),
    ]
