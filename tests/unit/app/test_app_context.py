"""Task 12: the app service that writes a step's context and records it in the manifest."""

from pathlib import Path

import pytest

from control_plane.app import context as app_context
from control_plane.app.context import ContextExistsError
from control_plane.context.builder import BRIEF_FILE, INDEX_FILE, StepInputs
from control_plane.context.manifest import ManifestError, ManifestMismatch
from fixtures.context_home import ContextHome, make_context_home


@pytest.fixture
def ctx(tmp_path: Path) -> ContextHome:
    return make_context_home(tmp_path)


def _prepare(ctx: ContextHome, step: str = "01-plan-1") -> app_context.PreparedContext:
    return app_context.prepare_step_context(
        ctx.home,
        ctx.run,
        step=step,
        role="planner",
        task_file=ctx.task_file,
        worktree=ctx.worktree.path,
        inputs=StepInputs(),
    )


def test_the_step_context_is_written_under_the_run_context_folder(ctx: ContextHome) -> None:
    prepared = _prepare(ctx)

    assert prepared.context_dir == ctx.run.path / "context" / "01-plan-1"
    assert (prepared.context_dir / BRIEF_FILE).read_text() == prepared.brief
    assert (prepared.context_dir / INDEX_FILE).is_file()
    assert (prepared.context_dir / "knowledge" / "facts.md").is_file()
    assert set(prepared.files) == {
        str(p.relative_to(prepared.context_dir))
        for p in prepared.context_dir.rglob("*")
        if p.is_file()
    }


def test_every_written_file_is_recorded_in_the_manifest_and_verifies(ctx: ContextHome) -> None:
    prepared = _prepare(ctx)

    manifest = app_context.read_manifest(ctx.run)

    assert {f.path for f in manifest.context_files} == {
        f"context/01-plan-1/{path}" for path in prepared.files
    }
    assert app_context.verify_run_manifest(ctx.home, ctx.run) == []


def test_a_changed_context_file_is_detected(ctx: ContextHome) -> None:
    prepared = _prepare(ctx)
    (prepared.context_dir / "procedure.md").write_text("Skip the tests.\n")

    assert app_context.verify_run_manifest(ctx.home, ctx.run) == [
        ManifestMismatch("changed", "run", "context/01-plan-1/procedure.md")
    ]


def test_two_steps_add_to_the_same_manifest(ctx: ContextHome) -> None:
    _prepare(ctx, "01-plan-1")
    _prepare(ctx, "01-plan-2")

    paths = [f.path for f in app_context.read_manifest(ctx.run).context_files]
    assert any(p.startswith("context/01-plan-1/") for p in paths)
    assert any(p.startswith("context/01-plan-2/") for p in paths)
    assert app_context.verify_run_manifest(ctx.home, ctx.run) == []


def test_a_step_context_is_written_once(ctx: ContextHome) -> None:
    _prepare(ctx)

    with pytest.raises(ContextExistsError, match="01-plan-1"):
        _prepare(ctx)


def test_a_bad_step_folder_name_is_refused(ctx: ContextHome) -> None:
    with pytest.raises(ValueError, match="step folder"):
        _prepare(ctx, "../steps")


def test_the_manifest_is_written_once(ctx: ContextHome) -> None:
    with pytest.raises(ManifestError, match="already"):
        app_context.write_manifest(ctx.run, app_context.read_manifest(ctx.run))


def test_preparing_without_a_manifest_is_an_error(ctx: ContextHome) -> None:
    (ctx.run.path / "manifest.json").unlink()

    with pytest.raises(ManifestError, match="no manifest"):
        _prepare(ctx)
