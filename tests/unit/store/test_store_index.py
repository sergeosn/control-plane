"""Task 4: the SQLite index is derived from the files and can always be rebuilt from them."""

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from control_plane.domain import Approval, LLMCall, Manifest, ToolCall
from control_plane.store.home import Home
from control_plane.store.index import SCHEMA_VERSION, TABLE_NAMES, Index
from control_plane.store.runs import RunStore

T0 = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "tasks"
SPEC_EXAMPLE = (FIXTURES / "T-0017-json-status.md").read_text()
SECOND_TASK = SPEC_EXAMPLE.replace("T-0017", "T-0018").replace("status: backlog", "status: done")
SHA = "0" * 40


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        current = self.now
        self.now += timedelta(seconds=1)
        return current


def make_manifest(run_id: str, task_id: str) -> Manifest:
    file_hash = {"path": "x", "sha256": "a" * 64}
    return Manifest(
        run_id=run_id,
        task_id=task_id,
        task_file=file_hash,
        project="control-plane",
        project_base_commit=SHA,
        engine_commit=SHA,
        flow=file_hash,
        rubric=file_hash,
        role_prompts=[],
        rules=[],
        models=[],
        budget={"max_iterations": 3, "max_cost_usd": 6.0, "max_wall_minutes": 60},
        context_files=[],
        created_at=T0,
    )


def make_llm_call(ts: datetime, **overrides: Any) -> LLMCall:
    fields: dict[str, Any] = {
        "ts": ts,
        "model": "claude-sonnet-5-5",
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_input_tokens": 50,
        "cache_creation_input_tokens": 10,
        "cost_usd": 0.001,
        "stop_reason": "tool_use",
    }
    fields.update(overrides)
    return LLMCall(**fields)


def make_tool_call(ts: datetime, **overrides: Any) -> ToolCall:
    fields: dict[str, Any] = {
        "ts": ts,
        "tool_use_id": "toolu_01",
        "tool": "read_file",
        "arguments": {"path": "src/x.py"},
        "tool_class": "read",
        "decision": "allow",
        "rule_id": "read-tools",
    }
    fields.update(overrides)
    return ToolCall(**fields)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(home: Home, clock: Clock) -> RunStore:
    return RunStore(home, clock=clock)


def write_tasks(home: Home) -> None:
    (home.tasks / "T-0017-json-status.md").write_text(SPEC_EXAMPLE)
    (home.tasks / "T-0018-second.md").write_text(SECOND_TASK)


def write_finished_run(store: RunStore, run_id: str = "R-0001") -> None:
    """A run with a manifest, two steps, model and tool calls, and a decided approval."""
    run = store.create(run_id)
    run.write_json("manifest.json", make_manifest(run_id, "T-0017"))
    run.log_event("run_started")
    run.log_event("step_started", step="plan", attempt=1)
    run.append_jsonl("steps/01-plan-1/llm_calls.jsonl", make_llm_call(T0))
    run.append_jsonl("steps/01-plan-1/tool_calls.jsonl", make_tool_call(T0))
    run.append_jsonl(
        "steps/01-plan-1/llm_calls.jsonl",
        make_llm_call(T0 + timedelta(seconds=5), stop_reason="end_turn", output_tokens=400),
    )
    run.write_json("steps/01-plan-1/artifact.json", {"summary": "plan"})
    run.log_event("step_finished", step="plan", attempt=1, outcome="ok")
    run.log_event("step_started", step="implement", attempt=1)
    run.append_jsonl(
        "steps/02-implement-1/tool_calls.jsonl",
        make_tool_call(
            T0 + timedelta(seconds=9),
            tool_use_id="toolu_02",
            tool="run_command",
            arguments={"argv": ["git", "push"]},
            tool_class="write-external",
            decision="park",
            rule_id="network-and-deps",
        ),
    )
    approval = Approval(
        id="A-0001",
        run_id=run_id,
        type="tool_call",
        asked="Run git push?",
        evidence=["steps/02-implement-1/transcript.jsonl"],
        created_at=T0 + timedelta(seconds=10),
    )
    run.append_approval(approval)
    run.log_event("run_parked", step="implement", attempt=1, message="approval A-0001")
    run.append_approval(approval.decide("deny", at=T0 + timedelta(minutes=1), note="not yet"))
    run.log_event("step_finished", step="implement", attempt=1, outcome="error")
    run.finish({"state": "failed"})


