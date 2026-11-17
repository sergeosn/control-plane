"""Task 6: the tool input models and the tool definitions sent to the model (SPEC.md 10.4)."""

import pytest
from pydantic import ValidationError

from control_plane.llm import ToolDefinition
from control_plane.tools import (
    ALL_TOOLS,
    TOOL_INPUTS,
    EditFileInput,
    ReadFileInput,
    RunCommandInput,
    SearchInput,
    tool_definitions,
)

SPEC_TOOLS = {
    "read_file",
    "list_dir",
    "search",
    "write_file",
    "edit_file",
    "delete_file",
    "run_command",
}


def test_every_tool_of_section_10_4_has_an_input_model() -> None:
    assert set(TOOL_INPUTS) == SPEC_TOOLS
    assert set(ALL_TOOLS) == SPEC_TOOLS


def test_read_file_accepts_an_optional_line_range() -> None:
    assert ReadFileInput(path="a.py").start_line is None
    ranged = ReadFileInput(path="a.py", start_line=10, end_line=20)
    assert (ranged.start_line, ranged.end_line) == (10, 20)


def test_read_file_rejects_a_range_that_ends_before_it_starts() -> None:
    with pytest.raises(ValidationError, match="end_line"):
        ReadFileInput(path="a.py", start_line=20, end_line=10)


def test_read_file_rejects_a_line_number_below_one() -> None:
    with pytest.raises(ValidationError, match="start_line"):
        ReadFileInput(path="a.py", start_line=0)


def test_edit_file_rejects_empty_old_text() -> None:
    with pytest.raises(ValidationError, match="old"):
        EditFileInput(path="a.py", old="", new="x")


def test_run_command_rejects_an_empty_argv_and_a_shell_string() -> None:
    with pytest.raises(ValidationError, match="argv"):
        RunCommandInput(argv=[])
    with pytest.raises(ValidationError, match="argv"):
        RunCommandInput.model_validate({"argv": "ls -la"})


def test_run_command_rejects_a_non_positive_timeout() -> None:
    with pytest.raises(ValidationError, match="timeout"):
        RunCommandInput(argv=["ls"], timeout=0)


def test_inputs_reject_unknown_keys() -> None:
    with pytest.raises(ValidationError, match="shell"):
        RunCommandInput.model_validate({"argv": ["ls"], "shell": True})


def test_search_glob_is_optional() -> None:
    assert SearchInput(pattern="def ").glob is None


def test_tool_definitions_are_built_from_the_input_models() -> None:
    definitions = tool_definitions(["read_file", "run_command"])

    assert [d.name for d in definitions] == ["read_file", "run_command"]
    assert all(isinstance(d, ToolDefinition) for d in definitions)
    read = definitions[0]
    assert read.description
    assert set(read.input_schema["properties"]) == {"path", "start_line", "end_line"}
    assert read.input_schema["additionalProperties"] is False
    assert read.strict is False


def test_tool_definitions_default_to_every_tool() -> None:
    assert [d.name for d in tool_definitions()] == list(ALL_TOOLS)


def test_tool_definitions_reject_an_unknown_tool() -> None:
    with pytest.raises(KeyError, match="submit_plan"):
        tool_definitions(["submit_plan"])
