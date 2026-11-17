"""Task 2: Plan, WorkReport, Review and Manifest round-trip through their JSON files."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from control_plane.domain import (
    Criterion,
    CriterionReview,
    FileHash,
    Finding,
    Manifest,
    Plan,
    PlannedTest,
    PlanStep,
    Review,
    RoleModel,
    RubricRow,
    RunBudget,
    WorkReport,
)


def make_plan(**overrides: object) -> Plan:
    fields: dict[str, object] = {
        "summary": "Add a --json flag.",
        "steps": [
            PlanStep(id="S1", description="Add the flag.", files=["src/control_plane/cli.py"]),
            PlanStep(id="S2", description="Test it.", files=["tests/unit/test_cli.py"]),
        ],
        "files_to_touch": ["src/control_plane/cli.py", "tests/unit/test_cli.py"],
        "tests": [
            PlannedTest(criterion="AC1", description="JSON lines are printed."),
            PlannedTest(criterion="AC2", description="Default output is unchanged."),
        ],
        "risks": ["Output format changes later."],
        "out_of_scope": ["Pagination."],
        "questions": [],
    }
    fields.update(overrides)
    return Plan(**fields)


def make_work_report() -> WorkReport:
    return WorkReport(
        summary="Done.",
        criteria=[
            Criterion(id="AC1", status="done", evidence="tests/unit/test_cli.py::test_json"),
            Criterion(id="AC2", status="not_done", evidence="Not started."),
        ],
        deviations=["Skipped S2."],
        open_issues=["AC2 remains."],
    )


def make_review() -> Review:
    return Review(
        rubric=[
            RubricRow(row_id="R1", result="pass", evidence="Tests exist."),
            RubricRow(row_id="R2", result="n/a", evidence="No migration."),
        ],
        criteria=[CriterionReview(id="AC1", met=True, evidence="Seen in the diff.")],
        findings=[
            Finding(
                severity="minor",
                file="src/control_plane/cli.py",
                line=12,
                description="Unused import.",
                suggestion="Remove it.",
            ),
            Finding(severity="minor", file="README.md", description="Typo."),
        ],
    )


def make_manifest() -> Manifest:
    return Manifest(
        run_id="R-20261117-0001",
        task_id="T-0017",
        task_file=FileHash(path="tasks/T-0017-json-status.md", sha256="a" * 64),
        project="control-plane",
        project_base_commit="0123456789abcdef0123456789abcdef01234567",
        engine_commit="fedcba9876543210fedcba9876543210fedcba98",
        flow=FileHash(path="lanes/development/flow.yaml", sha256="b" * 64),
        rubric=FileHash(path="lanes/development/rubric.md", sha256="c" * 64),
        role_prompts=[FileHash(path="lanes/development/roles/planner.md", sha256="d" * 64)],
        rules=[FileHash(path="rules/tools.yaml", sha256="e" * 64)],
        models=[
            RoleModel(role="planner", profile="default", model="claude-x", max_tokens=8000),
            RoleModel(
                role="worker",
                profile="default",
                model="claude-x",
                max_tokens=16000,
                temperature=0.0,
            ),
        ],
        budget=RunBudget(max_iterations=3, max_cost_usd=6.0, max_wall_minutes=60),
        context_files=[FileHash(path="context/BRIEF.md", sha256="f" * 64)],
        created_at=datetime(2026, 11, 17, 10, 0, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    "artifact",
    [make_plan(), make_work_report(), make_review(), make_manifest()],
    ids=["plan", "work_report", "review", "manifest"],
)
def test_artifact_round_trips_through_json(artifact: Plan | WorkReport | Review | Manifest) -> None:
    text = artifact.to_json()

    assert type(artifact).from_json(text) == artifact


def test_to_json_is_indented_and_ends_with_newline() -> None:
    text = make_plan().to_json()

    assert text.endswith("}\n")
    assert '\n  "summary"' in text


def test_plan_with_questions_is_blocked() -> None:
    assert make_plan().blocked is False
    assert make_plan(questions=["Which flag name?"]).blocked is True


def test_unknown_keys_are_rejected() -> None:
    with pytest.raises(ValidationError, match="changed_files"):
        WorkReport.from_json(
            '{"summary": "x", "criteria": [], "deviations": [], "open_issues": [],'
            ' "changed_files": ["a.py"]}'
        )


def test_criterion_status_is_constrained() -> None:
    with pytest.raises(ValidationError, match="status"):
        Criterion(id="AC1", status="maybe", evidence="")  # type: ignore[arg-type]


def test_finding_severity_is_constrained() -> None:
    with pytest.raises(ValidationError, match="severity"):
        Finding(severity="nit", file="a.py", description="x")  # type: ignore[arg-type]


def test_manifest_sha256_is_validated() -> None:
    with pytest.raises(ValidationError, match="sha256"):
        FileHash(path="a", sha256="not-a-hash")
