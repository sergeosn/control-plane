"""Run folders (SPEC.md section 5).

A run folder is written while the run is active, committed to the home repository when the
run finishes, and immutable after that. `RunStore.verify_finished` reports any finished run
folder whose files differ from the committed version.
"""

import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from control_plane.domain import Approval, Event
from control_plane.domain.base import DomainModel
from control_plane.store.files import append_jsonl, read_jsonl, write_json_atomic
from control_plane.store.git import Git
from control_plane.store.home import Home

RESULT_FILE = "result.json"
MANIFEST_FILE = "manifest.json"
EVENTS_FILE = "events.jsonl"
APPROVALS_FILE = "approvals.jsonl"
STEPS_DIR = "steps"
RUN_SUBDIRS = ("context", STEPS_DIR, "gates")
ARTIFACT_FILE = "artifact.json"
LLM_CALLS_FILE = "llm_calls.jsonl"
TOOL_CALLS_FILE = "tool_calls.jsonl"
"""Per-step files: the artifact and the audit logs of section 10.1 (`domain.calls`)."""

Clock = Callable[[], datetime]


def _wall_clock() -> datetime:
    return datetime.now(UTC)


def new_run_id(now: datetime) -> str:
    """A run id that sorts by creation time: `R-YYYYMMDD-HHMMSS-xxxx`."""
    return f"R-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


class RunFinishedError(Exception):
    """A write was attempted on a run folder that is already finished."""


@dataclass(frozen=True, order=True)
class RunDiscrepancy:
    """A finished run folder that differs from its committed version."""

    run_id: str
    kind: str
    """`modified`, `deleted`, `added`, or `uncommitted` for a folder git never saw."""
    path: str
    """Relative to the run folder; empty for `uncommitted`."""


class RunFolder:
    def __init__(self, home: Home, run_id: str, clock: Clock) -> None:
        self.home = home
        self.run_id = run_id
        self._clock = clock

    @property
    def path(self) -> Path:
        return self.home.runs / self.run_id

    @property
    def finished(self) -> bool:
        return (self.path / RESULT_FILE).exists()

    def _target(self, relative: str) -> Path:
        self._refuse_if_finished()
        target = (self.path / relative).resolve()
        if not target.is_relative_to(self.path.resolve()):
            raise ValueError(f"{relative!r} is outside the run folder of {self.run_id}")
        return target

    def _refuse_if_finished(self) -> None:
        if self.finished:
            raise RunFinishedError(f"run {self.run_id} is finished and its folder is immutable")

    def write_json(self, relative: str, data: DomainModel | Mapping[str, Any]) -> None:
        write_json_atomic(self._target(relative), data)

    def append_jsonl(self, relative: str, record: DomainModel | str) -> None:
        append_jsonl(self._target(relative), record)

    def log_event(
        self,
        kind: str,
        *,
        step: str | None = None,
        attempt: int | None = None,
        outcome: str | None = None,
        message: str | None = None,
    ) -> Event:
        event = Event(
            ts=self._clock(),
            run_id=self.run_id,
            kind=kind,
            step=step,
            attempt=attempt,
            outcome=outcome,
            message=message,
        )
        self.append_jsonl(EVENTS_FILE, event)
        return event

    def events(self) -> list[Event]:
        return read_jsonl(self.path / EVENTS_FILE, Event)

    def append_approval(self, approval: Approval) -> None:
        self.append_jsonl(APPROVALS_FILE, approval)

    def approvals(self) -> list[Approval]:
        return read_jsonl(self.path / APPROVALS_FILE, Approval)

    def finish(self, result: DomainModel | Mapping[str, Any]) -> str:
        """Write `result.json`, commit the run folder to the home repository, return the sha."""
        self.write_json(RESULT_FILE, result)
        git = Git(self.home.path)
        git.add(f"runs/{self.run_id}")
        return git.commit(f"Finish run {self.run_id}")


class RunStore:
    def __init__(self, home: Home, clock: Clock = _wall_clock) -> None:
        self.home = home
        self._clock = clock

    def create(self, run_id: str) -> RunFolder:
        folder = RunFolder(self.home, run_id, self._clock)
        if folder.path.exists():
            raise FileExistsError(f"run {run_id} already exists at {folder.path}")
        for name in RUN_SUBDIRS:
            (folder.path / name).mkdir(parents=True)
        return folder

    def open(self, run_id: str) -> RunFolder:
        folder = RunFolder(self.home, run_id, self._clock)
        if not folder.path.is_dir():
            raise FileNotFoundError(f"no run {run_id} under {self.home.runs}")
        return folder

    def list_ids(self) -> list[str]:
        return sorted(entry.name for entry in self.home.runs.iterdir() if entry.is_dir())

    def verify_finished(self) -> list[RunDiscrepancy]:
        """Every way a finished run folder differs from what the home repository committed."""
        git = Git(self.home.path)
        found: list[RunDiscrepancy] = []
        for run_id in self.list_ids():
            if not self.open(run_id).finished:
                continue
            prefix = f"runs/{run_id}"
            if not git.tracked(prefix):
                found.append(RunDiscrepancy(run_id, "uncommitted", ""))
                continue
            found.extend(
                RunDiscrepancy(run_id, kind, path.removeprefix(prefix + "/"))
                for kind, path in git.status(prefix)
            )
        return sorted(found)
