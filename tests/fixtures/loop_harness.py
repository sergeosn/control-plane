"""A harness that runs `ApiLoopExecutor` against `ScriptedClient` (docs/SPEC.md section 16).

Shared by the unit tests of the loop and the guardrail regression suite. It builds a home
repository, a run folder, a worktree and a policy from the shipped rules, and offers
builders for the scripted responses a test needs.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from control_plane.agent import (
    ApiLoopExecutor,
    StepBudget,
    StepContext,
    StepResult,
    StepState,
    load_transcript,
)
from control_plane.domain import Approval, LLMCall, ToolCall
from control_plane.llm import (
    Message,
    PriceTable,
    Response,
    ResponseBlock,
    ScriptedClient,
    TextBlock,
    ToolDefinition,
    ToolUseBlock,
    Usage,
)
from control_plane.policy import SUBMIT_TOOLS, TOOL_NAMES, PathJail, Policy, load_rules
from control_plane.store import Home, RunFolder, RunStore, create_home, read_jsonl
from control_plane.store.home import TEMPLATE_DIR
from control_plane.tools import ToolExecutor

T0 = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
MODEL = "scripted"
TOKENS_PER_TURN = 10
USD_PER_TOKEN = 0.0001
"""Each scripted turn costs 10 tokens and $0.001 unless a test says otherwise."""

SYSTEM = "You are the role under test."
BRIEF = "# Brief\n\nDo the task and finish by calling the submit tool.\n"

VALID_PLAN: dict[str, Any] = {
    "summary": "Add the flag",
    "steps": [{"id": "1", "description": "write the test", "files": ["tests/test_x.py"]}],
    "files_to_touch": ["tests/test_x.py"],
    "tests": [{"criterion": "AC1", "description": "the flag prints json"}],
    "risks": [],
    "out_of_scope": [],
    "questions": [],
}
VALID_WORK_REPORT: dict[str, Any] = {
    "summary": "Done",
    "criteria": [{"id": "AC1", "status": "done", "evidence": "tests/test_x.py passes"}],
    "deviations": [],
    "open_issues": [],
}
VALID_REVIEW: dict[str, Any] = {
    "rubric": [{"row_id": "R1", "result": "pass", "evidence": "ok"}],
    "criteria": [{"id": "AC1", "met": True, "evidence": "ok"}],
    "findings": [],
}
VALID_PAYLOADS = {"planner": VALID_PLAN, "worker": VALID_WORK_REPORT, "reviewer": VALID_REVIEW}


class Clock:
    """A clock a test moves by hand."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class TickingClient(ScriptedClient):
    """A scripted client that moves the clock forward on every model call."""

    def __init__(self, responses: Sequence[Response], clock: Clock, seconds: float) -> None:
        super().__init__(responses)
        self._clock = clock
        self._seconds = seconds

    async def complete(
        self,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        *,
        max_tokens: int | None = None,
    ) -> Response:
        self._clock.advance(self._seconds)
        return await super().complete(system, messages, tools, max_tokens=max_tokens)


# --- response builders --------------------------------------------------------


def usage(tokens: int = TOKENS_PER_TURN) -> Usage:
    return Usage(input_tokens=tokens, output_tokens=0)


def tool_use(tool_id: str, name: str, **arguments: Any) -> ToolUseBlock:
    return ToolUseBlock(id=tool_id, name=name, input=arguments)


def turn(
    *blocks: ResponseBlock, stop_reason: str = "tool_use", tokens: int = TOKENS_PER_TURN
) -> Response:
    return Response(model=MODEL, content=list(blocks), stop_reason=stop_reason, usage=usage(tokens))


def text_turn(text: str, stop_reason: str = "end_turn", tokens: int = TOKENS_PER_TURN) -> Response:
    return turn(TextBlock(text=text), stop_reason=stop_reason, tokens=tokens)


def read_turn(tool_id: str, path: str = "ok.txt", **extra: Any) -> Response:
    return turn(tool_use(tool_id, "read_file", path=path, **extra))


def submit_turn(
    role: str, payload: dict[str, Any] | None = None, tool_id: str = "toolu_submit"
) -> Response:
    data = VALID_PAYLOADS[role] if payload is None else payload
    return turn(tool_use(tool_id, SUBMIT_TOOLS[role], **data))


