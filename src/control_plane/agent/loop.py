"""`ApiLoopExecutor`: the tool loop of docs/SPEC.md section 8.2, with parking and resume
(section 12).

The loop knows the `LLMClient` protocol, the policy, the tool executor and the run folder,
and nothing about the flow. It takes a `StepContext` and returns a `StepResult`: a validated
artifact, a park, or a failure with its reason. Every model response is priced and written to
`llm_calls.jsonl`; every tool call and its decision to `tool_calls.jsonl`; and after every
model response and every tool result the transcript is appended and `state.json` rewritten
(section 8.6).

Parking: a call the policy parks gets an entry in the run's `approvals.jsonl`, the step's
`state.json` keeps the call as `pending` with the entry's id, and `run_step` returns so the
process can exit. Nothing waits in memory. A rate limit that outlives the client's retries
(`RateLimitedError`) parks the same way, without an approval entry: `state.json` carries
`resume_after`, and a resume before that time changes nothing. An endpoint that cannot be
reached (`EndpointUnreachableError`) parks exactly like it, with reason `connection`, and so
does a server error (`ServerFailedError`, reason `server_error`). A rejected token parks with
reason `auth` and no time, so a resume after the token is replaced tries again at once. Any
other `ModelCallError` fails the step with its reason; none reaches the user as a traceback.
`resume_step` reloads the transcript and the state, answers every unfinished call (the
human's decision for a parked one, the real result for one that was never reached, an
"interrupted" error for one that started and did not finish) and carries on with the same
loop.

One deliberate gap in the transcript: a response cut short by `max_tokens` inside a
`tool_use` block is retried once with a higher limit, and the cut response is recorded in
`llm_calls.jsonl` but not in the transcript, because the retry resends the same messages.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, Self, runtime_checkable

from pydantic import ValidationError

from control_plane.agent.budget import BudgetCounters, StepBudget, check_budget
from control_plane.agent.repeats import FAIL_AT, NOTICE_AT
from control_plane.agent.state import CallState, StepState, StepUsage
from control_plane.agent.transcript import load_transcript, transcript_line
from control_plane.domain.approval import Approval
from control_plane.domain.base import DomainModel
from control_plane.domain.calls import Decision, LLMCall, ToolCall
from control_plane.domain.plan import Plan
from control_plane.domain.review import Review
from control_plane.domain.schema import strict_schema
from control_plane.domain.work_report import WorkReport
from control_plane.llm.client import (
    AuthRejectedError,
    EndpointUnreachableError,
    InvalidRequestError,
    LLMClient,
    ModelCallError,
    ModelNotFoundError,
    RateLimitedError,
    RetryLaterError,
    ServerFailedError,
)
from control_plane.llm.pricing import PriceTable
from control_plane.llm.types import (
    Message,
    Response,
    ToolDefinition,
    ToolResultBlock,
    ToolUseBlock,
)
from control_plane.policy.classify import Classification, Policy
from control_plane.policy.roles import SUBMIT_TOOLS
from control_plane.store.runs import (
    APPROVALS_FILE,
    ARTIFACT_FILE,
    LLM_CALLS_FILE,
    STEPS_DIR,
    TOOL_CALLS_FILE,
    RunFolder,
)
from control_plane.tools.executor import ToolExecutor

TRANSCRIPT_FILE = "transcript.jsonl"
STATE_FILE = "state.json"
USAGE_FILE = "usage.json"

Outcome = Literal["ok", "blocked", "parked", "failed"]

REASON_BUDGET = "budget_exceeded"
REASON_LOOP = "loop_detected"
REASON_NO_SUBMIT = "no_submit"
REASON_INVALID_SUBMIT = "invalid_submit"
REASON_MAX_TOKENS = "max_tokens"
"""Why a step failed. `refusal` and `model_context_window_exceeded` are recorded as is."""

REASON_RATE_LIMIT = "rate_limit"
"""Why a step parked without an approval: the model endpoint kept rate-limiting (8.2)."""
REASON_CONNECTION = "connection"
"""Why a step parked without an approval: the model endpoint could not be reached."""
REASON_SERVER_ERROR = "server_error"
"""Why a step parked without an approval: the endpoint kept answering 5xx."""
REASON_AUTH = "auth"
"""Why a step parked without an approval: the endpoint rejected the token (401, 403)."""
REASON_MODEL_NOT_FOUND = "model_not_found"
REASON_BAD_REQUEST = "bad_request"
REASON_CONTEXT_WINDOW = "model_context_window_exceeded"
REASON_MODEL_ERROR = "model_error"
"""A model error no other reason covers."""
_PARK_UNTIL: dict[type[RetryLaterError], str] = {
    RateLimitedError: REASON_RATE_LIMIT,
    EndpointUnreachableError: REASON_CONNECTION,
    ServerFailedError: REASON_SERVER_ERROR,
}

MAX_REPAIRS = 2
"""Invalid submit payloads returned for repair before the step fails (section 8.2)."""

ARTIFACTS: dict[str, type[DomainModel]] = {
    "planner": Plan,
    "worker": WorkReport,
    "reviewer": Review,
}

SUBMIT_DESCRIPTIONS: dict[str, str] = {
    "planner": "Finish by submitting the plan. Call this exactly once, when the plan is complete.",
    "worker": (
        "Finish by submitting the work report. Call this exactly once, after the checks "
        "have been run."
    ),
    "reviewer": (
        "Finish by submitting the review. Call this exactly once, when every row is graded."
    ),
}

Clock = Callable[[], datetime]


def _wall_clock() -> datetime:
    return datetime.now(UTC)


class ResumeError(Exception):
    """A step cannot be resumed: it never started, it is finished, or its files disagree."""


def submit_tool(role: str) -> ToolDefinition:
    """The strict submit tool of `role`, its schema generated from the artifact model."""
    return ToolDefinition(
        name=SUBMIT_TOOLS[role],
        description=SUBMIT_DESCRIPTIONS[role],
        input_schema=strict_schema(ApiLoopExecutor.artifact_model(role)),
        strict=True,
    )


def approval_id(run_id: str, number: int) -> str:
    """The id of the `number`th approval entry of a run: `A-<run id without R->-NN`."""
    return f"A-{run_id.removeprefix('R-')}-{number:02d}"


@dataclass
class StepContext:
    """What one agent step is given. The flow engine builds it; the executor runs it."""

    run: RunFolder
    seq: int
    step: str
    attempt: int
    role: str
    system: str
    brief: str
    budget: StepBudget
    max_tokens: int = 8192

    @property
    def folder(self) -> str:
        """The step folder, relative to the run folder: `steps/NN-<step>-<attempt>`."""
        return f"{STEPS_DIR}/{self.seq:02d}-{self.step}-{self.attempt}"


@dataclass(frozen=True)
class StepResult:
    outcome: Outcome
    artifact: DomainModel | None
    reason: str | None
    message: str
    counters: BudgetCounters
    pending: list[CallState] = field(default_factory=list)
    resume_after: datetime | None = None


@runtime_checkable
class Executor(Protocol):
    async def run_step(self, step: StepContext) -> StepResult: ...

    async def resume_step(self, step: StepContext) -> StepResult: ...


class ApiLoopExecutor:
    def __init__(
        self,
        client: LLMClient,
        policy: Policy,
        tools: ToolExecutor,
        prices: PriceTable,
        clock: Clock = _wall_clock,
    ) -> None:
        self.client = client
        self.policy = policy
        self.tools = tools
        self.prices = prices
        self.clock = clock

    @staticmethod
    def artifact_model(role: str) -> type[DomainModel]:
        try:
            return ARTIFACTS[role]
        except KeyError:
            raise ValueError(
                f"no artifact for role {role!r}; roles: {', '.join(ARTIFACTS)}"
            ) from None

    async def run_step(self, step: StepContext) -> StepResult:
        return await _StepRun(self, step).run()

    async def resume_step(self, step: StepContext) -> StepResult:
        """Continue a parked or interrupted step from its transcript and `state.json`."""
        return await _StepRun.reload(self, step).resume()


class _StepRun:
    """The mutable state of one `run_step` or `resume_step` call."""

    def __init__(self, executor: ApiLoopExecutor, ctx: StepContext) -> None:
        self.executor = executor
        self.ctx = ctx
        self.run_folder = ctx.run
        self.model = ApiLoopExecutor.artifact_model(ctx.role)
        self.submit = SUBMIT_TOOLS[ctx.role]
        self.tools = [*executor.tools.definitions(), submit_tool(ctx.role)]
        self.state = StepState(
            step=ctx.step, attempt=ctx.attempt, role=ctx.role, started_at=executor.clock()
        )
        self.messages: list[Message] = []
        self.accepted: DomainModel | None = None
        self.last_invalid: str | None = None

    @classmethod
    def reload(cls, executor: ApiLoopExecutor, ctx: StepContext) -> Self:
        """A run rebuilt from the step folder, ready to resume. Raises `ResumeError`."""
        run = cls(executor, ctx)
        state_path = run.run_folder.path / ctx.folder / STATE_FILE
        if not state_path.exists():
            raise ResumeError(
                f"{ctx.folder} has no {STATE_FILE}: the step never started, so it cannot be "
                "resumed; run it instead"
            )
        run.state = StepState.from_json(state_path.read_text(encoding="utf-8"))
        if run.state.status == "finished":
            raise ResumeError(
                f"{ctx.folder} is finished with outcome {run.state.outcome!r} and cannot be resumed"
            )
        run.messages = load_transcript(run.run_folder.path / ctx.folder / TRANSCRIPT_FILE)
        return run

    # --- the loop -------------------------------------------------------------

    async def run(self) -> StepResult:
        self.event("step_started")
        self.append(Message.user(self.ctx.brief))
        return await self.loop()

    async def resume(self) -> StepResult:
        approvals = self.pending_approvals()
        undecided = sorted(
            call.approval_id
            for call in self.state.pending
            if call.approval_id is not None and not approvals[call.approval_id].decided
        )
        if undecided:
            noun = "approval is" if len(undecided) == 1 else "approvals are"
            return StepResult(
                outcome="parked",
                artifact=None,
                reason=None,
                message=f"{len(undecided)} {noun} undecided: {', '.join(undecided)}",
                counters=self.state.counters.model_copy(),
                pending=list(self.state.pending),
            )
        if self.state.resume_after is not None and self.executor.clock() < self.state.resume_after:
            return StepResult(
                outcome="parked",
                artifact=None,
                reason=self.state.reason or REASON_RATE_LIMIT,
                message=self.parked_message(),
                counters=self.state.counters.model_copy(),
                resume_after=self.state.resume_after,
            )
        self.state.resume_after = None
        was_parked = self.state.status == "parked"
        # Time spent parked is not the step's; the wall clock restarts where it stopped.
        self.state.started_at = self.executor.clock() - timedelta(
            seconds=self.state.counters.wall_seconds
        )
        self.state.status = "running"
        self.save()
        self.event("step_resumed")
        if was_parked:
            self.event("run_resumed")
        result = await self.settle(approvals)
        if result is not None:
            return result
        return await self.loop()

    async def loop(self) -> StepResult:
        ctx = self.ctx
        max_tokens: int | None = None
        while True:
            check = self.check_budget()
            if check is not None:
                return self.finish("failed", REASON_BUDGET, check)
            try:
                response = await self.executor.client.complete(
                    ctx.system, self.messages, self.tools, max_tokens=max_tokens
                )
            except ModelCallError as error:
                return self.model_error(error)
            self.record_response(response)
            max_tokens = None
            if response.stop_reason == "max_tokens":
                cut_tool_use = bool(response.content) and isinstance(
                    response.content[-1], ToolUseBlock
                )
                if cut_tool_use and not self.state.max_tokens_retried:
                    self.state.max_tokens_retried = True
                    max_tokens = ctx.max_tokens * 2
                    self.save()
                    continue
                self.append(Message.from_response(response))
                return self.finish(
                    "failed",
                    REASON_MAX_TOKENS,
                    f"the response was cut at max_tokens ({ctx.max_tokens})"
                    + (" twice inside a tool call" if cut_tool_use else ""),
                )
            self.append(Message.from_response(response))
            if response.stop_reason in ("refusal", "model_context_window_exceeded"):
                return self.finish(
                    "failed",
                    response.stop_reason,
                    f"the model stopped with stop_reason {response.stop_reason!r}",
                )
            if response.stop_reason == "pause_turn":
                continue
            if not response.tool_uses:
                if self.state.nudged:
                    return self.finish(
                        "failed",
                        REASON_NO_SUBMIT,
                        f"the model ended its turn twice without calling {self.submit}",
                    )
                self.state.nudged = True
                self.append(
                    Message.user(
                        f"You ended your turn without calling {self.submit}. Finish by "
                        f"calling {self.submit} with your result; nothing else counts."
                    )
                )
                continue
            result = await self.answer(response.tool_uses)
            if result is not None:
                return result

    # --- answering a model turn --------------------------------------------------

    async def answer(self, tool_uses: list[ToolUseBlock]) -> StepResult | None:
        """Answer every `tool_use` block of one response; `None` means keep going."""
        decisions = [self.classify(block) for block in tool_uses]
        self.state.calls = [
            CallState(
                tool_use_id=block.id,
                tool=block.name,
                arguments=dict(block.input),
                decision=decision.decision,
            )
            for block, decision in zip(tool_uses, decisions, strict=True)
        ]
        self.save()
        self.accepted, self.last_invalid = None, None
        for call, decision in zip(self.state.calls, decisions, strict=True):
            result = await self.answer_call(call, decision)
            if result is not None:
                return result
        return self.after_calls()

    async def settle(self, approvals: dict[str, Approval]) -> StepResult | None:
        """On resume, answer every call of the interrupted turn that has no result yet."""
        self.accepted, self.last_invalid = None, None
        for call in self.state.calls:
            if call.status == "finished":
                continue
            if call.status == "started":
                self.deliver(call, self.interrupted(call), True, decision=call.decision)
                continue
            approval = approvals.get(call.approval_id) if call.approval_id else None
            if approval is not None:
                await self.answer_decided(call, approval)
                continue
            # Never reached before the interruption: decide it as if it were new.
            decision = self.classify(
                ToolUseBlock(id=call.tool_use_id, name=call.tool, input=call.arguments)
            )
            call.decision = decision.decision
            result = await self.answer_call(call, decision)
            if result is not None:
                return result
        return self.after_calls()

    async def answer_call(self, call: CallState, decision: Classification) -> StepResult | None:
        repeats = self.state.repeat.observe(call.tool, call.arguments)
        if repeats >= FAIL_AT:
            return self.finish(
                "failed",
                REASON_LOOP,
                f"{call.tool} was called {repeats} times in a row with the same arguments",
            )
        if repeats >= NOTICE_AT:
            self.state.notices.append(
                f"engine notice: this is call {repeats} in a row of {call.tool} with the "
                "identical arguments; the result does not change, try something else"
            )
        if decision.decision == "park":
            self.park(call, decision)
            return None
        if decision.decision == "deny":
            content, is_error = decision.reason, True
        elif call.tool == self.submit:
            artifact, content = self.validate_submit(call.arguments)
            is_error = artifact is None
            if artifact is not None:
                self.accepted = artifact
            else:
                self.last_invalid = content
        else:
            content, is_error = await self.execute(call)
        self.deliver(
            call,
            content,
            is_error,
            decision=decision.decision,
            tool_class=decision.tool_class,
            rule_id=decision.rule_id,
        )
        return None

    async def answer_decided(self, call: CallState, approval: Approval) -> None:
        """Answer a parked call the way the human decided (section 12)."""
        if approval.answer == "approve":
            content, is_error = await self.execute(call)
            decision: Decision = "allow"
        else:
            reason = approval.note or approval.answer
            content, is_error = (
                f"{call.tool} was denied by the human who reviewed it: {reason}. "
                "Choose another approach.",
                True,
            )
            decision = "deny"
        self.deliver(call, content, is_error, decision=decision, approval_id=approval.id)

    def after_calls(self) -> StepResult | None:
        if self.accepted is not None:
            self.run_folder.write_json(f"{self.ctx.folder}/{ARTIFACT_FILE}", self.accepted)
            blocked = getattr(self.accepted, "blocked", False)
            return self.finish(
                "blocked" if blocked else "ok",
                None,
                f"{self.submit} accepted" + (" with questions" if blocked else ""),
                artifact=self.accepted,
            )
        if self.state.pending:
            return self.finish(
                "parked",
                None,
                f"{len(self.state.pending)} tool call(s) wait for approval",
                status="parked",
            )
        if self.last_invalid is not None:
            self.state.repairs += 1
            if self.state.repairs > MAX_REPAIRS:
                return self.finish("failed", REASON_INVALID_SUBMIT, self.last_invalid)
        return None

    # --- pieces ---------------------------------------------------------------

    def classify(self, block: ToolUseBlock) -> Classification:
        decision = self.executor.policy.classify(self.ctx.role, block)
        self.event(**decision.event_fields())
        return decision

    def park(self, call: CallState, decision: Classification) -> None:
        """Store the call as pending and raise its approval entry (section 12)."""
        number = len({approval.id for approval in self.run_folder.approvals()}) + 1
        approval = Approval(
            id=approval_id(self.run_folder.run_id, number),
            run_id=self.run_folder.run_id,
            type="tool_call",
            asked=(
                f"{self.ctx.role} asks: {decision.reason}; arguments: "
                f"{json.dumps(call.arguments, sort_keys=True)}"
            ),
            evidence=[
                f"{self.ctx.folder}/{TRANSCRIPT_FILE}",
                f"{self.ctx.folder}/{TOOL_CALLS_FILE}",
            ],
            created_at=self.executor.clock(),
        )
        self.run_folder.append_approval(approval)
        call.approval_id = approval.id
        self.record_tool_call(
            call, decision="park", tool_class=decision.tool_class, rule_id=decision.rule_id
        )
        self.save()

    def pending_approvals(self) -> dict[str, Approval]:
        """The latest line of every approval entry a pending call refers to."""
        wanted = {call.approval_id for call in self.state.pending if call.approval_id}
        latest = {a.id: a for a in self.run_folder.approvals() if a.id in wanted}
        missing = sorted(wanted - latest.keys())
        if missing:
            raise ResumeError(
                f"{self.ctx.folder} waits for approval {', '.join(missing)}, which is not in "
                f"{APPROVALS_FILE} of run {self.run_folder.run_id}"
            )
        return latest

    async def execute(self, call: CallState) -> tuple[str, bool]:
        """Run the tool, with the call marked `started` on disk first (crash recovery)."""
        call.status = "started"
        self.save()
        outcome = await self.executor.tools.execute(call.tool, call.arguments)
        return outcome.content, outcome.is_error

    def interrupted(self, call: CallState) -> str:
        return (
            f"{call.tool} with arguments {json.dumps(call.arguments, sort_keys=True)} was "
            "interrupted while it ran and its effect is unknown: the engine stopped before "
            "the call finished. Check what it may have changed (read the file, run git "
            "status or the tests) before deciding whether to repeat it."
        )

    def deliver(
        self,
        call: CallState,
        content: str,
        is_error: bool,
        *,
        decision: Decision,
        tool_class: str | None = None,
        rule_id: str | None = None,
        approval_id: str | None = None,
    ) -> None:
        """Finish a call: log it, append its `tool_result` with any pending notices."""
        if self.state.notices:
            content = content + "\n\n" + "\n".join(f"[{n}]" for n in self.state.notices)
            self.state.notices = []
        call.status, call.content, call.is_error = "finished", content, is_error
        self.record_tool_call(
            call,
            decision=decision,
            tool_class=tool_class,
            rule_id=rule_id,
            approval_id=approval_id,
        )
        result = ToolResultBlock(tool_use_id=call.tool_use_id, content=content, is_error=is_error)
        self.append(Message(role="user", content=[result]))

    def validate_submit(self, arguments: dict[str, Any]) -> tuple[DomainModel | None, str]:
        try:
            return self.model.model_validate(arguments), f"{self.submit} accepted"
        except ValidationError as exc:
            errors = "; ".join(
                f"{'.'.join(str(part) for part in error['loc']) or 'input'}: {error['msg']}"
                for error in exc.errors()
            )
            return None, (
                f"{self.submit} input is invalid: {errors}. Fix these fields and call "
                f"{self.submit} again with the whole payload."
            )

    def check_budget(self) -> str | None:
        """Update wall time, remember new 80% notices, return the breach if any."""
        self.state.counters.wall_seconds = (
            self.executor.clock() - self.state.started_at
        ).total_seconds()
        check = check_budget(self.ctx.budget, self.state.counters, self.state.warned)
        self.state.warned.extend(sorted(check.warned))
        self.state.notices.extend(check.notices)
        return check.breached

    def record_response(self, response: Response) -> None:
        usage = response.usage
        cost = self.executor.prices.cost_usd(response.model, usage)
        counters, tokens = self.state.counters, self.state.tokens
        counters.turns += 1
        counters.total_tokens += usage.total_tokens
        counters.cost_usd += cost
        tokens.input_tokens += usage.input_tokens
        tokens.output_tokens += usage.output_tokens
        tokens.cache_read_input_tokens += usage.cache_read_input_tokens
        tokens.cache_creation_input_tokens += usage.cache_creation_input_tokens
        self.run_folder.append_jsonl(
            f"{self.ctx.folder}/{LLM_CALLS_FILE}",
            LLMCall(
                ts=self.executor.clock(),
                model=response.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_input_tokens=usage.cache_read_input_tokens,
                cache_creation_input_tokens=usage.cache_creation_input_tokens,
                cost_usd=cost,
                stop_reason=response.stop_reason,
            ),
        )
        self.check_budget()
        self.save()

    def record_tool_call(
        self,
        call: CallState,
        *,
        decision: Decision,
        tool_class: str | None,
        rule_id: str | None,
        approval_id: str | None = None,
    ) -> None:
        self.run_folder.append_jsonl(
            f"{self.ctx.folder}/{TOOL_CALLS_FILE}",
            ToolCall(
                ts=self.executor.clock(),
                tool_use_id=call.tool_use_id,
                tool=call.tool,
                arguments=call.arguments,
                tool_class=tool_class,
                decision=decision,
                rule_id=rule_id,
                error=call.content if call.is_error else None,
                approval_id=approval_id,
            ),
        )

    def append(self, message: Message) -> None:
        """Add a message to the conversation and the transcript, then save the state."""
        if self.messages and self.messages[-1].role == message.role:
            self.messages[-1] = Message(
                role=message.role, content=[*self.messages[-1].content, *message.content]
            )
        else:
            self.messages.append(message)
        self.run_folder.append_jsonl(
            f"{self.ctx.folder}/{TRANSCRIPT_FILE}", transcript_line(message)
        )
        self.save()

    def save(self) -> None:
        self.run_folder.write_json(f"{self.ctx.folder}/{STATE_FILE}", self.state)
        self.run_folder.write_json(
            f"{self.ctx.folder}/{USAGE_FILE}", StepUsage.from_state(self.state)
        )

    def event(self, kind: str, **fields: str | None) -> None:
        self.run_folder.log_event(kind, step=self.ctx.step, attempt=self.ctx.attempt, **fields)

    def finish(
        self,
        outcome: Outcome,
        reason: str | None,
        message: str,
        *,
        artifact: DomainModel | None = None,
        status: Literal["parked", "finished"] = "finished",
    ) -> StepResult:
        self.state.status, self.state.outcome, self.state.reason = status, outcome, reason
        self.check_budget()
        self.save()
        self.event(f"step_{status}", outcome=outcome, message=message)
        if status == "parked":
            self.event("run_parked", outcome=outcome, message=self.parked_message())
        return StepResult(
            outcome=outcome,
            artifact=artifact,
            reason=reason,
            message=message,
            counters=self.state.counters.model_copy(),
            pending=list(self.state.pending),
            resume_after=self.state.resume_after,
        )

    def model_error(self, error: ModelCallError) -> StepResult:
        """Park or fail the step for a model call that failed (section 8.2)."""
        detail = str(error)
        if isinstance(error, RetryLaterError):
            reason = next(
                (r for kind, r in _PARK_UNTIL.items() if isinstance(error, kind)),
                REASON_MODEL_ERROR,
            )
            self.state.resume_after = error.resume_after
            return self.finish("parked", reason, f"{reason}: {detail}", status="parked")
        if isinstance(error, AuthRejectedError):
            return self.finish("parked", REASON_AUTH, f"{REASON_AUTH}: {detail}", status="parked")
        if isinstance(error, ModelNotFoundError):
            return self.finish("failed", REASON_MODEL_NOT_FOUND, detail)
        if isinstance(error, InvalidRequestError):
            too_long = error.context_window_exceeded
            return self.finish(
                "failed", REASON_CONTEXT_WINDOW if too_long else REASON_BAD_REQUEST, detail
            )
        return self.finish("failed", REASON_MODEL_ERROR, detail)

    def parked_message(self) -> str:
        if self.state.reason == REASON_AUTH:
            return "the model endpoint rejected the token; replace it and resume"
        if self.state.resume_after is not None:
            until = self.state.resume_after.isoformat()
            if self.state.reason == REASON_CONNECTION:
                return f"the model endpoint could not be reached; resume after {until}"
            if self.state.reason == REASON_SERVER_ERROR:
                return f"the model endpoint answered with a server error; resume after {until}"
            return f"rate limited; resume after {until}"
        waiting = ", ".join(c.approval_id or c.tool_use_id for c in self.state.pending)
        return f"waiting for approval {waiting}"
