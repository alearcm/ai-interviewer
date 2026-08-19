"""De-rusting loop: self-checks, recurrence-weighted ordering, analyze."""

import json
import os
import subprocess
import sys
import tempfile

from conftest import mini_pack_raw

from harness import checks
from harness.analyze import build_request, compact
from harness.pack import Pack, load_pack
from harness.report import recurrence_counts

RUN_CFG = {"timeout_s": 20.0, "output_max_chars": 2000}


def checked_pack(**over):
    raw = mini_pack_raw(**over)
    raw["pack"]["checks"] = {"file": "work.txt", "cmd": sys.executable + " {file}", "auto": False}
    raw["tasks"] = [
        {"id": "t1", "title": "One", "statement": "s", "check": "assert value == 1"},
        {"id": "t2", "title": "Two", "statement": "s", "check": "assert value == 2"},
        {"id": "t3", "title": "Three", "statement": "s"},  # no check
    ]
    return Pack(raw)


def rows_for(pack, saves):
    rows = [
        {"t": 0.0, "kind": "session_start", "pack": pack.name},
        {"t": 0.0, "kind": "pack_snapshot", "data": pack.snapshot()},
    ]
    t = 1.0
    for task_id, content in saves:
        rows.append({"t": t, "kind": "task_presented", "task_id": task_id, "title": task_id})
        if content is not None:
            rows.append({"t": t + 1, "kind": "file_saved", "path": "work.txt", "content": content})
        t += 10
    rows.append({"t": t, "kind": "session_end", "reason": "user"})
    return rows


def test_checks_pass_fail_and_edge_states():
    pack = checked_pack()
    rows = rows_for(pack, [("t1", "value = 1"), ("t2", "value = 1"), ("t3", "x"), ])
    results = checks.run_for_session(rows, pack, RUN_CFG)
    by_id = {r["task_id"]: r for r in results}
    assert by_id["t1"]["status"] == "ok"
    assert by_id["t2"]["status"] == "failing" and "AssertionError" in by_id["t2"]["out"]
    assert by_id["t3"]["status"] == "no-checks"


def test_checks_use_the_snapshot_from_each_tasks_own_slice():
    pack = checked_pack()
    # t1's correct answer is saved during t1; during t2 nothing is saved
    rows = rows_for(pack, [("t1", "value = 1"), ("t2", None)])
    results = checks.run_for_session(rows, pack, RUN_CFG)
    assert results[0]["status"] == "ok"
    assert results[1]["status"] == "nothing-saved"


def test_recurrence_counts_and_ordering(tmp_path):
    log = tmp_path / "rec.tsv"
    log.write_text(
        "s1\tindex-loop\tf.py\t3\tline\n"
        "s1\tindex-loop\tf.py\t9\tline\n"
        "s2\tstr-concat-loop\tf.py\t2\tline\n",
        encoding="utf-8",
    )
    counts = recurrence_counts(str(log))
    assert counts == {"index-loop": 2, "str-concat-loop": 1}
    assert recurrence_counts(str(tmp_path / "missing.tsv")) == {}

    # ordering: the task focused on the repeat offender comes first
    from harness.adapters import Canned
    from harness.chat import Pane
    from harness.session import Session

    raw = mini_pack_raw()
    raw["pack"]["tasks"] = {"order": "recurrence"}
    raw["pack"]["report"] = {"recurrence_log": str(log), "sections": []}
    raw["tasks"] = [
        {"id": "cold", "title": "c", "statement": "s", "focus": ["never-tripped"]},
        {"id": "hot", "title": "h", "statement": "s", "focus": ["index-loop"]},
    ]
    settings = {
        "model": {"provider": "canned"},
        "run": RUN_CFG,
        "paths": {"sessions_dir": str(tmp_path / "sessions")},
    }
    session = Session(Pack(raw), settings, Canned(), Pane(interactive=False),
                      sessions_dir=str(tmp_path / "sessions"))
    assert [t["id"] for t in session.task_order] == ["hot", "cold"]