# --- the harness ----------------------------------------------------------------


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        role: str,
        *,
        clock: Clock | None = None,
        usd_per_token: float = USD_PER_TOKEN,
    ) -> None:
        self.role = role
        self.clock = clock or Clock()
        self.home: Home = create_home(tmp_path / "home")
        self.run: RunFolder = RunStore(self.home, clock=self.clock).create("R-0001")
        self.context_dir = self.run.path / "context"
        (self.context_dir / "BRIEF.md").write_text(BRIEF)
        self.worktree = tmp_path / "worktree"
        self.worktree.mkdir()
        (self.worktree / "ok.txt").write_text("fine\n")
        self.policy = Policy(load_rules(TEMPLATE_DIR / "rules" / "tools.yaml"))
        rate = usd_per_token * 1_000_000
        self.prices = PriceTable.from_mapping(
            {MODEL: {"input": rate, "output": rate, "cache_write": rate, "cache_read": rate}}
        )
        self.step = {"planner": "plan", "worker": "implement", "reviewer": "review"}[role]
        self.seq = {"planner": 1, "worker": 2, "reviewer": 4}[role]

    def tools(self) -> ToolExecutor:
        jail = PathJail(
            self.worktree, writable=self.role == "worker", read_roots=[self.context_dir]
        )
        return ToolExecutor(jail, tools=TOOL_NAMES)

    def context(self, *, attempt: int = 1, max_tokens: int = 8192, **budget: float) -> StepContext:
        limits = {
            "max_turns": 40,
            "max_total_tokens": 400_000,
            "max_cost_usd": 2.0,
            "max_wall_seconds": 900,
        }
        limits.update(budget)
        return StepContext(
            run=self.run,
            seq=self.seq,
            step=self.step,
            attempt=attempt,
            role=self.role,
            system=SYSTEM,
            brief=BRIEF,
            budget=StepBudget(**limits),
            max_tokens=max_tokens,
        )

    def executor(
        self, client: ScriptedClient, *, tools: ToolExecutor | None = None
    ) -> ApiLoopExecutor:
        return ApiLoopExecutor(
            client=client,
            policy=self.policy,
            tools=tools or self.tools(),
            prices=self.prices,
            clock=self.clock,
        )

    async def run_step(
        self,
        responses: Sequence[Response],
        *,
        tools: ToolExecutor | None = None,
        tick_seconds: float = 0.0,
        attempt: int = 1,
        max_tokens: int = 8192,
        **budget: float,
    ) -> tuple[StepResult, ScriptedClient]:
        client = TickingClient(responses, self.clock, tick_seconds)
        executor = self.executor(client, tools=tools)
        result = await executor.run_step(
            self.context(attempt=attempt, max_tokens=max_tokens, **budget)
        )
        return result, client

    async def resume_step(
        self,
        responses: Sequence[Response],
        *,
        tools: ToolExecutor | None = None,
        tick_seconds: float = 0.0,
        attempt: int = 1,
        max_tokens: int = 8192,
        **budget: float,
    ) -> tuple[StepResult, ScriptedClient]:
        """Resume the step a previous `run_step` left parked or crashed, with a new client."""
        client = TickingClient(responses, self.clock, tick_seconds)
        executor = self.executor(client, tools=tools)
        result = await executor.resume_step(
            self.context(attempt=attempt, max_tokens=max_tokens, **budget)
        )
        return result, client

    def approvals(self) -> list[Approval]:
        """Every line of the run's `approvals.jsonl`, decisions included."""
        return self.run.approvals()

    # --- what the step folder holds ---------------------------------------------

    @property
    def step_dir(self) -> Path:
        return self.run.path / "steps" / f"{self.seq:02d}-{self.step}-1"

    def state(self) -> StepState:
        return StepState.from_json((self.step_dir / "state.json").read_text())

    def transcript(self) -> list[Message]:
        return load_transcript(self.step_dir / "transcript.jsonl")

    def llm_calls(self) -> list[LLMCall]:
        return read_jsonl(self.step_dir / "llm_calls.jsonl", LLMCall)

    def tool_calls(self) -> list[ToolCall]:
        return read_jsonl(self.step_dir / "tool_calls.jsonl", ToolCall)


def tool_results(message: Message) -> list[Any]:
    """The `tool_result` blocks of a user message, in order."""
    return [block for block in message.content if block.type == "tool_result"]
