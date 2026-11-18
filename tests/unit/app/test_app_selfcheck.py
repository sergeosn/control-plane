"""Task 15: `cplane selfcheck` runs the scripted end-to-end scenario with no network access
(docs/SPEC.md sections 13 and 16)."""

import socket
from pathlib import Path

import pytest

from control_plane.app import selfcheck


async def test_the_selfcheck_passes_and_names_each_check(tmp_path: Path) -> None:
    report = await selfcheck.run_selfcheck(tmp_path)

    assert report.ok, report.failures
    names = [check.name for check in report.checks]
    assert names == list(selfcheck.CHECKS)
    assert all(check.ok for check in report.checks)


async def test_the_selfcheck_runs_in_a_temporary_directory_by_default() -> None:
    report = await selfcheck.run_selfcheck()

    assert report.ok, report.failures
    assert not Path(report.workdir).exists()


async def test_the_selfcheck_reports_a_failure_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("the reviewer script is gone")

    monkeypatch.setattr(selfcheck, "scripted_clients", broken)

    report = await selfcheck.run_selfcheck(tmp_path)

    assert not report.ok
    assert any("the reviewer script is gone" in failure for failure in report.failures)


def test_network_is_blocked_inside_the_guard_and_restored_after() -> None:
    connect = socket.socket.connect

    with selfcheck.network_blocked():
        with pytest.raises(selfcheck.NetworkBlockedError):
            socket.getaddrinfo("example.invalid", 80)
        with socket.socket() as sock, pytest.raises(selfcheck.NetworkBlockedError):
            sock.connect(("127.0.0.1", 9))

    assert socket.socket.connect is connect


async def test_a_network_attempt_during_the_selfcheck_fails_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = selfcheck.scripted_clients

    def calling_out(home: object) -> object:
        socket.getaddrinfo("example.invalid", 443)
        return real(home)

    monkeypatch.setattr(selfcheck, "scripted_clients", calling_out)

    report = await selfcheck.run_selfcheck(tmp_path)

    assert not report.ok
    assert any("network" in failure for failure in report.failures)
