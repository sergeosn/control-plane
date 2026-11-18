"""A project repository for the gate tests (docs/SPEC.md section 11), built in a temporary
directory.

Flat layout: `calc.py` at the root, its tests under `tests/`, and a guardrail suite under
`tests/guardrails/`. The suite has 7 tests: 4 test functions, one of them parametrized with
3 cases. The files are clean for `ruff format --check` and `ruff check`, and the suite covers
every line, so every gate passes on the base commit with the configuration below.

`gate_project` gives a `ProjectConfig` whose commands use the tools of the running
interpreter's environment, so the gates run real tools with no network.
"""

import sys
from pathlib import Path

from control_plane.domain.project import (
    CommandGate,
    CoverageGate,
    GateSettings,
    GuardrailGate,
    ProjectConfig,
)
from control_plane.store.git import Git
from fixtures.project_repo import write

BIN = Path(sys.executable).parent
PYTEST = (sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider")
RUFF = str(BIN / "ruff")

CALC = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
TEST_CALC = """import pytest

from calc import add, sub


def test_add():
    assert add(1, 2) == 3


def test_sub():
    assert sub(3, 2) == 1


@pytest.mark.parametrize(("a", "b", "total"), [(0, 0, 0), (1, 1, 2), (2, 3, 5)])
def test_add_cases(a, b, total):
    assert add(a, b) == total


class TestCalc:
    def test_add_is_commutative(self):
        assert add(2, 5) == add(5, 2)
"""
TEST_GUARD = """import pytest

from calc import add


def test_add_rejects_a_string():
    with pytest.raises(TypeError):
        add("a", 1)
"""

FILES = {
    ".gitignore": "__pycache__/\n*.pyc\n",
    "calc.py": CALC,
    "tests/test_calc.py": TEST_CALC,
    "tests/guardrails/test_guard.py": TEST_GUARD,
    "notes.txt": "first line\n",
}
BASE_TEST_COUNT = 7


def make_gate_project(root: Path) -> Git:
    """The gate project at `root` on `main` with one commit."""
    git = Git(root)
    git.init()
    write(git.root, FILES)
    git.add(".")
    git.commit("Initial commit")
    return git


def make_control(project: Git) -> Path:
    """The control checkout next to the project, `<repo>-control`, at the project's head."""
    control = project.root.parent / f"{project.root.name}-control"
    project.run("worktree", "add", "--quiet", "--detach", str(control), project.head())
    return control


def gate_project(repo: Path, *, threshold: float = 100.0, max_lines: int = 50) -> ProjectConfig:
    return ProjectConfig(
        repo=repo,
        gates=GateSettings(
            format=CommandGate(argv=[RUFF, "format", "--check", "--no-cache", "."]),
            lint=CommandGate(argv=[RUFF, "check", "--no-cache", "."]),
            tests=CommandGate(argv=list(PYTEST)),
            coverage=CoverageGate(
                argv=[*PYTEST, "--cov=.", "--cov-report=json:{report}"], threshold=threshold
            ),
            diff_size={"max_changed_lines": max_lines},
            guardrail_regression=GuardrailGate(argv=[*PYTEST, "tests/guardrails"], source="."),
        ),
    )