def write_active_run(store: RunStore, run_id: str = "R-0002") -> None:
    """A bare run that has only started: no manifest, no steps, not finished."""
    store.create(run_id).log_event("run_started")


def user_version(path: Path) -> int:
    with sqlite3.connect(path) as db:
        return db.execute("PRAGMA user_version").fetchone()[0]


def table_names(path: Path) -> set[str]:
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


# --- Opening and the schema version --------------------------------------------------------


async def test_open_creates_the_database_under_dot_index_with_the_schema_version(
    home: Home,
) -> None:
    index = Index(home)

    rebuilt = await index.open()
    await index.close()

    assert rebuilt is True
    assert index.path == home.index / "state.db"
    assert index.path.is_file()
    assert user_version(index.path) == SCHEMA_VERSION
    assert set(TABLE_NAMES) <= table_names(index.path)
    assert set(TABLE_NAMES) == {"tasks", "runs", "steps", "llm_calls", "tool_calls", "approvals"}


async def test_open_on_a_fresh_database_loads_the_home_files(home: Home, store: RunStore) -> None:
    write_tasks(home)
    write_finished_run(store)
    write_active_run(store)

    async with Index(home) as index:
        snapshot = await index.snapshot()

    assert [row["id"] for row in snapshot["tasks"]] == ["T-0017", "T-0018"]
    assert [row["run_id"] for row in snapshot["runs"]] == ["R-0001", "R-0002"]
    assert len(snapshot["steps"]) == 2
    assert len(snapshot["llm_calls"]) == 2
    assert len(snapshot["tool_calls"]) == 2
    assert len(snapshot["approvals"]) == 1


async def test_open_with_a_matching_version_keeps_the_database_as_it_is(home: Home) -> None:
    write_tasks(home)
    async with Index(home):
        pass
    (home.tasks / "T-0018-second.md").unlink()

    index = Index(home)
    rebuilt = await index.open()
    rows = await index.tasks()
    await index.close()

    assert rebuilt is False
    assert [row["id"] for row in rows] == ["T-0017", "T-0018"]


async def test_a_schema_version_mismatch_deletes_the_file_and_rebuilds_it(home: Home) -> None:
    write_tasks(home)
    async with Index(home) as index:
        path = index.path
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 99")
        db.execute("CREATE TABLE leftover (x INTEGER)")
        db.execute("DELETE FROM tasks")
    (home.tasks / "T-0018-second.md").unlink()

    index = Index(home)
    rebuilt = await index.open()
    rows = await index.tasks()
    await index.close()

    assert rebuilt is True
    assert user_version(path) == SCHEMA_VERSION
    assert "leftover" not in table_names(path)
    assert [row["id"] for row in rows] == ["T-0017"]


async def test_a_file_that_is_not_a_database_is_rebuilt(home: Home) -> None:
    write_tasks(home)
    home.index.mkdir(exist_ok=True)
    (home.index / "state.db").write_bytes(b"this is not sqlite\n" * 100)

    async with Index(home) as index:
        rows = await index.tasks()

    assert [row["id"] for row in rows] == ["T-0017", "T-0018"]


# --- Rebuild gives the same results as incremental writes ----------------------------------


async def test_deleting_the_database_and_reindexing_gives_the_same_query_results(
    home: Home, store: RunStore
) -> None:
    index = Index(home)
    await index.open()
    write_tasks(home)
    await index.sync_tasks()
    write_finished_run(store)
    await index.sync_run("R-0001")
    write_active_run(store)
    await index.sync_run("R-0002")
    before = await index.snapshot()
    await index.close()

    index.path.unlink()
    async with Index(home) as fresh:
        after = await fresh.snapshot()

    assert set(before) == set(TABLE_NAMES)
    assert all(before[name] for name in TABLE_NAMES), "every table should have rows"
    assert after == before


async def test_sync_run_replaces_the_rows_of_that_run_only(home: Home, store: RunStore) -> None:
    write_finished_run(store, "R-0001")
    write_finished_run(store, "R-0002")
    async with Index(home) as index:
        (home.runs / "R-0002" / "steps" / "02-implement-1" / "tool_calls.jsonl").unlink()
        await index.sync_run("R-0002")
        tool_calls = await index.tool_calls()
        assert [(r["run_id"], r["tool_use_id"]) for r in tool_calls] == [
            ("R-0001", "toolu_01"),
            ("R-0001", "toolu_02"),
            ("R-0002", "toolu_01"),
        ]


