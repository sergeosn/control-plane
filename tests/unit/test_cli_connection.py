"""`cplane run` when the model endpoint cannot be reached (docs/SPEC.md sections 8.2, 12, 14).

The planner's client raises `EndpointUnreachableError`, as the real client does on
`openai.APIConnectionError` or `APITimeoutError`. The run parks with reason `connection`
instead of crashing, the command prints one line saying how to resume, and `cplane resume`
continues the run once the endpoint answers again.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from control_plane import cli
from control_plane.app import selfcheck
from control_plane.app import status as app_status
from control_plane.app import tasks as app_tasks
from control_plane.app.clients import ClientFactory
from control_plane.cli import main
from control_plane.domain.plane import ModelProfile
from control_plane.flow import read_flow_state
from control_plane.llm import (
    EndpointUnreachableError,
    LLMClient,
    Message,
    Response,
    ToolDefinition,
)
from control_plane.store.runs import RunStore


class UnreachableOnce:
    """Wraps a role's client: the first call raises, every later one is passed through."""

    def __init__(self, inner: LLMClient, resume_after: datetime) -> None:
        self.inner = inner
        self.resume_after = resume_after
        self.raised = False

    async def complete(
        self,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        *,
        max_tokens: int | None = None,
    ) -> Response:
        if not self.raised:
            self.raised = True
            raise EndpointUnreachableError(
                self.resume_after, "the endpoint https://gateway.test/v1 could not be reached"
            )
        return await self.inner.complete(system, messages, tools, max_tokens=max_tokens)


def unreachable_planner(scripted: ClientFactory, resume_after: datetime) -> ClientFactory:
    wrapped: dict[str, UnreachableOnce] = {}

    def build(role: str, profile: ModelProfile) -> LLMClient:
        if role != "planner":
            return scripted(role, profile)
        if role not in wrapped:
            wrapped[role] = UnreachableOnce(scripted(role, profile), resume_after)
        return wrapped[role]

    return build


@pytest.fixture
async def scenario(tmp_path: Path) -> selfcheck.Scenario:
    return await selfcheck.prepare(tmp_path)


def start(
    scenario: selfcheck.Scenario, monkeypatch: pytest.MonkeyPatch, resume_after: datetime
) -> int:
    clients = unreachable_planner(selfcheck.scripted_clients(scenario.home), resume_after)
    monkeypatch.setattr(cli, "make_clients", lambda _environ: clients)
    return main(["--home", str(scenario.home.path), "run", scenario.task_id])


def test_an_unreachable_endpoint_parks_the_run_and_prints_one_line(
    scenario: selfcheck.Scenario,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    resume_after = datetime.now(UTC) + timedelta(minutes=1)

    code = start(scenario, monkeypatch, resume_after)

    captured = capsys.readouterr()
    assert code == 0
    [run_id] = RunStore(scenario.home).list_ids()
    [line] = captured.out.splitlines()
    assert "could not be reached" in line
    assert f"cplane resume {run_id}" in line
    assert resume_after.isoformat() in line
    assert "Traceback" not in captured.err
    run = RunStore(scenario.home).open(run_id)
    assert read_flow_state(run).status == "parked"
    assert app_tasks.find_task(scenario.home, scenario.task_id).task.status == "parked"
    [status] = app_status.run_statuses(scenario.home, run_id)
    assert status.state == "parked"


def test_resume_continues_the_run_once_the_endpoint_answers(
    scenario: selfcheck.Scenario,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    start(scenario, monkeypatch, datetime.now(UTC) - timedelta(seconds=1))
    [run_id] = RunStore(scenario.home).list_ids()
    capsys.readouterr()

    code = main(["--home", str(scenario.home.path), "resume", run_id])

    out = capsys.readouterr().out
    assert code == 0
    assert f"Run {run_id} of {scenario.task_id}: parked at merge_gate" in out
    assert "could not be reached" not in out


def test_resume_before_the_time_stays_parked_and_says_so_again(
    scenario: selfcheck.Scenario,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    start(scenario, monkeypatch, datetime.now(UTC) + timedelta(hours=1))
    [run_id] = RunStore(scenario.home).list_ids()
    capsys.readouterr()

    code = main(["--home", str(scenario.home.path), "resume", run_id])

    [line] = capsys.readouterr().out.splitlines()
    assert code == 0
    assert "could not be reached" in line and f"cplane resume {run_id}" in line
