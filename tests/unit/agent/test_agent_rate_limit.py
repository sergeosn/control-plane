"""Task 10: a persistent rate limit parks the step with its `resume_after` (SPEC.md 8.2, 12).

The client raises `RateLimitedError` the way the real client does once the SDK's retries are
spent. The loop parks the run without an approval entry, keeps the time in `state.json`, and
a resume before that time changes nothing.
"""

from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

import pytest

from control_plane.agent import REASON_RATE_LIMIT, ApiLoopExecutor
from control_plane.llm import Message, RateLimitedError, Response, ToolDefinition
from fixtures.loop_harness import (
    T0,
    Harness,
    TickingClient,
    read_turn,
    submit_turn,
    tool_results,
)

RESUME_AFTER = T0 + timedelta(minutes=2)


class RateLimitingClient(TickingClient):
    """A scripted client whose call number `limited_call` raises `RateLimitedError`."""

    def __init__(self, responses: Sequence[Response], harness: Harness, limited_call: int) -> None:
        super().__init__(responses, harness.clock, 0.0)
        self.limited_call = limited_call
        self.count = 0

    async def complete(
        self,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        *,
        max_tokens: int | None = None,
    ) -> Response:
        self.count += 1
        if self.count == self.limited_call:
            raise RateLimitedError(RESUME_AFTER, "429 after 2 retries")
        return await super().complete(system, messages, tools, max_tokens=max_tokens)


@pytest.fixture
def worker(tmp_path: Path) -> Harness:
    return Harness(tmp_path, "worker")


async def park(worker: Harness, limited_call: int = 1, responses: Sequence[Response] = ()) -> None:
    client = RateLimitingClient(responses, worker, limited_call)
    result = await worker.executor(client).run_step(worker.context())
    assert result.outcome == "parked"


async def test_a_rate_limited_model_call_parks_the_step_with_the_resume_time(
    worker: Harness,
) -> None:
    client = RateLimitingClient([], worker, limited_call=1)

    result = await worker.executor(client).run_step(worker.context())

    assert result.outcome == "parked"
    assert result.reason == REASON_RATE_LIMIT
    assert result.resume_after == RESUME_AFTER
    assert result.pending == []
    state = worker.state()
    assert state.status == "parked"
    assert state.resume_after == RESUME_AFTER
    assert worker.approvals() == []
    assert [m.role for m in worker.transcript()] == ["user"]
    assert worker.llm_calls() == []


async def test_the_park_logs_a_run_parked_event_that_carries_the_time(worker: Harness) -> None:
    await park(worker)

    events = {e.kind: e for e in worker.run.events()}
    assert events["run_parked"].outcome == "parked"
    assert RESUME_AFTER.isoformat() in (events["run_parked"].message or "")
    assert events["step_parked"].message is not None
    assert REASON_RATE_LIMIT in events["step_parked"].message


async def test_resume_before_the_time_stays_parked_and_calls_no_model(worker: Harness) -> None:
    await park(worker)
    worker.clock.now = RESUME_AFTER - timedelta(seconds=1)

    result, client = await worker.resume_step([submit_turn("worker")])

    assert result.outcome == "parked"
    assert result.reason == REASON_RATE_LIMIT
    assert result.resume_after == RESUME_AFTER
    assert client.calls == []
    assert worker.state().status == "parked"


async def test_resume_at_the_time_continues_the_same_conversation(worker: Harness) -> None:
    await park(worker)
    worker.clock.now = RESUME_AFTER

    result, client = await worker.resume_step([submit_turn("worker")])

    assert result.outcome == "ok"
    assert [m.role for m in client.calls[0].messages] == ["user"]
    assert client.calls[0].messages[0].content[0].text == worker.transcript()[0].content[0].text
    state = worker.state()
    assert state.status == "finished"
    assert state.resume_after is None
    kinds = [e.kind for e in worker.run.events()]
    assert kinds.index("run_resumed") > kinds.index("run_parked")


async def test_a_rate_limit_after_a_tool_result_resumes_with_the_result_in_place(
    worker: Harness,
) -> None:
    await park(worker, limited_call=2, responses=[read_turn("toolu_r")])
    worker.clock.now = RESUME_AFTER + timedelta(seconds=30)

    result, client = await worker.resume_step([submit_turn("worker")])

    assert result.outcome == "ok"
    sent = client.calls[0].messages
    assert [m.role for m in sent] == ["user", "assistant", "user"]
    assert [r.tool_use_id for r in tool_results(sent[2])] == ["toolu_r"]
    assert len(worker.tool_calls()) == 2


async def test_parked_time_does_not_count_against_the_wall_budget(worker: Harness) -> None:
    await park(worker)
    worker.clock.now = RESUME_AFTER + timedelta(hours=1)

    result, _ = await worker.resume_step([submit_turn("worker")], max_wall_seconds=600)

    assert result.outcome == "ok"


async def test_the_executor_protocol_result_carries_resume_after_by_default_none(
    worker: Harness,
) -> None:
    result, _ = await worker.run_step([submit_turn("worker")])

    assert isinstance(worker.executor(TickingClient([], worker.clock, 0)), ApiLoopExecutor)
    assert result.resume_after is None
