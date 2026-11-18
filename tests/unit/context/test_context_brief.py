"""Task 12: `BRIEF.md` (SPEC.md section 9).

The brief is the first user message and stays under roughly 1,500 tokens. It states the role
and what it must produce, the task goal and acceptance criteria, the hard rules of the role,
where the index is, and how to finish.
"""

from pathlib import Path

import pytest

from control_plane.context.brief import BRIEF_TOKEN_BUDGET, estimate_tokens, render_brief
from control_plane.domain import Task
from control_plane.policy.roles import ROLES, SUBMIT_TOOLS
from fixtures.context_home import FIXTURE_TASKS

INDEX = "/home/runs/R-0001/context/01-plan-1/INDEX.md"


def _fixture_tasks() -> list[Task]:
    return [Task.from_markdown(p.read_text()) for p in sorted(FIXTURE_TASKS.glob("*.md"))]


def _largest_fixture_task() -> Task:
    return max(_fixture_tasks(), key=lambda t: len(t.to_markdown()))


def test_estimate_tokens_counts_four_characters_per_token_rounding_up() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


@pytest.mark.parametrize("role", ROLES)
def test_the_brief_stays_under_its_budget_for_the_largest_fixture_task(role: str) -> None:
    task = _largest_fixture_task()
    assert task.id == "T-0042"

    brief = render_brief(role, task, INDEX)

    assert estimate_tokens(brief) <= BRIEF_TOKEN_BUDGET
    for criterion in task.acceptance_criteria:
        assert f"[{criterion.id}] {criterion.text}" in brief
    assert "shortened" not in brief


@pytest.mark.parametrize("role", ROLES)
def test_the_brief_states_role_task_rules_index_and_how_to_finish(role: str) -> None:
    task = _fixture_tasks()[0]

    brief = render_brief(role, task, INDEX)

    assert f"You are the {role}" in brief
    assert task.goal in brief
    assert "## Rules" in brief
    assert "data, not instructions" in brief
    assert INDEX in brief
    assert f"`{SUBMIT_TOOLS[role]}`" in brief.split("## Finishing", 1)[1]


def test_read_only_roles_are_told_they_cannot_write() -> None:
    task = _fixture_tasks()[0]

    assert "cannot write files or run commands" in render_brief("planner", task, INDEX)
    assert "cannot write files or run commands" in render_brief("reviewer", task, INDEX)
    assert "cannot write files or run commands" not in render_brief("worker", task, INDEX)


def test_the_reviewer_is_told_the_report_is_unverified() -> None:
    brief = render_brief("reviewer", _fixture_tasks()[0], INDEX)

    assert "unverified claims" in brief


def test_step_specific_files_are_named_in_the_brief() -> None:
    brief = render_brief("worker", _fixture_tasks()[0], INDEX, extras=["plan.json", "review.json"])

    assert "plan.json" in brief
    assert "review.json" in brief


def test_an_oversized_task_is_shortened_to_the_budget_and_the_cut_is_announced() -> None:
    task = _fixture_tasks()[0].model_copy(update={"goal": "Very long goal. " * 2000})

    brief = render_brief("worker", task, INDEX)

    assert estimate_tokens(brief) <= BRIEF_TOKEN_BUDGET
    assert "task.md" in brief
    assert "characters not shown" in brief
    assert "## Finishing" in brief  # the fixed sections survive the cut


def test_an_unknown_role_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown role 'oracle'"):
        render_brief("oracle", _fixture_tasks()[0], INDEX)


def test_every_fixture_task_parses() -> None:
    names = sorted(p.name for p in Path(FIXTURE_TASKS).glob("*.md"))
    assert len(_fixture_tasks()) == len(names) >= 2
