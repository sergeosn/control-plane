"""Task 13: the command gates on a fixture repository (docs/SPEC.md section 11).

`format`, `lint` and `tests` run a command from `project.yaml` in the run worktree and fail on
a non-zero exit. `coverage` reads the coverage report and fails under the project threshold.
`guardrail_regression` runs the control copy's guardrail suite with the worktree's source
first on the import path.
"""

import shutil
import sys
from pathlib import Path

import pytest

from control_plane.domain.project import CommandGate, GateSettings, ProjectConfig
from control_plane.gates import GateContext, run_gate
from control_plane.store.git import Git
from control_plane.workspace import Worktree, create_worktree
from fixtures.gate_repo import CALC, TEST_GUARD, gate_project, make_control, make_gate_project
from fixtures.project_repo import write

RUN_ID = "R-20261117-130000-cd34"


@pytest.fixture
def project(tmp_path: Path) -> Git:
    return make_gate_project(tmp_path / "project")


@pytest.fixture
def tree(project: Git, tmp_path: Path) -> Worktree:
    return create_worktree(project.root, tmp_path / "home" / ".worktrees", RUN_ID, project.head())


def ctx(tree: Worktree, **settings: object) -> GateContext:
    return GateContext(tree=tree, project=gate_project(tree.repo, **settings))


# --- format -------------------------------------------------------------------------------


async def test_format_passes_on_formatted_code(tree: Worktree) -> None:
    result = await run_gate("format", ctx(tree))

    assert result.status == "pass", result.output


async def test_format_fails_when_the_formatter_reports_a_problem(tree: Worktree) -> None:
    write(tree.path, {"ugly.py": "x = {  'a':1}\n"})

    result = await run_gate("format", ctx(tree))

    assert result.status == "fail"
    assert "ugly.py" in result.output


# --- lint ---------------------------------------------------------------------------------


async def test_lint_passes_on_clean_code(tree: Worktree) -> None:
    result = await run_gate("lint", ctx(tree))

    assert result.status == "pass", result.output


async def test_lint_fails_when_the_linter_reports_a_problem(tree: Worktree) -> None:
    write(tree.path, {"calc.py": "import os\n\n" + CALC})

    result = await run_gate("lint", ctx(tree))

    assert result.status == "fail"
    assert "F401" in result.output


# --- tests --------------------------------------------------------------------------------


async def test_tests_pass_when_every_test_passes(tree: Worktree) -> None:
    result = await run_gate("tests", ctx(tree))

    assert result.status == "pass", result.output
    assert "7 passed" in result.output


async def test_tests_fail_when_any_test_fails(tree: Worktree) -> None:
    write(tree.path, {"calc.py": CALC.replace("a - b", "b - a")})

    result = await run_gate("tests", ctx(tree))

    assert result.status == "fail"
    assert "1 failed" in result.output
    assert "exit code 1" in result.summary


