"""Task 11: worktree creation and removal, branch naming, commit (docs/SPEC.md sections 5, 13)."""

import shutil
from pathlib import Path

import pytest

from control_plane.store.git import Git
from control_plane.workspace import (
    WorkspaceError,
    branch_name,
    commit,
    create_worktree,
    open_worktree,
    remove_worktree,
)
from fixtures.project_repo import write

RUN_ID = "R-20261117-120000-ab12"


def test_branch_name_is_run_slash_run_id() -> None:
    assert branch_name(RUN_ID) == f"run/{RUN_ID}"


@pytest.mark.parametrize("bad", ["", "../escape", "a/b", "has space", ".hidden", "x..y", "x.lock"])
def test_branch_name_rejects_a_run_id_that_is_not_a_safe_name(bad: str) -> None:
    with pytest.raises(WorkspaceError, match="run id"):
        branch_name(bad)


def test_worktree_is_created_from_the_recorded_base_commit_on_branch_run_id(
    project: Git, worktrees: Path
) -> None:
    base = project.head()
    write(project.root, {"later.txt": "main moved on\n"})
    project.add(".")
    project.commit("Later commit on main")

    tree = create_worktree(project.root, worktrees, RUN_ID, base)

    assert tree.path == worktrees / RUN_ID
    assert tree.branch == f"run/{RUN_ID}"
    assert tree.base_commit == base
    inside = Git(tree.path)
    assert inside.head() == base
    assert inside.current_branch() == f"run/{RUN_ID}"
    assert not (tree.path / "later.txt").exists()
    assert project.current_branch() == "main"


def test_a_base_given_as_a_ref_is_recorded_as_its_full_sha(project: Git, worktrees: Path) -> None:
    tree = create_worktree(project.root, worktrees, RUN_ID, "main")

    assert tree.base_commit == project.head()


def test_an_unknown_base_commit_fails_and_creates_nothing(project: Git, worktrees: Path) -> None:
    with pytest.raises(WorkspaceError, match="base commit"):
        create_worktree(project.root, worktrees, RUN_ID, "0" * 40)

    assert not (worktrees / RUN_ID).exists()


def test_a_second_worktree_for_the_same_run_fails(project: Git, worktrees: Path) -> None:
    create_worktree(project.root, worktrees, RUN_ID, project.head())

    with pytest.raises(WorkspaceError, match="already"):
        create_worktree(project.root, worktrees, RUN_ID, project.head())


def test_open_worktree_finds_an_existing_worktree(project: Git, worktrees: Path) -> None:
    created = create_worktree(project.root, worktrees, RUN_ID, project.head())

    assert open_worktree(project.root, worktrees, RUN_ID, created.base_commit) == created


def test_open_worktree_fails_when_there_is_none(project: Git, worktrees: Path) -> None:
    with pytest.raises(WorkspaceError, match="no worktree"):
        open_worktree(project.root, worktrees, RUN_ID, project.head())


def test_remove_deletes_the_worktree_and_keeps_the_branch(project: Git, worktrees: Path) -> None:
    tree = create_worktree(project.root, worktrees, RUN_ID, project.head())
    write(tree.path, {"scratch.txt": "uncommitted\n"})

    remove_worktree(tree)

    assert not tree.path.exists()
    assert f"run/{RUN_ID}" in project.run("branch", "--list", "run/*")
    assert str(tree.path) not in project.run("worktree", "list")


def test_remove_can_delete_the_branch_too(project: Git, worktrees: Path) -> None:
    tree = create_worktree(project.root, worktrees, RUN_ID, project.head())

    remove_worktree(tree, delete_branch=True)

    assert project.run("branch", "--list", "run/*").strip() == ""


def test_remove_of_a_worktree_whose_folder_is_gone_still_unregisters_it(
    project: Git, worktrees: Path
) -> None:
    tree = create_worktree(project.root, worktrees, RUN_ID, project.head())
    shutil.rmtree(tree.path)

    remove_worktree(tree, delete_branch=True)

    assert str(tree.path) not in project.run("worktree", "list")


def test_commit_records_every_change_on_the_run_branch(project: Git, worktrees: Path) -> None:
    tree = create_worktree(project.root, worktrees, RUN_ID, project.head())
    write(tree.path, {"app.py": "def value():\n    return 1  # same\n", "new/mod.py": "x = 1\n"})
    (tree.path / "notes.txt").unlink()

    sha = commit(tree, "Worker changes")

    inside = Git(tree.path)
    assert sha == inside.head()
    assert inside.status() == []
    assert inside.log_subjects()[0] == "Worker changes"
    assert project.head() == tree.base_commit


def test_commit_with_nothing_changed_returns_none(project: Git, worktrees: Path) -> None:
    tree = create_worktree(project.root, worktrees, RUN_ID, project.head())

    assert commit(tree, "Nothing") is None
    assert Git(tree.path).head() == tree.base_commit


def test_engine_commits_do_not_run_the_projects_hooks(project: Git, worktrees: Path) -> None:
    hook = project.root / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    tree = create_worktree(project.root, worktrees, RUN_ID, project.head())
    write(tree.path, {"app.py": "def value():\n    return 1  # hooked\n"})

    assert commit(tree, "Despite the hook") is not None
