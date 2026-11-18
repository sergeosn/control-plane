"""Task 10: a live smoke test against the real endpoint (docs/SPEC.md section 14).

Skipped unless `CPLANE_LIVE=1`. It is the one test allowed to open a socket: the
`no_network` fixture in `tests/conftest.py` stands down for a test marked `live` when
`CPLANE_LIVE=1`. Credentials and the model come from `CPLANE_LLM_BASE_URL`,
`CPLANE_LLM_AUTH_TOKEN` and `CPLANE_LIVE_MODEL`; never from `OPENAI_*` or `ANTHROPIC_*`.
"""

import os

import pytest

from control_plane.llm import Message, ToolDefinition, ToolResultBlock
from control_plane.llm.openai_compat import OpenAICompatibleClient

pytestmark = pytest.mark.live

READ_FILE = ToolDefinition(
    name="read_file",
    description="Read a file from the repository and return its text.",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Path relative to the root."}},
        "required": ["path"],
        "additionalProperties": False,
    },
    strict=True,
)


@pytest.fixture
def live_client() -> OpenAICompatibleClient:
    if os.environ.get("CPLANE_LIVE") != "1":
        pytest.skip("live tests run only with CPLANE_LIVE=1")
    model = os.environ.get("CPLANE_LIVE_MODEL")
    if not model:
        pytest.skip("CPLANE_LIVE_MODEL names the model for the live test")
    return OpenAICompatibleClient.from_environment(os.environ, model=model, max_tokens=512)


async def test_a_two_turn_tool_call_completes(live_client: OpenAICompatibleClient) -> None:
    system = (
        "You are a careful assistant. To answer, read README.md with the read_file tool, "
        "then reply with one short sentence that quotes its first line."
    )
    messages = [Message.user("What is the first line of README.md?")]

    first = await live_client.complete(system, messages, [READ_FILE])

    assert first.stop_reason == "tool_use"
    [call] = first.tool_uses
    assert call.name == "read_file"
    assert call.input["path"].endswith("README.md")
    assert first.usage.input_tokens > 0
    assert first.usage.output_tokens > 0

    messages.append(Message.from_response(first))
    messages.append(
        Message(
            role="user",
            content=[
                ToolResultBlock(
                    tool_use_id=call.id, content="# Control Plane\n\nTakes a task to a branch.\n"
                )
            ],
        )
    )

    second = await live_client.complete(system, messages, [READ_FILE])

    assert second.stop_reason == "end_turn"
    assert "Control Plane" in second.text
