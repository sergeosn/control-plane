"""The context of one agent step: its files, `INDEX.md` and `BRIEF.md` (docs/SPEC.md section 9).

`build_step_context` computes every file from the home repository, the task file, the
worktree and the step-specific inputs, following the role's recipe (`recipes.RECIPES`). It
writes nothing: `control_plane.app.context` writes the files into the run folder and records
their hashes in the manifest.

The files of a step live in `context/<NN-step-attempt>/` of the run folder. The index lists
them by absolute path, because the path jail reads a relative path from the worktree and
the context directory is a separate, read-only root.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path

from control_plane.context.brief import render_brief
from control_plane.context.recipes import RECIPES, Recipe
from control_plane.context.repo_map import build_repo_map
from control_plane.domain.plan import Plan
from control_plane.domain.review import Review
from control_plane.domain.task import Task, TaskFileError
from control_plane.domain.work_report import WorkReport
from control_plane.store.home import Home

BRIEF_FILE = "BRIEF.md"
INDEX_FILE = "INDEX.md"
TASK_FILE = "task.md"
REPO_MAP_FILE = "repo_map.md"
SCOPE_FILE = "scope.md"
KNOWLEDGE_DIR = "knowledge"
ARCHIVE_DIR = "archive"

_GATE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_HEADING = re.compile(r"^#+\s+(.+?)\s*$")


class ContextError(Exception):
    """A step's context cannot be built. The message says why, in one line."""


@dataclass(frozen=True)
class StepInputs:
    """The step-specific inputs of section 9. Which a role may get is set by its recipe."""

    plan: Plan | None = None
    previous_plan: Plan | None = None
    work_report: WorkReport | None = None
    review: Review | None = None
    diff: bytes | None = None
    gate_outputs: Mapping[str, str] = field(default_factory=dict)
    """Gate name to its output, as the last gate run wrote it."""
    denial_reason: str | None = None
    answers: str | None = None
    out_of_plan: tuple[str, ...] = ()
    """The scope check: changed files the plan's `files_to_touch` did not name."""

    def given(self) -> list[str]:
        """The names of the inputs that carry something."""
        return [f.name for f in fields(self) if getattr(self, f.name) not in (None, {}, ())]


@dataclass(frozen=True)
class ContextFile:
    path: str
    """Relative to the step's context directory."""
    description: str
    content: bytes

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    @property
    def lines(self) -> int:
        return len(self.text.splitlines())


@dataclass(frozen=True)
class BuiltContext:
    brief: str
    files: tuple[ContextFile, ...]
    """Every file of the step's context directory, `INDEX.md` and `BRIEF.md` last."""

    def file(self, path: str) -> ContextFile:
        for file in self.files:
            if file.path == path:
                return file
        raise KeyError(path)


def build_step_context(
    *,
    home: Home,
    task_file: Path,
    role: str,
    worktree: Path,
    context_dir: Path,
    inputs: StepInputs,
) -> BuiltContext:
    """The files of one step's context, following the recipe of `role`."""
    recipe = _recipe(role, inputs)
    try:
        task_text = task_file.read_text()
        task = Task.from_markdown(task_text)
    except (OSError, TaskFileError) as exc:
        raise ContextError(f"task file {task_file}: {exc}") from exc
    lane = home.lanes / task.lane

    files = [
        _text(TASK_FILE, f"the task file of {task.id}: goal and acceptance criteria", task_text)
    ]
    if "knowledge" in recipe.always:
        files += _knowledge(home.projects / task.project / KNOWLEDGE_DIR)
    if "procedure" in recipe.always:
        files.append(_lane_file(lane, "procedure.md", "the lane procedure: how work is done"))
    if "rubric" in recipe.always:
        files.append(_lane_file(lane, "rubric.md", "the rubric: the rows a review grades by id"))
    files.append(
        _text(
            REPO_MAP_FILE,
            "the repository map: tracked files, line counts, top-level Python symbols",
            build_repo_map(worktree),
        )
    )
    step_files = _step_files(role, inputs)
    files += step_files

    index = _text(INDEX_FILE, "the index of this step's files", render_index(files, context_dir))
    brief = render_brief(
        role, task, str(context_dir / INDEX_FILE), extras=[f.path for f in step_files]
    )
    files += [index, _text(BRIEF_FILE, "the brief, sent as the first message", brief)]
    return BuiltContext(brief=brief, files=tuple(files))


