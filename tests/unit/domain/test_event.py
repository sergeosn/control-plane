"""Task 2: run events round-trip through `events.jsonl`."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from control_plane.domain import Event

TS = datetime(2026, 11, 17, 9, 0, 0, 123456, tzinfo=UTC)


@pytest.mark.parametrize(
    "event",
    [
        Event(ts=TS, run_id="R-1", kind="run_started"),
        Event(ts=TS, run_id="R-1", kind="step_finished", step="implement", attempt=2, outcome="ok"),
        Event(
            ts=TS,
            run_id="R-1",
            kind="approval_raised",
            step="merge_gate",
            attempt=1,
            message="Merge approval A-0001 raised",
        ),
    ],
    ids=["bare", "step-outcome", "message"],
)
def test_event_round_trips_through_jsonl(event: Event) -> None:
    line = event.to_jsonl_line()

    assert line.endswith("\n")
    assert "\n" not in line[:-1]
    assert Event.from_jsonl_line(line) == event


def test_optional_fields_default_to_none() -> None:
    event = Event(ts=TS, run_id="R-1", kind="run_started")

    assert event.step is None
    assert event.attempt is None
    assert event.outcome is None
    assert event.message is None


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValidationError, match="ts"):
        Event(ts=datetime(2026, 11, 17, 9, 0), run_id="R-1", kind="run_started")  # noqa: DTZ001


def test_extra_keys_are_rejected() -> None:
    with pytest.raises(ValidationError, match="payload"):
        Event.from_jsonl_line(
            '{"ts": "2026-11-17T09:00:00Z", "run_id": "R-1", "kind": "x", "payload": {}}\n'
        )
