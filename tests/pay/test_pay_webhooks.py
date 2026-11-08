"""Webhook intake: store, verify, dedupe on event id, apply with state ranks."""

from __future__ import annotations

import json

import pytest
from pay_helpers import WEBHOOK_ID, new_rescue, to_held, to_probing

from keyhold.pay.client import FakePayPal
from keyhold.pay.ledger import Ledger, Payments
from keyhold.pay.reconciler import Reconciler
from keyhold.pay.webhooks import HANDLED_EVENTS, WebhookProcessor
from keyhold.types import MoneyOp, OutboxStatus, PayPalError, RescueState


@pytest.fixture
def hooks(ledger: Ledger, fake: FakePayPal) -> WebhookProcessor:
    return WebhookProcessor(ledger, fake, WEBHOOK_ID)


def deliver_all(hooks: WebhookProcessor, fake: FakePayPal) -> list[str]:
    return [hooks.handle(h, raw).outcome for h, raw in fake.take_deliveries(WEBHOOK_ID)]


def snapshot(ledger: Ledger, rescue_id: str):
    rec = ledger.get(rescue_id)
    return (
        rec.state,
        rec.auth_status,
        rec.capture_id,
        rec.captured_amount,
        rec.capture_status,
        rec.refund_id,
        rec.refunded_amount,
    )


def test_handled_event_types() -> None:
    assert {
        "PAYMENT.AUTHORIZATION.CREATED",
        "PAYMENT.AUTHORIZATION.VOIDED",
        "PAYMENT.CAPTURE.COMPLETED",
        "PAYMENT.CAPTURE.REFUNDED",
        "PAYMENT.CAPTURE.DENIED",
        "PAYMENT.CAPTURE.DECLINED",
        "PAYMENT.CAPTURE.PENDING",
        "PAYMENT.CAPTURE.REVERSED",
        "PAYMENT.REFUND.PENDING",
        "PAYMENT.REFUND.FAILED",
    } == HANDLED_EVENTS


def test_second_tick_for_each_operation(
    ledger: Ledger, pay: Payments, fake: FakePayPal, hooks: WebhookProcessor
) -> None:
    rec = to_probing(ledger, pay, fake)
    pay.capture(rec.id)
    pay.refund(rec.id, "warranty failed")
    before = ledger.get(rec.id)
    assert before.auth_webhook_at is None and before.capture_webhook_at is None
    assert deliver_all(hooks, fake) == ["applied", "applied", "applied"]
    after = ledger.get(rec.id)
    assert after.auth_webhook_at and after.capture_webhook_at and after.refund_webhook_at
    assert snapshot(ledger, rec.id) == snapshot(ledger, rec.id)  # stable
    assert after.state is RescueState.REFUNDED


def test_raw_body_and_headers_are_stored(
    ledger: Ledger, pay: Payments, fake: FakePayPal, hooks: WebhookProcessor
) -> None:
    to_held(ledger, pay, fake)
    ((headers, raw),) = fake.take_deliveries(WEBHOOK_ID)
    delivery_id = hooks.receive({**headers, "Cookie": "session=abc"}, raw)
    stored = ledger.delivery(delivery_id)
    assert stored.raw == raw and stored.outcome is None and stored.verified is None
    assert stored.headers["PAYPAL-TRANSMISSION-ID"] == headers["PAYPAL-TRANSMISSION-ID"]
    assert "Cookie" not in stored.headers
    out = hooks.process(delivery_id)
    assert out.outcome == "applied"
    stored = ledger.delivery(delivery_id)
    assert stored.verified is True and stored.event_type == "PAYMENT.AUTHORIZATION.CREATED"
    assert hooks.process(delivery_id).detail == "already processed"


def test_replayed_event_is_a_duplicate_and_changes_nothing(
    ledger: Ledger, pay: Payments, fake: FakePayPal, hooks: WebhookProcessor
) -> None:
    rec = to_probing(ledger, pay, fake)
    pay.capture(rec.id)
    deliveries = fake.take_deliveries(WEBHOOK_ID)
    for h, raw in deliveries:
        hooks.handle(h, raw)
    before = snapshot(ledger, rec.id)
    events_before = len(ledger.events(rec.id))
    # PayPal retries the same events (fresh transmission ids, same event ids).
    for _, raw in deliveries:
        out = hooks.handle(fake.sign_delivery(raw, WEBHOOK_ID), raw)
        assert out.outcome == "duplicate"
    assert snapshot(ledger, rec.id) == before
    assert len(ledger.events(rec.id)) == events_before


