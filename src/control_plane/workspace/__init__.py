"""Worktrees, diff, merge (SPEC.md sections 5 and 13)."""

from control_plane.workspace.diff import diff_bytes, diff_patch, export_diff
from control_plane.workspace.merge import (
    DEFAULT_TEST_TIMEOUT_S,
    MAIN_BRANCH,
    MergeOutcome,
    MergeReason,
    MergeResult,
    merge_and_verify,
)
from control_plane.workspace.worktree import (
    BRANCH_PREFIX,
    WorkspaceError,
    Worktree,
    branch_name,
    commit,
    create_worktree,
    open_worktree,
    project_git,
    remove_worktree,
)

__all__ = [
    "BRANCH_PREFIX",
    "DEFAULT_TEST_TIMEOUT_S",
    "MAIN_BRANCH",
    "MergeOutcome",
    "MergeReason",
    "MergeResult",
    "WorkspaceError",
    "Worktree",
    "branch_name",
    "commit",
    "create_worktree",
    "diff_bytes",
    "diff_patch",
    "export_diff",
    "merge_and_verify",
    "open_worktree",
    "project_git",
    "remove_worktree",
]
