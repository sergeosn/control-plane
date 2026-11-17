"""Task 3: JSON files are written atomically and JSONL files are append-only (SPEC.md 5)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from control_plane.domain import Event
from control_plane.store import files
from control_plane.store.files import append_jsonl, read_jsonl, write_json_atomic

TS = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)


def test_write_json_atomic_writes_a_model_as_indented_json_with_a_newline(tmp_path: Path) -> None:
    target = tmp_path / "event.json"
    event = Event(ts=TS, run_id="R-1", kind="run_started")

    write_json_atomic(target, event)

    assert target.read_text() == event.to_json()
    assert target.read_text().endswith("}\n")


def test_write_json_atomic_accepts_a_mapping(tmp_path: Path) -> None:
    target = tmp_path / "result.json"

    write_json_atomic(target, {"state": "done", "cost_usd": 1.5})

    assert json.loads(target.read_text()) == {"state": "done", "cost_usd": 1.5}
    assert target.read_text().endswith("\n")


def test_write_json_atomic_replaces_an_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    write_json_atomic(target, {"n": 1})

    write_json_atomic(target, {"n": 2})

    assert json.loads(target.read_text()) == {"n": 2}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_failure_between_temporary_write_and_rename_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "manifest.json"

    def fail_rename(src: object, dst: object) -> None:
        raise OSError("disk went away")

    monkeypatch.setattr(files.os, "replace", fail_rename)

    with pytest.raises(OSError, match="disk went away"):
        write_json_atomic(target, {"run_id": "R-1"})

    assert not target.exists()
    assert list(tmp_path.iterdir()) == [], "a temporary file was left behind"


def test_failure_before_rename_keeps_the_previous_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    write_json_atomic(target, {"n": 1})

    def fail_rename(src: object, dst: object) -> None:
        raise OSError("disk went away")

    monkeypatch.setattr(files.os, "replace", fail_rename)

    with pytest.raises(OSError):
        write_json_atomic(target, {"n": 2})

    assert json.loads(target.read_text()) == {"n": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_append_jsonl_adds_one_line_per_call_and_keeps_earlier_lines(tmp_path: Path) -> None:
    target = tmp_path / "events.jsonl"
    first = Event(ts=TS, run_id="R-1", kind="run_started")
    second = Event(ts=TS, run_id="R-1", kind="step_started", step="plan", attempt=1)

    append_jsonl(target, first)
    append_jsonl(target, second)

    assert target.read_text() == first.to_jsonl_line() + second.to_jsonl_line()
    assert read_jsonl(target, Event) == [first, second]


def test_append_jsonl_accepts_a_preformatted_line(tmp_path: Path) -> None:
    target = tmp_path / "lines.jsonl"

    append_jsonl(target, '{"a": 1}\n')
    append_jsonl(target, '{"a": 2}')

    assert target.read_text() == '{"a": 1}\n{"a": 2}\n'


def test_append_jsonl_rejects_a_line_with_an_embedded_newline(tmp_path: Path) -> None:
    target = tmp_path / "lines.jsonl"

    with pytest.raises(ValueError, match="one line"):
        append_jsonl(target, '{"a":\n1}\n')

    assert not target.exists()


def test_read_jsonl_of_a_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_jsonl(tmp_path / "missing.jsonl", Event) == []
