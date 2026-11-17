"""The tool executor: runs one tool call and produces the body of its `tool_result`.

The executor is the last check before anything touches disk (SPEC.md section 10.1): it
refuses a tool it was not given, validates the arguments with the tool's Pydantic model,
resolves every path through the jail, and turns every failure into an `is_error` result
whose message says what went wrong and what to try (section 8.2). It never raises for a
bad call; the loop above it only ever sees a `ToolOutcome`.
"""

import fnmatch
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from control_plane.llm.types import ToolDefinition
from control_plane.policy.jail import PathDeniedError, PathJail
from control_plane.tools.commands import CommandRunner
from control_plane.tools.definitions import ALL_TOOLS, TOOL_INPUTS, tool_definitions
from control_plane.tools.errors import ToolError
from control_plane.tools.inputs import (
    DeleteFileInput,
    EditFileInput,
    ListDirInput,
    ReadFileInput,
    RunCommandInput,
    SearchInput,
    WriteFileInput,
)
from control_plane.tools.truncate import head_and_tail, numbered_lines

_SKIPPED_DIRS = frozenset({".git", "__pycache__", ".venv", "node_modules"})
_SEARCH_LINE_CHARS = 200


@dataclass(frozen=True)
class Limits:
    """Truncation and timeout limits. The defaults follow SPEC.md section 9."""

    read_lines: int = 400
    list_entries: int = 500
    search_matches: int = 200
    output_chars: int = 20_000
    command_timeout: float = 120.0
    max_command_timeout: float = 600.0


@dataclass(frozen=True)
class ToolOutcome:
    """What goes into the `tool_result` block: its content and whether it is an error."""

    content: str
    is_error: bool = False


