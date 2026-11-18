"""The flow engine: the state machine of docs/SPEC.md section 7, with persistence and resume.

The engine knows the flow, the task and the run folder, and nothing about models or tools.
An agent step is handed to an `Executor` (section 8.1); the context, the gates and the
actions come through `StepPorts`, which the application layer implements over the context
builder, `run_checks` and `merge_and_verify`. The engine decides what runs next, what each
step is given, when the run parks and when it ends.

Persistence: `flow.json` is rewritten after every change, and a step is marked `started` on
disk before it runs, so `resume` continues from inside any step kind:

- agent: a finished step folder is read back, a started one is resumed by the executor, and
  one that never wrote its `state.json` is run;
- gate: results already on disk are used, a folder without results gets a new attempt;
- approval: the latest line of its entry decides; undecided changes nothing;
- action: never run twice. An action that started and did not finish emits `error` with a
  message that its effect is unknown, as an interrupted tool call does (section 12).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from control_plane.agent import (
    ARTIFACTS,
    REASON_BUDGET,
    STATE_FILE,
    Executor,
    StepContext,
    StepResult,
    StepState,
    approval_id,
)
from control_plane.app.gates import RESULTS_FILE
from control_plane.context.builder import StepInputs
from control_plane.context.recipes import RECIPES
from control_plane.domain.approval import Approval, ApprovalType
from control_plane.domain.base import DomainModel
from control_plane.domain.gates import GateReport
from control_plane.domain.plan import Plan
from control_plane.domain.review import Review
from control_plane.domain.task import Task
from control_plane.domain.work_report import WorkReport
from control_plane.flow.schema import (
    DONE,
    TERMINALS,
    ActionStep,
    AgentStep,
    ApprovalStep,
    Flow,
    GateStep,
    Step,
    is_iteration_step,
)
from control_plane.flow.state import (
    FLOW_STATE_FILE,
    ActiveStep,
    FlowError,
    FlowState,
    PendingBudget,
    Returned,
    RunResult,
    read_flow_state,
)
from control_plane.policy.protected import PathPatterns
from control_plane.store.runs import ARTIFACT_FILE, STEPS_DIR, RunFolder

GATES_DIR = "gates"
_GLOB_CHARS = frozenset("*?[")

Clock = Callable[[], datetime]


def _wall_clock() -> datetime:
    return datetime.now(UTC)


# --- what the engine asks of the application layer --------------------------------


@dataclass(frozen=True)
class AgentRequest:
    run: RunFolder
    step: str
    role: str
    seq: int
    attempt: int
    inputs: StepInputs
    """The step-specific inputs the role's recipe allows. The engine has no diff: the port
    adds `diff` for the reviewer."""
    resuming: bool
    """True when the step was started before: its context may already be written."""

    @property
    def folder(self) -> str:
        return f"{self.seq:02d}-{self.step}-{self.attempt}"


@dataclass(frozen=True)
class PreparedAgent:
    context: StepContext
    executor: Executor
    """Built per step, since a step's path jail reads that step's context directory."""


@dataclass(frozen=True)
class GateRequest:
    run: RunFolder
    step: str
    attempt: int
    """The gate folder: `gates/<attempt>/`."""
    gates: tuple[str, ...]
    plan: Plan | None


@dataclass(frozen=True)
class ActionRequest:
    run: RunFolder
    step: str
    action: str


@dataclass(frozen=True)
class ActionResult:
    outcome: Literal["ok", "error"]
    message: str


class StepPorts(Protocol):
    async def prepare_agent(self, request: AgentRequest) -> PreparedAgent: ...

    async def run_gates(self, request: GateRequest) -> GateReport: ...

    async def run_action(self, request: ActionRequest) -> ActionResult: ...

    def changed_files(self, run: RunFolder) -> list[str]:
        """The worktree's changed paths, for the protected files of the merge approval."""
        ...


@dataclass(frozen=True)
class FlowReport:
    status: Literal["parked", "done", "failed"]
    step: str
    message: str
    approval_id: str | None = None


# --- the engine ------------------------------------------------------------------------


