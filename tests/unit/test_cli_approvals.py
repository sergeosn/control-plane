"""Task 9: `cplane approvals`, `cplane approve` and `cplane deny` (SPEC.md sections 12 and 14)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from control_plane.cli import main
from control_plane.domain import Approval
from control_plane.store.home import Home, create_home
from control_plane.store.runs import RunFolder, RunStore

T0 = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)


@pytest.fixture
def home(tmp_path: Path) -> Home:
    return create_home(tmp_path / "home")


def park(home: Home, run_id: str = "R-0001", number: int = 1, at: datetime = T0) -> Approval:
    store = RunStore(home)
    run = store.open(run_id) if (home.runs / run_id).is_dir() else store.create(run_id)
    approval = Approval(
        id=f"A-{run_id[2:]}-{number:02d}",
        run_id=run_id,
        type="tool_call",
        asked="worker wants to call run_command with git push: parked for approval",
        evidence=["steps/02-implement-1/transcript.jsonl"],
        created_at=at,
    )
    run.append_approval(approval)
    return approval


def lines(run: RunFolder) -> list[Approval]:
    return run.approvals()


# --- cplane approvals -------------------------------------------------------------


def test_approvals_with_an_empty_queue_says_so(
    home: Home, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--home", str(home.path), "approvals"])

    assert code == 0
    assert capsys.readouterr().out == "No pending approvals.\n"


def test_approvals_lists_pending_entries_with_id_run_type_time_and_question(
    home: Home, capsys: pytest.CaptureFixture[str]
) -> None:
    park(home, "R-0002", at=T0 + timedelta(minutes=1))
    park(home, "R-0001")

    code = main(["--home", str(home.path), "approvals"])

    assert code == 0
    assert capsys.readouterr().out.splitlines() == [
        "A-0001-01  R-0001  tool_call  2026-11-17T09:00:00+00:00  "
        "worker wants to call run_command with git push: parked for approval",
        "A-0002-01  R-0002  tool_call  2026-11-17T09:01:00+00:00  "
        "worker wants to call run_command with git push: parked for approval",
    ]


def test_approvals_all_includes_decided_entries_with_their_answer(
    home: Home, capsys: pytest.CaptureFixture[str]
) -> None:
    park(home)
    assert main(["--home", str(home.path), "approve", "A-0001-01", "--note", "fine"]) == 0
    capsys.readouterr()

    assert main(["--home", str(home.path), "approvals"]) == 0
    assert capsys.readouterr().out == "No pending approvals.\n"
    assert main(["--home", str(home.path), "approvals", "--all"]) == 0
    [line] = capsys.readouterr().out.splitlines()
    assert line.startswith("A-0001-01  R-0001  tool_call  2026-11-17T09:00:00+00:00  ")
    assert line.endswith("  [approve: fine]")


# --- cplane approve and deny --------------------------------------------------------


def test_approve_appends_the_decision_and_reports_it(
    home: Home, capsys: pytest.CaptureFixture[str]
) -> None:
    park(home)

    code = main(["--home", str(home.path), "approve", "A-0001-01", "--note", "Looks fine."])

    assert code == 0
    assert capsys.readouterr().out == "Approved A-0001-01 (run R-0001).\n"
    [original, decided] = lines(RunStore(home).open("R-0001"))
    assert not original.decided
    assert decided.answer == "approve" and decided.note == "Looks fine."
    assert decided.decided_at is not None and decided.decided_at.tzinfo is not None


def test_approve_without_a_note_is_fine(home: Home, capsys: pytest.CaptureFixture[str]) -> None:
    park(home)

    assert main(["--home", str(home.path), "approve", "A-0001-01"]) == 0
    assert lines(RunStore(home).open("R-0001"))[-1].note is None


def test_deny_records_the_reason_and_reports_it(
    home: Home, capsys: pytest.CaptureFixture[str]
) -> None:
    park(home)

    code = main(["--home", str(home.path), "deny", "A-0001-01", "--reason", "Not from here."])

    assert code == 0
    assert capsys.readouterr().out == "Denied A-0001-01 (run R-0001): Not from here.\n"
    decided = lines(RunStore(home).open("R-0001"))[-1]
    assert decided.answer == "deny" and decided.note == "Not from here."


def test_deny_requires_a_reason(home: Home, capsys: pytest.CaptureFixture[str]) -> None:
    park(home)

    with pytest.raises(SystemExit) as exit_info:
        main(["--home", str(home.path), "deny", "A-0001-01"])

    assert exit_info.value.code == 2
    assert "--reason" in capsys.readouterr().err
    assert len(lines(RunStore(home).open("R-0001"))) == 1


def test_deciding_an_unknown_id_fails_with_a_message(
    home: Home, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--home", str(home.path), "approve", "A-0001-09"])

    assert code == 1
    assert capsys.readouterr().err == "cplane: no approval A-0001-09 in the queue\n"


def test_deciding_twice_fails_with_a_message(
    home: Home, capsys: pytest.CaptureFixture[str]
) -> None:
    park(home)
    assert main(["--home", str(home.path), "approve", "A-0001-01"]) == 0
    capsys.readouterr()

    code = main(["--home", str(home.path), "deny", "A-0001-01", "--reason", "oops"])

    assert code == 1
    err = capsys.readouterr().err
    assert err.startswith("cplane: ") and "A-0001-01" in err and "approve" in err
    assert len(lines(RunStore(home).open("R-0001"))) == 2


def test_the_wrong_decision_for_a_type_fails_with_a_message(
    home: Home, capsys: pytest.CaptureFixture[str]
) -> None:
    run = RunStore(home).create("R-0001")
    run.append_approval(
        Approval(
            id="A-0001-01",
            run_id="R-0001",
            type="budget",
            asked="The step budget is exhausted.",
            evidence=[],
            created_at=T0,
        )
    )

    code = main(["--home", str(home.path), "approve", "A-0001-01"])

    assert code == 1
    err = capsys.readouterr().err
    assert err.startswith("cplane: ") and "budget" in err
    assert len(lines(run)) == 1


def test_approvals_without_a_home_fails_with_a_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CPLANE_HOME", raising=False)

    assert main(["approvals"]) == 1
    assert "no home repository" in capsys.readouterr().err