class ToolExecutor:
    def __init__(
        self,
        jail: PathJail,
        *,
        tools: Iterable[str] = ALL_TOOLS,
        limits: Limits | None = None,
        runner: CommandRunner | None = None,
    ) -> None:
        self.jail = jail
        self.tools = tuple(tools)
        unknown = [name for name in self.tools if name not in TOOL_INPUTS]
        if unknown:
            raise KeyError(f"unknown tools: {', '.join(unknown)}")
        self.limits = limits or Limits()
        self.runner = runner or CommandRunner(jail.worktree)

    def definitions(self) -> list[ToolDefinition]:
        return tool_definitions(self.tools)

    async def execute(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        if name not in self.tools:
            return ToolOutcome(
                f"tool {name!r} is not available here; available tools: {', '.join(self.tools)}",
                is_error=True,
            )
        try:
            args = TOOL_INPUTS[name].model_validate(dict(arguments))
        except ValidationError as exc:
            return ToolOutcome(f"invalid arguments for {name}: {_format_errors(exc)}", True)
        try:
            content = await self._dispatch(name, args)
        except (PathDeniedError, ToolError) as exc:
            return ToolOutcome(str(exc), is_error=True)
        except OSError as exc:
            return ToolOutcome(f"{name} failed: {exc.strerror or exc}", is_error=True)
        return ToolOutcome(content)

    async def _dispatch(self, name: str, args: Any) -> str:
        match name:
            case "read_file":
                return self._read_file(args)
            case "list_dir":
                return self._list_dir(args)
            case "search":
                return self._search(args)
            case "write_file":
                return self._write_file(args)
            case "edit_file":
                return self._edit_file(args)
            case "delete_file":
                return self._delete_file(args)
            case _:
                return await self._run_command(args)

    # --- read tools -----------------------------------------------------------

    def _read_file(self, args: ReadFileInput) -> str:
        path = self.jail.resolve(args.path)
        shown = self.jail.display(path)
        if path.is_dir():
            raise ToolError(f"{shown} is a directory; use list_dir to see its entries")
        if not path.is_file():
            raise ToolError(f"{shown} does not exist; use list_dir to find the right path")
        lines = _read_text(path).splitlines()
        total = len(lines)
        if total == 0:
            return f"{shown} is an empty file"
        start = args.start_line or 1
        if start > total:
            raise ToolError(
                f"start_line {start} is past the end of {shown}, which has {total} lines"
            )
        end = min(args.end_line or total, total, start + self.limits.read_lines - 1)
        body = numbered_lines(lines[start - 1 : end], start=start)
        remaining = total - end
        if remaining > 0 and (args.end_line is None or args.end_line > end):
            noun = "line" if remaining == 1 else "lines"
            body += (
                f"\n[showing lines {start}-{end} of {total}; {remaining} {noun} not shown; "
                f"call again with start_line={end + 1} to continue]"
            )
        return body

    def _list_dir(self, args: ListDirInput) -> str:
        path = self.jail.resolve(args.path)
        shown = self.jail.display(path)
        if path.is_file():
            raise ToolError(f"{shown} is a file; use read_file to read it")
        if not path.is_dir():
            raise ToolError(f"{shown} does not exist; use list_dir on its parent to look")
        entries = sorted(path.iterdir(), key=lambda p: p.name)
        if not entries:
            return f"{shown} is an empty directory"
        names = [entry.name + ("/" if entry.is_dir() else "") for entry in entries]
        limit = self.limits.list_entries
        if len(names) > limit:
            dropped = len(names) - limit
            names = names[:limit] + [
                f"[{dropped} entries not shown of {len(names)}; list a subdirectory instead]"
            ]
        return "\n".join(names)

    def _search(self, args: SearchInput) -> str:
        try:
            pattern = re.compile(args.pattern)
        except re.error as exc:
            raise ToolError(
                f"invalid pattern {args.pattern!r}: {exc}; the pattern is a Python regular "
                "expression"
            ) from exc
        limit = self.limits.search_matches
        matches: list[str] = []
        dropped = 0
        for root in self.jail.roots:
            for path in _walk(root):
                shown = self.jail.display(path)
                if args.glob and not fnmatch.fnmatch(shown, args.glob):
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                for number, line in enumerate(text.splitlines(), 1):
                    if not pattern.search(line):
                        continue
                    if len(matches) >= limit:
                        dropped += 1
                        continue
                    if len(line) > _SEARCH_LINE_CHARS:
                        line = line[:_SEARCH_LINE_CHARS] + "..."
                    matches.append(f"{shown}:{number}:{line}")
        if not matches:
            return f"no matches for {args.pattern!r}"
        if dropped:
            matches.append(f"[{dropped} more matches not shown; narrow the pattern or add a glob]")
        return "\n".join(matches)

    # --- write tools ----------------------------------------------------------

    def _write_file(self, args: WriteFileInput) -> str:
        path = self.jail.resolve(args.path, write=True)
        shown = self.jail.display(path)
        if path.is_dir():
            raise ToolError(f"{shown} is a directory; give the path of a file")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args.content, encoding="utf-8")
        return f"wrote {len(args.content.encode('utf-8'))} bytes to {shown}"

    def _edit_file(self, args: EditFileInput) -> str:
        path = self.jail.resolve(args.path, write=True)
        shown = self.jail.display(path)
        if not path.is_file():
            raise ToolError(f"{shown} does not exist; use write_file to create a file")
        text = _read_text(path)
        count = text.count(args.old)
        if count == 0:
            raise ToolError(
                f"old text not found in {shown}; use read_file and copy the exact text, "
                "whitespace and indentation included"
            )
        if count > 1:
            raise ToolError(
                f"old text occurs {count} times in {shown}; include more surrounding lines "
                "so it is unique"
            )
        path.write_text(text.replace(args.old, args.new, 1), encoding="utf-8")
        return f"edited {shown}: replaced 1 occurrence"

    def _delete_file(self, args: DeleteFileInput) -> str:
        path = self.jail.resolve(args.path, write=True)
        shown = self.jail.display(path)
        if path.is_dir():
            raise ToolError(f"{shown} is a directory; delete_file removes single files only")
        if not path.is_file():
            raise ToolError(f"{shown} does not exist; nothing to delete")
        path.unlink()
        return f"deleted {shown}"

    # --- commands -------------------------------------------------------------

    async def _run_command(self, args: RunCommandInput) -> str:
        timeout = min(args.timeout or self.limits.command_timeout, self.limits.max_command_timeout)
        result = await self.runner.run(args.argv, timeout=timeout)
        limit = self.limits.output_chars
        output = (
            f"stdout:\n{_stream(result.stdout, limit)}\nstderr:\n{_stream(result.stderr, limit)}"
        )
        if result.timed_out:
            raise ToolError(
                f"command exceeded its timeout of {timeout:g}s and was killed together with "
                f"its child processes; output so far:\n{output}"
            )
        return f"exit code: {result.exit_code}\n{output}"


def _stream(text: str, limit: int) -> str:
    return head_and_tail(text, limit=limit) if text else "(empty)"


def _format_errors(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error["loc"]) or "input"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _walk(root: Path) -> Iterable[Path]:
    """Regular files under `root`, sorted, never through a symlink."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in _SKIPPED_DIRS and not os.path.islink(os.path.join(dirpath, name))
        )
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if not path.is_symlink() and path.is_file():
                yield path
