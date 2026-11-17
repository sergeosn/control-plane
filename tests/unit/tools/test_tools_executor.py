"""Task 6: the tool executor runs one tool call and answers with a `tool_result` body.

A failed tool returns `is_error` with a message that says what went wrong and what to try
(SPEC.md section 8.2). Truncation is always announced (section 9).
"""

import os
import sys
from pathlib import Path

import pytest

from control_plane.policy import PathJail
from control_plane.tools import Limits, ToolExecutor, ToolOutcome

PY = sys.executable


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    root = tmp_path / "worktree"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("def a():\n    return 1\n")
    (root / "src" / "b.py").write_text("def b():\n    return 2\n")
    (root / "README.md").write_text("# readme\n")
    return root


@pytest.fixture
def context(tmp_path: Path) -> Path:
    folder = tmp_path / "run" / "context"
    folder.mkdir(parents=True)
    (folder / "BRIEF.md").write_text("# brief\n")
    return folder


@pytest.fixture
def executor(worktree: Path, context: Path) -> ToolExecutor:
    return ToolExecutor(PathJail(worktree, read_roots=[context]))


# --- dispatch ---------------------------------------------------------------


async def test_an_unknown_tool_is_an_error(executor: ToolExecutor) -> None:
    outcome = await executor.execute("fetch_url", {"url": "http://x"})

    assert outcome.is_error
    assert "fetch_url" in outcome.content


async def test_a_tool_not_exposed_to_this_executor_is_denied_even_when_well_formed(
    worktree: Path,
) -> None:
    executor = ToolExecutor(PathJail(worktree), tools=["read_file", "list_dir", "search"])

    outcome = await executor.execute("write_file", {"path": "x.txt", "content": "x"})

    assert outcome.is_error
    assert "write_file" in outcome.content
    assert not (worktree / "x.txt").exists()


async def test_invalid_arguments_are_reported_by_field(executor: ToolExecutor) -> None:
    outcome = await executor.execute("read_file", {"path": "src/a.py", "start_line": "ten"})

    assert outcome.is_error
    assert "start_line" in outcome.content


async def test_a_denied_path_is_an_error_result_not_an_exception(
    executor: ToolExecutor, tmp_path: Path
) -> None:
    outcome = await executor.execute("read_file", {"path": "../outside.txt"})

    assert outcome == ToolOutcome(content=outcome.content, is_error=True)
    assert ".." in outcome.content


async def test_definitions_match_the_exposed_tools(worktree: Path) -> None:
    executor = ToolExecutor(PathJail(worktree), tools=["read_file", "search"])

    assert [d.name for d in executor.definitions()] == ["read_file", "search"]


# --- read_file --------------------------------------------------------------


async def test_read_file_returns_numbered_lines(executor: ToolExecutor) -> None:
    outcome = await executor.execute("read_file", {"path": "src/a.py"})

    assert not outcome.is_error
    assert outcome.content == "1\tdef a():\n2\t    return 1"


async def test_read_file_accepts_a_range(executor: ToolExecutor, worktree: Path) -> None:
    (worktree / "n.txt").write_text("".join(f"l{i}\n" for i in range(1, 11)))

    outcome = await executor.execute("read_file", {"path": "n.txt", "start_line": 3, "end_line": 4})

    assert outcome.content == "3\tl3\n4\tl4"


async def test_read_file_caps_at_400_lines_and_says_how_many_were_dropped(
    worktree: Path,
) -> None:
    executor = ToolExecutor(PathJail(worktree))
    (worktree / "big.txt").write_text("".join(f"l{i}\n" for i in range(1, 1001)))

    outcome = await executor.execute("read_file", {"path": "big.txt"})

    body, _, notice = outcome.content.rpartition("\n")
    assert body.count("\n") == 399
    assert body.endswith("400\tl400")
    assert "600" in notice and "start_line" in notice


async def test_read_file_truncation_limit_is_configurable(worktree: Path) -> None:
    executor = ToolExecutor(PathJail(worktree), limits=Limits(read_lines=2))
    (worktree / "n.txt").write_text("a\nb\nc\nd\n")

    outcome = await executor.execute("read_file", {"path": "n.txt", "start_line": 2})

    assert outcome.content.startswith("2\tb\n3\tc\n")
    assert "1 line" in outcome.content


