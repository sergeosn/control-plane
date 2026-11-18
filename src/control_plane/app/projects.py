"""`cplane project add <name> --repo <path>` (docs/SPEC.md sections 5, 11 and 14).

A project is `projects/<name>/project.yaml` with the repository path, the protected paths and
the gate commands, plus its knowledge files. The gate commands are usually edited by hand
afterwards; `add_project` takes them for callers that know them up front, such as the
selfcheck.
"""

from collections.abc import Sequence
from pathlib import Path

import yaml

from control_plane.domain.project import GateSettings, ProjectConfig
from control_plane.store.files import write_text_atomic
from control_plane.store.git import Git
from control_plane.store.home import Home
from control_plane.store.projects import (
    PROJECT_FILE,
    ProjectConfigError,
    load_project,
    project_file,
)
from control_plane.workspace.merge import MAIN_BRANCH

KNOWLEDGE_FILES = {
    "facts.md": "# Facts\n\nWhat is true about this project and worth knowing before a change.\n",
    "constraints.md": "# Constraints\n\nWhat every change to this project must respect.\n",
    "decisions/.gitkeep": "",
}


class ProjectError(Exception):
    """A project cannot be registered. The message says why, in one line."""


def add_project(
    home: Home,
    name: str,
    repo: Path,
    *,
    protected: Sequence[str] = (),
    gates: GateSettings | None = None,
) -> ProjectConfig:
    try:
        path = project_file(home, name)
    except ProjectConfigError as exc:
        raise ProjectError(str(exc)) from exc
    if path.parent.exists():
        raise ProjectError(f"project {name} already exists: {path.parent}")
    repo = repo.expanduser().resolve()
    if not repo.is_dir():
        raise ProjectError(f"repository {repo} does not exist")
    probe = Git(repo).process("rev-parse", "--verify", "--quiet", f"refs/heads/{MAIN_BRANCH}")
    if probe.returncode != 0:
        raise ProjectError(f"{repo} is not a git repository with a {MAIN_BRANCH} branch")

    config = ProjectConfig(
        repo=repo, protected_paths=list(protected), gates=gates or GateSettings()
    )
    data = config.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    data = {"version": 1, "repo": str(repo), **{k: v for k, v in data.items() if k != "repo"}}
    write_text_atomic(path, yaml.safe_dump(data, sort_keys=False))
    for relative, text in KNOWLEDGE_FILES.items():
        write_text_atomic(path.parent / "knowledge" / relative, text)
    folder = path.parent.relative_to(home.path).as_posix()
    Git(home.path).commit_paths(f"Add project {name}", folder)
    try:
        return load_project(home, name)
    except ProjectConfigError as exc:  # pragma: no cover - written from a validated model
        raise ProjectError(str(exc)) from exc


def list_projects(home: Home) -> list[str]:
    return sorted(p.parent.name for p in home.projects.glob(f"*/{PROJECT_FILE}"))
