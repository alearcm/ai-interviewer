"""The chat pane: a line-oriented terminal front end.

Uses prompt_toolkit when available so interviewer lines can arrive
cleanly while you type; otherwise falls back to plain stdin/stdout
(fine, but async lines interleave with your typing). Reading from a
pipe also works, which is how the end-to-end test drives a session.

The pane is deliberately dumb: it renders rows and forwards typed
lines to the session. All interpretation (commands, events, the gate)
happens elsewhere.
"""

from __future__ import annotations

import sys
import threading
from typing import Any, Callable, Dict

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.patch_stdout import patch_stdout

    HAVE_PT = True
except Exception:  # noqa: BLE001
    HAVE_PT = False


def fmt_t(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return "%02d:%02d" % divmod(seconds, 60)


class Pane:
    def __init__(self, interactive: bool = True) -> None:
        self.interactive = interactive and sys.stdin.isatty() and HAVE_PT
        self._lock = threading.Lock()

    # rendering ----------------------------------------------------------
    def _emit(self, line: str) -> None:
        with self._lock:
            print(line, flush=True)

    def interviewer(self, t: float, text: str) -> None:
        self._emit("[%s] INTERVIEWER> %s" % (fmt_t(t), text))

    def notice(self, text: str) -> None:
        self._emit("  * %s" % text)

    def task(self, t: float, title: str, statement: str) -> None:
        bar = "-" * 60
        self._emit("[%s] %s\nTASK: %s\n%s\n%s" % (fmt_t(t), bar, title, statement, bar))

    def run_result(self, row: Dict[str, Any]) -> None:
        parts = ["[%s] $ %s (exit %s, %sms)" % (
            fmt_t(row.get("t", 0)), row["cmd"], row["exit_status"], row.get("duration_ms", "?"),
        )]
        if row.get("out"):
            parts.append(row["out"].rstrip())
        if row.get("err"):
            parts.append(row["err"].rstrip())
        self._emit("\n".join(parts))

    def saved(self, t: float, path: str) -> None:
        self._emit("[%s] * saved %s" % (fmt_t(t), path))

    # input --------------------------------------------------------------
    def read_loop(self, on_line: Callable[[str], None], is_over: Callable[[], bool]) -> None:
        """Read lines until the session is over or input ends. Runs on
        the calling thread; the session loop runs elsewhere."""
        if self.interactive:
            session: Any = PromptSession()
            with patch_stdout():
                while not is_over():
                    try:
                        line = session.prompt("YOU> ")
                    except KeyboardInterrupt:
                        on_line("/end")
                        continue
                    except EOFError:
                        on_line("/end")
                        break
                    if is_over():
                        break
                    if line.strip():
                        on_line(line.strip())
            return
        while not is_over():
            try:
                line = sys.stdin.readline()
            except KeyboardInterrupt:
                on_line("/end")
                continue
            if line == "":  # end of input
                on_line("/end")
                break
            if is_over():
                break
            if line.strip():
                on_line(line.strip())