def test_analyze_request_is_offline_and_compact():
    pack = load_pack("packs/python-idiom-fluency")
    rows = [
        {"t": 0.0, "kind": "session_start", "pack": pack.name},
        {"t": 0.0, "kind": "pack_snapshot", "data": pack.snapshot()},
        {"t": 5.0, "kind": "gate_decision", "wake": "idle", "rule": None,
         "facts": {"x": 1}, "evaluations": [{"rule": "r"}]},
        {"t": 9.0, "kind": "user_message", "text": "hello"},
    ]
    system, user = build_request(rows)
    assert "Java-shaped" in system          # the pack's own rubric is used
    assert "pack_snapshot" not in user      # embedded pack never shipped out
    assert '"facts"' not in user            # gate bookkeeping trimmed
    assert "hello" in user


def test_analyze_compaction_bounds_size():
    big = "x = 1\n" * 20_000
    rows = [{"t": float(i), "kind": "file_saved", "path": "a.txt", "content": big} for i in range(6)]
    out = compact(rows, cap_chars=300_000)
    kept = [r for r in out if "omitted" not in r["content"]]
    assert kept[0]["t"] == 0.0 and kept[-1]["t"] == 5.0  # first and last survive
    assert sum(len(json.dumps(r)) for r in out) <= 320_000


def test_rehab_pack_loads_with_shared_recurrence_log():
    rehab = load_pack("packs/python-rehab")
    fluency = load_pack("packs/python-idiom-fluency")
    assert rehab.tasks_order == "recurrence"
    assert rehab.recurrence_log == fluency.recurrence_log  # shared spaced repetition
    assert len(rehab.tasks) == 12
    assert rehab.checks_auto and rehab.checks_file == "solution.py"
    for task in rehab.tasks:
        assert task["focus"], "rehab drill %s has no focus ids" % task["id"]
        assert task["check"], "rehab drill %s has no hidden checks" % task["id"]
    watch = next(s for s in rehab.sections if s["type"] == "watchlist")
    ids = {p.id for p in watch["patterns"]}
    for task in rehab.tasks:
        assert set(task["focus"]) <= ids, task["id"]


def test_pack_a_tasks_now_carry_checks_and_focus():
    pack = load_pack("packs/python-idiom-fluency")
    assert pack.checks_auto
    for task in pack.tasks:
        assert task["check"].strip(), task["id"]
        assert task["focus"], task["id"]


def test_rehab_checks_run_green_against_references():
    """Each drill's reference must pass its own hidden checks through
    the real check runner path."""
    pack = load_pack("packs/python-rehab")
    saves = []
    for task in pack.tasks[:4]:  # a sample keeps the suite fast
        ref = task["appendix"].split("Idiomatic reference:\n", 1)[1]
        saves.append((task["id"], ref))
    rows = [
        {"t": 0.0, "kind": "session_start", "pack": pack.name},
        {"t": 0.0, "kind": "pack_snapshot", "data": pack.snapshot()},
    ]
    t = 1.0
    for task_id, content in saves:
        rows.append({"t": t, "kind": "task_presented", "task_id": task_id, "title": task_id})
        rows.append({"t": t + 1, "kind": "file_saved", "path": "solution.py", "content": content})
        t += 10
    rows.append({"t": t, "kind": "session_end", "reason": "user"})
    results = checks.run_for_session(rows, pack, RUN_CFG)
    assert all(r["status"] == "ok" for r in results), results


def test_rehab_pack_guards_against_hollow_passes():
    """The advance rule must require a save inside the current drill —
    an untouched seed or a `ls`-style run can't fake a pass — and every
    seed must carry a visible self-test so exit 0 means behavior held."""
    rehab = load_pack("packs/python-rehab")
    adv = next(r for r in rehab.rules if r.id == "advance-on-clean-run")
    assert "since_last_save_s" in adv.raw["when"]
    assert any(r.id == "set-complete" for r in rehab.rules)
    for task in rehab.tasks:
        seed = next(s for s in task["seed"] if s["path"] == "solution.py")
        assert '__main__' in seed["content"], task["id"]


def test_rehab_seeds_run_green(tmp_path):
    """Seeds ship runnable: the self-test block passes as given, so the
    learner's job is purely the rewrite, never fixing our harness."""
    from harness.runner import run_command

    rehab = load_pack("packs/python-rehab")
    for task in rehab.tasks:
        seed = next(s for s in task["seed"] if s["path"] == "solution.py")
        name = "%s.py" % task["id"]
        with open(os.path.join(str(tmp_path), name), "w", encoding="utf-8") as fh:
            fh.write(seed["content"])
        res = run_command(sys.executable + " " + name, str(tmp_path), RUN_CFG)
        assert res["exit_status"] == 0, (task["id"], res["err"])
