"""The SQLite index (SPEC.md section 14): a queryable copy of what the files hold.

Files first and index second. Every row here is derived from a task file or a run folder,
so the database can be deleted and rebuilt at any time (`Index.rebuild`, behind
`cplane reindex`). There are no migrations: the schema version lives in SQLite's
`user_version` pragma, and `open` deletes and rebuilds the file when it does not match.

Writers keep the index current with `sync_tasks` and `sync_run`, which re-derive the rows
of one task table or one run from its files. Wrapping such a call in `attempt` turns a
failure into a log line, so a file write that already happened is never undone or reported
as failed because the index could not follow.
"""

import json
import logging
import re
from collections.abc import Awaitable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from pydantic import ValidationError
from sqlalchemy import (
    Boolean,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    delete,
    func,
    insert,
    select,
    text,
)
from sqlalchemy.exc import DatabaseError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from control_plane.domain import Approval, Event, LLMCall, Manifest, Task, TaskFileError, ToolCall
from control_plane.store.home import Home
from control_plane.store.runs import (
    APPROVALS_FILE,
    ARTIFACT_FILE,
    EVENTS_FILE,
    LLM_CALLS_FILE,
    MANIFEST_FILE,
    RESULT_FILE,
    STEPS_DIR,
    TOOL_CALLS_FILE,
)

SCHEMA_VERSION = 1
DB_FILE = "state.db"
TABLE_NAMES = ("tasks", "runs", "steps", "llm_calls", "tool_calls", "approvals")
RUN_STATE_EVENTS = ("run_parked", "run_resumed")
"""The events that move an unfinished run between `running` and `parked` (section 12)."""

logger = logging.getLogger(__name__)

_STEP_DIR = re.compile(r"^(\d+)-(.+)-(\d+)$")

METADATA = MetaData()

TASKS = Table(
    "tasks",
    METADATA,
    Column("id", String, primary_key=True),
    Column("path", String, nullable=False),
    Column("title", String, nullable=False),
    Column("project", String, nullable=False),
    Column("lane", String, nullable=False),
    Column("priority", Integer, nullable=False),
    Column("plan_approval", Boolean, nullable=False),
    Column("status", String, nullable=False),
)

RUNS = Table(
    "runs",
    METADATA,
    Column("run_id", String, primary_key=True),
    Column("task_id", String),
    Column("project", String),
    Column("state", String, nullable=False),
    Column("finished", Boolean, nullable=False),
    Column("current_step", String),
    Column("created_at", String),
    Column("last_event_at", String),
)

