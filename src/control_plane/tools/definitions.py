"""The tools as declared to the model (SPEC.md section 10.4).

Which tools a role gets is decided by the policy (Task 7); this module only knows how to
describe each tool. Ordinary tools are not strict: strict mode is reserved for the submit
tools (section 8.3), and the optional arguments here would otherwise have to be sent as
`null` on every call.
"""

from collections.abc import Iterable

from control_plane.llm.types import ToolDefinition
from control_plane.tools.inputs import (
    DeleteFileInput,
    EditFileInput,
    ListDirInput,
    ReadFileInput,
    RunCommandInput,
    SearchInput,
    ToolInput,
    WriteFileInput,
)

TOOL_INPUTS: dict[str, type[ToolInput]] = {
    "read_file": ReadFileInput,
    "list_dir": ListDirInput,
    "search": SearchInput,
    "write_file": WriteFileInput,
    "edit_file": EditFileInput,
    "delete_file": DeleteFileInput,
    "run_command": RunCommandInput,
}

ALL_TOOLS: tuple[str, ...] = tuple(TOOL_INPUTS)

TOOL_DESCRIPTIONS: dict[str, str] = {
    "read_file": (
        "Read a text file as numbered lines. Returns at most 400 lines per call; pass "
        "start_line and end_line (1-based, inclusive) to read a range. A truncated result "
        "says how many lines were not shown. Paths are relative to the worktree."
    ),
    "list_dir": (
        "List the entries of a directory, sorted, with a trailing '/' on directories. "
        "Paths are relative to the worktree."
    ),
    "search": (
        "Search every readable file for a regular expression. Returns 'path:line:text' "
        "matches, capped; a truncated result says how many matches were dropped. The "
        "optional glob (for example '*.py' or 'src/*') limits which files are searched."
    ),
    "write_file": (
        "Create or overwrite a file inside the worktree with the given content, creating "
        "parent directories as needed."
    ),
    "edit_file": (
        "Replace one occurrence of `old` with `new` in a file inside the worktree. The old "
        "text must match exactly once, whitespace included; include enough surrounding "
        "lines to make it unique."
    ),
    "delete_file": "Delete one file inside the worktree. Directories cannot be deleted.",
    "run_command": (
        "Run a command given as an argv list (no shell) in the worktree. Returns the exit "
        "code, stdout and stderr; long output keeps its head and tail and says how much was "
        "dropped. The optional timeout is in seconds; a command that exceeds it is killed."
    ),
}


def tool_definitions(names: Iterable[str] = ALL_TOOLS) -> list[ToolDefinition]:
    """The `ToolDefinition` of each named tool, in the given order.

    Raises `KeyError` for a name that is not one of the seven tools.
    """
    definitions = []
    for name in names:
        if name not in TOOL_INPUTS:
            raise KeyError(f"unknown tool {name!r}; known tools: {', '.join(ALL_TOOLS)}")
        definitions.append(
            ToolDefinition(
                name=name,
                description=TOOL_DESCRIPTIONS[name],
                input_schema=TOOL_INPUTS[name].model_json_schema(),
                strict=False,
            )
        )
    return definitions
