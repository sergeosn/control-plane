"""Task 11: the `merge_and_verify` action with undo on red (docs/SPEC.md section 13)."""

import sys
from pathlib import Path

import pytest

from control_plane.store.git import Git
from control_plane.workspace import Worktree, commit, create_worktree, merge_and_verify
from fixtures.project_repo import TEST_ARGV, write

RUN_ID = "R-20261117-120000-ab12"


@pytest.fixture
def tree(project: Git, worktrees: Path) -> Worktree:
    return create_worktree(project.root, worktrees, RUN_ID, project.head())


def worker_change(tree: Worktree, files: dict[str, str]) -> str:
    write(tree.path, files)
    sha = commit(tree, "Worker change")
    assert sha is not None
    return sha


def parents(git: Git, rev: str) -> list[str]:
    return git.run("rev-list", "--parents", "-n", "1", rev).split()[1:]


def merge_in_progress(git: Git) -> bool:
    return (git.root / ".git" / "MERGE_HEAD").exists()


async def test_a_merge_with_green_tests_lands_on_main(
    project: Git, tree: Worktree, tmp_path: Path
) -> None:
    pre = project.head()
    branch_head = worker_change(
        tree,
        {
            "feature.py": "def feature():\n    return 'on'\n",
            "test_feature.py": "from feature import feature\n\n\ndef test_feature():\n"
            "    assert feature() == 'on'\n",
        },
    )
    output = tmp_path / "run" / "merge-tests.txt"

    result = await merge_and_verify(tree, TEST_ARGV, output_file=output)

    assert result.outcome == "ok"
    assert result.reason == "merged"
    assert result.pre_merge_commit == pre
    assert result.merge_commit == project.head() == result.head
    assert parents(project, "HEAD") == [pre, branch_head]
    assert project.current_branch() == "main"
    assert (project.root / "feature.py").exists()
    assert "2 passed" in result.test_output
    assert "2 passed" in output.read_text()


async def test_a_merge_with_red_tests_is_undone_and_main_is_back_at_the_pre_merge_commit(
    project: Git, tree: Worktree, tmp_path: Path
) -> None:
    pre = project.head()
    worker_change(tree, {"app.py": "def value():\n    return 2\n"})
    output = tmp_path / "run" / "merge-tests.txt"

    result = await merge_and_verify(tree, TEST_ARGV, output_file=output)

    assert result.outcome == "error"
    assert result.reason == "tests_failed"
    assert result.merge_commit is not None
    assert result.merge_commit != pre
    assert project.head() == pre == result.head
    assert project.current_branch() == "main"
    assert project.status() == []
    assert (project.root / "app.py").read_text() == "def value():\n    return 1\n"
    assert "1 failed" in result.test_output
    assert "1 failed" in output.read_text()


async def test_a_conflicting_merge_fails_cleanly_and_leaves_main_untouched(
    project: Git, tree: Worktree
) -> None:
    worker_change(tree, {"notes.txt": "the worker's line\n"})
    write(project.root, {"notes.txt": "someone else's line\n"})
    project.add(".")
    pre = project.commit("Concurrent change on main")

    result = await merge_and_verify(tree, TEST_ARGV)

    assert result.outcome == "error"
    assert result.reason == "conflict"
    assert "notes.txt" in result.message
    assert result.merge_commit is None
    assert result.test_output == ""
    assert project.head() == pre == result.head
    assert project.status() == []
    assert not merge_in_progress(project)
    assert (project.root / "notes.txt").read_text() == "someone else's line\n"


async def test_uncommitted_worktree_changes_refuse_the_merge(project: Git, tree: Worktree) -> None:
    pre = project.head()
    write(tree.path, {"app.py": "def value():\n    return 1  # not committed\n"})

    result = await merge_and_verify(tree, TEST_ARGV)

    assert (result.outcome, result.reason) == ("error", "uncommitted")
    assert project.head() == pre


async def test_a_branch_with_nothing_new_refuses_the_merge(project: Git, tree: Worktree) -> None:
    pre = project.head()

    result = await merge_and_verify(tree, TEST_ARGV)

    assert (result.outcome, result.reason) == ("error", "nothing_to_merge")
    assert project.head() == pre


async def test_local_changes_on_main_refuse_the_merge(project: Git, tree: Worktree) -> None:
    worker_change(tree, {"feature.py": "x = 1\n"})
    pre = project.head()
    write(project.root, {"notes.txt": "edited by hand on main\n"})

    result = await merge_and_verify(tree, TEST_ARGV)

    assert (result.outcome, result.reason) == ("error", "dirty_main")
    assert project.head() == pre
    assert (project.root / "notes.txt").read_text() == "edited by hand on main\n"


async def test_a_primary_checkout_not_on_main_refuses_the_merge(
    project: Git, tree: Worktree
) -> None:
    worker_change(tree, {"feature.py": "x = 1\n"})
    project.run("switch", "--quiet", "--create", "elsewhere")

    result = await merge_and_verify(tree, TEST_ARGV)

    assert (result.outcome, result.reason) == ("error", "not_on_main")
    assert project.current_branch() == "elsewhere"


async def test_a_merge_blocked_by_an_untracked_file_on_main_fails_cleanly(
    project: Git, tree: Worktree
) -> None:
    worker_change(tree, {"feature.py": "x = 1\n"})
    pre = project.head()
    write(project.root, {"feature.py": "untracked on main\n"})

    result = await merge_and_verify(tree, TEST_ARGV)

    assert (result.outcome, result.reason) == ("error", "merge_failed")
    assert project.head() == pre
    assert not merge_in_progress(project)
    assert (project.root / "feature.py").read_text() == "untracked on main\n"


async def test_a_test_command_that_cannot_start_counts_as_red(project: Git, tree: Worktree) -> None:
    pre = project.head()
    worker_change(tree, {"feature.py": "x = 1\n"})

    result = await merge_and_verify(tree, ["no-such-test-runner-xyz"])

    assert (result.outcome, result.reason) == ("error", "tests_failed")
    assert "command not found" in result.test_output
    assert project.head() == pre


async def test_a_test_run_that_times_out_counts_as_red(project: Git, tree: Worktree) -> None:
    pre = project.head()
    worker_change(tree, {"feature.py": "x = 1\n"})

    result = await merge_and_verify(
        tree, [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5
    )

    assert (result.outcome, result.reason) == ("error", "tests_failed")
    assert "timed out" in result.message
    assert project.head() == pre
