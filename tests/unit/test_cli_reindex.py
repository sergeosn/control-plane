"""Task 4: `cplane reindex` rebuilds the index and verifies finished run folders."""

import asyncio
from pathlib import Path

import pytest

from control_plane.cli import main
from control_plane.store.home import create_home
from control_plane.store.index import Index
from control_plane.store.runs import RunStore


def test_reindex_rebuilds_the_index_and_prints_the_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = create_home(tmp_path / "home")
    run = RunStore(home).create("R-0001")
    run.log_event("run_started")
    run.finish({"state": "done"})

    code = main(["--home", str(home.path), "reindex"])

    out, err = capsys.readouterr()
    assert code == 0
    assert err == ""
    assert out.splitlines() == [
        f"Rebuilt index at {home.index / 'state.db'}",
        "tasks: 0, runs: 1, steps: 0, llm_calls: 0, tool_calls: 0, approvals: 0",
        "Finished run folders match their commits.",
    ]
    assert (home.index / "state.db").is_file()


def test_reindex_finds_the_home_through_cplane_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = create_home(tmp_path / "home")
    monkeypatch.setenv("CPLANE_HOME", str(home.path))

    assert main(["reindex"]) == 0
    assert "Rebuilt index" in capsys.readouterr().out


def test_reindex_without_a_home_fails_with_a_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CPLANE_HOME", raising=False)

    code = main(["reindex"])

    assert code == 1
    assert "no home repository" in capsys.readouterr().err


def test_reindex_lists_changed_finished_runs_and_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = create_home(tmp_path / "home")
    run = RunStore(home).create("R-0001")
    run.write_json("notes.json", {"a": 1})
    run.finish({"state": "done"})
    (run.path / "notes.json").write_text("{}\n")
    (run.path / "late.json").write_text("{}\n")

    code = main(["--home", str(home.path), "reindex"])

    out, err = capsys.readouterr()
    assert code == 1
    assert "runs: 1" in out
    assert err.splitlines() == [
        "cplane: finished run R-0001 differs from its commit: added late.json",
        "cplane: finished run R-0001 differs from its commit: modified notes.json",
    ]


def test_reindex_lists_files_it_could_not_index_and_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = create_home(tmp_path / "home")
    (home.tasks / "T-0019-broken.md").write_text("---\nid: T-0019\n---\n")

    code = main(["--home", str(home.path), "reindex"])

    out, err = capsys.readouterr()
    assert code == 1
    assert "tasks: 0" in out
    assert err.startswith("cplane: not indexed: tasks/T-0019-broken.md: ")


def test_reindex_does_not_leave_the_index_open(tmp_path: Path) -> None:
    home = create_home(tmp_path / "home")

    main(["--home", str(home.path), "reindex"])

    async def opens_without_rebuilding() -> bool:
        index = Index(home)
        try:
            return await index.open()
        finally:
            await index.close()

    assert asyncio.run(opens_without_rebuilding()) is False
