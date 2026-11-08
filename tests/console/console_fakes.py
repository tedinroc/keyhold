"""Fakes for the console tests: a QMP server on loopback and a docker CLI that never runs docker.

Imported by tests/console/conftest.py (fixtures) and by tests that need the classes directly.
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from keyhold.console.docker import RUNNER_IMAGE

GREETING = {"QMP": {"version": {"qemu": {"major": 10, "minor": 0, "micro": 13}, "package": ""}, "capabilities": []}}


class QMPCommandError(Exception):
    def __init__(self, error_class: str, desc: str) -> None:
        super().__init__(desc)
        self.error_class = error_class
        self.desc = desc


class FakeQMPServer:
    """A QMP server on 127.0.0.1. Every connection gets the greeting, then each command is
    answered by ``handlers[name](arguments)`` (raise ``QMPCommandError`` for an error reply).
    ``log`` records (connection number, command, arguments)."""

    def __init__(self) -> None:
        self.handlers: dict[str, Callable[[dict[str, Any]], Any]] = {"qmp_capabilities": lambda a: {}}
        self.log: list[tuple[int, str, dict[str, Any]]] = []
        self.pending_events: list[dict[str, Any]] = []
        self.drop_first = 0  # close this many connections right after accepting them
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self.port = self._listener.getsockname()[1]
        self._conns: list[socket.socket] = []
        self._closed = False
        self._accepted = 0
        self._lock = threading.Lock()
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def commands(self) -> list[str]:
        return [c for _, c, _ in self.log if c != "qmp_capabilities"]

    def emit(self, event: str, data: dict[str, Any] | None = None) -> None:
        """Queue an event sent before the next reply on any connection."""
        self.pending_events.append({"event": event, "data": data or {}, "timestamp": {"seconds": 0, "microseconds": 0}})

    def close(self) -> None:
        self._closed = True
        for sock in [self._listener, *self._conns]:
            try:
                sock.close()
            except OSError:
                pass

    def _accept_loop(self) -> None:
        while not self._closed:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            self._accepted += 1
            if self._accepted <= self.drop_first:
                conn.close()
                continue
            self._conns.append(conn)
            threading.Thread(target=self._serve, args=(conn, self._accepted), daemon=True).start()

    def _serve(self, conn: socket.socket, number: int) -> None:
        def send(obj: dict[str, Any]) -> None:
            conn.sendall((json.dumps(obj) + "\r\n").encode())

        try:
            send(GREETING)
            buffer = b""
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    message = json.loads(line)
                    name = message["execute"]
                    arguments = message.get("arguments", {})
                    with self._lock:
                        self.log.append((number, name, arguments))
                        events, self.pending_events = self.pending_events, []
                    for event in events:
                        send(event)
                    handler = self.handlers.get(name)
                    if handler is None:
                        send({"id": message.get("id"), "error": {"class": "CommandNotFound", "desc": f"no {name}"}})
                        continue
                    try:
                        result = handler(arguments)
                    except QMPCommandError as exc:
                        send({"id": message.get("id"), "error": {"class": exc.error_class, "desc": exc.desc}})
                        continue
                    if result == "CLOSE":
                        conn.close()
                        return
                    send({"id": message.get("id"), "return": result if result is not None else {}})
        except OSError:
            return


class FakeDocker:
    """Stands in for ``subprocess.run`` of the docker CLI. Records every argv and answers the
    commands DockerRunner uses. ``tool`` commands (qemu-img, genisoimage, cp) create their output
    file under the VM home so callers see the result they expect."""

    def __init__(self, vm_home: Path) -> None:
        self.vm_home = Path(vm_home).resolve()
        self.calls: list[list[str]] = []
        self.ports: dict[int, int] = {}
        self.running: dict[str, bool] = {}
        self.exit_codes: dict[str, int] = {}
        self.kvm = False
        self.fail: dict[str, tuple[int, str]] = {}  # docker subcommand -> (exit code, stderr)
        self.exit_after_polls: dict[str, int] = {}  # container -> inspections before it exits

    def host_path(self, container_path: str) -> Path:
        assert container_path.startswith("/vm/"), container_path
        return self.vm_home / container_path[len("/vm/") :]

    def __call__(self, argv, capture_output=True, text=True, timeout=None, env=None):  # noqa: ARG002
        argv = list(argv)
        self.calls.append(argv)
        sub = argv[1]
        if sub in self.fail:
            code, err = self.fail[sub]
            return subprocess.CompletedProcess(argv, code, "", err)
        out = ""
        code = 0
        if sub == "run" and "-d" in argv:
            name = argv[argv.index("--name") + 1]
            self.running[name] = True
            out = "c0ffee\n"
        elif sub == "run" and "--device" in argv and "sh" in argv:  # kvm probe
            code = 0 if self.kvm else 125
        elif sub == "run":
            self._tool(argv[argv.index(RUNNER_IMAGE) + 1 :])
        elif sub == "port":
            port = int(argv[3].split("/")[0])
            out = f"127.0.0.1:{self.ports.get(port, 40000 + port)}\n"
        elif sub == "inspect":
            name = argv[-1]
            if name not in self.running:
                code = 1
            elif "{{.State.Running}}" in argv:
                out = "true\n" if self.running[name] else "false\n"
            else:
                left = self.exit_after_polls.get(name)
                if left is not None:
                    if left <= 0:
                        self.running[name] = False
                    self.exit_after_polls[name] = left - 1
                state = {"Running": self.running[name], "ExitCode": self.exit_codes.get(name, 0)}
                out = json.dumps(state)
        elif sub == "rm":
            self.running.pop(argv[-1], None)
        elif sub == "logs":
            out = "qemu log line\n"
        elif sub == "ps":
            out = "".join(f"{n}\n" for n in self.running)
        elif sub == "info":
            out = "aarch64\n" if "{{.Architecture}}" in argv else "29.0\n"
        elif sub == "image":
            code = 0
        return subprocess.CompletedProcess(argv, code, out, "")

    def _tool(self, argv: list[str]) -> None:
        if argv[0] == "qemu-img":
            self.host_path(argv[-1]).write_bytes(b"QFI\xfb fake overlay")
        elif argv[0] == "genisoimage":
            out = argv[argv.index("-output") + 1]
            self.host_path(out).write_bytes(b"fake iso")
        elif argv[0] == "cp":
            self.host_path(argv[-1]).write_bytes(b"fake vars")

    def docker_calls(self, sub: str) -> list[list[str]]:
        return [c for c in self.calls if c[1] == sub]
