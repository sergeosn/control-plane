"""Task 3: the thin git wrapper the home repository and the run store use."""

from pathlib import Path

import pytest

from control_plane.store.git import Git, GitError


@pytest.fixture
def repo(tmp_path: Path) -> Git:
    git = Git(tmp_path / "repo")
    git.init()
    return git


def test_init_creates_a_repository_on_branch_main(repo: Git) -> None:
    assert (repo.root / ".git").is_dir()
    assert repo.current_branch() == "main"


def test_commit_returns_the_sha_of_the_new_head(repo: Git) -> None:
    (repo.root / "a.txt").write_text("a\n")
    repo.add("a.txt")

    sha = repo.commit("first")

    assert sha == repo.head()
    assert len(sha) == 40
    assert repo.log_subjects() == ["first"]


def test_commits_are_authored_by_the_engine_not_the_user(repo: Git) -> None:
    (repo.root / "a.txt").write_text("a\n")
    repo.add("a.txt")
    repo.commit("first")

    assert repo.head_author() == "Control Plane <cplane@localhost>"


def test_status_reports_modified_deleted_and_untracked_paths(repo: Git) -> None:
    for name in ("keep.txt", "edit.txt", "gone.txt"):
        (repo.root / name).write_text("x\n")
    repo.add(".")
    repo.commit("first")
    (repo.root / "edit.txt").write_text("y\n")
    (repo.root / "gone.txt").unlink()
    (repo.root / "new.txt").write_text("z\n")

    assert sorted(repo.status()) == [
        ("added", "new.txt"),
        ("deleted", "gone.txt"),
        ("modified", "edit.txt"),
    ]
    assert repo.status("keep.txt") == []


def test_is_ignored_honours_the_gitignore(repo: Git) -> None:
    (repo.root / ".gitignore").write_text(".index/\n")
    (repo.root / ".index").mkdir()
    (repo.root / ".index" / "state.db").write_text("")

    assert repo.is_ignored(".index/state.db")
    assert not repo.is_ignored("plane.yaml")


def test_tracked_lists_committed_files_under_a_path(repo: Git) -> None:
    (repo.root / "runs").mkdir()
    (repo.root / "runs" / "a.json").write_text("{}\n")
    repo.add("runs")
    repo.commit("first")
    (repo.root / "runs" / "b.json").write_text("{}\n")

    assert repo.tracked("runs") == ["runs/a.json"]


def test_a_failing_git_command_raises_git_error_with_its_output(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(GitError, match="not a git repository"):
        Git(plain).head()


def test_is_ignored_outside_a_repository_raises_git_error(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(GitError, match="check-ignore"):
        Git(plain).is_ignored("x")
