"""Running the checks of one `implement` attempt (docs/SPEC.md sections 5 and 11).

`run_checks` runs every gate the flow's `gate` step lists, in order and all of them even
after one fails, so the next attempt sees every problem at once. It writes one output file
per gate to `gates/<attempt>/<name>.txt` and the outcome to `gates/<attempt>/results.json`.
A gate's folder is written once; a new attempt gets a new number.

It also runs the `scope` check, before any command can leave files in the worktree. Scope
never fails the checks: its out-of-plan files are recorded in `results.json` for the
reviewer's context (`StepInputs.out_of_plan`).
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

from control_plane.domain.gates import GateRecord, GateReport
from control_plane.domain.plan import Plan
from control_plane.domain.project import ProjectConfig
from control_plane.gates import GateContext, check_gate_names, run_gate
from control_plane.gates.scope import out_of_plan
from control_plane.store.runs import RunFolder
from control_plane.workspace.diff import changed_files
from control_plane.workspace.worktree import Worktree

GATES_DIR = "gates"
RESULTS_FILE = "results.json"


class GatesExistError(Exception):
    """The checks of this attempt were already written."""


@dataclass(frozen=True)
class Checks:
    report: GateReport
    outputs: dict[str, str]
    """Gate name to the text of its output file."""

    def failed_outputs(self) -> dict[str, str]:
        """The outputs of the failing gates: what the next `implement` attempt is given."""
        return {name: self.outputs[name] for name in self.report.failed}


async def run_checks(
    run: RunFolder,
    *,
    attempt: int,
    tree: Worktree,
    project: ProjectConfig,
    gates: Sequence[str],
    plan: Plan,
) -> Checks:
    check_gate_names(list(gates))
    if attempt < 1:
        raise ValueError(f"attempt must be 1 or more, not {attempt}")
    folder = f"{GATES_DIR}/{attempt}"
    if (run.path / folder).exists():
        raise GatesExistError(
            f"the checks of attempt {attempt} of run {run.run_id} are already written"
        )

    changes = await asyncio.to_thread(changed_files, tree)
    scope = out_of_plan(plan.files_to_touch, changes)
    ctx = GateContext(tree=tree, project=project)
    results = [await run_gate(name, ctx) for name in gates]

    records = []
    for result in results:
        output_file = f"{folder}/{result.name}.txt"
        run.write_bytes(output_file, result.output.encode("utf-8"))
        records.append(
            GateRecord(
                name=result.name,
                status=result.status,
                summary=result.summary,
                output_file=output_file,
            )
        )
    failed = any(result.status == "fail" for result in results)
    report = GateReport(
        attempt=attempt,
        outcome="fail" if failed else "pass",
        gates=records,
        out_of_plan=scope,
    )
    run.write_json(f"{folder}/{RESULTS_FILE}", report)
    return Checks(report=report, outputs={result.name: result.output for result in results})
