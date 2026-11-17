"""Shared test configuration.

Tests never touch the network or a model (SPEC.md section 16). The fixture below
enforces that in code: any attempt to resolve a host or connect a socket fails the test.
"""

import socket
from collections.abc import Iterator

import pytest


def _deny_network(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("tests must not touch the network")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(socket.socket, "connect", _deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", _deny_network)
    monkeypatch.setattr(socket, "getaddrinfo", _deny_network)
    yield
