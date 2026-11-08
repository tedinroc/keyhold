"""SubprocessSsh: the ssh command line, host-key policy, failure messages and key-path hygiene.

Most tests swap the subprocess runner for a fake. The "real subprocess" tests run small shell
scripts that pretend to be ssh, so the timeout and environment handling are exercised without
opening any connection.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import textwrap
import time

import pytest
from probe_helpers import FakeRunner, make_spec

from keyhold.probe.owner import (
    HostKeyPolicy,
    ProbeConfigError,
    SubprocessSsh,
    ensure_probe_key,
    known_hosts_path,
    run_probe,
)
from keyhold.probe.fake import ScriptedHttp
from keyhold.probe.transports import CommandResult, CommandTimeout, scrub


def opt(argv: list[str], name: str) -> str:
    """Value of ``-o name=value`` in argv."""
    values = [argv[i + 1].split("=", 1)[1] for i, a in enumerate(argv[:-1]) if a == "-o" and argv[i + 1].startswith(name + "=")]
    assert len(values) == 1, f"{name} appears {len(values)} times"
    return values[0]


def test_command_line_is_batch_mode_with_explicit_host_key_checking(spec, key_file, known_hosts) -> None:
    runner = FakeRunner()
    ssh = SubprocessSsh(key_file, known_hosts, connect_timeout_s=7, total_timeout_s=20, runner=runner)

    check = ssh.login(spec)

    assert check.ok and check.detail == "logged in as probe"
    argv, env, timeout = runner.calls[0]
    assert argv[0] == "ssh" and argv[1:3] == ["-F", "/dev/null"]
    assert argv[argv.index("-i") + 1] == str(key_file)
    assert argv[argv.index("-p") + 1] == "2222"
    assert argv[argv.index("-l") + 1] == "probe"
    assert opt(argv, "BatchMode") == "yes"
    assert opt(argv, "StrictHostKeyChecking") == "accept-new"
    assert opt(argv, "UserKnownHostsFile") == str(known_hosts)
    assert opt(argv, "GlobalKnownHostsFile") == "/dev/null"
    assert opt(argv, "ConnectTimeout") == "7"
    assert opt(argv, "IdentitiesOnly") == "yes"
    assert opt(argv, "IdentityAgent") == "none"
    assert opt(argv, "PasswordAuthentication") == "no"
    assert opt(argv, "KbdInteractiveAuthentication") == "no"
    assert opt(argv, "PreferredAuthentications") == "publickey"
    assert opt(argv, "ForwardAgent") == "no"
    assert opt(argv, "ProxyCommand") == "none"
    assert "StrictHostKeyChecking=no" not in argv
    # The destination comes after "--", so a host can never be read as an option.
    assert argv[-3:-1] == ["--", "srv.test"]
    assert argv[-1].startswith("echo keyhold-probe-")
    assert timeout == 20
    assert known_hosts.parent.is_dir()
    assert stat.S_IMODE(known_hosts.parent.stat().st_mode) & 0o077 == 0


def test_each_login_uses_a_fresh_marker(spec, key_file, known_hosts) -> None:
    runner = FakeRunner()
    ssh = SubprocessSsh(key_file, known_hosts, runner=runner)
    ssh.login(spec)
    ssh.login(spec)
    assert runner.calls[0][0][-1] != runner.calls[1][0][-1]


def test_login_needs_the_exact_marker(spec, key_file, known_hosts) -> None:
    ssh = SubprocessSsh(key_file, known_hosts, runner=FakeRunner(stdout=b"keyhold-probe-0000000000000000\n"))
    check = ssh.login(spec)
    assert not check.ok and "marker" in check.detail


def test_remote_command_failure_is_not_a_pass(spec, key_file, known_hosts) -> None:
    ssh = SubprocessSsh(key_file, known_hosts, runner=FakeRunner(returncode=127, stdout=b""))
    check = ssh.login(spec)
    assert not check.ok and "status 127" in check.detail


def test_pinned_policy_requires_a_pinned_key(spec, key_file, known_hosts) -> None:
    runner = FakeRunner()
    ssh = SubprocessSsh(key_file, known_hosts, host_key_policy=HostKeyPolicy.PINNED, runner=runner)
    check = ssh.login(spec)
    assert not check.ok and "no pinned host key" in check.detail
    assert runner.calls == []

    known_hosts.parent.mkdir(parents=True)
    known_hosts.write_text("[srv.test]:2222 ssh-ed25519 AAAA\n")
    assert ssh.login(spec).ok
    assert opt(runner.argv, "StrictHostKeyChecking") == "yes"


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("Permission denied (publickey).", "login refused: the server did not accept the probe key"),
        ("ssh: connect to host srv.test port 2222: Connection refused", "connection refused"),
        ("ssh: connect to host srv.test port 2222: Operation timed out", "connection timed out"),
        ("Connection timed out during banner exchange", "connection timed out"),
        ("ssh: Could not resolve hostname srv.test: nodename nor servname provided", "host name did not resolve"),
        ("ssh: connect to host srv.test port 2222: No route to host", "network unreachable"),
        ("kex_exchange_identification: read: Connection reset by peer", "connection closed during the SSH handshake"),
        (
            "@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\nHost key verification failed.",
            "host key verification failed: the server did not present the host key pinned for this rescue",
        ),
        (
            "No ED25519 host key is known for [srv.test]:2222 and you have requested strict checking.",
            "host key verification failed: the server did not present the host key pinned for this rescue",
        ),
        ("Load key \"{key}\": invalid format", "the probe key file could not be used"),
        ("", "ssh failed with no message"),
    ],
)
def test_failures_get_fixed_plain_messages(spec, key_file, known_hosts, stderr: str, expected: str) -> None:
    runner = FakeRunner(returncode=255, stdout=b"", stderr=stderr.format(key=key_file).encode())
    check = SubprocessSsh(key_file, known_hosts, runner=runner).login(spec)
    assert not check.ok
    assert check.detail == expected


def test_key_path_never_appears_in_details(spec, key_file, known_hosts, tmp_path) -> None:
    noisy = f"strange failure near {key_file} ({key_file.name}) and {known_hosts}\nsecond line"
    ssh = SubprocessSsh(key_file, known_hosts, runner=FakeRunner(returncode=255, stdout=b"", stderr=noisy.encode()))

    check = ssh.login(spec)
    result = run_probe(spec, ssh=ssh, http=ScriptedHttp([b"keyhold-ok"]))

    for text in (check.detail, result.detail, repr(ssh)):
        assert str(key_file) not in text
        assert key_file.name not in text
        assert str(tmp_path) not in text
    assert check.detail.startswith("ssh failed: strange failure near")
    assert "second line" not in check.detail


def test_scrub_removes_paths_and_control_characters() -> None:
    assert scrub("bad \x1b[31m /home/a/.ssh/id \t thing", ["thing"]) == "bad [31m <path> <hidden>"
    assert len(scrub("x" * 500, limit=50)) == 50


def test_missing_or_shared_key_file_fails_without_running_ssh(spec, key_file, known_hosts, tmp_path) -> None:
    runner = FakeRunner()
    missing = SubprocessSsh(tmp_path / "nope", known_hosts, runner=runner).login(spec)
    assert not missing.ok and missing.detail == "the probe key file is missing"

    os.chmod(key_file, 0o644)
    shared = SubprocessSsh(key_file, known_hosts, runner=runner).login(spec)
    assert not shared.ok and "mode 600" in shared.detail
    assert runner.calls == []


@pytest.mark.parametrize(
    "overrides",
    [{"host": "-oProxyCommand=touch"}, {"host": "someone@srv.test"}, {"ssh_user": "a;b"}],
)
def test_unsafe_targets_never_reach_the_command_line(key_file, known_hosts, overrides) -> None:
    runner = FakeRunner()
    check = SubprocessSsh(key_file, known_hosts, runner=runner).login(make_spec(**overrides))
    assert not check.ok
    assert runner.calls == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (CommandTimeout(30), "ssh did not finish within 30 s"),
        (subprocess.TimeoutExpired(["ssh"], 30), "ssh did not finish within 30 s"),
        (FileNotFoundError("ssh"), "the ssh client is not installed on the probe runner"),
        (OSError("boom"), "ssh could not start (OSError)"),
    ],
)
def test_runner_errors_are_failed_logins(spec, key_file, known_hosts, error, expected) -> None:
    check = SubprocessSsh(key_file, known_hosts, runner=FakeRunner(raises=error)).login(spec)
    assert not check.ok and check.detail == expected


def test_child_environment_has_no_agent_socket_or_credentials(spec, key_file, known_hosts, monkeypatch) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-value")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "test-value")
    runner = FakeRunner()
    SubprocessSsh(key_file, known_hosts, runner=runner).login(spec)
    env = runner.calls[0][1]
    assert set(env) == {"PATH", "HOME", "LANG", "LC_ALL"}


def test_constructor_validation(key_file, tmp_path) -> None:
    with pytest.raises(ProbeConfigError):
        SubprocessSsh(key_file, tmp_path / "has space" / "kh")
    with pytest.raises(ProbeConfigError):
        SubprocessSsh(key_file, tmp_path / "kh", connect_timeout_s=10, total_timeout_s=10)
    with pytest.raises(ValueError):
        SubprocessSsh(key_file, tmp_path / "kh", host_key_policy="no")


def test_from_env_uses_a_known_hosts_file_per_rescue(key_file, tmp_path) -> None:
    with pytest.raises(ProbeConfigError, match="KEYHOLD_PROBE_KEY"):
        SubprocessSsh.from_env("r1", environ={})
    env = {"KEYHOLD_PROBE_KEY": str(key_file), "KEYHOLD_PROBE_STATE_DIR": str(tmp_path / "state")}
    runner = FakeRunner()
    ssh = SubprocessSsh.from_env("r1", environ=env, runner=runner)
    ssh.login(make_spec())
    assert opt(runner.argv, "UserKnownHostsFile") == str(tmp_path / "state" / "known_hosts" / "r1")
    assert known_hosts_path("var/probe", "r2").as_posix() == "var/probe/known_hosts/r2"
    for bad in ("", "../x", "a/b", "-r", "a b"):
        with pytest.raises(ProbeConfigError):
            known_hosts_path("var/probe", bad)


def test_public_key_line_strips_the_comment(key_file, known_hosts) -> None:
    runner = FakeRunner(stdout=b"ssh-ed25519 AAAAC3Nza someone@laptop\n")
    ssh = SubprocessSsh(key_file, known_hosts, runner=runner)
    assert ssh.public_key_line() == "ssh-ed25519 AAAAC3Nza"
    assert runner.argv[:3] == ["ssh-keygen", "-y", "-f"]

    with pytest.raises(ProbeConfigError):
        SubprocessSsh(key_file, known_hosts, runner=FakeRunner(returncode=1, stdout=b"")).public_key_line()


# ---------------------------------------------------------------------------------------------
# Real subprocess, fake ssh binary (shell scripts that never open a connection)
# ---------------------------------------------------------------------------------------------


def script(tmp_path, name: str, body: str):
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
    path.chmod(0o755)
    return str(path)


def test_real_subprocess_success_and_environment(spec, key_file, known_hosts, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-value")
    dump = tmp_path / "seen.txt"
    fake_ssh = script(
        tmp_path,
        "fake-ssh",
        f"""
        env > "{dump}"
        for a in "$@"; do last="$a"; done
        printf '%s\\n' "${{last#echo }}"
        """,
    )
    ssh = SubprocessSsh(key_file, known_hosts, ssh_binary=fake_ssh)
    check = ssh.login(spec)
    assert check.ok, check.detail
    seen = dump.read_text()
    assert "ANTHROPIC_API_KEY" not in seen and "SSH_AUTH_SOCK" not in seen
    assert "LC_ALL=C" in seen


def test_real_subprocess_permission_denied(spec, key_file, known_hosts, tmp_path) -> None:
    fake_ssh = script(
        tmp_path,
        "fake-ssh",
        """
        echo "probe@srv.test: Permission denied (publickey)." >&2
        exit 255
        """,
    )
    check = SubprocessSsh(key_file, known_hosts, ssh_binary=fake_ssh).login(spec)
    assert not check.ok and check.detail.startswith("login refused")


def test_real_subprocess_timeout_kills_ssh(spec, key_file, known_hosts, tmp_path) -> None:
    fake_ssh = script(tmp_path, "fake-ssh", "exec sleep 30\n")
    ssh = SubprocessSsh(key_file, known_hosts, ssh_binary=fake_ssh, connect_timeout_s=1, total_timeout_s=1.2)
    started = time.monotonic()
    check = ssh.login(spec)
    assert time.monotonic() - started < 10
    assert not check.ok and check.detail == "ssh did not finish within 1.2 s"


def test_real_subprocess_missing_binary(spec, key_file, known_hosts, tmp_path) -> None:
    check = SubprocessSsh(key_file, known_hosts, ssh_binary=str(tmp_path / "no-ssh")).login(spec)
    assert not check.ok and "not installed" in check.detail


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen not installed")
def test_ensure_probe_key_creates_a_private_key(tmp_path) -> None:
    key = ensure_probe_key(tmp_path / "probe" / "id_ed25519")
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert ensure_probe_key(key) == key  # second call leaves it alone
    line = SubprocessSsh(key, tmp_path / "kh").public_key_line()
    assert line.startswith("ssh-ed25519 ") and len(line.split()) == 2


def test_ensure_probe_key_reports_keygen_failure(tmp_path) -> None:
    def failing(argv, env, timeout):
        return CommandResult(1, b"", b"nope")

    with pytest.raises(ProbeConfigError):
        ensure_probe_key(tmp_path / "k", runner=failing)
