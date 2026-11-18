"""Task 8: `ApiLoopExecutor` runs one step against the `LLMClient` protocol
(docs/SPEC.md 8.2 to 8.6).

Every case drives the loop with `ScriptedClient` and reads what the step folder holds.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from control_plane.agent import (
    REASON_BUDGET,
    REASON_INVALID_SUBMIT,
    REASON_LOOP,
    REASON_MAX_TOKENS,
    REASON_NO_SUBMIT,
    ApiLoopExecutor,
    Executor,
    StepContext,
    StepResult,
    submit_tool,
)
from control_plane.domain import Plan, Review, WorkReport
from control_plane.llm import Message, TextBlock
from control_plane.tools import ToolExecutor, ToolOutcome
from fixtures.loop_harness import (
    BRIEF,
    SYSTEM,
    VALID_PLAN,
    Clock,
    Harness,
    TickingClient,
    read_turn,
    submit_turn,
    text_turn,
    tool_results,
    tool_use,
    turn,
)


@pytest.fixture
def planner(tmp_path: Path) -> Harness:
    return Harness(tmp_path, "planner")


@pytest.fixture
def worker(tmp_path: Path) -> Harness:
    return Harness(tmp_path, "worker")


# --- happy path (8.2, 8.3, 8.4) -----------------------------------------------


def test_api_loop_executor_is_an_executor(planner: Harness) -> None:
    assert isinstance(planner.executor(TickingClient([], planner.clock, 0)), Executor)


async def test_a_scripted_happy_path_ends_in_a_validated_artifact_on_disk(
    planner: Harness,
) -> None:
    result, client = await planner.run_step([read_turn("toolu_01"), submit_turn("planner")])

    assert result == StepResult(
        outcome="ok",
        artifact=Plan.model_validate(VALID_PLAN),
        reason=None,
        message=result.message,
        counters=result.counters,
        pending=[],
    )
    assert Plan.from_json((planner.step_dir / "artifact.json").read_text()) == result.artifact
    assert client.remaining == 0
    assert result.counters.turns == 2
    assert result.counters.total_tokens == 20
    assert result.counters.cost_usd == pytest.approx(0.002)


async def test_the_brief_is_the_first_user_message_and_the_system_prompt_is_the_role_prompt(
    planner: Harness,
) -> None:
    _, client = await planner.run_step([submit_turn("planner")])

    first = client.calls[0]
    assert first.system == SYSTEM
    assert first.messages == [Message.user(BRIEF)]
    assert [tool.name for tool in first.tools] == [
        "read_file",
        "list_dir",
        "search",
        "write_file",
        "edit_file",
        "delete_file",
        "run_command",
        "submit_plan",
    ]


@pytest.mark.parametrize(
    ("role", "model"), [("planner", Plan), ("worker", WorkReport), ("reviewer", Review)]
)
def test_the_submit_tool_is_strict_and_generated_from_the_artifact_model(
    role: str, model: type[Any]
) -> None:
    definition = submit_tool(role)

    assert definition.strict
    assert definition.name.startswith("submit_")
    assert definition.input_schema["additionalProperties"] is False
    assert set(definition.input_schema["required"]) == set(model.model_fields)


@pytest.mark.parametrize("role", ["worker", "reviewer"])
async def test_every_role_ends_in_its_own_artifact(tmp_path: Path, role: str) -> None:
    harness = Harness(tmp_path, role)

    result, _ = await harness.run_step([submit_turn(role)])

    assert result.outcome == "ok"
    assert (harness.step_dir / "artifact.json").exists()
    assert type(result.artifact) is {"worker": WorkReport, "reviewer": Review}[role]


async def test_a_plan_with_questions_is_blocked(planner: Harness) -> None:
    payload = {**VALID_PLAN, "questions": ["Which database?"]}

    result, _ = await planner.run_step([submit_turn("planner", payload)])

    assert result.outcome == "blocked"
    assert isinstance(result.artifact, Plan)
    assert (planner.step_dir / "artifact.json").exists()


async def test_a_valid_submit_gets_a_tool_result_and_the_audit_logs_are_complete(
    planner: Harness,
) -> None:
    await planner.run_step([read_turn("toolu_01"), submit_turn("planner")])

    transcript = planner.transcript()
    assert [m.role for m in transcript] == ["user", "assistant", "user", "assistant", "user"]
    assert tool_results(transcript[-1])[0].tool_use_id == "toolu_submit"
    assert not tool_results(transcript[-1])[0].is_error
    llm_calls = planner.llm_calls()
    assert [c.stop_reason for c in llm_calls] == ["tool_use", "tool_use"]
    assert llm_calls[0].cost_usd == pytest.approx(0.001)
    tool_calls = planner.tool_calls()
    assert [(c.tool, c.decision) for c in tool_calls] == [
        ("read_file", "allow"),
        ("submit_plan", "allow"),
    ]
    usage = json.loads((planner.step_dir / "usage.json").read_text())
    assert usage["turns"] == 2 and usage["input_tokens"] == 20
    events = planner.run.events()
    assert events[0].kind == "step_started"
    assert events[-1].kind == "step_finished" and events[-1].outcome == "ok"
    assert all(e.step == "plan" and e.attempt == 1 for e in events)


# --- several tool_use blocks (8.2) --------------------------------------------


async def test_several_tool_use_blocks_produce_one_user_message_with_one_result_per_block(
    planner: Harness,
) -> None:
    response = turn(
        tool_use("toolu_a", "read_file", path="ok.txt"),
        TextBlock(text="and then"),
        tool_use("toolu_b", "read_file", path="missing.txt"),
        tool_use("toolu_c", "list_dir", path="."),
    )

    _, client = await planner.run_step([response, submit_turn("planner")])

    reply = client.calls[1].messages[-1]
    assert reply.role == "user"
    assert [block.type for block in reply.content] == ["tool_result"] * 3
    assert [block.tool_use_id for block in reply.content] == ["toolu_a", "toolu_b", "toolu_c"]
    assert [block.is_error for block in reply.content] == [False, True, False]
    assert "1\tfine" in reply.content[0].content
    assert client.calls[1].messages[-2] == Message.from_response(response)
    sent = client.calls[1].messages
    assert planner.transcript()[: len(sent)] == sent


async def test_a_denied_call_gets_an_error_result_and_does_not_run(planner: Harness) -> None:
    response = turn(tool_use("toolu_w", "write_file", path="new.txt", content="x"))

    _, client = await planner.run_step([response, submit_turn("planner")])

    [result] = tool_results(client.calls[1].messages[-1])
    assert result.is_error and "denied" in result.content and "planner" in result.content
    assert not (planner.worktree / "new.txt").exists()
    [call, _] = planner.tool_calls()
    assert call.decision == "deny" and call.error == result.content


async def test_a_failed_tool_records_its_error_in_the_tool_call_log(planner: Harness) -> None:
    await planner.run_step([read_turn("toolu_01", path="missing.txt"), submit_turn("planner")])

    call = planner.tool_calls()[0]
    assert call.decision == "allow"
    assert call.error is not None and "missing.txt" in call.error


# --- invalid submit payloads (8.3) --------------------------------------------


async def test_an_invalid_submit_is_an_error_with_the_validation_messages_and_repair_is_accepted(
    planner: Harness,
) -> None:
    broken = {k: v for k, v in VALID_PLAN.items() if k != "summary"}

    result, client = await planner.run_step(
        [submit_turn("planner", broken, "toolu_s1"), submit_turn("planner", tool_id="toolu_s2")]
    )

    [error] = tool_results(client.calls[1].messages[-1])
    assert error.tool_use_id == "toolu_s1" and error.is_error
    assert "summary" in error.content
    assert "submit_plan" in error.content
    assert result.outcome == "ok"
    assert planner.state().repairs == 1


async def test_the_third_invalid_submit_fails_the_step(planner: Harness) -> None:
    broken = {**VALID_PLAN, "steps": "not a list"}
    attempts = [submit_turn("planner", broken, f"toolu_s{i}") for i in range(1, 4)]

    result, client = await planner.run_step([*attempts, submit_turn("planner")])

    assert result.outcome == "failed"
    assert result.reason == REASON_INVALID_SUBMIT
    assert "steps" in result.message
    assert len(client.calls) == 3
    assert not (planner.step_dir / "artifact.json").exists()
    assert planner.state().status == "finished" and planner.state().reason == REASON_INVALID_SUBMIT


# --- end_turn without a submit call (8.2) --------------------------------------


async def test_end_turn_without_a_submit_is_nudged_once(planner: Harness) -> None:
    result, client = await planner.run_step(
        [text_turn("I think I am done."), submit_turn("planner")]
    )

    nudge = client.calls[1].messages[-1]
    assert nudge.role == "user"
    assert [b.type for b in nudge.content] == ["text"]
    assert "submit_plan" in nudge.content[0].text
    assert result.outcome == "ok"


async def test_a_second_end_turn_without_a_submit_fails_the_step(planner: Harness) -> None:
    result, client = await planner.run_step(
        [text_turn("done"), text_turn("really done"), submit_turn("planner")]
    )

    assert result.outcome == "failed"
    assert result.reason == REASON_NO_SUBMIT
    assert len(client.calls) == 2


# --- budgets (8.5) -------------------------------------------------------------


async def test_the_turn_budget_stops_the_step(planner: Harness) -> None:
    reads = [read_turn(f"toolu_{i}") for i in range(5)]

    result, client = await planner.run_step(reads, max_turns=2)

    assert result.outcome == "failed"
    assert result.reason == REASON_BUDGET
    assert "turn" in result.message
    assert len(client.calls) == 2


async def test_the_token_budget_stops_the_step(planner: Harness) -> None:
    reads = [read_turn(f"toolu_{i}") for i in range(5)]

    result, client = await planner.run_step(reads, max_total_tokens=25)

    assert result.outcome == "failed"
    assert result.reason == REASON_BUDGET
    assert "token" in result.message
    assert len(client.calls) == 3


async def test_the_cost_budget_stops_the_step(planner: Harness) -> None:
    reads = [read_turn(f"toolu_{i}") for i in range(5)]

    result, client = await planner.run_step(reads, max_cost_usd=0.0025)

    assert result.outcome == "failed"
    assert result.reason == REASON_BUDGET
    assert "cost" in result.message
    assert len(client.calls) == 3


async def test_the_wall_time_budget_stops_the_step(planner: Harness) -> None:
    reads = [read_turn(f"toolu_{i}") for i in range(5)]

    result, client = await planner.run_step(reads, tick_seconds=100, max_wall_seconds=250)

    assert result.outcome == "failed"
    assert result.reason == REASON_BUDGET
    assert "wall" in result.message
    assert len(client.calls) == 3
    assert result.counters.wall_seconds == pytest.approx(300)


async def test_the_80_percent_notice_arrives_inside_a_tool_result(planner: Harness) -> None:
    reads = [read_turn(f"toolu_{i}", path=f"f{i}.txt") for i in range(4)]
    for i in range(4):
        (planner.worktree / f"f{i}.txt").write_text("x\n")

    result, client = await planner.run_step([*reads, submit_turn("planner")], max_turns=5)

    assert result.outcome == "ok"
    before = tool_results(client.calls[3].messages[-1])[0].content
    at_80 = client.calls[4].messages[-1]
    assert [b.type for b in at_80.content] == ["tool_result"]
    notice = at_80.content[0].content
    assert "notice" not in before
    assert "turns" in notice and "80%" in notice and "1 turns left" in notice
    assert "80%" not in tool_results(planner.transcript()[-1])[0].content


async def test_a_budget_breach_is_recorded_in_state_and_events(planner: Harness) -> None:
    await planner.run_step([read_turn("toolu_0"), read_turn("toolu_1")], max_turns=1)

    state = planner.state()
    assert state.status == "finished" and state.outcome == "failed"
    assert state.reason == REASON_BUDGET
    assert planner.run.events()[-1].outcome == "failed"


# --- loop detection (8.5) ------------------------------------------------------


async def test_three_identical_calls_in_a_row_add_a_notice(planner: Harness) -> None:
    reads = [read_turn(f"toolu_{i}") for i in range(3)]

    _, client = await planner.run_step([*reads, submit_turn("planner")])

    second = tool_results(client.calls[2].messages[-1])[0].content
    third = tool_results(client.calls[3].messages[-1])[0].content
    assert "identical" not in second
    assert "identical" in third and "read_file" in third


async def test_five_identical_calls_in_a_row_fail_the_step(planner: Harness) -> None:
    reads = [read_turn(f"toolu_{i}") for i in range(6)]

    result, client = await planner.run_step(reads)

    assert result.outcome == "failed"
    assert result.reason == REASON_LOOP
    assert len(client.calls) == 5
    assert len(planner.tool_calls()) == 4


async def test_a_different_call_resets_the_repeat_count(planner: Harness) -> None:
    script = [
        read_turn("toolu_0"),
        read_turn("toolu_1"),
        turn(tool_use("toolu_2", "list_dir", path=".")),
        read_turn("toolu_3"),
        read_turn("toolu_4"),
        submit_turn("planner"),
    ]

    result, client = await planner.run_step(script)

    assert result.outcome == "ok"
    contents = [tool_results(c.messages[-1])[0].content for c in client.calls[1:]]
    assert not any("identical" in c for c in contents)


async def test_identical_calls_are_counted_across_blocks_of_one_response(planner: Harness) -> None:
    same = [tool_use(f"toolu_{i}", "read_file", path="ok.txt") for i in range(5)]

    result, _ = await planner.run_step([turn(*same)])

    assert result.outcome == "failed" and result.reason == REASON_LOOP


# --- stop reasons (8.2) --------------------------------------------------------


@pytest.mark.parametrize("stop_reason", ["refusal", "model_context_window_exceeded"])
async def test_refusal_and_context_window_exceeded_end_the_step_with_the_reason(
    planner: Harness, stop_reason: str
) -> None:
    result, client = await planner.run_step(
        [text_turn("", stop_reason=stop_reason), submit_turn("planner")]
    )

    assert result.outcome == "failed"
    assert result.reason == stop_reason
    assert len(client.calls) == 1
    assert planner.state().reason == stop_reason
    assert planner.llm_calls()[0].stop_reason == stop_reason


async def test_max_tokens_on_text_ends_the_step_with_the_reason(planner: Harness) -> None:
    result, client = await planner.run_step(
        [text_turn("very long", stop_reason="max_tokens"), submit_turn("planner")]
    )

    assert result.outcome == "failed"
    assert result.reason == REASON_MAX_TOKENS
    assert len(client.calls) == 1


async def test_max_tokens_on_an_incomplete_tool_use_is_retried_once_with_a_higher_limit(
    planner: Harness,
) -> None:
    cut = turn(tool_use("toolu_cut", "read_file", path="ok.txt"), stop_reason="max_tokens")

    result, client = await planner.run_step([cut, submit_turn("planner")], max_tokens=1000)

    assert result.outcome == "ok"
    assert client.calls[0].max_tokens is None
    assert client.calls[1].max_tokens == 2000
    assert client.calls[1].messages == client.calls[0].messages
    assert [c.stop_reason for c in planner.llm_calls()] == ["max_tokens", "tool_use"]
    assert [m.role for m in planner.transcript()] == ["user", "assistant", "user"]


async def test_a_second_max_tokens_on_a_tool_use_fails_the_step(planner: Harness) -> None:
    cut = turn(tool_use("toolu_cut", "read_file", path="ok.txt"), stop_reason="max_tokens")

    result, client = await planner.run_step([cut, cut, submit_turn("planner")])

    assert result.outcome == "failed"
    assert result.reason == REASON_MAX_TOKENS
    assert len(client.calls) == 2


async def test_pause_turn_continues_the_conversation(planner: Harness) -> None:
    result, client = await planner.run_step(
        [text_turn("working", stop_reason="pause_turn"), submit_turn("planner")]
    )

    assert result.outcome == "ok"
    assert client.calls[1].messages[-1].role == "assistant"


# --- persistence (8.6) ---------------------------------------------------------


async def test_transcript_and_state_are_current_at_every_model_call(planner: Harness) -> None:
    seen: list[tuple[list[Message], int, int]] = []
    harness = planner

    class Checking(TickingClient):
        async def complete(self, system: Any, messages: Any, tools: Any, **kw: Any) -> Any:
            state = harness.state()
            seen.append((harness.transcript(), state.counters.turns, len(harness.llm_calls())))
            return await super().complete(system, messages, tools, **kw)

    script = [read_turn("toolu_0"), read_turn("toolu_1"), submit_turn("planner")]
    client = Checking(script, planner.clock, 0)
    await planner.executor(client).run_step(planner.context())

    assert [turns for _, turns, _ in seen] == [0, 1, 2]
    assert [calls for _, _, calls in seen] == [0, 1, 2]
    assert [transcript for transcript, _, _ in seen] == [call.messages for call in client.calls]


async def test_transcript_and_state_are_current_after_each_tool_result(planner: Harness) -> None:
    class Crashing(ToolExecutor):
        async def execute(self, name: str, arguments: Any) -> ToolOutcome:
            if arguments.get("path") == "second.txt":
                raise RuntimeError("power cut")
            return await super().execute(name, arguments)

    response = turn(
        tool_use("toolu_a", "read_file", path="ok.txt"),
        tool_use("toolu_b", "read_file", path="second.txt"),
        tool_use("toolu_c", "read_file", path="ok.txt"),
    )
    tools = Crashing(planner.tools().jail, tools=planner.tools().tools)

    with pytest.raises(RuntimeError, match="power cut"):
        await planner.run_step([response], tools=tools)

    transcript = planner.transcript()
    assert [m.role for m in transcript] == ["user", "assistant", "user"]
    [done] = tool_results(transcript[-1])
    assert done.tool_use_id == "toolu_a"
    state = planner.state()
    assert [(c.tool_use_id, c.status) for c in state.calls] == [
        ("toolu_a", "finished"),
        ("toolu_b", "started"),
        ("toolu_c", "pending"),
    ]
    assert state.calls[0].content == done.content
    assert state.status == "running"


async def test_state_holds_the_budget_counters(planner: Harness) -> None:
    await planner.run_step([read_turn("toolu_0"), submit_turn("planner")], tick_seconds=5)

    state = planner.state()
    assert state.counters.turns == 2
    assert state.counters.total_tokens == 20
    assert state.counters.cost_usd == pytest.approx(0.002)
    assert state.counters.wall_seconds == pytest.approx(10)
    assert state.started_at == Clock().now


# --- parking (8.2, section 12: the loop's half of it) --------------------------


async def test_a_parked_call_stores_the_pending_call_and_returns_parked(worker: Harness) -> None:
    response = turn(
        tool_use("toolu_r", "read_file", path="ok.txt"),
        tool_use("toolu_p", "run_command", argv=["git", "push"]),
    )

    result, client = await worker.run_step([response, submit_turn("worker")])

    assert result.outcome == "parked"
    assert [c.tool_use_id for c in result.pending] == ["toolu_p"]
    assert len(client.calls) == 1
    state = worker.state()
    assert state.status == "parked"
    assert [(c.tool_use_id, c.status, c.decision) for c in state.calls] == [
        ("toolu_r", "finished", "allow"),
        ("toolu_p", "pending", "park"),
    ]
    transcript = worker.transcript()
    assert [b.tool_use_id for b in tool_results(transcript[-1])] == ["toolu_r"]
    assert worker.tool_calls()[1].decision == "park"
    assert worker.run.events()[-1].outcome == "parked"


# --- the step context --------------------------------------------------------


def test_the_step_context_names_its_folder(planner: Harness) -> None:
    assert planner.context().folder == "steps/01-plan-1"
    assert planner.context(attempt=2).folder == "steps/01-plan-2"


def test_the_executor_rejects_a_role_without_an_artifact(planner: Harness) -> None:
    context = planner.context()
    broken = StepContext(**{**context.__dict__, "role": "janitor"})

    with pytest.raises(ValueError, match="janitor"):
        ApiLoopExecutor.artifact_model(broken.role)
