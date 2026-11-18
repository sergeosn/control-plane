"""The flow file and its validation at load (docs/SPEC.md section 7).

A flow is a YAML file of steps. Each step maps its outcomes to the next step under `next`
(not `on`, which YAML 1.1 reads as the boolean true). `load_flow` parses the file into
`Flow` and runs `validate_flow`, which reports every problem at once: a transition to an
unknown step, a step not reachable from `start`, an unreachable `done`, an agent step whose
role the lane does not define, and an outcome a step can emit without a transition. A flow
that fails validation is rejected before any run starts.
"""

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import Field, ValidationError

from control_plane.agent import ARTIFACTS
from control_plane.domain.base import DomainModel
from control_plane.domain.plan import Plan
from control_plane.domain.review import Review
from control_plane.domain.task import Task
from control_plane.domain.work_report import WorkReport
from control_plane.gates import UnknownGateError, check_gate_names

DONE = "done"
FAILED = "failed"
TERMINALS: tuple[str, ...] = (DONE, FAILED)

Produces = Literal["plan", "work_report", "review"]
PRODUCED_MODELS: dict[str, type[DomainModel]] = {
    "plan": Plan,
    "work_report": WorkReport,
    "review": Review,
}
PRODUCED_OUTCOMES: dict[str, tuple[str, ...]] = {
    "plan": ("ok", "blocked"),
    "work_report": ("ok",),
    "review": ("pass", "fail"),
}
"""An agent step emits what its artifact allows: a plan with questions is `blocked`, a review
is `pass` or `fail` by its derived verdict. A failed step is not an outcome: the engine
handles it (a budget breach parks for a `budget` approval, anything else fails the run)."""

APPROVAL_OUTCOMES: dict[str, tuple[str, ...]] = {
    "question": ("answered",),
    "plan": ("approve", "deny"),
    "merge": ("approve", "deny"),
}
"""The approval types a flow step can raise. `tool_call` and `budget` are raised by the
engine itself, not by a step."""

ACTIONS: tuple[str, ...] = ("merge_and_verify",)
ACTION_OUTCOMES: tuple[str, ...] = ("ok", "error")
GATE_OUTCOMES: tuple[str, ...] = ("pass", "fail")

CONDITION_PREFIX = "task."


class FlowValidationError(ValueError):
    """A flow file is malformed or fails validation. `problems` lists every reason."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("invalid flow: " + "; ".join(problems))


class _Step(DomainModel):
    next: dict[str, str] = Field(min_length=1)
    when: str | None = None
    """`task.<field>`: a boolean field of the task. False skips the step, which then emits
    the first outcome listed under `next`."""


class AgentStep(_Step):
    kind: Literal["agent"]
    role: str = Field(min_length=1)
    produces: Produces


class GateStep(_Step):
    kind: Literal["gate"]
    gates: list[str] = Field(min_length=1)


class ApprovalStep(_Step):
    kind: Literal["approval"]
    type: Literal["question", "plan", "merge"]


class ActionStep(_Step):
    kind: Literal["action"]
    action: str = Field(min_length=1)


Step = Annotated[AgentStep | GateStep | ApprovalStep | ActionStep, Field(discriminator="kind")]


class FlowLimits(DomainModel):
    max_iterations: int = Field(ge=1)
    max_cost_usd: float = Field(gt=0)
    max_wall_minutes: int = Field(ge=1)


class Flow(DomainModel):
    name: str = Field(min_length=1)
    version: int = Field(ge=1)
    start: str = Field(min_length=1)
    limits: FlowLimits
    steps: dict[str, Step] = Field(min_length=1)


def outcomes_of(step: Step) -> tuple[str, ...]:
    """Every outcome `step` can emit, each of which needs a transition."""
    match step:
        case AgentStep():
            return PRODUCED_OUTCOMES[step.produces]
        case GateStep():
            return GATE_OUTCOMES
        case ApprovalStep():
            return APPROVAL_OUTCOMES[step.type]
        case ActionStep():
            return ACTION_OUTCOMES


def is_iteration_step(step: Step) -> bool:
    """A step whose every re-entry increments the iteration counter: the worker's step."""
    return isinstance(step, AgentStep) and step.produces == "work_report"


