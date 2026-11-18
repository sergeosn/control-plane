"""Task 13: the `test_integrity` gate (docs/SPEC.md section 11).

It compares the test files of the worktree with those of the base commit, without running
anything: a test that existed at the base and is gone fails it, so does a fall in the number
of tests (parametrized cases counted), and so does a new skip or xfail marker.
"""

from pathlib import Path

import pytest

from control_plane.gates import GateContext, run_gate
from control_plane.gates.integrity import analyse_source
from control_plane.store.git import Git
from control_plane.workspace import Worktree, commit, create_worktree
from fixtures.gate_repo import BASE_TEST_COUNT, TEST_CALC, gate_project, make_gate_project
from fixtures.project_repo import write

RUN_ID = "R-20261117-140000-ef56"
CALC_TESTS = "tests/test_calc.py"


@pytest.fixture
def tree(tmp_path: Path) -> Worktree:
    project: Git = make_gate_project(tmp_path / "project")
    return create_worktree(project.root, tmp_path / "home" / ".worktrees", RUN_ID, project.head())


async def integrity(tree: Worktree):
    return await run_gate("test_integrity", GateContext(tree=tree, project=gate_project(tree.repo)))


# --- analysis of one file -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "tests"),
    [
        ("def test_a():\n    pass\n", {"test_a": 1}),
        ("async def test_a():\n    pass\n", {"test_a": 1}),
        ("def helper():\n    pass\n", {}),
        (
            "import pytest\n\n@pytest.mark.parametrize('x', [1, 2, 3])\ndef test_a(x):\n    pass\n",
            {"test_a": 3},
        ),
        (
            "import pytest\n\n@pytest.mark.parametrize('x', [1, 2])\n"
            "@pytest.mark.parametrize('y', (1, 2, 3))\ndef test_a(x, y):\n    pass\n",
            {"test_a": 6},
        ),
        (
            "import pytest\n\n@pytest.mark.parametrize(argnames='x', argvalues=[1, 2])\n"
            "def test_a(x):\n    pass\n",
            {"test_a": 2},
        ),
        (
            "import pytest\n\nCASES = [1, 2]\n\n@pytest.mark.parametrize('x', CASES)\n"
            "def test_a(x):\n    pass\n",
            {"test_a": 1},
        ),
        (
            "class TestThing:\n    def test_a(self):\n        pass\n\n"
            "    def helper(self):\n        pass\n\n"
            "    class TestInner:\n        def test_b(self):\n            pass\n",
            {"TestThing::test_a": 1, "TestThing::TestInner::test_b": 1},
        ),
        ("class Helper:\n    def test_a(self):\n        pass\n", {}),
        (
            "import pytest\n\n@pytest.mark.parametrize('x', [1, 2])\nclass TestThing:\n"
            "    def test_a(self, x):\n        pass\n",
            {"TestThing::test_a": 2},
        ),
    ],
)
def test_analysis_finds_each_test_with_its_case_count(source: str, tests: dict[str, int]) -> None:
    assert analyse_source(source).tests == tests


@pytest.mark.parametrize(
    ("source", "markers"),
    [
        ("import pytest\n\n@pytest.mark.skip\ndef test_a():\n    pass\n", 1),
        ("import pytest\n\n@pytest.mark.skip(reason='x')\ndef test_a():\n    pass\n", 1),
        ("import pytest\n\n@pytest.mark.skipif(True, reason='x')\ndef test_a():\n    pass\n", 1),
        ("import pytest\n\n@pytest.mark.xfail\ndef test_a():\n    pass\n", 1),
        ("import pytest\n\ndef test_a():\n    pytest.skip('later')\n", 1),
        ("import pytest\n\ndef test_a():\n    pytest.xfail('later')\n", 1),
        ("import pytest\n\nnp = pytest.importorskip('numpy')\n", 1),
        ("import pytest\n\npytestmark = pytest.mark.skip\n", 1),
        ("import pytest as pt\n\n@pt.mark.xfail(strict=False)\ndef test_a():\n    pass\n", 1),
        ("from pytest import mark\n\n@mark.skip\ndef test_a():\n    pass\n", 1),
        ("import unittest\n\n@unittest.skip('x')\ndef test_a():\n    pass\n", 1),
        ("import unittest\n\n@unittest.expectedFailure\ndef test_a():\n    pass\n", 1),
        ("from unittest import skipIf\n\n@skipIf(True, 'x')\ndef test_a():\n    pass\n", 1),
        ("from pytest import skip\n\ndef test_a():\n    skip('x')\n", 1),
        (
            "import pytest\n\n@pytest.mark.skip\n@pytest.mark.xfail\ndef test_a():\n    pass\n",
            2,
        ),
        ("import pytest\n\n@pytest.mark.parametrize('x', [1])\ndef test_a(x):\n    pass\n", 0),
        ("skip = 3\n\ndef test_a():\n    assert skip == 3\n", 0),
        ("def test_a():\n    make().skip()\n", 0),
        ("def test_a():\n    pass\n", 0),
    ],
)
def test_analysis_counts_skip_and_xfail_markers(source: str, markers: int) -> None:
    assert analyse_source(source).markers == markers


