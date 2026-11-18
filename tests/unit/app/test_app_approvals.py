"""Task 9: the approval queue behind `cplane approvals`, `approve` and `deny` (docs/SPEC.md 12).

The queue is read from the `approvals.jsonl` of every run folder. A decision is appended to
the run's file first and mirrored to the index second; an index failure never undoes it.
"""

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from control_plane.app import approvals as app
from control_plane.domain import Approval
from control_plane.store.home import Home, create_home
from control_plane.store.index import Index
from control_plane.store.runs import RunFinishedError, RunFolder, RunStore

T0 = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)


class Clock:
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
def home(tmp_path: Path) -> Home:
    return create_home(tmp_path / "home")


@pytest.fixture
def store(home: Home, clock: Clock) -> RunStore:
    return RunStore(home, clock=clock)


def entry(run: RunFolder, number: int, type_: str = "tool_call", at: datetime = T0) -> Approval:
    approval = Approval(
        id=f"A-{run.run_id[2:]}-{number:02d}",
        run_id=run.run_id,
        type=type_,  # type: ignore[arg-type]
        asked=f"worker wants to call run_command git push ({number})",
        evidence=["steps/02-implement-1/transcript.jsonl"],
        created_at=at,
    )
    run.append_approval(approval)
    return approval


# --- listing the queue --------------------------------------------------------------


def test_an_empty_home_has_an_empty_queue(home: Home) -> None:
    assert app.list_queue(home) == []
    assert app.list_queue(home, include_decided=True) == []


def test_the_queue_spans_every_run_and_lists_pending_entries_oldest_first(
    home: Home, store: RunStore
) -> None:
    first, second = store.create("R-0001"), store.create("R-0002")
    late = entry(second, 1, at=T0 + timedelta(minutes=5))
    early = entry(first, 1, at=T0)
    decided = entry(first, 2, at=T0 + timedelta(minutes=1))
    first.append_approval(decided.decide("approve", at=T0 + timedelta(minutes=2)))

    assert app.list_queue(home) == [early, late]
    assert [a.id for a in app.list_queue(home, include_decided=True)] == [
        early.id,
        decided.id,
        late.id,
    ]
    assert app.list_queue(home, include_decided=True)[1].answer == "approve"


def test_the_queue_ignores_a_run_folder_without_approvals(home: Home, store: RunStore) -> None:
    store.create("R-0001")

    assert app.list_queue(home) == []


def test_find_returns_the_latest_line_of_an_entry(home: Home, store: RunStore) -> None:
    run = store.create("R-0001")
    original = entry(run, 1)
    run.append_approval(original.decide("deny", note="no", at=T0 + timedelta(minutes=1)))

    found = app.find_approval(home, original.id)

    assert found.answer == "deny" and found.note == "no"


def test_find_rejects_an_unknown_id(home: Home) -> None:
    with pytest.raises(app.ApprovalNotFoundError, match="A-0001-09"):
        app.find_approval(home, "A-0001-09")


# --- deciding -------------------------------------------------------------------------


async def test_approve_appends_the_decision_and_mirrors_it_to_the_index(
    home: Home, store: RunStore, clock: Clock
) -> None:
    run = store.create("R-0001")
    original = entry(run, 1)

    decided = await app.approve(home, original.id, note="Looks fine.", clock=clock)

    assert decided.answer == "approve" and decided.note == "Looks fine."
    assert decided.decided_at == T0
    assert run.approvals() == [original, decided]
    assert app.list_queue(home) == []
    event = run.events()[-1]
    assert event.kind == "approval_decided" and event.outcome == "approve"
    assert event.message is not None and original.id in event.message
    async with Index(home) as index:
        [row] = await index.approvals()
    assert (row["id"], row["answer"], row["note"]) == (original.id, "approve", "Looks fine.")


async def test_deny_records_the_reason(home: Home, store: RunStore, clock: Clock) -> None:
    run = store.create("R-0001")
    original = entry(run, 1)

    decided = await app.deny(home, original.id, reason="Not from the worker.", clock=clock)

    assert decided.answer == "deny" and decided.note == "Not from the worker."
    assert run.approvals()[-1] == decided


