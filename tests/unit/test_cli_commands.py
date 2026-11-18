"""Task 15: the CLI commands of docs/SPEC.md section 14 that Tasks 3 to 9 did not add.

The end-to-end test (`tests/integration/test_end_to_end.py`) drives them all in sequence;
these tests cover each command's options, output and exit codes on its own.
"""

from pathlib import Path

import pytest

from control_plane import cli
from control_plane.app import selfcheck
from control_plane.app.clients import ScriptedClients
from control_plane.app.selfcheck import Scenario
from control_plane.cli import main
from control_plane.store.home import Home, create_home
from fixtures.project_repo import make_project

FIXTURE_TASK = Path(__file__).parents[1] / "fixtures" / "tasks" / "T-0017-json-status.md"
DEFAULT_MAKE_CLIENTS = cli.make_clients


@pytest.fixture
def home(tmp_path: Path) -> Home:
    return create_home(tmp_path / "home")


def run_cli(home: Home, *args: str) -> int:
    return main(["--home", str(home.path), *args])


@pytest.fixture
async def scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Scenario:
    prepared = await selfcheck.prepare(tmp_path)
    clients = selfcheck.scripted_clients(prepared.home)
    monkeypatch.setattr(cli, "make_clients", lambda _environ: clients)
    return prepared


# --- project add ----------------------------------------------------------------------


