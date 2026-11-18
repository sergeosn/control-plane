"""An unreachable model endpoint parks the step like a persistent rate limit (docs/SPEC.md 8.2, 12).

The client raises `EndpointUnreachableError` the way the real client does when the SDK gives
up on a connection error or a timeout. The loop parks the run with reason `connection`
and `resume_after`, without an approval entry, and a resume before that time changes nothing.
"""

from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

import pytest

from control_plane.agent import REASON_CONNECTION, REASON_RATE_LIMIT
from control_plane.llm import EndpointUnreachableError, Message, Response, ToolDefinition
from fixtures.loop_harness import T0, Harness, TickingClient, submit_turn

RESUME_AFTER = T0 + timedelta(minutes=1)


class UnreachableClient(TickingClient):
    """A scripted client whose first call raises `EndpointUnreachableError`."""

    def __init__(self, responses: Sequence[Response], harness: Harness) -> None:
        super().__init__(responses, harness.clock, 0.0)
        self.failed = False

    async def complete(
        self,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        *,
        max_tokens: int | None = None,
    ) -> Response:
        if not self.failed:
            self.failed = True
            raise EndpointUnreachableError(RESUME_AFTER, "https://gateway.test/v1: refused")
        return await super().complete(system, messages, tools, max_tokens=max_tokens)


@pytest.fixture
def worker(tmp_path: Path) -> Harness:
    return Harness(tmp_path, "worker")


async def park(worker: Harness) -> None:
    result = await worker.executor(UnreachableClient([], worker)).run_step(worker.context())
    assert result.outcome == "parked"


async def test_an_unreachable_endpoint_parks_the_step_with_reason_connection(
    worker: Harness,
) -> None:
    result = await worker.executor(UnreachableClient([], worker)).run_step(worker.context())

    assert (result.outcome, result.reason) == ("parked", REASON_CONNECTION)
    assert result.resume_after == RESUME_AFTER
    assert result.pending == []
    state = worker.state()
    assert (state.status, state.reason, state.resume_after) == (
        "parked",
        REASON_CONNECTION,
        RESUME_AFTER,
    )
    assert worker.approvals() == []
    assert worker.llm_calls() == []


async def test_the_park_event_says_the_endpoint_could_not_be_reached(worker: Harness) -> None:
    await park(worker)

    events = {e.kind: e for e in worker.run.events()}
    parked = events["run_parked"].message or ""
    assert "could not be reached" in parked and RESUME_AFTER.isoformat() in parked
    assert REASON_CONNECTION in (events["step_parked"].message or "")


async def test_resume_before_the_time_stays_parked_with_reason_connection(
    worker: Harness,
) -> None:
    await park(worker)
    worker.clock.now = RESUME_AFTER - timedelta(seconds=1)

    result, client = await worker.resume_step([submit_turn("worker")])

    assert (result.outcome, result.reason) == ("parked", REASON_CONNECTION)
    assert result.resume_after == RESUME_AFTER
    assert "could not be reached" in result.message
    assert client.calls == []


async def test_resume_at_the_time_continues_the_same_conversation(worker: Harness) -> None:
    await park(worker)
    worker.clock.now = RESUME_AFTER

    result, client = await worker.resume_step([submit_turn("worker")])

    assert result.outcome == "ok"
    assert [m.role for m in client.calls[0].messages] == ["user"]
    state = worker.state()
    assert (state.status, state.resume_after) == ("finished", None)


def test_the_two_reasons_differ() -> None:
    assert REASON_CONNECTION == "connection" != REASON_RATE_LIMIT