def test_analysis_of_the_fixture_suite_counts_every_case() -> None:
    summary = analyse_source(TEST_CALC)

    assert sum(summary.tests.values()) == BASE_TEST_COUNT - 1
    assert summary.markers == 0


# --- the gate on the fixture repository ----------------------------------------------------


async def test_integrity_passes_when_tests_are_unchanged_or_added(tree: Worktree) -> None:
    write(tree.path, {"tests/test_more.py": "def test_more():\n    assert True\n"})

    result = await integrity(tree)

    assert result.status == "pass", result.output
    assert f"{BASE_TEST_COUNT} -> {BASE_TEST_COUNT + 1}" in result.summary


async def test_integrity_catches_a_deleted_test(tree: Worktree) -> None:
    source = TEST_CALC.replace("def test_sub():\n    assert sub(3, 2) == 1\n\n\n", "")
    write(tree.path, {CALC_TESTS: source})

    result = await integrity(tree)

    assert result.status == "fail"
    assert "tests/test_calc.py::test_sub" in result.output
    assert "deleted" in result.output


async def test_integrity_catches_a_deleted_test_file(tree: Worktree) -> None:
    (tree.path / "tests" / "guardrails" / "test_guard.py").unlink()

    result = await integrity(tree)

    assert result.status == "fail"
    assert "tests/guardrails/test_guard.py::test_add_rejects_a_string" in result.output


async def test_integrity_catches_a_deleted_test_that_was_committed(tree: Worktree) -> None:
    (tree.path / "tests" / "guardrails" / "test_guard.py").unlink()
    commit(tree, "Remove the guard")

    result = await integrity(tree)

    assert result.status == "fail"
    assert "test_add_rejects_a_string" in result.output


async def test_integrity_catches_a_reduced_test_count(tree: Worktree) -> None:
    write(tree.path, {CALC_TESTS: TEST_CALC.replace(", (2, 3, 5)]", "]")})

    result = await integrity(tree)

    assert result.status == "fail"
    assert f"{BASE_TEST_COUNT} -> {BASE_TEST_COUNT - 1}" in result.output
    assert "deleted" not in result.output


async def test_integrity_catches_a_new_skip_marker(tree: Worktree) -> None:
    source = TEST_CALC.replace("def test_add():", "@pytest.mark.skip\ndef test_add():")
    write(tree.path, {CALC_TESTS: source})

    result = await integrity(tree)

    assert result.status == "fail"
    assert "tests/test_calc.py" in result.output
    assert "skip or xfail" in result.output


async def test_integrity_catches_a_new_xfail_marker_in_a_new_conftest(tree: Worktree) -> None:
    conftest = "import pytest\n\npytestmark = pytest.mark.xfail\n"
    write(tree.path, {"tests/conftest.py": conftest})

    result = await integrity(tree)

    assert result.status == "fail"
    assert "tests/conftest.py" in result.output


async def test_integrity_fails_on_a_test_file_it_cannot_parse(tree: Worktree) -> None:
    write(tree.path, {CALC_TESTS: TEST_CALC + "\ndef broken(:\n"})

    result = await integrity(tree)

    assert result.status == "fail"
    assert "cannot parse tests/test_calc.py" in result.output


async def test_integrity_ignores_files_that_are_not_tests(tree: Worktree) -> None:
    write(tree.path, {"calc.py": "import pytest\n\npytest.skip('x')\n"})

    result = await integrity(tree)

    assert result.status == "pass", result.output


async def test_integrity_treats_a_test_file_broken_at_the_base_as_holding_no_tests(
    tmp_path: Path,
) -> None:
    project = make_gate_project(tmp_path / "broken")
    write(project.root, {"tests/test_broken.py": "def test_x(:\n"})
    project.add(".")
    project.commit("Broken test file")
    tree = create_worktree(project.root, tmp_path / "wt", "R-broken", project.head())
    write(tree.path, {"tests/test_broken.py": "def test_x():\n    assert True\n"})

    result = await integrity(tree)

    assert result.status == "pass", result.output
    assert f"{BASE_TEST_COUNT} -> {BASE_TEST_COUNT + 1}" in result.summary
