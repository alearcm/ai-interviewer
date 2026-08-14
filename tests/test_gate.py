from conftest import mini_pack_raw

from harness.facts import NEVER
from harness.gate import Gate
from harness.pack import Pack


def facts(**over):
    base = {
        "kind": "idle",
        "mark": "",
        "elapsed_s": 100.0,
        "elapsed_min": 100 / 60,
        "remaining_s": 200.0,
        "remaining_min": 200 / 60,
        "idle_s": 10.0,
        "saves_total": 0,
        "runs_total": 0,
        "user_messages_total": 0,
        "speaks_total": 0,
        "unprompted_speaks": 0,
        "budget_left": 4,
        "last_run_status": -1,
        "last_run_ok": False,
        "failed_run_streak": 0,
        "since_last_speak_s": NEVER,
        "since_last_save_s": NEVER,
        "since_last_run_s": NEVER,
        "since_last_user_message_s": NEVER,
        "task_open": True,
        "task_index": 0,
        "tasks_total": 2,
        "tasks_left": 1,
        "task_elapsed_s": 50.0,
        "task_user_messages": 0,
        "hint_level": 0,
        "has_workspace": False,
    }
    base.update(over)
    return base


def make_gate(rules, ladder=("a", "b", "c")):
    raw = mini_pack_raw(rules=list(rules), hints__ladder=list(ladder))
    return Gate(Pack(raw))


def test_default_is_silence():
    gate = make_gate([])
    decision = gate.evaluate(facts(), 100.0)
    assert decision.rule is None
    assert decision.action == "silence"


def test_no_rule_for_this_wake_kind_stays_silent():
    gate = make_gate([{"id": "r", "on": ["user_message"], "when": "true"}])
    decision = gate.evaluate(facts(kind="idle"), 100.0)
    assert decision.rule is None


def test_when_false_stays_silent_and_is_logged():
    gate = make_gate([{"id": "r", "on": ["idle"], "when": "idle_s >= 120"}])
    decision = gate.evaluate(facts(idle_s=10), 100.0)
    assert decision.rule is None
    assert decision.evaluations == [{"rule": "r", "fired": False, "reason": "when_false"}]


def test_budget_blocks_counting_rules_only():
    counting = {"id": "c", "on": ["idle"], "when": "true", "counts_toward_budget": True}
    free = {"id": "f", "on": ["user_message"], "when": "true", "counts_toward_budget": False}
    gate = make_gate([counting, free])
    blocked = gate.evaluate(facts(kind="idle", budget_left=0), 100.0)
    assert blocked.rule is None
    assert blocked.evaluations[0]["reason"] == "budget"
    ok = gate.evaluate(facts(kind="user_message", budget_left=0), 100.0)
    assert ok.rule is not None and ok.rule.id == "f"


def test_cooldown():
    gate = make_gate([{"id": "r", "on": ["idle"], "when": "true", "cooldown_s": 100}])
    first = gate.evaluate(facts(), 50.0)
    assert first.rule.id == "r"
    gate.commit(first, 50.0)
    during = gate.evaluate(facts(), 100.0)
    assert during.rule is None
    assert during.evaluations[0]["reason"] == "cooldown"
    after = gate.evaluate(facts(), 151.0)
    assert after.rule.id == "r"


def test_max_fires():
    gate = make_gate([{"id": "r", "on": ["idle"], "when": "true", "max_fires": 1}])
    first = gate.evaluate(facts(), 1.0)
    gate.commit(first, 1.0)
    second = gate.evaluate(facts(), 2.0)
    assert second.rule is None
    assert second.evaluations[0]["reason"] == "max_fires"


def test_priority_and_shadowing():
    low = {"id": "low", "on": ["idle"], "when": "true", "priority": 1}
    high = {"id": "high", "on": ["idle"], "when": "true", "priority": 9}
    gate = make_gate([low, high])
    decision = gate.evaluate(facts(), 1.0)
    assert decision.rule.id == "high"
    reasons = {e["rule"]: e["reason"] for e in decision.evaluations}
    assert reasons == {"high": "fired", "low": "shadowed"}


def test_hint_never_skips_levels():
    gate = make_gate([{"id": "r", "on": ["idle"], "when": "true", "hint": 3}])
    decision = gate.evaluate(facts(hint_level=0), 1.0)
    assert decision.hint_level == 1  # asked for 3, clamped to current+1


def test_hint_escalates_one_step_and_caps_at_ladder_top():
    gate = make_gate([{"id": "r", "on": ["idle"], "when": "true", "hint": "escalate"}])
    assert gate.evaluate(facts(hint_level=0), 1.0).hint_level == 1
    assert gate.evaluate(facts(hint_level=1), 2.0).hint_level == 2
    assert gate.evaluate(facts(hint_level=2), 3.0).hint_level == 3
    assert gate.evaluate(facts(hint_level=3), 4.0).hint_level == 3


def test_determinism_same_facts_same_decision():
    rules = [
        {"id": "a", "on": ["idle"], "when": "idle_s > 5", "priority": 2},
        {"id": "b", "on": ["any"], "when": "true", "priority": 1},
    ]
    d1 = make_gate(rules).evaluate(facts(), 9.0)
    d2 = make_gate(rules).evaluate(facts(), 9.0)
    assert d1.rule.id == d2.rule.id == "a"
    assert d1.evaluations == d2.evaluations
