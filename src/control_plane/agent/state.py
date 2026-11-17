"""`state.json` and `usage.json` of a step (SPEC.md sections 5 and 8.6).

`state.json` is rewritten after every model response and every tool result. It holds the
budget counters, the calls of the current model turn with their status, and the counters of
the loop's own rules (nudges, repairs, the `max_tokens` retry, repeated calls), so a step can
resume after a park or a crash. A call is marked `started` before it runs and `finished`
after, which is what crash recovery (section 12) reads.
"""

from typing import Any, Literal

from pydantic import AwareDatetime, Field

from control_plane.agent.budget import BudgetCounters
from control_plane.agent.repeats import RepeatTracker
from control_plane.domain.base import DomainModel
from control_plane.domain.calls import Decision

CallStatus = Literal["pending", "started", "finished"]
StepStatus = Literal["running", "parked", "finished"]


class CallState(DomainModel):
    """One `tool_use` of the current model turn.

    A parked call stays `pending` and carries the id of its approval entry, which is what
    `resume_step` reads to find the human's decision (section 12).
    """

    tool_use_id: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    arguments: dict[str, Any]
    decision: Decision
    status: CallStatus = "pending"
    content: str | None = None
    is_error: bool = False
    approval_id: str | None = None


class TokenCounts(DomainModel):
    input_tokens: int = Field(ge=0, default=0)
    output_tokens: int = Field(ge=0, default=0)
    cache_read_input_tokens: int = Field(ge=0, default=0)
    cache_creation_input_tokens: int = Field(ge=0, default=0)


class StepState(DomainModel):
    step: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    role: str = Field(min_length=1)
    status: StepStatus = "running"
    outcome: str | None = None
    reason: str | None = None
    started_at: AwareDatetime
    counters: BudgetCounters = Field(default_factory=BudgetCounters)
    tokens: TokenCounts = Field(default_factory=TokenCounts)
    warned: list[str] = Field(default_factory=list)
    notices: list[str] = Field(default_factory=list)
    calls: list[CallState] = Field(default_factory=list)
    nudged: bool = False
    repairs: int = Field(ge=0, default=0)
    max_tokens_retried: bool = False
    repeat: RepeatTracker = Field(default_factory=RepeatTracker)

    @property
    def pending(self) -> list[CallState]:
        return [call for call in self.calls if call.status == "pending"]


class StepUsage(DomainModel):
    """`usage.json`: the step's totals, for the metrics of section 16."""

    turns: int
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    total_tokens: int
    cost_usd: float
    wall_seconds: float

    @classmethod
    def from_state(cls, state: StepState) -> "StepUsage":
        return cls(
            turns=state.counters.turns,
            **state.tokens.model_dump(),
            total_tokens=state.counters.total_tokens,
            cost_usd=state.counters.cost_usd,
            wall_seconds=state.counters.wall_seconds,
        )
