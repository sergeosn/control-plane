"""Task 12: per-role context recipes and `INDEX.md` (docs/SPEC.md section 9).

`build_step_context` only computes; it writes nothing. The app service writes its files.
"""

import re
from pathlib import Path

import pytest

from control_plane.context.builder import (
    BRIEF_FILE,
    INDEX_FILE,
    BuiltContext,
    ContextError,
    StepInputs,
    build_step_context,
)
from control_plane.context.recipes import RECIPES
from control_plane.domain import Plan, Review, WorkReport
from control_plane.policy.roles import ROLES
from fixtures.context_home import ContextHome, make_context_home
from fixtures.loop_harness import VALID_PLAN, VALID_REVIEW, VALID_WORK_REPORT

INDEX_LINE = re.compile(r"^- `([^`]+)` \((\d+) lines?\): (.+)$")

PLAN = Plan.model_validate(VALID_PLAN)
REPORT = WorkReport.model_validate(VALID_WORK_REPORT)
REVIEW = Review.model_validate(
    {
        **VALID_REVIEW,
        "findings": [
            {
                "severity": "major",
                "file": "app.py",
                "line": 2,
                "description": "value is wrong",
                "suggestion": None,
            }
        ],
    }
)
DIFF = b"diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x\n+y\n"

FULL_INPUTS = {
    "planner": StepInputs(previous_plan=PLAN, denial_reason="Too broad.", answers="Use JSON."),
    "worker": StepInputs(
        plan=PLAN,
        gate_outputs={"tests": "1 failed\nFAILED test_app.py::test_value\n", "lint": "ok\n"},
        review=REVIEW,
        work_report=REPORT,
        denial_reason="Do not merge yet.",
    ),
    "reviewer": StepInputs(
        plan=PLAN,
        diff=DIFF,
        gate_outputs={"tests": "1 passed\n"},
        work_report=REPORT,
        out_of_plan=("notes.txt",),
    ),
}
MINIMAL_INPUTS = {
    "planner": StepInputs(),
    "worker": StepInputs(plan=PLAN),
    "reviewer": StepInputs(plan=PLAN, diff=DIFF, work_report=REPORT),
}


@pytest.fixture
def ctx(tmp_path: Path) -> ContextHome:
    return make_context_home(tmp_path)


def _build(ctx: ContextHome, role: str, inputs: StepInputs, folder: str = "01-step-1"):
    return build_step_context(
        home=ctx.home,
        task_file=ctx.task_file,
        role=role,
        worktree=ctx.worktree.path,
        context_dir=ctx.run.path / "context" / folder,
        inputs=inputs,
    )


def _index_entries(built: BuiltContext) -> dict[str, tuple[int, str]]:
    entries = {}
    for line in built.file(INDEX_FILE).text.splitlines():
        match = INDEX_LINE.match(line)
        if match:
            entries[match.group(1)] = (int(match.group(2)), match.group(3))
    return entries


def _provided(built: BuiltContext) -> set[str]:
    return {f.path for f in built.files if f.path not in (INDEX_FILE, BRIEF_FILE)}


@pytest.mark.parametrize("role", ROLES)
def test_the_index_lists_every_provided_file_with_a_description_and_a_line_count(
    ctx: ContextHome, role: str
) -> None:
    built = _build(ctx, role, FULL_INPUTS[role])
    context_dir = ctx.run.path / "context" / "01-step-1"

    entries = _index_entries(built)

    assert set(entries) == {str(context_dir / path) for path in _provided(built)}
    for file in built.files:
        if file.path in (INDEX_FILE, BRIEF_FILE):
            continue
        lines, description = entries[str(context_dir / file.path)]
        assert lines == file.lines == len(file.content.decode().splitlines())
        assert description.strip()


def test_the_index_says_where_its_paths_point(ctx: ContextHome) -> None:
    built = _build(ctx, "planner", StepInputs())

    text = built.file(INDEX_FILE).text
    assert "read_file" in text
    assert str(ctx.run.path / "context" / "01-step-1") in text


@pytest.mark.parametrize(
    ("role", "present", "absent"),
    [
        ("planner", {"procedure.md", "repo_map.md"}, {"rubric.md", "plan.json", "diff.patch"}),
        ("worker", {"procedure.md", "rubric.md", "repo_map.md", "plan.json"}, {"diff.patch"}),
        ("reviewer", {"rubric.md", "plan.json", "diff.patch"}, {"procedure.md"}),
    ],
)
def test_each_role_gets_its_recipe(
    ctx: ContextHome, role: str, present: set[str], absent: set[str]
) -> None:
    provided = _provided(_build(ctx, role, MINIMAL_INPUTS[role]))

    assert present <= provided
    assert not absent & provided
    assert "task.md" in provided