async def test_deny_without_a_reason_is_rejected_before_anything_is_written(
    home: Home, store: RunStore
) -> None:
    run = store.create("R-0001")
    original = entry(run, 1)

    with pytest.raises(app.ApprovalError, match="reason"):
        await app.deny(home, original.id, reason="")

    assert run.approvals() == [original]


async def test_a_decided_entry_cannot_be_decided_again(
    home: Home, store: RunStore, clock: Clock
) -> None:
    run = store.create("R-0001")
    original = entry(run, 1)
    await app.approve(home, original.id, clock=clock)

    with pytest.raises(app.ApprovalDecidedError, match="approve"):
        await app.deny(home, original.id, reason="changed my mind", clock=clock)

    assert len(run.approvals()) == 2


async def test_an_unknown_id_cannot_be_decided(home: Home, clock: Clock) -> None:
    with pytest.raises(app.ApprovalNotFoundError):
        await app.approve(home, "A-0001-01", clock=clock)


async def test_approve_and_deny_send_extend_and_end_for_a_budget(
    home: Home, store: RunStore, clock: Clock
) -> None:
    run = store.create("R-0001")
    extended, ended = entry(run, 1, type_="budget"), entry(run, 2, type_="budget")

    assert (await app.approve(home, extended.id, clock=clock)).answer == "extend"
    decided = await app.deny(home, ended.id, reason="too costly", clock=clock)
    assert (decided.answer, decided.note) == ("end", "too costly")


async def test_approve_of_a_question_is_rejected_and_names_answer(
    home: Home, store: RunStore, clock: Clock
) -> None:
    run = store.create("R-0001")
    original = entry(run, 1, type_="question")

    with pytest.raises(app.ApprovalError, match="cplane answer"):
        await app.approve(home, original.id, clock=clock)
    with pytest.raises(app.ApprovalError, match="cplane answer"):
        await app.deny(home, original.id, reason="no", clock=clock)

    assert run.approvals() == [original]


async def test_answer_records_text_on_a_question_only(
    home: Home, store: RunStore, clock: Clock
) -> None:
    run = store.create("R-0001")
    question, call = entry(run, 1, type_="question"), entry(run, 2)

    assert (await app.answer(home, question.id, "Use --json.", clock=clock)).answer == (
        "Use --json."
    )
    with pytest.raises(app.ApprovalError, match="tool_call"):
        await app.answer(home, call.id, "approve", clock=clock)
    with pytest.raises(app.ApprovalError, match="answer"):
        await app.answer(home, question.id, "", clock=clock)

    assert len(run.approvals()) == 3


async def test_decide_takes_any_answer_a_question_accepts(
    home: Home, store: RunStore, clock: Clock
) -> None:
    run = store.create("R-0001")
    original = entry(run, 1, type_="question")

    decided = await app.decide(home, original.id, "Use --json.", clock=clock)

    assert decided.answer == "Use --json."


async def test_an_entry_of_a_finished_run_cannot_be_decided(
    home: Home, store: RunStore, clock: Clock
) -> None:
    run = store.create("R-0001")
    original = entry(run, 1)
    run.finish({"state": "failed"})

    with pytest.raises(RunFinishedError):
        await app.approve(home, original.id, clock=clock)


async def test_an_index_failure_does_not_undo_the_decision(
    home: Home,
    store: RunStore,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    run = store.create("R-0001")
    original = entry(run, 1)

    async def broken(self: Index, run_id: str) -> list[str]:
        raise OSError("disk full")

    monkeypatch.setattr(Index, "sync_run", broken)

    with caplog.at_level(logging.ERROR):
        decided = await app.approve(home, original.id, clock=clock)

    assert run.approvals()[-1] == decided
    assert "index write for run R-0001 failed" in caplog.text


async def test_decide_leaves_the_index_closed(home: Home, store: RunStore, clock: Clock) -> None:
    run = store.create("R-0001")
    original = entry(run, 1)

    await app.approve(home, original.id, clock=clock)

    index = Index(home)
    try:
        assert await index.open() is False
    finally:
        await index.close()
