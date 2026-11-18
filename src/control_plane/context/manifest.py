"""Building and verifying `manifest.json` (docs/SPEC.md section 9).

The manifest records what a run was given: the task file hash, the project base commit, the
engine commit, hashes of the lane's flow, rubric and role prompts and of the rules, the
model and parameters per role, the budgets, and every context file with its SHA-256.

Paths of home files (task, flow, rubric, role prompts, rules) are relative to the home
repository; paths of context files are relative to the run folder. `verify_manifest` checks
every recorded hash against the file it names and reports each one that changed or is gone.
"""

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from control_plane.domain.manifest import FileHash, Manifest, RoleModel, RunBudget
from control_plane.domain.task import Task, TaskFileError

MismatchKind = Literal["changed", "missing"]
Where = Literal["home", "run"]


class ManifestError(Exception):
    """A manifest cannot be built, read or extended. The message says why."""


@dataclass(frozen=True, order=True)
class ManifestMismatch:
    kind: MismatchKind
    where: Where
    """`home` for a path relative to the home repository, `run` for one in the run folder."""
    path: str


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hash(base: Path, path: Path) -> FileHash:
    """The hash of `path`, recorded under its path relative to `base`."""
    return FileHash(path=path.relative_to(base).as_posix(), sha256=sha256_of(path.read_bytes()))


def start_manifest(
    *,
    home: Path,
    run_id: str,
    task_file: Path,
    project_base_commit: str,
    engine_commit: str,
    models: Sequence[RoleModel],
    budget: RunBudget,
    created_at: datetime,
) -> Manifest:
    """The manifest of a run before any step: no context files yet."""
    home = home.resolve()
    task_file = task_file.resolve()
    if not task_file.is_relative_to(home):
        raise ManifestError(f"task file {task_file} is not inside the home {home}")
    try:
        task = Task.from_markdown(task_file.read_text())
    except TaskFileError as exc:
        raise ManifestError(f"task file {task_file}: {exc}") from exc
    lane = home / "lanes" / task.lane
    return Manifest(
        run_id=run_id,
        task_id=task.id,
        task_file=file_hash(home, task_file),
        project=task.project,
        project_base_commit=project_base_commit,
        engine_commit=engine_commit,
        flow=_required(home, lane / "flow.yaml"),
        rubric=_required(home, lane / "rubric.md"),
        role_prompts=[file_hash(home, p) for p in sorted((lane / "roles").glob("*.md"))],
        rules=[file_hash(home, p) for p in sorted((home / "rules").glob("*.yaml"))],
        models=list(models),
        budget=budget,
        context_files=[],
        created_at=created_at,
    )


def _required(home: Path, path: Path) -> FileHash:
    if not path.is_file():
        raise ManifestError(f"lane {path.parent.name} has no {path.name}: {path}")
    return file_hash(home, path)


def add_context_files(manifest: Manifest, entries: Iterable[FileHash]) -> Manifest:
    """`manifest` with `entries` appended to its context files. A path is recorded once."""
    recorded = {entry.path for entry in manifest.context_files}
    added = list(entries)
    for entry in added:
        if entry.path in recorded:
            raise ManifestError(f"context file {entry.path} is already recorded")
        recorded.add(entry.path)
    return manifest.model_copy(update={"context_files": [*manifest.context_files, *added]})


def verify_manifest(manifest: Manifest, *, home: Path, run_folder: Path) -> list[ManifestMismatch]:
    """Every recorded hash that no longer matches its file, sorted by place and path."""
    home_files = [
        manifest.task_file,
        manifest.flow,
        manifest.rubric,
        *manifest.role_prompts,
        *manifest.rules,
    ]
    found = [
        *_check(home_files, home, "home"),
        *_check(manifest.context_files, run_folder, "run"),
    ]
    return sorted(found, key=lambda m: (m.where, m.path))


def _check(entries: Iterable[FileHash], base: Path, where: Where) -> list[ManifestMismatch]:
    found = []
    for entry in entries:
        path = base / entry.path
        if not path.is_file():
            found.append(ManifestMismatch("missing", where, entry.path))
        elif sha256_of(path.read_bytes()) != entry.sha256:
            found.append(ManifestMismatch("changed", where, entry.path))
    return found
