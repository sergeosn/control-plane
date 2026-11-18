"""Task 16: `cplane self-update` adopts a version of the system (docs/SPEC.md section 13).

The selfcheck runs in the primary checkout; when it passes, the tag `last-known-good` moves
to the target and the control checkout is checked out at the tag, detached, and synced.
When it fails nothing changes. Fixture repositories live in a temporary directory and the
selfcheck and `uv` are fakes.
"""

from pathlib import Path

import pytest

from control_plane.app import selfupdate
from control_plane.app.selfupdate import SelfUpdateError, self_update
from control_plane.domain.project import GuardrailGate, ProjectConfig
from control_plane.store.git import Git
from fixtures.selfupdate_repo import (
    NEW_FILE,
    SelfUpdateRepos,
    fake_command,
    head,
    is_detached,
    make_repos,
    ran_in,
    tag_commit,
)


@pytest.fixture
def repos(tmp_path: Path) -> SelfUpdateRepos:
    return make_repos(tmp_path)


class Fakes:
    def __init__(self, root: Path) -> None:
        self.selfcheck_log = root / "selfcheck.log"
        self.sync_log = root / "sync.log"

    def selfcheck(self, fail_when: str | None = None) -> list[str]:
        return fake_command(
            self.selfcheck_log, output="fake selfcheck says hi", fail_when=fail_when
        )

    def sync(self, fail_when: str | None = None) -> list[str]:
        return fake_command(self.sync_log, output="fake uv sync", fail_when=fail_when)


@pytest.fixture
def fakes(tmp_path: Path) -> Fakes:
    return Fakes(tmp_path)


async def update(
    repos: SelfUpdateRepos,
    fakes: Fakes,
    *,
    to: str | None = None,
    selfcheck_fails: str | None = None,
    sync_fails: str | None = None,
    project: str = "control-plane",
) -> selfupdate.SelfUpdateResult:
    return await self_update(
        repos.home,
        project,
        to=to,
        selfcheck_argv=fakes.selfcheck(selfcheck_fails),
        sync_argv=fakes.sync(sync_fails),
    )


def test_the_real_commands_are_uv_run_selfcheck_and_uv_sync() -> None:
    assert selfupdate.SELFCHECK_ARGV == ("uv", "run", "--frozen", "cplane", "selfcheck")
    assert selfupdate.SYNC_ARGV == ("uv", "sync", "--frozen")
    assert selfupdate.DEFAULT_PROJECT == "control-plane"
    assert selfupdate.TAG == "last-known-good"


# --- a passing selfcheck adopts main ---------------------------------------------------


async def test_a_passing_selfcheck_moves_the_tag_and_the_control_checkout_to_main(
    repos: SelfUpdateRepos, fakes: Fakes
) -> None:
    result = await update(repos, fakes)

    assert result.adopted
    assert (result.previous, result.current) == (repos.old, repos.new)
    assert tag_commit(repos.primary) == repos.new
    assert head(repos.control) == repos.new
    assert is_detached(repos.control)
    assert (repos.control / NEW_FILE).is_file()


async def test_the_selfcheck_runs_in_the_primary_and_uv_sync_in_the_control_checkout(
    repos: SelfUpdateRepos, fakes: Fakes
) -> None:
    await update(repos, fakes)

    assert ran_in(fakes.selfcheck_log) == [repos.primary.root.resolve()]
    assert ran_in(fakes.sync_log) == [repos.control.resolve()]


async def test_without_a_tag_the_previous_commit_is_none(
    repos: SelfUpdateRepos, fakes: Fakes
) -> None:
    repos.primary.run("tag", "--delete", "last-known-good")

    result = await update(repos, fakes)

    assert (result.previous, result.current) == (None, repos.new)
    assert tag_commit(repos.primary) == repos.new


async def test_a_control_checkout_that_is_a_separate_clone_is_moved_too(
    tmp_path: Path, fakes: Fakes
) -> None:
    repos = make_repos(tmp_path / "cloned", clone=True)

    result = await update(repos, fakes)

    assert result.adopted
    assert head(repos.control) == repos.new
    assert is_detached(repos.control)


async def test_the_control_checkout_follows_the_guardrail_gate_rule(tmp_path: Path) -> None:
    repo = tmp_path / "control-plane"
    assert ProjectConfig(repo=repo).control_checkout() == tmp_path / "control-plane-control"
    elsewhere = tmp_path / "elsewhere"
    gate = GuardrailGate(argv=["pytest"], control=elsewhere)
    config = ProjectConfig(repo=repo, gates={"guardrail_regression": gate})
    assert config.control_checkout() == elsewhere == config.guardrail_control()


# --- a failing selfcheck changes nothing -----------------------------------------------


async def test_a_failing_selfcheck_changes_nothing_and_returns_its_output(
    repos: SelfUpdateRepos, fakes: Fakes
) -> None:
    result = await update(repos, fakes, selfcheck_fails=".")

    assert not result.adopted
    assert result.failed == "selfcheck"
    assert "fake selfcheck says hi" in result.output
    assert (result.previous, result.current) == (repos.old, repos.old)
    assert tag_commit(repos.primary) == repos.old
    assert head(repos.control) == repos.old
    assert ran_in(fakes.sync_log) == []


