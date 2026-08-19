"""Seeding, interviewer pad writes, attribution, and the two new packs."""

import os
import time

import pytest
from conftest import mini_pack_raw

from harness import checks
from harness.adapters import Canned
from harness.chat import Pane
from harness.events import read_transcript
from harness.pack import Pack, PackError, load_pack
from harness.report import build_report, watchlist_hits
from harness.session import Session

RUN_CFG = {"timeout_s": 20.0, "output_max_chars": 2000, "backend": "local"}


def ws_pack(rules, tasks, **interviewer_over):
    raw = mini_pack_raw(
        session={"minutes": 0.5, "workspace": True, "idle_threshold_s": 30,
                 "debounce_ms": 150, "primary_file": "pad.txt"},
    )
    raw["pack"]["rules"] = rules
    raw["pack"]["interviewer"].update(interviewer_over)
    raw["tasks"] = tasks
    return Pack(raw)


def settings_for(tmp_path):
    return {
        "model": {"provider": "canned"},
        "run": RUN_CFG,
        "paths": {"sessions_dir": str(tmp_path)},
    }


def run_session(pack, tmp_path, adapter=None, script=()):
    session = Session(pack, settings_for(tmp_path), adapter or Canned(),
                      Pane(interactive=False), sessions_dir=str(tmp_path))
    session.start()
    time.sleep(0.5)  # seeding + debounce window
    for line in script:
        session.submit_line(line)
        time.sleep(0.4)
    time.sleep(0.5)
    session.submit_line("/end")
    deadline = time.time() + 10
    while not session.is_over() and time.time() < deadline:
        time.sleep(0.1)
    assert session.is_over()
    return session, read_transcript(os.path.join(session.dir, "transcript.jsonl"))


def test_seed_files_written_and_not_echoed_as_saves(tmp_path):
    tasks = [{"id": "t1", "title": "T", "statement": "s",
              "seed": [{"path": "pad.txt", "content": "def stub():\n    pass\n"}]}]
    session, rows = run_session(ws_pack([], tasks), tmp_path)
    seed_rows = [r for r in rows if r["kind"] == "pad_write" and r["rule"] == "seed"]
    assert len(seed_rows) == 1 and seed_rows[0]["path"] == "pad.txt"
    with open(os.path.join(session.workspace, "pad.txt"), encoding="utf-8") as fh:
        assert "def stub" in fh.read()
    # the engine's own write must NOT appear as a candidate save
    assert not any(r["kind"] == "file_saved" for r in rows)


def test_candidate_edits_after_seed_still_observed(tmp_path):
    tasks = [{"id": "t1", "title": "T", "statement": "s",
              "seed": [{"path": "pad.txt", "content": "seeded\n"}]}]
    pack = ws_pack([], tasks)
    session = Session(pack, settings_for(tmp_path), Canned(), Pane(interactive=False),
                      sessions_dir=str(tmp_path))
    session.start()
    time.sleep(0.5)
    with open(os.path.join(session.workspace, "pad.txt"), "w", encoding="utf-8") as fh:
        fh.write("seeded\nmine now\n")
    time.sleep(1.0)
    session.submit_line("/end")
    deadline = time.time() + 10
    while not session.is_over() and time.time() < deadline:
        time.sleep(0.1)
    rows = read_transcript(os.path.join(session.dir, "transcript.jsonl"))
    saves = [r for r in rows if r["kind"] == "file_saved"]
    assert len(saves) == 1 and "mine now" in saves[0]["content"]


def test_pack_sourced_write_rule_appends_marked_template(tmp_path):
    rules = [{
        "id": "paste-probe", "on": ["user_message"], "when": "true",
        "action": "write", "write_file": "pad.txt", "write_mode": "append",
        "write_source": "pack", "write_content": "run this:\n{task.probe}",
    }]
    tasks = [{"id": "t1", "title": "T", "statement": "s",
              "probe": "assert f(1) == 2",
              "seed": [{"path": "pad.txt", "content": "work\n"}]}]
    session, rows = run_session(ws_pack(rules, tasks), tmp_path, script=["done I think"])
    writes = [r for r in rows if r["kind"] == "pad_write" and r["rule"] == "paste-probe"]
    assert len(writes) == 1
    assert writes[0]["source"] == "pack" and writes[0]["mode"] == "append"
    with open(os.path.join(session.workspace, "pad.txt"), encoding="utf-8") as fh:
        content = fh.read()
    assert content.startswith("work\n")            # candidate text untouched
    assert "#> run this:" in content               # marker on every line
    assert "#> assert f(1) == 2" in content        # template filled from the task


