"""Task 12: the repo map (SPEC.md section 9).

The tree of tracked files with line counts and, for Python files, top-level classes and
functions from `ast`. Deep directories are collapsed to keep it bounded, and the collapse is
announced.
"""

from pathlib import Path

from control_plane.context.repo_map import (
    MAX_SYMBOLS,
    REPO_MAP_MAX_LINES,
    RepoFile,
    build_repo_map,
    render_repo_map,
    scan_repo,
)
from control_plane.store.git import Git
from fixtures.project_repo import write


def _repo(root: Path, files: dict[str, str]) -> Path:
    git = Git(root)
    git.init()
    write(root, files)
    git.add(".")
    git.commit("Initial commit")
    return root


def test_scan_lists_tracked_files_with_line_counts_and_python_symbols(tmp_path: Path) -> None:
    root = _repo(
        tmp_path / "repo",
        {
            "README.md": "# Demo\n\nOne\nTwo\n",
            "pkg/core.py": (
                "import os\n\nclass Engine:\n    def run(self):\n        pass\n\n"
                "def start():\n    pass\n\nasync def stop():\n    pass\n\n_private = 1\n"
            ),
        },
    )
    (root / "untracked.py").write_text("def hidden():\n    pass\n")

    files = {f.path: f for f in scan_repo(root)}

    assert set(files) == {"README.md", "pkg/core.py"}
    assert files["README.md"] == RepoFile("README.md", 4, ())
    assert files["pkg/core.py"].lines == 13
    assert files["pkg/core.py"].symbols == ("class Engine", "start", "stop")


def test_a_python_file_that_does_not_parse_is_listed_without_symbols(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo", {"broken.py": "def oops(:\n"})

    assert scan_repo(root) == [RepoFile("broken.py", 1, ())]


def test_a_binary_file_has_no_line_count(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo", {"a.txt": "x\n"})
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00binary")
    Git(root).add(".")
    Git(root).commit("logo")

    files = {f.path: f for f in scan_repo(root)}

    assert files["logo.png"].lines is None
    assert "logo.png (binary)" in render_repo_map(list(files.values()))


def test_the_map_is_a_tree_with_counts_and_symbols() -> None:
    files = [
        RepoFile("README.md", 10, ()),
        RepoFile("src/pkg/cli.py", 143, ("build_parser", "main")),
        RepoFile("src/pkg/__init__.py", 5, ()),
    ]

    text = render_repo_map(files)

    assert text.splitlines()[0] == "# Repository map"
    assert "3 tracked files, 158 lines." in text
    body = text.split("\n\n", 2)[2].splitlines()
    assert body == [
        "README.md (10 lines)",
        "src/",
        "  pkg/",
        "    __init__.py (5 lines)",
        "    cli.py (143 lines): build_parser, main",
    ]


def test_long_symbol_lists_are_capped_and_say_how_many_were_dropped() -> None:
    symbols = tuple(f"f{i}" for i in range(MAX_SYMBOLS + 5))

    text = render_repo_map([RepoFile("big.py", 900, symbols)])

    assert f"f{MAX_SYMBOLS - 1}, +5 more" in text
    assert f"f{MAX_SYMBOLS}," not in text


def test_the_map_stays_bounded_on_a_repository_with_thousands_of_files(tmp_path: Path) -> None:
    files = {
        f"src/area{a}/module{m}/part{p}/file{f}.py": f"def fn{f}():\n    return {f}\n"
        for a in range(6)
        for m in range(6)
        for p in range(4)
        for f in range(25)
    }
    files["README.md"] = "# Big\n"
    assert len(files) > 3000
    root = _repo(tmp_path / "repo", files)

    text = build_repo_map(root)

    lines = text.splitlines()
    assert len(lines) <= REPO_MAP_MAX_LINES
    assert f"{len(files)} tracked files" in text
    assert "collapsed" in lines[2]
    assert "README.md (1 line)" in text
    assert "files," in text  # a collapsed directory states what it holds


def test_a_flat_directory_too_large_to_list_is_cut_and_the_cut_is_announced() -> None:
    files = [RepoFile(f"file{i:05d}.txt", 1, ()) for i in range(2000)]

    text = render_repo_map(files, max_lines=100)

    lines = text.splitlines()
    assert len(lines) <= 100
    assert lines[-1].startswith("[")
    assert "not shown" in lines[-1]
    assert "list_dir" in lines[-1]


def test_a_small_repository_is_not_collapsed() -> None:
    text = render_repo_map([RepoFile("a/b/c/d/e.py", 1, ("f",))])

    assert "collapsed" not in text
    assert "        e.py (1 line): f" in text
