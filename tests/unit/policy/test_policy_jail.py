"""Task 6: the path jail (SPEC.md section 10.2).

Every path argument is resolved with symlinks followed and must fall inside the roots.
"""

import os
from pathlib import Path

import pytest

from control_plane.policy import PathDeniedError, PathJail


@pytest.fixture
def tree(tmp_path: Path) -> dict[str, Path]:
    worktree = tmp_path / "worktree"
    context = tmp_path / "run" / "context"
    outside = tmp_path / "outside"
    for folder in (worktree / "src", context, outside):
        folder.mkdir(parents=True)
    (worktree / "src" / "a.py").write_text("print('a')\n")
    (context / "BRIEF.md").write_text("brief\n")
    (outside / "secret.txt").write_text("secret\n")
    return {"worktree": worktree, "context": context, "outside": outside}


@pytest.fixture
def jail(tree: dict[str, Path]) -> PathJail:
    return PathJail(tree["worktree"], read_roots=[tree["context"]])


def test_a_relative_path_resolves_against_the_worktree(jail: PathJail, tree: dict) -> None:
    assert jail.resolve("src/a.py") == (tree["worktree"] / "src" / "a.py").resolve()


def test_a_path_that_does_not_exist_yet_is_allowed_for_writing(jail: PathJail, tree: dict) -> None:
    assert jail.resolve("src/new.py", write=True) == (tree["worktree"] / "src" / "new.py").resolve()


def test_a_relative_path_with_dotdot_is_denied(jail: PathJail) -> None:
    with pytest.raises(PathDeniedError, match=r"\.\."):
        jail.resolve("../outside/secret.txt")


def test_dotdot_is_denied_even_when_it_would_stay_inside(jail: PathJail) -> None:
    with pytest.raises(PathDeniedError, match=r"\.\."):
        jail.resolve("src/../src/a.py")


def test_an_absolute_path_outside_the_roots_is_denied(jail: PathJail, tree: dict) -> None:
    with pytest.raises(PathDeniedError, match="outside"):
        jail.resolve(str(tree["outside"] / "secret.txt"))


def test_an_absolute_path_inside_the_worktree_is_allowed(jail: PathJail, tree: dict) -> None:
    target = tree["worktree"] / "src" / "a.py"
    assert jail.resolve(str(target)) == target.resolve()


def test_a_sibling_directory_with_the_root_as_prefix_is_outside(jail: PathJail, tree: dict) -> None:
    sibling = tree["worktree"].parent / "worktree-evil"
    sibling.mkdir()
    with pytest.raises(PathDeniedError, match="outside"):
        jail.resolve(str(sibling / "x.txt"))


def test_a_symlink_inside_the_worktree_pointing_outside_is_denied(
    jail: PathJail, tree: dict
) -> None:
    os.symlink(tree["outside"] / "secret.txt", tree["worktree"] / "link.txt")
    with pytest.raises(PathDeniedError, match="symlink"):
        jail.resolve("link.txt")


def test_a_symlinked_directory_pointing_outside_is_denied_for_new_files(
    jail: PathJail, tree: dict
) -> None:
    os.symlink(tree["outside"], tree["worktree"] / "escape", target_is_directory=True)
    with pytest.raises(PathDeniedError, match="outside"):
        jail.resolve("escape/new.txt", write=True)


def test_a_symlink_that_stays_inside_the_worktree_is_allowed(jail: PathJail, tree: dict) -> None:
    os.symlink(tree["worktree"] / "src" / "a.py", tree["worktree"] / "alias.py")
    assert jail.resolve("alias.py") == (tree["worktree"] / "src" / "a.py").resolve()


def test_the_context_root_is_readable(jail: PathJail, tree: dict) -> None:
    target = tree["context"] / "BRIEF.md"
    assert jail.resolve(str(target)) == target.resolve()


def test_the_context_root_is_not_writable(jail: PathJail, tree: dict) -> None:
    with pytest.raises(PathDeniedError, match="read-only"):
        jail.resolve(str(tree["context"] / "BRIEF.md"), write=True)


def test_a_read_only_worktree_denies_writes(tree: dict) -> None:
    jail = PathJail(tree["worktree"], writable=False)
    assert jail.resolve("src/a.py")
    with pytest.raises(PathDeniedError, match="read-only"):
        jail.resolve("src/a.py", write=True)


def test_an_empty_path_is_denied(jail: PathJail) -> None:
    with pytest.raises(PathDeniedError, match="empty"):
        jail.resolve("")


def test_the_denial_names_the_roots(jail: PathJail, tree: dict) -> None:
    with pytest.raises(PathDeniedError) as info:
        jail.resolve(str(tree["outside"] / "secret.txt"))
    assert str(tree["worktree"].resolve()) in str(info.value)


def test_display_is_relative_inside_the_worktree_and_absolute_elsewhere(
    jail: PathJail, tree: dict
) -> None:
    inside = jail.resolve("src/a.py")
    context = jail.resolve(str(tree["context"] / "BRIEF.md"))

    assert jail.display(inside) == "src/a.py"
    assert jail.display(context) == str(context)


def test_roots_are_resolved_so_a_symlinked_root_still_contains_its_files(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "f.txt").write_text("x\n")
    link = tmp_path / "link"
    os.symlink(real, link, target_is_directory=True)

    jail = PathJail(link)

    assert jail.resolve("f.txt") == real.resolve() / "f.txt"
