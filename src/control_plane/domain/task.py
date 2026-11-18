"""The task file: Markdown with YAML front matter (docs/SPEC.md section 6)."""

import re
from typing import Literal, Self

import yaml
from pydantic import Field, ValidationError, field_validator

from control_plane.domain.base import DomainModel

TaskStatus = Literal["backlog", "running", "parked", "done", "failed"]

FRONT_MATTER_DELIMITER = "---"
GOAL_HEADING = "Goal"
CRITERIA_HEADING = "Acceptance criteria"
KNOWN_HEADINGS = (GOAL_HEADING, CRITERIA_HEADING)

_HEADING = re.compile(r"^## (.+?)\s*$")
_CRITERION_LINE = re.compile(r"^- \[([A-Za-z0-9_-]+)\] (.+?)\s*$")


class TaskFileError(ValueError):
    """A task file could not be parsed. The message names the offending field."""


class Roles(DomainModel):
    """Model profile per role; profiles are defined in `plane.yaml`."""

    planner: str
    worker: str
    reviewer: str


class Budget(DomainModel):
    max_iterations: int = Field(ge=1)
    max_cost_usd: float = Field(gt=0)


class AcceptanceCriterion(DomainModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    text: str = Field(min_length=1)


class Task(DomainModel):
    id: str = Field(pattern=r"^T-\d{4}$")
    title: str = Field(min_length=1)
    project: str = Field(min_length=1)
    lane: str = Field(min_length=1)
    priority: int
    plan_approval: bool
    roles: Roles
    budget: Budget
    status: TaskStatus
    goal: str = Field(min_length=1)
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1)

    @field_validator("acceptance_criteria")
    @classmethod
    def _ids_are_unique(cls, criteria: list[AcceptanceCriterion]) -> list[AcceptanceCriterion]:
        seen: set[str] = set()
        for criterion in criteria:
            if criterion.id in seen:
                raise ValueError(f"duplicate acceptance criterion id {criterion.id!r}")
            seen.add(criterion.id)
        return criteria

    @classmethod
    def from_markdown(cls, text: str) -> Self:
        """Parse a task file. Raises `TaskFileError` naming the field that is wrong."""
        front_matter, body = _split_front_matter(text)
        data = _parse_front_matter(front_matter)
        sections = _split_sections(body)
        if GOAL_HEADING in sections:
            data["goal"] = sections[GOAL_HEADING].strip()
        if CRITERIA_HEADING in sections:
            data["acceptance_criteria"] = _parse_criteria(sections[CRITERIA_HEADING])
        try:
            return cls.model_validate(data)
        except ValidationError as error:
            raise TaskFileError(_describe(error)) from error

    def to_markdown(self) -> str:
        """Render the task file. `from_markdown(task.to_markdown()) == task`."""
        front = self.model_dump(mode="json", exclude={"goal", "acceptance_criteria"})
        front_matter = yaml.safe_dump(front, sort_keys=False, allow_unicode=True)
        criteria = "\n".join(f"- [{c.id}] {c.text}" for c in self.acceptance_criteria)
        return (
            f"{FRONT_MATTER_DELIMITER}\n{front_matter}{FRONT_MATTER_DELIMITER}\n"
            f"\n## {GOAL_HEADING}\n\n{self.goal}\n"
            f"\n## {CRITERIA_HEADING}\n\n{criteria}\n"
        )


def _split_front_matter(text: str) -> tuple[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != FRONT_MATTER_DELIMITER:
        raise TaskFileError("front matter: file must start with a '---' line")
    for index in range(1, len(lines)):
        if lines[index].rstrip() == FRONT_MATTER_DELIMITER:
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :])
    raise TaskFileError("front matter: closing '---' line not found")


def _parse_front_matter(text: str) -> dict[str, object]:
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise TaskFileError(f"front matter: not valid YAML: {error}") from error
    if not isinstance(loaded, dict):
        raise TaskFileError("front matter: must be a YAML mapping")
    return {str(key): value for key, value in loaded.items()}


def _split_sections(body: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in body.splitlines():
        heading = _HEADING.match(line)
        if heading:
            current = heading.group(1)
            if current not in KNOWN_HEADINGS:
                raise TaskFileError(f"body: unknown section '## {current}'")
            if current in sections:
                raise TaskFileError(f"body: duplicate section '## {current}'")
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
        elif line.strip():
            raise TaskFileError(f"body: text before the first section: {line.strip()!r}")
    return {name: "\n".join(lines) for name, lines in sections.items()}


def _parse_criteria(text: str) -> list[dict[str, str]]:
    criteria: list[dict[str, str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        match = _CRITERION_LINE.match(line)
        if not match:
            raise TaskFileError(
                f"acceptance_criteria: line {line.strip()!r} is not of the form '- [ID] text'"
            )
        criteria.append({"id": match.group(1), "text": match.group(2)})
    return criteria


def _describe(error: ValidationError) -> str:
    problems = []
    for item in error.errors():
        field = ".".join(str(part) for part in item["loc"]) or "task"
        problems.append(f"{field}: {item['msg']}")
    return "; ".join(problems)
