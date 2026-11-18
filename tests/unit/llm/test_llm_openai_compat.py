"""Task 10: the OpenAI-compatible client (docs/SPEC.md sections 8.2, 8.3 and 14).

Every request is answered by an `httpx2.MockTransport`, so no test opens a socket. The
mapping tests use `tests/fixtures/llm/openai_tool_call.json`, a real response of the proxy.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from openai import InternalServerError
from openai.types.chat import ChatCompletion

from control_plane.llm import (
    LLMClient,
    Message,
    RateLimitedError,
    TextBlock,
    ToolDefinition,
    ToolResultBlock,
    ToolUseBlock,
)
from control_plane.llm.openai_compat import (
    DEFAULT_RATE_LIMIT_WAIT,
    CredentialsError,
    OpenAICompatibleClient,
    parse_response,
    request_messages,
    request_tools,
)

FIXTURE = Path(__file__).parents[2] / "fixtures" / "llm" / "openai_tool_call.json"
BASE_URL = "https://gateway.test/v1"
TOKEN = "token-for-tests"
MODEL = "bedrock_mantle/openai/gpt-5.6-luna"
NOW = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)

READ_FILE = ToolDefinition(
    name="read_file",
    description="Read a file.",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    },
    strict=True,
)
LIST_FILES = ToolDefinition(
    name="list_files",
    description="List files.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    strict=False,
)


def fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def completion(
    *,
    finish_reason: str = "stop",
    content: str | None = "Hello",
    tool_calls: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A chat completion body in the proxy's shape, with the fixture as the template."""
    body = fixture()
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    body["choices"] = [{"finish_reason": finish_reason, "index": 0, "message": message}]
    if usage is not None:
        body["usage"] = usage
    return body


def tool_call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "index": 0,
        "function": {"name": name, "arguments": arguments},
    }


class FakeEndpoint:
    """Serves prepared HTTP replies in order and records every request it got."""

    def __init__(self, *replies: httpx2.Response) -> None:
        self.replies = list(replies)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if not self.replies:
            raise AssertionError("the fake endpoint got more requests than it has replies")
        return self.replies.pop(0)

    def http_client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self))

    def body(self, index: int = 0) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


def ok(body: dict[str, Any]) -> httpx2.Response:
    return httpx2.Response(200, json=body)


def too_many(headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(429, json={"error": {"message": "rate limited"}}, headers=headers or {})


def client(
    endpoint: FakeEndpoint,
    *,
    max_retries: int = 0,
    strict: bool = True,
    temperature: float | None = None,
    max_tokens: int = 4096,
    clock: Callable[[], datetime] = lambda: NOW,
) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        model=MODEL,
        base_url=BASE_URL,
        api_key=TOKEN,
        max_tokens=max_tokens,
        temperature=temperature,
        strict=strict,
        max_retries=max_retries,
        http_client=endpoint.http_client(),
        clock=clock,
    )


# --- the request ----------------------------------------------------------------


def test_the_client_satisfies_the_protocol() -> None:
    assert isinstance(client(FakeEndpoint()), LLMClient)


async def test_a_call_posts_to_chat_completions_with_the_bearer_token() -> None:
    endpoint = FakeEndpoint(ok(fixture()))

    await client(endpoint).complete("Be brief.", [Message.user("Read README.md")], [READ_FILE])

    request = endpoint.requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"{BASE_URL}/chat/completions"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    body = endpoint.body()
    assert body["model"] == MODEL
    assert body["messages"][0] == {"role": "system", "content": "Be brief."}
    assert body["messages"][1] == {"role": "user", "content": "Read README.md"}
    assert body["max_completion_tokens"] == 4096
    assert "temperature" not in body
    assert body["stream"] is False


