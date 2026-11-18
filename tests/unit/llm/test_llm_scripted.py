"""Task 5: `ScriptedClient` replays prepared responses in order (docs/SPEC.md section 16)."""

import pytest

from control_plane.llm import (
    LLMClient,
    Message,
    Response,
    ScriptedClient,
    ScriptExhaustedError,
    TextBlock,
    ToolDefinition,
    ToolUseBlock,
    Usage,
)

TOOL = ToolDefinition(
    name="read_file",
    description="Read a file.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
)


def text_response(text: str) -> Response:
    return Response(
        model="scripted",
        content=[TextBlock(text=text)],
        stop_reason="end_turn",
        usage=Usage(input_tokens=1, output_tokens=1),
    )


def tool_response() -> Response:
    return Response(
        model="scripted",
        content=[ToolUseBlock(id="toolu_01", name="read_file", input={"path": "a.py"})],
        stop_reason="tool_use",
        usage=Usage(input_tokens=1, output_tokens=1),
    )


def test_scripted_client_satisfies_the_protocol() -> None:
    assert isinstance(ScriptedClient([]), LLMClient)


async def test_scripted_client_replays_its_script_in_order() -> None:
    client = ScriptedClient([tool_response(), text_response("done")])

    first = await client.complete("sys", [Message.user("go")], [TOOL])
    second = await client.complete("sys", [Message.user("go")], [TOOL])

    assert first == tool_response()
    assert second == text_response("done")


async def test_scripted_client_raises_a_clear_error_when_the_script_runs_out() -> None:
    client = ScriptedClient([text_response("only one")])
    await client.complete("sys", [Message.user("go")], [])

    with pytest.raises(ScriptExhaustedError, match=r"1 response.*call 2"):
        await client.complete("sys", [Message.user("again")], [])


async def test_scripted_client_with_an_empty_script_fails_on_the_first_call() -> None:
    with pytest.raises(ScriptExhaustedError, match="0 responses"):
        await ScriptedClient([]).complete("sys", [Message.user("go")], [])


async def test_scripted_client_records_what_each_call_was_given() -> None:
    client = ScriptedClient([text_response("a"), text_response("b")])
    messages = [Message.user("brief")]

    await client.complete("system prompt", messages, [TOOL])
    await client.complete("system prompt", messages + [Message.user("more")], [])

    assert len(client.calls) == 2
    assert client.calls[0].system == "system prompt"
    assert client.calls[0].messages == messages
    assert client.calls[0].tools == [TOOL]
    assert client.calls[1].messages[-1] == Message.user("more")
    assert client.calls[1].tools == []


async def test_scripted_client_copies_the_messages_it_records() -> None:
    client = ScriptedClient([text_response("a")])
    messages = [Message.user("brief")]

    await client.complete("sys", messages, [])
    messages.append(Message.user("appended later"))

    assert len(client.calls[0].messages) == 1


async def test_scripted_client_records_a_max_tokens_override() -> None:
    client = ScriptedClient([text_response("a"), text_response("b")])

    await client.complete("sys", [Message.user("go")], [])
    await client.complete("sys", [Message.user("go")], [], max_tokens=32768)

    assert client.calls[0].max_tokens is None
    assert client.calls[1].max_tokens == 32768


async def test_scripted_client_reports_how_many_responses_remain() -> None:
    client = ScriptedClient([text_response("a"), text_response("b")])

    assert client.remaining == 2
    await client.complete("sys", [Message.user("go")], [])
    assert client.remaining == 1
