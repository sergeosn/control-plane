"""Protected paths (SPEC.md section 10.2).

`project.yaml` lists paths whose change deserves extra attention. Writing them is allowed;
the effect is on approvals, where a plan or a diff that touches them is called out. A
pattern is a file path, a directory (everything under it) or a glob in `fnmatch` syntax,
always relative to the repository root with forward slashes.
"""

import fnmatch
from collections.abc import Iterable, Mapping
from typing import Self

PROJECT_KEY = "protected_paths"
_GLOB_CHARS = frozenset("*?[")


class ProtectedPaths:
    def __init__(self, patterns: Iterable[str]) -> None:
        self.patterns: tuple[str, ...] = tuple(_normalise(p) for p in patterns)

    @classmethod
    def from_project(cls, project: Mapping[str, object]) -> Self:
        """The `protected_paths` list of a `project.yaml` mapping; absent means none."""
        raw = project.get(PROJECT_KEY, [])
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise ValueError(f"{PROJECT_KEY} must be a list of paths")
        return cls(raw)

    def __bool__(self) -> bool:
        return bool(self.patterns)

    def matches(self, path: str) -> bool:
        path = _normalise(path)
        if not path:
            return False
        return any(_match(pattern, path) for pattern in self.patterns)

    def touched(self, paths: Iterable[str]) -> list[str]:
        """The protected paths among `paths`, as given, sorted and without repeats."""
        return sorted({path for path in paths if self.matches(path)})


def _normalise(path: str) -> str:
    path = path.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path.strip("/")


def _match(pattern: str, path: str) -> bool:
    if _GLOB_CHARS.isdisjoint(pattern):
        return path == pattern or path.startswith(pattern + "/")
    return fnmatch.fnmatchcase(path, pattern)
