"""What every gate is given and what it returns (docs/SPEC.md section 11)."""

from dataclasses import dataclass

from control_plane.domain.gates import GateStatus
from control_plane.domain.project import ProjectConfig
from control_plane.workspace.worktree import Worktree


@dataclass(frozen=True)
class GateContext:
    tree: Worktree
    """The run worktree under check, with the base commit the run started from."""
    project: ProjectConfig
    """Read from the home repository, never from the worktree."""


@dataclass(frozen=True)
class GateResult:
    name: str
    status: GateStatus
    summary: str
    """One line: what the gate found."""
    output: str
    """The whole text of `gates/<attempt>/<name>.txt`, starting with the summary."""


def gate_result(name: str, status: GateStatus, summary: str, detail: str = "") -> GateResult:
    output = f"{name}: {status}: {summary}\n"
    if detail:
        output += "\n" + (detail if detail.endswith("\n") else detail + "\n")
    return GateResult(name=name, status=status, summary=summary, output=output)


def not_configured(name: str) -> GateResult:
    return gate_result(name, "skipped", f"not configured in project.yaml: {name} has no command")