async def test_max_tokens_can_be_raised_for_one_call() -> None:
    endpoint = FakeEndpoint(ok(fixture()), ok(fixture()))
    llm = client(endpoint)

    await llm.complete("s", [Message.user("go")], [], max_tokens=16384)
    await llm.complete("s", [Message.user("go")], [])

    assert endpoint.body(0)["max_completion_tokens"] == 16384
    assert endpoint.body(1)["max_completion_tokens"] == 4096


async def test_a_profile_temperature_is_sent_when_set() -> None:
    endpoint = FakeEndpoint(ok(fixture()))

    await client(endpoint, temperature=0.0).complete("s", [Message.user("go")], [])

    assert endpoint.body()["temperature"] == 0.0


async def test_no_tools_key_is_sent_without_tools() -> None:
    endpoint = FakeEndpoint(ok(fixture()))

    await client(endpoint).complete("s", [Message.user("go")], [])

    assert "tools" not in endpoint.body()


def test_tools_become_function_tools_and_strict_follows_the_definition() -> None:
    assert request_tools([READ_FILE, LIST_FILES], strict=True) == [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file.",
                "parameters": READ_FILE.input_schema,
                "strict": True,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List files.",
                "parameters": LIST_FILES.input_schema,
            },
        },
    ]


def test_a_profile_with_strict_off_declares_no_tool_strict() -> None:
    tools = request_tools([READ_FILE, LIST_FILES], strict=False)

    assert all("strict" not in tool["function"] for tool in tools)


def test_an_assistant_turn_becomes_tool_calls_with_json_string_arguments() -> None:
    assistant = Message(
        role="assistant",
        content=[
            TextBlock(text="Let me look."),
            ToolUseBlock(id="call_1", name="read_file", input={"path": "a.py"}),
            ToolUseBlock(id="call_2", name="read_file", input={"path": "b.py"}),
        ],
    )

    assert request_messages("sys", [assistant]) == [
        {"role": "system", "content": "sys"},
        {
            "role": "assistant",
            "content": "Let me look.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "b.py"}'},
                },
            ],
        },
    ]


def test_an_assistant_turn_without_text_sends_null_content() -> None:
    assistant = Message(
        role="assistant",
        content=[ToolUseBlock(id="call_1", name="read_file", input={"path": "a.py"})],
    )

    [_, mapped] = request_messages("sys", [assistant])

    assert mapped["content"] is None
    assert [call["id"] for call in mapped["tool_calls"]] == ["call_1"]


def test_each_tool_result_becomes_one_tool_message_and_an_error_is_prefixed() -> None:
    user = Message(
        role="user",
        content=[
            ToolResultBlock(tool_use_id="call_1", content="line 1\n"),
            ToolResultBlock(tool_use_id="call_2", content="no such file", is_error=True),
            TextBlock(text="Carry on."),
        ],
    )

    assert request_messages("sys", [user]) == [
        {"role": "system", "content": "sys"},
        {"role": "tool", "tool_call_id": "call_1", "content": "line 1\n"},
        {"role": "tool", "tool_call_id": "call_2", "content": "Error: no such file"},
        {"role": "user", "content": "Carry on."},
    ]


def test_consecutive_user_text_blocks_join_into_one_user_message() -> None:
    user = Message(role="user", content=[TextBlock(text="one"), TextBlock(text="two")])

    assert request_messages("sys", [user])[1] == {"role": "user", "content": "one\n\ntwo"}


# --- the response ---------------------------------------------------------------


def test_the_proxy_fixture_maps_to_a_tool_use_response() -> None:
    response = parse_response(ChatCompletion.model_validate(fixture()))

    assert response.model == MODEL
    assert response.stop_reason == "tool_use"
    assert response.content == [
        ToolUseBlock(
            id="call_138c2400c3895609a601dd62608a0807",
            name="read_file",
            input={"path": "README.md"},
        )
    ]
    assert response.text == ""
    assert response.usage.input_tokens == 45
    assert response.usage.output_tokens == 19
    assert response.usage.cache_read_input_tokens == 0
    assert response.usage.cache_creation_input_tokens == 0


