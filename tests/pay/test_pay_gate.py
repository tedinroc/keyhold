"""HoldLease and GatedConsole: no keystroke without a live PayPal hold for this rescue."""

from __future__ import annotations

import threading
from dataclasses import replace
from decimal import Decimal

import pytest
from pay_helpers import new_rescue, to_held

from keyhold.console.fake import FakeConsole, Screen
from keyhold.pay.client import FakePayPal, fake_error
from keyhold.pay.gate import GatedConsole, HoldLease
from keyhold.types import HoldRevoked, RescueState, Tier


class TamperedGateway:
    """FakePayPal whose get_authorization answer is changed (or replaced by an error)."""

    def __init__(self, inner: FakePayPal, *, error=None, **changes) -> None:
        self.inner = inner
        self.error = error
        self.changes = changes

    def get_authorization(self, auth_id: str):
        if self.error is not None:
            raise self.error
        return replace(self.inner.get_authorization(auth_id), **self.changes)

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


def gets(fake: FakePayPal) -> int:
    return sum(1 for c in fake.calls if c.op == "get_authorization")


def lease_for(ledger, gateway, rec, clock, **kw) -> HoldLease:
    kw.setdefault("check_every", 1000)
    kw.setdefault("ttl_s", 10_000.0)
    return HoldLease(ledger, gateway, rec.id, clock=clock, **kw)


def console() -> FakeConsole:
    return FakeConsole({"shell": Screen([], prompt="# ")})


# -- the grant ---------------------------------------------------------------------------------


def test_grant_needs_paypal_to_show_created_for_the_quote(ledger, pay, fake, clock) -> None:
    rec = to_held(ledger, pay, fake)
    lease = lease_for(ledger, fake, rec, clock)
    lease.require()
    assert lease.checks == 1 and gets(fake) == 1
    assert lease.authorization is not None and lease.authorization.status == "CREATED"
    kinds = [e.kind for e in ledger.events(rec.id)]
    assert "gate_check" in kinds


def test_no_authorization_means_no_input(ledger, pay, fake, clock) -> None:
    rec = new_rescue(ledger)
    pay.create_hold(rec.id)  # HOLD_PENDING, never authorized
    lease = lease_for(ledger, fake, ledger.get(rec.id), clock)
    with pytest.raises(HoldRevoked, match="HOLD_PENDING"):
        lease.require()
    assert gets(fake) == 0


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"amount": Decimal("5.00")}, "below the quote"),
        ({"status": "VOIDED"}, "VOIDED"),
        ({"status": "PENDING"}, "PENDING"),
        ({"status": "CAPTURED"}, "CAPTURED"),
        ({"id": "A-SOMEONE-ELSE"}, "different authorization"),
        ({"order_id": "O-OTHER"}, "another order"),
        ({"custom_id": "f" * 64}, "another probe spec"),
        ({"invoice_id": "keyhold-other-rescue"}, "another rescue"),
        ({"order_id": None}, "does not name this rescue's order"),
        ({"custom_id": None}, "does not carry the probe spec hash"),
        ({"invoice_id": None}, "does not carry this rescue's invoice id"),
        ({"expiration_time": "2020-01-01T00:00:00Z"}, "expired"),
    ],
)
def test_grant_refuses_anything_but_a_live_hold_for_this_rescue(ledger, pay, fake, clock, changes, message) -> None:
    rec = to_held(ledger, pay, fake)
    lease = lease_for(ledger, TamperedGateway(fake, **changes), rec, clock)
    with pytest.raises(HoldRevoked, match=message):
        lease.require()
    assert lease.revoked


def test_more_than_the_quote_is_fine(ledger, pay, fake, clock) -> None:
    rec = to_held(ledger, pay, fake)
    lease = lease_for(ledger, TamperedGateway(fake, amount=Decimal("100.00")), rec, clock)
    lease.require()


def test_missing_authorization_revokes(ledger, pay, fake, clock) -> None:
    rec = to_held(ledger, pay, fake)
    gone = TamperedGateway(fake, error=fake_error("RESOURCE_NOT_FOUND", "no such authorization"))
    lease = lease_for(ledger, gone, rec, clock)
    with pytest.raises(HoldRevoked, match="no authorization"):
        lease.require()


