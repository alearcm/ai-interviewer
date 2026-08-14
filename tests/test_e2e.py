"""End-to-end, in process: a real Session with the canned adapter,
real scheduler, real transcript — no network, no model, no key.
Timings are compressed (a 3-9 second session) but nothing is mocked.
"""

import os
import time

from conftest import mini_pack_raw

from harness.adapters import Canned
from harness.chat import Pane
from harness.events import read_transcript
from harness.pack import Pack
from harness.regrade import main as regrade_main
from harness.report import build_report
from harness.session import Session


def make_settings(tmp_path):
    return {
        "model": {"provider": "canned"},
        "run": {"timeout_s": 10.0, "output_max_chars": 2000},
        "paths": {"sessions_dir": str(tmp_path)},
    }


def wait_over(session, deadline_s=15.0):
    end = time.time() + deadline_s
    while not session.is_over() and time.time() < end:
        time.sleep(0.1)
    assert session.is_over(), "session never finished"


def test_full_session_without_workspace(tmp_path):
    pack = Pack(mini_pack_raw())
    session = Session(
        pack, make_settings(tmp_path), Canned(["Ack."]), Pane(interactive=False),
        sessions_dir=str(tmp_path),
    )
    session.start()
    assert session.workspace is None and session.watch is None
    time.sleep(0.4)
    session.submit_line("first answer")
    time.sleep(0.3)
    session.submit_line("/next")
    time.sleep(0.3)
    session.submit_line("second answer")
    wait_over(session)  # 0.05 min clock expires on its own

    rows = read_transcript(os.path.join(session.dir, "transcript.jsonl"))
    kinds = [r["kind"] for r in rows]
    assert kinds[0] == "session_start"
    assert kinds[1] == "pack_snapshot"
    assert kinds[-1] == "session_end"
    assert rows[-1]["reason"] == "time"
    assert kinds.count("task_presented") == 2  # initial + /next
    assert "idle" in kinds  # threshold wake fired with no polling loop

    # NON-NEGOTIABLE: every wake gets a gate decision row with the rule
    wake_kinds = {"session_start", "task_presented", "file_saved", "run_executed",
                  "user_message", "idle", "clock_mark"}
    wakes = [r for r in rows if r["kind"] in wake_kinds]
    decisions = [r for r in rows if r["kind"] == "gate_decision"]
    assert len(decisions) == len(wakes)
    assert all("rule" in d and "evaluations" in d and "facts" in d for d in decisions)

    # the reply rule answered both messages; idle nudges escalated
    replies = [r for r in rows if r["kind"] == "interviewer_message" and r.get("rule") == "reply"]
    assert len(replies) == 2
    nudges = [r for r in rows if r["kind"] == "interviewer_message" and r.get("rule") == "idle-nudge"]
    assert nudges, "idle rule never fired"
    assert nudges[0]["hint_level"] == 1  # escalation starts at L1, never skips

    assert os.path.isfile(os.path.join(session.dir, "report.md"))


def test_workspace_observation_run_capture_and_spool(tmp_path):
    raw = mini_pack_raw(
        session={"minutes": 0.5, "workspace": True, "idle_threshold_s": 30, "debounce_ms": 150},
    )
    pack = Pack(raw)
    session = Session(
        pack, make_settings(tmp_path), Canned(), Pane(interactive=False),
        sessions_dir=str(tmp_path),
    )
    session.start()
    assert session.workspace and os.path.isdir(session.workspace)
    assert os.path.isfile(os.path.join(session.workspace, ".session-spool"))
    time.sleep(0.4)

    # an editor save: two quick writes debounce into one snapshot
    target = os.path.join(session.workspace, "notes.txt")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("draft one\n")
    with open(target, "a", encoding="utf-8") as fh:
        fh.write("draft two\n")
    time.sleep(1.2)

    # a run through the chat pane
    session.submit_line("/run echo observed-run")
    time.sleep(0.4)

    # an external run arriving through the spool (what tools/irun writes)
    entry = os.path.join(session.spool, "run-999-1")
    os.makedirs(entry)
    for name, content in (
        ("cmd.txt", "made-up-cmd --flag"),
        ("out.txt", "external output\n"),
        ("err.txt", ""),
        ("status.txt", "0"),
        ("ms.txt", "12"),
    ):
        with open(os.path.join(entry, name), "w", encoding="utf-8") as fh:
            fh.write(content)
    with open(os.path.join(entry, "done"), "w", encoding="utf-8") as fh:
        fh.write("")
    time.sleep(1.0)

    session.submit_line("/end")
    wait_over(session)

    rows = read_transcript(os.path.join(session.dir, "transcript.jsonl"))
    saves = [r for r in rows if r["kind"] == "file_saved" and r["path"] == "notes.txt"]
    assert len(saves) == 1, "debounce failed: %d snapshots" % len(saves)
    assert "draft one" in saves[0]["content"] and "draft two" in saves[0]["content"]

    runs = [r for r in rows if r["kind"] == "run_executed"]
    chat_runs = [r for r in runs if r["source"] == "chat"]
    ext_runs = [r for r in runs if r["source"] == "external"]
    assert chat_runs and chat_runs[0]["exit_status"] == 0
    assert "observed-run" in chat_runs[0]["out"]
    assert ext_runs and ext_runs[0]["cmd"] == "made-up-cmd --flag"
    assert ext_runs[0]["out"] == "external output\n"

    assert rows[-1]["kind"] == "session_end" and rows[-1]["reason"] == "user"


def test_regrade_reproduces_the_report_offline(tmp_path):
    pack = Pack(mini_pack_raw())
    session = Session(
        pack, make_settings(tmp_path), Canned(["Ack."]), Pane(interactive=False),
        sessions_dir=str(tmp_path),
    )
    session.start()
    time.sleep(0.4)
    session.submit_line("an answer for the record")
    wait_over(session)

    with open(os.path.join(session.dir, "report.md"), "r", encoding="utf-8") as fh:
        live = fh.read()

    assert regrade_main([session.dir]) == 0
    with open(os.path.join(session.dir, "report.regraded.md"), "r", encoding="utf-8") as fh:
        regraded = fh.read()
    assert regraded == live, "offline regrade differs from the live report"

    rows = read_transcript(os.path.join(session.dir, "transcript.jsonl"))
    assert build_report(rows) == build_report(rows)
