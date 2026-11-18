"""Task 6: the command runner (docs/SPEC.md section 10.2).

argv only, never a shell; the worktree as working directory; a timeout that kills the whole
process group; an environment built from an allowlist with a temporary HOME.
"""

import os
import sys
import time
from pathlib import Path

import pytest

from control_plane.tools import CommandRunner, ToolError

PY = sys.executable


@pytest.fixture
def runner(tmp_path: Path) -> CommandRunner:
    return CommandRunner(tmp_path)


async def test_runs_argv_and_captures_exit_code_stdout_and_stderr(runner: CommandRunner) -> None:
    code = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"

    result = await runner.run([PY, "-c", code], timeout=10)

    assert result.exit_code == 3
    assert result.stdout == "out\n"
    assert result.stderr == "err\n"
    assert result.timed_out is False


async def test_the_working_directory_is_the_worktree(runner: CommandRunner, tmp_path: Path) -> None:
    result = await runner.run([PY, "-c", "import os; print(os.getcwd())"], timeout=10)

    assert Path(result.stdout.strip()).resolve() == tmp_path.resolve()


async def test_argv_is_not_interpreted_by_a_shell(runner: CommandRunner) -> None:
    result = await runner.run(
        [PY, "-c", "import sys; print(sys.argv[1])", "$HOME; echo x"], timeout=10
    )

    assert result.stdout == "$HOME; echo x\n"


async def test_a_missing_executable_is_an_instructive_error(runner: CommandRunner) -> None:
    with pytest.raises(ToolError, match="no-such-binary-xyz"):
        await runner.run(["no-such-binary-xyz", "--version"], timeout=10)


async def test_a_command_that_exceeds_its_timeout_is_killed_with_its_children(
    runner: CommandRunner,
) -> None:
    code = (
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen([{PY!r}, '-c', 'import time; time.sleep(60)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()

    result = await runner.run([PY, "-c", code], timeout=0.5)

    assert result.timed_out is True
    assert time.monotonic() - started < 10
    child_pid = int(result.stdout.strip())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _is_alive(child_pid):
        time.sleep(0.05)
    assert not _is_alive(child_pid), "the grandchild survived the timeout"


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie is still listed by `kill -0`; read its state from `ps`.
    import subprocess

    probe = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return probe.stdout.strip() != "" and not probe.stdout.strip().startswith("Z")


async def test_a_subprocess_does_not_see_the_api_key(
    runner: CommandRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CPLANE_ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("CPLANE_LLM_AUTH_TOKEN", "bearer-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-other")

    result = await runner.run([PY, "-c", "import os; print(sorted(os.environ))"], timeout=10)

    assert "CPLANE_ANTHROPIC_API_KEY" not in result.stdout
    assert "CPLANE_LLM_AUTH_TOKEN" not in result.stdout
    assert "ANTHROPIC_API_KEY" not in result.stdout
    assert "sk-secret" not in result.stdout


async def test_a_subprocess_gets_a_temporary_home_that_is_removed_afterwards(
    runner: CommandRunner,
) -> None:
    result = await runner.run([PY, "-c", "import os; print(os.environ['HOME'])"], timeout=10)

    home = result.stdout.strip()
    assert home != os.environ.get("HOME")
    assert not Path(home).exists()


async def test_only_allowlisted_variables_reach_the_subprocess(
    runner: CommandRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CPLANE_TEST_UNLISTED", "1")

    result = await runner.run([PY, "-c", "import os; print(sorted(os.environ))"], timeout=10)

    assert "CPLANE_TEST_UNLISTED" not in result.stdout
    assert "PATH" in result.stdout


async def test_extra_env_is_passed_through(tmp_path: Path) -> None:
    runner = CommandRunner(tmp_path, extra_env={"CPLANE_TEST_EXTRA": "yes"})

    result = await runner.run(
        [PY, "-c", "import os; print(os.environ['CPLANE_TEST_EXTRA'])"], timeout=10
    )

    assert result.stdout == "yes\n"


async def test_output_is_decoded_leniently(runner: CommandRunner) -> None:
    code = "import sys; sys.stdout.buffer.write(b'ok \\xff\\n')"

    result = await runner.run([PY, "-c", code], timeout=10)

    assert result.stdout.startswith("ok ")


async def test_a_file_that_cannot_be_executed_is_an_instructive_error(
    runner: CommandRunner, tmp_path: Path
) -> None:
    script = tmp_path / "not-executable.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o644)

    with pytest.raises(ToolError, match="cannot start"):
        await runner.run([str(script)], timeout=10)
