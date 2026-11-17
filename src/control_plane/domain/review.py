"""The reviewer's artifact (SPEC.md section 8.4).

There is no verdict field. The engine derives the verdict from the rows, criteria and
findings, so the model cannot pass a review by declaring it passed.
"""

from typing import Literal

from pydantic import Field

from control_plane.domain.base import DomainModel

RubricResult = Literal["pass", "fail", "n/a"]
Severity = Literal["blocker", "major", "minor"]
Verdict = Literal["pass", "fail"]

FAILING_SEVERITIES: frozenset[str] = frozenset({"blocker", "major"})


class RubricRow(DomainModel):
    row_id: str = Field(min_length=1)
    result: RubricResult
    evidence: str


class CriterionReview(DomainModel):
    id: str = Field(min_length=1)
    met: bool
    evidence: str


class Finding(DomainModel):
    severity: Severity
    file: str = Field(min_length=1)
    line: int | None = None
    description: str
    suggestion: str | None = None


class Review(DomainModel):
    rubric: list[RubricRow]
    criteria: list[CriterionReview]
    findings: list[Finding]

    @property
    def verdict(self) -> Verdict:
        """`pass` only if no rubric row failed, every criterion is met and no finding is a
        blocker or major."""
        if any(row.result == "fail" for row in self.rubric):
            return "fail"
        if any(not criterion.met for criterion in self.criteria):
            return "fail"
        if any(finding.severity in FAILING_SEVERITIES for finding in self.findings):
            return "fail"
        return "pass"
