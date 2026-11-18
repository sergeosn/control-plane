"""Guardrail regression (SPEC.md sections 9 and 16): the reviewer never sees the worker's
transcript.

The worker's reasoning would argue the reviewer into agreement, so it is kept out of the
reviewer's context structurally: no recipe input carries a transcript, no file of the
reviewer's context holds one, and the reviewer's read root cannot reach the step folders.
"""

from pathlib import Path

import pytest

from control_plane.app import context as app_context
from control_plane.context.builder import StepInputs
from control_plane.context.recipes import RECIPES
from control_plane.domain import Plan, WorkReport
from control_plane.policy.jail import PathDeniedError, PathJail
from fixtures.context_home import make_context_home
from fixtures.loop_harness import VALID_PLAN, VALID_WORK_REPORT

MARKER = "WORKER-PRIVATE-REASONING-7f3a"


def test_the_reviewer_context_contains_no_worker_transcript(tmp_path: Path) -> None:
    ctx = make_context_home(tmp_path)
    worker_step = ctx.run.path / "steps" / "02-implement-1"
    worker_step.mkdir(parents=True)
    transcript = worker_step / "transcript.jsonl"
    transcript.write_text(
        '{"role":"assistant","content":[{"type":"text","text":"' + MARKER + '"}]}\n'
    )
    (worker_step / "state.json").write_text('{"note": "' + MARKER + '"}\n')

    prepared = app_context.prepare_step_context(
        ctx.home,
        ctx.run,
        step="04-review-1",
        role="reviewer",
        task_file=ctx.task_file,
        worktree=ctx.worktree.path,
        inputs=StepInputs(
            plan=Plan.model_validate(VALID_PLAN),
            diff=b"diff --git a/app.py b/app.py\n",
            work_report=WorkReport.model_validate(VALID_WORK_REPORT),
        ),
    )

    files = [p for p in prepared.context_dir.rglob("*") if p.is_file()]
    assert files
    for path in files:
        assert "transcript" not in path.name
        assert MARKER not in path.read_text(errors="replace"), path
    assert MARKER not in prepared.brief

    jail = PathJail(ctx.worktree.path, writable=False, read_roots=[prepared.context_dir])
    with pytest.raises(PathDeniedError):
        jail.resolve(str(transcript))


def test_no_recipe_input_can_carry_a_transcript() -> None:
    assert not any("transcript" in name for name in StepInputs.__dataclass_fields__)
    reviewer = RECIPES["reviewer"]
    sources = (*reviewer.always, *reviewer.required, *reviewer.optional)
    assert not any("transcript" in source for source in sources)
