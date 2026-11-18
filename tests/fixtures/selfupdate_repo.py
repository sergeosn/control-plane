"""Fixture repositories for `cplane self-update` (docs/SPEC.md section 13).

A home with the project registered, a primary checkout on `main` with two commits, a control
checkout next to it at the first one, and the tag `last-known-good` on the first one. The
selfcheck and `uv sync` are replaced by small Python commands that record the folder they
ran in and exit 0, or 1 when a given file exists there.
"""

import sys
from dataclasses import dataclass
from pathlib import Path

from control_plane.app.projects import add_project
from control_plane.store.git import Git
from control_plane.store.home import Home, create_home
from fixtures.project_repo import make_project

TAG = "last-known-good"
NEW_FILE = "new.txt"
"""Only the second commit of `main` has it, so a fake can tell the new version from the old."""


@dataclass(frozen=True)
class SelfUpdateRepos:
    home: Home
    primary: Git
    control: Path
    old: str
    """The first commit, where the tag and the control checkout start."""
    new: str
    """The head of `main`."""


def make_repos(
    root: Path, *, project: str = "control-plane", clone: bool = False
) -> SelfUpdateRepos:
    """The fixture; `clone` makes the control checkout a separate clone, not a worktree."""
    primary = make_project(root / "primary")
    old = primary.head()
    (primary.root / NEW_FILE).write_text("the new version\n")
    primary.add(NEW_FILE)
    new = primary.commit("A new version")
    control = root / "primary-control"
    if clone:
        Git(root).run("clone", "--quiet", "--no-checkout", str(primary.root), str(control))
        Git(control).run("checkout", "--quiet", "--detach", old)
    else:
        primary.run("worktree", "add", "--quiet", "--detach", str(control), old)
    primary.run("tag", TAG, old)
    home = create_home(root / "home")
    add_project(home, project, primary.root)
    return SelfUpdateRepos(home=home, primary=primary, control=control, old=old, new=new)


def fake_command(log: Path, *, output: str, fail_when: str | None = None) -> list[str]:
    """argv of a command that appends its working folder to `log`, prints `output`, and
    exits 1 when `fail_when` exists in its working folder (`"."` always does), else 0."""
    script = (
        "import os, sys\n"
        f"open({str(log)!r}, 'a').write(os.getcwd() + '\\n')\n"
        f"print({output!r})\n"
        f"sys.exit(1 if {fail_when!r} is not None and os.path.exists({fail_when!r}) else 0)\n"
    )
    return [sys.executable, "-c", script]


def ran_in(log: Path) -> list[Path]:
    """The folders a fake command ran in, in order; empty when it never ran."""
    if not log.exists():
        return []
    return [Path(line).resolve() for line in log.read_text().splitlines()]


def head(root: Path) -> str:
    return Git(root).head()


def tag_commit(primary: Git) -> str | None:
    found = primary.process("rev-parse", "--verify", "--quiet", f"refs/tags/{TAG}^{{commit}}")
    return found.stdout.strip() if found.returncode == 0 else None


def is_detached(root: Path) -> bool:
    return Git(root).process("symbolic-ref", "--quiet", "HEAD").returncode != 0
