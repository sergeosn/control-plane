"""The planner's artifact (SPEC.md section 8.4)."""

from pydantic import Field

from control_plane.domain.base import DomainModel


class PlanStep(DomainModel):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    files: list[str]


class PlannedTest(DomainModel):
    """A test the plan commits to, naming the acceptance criterion it proves."""

    criterion: str = Field(min_length=1)
    description: str = Field(min_length=1)


class Plan(DomainModel):
    summary: str = Field(min_length=1)
    steps: list[PlanStep]
    files_to_touch: list[str]
    tests: list[PlannedTest]
    risks: list[str]
    out_of_scope: list[str]
    questions: list[str]

    @property
    def blocked(self) -> bool:
        """A plan with questions makes the step emit `blocked`."""
        return bool(self.questions)