async def test_read_file_past_the_end_is_an_error_naming_the_length(
    executor: ToolExecutor,
) -> None:
    outcome = await executor.execute("read_file", {"path": "src/a.py", "start_line": 50})

    assert outcome.is_error
    assert "2 lines" in outcome.content


async def test_read_file_of_a_missing_file_says_so(executor: ToolExecutor) -> None:
    outcome = await executor.execute("read_file", {"path": "src/zzz.py"})

    assert outcome.is_error
    assert "src/zzz.py" in outcome.content
    assert "list_dir" in outcome.content


async def test_read_file_of_a_directory_says_to_use_list_dir(executor: ToolExecutor) -> None:
    outcome = await executor.execute("read_file", {"path": "src"})

    assert outcome.is_error
    assert "list_dir" in outcome.content


async def test_read_file_of_an_empty_file(executor: ToolExecutor, worktree: Path) -> None:
    (worktree / "empty.txt").write_text("")

    outcome = await executor.execute("read_file", {"path": "empty.txt"})

    assert not outcome.is_error
    assert "empty" in outcome.content


async def test_read_file_can_read_the_context_root(executor: ToolExecutor, context: Path) -> None:
    outcome = await executor.execute("read_file", {"path": str(context / "BRIEF.md")})

    assert outcome.content == "1\t# brief"


# --- list_dir ---------------------------------------------------------------


async def test_list_dir_marks_directories(executor: ToolExecutor) -> None:
    outcome = await executor.execute("list_dir", {"path": "."})

    assert outcome.content.splitlines() == ["README.md", "src/"]


async def test_list_dir_of_a_file_is_an_error(executor: ToolExecutor) -> None:
    outcome = await executor.execute("list_dir", {"path": "README.md"})

    assert outcome.is_error
    assert "read_file" in outcome.content


async def test_list_dir_is_capped_and_announces_it(worktree: Path) -> None:
    executor = ToolExecutor(PathJail(worktree), limits=Limits(list_entries=3))
    for i in range(5):
        (worktree / f"f{i}.txt").write_text("")

    outcome = await executor.execute("list_dir", {"path": "."})

    lines = outcome.content.splitlines()
    assert len(lines) == 4
    assert "4 entries not shown" in lines[-1]


async def test_list_dir_of_an_empty_directory(executor: ToolExecutor, worktree: Path) -> None:
    (worktree / "void").mkdir()

    outcome = await executor.execute("list_dir", {"path": "void"})

    assert not outcome.is_error
    assert "empty" in outcome.content


# --- search -----------------------------------------------------------------


async def test_search_reports_path_line_and_text(executor: ToolExecutor) -> None:
    outcome = await executor.execute("search", {"pattern": r"return \d"})

    assert outcome.content.splitlines() == ["src/a.py:2:    return 1", "src/b.py:2:    return 2"]


async def test_search_glob_narrows_the_files(executor: ToolExecutor) -> None:
    outcome = await executor.execute("search", {"pattern": "def", "glob": "*b.py"})

    assert outcome.content.splitlines() == ["src/b.py:1:def b():"]


async def test_search_with_no_match_says_so(executor: ToolExecutor) -> None:
    outcome = await executor.execute("search", {"pattern": "nothing-here"})

    assert not outcome.is_error
    assert "no match" in outcome.content


async def test_search_with_a_bad_pattern_is_an_error(executor: ToolExecutor) -> None:
    outcome = await executor.execute("search", {"pattern": "("})

    assert outcome.is_error
    assert "pattern" in outcome.content


async def test_search_caps_matches_and_says_how_many_were_dropped(worktree: Path) -> None:
    executor = ToolExecutor(PathJail(worktree), limits=Limits(search_matches=5))
    (worktree / "many.txt").write_text("hit\n" * 20)

    outcome = await executor.execute("search", {"pattern": "hit"})

    lines = outcome.content.splitlines()
    assert len(lines) == 6
    assert "15 more" in lines[-1]


