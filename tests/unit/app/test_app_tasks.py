"""Task 15: task files behind `cplane task new`, `list` and `show` (docs/SPEC.md 6 and 14)."""

import shutil
from pathlib import Path

import pytest

from control_plane.app import tasks as app_tasks
from control_plane.app.tasks import TaskError
from control_plane.domain import Task
from control_plane.store.git import Git
from control_plane.store.home import Home, create_home
from control_plane.store.index import Index

FIXTURE = Path(__file__).parents[2] / "fixtures" / "tasks" / "T-0017-json-status.md"


@pytest.fixture
def home(tmp_path: Path) -> Home:
    return create_home(tmp_path / "home")


async def new(home: Home, title: str = "Add --json output to `cplane status`", **extra):
    fields = {
        "title": title,
        "project": "control-plane",
        "goal": "`cplane status` can print machine-readable output.",
        "criteria": ["`--json` prints one object per run.", "Both paths are tested."],
    }
    fields.update(extra)
    return await app_tasks.new_task(home, **fields)


async def test_a_new_task_gets_the_next_id_a_slug_and_the_defaults(home: Home) -> None:
    first = await new(home)
    second = await new(home, title="Second task")

    assert first.task.id == "T-0001" and second.task.id == "T-0002"
    assert first.path == home.tasks / "T-0001-add-json-output-to-cplane-status.md"
    task = first.task
    assert task.status == "backlog"
    assert task.lane == "development"
    assert task.roles.reviewer == "strict"
    assert task.budget.max_iterations == 3
    assert [c.id for c in task.acceptance_criteria] == ["AC1", "AC2"]
    assert Task.from_markdown(first.path.read_text()) == task


async def test_a_new_task_is_committed_and_indexed(home: Home) -> None:
    created = await new(home)

    git = Git(home.path)
    assert git.log_subjects()[0] == f"Add task {created.task.id}: {created.task.title}"
    assert not git.status("tasks")
    async with Index(home) as index:
        assert [row["id"] for row in await index.tasks()] == ["T-0001"]


async def test_a_new_task_commits_only_its_own_file(home: Home) -> None:
    (home.path / "scratch.txt").write_text("not mine\n")
    Git(home.path).add("scratch.txt")

    await new(home)

    assert Git(home.path).status("scratch.txt") == [("added", "scratch.txt")]


async def test_options_override_the_defaults(home: Home) -> None:
    created = await new(
        home, priority=1, plan_approval=True, max_iterations=5, max_cost_usd=9.5, lane="development"
    )

    task = created.task
    assert (task.priority, task.plan_approval) == (1, True)
    assert (task.budget.max_iterations, task.budget.max_cost_usd) == (5, 9.5)


async def test_a_task_needs_a_criterion(home: Home) -> None:
    with pytest.raises(TaskError, match="acceptance_criteria"):
        await new(home, criteria=[])


async def test_a_task_file_can_be_added_as_written(home: Home) -> None:
    added = await app_tasks.add_task_file(home, FIXTURE)

    assert added.path == home.tasks / FIXTURE.name
    assert added.path.read_text() == FIXTURE.read_text()
    with pytest.raises(TaskError, match="T-0017 already exists"):
        await app_tasks.add_task_file(home, FIXTURE)


async def test_a_malformed_task_file_is_refused_naming_the_field(
    home: Home, tmp_path: Path
) -> None:
    bad = tmp_path / "T-0001-bad.md"
    bad.write_text(FIXTURE.read_text().replace("priority: 2", "priority: high"))

    with pytest.raises(TaskError, match="priority"):
        await app_tasks.add_task_file(home, bad)
    assert not (home.tasks / bad.name).exists()


async def test_list_and_find(home: Home) -> None:
    await new(home, title="One")
    await new(home, title="Two")

    assert [entry.task.title for entry in app_tasks.list_tasks(home)] == ["One", "Two"]
    assert app_tasks.find_task(home, "T-0002").task.title == "Two"
    with pytest.raises(TaskError, match="no task T-0009"):
        app_tasks.find_task(home, "T-0009")


async def test_list_reports_a_broken_file_without_hiding_the_others(home: Home) -> None:
    await new(home, title="One")
    (home.tasks / "T-0005-broken.md").write_text("no front matter\n")

    listed = app_tasks.list_tasks(home)

    assert [entry.task.title for entry in listed] == ["One"]
    assert app_tasks.broken_task_files(home) == [
        "tasks/T-0005-broken.md: front matter: file must start with a '---' line"
    ]


async def test_set_status_rewrites_only_the_status_line(home: Home) -> None:
    shutil.copyfile(FIXTURE, home.tasks / FIXTURE.name)

    entry = app_tasks.set_status(home, "T-0017", "running")

    assert entry.task.status == "running"
    before = FIXTURE.read_text().splitlines()
    after = entry.path.read_text().splitlines()
    assert [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b] == [
        before.index("status: backlog")
    ]
