"""Fixed event taxonomy and the append-only JSONL transcript.

Every observable moment of a session becomes one JSON object per line.
The set of kinds below is closed: packs configure behavior through
rules and templates, but they can never invent new kinds. Every row
carries a wall-clock stamp ("ts") and a seconds offset from session
start ("t"), so a transcript can be replayed offline with full timing.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from typing import Any, Dict, List

SESSION_START = "session_start"
PACK_SNAPSHOT = "pack_snapshot"
TASK_PRESENTED = "task_presented"
FILE_SAVED = "file_saved"
RUN_EXECUTED = "run_executed"
USER_MESSAGE = "user_message"
INTERVIEWER_MESSAGE = "interviewer_message"
GATE_DECISION = "gate_decision"
IDLE = "idle"
CLOCK_MARK = "clock_mark"
NOTE = "note"
SESSION_END = "session_end"

ALL_KINDS = frozenset(
    {
        SESSION_START,
        PACK_SNAPSHOT,
        TASK_PRESENTED,
        FILE_SAVED,
        RUN_EXECUTED,
        USER_MESSAGE,
        INTERVIEWER_MESSAGE,
        GATE_DECISION,
        IDLE,
        CLOCK_MARK,
        NOTE,
        SESSION_END,
    }
)

# Kinds that wake the gate. Everything else is bookkeeping.
WAKE_KINDS = frozenset(
    {
        SESSION_START,
        TASK_PRESENTED,
        FILE_SAVED,
        RUN_EXECUTED,
        USER_MESSAGE,
        IDLE,
        CLOCK_MARK,
    }
)

# Kinds that count as candidate activity (they reset the idle timer).
ACTIVITY_KINDS = frozenset({FILE_SAVED, RUN_EXECUTED, USER_MESSAGE})


def iso_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds")


class Transcript:
    """Append-only JSONL writer. One flush + fsync per row; rows are
    never edited after the fact."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._fh = open(path, "a", encoding="utf-8")

    def append(self, kind: str, t: float, **fields: Any) -> Dict[str, Any]:
        if kind not in ALL_KINDS:
            raise ValueError("unknown event kind: %r" % (kind,))
        row: Dict[str, Any] = {"t": round(float(t), 3), "ts": iso_now(), "kind": kind}
        row.update(fields)
        self._fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        return row

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


def read_transcript(path: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows
