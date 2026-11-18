"""Task 15: the run service behind `cplane run` and `cplane resume` (docs/SPEC.md sections 7, 13
and 14).

The service wires the flow engine to the real context builder, gates, executor and merge, on
the selfcheck's fixture project with scripted models.
"""

import json
from pathlib import Path

import pytest
import yaml

from control_plane.app import approvals as app_approvals
from control_plane.app import runs as app_runs
from control_plane.app import selfcheck
from control_plane.app import tasks as app_tasks
from control_plane.app.clients import ScriptedClients, environment_clients
from control_plane.app.runs import RunError
from control_plane.domain.project import CommandGate
from control_plane.flow import read_flow_state
from control_plane.llm import CredentialsError, OpenAICompatibleClient
from control_plane.store.git import Git
from control_plane.store.index import Index
from control_plane.store.plane import ModelProfile
from control_plane.store.runs import RunStore
from control_plane.workspace import branch_name, project_git


@pytest.fixture
async def scenario(tmp_path: Path) -> selfcheck.Scenario:
    return await selfcheck.prepare(tmp_path)


async def run_to_merge_gate(
    scenario: selfcheck.Scenario,
) -> tuple[app_runs.RunOutcome, ScriptedClients]:
    clients = selfcheck.scripted_clients(scenario.home)
    outcome = await app_runs.run_task(scenario.home, scenario.task_id, clients=clients)
    return outcome, clients


async def test_a_run_goes_through_a_failed_gate_and_parks_at_the_merge_approval(
    scenario: selfcheck.Scenario,
) -> None:
    outcome, clients = await run_to_merge_gate(scenario)

    assert (outcome.status, outcome.step) == ("parked", "merge_gate")
    [approval] = app_approvals.list_queue(scenario.home)
    assert approval.type == "merge" and approval.id == outcome.approval_id
    run = RunStore(scenario.home).open(outcome.run_id)
    state = read_flow_state(run)
    assert state.iteration == 2
    first, second = (json.loads((run.path / f"gates/{n}/results.json").read_text()) for n in (1, 2))
    assert (first["outcome"], second["outcome"]) == ("fail", "pass")
    assert app_tasks.find_task(scenario.home, scenario.task_id).task.status == "parked"
    assert all(client.remaining == 0 for client in clients.clients.values())
    async with Index(scenario.home) as index:
        [row] = await index.runs()
    assert (row["state"], row["task_id"]) == ("parked", scenario.task_id)


async def test_each_step_is_given_its_role_prompt_and_its_context(
    scenario: selfcheck.Scenario,
) -> None:
    outcome, clients = await run_to_merge_gate(scenario)

    lane = scenario.home.lanes / "development" / "roles"
    for role, client in clients.clients.items():
        assert client.calls[0].system == (lane / f"{role}.md").read_text()
    context = RunStore(scenario.home).open(outcome.run_id).path / "context"
    assert (context / "04-implement-2" / "gates" / "tests.txt").is_file()
    assert not (context / "02-implement-1" / "gates").exists()
    review = context / "06-review-1"
    assert "+def mul(a, b):" in (review / "diff.patch").read_text()
    assert (review / "work_report.json").is_file()
    worker_tools = {tool.name for tool in clients.clients["worker"].calls[0].tools}
    reviewer_tools = {tool.name for tool in clients.clients["reviewer"].calls[0].tools}
    assert "write_file" in worker_tools and "write_file" not in reviewer_tools


async def test_an_approved_merge_resumes_to_done_and_merges_main(
    scenario: selfcheck.Scenario,
) -> None:
    outcome, clients = await run_to_merge_gate(scenario)
    await app_approvals.approve(scenario.home, outcome.approval_id)

    resumed = await app_runs.resume_run(scenario.home, outcome.run_id, clients=clients)

    assert resumed.status == "done"
    main = project_git(scenario.repo)
    assert main.log_subjects()[0] == f"Merge {branch_name(outcome.run_id)}"
    assert "def mul(a, b):" in (scenario.repo / "calc.py").read_text()
    branch_log = main.run("log", "-1", "--format=%s|%an", branch_name(outcome.run_id)).strip()
    assert branch_log == f"{scenario.task_id}: {selfcheck.TASK_TITLE}|Control Plane"
    task = app_tasks.find_task(scenario.home, scenario.task_id)
    assert task.task.status == "done"
    home_git = Git(scenario.home.path)
    assert not home_git.status("tasks") and not home_git.status("runs")
    assert home_git.log_subjects()[0] == f"Task {scenario.task_id}: done"
    result = json.loads((scenario.home.runs / outcome.run_id / "result.json").read_text())
    assert result == {"state": "done", "reason": None, "iterations": 2}


