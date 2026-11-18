"""Model call errors park or fail the step, never crash it (docs/SPEC.md sections 8.2 and 12).

Each row of the table: a 5xx parks with `server_error` until a time, like `connection`; a
rejected token parks with `auth` and no time; a 404 fails with `model_not_found`; a 400 or
422 fails with `bad_request`, or `model_context_window_exceeded` when the context is too
long; any other model error fails with `model_error`.
"""

from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

import pytest

from control_plane.agent import (
    REASON_AUTH,
    REASON_BAD_REQUEST,
    REASON_CONTEXT_WINDOW,
    REASON_MODEL_ERROR,
    REASON_MODEL_NOT_FOUND,
    REASON_SERVER_ERROR,
    StepResult,
)
from control_plane.llm import (
    AuthRejectedError,
    InvalidRequestError,
    Message,
    ModelCallError,
    ModelNotFoundError,
    Response,
    ServerFailedError,
    ToolDefinition,
)
from fixtures.loop_harness import T0, Harness, TickingClient, submit_turn

RESUME_AFTER = T0 + timedelta(minutes=1)


class FailsOnce(TickingClient):
    """A scripted client whose first call raises `error`."""

    def __init__(self, harness: Harness, error: Exception) -> None:
        super().__init__([], harness.clock, 0.0)
        self.error: Exception | None = error

    async def complete(
        self,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        *,
        max_tokens: int | None = None,
    ) -> Response:
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        return await super().complete(system, messages, tools, max_tokens=max_tokens)


@pytest.fixture
def worker(tmp_path: Path) -> Harness:
    return Harness(tmp_path, "worker")


async def run_failing(worker: Harness, error: Exception) -> StepResult:
    return await worker.executor(FailsOnce(worker, error)).run_step(worker.context())


# --- 5xx: park until a time --------------------------------------------------------------


async def test_a_server_error_parks_with_reason_server_error_until_a_time(
    worker: Harness,
) -> None:
    result = await run_failing(worker, ServerFailedError(RESUME_AFTER, "503 after 2 retries"))

    assert (result.outcome, result.reason, result.resume_after) == (
        "parked",
        REASON_SERVER_ERROR,
        RESUME_AFTER,
    )
    assert worker.approvals() == []
    events = {e.kind: e for e in worker.run.events()}
    assert "server error" in (events["run_parked"].message or "")


async def test_a_server_error_park_resumes_only_after_the_time(worker: Harness) -> None:
    await run_failing(worker, ServerFailedError(RESUME_AFTER, "503"))
    worker.clock.now = RESUME_AFTER - timedelta(seconds=1)

    early, client = await worker.resume_step([submit_turn("worker")])
    assert (early.outcome, early.reason) == ("parked", REASON_SERVER_ERROR)
    assert client.calls == []

    worker.clock.now = RESUME_AFTER
    result, _ = await worker.resume_step([submit_turn("worker")])
    assert result.outcome == "ok"


# --- 401, 403: park until the token is replaced ------------------------------------------


async def test_a_rejected_token_parks_with_reason_auth_and_no_time(worker: Harness) -> None:
    result = await run_failing(worker, AuthRejectedError("401; update CPLANE_LLM_AUTH_TOKEN"))

    assert (result.outcome, result.reason, result.resume_after) == ("parked", REASON_AUTH, None)
    state = worker.state()
    assert (state.status, state.reason, state.resume_after) == ("parked", REASON_AUTH, None)
    assert worker.approvals() == []
    events = {e.kind: e for e in worker.run.events()}
    assert "rejected the token" in (events["run_parked"].message or "")
    assert "CPLANE_LLM_AUTH_TOKEN" in (events["step_parked"].message or "")


async def test_an_auth_park_resumes_at_once_with_the_same_conversation(worker: Harness) -> None:
    await run_failing(worker, AuthRejectedError("401"))

    result, client = await worker.resume_step([submit_turn("worker")])

    assert result.outcome == "ok"
    assert [m.role for m in client.calls[0].messages] == ["user"]
    kinds = [e.kind for e in worker.run.events()]
    assert kinds.index("run_resumed") > kinds.index("run_parked")


# --- failures ----------------------------------------------------------------------------


async def test_a_404_fails_the_step_with_reason_model_not_found(worker: Harness) -> None:
    result = await run_failing(
        worker, ModelNotFoundError("no model 'gpt-x'; check the model id in plane.yaml")
    )

    assert (result.outcome, result.reason) == ("failed", REASON_MODEL_NOT_FOUND)
    assert "gpt-x" in result.message and "plane.yaml" in result.message
    assert worker.state().status == "finished"


async def test_a_rejected_request_fails_the_step_with_reason_bad_request(
    worker: Harness,
) -> None:
    result = await run_failing(worker, InvalidRequestError("400: tools[0] is invalid"))

    assert (result.outcome, result.reason) == ("failed", REASON_BAD_REQUEST)
    assert "tools[0] is invalid" in result.message


async def test_a_context_that_is_too_long_fails_as_context_window_exceeded(
    worker: Harness,
) -> None:
    error = InvalidRequestError("400: maximum context length", context_window_exceeded=True)

    result = await run_failing(worker, error)

    assert (result.outcome, result.reason) == ("failed", REASON_CONTEXT_WINDOW)
    assert REASON_CONTEXT_WINDOW == "model_context_window_exceeded"


async def test_any_other_model_error_fails_the_step_with_reason_model_error(
    worker: Harness,
) -> None:
    result = await run_failing(worker, ModelCallError("answered 418"))

    assert (result.outcome, result.reason) == ("failed", REASON_MODEL_ERROR)
    assert "418" in result.message


def test_the_reasons_are_the_ones_the_spec_names() -> None:
    assert (REASON_SERVER_ERROR, REASON_AUTH, REASON_MODEL_NOT_FOUND, REASON_BAD_REQUEST) == (
        "server_error",
        "auth",
        "model_not_found",
        "bad_request",
    )