@pytest.mark.parametrize("role", ROLES)
def test_knowledge_is_indexed_and_the_archive_is_not(ctx: ContextHome, role: str) -> None:
    provided = _provided(_build(ctx, role, MINIMAL_INPUTS[role]))

    assert {
        "knowledge/facts.md",
        "knowledge/constraints.md",
        "knowledge/decisions/0001-files-first.md",
    } <= provided
    assert not any(path.startswith("knowledge/archive/") for path in provided)


def test_a_project_without_knowledge_still_builds(ctx: ContextHome) -> None:
    for path in sorted((ctx.home.projects).rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()

    provided = _provided(_build(ctx, "planner", StepInputs()))

    assert not any(path.startswith("knowledge/") for path in provided)


def test_a_worker_iteration_gets_gate_output_findings_previous_report_and_denial(
    ctx: ContextHome,
) -> None:
    built = _build(ctx, "worker", FULL_INPUTS["worker"])

    assert {
        "gates/tests.txt",
        "gates/lint.txt",
        "review.json",
        "work_report.json",
        "denial.md",
    } <= _provided(built)
    assert Review.from_json(built.file("review.json").text) == REVIEW
    assert "FAILED test_app.py::test_value" in built.file("gates/tests.txt").text


def test_a_planner_replan_gets_the_previous_plan_and_the_reason(ctx: ContextHome) -> None:
    built = _build(ctx, "planner", FULL_INPUTS["planner"])

    assert {"previous_plan.json", "denial.md", "answers.md"} <= _provided(built)
    assert "Too broad." in built.file("denial.md").text
    assert "Use JSON." in built.file("answers.md").text


def test_the_reviewer_gets_the_report_described_as_unverified_claims(ctx: ContextHome) -> None:
    built = _build(ctx, "reviewer", FULL_INPUTS["reviewer"])

    description = next(
        d for p, (_, d) in _index_entries(built).items() if p.endswith("work_report.json")
    )
    assert "unverified" in description
    assert built.file("diff.patch").content == DIFF


def test_the_brief_is_a_file_and_names_the_index(ctx: ContextHome) -> None:
    built = _build(ctx, "worker", FULL_INPUTS["worker"])

    assert built.file(BRIEF_FILE).text == built.brief
    assert str(ctx.run.path / "context" / "01-step-1" / INDEX_FILE) in built.brief
    assert "review.json" in built.brief


def test_the_repo_map_comes_from_the_worktree(ctx: ContextHome) -> None:
    built = _build(ctx, "planner", StepInputs())

    repo_map = built.file("repo_map.md").text
    assert "app.py (2 lines): value" in repo_map


@pytest.mark.parametrize(
    ("role", "inputs", "message"),
    [
        ("worker", StepInputs(), "worker needs plan"),
        ("reviewer", StepInputs(plan=PLAN, work_report=REPORT), "reviewer needs diff"),
        ("planner", StepInputs(diff=DIFF), "planner is not given diff"),
        ("reviewer", StepInputs(plan=PLAN, diff=DIFF, work_report=REPORT, review=REVIEW), "review"),
    ],
)
def test_inputs_outside_the_recipe_are_refused(
    ctx: ContextHome, role: str, inputs: StepInputs, message: str
) -> None:
    with pytest.raises(ContextError, match=message):
        _build(ctx, role, inputs)


def test_a_missing_lane_file_is_an_error_that_names_it(ctx: ContextHome) -> None:
    (ctx.home.lanes / "development" / "rubric.md").unlink()

    with pytest.raises(ContextError, match="rubric.md"):
        _build(ctx, "reviewer", MINIMAL_INPUTS["reviewer"])


def test_an_unknown_role_is_an_error(ctx: ContextHome) -> None:
    with pytest.raises(ContextError, match="unknown role"):
        _build(ctx, "oracle", StepInputs())


def test_every_recipe_input_is_a_field_of_step_inputs() -> None:
    fields = set(StepInputs.__dataclass_fields__)
    for recipe in RECIPES.values():
        assert set(recipe.required) | set(recipe.optional) <= fields
