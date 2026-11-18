"""The flow engine: the state machine with persistence and resume (docs/SPEC.md section 7)."""

import json
from pathlib import Path

import pytest

from control_plane.app.approvals import approve, decide, deny
from control_plane.domain import Approval
from control_plane.flow import FlowError, read_flow_state
from control_plane.store.runs import RESULT_FILE
from fixtures.flow_harness import (
    FAILING_GATE,
    OUT_OF_PLAN,
    CrashError,
    FakePorts,
    engine,
    make_task,
    plan_turn,
    review_turn,
    state_of,
    work_turn,
)
from fixtures.loop_harness import Harness, read_turn, text_turn, tool_use, turn


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path, "worker")


def pending(harness: Harness) -> list[Approval]:
    latest = {approval.id: approval for approval in harness.run.approvals()}
    return [approval for approval in latest.values() if not approval.decided]


def event_kinds(harness: Harness) -> list[str]:
    return [event.kind for event in harness.run.events()]


def transitions(harness: Harness) -> list[tuple[str | None, str | None, str | None]]:
    return [
        (event.step, event.outcome, event.message)
        for event in harness.run.events()
        if event.kind == "transition"
    ]


# --- a whole run ------------------------------------------------------------------


async def test_a_scripted_run_iterates_and_parks_at_the_merge_approval(harness: Harness) -> None:
    ports = FakePorts(
        harness,
        [
            plan_turn(),
            work_turn(),  # attempt 1: the gates fail
            work_turn(),  # attempt 2: the gates pass, the review fails
            review_turn(passed=False),
            work_turn(),  # attempt 3: the gates pass, the review passes
            review_turn(passed=True),
        ],
        gates=["fail", "pass", "pass"],
    )

    report = await engine(harness, ports).start(harness.run)

    assert report.status == "parked"
    assert report.step == "merge_gate"
    assert transitions(harness) == [
        ("plan", "ok", "plan_gate"),
        ("plan_gate", "approve", "implement"),
        ("implement", "ok", "checks"),
        ("checks", "fail", "implement"),
        ("implement", "ok", "checks"),
        ("checks", "pass", "review"),
        ("review", "fail", "implement"),
        ("implement", "ok", "checks"),
        ("checks", "pass", "review"),
        ("review", "pass", "merge_gate"),
    ]
    assert "step_skipped" in event_kinds(harness)
    assert event_kinds(harness)[-1] == "run_parked"

    [merge] = pending(harness)
    assert merge.type == "merge"
    assert report.approval_id == merge.id
    state = state_of(harness)
    assert state.status == "parked"
    assert state.iteration == 3
    assert state.attempts["implement"] == 3

    # What came back on failure reached the next attempt as its inputs.
    workers = [r for r in ports.agent_requests if r.role == "worker"]
    assert [w.attempt for w in workers] == [1, 2, 3]
    assert workers[0].inputs.gate_outputs == {}
    assert workers[1].inputs.gate_outputs == {FAILING_GATE: f"{FAILING_GATE}: fail\n"}
    assert workers[1].inputs.work_report is not None
    assert workers[1].inputs.review is None
    assert workers[2].inputs.review is not None
    assert workers[2].inputs.review.verdict == "fail"
    assert workers[2].inputs.gate_outputs == {}
    assert all(w.inputs.plan is not None for w in workers)

    reviewers = [r for r in ports.agent_requests if r.role == "reviewer"]
    assert reviewers[0].inputs.out_of_plan == tuple(OUT_OF_PLAN)
    assert set(reviewers[0].inputs.gate_outputs) == set(LANE_GATES)
    assert reviewers[0].inputs.work_report is not None

    # Every agent step has its own folder, numbered in order of entry.
    folders = sorted(p.name for p in (harness.run.path / "steps").iterdir())
    assert folders == [
        "01-plan-1",
        "02-implement-1",
        "04-implement-2",
        "06-review-1",
        "07-implement-3",
        "09-review-2",
    ]
    assert [r.attempt for r in ports.gate_requests] == [1, 2, 3]


LANE_GATES = [
    "format",
    "lint",
    "tests",
    "coverage",
    "test_integrity",
    "diff_size",
    "secrets",
    "guardrail_regression",
]


