"""Command-line entry: run a session, or regrade a finished one.

See the README for full usage. In short:

    -m harness run --pack packs/<name>
    -m harness regrade sessions/<id>
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from . import regrade as regrade_mod
from .adapters import AdapterError, make_adapter
from .chat import Pane
from .pack import PackError, load_pack
from .session import Session
from .settings import load_settings
from .watcher import WatchUnavailable


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="harness", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="run a live session")
    run_p.add_argument("--pack", required=True, help="pack directory (or name under ./packs)")
    run_p.add_argument("--workspace", default=None, help="observe this directory (default: a fresh one inside the session dir)")
    run_p.add_argument("--task", default=None, help="start with this task id")
    run_p.add_argument("--minutes", type=float, default=None, help="override the pack's session length")
    run_p.add_argument("--provider", default=None, help="override [model] provider (openai-compat | anthropic | canned)")
    run_p.add_argument("--config", default=None, help="settings file (default: ./config.toml if present)")

    reg_p = sub.add_parser("regrade", help="rebuild a report from a transcript, fully offline")
    reg_p.add_argument("session_dir")
    reg_p.add_argument("--out", default=None)
    reg_p.add_argument("--stdout", action="store_true")

    web_p = sub.add_parser("web", help="serve the browser front end (localhost/tailnet only)")
    web_p.add_argument("--host", default=None)
    web_p.add_argument("--port", type=int, default=None)
    web_p.add_argument("--packs", default=None, help="packs directory (default: ./packs)")
    web_p.add_argument("--config", default=None)

    chk_p = sub.add_parser("check", help="run the pack's hidden checks against a finished session")
    chk_p.add_argument("session_dir")
    chk_p.add_argument("--config", default=None)

    ana_p = sub.add_parser("analyze", help="deep review of a finished session via the [analyze] model")
    ana_p.add_argument("session_dir")
    ana_p.add_argument("--config", default=None)
    ana_p.add_argument("--out", default=None)

    args = parser.parse_args(argv)

    if args.cmd == "web":
        from .web import WebUnavailable, serve

        try:
            settings = load_settings(args.config)
            if args.packs:
                settings["web"]["packs_dir"] = args.packs
            serve(settings, host=args.host, port=args.port)
        except WebUnavailable as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 2
        return 0

    if args.cmd == "check":
        from .checks import main as check_main

        return check_main([args.session_dir] + (["--config", args.config] if args.config else []))

    if args.cmd == "analyze":
        from .analyze import main as analyze_main

        argv2 = [args.session_dir]
        if args.config:
            argv2 += ["--config", args.config]
        if args.out:
            argv2 += ["--out", args.out]
        return analyze_main(argv2)

    if args.cmd == "regrade":
        regrade_argv = [args.session_dir]
        if args.out:
            regrade_argv += ["--out", args.out]
        if args.stdout:
            regrade_argv.append("--stdout")
        return regrade_mod.main(regrade_argv)

    try:
        settings = load_settings(args.config)
        if args.provider:
            settings["model"]["provider"] = args.provider
        pack = load_pack(args.pack)
        adapter = make_adapter(settings["model"], pack.fallback_lines)
        pane = Pane()
        session = Session(
            pack,
            settings,
            adapter,
            pane,
            workspace=args.workspace,
            task_id=args.task,
            minutes=args.minutes,
        )
    except (PackError, AdapterError, WatchUnavailable, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    if session.workspace:
        pane.notice("workspace: %s (open it in your editor; saves are observed)" % session.workspace)
        pane.notice("runs: use /run here, or tools/irun from a terminal inside the workspace")
    elif args.workspace:
        pane.notice("--workspace ignored: this pack runs without a workspace")
    try:
        session.run()
    except WatchUnavailable as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
