"""Shared helpers for the owner-probe tests: a made-up public key, a spec builder and a fake runner.

The public key is built from fixed bytes; it is not a real key and has no private half.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence

from keyhold.probe.transports import CommandResult
from keyhold.types import ProbeSpec


def fake_ed25519_line(seed: int = 7, comment: str | None = "someone@laptop") -> str:
    name = b"ssh-ed25519"
    raw = len(name).to_bytes(4, "big") + name + (32).to_bytes(4, "big") + bytes((seed + i) % 256 for i in range(32))
    line = "ssh-ed25519 " + base64.b64encode(raw).decode("ascii")
    return f"{line} {comment}" if comment else line


def make_spec(**overrides: object) -> ProbeSpec:
    fields: dict[str, object] = {
        "host": "srv.test",
        "ssh_port": 2222,
        "ssh_user": "probe",
        "probe_public_key": fake_ed25519_line(comment=None),
        "url": "http://srv.test:8080/health",
        "expect_substring": "keyhold-ok",
    }
    fields.update(overrides)
    if "expect_sha256" in overrides and "expect_substring" not in overrides:
        fields["expect_substring"] = None
    return ProbeSpec(**fields)  # type: ignore[arg-type]


class FakeRunner:
    """Stands in for ``run_command``. By default it answers like a successful login: it prints the
    marker that the remote ``echo`` command would print."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes | None = None,
        stderr: bytes = b"",
        raises: BaseException | None = None,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.raises = raises
        self.calls: list[tuple[list[str], dict[str, str], float]] = []

    def __call__(self, argv: Sequence[str], env: Mapping[str, str], timeout_s: float) -> CommandResult:
        self.calls.append((list(argv), dict(env), timeout_s))
        if self.raises is not None:
            raise self.raises
        stdout = self.stdout
        if stdout is None:
            remote = argv[-1]
            stdout = (remote[len("echo ") :] + "\n").encode() if remote.startswith("echo ") else b""
        return CommandResult(self.returncode, stdout, self.stderr)

    @property
    def argv(self) -> list[str]:
        return self.calls[-1][0]