async def test_an_approved_merge_finishes_the_run(harness: Harness) -> None:
    ports = FakePorts(
        harness,
        [plan_turn(), work_turn(), review_turn(passed=True)],
        gates=["pass"],
        changed=["src/control_plane/policy/rules.py", "README.md"],
    )
    flow = engine(harness, ports, protected=["src/control_plane/policy/"])
    report = await flow.start(harness.run)
    [merge] = pending(harness)
    assert merge.evidence[0] == "protected: src/control_plane/policy/rules.py"

    await approve(harness.home, merge.id, clock=harness.clock)
    report = await engine(harness, ports, protected=["src/control_plane/policy/"]).resume(
        harness.run
    )

    assert report.status == "done"
    assert harness.run.finished
    result = json.loads((harness.run.path / RESULT_FILE).read_text())
    assert result["state"] == "done"
    assert [r.action for r in ports.action_requests] == ["merge_and_verify"]
    assert event_kinds(harness)[-1] == "run_finished"


async def test_a_denied_merge_returns_the_reason_to_the_worker(harness: Harness) -> None:
    ports = FakePorts(
        harness,
        [plan_turn(), work_turn(), review_turn(passed=True), work_turn(), review_turn(passed=True)],
        gates=["pass", "pass"],
    )
    await engine(harness, ports).start(harness.run)
    [merge] = pending(harness)
    await deny(harness.home, merge.id, reason="keep the old output too", clock=harness.clock)

    await engine(harness, ports).resume(harness.run)

    last_worker = [r for r in ports.agent_requests if r.role == "worker"][-1]
    assert last_worker.attempt == 2
    assert last_worker.inputs.denial_reason == "keep the old output too"


async def test_a_failed_merge_fails_the_run(harness: Harness) -> None:
    ports = FakePorts(
        harness,
        [plan_turn(), work_turn(), review_turn(passed=True)],
        gates=["pass"],
        action="error",
    )
    await engine(harness, ports).start(harness.run)
    await approve(harness.home, pending(harness)[0].id, clock=harness.clock)

    report = await engine(harness, ports).resume(harness.run)

    assert report.status == "failed"
    result = json.loads((harness.run.path / RESULT_FILE).read_text())
    assert result["state"] == "failed"
    assert result["reason"] == "merge error"


# --- iteration budget -------------------------------------------------------------


async def test_exceeding_max_iterations_parks_with_a_budget_approval(harness: Harness) -> None:
    ports = FakePorts(harness, [plan_turn(), work_turn()], gates=["fail"])
    task = make_task(max_iterations=1)

    report = await engine(harness, ports, task).start(harness.run)

    assert report.status == "parked"
    assert report.step == "implement"
    [budget] = pending(harness)
    assert budget.type == "budget"
    assert "max_iterations 1" in budget.asked
    assert state_of(harness).iteration == 2
    # The second attempt never started.
    assert [r.attempt for r in ports.agent_requests if r.role == "worker"] == [1]


async def test_an_extended_budget_runs_the_next_attempt(harness: Harness) -> None:
    ports = FakePorts(harness, [plan_turn(), work_turn(), work_turn()], gates=["fail", "fail"])
    task = make_task(max_iterations=1)
    await engine(harness, ports, task).start(harness.run)
    await decide(harness.home, pending(harness)[0].id, "extend", clock=harness.clock)

    report = await engine(harness, ports, task).resume(harness.run)

    assert [r.attempt for r in ports.agent_requests if r.role == "worker"] == [1, 2]
    assert state_of(harness).max_iterations == 2
    # The second attempt failed its gates too, so the run parks on the budget again.
    assert report.status == "parked"
    assert pending(harness)[0].type == "budget"


async def test_ending_the_run_on_a_budget_approval_fails_it(harness: Harness) -> None:
    ports = FakePorts(harness, [plan_turn(), work_turn()], gates=["fail"])
    task = make_task(max_iterations=1)
    await engine(harness, ports, task).start(harness.run)
    await decide(harness.home, pending(harness)[0].id, "end", clock=harness.clock)

    report = await engine(harness, ports, task).resume(harness.run)

    assert report.status == "failed"
    assert json.loads((harness.run.path / RESULT_FILE).read_text())["reason"] == (
        "budget approval ended the run"
    )


async def test_a_step_visited_too_often_parks_with_a_budget_approval(harness: Harness) -> None:
    # Every denied plan goes back to the planner; the plan step may be entered
    # max_iterations + 1 times.
    ports = FakePorts(harness, [plan_turn(), plan_turn()])
    task = make_task(plan_approval=True, max_iterations=1)
    flow = engine(harness, ports, task)
    await flow.start(harness.run)
    await deny(harness.home, pending(harness)[0].id, reason="smaller", clock=harness.clock)
    await engine(harness, ports, task).resume(harness.run)
    await deny(harness.home, pending(harness)[0].id, reason="smaller", clock=harness.clock)

    report = await engine(harness, ports, task).resume(harness.run)

    assert report.status == "parked"
    assert report.step == "plan"
    [budget] = pending(harness)
    assert budget.type == "budget"
    assert "step 'plan' was entered 3 times" in budget.asked


