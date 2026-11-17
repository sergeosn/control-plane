"""Task 3: run folders are written atomically, committed when finished and verified after."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from control_plane.domain import Approval, Event
from control_plane.store.git import Git
from control_plane.store.home import Home
from control_plane.store.runs import (
    RunDiscrepancy,
    RunFinishedError,
    RunFolder,
    RunStore,
    new_run_id,
)

T0 = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)


class Clock:
    """A clock that advances one second per reading."""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        current = self.now
        self.now += timedelta(seconds=1)
        return current


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(home: Home, clock: Clock) -> RunStore:
    return RunStore(home, clock=clock)


@pytest.fixture
def run(store: RunStore) -> RunFolder:
    return store.create("R-0001")


def finish(run: RunFolder) -> str:
    return run.finish({"state": "done"})


def test_new_run_id_is_time_ordered_and_unique(clock: Clock) -> None:
    first = new_run_id(clock())
    second = new_run_id(clock())

    assert first.startswith("R-20261117-090000-")
    assert second.startswith("R-20261117-090001-")
    assert first != second
    assert first != new_run_id(T0)


def test_create_makes_the_run_folder_layout_from_section_5(home: Home, run: RunFolder) -> None:
    assert run.path == home.runs / "R-0001"
    assert (run.path / "context").is_dir()
    assert (run.path / "steps").is_dir()
    assert (run.path / "gates").is_dir()
    assert not run.finished


def test_create_refuses_an_existing_run_id(store: RunStore, run: RunFolder) -> None:
    with pytest.raises(FileExistsError, match="R-0001"):
        store.create("R-0001")


def test_open_finds_an_existing_run_and_rejects_a_missing_one(
    store: RunStore, run: RunFolder
) -> None:
    assert store.open("R-0001").path == run.path
    with pytest.raises(FileNotFoundError, match="R-9999"):
        store.open("R-9999")


def test_list_ids_returns_run_ids_in_order(store: RunStore) -> None:
    store.create("R-0002")
    store.create("R-0001")

    assert store.list_ids() == ["R-0001", "R-0002"]


def test_write_json_writes_a_file_inside_the_run_folder_atomically(run: RunFolder) -> None:
    run.write_json("steps/01-plan-1/state.json", {"pending": []})

    assert json.loads((run.path / "steps/01-plan-1/state.json").read_text()) == {"pending": []}
    assert [p.name for p in (run.path / "steps/01-plan-1").iterdir()] == ["state.json"]


def test_write_json_refuses_a_path_outside_the_run_folder(run: RunFolder) -> None:
    with pytest.raises(ValueError, match="outside the run folder"):
        run.write_json("../escape.json", {})
    with pytest.raises(ValueError, match="outside the run folder"):
        run.write_json("/tmp/escape.json", {})


def test_log_event_appends_to_events_jsonl_with_run_id_and_clock_time(run: RunFolder) -> None:
    first = run.log_event("run_started")
    second = run.log_event("step_finished", step="plan", attempt=1, outcome="ok", message="done")

    assert first == Event(ts=T0, run_id="R-0001", kind="run_started")
    assert second.ts == T0 + timedelta(seconds=1)
    assert second.step == "plan"
    assert run.events() == [first, second]
    lines = (run.path / "events.jsonl").read_text().splitlines()
    assert len(lines) == 2


def test_append_approval_round_trips_through_approvals_jsonl(run: RunFolder) -> None:
    approval = Approval(
        id="A-0001",
        run_id="R-0001",
        type="merge",
        asked="Merge run R-0001?",
        evidence=["diff.patch"],
        created_at=T0,
    )

    run.append_approval(approval)
    run.append_approval(approval.decide("approve", at=T0 + timedelta(minutes=1)))

    stored = run.approvals()
    assert [a.answer for a in stored] == [None, "approve"]
    assert stored[0] == approval


def test_append_jsonl_writes_arbitrary_lines_inside_the_run_folder(run: RunFolder) -> None:
    run.append_jsonl("steps/01-plan-1/transcript.jsonl", '{"role": "user"}\n')
    run.append_jsonl("steps/01-plan-1/transcript.jsonl", '{"role": "assistant"}\n')

    text = (run.path / "steps/01-plan-1/transcript.jsonl").read_text()
    assert text == '{"role": "user"}\n{"role": "assistant"}\n'


def test_finish_writes_result_json_and_commits_the_run_folder(home: Home, run: RunFolder) -> None:
    run.log_event("run_started")
    run.write_json("manifest.json", {"run_id": "R-0001"})
    git = Git(home.path)
    before = git.head()

    sha = finish(run)

    assert run.finished
    assert json.loads((run.path / "result.json").read_text()) == {"state": "done"}
    assert sha == git.head() != before
    assert git.log_subjects()[0] == "Finish run R-0001"
    assert git.status() == []
    assert set(git.tracked("runs/R-0001")) == {
        "runs/R-0001/events.jsonl",
        "runs/R-0001/manifest.json",
        "runs/R-0001/result.json",
    }


def test_finish_commits_only_the_run_folder(home: Home, run: RunFolder) -> None:
    stray = home.tasks / "T-0001-stray.md"
    stray.write_text("---\n")

    finish(run)

    assert Git(home.path).status() == [("added", "tasks/T-0001-stray.md")]


def test_finish_twice_is_refused(run: RunFolder) -> None:
    finish(run)

    with pytest.raises(RunFinishedError, match="R-0001"):
        finish(run)


def test_writes_after_finish_are_refused(run: RunFolder) -> None:
    finish(run)

    with pytest.raises(RunFinishedError, match="R-0001"):
        run.write_json("manifest.json", {})
    with pytest.raises(RunFinishedError, match="R-0001"):
        run.append_jsonl("events.jsonl", "{}\n")
    with pytest.raises(RunFinishedError, match="R-0001"):
        run.log_event("late")
    assert not (run.path / "manifest.json").exists()


def test_a_reopened_finished_run_is_still_finished(store: RunStore, run: RunFolder) -> None:
    finish(run)

    assert store.open("R-0001").finished


def test_verify_reports_nothing_when_finished_runs_are_unchanged(
    store: RunStore, run: RunFolder
) -> None:
    run.log_event("run_started")
    finish(run)
    store.create("R-0002").log_event("run_started")

    assert store.verify_finished() == []


def test_verify_reports_a_modified_file_in_a_finished_run(store: RunStore, run: RunFolder) -> None:
    run.log_event("run_started")
    finish(run)

    with (run.path / "events.jsonl").open("a") as handle:
        handle.write('{"tampered": true}\n')

    assert store.verify_finished() == [RunDiscrepancy("R-0001", "modified", "events.jsonl")]


def test_verify_reports_a_deleted_file_in_a_finished_run(store: RunStore, run: RunFolder) -> None:
    run.write_json("manifest.json", {"run_id": "R-0001"})
    finish(run)

    (run.path / "manifest.json").unlink()

    assert store.verify_finished() == [RunDiscrepancy("R-0001", "deleted", "manifest.json")]


def test_verify_reports_a_file_added_to_a_finished_run(store: RunStore, run: RunFolder) -> None:
    finish(run)

    (run.path / "gates" / "1").mkdir()
    (run.path / "gates" / "1" / "lint.txt").write_text("ok\n")

    assert store.verify_finished() == [RunDiscrepancy("R-0001", "added", "gates/1/lint.txt")]


def test_verify_reports_a_finished_run_that_was_never_committed(
    home: Home, store: RunStore, clock: Clock
) -> None:
    folder = home.runs / "R-0007"
    folder.mkdir()
    (folder / "result.json").write_text('{"state": "failed"}\n')

    assert store.verify_finished() == [RunDiscrepancy("R-0007", "uncommitted", "")]


def test_verify_ignores_changes_in_unfinished_runs(store: RunStore, run: RunFolder) -> None:
    run.log_event("run_started")

    assert store.verify_finished() == []


def test_verify_ignores_files_outside_run_folders(
    home: Home, store: RunStore, run: RunFolder
) -> None:
    finish(run)
    (home.tasks / "T-0001-x.md").write_text("---\n")

    assert store.verify_finished() == []


def test_verify_lists_discrepancies_sorted_by_run_and_path(store: RunStore) -> None:
    second = store.create("R-0002")
    second.write_json("manifest.json", {"run_id": "R-0002"})
    finish(second)
    first = store.create("R-0001")
    finish(first)
    (second.path / "manifest.json").write_text("{}\n")
    (first.path / "extra.json").write_text("{}\n")

    assert store.verify_finished() == [
        RunDiscrepancy("R-0001", "added", "extra.json"),
        RunDiscrepancy("R-0002", "modified", "manifest.json"),
    ]


def test_run_store_defaults_to_the_wall_clock(home: Home) -> None:
    run = RunStore(home).create("R-0001")

    event = run.log_event("run_started")

    assert event.ts.tzinfo is not None
    assert abs((datetime.now(UTC) - event.ts).total_seconds()) < 60


def test_tmp_path_is_not_a_home(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="runs"):
        RunStore(Home(tmp_path)).list_ids()
