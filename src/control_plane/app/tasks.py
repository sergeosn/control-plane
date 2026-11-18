"""Task files behind `cplane task new`, `list` and `show` (docs/SPEC.md sections 6 and 14).

A task is a Markdown file `tasks/T-NNNN-slug.md` in the home repository. A new task is
written, committed on its own and then mirrored to the index; an index failure is logged and
never undoes the file (section 5). The run service moves a task's `status` with
`set_status`, which rewrites the one `status:` line of the front matter and nothing else.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from control_plane.domain.task import Task, TaskFileError, TaskStatus
from control_plane.store.files import write_text_atomic
from control_plane.store.git import Git
from control_plane.store.home import Home
from control_plane.store.index import Index
from control_plane.store.plane import PlaneConfigError, load_plane

_ID_PREFIX = re.compile(r"^T-(\d{4})")
_STATUS_LINE = re.compile(r"^status:[ \t]*\S*[ \t]*$", re.MULTILINE)
SLUG_MAX = 48


class TaskError(Exception):
    """A task cannot be created, found or updated. The message says why, in one line."""


@dataclass(frozen=True)
class TaskEntry:
    task: Task
    path: Path

    def relative(self, home: Home) -> str:
        return self.path.relative_to(home.path).as_posix()


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:SLUG_MAX].rstrip("-") or "task"


def _entries(home: Home) -> tuple[list[TaskEntry], list[str]]:
    entries: list[TaskEntry] = []
    problems: list[str] = []
    for path in sorted(home.tasks.glob("*.md")):
        try:
            entries.append(TaskEntry(Task.from_markdown(path.read_text(encoding="utf-8")), path))
        except TaskFileError as exc:
            problems.append(f"{path.relative_to(home.path).as_posix()}: {exc}")
    return entries, problems


def list_tasks(home: Home) -> list[TaskEntry]:
    """Every valid task file, by id."""
    return sorted(_entries(home)[0], key=lambda entry: entry.task.id)


def broken_task_files(home: Home) -> list[str]:
    """The task files that do not parse, each with the reason."""
    return _entries(home)[1]


def find_task(home: Home, task_id: str) -> TaskEntry:
    for entry in list_tasks(home):
        if entry.task.id == task_id:
            return entry
    raise TaskError(f"no task {task_id} in {home.tasks}")


def _next_id(home: Home) -> str:
    numbers = [0]
    for path in home.tasks.glob("T-*.md"):
        match = _ID_PREFIX.match(path.name)
        if match:
            numbers.append(int(match.group(1)))
    return f"T-{max(numbers) + 1:04d}"


async def new_task(
    home: Home,
    *,
    title: str,
    project: str,
    goal: str,
    criteria: Sequence[str],
    priority: int = 2,
    plan_approval: bool = False,
    lane: str | None = None,
    max_iterations: int | None = None,
    max_cost_usd: float | None = None,
) -> TaskEntry:
    """Write a new task in the backlog with the next free id and the defaults of `plane.yaml`."""
    try:
        defaults = load_plane(home).defaults
    except PlaneConfigError as exc:
        raise TaskError(str(exc)) from exc
    task_id = _next_id(home)
    data = {
        "id": task_id,
        "title": title,
        "project": project,
        "lane": lane or defaults.lane,
        "priority": priority,
        "plan_approval": plan_approval,
        "roles": defaults.roles.model_dump(),
        "budget": {
            "max_iterations": max_iterations or defaults.budget.max_iterations,
            "max_cost_usd": max_cost_usd or defaults.budget.max_cost_usd,
        },
        "status": "backlog",
        "goal": goal.strip(),
        "acceptance_criteria": [
            {"id": f"AC{number}", "text": text.strip()}
            for number, text in enumerate(criteria, start=1)
        ],
    }
    try:
        task = Task.model_validate(data)
    except ValueError as exc:
        raise TaskError(f"task {task_id}: {_one_line(exc)}") from exc
    path = home.tasks / f"{task_id}-{slugify(title)}.md"
    return await _store(home, task, path, task.to_markdown())


async def add_task_file(home: Home, source: Path) -> TaskEntry:
    """Copy a task file written by hand into the backlog, unchanged, once it parses."""
    try:
        text = source.read_text(encoding="utf-8")
        task = Task.from_markdown(text)
    except OSError as exc:
        raise TaskError(f"{source}: cannot be read: {exc}") from exc
    except TaskFileError as exc:
        raise TaskError(f"{source}: {exc}") from exc
    if any(entry.task.id == task.id for entry in list_tasks(home)):
        raise TaskError(f"task {task.id} already exists in {home.tasks}")
    name = (
        source.name
        if source.name.startswith(f"{task.id}-")
        else f"{task.id}-{slugify(task.title)}.md"
    )
    return await _store(home, task, home.tasks / name, text)


async def _store(home: Home, task: Task, path: Path, text: str) -> TaskEntry:
    if path.exists():
        raise TaskError(f"{path} already exists")
    write_text_atomic(path, text)
    relative = path.relative_to(home.path).as_posix()
    Git(home.path).commit_paths(f"Add task {task.id}: {task.title}", relative)
    await sync_task_index(home)
    return TaskEntry(task, path)


def set_status(home: Home, task_id: str, status: TaskStatus) -> TaskEntry:
    """Rewrite the `status:` line of a task file; every other byte stays as it was."""
    entry = find_task(home, task_id)
    text = entry.path.read_text(encoding="utf-8")
    end = text.find("\n---", 3)
    front, rest = text[:end], text[end:]
    updated, count = _STATUS_LINE.subn(f"status: {status}", front, count=1)
    if count != 1:
        raise TaskError(f"{entry.path}: no `status:` line in the front matter")
    write_text_atomic(entry.path, updated + rest)
    return TaskEntry(Task.from_markdown(updated + rest), entry.path)


def commit_status(home: Home, entry: TaskEntry) -> None:
    """Commit a task file whose status changed, alone; nothing when it did not change."""
    git = Git(home.path)
    if git.status(entry.relative(home)):
        git.commit_paths(f"Task {entry.task.id}: {entry.task.status}", entry.relative(home))


async def sync_task_index(home: Home) -> None:
    index = Index(home)

    async def write() -> None:
        try:
            await index.open()
            await index.sync_tasks()
        finally:
            await index.close()

    await index.attempt(write(), "tasks")


def _one_line(exc: ValueError) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        return "; ".join(
            f"{'.'.join(str(p) for p in item['loc'])}: {item['msg']}" for item in errors()
        )
    return str(exc)
