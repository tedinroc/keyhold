"""Reconciler: replay PENDING outbox rows with their request id and repair from PayPal's view."""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest
from pay_helpers import WEBHOOK_ID, SimulatedCrash, crash_at, new_rescue, to_held, to_probing

from keyhold.pay.client import FakePayPal
from keyhold.pay.ledger import Ledger, Payments
from keyhold.pay.reconciler import Reconciler
from keyhold.pay.webhooks import WebhookProcessor
from keyhold.types import MoneyOp, OutboxStatus, PayPalError, RescueState


def restart(db_path, clock, fake: FakePayPal) -> tuple[Ledger, Payments, Reconciler]:
    """What a fresh process sees: a new connection on the same file and the same PayPal."""
    fake.hook = None
    led = Ledger(db_path, clock=clock)
    return led, Payments(led, fake), Reconciler(led, fake)


@pytest.mark.parametrize("phase", ["before", "after"])
def test_crash_during_capture_ends_in_exactly_one_capture(db_path, clock, phase: str) -> None:
    fake = FakePayPal(clock=clock)
    led = Ledger(db_path, clock=clock)
    pay = Payments(led, fake)
    rec = to_probing(led, pay, fake)
    fake.hook = crash_at("capture", phase)
    with pytest.raises(SimulatedCrash):
        pay.capture(rec.id)
    led.close()
    assert len(fake.all_captures()) == (1 if phase == "after" else 0)

    led, pay, rec_ = restart(db_path, clock, fake)
    assert led.get_op(rec.id, MoneyOp.CAPTURE).status is OutboxStatus.PENDING
    assert led.get(rec.id).state is RescueState.PROBING
    report = rec_.startup()
    assert report.replayed == [f"{rec.id} capture: DONE"] and report.voided == []
    (capture,) = fake.all_captures()
    final = led.get(rec.id)
    assert final.state is RescueState.CAPTURED and final.capture_id == capture.id
    assert [c.request_id for c in fake.calls if c.op == "capture" and c.outcome in ("ok", "replay")] == [
        f"cap-{rec.id}"
    ] * (2 if phase == "after" else 1)
    # Running again, or the orchestrator retrying, changes nothing.
    assert rec_.run_once().replayed == []
    assert pay.capture(rec.id).id == capture.id
    assert len(fake.all_captures()) == 1
    led.close()


