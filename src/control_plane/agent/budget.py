"""Step budgets (SPEC.md section 8.5).

Four limits per step: turns, tokens, cost and wall time. They are checked before each model
call. At 80% of any of them the next `tool_result` carries a notice with what is left; a
breach fails the step. The check is a pure function of the budget and the counters, so the
loop can run it as often as it likes and remember which notices it already gave.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field

from pydantic import Field

from control_plane.domain.base import DomainModel

WARN_AT = 0.8


class StepBudget(DomainModel):
    max_turns: int = Field(ge=1)
    max_total_tokens: int = Field(ge=1)
    max_cost_usd: float = Field(gt=0)
    max_wall_seconds: float = Field(gt=0)


class BudgetCounters(DomainModel):
    """What the step has used so far. `wall_seconds` is measured from the step's start."""

    turns: int = Field(ge=0, default=0)
    total_tokens: int = Field(ge=0, default=0)
    cost_usd: float = Field(ge=0, default=0.0)
    wall_seconds: float = Field(ge=0, default=0.0)


@dataclass(frozen=True)
class BudgetCheck:
    """`breached` names the first exhausted budget; `notices` are the new 80% warnings and
    `warned` the budgets they are about, so the caller can remember them."""

    breached: str | None
    notices: list[str] = field(default_factory=list)
    warned: set[str] = field(default_factory=set)


_BUDGETS: tuple[tuple[str, str, str], ...] = (
    ("turns", "max_turns", "turns"),
    ("total_tokens", "max_total_tokens", "tokens"),
    ("cost_usd", "max_cost_usd", "USD"),
    ("wall_seconds", "max_wall_seconds", "seconds"),
)
"""Counter field, limit field and the unit for messages, in the order breaches are reported."""


def check_budget(
    budget: StepBudget, counters: BudgetCounters, warned: Iterable[str] = ()
) -> BudgetCheck:
    already = set(warned)
    breached: str | None = None
    notices: list[str] = []
    newly: set[str] = set()
    for name, limit_name, unit in _BUDGETS:
        used = getattr(counters, name)
        limit = getattr(budget, limit_name)
        if used >= limit:
            if breached is None:
                breached = f"{name} budget exhausted: {used:g} of {limit:g} {unit} used"
            continue
        if used >= WARN_AT * limit and name not in already:
            newly.add(name)
            notices.append(
                f"engine notice: {WARN_AT:.0%} of the {name} budget is used: {used:g} of "
                f"{limit:g} {unit}, {limit - used:g} {unit} left; finish soon"
            )
    return BudgetCheck(breached, notices, newly)