def test_unverified_delivery_is_kept_but_never_applied(
    ledger: Ledger, pay: Payments, fake: FakePayPal, hooks: WebhookProcessor
) -> None:
    rec = to_held(ledger, pay, fake)
    fake.void_externally(rec.authorization_id)
    deliveries = fake.take_deliveries(WEBHOOK_ID)
    headers, raw = deliveries[-1]
    forged = {**headers, "PAYPAL-TRANSMISSION-SIG": "Zm9yZ2Vk"}
    out = hooks.handle(forged, raw)
    assert out.outcome == "unverified"
    assert ledger.get(rec.id).state is RescueState.HELD
    assert ledger.delivery(out.delivery_id).verified is False
    # The forged copy did not claim the event id: the genuine delivery still applies.
    assert hooks.handle(headers, raw).outcome == "applied"
    assert ledger.get(rec.id).state is RescueState.VOIDED


def test_simulator_style_event_without_our_webhook_id_fails(ledger: Ledger, fake: FakePayPal) -> None:
    rec = new_rescue(ledger)
    raw = json.dumps({"id": "WH-SIM", "event_type": "PAYMENT.AUTHORIZATION.VOIDED", "resource": {"id": "A"}}).encode()
    headers = fake.sign_delivery(raw, "WEBHOOK_ID")  # what the dashboard simulator uses
    assert WebhookProcessor(ledger, fake, WEBHOOK_ID).handle(headers, raw).outcome == "unverified"
    assert WebhookProcessor(ledger, fake, "").handle(headers, raw).outcome == "unverified"
    assert ledger.get(rec.id).state is RescueState.QUOTED


def test_verifier_that_blows_up_is_not_verified(ledger: Ledger, fake: FakePayPal) -> None:
    class Broken(FakePayPal):
        def verify_webhook(self, headers, raw, webhook_id):
            raise RuntimeError("bug")

    raw = b'{"id":"WH-1","event_type":"PAYMENT.CAPTURE.COMPLETED","resource":{"id":"C"}}'
    assert WebhookProcessor(ledger, Broken(), WEBHOOK_ID).handle({}, raw).outcome == "unverified"


def test_verification_outage_is_retried_not_dropped(
    ledger: Ledger, pay: Payments, fake: FakePayPal, clock
) -> None:
    """Regression: a verify endpoint outage used to mark a genuine delivery UNVERIFIED for good,
    so process_pending never retried it and its second tick never came."""

    class Flaky(FakePayPal):
        down = True

        def verify_webhook(self, headers, raw, webhook_id):
            if self.down:
                raise PayPalError("SERVICE_UNAVAILABLE", status=503, retryable=True)
            return super().verify_webhook(headers, raw, webhook_id)

    flaky = Flaky(clock=clock)
    payments = Payments(ledger, flaky)
    rec = to_probing(ledger, payments, flaky)
    payments.capture(rec.id)
    hooks = WebhookProcessor(ledger, flaky, WEBHOOK_ID)
    capture_delivery = flaky.take_deliveries(WEBHOOK_ID)[-1]
    first = hooks.handle(*capture_delivery)
    assert first.outcome == "pending"
    assert ledger.delivery(first.delivery_id).outcome is None  # still unprocessed
    assert ledger.get(rec.id).capture_webhook_at is None
    flaky.down = False
    assert [o.outcome for o in hooks.process_pending()] == ["applied"]
    assert ledger.get(rec.id).capture_webhook_at is not None


def test_verification_that_never_runs_gives_up_after_the_cap(ledger: Ledger, fake: FakePayPal) -> None:
    from keyhold.pay.webhooks import MAX_VERIFY_ATTEMPTS

    class Down(FakePayPal):
        def verify_webhook(self, headers, raw, webhook_id):
            raise PayPalError("SERVICE_UNAVAILABLE", status=503, retryable=True)

    hooks = WebhookProcessor(ledger, Down(), WEBHOOK_ID)
    raw = b'{"id":"WH-9","event_type":"PAYMENT.CAPTURE.COMPLETED","resource":{"id":"C"}}'
    delivery_id = hooks.receive({}, raw)
    for _ in range(MAX_VERIFY_ATTEMPTS - 1):
        assert hooks.process(delivery_id).outcome == "pending"
    assert hooks.process(delivery_id).outcome == "unverified"
    assert ledger.delivery(delivery_id).raw == b""  # the unverified body is pruned


