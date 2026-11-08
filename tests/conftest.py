"""Shared test setup.

Every test runs with:
- no real network: socket connects to anything but loopback raise ``NetworkBlocked``;
- no credentials: PAYPAL_*, ANTHROPIC_* and KEYHOLD_* variables are removed from the environment.

Fixtures: ``clock`` (a FakeClock that only moves when told to) and ``db_path`` (a fresh SQLite path).
"""

from __future__ import annotations

import ipaddress
import os
import socket

import pytest


class NetworkBlocked(RuntimeError):
    """A test tried to open a non-loopback network connection."""


def _is_loopback(address: object) -> bool:
    if not isinstance(address, tuple) or not address:
        return True  # AF_UNIX paths and socketpairs stay local
    host = str(address[0])
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_connect(self: socket.socket, address: object) -> None:
        if not _is_loopback(address):
            raise NetworkBlocked(f"tests may not connect to {address!r}")
        return real_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: object) -> int:
        if not _is_loopback(address):
            raise NetworkBlocked(f"tests may not connect to {address!r}")
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)


@pytest.fixture(autouse=True)
def _scrub_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(("PAYPAL_", "ANTHROPIC_", "KEYHOLD_")):
            monkeypatch.delenv(name, raising=False)


class FakeClock:
    """Implements ``keyhold.types.Clock``. ``sleep`` advances time instantly."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.t = float(start)
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += max(0.0, float(seconds))

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "keyhold-test.db"
