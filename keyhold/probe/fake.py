"""Fake SSH and HTTP transports for tests and the offline demo. No network, no subprocess.

- ``FakeServer``: a pretend owner server whose state the test (or a fake console) flips. ``fix()``
  makes both checks pass, ``break_down()`` makes both fail. ``server.ssh`` and ``server.http`` are
  the transports to hand to ``run_probe``.
- ``ScriptedSsh`` / ``ScriptedHttp``: return a fixed sequence of outcomes, then keep repeating the
  last one.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Union

from keyhold.types import ProbeSpec

from .transports import HttpFetch, SshCheck

SshOutcome = Union[bool, SshCheck, BaseException]
HttpOutcome = Union[HttpFetch, bytes, str, BaseException]


class FakeServer:
    def __init__(
        self,
        *,
        ssh_up: bool = False,
        http_up: bool = False,
        status: int = 200,
        body: bytes | str = b"",
    ) -> None:
        self.ssh_up = ssh_up
        self.http_up = http_up
        self.status = status
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.logins: list[ProbeSpec] = []
        self.gets: list[str] = []
        self.ssh = _ServerSsh(self)
        self.http = _ServerHttp(self)

    def fix(self, body: bytes | str | None = None, status: int = 200) -> None:
        self.ssh_up = True
        self.http_up = True
        self.status = status
        if body is not None:
            self.body = body.encode("utf-8") if isinstance(body, str) else body

    def break_down(self) -> None:
        self.ssh_up = False
        self.http_up = False


class _ServerSsh:
    def __init__(self, server: FakeServer) -> None:
        self._server = server

    def login(self, spec: ProbeSpec) -> SshCheck:
        self._server.logins.append(spec)
        if self._server.ssh_up:
            return SshCheck(True, f"logged in as {spec.ssh_user}")
        return SshCheck(False, "connection refused")


class _ServerHttp:
    def __init__(self, server: FakeServer) -> None:
        self._server = server

    def get(self, url: str) -> HttpFetch:
        self._server.gets.append(url)
        if not self._server.http_up:
            raise ConnectionRefusedError("connection refused")
        return HttpFetch(self._server.status, self._server.body)


class ScriptedSsh:
    """``True``/``False`` become a plain check; an exception instance is raised."""

    def __init__(self, outcomes: Iterable[SshOutcome]) -> None:
        self._outcomes = list(outcomes)
        if not self._outcomes:
            raise ValueError("give at least one outcome")
        self.calls: list[ProbeSpec] = []

    def login(self, spec: ProbeSpec) -> SshCheck:
        index = min(len(self.calls), len(self._outcomes) - 1)
        self.calls.append(spec)
        outcome = self._outcomes[index]
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, SshCheck):
            return outcome
        return SshCheck(True, f"logged in as {spec.ssh_user}") if outcome else SshCheck(False, "connection refused")


class ScriptedHttp:
    """``bytes``/``str`` become an HTTP 200 with that body; an exception instance is raised."""

    def __init__(self, outcomes: Iterable[HttpOutcome]) -> None:
        self._outcomes = list(outcomes)
        if not self._outcomes:
            raise ValueError("give at least one outcome")
        self.calls: list[str] = []

    def get(self, url: str) -> HttpFetch:
        index = min(len(self.calls), len(self._outcomes) - 1)
        self.calls.append(url)
        outcome = self._outcomes[index]
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, HttpFetch):
            return outcome
        body = outcome.encode("utf-8") if isinstance(outcome, str) else outcome
        return HttpFetch(200, body)
