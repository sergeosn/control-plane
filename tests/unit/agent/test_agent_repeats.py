"""Task 8: the same tool with the same arguments three times in a row adds a notice, five
times fails the step (SPEC.md section 8.5)."""

from control_plane.agent import FAIL_AT, NOTICE_AT, RepeatTracker


def test_the_thresholds_are_three_and_five() -> None:
    assert (NOTICE_AT, FAIL_AT) == (3, 5)


def test_identical_calls_are_counted_in_a_row() -> None:
    tracker = RepeatTracker()

    counts = [tracker.observe("read_file", {"path": "a"}) for _ in range(3)]

    assert counts == [1, 2, 3]


def test_a_different_call_resets_the_count() -> None:
    tracker = RepeatTracker()
    tracker.observe("read_file", {"path": "a"})
    tracker.observe("read_file", {"path": "a"})

    assert tracker.observe("read_file", {"path": "b"}) == 1
    assert tracker.observe("read_file", {"path": "a"}) == 1


def test_argument_order_does_not_make_a_call_different() -> None:
    tracker = RepeatTracker()
    tracker.observe("read_file", {"path": "a", "start_line": 1})

    assert tracker.observe("read_file", {"start_line": 1, "path": "a"}) == 2


def test_the_tracker_round_trips_through_its_state() -> None:
    tracker = RepeatTracker()
    tracker.observe("search", {"pattern": "x"})
    tracker.observe("search", {"pattern": "x"})

    restored = RepeatTracker.model_validate(tracker.model_dump())

    assert restored.observe("search", {"pattern": "x"}) == 3
