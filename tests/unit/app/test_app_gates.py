"""Task 13: running the checks of one attempt and writing `gates/<attempt>/` (docs/SPEC.md 5, 11).

Every gate the flow lists runs, even after one fails, so the next attempt sees every problem
at once. Each writes one output file; `results.json` records the outcome. The `scope` check
never fails the run: it adds the out-of-plan files to the reviewer's context.
"""

import json
from pathlib import Path

import pytest

from control_plane.app.gates import GatesExistError, run_checks
from control_plane.context.builder import StepInputs, build_step_context
from control_plane.context.recipes import RECIPES
from control_plane.domain import Plan, WorkReport
from control_plane.domain.gates import GateReport
from control_plane.domain.project import ProjectConfig
from control_plane.store import RunFolder, RunStore, create_home
from control_plane.store.git import Git
from control_plane.workspace import Worktree, create_worktree, diff_bytes
from fixtures.context_home import DEFAULT_TASK, FIXTURE_TASKS
from fixtures.gate_repo import CALC, gate_project, make_gate_project
from fixtures.loop_harness import VALID_PLAN, VALID_WORK_REPORT
from fixtures.project_repo import write

RUN_ID = "R-20261117-170000-ef12"
ALL_GATES = (
    "format",
    "lint",
    "tests",
    "coverage",
    "test_integrity",
    "diff_size",
    "secrets",
)
PLAN = Plan.model_validate({**VALID_PLAN, "files_to_touch": ["calc.py", "tests/"]})


@pytest.fixture
def project(tmp_path: Path) -> Git:
    return make_gate_project(tmp_path / "project")


@pytest.fixture
def run(tmp_path: Path) -> RunFolder:
    home = create_home(tmp_path / "home")
    return RunStore(home).create(RUN_ID)


@pytest.fixture
def tree(project: Git, run: RunFolder) -> Worktree:
    return create_worktree(project.root, run.home.worktrees, RUN_ID, project.head())


@pytest.fixture
def config(tree: Worktree) -> ProjectConfig:
    return gate_project(tree.repo)


def read_report(run: RunFolder, attempt: int) -> GateReport:
    return GateReport.from_json((run.path / "gates" / str(attempt) / "results.json").read_text())


async def test_every_gate_writes_its_output_file_and_the_results(
    run: RunFolder, tree: Worktree, config: ProjectConfig
) -> None:
    write(tree.path, {"calc.py": CALC + "\n\ndef double(a):\n    return add(a, a)\n"})
    write(tree.path, {"tests/test_double.py": TEST_DOUBLE})

    checks = await run_checks(run, attempt=1, tree=tree, project=config, gates=ALL_GATES, plan=PLAN)

    folder = run.path / "gates" / "1"
    assert sorted(p.name for p in folder.iterdir()) == sorted(
        [f"{name}.txt" for name in ALL_GATES] + ["results.json"]
    )
    assert checks.report.outcome == "pass", checks.outputs
    assert [g.name for g in checks.report.gates] == list(ALL_GATES)
    assert all(g.status == "pass" for g in checks.report.gates)
    assert read_report(run, 1) == checks.report
    assert (folder / "tests.txt").read_text() == checks.outputs["tests"]
    assert checks.report.gates[2].output_file == "gates/1/tests.txt"


async def test_one_failing_gate_fails_the_checks_and_the_rest_still_run(
    run: RunFolder, tree: Worktree, config: ProjectConfig
) -> None:
    write(tree.path, {"calc.py": CALC.replace("a - b", "b - a")})

    checks = await run_checks(run, attempt=2, tree=tree, project=config, gates=ALL_GATES, plan=PLAN)

    statuses = {g.name: g.status for g in checks.report.gates}
    assert checks.report.outcome == "fail"
    assert statuses["tests"] == "fail"
    assert statuses["secrets"] == "pass"
    assert checks.report.failed == ["tests"]
    assert set(checks.failed_outputs()) == set(checks.report.failed)
    assert "1 failed" in checks.failed_outputs()["tests"]