async def test_a_step_budget_breach_parks_and_an_extension_reruns_the_step(
    harness: Harness,
) -> None:
    ports = FakePorts(
        harness,
        [plan_turn(), read_turn("toolu_1"), read_turn("toolu_2")],
        gates=["pass"],
        step_budget={
            "max_turns": 1,
            "max_total_tokens": 1000,
            "max_cost_usd": 1.0,
            "max_wall_seconds": 60,
        },
    )
    report = await engine(harness, ports).start(harness.run)
    # The planner used its one turn and submitted; the worker read a file and breached.
    assert report.status == "parked"
    assert report.step == "implement"
    [budget] = pending(harness)
    assert budget.type == "budget"
    assert "budget_exceeded" in budget.asked

    await decide(harness.home, budget.id, "extend", clock=harness.clock)
    ports.client = type(ports.client)([work_turn(), review_turn(passed=True)])
    report = await engine(harness, ports).resume(harness.run)

    assert report.step == "merge_gate"
    workers = [r for r in ports.agent_requests if r.role == "worker"]
    assert [w.attempt for w in workers] == [1, 2]
    # A rerun is not a transition back into implement: the iteration counter is unchanged.
    assert state_of(harness).iteration == 1


async def test_a_step_that_fails_for_another_reason_fails_the_run(harness: Harness) -> None:
    ports = FakePorts(harness, [text_turn("I am done."), text_turn("Really done.")])

    report = await engine(harness, ports).start(harness.run)

    assert report.status == "failed"
    result = json.loads((harness.run.path / RESULT_FILE).read_text())
    assert result["reason"].startswith("step 'plan' failed: no_submit")


# --- conditions and protected paths ---------------------------------------------


async def test_a_false_condition_skips_the_step(harness: Harness) -> None:
    ports = FakePorts(harness, [plan_turn(), work_turn()], gates=["fail"])
    task = make_task(plan_approval=False, max_iterations=1)

    await engine(harness, ports, task).start(harness.run)

    skipped = [e for e in harness.run.events() if e.kind == "step_skipped"]
    assert [(e.step, e.outcome) for e in skipped] == [("plan_gate", "approve")]
    assert all(a.type != "plan" for a in harness.run.approvals())


async def test_a_true_condition_raises_the_plan_approval(harness: Harness) -> None:
    ports = FakePorts(harness, [plan_turn()])

    report = await engine(harness, ports, make_task(plan_approval=True)).start(harness.run)

    assert report.status == "parked"
    assert report.step == "plan_gate"
    [plan] = pending(harness)
    assert plan.type == "plan"
    assert plan.evidence == ["steps/01-plan-1/artifact.json"]


async def test_a_plan_touching_a_protected_path_forces_the_plan_approval(
    harness: Harness,
) -> None:
    files = ("src/control_plane/policy/rules.py", "tests/test_x.py")
    ports = FakePorts(harness, [plan_turn(files=files)])
    task = make_task(plan_approval=False)

    report = await engine(harness, ports, task, protected=["src/control_plane/policy/"]).start(
        harness.run
    )

    assert report.step == "plan_gate"
    [plan] = pending(harness)
    assert plan.type == "plan"
    assert plan.evidence[0] == "protected: src/control_plane/policy/rules.py"
    assert "step_skipped" not in event_kinds(harness)


async def test_a_plan_directory_that_holds_a_protected_path_forces_the_approval(
    harness: Harness,
) -> None:
    ports = FakePorts(harness, [plan_turn(files=("src/control_plane/",))])

    report = await engine(harness, ports, protected=["src/control_plane/policy/"]).start(
        harness.run
    )

    assert report.step == "plan_gate"
    assert pending(harness)[0].evidence[0] == "protected: src/control_plane/policy"


async def test_a_denied_plan_goes_back_to_the_planner_with_the_reason(harness: Harness) -> None:
    ports = FakePorts(harness, [plan_turn(), plan_turn()])
    task = make_task(plan_approval=True)
    await engine(harness, ports, task).start(harness.run)
    await deny(harness.home, pending(harness)[0].id, reason="too broad", clock=harness.clock)

    await engine(harness, ports, task).resume(harness.run)

    second = ports.agent_requests[-1]
    assert (second.step, second.attempt) == ("plan", 2)
    assert second.inputs.denial_reason == "too broad"
    assert second.inputs.previous_plan is not None


