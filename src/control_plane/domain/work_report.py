"""The worker's artifact (docs/SPEC.md section 8.4).

The report does not list changed files or commands run: the engine takes those from git and
from its own tool-call log, because a self-report is a claim and the log is a fact.
"""

from typing import Literal

from pydantic import Field

from control_plane.domain.base import DomainModel

CriterionStatus = Literal["done", "not_done"]


class Criterion(DomainModel):
    id: str = Field(min_length=1)
    status: CriterionStatus
    evidence: str


class WorkReport(DomainModel):
    summary: str = Field(min_length=1)
    criteria: list[Criterion]
    deviations: list[str]
    open_issues: list[str]
