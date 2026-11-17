"""`classify(role, call)` (SPEC.md section 10.3).

Evaluation order: hard checks first, then the first matching rule, then the default. The
hard checks here are the role's tool exposure (section 10.4) and the shape of `argv`. The
decision uses only the role, the tool name and the arguments: no text the model reads or
writes can change it. Every decision carries the rule id that produced it and is handed to
the log, so the audit trail of section 10.1 is complete.
"""

import shlex
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Self

from control_plane.domain.calls import Decision, ToolCall
from control_plane.llm.types import ToolUseBlock
from control_plane.policy.roles import SUBMIT_TOOLS, TOOL_NAMES, tools_for
from control_plane.policy.rules import CLASS_DECISIONS, COMMAND_TOOL, Rules, load_rules
from control_plane.store.home import Home

EVENT_KIND = "tool_decision"
RULES_FILE = "tools.yaml"


@dataclass(frozen=True)
class Classification:
    """The outcome of one policy decision.

    `tool_class` and `rule_id` are `None` when no rule was involved: a hard check decided,
    or the default applied. `reason` says what was decided and why, in words the model
    can act on when it is returned as an error.
    """

    tool: str
    arguments: dict[str, Any]
    decision: Decision
    tool_class: str | None
    rule_id: str | None
    reason: str

    def event_fields(self) -> dict[str, str]:
        """Keyword arguments for `RunFolder.log_event`: kind, outcome and message."""
        return {"kind": EVENT_KIND, "outcome": self.decision, "message": self.reason}

    def to_tool_call(self, *, ts: datetime, tool_use_id: str) -> ToolCall:
        """The `tool_calls.jsonl` record of this decision, before the call runs."""
        return ToolCall(
            ts=ts,
            tool_use_id=tool_use_id,
            tool=self.tool,
            arguments=self.arguments,
            tool_class=self.tool_class,
            decision=self.decision,
            rule_id=self.rule_id,
            error=None if self.decision == "allow" else self.reason,
        )


DecisionLog = Callable[[Classification], None]


class Policy:
    def __init__(self, rules: Rules, *, log: DecisionLog | None = None) -> None:
        self.rules = rules
        self._log = log

    @classmethod
    def from_home(cls, home: Home, *, log: DecisionLog | None = None) -> Self:
        return cls(load_rules(home.rules / RULES_FILE), log=log)

    def classify(self, role: str, call: ToolUseBlock) -> Classification:
        """Decide `call` for `role`. Raises `ValueError` for a role not in the table."""
        decision = self._decide(role, call.name, dict(call.input))
        if self._log is not None:
            self._log(decision)
        return decision

    def _decide(self, role: str, tool: str, arguments: dict[str, Any]) -> Classification:
        exposed = tools_for(role)
        shown = _show(tool, arguments)

        def hard(decision: Decision, reason: str) -> Classification:
            return Classification(tool, arguments, decision, None, None, reason)

        if tool not in exposed:
            return hard(
                "deny",
                f"{shown} denied: the {role} does not have the tool {tool!r}; "
                f"its tools are {', '.join(exposed)}",
            )
        if tool == SUBMIT_TOOLS[role]:
            return hard("allow", f"{tool} allowed: it is the {role}'s submit tool")
        assert tool in TOOL_NAMES
        argv: list[str] | None = None
        if tool == COMMAND_TOOL:
            argv = arguments.get("argv")
            if not _is_argv(argv):
                return hard(
                    "deny",
                    f"{tool} denied: argv must be a non-empty list of strings, got {argv!r}",
                )
        rule = self.rules.first_match(tool, argv)
        if rule is None:
            decision = self.rules.default_decision
            return hard(
                decision,
                f"{shown} {_verb(decision)}: no rule matches and the default is "
                f"{self.rules.default!r}",
            )
        decision = CLASS_DECISIONS[rule.tool_class]
        return Classification(
            tool,
            arguments,
            decision,
            rule.tool_class,
            rule.id,
            f"{shown} {_verb(decision)}: it is {rule.tool_class} by rule {rule.id!r}",
        )


def _is_argv(value: object) -> bool:
    return (
        isinstance(value, list) and len(value) > 0 and all(isinstance(item, str) for item in value)
    )


def _show(tool: str, arguments: dict[str, Any]) -> str:
    argv = arguments.get("argv")
    if tool == COMMAND_TOOL and _is_argv(argv):
        return f"{tool} {shlex.join(argv)}"
    return tool


def _verb(decision: Decision) -> str:
    return {"allow": "allowed", "deny": "denied", "park": "parked for approval"}[decision]
