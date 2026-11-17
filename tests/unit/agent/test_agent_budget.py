"""Task 8: step budgets are checked before each model call; 80% raises a notice (8.5)."""

import pytest
from pydantic import ValidationError

from control_plane.agent import BudgetCounters, StepBudget, check_budget

BUDGET = StepBudget(max_turns=10, max_total_tokens=1000, max_cost_usd=1.0, max_wall_seconds=100)


def counters(**values: float) -> BudgetCounters:
    return BudgetCounters(**values)


def test_nothing_is_reported_while_every_budget_is_below_80_percent() -> None:
    check = check_budget(
        BUDGET, counters(turns=7, total_tokens=799, cost_usd=0.79, wall_seconds=79)
    )

    assert check.breached is None
    assert check.notices == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("turns", 10), ("total_tokens", 1000), ("cost_usd", 1.0), ("wall_seconds", 100.0)],
)
def test_reaching_a_limit_is_a_breach_that_names_the_budget(field: str, value: float) -> None:
    check = check_budget(BUDGET, counters(**{field: value}))

    assert check.breached is not None
    assert field.split("_")[0] in check.breached


def test_the_first_breached_budget_is_reported_in_table_order() -> None:
    check = check_budget(BUDGET, counters(turns=10, cost_usd=5.0))

    assert check.breached is not None
    assert "turn" in check.breached


@pytest.mark.parametrize(
    ("field", "value", "left"),
    [
        ("turns", 8, "2"),
        ("total_tokens", 800, "200"),
        ("cost_usd", 0.8, "0.2"),
        ("wall_seconds", 80.0, "20"),
    ],
)
def test_80_percent_of_a_budget_raises_a_notice_that_says_what_is_left(
    field: str, value: float, left: str
) -> None:
    check = check_budget(BUDGET, counters(**{field: value}))

    assert check.breached is None
    assert len(check.notices) == 1
    assert "80%" in check.notices[0]
    assert left in check.notices[0]


def test_a_budget_already_warned_about_is_not_warned_about_again() -> None:
    check = check_budget(BUDGET, counters(turns=9), warned={"turns"})

    assert check.notices == []
    assert check.warned == set()


def test_a_new_warning_names_the_budget_so_it_can_be_remembered() -> None:
    check = check_budget(BUDGET, counters(turns=9, cost_usd=0.9))

    assert check.warned == {"turns", "cost_usd"}
    assert len(check.notices) == 2


def test_a_step_budget_rejects_zero_limits() -> None:
    with pytest.raises(ValidationError, match="max_turns"):
        StepBudget(max_turns=0, max_total_tokens=1, max_cost_usd=1.0, max_wall_seconds=1)
