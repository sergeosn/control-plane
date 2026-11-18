"""The approval queue behind `cplane approvals`, `approve`, `deny` and `answer` (docs/SPEC.md
section 12).

`approve` and `deny` send the positive and the negative answer of the entry's type: `approve`
and `deny` for `plan`, `tool_call` and `merge`, `extend` and `end` for `budget`. A `question`
takes free text through `answer` instead.

There is one queue, and it is the union of every run's `approvals.jsonl`: an entry is raised
as one line and decided as a second line with the same id, so the latest line of an id is
its current state. A decision is written to the run folder first and mirrored to the index
second; an index failure is logged and never undoes the file write (section 5).
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import ValidationError

from control_plane.domain import Approval, ApprovalType
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


POSITIVE: dict[ApprovalType, str] = {
    "plan": "approve",
    "tool_call": "approve",
    "merge": "approve",
    "budget": "extend",
}
"""What `approve` sends, per type."""

NEGATIVE: dict[ApprovalType, str] = {
    "plan": "deny",
    "tool_call": "deny",
    "merge": "deny",
    "budget": "end",
}
"""What `deny` sends, per type; its reason is kept as the note."""


def _decision_entry(home: Home, approval_id: str) -> Approval:
    approval = find_approval(home, approval_id)
    if approval.type == "question":
        raise ApprovalError(
            f"approval {approval_id} is a question; answer it with "
            f'`cplane answer {approval_id} "..."`'
        )
    return approval


async def approve(
    home: Home, approval_id: str, *, note: str | None = None, clock: Clock = _wall_clock
) -> Approval:
    """Send the positive answer of the entry's type: `approve`, or `extend` for a budget."""
    approval = _decision_entry(home, approval_id)
    return await decide(home, approval_id, POSITIVE[approval.type], note=note, clock=clock)


async def deny(
    home: Home, approval_id: str, *, reason: str, clock: Clock = _wall_clock
) -> Approval:
    """Send the negative answer of the entry's type: `deny`, or `end` for a budget."""
    approval = _decision_entry(home, approval_id)
    if not reason.strip():
        raise ApprovalError(f"approval {approval_id}: a deny needs its reason")
    return await decide(home, approval_id, NEGATIVE[approval.type], note=reason, clock=clock)


async def answer(
    home: Home, approval_id: str, text: str, *, clock: Clock = _wall_clock
) -> Approval:
    """Record `text` as the answer to a `question` entry."""
    approval = find_approval(home, approval_id)
    if approval.type != "question":
        raise ApprovalError(
            f"approval {approval_id} is a {approval.type} entry, not a question; decide it "
            f"with `cplane approve {approval_id}` or `cplane deny {approval_id} --reason ...`"
        )
    if not text.strip():
        raise ApprovalError(f"approval {approval_id}: an answer needs text")
    return await decide(home, approval_id, text, clock=clock)


async def _mirror(index: Index, run_id: str) -> None:
    try:
        await index.open()
        await index.sync_run(run_id)
    finally:
        await index.close()


def _one_line(exc: ValidationError) -> str:
    return "; ".join(item["msg"].removeprefix("Value error, ") for item in exc.errors())
