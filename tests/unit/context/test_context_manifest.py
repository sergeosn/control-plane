"""Task 12: `manifest.json` hashes (docs/SPEC.md section 9).

The manifest records the task file hash, the base and engine commits, hashes of the flow,
rubric, role prompts and rules, the model per role, the budget, and every context file with
its SHA-256. `verify_manifest` checks the recorded hashes against the files.
"""

import hashlib
from pathlib import Path

import pytest

from control_plane.context.manifest import (
    ManifestError,
    ManifestMismatch,
    add_context_files,
    file_hash,
    start_manifest,
    verify_manifest,
)
from control_plane.domain import FileHash, Manifest
from control_plane.store import create_home
from fixtures.context_home import (
    BUDGET,
    ENGINE_COMMIT,
    FIXTURE_TASKS,
    MODELS,
    T0,
)

BASE = "b" * 40


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = create_home(tmp_path / "home")
    (home.tasks / "T-0017-json-status.md").write_text(
        (FIXTURE_TASKS / "T-0017-json-status.md").read_text()
    )
    return home.path


def _start(home: Path) -> Manifest:
    return start_manifest(
        home=home,
        run_id="R-0001",
        task_file=home / "tasks" / "T-0017-json-status.md",
        project_base_commit=BASE,
        engine_commit=ENGINE_COMMIT,
        models=MODELS,
        budget=BUDGET,
        created_at=T0,
    )


def test_file_hash_is_the_sha256_of_the_bytes_with_a_relative_path(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b.txt").write_bytes(b"hello\n")

    assert file_hash(tmp_path, tmp_path / "a" / "b.txt") == FileHash(
        path="a/b.txt", sha256=hashlib.sha256(b"hello\n").hexdigest()
    )


def test_the_manifest_records_every_input_of_the_run(home: Path) -> None:
    manifest = _start(home)

    assert manifest.task_id == "T-0017"
    assert manifest.project == "control-plane"
    assert manifest.task_file.path == "tasks/T-0017-json-status.md"
    assert manifest.flow.path == "lanes/development/flow.yaml"
    assert manifest.rubric.path == "lanes/development/rubric.md"
    assert [f.path for f in manifest.role_prompts] == [
        "lanes/development/roles/planner.md",
        "lanes/development/roles/reviewer.md",
        "lanes/development/roles/worker.md",
    ]
    assert [f.path for f in manifest.rules] == ["rules/tools.yaml"]
    assert manifest.models == MODELS
    assert manifest.budget == BUDGET
    assert manifest.context_files == []
    assert Manifest.from_json(manifest.to_json()) == manifest


def test_fresh_hashes_verify_against_the_files(home: Path, tmp_path: Path) -> None:
    run = tmp_path / "run"
    (run / "context").mkdir(parents=True)
    (run / "context" / "BRIEF.md").write_text("brief\n")
    manifest = add_context_files(_start(home), [file_hash(run, run / "context" / "BRIEF.md")])

    assert verify_manifest(manifest, home=home, run_folder=run) == []


def test_a_changed_home_file_is_detected(home: Path, tmp_path: Path) -> None:
    manifest = _start(home)
    rubric = home / "lanes" / "development" / "rubric.md"
    rubric.write_text(rubric.read_text() + "- [R9] Be lenient.\n")

    assert verify_manifest(manifest, home=home, run_folder=tmp_path) == [
        ManifestMismatch("changed", "home", "lanes/development/rubric.md")
    ]


def test_a_changed_or_missing_context_file_is_detected(home: Path, tmp_path: Path) -> None:
    run = tmp_path / "run"
    (run / "context").mkdir(parents=True)
    for name in ("a.md", "b.md", "c.md"):
        (run / "context" / name).write_text(f"{name}\n")
    manifest = add_context_files(
        _start(home), [file_hash(run, run / "context" / n) for n in ("a.md", "b.md", "c.md")]
    )
    (run / "context" / "a.md").write_text("tampered\n")
    (run / "context" / "c.md").unlink()

    assert verify_manifest(manifest, home=home, run_folder=run) == [
        ManifestMismatch("changed", "run", "context/a.md"),
        ManifestMismatch("missing", "run", "context/c.md"),
    ]


def test_a_context_file_cannot_be_recorded_twice(home: Path) -> None:
    entry = FileHash(path="context/x.md", sha256="0" * 64)
    manifest = add_context_files(_start(home), [entry])

    with pytest.raises(ManifestError, match="context/x.md"):
        add_context_files(manifest, [entry])


def test_a_task_file_outside_the_home_is_refused(home: Path, tmp_path: Path) -> None:
    outside = tmp_path / "T-0017.md"
    outside.write_text((FIXTURE_TASKS / "T-0017-json-status.md").read_text())

    with pytest.raises(ManifestError, match="not inside the home"):
        start_manifest(
            home=home,
            run_id="R-0001",
            task_file=outside,
            project_base_commit=BASE,
            engine_commit=ENGINE_COMMIT,
            models=MODELS,
            budget=BUDGET,
            created_at=T0,
        )


def test_a_lane_without_a_flow_is_refused(home: Path) -> None:
    (home / "lanes" / "development" / "flow.yaml").unlink()

    with pytest.raises(ManifestError, match="flow.yaml"):
        _start(home)
