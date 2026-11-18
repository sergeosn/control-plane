"""Task 15: `cplane project add <name> --repo <path>` (docs/SPEC.md sections 5, 11 and 14)."""

from pathlib import Path

import pytest

from control_plane.app import projects as app_projects
from control_plane.app.projects import ProjectError
from control_plane.domain.project import CommandGate, GateSettings
from control_plane.store.git import Git
from control_plane.store.home import Home, create_home
from control_plane.store.projects import load_project
from fixtures.project_repo import make_project


@pytest.fixture
def home(tmp_path: Path) -> Home:
    return create_home(tmp_path / "home")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_project(tmp_path / "project").root


def test_add_writes_project_yaml_and_knowledge_and_commits(home: Home, repo: Path) -> None:
    project = app_projects.add_project(home, "demo", repo, protected=["tests/guardrails/"])

    assert project.repo == repo.resolve()
    assert load_project(home, "demo") == project
    assert project.protected_paths == ["tests/guardrails/"]
    knowledge = home.projects / "demo" / "knowledge"
    assert (knowledge / "facts.md").read_text().startswith("# Facts")
    assert (knowledge / "constraints.md").read_text().startswith("# Constraints")
    git = Git(home.path)
    assert git.log_subjects()[0] == "Add project demo"
    assert not git.status("projects")


def test_add_records_the_gates_given(home: Home, repo: Path) -> None:
    gates = GateSettings(tests=CommandGate(argv=["python", "-m", "pytest"], timeout_s=60))

    project = app_projects.add_project(home, "demo", repo, gates=gates)

    assert load_project(home, "demo").gates.tests == gates.tests
    assert project.gates.tests is not None


def test_add_refuses_a_second_project_of_the_same_name(home: Home, repo: Path) -> None:
    app_projects.add_project(home, "demo", repo)

    with pytest.raises(ProjectError, match="project demo already exists"):
        app_projects.add_project(home, "demo", repo)


def test_add_refuses_a_path_that_is_not_a_git_repository_on_main(
    home: Home, tmp_path: Path
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(ProjectError, match="not a git repository with a main branch"):
        app_projects.add_project(home, "demo", plain)
    with pytest.raises(ProjectError, match="does not exist"):
        app_projects.add_project(home, "demo", tmp_path / "missing")


def test_add_refuses_a_name_that_is_not_a_folder_name(home: Home, repo: Path) -> None:
    with pytest.raises(ProjectError, match="plain folder name"):
        app_projects.add_project(home, "../escape", repo)


def test_list_projects(home: Home, repo: Path) -> None:
    app_projects.add_project(home, "b-demo", repo)
    app_projects.add_project(home, "a-demo", repo)

    assert app_projects.list_projects(home) == ["a-demo", "b-demo"]
