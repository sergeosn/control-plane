"""Shared test configuration.

Tests never touch the network or a model (docs/SPEC.md section 16). The fixture below
enforces that in code: any attempt to resolve a host or connect a socket fails the test.

The one exception is a test marked `live` while `CPLANE_LIVE=1` is set (Task 10): the live
smoke test is opt-in, skipped in CI, and the only test that may reach the endpoint.
"""

import os
import socket
from collections.abc import Iterator

import pytest


def _deny_network(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("tests must not touch the network")


def _live_allowed(request: pytest.FixtureRequest) -> bool:
    return (
        request.node.get_closest_marker("live") is not None and os.environ.get("CPLANE_LIVE") == "1"
    )


@pytest.fixture(autouse=True)
def no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    if _live_allowed(request):
        yield
        return
    monkeypatch.setattr(socket.socket, "connect", _deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", _deny_network)
    monkeypatch.setattr(socket, "getaddrinfo", _deny_network)
    yield
