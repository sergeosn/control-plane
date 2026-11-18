"""The OpenAI-compatible client (docs/SPEC.md sections 8.2, 8.3 and 14).

The gateway is a LiteLLM proxy that speaks the chat-completions format, so this client
translates between the engine's vocabulary (`control_plane.llm.types`, modelled on the
Messages API) and that format:

- a tool becomes `{"type": "function", "function": {name, description, parameters, strict}}`;
  `strict` is sent only when both the profile and the tool definition ask for it;
- an assistant `tool_use` block becomes a `tool_calls` entry whose `arguments` is a JSON
  string, and one comes back the same way;
- each `tool_result` block becomes one `{"role": "tool"}` message. The format has no
  `is_error` flag, so an error result is prefixed with `Error: `;
- `finish_reason` maps `tool_calls` to `tool_use`, `stop` to `end_turn`, `length` to
  `max_tokens` and `content_filter` to `refusal`;
- usage: `prompt_tokens` counts the cached and cache-written tokens too, so they are taken
  out of `input_tokens`, which keeps the four classes disjoint, as the price table expects.
  Cache writes come from `prompt_tokens_details.cache_creation_tokens` (the proxy's name)
  or `cache_write_tokens` (OpenAI's).

Credentials are passed explicitly: `CPLANE_LLM_BASE_URL` and `CPLANE_LLM_AUTH_TOKEN` through
`from_environment`. The SDK's own `OPENAI_*` variables are never consulted. The SDK retries
rate-limit and server errors; a 429 that outlives the retries becomes `RateLimitedError`
with the time to resume at (section 8.2), which the loop turns into a park.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Self

import httpx2
from openai import AsyncOpenAI, RateLimitError
from openai.types.chat import ChatCompletion

from control_plane.llm.client import RateLimitedError
from control_plane.llm.types import (
    Message,
    Response,
    ResponseBlock,
    StopReason,
    TextBlock,
    ToolDefinition,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)

BASE_URL_VAR = "CPLANE_LLM_BASE_URL"
AUTH_TOKEN_VAR = "CPLANE_LLM_AUTH_TOKEN"

DEFAULT_MAX_RETRIES = 2
DEFAULT_RATE_LIMIT_WAIT = timedelta(seconds=60)
"""How long to park when a 429 carries no usable `Retry-After`."""

STOP_REASONS: dict[str, StopReason] = {
    "tool_calls": "tool_use",
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "refusal",
}

ERROR_PREFIX = "Error: "

Clock = Callable[[], datetime]


def _wall_clock() -> datetime:
    return datetime.now(UTC)


class CredentialsError(Exception):
    """A credential variable is missing or empty. The message names the variable."""


# --- request --------------------------------------------------------------------


def request_tools(tools: Sequence[ToolDefinition], *, strict: bool) -> list[dict[str, Any]]:
    """Our tool definitions as chat-completions function tools."""
    mapped = []
    for tool in tools:
        function: dict[str, Any] = {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        }
        if strict and tool.strict:
            function["strict"] = True
        mapped.append({"type": "function", "function": function})
    return mapped


def request_messages(system: str, messages: Sequence[Message]) -> list[dict[str, Any]]:
    """The system prompt and the conversation as chat-completions messages."""
    mapped: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for message in messages:
        if message.role == "assistant":
            mapped.append(_assistant_message(message))
        else:
            mapped.extend(_user_messages(message))
    return mapped


def _assistant_message(message: Message) -> dict[str, Any]:
    texts = [block.text for block in message.content if isinstance(block, TextBlock)]
    calls = [
        {
            "id": block.id,
            "type": "function",
            "function": {"name": block.name, "arguments": json.dumps(block.input)},
        }
        for block in message.content
        if isinstance(block, ToolUseBlock)
    ]
    mapped: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts) or None}
    if calls:
        mapped["tool_calls"] = calls
    return mapped


def _user_messages(message: Message) -> list[dict[str, Any]]:
    """Tool results each as a `tool` message, consecutive text blocks as one user message."""
    mapped: list[dict[str, Any]] = []
    texts: list[str] = []

    def flush() -> None:
        if texts:
            mapped.append({"role": "user", "content": "\n\n".join(texts)})
            texts.clear()

    for block in message.content:
        if isinstance(block, ToolResultBlock):
            flush()
            content = ERROR_PREFIX + block.content if block.is_error else block.content
            mapped.append({"role": "tool", "tool_call_id": block.tool_use_id, "content": content})
        elif isinstance(block, TextBlock):
            texts.append(block.text)
    flush()
    return mapped


# --- response -------------------------------------------------------------------


def parse_response(completion: ChatCompletion) -> Response:
    """Our `Response` from a chat completion. Raises `ValueError` on a shape the loop
    cannot act on: no choice, no usage, an unknown finish reason, or tool arguments that
    are not a JSON object."""
    if not completion.choices:
        raise ValueError("the completion has no choices")
    choice = completion.choices[0]
    stop_reason = STOP_REASONS.get(choice.finish_reason)
    if stop_reason is None:
        raise ValueError(
            f"unknown finish_reason {choice.finish_reason!r}; known: {', '.join(STOP_REASONS)}"
        )
    if completion.usage is None:
        raise ValueError("the completion has no usage, so it cannot be priced")
    content: list[ResponseBlock] = []
    if choice.message.content:
        content.append(TextBlock(text=choice.message.content))
    for call in choice.message.tool_calls or []:
        if call.type != "function":
            raise ValueError(f"unsupported tool call type {call.type!r} for call {call.id}")
        content.append(
            ToolUseBlock(
                id=call.id,
                name=call.function.name,
                input=_arguments(call.function.name, call.function.arguments),
            )
        )
    return Response(
        model=completion.model,
        content=content,
        stop_reason=stop_reason,
        usage=_usage(completion),
    )


def _arguments(tool: str, raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError as error:
        raise ValueError(f"tool call {tool}: arguments are not valid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ValueError(f"tool call {tool}: arguments must be a JSON object, got {raw!r}")
    return parsed


def _usage(completion: ChatCompletion) -> Usage:
    usage = completion.usage
    assert usage is not None
    details = usage.prompt_tokens_details
    cache_read = (details.cached_tokens if details else None) or 0
    # The proxy reports cache writes as `cache_creation_tokens`; OpenAI's own spelling is
    # `cache_write_tokens`. Either counts.
    cache_write = 0
    if details is not None:
        cache_write = (
            getattr(details, "cache_creation_tokens", None) or details.cache_write_tokens or 0
        )
    return Usage(
        input_tokens=max(usage.prompt_tokens - cache_read - cache_write, 0),
        output_tokens=usage.completion_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
    )


# --- rate limits ----------------------------------------------------------------


def resume_after(error: RateLimitError, now: datetime) -> datetime:
    """When to try again: `Retry-After` in seconds or as an HTTP date, else the default."""
    header = error.response.headers.get("retry-after")
    if header:
        try:
            return now + timedelta(seconds=float(header))
        except ValueError:
            pass
        try:
            return parsedate_to_datetime(header).astimezone(UTC)
        except (TypeError, ValueError):
            pass
    return now + DEFAULT_RATE_LIMIT_WAIT


# --- the client -----------------------------------------------------------------


class OpenAICompatibleClient:
    """`LLMClient` over a chat-completions endpoint, built for one model profile."""

    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        api_key: str,
        max_tokens: int = 8192,
        temperature: float | None = None,
        strict: bool = True,
        max_retries: int = DEFAULT_MAX_RETRIES,
        http_client: httpx2.AsyncClient | None = None,
        clock: Clock = _wall_clock,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.strict = strict
        self.max_retries = max_retries
        self._clock = clock
        self._sdk = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=max_retries,
            http_client=http_client,
        )

    @classmethod
    def from_environment(cls, environ: Mapping[str, str], *, model: str, **options: Any) -> Self:
        """A client whose gateway and bearer token come from `CPLANE_LLM_BASE_URL` and
        `CPLANE_LLM_AUTH_TOKEN` of `environ`. Raises `CredentialsError` naming the one
        that is missing. No other variable is read."""
        values = {}
        for name in (BASE_URL_VAR, AUTH_TOKEN_VAR):
            value = environ.get(name, "").strip()
            if not value:
                raise CredentialsError(f"{name} is not set; the gateway needs it")
            values[name] = value
        return cls(model, base_url=values[BASE_URL_VAR], api_key=values[AUTH_TOKEN_VAR], **options)

    async def complete(
        self,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        *,
        max_tokens: int | None = None,
    ) -> Response:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": request_messages(system, messages),
            "max_completion_tokens": max_tokens or self.max_tokens,
            "stream": False,
        }
        if tools:
            request["tools"] = request_tools(tools, strict=self.strict)
        if self.temperature is not None:
            request["temperature"] = self.temperature
        try:
            completion = await self._sdk.chat.completions.create(**request)
        except RateLimitError as error:
            now = self._clock()
            until = resume_after(error, now)
            wait = int((until - now).total_seconds())
            raise RateLimitedError(
                until,
                f"the endpoint answered 429 for {self.model} after {self.max_retries} "
                f"retries; try again after {until.isoformat()} ({wait}s)",
            ) from error
        return parse_response(completion)
