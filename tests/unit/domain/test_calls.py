"""Task 4: the per-step audit records the index reads: one model call, one tool call."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from control_plane.domain import LLMCall, ToolCall

TS = datetime(2026, 11, 17, 9, 0, 0, tzinfo=UTC)


def make_llm_call(**overrides: object) -> LLMCall:
    fields: dict[str, object] = {
        "ts": TS,
        "model": "claude-sonnet-5-5",
        "input_tokens": 1200,
        "output_tokens": 300,
        "cache_read_input_tokens": 1000,
        "cache_creation_input_tokens": 0,
        "cost_usd": 0.0054,
        "stop_reason": "tool_use",
    }
    fields.update(overrides)
    return LLMCall(**fields)


def make_tool_call(**overrides: object) -> ToolCall:
    fields: dict[str, object] = {
        "ts": TS,
        "tool_use_id": "toolu_01",
        "tool": "run_command",
        "arguments": {"argv": ["pytest", "-q"]},
        "tool_class": "write-local",
        "decision": "allow",
        "rule_id": "local-commands",
    }
    fields.update(overrides)
    return ToolCall(**fields)


def test_llm_call_round_trips_through_jsonl() -> None:
    call = make_llm_call()

    line = call.to_jsonl_line()

    assert "\n" not in line[:-1]
    assert LLMCall.from_jsonl_line(line) == call


def test_llm_call_cache_counts_default_to_zero() -> None:
    call = LLMCall(
        ts=TS,
        model="m",
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.0,
        stop_reason="end_turn",
    )

    assert call.cache_read_input_tokens == 0
    assert call.cache_creation_input_tokens == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [("input_tokens", -1), ("cost_usd", -0.01), ("model", ""), ("stop_reason", "")],
)
def test_llm_call_rejects_negative_counts_and_empty_names(field: str, value: object) -> None:
    with pytest.raises(ValidationError, match=field):
        make_llm_call(**{field: value})


def test_tool_call_round_trips_through_jsonl() -> None:
    call = make_tool_call(error="denied: git push is write-external")

    line = call.to_jsonl_line()

    assert "\n" not in line[:-1]
    assert ToolCall.from_jsonl_line(line) == call


def test_tool_call_without_a_matching_rule_has_no_class_or_rule() -> None:
    call = make_tool_call(tool_class=None, rule_id=None, decision="park")

    assert call.tool_class is None
    assert call.rule_id is None
    assert call.error is None


def test_tool_call_decision_is_allow_deny_or_park() -> None:
    with pytest.raises(ValidationError, match="decision"):
        make_tool_call(decision="maybe")


def test_naive_timestamps_are_rejected() -> None:
    naive = datetime(2026, 11, 17, 9, 0)  # noqa: DTZ001
    with pytest.raises(ValidationError, match="ts"):
        make_llm_call(ts=naive)
    with pytest.raises(ValidationError, match="ts"):
        make_tool_call(ts=naive)


def test_extra_keys_are_rejected() -> None:
    with pytest.raises(ValidationError, match="extra"):
        ToolCall.from_jsonl_line(make_tool_call().to_jsonl_line()[:-2] + ', "extra": 1}\n')
