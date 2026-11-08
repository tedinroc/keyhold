"""The two checks behind an owner probe: an SSH login and an HTTP GET.

``SubprocessSsh`` logs in through the system ``ssh`` client in batch mode with the owner probe key.
The private key path lives only inside that object. It is handed to the subprocess as an argument
and nowhere else: it is not in ``repr``, not in any result detail, and it is scrubbed from anything
``ssh`` prints. The subprocess gets a minimal environment (no agent socket, no API credentials).

Host keys are handled explicitly, never with ``StrictHostKeyChecking=no``:

- ``HostKeyPolicy.ACCEPT_NEW`` (default): trust on first use, per rescue. The first probe of a rescue
  records the server's host key in that rescue's own known_hosts file; every later probe of the
  same rescue (the second stable pass, every warranty check) must present the same key.
- ``HostKeyPolicy.PINNED``: the known_hosts file must already hold the server's key.

``HttpxFetcher`` does one GET with no proxy, no redirects, ``Accept-Encoding: identity`` (so a
sha256 expectation hashes the bytes the server sent) and a size cap.

Both are injectable. ``run_probe`` accepts any object with ``login(spec)`` or ``get(url)``, the
subprocess runner can be replaced, and ``HttpxFetcher`` accepts an ``httpx.Client`` built on
``httpx.MockTransport``, so tests never touch the network.
"""

from __future__ import annotations

import enum
import ipaddress
import os
import re
import secrets
import signal
import stat
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx

from keyhold.types import ProbeSpec

USER_AGENT = "keyhold-probe/0.1"
PROBE_KEY_ENV = "KEYHOLD_PROBE_KEY"
PROBE_STATE_ENV = "KEYHOLD_PROBE_STATE_DIR"
DEFAULT_STATE_DIR = "var/probe"
MARKER_PREFIX = "keyhold-probe-"


class ProbeConfigError(RuntimeError):
    """The probe runner is misconfigured (no key, unusable paths). A deployment problem, not a
    server failure, so it is raised instead of being reported as a failed probe."""


# --------------------------------------------------------------------------------------------
# Results and protocols
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SshCheck:
    """Outcome of one SSH login. ``detail`` is safe to show anywhere: it never holds a local path."""

    ok: bool
    detail: str


@dataclass(frozen=True)
class HttpFetch:
    """One HTTP response. ``body`` holds at most the fetcher's size cap; ``truncated`` says the
    server sent more than that."""

    status: int
    body: bytes
    truncated: bool = False
    location: str | None = None


@runtime_checkable
class SshTransport(Protocol):
    def login(self, spec: ProbeSpec) -> SshCheck:
        """Log in to ``spec.host`` as ``spec.ssh_user`` with the probe key. Should not raise."""
        ...


@runtime_checkable
class HttpTransport(Protocol):
    def get(self, url: str) -> HttpFetch:
        """GET ``url``. Raises on transport failure (connect error, timeout)."""
        ...


# --------------------------------------------------------------------------------------------
# Target validation (argument-injection guard for the ssh command line)
# --------------------------------------------------------------------------------------------

_HOST_LABEL = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")
_SSH_USER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")


def valid_host(host: str) -> bool:
    """True for a DNS name (RFC 1123 labels) or an IP address literal without brackets."""
    if not host or len(host) > 253 or host.startswith("-"):
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    name = host[:-1] if host.endswith(".") else host
    return bool(name) and all(_HOST_LABEL.match(label) for label in name.split("."))


def valid_ssh_user(user: str) -> bool:
    """A conventional Unix user name: letters, digits, ``_ . -``, not starting with ``-``."""
    return bool(_SSH_USER.match(user or ""))


def target_problem(spec: ProbeSpec) -> str | None:
    """Why ``spec`` cannot be put on an ssh command line, or None."""
    if not valid_host(spec.host):
        return "host is not a valid host name or address"
    if not valid_ssh_user(spec.ssh_user):
        return "ssh user is not a valid user name"
    try:
        port = int(spec.ssh_port)
    except (TypeError, ValueError):
        return "ssh port is not a number"
    if not 1 <= port <= 65535:
        return "ssh port is out of range"
    return None


# --------------------------------------------------------------------------------------------
# Subprocess runner
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class CommandTimeout(Exception):
    """The command did not finish in time. Carries no command line (it would hold the key path)."""

    def __init__(self, timeout_s: float) -> None:
        super().__init__(f"command did not finish within {timeout_s:g} s")
        self.timeout_s = timeout_s


Runner = Callable[[Sequence[str], Mapping[str, str], float], CommandResult]