class FlowEngine:
    def __init__(
        self,
        flow: Flow,
        *,
        task: Task,
        ports: StepPorts,
        protected: PathPatterns | None = None,
        clock: Clock = _wall_clock,
    ) -> None:
        self.flow = flow
        self.task = task
        self.ports = ports
        self.protected = protected or PathPatterns(())
        self.clock = clock

    async def start(self, run: RunFolder) -> FlowReport:
        """Run the flow from `start` until the run parks or ends."""
        if (run.path / FLOW_STATE_FILE).exists():
            raise FlowError(f"run {run.run_id} was already started; resume it instead")
        state = FlowState(
            run_id=run.run_id,
            flow=self.flow.name,
            flow_version=self.flow.version,
            status="running",
            current=self.flow.start,
            max_iterations=self.task.budget.max_iterations,
        )
        self._count_entry(state, self.flow.start)
        self._save(run, state)
        run.log_event(
            "run_started",
            step=self.flow.start,
            message=f"flow {self.flow.name} v{self.flow.version}, task {self.task.id}",
        )
        return await self._advance(run, state)

    async def resume(self, run: RunFolder) -> FlowReport:
        """Continue a parked or interrupted run from `flow.json` and its step folders."""
        state = read_flow_state(run)
        if state.flow != self.flow.name:
            raise FlowError(f"run {run.run_id} follows flow {state.flow!r}, not {self.flow.name!r}")
        if state.status in ("done", "failed"):
            if run.finished:
                raise FlowError(f"run {run.run_id} is finished: {state.status}")
            return self._finish(run, state)
        if state.budget is not None:
            report = self._settle_budget(run, state)
            if report is not None:
                return report
        return await self._advance(run, state)

    # --- the loop ------------------------------------------------------------------

    async def _advance(self, run: RunFolder, state: FlowState) -> FlowReport:
        while state.current not in TERMINALS:
            name = state.current
            step = self.flow.steps[name]
            resuming = state.active is not None
            if not resuming:
                breach = self._over_budget(state, name)
                if breach is not None:
                    return self._park_budget(run, state, "iterations", breach, [FLOW_STATE_FILE])
                if step.when is not None and not self._condition(run, state, step):
                    outcome = next(iter(step.next))
                    run.log_event("step_skipped", step=name, outcome=outcome, message=step.when)
                    self._transition(run, state, name, None, outcome)
                    continue
                state.seq += 1
                attempt = state.attempts.get(name, 0) + 1
                state.attempts[name] = attempt
                state.active = ActiveStep(step=name, seq=state.seq, attempt=attempt)
                self._save(run, state)
            result = await self._run(run, state, step, resuming)
            if isinstance(result, FlowReport):
                return result
            active = state.active
            self._transition(run, state, name, active.attempt if active else None, result)
        return self._finish(run, state)

    async def _run(
        self, run: RunFolder, state: FlowState, step: Step, resuming: bool
    ) -> str | FlowReport:
        match step:
            case AgentStep():
                return await self._run_agent(run, state, step, resuming)
            case GateStep():
                return await self._run_gate(run, state, step)
            case ApprovalStep():
                return self._run_approval(run, state, step)
            case ActionStep():
                return await self._run_action(run, state, step, resuming)

    def _transition(
        self, run: RunFolder, state: FlowState, name: str, attempt: int | None, outcome: str
    ) -> None:
        try:
            target = self.flow.steps[name].next[outcome]
        except KeyError:
            raise FlowError(f"step {name!r} emitted {outcome!r}, which has no transition") from None
        run.log_event("transition", step=name, attempt=attempt, outcome=outcome, message=target)
        state.active = None
        state.current = target
        state.status = "running"
        if target not in TERMINALS:
            self._count_entry(state, target)
        self._save(run, state)

    def _count_entry(self, state: FlowState, name: str) -> None:
        state.visits[name] = state.visits.get(name, 0) + 1
        if is_iteration_step(self.flow.steps[name]):
            state.iteration += 1

    def _over_budget(self, state: FlowState, name: str) -> str | None:
        """Why entering `name` would exceed the run's iteration budget, if it would."""
        step = self.flow.steps[name]
        if is_iteration_step(step):
            if state.iteration > state.max_iterations:
                return (
                    f"iteration {state.iteration} of step {name!r} exceeds max_iterations "
                    f"{state.max_iterations}; extend by one iteration or end the run"
                )
            return None
        visits, limit = state.visits.get(name, 0), state.max_iterations + 1
        if visits > limit:
            return (
                f"step {name!r} was entered {visits} times, more than max_iterations + 1 = "
                f"{limit}; extend by one iteration or end the run"
            )
        return None

    def _condition(self, run: RunFolder, state: FlowState, step: Step) -> bool:
        """The step's `when`; a plan touching a protected path forces the plan approval."""
        assert step.when is not None
        if getattr(self.task, step.when.removeprefix("task.")):
            return True
        return (
            isinstance(step, ApprovalStep)
            and step.type == "plan"
            and bool(self._protected_in_plan(run, state))
        )

    def _finish(self, run: RunFolder, state: FlowState) -> FlowReport:
        status: Literal["done", "failed"] = "done" if state.current == DONE else "failed"
        state.status = status
        state.active = None
        self._save(run, state)
        run.log_event("run_finished", outcome=status, message=state.reason)
        run.finish(RunResult(state=status, reason=state.reason, iterations=state.iteration))
        return FlowReport(status=status, step=state.current, message=state.reason or status)

    def _fail(self, run: RunFolder, state: FlowState, reason: str) -> FlowReport:
        state.reason = reason
        state.current = "failed"
        return self._finish(run, state)

    def _save(self, run: RunFolder, state: FlowState) -> None:
        run.write_json(FLOW_STATE_FILE, state)

    # --- agent steps ---------------------------------------------------------------

    async def _run_agent(
        self, run: RunFolder, state: FlowState, step: AgentStep, resuming: bool
    ) -> str | FlowReport:
        active = _active(state)
        request = AgentRequest(
            run=run,
            step=active.step,
            role=step.role,
            seq=active.seq,
            attempt=active.attempt,
            inputs=self._inputs(run, state, step.role),
            resuming=resuming,
        )
        folder = f"{STEPS_DIR}/{request.folder}"
        state_path = run.path / folder / STATE_FILE
        on_disk = StepState.from_json(state_path.read_text()) if state_path.is_file() else None
        if on_disk is not None and on_disk.status == "finished":
            result = _finished_result(run, folder, on_disk)
        else:
            prepared = await self.ports.prepare_agent(request)
            if on_disk is None:
                result = await prepared.executor.run_step(prepared.context)
            else:
                result = await prepared.executor.resume_step(prepared.context)

        if result.outcome == "parked":
            if state.status != "parked" or active.phase != "parked":
                active.phase, state.status = "parked", "parked"
                self._save(run, state)
            waiting = next((c.approval_id for c in result.pending if c.approval_id), None)
            return FlowReport("parked", active.step, result.message, approval_id=waiting)
        if result.outcome == "failed":
            if result.reason == REASON_BUDGET:
                asked = (
                    f"step {active.step!r} attempt {active.attempt}: {REASON_BUDGET}: "
                    f"{result.message}; extend to run the step again with a fresh budget, or "
                    "end the run"
                )
                return self._park_budget(run, state, "step", asked, [f"{folder}/{STATE_FILE}"])
            return self._fail(
                run, state, f"step {active.step!r} failed: {result.reason}: {result.message}"
            )

        state.latest[step.produces] = request.folder
        state.returned = None
        if isinstance(result.artifact, Review):
            if result.artifact.verdict == "fail":
                state.returned = Returned(kind="review", ref=request.folder)
            return result.artifact.verdict
        return result.outcome

    def _inputs(self, run: RunFolder, state: FlowState, role: str) -> StepInputs:
        """What comes back on failure, and the latest artifacts, as the role's recipe allows."""
        values: dict[str, object] = {}
        plan = self._artifact(run, state, "plan", Plan)
        if plan is not None:
            values["plan"] = values["previous_plan"] = plan
        report = self._artifact(run, state, "work_report", WorkReport)
        if report is not None:
            values["work_report"] = report
        gates = self._gate_report(run, state.gate_attempt)
        returned = state.returned
        if returned is None:
            if gates is not None:
                values["gate_outputs"] = _gate_outputs(run, gates, failed_only=False)
        elif returned.kind == "gate":
            failed = self._gate_report(run, int(returned.ref))
            if failed is not None:
                values["gate_outputs"] = _gate_outputs(run, failed, failed_only=True)
        elif returned.kind == "review":
            values["review"] = _read(run, returned.ref, Review)
        elif returned.kind == "denial":
            values["denial_reason"] = returned.ref
        else:
            values["answers"] = returned.ref
        if gates is not None:
            values["out_of_plan"] = tuple(gates.out_of_plan)
        recipe = RECIPES.get(role)
        allowed = (*recipe.required, *recipe.optional) if recipe else ()
        return StepInputs(**{name: v for name, v in values.items() if name in allowed})

    def _artifact[M: DomainModel](
        self, run: RunFolder, state: FlowState, name: str, model: type[M]
    ) -> M | None:
        folder = state.latest.get(name)
        return None if folder is None else _read(run, folder, model)

    def _gate_report(self, run: RunFolder, attempt: int | None) -> GateReport | None:
        if attempt is None:
            return None
        path = run.path / GATES_DIR / str(attempt) / RESULTS_FILE
        return GateReport.from_json(path.read_text()) if path.is_file() else None

    # --- gate steps ----------------------------------------------------------------

    async def _run_gate(self, run: RunFolder, state: FlowState, step: GateStep) -> str:
        active = _active(state)
        report: GateReport | None = None
        if active.gate_folder is None:
            active.gate_folder = state.gate_attempt = (state.gate_attempt or 0) + 1
            self._save(run, state)
        elif (run.path / GATES_DIR / str(active.gate_folder)).exists():
            report = self._gate_report(run, active.gate_folder)
            if report is None:
                # A crash left a gate folder without results; a gate folder is written once.
                active.gate_folder = state.gate_attempt = (state.gate_attempt or 0) + 1
                self._save(run, state)
        if report is None:
            report = await self.ports.run_gates(
                GateRequest(
                    run=run,
                    step=active.step,
                    attempt=active.gate_folder,
                    gates=tuple(step.gates),
                    plan=self._artifact(run, state, "plan", Plan),
                )
            )
        if report.outcome == "fail":
            state.returned = Returned(kind="gate", ref=str(active.gate_folder))
        return report.outcome

    # --- approval steps --------------------------------------------------------------

    def _run_approval(
        self, run: RunFolder, state: FlowState, step: ApprovalStep
    ) -> str | FlowReport:
        active = _active(state)
        if active.approval_id is None:
            active.approval_id = self._next_approval_id(run)
            active.phase, state.status = "parked", "parked"
            self._save(run, state)
        entry = _latest_line(run, active.approval_id)
        if entry is None:
            asked, evidence = self._describe(run, state, step)
            self._raise(run, active.approval_id, step.type, asked, evidence)
            run.log_event(
                "run_parked",
                step=active.step,
                attempt=active.attempt,
                message=f"waiting for {step.type} approval {active.approval_id}",
            )
            return FlowReport(
                "parked",
                active.step,
                f"waiting for {step.type} approval {active.approval_id}",
                approval_id=active.approval_id,
            )
        if not entry.decided:
            return FlowReport(
                "parked",
                active.step,
                f"approval {entry.id} is undecided",
                approval_id=entry.id,
            )
        run.log_event("run_resumed", step=active.step, attempt=active.attempt, outcome=entry.answer)
        assert entry.answer is not None
        if step.type == "question":
            state.returned = Returned(kind="answers", ref=entry.answer)
            return "answered"
        if entry.answer == "deny":
            state.returned = Returned(kind="denial", ref=entry.note or "")
        return entry.answer

    def _describe(
        self, run: RunFolder, state: FlowState, step: ApprovalStep
    ) -> tuple[str, list[str]]:
        plan = self._artifact(run, state, "plan", Plan)
        plan_file = _artifact_path(state, "plan")
        if step.type == "question":
            questions = "; ".join(plan.questions) if plan else "the plan has questions"
            return f"The planner asks: {questions}", _present(plan_file)
        if step.type == "plan":
            protected = [f"protected: {p}" for p in self._protected_in_plan(run, state)]
            summary = plan.summary if plan else "no plan"
            return (
                f"Approve the plan for {self.task.id}: {summary}",
                [*protected, *_present(plan_file)],
            )
        changed = self.protected.touched(self.ports.changed_files(run))
        evidence = [f"protected: {path}" for path in changed]
        evidence += _present(_artifact_path(state, "work_report"), _artifact_path(state, "review"))
        if state.gate_attempt is not None:
            evidence.append(f"{GATES_DIR}/{state.gate_attempt}/{RESULTS_FILE}")
        return f"Merge branch run/{run.run_id} of {self.task.id} into main", evidence

    def _protected_in_plan(self, run: RunFolder, state: FlowState) -> list[str]:
        """Protected paths the plan's `files_to_touch` names, or holds in a directory."""
        plan = self._artifact(run, state, "plan", Plan)
        if plan is None or not self.protected:
            return []
        found = set(self.protected.touched(plan.files_to_touch))
        entries = PathPatterns(plan.files_to_touch)
        found.update(
            pattern
            for pattern in self.protected.patterns
            if _GLOB_CHARS.isdisjoint(pattern) and entries.matches(pattern)
        )
        return sorted(found)

    # --- action steps ----------------------------------------------------------------

    async def _run_action(
        self, run: RunFolder, state: FlowState, step: ActionStep, resuming: bool
    ) -> str:
        active = _active(state)
        if resuming:
            state.reason = (
                f"action {step.action} of step {active.step!r} was interrupted and its effect is "
                "unknown; check the primary checkout before running the task again"
            )
            return "error"
        result = await self.ports.run_action(
            ActionRequest(run=run, step=active.step, action=step.action)
        )
        if result.outcome == "error":
            state.reason = result.message
        return result.outcome

    # --- budget approvals --------------------------------------------------------------

    def _park_budget(
        self,
        run: RunFolder,
        state: FlowState,
        kind: Literal["iterations", "step"],
        asked: str,
        evidence: list[str],
    ) -> FlowReport:
        entry_id = self._next_approval_id(run)
        state.budget = PendingBudget(approval_id=entry_id, kind=kind, asked=asked)
        state.status = "parked"
        self._save(run, state)
        self._raise(run, entry_id, "budget", asked, evidence)
        run.log_event("run_parked", step=state.current, message=f"budget approval {entry_id}")
        return FlowReport("parked", state.current, asked, approval_id=entry_id)

    def _settle_budget(self, run: RunFolder, state: FlowState) -> FlowReport | None:
        """Apply a decided `budget` approval; a report while the run stays parked."""
        budget = state.budget
        assert budget is not None
        entry = _latest_line(run, budget.approval_id)
        if entry is None:
            self._raise(run, budget.approval_id, "budget", budget.asked, [FLOW_STATE_FILE])
            entry = _latest_line(run, budget.approval_id)
        assert entry is not None
        if not entry.decided:
            return FlowReport(
                "parked", state.current, f"approval {entry.id} is undecided", entry.id
            )
        run.log_event("run_resumed", step=state.current, outcome=entry.answer, message=entry.id)
        state.budget = None
        if entry.answer == "end":
            return self._fail(run, state, "budget approval ended the run")
        if budget.kind == "iterations":
            state.max_iterations += 1
        else:
            # A rerun of the step with a fresh budget: a new attempt, not a new iteration.
            state.active = None
        state.status = "running"
        self._save(run, state)
        return None

    def _next_approval_id(self, run: RunFolder) -> str:
        return approval_id(run.run_id, len({a.id for a in run.approvals()}) + 1)

    def _raise(
        self, run: RunFolder, entry_id: str, kind: ApprovalType, asked: str, evidence: list[str]
    ) -> None:
        run.append_approval(
            Approval(
                id=entry_id,
                run_id=run.run_id,
                type=kind,
                asked=asked,
                evidence=evidence,
                created_at=self.clock(),
            )
        )