STEPS = Table(
    "steps",
    METADATA,
    Column("run_id", String, primary_key=True),
    Column("dir", String, primary_key=True),
    Column("seq", Integer, nullable=False),
    Column("name", String, nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("outcome", String),
    Column("has_artifact", Boolean, nullable=False),
)

LLM_CALLS = Table(
    "llm_calls",
    METADATA,
    Column("run_id", String, primary_key=True),
    Column("step_dir", String, primary_key=True),
    Column("seq", Integer, primary_key=True),
    Column("ts", String, nullable=False),
    Column("model", String, nullable=False),
    Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("cache_read_input_tokens", Integer, nullable=False),
    Column("cache_creation_input_tokens", Integer, nullable=False),
    Column("cost_usd", Float, nullable=False),
    Column("stop_reason", String, nullable=False),
)

TOOL_CALLS = Table(
    "tool_calls",
    METADATA,
    Column("run_id", String, primary_key=True),
    Column("step_dir", String, primary_key=True),
    Column("seq", Integer, primary_key=True),
    Column("ts", String, nullable=False),
    Column("tool_use_id", String, nullable=False),
    Column("tool", String, nullable=False),
    Column("arguments", Text, nullable=False),
    Column("tool_class", String),
    Column("decision", String, nullable=False),
    Column("rule_id", String),
    Column("error", String),
)

APPROVALS = Table(
    "approvals",
    METADATA,
    Column("run_id", String, primary_key=True),
    Column("id", String, primary_key=True),
    Column("type", String, nullable=False),
    Column("asked", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("answer", String),
    Column("note", String),
    Column("decided_at", String),
)

TABLES: dict[str, Table] = {
    "tasks": TASKS,
    "runs": RUNS,
    "steps": STEPS,
    "llm_calls": LLM_CALLS,
    "tool_calls": TOOL_CALLS,
    "approvals": APPROVALS,
}

Row = dict[str, Any]


class Index:
    def __init__(self, home: Home) -> None:
        self.home = home
        self.path: Path = home.index / DB_FILE
        self._engine: AsyncEngine | None = None

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        return self._engine

    async def open(self) -> bool:
        """Make the index usable. Returns True when it had to be built from the files."""
        if self.path.exists() and await self._stored_version() == SCHEMA_VERSION:
            return False
        await self.rebuild()
        return True

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    async def _stored_version(self) -> int | None:
        """The schema version in the file, or None when the file is not a database."""
        try:
            async with self.engine.connect() as conn:
                return (await conn.execute(text("PRAGMA user_version"))).scalar_one()
        except DatabaseError:
            return None

    async def rebuild(self) -> list[str]:
        """Delete the database and derive it again from the home repository.

        Returns a description of every file or record that could not be indexed.
        """
        await self.close()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-journal", "-wal", "-shm"):
            self.path.with_name(self.path.name + suffix).unlink(missing_ok=True)
        async with self.engine.begin() as conn:
            await conn.run_sync(METADATA.create_all)
            await conn.execute(text(f"PRAGMA user_version = {SCHEMA_VERSION}"))
        problems = await self.sync_tasks()
        for run_id in sorted(entry.name for entry in self.home.runs.iterdir() if entry.is_dir()):
            problems.extend(await self.sync_run(run_id))
        return problems

    async def attempt(self, write: Awaitable[Any], what: str) -> bool:
        """Await an index write; on failure log it and return False instead of raising.

        `what` names the files the write mirrors, such as `run R-0001`, for the log line.
        """
        try:
            await write
        except Exception:
            logger.exception(
                "index write for %s failed; the files are the source of truth"
                " and `cplane reindex` rebuilds the index",
                what,
            )
            return False
        return True

    # --- Writers ----------------------------------------------------------------------------

    async def sync_tasks(self) -> list[str]:
        """Re-derive the `tasks` table from `tasks/*.md`. Returns the files it had to skip."""
        rows, problems = _task_rows(self.home)
        async with self.engine.begin() as conn:
            await conn.execute(delete(TASKS))
            if rows:
                await conn.execute(insert(TASKS), rows)
        return problems

    async def sync_run(self, run_id: str) -> list[str]:
        """Re-derive every row of one run from its folder. Returns the records it had to skip."""
        derived = _run_rows(self.home, run_id)
        async with self.engine.begin() as conn:
            for table in (RUNS, STEPS, LLM_CALLS, TOOL_CALLS, APPROVALS):
                await conn.execute(delete(table).where(table.c.run_id == run_id))
            for table, rows in derived.rows.items():
                if rows:
                    await conn.execute(insert(table), rows)
        return derived.problems

    # --- Queries ----------------------------------------------------------------------------

    async def rows(self, table: str) -> list[Row]:
        """Every row of one of the six tables, in primary-key order."""
        tbl = TABLES[table]
        async with self.engine.connect() as conn:
            result = await conn.execute(select(tbl).order_by(*tbl.primary_key.columns))
            return [dict(row._mapping) for row in result]

    async def tasks(self) -> list[Row]:
        return await self.rows("tasks")

    async def runs(self) -> list[Row]:
        return await self.rows("runs")

    async def steps(self) -> list[Row]:
        return await self.rows("steps")

    async def llm_calls(self) -> list[Row]:
        return await self.rows("llm_calls")

    async def tool_calls(self) -> list[Row]:
        return await self.rows("tool_calls")

    async def approvals(self) -> list[Row]:
        return await self.rows("approvals")

    async def snapshot(self) -> dict[str, list[Row]]:
        return {name: await self.rows(name) for name in TABLE_NAMES}

    async def counts(self) -> dict[str, int]:
        async with self.engine.connect() as conn:
            return {
                name: (await conn.execute(select(func.count()).select_from(tbl))).scalar_one()
                for name, tbl in TABLES.items()
            }


# --- Deriving rows from files ------------------------------------------------------------


def _task_rows(home: Home) -> tuple[list[Row], list[str]]:
    rows: list[Row] = []
    problems: list[str] = []
    for path in sorted(home.tasks.glob("*.md")):
        relative = path.relative_to(home.path).as_posix()
        try:
            task = Task.from_markdown(path.read_text(encoding="utf-8"))
        except TaskFileError as error:
            problems.append(f"{relative}: {error}")
            continue
        rows.append(
            {
                "id": task.id,
                "path": relative,
                "title": task.title,
                "project": task.project,
                "lane": task.lane,
                "priority": task.priority,
                "plan_approval": task.plan_approval,
                "status": task.status,
            }
        )
    return rows, problems


@dataclass
class _RunRows:
    rows: dict[Table, list[Row]] = field(
        default_factory=lambda: {t: [] for t in (RUNS, STEPS, LLM_CALLS, TOOL_CALLS, APPROVALS)}
    )
    problems: list[str] = field(default_factory=list)


def _run_rows(home: Home, run_id: str) -> _RunRows:
    """Every row of one run, derived from its folder; nothing for a folder that is gone."""
    out = _RunRows()
    folder = home.runs / run_id
    if not folder.is_dir():
        return out
    relative = f"runs/{run_id}"
    manifest = _read_manifest(folder / MANIFEST_FILE, f"{relative}/{MANIFEST_FILE}", out)
    events = _read_records(folder / EVENTS_FILE, Event, f"{relative}/{EVENTS_FILE}", out)
    out.rows[RUNS].append(_run_row(folder, run_id, manifest, events, out))
    steps_dir = folder / STEPS_DIR
    for step in sorted(steps_dir.iterdir()) if steps_dir.is_dir() else []:
        match = _STEP_DIR.match(step.name)
        if step.is_dir() and match:
            location = f"{relative}/{STEPS_DIR}/{step.name}"
            out.rows[STEPS].append(_step_row(run_id, step, match, events))
            out.rows[LLM_CALLS].extend(_llm_call_rows(run_id, step, location, out))
            out.rows[TOOL_CALLS].extend(_tool_call_rows(run_id, step, location, out))
    out.rows[APPROVALS].extend(_approval_rows(run_id, folder, relative, out))
    return out


def _run_row(
    folder: Path, run_id: str, manifest: Manifest | None, events: list[Event], out: _RunRows
) -> Row:
    finished = (folder / RESULT_FILE).exists()
    if finished:
        state = _read_state(folder / RESULT_FILE, f"runs/{run_id}/{RESULT_FILE}", out)
    else:
        state = _active_state(events)
    with_step = [event for event in events if event.step is not None]
    created = manifest.created_at if manifest else events[0].ts if events else None
    return {
        "run_id": run_id,
        "task_id": manifest.task_id if manifest else None,
        "project": manifest.project if manifest else None,
        "state": state,
        "finished": finished,
        "current_step": with_step[-1].step if with_step else None,
        "created_at": created.isoformat() if created else None,
        "last_event_at": events[-1].ts.isoformat() if events else None,
    }


def _active_state(events: list[Event]) -> str:
    """`parked` after a `run_parked` event that no `run_resumed` followed, else `running`."""
    marks = [event.kind for event in events if event.kind in RUN_STATE_EVENTS]
    return "parked" if marks and marks[-1] == "run_parked" else "running"


def _step_row(run_id: str, step: Path, match: re.Match[str], events: list[Event]) -> Row:
    seq, name, attempt = int(match.group(1)), match.group(2), int(match.group(3))
    outcomes = [
        e.outcome
        for e in events
        if e.step == name and e.attempt == attempt and e.outcome is not None
    ]
    return {
        "run_id": run_id,
        "dir": step.name,
        "seq": seq,
        "name": name,
        "attempt": attempt,
        "outcome": outcomes[-1] if outcomes else None,
        "has_artifact": (step / ARTIFACT_FILE).exists(),
    }


def _llm_call_rows(run_id: str, step: Path, location: str, out: _RunRows) -> list[Row]:
    calls = _read_records(step / LLM_CALLS_FILE, LLMCall, f"{location}/{LLM_CALLS_FILE}", out)
    return [
        {
            "run_id": run_id,
            "step_dir": step.name,
            "seq": seq,
            "ts": call.ts.isoformat(),
            "model": call.model,
            "input_tokens": call.input_tokens,
            "output_tokens": call.output_tokens,
            "cache_read_input_tokens": call.cache_read_input_tokens,
            "cache_creation_input_tokens": call.cache_creation_input_tokens,
            "cost_usd": call.cost_usd,
            "stop_reason": call.stop_reason,
        }
        for seq, call in enumerate(calls, start=1)
    ]


def _tool_call_rows(run_id: str, step: Path, location: str, out: _RunRows) -> list[Row]:
    calls = _read_records(step / TOOL_CALLS_FILE, ToolCall, f"{location}/{TOOL_CALLS_FILE}", out)
    return [
        {
            "run_id": run_id,
            "step_dir": step.name,
            "seq": seq,
            "ts": call.ts.isoformat(),
            "tool_use_id": call.tool_use_id,
            "tool": call.tool,
            "arguments": json.dumps(call.arguments, sort_keys=True),
            "tool_class": call.tool_class,
            "decision": call.decision,
            "rule_id": call.rule_id,
            "error": call.error,
        }
        for seq, call in enumerate(calls, start=1)
    ]


def _approval_rows(run_id: str, folder: Path, relative: str, out: _RunRows) -> list[Row]:
    """One row per approval id; a later line (the decision) supersedes an earlier one."""
    latest: dict[str, Approval] = {}
    for approval in _read_records(
        folder / APPROVALS_FILE, Approval, f"{relative}/{APPROVALS_FILE}", out
    ):
        latest[approval.id] = approval
    return [
        {
            "run_id": run_id,
            "id": approval.id,
            "type": approval.type,
            "asked": approval.asked,
            "created_at": approval.created_at.isoformat(),
            "answer": approval.answer,
            "note": approval.note,
            "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
        }
        for approval in latest.values()
    ]


def _read_manifest(path: Path, label: str, out: _RunRows) -> Manifest | None:
    if not path.exists():
        return None
    try:
        return Manifest.from_json(path.read_text(encoding="utf-8"))
    except (ValidationError, ValueError) as error:
        out.problems.append(f"{label}: {_one_line(error)}")
        return None


def _read_state(path: Path, label: str, out: _RunRows) -> str:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        out.problems.append(f"{label}: {_one_line(error)}")
        return "finished"
    state = result.get("state") if isinstance(result, dict) else None
    return str(state) if state else "finished"


def _read_records[M: Event | LLMCall | ToolCall | Approval](
    path: Path, model: type[M], label: str, out: _RunRows
) -> list[M]:
    """The valid records of a JSONL file; each invalid line becomes a problem."""
    if not path.exists():
        return []
    records: list[M] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                records.append(model.from_jsonl_line(line))
            except (ValidationError, ValueError) as error:
                out.problems.append(f"{label}: line {number}: {_one_line(error)}")
    return records


def _one_line(error: Exception) -> str:
    """A validation error as `field: message; field: message`, anything else as one line."""
    if isinstance(error, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or 'record'}: {item['msg']}"
            for item in error.errors()
        )
    return " ".join(str(error).split())
