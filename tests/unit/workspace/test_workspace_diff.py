"""Task 11: `diff.patch` matches the changes made in the worktree (SPEC.md section 9)."""

from pathlib import Path

import pytest

from control_plane.store.git import Git
from control_plane.workspace import Worktree, commit, create_worktree, diff_patch, export_diff
from fixtures.project_repo import write

RUN_ID = "R-20261117-120000-ab12"


def snapshot(root: Path) -> dict[str, bytes]:
    """Every file under `root` that git would track, by relative path."""
    git = Git(root)
    listed = git.run("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return {
        name: (root / name).read_bytes()
        for name in listed.split("\0")
        if name and (root / name).is_file()
    }


@pytest.fixture
def tree(project: Git, worktrees: Path) -> Worktree:
    return create_worktree(project.root, worktrees, RUN_ID, project.head())


def apply_to_base(project: Git, base: str, patch: Path, where: Path) -> Path:
    """Check the base out in a fresh worktree and apply `patch` to it."""
    project.run("worktree", "add", "--detach", str(where), base)
    Git(where).run("apply", "--binary", str(patch))
    return where


def test_diff_patch_matches_the_changes_made_in_the_worktree(
    project: Git, tree: Worktree, tmp_path: Path
) -> None:
    # A committed change and uncommitted ones of every kind: edit, add, nested add, delete,
    # binary. Ignored files are not changes.
    write(tree.path, {"app.py": "def value():\n    return 1  # committed edit\n"})
    commit(tree, "Committed part")
    write(tree.path, {"test_app.py": "# edited\n", "pkg/new.py": "x = 1\n", "added.txt": "a\n"})
    (tree.path / "notes.txt").unlink()
    (tree.path / "blob.bin").write_bytes(bytes(range(256)))
    (tree.path / "latin1.txt").write_bytes("caf\u00e9\n".encode("latin-1"))
    write(tree.path, {"__pycache__/app.cpython-312.pyc": "ignored\n"})

    target = tmp_path / "run" / "diff.patch"
    export_diff(tree, target)

    applied = apply_to_base(project, tree.base_commit, target, tmp_path / "check")
    assert snapshot(applied) == snapshot(tree.path)
    assert "__pycache__" not in target.read_text(errors="replace")


def test_export_does_not_change_the_worktrees_index_or_files(
    tree: Worktree, tmp_path: Path
) -> None:
    write(tree.path, {"added.txt": "a\n", "app.py": "def value():\n    return 2\n"})
    inside = Git(tree.path)
    status_before = inside.status()
    staged_before = inside.run("diff", "--cached", "--name-only")

    export_diff(tree, tmp_path / "diff.patch")

    assert inside.status() == status_before
    assert inside.run("diff", "--cached", "--name-only") == staged_before


def test_the_diff_of_an_untouched_worktree_is_empty(tree: Worktree, tmp_path: Path) -> None:
    target = tmp_path / "diff.patch"

    export_diff(tree, target)

    assert target.read_text() == ""
    assert diff_patch(tree) == ""


def test_user_diff_settings_do_not_change_the_patch_format(
    project: Git, tree: Worktree, tmp_path: Path
) -> None:
    project.run("config", "diff.noprefix", "true")
    project.run("config", "color.ui", "always")
    write(tree.path, {"app.py": "def value():\n    return 3\n"})

    patch = diff_patch(tree)

    assert "--- a/app.py" in patch
    assert "+++ b/app.py" in patch
    assert "\x1b[" not in patch
