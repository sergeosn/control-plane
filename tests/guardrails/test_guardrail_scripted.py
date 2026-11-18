"""Guardrail regression suite, through a scripted model (docs/SPEC.md sections 10 and 16).

`test_guardrail_hard_checks.py` and `test_guardrail_policy.py` drive the tool executor and the
policy directly. These cases script the model turn that attempts the same forbidden thing and
assert the outcome through `ApiLoopExecutor`, the way a real step meets it: what the model is
told, what was logged, and that nothing outside the rules happened on disk.
"""

import os
import sys
from pathlib import Path

import pytest

from control_plane.llm import ToolResultBlock
from fixtures.loop_harness import Harness, submit_turn, tool_results, tool_use, turn

PY = sys.executable


def secret_file(harness: Harness) -> Path:
    path = harness.worktree.parent / "secret.txt"
    path.write_text("do not read me\n")
    return path


def answers(client: object, call: int) -> list[ToolResultBlock]:
    """The tool results the model was sent before call number `call` (0-based)."""
    return tool_results(client.calls[call].messages[-1])  # type: ignore[attr-defined]


@pytest.fixture
def worker(tmp_path: Path) -> Harness:
    return Harness(tmp_path, "worker")


async def test_a_scripted_path_with_dotdot_or_outside_the_worktree_is_denied(
    worker: Harness,
) -> None:
    secret = secret_file(worker)
    script = [
        turn(
            tool_use("toolu_1", "read_file", path="../secret.txt"),
            tool_use("toolu_2", "read_file", path=str(secret)),
            tool_use("toolu_3", "write_file", path=str(secret), content="owned"),
        ),
        submit_turn("worker"),
    ]

    result, client = await worker.run_step(script)

    assert result.outcome == "ok"
    results = answers(client, 1)
    assert [r.is_error for r in results] == [True, True, True]
    assert all("do not read me" not in r.content for r in results)
    assert secret.read_text() == "do not read me\n"
    logged = {c.tool_use_id: c.error is not None for c in worker.tool_calls()}
    assert [logged[f"toolu_{n}"] for n in (1, 2, 3)] == [True, True, True]


async def test_a_scripted_symlink_pointing_outside_is_denied(worker: Harness) -> None:
    secret = secret_file(worker)
    os.symlink(secret, worker.worktree / "innocent.txt")
    script = [
        turn(
            tool_use("toolu_1", "read_file", path="innocent.txt"),
            tool_use("toolu_2", "write_file", path="innocent.txt", content="owned"),
        ),
        submit_turn("worker"),
    ]

    _, client = await worker.run_step(script)

    read, write = answers(client, 1)
    assert read.is_error and "do not read me" not in read.content
    assert write.is_error
    assert secret.read_text() == "do not read me\n"


@pytest.mark.parametrize("role", ["planner", "reviewer"])
async def test_a_scripted_planner_or_reviewer_write_is_denied(tmp_path: Path, role: str) -> None:
    harness = Harness(tmp_path, role)
    marker = harness.worktree / "marker.txt"
    script = [
        turn(
            tool_use("toolu_1", "write_file", path="marker.txt", content="x"),
            tool_use("toolu_2", "edit_file", path="ok.txt", old="fine", new="bad"),
            tool_use("toolu_3", "delete_file", path="ok.txt"),
            tool_use("toolu_4", "run_command", argv=[PY, "-c", "open('marker.txt', 'w')"]),
        ),
        submit_turn(role),
    ]

    result, client = await harness.run_step(script)

    assert result.outcome == "ok"
    assert all(r.is_error for r in answers(client, 1))
    decisions = {c.tool_use_id: c.decision for c in harness.tool_calls()}
    assert [decisions[f"toolu_{n}"] for n in (1, 2, 3, 4)] == ["deny"] * 4
    assert not marker.exists()
    assert (harness.worktree / "ok.txt").read_text() == "fine\n"
    assert harness.approvals() == []


async def test_a_scripted_unknown_command_parks_and_a_blocked_one_is_denied(
    worker: Harness,
) -> None:
    marker = worker.worktree / "marker.txt"
    script = [
        turn(
            tool_use("toolu_env", "run_command", argv=["env"]),
            tool_use("toolu_unknown", "run_command", argv=[PY, "-c", "open('marker.txt', 'w')"]),
        ),
        submit_turn("worker"),
    ]

    result, client = await worker.run_step(script)

    assert result.outcome == "parked"
    assert [c.tool_use_id for c in result.pending] == ["toolu_unknown"]
    decisions = {c.tool_use_id: (c.decision, c.rule_id) for c in worker.tool_calls()}
    assert decisions == {"toolu_env": ("deny", "never"), "toolu_unknown": ("park", None)}
    [approval] = worker.approvals()
    assert approval.type == "tool_call"
    assert len(client.calls) == 1
    assert not marker.exists()


async def test_a_scripted_subprocess_does_not_see_the_api_key(
    worker: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CPLANE_ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("CPLANE_LLM_AUTH_TOKEN", "gateway-secret")
    dump = turn(
        tool_use("toolu_dump", "run_command", argv=[PY, "-c", "import os; print(os.environ)"])
    )
    await worker.run_step([dump])
    [approval] = worker.approvals()
    worker.run.append_approval(approval.decide("approve", at=worker.clock()))

    result, client = await worker.resume_step([submit_turn("worker")])

    assert result.outcome == "ok"
    [output] = answers(client, 0)
    assert "exit code: 0" in output.content
    for secret in ("sk-ant-secret", "CPLANE_ANTHROPIC_API_KEY", "gateway-secret"):
        assert secret not in output.content
