"""A harness that runs the flow engine with scripted models (docs/SPEC.md sections 7 and 16).

The agent steps run the real `ApiLoopExecutor` against one `ScriptedClient`, whose responses
are replayed in order across steps. The gate and action steps are scripted too: `FakePorts`
writes the gate folder a real gate run would write, with the outcomes the test lists, and
answers the merge action with the outcome the test sets. Any port can be told to crash, so a
test can stop the engine inside a step and resume it with a fresh engine, as a new process
would.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from control_plane.agent import ApiLoopExecutor, StepBudget, StepContext
from control_plane.domain import Budget, Plan, Task
from control_plane.domain.gates import GateRecord, GateReport
from control_plane.flow import (
    ActionRequest,
    ActionResult,
    AgentRequest,
    FlowEngine,
    FlowState,
    GateRequest,
    PreparedAgent,
    load_lane,
    read_flow_state,
)
from control_plane.llm import Response, ScriptedClient
from control_plane.policy import PathJail, ProtectedPaths
from control_plane.store.home import TEMPLATE_DIR
from control_plane.tools import ToolExecutor
from fixtures.loop_harness import SYSTEM, Harness, submit_turn

TASK_FILE = Path(__file__).parent / "tasks" / "T-0017-json-status.md"
LANE = load_lane(TEMPLATE_DIR / "lanes" / "development")
FAILING_GATE = "tests"
OUT_OF_PLAN = ["notes.txt"]

STEP_BUDGET = {
    "max_turns": 40,
    "max_total_tokens": 400_000,
    "max_cost_usd": 2.0,
    "max_wall_seconds": 900,
}

FAILED_REVIEW: dict[str, Any] = {
    "rubric": [{"row_id": "R1", "result": "pass", "evidence": "ok"}],
    "criteria": [{"id": "AC1", "met": False, "evidence": "no test for the flag"}],
    "findings": [
        {
            "severity": "major",
            "file": "src/cli.py",
            "line": 12,
            "description": "the flag is parsed but never used",
            "suggestion": None,
        }
    ],
}


class CrashError(Exception):
    """Stands in for the process dying inside a step."""


def make_task(*, plan_approval: bool = False, max_iterations: int = 3) -> Task:
    task = Task.from_markdown(TASK_FILE.read_text())
    return task.model_copy(
        update={
            "plan_approval": plan_approval,
            "budget": Budget(max_iterations=max_iterations, max_cost_usd=4.0),
        }
    )


def plan_payload(files: Sequence[str] = ("tests/test_x.py",), questions: Sequence[str] = ()):
    return {
        "summary": "Add the flag",
        "steps": [{"id": "1", "description": "write the test", "files": list(files)}],
        "files_to_touch": list(files),
        "tests": [{"criterion": "AC1", "description": "the flag prints json"}],
        "risks": [],
        "out_of_scope": [],
        "questions": list(questions),
    }


def plan_turn(**kwargs: Any) -> Response:
    return submit_turn("planner", plan_payload(**kwargs))


def work_turn() -> Response:
    return submit_turn("worker")


def review_turn(*, passed: bool) -> Response:
    return submit_turn("reviewer", None if passed else FAILED_REVIEW)


class FakePorts:
    def __init__(
        self,
        harness: Harness,
        responses: Sequence[Response],
        *,
        gates: Sequence[str] = (),
        action: str = "ok",
        changed: Sequence[str] = (),
        step_budget: dict[str, float] | None = None,
    ) -> None:
        self.harness = harness
        self.client = ScriptedClient(responses)
        self.gate_outcomes = list(gates)
        self.action_outcome = action
        self.changed = list(changed)
        self.step_budget = StepBudget(**(step_budget or STEP_BUDGET))
        self.crash: str | None = None
        """`agent`, `gates_before`, `gates_after` or `action`: where to stop the engine."""
        self.agent_requests: list[AgentRequest] = []
        self.gate_requests: list[GateRequest] = []
        self.action_requests: list[ActionRequest] = []

    async def prepare_agent(self, request: AgentRequest) -> PreparedAgent:
        self.agent_requests.append(request)
        if self.crash == "agent":
            raise CrashError("died before the step started")
        harness = self.harness
        jail = PathJail(
            harness.worktree,
            writable=request.role == "worker",
            read_roots=[harness.context_dir],
        )
        executor = ApiLoopExecutor(
            client=self.client,
            policy=harness.policy,
            tools=ToolExecutor(jail),
            prices=harness.prices,
            clock=harness.clock,
        )
        context = StepContext(
            run=request.run,
            seq=request.seq,
            step=request.step,
            attempt=request.attempt,
            role=request.role,
            system=SYSTEM,
            brief=f"# Brief\n\n{request.step}, attempt {request.attempt}.\n",
            budget=self.step_budget,
        )
        return PreparedAgent(context=context, executor=executor)

    async def run_gates(self, request: GateRequest) -> GateReport:
        self.gate_requests.append(request)
        if self.crash == "gates_before":
            raise CrashError("died before the gates ran")
        outcome = self.gate_outcomes.pop(0)
        folder = f"gates/{request.attempt}"
        records = []
        for name in request.gates:
            status = "fail" if outcome == "fail" and name == FAILING_GATE else "pass"
            output_file = f"{folder}/{name}.txt"
            request.run.write_bytes(output_file, f"{name}: {status}\n".encode())
            records.append(
                GateRecord(name=name, status=status, summary=status, output_file=output_file)
            )
        report = GateReport(
            attempt=request.attempt, outcome=outcome, gates=records, out_of_plan=OUT_OF_PLAN
        )
        request.run.write_json(f"{folder}/results.json", report)
        if self.crash == "gates_after":
            raise CrashError("died after the gates ran")
        return report

    async def run_action(self, request: ActionRequest) -> ActionResult:
        self.action_requests.append(request)
        if self.crash == "action":
            raise CrashError("died inside the action")
        return ActionResult(outcome=self.action_outcome, message=f"merge {self.action_outcome}")

    def changed_files(self, run: object) -> list[str]:
        return list(self.changed)


def engine(
    harness: Harness,
    ports: FakePorts,
    task: Task | None = None,
    *,
    protected: Sequence[str] = (),
) -> FlowEngine:
    return FlowEngine(
        LANE.flow,
        task=task or make_task(),
        ports=ports,
        protected=ProtectedPaths(protected),
        clock=harness.clock,
    )


def state_of(harness: Harness) -> FlowState:
    return read_flow_state(harness.run)


def plan_of(request: AgentRequest | GateRequest) -> Plan | None:
    if isinstance(request, GateRequest):
        return request.plan
    return request.inputs.plan
