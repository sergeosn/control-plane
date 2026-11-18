"""`diff.patch`: every change in a run worktree against its base commit (docs/SPEC.md section 9).

The patch covers committed and uncommitted changes alike, untracked files included and
ignored files excluded, so it is what the worker produced whether or not anything was
committed yet. It is built in a temporary copy of the index, so exporting it never stages
anything in the worktree. The options are explicit so that the user's git configuration
(prefixes, colour, external diff tools) cannot change the format: the patch always applies
to the base commit with `git apply`.

`changed_files` and `added_lines` read the same changes for the gates and the scope check
(docs/SPEC.md section 11): per file, the lines added and deleted; and every added line with its
number in the new file.
"""

import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
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
# Paths are printed as they are, not quoted with octal escapes, so a report names the file.
_LITERAL_PATHS = ("-c", "core.quotePath=false")
_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class FileChange:
    path: str
    """The path in the worktree; for a deleted file, the path it had."""
    old_path: str | None
    """For a rename, the path at the base commit; otherwise None."""
    added: int
    deleted: int
    binary: bool
    """A binary file has no line counts; `added` and `deleted` are 0."""

    @property
    def changed_lines(self) -> int:
        return self.added + self.deleted


@dataclass(frozen=True)
class AddedLine:
    path: str
    line: int
    """1-based, in the worktree's version of the file."""
    text: str


def diff_bytes(tree: Worktree) -> bytes:
    """The patch from the base commit to the worktree's current files, byte for byte."""
    return _diff(tree, *_DIFF_OPTIONS)


def changed_files(tree: Worktree) -> list[FileChange]:
    """Every file that differs from the base commit, with its added and deleted line counts."""
    raw = _diff(tree, "--numstat", "-z", "--find-renames", "--no-ext-diff")
    fields = raw.decode("utf-8", errors="replace").split("\0")
    changes = []
    position = 0
    while position < len(fields) and fields[position]:
        added, deleted, path = fields[position].split("\t", 2)
        position += 1
        old_path = None
        if not path:
            old_path, path = fields[position], fields[position + 1]
            position += 2
        binary = added == "-"
        changes.append(
            FileChange(
                path=path,
                old_path=old_path,
                added=0 if binary else int(added),
                deleted=0 if binary else int(deleted),
                binary=binary,
            )
        )
    return changes


def added_lines(tree: Worktree) -> list[AddedLine]:
    """Every line the worktree adds to a text file, in file order. Binary files have none."""
    raw = _diff(
        tree,
        "--unified=0",
        "--find-renames",
        "--no-color",
        "--no-ext-diff",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        config=_LITERAL_PATHS,
    )
    found: list[AddedLine] = []
    path: str | None = None
    line = to_add = to_delete = 0
    for text in raw.decode("utf-8", errors="replace").split("\n"):
        if to_add and text.startswith("+"):
            if path is not None:
                found.append(AddedLine(path, line, text[1:]))
            line += 1
            to_add -= 1
        elif to_delete and text.startswith("-"):
            to_delete -= 1
        elif to_add or to_delete or text.startswith("\\"):
            continue
        elif text.startswith("+++ "):
            target = text[4:].rstrip("\t")
            path = target[2:] if target.startswith("b/") else None
        elif match := _HUNK.match(text):
            deleted, start, added = match.groups()
            to_delete = 1 if deleted is None else int(deleted)
            to_add = 1 if added is None else int(added)
            line = int(start)
    return found


def _diff(tree: Worktree, *options: str, config: tuple[str, ...] = ()) -> bytes:
    """`git diff --cached <options> <base>` over a temporary index holding every change.

    The worktree's own index is copied and every file added to the copy, so untracked files
    count and ignored ones do not, and nothing is staged in the worktree itself.
    """
    git = project_git(tree.path)
    with _full_index(tree) as env:
        return git.run_bytes(*config, "diff", "--cached", *options, tree.base_commit, env=env)


@contextmanager
def _full_index(tree: Worktree) -> Iterator[dict[str, str]]:
    git = project_git(tree.path)
    index = Path(git.run("rev-parse", "--git-path", "index").strip())
    if not index.is_absolute():
        index = tree.path / index
    with tempfile.TemporaryDirectory(prefix="cplane-diff-") as scratch:
        temp_index = Path(scratch) / "index"
        env = {"GIT_INDEX_FILE": str(temp_index)}
        if index.exists():
            # copy2 keeps the index's mtime. Git compares an entry by content when its mtime
            # is not older than the index (the racy-index check); a copy stamped later would
            # pass a same-size edit made in the checkout's tick as unchanged.
            shutil.copy2(index, temp_index)
        else:
            git.run("read-tree", "HEAD", env=env)
        git.run("add", "--all", env=env)
        yield env


def diff_patch(tree: Worktree) -> str:
    """The patch as text; bytes that are not UTF-8 are replaced. Use `export_diff` to keep them."""
    return diff_bytes(tree).decode("utf-8", errors="replace")


def export_diff(tree: Worktree, target: Path) -> None:
    """Write the worktree's patch to `target`, usually the run folder's `diff.patch`."""
    write_bytes_atomic(target, diff_bytes(tree))
