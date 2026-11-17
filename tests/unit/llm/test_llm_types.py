"""Task 5: request and response types that mirror the Messages API (SPEC.md section 8.2)."""

import pytest
from pydantic import ValidationError

from control_plane.llm import (
    Message,
    Response,
    TextBlock,
    ToolDefinition,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)


def make_response(**overrides: object) -> Response:
    fields: dict[str, object] = {
        "model": "claude-sonnet-5-5",
        "content": [
            TextBlock(text="Let me look."),
            ToolUseBlock(id="toolu_01", name="read_file", input={"path": "a.py"}),
            TextBlock(text="And this one."),
            ToolUseBlock(id="toolu_02", name="read_file", input={"path": "b.py"}),
        ],
        "stop_reason": "tool_use",
        "usage": Usage(input_tokens=10, output_tokens=5),
    }
    fields.update(overrides)
    return Response(**fields)


def test_response_text_joins_the_text_blocks_in_order() -> None:
    assert make_response().text == "Let me look.\nAnd this one."


def test_response_tool_uses_lists_the_tool_use_blocks_in_order() -> None:
    uses = make_response().tool_uses

    assert [use.id for use in uses] == ["toolu_01", "toolu_02"]
    assert all(isinstance(use, ToolUseBlock) for use in uses)


def test_response_without_tool_use_has_no_tool_uses_and_empty_text_when_no_text() -> None:
    response = make_response(content=[], stop_reason="end_turn")

    assert response.tool_uses == []
    assert response.text == ""


def test_usage_total_counts_all_four_fields() -> None:
    usage = Usage(
        input_tokens=100,
        output_tokens=20,
        cache_read_input_tokens=1000,
        cache_creation_input_tokens=300,
    )

    assert usage.total_tokens == 1420


def test_usage_cache_counts_default_to_zero_and_reject_negatives() -> None:
    assert Usage(input_tokens=1, output_tokens=1).cache_read_input_tokens == 0
    assert Usage(input_tokens=1, output_tokens=1).cache_creation_input_tokens == 0
    with pytest.raises(ValidationError, match="output_tokens"):
        Usage(input_tokens=1, output_tokens=-1)


def test_response_round_trips_through_json_with_block_types_preserved() -> None:
    response = make_response()

    restored = Response.model_validate_json(response.model_dump_json())

    assert restored == response
    assert restored.content[1].type == "tool_use"


def test_response_rejects_an_unknown_block_type() -> None:
    with pytest.raises(ValidationError, match="type"):
        Response.model_validate(
            {
                "model": "m",
                "content": [{"type": "image", "data": "..."}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        )


def test_response_rejects_an_unknown_stop_reason() -> None:
    with pytest.raises(ValidationError, match="stop_reason"):
        make_response(stop_reason="because")


@pytest.mark.parametrize(
    "stop_reason",
    ["end_turn", "tool_use", "max_tokens", "stop_sequence", "refusal", "pause_turn"],
)
def test_every_stop_reason_the_loop_handles_is_accepted(stop_reason: str) -> None:
    assert make_response(stop_reason=stop_reason).stop_reason == stop_reason


def test_user_message_carries_tool_results_with_matching_ids() -> None:
    message = Message(
        role="user",
        content=[
            ToolResultBlock(tool_use_id="toolu_01", content="line 1\n"),
            ToolResultBlock(tool_use_id="toolu_02", content="no such file", is_error=True),
        ],
    )

    restored = Message.model_validate_json(message.model_dump_json())

    assert restored == message
    assert restored.content[0].is_error is False
    assert restored.content[1].is_error is True


def test_message_from_plain_text_wraps_it_in_a_text_block() -> None:
    message = Message.user("BRIEF")

    assert message.role == "user"
    assert message.content == [TextBlock(text="BRIEF")]


def test_assistant_message_from_a_response_keeps_its_content() -> None:
    response = make_response()

    message = Message.from_response(response)

    assert message.role == "assistant"
    assert message.content == response.content


def test_message_rejects_a_role_other_than_user_or_assistant() -> None:
    with pytest.raises(ValidationError, match="role"):
        Message(role="system", content=[TextBlock(text="x")])


def test_tool_definition_is_strict_by_default() -> None:
    tool = ToolDefinition(
        name="submit_plan",
        description="Finish by submitting the plan.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )

    assert tool.strict is True


def test_tool_definition_rejects_an_empty_name() -> None:
    with pytest.raises(ValidationError, match="name"):
        ToolDefinition(name="", description="d", input_schema={"type": "object"})


def test_blocks_reject_extra_keys() -> None:
    with pytest.raises(ValidationError, match="extra"):
        TextBlock.model_validate({"type": "text", "text": "hi", "extra": 1})
