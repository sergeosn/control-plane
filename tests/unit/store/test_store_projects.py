"""Task 13: `project.yaml` in the home repository holds the gate commands (docs/SPEC.md section 11).

The commands come from the home repository, never from the worktree, so an agent cannot
edit a gate into passing.
"""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from control_plane.domain.project import CoverageGate, GateSettings, ProjectConfig
from control_plane.store import Home, create_home
from control_plane.store.projects import ProjectConfigError, load_project, project_file

PROJECT_YAML = {
    "version": 1,
    "repo": "/srv/repos/control-plane",
    "protected_paths": ["src/control_plane/policy/", "tests/guardrails/"],
    "gates": {
        "format": {"argv": ["uv", "run", "ruff", "format", "--check", "."]},
        "lint": {"argv": ["uv", "run", "ruff", "check", "."]},
        "tests": {"argv": ["uv", "run", "pytest", "-q"], "timeout_s": 600},
        "coverage": {
            "argv": ["uv", "run", "pytest", "--cov", "--cov-report=json:{report}"],
            "threshold": 85,
        },
        "diff_size": {"max_changed_lines": 400},
        "secrets": {"patterns": {"internal_token": "itk_[a-z0-9]{24}"}},
        "guardrail_regression": {"argv": ["uv", "run", "pytest", "tests/guardrails"]},
    },
}


@pytest.fixture
def home(tmp_path: Path) -> Home:
    return create_home(tmp_path / "home")


def put_project(home: Home, data: object, name: str = "control-plane") -> Path:
    path = project_file(home, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else yaml.safe_dump(data))
    return path


def test_a_project_yaml_loads_with_its_gate_commands(home: Home) -> None:
    put_project(home, PROJECT_YAML)

    project = load_project(home, "control-plane")

    assert project.repo == Path("/srv/repos/control-plane")
    assert project.protected_paths == ["src/control_plane/policy/", "tests/guardrails/"]
    assert project.gates.lint is not None
    assert project.gates.lint.argv == ["uv", "run", "ruff", "check", "."]
    assert project.gates.tests is not None and project.gates.tests.timeout_s == 600
    assert project.gates.coverage is not None and project.gates.coverage.threshold == 85
    assert project.gates.diff_size.max_changed_lines == 400
    assert project.gates.secrets.patterns == {"internal_token": "itk_[a-z0-9]{24}"}


def test_the_project_file_lives_in_the_home_repository(home: Home) -> None:
    assert project_file(home, "demo") == home.projects / "demo" / "project.yaml"


def test_the_control_checkout_defaults_to_the_repo_path_with_a_control_suffix(home: Home) -> None:
    put_project(home, PROJECT_YAML)

    project = load_project(home, "control-plane")

    assert project.guardrail_control() == Path("/srv/repos/control-plane-control")


def test_a_relative_repo_and_control_resolve_against_the_home(home: Home) -> None:
    data = {
        "repo": "../repos/demo",
        "gates": {"guardrail_regression": {"argv": ["pytest"], "control": "../repos/ctl"}},
    }
    put_project(home, data, name="demo")

    project = load_project(home, "demo")

    assert project.repo == (home.path / "../repos/demo").resolve()
    assert project.guardrail_control() == (home.path / "../repos/ctl").resolve()


def test_gates_without_settings_get_their_defaults() -> None:
    gates = GateSettings()

    assert gates.format is None and gates.tests is None and gates.coverage is None
    assert gates.guardrail_regression is None
    assert gates.diff_size.max_changed_lines > 0
    assert "test_*.py" in gates.test_integrity.patterns
    assert gates.secrets.patterns == {}


def test_a_missing_project_file_is_reported(home: Home) -> None:
    with pytest.raises(ProjectConfigError, match="no project.yaml for project 'ghost'"):
        load_project(home, "ghost")


@pytest.mark.parametrize("name", ["", "../escape", "a/b", ".hidden"])
def test_a_project_name_that_is_not_a_plain_folder_name_is_refused(home: Home, name: str) -> None:
    with pytest.raises(ProjectConfigError, match="project name"):
        project_file(home, name)


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"gates": {"coverage": {"argv": ["pytest"], "threshold": 150}}}, "gates.coverage"),
        ({"gates": {"lint": {"argv": []}}}, "gates.lint.argv"),
        ({"gates": {"typecheck": {"argv": ["mypy"]}}}, "gates.typecheck"),
        ({"gates": {"secrets": {"patterns": {"bad": "("}}}}, "gates.secrets.patterns"),
        ({"gates": {"diff_size": {"max_changed_lines": 0}}}, "gates.diff_size.max_changed_lines"),
        ({"repo": None}, "repo"),
    ],
)
def test_a_malformed_project_file_is_rejected_naming_the_field(
    home: Home, change: dict[str, object], field: str
) -> None:
    put_project(home, {**PROJECT_YAML, **change})

    with pytest.raises(ProjectConfigError) as caught:
        load_project(home, "control-plane")

    assert field in str(caught.value)
    assert "project.yaml" in str(caught.value)


def test_a_project_file_that_is_not_a_mapping_is_rejected(home: Home) -> None:
    put_project(home, "- just\n- a list\n")

    with pytest.raises(ProjectConfigError, match="mapping"):
        load_project(home, "control-plane")


def test_a_project_file_that_is_not_yaml_is_rejected(home: Home) -> None:
    put_project(home, "repo: [unclosed\n")

    with pytest.raises(ProjectConfigError, match="project.yaml"):
        load_project(home, "control-plane")


def test_the_coverage_command_must_name_where_the_report_goes() -> None:
    with pytest.raises(ValidationError, match=r"\{report\}"):
        CoverageGate(argv=["pytest", "--cov"], threshold=80)


def test_a_project_without_a_guardrail_gate_has_no_control_checkout() -> None:
    assert ProjectConfig(repo=Path("/r")).guardrail_control() is None
