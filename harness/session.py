"""Session orchestration: lifecycle, clock, and the wake loop.

Everything funnels into one queue consumed by a single loop thread:
watcher snapshots, chat lines, run captures, and scheduler deadlines.
The scheduler is deadline-driven — it sleeps on a condition variable
until the nearest deadline (session end, pack clock marks, the idle
threshold) and never samples on an interval. The idle deadline is
pushed back every time candidate activity arrives, so an idle wake
means the threshold genuinely elapsed with nothing happening.

Per wake: append the event, update state, evaluate the gate, log the
decision, and only then — if a rule fired — call the model to phrase
the interjection at the level the gate chose.
"""

from __future__ import annotations

import heapq
import os
import random
import threading
import time
from queue import Queue
from typing import Any, Dict, List, Optional

from . import events, report
from .adapters import Adapter, AdapterError
from .chat import Pane, fmt_t
from .gate import Gate
from .facts import NEVER
from .pack import Pack
from .phrasing import build_call, shape_reply
from .runner import execute
from .watcher import WorkspaceWatch

def help_text(has_workspace: bool) -> str:
    run_part = "/run CMD  run a command in the workspace   " if has_workspace else ""
    return (
        run_part + "/next  next task   /time  clock   /end  finish now   "
        "anything else is said to the interviewer"
    )


