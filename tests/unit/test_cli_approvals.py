"""Task 9: `cplane approvals`, `cplane approve`, `cplane deny` and `cplane answer` (docs/SPEC.md
sections 12 and 14)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from control_plane import cli
from control_plane.app.runs import RunOutcome
from control_plane.cli import main
from control_plane.domain import Approval
from control_plane.domain.approval import ApprovalType
from control_plane.store.home import Home, create_home
from control_plane.store.runs import RunFolder, RunStore

T0 = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)


@pytest.fixture
def home(tmp_path: Path) -> Home:
    return create_home(tmp_path / "home")


def park(
    home: Home,
    run_id: str = "R-0001",
    number: int = 1,
    at: datetime = T0,
    type_: ApprovalType = "tool_call",
) -> Approval:
    store = RunStore(home)
    run = store.open(run_id) if (home.runs / run_id).is_dir() else store.create(run_id)
    approval = Approval(
        id=f"A-{run_id[2:]}-{number:02d}",
        run_id=run_id,
        type=type_,
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


# --- the answer each type takes ------------------------------------------------------


@pytest.mark.parametrize("type_", ["plan", "tool_call", "merge"])
def test_approve_sends_approve_for_plan_tool_call_and_merge(
    home: Home, capsys: pytest.CaptureFixture[str], type_: ApprovalType
) -> None:
    park(home, type_=type_)

    assert main(["--home", str(home.path), "approve", "A-0001-01"]) == 0

    assert capsys.readouterr().out == "Approved A-0001-01 (run R-0001).\n"
    assert lines(RunStore(home).open("R-0001"))[-1].answer == "approve"


@pytest.mark.parametrize("type_", ["plan", "tool_call", "merge"])
def test_deny_sends_deny_for_plan_tool_call_and_merge(
    home: Home, capsys: pytest.CaptureFixture[str], type_: ApprovalType
) -> None:
    park(home, type_=type_)

    assert main(["--home", str(home.path), "deny", "A-0001-01", "--reason", "No."]) == 0

    assert capsys.readouterr().out == "Denied A-0001-01 (run R-0001): No.\n"
    decided = lines(RunStore(home).open("R-0001"))[-1]
    assert (decided.answer, decided.note) == ("deny", "No.")


def test_approve_extends_a_budget(home: Home, capsys: pytest.CaptureFixture[str]) -> None:
    park(home, type_="budget")

    code = main(["--home", str(home.path), "approve", "A-0001-01", "--note", "one more"])

    assert code == 0
    assert capsys.readouterr().out == "Extended the budget for A-0001-01 (run R-0001).\n"
    decided = lines(RunStore(home).open("R-0001"))[-1]
    assert (decided.answer, decided.note) == ("extend", "one more")


def test_deny_ends_a_budget(home: Home, capsys: pytest.CaptureFixture[str]) -> None:
    park(home, type_="budget")

    code = main(["--home", str(home.path), "deny", "A-0001-01", "--reason", "Too costly."])

    assert code == 0
    assert capsys.readouterr().out == "Ended A-0001-01 (run R-0001): Too costly.\n"
    decided = lines(RunStore(home).open("R-0001"))[-1]
    assert (decided.answer, decided.note) == ("end", "Too costly.")


def test_deny_of_a_budget_still_needs_a_reason(
    home: Home, capsys: pytest.CaptureFixture[str]
) -> None:
    park(home, type_="budget")

    code = main(["--home", str(home.path), "deny", "A-0001-01", "--reason", " "])

    assert code == 1
    assert "reason" in capsys.readouterr().err
    assert len(lines(RunStore(home).open("R-0001"))) == 1


def test_answer_records_the_text_for_a_question(
    home: Home, capsys: pytest.CaptureFixture[str]
) -> None:
    park(home, type_="question")

    code = main(["--home", str(home.path), "answer", "A-0001-01", "Keep the old flag too."])

    assert code == 0
    assert capsys.readouterr().out == "Answered A-0001-01 (run R-0001).\n"
    decided = lines(RunStore(home).open("R-0001"))[-1]
    assert decided.answer == "Keep the old flag too." and decided.note is None


def test_answer_needs_text(home: Home, capsys: pytest.CaptureFixture[str]) -> None:
    park(home, type_="question")

    code = main(["--home", str(home.path), "answer", "A-0001-01", "  "])

    assert code == 1
    assert "answer" in capsys.readouterr().err
    assert len(lines(RunStore(home).open("R-0001"))) == 1


@pytest.mark.parametrize("command", [["approve"], ["deny", "--reason", "no"]])
def test_approve_or_deny_of_a_question_points_to_answer(
    home: Home, capsys: pytest.CaptureFixture[str], command: list[str]
) -> None:
    park(home, type_="question")

    code = main(["--home", str(home.path), command[0], "A-0001-01", *command[1:]])

    assert code == 1
    err = capsys.readouterr().err
    assert err.startswith("cplane: ") and "question" in err
    assert 'cplane answer A-0001-01 "..."' in err
    assert len(lines(RunStore(home).open("R-0001"))) == 1


@pytest.mark.parametrize("type_", ["plan", "tool_call", "merge", "budget"])
def test_answer_of_a_decision_entry_points_to_approve_and_deny(
    home: Home, capsys: pytest.CaptureFixture[str], type_: ApprovalType
) -> None:
    park(home, type_=type_)

    code = main(["--home", str(home.path), "answer", "A-0001-01", "approve"])

    assert code == 1
    err = capsys.readouterr().err
    assert err.startswith("cplane: ") and type_ in err
    assert "cplane approve A-0001-01" in err and "cplane deny A-0001-01" in err
    assert len(lines(RunStore(home).open("R-0001"))) == 1


def test_answer_of_an_unknown_id_fails_with_a_message(
    home: Home, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--home", str(home.path), "answer", "A-0001-09", "text"]) == 1
    assert capsys.readouterr().err == "cplane: no approval A-0001-09 in the queue\n"


# --- the hint printed when a run parks ------------------------------------------------


def _parked(type_: ApprovalType | None) -> RunOutcome:
    return RunOutcome(
        run_id="R-0001",
        task_id="T-0001",
        status="parked",
        step="somewhere",
        message="waiting",
        approval_id="A-0001-01",
        approval_type=type_,
    )


@pytest.mark.parametrize("type_", ["plan", "tool_call", "merge"])
def test_the_park_hint_names_approve_and_deny(
    capsys: pytest.CaptureFixture[str], type_: ApprovalType
) -> None:
    cli._report(_parked(type_))

    hint = capsys.readouterr().out.splitlines()[-1]
    assert hint == (
        "Decide it with `cplane approve A-0001-01` or "
        "`cplane deny A-0001-01 --reason ...`, then `cplane resume R-0001`."
    )


def test_the_park_hint_for_a_budget_says_what_approve_and_deny_do(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli._report(_parked("budget"))

    hint = capsys.readouterr().out.splitlines()[-1]
    assert hint == (
        "Extend the budget with `cplane approve A-0001-01` or end the run with "
        "`cplane deny A-0001-01 --reason ...`, then `cplane resume R-0001`."
    )


def test_the_park_hint_for_a_question_names_answer(capsys: pytest.CaptureFixture[str]) -> None:
    cli._report(_parked("question"))

    hint = capsys.readouterr().out.splitlines()[-1]
    assert hint == 'Answer it with `cplane answer A-0001-01 "..."`, then `cplane resume R-0001`.'


def test_approvals_without_a_home_fails_with_a_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CPLANE_HOME", raising=False)

    assert main(["approvals"]) == 1
    assert "no home repository" in capsys.readouterr().err