@pytest.mark.parametrize(
    ("finish_reason", "stop_reason"),
    [
        ("tool_calls", "tool_use"),
        ("stop", "end_turn"),
        ("length", "max_tokens"),
        ("content_filter", "refusal"),
    ],
)
async def test_each_finish_reason_maps_to_its_stop_reason(
    finish_reason: str, stop_reason: str
) -> None:
    endpoint = FakeEndpoint(ok(completion(finish_reason=finish_reason)))

    response = await client(endpoint).complete("s", [Message.user("go")], [])

    assert response.stop_reason == stop_reason


def test_an_unknown_finish_reason_is_an_error_that_names_it() -> None:
    body = ChatCompletion.model_validate(completion(finish_reason="function_call"))

    with pytest.raises(ValueError, match="function_call"):
        parse_response(body)


async def test_usage_maps_all_four_token_classes_without_double_counting() -> None:
    endpoint = FakeEndpoint(
        ok(
            completion(
                usage={
                    "prompt_tokens": 100,
                    "completion_tokens": 7,
                    "total_tokens": 107,
                    "prompt_tokens_details": {"cached_tokens": 30, "cache_creation_tokens": 20},
                }
            )
        )
    )

    usage = (await client(endpoint).complete("s", [Message.user("go")], [])).usage

    assert usage.input_tokens == 50
    assert usage.output_tokens == 7
    assert usage.cache_read_input_tokens == 30
    assert usage.cache_creation_input_tokens == 20
    assert usage.total_tokens == 107


