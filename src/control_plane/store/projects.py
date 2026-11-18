"""Project files of the home repository: `projects/<name>/project.yaml` (docs/SPEC.md 5, 11).

Relative paths in the file (`repo`, the guardrail gate's `control`) are resolved against the
home repository, so the file means the same thing whichever directory a command runs from.
"""

import re
from pathlib import Path

import yaml
from pydantic import ValidationError

from control_plane.domain.project import ProjectConfig
from control_plane.store.home import Home

PROJECT_FILE = "project.yaml"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class ProjectConfigError(Exception):
    """A project file is missing or malformed. The message names the file and the field."""


def project_file(home: Home, name: str) -> Path:
    if not _NAME.fullmatch(name) or ".." in name:
        raise ProjectConfigError(
            f"project name {name!r} must be a plain folder name: letters, digits, '.', '-', '_'"
        )
    return home.projects / name / PROJECT_FILE


def load_project(home: Home, name: str) -> ProjectConfig:
    path = project_file(home, name)
    if not path.is_file():
        raise ProjectConfigError(f"no {PROJECT_FILE} for project {name!r}: {path}")
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ProjectConfigError(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ProjectConfigError(f"{path}: must be a mapping of settings")
    try:
        project = ProjectConfig.model_validate(data)
    except ValidationError as exc:
        raise ProjectConfigError(f"{path}: {_describe(exc)}") from exc
    return _resolve_paths(project, home.path)


def _describe(exc: ValidationError) -> str:
    problems = []
    for error in exc.errors():
        field = ".".join(str(part) for part in error["loc"]) or "(file)"
        problems.append(f"{field}: {error['msg']}")
    return "; ".join(problems)


def _resolve_paths(project: ProjectConfig, base: Path) -> ProjectConfig:
    project = project.model_copy(deep=True)
    project.repo = _resolve(project.repo, base)
    guardrail = project.gates.guardrail_regression
    if guardrail is not None and guardrail.control is not None:
        guardrail.control = _resolve(guardrail.control, base)
    return project


def _resolve(path: Path, base: Path) -> Path:
    path = path.expanduser()
    return path if path.is_absolute() else (base / path).resolve()
