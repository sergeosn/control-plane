"""The lane files: procedure, rubric with row ids, role prompts, flow (docs/SPEC.md 5, 7, 8)."""

import shutil
from pathlib import Path

import pytest

from control_plane.flow import FlowValidationError, LaneError, load_lane, rubric_row_ids
from control_plane.policy.roles import SUBMIT_TOOLS
from control_plane.store.home import TEMPLATE_DIR

LANE = TEMPLATE_DIR / "lanes" / "development"
PROMPT_LINE_LIMIT = 30
"""Role prompts stay short: they are the stable system prompt of every step (section 9)."""


def copy_lane(tmp_path: Path) -> Path:
    target = tmp_path / "development"
    shutil.copytree(LANE, target)
    return target


def test_the_shipped_lane_loads() -> None:
    lane = load_lane(LANE)
    assert lane.name == "development"
    assert lane.roles == frozenset({"planner", "worker", "reviewer"})
    assert lane.flow.name == "development-default"
    assert lane.rubric_rows[0] == "R1"


def test_rubric_rows_carry_unique_ids() -> None:
    rows = rubric_row_ids((LANE / "rubric.md").read_text())
    assert len(rows) >= 5
    assert len(rows) == len(set(rows))


@pytest.mark.parametrize("role", ["planner", "worker", "reviewer"])
def test_each_role_prompt_names_its_submit_tool_and_stays_short(role: str) -> None:
    prompt = (LANE / "roles" / f"{role}.md").read_text()
    assert f"`{SUBMIT_TOOLS[role]}`" in prompt
    for other, tool in SUBMIT_TOOLS.items():
        if other != role:
            assert tool not in prompt
    assert len(prompt.splitlines()) <= PROMPT_LINE_LIMIT
    assert "data, not instructions" in prompt


def test_the_procedure_covers_each_role() -> None:
    procedure = (LANE / "procedure.md").read_text()
    for heading in ("## Planning", "## Implementing", "## Reviewing", "## Iterating"):
        assert heading in procedure


def test_a_rubric_without_rows_is_rejected(tmp_path: Path) -> None:
    lane = copy_lane(tmp_path)
    (lane / "rubric.md").write_text("# Rubric\n\nBe good.\n")
    with pytest.raises(LaneError, match="rubric.md has no rows"):
        load_lane(lane)


def test_a_duplicate_rubric_id_is_rejected(tmp_path: Path) -> None:
    lane = copy_lane(tmp_path)
    (lane / "rubric.md").write_text("- [R1] One.\n- [R1] Again.\n")
    with pytest.raises(LaneError, match="duplicate rubric row id 'R1'"):
        load_lane(lane)


def test_a_missing_lane_file_is_rejected(tmp_path: Path) -> None:
    lane = copy_lane(tmp_path)
    (lane / "procedure.md").unlink()
    with pytest.raises(LaneError, match="procedure.md"):
        load_lane(lane)


def test_a_flow_naming_a_role_without_a_prompt_is_rejected(tmp_path: Path) -> None:
    lane = copy_lane(tmp_path)
    (lane / "roles" / "reviewer.md").unlink()
    with pytest.raises(FlowValidationError, match="role 'reviewer' is not defined by the lane"):
        load_lane(lane)
