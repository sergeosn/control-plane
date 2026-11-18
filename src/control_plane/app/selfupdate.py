"""`cplane self-update`: adopt a version of the system (docs/SPEC.md section 13).

The primary checkout is the project's repository in `project.yaml`; the control checkout is
`ProjectConfig.control_checkout()`, the same folder the guardrail gate takes its suite from.
The selfcheck (`uv run --frozen cplane selfcheck`) runs in the primary checkout through the
gates' command runner. When it passes, the tag `last-known-good` moves to the target commit
(the head of `main`, or `--to REF`), the control checkout is checked out at the tag,
detached, and `uv sync --frozen` runs there. When the selfcheck fails nothing changes. When
the sync fails the tag and the control checkout are put back where they were and synced
again. Rolling back is `--to` the previous commit.

The tag lives in the primary repository. A control checkout that is a worktree of it shares
the tag; one that is a separate clone gets the tag fetched from the primary checkout. Git
runs with hooks switched off, as for every project command.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from control_plane.domain.project import CommandGate, ProjectConfig
from control_plane.gates.base import GateResult
from control_plane.gates.commands import command_gate
from control_plane.store.git import Git, GitError
from control_plane.store.home import Home
from control_plane.store.projects import ProjectConfigError, load_project
from control_plane.workspace.merge import MAIN_BRANCH
from control_plane.workspace.worktree import project_git

DEFAULT_PROJECT = "control-plane"
TAG = "last-known-good"
SELFCHECK_ARGV: tuple[str, ...] = ("uv", "run", "--frozen", "cplane", "selfcheck")
SYNC_ARGV: tuple[str, ...] = ("uv", "sync", "--frozen")
SELFCHECK_TIMEOUT_S = 30 * 60.0
SYNC_TIMEOUT_S = 15 * 60.0


class SelfUpdateError(Exception):
    """The update was refused before anything changed. The message says why, in one line."""


@dataclass(frozen=True)
class SelfUpdateResult:
    adopted: bool
    primary: Path
    control: Path
    target: str
    """The commit asked for."""
    previous: str | None
    """The commit the tag pointed to before; None when there was no tag."""
    current: str | None
    """The commit the tag points to now: `target` when adopted, else `previous`."""
    failed: Literal["selfcheck", "sync"] | None
    output: str
    """The selfcheck's output when it failed; the sync's output otherwise."""


async def self_update(
    home: Home,
    project: str = DEFAULT_PROJECT,
    *,
    to: str | None = None,
    selfcheck_argv: Sequence[str] | None = None,
    sync_argv: Sequence[str] | None = None,
) -> SelfUpdateResult:
    """Adopt `to` (default: `main` of the primary checkout) when the selfcheck passes.

    Raises `SelfUpdateError`, changing nothing, for an unknown project or ref, a primary
    checkout that is not on `main` or has local changes, or a control checkout that is
    missing or has local changes.
    """
    config = _load(home, project)
    primary = project_git(config.repo)
    _check_primary(primary)
    control = _open_control(config)
    target = _resolve(primary, to)
    previous = _tag_commit(primary)

    selfcheck = await _command("selfcheck", selfcheck_argv or SELFCHECK_ARGV, primary.root)
    result = _Result(primary.root, control.root, target, previous)
    if selfcheck.status != "pass":
        return result.refused("selfcheck", selfcheck.output)

    shared = _same_repository(primary, control)
    control_before = control.head()
    _set_tag(primary, control, shared, target)
    try:
        control.run("checkout", "--quiet", "--detach", f"refs/tags/{TAG}")
    except GitError:
        _set_tag(primary, control, shared, previous)
        raise
    sync = await _command("sync", sync_argv or SYNC_ARGV, control.root)
    if sync.status == "pass":
        return result.adopted(sync.output)

    _set_tag(primary, control, shared, previous)
    control.run("checkout", "--quiet", "--detach", control_before)
    resync = await _command("sync", sync_argv or SYNC_ARGV, control.root)
    restored = f"--- after putting {control_before} back ---\n{resync.output}"
    return result.refused("sync", f"{sync.output}\n{restored}")


@dataclass(frozen=True)
class _Result:
    primary: Path
    control: Path
    target: str
    previous: str | None

    def adopted(self, output: str) -> SelfUpdateResult:
        return SelfUpdateResult(
            True, self.primary, self.control, self.target, self.previous, self.target, None, output
        )

    def refused(self, failed: Literal["selfcheck", "sync"], output: str) -> SelfUpdateResult:
        return SelfUpdateResult(
            False,
            self.primary,
            self.control,
            self.target,
            self.previous,
            self.previous,
            failed,
            output,
        )


def _load(home: Home, project: str) -> ProjectConfig:
    try:
        return load_project(home, project)
    except ProjectConfigError as exc:
        raise SelfUpdateError(str(exc)) from exc


def _check_primary(primary: Git) -> None:
    if not primary.root.is_dir():
        raise SelfUpdateError(f"the primary checkout {primary.root} does not exist")
    branch = primary.process("symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    if branch != MAIN_BRANCH:
        raise SelfUpdateError(
            f"the primary checkout {primary.root} is not on {MAIN_BRANCH} "
            f"(it is on {branch or 'a detached HEAD'})"
        )
    if _local_changes(primary):
        raise SelfUpdateError(
            f"the primary checkout {primary.root} has local changes; commit or stash them"
        )


def _open_control(config: ProjectConfig) -> Git:
    path = config.control_checkout()
    control = project_git(path)
    top = control.process("rev-parse", "--show-toplevel") if path.is_dir() else None
    if top is None or top.returncode != 0 or Path(top.stdout.strip()) != path.resolve():
        raise SelfUpdateError(
            f"no control checkout at {path}; create it with "
            f"`git -C {config.repo} worktree add --detach {path} {TAG}`"
        )
    if _local_changes(control):
        raise SelfUpdateError(
            f"the control checkout {path} has local changes; only self-update writes there"
        )
    return control


def _local_changes(git: Git) -> bool:
    """Changes to tracked files, staged or not; untracked files do not count."""
    return bool(git.run("status", "--porcelain", "--untracked-files=no").strip())


def _resolve(primary: Git, to: str | None) -> str:
    ref = f"refs/heads/{MAIN_BRANCH}" if to is None else to
    found = None if ref.startswith("-") else _commit(primary, ref)
    if found is None:
        raise SelfUpdateError(f"{ref!r} is not a commit in {primary.root}")
    return found


def _commit(git: Git, ref: str) -> str | None:
    found = git.process("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return found.stdout.strip() if found.returncode == 0 else None


def _tag_commit(git: Git) -> str | None:
    return _commit(git, f"refs/tags/{TAG}")


def _same_repository(primary: Git, control: Git) -> bool:
    def common(git: Git) -> Path:
        return (git.root / git.run("rev-parse", "--git-common-dir").strip()).resolve()

    return common(primary) == common(control)


def _set_tag(primary: Git, control: Git, shared: bool, commit: str | None) -> None:
    """Point the tag at `commit` in the primary repository, or remove it for None, and
    mirror that into a control checkout that is a separate clone."""
    clones = [] if shared else [control]
    if commit is not None:
        primary.run("tag", "--force", TAG, commit)
        for clone in clones:
            clone.run(
                "fetch",
                "--quiet",
                "--no-tags",
                str(primary.root),
                f"+refs/tags/{TAG}:refs/tags/{TAG}",
            )
        return
    for git in (primary, *clones):
        if _tag_commit(git) is not None:
            git.run("tag", "--delete", TAG)


async def _command(name: str, argv: Sequence[str], cwd: Path) -> GateResult:
    timeout = SELFCHECK_TIMEOUT_S if name == "selfcheck" else SYNC_TIMEOUT_S
    return await command_gate(name, CommandGate(argv=list(argv), timeout_s=timeout), cwd)
