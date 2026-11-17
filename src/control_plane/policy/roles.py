"""Which tools each role gets (SPEC.md sections 8 and 10.4).

The table is the first exposure check: a role calling a tool it does not have is denied
before any rule is consulted. The executor repeats the check (section 10.1).

The tool names are spelled out here rather than imported from `control_plane.tools`, so the
policy stays the lower layer; a test asserts that the two lists agree.
"""

READ_TOOLS: tuple[str, ...] = ("read_file", "list_dir", "search")
WRITE_TOOLS: tuple[str, ...] = ("write_file", "edit_file", "delete_file", "run_command")
TOOL_NAMES: tuple[str, ...] = (*READ_TOOLS, *WRITE_TOOLS)
"""The seven tools of section 10.4, the ones the rules classify."""

SUBMIT_TOOLS: dict[str, str] = {
    "planner": "submit_plan",
    "worker": "submit_work_report",
    "reviewer": "submit_review",
}
"""The tool each role ends by calling (section 8.3)."""

ROLE_TOOLS: dict[str, tuple[str, ...]] = {
    "planner": (*READ_TOOLS, SUBMIT_TOOLS["planner"]),
    "worker": (*READ_TOOLS, *WRITE_TOOLS, SUBMIT_TOOLS["worker"]),
    "reviewer": (*READ_TOOLS, SUBMIT_TOOLS["reviewer"]),
}

ROLES: tuple[str, ...] = tuple(ROLE_TOOLS)


def tools_for(role: str) -> tuple[str, ...]:
    """Every tool `role` may call, its submit tool last. Raises `ValueError` for a role
    that is not in the table."""
    try:
        return ROLE_TOOLS[role]
    except KeyError:
        raise ValueError(f"unknown role {role!r}; roles: {', '.join(ROLES)}") from None


def is_exposed(role: str, tool: str) -> bool:
    return tool in tools_for(role)
