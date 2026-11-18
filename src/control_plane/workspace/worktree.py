"""Run worktrees: one per run, on branch `run/<run-id>` (SPEC.md sections 5 and 13).

A run's worktree is the only place an agent can write. It lives at
`<home>/.worktrees/<run-id>` and is created from the base commit recorded in the run's
manifest, so the run starts from exactly the code it says it started from.

Every git command here runs with the project's hooks switched off: the engine's own commits
and merges must not execute code from the repository it is operating on.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from control_plane.store.git import Git, GitError

BRANCH_PREFIX = "run/"

# A run id is used as a branch name component and as a folder name, so it is kept to a
# character set that is safe for both: no slash, no dot, no `..`, no `.lock` suffix.
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

# The engine never runs the project's hooks; the null device is an empty hooks directory.
_NO_HOOKS = {"core.hooksPath": "/dev/null"}


class WorkspaceError(Exception):
    """A workspace operation could not be done. The message says why, in one line."""


def project_git(root: Path) -> Git:
    """A `Git` for a project checkout or a run worktree: engine identity, no hooks."""
    return Git(root, config=_NO_HOOKS)


def branch_name(run_id: str) -> str:
    """The branch of a run's worktree: `run/<run-id>`."""
    if not _RUN_ID.fullmatch(run_id):
        raise WorkspaceError(
            f"run id {run_id!r} cannot name a branch: use letters, digits, '-' and '_' only"
        )
    return f"{BRANCH_PREFIX}{run_id}"


@dataclass(frozen=True)
class Worktree:
    repo: Path
    """The primary checkout of the project, on `main`."""
    path: Path
    run_id: str
    base_commit: str
    """Full sha of the commit the worktree was created from."""

    @property
    def branch(self) -> str:
        return branch_name(self.run_id)


def create_worktree(repo: Path, worktrees_dir: Path, run_id: str, base_commit: str) -> Worktree:
    """Create the worktree of `run_id` at `worktrees_dir/<run-id>` from `base_commit`."""
    branch = branch_name(run_id)
    path = worktrees_dir / run_id
    git = project_git(repo)
    if path.exists():
        raise WorkspaceError(f"a worktree for run {run_id} already exists at {path}")
    resolved = git.process("rev-parse", "--verify", "--quiet", f"{base_commit}^{{commit}}")
    if resolved.returncode != 0:
        raise WorkspaceError(f"base commit {base_commit!r} is not a commit in {repo}")
    sha = resolved.stdout.strip()
    if git.process("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0:
        raise WorkspaceError(f"branch {branch} already exists in {repo}")
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    try:
        git.run("worktree", "add", "--quiet", "-b", branch, str(path), sha)
    except GitError as exc:
        raise WorkspaceError(str(exc)) from exc
    return Worktree(repo=repo, path=path, run_id=run_id, base_commit=sha)


def open_worktree(repo: Path, worktrees_dir: Path, run_id: str, base_commit: str) -> Worktree:
    """The existing worktree of `run_id`, for a run that resumes in a new process."""
    branch = branch_name(run_id)
    path = worktrees_dir / run_id
    missing = WorkspaceError(f"no worktree for run {run_id} on branch {branch} at {path}")
    if not path.is_dir():
        raise missing
    current = project_git(path).process("symbolic-ref", "--quiet", "--short", "HEAD")
    if current.returncode != 0 or current.stdout.strip() != branch:
        raise missing
    return Worktree(repo=repo, path=path, run_id=run_id, base_commit=base_commit)


def remove_worktree(tree: Worktree, *, delete_branch: bool = False) -> None:
    """Remove the worktree folder and unregister it. The branch stays unless asked otherwise."""
    git = project_git(tree.repo)
    if tree.path.exists():
        git.run("worktree", "remove", "--force", str(tree.path))
    git.run("worktree", "prune")
    if delete_branch:
        git.run("branch", "--quiet", "-D", tree.branch)


def commit(tree: Worktree, message: str) -> str | None:
    """Commit every change in the worktree to its run branch.

    Returns the new head's sha, or None when there was nothing to commit. Files the project
    ignores are not committed.
    """
    git = project_git(tree.path)
    git.run("add", "--all")
    if git.process("diff", "--cached", "--quiet").returncode == 0:
        return None
    git.run("commit", "--quiet", "--message", message)
    return git.head()