def test_model_sourced_write_is_marked_and_attributed(tmp_path):
    rules = [{
        "id": "model-note", "on": ["user_message"], "when": "true",
        "action": "write", "write_file": "pad.txt", "write_source": "model",
        "counts_toward_budget": True,
    }]
    tasks = [{"id": "t1", "title": "T", "statement": "s",
              "seed": [{"path": "pad.txt", "content": "work\n"}]}]
    adapter = Canned(["What happens on empty input?"])
    session, rows = run_session(ws_pack(rules, tasks), tmp_path, adapter, script=["hm"])
    writes = [r for r in rows if r["kind"] == "pad_write" and r["rule"] == "model-note"]
    assert len(writes) == 1 and writes[0]["source"] == "model" and writes[0]["counted"]
    with open(os.path.join(session.workspace, "pad.txt"), encoding="utf-8") as fh:
        assert "#> What happens on empty input?" in fh.read()


def test_write_template_missing_field_skips_with_note(tmp_path):
    rules = [{
        "id": "bad-template", "on": ["user_message"], "when": "true",
        "action": "write", "write_file": "pad.txt",
        "write_source": "pack", "write_content": "{task.nonexistent}",
    }]
    tasks = [{"id": "t1", "title": "T", "statement": "s",
              "seed": [{"path": "pad.txt", "content": "work\n"}]}]
    _, rows = run_session(ws_pack(rules, tasks), tmp_path, script=["hm"])
    assert not any(r["kind"] == "pad_write" and r["rule"] == "bad-template" for r in rows)
    assert any(r["kind"] == "note" and "bad-template" in r.get("text", "") for r in rows)


def test_write_rules_require_workspace():
    raw = mini_pack_raw(rules=[{
        "id": "w", "on": ["idle"], "when": "true", "action": "write",
        "write_file": "x.txt", "write_source": "pack", "write_content": "y",
    }])
    with pytest.raises(PackError):
        Pack(raw)


def test_watchlist_excludes_interviewer_lines():
    pack = load_pack("packs/system-design-doc")

    def hits_for(doc):
        rows = [
            {"t": 0.0, "kind": "session_start", "pack": pack.name},
            {"t": 0.0, "kind": "pack_snapshot", "data": pack.snapshot()},
            {"t": 1.0, "kind": "task_presented", "task_id": pack.tasks[0]["id"],
             "title": "x", "statement": "s"},
            {"t": 9.0, "kind": "file_saved", "path": "design.md", "content": doc},
            {"t": 20.0, "kind": "session_end", "reason": "user"},
        ]
        return watchlist_hits(rows, pack)

    # non-vacuous: flagged words appear ONLY on interviewer-marked lines
    only_marked = "> INT: is 'scalable' a number?\n> INT: simply quantify the TODO.\n"
    assert hits_for(only_marked) == []

    mixed = "We keep it scalable.\n> INT: scalable is not a number.\n"
    hits = hits_for(mixed)
    assert len(hits) == 1 and hits[0]["line"] == "We keep it scalable."


def test_new_packs_load_with_seeds_and_materials():
    algo = load_pack("packs/leetcode-drill")
    assert len(algo.tasks) == 8
    for task in algo.tasks:
        assert task["seed"] and task["seed"][0]["path"] == "solution.py"
        assert task["probe"].strip()          # opaque pass-through field
        assert task["check"].strip()
    assert any(r.action == "write" for r in algo.rules)
    assert algo.checks_auto

    design = load_pack("packs/system-design-doc")
    assert len(design.tasks) == 6
    for task in design.tasks:
        assert task["seed"] and task["seed"][0]["path"] == "design.md"
        assert "reveal ONLY if asked" in task["notes"]
    marks = {m["id"] for m in design.clock_marks}
    assert {"high_level", "deep_dive", "scale", "wrap"} <= marks


def test_algo_references_pass_their_own_hidden_checks():
    pack = load_pack("packs/leetcode-drill")
    rows = [
        {"t": 0.0, "kind": "session_start", "pack": pack.name},
        {"t": 0.0, "kind": "pack_snapshot", "data": pack.snapshot()},
    ]
    t = 1.0
    for task in pack.tasks[:3]:
        ref = task["appendix"].split("Reference:\n", 1)[1]
        rows.append({"t": t, "kind": "task_presented", "task_id": task["id"], "title": task["id"]})
        rows.append({"t": t + 1, "kind": "file_saved", "path": "solution.py", "content": ref})
        t += 10
    rows.append({"t": t, "kind": "session_end", "reason": "user"})
    results = checks.run_for_session(rows, pack, RUN_CFG)
    assert results and all(r["status"] == "ok" for r in results), results


def test_same_second_sessions_get_distinct_dirs(tmp_path):
    pack = ws_pack([], [{"id": "t1", "title": "T", "statement": "s"}])
    a = Session(pack, settings_for(tmp_path), Canned(), Pane(interactive=False),
                sessions_dir=str(tmp_path))
    b = Session(pack, settings_for(tmp_path), Canned(), Pane(interactive=False),
                sessions_dir=str(tmp_path))
    assert a.session_id != b.session_id
    assert a.dir != b.dir


