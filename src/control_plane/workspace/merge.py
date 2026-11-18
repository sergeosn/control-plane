"""The `merge_and_verify` action (SPEC.md section 13).

It merges a run branch into `main` of the primary checkout with a merge commit and runs the
project's full test suite there. Green, the merge stays. Red, `main` is reset to the
pre-merge commit, so it is exactly where it was before, and the run fails with the test
output. A merge that conflicts or cannot be made is abandoned and `main` is left untouched.

Before merging it refuses, without changing anything, when the worktree has uncommitted
changes (they would silently be left out), when the branch holds nothing new, and when the
primary checkout is not on `main` or has local changes to tracked files (only this action
writes `main`, so a change there is someone else's work and must not be reset away).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from control_plane.store.files import write_text_atomic
from control_plane.store.git import Git
from control_plane.tools.commands import CommandRunner
from control_plane.tools.errors import ToolError
from control_plane.workspace.worktree import Worktree, project_git

MAIN_BRANCH = "main"
DEFAULT_TEST_TIMEOUT_S = 30 * 60.0

MergeOutcome = Literal["ok", "error"]
MergeReason = Literal[
    "merged",
    "tests_failed",
    "conflict",
    "merge_failed",
    "uncommitted",
    "nothing_to_merge",
    "dirty_main",
    "not_on_main",
]


@dataclass(frozen=True)
class MergeResult:
    outcome: MergeOutcome
    """The step outcome of the flow's `merge` action: `ok` or `error`."""
    reason: MergeReason
    message: str
    pre_merge_commit: str
    head: str
    """Where `main` is after the action."""
    merge_commit: str | None = None
    """The merge commit, kept on green and reset away on red; None when none was made."""
    test_output: str = ""


@dataclass(frozen=True)
class _TestRun:
    passed: bool
    summary: str
    output: str


async def merge_and_verify(
    tree: Worktree,
    test_argv: Sequence[str],
    *,
    main_branch: str = MAIN_BRANCH,
    timeout: float = DEFAULT_TEST_TIMEOUT_S,
    output_file: Path | None = None,
) -> MergeResult:
    """Merge the run branch of `tree` into `main_branch` and verify it with `test_argv`.

    `test_argv` runs in the primary checkout through the same runner as `run_command`. Its
    output is returned and, when `output_file` is given, saved there whatever the result.
    """
    primary = project_git(tree.repo)
    pre = primary.head()

    def refuse(reason: MergeReason, message: str) -> MergeResult:
        return MergeResult("error", reason, message, pre_merge_commit=pre, head=primary.head())

    if project_git(tree.path).status():
        return refuse("uncommitted", f"{tree.branch} has uncommitted changes; commit them first")
    current = primary.process("symbolic-ref", "--quiet", "--short", "HEAD")
    if current.returncode != 0 or current.stdout.strip() != main_branch:
        found = current.stdout.strip() or "a detached HEAD"
        return refuse("not_on_main", f"{tree.repo} is on {found}, not {main_branch}")
    if primary.run("status", "--porcelain=v1", "--untracked-files=no").strip():
        return refuse("dirty_main", f"{main_branch} in {tree.repo} has local changes")
    if primary.process("merge-base", "--is-ancestor", tree.branch, "HEAD").returncode == 0:
        return refuse("nothing_to_merge", f"{tree.branch} has nothing that {main_branch} lacks")

    merged = primary.process(
        "merge", "--no-ff", "--no-edit", "--message", f"Merge {tree.branch}", tree.branch
    )
    if merged.returncode != 0:
        conflicted = primary.run("diff", "--name-only", "--diff-filter=U").split()
        _restore(primary, pre)
        if conflicted:
            files = ", ".join(conflicted)
            return refuse("conflict", f"merging {tree.branch} conflicts in: {files}")
        detail = merged.stderr.strip() or merged.stdout.strip()
        return refuse("merge_failed", f"merging {tree.branch} failed: {detail}")
    merge_commit = primary.head()

    tests = await _run_tests(tree.repo, test_argv, timeout)
    if output_file is not None:
        write_text_atomic(output_file, tests.output)
    if tests.passed:
        return MergeResult(
            "ok",
            "merged",
            f"merged {tree.branch} into {main_branch}; tests passed",
            pre_merge_commit=pre,
            head=merge_commit,
            merge_commit=merge_commit,
            test_output=tests.output,
        )
    _restore(primary, pre)
    return MergeResult(
        "error",
        "tests_failed",
        f"tests {tests.summary} after merging {tree.branch}; {main_branch} reset to {pre[:12]}",
        pre_merge_commit=pre,
        head=primary.head(),
        merge_commit=merge_commit,
        test_output=tests.output,
    )


def _restore(primary: Git, pre: str) -> None:
    """Put the primary checkout back at `pre`: abandon a merge in progress, drop a merge commit.

    Untracked files are left alone; the checkout had no local changes to tracked files before
    the merge, so a hard reset loses nothing that was not the merge's own.
    """
    primary.process("merge", "--abort")
    tracked_changes = primary.run("status", "--porcelain=v1", "--untracked-files=no").strip()
    if primary.head() != pre or tracked_changes:
        primary.run("reset", "--quiet", "--hard", pre)


async def _run_tests(cwd: Path, argv: Sequence[str], timeout: float) -> _TestRun:
    try:
        result = await CommandRunner(cwd).run(argv, timeout=timeout)
    except ToolError as exc:
        return _TestRun(passed=False, summary=f"could not start ({exc})", output=f"{exc}\n")
    if result.timed_out:
        summary = f"timed out after {timeout:g}s"
    elif result.exit_code == 0:
        summary = "passed"
    else:
        summary = f"failed with exit code {result.exit_code}"
    output = (
        f"$ {' '.join(result.argv)}\n{summary}\n"
        f"--- stdout ---\n{result.stdout}--- stderr ---\n{result.stderr}"
    )
    return _TestRun(passed=summary == "passed", summary=summary, output=output)