def test_a_brief_paypal_outage_is_retried_then_fails_closed(ledger, pay, fake, clock) -> None:
    rec = to_held(ledger, pay, fake)
    fake.inject("get_authorization", "INTERNAL_SERVER_ERROR", times=2)
    lease = lease_for(ledger, fake, rec, clock, retries=2)
    lease.require()  # third attempt answers
    fake.inject("get_authorization", "INTERNAL_SERVER_ERROR", times=3)
    with pytest.raises(HoldRevoked, match="could not confirm"):
        lease.check()
    fake.clear_injections()
    with pytest.raises(HoldRevoked):
        lease.require()  # revoked for good


# -- re-checks ---------------------------------------------------------------------------------


def test_rechecks_every_n_actions(ledger, pay, fake, clock) -> None:
    rec = to_held(ledger, pay, fake)
    lease = lease_for(ledger, fake, rec, clock, check_every=3)
    for _ in range(7):
        lease.require()
    # first call, then after every third action: calls 1, 4 and 7
    assert gets(fake) == 3
    assert lease.actions == 7


def test_rechecks_when_the_ttl_runs_out(ledger, pay, fake, clock) -> None:
    rec = to_held(ledger, pay, fake)
    lease = lease_for(ledger, fake, rec, clock, ttl_s=15.0)
    lease.require()
    clock.advance(14)
    lease.require()
    assert gets(fake) == 1
    clock.advance(1)
    lease.require()
    assert gets(fake) == 2


def test_void_at_paypal_is_seen_on_the_next_check(ledger, pay, fake, clock) -> None:
    rec = to_held(ledger, pay, fake)
    lease = lease_for(ledger, fake, rec, clock, check_every=2)
    lease.require()
    lease.require()
    fake.void_externally(rec.authorization_id)
    with pytest.raises(HoldRevoked, match="VOIDED"):
        lease.require()
    fake.set_authorization_status(rec.authorization_id, "CREATED")
    with pytest.raises(HoldRevoked):
        lease.require()  # never grants again


def test_expiry_at_paypal_revokes(ledger, pay, fake, clock) -> None:
    rec = to_held(ledger, pay, fake)
    lease = lease_for(ledger, fake, rec, clock, check_every=1)
    lease.require()
    fake.expire(rec.authorization_id)
    with pytest.raises(HoldRevoked, match="expired"):
        lease.require()


def test_expiry_is_also_caught_between_checks(ledger, clock, db_path) -> None:
    from keyhold.pay.ledger import Payments

    fake = FakePayPal(clock=clock, auth_valid_seconds=600)
    pay = Payments(ledger, fake)
    rec = to_held(ledger, pay, fake)
    lease = lease_for(ledger, fake, rec, clock, expiry_margin_s=120)
    lease.require()
    clock.advance(481)  # 119 s left: inside the margin
    with pytest.raises(HoldRevoked, match="expired"):
        lease.require()
    assert gets(fake) == 1  # no PayPal call was needed to see it


def test_ledger_void_cuts_input_without_asking_paypal(ledger, pay, fake, clock) -> None:
    rec = to_held(ledger, pay, fake)
    lease = lease_for(ledger, fake, rec, clock)
    lease.require()
    pay.void(rec.id, "owner pressed Stop")
    with pytest.raises(HoldRevoked):
        lease.require()
    assert gets(fake) == 1


def test_a_started_capture_ends_the_session(ledger, pay, fake, clock) -> None:
    rec = to_held(ledger, pay, fake)
    lease = lease_for(ledger, fake, rec, clock)
    ledger.transition(rec.id, RescueState.ACTIVE)
    lease.require()
    ledger.transition(rec.id, RescueState.PROBING)
    with pytest.raises(HoldRevoked, match="PROBING"):
        lease.require()


def test_deadline_revokes(ledger, pay, fake, clock) -> None:
    rec = to_held(ledger, pay, fake)
    lease = lease_for(ledger, fake, rec, clock, deadline=clock.now() + 60)
    lease.require()
    clock.advance(60)
    with pytest.raises(HoldRevoked, match="time limit"):
        lease.require()


