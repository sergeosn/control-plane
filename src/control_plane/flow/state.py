"""`flow.json` and `result.json` of a run (docs/SPEC.md sections 5 and 7).

`flow.json` is the state machine's position, rewritten atomically after every change: the
current step, the step being run and in which phase, the counters, what came back on failure
for the next attempt, and the approval the run waits for. With it and the step folders a run
resumes from inside any step after a park or a crash. Nothing waits in memory.
"""

from typing import Literal

from pydantic import Field

from control_plane.domain.base import DomainModel
from control_plane.store.runs import RunFolder

FLOW_STATE_FILE = "flow.json"

RunStatus = Literal["running", "parked", "done", "failed"]


class FlowError(Exception):
    """A run cannot be started or resumed. The message says why, in one line."""


class ActiveStep(DomainModel):
    """The step the run is inside. `started` is marked on disk before the step runs."""

    step: str = Field(min_length=1)
    seq: int = Field(ge=1)
    """The step folder number: `NN` of `steps/NN-<step>-<attempt>`."""
    attempt: int = Field(ge=1)
    phase: Literal["started", "parked"] = "started"
    approval_id: str | None = None
    """The entry an approval step raised."""
    gate_folder: int | None = None
    """The `gates/<attempt>/` folder a gate step writes; a folder is written once."""


class Returned(DomainModel):
    """What came back on failure, for the next agent step (section 7)."""

    kind: Literal["gate", "review", "denial", "answers"]
    ref: str
    """The gate folder number, the review's step folder, or the human's text."""


class PendingBudget(DomainModel):
    """A `budget` approval the run is parked on."""

    approval_id: str = Field(min_length=1)
    kind: Literal["iterations", "step"]
    """`iterations`: an iteration or visit limit; `step`: a step budget breach."""
    asked: str
    """Kept so a crash between saving this state and raising the entry can raise it again."""


class FlowState(DomainModel):
    run_id: str = Field(min_length=1)
    flow: str = Field(min_length=1)
    flow_version: int = Field(ge=1)
    status: RunStatus
    current: str = Field(min_length=1)
    seq: int = Field(ge=0, default=0)
    iteration: int = Field(ge=0, default=0)
    max_iterations: int = Field(ge=1)
    """The task's budget, raised by one for each `extend` of a `budget` approval."""
    visits: dict[str, int] = Field(default_factory=dict)
    attempts: dict[str, int] = Field(default_factory=dict)
    active: ActiveStep | None = None
    budget: PendingBudget | None = None
    latest: dict[str, str] = Field(default_factory=dict)
    """Artifact name (`plan`, `work_report`, `review`) to the step folder of its latest one."""
    gate_attempt: int | None = None
    """The number of the latest `gates/<attempt>/` folder."""
    returned: Returned | None = None
    reason: str | None = None
    """Why the run failed."""


class RunResult(DomainModel):
    """`result.json`: written when the run ends, which makes the run folder immutable."""

    state: Literal["done", "failed"]
    reason: str | None
    iterations: int


def read_flow_state(run: RunFolder) -> FlowState:
    path = run.path / FLOW_STATE_FILE
    if not path.is_file():
        raise FlowError(f"run {run.run_id} has no {FLOW_STATE_FILE}: it was never started")
    return FlowState.from_json(path.read_text(encoding="utf-8"))
