"""The command runner behind `run_command` (SPEC.md section 10.2).

A command is an argv list and never goes through a shell. It runs in the worktree, in its
own session, so a timeout kills the whole process group and not only the direct child.
The environment is built from an allowlist with `HOME` set to a temporary directory, so the
API key and any other secret in the engine's environment never reach a subprocess.
"""

import asyncio
import os
import shutil
import signal
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from control_plane.tools.errors import ToolError

DEFAULT_ENV_ALLOWLIST: tuple[str, ...] = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ")

# How long to wait for the pipes to close after the process group was killed. A straggler
# that escaped the group and still holds the pipe does not keep the engine waiting.
_DRAIN_SECONDS = 2.0
# Bytes kept per stream. Output beyond this is read and discarded so the child never blocks.
_MAX_CAPTURE = 4 * 1024 * 1024


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float


class CommandRunner:
    def __init__(
        self,
        cwd: Path,
        *,
        env_allowlist: Sequence[str] = DEFAULT_ENV_ALLOWLIST,
        extra_env: Mapping[str, str] | None = None,
    ) -> None:
        self.cwd = cwd
        self._allowlist = tuple(env_allowlist)
        self._extra_env = dict(extra_env or {})

    def environment(self, home: str) -> dict[str, str]:
        env = {name: os.environ[name] for name in self._allowlist if name in os.environ}
        env["HOME"] = home
        env.update(self._extra_env)
        return env

    async def run(self, argv: Sequence[str], *, timeout: float) -> CommandResult:
        """Run `argv` and collect its output. Raises `ToolError` when it cannot be started."""
        argv = tuple(argv)
        home = tempfile.mkdtemp(prefix="cplane-home-")
        started = time.monotonic()
        try:
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=self.cwd,
                    env=self.environment(home),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise ToolError(
                    f"command not found: {argv[0]!r}; check the name or give its full path"
                ) from exc
            except OSError as exc:
                raise ToolError(f"cannot start {argv[0]!r}: {exc}") from exc

            assert process.stdout is not None and process.stderr is not None
            out, err = bytearray(), bytearray()
            pumps = (
                asyncio.ensure_future(_pump(process.stdout, out)),
                asyncio.ensure_future(_pump(process.stderr, err)),
            )
            timed_out = False
            try:
                await asyncio.wait_for(process.wait(), timeout)
            except TimeoutError:
                timed_out = True
                _kill_group(process)
                await process.wait()
            _, pending = await asyncio.wait(pumps, timeout=_DRAIN_SECONDS)
            for task in pending:
                task.cancel()
            return CommandResult(
                argv=argv,
                exit_code=None if timed_out else process.returncode,
                stdout=out.decode("utf-8", errors="replace"),
                stderr=err.decode("utf-8", errors="replace"),
                timed_out=timed_out,
                duration_s=time.monotonic() - started,
            )
        finally:
            shutil.rmtree(home, ignore_errors=True)


async def _pump(stream: asyncio.StreamReader, into: bytearray) -> None:
    while chunk := await stream.read(65536):
        if len(into) < _MAX_CAPTURE:
            into.extend(chunk)


def _kill_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        process.kill()
