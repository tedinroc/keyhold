"""Checkout-time checks of the probe spec and the probe public key."""

from __future__ import annotations

import base64
import hashlib

import pytest
from probe_helpers import fake_ed25519_line, make_spec

from keyhold.probe.owner import (
    ProbeSpecError,
    authorized_keys_line,
    build_spec,
    check_spec,
    parse_public_key,
)
from keyhold.probe.spec import url_problem


def form(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "host": "srv.test",
        "ssh_port": "2222",
        "ssh_user": "probe",
        "probe_public_key": fake_ed25519_line(),
        "url": "http://srv.test:8080/health",
        "expect_substring": "keyhold-ok",
        "expect_sha256": "",
    }
    fields.update(overrides)
    return fields


def test_parse_public_key_accepts_ed25519_and_drops_comment() -> None:
    key = parse_public_key(fake_ed25519_line(comment="someone@laptop"))
    assert key.key_type == "ssh-ed25519"
    assert key.line == fake_ed25519_line(comment=None)
    assert "laptop" not in key.line
    digest = hashlib.sha256(base64.b64decode(key.blob)).digest()
    assert key.fingerprint == "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "empty"),
        ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----", "private key"),
        ("ssh-ed25519", "look like"),
        ("ssh-dss AAAAB3NzaC1kc3MAAACBAP", "unsupported key type"),
        ('command="sh" ' + fake_ed25519_line(), "unsupported key type"),
        ("ssh-ed25519 not*base64", "base64"),
        ("ssh-ed25519 " + base64.b64encode(b"\x00\x00\x00\x07ssh-rsa" + b"\x00" * 40).decode(), "does not match"),
        ("ssh-ed25519 " + base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x10" + b"\x01" * 16).decode(), "wrong length"),
        (fake_ed25519_line() + "\n" + fake_ed25519_line(), "single line"),
    ],
)
def test_parse_public_key_rejects(text: str, reason: str) -> None:
    with pytest.raises(ProbeSpecError, match=reason):
        parse_public_key(text)


def test_authorized_keys_line_is_restricted() -> None:
    line = authorized_keys_line(fake_ed25519_line())
    assert line == f"restrict {fake_ed25519_line(comment=None)} keyhold-probe"


def test_build_spec_normalizes_and_hash_ignores_key_comment() -> None:
    a = build_spec(**form(probe_public_key=fake_ed25519_line(comment="a@one")))  # type: ignore[arg-type]
    b = build_spec(**form(probe_public_key=fake_ed25519_line(comment="b@two")))  # type: ignore[arg-type]
    assert a.ssh_port == 2222 and a.expect_sha256 is None
    assert a.probe_public_key == fake_ed25519_line(comment=None)
    assert a.spec_hash() == b.spec_hash()
    assert len(a.spec_hash()) == 64


def test_build_spec_with_sha256_lowercases_it() -> None:
    digest = hashlib.sha256(b"page").hexdigest().upper()
    spec = build_spec(**form(expect_substring="", expect_sha256=digest))  # type: ignore[arg-type]
    assert spec.expect_substring is None
    assert spec.expect_sha256 == digest.lower()


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"host": "-oProxyCommand=sh"}, "host"),
        ({"host": "someone@srv.test"}, "host"),
        ({"host": "srv..test"}, "host"),
        ({"ssh_user": "root;id"}, "ssh user"),
        ({"ssh_user": "-l"}, "ssh_user|ssh user"),
        ({"ssh_port": "ssh"}, "port"),
        ({"ssh_port": "70000"}, "ssh_port"),
        ({"url": "ftp://srv.test/"}, "http"),
        ({"url": "http://user:pass@srv.test/"}, "user name or password"),
        ({"url": "http://srv.test/a b"}, "spaces"),
        ({"url": "http:///nohost"}, "valid host"),
        ({"expect_substring": "   "}, "expected text"),
        ({"expect_substring": "x" * 5000}, "at most"),
        ({"expect_substring": "", "expect_sha256": ""}, "exactly one"),
        ({"expect_sha256": "ab" * 32}, "exactly one"),
        ({"expect_substring": "", "expect_sha256": "zz"}, "64 hex"),
        ({"probe_public_key": "ssh-ed25519 AAAA probe"}, "too short|does not match|base64"),
    ],
)
def test_build_spec_rejects(overrides: dict[str, object], reason: str) -> None:
    with pytest.raises(ProbeSpecError, match=reason):
        build_spec(**form(**overrides))  # type: ignore[arg-type]


def test_check_spec_accepts_good_spec_and_rejects_placeholder_key() -> None:
    check_spec(make_spec())
    with pytest.raises(ProbeSpecError):
        check_spec(make_spec(probe_public_key="ssh-ed25519 AAAA probe"))


def test_url_problem_accepts_plain_urls() -> None:
    assert url_problem("https://srv.test/") is None
    assert url_problem("http://localhost:8080/status?x=1") is None
    assert url_problem("http://srv.test:99999/") is not None
