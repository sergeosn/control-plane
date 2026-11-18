"""A home ready for context building (docs/SPEC.md section 9), in a temporary directory.

It holds a registered project with knowledge files (one archived), a task file copied from
`fixtures/tasks`, a project repository with a run worktree, and a run folder whose
`manifest.json` has been started.
"""

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from control_plane.app import context as app_context
from control_plane.context.manifest import start_manifest
from control_plane.domain import RoleModel, RunBudget
from control_plane.store import Home, RunFolder, RunStore, create_home
from control_plane.workspace import Worktree, create_worktree
from fixtures.project_repo import make_project

FIXTURE_TASKS = Path(__file__).parent / "tasks"
DEFAULT_TASK = "T-0017-json-status.md"
PROJECT = "control-plane"
RUN_ID = "R-0001"
ENGINE_COMMIT = "e" * 40
T0 = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)

KNOWLEDGE = {
    "facts.md": "# Facts\n\nThe engine is written in Python 3.12.\nIt runs from a control copy.\n",
    "constraints.md": "# Constraints\n\nTests never touch the network.\n",
    "decisions/0001-files-first.md": "# 0001 Files first, index second\n\nThe index is derived.\n",
    "archive/0000-retired.md": "# Retired\n\nThis decision no longer holds.\n",
}

MODELS = [
    RoleModel(role="planner", profile="default", model="claude-sonnet-5-5", max_tokens=8192),
    RoleModel(role="worker", profile="default", model="claude-sonnet-5-5", max_tokens=8192),
    RoleModel(
        role="reviewer",
        profile="strict",
        model="claude-opus-5-5",
        max_tokens=8192,
        temperature=0.0,
    ),
]
BUDGET = RunBudget(max_iterations=3, max_cost_usd=6.0, max_wall_minutes=60)


@dataclass(frozen=True)
class ContextHome:
    home: Home
    task_file: Path
    worktree: Worktree
    run: RunFolder


def make_context_home(tmp_path: Path, task: str = DEFAULT_TASK) -> ContextHome:
    home = create_home(tmp_path / "home")
    knowledge = home.projects / PROJECT / "knowledge"
    for name, text in KNOWLEDGE.items():
        path = knowledge / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    task_file = home.tasks / task
    shutil.copyfile(FIXTURE_TASKS / task, task_file)
    project = make_project(tmp_path / "project")
    tree = create_worktree(project.root, home.worktrees, RUN_ID, project.head())
    run = RunStore(home, clock=lambda: T0).create(RUN_ID)
    manifest = start_manifest(
        home=home.path,
        run_id=RUN_ID,
        task_file=task_file,
        project_base_commit=tree.base_commit,
        engine_commit=ENGINE_COMMIT,
        models=MODELS,
        budget=BUDGET,
        created_at=T0,
    )
    app_context.write_manifest(run, manifest)
    return ContextHome(home=home, task_file=task_file, worktree=tree, run=run)
