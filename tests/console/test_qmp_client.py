"""QMPClient against a fake QMP server on loopback."""

from __future__ import annotations

import socket

import pytest
from console_fakes import QMPCommandError

from keyhold.console.qmp import QMPClient, QMPConnectionError, QMPError


def test_connect_negotiates_and_executes(qmp_server) -> None:
    qmp_server.handlers["query-status"] = lambda a: {"status": "running", "running": True}
    client = QMPClient.connect("127.0.0.1", qmp_server.port, wait=5)
    assert client.greeting["QMP"]["version"]["qemu"]["major"] == 10
    assert client.execute("query-status") == {"status": "running", "running": True}
    assert [c for _, c, _ in qmp_server.log] == ["qmp_capabilities", "query-status"]
    client.close()
    assert client.closed
    with pytest.raises(QMPConnectionError):
        client.execute("query-status")


def test_arguments_and_error_replies(qmp_server) -> None:
    def screendump(args):
        if args.get("format") == "png":
            raise QMPCommandError("GenericError", "format png is not available")
        return {}

    qmp_server.handlers["screendump"] = screendump
    client = QMPClient.connect("127.0.0.1", qmp_server.port, wait=5)
    with pytest.raises(QMPError) as info:
        client.execute("screendump", {"filename": "/x.png", "format": "png"})
    assert info.value.error_class == "GenericError" and "png" in info.value.desc
    assert client.execute("screendump", {"filename": "/x.ppm"}) == {}
    assert qmp_server.log[-1][2] == {"filename": "/x.ppm"}
    with pytest.raises(QMPError) as missing:
        client.execute("no-such-command")
    assert missing.value.error_class == "CommandNotFound"


def test_events_are_collected_between_replies(qmp_server) -> None:
    qmp_server.handlers["system_powerdown"] = lambda a: {}
    qmp_server.handlers["query-status"] = lambda a: {"status": "shutdown", "running": False}
    client = QMPClient.connect("127.0.0.1", qmp_server.port, wait=5)
    client.execute("system_powerdown")
    qmp_server.emit("SHUTDOWN", {"guest": True, "reason": "guest-shutdown"})
    assert client.execute("query-status")["status"] == "shutdown"
    event = client.wait_event("SHUTDOWN", timeout=1)
    assert event is not None and event["data"]["reason"] == "guest-shutdown"
    assert client.wait_event("SHUTDOWN", timeout=0.2) is None


def test_connect_retries_until_qemu_answers(qmp_server) -> None:
    qmp_server.drop_first = 2  # a port proxy that accepts before QEMU listens behind it
    client = QMPClient.connect("127.0.0.1", qmp_server.port, wait=10)
    assert client.greeting
    client.close()


def test_connect_gives_up() -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing listens here now
    with pytest.raises(QMPConnectionError):
        QMPClient.connect("127.0.0.1", port, wait=0.3)


def test_closed_connection_is_reported(qmp_server) -> None:
    qmp_server.handlers["quit"] = lambda a: "CLOSE"
    client = QMPClient.connect("127.0.0.1", qmp_server.port, wait=5)
    with pytest.raises(QMPConnectionError):
        client.execute("quit")
    assert client.closed


def test_rejects_a_server_that_is_not_qmp() -> None:
    left, right = socket.socketpair()
    right.sendall(b'{"hello": "world"}\n')
    client = QMPClient(left, timeout=2)
    with pytest.raises(QMPConnectionError):
        client.handshake()
    right.sendall(b"not json\n")
    with pytest.raises(QMPConnectionError):
        client._read_message()
    left.close()
    right.close()
