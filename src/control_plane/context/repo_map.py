"""The repo map (SPEC.md section 9).

The tree of tracked files with line counts and, for Python files, top-level classes and
functions taken from the `ast` module. It is generated per run from the worktree.

The map is bounded: when the full tree does not fit in `max_lines`, directories below a
depth are collapsed into one line each that says how many files and lines they hold, at the
deepest depth that fits. A tree that does not fit even with every top-level directory
collapsed is cut, and the cut is announced. Truncation is always announced (section 9).
"""

import ast
import warnings
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from control_plane.store.git import Git

REPO_MAP_MAX_LINES = 400
"""One `read_file` call shows the whole map."""

MAX_SYMBOLS = 12
"""Top-level symbols shown per Python file; the rest are counted."""

INDENT = "  "
_BINARY_PROBE = 8000


@dataclass(frozen=True)
class RepoFile:
    path: str
    """Relative to the repository root, with `/` separators."""
    lines: int | None
    """None for a binary file or a symlink, whose lines are not counted."""
    symbols: tuple[str, ...] = ()
    """Top-level classes (as `class Name`) and functions of a Python file, in order."""
    symlink: bool = False


def scan_repo(root: Path) -> list[RepoFile]:
    """Every file git tracks under `root`, sorted by path."""
    listed = Git(root).run("ls-files", "-z")
    files = (describe_file(root, path) for path in sorted(set(listed.split("\0")) - {""}))
    return [file for file in files if file is not None]


def describe_file(root: Path, relative: str) -> RepoFile | None:
    """The map entry of one file; None when it is not a regular file in the checkout."""
    path = root / relative
    if path.is_symlink():
        return RepoFile(relative, None, symlink=True)
    if not path.is_file():
        return None
    data = path.read_bytes()
    if b"\0" in data[:_BINARY_PROBE]:
        return RepoFile(relative, None)
    text = data.decode("utf-8", errors="replace")
    symbols = _python_symbols(text) if relative.endswith(".py") else ()
    return RepoFile(relative, len(text.splitlines()), symbols)


def _python_symbols(text: str) -> tuple[str, ...]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return ()
    symbols: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            symbols.append(f"class {node.name}")
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            symbols.append(node.name)
    return tuple(symbols)


@dataclass
class _Dir:
    name: str
    depth: int
    dirs: dict[str, "_Dir"] = field(default_factory=dict)
    files: list[RepoFile] = field(default_factory=list)
    file_count: int = 0
    line_count: int = 0

    def max_depth(self) -> int:
        return max((d.max_depth() for d in self.dirs.values()), default=self.depth)


def _tree(files: Sequence[RepoFile]) -> _Dir:
    root = _Dir("", 0)
    for file in files:
        node = root
        node.file_count += 1
        node.line_count += file.lines or 0
        for part in file.path.split("/")[:-1]:
            node = node.dirs.setdefault(part, _Dir(part, node.depth + 1))
            node.file_count += 1
            node.line_count += file.lines or 0
        node.files.append(file)
    return root


def _lines(count: int) -> str:
    return f"{count} line" if count == 1 else f"{count} lines"


def _file_line(file: RepoFile, indent: str) -> str:
    name = file.path.rsplit("/", 1)[-1]
    if file.symlink:
        return f"{indent}{name} (symlink)"
    if file.lines is None:
        return f"{indent}{name} (binary)"
    line = f"{indent}{name} ({_lines(file.lines)})"
    if file.symbols:
        shown = ", ".join(file.symbols[:MAX_SYMBOLS])
        extra = len(file.symbols) - MAX_SYMBOLS
        line += f": {shown}" + (f", +{extra} more" if extra > 0 else "")
    return line


def _render(node: _Dir, expand_to: int, out: list[str]) -> None:
    indent = INDENT * node.depth
    for file in sorted(node.files, key=lambda f: f.path):
        out.append(_file_line(file, indent))
    for child in sorted(node.dirs.values(), key=lambda d: d.name):
        if child.depth > expand_to:
            files = f"{child.file_count} file" + ("" if child.file_count == 1 else "s")
            out.append(f"{indent}{child.name}/ [collapsed: {files}, {_lines(child.line_count)}]")
        else:
            out.append(f"{indent}{child.name}/")
            _render(child, expand_to, out)


def render_repo_map(files: Sequence[RepoFile], max_lines: int = REPO_MAP_MAX_LINES) -> str:
    """The map of `files` in at most `max_lines` lines."""
    root = _tree(files)
    deepest = root.max_depth()
    summary = (
        f"{root.file_count} tracked files, {root.line_count} lines. Python files list their "
        "top-level classes and functions."
    )
    for expand_to in range(deepest, -1, -1):
        body: list[str] = []
        _render(root, expand_to, body)
        note = ""
        if expand_to < deepest:
            note = (
                f" Directories deeper than {expand_to} levels are collapsed; use list_dir "
                "to see inside them."
            )
        header = ["# Repository map", "", summary + note, ""]
        if len(header) + len(body) <= max_lines:
            return "\n".join(header + body) + "\n"
    keep = max(max_lines - len(header) - 1, 0)
    dropped = len(body) - keep
    cut = (
        f"[{dropped} more lines not shown of {len(body)}; use list_dir to see the rest of "
        "the repository]"
    )
    return "\n".join([*header, *body[:keep], cut]) + "\n"


def build_repo_map(root: Path, max_lines: int = REPO_MAP_MAX_LINES) -> str:
    """The map of the repository checked out at `root`."""
    return render_repo_map(scan_repo(root), max_lines)