def run_command(argv: Sequence[str], env: Mapping[str, str], timeout_s: float) -> CommandResult:
    """Run ``argv`` with stdin closed, capture output, kill the whole process group on timeout."""
    proc = subprocess.Popen(  # noqa: S603 - argv is built from validated parts, no shell
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        close_fds=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise CommandTimeout(timeout_s) from None
    return CommandResult(proc.returncode, out, err)


def child_env() -> dict[str, str]:
    """Minimal environment for ssh: no SSH_AUTH_SOCK, no API credentials, C locale."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/"),
        "LANG": "C",
        "LC_ALL": "C",
    }


# --------------------------------------------------------------------------------------------
# Output scrubbing
# --------------------------------------------------------------------------------------------

_PATH_LIKE = re.compile(r"[~.]*/[^\s\"'(),;]+")
_SPACES = re.compile(r"\s+")


def scrub(text: str, hide: Sequence[str] = (), *, limit: int = 200) -> str:
    """One printable line of ``text`` with the ``hide`` strings and anything path-like removed."""
    for secret in sorted((h for h in hide if h), key=len, reverse=True):
        text = text.replace(secret, "<hidden>")
    text = _PATH_LIKE.sub("<path>", text)
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    text = _SPACES.sub(" ", text).strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


# Checked in order; the first match wins. Messages are fixed text, safe to show the agent and owner.
_SSH_ERRORS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("REMOTE HOST IDENTIFICATION HAS CHANGED", "Host key verification failed", "you have requested strict checking"),
        "host key verification failed: the server did not present the host key pinned for this rescue",
    ),
    (
        ("UNPROTECTED PRIVATE KEY FILE", "bad permissions", "Load key", "invalid format", "incorrect passphrase"),
        "the probe key file could not be used",
    ),
    (("Permission denied", "Too many authentication failures"), "login refused: the server did not accept the probe key"),
    (("Connection refused",), "connection refused"),
    (("timed out",), "connection timed out"),
    (
        ("Could not resolve hostname", "Name or service not known", "nodename nor servname"),
        "host name did not resolve",
    ),
    (("No route to host", "Network is unreachable", "Host is down"), "network unreachable"),
    (
        ("kex_exchange_identification", "Connection closed by", "Connection reset", "Broken pipe", "banner exchange"),
        "connection closed during the SSH handshake",
    ),
)


def explain_ssh_failure(stderr: str, hide: Sequence[str] = ()) -> str:
    for needles, message in _SSH_ERRORS:
        if any(n.lower() in stderr.lower() for n in needles):
            return message
    for line in stderr.splitlines():
        cleaned = scrub(line, hide, limit=160)
        if cleaned:
            return f"ssh failed: {cleaned}"
    return "ssh failed with no message"


# --------------------------------------------------------------------------------------------
# SSH
# --------------------------------------------------------------------------------------------


class HostKeyPolicy(str, enum.Enum):
    """``StrictHostKeyChecking`` value. There is deliberately no ``no``."""

    PINNED = "yes"
    ACCEPT_NEW = "accept-new"


_RESCUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def known_hosts_path(state_dir: str | os.PathLike[str], rescue_id: str) -> Path:
    """The per-rescue known_hosts file: ``{state_dir}/known_hosts/{rescue_id}``.

    Per rescue, not per spec: a reset VM overlay gets fresh host keys, and two rescues of the same
    spec must not trip over each other's pinned key.
    """
    if not _RESCUE_ID.match(rescue_id or "") or ".." in rescue_id:
        raise ProbeConfigError("rescue_id must be letters, digits, '_', '.' or '-'")
    return Path(state_dir) / "known_hosts" / rescue_id


class SubprocessSsh:
    """SSH login check through the system ``ssh`` client (OpenSSH 7.6 or later).

    The remote command is ``echo keyhold-probe-<random>``; the login counts only when ssh exits 0
    and that exact marker comes back, which proves a shell session really started.
    """

    def __init__(
        self,
        key_path: str | os.PathLike[str],
        known_hosts: str | os.PathLike[str],
        *,
        host_key_policy: HostKeyPolicy | str = HostKeyPolicy.ACCEPT_NEW,
        connect_timeout_s: int = 10,
        total_timeout_s: float = 30.0,
        ssh_binary: str = "ssh",
        keygen_binary: str = "ssh-keygen",
        runner: Runner | None = None,
    ) -> None:
        key = os.fspath(key_path)
        hosts = os.fspath(known_hosts)
        if not key:
            raise ProbeConfigError("the probe key path is empty")
        if not hosts or any(ch.isspace() for ch in hosts):
            raise ProbeConfigError("the known_hosts path must be non-empty and contain no whitespace")
        if int(connect_timeout_s) < 1:
            raise ProbeConfigError("connect_timeout_s must be at least 1")
        if total_timeout_s <= connect_timeout_s:
            raise ProbeConfigError("total_timeout_s must be longer than connect_timeout_s")
        self.__key = key
        self.__known_hosts = hosts
        self.host_key_policy = HostKeyPolicy(host_key_policy)
        self.connect_timeout_s = int(connect_timeout_s)
        self.total_timeout_s = float(total_timeout_s)
        self.ssh_binary = ssh_binary
        self.keygen_binary = keygen_binary
        self._runner: Runner = runner or run_command

    @classmethod
    def from_env(
        cls,
        rescue_id: str,
        *,
        environ: Mapping[str, str] | None = None,
        **kwargs: object,
    ) -> SubprocessSsh:
        """Build from ``KEYHOLD_PROBE_KEY`` (private key path) and ``KEYHOLD_PROBE_STATE_DIR``
        (default ``var/probe``), with a known_hosts file for this rescue."""
        env = os.environ if environ is None else environ
        key = env.get(PROBE_KEY_ENV, "")
        if not key:
            raise ProbeConfigError(f"{PROBE_KEY_ENV} is not set")
        state = env.get(PROBE_STATE_ENV) or DEFAULT_STATE_DIR
        return cls(key, known_hosts_path(state, rescue_id), **kwargs)  # type: ignore[arg-type]

    def __repr__(self) -> str:
        return (
            f"SubprocessSsh(host_key_policy={self.host_key_policy.value!r}, "
            f"connect_timeout_s={self.connect_timeout_s}, total_timeout_s={self.total_timeout_s:g})"
        )

    # -- private helpers -------------------------------------------------------------------

    def _hidden(self) -> list[str]:
        hide = [self.__key, self.__known_hosts]
        try:
            hide.append(os.path.realpath(self.__key))
        except OSError:
            pass
        base = os.path.basename(self.__key)
        if len(base) >= 3:
            hide.append(base)
        return hide

    def _key_problem(self) -> str | None:
        try:
            st = os.stat(self.__key)
        except OSError:
            return "the probe key file is missing"
        if not stat.S_ISREG(st.st_mode):
            return "the probe key file is not a regular file"
        if st.st_mode & 0o077:
            return "the probe key file is readable by other users; it must be mode 600"
        return None

    def _known_hosts_problem(self) -> str | None:
        path = Path(self.__known_hosts)
        if self.host_key_policy is HostKeyPolicy.PINNED:
            try:
                if path.stat().st_size > 0:
                    return None
            except OSError:
                pass
            return "no pinned host key is on file for this rescue"
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            return "the known_hosts folder for this rescue could not be created"
        return None

    def _argv(self, spec: ProbeSpec, remote_command: str) -> list[str]:
        return [
            self.ssh_binary,
            "-F", "/dev/null",  # ignore user and system ssh_config (no ProxyCommand, no Include)
            "-T",
            "-i", self.__key,
            "-p", str(int(spec.ssh_port)),
            "-l", spec.ssh_user,
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "IdentityAgent=none",
            "-o", "PreferredAuthentications=publickey",
            "-o", "PubkeyAuthentication=yes",
            "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no",
            "-o", f"StrictHostKeyChecking={self.host_key_policy.value}",
            "-o", f"UserKnownHostsFile={self.__known_hosts}",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "UpdateHostKeys=no",
            "-o", "CheckHostIP=no",
            "-o", f"ConnectTimeout={self.connect_timeout_s}",
            "-o", "ConnectionAttempts=1",
            "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=2",
            "-o", "ProxyCommand=none",
            "-o", "ProxyJump=none",
            "-o", "ControlMaster=no",
            "-o", "ControlPath=none",
            "-o", "ForwardAgent=no",
            "-o", "ForwardX11=no",
            "-o", "ClearAllForwardings=yes",
            "-o", "PermitLocalCommand=no",
            "-o", "LogLevel=ERROR",
            "--",
            spec.host,
            remote_command,
        ]

    # -- public API ------------------------------------------------------------------------

    def login(self, spec: ProbeSpec) -> SshCheck:
        problem = target_problem(spec) or self._key_problem() or self._known_hosts_problem()
        if problem:
            return SshCheck(False, problem)
        marker = MARKER_PREFIX + secrets.token_hex(8)
        argv = self._argv(spec, f"echo {marker}")
        try:
            out = self._runner(argv, child_env(), self.total_timeout_s)
        except (CommandTimeout, subprocess.TimeoutExpired):
            return SshCheck(False, f"ssh did not finish within {self.total_timeout_s:g} s")
        except FileNotFoundError:
            return SshCheck(False, "the ssh client is not installed on the probe runner")
        except Exception as exc:  # noqa: BLE001 - any runner failure is a failed login, never a crash
            return SshCheck(False, f"ssh could not start ({type(exc).__name__})")
        stdout = out.stdout.decode("utf-8", "replace")
        if out.returncode == 0 and marker in stdout.split():
            return SshCheck(True, f"logged in as {spec.ssh_user}")
        if out.returncode == 0:
            return SshCheck(False, "logged in, but the check command did not print its marker")
        if out.returncode != 255:
            return SshCheck(False, f"logged in, but the check command exited with status {out.returncode}")
        return SshCheck(False, explain_ssh_failure(out.stderr.decode("utf-8", "replace"), self._hidden()))

    def public_key_line(self) -> str:
        """The ``type base64`` public half of the probe key, without a comment (a default comment
        is ``user@machine`` of the runner, which should not be shown)."""
        if self._key_problem():
            raise ProbeConfigError("the probe key file is missing or not private")
        try:
            out = self._runner([self.keygen_binary, "-y", "-f", self.__key], child_env(), 15.0)
        except Exception:  # noqa: BLE001
            raise ProbeConfigError("could not read the probe public key") from None
        parts = out.stdout.decode("utf-8", "replace").split()
        if out.returncode != 0 or len(parts) < 2:
            raise ProbeConfigError("could not read the probe public key")
        return f"{parts[0]} {parts[1]}"


def ensure_probe_key(
    path: str | os.PathLike[str],
    *,
    runner: Runner | None = None,
    keygen_binary: str = "ssh-keygen",
) -> Path:
    """Create an unencrypted ed25519 probe key at ``path`` (mode 600) unless one is already there.

    The comment is ``keyhold-probe`` rather than the default ``user@machine``.
    """
    key = Path(path)
    if key.exists():
        return key
    key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    argv = [keygen_binary, "-q", "-t", "ed25519", "-N", "", "-C", "keyhold-probe", "-f", os.fspath(key)]
    try:
        out = (runner or run_command)(argv, child_env(), 30.0)
    except Exception:  # noqa: BLE001
        raise ProbeConfigError("ssh-keygen could not create the probe key") from None
    if out.returncode != 0 or not key.exists():
        raise ProbeConfigError("ssh-keygen could not create the probe key")
    os.chmod(key, 0o600)
    return key


# --------------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------------


class HttpxFetcher:
    """One GET of the owner's URL: no proxy from the environment, no redirects, no compression,
    at most ``max_bytes`` read, and the whole read bounded by ``timeout_s``."""

    def __init__(
        self,
        *,
        timeout_s: float = 10.0,
        max_bytes: int = 2 * 1024 * 1024,
        follow_redirects: bool = False,
        verify_tls: bool = True,
        client: httpx.Client | None = None,
    ) -> None:
        if timeout_s <= 0 or max_bytes <= 0:
            raise ProbeConfigError("timeout_s and max_bytes must be positive")
        self.timeout_s = float(timeout_s)
        self.max_bytes = int(max_bytes)
        self.follow_redirects = follow_redirects
        self.verify_tls = verify_tls
        self._client = client

    def make_client(self) -> httpx.Client:
        return httpx.Client(
            timeout=httpx.Timeout(self.timeout_s),
            follow_redirects=False,
            verify=self.verify_tls,
            trust_env=False,  # a proxy from the environment must not stand between probe and server
        )

    def get(self, url: str) -> HttpFetch:
        own = self._client is None
        client = self.make_client() if own else self._client
        assert client is not None
        deadline = time.monotonic() + self.timeout_s
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity", "Cache-Control": "no-cache"}
        try:
            with client.stream("GET", url, headers=headers, follow_redirects=self.follow_redirects) as resp:
                body = bytearray()
                truncated = False
                for chunk in resp.iter_bytes():
                    body.extend(chunk)
                    if len(body) > self.max_bytes:
                        truncated = True
                        del body[self.max_bytes :]
                        break
                    if time.monotonic() > deadline:
                        raise httpx.ReadTimeout("the page took too long to arrive", request=resp.request)
                return HttpFetch(resp.status_code, bytes(body), truncated, resp.headers.get("location"))
        finally:
            if own:
                client.close()
