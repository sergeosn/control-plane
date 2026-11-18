"""The flow schema and its validation at load (docs/SPEC.md section 7)."""

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from control_plane.flow import (
    Flow,
    FlowValidationError,
    load_flow,
    outcomes_of,
    validate_flow,
)
from control_plane.store.home import TEMPLATE_DIR

FLOW_FILE = TEMPLATE_DIR / "lanes" / "development" / "flow.yaml"
LANE_ROLES = frozenset({"planner", "worker", "reviewer"})


def shipped() -> dict[str, Any]:
    return yaml.safe_load(FLOW_FILE.read_text())


def problems_of(data: dict[str, Any], roles: frozenset[str] = LANE_ROLES) -> list[str]:
    with pytest.raises(FlowValidationError) as caught:
        validate_flow(Flow.model_validate(data), roles)
    return caught.value.problems


def test_the_shipped_flow_is_valid() -> None:
    flow = load_flow(FLOW_FILE, LANE_ROLES)
    assert flow.start == "plan"
    assert list(flow.steps) == [
        "plan",
        "question",
        "plan_gate",
        "implement",
        "checks",
        "review",
        "merge_gate",
        "merge",
    ]
    assert flow.steps["plan_gate"].when == "task.plan_approval"
    assert flow.limits.max_iterations == 3


def test_a_missing_target_is_rejected() -> None:
    data = shipped()
    data["steps"]["checks"]["next"]["fail"] = "fix_it"
    assert problems_of(data) == ["step 'checks': outcome 'fail' goes to unknown step 'fix_it'"]


def test_an_unreachable_step_is_rejected() -> None:
    data = shipped()
    data["steps"]["orphan"] = {"kind": "gate", "gates": ["lint"], "next": {"pass": "review"}}
    data["steps"]["orphan"]["next"]["fail"] = "implement"
    assert problems_of(data) == ["step 'orphan' is not reachable from start 'plan'"]


def test_an_unreachable_done_is_rejected() -> None:
    data = shipped()
    data["steps"]["merge"]["next"] = {"ok": "failed", "error": "failed"}
    assert problems_of(data) == ["'done' is not reachable from start 'plan'"]


def test_an_unknown_role_is_rejected() -> None:
    data = shipped()
    data["steps"]["review"]["role"] = "auditor"
    problems = problems_of(data)
    assert problems == ["step 'review': role 'auditor' is not defined by the lane"]


def test_an_uncovered_outcome_is_rejected() -> None:
    data = shipped()
    del data["steps"]["plan"]["next"]["blocked"]
    problems = problems_of(data)
    assert "step 'plan': outcome 'blocked' has no transition" in problems
    # Without the transition the question step is no longer reachable either.
    assert "step 'question' is not reachable from start 'plan'" in problems


def test_an_outcome_the_step_cannot_emit_is_rejected() -> None:
    data = shipped()
    data["steps"]["implement"]["next"]["blocked"] = "plan"
    assert problems_of(data) == ["step 'implement': outcome 'blocked' is not one it can emit"]


def test_an_unknown_start_is_rejected() -> None:
    data = shipped()
    data["start"] = "begin"
    assert "start 'begin' is not a step" in problems_of(data)


def test_a_role_must_produce_its_own_artifact() -> None:
    data = shipped()
    data["steps"]["plan"]["produces"] = "review"
    data["steps"]["plan"]["next"] = {"pass": "plan_gate", "fail": "question"}
    assert problems_of(data) == ["step 'plan': role 'planner' produces plan, not review"]


def test_a_condition_must_name_a_boolean_task_field() -> None:
    data = shipped()
    data["steps"]["plan_gate"]["when"] = "task.priority"
    assert problems_of(data) == [
        "step 'plan_gate': when 'task.priority' is not a boolean task field;"
        " fields: task.plan_approval"
    ]


def test_an_unknown_gate_is_rejected() -> None:
    data = shipped()
    data["steps"]["checks"]["gates"].append("vibes")
    problems = problems_of(data)
    assert len(problems) == 1
    assert problems[0].startswith("step 'checks': unknown gate 'vibes'")


def test_a_step_may_not_be_named_after_a_terminal() -> None:
    data = shipped()
    data["steps"]["done"] = copy.deepcopy(data["steps"]["merge"])
    assert "step 'done': 'done' and 'failed' are terminal and cannot be steps" in problems_of(data)


def test_every_problem_is_reported_at_once() -> None:
    data = shipped()
    data["steps"]["review"]["role"] = "auditor"
    data["steps"]["checks"]["next"]["fail"] = "fix_it"
    with pytest.raises(FlowValidationError) as caught:
        validate_flow(Flow.model_validate(data), LANE_ROLES)
    assert len(caught.value.problems) == 2
    assert "auditor" in str(caught.value)
    assert "fix_it" in str(caught.value)


@pytest.mark.parametrize(
    ("step", "expected"),
    [
        ("plan", ("ok", "blocked")),
        ("question", ("answered",)),
        ("plan_gate", ("approve", "deny")),
        ("implement", ("ok",)),
        ("checks", ("pass", "fail")),
        ("review", ("pass", "fail")),
        ("merge_gate", ("approve", "deny")),
        ("merge", ("ok", "error")),
    ],
)
def test_outcomes_follow_the_step_kind(step: str, expected: tuple[str, ...]) -> None:
    flow = load_flow(FLOW_FILE, LANE_ROLES)
    assert outcomes_of(flow.steps[step]) == expected


def test_a_malformed_file_names_the_field(tmp_path: Path) -> None:
    data = shipped()
    data["steps"]["merge"]["kind"] = "teleport"
    path = tmp_path / "flow.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(FlowValidationError) as caught:
        load_flow(path, LANE_ROLES)
    assert "steps.merge" in str(caught.value)


def test_a_file_that_is_not_yaml_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "flow.yaml"
    path.write_text("steps: [unclosed\n")
    with pytest.raises(FlowValidationError, match="not valid YAML"):
        load_flow(path, LANE_ROLES)


def test_a_file_that_is_not_a_mapping_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "flow.yaml"
    path.write_text("- plan\n")
    with pytest.raises(FlowValidationError, match="mapping"):
        load_flow(path, LANE_ROLES)
