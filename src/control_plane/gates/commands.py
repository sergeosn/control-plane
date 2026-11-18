"""The gates that run a command: `format`, `lint`, `tests`, `coverage`, `guardrail_regression`.

Each command comes from `project.yaml` and runs through the same runner as `run_command`
(docs/SPEC.md section 10.2): an argv list, no shell, a timeout, an environment built from an
allowlist. The gates add `PYTHONDONTWRITEBYTECODE`, so checking the worktree does not leave
bytecode in it, and the coverage gate keeps its data file and report outside the worktree.
"""

import json
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from control_plane.domain.project import (
    REPORT_PLACEHOLDER,
    CommandGate,
    CoverageGate,
    ProjectConfig,
)
from control_plane.gates.base import GateResult, gate_result, not_configured
from control_plane.tools.commands import CommandRunner
from control_plane.tools.errors import ToolError
from control_plane.tools.truncate import head_and_tail

STREAM_LIMIT = 100_000
"""Characters kept per output stream; the head and the tail survive, the cut is announced."""
_GATE_ENV = {"PYTHONDONTWRITEBYTECODE": "1"}


@dataclass(frozen=True)
class _Ran:
    ok: bool
    summary: str
    output: str


async def command_gate(name: str, gate: CommandGate | None, cwd: Path) -> GateResult:
    """Pass when `gate.argv` exits 0 in `cwd`."""
    if gate is None:
        return not_configured(name)
    ran = await _execute(gate.argv, cwd, gate.timeout_s, _GATE_ENV)
    return gate_result(name, "pass" if ran.ok else "fail", ran.summary, ran.output)


async def coverage_gate(gate: CoverageGate | None, cwd: Path) -> GateResult:
    """Pass when the total of the coverage report is at or above the project threshold."""
    name = "coverage"
    if gate is None:
        return not_configured(name)
    with tempfile.TemporaryDirectory(prefix="cplane-coverage-") as scratch:
        report = Path(scratch) / "coverage.json"
        argv = [part.replace(REPORT_PLACEHOLDER, str(report)) for part in gate.argv]
        env = {**_GATE_ENV, "COVERAGE_FILE": str(Path(scratch) / ".coverage")}
        ran = await _execute(argv, cwd, gate.timeout_s, env)
        if not report.is_file():
            return gate_result(name, "fail", f"no coverage report: {ran.summary}", ran.output)
        try:
            percent = float(json.loads(report.read_text())["totals"]["percent_covered"])
        except (ValueError, KeyError, TypeError) as exc:
            summary = f"coverage report unreadable ({type(exc).__name__}: {exc})"
            return gate_result(name, "fail", summary, ran.output)
    measured = f"coverage {percent:.2f}%"
    if percent < gate.threshold:
        summary = f"{measured} is under the threshold {gate.threshold:.2f}%"
        return gate_result(name, "fail", summary, ran.output)
    return gate_result(name, "pass", f"{measured} (threshold {gate.threshold:.2f}%)", ran.output)


async def guardrail_gate(project: ProjectConfig, worktree: Path) -> GateResult:
    """Run the control checkout's guardrail suite against the worktree's source.

    The command runs in the control checkout, so its tests, conftest files and fixtures are
    the ones the agent could not touch. The worktree's source folder is the first entry of
    `PYTHONPATH`, ahead of any installed copy of the package, and `PYTHONSAFEPATH` keeps
    `python -m` from putting the control checkout's own directory ahead of it.
    """
    name = "guardrail_regression"
    gate = project.gates.guardrail_regression
    control = project.guardrail_control()
    if gate is None or control is None:
        return not_configured(name)
    if not control.is_dir():
        return gate_result(name, "fail", f"no control checkout at {control}")
    source = worktree / gate.source
    if not source.is_dir():
        return gate_result(name, "fail", f"the worktree has no source folder {gate.source}")
    env = {**_GATE_ENV, "PYTHONPATH": str(source), "PYTHONSAFEPATH": "1"}
    ran = await _execute(gate.argv, control, gate.timeout_s, env)
    summary = f"control suite in {control} against {source}: {ran.summary}"
    return gate_result(name, "pass" if ran.ok else "fail", summary, ran.output)


async def _execute(argv: Sequence[str], cwd: Path, timeout: float, env: Mapping[str, str]) -> _Ran:
    shown = " ".join(argv)
    try:
        result = await CommandRunner(cwd, extra_env=env).run(argv, timeout=timeout)
    except ToolError as exc:
        return _Ran(False, f"`{shown}` could not start: {exc}", f"$ {shown}\n{exc}\n")
    if result.timed_out:
        summary = f"`{shown}` timed out after {timeout:g}s"
    elif result.exit_code == 0:
        summary = f"`{shown}` passed"
    else:
        summary = f"`{shown}` failed with exit code {result.exit_code}"
    output = (
        f"$ {shown}\n"
        f"--- stdout ---\n{head_and_tail(result.stdout, limit=STREAM_LIMIT)}\n"
        f"--- stderr ---\n{head_and_tail(result.stderr, limit=STREAM_LIMIT)}\n"
    )
    return _Ran(not result.timed_out and result.exit_code == 0, summary, output)