async def test_a_command_gate_runs_in_the_worktree_without_the_engine_environment(
    tree: Worktree, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-for-subprocesses")
    probe = "import os; print(os.getcwd()); print(os.environ.get('ANTHROPIC_API_KEY'))"
    project = ProjectConfig(
        repo=tree.repo,
        gates=GateSettings(tests=CommandGate(argv=[sys.executable, "-c", probe])),
    )

    result = await run_gate("tests", GateContext(tree=tree, project=project))

    assert result.status == "pass"
    assert str(tree.path.resolve()) in result.output
    assert "sk-ant-not-for-subprocesses" not in result.output


async def test_a_command_that_times_out_fails_the_gate(tree: Worktree) -> None:
    sleeper = CommandGate(argv=[sys.executable, "-c", "import time; time.sleep(30)"], timeout_s=0.5)
    project = ProjectConfig(repo=tree.repo, gates=GateSettings(lint=sleeper))

    result = await run_gate("lint", GateContext(tree=tree, project=project))

    assert result.status == "fail"
    assert "timed out" in result.summary


async def test_a_command_that_cannot_start_fails_the_gate(tree: Worktree) -> None:
    missing = CommandGate(argv=["no-such-linter-on-this-machine"])
    project = ProjectConfig(repo=tree.repo, gates=GateSettings(lint=missing))

    result = await run_gate("lint", GateContext(tree=tree, project=project))

    assert result.status == "fail"
    assert "could not start" in result.summary


@pytest.mark.parametrize("name", ["format", "lint", "tests", "coverage", "guardrail_regression"])
async def test_a_command_gate_not_configured_in_project_yaml_is_skipped(
    tree: Worktree, name: str
) -> None:
    project = ProjectConfig(repo=tree.repo)

    result = await run_gate(name, GateContext(tree=tree, project=project))

    assert result.status == "skipped"
    assert "project.yaml" in result.summary


async def test_command_gates_leave_no_bytecode_in_the_worktree(tree: Worktree) -> None:
    await run_gate("tests", ctx(tree))

    assert not list(tree.path.rglob("__pycache__"))


# --- coverage -----------------------------------------------------------------------------


async def test_coverage_passes_at_or_above_the_threshold(tree: Worktree) -> None:
    result = await run_gate("coverage", ctx(tree, threshold=100.0))

    assert result.status == "pass", result.output
    assert "100.00% (threshold 100.00%)" in result.summary


async def test_coverage_fails_under_the_threshold(tree: Worktree) -> None:
    write(tree.path, {"calc.py": CALC + "\n\ndef mul(a, b):\n    return a * b\n"})

    result = await run_gate("coverage", ctx(tree, threshold=100.0))

    assert result.status == "fail"
    assert "under the threshold" in result.summary


async def test_coverage_fails_when_the_command_writes_no_report(tree: Worktree) -> None:
    project = gate_project(tree.repo)
    assert project.gates.coverage is not None
    project.gates.coverage.argv = [sys.executable, "-c", "pass", "{report}"]

    result = await run_gate("coverage", GateContext(tree=tree, project=project))

    assert result.status == "fail"
    assert "no coverage report" in result.summary


async def test_coverage_fails_when_the_report_is_unreadable(tree: Worktree) -> None:
    project = gate_project(tree.repo)
    assert project.gates.coverage is not None
    writer = "import sys, pathlib; pathlib.Path(sys.argv[1]).write_text('{\"totals\": 3}')"
    project.gates.coverage.argv = [sys.executable, "-c", writer, "{report}"]

    result = await run_gate("coverage", GateContext(tree=tree, project=project))

    assert result.status == "fail"
    assert "unreadable" in result.summary


async def test_coverage_leaves_no_data_file_in_the_worktree(tree: Worktree) -> None:
    await run_gate("coverage", ctx(tree))

    assert not (tree.path / ".coverage").exists()


# --- guardrail_regression ------------------------------------------------------------------


async def test_guardrail_regression_passes_when_the_control_suite_passes_on_the_worktree(
    project: Git, tree: Worktree
) -> None:
    make_control(project)
    write(tree.path, {"notes.txt": "first line\nsecond line\n"})

    result = await run_gate("guardrail_regression", ctx(tree))

    assert result.status == "pass", result.output
    assert "1 passed" in result.output


async def test_guardrail_regression_runs_the_control_suite_against_the_worktree_source(
    project: Git, tree: Worktree
) -> None:
    """The worker weakens `add` and edits its own copy of the guardrail test to match.

    Its own suite is green; the control copy's untouched guardrail test, run against the
    worktree's `calc.py`, is not.
    """
    make_control(project)
    weakened = (
        "def add(a, b):\n"
        "    if isinstance(a, str):\n"
        "        return a + str(b)\n"
        "    return a + b\n\n\n"
    )
    write(
        tree.path,
        {
            "calc.py": weakened + CALC.split("\n\n\n", 1)[1],
            "tests/guardrails/test_guard.py": TEST_GUARD.replace(
                '    with pytest.raises(TypeError):\n        add("a", 1)\n',
                '    assert add("a", 1) == "a1"\n',
            ),
        },
    )

    own_suite = await run_gate("tests", ctx(tree))
    guardrail = await run_gate("guardrail_regression", ctx(tree))

    assert own_suite.status == "pass", own_suite.output
    assert guardrail.status == "fail"
    assert "test_add_rejects_a_string" in guardrail.output
    assert "DID NOT RAISE" in guardrail.output


async def test_guardrail_regression_uses_the_control_copy_even_if_the_worktree_deletes_it(
    project: Git, tree: Worktree
) -> None:
    make_control(project)
    shutil.rmtree(tree.path / "tests" / "guardrails")

    result = await run_gate("guardrail_regression", ctx(tree))

    assert result.status == "pass", result.output
    assert "1 passed" in result.output


async def test_guardrail_regression_fails_without_a_control_checkout(tree: Worktree) -> None:
    result = await run_gate("guardrail_regression", ctx(tree))

    assert result.status == "fail"
    assert "control checkout" in result.summary


async def test_guardrail_regression_fails_when_the_source_folder_is_missing(
    project: Git, tree: Worktree
) -> None:
    make_control(project)
    config = gate_project(tree.repo)
    assert config.gates.guardrail_regression is not None
    config.gates.guardrail_regression.source = "src"

    result = await run_gate("guardrail_regression", GateContext(tree=tree, project=config))

    assert result.status == "fail"
    assert "src" in result.summary


async def test_an_unknown_gate_is_refused(tree: Worktree) -> None:
    with pytest.raises(ValueError, match="unknown gate 'typecheck'"):
        await run_gate("typecheck", ctx(tree))
