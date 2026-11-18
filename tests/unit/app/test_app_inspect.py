"""Task 12: `cplane inspect <run> --context` on a scripted run (docs/SPEC.md section 9).

Per step: the files provided, the files actually read, and tokens by source.
"""

from pathlib import Path

import pytest

from control_plane.agent import ApiLoopExecutor, StepBudget, StepContext
from control_plane.app import context as app_context
from control_plane.app import inspect as app_inspect
from control_plane.app.inspect import InspectError
from control_plane.cli import main
from control_plane.context.builder import StepInputs
from control_plane.llm import PriceTable, ScriptedClient
from control_plane.policy import PathJail, Policy, load_rules
from control_plane.policy.roles import READ_TOOLS
from control_plane.store.home import TEMPLATE_DIR
from control_plane.tools import ToolExecutor
from fixtures.context_home import ContextHome, make_context_home
from fixtures.loop_harness import MODEL, submit_turn, tool_use, turn


@pytest.fixture
async def scripted(tmp_path: Path) -> ContextHome:
    """A planner step that reads the index, one knowledge file, one worktree file and a
    context file that does not exist, then submits."""
    ctx = make_context_home(tmp_path)
    prepared = app_context.prepare_step_context(
        ctx.home,
        ctx.run,
        step="01-plan-1",
        role="planner",
        task_file=ctx.task_file,
        worktree=ctx.worktree.path,
        inputs=StepInputs(),
    )
    context_dir = prepared.context_dir
    responses = [
        turn(tool_use("t1", "read_file", path=str(context_dir / "INDEX.md"))),
        turn(
            tool_use("t2", "read_file", path=str(context_dir / "knowledge" / "facts.md")),
            tool_use("t3", "read_file", path="app.py"),
        ),
        turn(tool_use("t4", "read_file", path=str(context_dir / "nope.md"))),
        turn(tool_use("t5", "search", pattern="value")),
        submit_turn("planner"),
    ]
    jail = PathJail(ctx.worktree.path, writable=False, read_roots=[context_dir])
    executor = ApiLoopExecutor(
        client=ScriptedClient(responses),
        policy=Policy(load_rules(TEMPLATE_DIR / "rules" / "tools.yaml")),
        tools=ToolExecutor(jail, tools=READ_TOOLS),
        prices=PriceTable.from_mapping(
            {MODEL: {"input": 1.0, "output": 1.0, "cache_write": 1.0, "cache_read": 1.0}}
        ),
    )
    result = await executor.run_step(
        StepContext(
            run=ctx.run,
            seq=1,
            step="plan",
            attempt=1,
            role="planner",
            system="You are the planner.",
            brief=prepared.brief,
            budget=StepBudget(
                max_turns=40, max_total_tokens=400_000, max_cost_usd=2.0, max_wall_seconds=900
            ),
        )
    )
    assert result.outcome == "ok"
    return ctx


async def test_inspect_shows_provided_files_against_files_read(scripted: ContextHome) -> None:
    [step] = app_inspect.inspect_context(scripted.home, "R-0001")

    assert step.step == "01-plan-1"
    marks = {f.path: f.mark for f in step.provided}
    assert marks["BRIEF.md"] == "prompt"
    assert marks["INDEX.md"] == "read"
    assert marks["knowledge/facts.md"] == "read"
    assert marks["procedure.md"] == "-"
    assert marks["repo_map.md"] == "-"
    assert step.read_count == 2
    assert step.read_elsewhere == ["app.py"]
    assert all(f.lines > 0 for f in step.provided)


async def test_inspect_estimates_tokens_by_source(scripted: ContextHome) -> None:
    [step] = app_inspect.inspect_context(scripted.home, "R-0001")

    assert set(step.tokens_by_source) >= {"brief", "context", "worktree", "search", "submit"}
    assert all(tokens > 0 for tokens in step.tokens_by_source.values())
    assert step.measured_input_tokens == 50


async def test_the_step_filter_takes_a_folder_or_a_step_name(scripted: ContextHome) -> None:
    assert [s.step for s in app_inspect.inspect_context(scripted.home, "R-0001", "plan")] == [
        "01-plan-1"
    ]
    assert app_inspect.inspect_context(scripted.home, "R-0001", "01-plan-1")
    with pytest.raises(InspectError, match="no step 'review'"):
        app_inspect.inspect_context(scripted.home, "R-0001", "review")


def test_an_unknown_run_is_an_error(tmp_path: Path) -> None:
    ctx = make_context_home(tmp_path)

    with pytest.raises(InspectError, match="no run R-9999"):
        app_inspect.inspect_context(ctx.home, "R-9999")


def test_a_step_without_a_transcript_shows_nothing_read(tmp_path: Path) -> None:
    ctx = make_context_home(tmp_path)
    app_context.prepare_step_context(
        ctx.home,
        ctx.run,
        step="01-plan-1",
        role="planner",
        task_file=ctx.task_file,
        worktree=ctx.worktree.path,
        inputs=StepInputs(),
    )

    [step] = app_inspect.inspect_context(ctx.home, "R-0001")

    assert step.read_count == 0
    assert step.tokens_by_source == {}
    assert step.measured_input_tokens is None


async def test_cplane_inspect_context_prints_the_report(
    scripted: ContextHome, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--home", str(scripted.home.path), "inspect", "R-0001", "--context"])

    out, err = capsys.readouterr()
    assert code == 0, err
    lines = out.splitlines()
    assert "Run R-0001" in lines[0]
    assert any(line.startswith("Step 01-plan-1:") and "2 read" in line for line in lines)
    assert any(line.split() == ["read", "knowledge/facts.md", "(4", "lines)"] for line in lines)
    assert any(line.split()[:2] == ["-", "procedure.md"] for line in lines)
    assert any(line.split()[:2] == ["prompt", "BRIEF.md"] for line in lines)
    assert "  read outside the context: app.py" in lines
    assert any(line.startswith("  tokens by source (estimated):") for line in lines)
    assert "  measured input tokens: 50" in lines


async def test_cplane_inspect_without_context_lists_the_steps(
    scripted: ContextHome, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--home", str(scripted.home.path), "inspect", "R-0001"])

    out, _ = capsys.readouterr()
    assert code == 0
    assert "01-plan-1" in out
    assert "--context" in out


def test_cplane_inspect_of_an_unknown_run_fails_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = make_context_home(tmp_path)

    code = main(["--home", str(ctx.home.path), "inspect", "R-9999", "--context"])

    _, err = capsys.readouterr()
    assert code == 1
    assert "cplane: no run R-9999" in err