def render_index(files: list[ContextFile], context_dir: Path) -> str:
    lines = [
        "# Index",
        "",
        "Every file provided for this step, one per line: its path, its length in lines and "
        "what it contains. Read a file with `read_file` and the path exactly as listed. The "
        f"files are in the context directory `{context_dir}`. Repository files are not "
        f"listed here; `{REPO_MAP_FILE}` maps them.",
        "",
    ]
    for file in files:
        count = file.lines
        unit = "line" if count == 1 else "lines"
        lines.append(f"- `{context_dir / file.path}` ({count} {unit}): {file.description}")
    return "\n".join(lines) + "\n"


def render_scope(paths: tuple[str, ...]) -> str:
    lines = [
        "# Out-of-plan files",
        "",
        "The diff changes these files, which the plan's `files_to_touch` does not name. The "
        "scope check does not fail the run; judge whether each change is justified by the "
        "task, the plan or the work report's deviations.",
        "",
    ]
    lines += [f"- `{path}`" for path in paths]
    return "\n".join(lines) + "\n"


def _recipe(role: str, inputs: StepInputs) -> Recipe:
    try:
        recipe = RECIPES[role]
    except KeyError:
        raise ContextError(f"unknown role {role!r}; roles: {', '.join(RECIPES)}") from None
    given = inputs.given()
    allowed = (*recipe.required, *recipe.optional)
    refused = [name for name in given if name not in allowed]
    if refused:
        raise ContextError(
            f"{role} is not given {', '.join(refused)}; its recipe allows: "
            f"{', '.join(allowed) or 'nothing step-specific'}"
        )
    missing = [name for name in recipe.required if name not in given]
    if missing:
        raise ContextError(f"{role} needs {', '.join(missing)}")
    return recipe


def _text(path: str, description: str, text: str) -> ContextFile:
    return ContextFile(path, description, text.encode("utf-8"))


def _knowledge(root: Path) -> list[ContextFile]:
    if not root.is_dir():
        return []
    files = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file() or relative.parts[0] == ARCHIVE_DIR:
            continue
        if any(part.startswith(".") for part in relative.parts):
            continue
        text = path.read_text(errors="replace")
        title = _title(text) or relative.as_posix()
        files.append(
            _text(f"{KNOWLEDGE_DIR}/{relative.as_posix()}", f"project knowledge: {title}", text)
        )
    return files


def _title(text: str) -> str | None:
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match:
            return match.group(1)
        if line.strip():
            return None
    return None


def _lane_file(lane: Path, name: str, description: str) -> ContextFile:
    path = lane / name
    if not path.is_file():
        raise ContextError(f"lane {lane.name} has no {name}: {path}")
    return _text(name, description, path.read_text())


def _step_files(role: str, inputs: StepInputs) -> list[ContextFile]:
    files: list[ContextFile] = []
    if inputs.previous_plan is not None:
        files.append(
            _text(
                "previous_plan.json", "the previous plan, sent back", inputs.previous_plan.to_json()
            )
        )
    if inputs.answers is not None:
        files.append(
            _text(
                "answers.md",
                "the human's answers to the plan's questions",
                f"# Answers\n\n{inputs.answers}\n",
            )
        )
    if inputs.denial_reason is not None:
        files.append(
            _text(
                "denial.md",
                "why a human denied the last result",
                f"# Denial reason\n\n{inputs.denial_reason}\n",
            )
        )
    if inputs.plan is not None:
        files.append(_text("plan.json", "the approved plan", inputs.plan.to_json()))
    if inputs.diff is not None:
        files.append(
            ContextFile(
                "diff.patch",
                "the change under review: the worktree against the base commit",
                inputs.diff,
            )
        )
    for name, output in sorted(inputs.gate_outputs.items()):
        if not _GATE_NAME.fullmatch(name):
            raise ContextError(f"gate name {name!r} cannot name a file")
        files.append(_text(f"gates/{name}.txt", f"output of the {name} gate", output))
    if inputs.out_of_plan:
        files.append(
            _text(
                SCOPE_FILE,
                "the scope check: changed files the plan did not list",
                render_scope(inputs.out_of_plan),
            )
        )
    if inputs.review is not None:
        files.append(
            _text(
                "review.json",
                "the previous review: the findings to address",
                inputs.review.to_json(),
            )
        )
    if inputs.work_report is not None:
        description = (
            "the worker's report: unverified claims, to check against diff.patch and the gates"
            if role == "reviewer"
            else "your previous work report"
        )
        files.append(_text("work_report.json", description, inputs.work_report.to_json()))
    return files
