"""Task 3: `create_home` produces the layout from docs/SPEC.md section 5 as a git repository."""

import re
from pathlib import Path

import pytest
import yaml

from control_plane.store.git import Git
from control_plane.store.home import Home, HomeExistsError, HomeNotFoundError, create_home

EXPECTED_FILES = [
    "plane.yaml",
    ".gitignore",
    "lanes/development/procedure.md",
    "lanes/development/rubric.md",
    "lanes/development/flow.yaml",
    "lanes/development/roles/planner.md",
    "lanes/development/roles/worker.md",
    "lanes/development/roles/reviewer.md",
    "rules/tools.yaml",
]

EXPECTED_DIRS = ["projects", "tasks", "runs", ".index", "lanes/development/roles"]


def test_create_home_writes_every_file_and_directory_of_the_section_5_layout(home: Home) -> None:
    for relative in EXPECTED_FILES:
        assert (home.path / relative).is_file(), relative
    for relative in EXPECTED_DIRS:
        assert (home.path / relative).is_dir(), relative


def test_home_knows_its_own_paths(home: Home) -> None:
    assert home.plane_yaml == home.path / "plane.yaml"
    assert home.projects == home.path / "projects"
    assert home.lanes == home.path / "lanes"
    assert home.rules == home.path / "rules"
    assert home.tasks == home.path / "tasks"
    assert home.runs == home.path / "runs"
    assert home.index == home.path / ".index"
    assert home.worktrees == home.path / ".worktrees"


def test_create_home_makes_a_git_repository_with_one_clean_commit(home: Home) -> None:
    git = Git(home.path)

    assert git.current_branch() == "main"
    assert git.log_subjects() == ["Initialize control plane home"]
    assert git.status() == []


def test_the_first_commit_tracks_the_whole_layout(home: Home) -> None:
    tracked = Git(home.path).tracked(".")

    for relative in EXPECTED_FILES:
        assert relative in tracked, relative
    for relative in ("projects", "tasks", "runs"):
        assert any(p.startswith(relative + "/") for p in tracked), f"{relative}/ is not committed"


def test_index_and_worktrees_are_ignored_by_git(home: Home) -> None:
    git = Git(home.path)

    assert git.is_ignored(".index/state.db")
    assert git.is_ignored(".worktrees/R-1/x")


def test_flow_yaml_is_the_default_development_flow_from_section_7(home: Home) -> None:
    flow = yaml.safe_load((home.lanes / "development" / "flow.yaml").read_text())

    assert flow["name"] == "development-default"
    assert flow["start"] == "plan"
    assert set(flow["steps"]) == {
        "plan",
        "question",
        "plan_gate",
        "implement",
        "checks",
        "review",
        "merge_gate",
        "merge",
    }
    for name, step in flow["steps"].items():
        assert "next" in step, f"step {name} has no `next` transition map"
        assert True not in step, f"step {name} used the YAML 1.1 boolean key `on`"


def test_tools_yaml_holds_the_default_classification_rules_from_section_10_3(home: Home) -> None:
    rules = yaml.safe_load((home.rules / "tools.yaml").read_text())

    assert rules["version"] == 1
    assert rules["default"] == "ask"
    assert [rule["id"] for rule in rules["rules"]] == [
        "read-tools",
        "local-file-writes",
        "local-commands",
        "network-and-deps",
        "history-rewrite",
        "never",
    ]
    assert {rule["class"] for rule in rules["rules"]} == {
        "read",
        "write-local",
        "write-external",
        "destructive",
        "blocked",
    }


def test_plane_yaml_defines_the_profiles_the_task_fixture_names(home: Home) -> None:
    plane = yaml.safe_load(home.plane_yaml.read_text())

    assert {"default", "strict"} <= set(plane["profiles"])
    for profile in plane["profiles"].values():
        assert profile["wire_format"] in {"anthropic", "openai-compatible"}
        assert profile["model"]
    assert plane["defaults"]["lane"] == "development"
    assert isinstance(plane["prices"], dict)


def test_rubric_rows_carry_ids(home: Home) -> None:
    rows = [
        line
        for line in (home.lanes / "development" / "rubric.md").read_text().splitlines()
        if line.startswith("- ")
    ]

    assert rows, "the rubric has no rows"
    ids = [re.match(r"^- \[([A-Z0-9_-]+)\] ", line) for line in rows]
    assert all(ids), rows
    assert len({m.group(1) for m in ids if m}) == len(rows), "rubric ids are not unique"


@pytest.mark.parametrize("role", ["planner", "worker", "reviewer"])
def test_role_prompts_say_that_text_grants_nothing(home: Home, role: str) -> None:
    text = (home.lanes / "development" / "roles" / f"{role}.md").read_text()

    assert "data" in text and "instructions" in text


def test_create_home_refuses_a_non_empty_directory(tmp_path: Path) -> None:
    target = tmp_path / "home"
    target.mkdir()
    (target / "something.txt").write_text("")

    with pytest.raises(HomeExistsError, match="not empty"):
        create_home(target)

    assert not (target / "plane.yaml").exists()


def test_create_home_accepts_an_empty_existing_directory(tmp_path: Path) -> None:
    target = tmp_path / "home"
    target.mkdir()

    home = create_home(target)

    assert home.path == target
    assert home.plane_yaml.is_file()


def test_create_home_creates_missing_parent_directories(tmp_path: Path) -> None:
    home = create_home(tmp_path / "deep" / "er" / "home")

    assert home.plane_yaml.is_file()


def test_locate_prefers_the_option_over_the_environment(tmp_path: Path) -> None:
    by_option = create_home(tmp_path / "a")
    by_env = create_home(tmp_path / "b")

    found = Home.locate(str(by_option.path), {"CPLANE_HOME": str(by_env.path)})

    assert found == by_option


def test_locate_falls_back_to_cplane_home(tmp_path: Path) -> None:
    by_env = create_home(tmp_path / "b")

    assert Home.locate(None, {"CPLANE_HOME": str(by_env.path)}) == by_env


def test_locate_without_option_or_environment_raises(tmp_path: Path) -> None:
    with pytest.raises(HomeNotFoundError, match="--home or CPLANE_HOME"):
        Home.locate(None, {})


def test_locate_rejects_a_directory_that_is_not_a_home(tmp_path: Path) -> None:
    with pytest.raises(HomeNotFoundError, match="plane.yaml"):
        Home.locate(str(tmp_path), {})