def test_retryable_error_during_replay_stays_pending(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = to_probing(ledger, pay, fake)
    fake.inject("capture", "INTERNAL_SERVER_ERROR", times=2)
    with pytest.raises(PayPalError):
        pay.capture(rec.id)
    rc = Reconciler(ledger, fake)
    report = rc.run_once()
    assert report.replayed == [f"{rec.id} capture: PENDING (INTERNAL_SERVER_ERROR)"] and report.errors
    assert ledger.get_op(rec.id, MoneyOp.CAPTURE).attempts == 2
    report = rc.run_once()
    assert report.replayed == [f"{rec.id} capture: DONE"]
    assert ledger.get(rec.id).state is RescueState.CAPTURED and len(fake.all_captures()) == 1


def test_crash_during_void_is_replayed(db_path, clock) -> None:
    fake = FakePayPal(clock=clock)
    led = Ledger(db_path, clock=clock)
    pay = Payments(led, fake)
    rec = to_held(led, pay, fake)
    fake.hook = crash_at("void", "after")
    with pytest.raises(SimulatedCrash):
        pay.void(rec.id, "owner pressed Stop")
    led.close()
    led, _, rc = restart(db_path, clock, fake)
    rc.startup()
    final = led.get(rec.id)
    assert final.state is RescueState.VOIDED and final.end_reason == "owner pressed Stop"
    assert len(fake.money_calls("void")) == 1
    led.close()


def test_crash_during_create_and_authorize(db_path, clock) -> None:
    fake = FakePayPal(clock=clock)
    led = Ledger(db_path, clock=clock)
    pay = Payments(led, fake)
    rec = new_rescue(led)
    fake.hook = crash_at("create_hold", "after")
    with pytest.raises(SimulatedCrash):
        pay.create_hold(rec.id)
    led.close()
    led, pay, rc = restart(db_path, clock, fake)
    rc.run_once()
    rec = led.get(rec.id)
    assert rec.state is RescueState.HOLD_PENDING and fake.order_count() == 1
    fake.approve(rec.order_id)
    fake.hook = crash_at("authorize", "after")
    with pytest.raises(SimulatedCrash):
        pay.authorize(rec.id)
    led.close()
    led, pay, rc = restart(db_path, clock, fake)
    rc.run_once()
    assert led.get(rec.id).state is RescueState.HELD and len(fake.all_authorizations()) == 1
    led.close()


def test_startup_releases_orphaned_holds(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    held = to_held(ledger, pay, fake)
    probing = to_probing(ledger, pay, fake)
    captured = to_probing(ledger, pay, fake)
    pay.capture(captured.id)
    quoted = new_rescue(ledger, n=9)
    rc = Reconciler(ledger, fake)
    assert rc.run_once().voided == []  # a periodic pass does not guess which sessions are alive
    report = rc.startup()
    assert sorted(report.voided) == sorted([held.id, probing.id])
    for rid in (held.id, probing.id):
        rec = ledger.get(rid)
        assert rec.state is RescueState.VOIDED and "restarted" in rec.end_reason
        assert fake.get_authorization(rec.authorization_id).status == "VOIDED"
    assert ledger.get(captured.id).state is RescueState.CAPTURED
    assert ledger.get(quoted.id).state is RescueState.QUOTED


def test_periodic_pass_releases_only_rescues_nobody_drives(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    alive = to_held(ledger, pay, fake)
    dead = to_held(ledger, pay, fake)
    rc = Reconciler(ledger, fake, live_rescues=lambda: [alive.id])
    assert rc.run_once().voided == [dead.id]
    assert ledger.get(alive.id).state is RescueState.HELD


def test_external_void_and_expiry(ledger: Ledger, pay: Payments, fake: FakePayPal, clock) -> None:
    a = to_held(ledger, pay, fake)
    ledger.transition(a.id, RescueState.ACTIVE)
    fake.void_externally(a.authorization_id)
    b = to_held(ledger, pay, fake)
    fake.expire(b.authorization_id)
    report = Reconciler(ledger, fake).run_once()
    assert ledger.get(a.id).state is RescueState.VOIDED
    assert ledger.get(b.id).state is RescueState.VOIDED and ledger.get(b.id).end_reason == "authorization expired"
    assert len(report.repaired) == 2


def test_capture_made_outside_the_ledger_is_recorded(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = to_probing(ledger, pay, fake)
    made = fake.capture(rec.authorization_id, Decimal("19.00"), True, "x", "outside")
    Reconciler(ledger, fake).run_once()
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.CAPTURED and rec.capture_id == made.id


def test_refund_made_outside_the_ledger_is_recorded(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = to_probing(ledger, pay, fake)
    cap = pay.capture(rec.id)
    ref = fake.refund_externally(cap.id)
    Reconciler(ledger, fake).run_once()
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.REFUNDED and rec.refund_id == ref.id and rec.capture_status == "REFUNDED"


def test_pending_capture_clears(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = to_probing(ledger, pay, fake)
    fake.capture_status = "PENDING"
    cap = pay.capture(rec.id)
    assert ledger.get(rec.id).capture_status == "PENDING"
    fake.complete_capture(cap.id)
    report = Reconciler(ledger, fake).run_once()
    assert ledger.get(rec.id).capture_status == "COMPLETED" and report.repaired


def test_authorization_found_by_order(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = new_rescue(ledger)
    order = pay.create_hold(rec.id)
    fake.approve(order.id)
    fake.authorize(order.id, "outside")  # authorized without Keyhold hearing about it
    Reconciler(ledger, fake).run_once()
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.HELD and rec.authorization_id


def test_pending_authorization_becomes_held(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = new_rescue(ledger)
    fake.approve(pay.create_hold(rec.id).id)
    fake.next_authorization_status = "PENDING"
    pay.authorize(rec.id)
    rc = Reconciler(ledger, fake)
    rc.run_once()
    assert ledger.get(rec.id).state is RescueState.HOLD_PENDING
    fake.set_authorization_status(ledger.get(rec.id).authorization_id, "CREATED")
    rc.run_once()
    assert ledger.get(rec.id).state is RescueState.HELD


def test_unapproved_order_times_out(ledger: Ledger, pay: Payments, fake: FakePayPal, clock) -> None:
    rec = new_rescue(ledger)
    pay.create_hold(rec.id)
    stale_quote = new_rescue(ledger, n=2)
    rc = Reconciler(ledger, fake, approval_timeout=600)
    rc.run_once()
    assert ledger.get(rec.id).state is RescueState.HOLD_PENDING
    clock.advance(601)
    rc.run_once()
    assert ledger.get(rec.id).state is RescueState.VOIDED
    assert ledger.get(stale_quote.id).state is RescueState.VOIDED


def test_reconcile_is_idempotent(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = to_probing(ledger, pay, fake)
    pay.capture(rec.id)
    rc = Reconciler(ledger, fake)
    first = rc.run_once()
    second = rc.run_once()
    assert first.clean and second.clean and second.repaired == [] and second.replayed == []


def test_reconciler_processes_stored_webhooks(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    to_held(ledger, pay, fake)
    hooks = WebhookProcessor(ledger, fake, WEBHOOK_ID)
    for headers, raw in fake.take_deliveries(WEBHOOK_ID):
        hooks.receive(headers, raw)
    report = Reconciler(ledger, fake, webhooks=hooks).run_once()
    assert report.webhooks == 1


def test_paypal_outage_is_reported_not_raised(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    to_held(ledger, pay, fake)
    fake.inject("get_authorization", "INTERNAL_SERVER_ERROR")
    report = Reconciler(ledger, fake).run_once()
    assert report.errors and not report.clean


def test_background_loop_starts_and_stops(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = to_held(ledger, pay, fake)
    rc = Reconciler(ledger, fake, interval=0.01)
    stop = rc.start()
    deadline = threading.Event()
    for _ in range(200):
        if ledger.get(rec.id).state is RescueState.VOIDED:
            break
        deadline.wait(0.01)
    stop.set()
    rc._thread.join(timeout=2)
    assert not rc._thread.is_alive()
    assert ledger.get(rec.id).state is RescueState.VOIDED  # the startup pass released the orphan
