"""Task 13: the changed files and the added lines of a run worktree (docs/SPEC.md section 11).

The gates and the scope check read the same changes `diff.patch` holds: everything in the
worktree against the base commit, untracked files included and ignored files excluded.
"""

from pathlib import Path

import pytest

from control_plane.store.git import Git
from control_plane.workspace import (
    AddedLine,
    FileChange,
    Worktree,
    added_lines,
    changed_files,
    commit,
    create_worktree,
)
from fixtures.project_repo import write

RUN_ID = "R-20261117-160000-cd90"


@pytest.fixture
def tree(project: Git, worktrees: Path) -> Worktree:
    return create_worktree(project.root, worktrees, RUN_ID, project.head())


def test_an_unchanged_worktree_has_no_changes(tree: Worktree) -> None:
    assert changed_files(tree) == []
    assert added_lines(tree) == []


def test_changed_files_counts_lines_per_file_across_commits_and_the_working_tree(
    tree: Worktree,
) -> None:
    write(tree.path, {"app.py": "def value():\n    return 2\n"})
    commit(tree, "Change value")
    write(tree.path, {"new.txt": "a\nb\n", "__pycache__/x.pyc": "ignored"})
    (tree.path / "notes.txt").unlink()

    changes = {change.path: change for change in changed_files(tree)}

    assert changes == {
        "app.py": FileChange("app.py", None, added=1, deleted=1, binary=False),
        "new.txt": FileChange("new.txt", None, added=2, deleted=0, binary=False),
        "notes.txt": FileChange("notes.txt", None, added=0, deleted=1, binary=False),
    }
    assert changes["app.py"].changed_lines == 2


def test_a_renamed_file_keeps_its_old_path(tree: Worktree) -> None:
    (tree.path / "test_app.py").rename(tree.path / "test_renamed.py")

    assert changed_files(tree) == [
        FileChange("test_renamed.py", "test_app.py", added=0, deleted=0, binary=False)
    ]


def test_a_binary_file_is_marked_binary_with_no_line_counts(tree: Worktree) -> None:
    (tree.path / "logo.bin").write_bytes(b"\x00\x01\x02\xff")

    assert changed_files(tree) == [FileChange("logo.bin", None, 0, 0, binary=True)]


def test_a_path_with_spaces_and_unicode_is_reported_as_is(tree: Worktree) -> None:
    write(tree.path, {"docs/read me ü.txt": "hello\n"})

    assert [c.path for c in changed_files(tree)] == ["docs/read me ü.txt"]
    assert added_lines(tree) == [AddedLine("docs/read me ü.txt", 1, "hello")]


def test_added_lines_gives_each_new_line_with_its_number_in_the_new_file(tree: Worktree) -> None:
    write(tree.path, {"notes.txt": "first line\nsecond\n+++ third\n--- fourth\n"})
    write(tree.path, {"extra.txt": "only\n"})

    assert added_lines(tree) == [
        AddedLine("extra.txt", 1, "only"),
        AddedLine("notes.txt", 2, "second"),
        AddedLine("notes.txt", 3, "+++ third"),
        AddedLine("notes.txt", 4, "--- fourth"),
    ]


def test_added_lines_skips_removed_lines_and_files_without_a_trailing_newline(
    tree: Worktree,
) -> None:
    write(tree.path, {"app.py": "def value():\n    return 3"})

    assert added_lines(tree) == [AddedLine("app.py", 2, "    return 3")]


def test_computing_changes_stages_nothing_in_the_worktree(tree: Worktree) -> None:
    write(tree.path, {"new.txt": "a\n"})

    changed_files(tree)
    added_lines(tree)

    git = Git(tree.path)
    assert git.run("diff", "--cached", "--name-only") == ""