# --- helpers -----------------------------------------------------------------------------


def _active(state: FlowState) -> ActiveStep:
    if state.active is None:
        raise FlowError(f"run {state.run_id} is not inside a step")
    return state.active


def _read[M: DomainModel](run: RunFolder, folder: str, model: type[M]) -> M:
    path = run.path / STEPS_DIR / folder / ARTIFACT_FILE
    return model.from_json(path.read_text(encoding="utf-8"))


def _artifact_path(state: FlowState, name: str) -> str | None:
    folder = state.latest.get(name)
    return None if folder is None else f"{STEPS_DIR}/{folder}/{ARTIFACT_FILE}"


def _present(*paths: str | None) -> list[str]:
    return [path for path in paths if path is not None]


def _latest_line(run: RunFolder, entry_id: str) -> Approval | None:
    latest = None
    for approval in run.approvals():
        if approval.id == entry_id:
            latest = approval
    return latest


def _gate_outputs(run: RunFolder, report: GateReport, *, failed_only: bool) -> dict[str, str]:
    return {
        gate.name: (run.path / gate.output_file).read_text(encoding="utf-8", errors="replace")
        for gate in report.gates
        if not failed_only or gate.status == "fail"
    }


def _finished_result(run: RunFolder, folder: str, on_disk: StepState) -> StepResult:
    """The result of a step that finished before the engine could act on it."""
    artifact = None
    if on_disk.outcome in ("ok", "blocked"):
        model = ARTIFACTS[on_disk.role]
        artifact = model.from_json((run.path / folder / ARTIFACT_FILE).read_text())
    return StepResult(
        outcome=on_disk.outcome or "failed",
        artifact=artifact,
        reason=on_disk.reason,
        message=f"{folder} finished before the engine moved on",
        counters=on_disk.counters,
    )


__all__ = [
    "ActionRequest",
    "ActionResult",
    "AgentRequest",
    "FlowEngine",
    "FlowReport",
    "GateRequest",
    "PreparedAgent",
    "StepPorts",
]
