"""Deterministic checks (docs/SPEC.md section 11).

`run_gate` runs one gate by its name in the flow. Every gate reads its settings from the
project's `project.yaml`; the gates that read files work on the worktree and the base commit
only. The gates that read the diff run in a thread, since git is called synchronously.
"""

import asyncio

from control_plane.gates.base import GateContext, GateResult
from control_plane.gates.changes import diff_size_gate, secrets_gate
from control_plane.gates.commands import command_gate, coverage_gate, guardrail_gate
from control_plane.gates.integrity import integrity_gate

GATE_NAMES: tuple[str, ...] = (
    "format",
    "lint",
    "tests",
    "coverage",
    "test_integrity",
    "diff_size",
    "secrets",
    "guardrail_regression",
)


class UnknownGateError(ValueError):
    """A flow named a gate the engine does not have."""


def check_gate_names(names: list[str] | tuple[str, ...]) -> None:
    unknown = [name for name in names if name not in GATE_NAMES]
    if unknown:
        raise UnknownGateError(f"unknown gate {unknown[0]!r}; gates: {', '.join(GATE_NAMES)}")


async def run_gate(name: str, ctx: GateContext) -> GateResult:
    check_gate_names([name])
    gates = ctx.project.gates
    tree = ctx.tree
    match name:
        case "format" | "lint" | "tests":
            return await command_gate(name, getattr(gates, name), tree.path)
        case "coverage":
            return await coverage_gate(gates.coverage, tree.path)
        case "test_integrity":
            return await asyncio.to_thread(integrity_gate, gates.test_integrity, tree)
        case "diff_size":
            return await asyncio.to_thread(diff_size_gate, gates.diff_size, tree)
        case "secrets":
            return await asyncio.to_thread(secrets_gate, gates.secrets, tree)
        case _:
            return await guardrail_gate(ctx.project, tree.path)


__all__ = [
    "GATE_NAMES",
    "GateContext",
    "GateResult",
    "UnknownGateError",
    "check_gate_names",
    "run_gate",
]
