"""Task 2: approval queue entries (docs/SPEC.md section 12) round-trip through JSONL."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from control_plane.domain import Approval

CREATED = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
DECIDED = datetime(2026, 11, 17, 9, 30, tzinfo=UTC)


def pending(type_: str = "merge") -> Approval:
    return Approval(
        id="A-0001",
        run_id="R-20261117-0001",
        type=type_,  # type: ignore[arg-type]
        asked="Merge run/R-20261117-0001 into main?",
        evidence=["diff.patch", "gates/1/tests.txt"],
        created_at=CREATED,
    )


@pytest.mark.parametrize(
    "approval",
    [
        pending(),
        pending().decide("approve", note="Looks fine.", at=DECIDED),
        pending("question").decide("Use --json.", at=DECIDED),
        pending("budget").decide("extend", at=DECIDED),
    ],
    ids=["pending", "approved", "answered", "extended"],
)
def test_approval_round_trips_through_jsonl(approval: Approval) -> None:
    line = approval.to_jsonl_line()

    assert line.endswith("\n")
    assert "\n" not in line[:-1]
    assert Approval.from_jsonl_line(line) == approval


def test_pending_approval_has_no_decision() -> None:
    approval = pending()

    assert approval.decided is False
    assert approval.answer is None
    assert approval.note is None
    assert approval.decided_at is None


def test_decide_returns_a_decided_copy() -> None:
    before = pending()

    after = before.decide("deny", note="Not yet.", at=DECIDED)

    assert before.decided is False
    assert after.decided is True
    assert after.answer == "deny"
    assert after.note == "Not yet."
    assert after.decided_at == DECIDED


@pytest.mark.parametrize(
    ("type_", "answers"),
    [
        ("plan", ["approve", "deny"]),
        ("tool_call", ["approve", "deny"]),
        ("merge", ["approve", "deny"]),
        ("budget", ["extend", "end"]),
    ],
)
def test_each_type_accepts_its_decisions(type_: str, answers: list[str]) -> None:
    for answer in answers:
        note = "reason" if answer == "deny" else None
        assert pending(type_).decide(answer, note=note, at=DECIDED).answer == answer


@pytest.mark.parametrize(
    ("type_", "answer"),
    [("plan", "extend"), ("merge", "yes"), ("budget", "approve"), ("tool_call", "end")],
)
def test_wrong_decision_for_type_is_rejected(type_: str, answer: str) -> None:
    with pytest.raises(ValidationError, match="answer"):
        pending(type_).decide(answer, at=DECIDED)


def test_deny_requires_a_reason() -> None:
    with pytest.raises(ValidationError, match="note"):
        pending("plan").decide("deny", at=DECIDED)


def test_answer_without_timestamp_is_rejected() -> None:
    with pytest.raises(ValidationError, match="decided_at"):
        Approval(
            id="A-0001",
            run_id="R-1",
            type="merge",
            asked="?",
            evidence=[],
            created_at=CREATED,
            answer="approve",
        )


def test_timestamp_without_answer_is_rejected() -> None:
    with pytest.raises(ValidationError, match="answer"):
        Approval(
            id="A-0001",
            run_id="R-1",
            type="merge",
            asked="?",
            evidence=[],
            created_at=CREATED,
            decided_at=DECIDED,
        )


def test_unknown_type_is_rejected() -> None:
    with pytest.raises(ValidationError, match="type"):
        pending("vibes")