def test_project_add(home: Home, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = make_project(tmp_path / "project").root

    code = run_cli(home, "project", "add", "demo", "--repo", str(repo), "--protected", "src/")

    assert code == 0
    assert f"Added project demo at {repo.resolve()}" in capsys.readouterr().out
    assert (home.projects / "demo" / "project.yaml").is_file()
    assert run_cli(home, "project", "add", "demo", "--repo", str(repo)) == 1
    assert "already exists" in capsys.readouterr().err


# --- task new, list, show -------------------------------------------------------------


def test_task_new_list_and_show(home: Home, capsys: pytest.CaptureFixture[str]) -> None:
    code = run_cli(
        home,
        "task",
        "new",
        "--title",
        "Add a flag",
        "--project",
        "demo",
        "--goal",
        "A flag exists.",
        "--criterion",
        "The flag works.",
        "--criterion",
        "It is tested.",
        "--plan-approval",
    )
    assert code == 0
    assert "Created T-0001: tasks/T-0001-add-a-flag.md" in capsys.readouterr().out

    assert run_cli(home, "task", "list") == 0
    listed = capsys.readouterr().out
    assert "T-0001  backlog  p2  demo  Add a flag" in listed

    assert run_cli(home, "task", "show", "T-0001") == 0
    shown = capsys.readouterr().out
    assert "plan_approval: true" in shown and "- [AC2] It is tested." in shown


def test_task_new_from_a_file(home: Home, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(home, "task", "new", "--file", str(FIXTURE_TASK)) == 0
    assert "Created T-0017" in capsys.readouterr().out


def test_task_new_needs_a_file_or_the_fields(
    home: Home, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(home, "task", "new", "--title", "Only a title") == 2
    assert "--title, --project, --goal and --criterion" in capsys.readouterr().err
    assert run_cli(home, "task", "new", "--file", str(FIXTURE_TASK), "--title", "Both") == 2


def test_task_list_empty_and_broken(home: Home, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(home, "task", "list") == 0
    assert "No tasks." in capsys.readouterr().out
    (home.tasks / "T-0002-broken.md").write_text("nope\n")

    assert run_cli(home, "task", "list") == 1
    assert "not a task: tasks/T-0002-broken.md" in capsys.readouterr().err


def test_task_show_unknown(home: Home, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(home, "task", "show", "T-0404") == 1
    assert "no task T-0404" in capsys.readouterr().err


# --- run, resume, status, inspect -----------------------------------------------------


async def test_run_prints_where_the_run_parked(
    scenario: Scenario, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await _main(scenario.home, "run", scenario.task_id)

    out = capsys.readouterr().out
    assert code == 0
    assert "parked at merge_gate" in out and "cplane approve A-" in out


async def test_run_of_an_unknown_task_fails(
    scenario: Scenario, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await _main(scenario.home, "run", "T-0404") == 1
    assert "no task T-0404" in capsys.readouterr().err


async def test_run_without_credentials_names_the_variable(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "make_clients", DEFAULT_MAKE_CLIENTS)
    monkeypatch.delenv("CPLANE_LLM_BASE_URL", raising=False)

    assert await _main(scenario.home, "run", scenario.task_id) == 1
    assert "CPLANE_LLM_BASE_URL is not set" in capsys.readouterr().err


async def test_a_failed_run_exits_non_zero(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clients = selfcheck.scripted_clients(scenario.home)
    clients.clients["planner"] = selfcheck.text_only_client(scenario.home, "planner")
    monkeypatch.setattr(cli, "make_clients", lambda _environ: clients)

    assert await _main(scenario.home, "run", scenario.task_id) == 1
    assert "failed at failed" in capsys.readouterr().out


async def test_resume_needs_a_run_id_or_all_but_not_both(
    scenario: Scenario, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await _main(scenario.home, "resume") == 2
    assert await _main(scenario.home, "resume", "R-1", "--all") == 2
    assert "a run id or --all" in capsys.readouterr().err


async def test_resume_all_with_nothing_to_resume(
    scenario: Scenario, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await _main(scenario.home, "resume", "--all") == 0
    assert "No unfinished runs." in capsys.readouterr().out


async def test_resume_all_reports_a_run_that_cannot_resume_and_exits_non_zero(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    await _main(scenario.home, "run", scenario.task_id)
    monkeypatch.setattr(cli, "make_clients", lambda _environ: ScriptedClients({}))
    (scenario.home.runs / _only_run(scenario) / "flow.json").write_text("{}")
    capsys.readouterr()

    assert await _main(scenario.home, "resume", "--all") == 1
    assert "could not resume" in capsys.readouterr().err


async def test_status_and_inspect_transcript(
    scenario: Scenario, capsys: pytest.CaptureFixture[str]
) -> None:
    await _main(scenario.home, "run", scenario.task_id)
    run_id = _only_run(scenario)
    capsys.readouterr()

    assert await _main(scenario.home, "status") == 0
    line = capsys.readouterr().out
    assert run_id in line and "parked" in line and "merge_gate" in line and "iteration 2/3" in line

    assert await _main(scenario.home, "status", run_id) == 0
    assert run_id in capsys.readouterr().out
    assert await _main(scenario.home, "status", "R-0404") == 1

    assert await _main(scenario.home, "inspect", run_id, "--transcript", "--step", "plan") == 0
    out = capsys.readouterr().out
    assert "== 01-plan-1 ==" in out
    assert "[tool_use submit_plan]" in out and "[text]" in out


async def test_status_with_no_runs(home: Home, capsys: pytest.CaptureFixture[str]) -> None:
    assert await _main(home, "status") == 0
    assert "No runs." in capsys.readouterr().out


# --- selfcheck ------------------------------------------------------------------------


async def test_selfcheck_passes(capsys: pytest.CaptureFixture[str]) -> None:
    assert await _main(None, "selfcheck") == 0
    out = capsys.readouterr().out
    assert "selfcheck passed" in out
    for name in selfcheck.CHECKS:
        assert f"ok    {name}" in out


async def test_selfcheck_failure_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(*_args: object) -> object:
        raise RuntimeError("scripts are gone")

    monkeypatch.setattr(selfcheck, "scripted_clients", broken)

    assert await _main(None, "selfcheck") == 1
    assert "selfcheck FAILED" in capsys.readouterr().out


# --- helpers --------------------------------------------------------------------------


async def _main(home: Home | None, *args: str) -> int:
    """Run the CLI in a worker thread: it calls `asyncio.run`, which a running loop forbids."""
    import asyncio

    argv = ([] if home is None else ["--home", str(home.path)]) + list(args)
    return await asyncio.to_thread(main, argv)


def _only_run(scenario: Scenario) -> str:
    [run] = [p.name for p in scenario.home.runs.iterdir() if p.is_dir()]
    return run