async def test_sync_run_of_a_run_folder_that_is_gone_removes_its_rows(
    home: Home, store: RunStore
) -> None:
    write_active_run(store, "R-0002")
    async with Index(home) as index:
        (home.runs / "R-0002" / "events.jsonl").unlink()
        (home.runs / "R-0002" / "context").rmdir()
        (home.runs / "R-0002" / "steps").rmdir()
        (home.runs / "R-0002" / "gates").rmdir()
        (home.runs / "R-0002").rmdir()
        await index.sync_run("R-0002")
        assert await index.runs() == []


# --- A failing index write never fails the file write before it ---------------------------


async def test_a_failing_index_write_is_logged_and_does_not_fail_the_file_write(
    home: Home, store: RunStore, caplog: pytest.LogCaptureFixture
) -> None:
    index = Index(home)
    await index.open()
    await index.close()
    index.path.write_bytes(b"corrupt" * 100)
    run = store.create("R-0001")

    run.log_event("run_started")
    with caplog.at_level(logging.ERROR, logger="control_plane.store.index"):
        ok = await index.attempt(index.sync_run("R-0001"), "run R-0001")
    await index.close()

    assert ok is False
    assert [e.kind for e in run.events()] == ["run_started"]
    assert len(caplog.records) == 1
    assert "index" in caplog.records[0].getMessage()
    assert "R-0001" in caplog.records[0].getMessage()
    assert "reindex" in caplog.records[0].getMessage()


async def test_attempt_returns_true_when_the_index_write_succeeds(
    home: Home, store: RunStore
) -> None:
    write_active_run(store)
    async with Index(home) as index:
        assert await index.attempt(index.sync_run("R-0002"), "run R-0002") is True


# --- What the rows hold ---------------------------------------------------------------------


async def test_task_rows_carry_the_front_matter_and_the_file_path(home: Home) -> None:
    write_tasks(home)

    async with Index(home) as index:
        rows = await index.tasks()

    assert rows[0] == {
        "id": "T-0017",
        "path": "tasks/T-0017-json-status.md",
        "title": "Add --json output to `cplane status`",
        "project": "control-plane",
        "lane": "development",
        "priority": 2,
        "plan_approval": False,
        "status": "backlog",
    }
    assert rows[1]["id"] == "T-0018"
    assert rows[1]["status"] == "done"


async def test_a_malformed_task_file_is_reported_and_skipped(home: Home) -> None:
    write_tasks(home)
    (home.tasks / "T-0019-broken.md").write_text("---\nid: T-0019\n---\n")

    async with Index(home) as index:
        problems = await index.sync_tasks()
        rows = await index.tasks()

    assert [row["id"] for row in rows] == ["T-0017", "T-0018"]
    assert len(problems) == 1
    assert "tasks/T-0019-broken.md" in problems[0]
    assert "title" in problems[0]


async def test_run_rows_come_from_manifest_events_and_result(home: Home, store: RunStore) -> None:
    write_finished_run(store)
    write_active_run(store)

    async with Index(home) as index:
        rows = await index.runs()

    assert rows[0] == {
        "run_id": "R-0001",
        "task_id": "T-0017",
        "project": "control-plane",
        "state": "failed",
        "finished": True,
        "current_step": "implement",
        "created_at": "2026-11-17T09:00:00+00:00",
        "last_event_at": "2026-11-17T09:00:05+00:00",
    }
    assert rows[1] == {
        "run_id": "R-0002",
        "task_id": None,
        "project": None,
        "state": "running",
        "finished": False,
        "current_step": None,
        "created_at": "2026-11-17T09:00:06+00:00",
        "last_event_at": "2026-11-17T09:00:06+00:00",
    }


async def test_step_rows_come_from_step_folders_and_their_events(
    home: Home, store: RunStore
) -> None:
    write_finished_run(store)
    (home.runs / "R-0001" / "steps" / "not-a-step").mkdir()

    async with Index(home) as index:
        rows = await index.steps()

    assert rows == [
        {
            "run_id": "R-0001",
            "dir": "01-plan-1",
            "seq": 1,
            "name": "plan",
            "attempt": 1,
            "outcome": "ok",
            "has_artifact": True,
        },
        {
            "run_id": "R-0001",
            "dir": "02-implement-1",
            "seq": 2,
            "name": "implement",
            "attempt": 1,
            "outcome": "error",
            "has_artifact": False,
        },
    ]


