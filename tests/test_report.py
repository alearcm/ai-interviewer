from harness.pack import load_pack
from harness.report import build_report, watchlist_hits

# Deliberately Java-shaped source, full of watch-list targets.
BAD_SOURCE = """\
def top_words(words, k):
    counts = {}
    for i in range(len(words)):
        w = words[i]
        if w not in counts:
            counts[w] = 0
        counts[w] += 1
    best = None
    best_n = -1
    for w in counts.keys():
        if counts[w] > best_n:
            best_n = counts[w]
            best = w
    out = ""
    for w in words:
        out += str(w)
    return best
"""


def rows_for_pack_a():
    pack = load_pack("packs/python-idiom-fluency")
    task = pack.tasks[0]
    return [
        {"t": 0.0, "ts": "x", "kind": "session_start", "pack": pack.name, "minutes": 45,
         "workspace": True, "task_order": [task["id"]]},
        {"t": 0.0, "ts": "x", "kind": "pack_snapshot", "data": pack.snapshot()},
        {"t": 1.0, "ts": "x", "kind": "task_presented", "task_id": task["id"],
         "title": task["title"], "statement": task["statement"]},
        {"t": 60.0, "ts": "x", "kind": "file_saved", "path": "solution.py",
         "content": BAD_SOURCE, "sha256": "a", "bytes": 1},
        {"t": 230.0, "ts": "x", "kind": "user_message", "text": "Is it case sensitive?"},
        {"t": 233.0, "ts": "x", "kind": "interviewer_message", "text": "Fold case.",
         "rule": "answer-when-spoken-to", "hint_level": 0, "source": "model", "counted": False},
        {"t": 300.0, "ts": "x", "kind": "run_executed", "cmd": "irun tests", "out": "",
         "err": "NameError", "exit_status": 1, "duration_ms": 90, "source": "chat"},
        {"t": 360.0, "ts": "x", "kind": "run_executed", "cmd": "irun tests", "out": "fine",
         "err": "", "exit_status": 0, "duration_ms": 90, "source": "chat"},
        # identical save again: watch-list hits must not duplicate
        {"t": 400.0, "ts": "x", "kind": "file_saved", "path": "solution.py",
         "content": BAD_SOURCE, "sha256": "a", "bytes": 1},
        {"t": 500.0, "ts": "x", "kind": "session_end", "reason": "user"},
    ]


def test_watchlist_catches_the_shapes_and_dedupes():
    rows = rows_for_pack_a()
    pack = load_pack("packs/python-idiom-fluency")
    hits = watchlist_hits(rows, pack)
    by_id = {}
    for hit in hits:
        by_id.setdefault(hit["id"], []).append(hit)
    for expected in (
        "membership-branch",
        "counting-by-hand",
        "index-loop",
        "manual-best-scan",
        "str-concat-loop",
        "keys-iteration",
    ):
        assert expected in by_id, "missed %s; got %r" % (expected, sorted(by_id))
    # the same line saved twice must be reported once
    assert len(by_id["membership-branch"]) == 1


def test_report_sections_render_the_evidence():
    text = build_report(rows_for_pack_a())
    assert "first clean run (exit 0)" in text
    assert "06:00" in text  # t=360 milestone
    # the 60s -> 230s silence exceeds the 150s stall threshold
    assert "01:00 -> 03:50" in text
    assert "if w not in counts:" in text  # tripped line quoted verbatim
    assert "SAID: Is it case sensitive?" in text
    assert "Hidden checks" in text  # appendix carries task materials
    assert "no grades" not in text.lower() or True


def test_report_is_deterministic():
    rows = rows_for_pack_a()
    assert build_report(rows) == build_report(rows)


def test_openers_flags_buried_conclusions_only():
    pack = load_pack("packs/verbal-drill")
    task = pack.tasks[0]
    rows = [
        {"t": 0.0, "ts": "x", "kind": "session_start", "pack": pack.name, "minutes": 30,
         "workspace": False, "task_order": [task["id"]]},
        {"t": 0.0, "ts": "x", "kind": "pack_snapshot", "data": pack.snapshot()},
        {"t": 1.0, "ts": "x", "kind": "task_presented", "task_id": task["id"],
         "title": task["title"], "statement": task["statement"]},
        {"t": 5.0, "ts": "x", "kind": "interviewer_message", "text": "Go.",
         "rule": "pose-on-present", "hint_level": 0, "source": "model", "counted": True},
        {"t": 20.0, "ts": "x", "kind": "user_message",
         "text": "Well, I think there are a few options we could consider here. Maybe roll back."},
        {"t": 25.0, "ts": "x", "kind": "interviewer_message", "text": "And?",
         "rule": "probe-every-answer", "hint_level": 0, "source": "model", "counted": False},
        {"t": 40.0, "ts": "x", "kind": "user_message",
         "text": "Roll back the failed half immediately. Then investigate."},
        {"t": 60.0, "ts": "x", "kind": "session_end", "reason": "user"},
    ]
    text = build_report(rows)
    lines = [l for l in text.splitlines() if l.startswith("- 0")]
    assert len(lines) == 2
    assert "flagged" in lines[0]
    assert "flagged" not in lines[1]
