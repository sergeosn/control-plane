"""`diff.patch`: every change in a run worktree against its base commit (SPEC.md section 9).

The patch covers committed and uncommitted changes alike, untracked files included and
ignored files excluded, so it is what the worker produced whether or not anything was
committed yet. It is built in a temporary copy of the index, so exporting it never stages
anything in the worktree. The options are explicit so that the user's git configuration
(prefixes, colour, external diff tools) cannot change the format: the patch always applies
to the base commit with `git apply`.
"""

import shutil
import tempfile
from pathlib import Path

from control_plane.store.files import write_bytes_atomic
from control_plane.workspace.worktree import Worktree, project_git

_DIFF_OPTIONS = (
    "--binary",
    "--full-index",
    "--find-renames",
    "--no-color",
    "--no-ext-diff",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)


def diff_bytes(tree: Worktree) -> bytes:
    """The patch from the base commit to the worktree's current files, byte for byte."""
    git = project_git(tree.path)
    index = Path(git.run("rev-parse", "--git-path", "index").strip())
    if not index.is_absolute():
        index = tree.path / index
    with tempfile.TemporaryDirectory(prefix="cplane-diff-") as scratch:
        temp_index = Path(scratch) / "index"
        env = {"GIT_INDEX_FILE": str(temp_index)}
        if index.exists():
            shutil.copyfile(index, temp_index)
        else:
            git.run("read-tree", "HEAD", env=env)
        git.run("add", "--all", env=env)
        return git.run_bytes("diff", "--cached", *_DIFF_OPTIONS, tree.base_commit, env=env)


def diff_patch(tree: Worktree) -> str:
    """The patch as text; bytes that are not UTF-8 are replaced. Use `export_diff` to keep them."""
    return diff_bytes(tree).decode("utf-8", errors="replace")


def export_diff(tree: Worktree, target: Path) -> None:
    """Write the worktree's patch to `target`, usually the run folder's `diff.patch`."""
    write_bytes_atomic(target, diff_bytes(tree))
