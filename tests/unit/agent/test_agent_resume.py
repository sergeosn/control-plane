"""Task 9: parking creates approval entries and a step resumes from its transcript
(docs/SPEC.md 12).

Every case parks a worker step with `ScriptedClient`, decides the approval the way a human
would through `approvals.jsonl`, and resumes the step with a fresh executor, so nothing
survives in memory between the park and the resume.
"""

from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from control_plane.agent import ApiLoopExecutor, Executor, ResumeError, StepState
from control_plane.domain import Approval, WorkReport
from control_plane.llm import Message
from control_plane.tools import ToolExecutor, ToolOutcome
from fixtures.loop_harness import (
    Harness,
    TickingClient,
    read_turn,
    submit_turn,
    tool_results,
    tool_use,
    turn,
)

PUSH = ["git", "push", "origin", "run/R-0001"]


class Recording(ToolExecutor):
    """A tool executor that records every execution and never runs a command."""

    def __init__(self, inner: ToolExecutor) -> None:
        super().__init__(inner.jail, tools=inner.tools)
        self.executed: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        self.executed.append((name, dict(arguments)))
        if name == "run_command":
            return ToolOutcome("exit code: 0\nstdout:\npushed\nstderr:\n(empty)")
        return await super().execute(name, arguments)


class Crashing(Recording):
    """Raises in the middle of the first command, the way a power cut would."""

    crashed = False

    async def execute(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        if name == "run_command" and not self.crashed:
            self.crashed = True
            raise RuntimeError("power cut")
        return await super().execute(name, arguments)


@pytest.fixture
def worker(tmp_path: Path) -> Harness:
    return Harness(tmp_path, "worker")


@pytest.fixture
def tools(worker: Harness) -> Recording:
    return Recording(worker.tools())


def push_turn(tool_id: str = "toolu_p") -> Any:
    return turn(tool_use(tool_id, "run_command", argv=PUSH))


def mixed_turn() -> Any:
    return turn(tool_use("toolu_r", "read_file", path="ok.txt"), push_turn().content[0])


def pending(worker: Harness) -> Approval:
    [approval] = [a for a in worker.approvals() if not a.decided]
    return approval


def decide(worker: Harness, answer: str, note: str | None = None) -> Approval:
    decided = pending(worker).decide(answer, note=note, at=worker.clock())
    worker.run.append_approval(decided)
    return decided


# --- parking (Done when 1) ------------------------------------------------------


async def test_a_write_external_call_parks_the_run_and_creates_an_approval_entry(
    worker: Harness, tools: Recording
) -> None:
    result, client = await worker.run_step([push_turn(), submit_turn("worker")], tools=tools)

    assert result.outcome == "parked"
    assert tools.executed == []
    assert client.remaining == 1, "the loop returned instead of calling the model again"
    [approval] = worker.approvals()
    assert approval.id == "A-0001-01"
    assert approval.run_id == "R-0001"
    assert approval.type == "tool_call"
    assert not approval.decided
    assert approval.created_at == worker.clock()
    assert "worker" in approval.asked and "run_command" in approval.asked
    assert "git push origin run/R-0001" in approval.asked
    assert "write-external" in approval.asked
    assert approval.evidence == [
        "steps/02-implement-1/transcript.jsonl",
        "steps/02-implement-1/tool_calls.jsonl",
    ]
    state = worker.state()
    assert state.status == "parked"
    assert [(c.tool_use_id, c.status, c.approval_id) for c in state.calls] == [
        ("toolu_p", "pending", "A-0001-01")
    ]
    kinds = [(e.kind, e.outcome) for e in worker.run.events()[-2:]]
    assert kinds == [("step_parked", "parked"), ("run_parked", "parked")]
    assert "A-0001-01" in (worker.run.events()[-1].message or "")


async def test_each_parked_call_gets_its_own_approval_entry(
    worker: Harness, tools: Recording
) -> None:
    response = turn(
        tool_use("toolu_1", "run_command", argv=["git", "push"]),
        tool_use("toolu_2", "run_command", argv=["gh", "pr", "create"]),
    )

    result, _ = await worker.run_step([response], tools=tools)

    assert result.outcome == "parked"
    assert [a.id for a in worker.approvals()] == ["A-0001-01", "A-0001-02"]
    assert [c.approval_id for c in worker.state().calls] == ["A-0001-01", "A-0001-02"]


async def test_a_second_park_in_the_same_run_continues_the_approval_numbering(
    worker: Harness, tools: Recording
) -> None:
    await worker.run_step([push_turn("toolu_1")], tools=tools)
    decide(worker, "deny", "not from here")

    await worker.resume_step([push_turn("toolu_2")], tools=tools)

    assert [a.id for a in worker.approvals()] == ["A-0001-01", "A-0001-01", "A-0001-02"]


# --- approval and denial (Done when 2 and 3) ------------------------------------


async def test_after_approval_the_call_executes_exactly_once_and_the_loop_continues(
    worker: Harness, tools: Recording
) -> None:
    await worker.run_step([push_turn()], tools=tools)
    decide(worker, "approve", "go ahead")

    result, client = await worker.resume_step([submit_turn("worker")], tools=tools)

    assert result.outcome == "ok"
    assert isinstance(result.artifact, WorkReport)
    assert tools.executed == [("run_command", {"argv": PUSH})]
    [first] = client.calls
    [answer] = tool_results(first.messages[-1])
    assert answer.tool_use_id == "toolu_p" and not answer.is_error
    assert "pushed" in answer.content
    assert [m.role for m in first.messages] == ["user", "assistant", "user"]
    state = worker.state()
    assert state.status == "finished" and state.outcome == "ok"
    assert state.calls[0].status == "finished"
    kinds = [e.kind for e in worker.run.events()]
    assert kinds.index("run_resumed") > kinds.index("run_parked")
    assert "step_resumed" in kinds
    resumed = worker.tool_calls()[-2]
    assert (resumed.tool_use_id, resumed.decision, resumed.approval_id, resumed.error) == (
        "toolu_p",
        "allow",
        "A-0001-01",
        None,
    )


async def test_after_denial_the_model_gets_an_error_result_with_the_reason_and_continues(
    worker: Harness, tools: Recording
) -> None:
    await worker.run_step([push_turn()], tools=tools)
    decide(worker, "deny", "Pushes go through the merge step, not from the worker.")

    result, client = await worker.resume_step(
        [read_turn("toolu_r"), submit_turn("worker")], tools=tools
    )

    assert result.outcome == "ok"
    assert tools.executed == [("read_file", {"path": "ok.txt"})]
    [answer] = tool_results(client.calls[0].messages[-1])
    assert answer.tool_use_id == "toolu_p" and answer.is_error
    assert "denied" in answer.content
    assert "Pushes go through the merge step, not from the worker." in answer.content
    denied = worker.tool_calls()[1]
    assert (denied.decision, denied.approval_id) == ("deny", "A-0001-01")
    assert denied.error == answer.content
    assert len(client.calls) == 2


async def test_the_transcript_on_resume_is_the_conversation_the_model_saw_before_the_park(
    worker: Harness, tools: Recording
) -> None:
    _, before = await worker.run_step([read_turn("toolu_0"), push_turn()], tools=tools)
    decide(worker, "approve")

    _, after = await worker.resume_step([submit_turn("worker")], tools=tools)

    sent_before = before.calls[-1].messages
    sent_after = after.calls[0].messages
    assert sent_after[: len(sent_before)] == sent_before
    assert sent_after[len(sent_before) :] == [
        Message.from_response(push_turn()),
        Message(role="user", content=tool_results(sent_after[-1])),
    ]
    assert worker.transcript()[: len(sent_after)] == sent_after


# --- crash recovery (Done when 4) -------------------------------------------------


async def test_a_call_started_and_not_finished_is_not_re_run_and_returns_the_interrupted_error(
    worker: Harness,
) -> None:
    crashing = Crashing(worker.tools())
    response = turn(
        tool_use("toolu_a", "read_file", path="ok.txt"),
        tool_use("toolu_b", "run_command", argv=["pytest"]),
        tool_use("toolu_c", "read_file", path="ok.txt"),
    )
    with pytest.raises(RuntimeError, match="power cut"):
        await worker.run_step([response], tools=crashing)
    assert [c.status for c in worker.state().calls] == ["finished", "started", "pending"]

    tools = Recording(worker.tools())
    result, client = await worker.resume_step([submit_turn("worker")], tools=tools)

    assert result.outcome == "ok"
    assert tools.executed == [("read_file", {"path": "ok.txt"})], "toolu_b was not re-run"
    [first] = client.calls
    answers = tool_results(first.messages[-1])
    assert [a.tool_use_id for a in answers] == ["toolu_a", "toolu_b", "toolu_c"]
    assert [a.is_error for a in answers] == [False, True, False]
    interrupted = answers[1].content
    assert "interrupted" in interrupted and "unknown" in interrupted and "pytest" in interrupted
    assert [(c.tool, c.status) for c in worker.state().calls] == [
        ("submit_work_report", "finished")
    ], "state.json holds the last turn's calls once the loop went on"
    logged = [c for c in worker.tool_calls() if c.tool_use_id == "toolu_b"]
    assert logged[-1].error == interrupted and logged[-1].approval_id is None


async def test_a_resumed_step_that_was_running_logs_no_run_resumed_event(
    worker: Harness,
) -> None:
    crashing = Crashing(worker.tools())
    with pytest.raises(RuntimeError):
        await worker.run_step(
            [turn(tool_use("toolu_b", "run_command", argv=["pytest"]))], tools=crashing
        )

    await worker.resume_step([submit_turn("worker")], tools=Recording(worker.tools()))

    kinds = [e.kind for e in worker.run.events()]
    assert "step_resumed" in kinds and "run_resumed" not in kinds and "run_parked" not in kinds


# --- one allowed and one parked (Done when 5) --------------------------------------


async def test_one_allowed_and_one_parked_call_resume_with_both_results_in_one_message(
    worker: Harness, tools: Recording
) -> None:
    result, _ = await worker.run_step([mixed_turn()], tools=tools)
    assert result.outcome == "parked"
    assert tools.executed == [("read_file", {"path": "ok.txt"})]
    decide(worker, "approve")

    result, client = await worker.resume_step([submit_turn("worker")], tools=tools)

    assert result.outcome == "ok"
    assert tools.executed == [
        ("read_file", {"path": "ok.txt"}),
        ("run_command", {"argv": PUSH}),
    ]
    [first] = client.calls
    reply = first.messages[-1]
    assert reply.role == "user"
    assert [b.type for b in reply.content] == ["tool_result", "tool_result"]
    assert [b.tool_use_id for b in reply.content] == ["toolu_r", "toolu_p"]
    assert [b.is_error for b in reply.content] == [False, False]
    assert "1\tfine" in reply.content[0].content and "pushed" in reply.content[1].content
    assert first.messages[-2] == Message.from_response(mixed_turn())


async def test_a_parked_call_denied_does_not_disturb_the_allowed_result(
    worker: Harness, tools: Recording
) -> None:
    await worker.run_step([mixed_turn()], tools=tools)
    decide(worker, "deny", "no")

    _, client = await worker.resume_step([submit_turn("worker")], tools=tools)

    [read, push] = tool_results(client.calls[0].messages[-1])
    assert (read.tool_use_id, read.is_error) == ("toolu_r", False)
    assert (push.tool_use_id, push.is_error) == ("toolu_p", True) and "no" in push.content


# --- what resume refuses -------------------------------------------------------


async def test_resume_with_an_undecided_approval_stays_parked_and_touches_nothing(
    worker: Harness, tools: Recording
) -> None:
    await worker.run_step([push_turn()], tools=tools)
    events_before = worker.run.events()
    state_before = (worker.step_dir / "state.json").read_text()

    result, client = await worker.resume_step([submit_turn("worker")], tools=tools)

    assert result.outcome == "parked"
    assert [c.tool_use_id for c in result.pending] == ["toolu_p"]
    assert "A-0001-01" in result.message and "undecided" in result.message
    assert client.calls == []
    assert tools.executed == []
    assert worker.run.events() == events_before
    assert (worker.step_dir / "state.json").read_text() == state_before


async def test_resume_refuses_a_step_that_never_started(worker: Harness) -> None:
    with pytest.raises(ResumeError, match="state.json"):
        await worker.resume_step([])


async def test_resume_refuses_a_finished_step(worker: Harness) -> None:
    await worker.run_step([submit_turn("worker")])

    with pytest.raises(ResumeError, match="finished"):
        await worker.resume_step([])


async def test_resume_refuses_a_step_whose_approval_entry_is_missing(
    worker: Harness, tools: Recording
) -> None:
    await worker.run_step([push_turn()], tools=tools)
    (worker.run.path / "approvals.jsonl").unlink()

    with pytest.raises(ResumeError, match="A-0001-01"):
        await worker.resume_step([submit_turn("worker")], tools=tools)


# --- budgets across a park -------------------------------------------------------


async def test_time_spent_parked_does_not_count_against_the_wall_budget(
    worker: Harness, tools: Recording
) -> None:
    await worker.run_step([push_turn()], tools=tools, tick_seconds=10, max_wall_seconds=100)
    decide(worker, "approve")
    worker.clock.advance(3600)

    result, _ = await worker.resume_step(
        [submit_turn("worker")], tools=tools, tick_seconds=10, max_wall_seconds=100
    )

    assert result.outcome == "ok"
    assert result.counters.wall_seconds == pytest.approx(20)
    assert result.counters.turns == 2


async def test_the_other_budget_counters_carry_over_a_park(
    worker: Harness, tools: Recording
) -> None:
    await worker.run_step([push_turn()], tools=tools)
    decide(worker, "approve")

    result, client = await worker.resume_step(
        [read_turn("toolu_1"), read_turn("toolu_2")], tools=tools, max_turns=2
    )

    assert result.outcome == "failed" and result.reason == "budget_exceeded"
    assert len(client.calls) == 1
    assert result.counters.total_tokens == 20


async def test_repeat_detection_is_not_fooled_by_a_park(worker: Harness, tools: Recording) -> None:
    await worker.run_step([push_turn()], tools=tools)
    decide(worker, "approve")

    result, client = await worker.resume_step(
        [read_turn("toolu_1"), read_turn("toolu_2"), submit_turn("worker")], tools=tools
    )

    assert result.outcome == "ok"
    second = tool_results(client.calls[1].messages[-1])[0].content
    assert "identical" not in second
    assert worker.state().repeat.count == 1


# --- the executor seam ---------------------------------------------------------------


def test_resume_step_is_part_of_the_executor_protocol(worker: Harness) -> None:
    executor = worker.executor(TickingClient([], worker.clock, 0))

    assert isinstance(executor, Executor)
    assert isinstance(ApiLoopExecutor.resume_step, type(ApiLoopExecutor.run_step))


def test_the_state_file_round_trips_the_approval_id() -> None:
    state = StepState.model_validate(
        {
            "step": "implement",
            "attempt": 1,
            "role": "worker",
            "started_at": "2026-11-17T09:00:00Z",
            "calls": [
                {
                    "tool_use_id": "toolu_p",
                    "tool": "run_command",
                    "arguments": {"argv": PUSH},
                    "decision": "park",
                    "approval_id": "A-0001-01",
                }
            ],
        }
    )

    assert StepState.from_json(state.to_json()) == state
    assert state.calls[0].approval_id == "A-0001-01"
    assert state.started_at + timedelta(0) == state.started_at