async def test_a_plan_with_questions_parks_and_the_answer_reaches_the_planner(
    harness: Harness,
) -> None:
    ports = FakePorts(harness, [plan_turn(questions=["Which format?"]), plan_turn()])
    task = make_task(plan_approval=True)
    await engine(harness, ports, task).start(harness.run)
    [question] = pending(harness)
    assert question.type == "question"
    assert "Which format?" in question.asked

    await decide(harness.home, question.id, "JSON lines", clock=harness.clock)
    report = await engine(harness, ports, task).resume(harness.run)

    assert report.step == "plan_gate"

    second = ports.agent_requests[-1]
    assert (second.step, second.attempt) == ("plan", 2)
    assert second.inputs.answers == "JSON lines"


# --- resume from inside every step kind -------------------------------------------


async def test_resume_inside_an_agent_step_parked_on_a_tool_call(harness: Harness) -> None:
    push = turn(tool_use("toolu_push", "run_command", argv=["git", "push"]))
    ports = FakePorts(harness, [plan_turn(), push], gates=["pass"])
    report = await engine(harness, ports).start(harness.run)
    assert (report.status, report.step) == ("parked", "implement")
    [call] = pending(harness)
    assert call.type == "tool_call"

    # Still undecided: nothing changes.
    before = (harness.run.path / "flow.json").read_bytes()
    report = await engine(harness, ports).resume(harness.run)
    assert report.status == "parked"
    assert (harness.run.path / "flow.json").read_bytes() == before

    await deny(harness.home, call.id, reason="no pushing", clock=harness.clock)
    ports.client = type(ports.client)([work_turn(), review_turn(passed=True)])
    report = await engine(harness, ports).resume(harness.run)

    assert report.step == "merge_gate"
    # Started, resumed while undecided, resumed after the decision: one attempt throughout.
    workers = [r for r in ports.agent_requests if r.role == "worker"]
    assert [w.attempt for w in workers] == [1, 1, 1]
    assert [w.resuming for w in workers] == [False, True, True]


async def test_resume_inside_an_agent_step_that_never_started(harness: Harness) -> None:
    ports = FakePorts(harness, [plan_turn(), work_turn(), review_turn(passed=True)], gates=["pass"])
    ports.crash = "agent"
    with pytest.raises(CrashError):
        await engine(harness, ports).start(harness.run)
    assert state_of(harness).active is not None

    ports.crash = None
    report = await engine(harness, ports).resume(harness.run)

    assert report.step == "merge_gate"
    assert ports.agent_requests[1].resuming


async def test_resume_after_a_crash_between_a_finished_step_and_its_transition(
    harness: Harness,
) -> None:
    ports = FakePorts(harness, [plan_turn(), work_turn(), review_turn(passed=True)], gates=["pass"])
    ports.crash = "gates_before"
    with pytest.raises(CrashError):
        await engine(harness, ports).start(harness.run)
    # Pretend the crash came after the worker finished and before the engine moved on:
    # rewind the flow state to the implement step, which is finished on disk.
    state = state_of(harness)
    implement_seq = 2
    rewound = state.model_copy(
        update={
            "current": "implement",
            "active": state.active.model_copy(
                update={"step": "implement", "seq": implement_seq, "attempt": 1}
            ),
        }
    )
    harness.run.write_json("flow.json", rewound)

    ports.crash = None
    report = await engine(harness, ports).resume(harness.run)

    assert report.step == "merge_gate"
    # The finished worker step was read from disk, not run again.
    assert len([r for r in ports.agent_requests if r.role == "worker"]) == 1


async def test_resume_inside_a_gate_step_that_had_not_written_its_results(
    harness: Harness,
) -> None:
    ports = FakePorts(harness, [plan_turn(), work_turn(), review_turn(passed=True)], gates=["pass"])
    ports.crash = "gates_before"
    with pytest.raises(CrashError):
        await engine(harness, ports).start(harness.run)

    ports.crash = None
    report = await engine(harness, ports).resume(harness.run)

    assert report.step == "merge_gate"
    assert [r.attempt for r in ports.gate_requests] == [1, 1]


