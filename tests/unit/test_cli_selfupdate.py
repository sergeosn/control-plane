"""Task 16: `cplane self-update [--project NAME] [--to REF]` (docs/SPEC.md sections 13 and 14).

The selfcheck and `uv sync` commands are replaced by fakes through the module's constants,
which the command reads at call time.
"""

from pathlib import Path

import pytest

from control_plane.app import selfupdate
from control_plane.cli import main
from fixtures.selfupdate_repo import (
    NEW_FILE,
    SelfUpdateRepos,
    fake_command,
    head,
    make_repos,
    tag_commit,
)


def install_fakes(
    monkeypatch: pytest.MonkeyPatch, root: Path, *, selfcheck_fails: bool = False
) -> None:
    selfcheck = fake_command(
        root / "selfcheck.log",
        output="FAIL  e2e: the fake broke" if selfcheck_fails else "ok    e2e: fine",
        fail_when="." if selfcheck_fails else None,
    )
    monkeypatch.setattr(selfupdate, "SELFCHECK_ARGV", tuple(selfcheck))
    monkeypatch.setattr(selfupdate, "SYNC_ARGV", tuple(fake_command(root / "sync.log", output="")))


def run_cli(repos: SelfUpdateRepos, *args: str) -> int:
    return main(["--home", str(repos.home.path), "self-update", *args])


def test_self_update_defaults_to_the_control_plane_project_and_prints_both_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repos = make_repos(tmp_path)
    install_fakes(monkeypatch, tmp_path)

    code = run_cli(repos)

    out = capsys.readouterr().out
    assert code == 0
    assert repos.old in out and repos.new in out
    assert f"last-known-good: {repos.old} -> {repos.new}" in out
    assert head(repos.control) == repos.new


def test_project_and_to_select_the_project_and_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repos = make_repos(tmp_path, project="engine")
    install_fakes(monkeypatch, tmp_path)
    assert run_cli(repos, "--project", "engine") == 0
    capsys.readouterr()

    code = run_cli(repos, "--project", "engine", "--to", repos.old)

    assert code == 0
    assert f"last-known-good: {repos.new} -> {repos.old}" in capsys.readouterr().out
    assert tag_commit(repos.primary) == repos.old == head(repos.control)


def test_a_failing_selfcheck_prints_its_output_exits_1_and_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repos = make_repos(tmp_path)
    install_fakes(monkeypatch, tmp_path, selfcheck_fails=True)

    code = run_cli(repos)

    captured = capsys.readouterr()
    assert code == 1
    assert "FAIL  e2e: the fake broke" in captured.out
    assert "nothing changed" in captured.err
    assert tag_commit(repos.primary) == repos.old == head(repos.control)


def test_a_refusal_is_one_line_on_stderr_and_exit_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repos = make_repos(tmp_path)
    install_fakes(monkeypatch, tmp_path)
    (repos.primary.root / "app.py").write_text("changed\n")

    code = run_cli(repos)

    err = capsys.readouterr().err
    assert code == 1
    assert err.startswith("cplane: ") and "local changes" in err


def test_a_failing_sync_says_the_tag_and_the_control_checkout_are_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repos = make_repos(tmp_path)
    install_fakes(monkeypatch, tmp_path)
    sync = fake_command(tmp_path / "sync.log", output="uv: lockfile mismatch", fail_when=NEW_FILE)
    monkeypatch.setattr(selfupdate, "SYNC_ARGV", tuple(sync))

    code = run_cli(repos)

    captured = capsys.readouterr()
    assert code == 1
    assert "uv: lockfile mismatch" in captured.out
    assert f"back at {repos.old}" in captured.err
    assert tag_commit(repos.primary) == repos.old == head(repos.control)
