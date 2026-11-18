"""The rules file `rules/tools.yaml` (docs/SPEC.md section 10.3).

The file is read with the YAML composer so that every value keeps its line and column.
Validation runs through the Pydantic models below, and each validation error is reported
at the position of the value it is about, so a human can fix the file without guessing.
Rules live in the home repository: changing one is a commit a human made.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from control_plane.domain.calls import Decision
from control_plane.policy.roles import TOOL_NAMES

ToolClass = Literal["read", "write-local", "write-external", "destructive", "blocked"]
Default = Literal["ask", "deny"]

CLASS_DECISIONS: dict[str, Decision] = {
    "read": "allow",
    "write-local": "allow",
    "write-external": "park",
    "destructive": "park",
    "blocked": "deny",
}
"""What each class means for a call (the table of section 10.3)."""

DEFAULT_DECISIONS: dict[str, Decision] = {"ask": "park", "deny": "deny"}
"""What `default` means for a call no rule matches."""

COMMAND_TOOL = "run_command"
RULE_ID = r"^[a-z0-9][a-z0-9-]*$"

ArgvPrefix = Annotated[list[Annotated[str, Field(min_length=1)]], Field(min_length=1)]
"""The leading arguments of a command: at least one, none empty."""

Position = tuple[int, int]
"""A 1-based (line, column)."""
Loc = tuple[str | int, ...]


class RulesError(Exception):
    """A rules file could not be loaded. The message starts with `source:line:column:`."""

    def __init__(self, source: str, position: Position, message: str) -> None:
        self.source = source
        self.line, self.column = position
        super().__init__(f"{source}:{self.line}:{self.column}: {message}")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Rule(_Model):
    id: str = Field(pattern=RULE_ID)
    tool: list[str] = Field(min_length=1)
    argv_prefix: list[ArgvPrefix] | None = None
    tool_class: ToolClass = Field(alias="class")

    @field_validator("tool", mode="before")
    @classmethod
    def _one_tool_is_a_list(cls, value: object) -> object:
        return [value] if isinstance(value, str) else value

    @field_validator("tool")
    @classmethod
    def _tools_are_known(cls, tools: list[str]) -> list[str]:
        unknown = [name for name in tools if name not in TOOL_NAMES]
        if unknown:
            raise ValueError(
                f"unknown tool {', '.join(repr(n) for n in unknown)}; "
                f"tools: {', '.join(TOOL_NAMES)}"
            )
        return tools

    @field_validator("argv_prefix")
    @classmethod
    def _prefixes_are_for_commands(
        cls, prefixes: list[list[str]] | None, info: ValidationInfo
    ) -> list[list[str]] | None:
        if prefixes is None:
            return None
        tools = info.data.get("tool", [])
        if any(tool != COMMAND_TOOL for tool in tools):
            raise ValueError(f"argv_prefix applies to {COMMAND_TOOL} only")
        return prefixes

    def matches(self, tool: str, argv: list[str] | None) -> bool:
        """Whether this rule applies to a call of `tool` with `argv` (commands only)."""
        if tool not in self.tool:
            return False
        if self.argv_prefix is None:
            return True
        if argv is None:
            return False
        return any(argv[: len(prefix)] == prefix for prefix in self.argv_prefix)


class Rules(_Model):
    version: Literal[1]
    default: Default
    rules: list[Rule] = Field(min_length=1)

    @model_validator(mode="after")
    def _ids_are_unique(self) -> Self:
        seen: set[str] = set()
        for rule in self.rules:
            if rule.id in seen:
                raise ValueError(f"duplicate rule id {rule.id!r}")
            seen.add(rule.id)
        return self

    @property
    def default_decision(self) -> Decision:
        return DEFAULT_DECISIONS[self.default]

    def first_match(self, tool: str, argv: list[str] | None) -> Rule | None:
        return next((rule for rule in self.rules if rule.matches(tool, argv)), None)


def load_rules(path: Path) -> Rules:
    """Load and validate a rules file. Raises `RulesError` with the position of the error."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RulesError(str(path), (1, 1), f"cannot read the rules file: {exc}") from exc
    return parse_rules(text, source=str(path))


