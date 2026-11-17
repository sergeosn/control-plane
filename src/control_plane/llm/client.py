"""The `LLMClient` protocol and `ScriptedClient` (SPEC.md sections 8.2 and 16).

A client is built for one model profile, so a call carries only what changes between turns:
the system prompt, the conversation and the tools. `max_tokens` can be raised for one call,
which the loop does once after a `max_tokens` stop (section 8.2).
"""

from collections.abc import Sequence
from dataclasses import dataclass
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