# --- --to ------------------------------------------------------------------------------


async def test_to_a_ref_adopts_that_commit_which_is_how_a_rollback_works(
    repos: SelfUpdateRepos, fakes: Fakes
) -> None:
    await update(repos, fakes)

    result = await update(repos, fakes, to=repos.old)

    assert result.adopted
    assert (result.previous, result.current) == (repos.new, repos.old)
    assert tag_commit(repos.primary) == repos.old
    assert head(repos.control) == repos.old
    assert not (repos.control / NEW_FILE).exists()


async def test_to_accepts_any_ref_the_primary_knows(repos: SelfUpdateRepos, fakes: Fakes) -> None:
    repos.primary.run("tag", "v0.1", repos.new)

    result = await update(repos, fakes, to="v0.1")

    assert result.current == repos.new == head(repos.control)


async def test_to_an_unknown_ref_is_refused_before_the_selfcheck(
    repos: SelfUpdateRepos, fakes: Fakes
) -> None:
    with pytest.raises(SelfUpdateError, match="no-such-ref"):
        await update(repos, fakes, to="no-such-ref")

    assert ran_in(fakes.selfcheck_log) == []


# --- refusals --------------------------------------------------------------------------


async def test_local_changes_in_the_primary_checkout_are_refused(
    repos: SelfUpdateRepos, fakes: Fakes
) -> None:
    (repos.primary.root / "app.py").write_text("changed\n")

    with pytest.raises(SelfUpdateError, match="local changes"):
        await update(repos, fakes)

    assert ran_in(fakes.selfcheck_log) == []
    assert tag_commit(repos.primary) == repos.old


async def test_a_staged_new_file_in_the_primary_checkout_is_a_local_change(
    repos: SelfUpdateRepos, fakes: Fakes
) -> None:
    (repos.primary.root / "staged.txt").write_text("x\n")
    repos.primary.add("staged.txt")

    with pytest.raises(SelfUpdateError, match="local changes"):
        await update(repos, fakes)


async def test_a_primary_checkout_off_main_is_refused(repos: SelfUpdateRepos, fakes: Fakes) -> None:
    repos.primary.run("checkout", "--quiet", "-b", "feature")

    with pytest.raises(SelfUpdateError, match="not on main"):
        await update(repos, fakes)

    assert ran_in(fakes.selfcheck_log) == []


async def test_a_missing_control_checkout_is_refused(
    tmp_path: Path, repos: SelfUpdateRepos, fakes: Fakes
) -> None:
    repos.primary.run("worktree", "remove", "--force", str(repos.control))

    with pytest.raises(SelfUpdateError, match="no control checkout"):
        await update(repos, fakes)

    assert ran_in(fakes.selfcheck_log) == []


async def test_local_changes_in_the_control_checkout_are_refused(
    repos: SelfUpdateRepos, fakes: Fakes
) -> None:
    (repos.control / "app.py").write_text("edited by hand\n")

    with pytest.raises(SelfUpdateError, match="control checkout .* local changes"):
        await update(repos, fakes)

    assert ran_in(fakes.selfcheck_log) == []


async def test_an_unknown_project_is_refused(repos: SelfUpdateRepos, fakes: Fakes) -> None:
    with pytest.raises(SelfUpdateError, match="nope"):
        await update(repos, fakes, project="nope")


# --- a failing uv sync puts everything back --------------------------------------------


async def test_a_failing_sync_puts_the_tag_and_the_control_checkout_back(
    repos: SelfUpdateRepos, fakes: Fakes
) -> None:
    result = await update(repos, fakes, sync_fails=NEW_FILE)

    assert not result.adopted
    assert result.failed == "sync"
    assert "fake uv sync" in result.output
    assert (result.previous, result.current) == (repos.old, repos.old)
    assert tag_commit(repos.primary) == repos.old
    assert head(repos.control) == repos.old
    assert ran_in(fakes.sync_log) == [repos.control.resolve()] * 2


async def test_a_failing_sync_without_a_previous_tag_removes_the_tag(
    repos: SelfUpdateRepos, fakes: Fakes
) -> None:
    repos.primary.run("tag", "--delete", "last-known-good")

    result = await update(repos, fakes, sync_fails=NEW_FILE)

    assert not result.adopted
    assert tag_commit(repos.primary) is None
    assert head(repos.control) == repos.old


async def test_the_project_hooks_do_not_run(repos: SelfUpdateRepos, fakes: Fakes) -> None:
    hooks = Path(Git(repos.primary.root).run("rev-parse", "--git-common-dir").strip())
    hooks = (repos.primary.root / hooks / "hooks").resolve()
    hooks.mkdir(parents=True, exist_ok=True)
    marker = repos.primary.root.parent / "hook-ran"
    hook = hooks / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)

    await update(repos, fakes)

    assert not marker.exists()
