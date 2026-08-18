"""Post-session self-check: run each presented task's hidden checks
against what the candidate actually shipped.

Mechanical, not grading: for every task presented in the transcript,
take the last saved snapshot of the pack's [checks] file within that
task's time slice, append the task's opaque `check` text, and run the
pack's [checks] cmd on the result. Exit status 0 = pass. Which file,
which command, and what the checks say are all pack data; the engine
only slices time, concatenates text, and runs one command per task.

Never runs during the session — the interviewer's knowledge stays
limited to what was on screen.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any, Dict, List

from . import events
from .pack import Pack
from .runner import run_command


def run_for_session(rows: List[Dict[str, Any]], pack: Pack, run_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not pack.checks_file or not pack.checks_cmd:
        return []
    presented = [(i, r) for i, r in enumerate(rows) if r["kind"] == events.TASK_PRESENTED]
    results: List[Dict[str, Any]] = []
    for n, (start, row) in enumerate(presented):
        end = presented[n + 1][0] if n + 1 < len(presented) else len(rows)
        snapshot = None
        for r in rows[start:end]:
            if r["kind"] == events.FILE_SAVED and r["path"] == pack.checks_file:
                snapshot = r["content"]
        task = pack.task_by_id(row["task_id"]) or {}
        check = task.get("check", "")
        base = {"task_id": row["task_id"], "title": row.get("title", ""), "out": ""}
        if not check:
            results.append({**base, "status": "no-checks"})
            continue
        if snapshot is None:
            results.append({**base, "status": "nothing-saved"})
            continue
        with tempfile.TemporaryDirectory() as tmp:
            name = os.path.basename(pack.checks_file)
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                fh.write(snapshot.rstrip("\n") + "\n\n" + check.strip() + "\n")
            res = run_command(pack.checks_cmd.replace("{file}", name), tmp, run_cfg)
        ok = res["exit_status"] == 0
        tail = (res["err"] or res["out"] or "").strip()
        results.append({**base, "status": "ok" if ok else "failing", "out": "" if ok else tail[-1500:]})
    return results


def render(results: List[Dict[str, Any]]) -> str:
    lines = ["# Self-check", ""]
    for r in results:
        lines.append("## %s (%s) — %s" % (r["title"], r["task_id"], r["status"]))
        if r["out"]:
            lines.append("")
            lines.append("```\n%s\n```" % r["out"])
        lines.append("")
    return "\n".join(lines)


def main(argv: List[str]) -> int:
    import argparse
    import sys

    from .events import read_transcript
    from .settings import load_settings

    parser = argparse.ArgumentParser(prog="check", description="run the pack's hidden checks against a finished session")
    parser.add_argument("session_dir")
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)

    path = os.path.join(args.session_dir, "transcript.jsonl")
    if not os.path.isfile(path):
        print("no transcript at %s" % path, file=sys.stderr)
        return 2
    rows = read_transcript(path)
    pack = None
    for row in rows:
        if row["kind"] == events.PACK_SNAPSHOT:
            pack = Pack.from_snapshot(row["data"])
            break
    if pack is None:
        print("transcript has no pack snapshot", file=sys.stderr)
        return 2
    if not pack.checks_cmd:
        print("this pack defines no [checks]; nothing to run")
        return 0
    results = run_for_session(rows, pack, load_settings(args.config)["run"])
    out = os.path.join(args.session_dir, "checks.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(render(results))
    for r in results:
        print("%-12s %s (%s)" % (r["status"], r["title"], r["task_id"]))
    print(out)
    return 0 if all(r["status"] in ("ok", "no-checks") for r in results) else 1
