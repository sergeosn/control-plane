"""Task 13: the `scope` check compares the diff with the plan's `files_to_touch` (docs/SPEC.md 11).

A plan entry is a file, a directory (everything under it) or a glob, as protected paths are.
"""

import pytest

from control_plane.gates.scope import out_of_plan
from control_plane.workspace import FileChange


def change(path: str, old_path: str | None = None) -> FileChange:
    return FileChange(path, old_path, added=1, deleted=0, binary=False)


def test_files_the_plan_names_are_in_scope() -> None:
    assert out_of_plan(["app.py", "tests/test_app.py"], [change("app.py")]) == []


def test_files_the_plan_does_not_name_are_out_of_plan_sorted() -> None:
    changes = [change("z.py"), change("app.py"), change("docs/a.md")]

    assert out_of_plan(["app.py"], changes) == ["docs/a.md", "z.py"]


@pytest.mark.parametrize(
    ("planned", "path"),
    [
        ("src/pkg/", "src/pkg/deep/mod.py"),
        ("src/pkg", "src/pkg/mod.py"),
        ("tests/test_*.py", "tests/test_new.py"),
        ("./app.py", "app.py"),
    ],
)
def test_a_plan_entry_can_be_a_directory_or_a_glob(planned: str, path: str) -> None:
    assert out_of_plan([planned], [change(path)]) == []


def test_a_rename_touches_both_its_old_and_its_new_path() -> None:
    assert out_of_plan(["new.py"], [change("new.py", old_path="old.py")]) == ["old.py"]


def test_an_empty_plan_puts_every_change_out_of_plan() -> None:
    assert out_of_plan([], [change("a.py"), change("a.py")]) == ["a.py"]
