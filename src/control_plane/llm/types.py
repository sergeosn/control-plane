"""Request and response types that mirror the parts of the Messages API the loop uses.

docs/SPEC.md section 8.2: text and `tool_use` blocks in a response, `tool_result` blocks in the
next user message, `stop_reason`, and the four usage fields. Nothing here depends on an SDK,
so the loop (section 8.2), the scripted client (section 16) and the real client (Task 10)
share one vocabulary.
"""

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field

StopReason = Literal[
    "end_turn",
    "tool_use",
    "max_tokens",
    "stop_sequence",
    "refusal",
    "pause_turn",
    "model_context_window_exceeded",
]

Role = Literal["user", "assistant"]


class LLMModel(BaseModel):
    """Base of every wire type: unknown keys are rejected, so a drifted shape is noticed."""

    model_config = ConfigDict(extra="forbid")


class TextBlock(LLMModel):
    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(LLMModel):
    """The model asks for a tool. `id` is what the matching `tool_result` must carry."""

    type: Literal["tool_use"] = "tool_use"
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    input: dict[str, Any]


class ToolResultBlock(LLMModel):
    """The engine answers one `tool_use`. Notices are appended to `content` (section 8.2)."""

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str = Field(min_length=1)
    content: str
    is_error: bool = False


ResponseBlock = Annotated[TextBlock | ToolUseBlock, Field(discriminator="type")]
MessageBlock = Annotated[TextBlock | ToolUseBlock | ToolResultBlock, Field(discriminator="type")]


class Usage(LLMModel):
    """Token counts as the API reports them. Cache counts default to zero for models
    that do not report them."""

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_input_tokens: int = Field(ge=0, default=0)
    cache_creation_input_tokens: int = Field(ge=0, default=0)

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_input_tokens
            + self.cache_creation_input_tokens
        )


class Response(LLMModel):
    """One model response: what the loop appends to the transcript and prices."""

    model: str = Field(min_length=1)
    content: list[ResponseBlock]
    stop_reason: StopReason
    usage: Usage

    @property
    def text(self) -> str:
        """The text blocks joined with newlines, empty when there are none."""
        return "\n".join(block.text for block in self.content if isinstance(block, TextBlock))

    @property
    def tool_uses(self) -> list[ToolUseBlock]:
        return [block for block in self.content if isinstance(block, ToolUseBlock)]


class Message(LLMModel):
    """One turn of the conversation sent to the model."""

    role: Role
    content: list[MessageBlock]

    @classmethod
    def user(cls, text: str) -> Self:
        return cls(role="user", content=[TextBlock(text=text)])

    @classmethod
    def from_response(cls, response: Response) -> Self:
        """The assistant turn that replays `response` in the next request."""
        return cls(role="assistant", content=list(response.content))


class ToolDefinition(LLMModel):
    """A tool as declared in a request. Submit tools are strict (section 8.3)."""

    name: str = Field(min_length=1)
    description: str
    input_schema: dict[str, Any]
    strict: bool = True
