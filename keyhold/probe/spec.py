"""Checkout-time checks for a ``ProbeSpec`` (the dataclass itself lives in ``keyhold.types``).

``build_spec`` is what the checkout uses: it constructs the spec, checks every field, and stores
the probe public key as ``type base64`` with no comment, so the spec hash (which goes into the
order's custom_id) does not depend on a comment and no ``user@machine`` comment is kept.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from urllib.parse import urlsplit

from keyhold.types import ProbeSpec

from .transports import valid_host, valid_ssh_user

MAX_URL_LENGTH = 2048
MAX_EXPECTED_TEXT = 4096

PUBLIC_KEY_TYPES: frozenset[str] = frozenset(
    {
        "ssh-ed25519",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ssh-ed25519@openssh.com",
        "sk-ecdsa-sha2-nistp256@openssh.com",
        "ssh-rsa",
    }
)


class ProbeSpecError(ValueError):
    """A probe spec field is unusable. The message is plain text for the owner."""


@dataclass(frozen=True)
class PublicKey:
    key_type: str
    blob: str  # base64 key data exactly as written in the key line

    @property
    def line(self) -> str:
        return f"{self.key_type} {self.blob}"

    @property
    def fingerprint(self) -> str:
        """OpenSSH-style ``SHA256:...`` fingerprint, for showing the owner which key to install."""
        digest = hashlib.sha256(base64.b64decode(self.blob)).digest()
        return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def parse_public_key(text: str) -> PublicKey:
    """Parse one OpenSSH public key line (``type base64 [comment]``) and check the key data."""
    text = (text or "").strip()
    if "PRIVATE KEY" in text:
        raise ProbeSpecError("this is a private key; paste the public key (the .pub line) instead")
    if not text:
        raise ProbeSpecError("the probe public key is empty")
    if "\n" in text or "\r" in text:
        raise ProbeSpecError("the probe public key must be a single line")
    parts = text.split()
    if len(parts) < 2:
        raise ProbeSpecError("the probe public key must look like 'ssh-ed25519 AAAA...'")
    key_type, blob = parts[0], parts[1]
    if key_type not in PUBLIC_KEY_TYPES:
        raise ProbeSpecError(f"unsupported key type {key_type[:40]!r}; use an ssh-ed25519 key")
    try:
        raw = base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError):
        raise ProbeSpecError("the probe public key data is not valid base64") from None
    if len(raw) < 4:
        raise ProbeSpecError("the probe public key data is too short")
    name_len = int.from_bytes(raw[:4], "big")
    if raw[4 : 4 + name_len] != key_type.encode("ascii") or len(raw) <= 4 + name_len:
        raise ProbeSpecError("the probe public key data does not match its key type")
    if key_type == "ssh-ed25519" and len(raw) != 4 + 11 + 4 + 32:
        raise ProbeSpecError("the ssh-ed25519 key data has the wrong length")
    return PublicKey(key_type, blob)


def authorized_keys_line(public_key: str | PublicKey) -> str:
    """The line the owner adds to ``~/.ssh/authorized_keys`` of the probe user.

    ``restrict`` turns off port, agent and X11 forwarding and the pty; a plain shell command (the
    probe's ``echo``) still runs, which is all the probe needs.
    """
    key = public_key if isinstance(public_key, PublicKey) else parse_public_key(public_key)
    return f"restrict {key.line} keyhold-probe"


def url_problem(url: str) -> str | None:
    if not url or len(url) > MAX_URL_LENGTH:
        return f"the URL must be 1-{MAX_URL_LENGTH} characters"
    if any(ch.isspace() or not ch.isprintable() for ch in url):
        return "the URL must not contain spaces or control characters"
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises ValueError for a bad port
    except ValueError:
        return "the URL could not be parsed"
    if parts.scheme not in ("http", "https"):
        return "the URL must start with http:// or https://"
    if parts.username is not None or parts.password is not None:
        return "the URL must not contain a user name or password"
    if not parts.hostname or not valid_host(parts.hostname):
        return "the URL has no valid host"
    return None


def check_spec(spec: ProbeSpec) -> None:
    """Raise ``ProbeSpecError`` with a plain reason if ``spec`` should not be accepted at checkout."""
    if not valid_host(spec.host):
        raise ProbeSpecError("host must be a host name or an IP address")
    if not valid_ssh_user(spec.ssh_user):
        raise ProbeSpecError("ssh user must be a plain user name (letters, digits, '_', '.', '-')")
    parse_public_key(spec.probe_public_key)
    problem = url_problem(spec.url)
    if problem:
        raise ProbeSpecError(problem)
    if spec.expect_substring is not None:
        if not spec.expect_substring.strip():
            raise ProbeSpecError("the expected text must not be empty")
        if len(spec.expect_substring) > MAX_EXPECTED_TEXT:
            raise ProbeSpecError(f"the expected text must be at most {MAX_EXPECTED_TEXT} characters")


def build_spec(
    *,
    host: str,
    ssh_port: int | str,
    ssh_user: str,
    probe_public_key: str,
    url: str,
    expect_substring: str | None = None,
    expect_sha256: str | None = None,
) -> ProbeSpec:
    """Construct and check a spec at checkout. The public key is stored without its comment.

    An empty form field counts as not set, so a form can send both expectation fields.
    """
    try:
        port = int(ssh_port)
    except (TypeError, ValueError):
        raise ProbeSpecError("ssh port must be a number") from None
    key = parse_public_key(probe_public_key)
    substring = expect_substring if expect_substring else None
    digest = expect_sha256.strip().lower() if expect_sha256 and expect_sha256.strip() else None
    try:
        spec = ProbeSpec(
            host=host.strip(),
            ssh_port=port,
            ssh_user=ssh_user.strip(),
            probe_public_key=key.line,
            url=url.strip(),
            expect_substring=substring,
            expect_sha256=digest,
        )
    except ValueError as exc:
        raise ProbeSpecError(str(exc)) from None
    check_spec(spec)
    return spec
