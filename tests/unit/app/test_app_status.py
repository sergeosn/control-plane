"""Task 15: `cplane status` and `cplane inspect --transcript` (docs/SPEC.md sections 9 and 14)."""

from pathlib import Path

import pytest

from control_plane.app import inspect as app_inspect
from control_plane.app import runs as app_runs
from control_plane.app import selfcheck
from control_plane.app import status as app_status
from control_plane.app.inspect import InspectError
from control_plane.app.status import StatusError
from control_plane.domain import LLMCall
from control_plane.store.files import read_jsonl
from control_plane.store.runs import RunStore


@pytest.fixture
async def parked(tmp_path: Path) -> tuple[selfcheck.Scenario, str]:
    scenario = await selfcheck.prepare(tmp_path)
    clients = selfcheck.scripted_clients(scenario.home)
    outcome = await app_runs.run_task(scenario.home, scenario.task_id, clients=clients)
    return scenario, outcome.run_id


def test_status_shows_state_step_iteration_and_cost(parked: tuple[selfcheck.Scenario, str]) -> None:
    scenario, run_id = parked

    [status] = app_status.run_statuses(scenario.home)

    run = RunStore(scenario.home).open(run_id)
    cost = sum(
        call.cost_usd
        for step in (run.path / "steps").iterdir()
        for call in read_jsonl(step / "llm_calls.jsonl", LLMCall)
    )
    assert status.run_id == run_id and status.task_id == scenario.task_id
    assert (status.state, status.step) == ("parked", "merge_gate")
    assert (status.iteration, status.max_iterations) == (2, 3)
    assert status.cost_usd == pytest.approx(cost) and cost > 0
    assert status.approval_id is not None
    assert app_status.run_statuses(scenario.home, run_id) == [status]


def test_status_of_an_unknown_run_is_an_error(parked: tuple[selfcheck.Scenario, str]) -> None:
    scenario, _ = parked

    with pytest.raises(StatusError, match="no run R-0404"):
        app_status.run_statuses(scenario.home, "R-0404")


def test_status_of_a_run_that_never_started(tmp_path: Path) -> None:
    from control_plane.store.home import create_home

    home = create_home(tmp_path / "home")
    RunStore(home).create("R-0001")

    [status] = app_status.run_statuses(home)

    assert (status.state, status.step, status.cost_usd) == ("created", None, 0.0)


def test_transcripts_of_every_step_or_one(parked: tuple[selfcheck.Scenario, str]) -> None:
    scenario, run_id = parked

    every = app_inspect.transcripts(scenario.home, run_id)
    review = app_inspect.transcripts(scenario.home, run_id, "review")

    assert [t.step for t in every] == [
        "01-plan-1",
        "02-implement-1",
        "04-implement-2",
        "06-review-1",
    ]
    [only] = review
    assert only.step == "06-review-1"
    assert only.messages[0].role == "user"
    names = [b.name for m in only.messages for b in m.content if b.type == "tool_use"]
    assert names == ["submit_review"]
    with pytest.raises(InspectError, match="no step 'deploy'"):
        app_inspect.transcripts(scenario.home, run_id, "deploy")
