"""One line of a run's `events.jsonl` (docs/SPEC.md section 5)."""

from pydantic import AwareDatetime, Field

from control_plane.domain.base import DomainModel


class Event(DomainModel):
    ts: AwareDatetime
    run_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    step: str | None = None
    attempt: int | None = None
    outcome: str | None = None
    message: str | None = None