async def test_a_gate_not_configured_is_recorded_as_skipped_and_does_not_fail(
    run: RunFolder, tree: Worktree
) -> None:
    bare = ProjectConfig(repo=tree.repo)

    checks = await run_checks(
        run, attempt=1, tree=tree, project=bare, gates=("lint", "secrets"), plan=PLAN
    )

    assert checks.report.outcome == "pass"
    assert {g.name: g.status for g in checks.report.gates} == {
        "lint": "skipped",
        "secrets": "pass",
    }
    assert "not configured" in (run.path / "gates" / "1" / "lint.txt").read_text()


async def test_the_checks_of_an_attempt_are_written_once(
    run: RunFolder, tree: Worktree, config: ProjectConfig
) -> None:
    await run_checks(run, attempt=1, tree=tree, project=config, gates=("secrets",), plan=PLAN)

    with pytest.raises(GatesExistError, match="attempt 1"):
        await run_checks(run, attempt=1, tree=tree, project=config, gates=("secrets",), plan=PLAN)


async def test_an_unknown_gate_is_refused_before_anything_runs_or_is_written(
    run: RunFolder, tree: Worktree, config: ProjectConfig
) -> None:
    with pytest.raises(ValueError, match="unknown gate 'typecheck'"):
        await run_checks(
            run, attempt=1, tree=tree, project=config, gates=("secrets", "typecheck"), plan=PLAN
        )

    assert not (run.path / "gates" / "1").exists()


async def test_an_attempt_is_numbered_from_one(
    run: RunFolder, tree: Worktree, config: ProjectConfig
) -> None:
    with pytest.raises(ValueError, match="attempt must be 1 or more"):
        await run_checks(run, attempt=0, tree=tree, project=config, gates=("secrets",), plan=PLAN)


async def test_scope_adds_out_of_plan_files_to_the_reviewer_context_and_does_not_fail(
    run: RunFolder, tree: Worktree, config: ProjectConfig, tmp_path: Path
) -> None:
    write(tree.path, {"notes.txt": "first line\nan unplanned edit\n"})

    checks = await run_checks(
        run, attempt=1, tree=tree, project=config, gates=("diff_size", "secrets"), plan=PLAN
    )

    assert checks.report.outcome == "pass"
    assert checks.report.out_of_plan == ["notes.txt"]
    assert read_report(run, 1).out_of_plan == ["notes.txt"]

    task_file = tmp_path / DEFAULT_TASK
    task_file.write_text((FIXTURE_TASKS / DEFAULT_TASK).read_text())
    reviewer = build_step_context(
        home=run.home,
        task_file=task_file,
        role="reviewer",
        worktree=tree.path,
        context_dir=run.path / "context" / "03-review-1",
        inputs=StepInputs(
            plan=PLAN,
            diff=diff_bytes(tree),
            work_report=WorkReport.model_validate(VALID_WORK_REPORT),
            gate_outputs=checks.outputs,
            out_of_plan=tuple(checks.report.out_of_plan),
        ),
    )
    scope = reviewer.file("scope.md")
    assert "- `notes.txt`" in scope.text
    assert "files_to_touch" in scope.text
    assert "scope.md" in reviewer.file("INDEX.md").text
    assert "scope.md" in reviewer.brief


async def test_a_change_inside_the_plan_leaves_nothing_out_of_plan(
    run: RunFolder, tree: Worktree, config: ProjectConfig
) -> None:
    write(tree.path, {"tests/test_double.py": TEST_DOUBLE})

    checks = await run_checks(
        run, attempt=1, tree=tree, project=config, gates=("secrets",), plan=PLAN
    )

    assert checks.report.out_of_plan == []


def test_only_the_reviewer_is_given_the_scope() -> None:
    assert "out_of_plan" in RECIPES["reviewer"].optional
    assert "out_of_plan" not in RECIPES["worker"].optional
    assert "out_of_plan" not in RECIPES["planner"].optional


def test_the_results_file_is_plain_json() -> None:
    report = GateReport(attempt=1, outcome="pass", gates=[], out_of_plan=["a.py"])

    assert json.loads(report.to_json())["out_of_plan"] == ["a.py"]


TEST_DOUBLE = """from calc import double


def test_double():
    assert double(2) == 4
"""
