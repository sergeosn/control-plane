"""A project repository for the workspace tests, built in a temporary directory.

The project holds a tiny pytest suite, so `merge_and_verify` runs a real test command:
`app.value()` must return 1. A run branch that changes it turns the suite red.
"""

import sys
from pathlib import Path

from control_plane.store.git import Git

TEST_ARGV = (sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider")

FILES = {
    ".gitignore": "__pycache__/\n*.pyc\n",
    "app.py": "def value():\n    return 1\n",
    "test_app.py": "from app import value\n\n\ndef test_value():\n    assert value() == 1\n",
    "notes.txt": "first line\n",
}


def write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def make_project(root: Path) -> Git:
    """A project repository at `root` on `main` with one commit."""
    git = Git(root)
    git.init()
    write(git.root, FILES)
    git.add(".")
    git.commit("Initial commit")
    return git