async def test_llm_call_rows_keep_file_order_and_usage(home: Home, store: RunStore) -> None:
    write_finished_run(store)

    async with Index(home) as index:
        rows = await index.llm_calls()

    assert rows[0] == {
        "run_id": "R-0001",
        "step_dir": "01-plan-1",
        "seq": 1,
        "ts": "2026-11-17T09:00:00+00:00",
        "model": "claude-sonnet-5-5",
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_input_tokens": 50,
        "cache_creation_input_tokens": 10,
        "cost_usd": 0.001,
        "stop_reason": "tool_use",
    }
    assert (rows[1]["seq"], rows[1]["stop_reason"], rows[1]["output_tokens"]) == (
        2,
        "end_turn",
        400,
    )


async def test_tool_call_rows_keep_arguments_as_json_and_the_decision(
    home: Home, store: RunStore
) -> None:
    write_finished_run(store)

    async with Index(home) as index:
        rows = await index.tool_calls()

    assert rows[1] == {
        "run_id": "R-0001",
        "step_dir": "02-implement-1",
        "seq": 1,
        "ts": "2026-11-17T09:00:09+00:00",
        "tool_use_id": "toolu_02",
        "tool": "run_command",
        "arguments": '{"argv": ["git", "push"]}',
        "tool_class": "write-external",
        "decision": "park",
        "rule_id": "network-and-deps",
        "error": None,
    }


async def test_approval_rows_hold_the_last_line_per_id(home: Home, store: RunStore) -> None:
    write_finished_run(store)

    async with Index(home) as index:
        rows = await index.approvals()

    assert rows == [
        {
            "run_id": "R-0001",
            "id": "A-0001",
            "type": "tool_call",
            "asked": "Run git push?",
            "created_at": "2026-11-17T09:00:10+00:00",
            "answer": "deny",
            "note": "not yet",
            "decided_at": "2026-11-17T09:01:00+00:00",
        }
    ]


async def test_a_malformed_record_in_a_run_is_reported_and_the_rest_is_indexed(
    home: Home, store: RunStore
) -> None:
    write_finished_run(store)
    (home.runs / "R-0001" / "manifest.json").write_text('{"run_id": "R-0001"}\n')
    with (home.runs / "R-0001" / "steps" / "01-plan-1" / "llm_calls.jsonl").open("a") as f:
        f.write('{"not": "a call"}\n')

    async with Index(home) as index:
        problems = await index.sync_run("R-0001")
        runs = await index.runs()
        llm_calls = await index.llm_calls()

    assert runs[0]["task_id"] is None
    assert runs[0]["state"] == "failed"
    assert len(llm_calls) == 2
    assert len(problems) == 2
    assert problems[0].startswith("runs/R-0001/manifest.json: task_id: Field required; ")
    assert "https://" not in problems[0]
    assert problems[1].startswith("runs/R-0001/steps/01-plan-1/llm_calls.jsonl: line 3: ")
    assert "ts: Field required" in problems[1]


async def test_blank_lines_in_a_jsonl_log_are_ignored_and_do_not_shift_the_sequence(
    home: Home, store: RunStore
) -> None:
    write_finished_run(store)
    log = home.runs / "R-0001" / "steps" / "01-plan-1" / "llm_calls.jsonl"
    log.write_text(log.read_text().replace("\n", "\n\n", 1))

    async with Index(home) as index:
        problems = await index.sync_run("R-0001")
        rows = await index.llm_calls()

    assert problems == []
    assert [row["seq"] for row in rows] == [1, 2]


async def test_rebuild_returns_every_problem_it_found(home: Home, store: RunStore) -> None:
    write_finished_run(store)
    (home.tasks / "T-0019-broken.md").write_text("---\nid: T-0019\n---\n")
    (home.runs / "R-0001" / "result.json").write_text("{not json\n")

    index = Index(home)
    problems = await index.rebuild()
    counts = await index.counts()
    await index.close()

    assert len(problems) == 2
    assert counts == {
        "tasks": 0,
        "runs": 1,
        "steps": 2,
        "llm_calls": 2,
        "tool_calls": 2,
        "approvals": 1,
    }