def test_revoke_keeps_the_first_reason_and_logs_once(ledger, pay, fake, clock) -> None:
    rec = to_held(ledger, pay, fake)
    lease = lease_for(ledger, fake, rec, clock)
    lease.revoke("the owner pressed Stop")
    lease.revoke("something else")
    assert lease.reason == "the owner pressed Stop"
    assert len(ledger.events(rec.id, kind="gate_revoked")) == 1
    with pytest.raises(HoldRevoked, match="owner pressed Stop"):
        lease.require()
    assert gets(fake) == 0


# -- GatedConsole ------------------------------------------------------------------------------


def test_gated_console_sends_input_only_under_a_live_lease(ledger, pay, fake, clock) -> None:
    rec = to_held(ledger, pay, fake, Tier.T1)
    inner = console()
    gated = GatedConsole(inner, lease_for(ledger, fake, rec, clock))
    gated.type_text("ls\n")
    gated.press(["ctrl", "c"])
    gated.power_cycle()
    assert [r.kind for r in inner.inputs] == ["type", "press", "power_cycle"]
    assert gated.inputs_sent == 3

    gated.lease.revoke("the owner pressed Stop")
    for action in (lambda: gated.type_text("ls\n"), lambda: gated.press(["ret"]), gated.power_cycle):
        with pytest.raises(HoldRevoked):
            action()
    assert len(inner.inputs) == 3
    assert gated.inputs_refused == 3
    assert gated.screenshot().startswith(b"\x89PNG")  # looking is always allowed


def test_gated_console_without_a_grant_sends_nothing(ledger, pay, fake, clock) -> None:
    rec = to_held(ledger, pay, fake)
    fake.void_externally(rec.authorization_id)
    inner = console()
    gated = GatedConsole(inner, lease_for(ledger, fake, rec, clock))
    gated.screenshot()
    with pytest.raises(HoldRevoked):
        gated.type_text("root\n")
    assert inner.inputs == []


def test_close_ends_the_session_but_not_the_console(ledger, pay, fake, clock) -> None:
    rec = to_held(ledger, pay, fake)
    inner = console()
    gated = GatedConsole(inner, lease_for(ledger, fake, rec, clock))
    gated.close()
    with pytest.raises(HoldRevoked, match="closed"):
        gated.type_text("x")
    assert not inner.closed
    inner.screenshot()


def test_works_with_a_plain_lease() -> None:
    class Flag:
        ok = True

        def require(self) -> None:
            if not self.ok:
                raise HoldRevoked("no")

    lease = Flag()
    inner = console()
    gated = GatedConsole(inner, lease)
    gated.type_text("a")
    lease.ok = False
    with pytest.raises(HoldRevoked):
        gated.type_text("b")
    assert inner.typed_text() == "a"


def test_revoke_waits_for_the_input_in_flight_and_blocks_the_next(ledger, pay, fake, clock) -> None:
    rec = to_held(ledger, pay, fake)
    lease = lease_for(ledger, fake, rec, clock)
    inner = console()
    gated = GatedConsole(inner, lease)
    sending = threading.Event()
    release = threading.Event()

    def slow(record) -> None:
        if record.value == "slow\n":
            sending.set()
            assert release.wait(5)

    inner.on_input(slow)
    worker = threading.Thread(target=gated.type_text, args=("slow\n",))
    worker.start()
    assert sending.wait(5)

    revoked = threading.Event()
    stopper = threading.Thread(target=lambda: (lease.revoke("the owner pressed Stop"), revoked.set()))
    stopper.start()
    assert not revoked.wait(0.2)  # still waiting for the input being sent
    with pytest.raises(HoldRevoked):
        lease.require()  # but nothing new is granted meanwhile
    release.set()
    worker.join(5)
    stopper.join(5)
    assert revoked.is_set()
    with pytest.raises(HoldRevoked):
        gated.type_text("after\n")
    assert [r.value for r in inner.inputs] == ["slow\n"]