def task_conditions() -> tuple[str, ...]:
    """The conditions `when` may take: `task.<field>` for each boolean task field."""
    return tuple(
        f"{CONDITION_PREFIX}{name}"
        for name, info in Task.model_fields.items()
        if info.annotation is bool
    )


def load_flow(path: Path, roles: frozenset[str]) -> Flow:
    """Parse and validate the flow file at `path` for a lane defining `roles`."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise FlowValidationError([f"{path.name}: not valid YAML: {exc}"]) from exc
    if not isinstance(data, dict):
        raise FlowValidationError([f"{path.name}: must be a YAML mapping"])
    try:
        flow = Flow.model_validate(data)
    except ValidationError as exc:
        raise FlowValidationError(
            [
                f"{'.'.join(str(part) for part in error['loc']) or 'flow'}: {error['msg']}"
                for error in exc.errors()
            ]
        ) from exc
    validate_flow(flow, roles)
    return flow


def validate_flow(flow: Flow, roles: frozenset[str]) -> None:
    """Raise `FlowValidationError` with every problem of `flow`; return if there is none."""
    problems: list[str] = []
    if flow.start not in flow.steps:
        problems.append(f"start {flow.start!r} is not a step")
    for name, step in flow.steps.items():
        problems.extend(_step_problems(name, step, flow, roles))
    reachable = _reachable(flow)
    for name in flow.steps:
        if name not in reachable and name not in TERMINALS:
            problems.append(f"step {name!r} is not reachable from start {flow.start!r}")
    if flow.start in flow.steps and DONE not in reachable:
        problems.append(f"{DONE!r} is not reachable from start {flow.start!r}")
    if problems:
        raise FlowValidationError(problems)


def _step_problems(name: str, step: Step, flow: Flow, roles: frozenset[str]) -> list[str]:
    where = f"step {name!r}"
    problems: list[str] = []
    if name in TERMINALS:
        problems.append(f"{where}: {DONE!r} and {FAILED!r} are terminal and cannot be steps")
    emits = outcomes_of(step)
    for outcome, target in step.next.items():
        if outcome not in emits:
            problems.append(f"{where}: outcome {outcome!r} is not one it can emit")
        if target not in flow.steps and target not in TERMINALS:
            problems.append(f"{where}: outcome {outcome!r} goes to unknown step {target!r}")
    problems.extend(
        f"{where}: outcome {outcome!r} has no transition"
        for outcome in emits
        if outcome not in step.next
    )
    if step.when is not None and step.when not in task_conditions():
        problems.append(
            f"{where}: when {step.when!r} is not a boolean task field;"
            f" fields: {', '.join(task_conditions())}"
        )
    match step:
        case AgentStep():
            if step.role not in roles:
                problems.append(f"{where}: role {step.role!r} is not defined by the lane")
            elif step.role in ARTIFACTS and ARTIFACTS[step.role] is not PRODUCED_MODELS.get(
                step.produces
            ):
                own = next(k for k, v in PRODUCED_MODELS.items() if v is ARTIFACTS[step.role])
                problems.append(f"{where}: role {step.role!r} produces {own}, not {step.produces}")
        case GateStep():
            try:
                check_gate_names(step.gates)
            except UnknownGateError as exc:
                problems.append(f"{where}: {exc}")
        case ActionStep():
            if step.action not in ACTIONS:
                problems.append(
                    f"{where}: unknown action {step.action!r}; actions: {', '.join(ACTIONS)}"
                )
        case _:
            pass
    return problems


def _reachable(flow: Flow) -> set[str]:
    """Every step and terminal reachable from `start` along transitions."""
    if flow.start not in flow.steps:
        return set()
    seen = {flow.start}
    queue = [flow.start]
    while queue:
        step = flow.steps.get(queue.pop())
        if step is None:
            continue
        for target in step.next.values():
            if target not in seen:
                seen.add(target)
                queue.append(target)
    return seen
