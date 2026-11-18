"""The path jail (docs/SPEC.md section 10.2).

Every path argument a tool receives is resolved with symlinks followed and must fall inside
one of the roots: the worktree (readable, and writable for the worker) and the run's
`context/` directory (readable). Anything else is denied, including a symlink inside the
worktree that points outside it. A `..` component is denied outright, even when it would
stay inside, so a path is always a plain descent from a root.

This is a hard check: no rule and no text the model reads can change its outcome.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


class PathDeniedError(Exception):
    """A path argument fell outside the roots or asked for more than the root allows."""


@dataclass(frozen=True)
class Root:
    path: Path
    writable: bool


class PathJail:
    """Resolves tool path arguments against a worktree and read-only extra roots."""

    def __init__(
        self, worktree: Path, *, writable: bool = True, read_roots: Sequence[Path] = ()
    ) -> None:
        self.worktree = worktree.resolve()
        self._roots = (
            Root(self.worktree, writable),
            *(Root(root.resolve(), False) for root in read_roots),
        )

    @property
    def roots(self) -> tuple[Path, ...]:
        return tuple(root.path for root in self._roots)

    def resolve(self, raw: str, *, write: bool = False) -> Path:
        """The real path `raw` names, or `PathDeniedError` with the reason.

        Relative paths are taken from the worktree. The returned path may not exist yet;
        its existing prefix has been resolved, so a symlinked parent cannot lead outside.
        """
        if not raw.strip():
            raise PathDeniedError("path is empty; give a path relative to the worktree")
        candidate = Path(raw)
        if ".." in candidate.parts:
            raise PathDeniedError(
                f"path {raw!r} contains '..'; give a path relative to the worktree "
                "without '..' components"
            )
        if not candidate.is_absolute():
            candidate = self.worktree / candidate
        resolved = candidate.resolve()
        root = self._root_of(resolved)
        if root is None:
            via = ""
            if resolved != candidate.absolute():
                via = f" (it resolves through a symlink to {resolved})"
            raise PathDeniedError(
                f"path {raw!r} is outside the allowed roots{via}; "
                f"allowed roots: {', '.join(str(r.path) for r in self._roots)}"
            )
        if write and not root.writable:
            raise PathDeniedError(
                f"path {raw!r} is under the read-only root {root.path}; "
                "only the worktree can be written"
            )
        return resolved

    def display(self, path: Path) -> str:
        """`path` relative to the worktree when inside it, otherwise as given."""
        if path.is_relative_to(self.worktree):
            return str(path.relative_to(self.worktree)) if path != self.worktree else "."
        return str(path)

    def _root_of(self, resolved: Path) -> Root | None:
        for root in self._roots:
            if resolved == root.path or resolved.is_relative_to(root.path):
                return root
        return None
