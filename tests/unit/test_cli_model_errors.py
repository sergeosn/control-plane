"""`cplane run` when the model endpoint answers with an error (docs/SPEC.md sections 8.2, 14).

The planner is the real `OpenAICompatibleClient`, answered by an `httpx2.MockTransport`, so
each row goes from the HTTP status through the client, the loop and the flow to the
command's output. No row may reach the user as a traceback.
"""

from collections.abc import Callable
from pathlib import Path

import httpx2
import pytest

from control_plane import cli
from control_plane.app import selfcheck
from control_plane.app import tasks as app_tasks
from control_plane.app.clients import ClientFactory
from control_plane.cli import main
from control_plane.domain.plane import ModelProfile
from control_plane.flow import read_flow_state
from control_plane.llm import LLMClient, OpenAICompatibleClient
from control_plane.store.runs import RunStore

Handler = Callable[[httpx2.Request], httpx2.Response]


def answering(status: int, message: str = "boom") -> Handler:
    def handle(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, json={"error": {"message": message}})

    return handle


def unreachable(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("connection refused")


class Planner:
    """A factory whose planner is the real client over `handler`; the others are scripted."""

    def __init__(self, scenario: selfcheck.Scenario, handler: Handler) -> None:
        self.scripted = selfcheck.scripted_clients(scenario.home)
        self.handler = handler
        self.model = ""

    def __call__(self, role: str, profile: ModelProfile) -> LLMClient:
        if role != "planner":
            return self.scripted(role, profile)
        self.model = profile.model
        return OpenAICompatibleClient(
            profile.model,
            base_url="https://gateway.test/v1",
            api_key="token-for-tests",
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler)),
        )


@pytest.fixture
async def scenario(tmp_path: Path) -> selfcheck.Scenario:
    return await selfcheck.prepare(tmp_path)


def use(monkeypatch: pytest.MonkeyPatch, clients: ClientFactory) -> None:
    monkeypatch.setattr(cli, "make_clients", lambda _environ: clients)


def run(
    scenario: selfcheck.Scenario, monkeypatch: pytest.MonkeyPatch, handler: Handler
) -> tuple[int, Planner]:
    planner = Planner(scenario, handler)
    use(monkeypatch, planner)
    return main(["--home", str(scenario.home.path), "run", scenario.task_id]), planner


def only_run(scenario: selfcheck.Scenario) -> str:
    [run_id] = RunStore(scenario.home).list_ids()
    return run_id


def task_status(scenario: selfcheck.Scenario) -> str:
    return app_tasks.find_task(scenario.home, scenario.task_id).task.status


# --- parks -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("handler", "said"),
    [
        *[(answering(status), "server errors") for status in (500, 502, 503, 504, 529)],
        (unreachable, "could not be reached"),
    ],
    ids=["500", "502", "503", "504", "529", "connection"],
)
def test_a_server_or_connection_error_parks_and_prints_one_line_with_the_time(
    scenario: selfcheck.Scenario,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    handler: Handler,
    said: str,
) -> None:
    code, _ = run(scenario, monkeypatch, handler)

    run_id = only_run(scenario)
    [line] = capsys.readouterr().out.splitlines()
    assert code == 0
    assert said in line and f"`cplane resume {run_id}` after 20" in line
    assert read_flow_state(RunStore(scenario.home).open(run_id)).status == "parked"
    assert task_status(scenario) == "parked"


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_token_parks_and_says_to_update_the_token_then_resume(
    scenario: selfcheck.Scenario,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: int,
) -> None:
    code, _ = run(scenario, monkeypatch, answering(status, "invalid token"))

    run_id = only_run(scenario)
    [line] = capsys.readouterr().out.splitlines()
    assert code == 0
    assert "rejected the token" in line and "CPLANE_LLM_AUTH_TOKEN" in line
    assert line.endswith(f"run `cplane resume {run_id}`.")
    assert task_status(scenario) == "parked"

    use(monkeypatch, selfcheck.scripted_clients(scenario.home))
    assert main(["--home", str(scenario.home.path), "resume", run_id]) == 0
    assert "parked at merge_gate" in capsys.readouterr().out


# --- failures ----------------------------------------------------------------------------


def test_a_404_fails_the_run_naming_the_model_and_plane_yaml(
    scenario: selfcheck.Scenario,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, planner = run(scenario, monkeypatch, answering(404, "model not found"))

    captured = capsys.readouterr()
    assert code == 1
    assert "failed" in captured.out and "model_not_found" in captured.out
    assert repr(planner.model) in captured.out and "plane.yaml" in captured.out
    assert "Traceback" not in captured.err
    assert task_status(scenario) == "failed"


@pytest.mark.parametrize("status", [400, 422])
def test_a_rejected_request_fails_the_run_with_the_endpoint_message(
    scenario: selfcheck.Scenario,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: int,
) -> None:
    code, _ = run(scenario, monkeypatch, answering(status, "tools[3].strict is not supported"))

    out = capsys.readouterr().out
    assert code == 1
    assert "bad_request" in out and "tools[3].strict is not supported" in out
    assert task_status(scenario) == "failed"


def test_a_context_that_is_too_long_fails_as_context_window_exceeded(
    scenario: selfcheck.Scenario,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    message = "This model's maximum context length is 128000 tokens."
    code, _ = run(scenario, monkeypatch, answering(400, message))

    out = capsys.readouterr().out
    assert code == 1
    assert "model_context_window_exceeded" in out and "bad_request" not in out


def test_any_other_status_fails_the_run_with_reason_model_error(
    scenario: selfcheck.Scenario,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _ = run(scenario, monkeypatch, answering(418, "teapot"))

    out = capsys.readouterr().out
    assert code == 1
    assert "model_error" in out and "418" in out
