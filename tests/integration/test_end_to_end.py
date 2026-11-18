"""Task 15: the scripted end-to-end test (docs/SPEC.md sections 13, 14 and 16).

A human's whole session, through the `cplane` command line, against a temporary home: create
the home, register the fixture project and configure its gates, write the task, run it, read
the queue, approve the merge, resume, and read the status. The models are scripted. On the
way the run plans, fails the `tests` gate, implements again, passes every gate and the
review, and parks at the merge approval; after the approval it merges into `main` of the
fixture project and the task is done.
"""

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from control_plane import cli
from control_plane.app import selfcheck
from control_plane.cli import main
from control_plane.store.git import Git
from control_plane.store.home import Home
from control_plane.workspace import branch_name, project_git


async def cplane(*argv: str) -> int:
    return await asyncio.to_thread(main, list(argv))


async def test_a_task_goes_from_backlog_to_merged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = selfcheck.build_fixture_project(tmp_path)
    home_path = tmp_path / "home"
    monkeypatch.setenv("CPLANE_HOME", str(home_path))
    monkeypatch.setattr(cli, "make_clients", lambda _environ: selfcheck.scripted_clients(home))

    # Set up: the home, the project and its gates, the task.
    assert await cplane("init", str(home_path)) == 0
    home = Home(home_path.resolve())
    assert await cplane("project", "add", selfcheck.PROJECT, "--repo", str(fixture.repo)) == 0
    project_yaml = home.projects / selfcheck.PROJECT / "project.yaml"
    config = yaml.safe_load(project_yaml.read_text())
    config["gates"] = fixture.gates.model_dump(mode="json", exclude_none=True)
    config["protected_paths"] = list(selfcheck.PROTECTED)
    project_yaml.write_text(yaml.safe_dump(config, sort_keys=False))
    task_args = ["--title", selfcheck.TASK_TITLE, "--project", selfcheck.PROJECT]
    task_args += ["--goal", selfcheck.TASK_GOAL]
    for criterion in selfcheck.TASK_CRITERIA:
        task_args += ["--criterion", criterion]
    assert await cplane("task", "new", *task_args) == 0
    assert await cplane("task", "list") == 0
    assert "T-0001  backlog" in capsys.readouterr().out

    # Run: it parks at the merge approval after one failed gate run.
    assert await cplane("run", "T-0001") == 0
    out = capsys.readouterr().out
    assert "parked at merge_gate" in out
    [run_id] = [p.name for p in home.runs.iterdir() if p.is_dir()]
    assert await cplane("status", run_id) == 0
    assert "iteration 2/3" in capsys.readouterr().out

    # The queue holds the merge approval; a human approves it.
    assert await cplane("approvals") == 0
    queue = capsys.readouterr().out.splitlines()
    [entry] = queue
    approval_id = entry.split()[0]
    assert f"{run_id}  merge" in entry
    assert await cplane("approve", approval_id, "--note", "looks right") == 0

    # Resume: merge into main and verify there.
    assert await cplane("resume", run_id) == 0
    assert "done" in capsys.readouterr().out

    # Main of the fixture project holds the change, merged by the engine.
    main_git = project_git(fixture.repo)
    assert main_git.log_subjects()[0] == f"Merge {branch_name(run_id)}"
    assert "def mul(a, b):\n    return a * b" in (fixture.repo / "calc.py").read_text()
    assert "test_mul" in (fixture.repo / "tests" / "test_calc.py").read_text()

    # The task is done, the run folder is finished and committed, the index agrees.
    assert await cplane("task", "show", "T-0001") == 0
    assert "status: done" in capsys.readouterr().out
    result = json.loads((home.runs / run_id / "result.json").read_text())
    assert result == {"state": "done", "reason": None, "iterations": 2}
    assert not Git(home.path).status("runs")
    assert await cplane("status") == 0
    assert f"{run_id}  T-0001  done" in capsys.readouterr().out
    assert await cplane("inspect", run_id, "--context", "--step", "review") == 0
    assert "diff.patch" in capsys.readouterr().out
    assert await cplane("reindex") == 0
    assert "Finished run folders match their commits." in capsys.readouterr().out
    assert await cplane("approvals", "--all") == 0
    assert "[approve: looks right]" in capsys.readouterr().out