async def test_resume_inside_a_gate_step_that_had_written_its_results(harness: Harness) -> None:
    ports = FakePorts(harness, [plan_turn(), work_turn(), review_turn(passed=True)], gates=["pass"])
    ports.crash = "gates_after"
    with pytest.raises(CrashError):
        await engine(harness, ports).start(harness.run)

    ports.crash = None
    report = await engine(harness, ports).resume(harness.run)

    assert report.step == "merge_gate"
    # The results on disk are used; the gates are not run again.
    assert len(ports.gate_requests) == 1


async def test_resume_inside_a_gate_step_with_a_partial_folder_uses_a_new_attempt(
    harness: Harness,
) -> None:
    ports = FakePorts(harness, [plan_turn(), work_turn(), review_turn(passed=True)], gates=["pass"])
    ports.crash = "gates_before"
    with pytest.raises(CrashError):
        await engine(harness, ports).start(harness.run)
    harness.run.write_bytes("gates/1/format.txt", b"format: half written\n")

    ports.crash = None
    await engine(harness, ports).resume(harness.run)

    assert [r.attempt for r in ports.gate_requests] == [1, 2]
    assert state_of(harness).gate_attempt == 2


async def test_resume_inside_an_approval_step(harness: Harness) -> None:
    ports = FakePorts(harness, [plan_turn(), work_turn(), review_turn(passed=True)], gates=["pass"])
    await engine(harness, ports, make_task(plan_approval=True)).start(harness.run)
    events_before = harness.run.events()
    report = await engine(harness, ports, make_task(plan_approval=True)).resume(harness.run)
    assert report.status == "parked"
    assert "undecided" in report.message
    assert harness.run.events() == events_before

    await approve(harness.home, pending(harness)[0].id, clock=harness.clock)
    await engine(harness, ports, make_task(plan_approval=True)).resume(harness.run)

    assert ("plan_gate", "approve", "implement") in transitions(harness)
    assert "run_resumed" in event_kinds(harness)


async def test_resume_inside_an_action_step_reports_the_interruption(harness: Harness) -> None:
    ports = FakePorts(harness, [plan_turn(), work_turn(), review_turn(passed=True)], gates=["pass"])
    await engine(harness, ports).start(harness.run)
    await approve(harness.home, pending(harness)[0].id, clock=harness.clock)
    ports.crash = "action"
    with pytest.raises(CrashError):
        await engine(harness, ports).resume(harness.run)

    ports.crash = None
    report = await engine(harness, ports).resume(harness.run)

    # The action is not run twice: its effect is unknown, so the run fails and says so.
    assert len(ports.action_requests) == 1
    assert report.status == "failed"
    reason = json.loads((harness.run.path / RESULT_FILE).read_text())["reason"]
    assert "interrupted" in reason


async def test_resume_between_steps(harness: Harness) -> None:
    ports = FakePorts(harness, [plan_turn(), work_turn(), review_turn(passed=True)], gates=["pass"])
    ports.crash = "gates_before"
    with pytest.raises(CrashError):
        await engine(harness, ports).start(harness.run)
    state = state_of(harness)
    harness.run.write_json("flow.json", state.model_copy(update={"active": None}))

    ports.crash = None
    report = await engine(harness, ports).resume(harness.run)

    assert report.step == "merge_gate"


# --- refusals ---------------------------------------------------------------------


async def test_a_finished_run_cannot_be_resumed(harness: Harness) -> None:
    ports = FakePorts(harness, [text_turn("no"), text_turn("no")])
    await engine(harness, ports).start(harness.run)
    with pytest.raises(FlowError, match="finished"):
        await engine(harness, ports).resume(harness.run)


async def test_a_run_cannot_be_started_twice(harness: Harness) -> None:
    ports = FakePorts(harness, [plan_turn(questions=["?"])])
    await engine(harness, ports).start(harness.run)
    with pytest.raises(FlowError, match="already started"):
        await engine(harness, ports).start(harness.run)


async def test_a_run_without_flow_state_cannot_be_resumed(harness: Harness) -> None:
    ports = FakePorts(harness, [])
    with pytest.raises(FlowError, match="no flow.json"):
        await engine(harness, ports).resume(harness.run)
    with pytest.raises(FlowError):
        read_flow_state(harness.run)


async def test_a_run_of_another_flow_cannot_be_resumed(harness: Harness) -> None:
    ports = FakePorts(harness, [plan_turn(questions=["?"])])
    await engine(harness, ports).start(harness.run)
    state = state_of(harness)
    harness.run.write_json("flow.json", state.model_copy(update={"flow": "other"}))
    with pytest.raises(FlowError, match="flow 'other'"):
        await engine(harness, ports).resume(harness.run)
