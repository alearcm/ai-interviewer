"""Execute a candidate-requested command in the workspace and capture
everything about it: the command line, both output streams, the exit
status, and the wall-clock duration. Used by the chat pane's /run.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Any, Dict


def _clip(text: str, limit: int) -> str:
    if len(text) > limit:
        return text[:limit] + "\n[truncated %d chars]" % (len(text) - limit)
    return text


def execute(cmd: str, cwd: str, *, timeout_s: float, output_max_chars: int) -> Dict[str, Any]:
    started = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        shell=True,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        out, err = proc.communicate()
        err = (err or "") + "\n[terminated after %.0fs]" % timeout_s
    status = proc.poll()
    return {
        "kind": "run_executed",
        "cmd": cmd,
        "out": _clip(out or "", output_max_chars),
        "err": _clip(err or "", output_max_chars),
        "exit_status": -9 if timed_out and status is None else int(status if status is not None else -1),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "source": "chat",
    }
