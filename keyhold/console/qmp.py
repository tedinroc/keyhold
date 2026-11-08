"""A small synchronous QMP client (QEMU Machine Protocol: one JSON object per line).

Only what Keyhold needs: connect and negotiate capabilities, run a command and get its return
value, and collect asynchronous events (SHUTDOWN, RESET, ...) that arrive in between.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

from keyhold.console.base import ConsoleError

__all__ = ["QMPClient", "QMPError", "QMPConnectionError"]


class QMPConnectionError(ConsoleError):
    """No QMP greeting, or the connection dropped."""


class QMPError(ConsoleError):
    """QEMU answered a command with an error. ``error_class`` is QMP's ``class`` field."""

    def __init__(self, command: str, error_class: str, desc: str) -> None:
        super().__init__(f"{command}: {error_class}: {desc}")
        self.command = command
        self.error_class = error_class
        self.desc = desc


class QMPClient:
    """One QMP connection. Thread-safe: commands are serialized with a lock."""

    def __init__(self, sock: socket.socket, *, timeout: float = 30.0) -> None:
        self._sock = sock
        self._sock.settimeout(timeout)
        self._buffer = bytearray()
        self._timeout = timeout
        self._lock = threading.Lock()
        self._next_id = 0
        self.events: list[dict[str, Any]] = []
        self.greeting: dict[str, Any] = {}
        self.closed = False

    # ------------------------------------------------------------------ connecting

    @classmethod
    def connect(
        cls,
        host: str,
        port: int,
        *,
        wait: float = 30.0,
        timeout: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> QMPClient:
        """Connect over TCP and negotiate capabilities, retrying for up to ``wait`` seconds.

        Retrying covers QEMU still starting, and a container port proxy that accepts the
        connection before QEMU listens behind it and then closes it.
        """
        deadline = clock() + wait
        last: Exception | None = None
        while True:
            sock: socket.socket | None = None
            try:
                sock = socket.create_connection((host, port), timeout=min(timeout, 5.0))
                client = cls(sock, timeout=timeout)
                client.handshake(greeting_timeout=min(timeout, 5.0))
                return client
            except (OSError, QMPConnectionError) as exc:
                last = exc
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            if clock() >= deadline:
                raise QMPConnectionError(f"no QMP on port {port} after {wait:.0f}s: {last}") from last
            sleep(0.25)

    def handshake(self, *, greeting_timeout: float | None = None) -> None:
        """Read the greeting and send ``qmp_capabilities``."""
        if greeting_timeout is not None:
            self._sock.settimeout(greeting_timeout)
        try:
            greeting = self._read_message()
        finally:
            self._sock.settimeout(self._timeout)
        if "QMP" not in greeting:
            raise QMPConnectionError(f"expected a QMP greeting, got {greeting!r}")
        self.greeting = greeting
        self.execute("qmp_capabilities")

    # ------------------------------------------------------------------ commands and events

    def execute(self, command: str, arguments: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        """Run ``command`` and return its ``return`` value. Raises ``QMPError`` on an error reply."""
        with self._lock:
            if self.closed:
                raise QMPConnectionError("QMP connection is closed")
            self._next_id += 1
            request_id = f"kh-{self._next_id}"
            message: dict[str, Any] = {"execute": command, "id": request_id}
            if arguments:
                message["arguments"] = arguments
            self._send(message)
            if timeout is not None:
                self._sock.settimeout(timeout)
            try:
                while True:
                    try:
                        reply = self._read_message()
                    except TimeoutError as exc:
                        raise QMPConnectionError(f"QMP command {command} timed out") from exc
                    if "event" in reply:
                        self.events.append(reply)
                        continue
                    if reply.get("id") != request_id:
                        continue  # a reply to an earlier command that timed out
                    if "error" in reply:
                        err = reply["error"] or {}
                        raise QMPError(command, str(err.get("class", "")), str(err.get("desc", "")))
                    return reply.get("return")
            finally:
                if timeout is not None:
                    self._sock.settimeout(self._timeout)

    def wait_event(
        self,
        name: str,
        *,
        timeout: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> dict[str, Any] | None:
        """Return the first event called ``name`` (already received or arriving within ``timeout``)."""
        deadline = clock() + timeout
        with self._lock:
            while True:
                for index, event in enumerate(self.events):
                    if event.get("event") == name:
                        return self.events.pop(index)
                remaining = deadline - clock()
                if remaining <= 0:
                    return None
                self._sock.settimeout(max(0.05, min(remaining, self._timeout)))
                try:
                    message = self._read_message()
                except TimeoutError:
                    continue
                except QMPConnectionError:
                    return None
                finally:
                    self._sock.settimeout(self._timeout)
                if "event" in message:
                    self.events.append(message)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self._sock.close()
        except OSError:
            pass

    # ------------------------------------------------------------------ wire

    def _send(self, message: dict[str, Any]) -> None:
        data = (json.dumps(message) + "\n").encode("utf-8")
        try:
            self._sock.sendall(data)
        except OSError as exc:
            self.closed = True
            raise QMPConnectionError(f"QMP send failed: {exc}") from exc

    def _readline(self) -> bytes:
        """One line from the socket. A timeout raises ``TimeoutError`` and keeps buffered data."""
        while True:
            end = self._buffer.find(b"\n")
            if end >= 0:
                line = bytes(self._buffer[: end + 1])
                del self._buffer[: end + 1]
                return line
            try:
                chunk = self._sock.recv(65536)
            except TimeoutError:
                raise
            except OSError as exc:
                self.closed = True
                raise QMPConnectionError(f"QMP read failed: {exc}") from exc
            if not chunk:
                self.closed = True
                raise QMPConnectionError("QMP connection closed by QEMU")
            self._buffer += chunk

    def _read_message(self) -> dict[str, Any]:
        while True:
            line = self._readline().strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError as exc:
                raise QMPConnectionError(f"bad QMP message: {line[:200]!r}") from exc
            if not isinstance(message, dict):
                raise QMPConnectionError(f"bad QMP message: {line[:200]!r}")
            return message