class Scheduler:
    """Deadline scheduler. Wakes only when something is actually due."""

    def __init__(self, push: Any) -> None:
        self._push = push
        self._cv = threading.Condition()
        self._heap: List[Any] = []
        self._seq = 0
        self._idle_at: Optional[float] = None
        self._idle_threshold: Optional[float] = None
        self._stopped = False
        self._thread = threading.Thread(target=self._run_loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def schedule(self, at_mono: float, payload: Dict[str, Any]) -> None:
        with self._cv:
            heapq.heappush(self._heap, (at_mono, self._seq, payload))
            self._seq += 1
            self._cv.notify()

    def arm_idle(self, threshold_s: float) -> None:
        with self._cv:
            self._idle_threshold = threshold_s
            self._idle_at = time.monotonic() + threshold_s
            self._cv.notify()

    def reset_idle(self) -> None:
        with self._cv:
            if self._idle_threshold is not None:
                self._idle_at = time.monotonic() + self._idle_threshold
                self._cv.notify()

    def stop(self) -> None:
        with self._cv:
            self._stopped = True
            self._cv.notify()

    def _run_loop(self) -> None:
        with self._cv:
            while not self._stopped:
                now = time.monotonic()
                fired = False
                while self._heap and self._heap[0][0] <= now:
                    payload = heapq.heappop(self._heap)[2]
                    self._push(payload)
                    fired = True
                if self._idle_at is not None and self._idle_at <= now:
                    self._push({"kind": events.IDLE})
                    self._idle_at = now + (self._idle_threshold or 0.0)
                    fired = True
                if fired:
                    continue
                deadlines = []
                if self._heap:
                    deadlines.append(self._heap[0][0])
                if self._idle_at is not None:
                    deadlines.append(self._idle_at)
                if deadlines:
                    self._cv.wait(max(0.0, min(deadlines) - time.monotonic()))
                else:
                    self._cv.wait()


class SessionState:
    def __init__(self) -> None:
        self.saves_total = 0
        self.runs_total = 0
        self.user_messages_total = 0
        self.speaks_total = 0
        self.unprompted_speaks = 0
        self.last_activity_t = 0.0
        self.last_speak_t: Optional[float] = None
        self.last_save_t: Optional[float] = None
        self.last_run_t: Optional[float] = None
        self.last_user_message_t: Optional[float] = None
        self.last_run_status = -1
        self.failed_run_streak = 0
        self.latest_snapshots: Dict[str, str] = {}
        self.chat_tail: List[Dict[str, str]] = []
        self.recent_runs: List[Dict[str, Any]] = []
        self.task_index = -1
        self.current_task: Optional[Dict[str, Any]] = None
        self.task_presented_t: Optional[float] = None
        self.task_user_messages = 0
        self.hint_level = 0
        self.fallback_i = 0


class Session:
    def __init__(
        self,
        pack: Pack,
        settings: Dict[str, Any],
        adapter: Adapter,
        pane: Pane,
        *,
        workspace: Optional[str] = None,
        task_id: Optional[str] = None,
        minutes: Optional[float] = None,
        sessions_dir: Optional[str] = None,
    ) -> None:
        self.pack = pack
        self.settings = settings
        self.adapter = adapter
        self.pane = pane
        self.minutes = float(minutes if minutes is not None else pack.minutes)
        self.state = SessionState()
        self.gate = Gate(pack)
        self._q: "Queue[Dict[str, Any]]" = Queue()
        self._over = threading.Event()
        self._loop_thread: Optional[threading.Thread] = None
        self.watch: Optional[WorkspaceWatch] = None

        base = sessions_dir or settings["paths"]["sessions_dir"]
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.session_id = "%s-%s" % (stamp, pack.name)
        self.dir = os.path.join(base, self.session_id)
        os.makedirs(self.dir, exist_ok=True)
        self.transcript = events.Transcript(os.path.join(self.dir, "transcript.jsonl"))

        self.workspace: Optional[str] = None
        self.spool: Optional[str] = None
        if pack.workspace:
            self.workspace = os.path.abspath(workspace or os.path.join(self.dir, "workspace"))
            os.makedirs(self.workspace, exist_ok=True)
            self.spool = os.path.join(self.dir, "spool")
            os.makedirs(self.spool, exist_ok=True)
            pointer = os.path.join(self.workspace, ".session-spool")
            with open(pointer, "w", encoding="utf-8") as fh:
                fh.write(os.path.abspath(self.spool) + "\n")

        order = list(pack.tasks)
        if pack.tasks_order == "shuffle":
            random.shuffle(order)
        if task_id:
            picked = pack.task_by_id(task_id)
            if picked is None:
                raise ValueError("no task with id %r in pack %r" % (task_id, pack.name))
            order = [picked] + [t for t in order if t["id"] != task_id]
        self.task_order = order

        self.scheduler = Scheduler(self._q.put)
        self._t0 = time.monotonic()

    # -- clock ----------------------------------------------------------
    def offset(self) -> float:
        return time.monotonic() - self._t0

    # -- public control -------------------------------------------------
    def start(self) -> None:
        self._t0 = time.monotonic()
        end_s = self.minutes * 60.0
        self.scheduler.schedule(self._t0 + end_s, {"kind": "_end", "reason": "time"})
        for mark in self.pack.clock_marks:
            at = mark["at_min"] * 60.0
            if at < end_s:
                self.scheduler.schedule(self._t0 + at, {"kind": events.CLOCK_MARK, "mark": mark["id"]})
        if self.pack.idle_threshold_s > 0:
            self.scheduler.arm_idle(self.pack.idle_threshold_s)
        self.scheduler.start()

        if self.workspace is not None:
            self.watch = WorkspaceWatch(
                self.workspace,
                self.spool,
                debounce_ms=self.pack.debounce_ms,
                snapshot_max_bytes=self.pack.snapshot_max_bytes,
                ignore_globs=self.pack.ignore_globs,
                emit=self._q.put,
            )
            self.watch.start()

        self._q.put(
            {
                "kind": events.SESSION_START,
                "pack": self.pack.name,
                "minutes": self.minutes,
                "workspace": bool(self.workspace),
                "task_order": [t["id"] for t in self.task_order],
            }
        )
        if self.pack.opening_line:
            self._q.put({"kind": "_opening"})
        self._q.put({"kind": "_advance"})

        self._loop_thread = threading.Thread(target=self._loop, daemon=True)
        self._loop_thread.start()

    def run(self) -> None:
        """Start everything and hand the calling thread to the pane."""
        self.start()
        self.pane.notice("session %s — %s minutes on the clock" % (self.session_id, ("%g" % self.minutes)))
        self.pane.notice(help_text(self.workspace is not None))
        self.pane.read_loop(self.submit_line, self._over.is_set)
        self.wait()

    def wait(self, timeout: Optional[float] = None) -> None:
        if self._loop_thread is not None:
            self._loop_thread.join(timeout)

    def is_over(self) -> bool:
        return self._over.is_set()

    # -- input ----------------------------------------------------------
    def submit_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        if line == "/end":
            self._q.put({"kind": "_end", "reason": "user"})
        elif line == "/next":
            self._q.put({"kind": "_advance", "by": "user"})
        elif line == "/help":
            self.pane.notice(help_text(self.workspace is not None))
        elif line == "/time":
            t = self.offset()
            self.pane.notice(
                "elapsed %s, remaining %s" % (fmt_t(t), fmt_t(self.minutes * 60.0 - t))
            )
        elif line.startswith("/run"):
            cmd = line[len("/run") :].strip()
            if self.workspace is None:
                self.pane.notice("this session has no workspace")
            elif not cmd:
                self.pane.notice("usage: /run CMD")
            else:
                row = execute(
                    cmd,
                    self.workspace,
                    timeout_s=float(self.settings["run"]["timeout_s"]),
                    output_max_chars=int(self.settings["run"]["output_max_chars"]),
                )
                self._q.put(row)
        elif line.startswith("/"):
            self.pane.notice("unknown command; %s" % help_text(self.workspace is not None))
        else:
            self._q.put({"kind": events.USER_MESSAGE, "text": line})

    # -- the loop --------------------------------------------------------
    def _loop(self) -> None:
        while True:
            item = self._q.get()
            kind = item.get("kind")
            if kind == "_end":
                self._finalize(str(item.get("reason", "time")))
                return
            if kind == "_advance":
                self._advance()
                continue
            if kind == "_opening":
                self._record(
                    events.INTERVIEWER_MESSAGE,
                    text=self.pack.opening_line,
                    rule=None,
                    hint_level=0,
                    source="opening",
                    counted=False,
                )
                continue
            fields = {k: v for k, v in item.items() if k != "kind"}
            if kind == events.IDLE:
                fields["idle_s"] = round(self.offset() - self.state.last_activity_t, 3)
            row = self._record(kind, **fields)
            if kind == events.SESSION_START:
                # the full pack goes in before any gate activity, so a
                # transcript is self-contained from its second row on
                self._record(events.PACK_SNAPSHOT, data=self.pack.snapshot())
            if kind in events.ACTIVITY_KINDS:
                self.scheduler.reset_idle()
            if kind in events.WAKE_KINDS:
                self._wake(row)

    def _record(self, kind: str, **fields: Any) -> Dict[str, Any]:
        row = self.transcript.append(kind, self.offset(), **fields)
        self._apply(row)
        self._display(row)
        return row

    # -- state ----------------------------------------------------------
    def _apply(self, row: Dict[str, Any]) -> None:
        state = self.state
        kind = row["kind"]
        t = row["t"]
        if kind == events.FILE_SAVED:
            state.saves_total += 1
            state.latest_snapshots.pop(row["path"], None)
            state.latest_snapshots[row["path"]] = row["content"]
            state.last_save_t = t
            state.last_activity_t = t
        elif kind == events.RUN_EXECUTED:
            state.runs_total += 1
            state.recent_runs.append(row)
            state.recent_runs = state.recent_runs[-5:]
            state.last_run_t = t
            state.last_run_status = int(row.get("exit_status", -1))
            state.failed_run_streak = 0 if state.last_run_status == 0 else state.failed_run_streak + 1
            state.last_activity_t = t
        elif kind == events.USER_MESSAGE:
            state.user_messages_total += 1
            state.task_user_messages += 1
            state.chat_tail.append({"kind": kind, "text": row["text"]})
            state.last_user_message_t = t
            state.last_activity_t = t
        elif kind == events.INTERVIEWER_MESSAGE:
            state.speaks_total += 1
            state.chat_tail.append({"kind": kind, "text": row["text"]})
            state.last_speak_t = t
            if row.get("counted"):
                state.unprompted_speaks += 1
            state.hint_level = max(state.hint_level, int(row.get("hint_level", 0)))
        elif kind == events.TASK_PRESENTED:
            state.chat_tail.append({"kind": kind, "text": row["statement"]})
        state.chat_tail = state.chat_tail[-40:]

    def _facts(self, row: Dict[str, Any]) -> Dict[str, Any]:
        state = self.state
        t = float(row["t"])
        total = self.minutes * 60.0

        def since(anchor: Optional[float]) -> float:
            return t - anchor if anchor is not None else NEVER

        return {
            "kind": row["kind"],
            "mark": row.get("mark", ""),
            "elapsed_s": t,
            "elapsed_min": t / 60.0,
            "remaining_s": max(0.0, total - t),
            "remaining_min": max(0.0, total - t) / 60.0,
            "idle_s": t - state.last_activity_t,
            "saves_total": state.saves_total,
            "runs_total": state.runs_total,
            "user_messages_total": state.user_messages_total,
            "speaks_total": state.speaks_total,
            "unprompted_speaks": state.unprompted_speaks,
            "budget_left": self.pack.interjection_budget - state.unprompted_speaks,
            "last_run_status": state.last_run_status,
            "last_run_ok": state.last_run_status == 0 and state.runs_total > 0,
            "failed_run_streak": state.failed_run_streak,
            "since_last_speak_s": since(state.last_speak_t),
            "since_last_save_s": since(state.last_save_t),
            "since_last_run_s": since(state.last_run_t),
            "since_last_user_message_s": since(state.last_user_message_t),
            "task_open": state.current_task is not None,
            "task_index": state.task_index,
            "tasks_total": len(self.task_order),
            "tasks_left": len(self.task_order) - (state.task_index + 1),
            "task_elapsed_s": since(state.task_presented_t),
            "task_user_messages": state.task_user_messages,
            "hint_level": state.hint_level,
            "has_workspace": self.workspace is not None,
        }

    # -- gate + speech ---------------------------------------------------
    def _wake(self, row: Dict[str, Any]) -> None:
        facts = self._facts(row)
        decision = self.gate.evaluate(facts, row["t"])
        compact = {
            k: (round(v, 1) if isinstance(v, float) else v) for k, v in facts.items()
        }
        self._record(
            events.GATE_DECISION,
            wake=row["kind"],
            rule=decision.rule.id if decision.rule else None,
            action=decision.action,
            hint_level=decision.hint_level,
            evaluations=decision.evaluations,
            facts=compact,
        )
        if decision.rule is None:
            return
        self.gate.commit(decision, row["t"])
        if decision.action == "speak":
            self._speak(decision)
        elif decision.action == "advance":
            self._advance(by=decision.rule.id)

    def _speak(self, decision: Any) -> None:
        rule = decision.rule
        state = self.state
        snapshots = dict(list(state.latest_snapshots.items())[-self.pack.recent_files :])
        t = self.offset()
        system, messages = build_call(
            self.pack,
            hint_level=decision.hint_level,
            rule_prompt=rule.prompt,
            task=state.current_task,
            chat_tail=list(state.chat_tail),
            snapshots=snapshots,
            recent_runs=list(state.recent_runs),
            elapsed_s=t,
            remaining_s=self.minutes * 60.0 - t,
        )
        source = "model"
        try:
            raw = self.adapter.reply(system, messages)
        except AdapterError as exc:
            self._record(events.NOTE, text="model call failed: %s" % exc)
            raw = ""
            source = "fallback"
        text, used_fallback, state.fallback_i = shape_reply(raw, self.pack, state.fallback_i)
        if used_fallback:
            source = "fallback"
        self._record(
            events.INTERVIEWER_MESSAGE,
            text=text,
            rule=rule.id,
            hint_level=decision.hint_level,
            source=source,
            counted=rule.counts_toward_budget,
        )

    def _advance(self, by: str = "user") -> None:
        state = self.state
        nxt = state.task_index + 1
        if nxt >= len(self.task_order):
            self._record(events.NOTE, text="no further tasks in this pack")
            return
        state.task_index = nxt
        task = self.task_order[nxt]
        state.current_task = task
        state.task_presented_t = self.offset()
        state.task_user_messages = 0
        state.hint_level = 0
        row = self._record(
            events.TASK_PRESENTED,
            task_id=task["id"],
            title=task["title"],
            statement=task["statement"],
        )
        self._wake(row)

    # -- output ----------------------------------------------------------
    def _display(self, row: Dict[str, Any]) -> None:
        kind = row["kind"]
        if kind == events.INTERVIEWER_MESSAGE:
            self.pane.interviewer(row["t"], row["text"])
        elif kind == events.TASK_PRESENTED:
            self.pane.task(row["t"], row["title"], row["statement"])
        elif kind == events.FILE_SAVED:
            self.pane.saved(row["t"], row["path"])
        elif kind == events.RUN_EXECUTED:
            self.pane.run_result(row)
        elif kind == events.NOTE:
            self.pane.notice(row["text"])

    # -- teardown --------------------------------------------------------
    def _finalize(self, reason: str) -> None:
        if self.watch is not None:
            self.watch.stop()
        self.scheduler.stop()
        self._record(events.SESSION_END, reason=reason)
        self.transcript.close()

        rows = events.read_transcript(self.transcript.path)
        text = report.build_report(rows)
        report_path = os.path.join(self.dir, "report.md")
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write(text)

        if self.pack.recurrence_log:
            hits = report.watchlist_hits(rows, self.pack)
            if hits:
                base = self.settings["paths"]["sessions_dir"]
                log_path = self.pack.recurrence_log
                if not os.path.isabs(log_path):
                    log_path = os.path.join(base, log_path)
                os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
                with open(log_path, "a", encoding="utf-8") as fh:
                    for hit in hits:
                        fh.write(
                            "%s\t%s\t%s\t%d\t%s\n"
                            % (self.session_id, hit["id"], hit["path"], hit["line_no"], hit["line"])
                        )

        self.pane.notice("session over (%s) — report: %s" % (reason, report_path))
        if self.pane.interactive:
            self.pane.notice("press Enter to close")
        self._over.set()
