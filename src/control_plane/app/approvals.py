"""The approval queue behind `cplane approvals`, `approve` and `deny` (SPEC.md section 12).

There is one queue, and it is the union of every run's `approvals.jsonl`: an entry is raised
as one line and decided as a second line with the same id, so the latest line of an id is
its current state. A decision is written to the run folder first and mirrored to the index
second; an index failure is logged and never undoes the file write (section 5).
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import ValidationError

from control_plane.domain import Approval
from control_plane.store.home import Home
from control_plane.store.index import Index
from control_plane.store.runs import RunStore

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]


def _wall_clock() -> datetime:
    return datetime.now(UTC)


class ApprovalError(Exception):
    """A decision could not be recorded. The message says why, in one line."""


class ApprovalNotFoundError(ApprovalError):
    """No run folder holds an entry with the id given."""


class ApprovalDecidedError(ApprovalError):
    """The entry already carries a decision; decisions are not revised."""


def _latest(home: Home) -> dict[str, Approval]:
    """The current line of every approval entry of every run."""
    store = RunStore(home)
    latest: dict[str, Approval] = {}
    for run_id in store.list_ids():
        for approval in store.open(run_id).approvals():
            latest[approval.id] = approval
    return latest


def list_queue(home: Home, *, include_decided: bool = False) -> list[Approval]:
    """The queue, oldest entry first; pending entries only unless `include_decided`."""
    entries = [a for a in _latest(home).values() if include_decided or not a.decided]
    return sorted(entries, key=lambda a: (a.created_at, a.id))


def find_approval(home: Home, approval_id: str) -> Approval:
    """The current line of the entry `approval_id`. Raises `ApprovalNotFoundError`."""
    try:
        return _latest(home)[approval_id]
    except KeyError:
        raise ApprovalNotFoundError(f"no approval {approval_id} in the queue") from None


async def decide(
    home: Home,
    approval_id: str,
    answer: str,
    *,
    note: str | None = None,
    clock: Clock = _wall_clock,
) -> Approval:
    """Record `answer` on a pending entry: the run's `approvals.jsonl` first, the index second.

    Raises `ApprovalNotFoundError`, `ApprovalDecidedError`, `ApprovalError` for an answer the
    entry's type does not take (or a deny without its reason), and `RunFinishedError` when
    the run folder is already immutable.
    """
    approval = find_approval(home, approval_id)
    if approval.decided:
        detail = f"{approval.answer}" + (f" ({approval.note})" if approval.note else "")
        raise ApprovalDecidedError(f"approval {approval_id} was already decided: {detail}")
    try:
        decided = approval.decide(answer, note=note, at=clock())
    except ValidationError as exc:
        raise ApprovalError(f"approval {approval_id}: {_one_line(exc)}") from exc
    run = RunStore(home).open(approval.run_id)
    run.append_approval(decided)
    run.log_event(
        "approval_decided",
        outcome=decided.answer,
        message=f"{approval_id}: {decided.note}" if decided.note else approval_id,
    )
    index = Index(home)
    await index.attempt(_mirror(index, run.run_id), f"run {run.run_id}")
    return decided


async def approve(
    home: Home, approval_id: str, *, note: str | None = None, clock: Clock = _wall_clock
) -> Approval:
    return await decide(home, approval_id, "approve", note=note, clock=clock)


async def deny(
    home: Home, approval_id: str, *, reason: str, clock: Clock = _wall_clock
) -> Approval:
    return await decide(home, approval_id, "deny", note=reason, clock=clock)


async def _mirror(index: Index, run_id: str) -> None:
    try:
        await index.open()
        await index.sync_run(run_id)
    finally:
        await index.close()


def _one_line(exc: ValidationError) -> str:
    return "; ".join(item["msg"].removeprefix("Value error, ") for item in exc.errors())