async def test_search_skips_git_internals_binaries_and_symlinks_out(
    executor: ToolExecutor, worktree: Path, tmp_path: Path
) -> None:
    (worktree / ".git").mkdir()
    (worktree / ".git" / "HEAD").write_text("needle\n")
    (worktree / "blob.bin").write_bytes(b"needle\x00\xff\xfe")
    outside = tmp_path / "outside.txt"
    outside.write_text("needle\n")
    os.symlink(outside, worktree / "link.txt")

    outcome = await executor.execute("search", {"pattern": "needle"})

    assert "no match" in outcome.content


async def test_search_covers_the_context_root_too(executor: ToolExecutor, context: Path) -> None:
    outcome = await executor.execute("search", {"pattern": "brief"})

    assert outcome.content == f"{context.resolve() / 'BRIEF.md'}:1:# brief"


async def test_search_trims_very_long_lines(worktree: Path) -> None:
    executor = ToolExecutor(PathJail(worktree))
    (worktree / "long.txt").write_text("needle " + "x" * 1000 + "\n")

    outcome = await executor.execute("search", {"pattern": "needle"})

    assert len(outcome.content) < 400
    assert outcome.content.endswith("...")


# --- write_file -------------------------------------------------------------


async def test_write_file_creates_parents_and_writes(
    executor: ToolExecutor, worktree: Path
) -> None:
    outcome = await executor.execute("write_file", {"path": "new/dir/f.txt", "content": "hello\n"})

    assert not outcome.is_error
    assert (worktree / "new" / "dir" / "f.txt").read_text() == "hello\n"
    assert "new/dir/f.txt" in outcome.content


async def test_write_file_overwrites(executor: ToolExecutor, worktree: Path) -> None:
    await executor.execute("write_file", {"path": "README.md", "content": "new\n"})

    assert (worktree / "README.md").read_text() == "new\n"


async def test_write_file_onto_a_directory_is_an_error(executor: ToolExecutor) -> None:
    outcome = await executor.execute("write_file", {"path": "src", "content": "x"})

    assert outcome.is_error
    assert "directory" in outcome.content


async def test_write_file_outside_the_worktree_is_denied(
    executor: ToolExecutor, context: Path
) -> None:
    outcome = await executor.execute(
        "write_file", {"path": str(context / "BRIEF.md"), "content": "x"}
    )

    assert outcome.is_error
    assert (context / "BRIEF.md").read_text() == "# brief\n"


# --- edit_file --------------------------------------------------------------


async def test_edit_file_replaces_a_unique_occurrence(
    executor: ToolExecutor, worktree: Path
) -> None:
    outcome = await executor.execute(
        "edit_file", {"path": "src/a.py", "old": "return 1", "new": "return 10"}
    )

    assert not outcome.is_error
    assert (worktree / "src" / "a.py").read_text() == "def a():\n    return 10\n"


async def test_edit_file_with_missing_old_text_is_instructive(
    executor: ToolExecutor, worktree: Path
) -> None:
    outcome = await executor.execute(
        "edit_file", {"path": "src/a.py", "old": "return 99", "new": "return 10"}
    )

    assert outcome.is_error
    assert "not found" in outcome.content
    assert "read_file" in outcome.content
    assert (worktree / "src" / "a.py").read_text() == "def a():\n    return 1\n"


async def test_edit_file_with_ambiguous_old_text_is_instructive(
    executor: ToolExecutor, worktree: Path
) -> None:
    (worktree / "dup.py").write_text("x = 1\ny = 1\n")

    outcome = await executor.execute("edit_file", {"path": "dup.py", "old": "= 1", "new": "= 2"})

    assert outcome.is_error
    assert "2 times" in outcome.content
    assert "unique" in outcome.content
    assert (worktree / "dup.py").read_text() == "x = 1\ny = 1\n"


async def test_edit_file_of_a_missing_file_is_an_error(executor: ToolExecutor) -> None:
    outcome = await executor.execute("edit_file", {"path": "nope.py", "old": "a", "new": "b"})

    assert outcome.is_error
    assert "nope.py" in outcome.content


# --- delete_file ------------------------------------------------------------


async def test_delete_file_removes_a_file(executor: ToolExecutor, worktree: Path) -> None:
    outcome = await executor.execute("delete_file", {"path": "README.md"})

    assert not outcome.is_error
    assert not (worktree / "README.md").exists()