def test_append_preserves_candidate_bytes_and_create_degrades(tmp_path):
    rules = [{
        "id": "note", "on": ["user_message"], "when": "true",
        "action": "write", "write_file": "pad.txt", "write_mode": "create",
        "write_source": "pack", "write_content": "look here",
    }]
    tasks = [{"id": "t1", "title": "T", "statement": "s"}]
    pack = ws_pack(rules, tasks)
    session = Session(pack, settings_for(tmp_path), Canned(), Pane(interactive=False),
                      sessions_dir=str(tmp_path))
    session.start()
    time.sleep(0.4)
    # candidate work that is NOT valid UTF-8, with trailing blank lines
    target = os.path.join(session.workspace, "pad.txt")
    original = b"x = 'caf\xe9'  # mine\n\n\n"
    with open(target, "wb") as fh:
        fh.write(original)
    time.sleep(0.6)
    session.submit_line("hm")  # fires the create-mode write rule
    time.sleep(0.6)
    session.submit_line("/end")
    deadline = time.time() + 10
    while not session.is_over() and time.time() < deadline:
        time.sleep(0.1)
    with open(target, "rb") as fh:
        final = fh.read()
    assert final.startswith(original), "candidate bytes were modified"
    assert b"#> look here" in final
    rows = read_transcript(os.path.join(session.dir, "transcript.jsonl"))
    write = next(r for r in rows if r["kind"] == "pad_write" and r["rule"] == "note")
    assert write["mode"] == "append"  # create onto existing work degraded


def test_oversized_seed_still_echo_suppressed(tmp_path):
    raw = mini_pack_raw(
        session={"minutes": 0.5, "workspace": True, "idle_threshold_s": 30,
                 "debounce_ms": 150, "snapshot_max_bytes": 64},
    )
    raw["tasks"] = [{"id": "t1", "title": "T", "statement": "s",
                     "seed": [{"path": "big.txt", "content": "z" * 300}]}]
    _, rows = run_session(Pack(raw), tmp_path)
    assert any(r["kind"] == "pad_write" and r["rule"] == "seed" for r in rows)
    assert not any(r["kind"] == "file_saved" for r in rows)


def test_write_failure_does_not_kill_the_session(tmp_path):
    rules = [{
        "id": "note", "on": ["user_message"], "when": "true",
        "action": "write", "write_file": "pad.txt",
        "write_source": "pack", "write_content": "hello",
    }]
    tasks = [{"id": "t1", "title": "T", "statement": "s"}]
    pack = ws_pack(rules, tasks)
    session = Session(pack, settings_for(tmp_path), Canned(), Pane(interactive=False),
                      sessions_dir=str(tmp_path))
    session.start()
    time.sleep(0.3)
    os.makedirs(os.path.join(session.workspace, "pad.txt"))  # the write target is a DIRECTORY
    session.submit_line("hm")
    time.sleep(0.5)
    session.submit_line("/end")  # must still work: the loop survived
    deadline = time.time() + 10
    while not session.is_over() and time.time() < deadline:
        time.sleep(0.1)
    assert session.is_over(), "write failure killed the session loop"
    rows = read_transcript(os.path.join(session.dir, "transcript.jsonl"))
    assert any(r["kind"] == "note" and "failed" in r.get("text", "") for r in rows)
    assert rows[-1]["kind"] == "session_end"


def test_algo_checks_are_falsifiable():
    """The seeded stub must FAIL every task's hidden checks — a check
    an empty stub passes is no check at all."""
    pack = load_pack("packs/leetcode-drill")
    rows = [
        {"t": 0.0, "kind": "session_start", "pack": pack.name},
        {"t": 0.0, "kind": "pack_snapshot", "data": pack.snapshot()},
    ]
    t = 1.0
    for task in pack.tasks[:3]:
        rows.append({"t": t, "kind": "task_presented", "task_id": task["id"], "title": task["id"]})
        rows.append({"t": t + 1, "kind": "file_saved", "path": "solution.py",
                     "content": task["seed"][0]["content"]})
        t += 10
    rows.append({"t": t, "kind": "session_end", "reason": "user"})
    results = checks.run_for_session(rows, pack, RUN_CFG)
    assert results and all(r["status"] == "failing" for r in results), results


