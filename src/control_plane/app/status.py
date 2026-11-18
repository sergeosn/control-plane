"""`cplane status [<run-id>]`: state, current step, iteration and cost (docs/SPEC.md section 14).

Read from the run folders, the source of truth: `result.json` for a finished run, `flow.json`
for one in progress, and every step's `llm_calls.jsonl` for the cost. This module only reads.
"""

import json
from dataclasses import dataclass

from control_plane.domain import LLMCall
from control_plane.flow import FLOW_STATE_FILE, read_flow_state
from control_plane.store.files import read_jsonl
from control_plane.store.home import Home
from control_plane.store.runs import (
    LLM_CALLS_FILE,
    MANIFEST_FILE,
    RESULT_FILE,
    STEPS_DIR,
    RunFolder,
    RunStore,
)


class StatusError(Exception):
    """The run asked for does not exist."""


@dataclass(frozen=True)
class RunStatus:
    run_id: str
    task_id: str | None
    state: str
    """`created` (never started), `running`, `parked`, `done` or `failed`."""
    step: str | None
    iteration: int
    max_iterations: int | None
    cost_usd: float
    approval_id: str | None
    """The undecided approval the run waits for, if any."""


def run_statuses(home: Home, run_id: str | None = None) -> list[RunStatus]:
    store = RunStore(home)
    if run_id is not None:
        try:
            return [_status(store.open(run_id))]
        except FileNotFoundError:
            raise StatusError(f"no run {run_id} in {home.path}") from None
    return [_status(store.open(name)) for name in store.list_ids()]


def _status(run: RunFolder) -> RunStatus:
    task_id = None
    manifest = run.path / MANIFEST_FILE
    if manifest.is_file():
        task_id = json.loads(manifest.read_text(encoding="utf-8")).get("task_id")
    state, step, iteration, limit = "created", None, 0, None
    if (run.path / FLOW_STATE_FILE).is_file():
        flow = read_flow_state(run)
        state, step, iteration, limit = (
            flow.status,
            flow.current,
            flow.iteration,
            flow.max_iterations,
        )
    if run.finished:
        state = json.loads((run.path / RESULT_FILE).read_text(encoding="utf-8"))["state"]
    pending = {}
    for approval in run.approvals():
        pending[approval.id] = approval
    waiting = [a.id for a in pending.values() if not a.decided]
    return RunStatus(
        run_id=run.run_id,
        task_id=task_id,
        state=state,
        step=step,
        iteration=iteration,
        max_iterations=limit,
        cost_usd=_cost(run),
        approval_id=waiting[-1] if waiting and state == "parked" else None,
    )


def _cost(run: RunFolder) -> float:
    steps = run.path / STEPS_DIR
    if not steps.is_dir():
        return 0.0
    return sum(
        call.cost_usd
        for step in sorted(steps.iterdir())
        for call in read_jsonl(step / LLM_CALLS_FILE, LLMCall)
    )
