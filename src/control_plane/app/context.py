"""Writing a step's context and the run's manifest (docs/SPEC.md section 9).

`prepare_step_context` builds the files of one step with `control_plane.context`, writes
them into `context/<NN-step-attempt>/` of the run folder, and records each file's hash in
`manifest.json`. The caller (the flow engine) sends `brief` as the step's first message and
gives the executor's path jail `context_dir` as its only read-only extra root, so a step can
read its own context and nothing else of the run folder.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from control_plane.context.builder import StepInputs, build_step_context
from control_plane.context.manifest import (
    ManifestError,
    ManifestMismatch,
    add_context_files,
    sha256_of,
    verify_manifest,
)
from control_plane.domain.manifest import FileHash, Manifest
from control_plane.store.home import Home
from control_plane.store.runs import MANIFEST_FILE, RunFolder

CONTEXT_DIR = "context"
_STEP_FOLDER = re.compile(r"^\d{2,}-[A-Za-z0-9_]+-\d+$")


class ContextExistsError(Exception):
    """The context of a step folder is already written; an attempt gets a new folder."""


@dataclass(frozen=True)
class PreparedContext:
    step: str
    """The step folder name, `NN-<step>-<attempt>`, shared with `steps/`."""
    context_dir: Path
    brief: str
    files: tuple[str, ...]
    """Every file written, relative to `context_dir`."""


def write_manifest(run: RunFolder, manifest: Manifest) -> None:
    """Write the run's first `manifest.json`. A run has one manifest; it is never replaced."""
    if (run.path / MANIFEST_FILE).exists():
        raise ManifestError(f"run {run.run_id} already has a manifest")
    run.write_json(MANIFEST_FILE, manifest)


def read_manifest(run: RunFolder) -> Manifest:
    path = run.path / MANIFEST_FILE
    if not path.is_file():
        raise ManifestError(f"run {run.run_id} has no manifest: {path}")
    return Manifest.from_json(path.read_text())


def prepare_step_context(
    home: Home,
    run: RunFolder,
    *,
    step: str,
    role: str,
    task_file: Path,
    worktree: Path,
    inputs: StepInputs,
) -> PreparedContext:
    """Write the context of step folder `step` and record it in the manifest."""
    if not _STEP_FOLDER.fullmatch(step):
        raise ValueError(f"step folder {step!r} is not of the form NN-<step>-<attempt>")
    manifest = read_manifest(run)
    context_dir = run.path / CONTEXT_DIR / step
    if context_dir.exists():
        raise ContextExistsError(
            f"the context of step {step} of run {run.run_id} is already written; "
            "every attempt gets a new step folder"
        )
    built = build_step_context(
        home=home,
        task_file=task_file,
        role=role,
        worktree=worktree,
        context_dir=context_dir,
        inputs=inputs,
    )
    hashes = []
    for file in built.files:
        relative = f"{CONTEXT_DIR}/{step}/{file.path}"
        run.write_bytes(relative, file.content)
        hashes.append(FileHash(path=relative, sha256=sha256_of(file.content)))
    run.write_json(MANIFEST_FILE, add_context_files(manifest, hashes))
    return PreparedContext(
        step=step,
        context_dir=context_dir,
        brief=built.brief,
        files=tuple(file.path for file in built.files),
    )


def verify_run_manifest(home: Home, run: RunFolder) -> list[ManifestMismatch]:
    """Every file whose hash no longer matches what the run's manifest recorded."""
    return verify_manifest(read_manifest(run), home=home.path, run_folder=run.path)