def test_pending_save_lands_in_its_own_task_slice(tmp_path):
    """The 0/3 self-check bug from live use: work is saved, the run
    passes, auto-advance fires — but the debounced file_saved row used
    to land AFTER the next task_presented, so the checker graded the
    wrong snapshot. Boundary flush must put it in the right slice."""
    raw = mini_pack_raw(
        session={"minutes": 0.5, "workspace": True, "idle_threshold_s": 30,
                 "debounce_ms": 2000},  # long debounce: advance always races it
    )
    raw["pack"]["checks"] = {"file": "work.txt", "cmd": "grep -q work {file}", "auto": False}
    raw["tasks"] = [
        {"id": "t1", "title": "One", "statement": "s", "check": "checked"},
        {"id": "t2", "title": "Two", "statement": "s", "check": "checked"},
    ]
    pack = Pack(raw)
    session = Session(pack, settings_for(tmp_path), Canned(), Pane(interactive=False),
                      sessions_dir=str(tmp_path))
    session.start()
    time.sleep(0.4)
    with open(os.path.join(session.workspace, "work.txt"), "w", encoding="utf-8") as fh:
        fh.write("task-one work\n")
    time.sleep(0.4)          # event delivered; 2s debounce still pending
    session.submit_line("/next")
    time.sleep(0.4)
    with open(os.path.join(session.workspace, "work.txt"), "w", encoding="utf-8") as fh:
        fh.write("task-two work\n")
    time.sleep(0.3)          # still pending when the session ends
    session.submit_line("/end")
    deadline = time.time() + 10
    while not session.is_over() and time.time() < deadline:
        time.sleep(0.1)

    rows = read_transcript(os.path.join(session.dir, "transcript.jsonl"))
    kinds_order = [(r["kind"], r.get("task_id") or r.get("path") or r.get("reason")) for r in rows
                   if r["kind"] in ("task_presented", "file_saved", "session_end")]
    # save #1 BEFORE task 2; save #2 BEFORE session_end
    assert kinds_order == [
        ("task_presented", "t1"),
        ("file_saved", "work.txt"),
        ("task_presented", "t2"),
        ("file_saved", "work.txt"),
        ("session_end", "user"),
    ], kinds_order

    # and the checker now grades each task against its own snapshot
    results = checks.run_for_session(rows, pack, RUN_CFG)
    assert [r["status"] for r in results] == ["ok", "ok"]


def test_advance_rule_with_prompt_debriefs_before_switching(tmp_path):
    """An advance rule that carries a prompt speaks first — grounded in
    the finished task — then presents the next one."""
    rules = [{
        "id": "debrief-and-advance", "on": ["user_message"], "when": "true",
        "action": "advance", "counts_toward_budget": False,
        "prompt": "Debrief the finished drill, then move on.",
    }]
    tasks = [
        {"id": "t1", "title": "One", "statement": "s"},
        {"id": "t2", "title": "Two", "statement": "s"},
    ]
    _, rows = run_session(ws_pack(rules, tasks), tmp_path,
                          adapter=Canned(["Verdict: fine."]), script=["done"])
    order = [(r["kind"], r.get("task_id") or r.get("rule") or "") for r in rows
             if r["kind"] in ("task_presented", "interviewer_message")]
    # opening line, task 1, then: debrief speech BEFORE task 2 appears
    assert ("interviewer_message", "debrief-and-advance") in order
    debrief_i = order.index(("interviewer_message", "debrief-and-advance"))
    t2_i = order.index(("task_presented", "t2"))
    assert debrief_i < t2_i
    speech = next(r for r in rows if r.get("rule") == "debrief-and-advance"
                  and r["kind"] == "interviewer_message")
    assert speech["text"] == "Verdict: fine."
    # task rows carry position info for the front end
    t_rows = [r for r in rows if r["kind"] == "task_presented"]
    assert [(r["seq"], r["of"]) for r in t_rows] == [(1, 2), (2, 2)]


def test_report_renders_pad_writes_with_attribution():
    pack = load_pack("packs/leetcode-drill")
    rows = [
        {"t": 0.0, "ts": "x", "kind": "session_start", "pack": pack.name},
        {"t": 0.0, "ts": "x", "kind": "pack_snapshot", "data": pack.snapshot()},
        {"t": 1.0, "ts": "x", "kind": "task_presented", "task_id": pack.tasks[0]["id"],
         "title": "T", "statement": "s"},
        {"t": 2.0, "ts": "x", "kind": "pad_write", "path": "solution.py",
         "text": "#> seeded", "mode": "create", "source": "pack", "rule": "seed",
         "counted": False, "hint_level": 0},
        {"t": 300.0, "ts": "x", "kind": "pad_write", "path": "solution.py",
         "text": "#> run this before you continue:\n#> assert f(1) == 2",
         "mode": "append", "source": "pack", "rule": "paste-probe-test",
         "counted": True, "hint_level": 0},
        {"t": 400.0, "ts": "x", "kind": "session_end", "reason": "user"},
    ]
    text = build_report(rows)
    assert "interviewer wrote into solution.py (paste-probe-test)" in text
    assert "INTERVIEWER WROTE (solution.py)" in text
    # seeds are setup, not interjections — they stay out of the timeline
    assert "(seed)" not in text
