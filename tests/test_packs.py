import pytest
from conftest import mini_pack_raw

from harness.pack import Pack, PackError, load_pack


def test_pack_a_loads_and_matches_spec():
    pack = load_pack("packs/python-idiom-fluency")
    assert pack.workspace is True
    assert pack.minutes == 45
    assert len(pack.tasks) == 12
    assert len(pack.ladder) == 3
    assert pack.interjection_budget == 4
    assert pack.max_sentences == 2
    rule_ids = {r.id for r in pack.rules}
    assert "answer-when-spoken-to" in rule_ids and "stuck-too-long" in rule_ids
    # every task ships its interviewer-only ambiguities and appendix
    for task in pack.tasks:
        assert "resolve ONLY if asked" in task["notes"]
        assert "Hidden checks" in task["appendix"]
    section_types = [s["type"] for s in pack.sections]
    assert "watchlist" in section_types and "gaps" in section_types
    watch = next(s for s in pack.sections if s["type"] == "watchlist")
    assert len(watch["patterns"]) >= 8


def test_pack_b_loads_with_no_workspace_and_no_ladder():
    pack = load_pack("packs/verbal-drill")
    assert pack.workspace is False
    assert pack.ladder == []
    assert pack.interjection_budget == 999
    assert len(pack.tasks) == 8
    assert [s["type"] for s in pack.sections] == ["openers"]


def test_snapshot_round_trip_is_identical():
    pack = load_pack("packs/python-idiom-fluency")
    clone = Pack.from_snapshot(pack.snapshot())
    assert clone.name == pack.name
    assert len(clone.tasks) == len(pack.tasks)
    assert [r.id for r in clone.rules] == [r.id for r in pack.rules]


def test_bad_fact_name_in_rule_rejected():
    raw = mini_pack_raw(rules=[{"id": "r", "on": ["idle"], "when": "no_such_fact > 1"}])
    with pytest.raises(PackError):
        Pack(raw)


def test_unknown_wake_kind_rejected():
    raw = mini_pack_raw(rules=[{"id": "r", "on": ["keystroke"], "when": "true"}])
    with pytest.raises(PackError):
        Pack(raw)


def test_hint_without_ladder_rejected():
    raw = mini_pack_raw(
        rules=[{"id": "r", "on": ["idle"], "when": "true", "hint": "escalate"}],
        hints__ladder=[],
    )
    with pytest.raises(PackError):
        Pack(raw)


def test_unknown_report_section_rejected():
    raw = mini_pack_raw(report={"sections": [{"type": "astrology"}]})
    with pytest.raises(PackError):
        Pack(raw)
