"""Execute a candidate-requested command and capture everything about
it: the command line, both output streams, the exit status, and the
wall-clock duration.

Two back-ends behind one seam. "local" runs the command directly in
the workspace — right for a personal machine. "container" wraps it in
`docker run` with no network, bounded cpu/memory/pids and only the
workspace mounted — the setting to flip before anyone else can reach
the web pane, because a run is arbitrary shell by design. Callers use
run_command() and never care which is active; the transcript records
the candidate's command either way (plus which backend ran it).

Known limit of the container backend: on a hard timeout the docker
client is killed; a truly wedged container can linger until
`docker container prune`. See docs/DEPLOY.md.
"""

from __future__ import annotations

import os
import shlex
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


def run_command(cmd: str, cwd: str, run_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch to the configured [run] backend."""
    timeout_s = float(run_cfg.get("timeout_s", 30.0))
    output_max = int(run_cfg.get("output_max_chars", 4000))
    backend = str(run_cfg.get("backend", "local"))
    if backend == "local":
        return execute(cmd, cwd, timeout_s=timeout_s, output_max_chars=output_max)
    if backend != "container":
        raise ValueError("[run] backend must be 'local' or 'container', not %r" % backend)
    image = str(run_cfg.get("container_image", ""))
    if not image:
        row = execute("exit 1", cwd, timeout_s=5, output_max_chars=output_max)
        row.update(
            cmd=cmd,
            err="[run] backend is 'container' but container_image is not set in config",
            exit_status=-1,
        )
        return row
    wrapped = (
        "docker run --rm --network none --cpus %s --memory %s --pids-limit 256 "
        "-v %s:/w -w /w %s sh -c %s"
        % (
            shlex.quote(str(run_cfg.get("container_cpus", "1.0"))),
            shlex.quote(str(run_cfg.get("container_memory", "512m"))),
            shlex.quote(os.path.abspath(cwd)),
            shlex.quote(image),
            shlex.quote(cmd),
        )
    )
    row = execute(wrapped, cwd, timeout_s=timeout_s, output_max_chars=output_max)
    row["cmd"] = cmd
    row["backend"] = "container"
    return row
