"""The `test_integrity` gate (docs/SPEC.md section 11).

It compares the test files of the worktree with those at the run's base commit and runs
nothing: the files are parsed with `ast`. It fails when

- a test that existed at the base is gone (a test is `path::name`, with `Class::` for a
  method of a `Test*` class; renaming a test or its file counts as deleting it),
- the number of tests fell, counting each case of a `parametrize` whose values are a
  literal list, tuple or set, so dropping a case is caught,
- a file has more skip or xfail markers than at the base: `pytest.mark.skip`, `skipif`,
  `xfail`, the `pytest.skip`, `pytest.xfail` and `pytest.importorskip` calls, and the
  `unittest` skip decorators, however `pytest` or `unittest` was imported,
- a test file of the worktree does not parse.

Which files count is set by `IntegrityGate.patterns`; `conftest.py` is among the defaults
because it can skip tests without being one.
"""

import ast
import fnmatch
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from control_plane.domain.project import IntegrityGate
from control_plane.gates.base import GateResult, gate_result
from control_plane.store.git import Git
from control_plane.workspace.worktree import Worktree, project_git

NAME = "test_integrity"
_MARKERS = frozenset(
    {"skip", "skipif", "xfail", "skipIf", "skipUnless", "expectedFailure", "importorskip"}
)
_MARKER_OWNERS = frozenset({"mark", "pytest", "unittest"})
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass(frozen=True)
class FileTests:
    tests: dict[str, int] = field(default_factory=dict)
    """Test name within the file (`Class::method` for a method) to its number of cases."""
    markers: int = 0


def analyse_source(source: str) -> FileTests:
    """The tests and the skip or xfail markers of one file. Raises `SyntaxError`."""
    module = ast.parse(source)
    tests: dict[str, int] = {}
    _collect(module.body, "", 1, tests)
    return FileTests(tests=tests, markers=_count_markers(module))


def integrity_gate(gate: IntegrityGate, tree: Worktree) -> GateResult:
    git = project_git(tree.path)
    base = _base_files(git, tree.base_commit, gate.patterns)
    current, unparsable = _current_files(tree, gate.patterns)

    base_ids = _ids(base)
    current_ids = _ids(current)
    deleted = sorted(set(base_ids) - set(current_ids))
    before, after = sum(base_ids.values()), sum(current_ids.values())
    new_markers = [
        (path, base.get(path, FileTests()).markers, summary.markers)
        for path, summary in sorted(current.items())
        if summary.markers > base.get(path, FileTests()).markers
    ]

    problems = [f"cannot parse {path}: {error}" for path, error in unparsable]
    if deleted:
        problems.append(
            "deleted tests (at the base commit, gone now):\n"
            + "\n".join(f"- {test}" for test in deleted)
        )
    if after < before:
        problems.append(f"the number of tests fell: {before} -> {after}")
    for path, was, now in new_markers:
        problems.append(f"new skip or xfail marker in {path}: {was} -> {now}")
    if problems:
        summary = "; ".join(problem.splitlines()[0].rstrip(":") for problem in problems)
        return gate_result(NAME, "fail", summary, "\n\n".join(problems))
    summary = f"tests {before} -> {after}; none removed, no new skip or xfail marker"
    return gate_result(NAME, "pass", summary)


def _collect(body: list[ast.stmt], prefix: str, cases: int, into: dict[str, int]) -> None:
    for node in body:
        if isinstance(node, _FUNCTIONS) and node.name.startswith("test"):
            into[prefix + node.name] = cases * _cases(node.decorator_list)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            _collect(node.body, f"{prefix}{node.name}::", cases * _cases(node.decorator_list), into)


def _cases(decorators: list[ast.expr]) -> int:
    count = 1
    for decorator in decorators:
        if not isinstance(decorator, ast.Call):
            continue
        name = _dotted(decorator.func)
        if not name or name[-1] != "parametrize":
            continue
        values = decorator.args[1] if len(decorator.args) > 1 else None
        for keyword in decorator.keywords:
            if keyword.arg == "argvalues":
                values = keyword.value
        if isinstance(values, ast.List | ast.Tuple | ast.Set):
            count *= len(values.elts)
    return count


def _count_markers(module: ast.Module) -> int:
    count = 0
    for node in ast.walk(module):
        if isinstance(node, ast.Attribute):
            name = _dotted(node)
            if name and len(name) > 1 and name[-1] in _MARKERS and name[-2] in _MARKER_OWNERS:
                count += 1
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _MARKERS:
                count += 1
        elif isinstance(node, (*_FUNCTIONS, ast.ClassDef)):
            count += sum(
                1 for d in node.decorator_list if isinstance(d, ast.Name) and d.id in _MARKERS
            )
    return count


def _dotted(node: ast.expr) -> list[str] | None:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        owner = _dotted(node.value)
        return None if owner is None else [*owner, node.attr]
    return None


def _is_test_file(path: str, patterns: list[str]) -> bool:
    name = PurePosixPath(path).name
    return any(
        fnmatch.fnmatchcase(path if "/" in pattern else name, pattern) for pattern in patterns
    )


def _base_files(git: Git, base: str, patterns: list[str]) -> dict[str, FileTests]:
    listed = git.run_bytes("ls-tree", "-r", "-z", "--name-only", base).decode("utf-8", "replace")
    files = {}
    for path in filter(None, listed.split("\0")):
        if not _is_test_file(path, patterns):
            continue
        source = git.run_bytes("show", f"{base}:{path}").decode("utf-8", errors="replace")
        try:
            files[path] = analyse_source(source)
        except SyntaxError:
            files[path] = FileTests()
    return files


def _current_files(
    tree: Worktree, patterns: list[str]
) -> tuple[dict[str, FileTests], list[tuple[str, str]]]:
    git = project_git(tree.path)
    listed = git.run_bytes("ls-files", "-z", "--cached", "--others", "--exclude-standard")
    files: dict[str, FileTests] = {}
    unparsable: list[tuple[str, str]] = []
    for path in sorted(set(filter(None, listed.decode("utf-8", "replace").split("\0")))):
        file = tree.path / path
        if not _is_test_file(path, patterns) or not file.is_file():
            continue
        try:
            files[path] = analyse_source(file.read_text(errors="replace"))
        except SyntaxError as exc:
            unparsable.append((path, f"{exc.msg} (line {exc.lineno})"))
    return files, unparsable


def _ids(files: dict[str, FileTests]) -> dict[str, int]:
    return {
        f"{path}::{test}": cases
        for path, summary in files.items()
        for test, cases in summary.tests.items()
    }
