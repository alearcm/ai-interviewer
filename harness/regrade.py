"""Offline regrade: rebuild a report from a transcript alone.

Reads sessions/<id>/transcript.jsonl, rebuilds the pack from the
embedded pack_snapshot row, and renders the pack's report template.
No network, no model, no live session — the transcript is the whole
world. Identical transcripts always yield identical reports.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from .events import read_transcript
from .report import build_report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="regrade", description="rebuild a session report offline"
    )
    parser.add_argument("session_dir", help="a session directory holding transcript.jsonl")
    parser.add_argument(
        "--out",
        default=None,
        help="where to write (default: report.regraded.md inside the session dir)",
    )
    parser.add_argument("--stdout", action="store_true", help="print instead of writing")
    args = parser.parse_args(argv)

    path = args.session_dir
    if os.path.isdir(path):
        path = os.path.join(path, "transcript.jsonl")
    if not os.path.isfile(path):
        print("no transcript at %s" % path, file=sys.stderr)
        return 2

    text = build_report(read_transcript(path))
    if args.stdout:
        sys.stdout.write(text)
        return 0
    out = args.out or os.path.join(os.path.dirname(path), "report.regraded.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(out)
    return 0
