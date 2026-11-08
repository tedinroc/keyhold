"""Smoke test: the package imports, the shared types behave, and the network guard is active."""

from __future__ import annotations

import importlib
import socket
from decimal import Decimal

import pytest

from keyhold.types import (
    ALLOWED_TRANSITIONS,
    Console,
    MoneyOp,
    PaymentGateway,
    ProbeSpec,
    Quote,
    RescueState,
    Tier,
    can_transition,
    money,
    request_id_for,
)

MODULES = [
    "keyhold",
    "keyhold.types",
    "keyhold.pay.client",
    "keyhold.pay.ledger",
    "keyhold.pay.reconciler",
    "keyhold.pay.webhooks",
    "keyhold.pay.gate",
    "keyhold.console.base",
    "keyhold.console.qemu",
    "keyhold.console.fake",
    "keyhold.console.image",
    "keyhold.faults",
    "keyhold.agent.model",
    "keyhold.agent.firewall",
    "keyhold.agent.quote",
    "keyhold.agent.loop",
    "keyhold.probe.owner",
    "keyhold.rescue",
    "web.app",
]


def test_smoke() -> None:
    for name in MODULES:
        assert importlib.import_module(name).__doc__, f"{name} has no docstring"

    # Prices and the quote hash.
    assert [t.price for t in Tier] == [Decimal("9.00"), Decimal("19.00"), Decimal("29.00")]
    q = Quote.build("sshd_config_error", ["sshd: /etc/ssh/sshd_config line 3: Bad option"], Tier.T1, "fix line 3")
    assert q.amount == Decimal("9.00") and q.hash_ok() and len(q.quote_hash) == 64
    assert Quote.build("sshd_config_error", q.evidence, Tier.T2, "fix line 3").quote_hash != q.quote_hash
    with pytest.raises(TypeError):
        money(9.0)  # type: ignore[arg-type]

    # Request ids are deterministic per (rescue, op).
    assert request_id_for("r1", MoneyOp.CAPTURE) == "cap-r1"
    assert request_id_for("r1", MoneyOp.VOID) == "void-r1"
    assert request_id_for("r1", MoneyOp.REFUND) == "refund-r1"

    # State machine: VOIDED reachable from every pre-capture state, never after capture.
    for s in (RescueState.QUOTED, RescueState.HOLD_PENDING, RescueState.HELD, RescueState.ACTIVE, RescueState.PROBING):
        assert can_transition(s, RescueState.VOIDED)
    # CAPTURED -> VOIDED exists only for a capture PayPal declined; the ledger guard enforces it.
    assert can_transition(RescueState.CAPTURED, RescueState.VOIDED)
    assert not can_transition(RescueState.WARRANTY_OK, RescueState.VOIDED)
    assert all(not ALLOWED_TRANSITIONS[s] for s in RescueState if s.terminal)

    # Probe spec: hash is stable and exactly one expectation is required.
    spec = ProbeSpec("srv.test", 22, "probe", "ssh-ed25519 AAAA probe", "http://srv.test/", expect_substring="ok")
    assert spec.spec_hash() == ProbeSpec(**spec.to_dict()).spec_hash()
    with pytest.raises(ValueError):
        ProbeSpec("srv.test", 22, "probe", "ssh-ed25519 AAAA probe", "http://srv.test/")

    # Protocols are importable for isinstance checks by the builders' fakes.
    assert isinstance(Console, type) and isinstance(PaymentGateway, type)

    # Tests cannot reach the network.
    with pytest.raises(RuntimeError), socket.socket() as sock:
        sock.connect(("unreachable.invalid", 443))
