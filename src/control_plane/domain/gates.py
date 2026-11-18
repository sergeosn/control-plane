"""`gates/<attempt>/results.json`: what the checks of one attempt found (docs/SPEC.md 5, 11)."""

from typing import Literal

from pydantic import Field

from control_plane.domain.base import DomainModel

GateStatus = Literal["pass", "fail", "skipped"]
"""`skipped`: the gate has no command in `project.yaml`. It does not fail the checks."""


class GateRecord(DomainModel):
    name: str = Field(min_length=1)
    status: GateStatus
    summary: str
    output_file: str
    """Relative to the run folder: `gates/<attempt>/<name>.txt`."""


class GateReport(DomainModel):
    attempt: int = Field(ge=1)
    outcome: Literal["pass", "fail"]
    """The outcome of the flow's `gate` step."""
    gates: list[GateRecord]
    out_of_plan: list[str]
    """The `scope` check: changed files the plan's `files_to_touch` did not name."""

    @property
    def failed(self) -> list[str]:
        return [gate.name for gate in self.gates if gate.status == "fail"]