def parse_rules(text: str, source: str = "<rules>") -> Rules:
    """Parse and validate the text of a rules file. Raises `RulesError` with the position."""
    node = _compose(text, source)
    if node is None:
        raise RulesError(source, (1, 1), "the rules file is empty; expected a mapping")
    if not isinstance(node, yaml.MappingNode):
        raise RulesError(source, _position(node), "the rules file must be a mapping")
    data = yaml.SafeLoader("").construct_document(node)
    marks = _Marks(node)
    _reject_duplicate_ids(data, marks, source)
    try:
        return Rules.model_validate(data)
    except ValidationError as exc:
        raise RulesError(source, *_describe(exc, marks)) from exc


def _compose(text: str, source: str) -> yaml.Node | None:
    try:
        return yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.MarkedYAMLError as exc:
        # The context mark is where the unfinished construct started, which is where the
        # fix goes; the problem mark is where the parser gave up.
        mark = exc.context_mark or exc.problem_mark
        position = (mark.line + 1, mark.column + 1) if mark else (1, 1)
        detail = exc.problem or str(exc)
        if exc.context_mark and exc.problem_mark:
            detail = (
                f"{exc.context}: {exc.problem} at line {exc.problem_mark.line + 1}, "
                f"column {exc.problem_mark.column + 1}"
            )
        raise RulesError(source, position, f"not valid YAML: {detail}") from exc
    except yaml.YAMLError as exc:
        raise RulesError(source, (1, 1), f"not valid YAML: {exc}") from exc


def _position(node: yaml.Node) -> Position:
    return (node.start_mark.line + 1, node.start_mark.column + 1)


class _Marks:
    """The position of every value and every key in the node tree, by Pydantic `loc`."""

    def __init__(self, root: yaml.Node) -> None:
        self.values: dict[Loc, Position] = {}
        self.keys: dict[Loc, Position] = {}
        self._walk(root, ())

    def _walk(self, node: yaml.Node, loc: Loc) -> None:
        self.values[loc] = _position(node)
        if isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                if isinstance(key, yaml.ScalarNode):
                    child = (*loc, str(key.value))
                    self.keys[child] = _position(key)
                    self._walk(value, child)
        elif isinstance(node, yaml.SequenceNode):
            for index, item in enumerate(node.value):
                self._walk(item, (*loc, index))

    def of(self, loc: Loc, *, key: bool = False) -> Position:
        """The position of `loc`, or of its nearest present ancestor."""
        if key and loc in self.keys:
            return self.keys[loc]
        while loc not in self.values:
            loc = loc[:-1]
        return self.values[loc]


def _reject_duplicate_ids(data: Any, marks: _Marks, source: str) -> None:
    rules = data.get("rules") if isinstance(data, Mapping) else None
    if not isinstance(rules, list):
        return
    seen: set[str] = set()
    for index, rule in enumerate(rules):
        rule_id = rule.get("id") if isinstance(rule, Mapping) else None
        if not isinstance(rule_id, str):
            continue
        if rule_id in seen:
            raise RulesError(
                source,
                marks.of(("rules", index, "id")),
                f"rules.{index}.id: duplicate rule id {rule_id!r}",
            )
        seen.add(rule_id)


def _describe(exc: ValidationError, marks: _Marks) -> tuple[Position, str]:
    """The position and message of the first validation error."""
    error = exc.errors()[0]
    loc: Loc = tuple(error["loc"])
    field = ".".join(str(part) for part in loc) or "rules file"
    position = marks.of(loc, key=error["type"] == "extra_forbidden")
    return position, f"{field}: {error['msg']}"