async def test_resume_while_the_approval_is_undecided_reports_still_parked(
    scenario: selfcheck.Scenario,
) -> None:
    outcome, clients = await run_to_merge_gate(scenario)

    again = await app_runs.resume_run(scenario.home, outcome.run_id, clients=clients)

    assert (again.status, again.approval_id) == ("parked", outcome.approval_id)
    assert "undecided" in again.message


async def test_resume_all_resumes_every_unfinished_run_and_reports_each(
    scenario: selfcheck.Scenario,
) -> None:
    outcome, clients = await run_to_merge_gate(scenario)
    await app_approvals.approve(scenario.home, outcome.approval_id)

    results = await app_runs.resume_runs(scenario.home, clients=clients)

    assert [(r.run_id, r.outcome.status if r.outcome else None) for r in results] == [
        (outcome.run_id, "done")
    ]
    assert await app_runs.resume_runs(scenario.home, clients=clients) == []


async def test_resume_of_a_finished_or_unknown_run_is_refused(
    scenario: selfcheck.Scenario,
) -> None:
    with pytest.raises(RunError, match="no run R-0404"):
        await app_runs.resume_run(scenario.home, "R-0404", clients=ScriptedClients({}))


async def test_a_failing_step_fails_the_run_and_the_task(scenario: selfcheck.Scenario) -> None:
    clients = selfcheck.scripted_clients(scenario.home)
    clients.clients["planner"] = selfcheck.text_only_client(scenario.home, "planner")

    outcome = await app_runs.run_task(scenario.home, scenario.task_id, clients=clients)

    assert outcome.status == "failed"
    assert "no_submit" in outcome.message
    assert app_tasks.find_task(scenario.home, scenario.task_id).task.status == "failed"


async def test_a_failed_task_can_be_run_again_but_an_active_one_cannot(
    scenario: selfcheck.Scenario,
) -> None:
    await run_to_merge_gate(scenario)

    with pytest.raises(RunError, match=r"is parked.*cplane resume"):
        await app_runs.run_task(
            scenario.home, scenario.task_id, clients=selfcheck.scripted_clients(scenario.home)
        )
    app_tasks.set_status(scenario.home, scenario.task_id, "failed")
    outcome, _ = await run_to_merge_gate(scenario)
    assert outcome.status == "parked"


async def test_missing_credentials_fail_before_any_run_folder_exists(
    scenario: selfcheck.Scenario,
) -> None:
    with pytest.raises(CredentialsError, match="CPLANE_LLM_BASE_URL"):
        await app_runs.run_task(scenario.home, scenario.task_id, clients=environment_clients({}))

    assert RunStore(scenario.home).list_ids() == []
    assert app_tasks.find_task(scenario.home, scenario.task_id).task.status == "backlog"


async def test_a_merge_with_no_tests_command_fails_the_run(scenario: selfcheck.Scenario) -> None:
    outcome, clients = await run_to_merge_gate(scenario)
    project_yaml = scenario.home.projects / selfcheck.PROJECT / "project.yaml"
    data = yaml.safe_load(project_yaml.read_text())
    del data["gates"]["tests"]
    project_yaml.write_text(yaml.safe_dump(data))
    await app_approvals.approve(scenario.home, outcome.approval_id)

    resumed = await app_runs.resume_run(scenario.home, outcome.run_id, clients=clients)

    assert resumed.status == "failed"
    assert "no `tests` gate" in resumed.message
    assert project_git(scenario.repo).log_subjects() == ["Initial commit"]


def test_environment_clients_build_the_gateway_client() -> None:
    environ = {"CPLANE_LLM_BASE_URL": "http://gateway.invalid", "CPLANE_LLM_AUTH_TOKEN": "t"}
    factory = environment_clients(environ)
    profile = ModelProfile(wire_format="openai-compatible", model="m", max_tokens=100, strict=False)

    client = factory("planner", profile)

    assert isinstance(client, OpenAICompatibleClient)
    assert (client.model, client.max_tokens, client.strict) == ("m", 100, False)


def test_environment_clients_refuse_the_unwritten_anthropic_wire_format() -> None:
    factory = environment_clients({})
    profile = ModelProfile(wire_format="anthropic", model="m")

    with pytest.raises(RunError, match="anthropic.*not written yet"):
        factory("planner", profile)


def test_scripted_clients_refuse_a_role_without_a_script() -> None:
    with pytest.raises(RunError, match="no script for role worker"):
        ScriptedClients({})("worker", ModelProfile(wire_format="anthropic", model="m"))


def test_merge_test_command_comes_from_the_tests_gate() -> None:
    gate = CommandGate(argv=["python", "-m", "unittest"], timeout_s=5)

    assert app_runs.merge_test_command(gate) == (["python", "-m", "unittest"], 5)
    with pytest.raises(RunError, match="no `tests` gate"):
        app_runs.merge_test_command(None)