async def test_delete_file_refuses_a_directory(executor: ToolExecutor, worktree: Path) -> None:
    outcome = await executor.execute("delete_file", {"path": "src"})

    assert outcome.is_error
    assert (worktree / "src").is_dir()


async def test_delete_file_of_a_missing_file_is_an_error(executor: ToolExecutor) -> None:
    outcome = await executor.execute("delete_file", {"path": "ghost.txt"})

    assert outcome.is_error
    assert "ghost.txt" in outcome.content


# --- run_command ------------------------------------------------------------


async def test_run_command_reports_exit_code_and_output(executor: ToolExecutor) -> None:
    code = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(2)"

    outcome = await executor.execute("run_command", {"argv": [PY, "-c", code]})

    assert not outcome.is_error
    assert "exit code: 2" in outcome.content
    assert "out\n" in outcome.content
    assert "err\n" in outcome.content


async def test_run_command_runs_in_the_worktree(executor: ToolExecutor, worktree: Path) -> None:
    outcome = await executor.execute(
        "run_command", {"argv": [PY, "-c", "import os; print(os.getcwd())"]}
    )

    lines = outcome.content.splitlines()
    assert Path(lines[lines.index("stdout:") + 1]).resolve() == worktree.resolve()


async def test_run_command_timeout_is_an_error_that_names_the_timeout(worktree: Path) -> None:
    executor = ToolExecutor(PathJail(worktree))

    outcome = await executor.execute(
        "run_command", {"argv": [PY, "-c", "import time; time.sleep(30)"], "timeout": 0.3}
    )

    assert outcome.is_error
    assert "0.3" in outcome.content
    assert "killed" in outcome.content


async def test_run_command_timeout_is_capped(worktree: Path) -> None:
    executor = ToolExecutor(PathJail(worktree), limits=Limits(max_command_timeout=0.2))

    outcome = await executor.execute(
        "run_command", {"argv": [PY, "-c", "import time; time.sleep(30)"], "timeout": 600}
    )

    assert outcome.is_error
    assert "0.2" in outcome.content


async def test_run_command_output_keeps_head_and_tail(worktree: Path) -> None:
    executor = ToolExecutor(PathJail(worktree), limits=Limits(output_chars=300))
    code = "print('\\n'.join(f'line {i:04d}' for i in range(500)))"

    outcome = await executor.execute("run_command", {"argv": [PY, "-c", code]})

    assert "line 0000" in outcome.content
    assert "line 0499" in outcome.content
    assert "line 0250" not in outcome.content
    assert "characters dropped" in outcome.content


async def test_run_command_with_a_missing_binary_is_an_error(executor: ToolExecutor) -> None:
    outcome = await executor.execute("run_command", {"argv": ["no-such-binary-xyz"]})

    assert outcome.is_error
    assert "no-such-binary-xyz" in outcome.content


async def test_run_command_does_not_leak_the_api_key(
    executor: ToolExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CPLANE_ANTHROPIC_API_KEY", "sk-secret")

    outcome = await executor.execute(
        "run_command",
        {"argv": [PY, "-c", "import os; print(os.environ.get('CPLANE_ANTHROPIC_API_KEY'))"]},
    )

    assert "sk-secret" not in outcome.content
    assert "None" in outcome.content


# --- construction and OS errors --------------------------------------------


def test_the_executor_rejects_an_unknown_tool_name_at_construction(worktree: Path) -> None:
    with pytest.raises(KeyError, match="fetch_url"):
        ToolExecutor(PathJail(worktree), tools=["read_file", "fetch_url"])


async def test_list_dir_of_a_missing_directory_is_an_error(executor: ToolExecutor) -> None:
    outcome = await executor.execute("list_dir", {"path": "nowhere"})

    assert outcome.is_error
    assert "nowhere" in outcome.content


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
async def test_an_unreadable_file_is_an_error_result(
    executor: ToolExecutor, worktree: Path
) -> None:
    locked = worktree / "locked.txt"
    locked.write_text("x\n")
    locked.chmod(0)
    try:
        outcome = await executor.execute("read_file", {"path": "locked.txt"})
    finally:
        locked.chmod(0o644)

    assert outcome.is_error
    assert "read_file failed" in outcome.content
