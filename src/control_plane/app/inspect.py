"""`cplane inspect <run> --context` (docs/SPEC.md section 9): the feedback loop for context work.

Per step it shows the files provided (from the manifest), which of them the model actually
read (successful `read_file` calls in `tool_calls.jsonl`), what it read outside its context,
and the tokens fed to the model by source. Tokens by source are estimated from the
transcript at `CHARS_PER_TOKEN` characters per token; the measured input total comes from
the step's `usage.json`. This module only reads.
"""

from dataclasses import dataclass
from pathlib import Path

from control_plane.agent.loop import TRANSCRIPT_FILE, USAGE_FILE
from control_plane.agent.state import StepUsage
from control_plane.agent.transcript import load_transcript
from control_plane.app.context import CONTEXT_DIR, read_manifest
from control_plane.context.brief import estimate_tokens
from control_plane.context.builder import BRIEF_FILE
from control_plane.context.manifest import ManifestError
from control_plane.domain.calls import ToolCall
from control_plane.llm.types import TextBlock, ToolResultBlock
from control_plane.store.files import read_jsonl
from control_plane.store.home import Home
from control_plane.store.runs import STEPS_DIR, TOOL_CALLS_FILE, RunFolder, RunStore

TOOL_SOURCES = {
    "list_dir": "search",
    "search": "search",
    "run_command": "commands",
    "write_file": "edits",
    "edit_file": "edits",
    "delete_file": "edits",
}
"""Where a tool's result is counted; `read_file` splits into `context` and `worktree`, the
submit tools count as `submit`, and a result with no logged call as `other`."""


class InspectError(Exception):
    """The run or step asked for cannot be inspected. The message says why."""


@dataclass(frozen=True)
class ProvidedFile:
    path: str
    """Relative to the step's context directory."""
    lines: int
    mark: str
    """`read` when the model read it, `prompt` for the brief, `-` when never read."""


@dataclass(frozen=True)
class StepContextReport:
    step: str
    provided: list[ProvidedFile]
    read_elsewhere: list[str]
    """Files read outside the step's context, as the model named them."""
    tokens_by_source: dict[str, int]
    measured_input_tokens: int | None
    """Input tokens the API reported for the step, cache reads and writes included."""

    @property
    def read_count(self) -> int:
        return sum(1 for file in self.provided if file.mark == "read")


def _open(home: Home, run_id: str) -> RunFolder:
    try:
        return RunStore(home).open(run_id)
    except FileNotFoundError:
        raise InspectError(f"no run {run_id} in {home.path}") from None


def list_steps(home: Home, run_id: str) -> list[str]:
    """The step folders of a run, `NN-<step>-<attempt>`, with a context or a step folder."""
    run = _open(home, run_id)
    names: set[str] = set()
    for parent in (run.path / CONTEXT_DIR, run.path / STEPS_DIR):
        if parent.is_dir():
            names.update(entry.name for entry in parent.iterdir() if entry.is_dir())
    return sorted(names)


def _step_name(folder: str) -> str:
    return folder.split("-", 1)[-1].rsplit("-", 1)[0]


def inspect_context(home: Home, run_id: str, step: str | None = None) -> list[StepContextReport]:
    """The context report of every step of `run_id`, or of the steps `step` names: a step
    folder (`01-plan-1`) or a step name (`plan`, every attempt)."""
    run = _open(home, run_id)
    folders = list_steps(home, run_id)
    if step is not None:
        chosen = [f for f in folders if step in (f, _step_name(f))]
        if not chosen:
            raise InspectError(
                f"no step {step!r} in run {run_id}; steps: {', '.join(folders) or 'none'}"
            )
        folders = chosen
    try:
        manifest = read_manifest(run)
    except ManifestError as exc:
        raise InspectError(str(exc)) from exc
    provided = [entry.path for entry in manifest.context_files]
    return [_report(run, folder, provided) for folder in folders]


def _report(run: RunFolder, folder: str, recorded: list[str]) -> StepContextReport:
    context_dir = run.path / CONTEXT_DIR / folder
    step_dir = run.path / STEPS_DIR / folder
    prefix = f"{CONTEXT_DIR}/{folder}/"
    calls = {call.tool_use_id: call for call in read_jsonl(step_dir / TOOL_CALLS_FILE, ToolCall)}

    read: set[str] = set()
    elsewhere: list[str] = []
    for call in calls.values():
        if call.tool != "read_file" or call.decision != "allow" or call.error is not None:
            continue
        path = str(call.arguments.get("path", ""))
        inside = _inside(context_dir, path)
        if inside is not None:
            read.add(inside)
        elif path not in elsewhere:
            elsewhere.append(path)

    files = []
    for entry in recorded:
        if not entry.startswith(prefix):
            continue
        relative = entry.removeprefix(prefix)
        path = context_dir / relative
        lines = len(path.read_text(errors="replace").splitlines()) if path.is_file() else 0
        mark = "prompt" if relative == BRIEF_FILE else "read" if relative in read else "-"
        files.append(ProvidedFile(relative, lines, mark))

    return StepContextReport(
        step=folder,
        provided=files,
        read_elsewhere=elsewhere,
        tokens_by_source=_tokens_by_source(step_dir, context_dir, calls),
        measured_input_tokens=_measured(step_dir),
    )


def _inside(context_dir: Path, raw: str) -> str | None:
    """`raw` relative to the context directory when it names a file in it, else None."""
    if not raw or not Path(raw).is_absolute():
        return None
    resolved = Path(raw).resolve()
    base = context_dir.resolve()
    return resolved.relative_to(base).as_posix() if resolved.is_relative_to(base) else None


def _tokens_by_source(
    step_dir: Path, context_dir: Path, calls: dict[str, ToolCall]
) -> dict[str, int]:
    totals: dict[str, int] = {}
    first_text = True
    for message in load_transcript(step_dir / TRANSCRIPT_FILE):
        if message.role != "user":
            continue
        for block in message.content:
            if isinstance(block, TextBlock):
                source = "brief" if first_text else "engine"
                first_text = False
                text = block.text
            elif isinstance(block, ToolResultBlock):
                source = _result_source(calls.get(block.tool_use_id), context_dir)
                text = block.content
            else:  # pragma: no cover - a user message holds no tool_use blocks
                continue
            totals[source] = totals.get(source, 0) + estimate_tokens(text)
    return {source: tokens for source, tokens in totals.items() if tokens > 0}


def _result_source(call: ToolCall | None, context_dir: Path) -> str:
    if call is None:
        return "other"
    if call.tool == "read_file":
        inside = _inside(context_dir, str(call.arguments.get("path", "")))
        return "context" if inside is not None else "worktree"
    return TOOL_SOURCES.get(call.tool, "submit")


def _measured(step_dir: Path) -> int | None:
    path = step_dir / USAGE_FILE
    if not path.is_file():
        return None
    usage = StepUsage.from_json(path.read_text())
    return usage.input_tokens + usage.cache_read_input_tokens + usage.cache_creation_input_tokens
