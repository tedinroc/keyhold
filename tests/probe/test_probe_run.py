"""run_probe and Prober with fake transports: a pass needs both halves, and nothing raises."""

from __future__ import annotations

import httpx
import pytest

from keyhold.probe.fake import FakeServer, ScriptedHttp, ScriptedSsh
from keyhold.probe.owner import HttpFetch, Prober, SshCheck, run_probe
from keyhold.types import ProbeResult


def test_pass_needs_ssh_and_page(spec, clock) -> None:
    result = run_probe(spec, ssh=ScriptedSsh([True]), http=ScriptedHttp(["... keyhold-ok ..."]), clock=clock)
    assert result == ProbeResult(
        ok=True,
        ssh_ok=True,
        http_ok=True,
        detail="ssh ok: logged in as probe; http ok: HTTP 200, expected text found",
        at=clock.now(),
    )


@pytest.mark.parametrize(
    ("ssh", "http", "ssh_ok", "http_ok", "fragment"),
    [
        ([False], ["keyhold-ok"], False, True, "ssh failed: connection refused"),
        ([True], ["nginx default page"], True, False, "http failed: HTTP 200, expected text not found"),
        ([True], [HttpFetch(503, b"keyhold-ok")], True, False, "http failed: HTTP 503"),
        ([True], [ConnectionRefusedError()], True, False, "http failed: could not connect"),
        ([True], [httpx.ConnectTimeout("slow")], True, False, "http failed: timed out"),
        ([True], [httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]")], True, False, "TLS certificate not trusted"),
        ([True], [httpx.RemoteProtocolError("bad")], True, False, "request failed (RemoteProtocolError)"),
        ([True], [KeyError("bug")], True, False, "http failed: check crashed (KeyError)"),
        ([RuntimeError("bug")], ["keyhold-ok"], False, True, "ssh failed: check crashed (RuntimeError)"),
        ([SshCheck(False, "login refused")], [b""], False, False, "ssh failed: login refused"),
    ],
)
def test_failures_are_reported_not_raised(spec, clock, ssh, http, ssh_ok, http_ok, fragment) -> None:
    result = run_probe(spec, ssh=ScriptedSsh(ssh), http=ScriptedHttp(http), clock=clock)
    assert result.ok is False
    assert (result.ssh_ok, result.http_ok) == (ssh_ok, http_ok)
    assert fragment in result.detail


def test_both_checks_run_even_when_ssh_fails(spec, clock) -> None:
    server = FakeServer(ssh_up=False, http_up=True, body="keyhold-ok")
    result = run_probe(spec, ssh=server.ssh, http=server.http, clock=clock)
    assert not result.ok and result.http_ok
    assert len(server.logins) == 1 and server.gets == [spec.url]


def test_prober_binds_spec_transports_and_clock(spec, clock) -> None:
    server = FakeServer()
    probe = Prober(spec, server.ssh, server.http, clock)
    assert probe().ok is False
    server.fix(body="<h1>keyhold-ok</h1>")
    clock.advance(5)
    result = probe()
    assert result.ok and result.at == clock.now()
    server.break_down()
    assert probe().ok is False


def test_scripted_fakes_repeat_their_last_outcome(spec) -> None:
    ssh = ScriptedSsh([False, True])
    assert [ssh.login(spec).ok for _ in range(4)] == [False, True, True, True]
    with pytest.raises(ValueError):
        ScriptedSsh([])
    with pytest.raises(ValueError):
        ScriptedHttp([])
