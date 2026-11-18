"""Task 7: which tools each role gets (docs/SPEC.md sections 8 and 10.4)."""

import pytest

from control_plane.policy import ROLE_TOOLS, ROLES, SUBMIT_TOOLS, TOOL_NAMES, is_exposed, tools_for
from control_plane.tools import ALL_TOOLS

READ_TOOLS = ("read_file", "list_dir", "search")
WRITE_TOOLS = ("write_file", "edit_file", "delete_file", "run_command")


def test_the_roles_are_planner_worker_and_reviewer() -> None:
    assert ROLES == ("planner", "worker", "reviewer")
    assert tuple(ROLE_TOOLS) == ROLES


def test_the_tool_names_match_the_tools_package() -> None:
    assert TOOL_NAMES == ALL_TOOLS


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("planner", (*READ_TOOLS, "submit_plan")),
        ("worker", (*READ_TOOLS, *WRITE_TOOLS, "submit_work_report")),
        ("reviewer", (*READ_TOOLS, "submit_review")),
    ],
)
def test_each_role_gets_the_tools_of_the_section_10_4_table(
    role: str, expected: tuple[str, ...]
) -> None:
    assert tools_for(role) == expected


def test_submit_tools_are_one_per_role() -> None:
    assert SUBMIT_TOOLS == {
        "planner": "submit_plan",
        "worker": "submit_work_report",
        "reviewer": "submit_review",
    }


@pytest.mark.parametrize("tool", WRITE_TOOLS)
@pytest.mark.parametrize("role", ["planner", "reviewer"])
def test_planner_and_reviewer_have_no_write_tools(role: str, tool: str) -> None:
    assert not is_exposed(role, tool)


def test_a_role_has_only_its_own_submit_tool() -> None:
    assert is_exposed("planner", "submit_plan")
    assert not is_exposed("planner", "submit_work_report")
    assert not is_exposed("worker", "submit_review")


def test_an_unknown_tool_is_not_exposed_to_anyone() -> None:
    assert all(not is_exposed(role, "teleport") for role in ROLES)


def test_an_unknown_role_is_an_error() -> None:
    with pytest.raises(ValueError, match="unknown role 'admin'"):
        tools_for("admin")
    with pytest.raises(ValueError, match="unknown role 'admin'"):
        is_exposed("admin", "read_file")
