"""Migration guarantees: state is relocatable and paths are not
assumed. A session directory copied to any other machine or path must
regrade byte-identically, and packs must load from absolute paths."""

import os
import shutil
import time

from conftest import mini_pack_raw

from harness.adapters import Canned
from harness.chat import Pane
from harness.pack import load_pack
from harness.regrade import main as regrade_main
from harness.session import Session


def test_relocated_session_dir_regrades_identically(tmp_path):
    pack_dir = tmp_path / "home"
    session = Session(
        __import__("harness.pack", fromlist=["Pack"]).Pack(mini_pack_raw()),
        {
            "model": {"provider": "canned"},
            "run": {"timeout_s": 10.0, "output_max_chars": 2000},
            "paths": {"sessions_dir": str(pack_dir)},
        },
        Canned(["Noted."]),
        Pane(interactive=False),
        sessions_dir=str(pack_dir),
    )
    session.start()
    time.sleep(0.3)
    session.submit_line("an answer for the record")
    deadline = time.time() + 12
    while not session.is_over() and time.time() < deadline:
        time.sleep(0.1)
    assert session.is_over()

    with open(os.path.join(session.dir, "report.md"), encoding="utf-8") as fh:
        original = fh.read()

    # "new machine": a completely unrelated path, transcript only
    moved = tmp_path / "other-host" / "restored" / os.path.basename(session.dir)
    shutil.copytree(session.dir, moved)
    assert regrade_main([str(moved)]) == 0
    with open(moved / "report.regraded.md", encoding="utf-8") as fh:
        assert fh.read() == original


def test_packs_load_from_absolute_paths(tmp_path):
    src = os.path.abspath("packs/verbal-drill")
    copy = tmp_path / "elsewhere" / "verbal-drill"
    shutil.copytree(src, copy)
    pack = load_pack(str(copy))
    assert pack.name == "verbal-drill" and len(pack.tasks) == 8


def test_session_ids_carry_no_absolute_paths(tmp_path):
    """Transcripts must not bake in machine-specific absolute paths for
    anything regrade needs (workspace paths in rows are relative)."""
    from harness.events import read_transcript
    from harness.pack import Pack

    raw = mini_pack_raw(
        session={"minutes": 0.1, "workspace": True, "idle_threshold_s": 30,
                 "debounce_ms": 150},
    )
    session = Session(
        Pack(raw),
        {
            "model": {"provider": "canned"},
            "run": {"timeout_s": 10.0, "output_max_chars": 2000},
            "paths": {"sessions_dir": str(tmp_path)},
        },
        Canned(),
        Pane(interactive=False),
        sessions_dir=str(tmp_path),
    )
    session.start()
    time.sleep(0.3)
    with open(os.path.join(session.workspace, "f.txt"), "w", encoding="utf-8") as fh:
        fh.write("hello\n")
    time.sleep(0.8)
    session.submit_line("/end")
    deadline = time.time() + 12
    while not session.is_over() and time.time() < deadline:
        time.sleep(0.1)
    rows = read_transcript(os.path.join(session.dir, "transcript.jsonl"))
    saves = [r for r in rows if r["kind"] == "file_saved"]
    assert saves and all(not os.path.isabs(r["path"]) for r in saves)
