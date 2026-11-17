"""An entry in the approval queue (SPEC.md section 12), one line of `approvals.jsonl`."""

from datetime import datetime
from typing import Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from control_plane.domain.base import DomainModel

ApprovalType = Literal["question", "plan", "tool_call", "budget", "merge"]

DECISIONS: dict[str, frozenset[str]] = {
    "plan": frozenset({"approve", "deny"}),
    "tool_call": frozenset({"approve", "deny"}),
    "merge": frozenset({"approve", "deny"}),
    "budget": frozenset({"extend", "end"}),
}
"""Allowed answers per type. A `question` takes any text as its answer."""


class Approval(DomainModel):
    id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    type: ApprovalType
    asked: str = Field(min_length=1)
    evidence: list[str]
    created_at: AwareDatetime
    answer: str | None = None
    note: str | None = None
    decided_at: AwareDatetime | None = None

    @property
    def decided(self) -> bool:
        return self.answer is not None

    def decide(self, answer: str, *, at: datetime, note: str | None = None) -> Self:
        """A copy of this entry with the human's decision recorded and validated."""
        return self.model_validate(
            {**self.model_dump(), "answer": answer, "note": note, "decided_at": at}
        )

    @model_validator(mode="after")
    def _decision_is_consistent(self) -> Self:
        if (self.answer is None) != (self.decided_at is None):
            raise ValueError("answer and decided_at must be set together")
        if self.answer is None:
            return self
        allowed = DECISIONS.get(self.type)
        if allowed is not None and self.answer not in allowed:
            raise ValueError(
                f"answer {self.answer!r} is not a decision for type {self.type!r};"
                f" expected one of {sorted(allowed)}"
            )
        if self.answer == "deny" and not self.note:
            raise ValueError("a deny needs its reason in note")
        return self
