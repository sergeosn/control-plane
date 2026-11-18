"""The `LLMClient` protocol and `ScriptedClient` (docs/SPEC.md sections 8.2 and 16).

A client is built for one model profile, so a call carries only what changes between turns:
the system prompt, the conversation and the tools. `max_tokens` can be raised for one call,
which the loop does once after a `max_tokens` stop (section 8.2).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from control_plane.llm.types import Message, Response, ToolDefinition


@runtime_checkable
class LLMClient(Protocol):
    async def complete(
        self,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        *,
        max_tokens: int | None = None,
    ) -> Response: ...


class ModelCallError(Exception):
    """A model call failed (docs/SPEC.md section 8.2). Any `LLMClient` may raise it or one of
    its subclasses, and the loop parks or fails the step; it never reaches the user as a
    traceback. Raised as itself, it is an error no subclass covers: the step fails with
    reason `model_error`."""


class RetryLaterError(ModelCallError):
    """A model error that passes with time: the run parks until `resume_after`."""

    def __init__(self, resume_after: datetime, message: str) -> None:
        super().__init__(message)
        self.resume_after = resume_after
        """The earliest time a resume should try the call again."""


class RateLimitedError(RetryLaterError):
    """A rate limit outlived the client's retries. The run parks with reason `rate_limit`
    until `resume_after`."""


class EndpointUnreachableError(RetryLaterError):
    """A connection error or a timeout outlived the client's retries. The run parks with
    reason `connection` until `resume_after`."""


class ServerFailedError(RetryLaterError):
    """The endpoint answered with a server error (5xx) after the client's retries. The run
    parks with reason `server_error` until `resume_after`."""


class AuthRejectedError(ModelCallError):
    """The endpoint rejected the token (401 or 403). The run parks with reason `auth` and no
    time: a resume after the token is replaced tries again."""


class ModelNotFoundError(ModelCallError):
    """The endpoint does not know the model id (404). The step fails: `model_not_found`."""


class InvalidRequestError(ModelCallError):
    """The endpoint rejected the request (400 or 422). The step fails with reason
    `bad_request`, or `model_context_window_exceeded` when the message says the context is
    too long."""

    def __init__(self, message: str, *, context_window_exceeded: bool = False) -> None:
        super().__init__(message)
        self.context_window_exceeded = context_window_exceeded


class ScriptExhaustedError(Exception):
    """The scripted client was asked for more responses than its script holds."""


@dataclass(frozen=True)
class RecordedCall:
    """What one `complete` call was given, kept so a test can assert on it."""

    system: str
    messages: list[Message]
    tools: list[ToolDefinition]
    max_tokens: int | None


class ScriptedClient:
    """Replays prepared responses in order and records every call. Never touches the network."""

    def __init__(self, responses: Sequence[Response]) -> None:
        self._script = list(responses)
        self._next = 0
        self.calls: list[RecordedCall] = []

    @property
    def remaining(self) -> int:
        return len(self._script) - self._next

    async def complete(
        self,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        *,
        max_tokens: int | None = None,
    ) -> Response:
        self.calls.append(RecordedCall(system, list(messages), list(tools), max_tokens))
        if self._next >= len(self._script):
            count = len(self._script)
            noun = "response" if count == 1 else "responses"
            raise ScriptExhaustedError(
                f"script exhausted: it holds {count} {noun} and call {self._next + 1} "
                "has nothing to replay"
            )
        response = self._script[self._next]
        self._next += 1
        return response
