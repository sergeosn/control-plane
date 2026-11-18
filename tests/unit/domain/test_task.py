"""Task 2: the task file format (docs/SPEC.md section 6) round-trips and rejects malformed files."""

from pathlib import Path

import pytest

from control_plane.domain import (
    AcceptanceCriterion,
    Budget,
    Roles,
    Task,
    TaskFileError,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "tasks"
SPEC_EXAMPLE = (FIXTURES / "T-0017-json-status.md").read_text()


def make_task(**overrides: object) -> Task:
    fields: dict[str, object] = {
        "id": "T-0001",
        "title": "A task: with punctuation",
        "project": "control-plane",
        "lane": "development",
        "priority": 1,
        "plan_approval": True,
        "roles": Roles(planner="default", worker="default", reviewer="strict"),
        "budget": Budget(max_iterations=2, max_cost_usd=1.5),
        "status": "backlog",
        "goal": "Do the thing.\n\nIn two paragraphs.",
        "acceptance_criteria": [
            AcceptanceCriterion(id="AC1", text="First."),
            AcceptanceCriterion(id="AC2", text="Second, with `code` and: colons."),
        ],
    }
    fields.update(overrides)
    return Task(**fields)


def test_spec_example_parses_front_matter_and_body() -> None:
    task = Task.from_markdown(SPEC_EXAMPLE)

    assert task.id == "T-0017"
    assert task.title == "Add --json output to `cplane status`"
    assert task.project == "control-plane"
    assert task.lane == "development"
    assert task.priority == 2
    assert task.plan_approval is False
    assert task.roles == Roles(planner="default", worker="default", reviewer="strict")
    assert task.budget == Budget(max_iterations=3, max_cost_usd=4.0)
    assert task.status == "backlog"
    assert task.goal == "`cplane status` can print machine-readable output."
    assert [c.id for c in task.acceptance_criteria] == ["AC1", "AC2", "AC3"]
    assert task.acceptance_criteria[1].text == "Without the flag the output is unchanged."


def test_task_round_trips_through_markdown() -> None:
    task = make_task()

    text = task.to_markdown()

    assert Task.from_markdown(text) == task


def test_spec_example_round_trips_through_markdown() -> None:
    task = Task.from_markdown(SPEC_EXAMPLE)

    assert Task.from_markdown(task.to_markdown()) == task


def test_rendering_is_idempotent() -> None:
    text = make_task().to_markdown()

    assert Task.from_markdown(text).to_markdown() == text


def test_rendered_task_has_front_matter_and_sections() -> None:
    text = make_task().to_markdown()

    assert text.startswith("---\nid: T-0001\n")
    assert "\n## Goal\n" in text
    assert "\n## Acceptance criteria\n" in text
    assert "- [AC1] First." in text


@pytest.mark.parametrize(
    ("broken", "field"),
    [
        (SPEC_EXAMPLE.replace("priority: 2", "priority: high"), "priority"),
        (SPEC_EXAMPLE.replace("title: Add --json output to `cplane status`\n", ""), "title"),
        (
            SPEC_EXAMPLE.replace("  max_cost_usd: 4.00", "  max_cost_usd: free"),
            "budget.max_cost_usd",
        ),
        (SPEC_EXAMPLE.replace("  reviewer: strict\n", ""), "roles.reviewer"),
        (SPEC_EXAMPLE.replace("status: backlog", "status: later"), "status"),
        (SPEC_EXAMPLE.replace("id: T-0017", "id: 17"), "id"),
        (SPEC_EXAMPLE.replace("status: backlog", "status: backlog\ncolour: red"), "colour"),
        (
            SPEC_EXAMPLE.replace(
                "## Goal\n\n`cplane status` can print machine-readable output.\n", ""
            ),
            "goal",
        ),
        (
            SPEC_EXAMPLE.replace(
                "- [AC2] Without the flag the output is unchanged.", "- [AC1] Twice."
            ),
            "acceptance_criteria",
        ),
        (
            SPEC_EXAMPLE.replace(
                "- [AC3] Both paths are covered by tests.", "- Both paths are covered by tests."
            ),
            "acceptance_criteria",
        ),
        (SPEC_EXAMPLE.split("## Acceptance criteria")[0], "acceptance_criteria"),
        (SPEC_EXAMPLE + "\n## Notes\n\nSomething.\n", "body"),
    ],
)
def test_malformed_task_file_is_rejected_with_the_field_named(broken: str, field: str) -> None:
    with pytest.raises(TaskFileError) as exc_info:
        Task.from_markdown(broken)

    assert field in str(exc_info.value)


@pytest.mark.parametrize(
    "text",
    [
        "## Goal\n\nNo front matter.\n",
        "---\nid: T-0001\n## Goal\n",
        "---\nid: [unclosed\n---\n## Goal\n",
    ],
)
def test_missing_or_unparseable_front_matter_is_rejected(text: str) -> None:
    with pytest.raises(TaskFileError, match="front matter"):
        Task.from_markdown(text)


def test_task_file_error_is_a_value_error() -> None:
    assert issubclass(TaskFileError, ValueError)


@pytest.mark.parametrize(
    ("text", "field"),
    [
        ("---\n- just\n- a list\n---\n\n## Goal\n\nx\n", "front matter"),
        (SPEC_EXAMPLE + "\n## Goal\n\nAgain.\n", "body"),
        (SPEC_EXAMPLE.replace("\n## Goal\n", "\nStray text.\n\n## Goal\n"), "body"),
    ],
    ids=["front-matter-not-a-mapping", "duplicate-section", "text-before-first-section"],
)
def test_other_structural_problems_name_what_is_wrong(text: str, field: str) -> None:
    with pytest.raises(TaskFileError, match=field):
        Task.from_markdown(text)