def test_malformed_ignored_and_unmatched(ledger: Ledger, fake: FakePayPal, hooks: WebhookProcessor) -> None:
    def signed(body: bytes):
        return fake.sign_delivery(body, WEBHOOK_ID), body

    assert hooks.handle(*signed(b"not json")).outcome == "malformed"
    assert hooks.handle(*signed(b'{"id":"WH-2"}')).outcome == "malformed"
    raw = json.dumps({"id": "WH-3", "event_type": "CHECKOUT.ORDER.APPROVED", "resource": {"id": "O1"}}).encode()
    assert hooks.handle(*signed(raw)).outcome == "ignored"
    # An unhandled type does not claim its event id, so the same event is ignored again rather
    # than recorded as applied.
    assert hooks.handle(*signed(raw)).outcome == "ignored"
    raw = json.dumps({"id": "WH-4", "event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {}}).encode()
    assert hooks.handle(*signed(raw)).outcome == "malformed"
    raw = json.dumps(
        {
            "id": "WH-5",
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {"id": "C-unknown", "status": "COMPLETED", "amount": {"value": "9.00"}},
        }
    ).encode()
    assert hooks.handle(*signed(raw)).outcome == "unmatched"
    assert any(e.kind == "webhook_unmatched" for e in ledger.events())


def test_authorization_webhook_moves_hold_pending_to_held(
    ledger: Ledger, pay: Payments, fake: FakePayPal, hooks: WebhookProcessor
) -> None:
    rec = new_rescue(ledger)
    fake.approve(pay.create_hold(rec.id).id)
    fake.inject("authorize", "INTERNAL_SERVER_ERROR", after=True)  # authorized, answer lost
    with pytest.raises(PayPalError):
        pay.authorize(rec.id)
    assert ledger.get(rec.id).state is RescueState.HOLD_PENDING
    assert deliver_all(hooks, fake) == ["applied"]
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.HELD and rec.authorization_id
    # The reconciler then closes the outbox row with the same request id.
    Reconciler(ledger, fake).run_once()
    assert ledger.get_op(rec.id, MoneyOp.AUTHORIZE).status is OutboxStatus.DONE
    assert len(fake.all_authorizations()) == 1


def test_capture_webhook_during_the_crash_window(
    ledger: Ledger, pay: Payments, fake: FakePayPal, hooks: WebhookProcessor
) -> None:
    rec = to_probing(ledger, pay, fake)
    fake.inject("capture", "TRANSPORT_ERROR", after=True)
    with pytest.raises(PayPalError):
        pay.capture(rec.id)
    assert deliver_all(hooks, fake)[-1] == "applied"
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.CAPTURED and rec.capture_id == fake.all_captures()[0].id
    Reconciler(ledger, fake).run_once()
    assert ledger.get_op(rec.id, MoneyOp.CAPTURE).status is OutboxStatus.DONE
    assert len(fake.all_captures()) == 1


def test_late_events_never_regress(ledger: Ledger, pay: Payments, fake: FakePayPal, hooks: WebhookProcessor) -> None:
    rec = to_probing(ledger, pay, fake)
    fake.capture_status = "PENDING"
    cap = pay.capture(rec.id)
    fake.complete_capture(cap.id)
    pending, completed = fake.take_deliveries(WEBHOOK_ID)[-2:]
    assert hooks.handle(*completed).outcome == "applied"
    assert ledger.get(rec.id).capture_status == "COMPLETED"
    assert hooks.handle(*pending).outcome == "applied"  # arrives late: recorded, no regress
    assert ledger.get(rec.id).capture_status == "COMPLETED"


def test_authorization_created_after_void(
    ledger: Ledger, pay: Payments, fake: FakePayPal, hooks: WebhookProcessor
) -> None:
    rec = to_held(ledger, pay, fake)
    pay.void(rec.id, "owner pressed Stop")
    created, voided = fake.take_deliveries(WEBHOOK_ID)
    assert hooks.handle(*voided).outcome == "applied"
    assert hooks.handle(*created).outcome == "applied"
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.VOIDED and rec.auth_status == "VOIDED"
    assert rec.auth_webhook_at and rec.void_webhook_at


def test_external_void_ends_the_rescue(
    ledger: Ledger, pay: Payments, fake: FakePayPal, hooks: WebhookProcessor
) -> None:
    rec = to_held(ledger, pay, fake)
    ledger.transition(rec.id, RescueState.ACTIVE)
    fake.void_externally(rec.authorization_id)
    deliver_all(hooks, fake)
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.VOIDED and rec.end_reason == "authorization voided at PayPal"


def test_void_webhook_while_a_capture_is_pending_waits(
    ledger: Ledger, pay: Payments, fake: FakePayPal, hooks: WebhookProcessor
) -> None:
    rec = to_probing(ledger, pay, fake)
    fake.inject("capture", "INTERNAL_SERVER_ERROR")  # nothing captured, row stays PENDING
    with pytest.raises(PayPalError):
        pay.capture(rec.id)
    fake.void_externally(rec.authorization_id)
    deliver_all(hooks, fake)
    assert ledger.get(rec.id).state is RescueState.PROBING  # the reconciler settles it
    Reconciler(ledger, fake).run_once()
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.VOIDED and fake.all_captures() == []
    assert ledger.get_op(rec.id, MoneyOp.CAPTURE).status is OutboxStatus.FAILED


@pytest.mark.parametrize("event_type", ["PAYMENT.CAPTURE.DECLINED", "PAYMENT.CAPTURE.DENIED"])
def test_capture_declined_ends_without_a_charge(
    ledger: Ledger, pay: Payments, fake: FakePayPal, hooks: WebhookProcessor, event_type: str
) -> None:
    """Regression: a declined capture used to stay CAPTURED (open forever, and able to end
    WARRANTY_OK), and the v2 event name PAYMENT.CAPTURE.DECLINED was ignored."""
    rec = to_probing(ledger, pay, fake)
    fake.capture_status = "PENDING"
    cap = pay.capture(rec.id)
    fake.deny_capture(cap.id, event_type)
    assert deliver_all(hooks, fake)[-1] == "applied"
    rec = ledger.get(rec.id)
    assert rec.capture_status == "DECLINED" and rec.state is RescueState.VOIDED
    assert "nothing was charged" in rec.end_reason
    assert any(e.kind == "capture_denied" for e in ledger.events(rec.id))
    assert ledger.check_invariants(rec.id) == []
    assert rec not in ledger.open_rescues()


def test_warranty_cannot_end_ok_on_a_capture_that_is_not_completed(
    ledger: Ledger, pay: Payments, fake: FakePayPal
) -> None:
    from keyhold.pay.ledger import IllegalTransition

    rec = to_probing(ledger, pay, fake)
    fake.capture_status = "PENDING"
    pay.capture(rec.id)
    with pytest.raises(IllegalTransition, match="completed capture"):
        ledger.transition(rec.id, RescueState.WARRANTY_OK)


def test_capture_reversed_is_recorded_and_flagged(
    ledger: Ledger, pay: Payments, fake: FakePayPal, hooks: WebhookProcessor
) -> None:
    rec = to_probing(ledger, pay, fake)
    cap = pay.capture(rec.id)
    deliver_all(hooks, fake)
    raw = json.dumps(
        {
            "id": "WH-REV",
            "event_type": "PAYMENT.CAPTURE.REVERSED",
            "resource": {"id": cap.id, "status": "REVERSED", "amount": {"currency_code": "USD", "value": "19.00"},
                         "supplementary_data": {"related_ids": {"authorization_id": rec.authorization_id}}},
        }
    ).encode()
    assert hooks.handle(fake.sign_delivery(raw, WEBHOOK_ID), raw).outcome == "applied"
    assert ledger.get(rec.id).capture_status == "REVERSED"
    assert any(e.kind == "attention" for e in ledger.events(rec.id))


def test_partial_refund_webhook_leaves_the_rescue_captured(
    ledger: Ledger, pay: Payments, fake: FakePayPal, hooks: WebhookProcessor
) -> None:
    """Regression: a webhook for any refund amount moved CAPTURED to REFUNDED, after which the
    warranty refund could not be sent."""
    rec = to_probing(ledger, pay, fake)
    cap = pay.capture(rec.id)
    deliver_all(hooks, fake)
    fake.refund_externally(cap.id, "4.00")
    assert deliver_all(hooks, fake) == ["applied"]
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.CAPTURED and rec.refunded_amount == 4
    assert rec.capture_status == "PARTIALLY_REFUNDED"
    # The warranty refund still goes out, for the rest of the capture.
    ref = pay.refund(rec.id, "warranty: two failed checks")
    rec = ledger.get(rec.id)
    assert ref.amount == 15 and rec.state is RescueState.REFUNDED and rec.refunded_amount == 19
    assert sum(r.amount for r in fake.refunds_for(cap.id)) == 19
    assert ledger.check_invariants(rec.id) == []


def test_pending_refund_webhooks(ledger: Ledger, pay: Payments, fake: FakePayPal, hooks: WebhookProcessor) -> None:
    rec = to_probing(ledger, pay, fake)
    cap = pay.capture(rec.id)
    deliver_all(hooks, fake)
    fake.refund_status = "PENDING"
    ref = pay.refund(rec.id, "warranty failed")
    assert deliver_all(hooks, fake) == ["applied"]  # PAYMENT.REFUND.PENDING
    assert ledger.get(rec.id).state is RescueState.CAPTURED
    fake.fail_refund(ref.id)
    assert deliver_all(hooks, fake) == ["applied"]  # PAYMENT.REFUND.FAILED
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.CAPTURED and rec.refund_status == "FAILED" and rec.refunded_amount == 0
    assert fake.get_capture(cap.id).status == "COMPLETED"


def test_authorization_created_after_the_rescue_ended_is_voided(
    ledger: Ledger, pay: Payments, fake: FakePayPal, hooks: WebhookProcessor
) -> None:
    """Regression: an authorization PayPal made after authorize failed with an unknown outcome was
    recorded and left CREATED, while the ledger showed the rescue VOIDED."""
    rec = new_rescue(ledger)
    order = pay.create_hold(rec.id)
    fake.approve(order.id)
    # PayPal made a hold Keyhold never heard about (a request whose answer was lost), and the
    # rescue ended before any read-back found it.
    fake.authorize(order.id, "lost-request")
    ledger.transition(rec.id, RescueState.VOIDED, reason="the quote was not paid in time")
    created = [d for d in fake.take_deliveries(WEBHOOK_ID)]
    out = hooks.handle(*created[0])
    assert out.outcome == "applied" and out.detail == "orphan hold"
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.VOIDED and rec.auth_status == "VOIDED"
    assert fake.get_authorization(rec.authorization_id).status == "VOIDED"
    assert fake.authorization_released(rec.authorization_id) == 19


def test_refund_webhook(ledger: Ledger, pay: Payments, fake: FakePayPal, hooks: WebhookProcessor) -> None:
    rec = to_probing(ledger, pay, fake)
    cap = pay.capture(rec.id)
    deliver_all(hooks, fake)
    ref = fake.refund_externally(cap.id)
    assert deliver_all(hooks, fake) == ["applied"]
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.REFUNDED and rec.refund_id == ref.id and rec.refund_webhook_at


def test_capture_for_a_voided_rescue_is_a_conflict(
    ledger: Ledger, pay: Payments, fake: FakePayPal, hooks: WebhookProcessor
) -> None:
    rec = to_held(ledger, pay, fake)
    pay.void(rec.id, "owner pressed Stop")
    fake.take_deliveries(WEBHOOK_ID)
    raw = json.dumps(
        {
            "id": "WH-ODD",
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {
                "id": "C-ODD",
                "status": "COMPLETED",
                "amount": {"currency_code": "USD", "value": "19.00"},
                "supplementary_data": {"related_ids": {"authorization_id": rec.authorization_id}},
            },
        }
    ).encode()
    out = hooks.handle(fake.sign_delivery(raw, WEBHOOK_ID), raw)
    assert out.outcome == "conflict" and out.rescue_id == rec.id
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.VOIDED and rec.capture_id is None
    assert ledger.check_invariants(rec.id) == []


def test_process_pending_picks_up_stored_deliveries(
    ledger: Ledger, pay: Payments, fake: FakePayPal, hooks: WebhookProcessor
) -> None:
    to_held(ledger, pay, fake)
    for headers, raw in fake.take_deliveries(WEBHOOK_ID):
        hooks.receive(headers, raw)
    outcomes = hooks.process_pending()
    assert [o.outcome for o in outcomes] == ["applied"]
    assert hooks.process_pending() == []
