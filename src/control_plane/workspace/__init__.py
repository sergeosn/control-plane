"""Worktrees, diff, merge (docs/SPEC.md sections 5 and 13)."""

from control_plane.workspace.diff import (
    AddedLine,
    FileChange,
    added_lines,
    changed_files,
    diff_bytes,
    diff_patch,
    export_diff,
)
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
    "AddedLine",
    "FileChange",
    "DEFAULT_TEST_TIMEOUT_S",
    "MAIN_BRANCH",
    "MergeOutcome",
    "MergeReason",
    "MergeResult",
    "WorkspaceError",
    "Worktree",
    "added_lines",
    "branch_name",
    "changed_files",
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
