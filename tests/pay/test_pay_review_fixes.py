"""Regression tests for the money review findings: unknown outcomes stay PENDING, a void is never
closed while the hold is live, warranty refunds survive PENDING captures, PENDING refunds and
partial refunds, declined funding sources, orders that expire unapproved, and the lease waits
out a short PayPal throttle."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pay_helpers import new_rescue, to_held, to_probing

from keyhold.pay.client import FakePayPal, fake_error
from keyhold.pay.gate import HoldLease
from keyhold.pay.ledger import Ledger, Payments, RefundDeferred
from keyhold.pay.reconciler import Reconciler
from keyhold.types import MoneyOp, OutboxStatus, PayPalError, RescueState

D = Decimal


def op(ledger: Ledger, rid: str, which: MoneyOp):
    entry = ledger.get_op(rid, which)
    assert entry is not None
    return entry


# -- outcomes PayPal did not decide stay PENDING -------------------------------------------------


@pytest.mark.parametrize(
    ("issue", "status"),
    [("AUTHENTICATION_FAILURE", 401), ("RATE_LIMIT_REACHED", 429), ("TOKEN_UNAVAILABLE", 503), ("UNEXPECTED_ERROR", None)],
)
def test_capture_with_an_open_outcome_stays_pending_and_replays(
    ledger: Ledger, pay: Payments, fake: FakePayPal, issue: str, status: int | None
) -> None:
    """Regression: a 401/429/token outage on capture closed cap-{rescue_id} as FAILED, after
    which the orchestrator voided the hold of a server the probe had just found fixed."""
    rec = to_probing(ledger, pay, fake)
    fake.inject("capture", issue, status=status)
    with pytest.raises(PayPalError):
        pay.capture(rec.id)
    entry = op(ledger, rec.id, MoneyOp.CAPTURE)
    assert entry.status is OutboxStatus.PENDING and entry.request_id == f"cap-{rec.id}"
    assert ledger.get(rec.id).state is RescueState.PROBING
    Reconciler(ledger, fake, live_rescues=lambda: [rec.id]).run_once()
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.CAPTURED and len(fake.all_captures()) == 1
    assert [c.request_id for c in fake.money_calls("capture")] == [f"cap-{rec.id}"]


def test_a_throttled_void_is_retried_until_paypal_voids(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    """Regression: a 429 on void closed void-{rescue_id} as FAILED for good, so the owner's
    Stop never released the hold and the reconciler reported 'void already failed'."""
    rec = to_held(ledger, pay, fake)
    fake.inject("void", "RATE_LIMIT_REACHED", status=429)
    with pytest.raises(PayPalError):
        pay.void(rec.id, "owner pressed Stop")
    assert op(ledger, rec.id, MoneyOp.VOID).status is OutboxStatus.PENDING
    report = Reconciler(ledger, fake, live_rescues=list).run_once()
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.VOIDED, report
    assert fake.get_authorization(rec.authorization_id).status == "VOIDED"


def test_a_refused_void_of_a_live_hold_is_sent_again_under_a_new_request_id(
    ledger: Ledger, pay: Payments, fake: FakePayPal
) -> None:
    rec = to_held(ledger, pay, fake)
    fake.inject("void", "PERMISSION_DENIED")  # a 4xx, while the authorization is still CREATED
    with pytest.raises(PayPalError):
        pay.void(rec.id, "owner pressed Stop")
    entry = op(ledger, rec.id, MoneyOp.VOID)
    assert entry.status is OutboxStatus.PENDING and entry.request_id == f"void-{rec.id}-2"
    assert any(e.kind == "void_refused" for e in ledger.events(rec.id))
    Reconciler(ledger, fake).run_once()
    assert ledger.get(rec.id).state is RescueState.VOIDED
    assert [c.request_id for c in fake.money_calls("void")] == [f"void-{rec.id}-2"]


def test_a_void_closed_failed_by_an_older_version_is_reopened(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = to_held(ledger, pay, fake)
    entry = ledger.begin_op(rec.id, MoneyOp.VOID, {"auth_id": rec.authorization_id},
                            allowed_states=[RescueState.HELD], note="Stop")
    ledger.fail_op(entry.id, "RATE_LIMIT_REACHED")
    report = Reconciler(ledger, fake, live_rescues=list).run_once()
    assert ledger.get(rec.id).state is RescueState.VOIDED, report
    assert fake.get_authorization(rec.authorization_id).status == "VOIDED"


# -- warranty refunds -----------------------------------------------------------------------------


def test_refund_waits_for_a_pending_capture_then_goes_out(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    """Regression: a refund of a PENDING capture was refused once and closed as FAILED; when the
    capture cleared, the owner had paid for a fix that failed its warranty."""
    rec = to_probing(ledger, pay, fake)
    fake.capture_status = "PENDING"
    cap = pay.capture(rec.id)
    with pytest.raises(RefundDeferred):
        pay.refund(rec.id, "warranty: two failed checks")
    assert fake.money_calls("refund") == []  # nothing sent while the capture is PENDING
    assert ledger.refund_due(rec.id) == "warranty: two failed checks"
    Reconciler(ledger, fake).run_once()
    assert ledger.get(rec.id).state is RescueState.CAPTURED and fake.money_calls("refund") == []
    fake.complete_capture(cap.id)
    report = Reconciler(ledger, fake).run_once()
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.REFUNDED and rec.refunded_amount == D("19.00"), report
    assert [r.amount for r in fake.refunds_for(cap.id)] == [D("19.00")]
    assert ledger.check_invariants(rec.id) == []


def test_a_refund_paypal_refuses_on_a_pending_capture_is_kept(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    """The capture turned PENDING after Keyhold last looked: PayPal answers PENDING_CAPTURE."""
    rec = to_probing(ledger, pay, fake)
    cap = pay.capture(rec.id)
    fake._state["captures"][cap.id]["status"] = "PENDING"  # PayPal-side change Keyhold has not seen
    with pytest.raises(RefundDeferred):
        pay.refund(rec.id, "warranty failed")
    assert op(ledger, rec.id, MoneyOp.REFUND).status is OutboxStatus.PENDING
    fake.complete_capture(cap.id)
    Reconciler(ledger, fake).run_once()
    assert ledger.get(rec.id).state is RescueState.REFUNDED
    assert len(fake.refunds_for(cap.id)) == 1


def test_a_pending_refund_is_not_refunded_until_it_completes(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    """Regression: a refund PayPal answered PENDING moved the rescue to REFUNDED (terminal), so a
    refund that later FAILED was never noticed."""
    rec = to_probing(ledger, pay, fake)
    cap = pay.capture(rec.id)
    fake.refund_status = "PENDING"
    first = pay.refund(rec.id, "warranty failed")
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.CAPTURED and rec.refund_status == "PENDING"
    assert rec.capture_status == "COMPLETED"
    fake.fail_refund(first.id)
    fake.refund_status = "COMPLETED"
    report = Reconciler(ledger, fake).run_once()
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.REFUNDED, report
    calls = [c.request_id for c in fake.money_calls("refund")]
    assert calls == [f"refund-{rec.id}", f"refund-{rec.id}-2"]  # the failed one is sent again
    assert fake.get_capture(cap.id).status == "REFUNDED"


def test_a_pending_refund_that_completes_ends_refunded(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = to_probing(ledger, pay, fake)
    pay.capture(rec.id)
    fake.refund_status = "PENDING"
    ref = pay.refund(rec.id, "warranty failed")
    fake.complete_refund(ref.id)
    Reconciler(ledger, fake).run_once()
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.REFUNDED and rec.refund_status == "COMPLETED"
    assert len(fake.money_calls("refund")) == 1


def test_a_partial_refund_elsewhere_is_not_taken_for_the_warranty_refund(
    ledger: Ledger, pay: Payments, fake: FakePayPal
) -> None:
    """Regression: REFUND_AMOUNT_EXCEEDED made Keyhold record a $4 outside refund as its own
    full refund and end REFUNDED, while PayPal had refunded $4 of $19."""
    rec = to_probing(ledger, pay, fake)
    cap = pay.capture(rec.id)
    fake.refund_externally(cap.id, "4.00")
    ref = pay.refund(rec.id, "warranty failed")
    rec = ledger.get(rec.id)
    assert ref.amount == D("15.00") and rec.state is RescueState.REFUNDED
    assert sum(r.amount for r in fake.refunds_for(cap.id)) == D("19.00")
    assert ledger.check_invariants(rec.id) == []


def test_a_partial_refund_racing_the_warranty_refund_is_resized(
    ledger: Ledger, pay: Payments, fake: FakePayPal
) -> None:
    rec = to_probing(ledger, pay, fake)
    cap = pay.capture(rec.id)
    real_refund = fake.refund

    def refund_after_outside(capture_id, amount, request_id):  # someone refunds $4 just before
        fake.refund = real_refund
        fake.refund_externally(capture_id, "4.00")
        return real_refund(capture_id, amount, request_id)

    fake.refund = refund_after_outside  # type: ignore[method-assign]
    with pytest.raises(PayPalError) as info:
        pay.refund(rec.id, "warranty failed")
    assert info.value.issue == "REFUND_AMOUNT_EXCEEDED"
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.CAPTURED  # not mistaken for a full refund
    entry = op(ledger, rec.id, MoneyOp.REFUND)
    assert entry.status is OutboxStatus.PENDING and entry.args["amount"] == "15.00"
    Reconciler(ledger, fake).run_once()
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.REFUNDED and sum(r.amount for r in fake.refunds_for(cap.id)) == D("19.00")


def test_refunded_needs_the_whole_capture_refunded(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    from keyhold.pay.ledger import IllegalTransition

    rec = to_probing(ledger, pay, fake)
    cap = pay.capture(rec.id)
    ledger.record_refund(rec.id, fake.refund_externally(cap.id, "4.00"))
    with pytest.raises(IllegalTransition, match="whole capture"):
        ledger.transition(rec.id, RescueState.REFUNDED)


def test_a_refund_refused_for_good_is_flagged(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = to_probing(ledger, pay, fake)
    pay.capture(rec.id)
    fake.inject("refund", "REFUND_TIME_LIMIT_EXCEEDED")
    with pytest.raises(PayPalError):
        pay.refund(rec.id, "warranty failed")
    assert op(ledger, rec.id, MoneyOp.REFUND).status is OutboxStatus.FAILED
    assert any(e.kind == "attention" for e in ledger.events(rec.id))


# -- authorize -------------------------------------------------------------------------------------


def test_a_declined_funding_source_keeps_the_order_open(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    """Regression: INSTRUMENT_DECLINED voided the rescue, so the buyer could not pick another
    funding source on the same approved order."""
    rec = new_rescue(ledger)
    fake.approve(pay.create_hold(rec.id).id)
    fake.inject("authorize", "INSTRUMENT_DECLINED")
    with pytest.raises(PayPalError):
        pay.authorize(rec.id)
    assert ledger.get(rec.id).state is RescueState.HOLD_PENDING
    pay.authorize(rec.id)  # the buyer chose another funding source
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.HELD
    assert op(ledger, rec.id, MoneyOp.AUTHORIZE).request_id == f"auth-{rec.id}-2"
    assert len(fake.all_authorizations()) == 1


def test_declines_are_limited(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = new_rescue(ledger)
    fake.approve(pay.create_hold(rec.id).id)
    fake.inject("authorize", "INSTRUMENT_DECLINED", times=3)
    for _ in range(3):
        with pytest.raises(PayPalError):
            pay.authorize(rec.id)
    assert ledger.get(rec.id).state is RescueState.VOIDED


def test_a_refused_authorize_with_a_hold_at_paypal_releases_it(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = new_rescue(ledger)
    fake.approve(pay.create_hold(rec.id).id)
    fake.inject("authorize", "UNPROCESSABLE_ENTITY", after=True)  # PayPal made the hold, then said no
    with pytest.raises(PayPalError):
        pay.authorize(rec.id)
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.VOIDED and rec.authorization_id is not None
    assert fake.get_authorization(rec.authorization_id).status == "VOIDED"
    assert ledger.orphan_holds() == []


def test_an_unknown_authorize_outcome_stays_pending(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = new_rescue(ledger)
    fake.approve(pay.create_hold(rec.id).id)
    fake.inject("authorize", "UNEXPECTED_ERROR", after=True)
    with pytest.raises(PayPalError):
        pay.authorize(rec.id)
    assert op(ledger, rec.id, MoneyOp.AUTHORIZE).status is OutboxStatus.PENDING
    Reconciler(ledger, fake).run_once()
    assert ledger.get(rec.id).state is RescueState.HELD


def test_an_orphan_hold_is_voided_by_the_reconciler(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = new_rescue(ledger)
    order = pay.create_hold(rec.id)
    fake.approve(order.id)
    auth = fake.authorize(order.id, "lost-request")
    ledger.transition(rec.id, RescueState.VOIDED, reason="quote expired")
    ledger.record_authorization(rec.id, auth)
    assert [r.id for r in ledger.orphan_holds()] == [rec.id]
    report = Reconciler(ledger, fake).run_once()
    assert rec.id in report.voided
    assert fake.get_authorization(auth.id).status == "VOIDED" and ledger.orphan_holds() == []


# -- reconciler -------------------------------------------------------------------------------------


def test_an_order_that_expired_unapproved_ends_the_rescue(ledger: Ledger, pay: Payments, fake: FakePayPal, clock) -> None:
    """Regression: GET order answered 404 for an order PayPal dropped, the error escaped and the
    rescue stayed HOLD_PENDING for ever."""
    rec = new_rescue(ledger)
    pay.create_hold(rec.id)
    clock.advance(4 * 3600)
    fake.inject("get_order", "RESOURCE_NOT_FOUND", times=5)
    report = Reconciler(ledger, fake).run_once()
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.VOIDED and "expired" in rec.end_reason, report
    assert fake.money_calls("void") == []


# -- the lease ----------------------------------------------------------------------------------------


def test_the_lease_waits_out_a_short_throttle(ledger: Ledger, pay: Payments, fake: FakePayPal, clock) -> None:
    """Regression: a 429 on GET authorization revoked the keyboard at once (and so voided)."""
    rec = to_held(ledger, pay, fake)
    lease = HoldLease(ledger, fake, rec.id, clock=clock, check_every=1000, ttl_s=10_000.0, retries=2)
    throttled = fake_error("RATE_LIMIT_REACHED", "slow down", status=429)
    throttled.retry_after = 3.0
    calls = {"n": 0}
    real = fake.get_authorization

    def flaky(auth_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise throttled
        return real(auth_id)

    fake.get_authorization = flaky  # type: ignore[method-assign]
    lease.require()
    assert not lease.revoked and calls["n"] == 2
    assert clock.sleeps and clock.sleeps[-1] >= 3.0


def test_refund_webhooks_in_any_order_agree_with_paypal(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    from pay_helpers import WEBHOOK_ID

    from keyhold.pay.webhooks import WebhookProcessor

    hooks = WebhookProcessor(ledger, fake, WEBHOOK_ID)
    rec = to_probing(ledger, pay, fake)
    cap = pay.capture(rec.id)
    for headers, raw in fake.take_deliveries(WEBHOOK_ID):
        hooks.handle(headers, raw)
    fake.refund_externally(cap.id, "4.00")
    outside = fake.take_deliveries(WEBHOOK_ID)
    pay.refund(rec.id, "warranty failed")  # refunds the remaining $15
    for headers, raw in [*fake.take_deliveries(WEBHOOK_ID), *outside]:  # the outside one arrives last
        assert hooks.handle(headers, raw).outcome == "applied"
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.REFUNDED and rec.refunded_amount == D("19.00")
    assert not [e for e in ledger.events(rec.id) if e.kind == "conflict"]