def test_usage_without_prompt_details_has_zero_cache_counts() -> None:
    body = completion(usage={"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15})

    usage = parse_response(ChatCompletion.model_validate(body)).usage

    assert (usage.input_tokens, usage.output_tokens) == (12, 3)
    assert (usage.cache_read_input_tokens, usage.cache_creation_input_tokens) == (0, 0)


def test_a_response_without_usage_is_an_error() -> None:
    body = fixture()
    body["usage"] = None

    with pytest.raises(ValueError, match="usage"):
        parse_response(ChatCompletion.model_validate(body))


def test_text_content_becomes_a_text_block_before_the_tool_calls() -> None:
    body = completion(
        finish_reason="tool_calls",
        content="Reading it now.",
        tool_calls=[tool_call("call_9", "read_file", '{"path": "x.py"}')],
    )

    response = parse_response(ChatCompletion.model_validate(body))

    assert response.content == [
        TextBlock(text="Reading it now."),
        ToolUseBlock(id="call_9", name="read_file", input={"path": "x.py"}),
    ]


def test_empty_text_content_adds_no_text_block() -> None:
    body = completion(content="")

    assert parse_response(ChatCompletion.model_validate(body)).content == []


def test_tool_arguments_that_are_not_a_json_object_are_an_error_naming_the_tool() -> None:
    body = completion(
        finish_reason="tool_calls",
        content=None,
        tool_calls=[tool_call("call_9", "read_file", '{"path": ')],
    )

    with pytest.raises(ValueError, match="read_file"):
        parse_response(ChatCompletion.model_validate(body))


def test_a_response_without_choices_is_an_error() -> None:
    body = fixture()
    body["choices"] = []

    with pytest.raises(ValueError, match="choices"):
        parse_response(ChatCompletion.model_validate(body))


# --- rate limits ----------------------------------------------------------------


async def test_a_429_that_outlives_the_retries_raises_rate_limited() -> None:
    endpoint = FakeEndpoint(too_many({"retry-after": "0"}), too_many({"retry-after": "0"}))

    with pytest.raises(RateLimitedError):
        await client(endpoint, max_retries=1).complete("s", [Message.user("go")], [])

    assert len(endpoint.requests) == 2


async def test_a_429_followed_by_success_returns_the_response() -> None:
    endpoint = FakeEndpoint(too_many({"retry-after": "0"}), ok(fixture()))

    response = await client(endpoint, max_retries=1).complete("s", [Message.user("go")], [])

    assert response.stop_reason == "tool_use"
    assert len(endpoint.requests) == 2


async def test_resume_after_comes_from_retry_after_in_seconds() -> None:
    endpoint = FakeEndpoint(too_many({"retry-after": "120"}))

    with pytest.raises(RateLimitedError) as info:
        await client(endpoint).complete("s", [Message.user("go")], [])

    assert info.value.resume_after == NOW + timedelta(seconds=120)
    assert "120" in str(info.value)


async def test_resume_after_comes_from_retry_after_as_an_http_date() -> None:
    endpoint = FakeEndpoint(too_many({"retry-after": "Tue, 17 Nov 2026 09:05:00 GMT"}))

    with pytest.raises(RateLimitedError) as info:
        await client(endpoint).complete("s", [Message.user("go")], [])

    assert info.value.resume_after == NOW + timedelta(minutes=5)


async def test_resume_after_defaults_when_the_header_is_missing_or_unreadable() -> None:
    for headers in ({}, {"retry-after": "soon"}):
        endpoint = FakeEndpoint(too_many(headers))

        with pytest.raises(RateLimitedError) as info:
            await client(endpoint).complete("s", [Message.user("go")], [])

        assert info.value.resume_after == NOW + DEFAULT_RATE_LIMIT_WAIT


async def test_other_api_errors_propagate_unchanged() -> None:
    endpoint = FakeEndpoint(httpx2.Response(500, json={"error": {"message": "boom"}}))

    with pytest.raises(InternalServerError):
        await client(endpoint).complete("s", [Message.user("go")], [])


# --- credentials ----------------------------------------------------------------


async def test_from_environment_reads_only_the_cplane_variables() -> None:
    endpoint = FakeEndpoint(ok(fixture()))
    environ = {
        "CPLANE_LLM_BASE_URL": BASE_URL,
        "CPLANE_LLM_AUTH_TOKEN": TOKEN,
        "OPENAI_API_KEY": "never-used",
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
    }

    llm = OpenAICompatibleClient.from_environment(
        environ, model=MODEL, http_client=endpoint.http_client()
    )
    await llm.complete("s", [Message.user("go")], [])

    assert endpoint.requests[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert str(endpoint.requests[0].url).startswith(BASE_URL)


@pytest.mark.parametrize("missing", ["CPLANE_LLM_BASE_URL", "CPLANE_LLM_AUTH_TOKEN"])
def test_from_environment_names_the_missing_variable(missing: str) -> None:
    environ = {"CPLANE_LLM_BASE_URL": BASE_URL, "CPLANE_LLM_AUTH_TOKEN": TOKEN}
    del environ[missing]

    with pytest.raises(CredentialsError, match=missing):
        OpenAICompatibleClient.from_environment(environ, model=MODEL)


def test_the_openai_variables_in_the_process_environment_are_not_a_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "never-used")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

    with pytest.raises(CredentialsError, match="CPLANE_LLM_AUTH_TOKEN"):
        OpenAICompatibleClient.from_environment({"CPLANE_LLM_BASE_URL": BASE_URL}, model=MODEL)


def test_cache_write_tokens_is_read_when_the_proxy_uses_that_spelling() -> None:
    body = completion(
        usage={
            "prompt_tokens": 40,
            "completion_tokens": 2,
            "total_tokens": 42,
            "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 15},
        }
    )

    usage = parse_response(ChatCompletion.model_validate(body)).usage

    assert usage.cache_creation_input_tokens == 15
    assert usage.input_tokens == 25


def test_a_tool_call_that_is_not_a_function_call_is_an_error_naming_it() -> None:
    body = completion(
        finish_reason="tool_calls",
        content=None,
        tool_calls=[{"id": "call_c", "type": "custom", "custom": {"name": "grep", "input": "x"}}],
    )

    with pytest.raises(ValueError, match="custom"):
        parse_response(ChatCompletion.model_validate(body))
