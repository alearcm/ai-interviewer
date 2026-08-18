"""Ship a finished transcript to a strong model for deep review.

The live session deliberately runs on a small model; the deep read is
this separate, explicit step. The rubric prompt is pack data
([report] analyze_prompt); the engine compacts the transcript (drops
the embedded pack, trims gate bookkeeping, bounds total size) and
makes exactly one call against the [analyze] model in config — which
can be a different provider than the live one.

With no key or an unreachable endpoint, it degrades usefully: the
exact request is written to analysis-request.md for manual pasting.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from . import events
from .adapters import AdapterError, make_adapter
from .events import read_transcript
from .pack import Pack
from .settings import load_settings

DEFAULT_PROMPT = """\
You are reviewing the full transcript of a timed, observed practice
session with an AI interviewer. The rows are JSON lines with wall-time
offsets: tasks presented, every file snapshot as saved, every command
run with its output, everything said by both sides, idle wakes, and
every interviewer gate decision.

Produce a direct, unsparing review in Markdown:
1. What actually happened — a two-paragraph narrative of the session.
2. The three costliest moments, with timestamps and evidence.
3. Recurring habits the candidate should break, each with the exact
   lines or utterances that show it.
4. What went well (briefly, no padding).
5. A prioritized drill list for the next three sessions.
Quote evidence verbatim. No scores, no encouragement filler."""

_TRIM_KINDS = {events.GATE_DECISION}


def compact(rows: List[Dict[str, Any]], cap_chars: int = 300_000) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        if row["kind"] == events.PACK_SNAPSHOT:
            continue
        row = dict(row)
        if row["kind"] in _TRIM_KINDS:
            row.pop("facts", None)
            row.pop("evaluations", None)
        out.append(row)

    def size() -> int:
        return sum(len(json.dumps(r, ensure_ascii=False)) for r in out)

    if size() > cap_chars:
        # drop middle snapshots per file first (keep first and last)
        by_path: Dict[str, List[Dict[str, Any]]] = {}
        for row in out:
            if row["kind"] == events.FILE_SAVED:
                by_path.setdefault(row["path"], []).append(row)
        for saves in by_path.values():
            for row in saves[1:-1]:
                if size() <= cap_chars:
                    break
                row["content"] = "[snapshot omitted for size: %d chars]" % len(row.get("content", ""))
    return out


def _find_pack(rows: List[Dict[str, Any]]) -> Optional[Pack]:
    for row in rows:
        if row["kind"] == events.PACK_SNAPSHOT:
            return Pack.from_snapshot(row["data"])
    return None


def build_request(rows: List[Dict[str, Any]]) -> Any:
    pack = _find_pack(rows)
    system = (pack.analyze_prompt if pack and pack.analyze_prompt else DEFAULT_PROMPT)
    lines = "\n".join(json.dumps(r, ensure_ascii=False) for r in compact(rows))
    materials = ""
    if pack is not None:
        presented: List[str] = []
        for row in rows:
            if row["kind"] == events.TASK_PRESENTED and row["task_id"] not in presented:
                presented.append(row["task_id"])
        chunks = []
        for task_id in presented:
            task = pack.task_by_id(task_id)
            if task and (task.get("notes") or task.get("appendix")):
                chunks.append(
                    "### %s\n%s\n\n%s" % (task_id, task.get("notes", ""), task.get("appendix", ""))
                )
        if chunks:
            materials = (
                "\n\nTask materials from the pack (interviewer notes, references, "
                "hidden checks — post-session context for your review):\n\n"
                + "\n\n".join(chunks)
            )
    user = (
        "The transcript follows as JSON lines. Review it per your instructions.\n\n"
        + lines
        + materials
    )
    return system, user


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="analyze", description="deep review of a finished session via the [analyze] model"
    )
    parser.add_argument("session_dir")
    parser.add_argument("--config", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    path = os.path.join(args.session_dir, "transcript.jsonl")
    if not os.path.isfile(path):
        print("no transcript at %s" % path, file=sys.stderr)
        return 2
    rows = read_transcript(path)
    system, user = build_request(rows)

    settings = load_settings(args.config)
    adapter = make_adapter(settings["analyze"])
    try:
        text = adapter.reply(system, [{"role": "user", "content": user}])
    except AdapterError as exc:
        request_path = os.path.join(args.session_dir, "analysis-request.md")
        with open(request_path, "w", encoding="utf-8") as fh:
            fh.write("# Analysis request (no reachable [analyze] model)\n\n")
            fh.write("## System prompt\n\n%s\n\n## User message\n\n%s\n" % (system, user))
        print("model call failed (%s)" % exc, file=sys.stderr)
        print("wrote the full request to %s — paste it into any strong model" % request_path)
        return 1

    out = args.out or os.path.join(args.session_dir, "analysis.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text.strip() + "\n")
    print(out)
    return 0
