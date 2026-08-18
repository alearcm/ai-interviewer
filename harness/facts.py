"""The closed set of facts a pack rule may reference.

The gate hands every rule the same flat dict. All keys are always
present (with neutral defaults) so evaluation never depends on which
kind of wake happened. `NEVER` is the value of "seconds since X"
facts when X has not happened yet — a large number, so thresholds
like `since_last_run_s > 300` read naturally.
"""

from __future__ import annotations

from typing import Any, Dict

NEVER = 10.0**9

# Facts available to rule `when` expressions.
RULE_FACT_NAMES = frozenset(
    {
        "kind",                     # wake kind
        "mark",                     # clock-mark id ("" unless a clock_mark wake)
        "elapsed_s",
        "elapsed_min",
        "remaining_s",
        "remaining_min",
        "idle_s",                   # seconds since last candidate activity
        "saves_total",
        "runs_total",
        "user_messages_total",
        "speaks_total",             # interviewer messages so far
        "unprompted_speaks",        # those that counted toward the budget
        "budget_left",
        "last_run_status",          # exit status of last run, -1 if none
        "last_run_ok",
        "failed_run_streak",
        "since_last_speak_s",
        "since_last_save_s",
        "since_last_run_s",
        "since_last_user_message_s",
        "task_open",                # a task has been presented
        "task_index",               # 0-based, -1 before the first task
        "tasks_total",
        "tasks_left",
        "task_elapsed_s",
        "task_user_messages",
        "hint_level",               # highest hint level used on this task, 0 = none
        "has_workspace",
        "pulses_total",             # edit pulses seen (0 when no front end sends them)
        "since_last_pulse_s",
    }
)

# Facts available to report milestone expressions (evaluated per event row).
EVENT_FACT_NAMES = frozenset(
    {
        "kind",
        "t",
        "path",
        "exit_status",
        "ok",           # run finished with exit status 0
        "source",
        "rule",
        "hint_level",
        "mark",
        "task_id",
    }
)


def event_facts(row: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten one transcript row into the milestone fact set."""
    exit_status = row.get("exit_status", -1)
    return {
        "kind": row.get("kind", ""),
        "t": row.get("t", 0.0),
        "path": row.get("path", "") or "",
        "exit_status": exit_status,
        "ok": row.get("kind") == "run_executed" and exit_status == 0,
        "source": row.get("source", "") or "",
        "rule": row.get("rule", "") or "",
        "hint_level": row.get("hint_level", 0) or 0,
        "mark": row.get("mark", "") or "",
        "task_id": row.get("task_id", "") or "",
    }
