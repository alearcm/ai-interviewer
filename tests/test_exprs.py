import pytest

from harness.exprs import Expr, ExprError

NAMES = {"idle_s", "runs_total", "mark", "task_open", "kind"}


def ev(text, **facts):
    return Expr(text, NAMES).as_bool(facts)


def test_comparisons_and_arithmetic():
    assert ev("idle_s >= 120", idle_s=120)
    assert not ev("idle_s >= 120", idle_s=119.9)
    assert ev("idle_s / 60 > 1 and runs_total == 0", idle_s=90, runs_total=0)
    assert ev("10 <= idle_s <= 20", idle_s=15)
    assert not ev("10 <= idle_s <= 20", idle_s=25)


def test_boolean_and_membership():
    assert ev("task_open and not runs_total", task_open=True, runs_total=0)
    assert ev("mark in ['ten_left', 'five_left']", mark="ten_left")
    assert ev("mark not in ['x']", mark="y")
    assert ev("true")
    assert not ev("false")


def test_short_circuit():
    # 'or' must not evaluate the right side when the left is truthy
    assert ev("task_open or idle_s > 0", task_open=True, idle_s=0)


def test_unknown_name_rejected_at_parse_time():
    with pytest.raises(ExprError):
        Expr("nonexistent_fact > 3", NAMES)


@pytest.mark.parametrize(
    "bad",
    [
        "__import__('os')",
        "(lambda: 1)()",
        "idle_s.real",
        "[1,2][0]",
        "f'{idle_s}'",
        "idle_s if task_open else 0",
    ],
)
def test_dangerous_shapes_rejected(bad):
    with pytest.raises(ExprError):
        Expr(bad, NAMES)
