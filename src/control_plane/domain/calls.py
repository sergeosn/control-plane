"""The audit records of one step: every model call and every tool call (SPEC.md section 10.1).

The loop appends them to `llm_calls.jsonl` and `tool_calls.jsonl` inside the step folder
(section 5). The index reads them (section 14), and the metrics of section 16 are computed
from them. Which run, step and attempt a record belongs to is given by where the file lives,
so the records do not repeat it.
"""

from typing import Any, Literal

from pydantic import AwareDatetime, Field

from control_plane.domain.base import DomainModel

Decision = Literal["allow", "deny", "park"]


class LLMCall(DomainModel):
    """One model response, with the usage the API reported and the cost it was priced at."""

    ts: AwareDatetime
    model: str = Field(min_length=1)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_input_tokens: int = Field(ge=0, default=0)
    cache_creation_input_tokens: int = Field(ge=0, default=0)
    cost_usd: float = Field(ge=0)
    stop_reason: str = Field(min_length=1)


class ToolCall(DomainModel):
    """One tool call and the policy decision it got (section 10.3).

    `tool_class` and `rule_id` are empty when no rule matched and the default applied.
    `error` holds the message of a failed or denied call. A parked call is logged twice:
    once with the `park` decision, and again on resume with the human's decision and the
    id of the approval entry that carries it (section 12).
    """

    ts: AwareDatetime
    tool_use_id: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    arguments: dict[str, Any]
    tool_class: str | None
    decision: Decision
    rule_id: str | None
    error: str | None = None
    approval_id: str | None = None
