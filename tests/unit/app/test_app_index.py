"""Task 4: `reindex` rebuilds the index from the files and verifies finished run folders."""

from pathlib import Path

from control_plane.app.index import ReindexReport, reindex
from control_plane.store.home import Home, create_home
from control_plane.store.index import SCHEMA_VERSION, Index
from control_plane.store.runs import RunDiscrepancy, RunStore

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "tasks"
SPEC_EXAMPLE = (FIXTURES / "T-0017-json-status.md").read_text()


def populated_home(tmp_path: Path) -> Home:
    home = create_home(tmp_path / "home")
    (home.tasks / "T-0017-json-status.md").write_text(SPEC_EXAMPLE)
    store = RunStore(home)
    run = store.create("R-0001")
    run.log_event("run_started")
    run.finish({"state": "done"})
    store.create("R-0002").log_event("run_started")
    return home


async def test_reindex_rebuilds_the_index_and_counts_the_rows(tmp_path: Path) -> None:
    home = populated_home(tmp_path)
    home.index.mkdir(exist_ok=True)
    (home.index / "state.db").write_bytes(b"stale")

    report = await reindex(home)

    assert isinstance(report, ReindexReport)
    assert report.counts == {
        "tasks": 1,
        "runs": 2,
        "steps": 0,
        "llm_calls": 0,
        "tool_calls": 0,
        "approvals": 0,
    }
    assert report.problems == []
    assert report.discrepancies == []
    assert report.ok
    async with Index(home) as index:
        assert [row["run_id"] for row in await index.runs()] == ["R-0001", "R-0002"]
    assert await Index(home)._stored_version() == SCHEMA_VERSION


async def test_reindex_reports_finished_run_folders_that_changed(tmp_path: Path) -> None:
    home = populated_home(tmp_path)
    (home.runs / "R-0001" / "extra.json").write_text("{}\n")

    report = await reindex(home)

    assert report.discrepancies == [RunDiscrepancy("R-0001", "added", "extra.json")]
    assert not report.ok


async def test_reindex_reports_files_it_could_not_index(tmp_path: Path) -> None:
    home = populated_home(tmp_path)
    (home.tasks / "T-0019-broken.md").write_text("---\nid: T-0019\n---\n")

    report = await reindex(home)

    assert len(report.problems) == 1
    assert "tasks/T-0019-broken.md" in report.problems[0]
    assert report.counts["tasks"] == 1
    assert not report.ok
