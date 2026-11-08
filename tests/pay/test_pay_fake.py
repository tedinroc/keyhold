"""FakePayPal enforces the PayPal rules Keyhold depends on, so tests that use it mean something."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from keyhold.pay.client import FakePayPal, iso_utc
from keyhold.types import PaymentGateway, PayPalError

D = Decimal


def held(fake: FakePayPal, amount: str = "19.00", n: int = 1) -> tuple[str, str]:
    order = fake.create_hold(D(amount), "spec-hash", f"inv-{n}", f"order-{n}")
    fake.approve(order.id)
    auth = fake.authorize(order.id, f"auth-{n}")
    return order.id, auth.id


def issue_of(fn) -> PayPalError:
    with pytest.raises(PayPalError) as info:
        fn()
    return info.value


def test_fake_is_a_payment_gateway(fake: FakePayPal) -> None:
    assert isinstance(fake, PaymentGateway)
    assert fake.client_token().startswith("fake-client-token-")


def test_authorize_needs_the_owner_approval(fake: FakePayPal) -> None:
    order = fake.create_hold(D("19.00"), "spec", "inv-1", "order-1")
    assert order.status == "CREATED" and order.intent == "AUTHORIZE"
    assert issue_of(lambda: fake.authorize(order.id, "auth-1")).issue == "ORDER_NOT_APPROVED"
    fake.approve(order.id)
    auth = fake.authorize(order.id, "auth-1")
    assert auth.status == "CREATED" and auth.amount == D("19.00") and auth.order_id == order.id
    assert fake.get_order(order.id).authorization_id == auth.id
    assert fake.get_order(order.id).status == "COMPLETED"
    assert issue_of(lambda: fake.authorize(order.id, "auth-again")).issue == "ORDER_ALREADY_AUTHORIZED"
    assert issue_of(lambda: fake.authorize("NOPE", "auth-x")).issue == "RESOURCE_NOT_FOUND"


def test_auto_approve(clock) -> None:
    fake = FakePayPal(clock=clock, auto_approve=True)
    order = fake.create_hold(D("9.00"), "spec", "inv-1", "order-1")
    assert fake.authorize(order.id, "auth-1").status == "CREATED"


def test_invoice_ids_are_unique(fake: FakePayPal) -> None:
    fake.create_hold(D("9.00"), "spec", "inv-1", "order-1")
    assert issue_of(lambda: fake.create_hold(D("9.00"), "spec", "inv-1", "order-2")).issue == "DUPLICATE_INVOICE_ID"


def test_capture_cannot_exceed_the_authorization(fake: FakePayPal) -> None:
    _, auth_id = held(fake)
    err = issue_of(lambda: fake.capture(auth_id, D("19.01"), True, "inv-cap", "cap-1"))
    assert err.issue == "MAX_CAPTURE_AMOUNT_EXCEEDED" and not err.retryable
    assert fake.captures_for(auth_id) == []


def test_final_capture_releases_the_remainder(fake: FakePayPal) -> None:
    _, auth_id = held(fake)
    cap = fake.capture(auth_id, D("12.50"), True, "inv-cap", "cap-1")
    assert cap.amount == D("12.50") and cap.final_capture and cap.status == "COMPLETED"
    assert fake.get_authorization(auth_id).status == "CAPTURED"
    assert fake.authorization_released(auth_id) == D("6.50")
    err = issue_of(lambda: fake.capture(auth_id, D("1.00"), True, "inv-cap", "cap-2"))
    assert err.issue == "AUTHORIZATION_ALREADY_CAPTURED"


def test_partial_capture_without_final_keeps_the_rest(fake: FakePayPal) -> None:
    _, auth_id = held(fake)
    fake.capture(auth_id, D("10.00"), False, "inv-cap", "cap-1")
    assert fake.get_authorization(auth_id).status == "PARTIALLY_CAPTURED"
    assert fake.authorization_released(auth_id) == D("0.00")
    assert issue_of(lambda: fake.capture(auth_id, D("9.01"), True, "x", "cap-2")).issue == "MAX_CAPTURE_AMOUNT_EXCEEDED"
    fake.capture(auth_id, D("9.00"), True, "x", "cap-3")
    assert fake.get_authorization(auth_id).status == "CAPTURED"


def test_same_request_id_returns_the_same_result(fake: FakePayPal) -> None:
    order = fake.create_hold(D("19.00"), "spec", "inv-1", "order-1")
    assert fake.create_hold(D("19.00"), "spec", "inv-1", "order-1") == order
    assert fake.order_count() == 1
    fake.approve(order.id)
    auth = fake.authorize(order.id, "auth-1")
    assert fake.authorize(order.id, "auth-1") == auth
    cap = fake.capture(auth.id, D("19.00"), True, "inv-cap", "cap-1")
    for _ in range(3):
        assert fake.capture(auth.id, D("19.00"), True, "inv-cap", "cap-1") == cap
    assert len(fake.captures_for(auth.id)) == 1
    ref = fake.refund(cap.id, D("19.00"), "refund-1")
    assert fake.refund(cap.id, D("19.00"), "refund-1") == ref
    assert len(fake.refunds_for(cap.id)) == 1
    assert [c.outcome for c in fake.calls if c.op == "capture"] == ["ok", "replay", "replay", "replay"]


def test_request_id_reused_for_a_different_request(fake: FakePayPal) -> None:
    _, auth_id = held(fake)
    fake.capture(auth_id, D("19.00"), True, "inv-cap", "cap-1")
    err = issue_of(lambda: fake.capture(auth_id, D("5.00"), True, "inv-cap", "cap-1"))
    assert err.issue == "DUPLICATE_REQUEST_ID"
    assert len(fake.captures_for(auth_id)) == 1


def test_void_after_capture_fails(fake: FakePayPal) -> None:
    _, auth_id = held(fake)
    fake.capture(auth_id, D("19.00"), True, "inv-cap", "cap-1")
    assert issue_of(lambda: fake.void(auth_id, "void-1")).issue == "PREVIOUSLY_CAPTURED"
    assert fake.get_authorization(auth_id).status == "CAPTURED"


def test_void_then_capture_fails(fake: FakePayPal) -> None:
    _, auth_id = held(fake)
    voided = fake.void(auth_id, "void-1")
    assert voided is not None and voided.status == "VOIDED"
    assert fake.void(auth_id, "void-1") == voided  # replay
    assert issue_of(lambda: fake.void(auth_id, "void-2")).issue == "PREVIOUSLY_VOIDED"
    assert issue_of(lambda: fake.capture(auth_id, D("19.00"), True, "x", "cap-1")).issue == "AUTHORIZATION_VOIDED"
    assert fake.authorization_released(auth_id) == D("19.00")


def test_refund_cannot_exceed_the_capture(fake: FakePayPal) -> None:
    _, auth_id = held(fake)
    cap = fake.capture(auth_id, D("15.00"), True, "inv-cap", "cap-1")
    assert issue_of(lambda: fake.refund(cap.id, D("15.01"), "refund-1")).issue == "REFUND_AMOUNT_EXCEEDED"
    fake.refund(cap.id, D("5.00"), "refund-2")
    assert fake.get_capture(cap.id).status == "PARTIALLY_REFUNDED"
    assert issue_of(lambda: fake.refund(cap.id, D("10.01"), "refund-3")).issue == "REFUND_AMOUNT_EXCEEDED"
    fake.refund(cap.id, D("10.00"), "refund-4")
    assert fake.get_capture(cap.id).status == "REFUNDED"
    assert issue_of(lambda: fake.refund(cap.id, D("0.01"), "refund-5")).issue == "CAPTURE_FULLY_REFUNDED"
    assert sum(r.amount for r in fake.refunds_for(cap.id)) == D("15.00")


def test_expired_authorization(fake: FakePayPal, clock) -> None:
    _, auth_id = held(fake)
    auth = fake.get_authorization(auth_id)
    assert auth.expiration_time == iso_utc(clock.now() + 29 * 24 * 3600)
    clock.advance(29 * 24 * 3600)
    assert issue_of(lambda: fake.capture(auth_id, D("19.00"), True, "x", "cap-1")).issue == "AUTHORIZATION_EXPIRED"
    assert issue_of(lambda: fake.void(auth_id, "void-1")).issue == "AUTHORIZATION_EXPIRED"
    # No EXPIRED status at PayPal: the status stays CREATED and the time tells.
    assert fake.get_authorization(auth_id).status == "CREATED"


@pytest.mark.parametrize(
    ("op", "issue", "retryable"),
    [
        ("capture", "AUTHORIZATION_EXPIRED", False),
        ("capture", "INTERNAL_SERVER_ERROR", True),
        ("capture", "TRANSACTION_REFUSED", False),
        ("authorize", "PAYMENT_DENIED", False),
        ("capture", "PREVIOUSLY_CAPTURED", False),
        ("refund", "REFUND_AMOUNT_EXCEEDED", False),
        ("void", "TRANSPORT_ERROR", True),
        ("get_authorization", "INTERNAL_SERVER_ERROR", True),
    ],
)
def test_injected_errors(fake: FakePayPal, op: str, issue: str, retryable: bool) -> None:
    order = fake.create_hold(D("19.00"), "spec", "inv-1", "order-1")
    fake.approve(order.id)
    if op != "authorize":
        auth = fake.authorize(order.id, "auth-1")
    calls = {
        "authorize": lambda: fake.authorize(order.id, "auth-1"),
        "capture": lambda: fake.capture(auth.id, D("19.00"), True, "x", "cap-1"),
        "void": lambda: fake.void(auth.id, "void-1"),
        "get_authorization": lambda: fake.get_authorization(auth.id),
    }
    if op == "refund":
        cap = fake.capture(auth.id, D("19.00"), True, "x", "cap-1")
        calls["refund"] = lambda: fake.refund(cap.id, D("19.00"), "refund-1")
    fake.inject(op, issue)
    err = issue_of(calls[op])
    assert err.issue == issue and err.retryable is retryable
    calls[op]()  # the injection was used up; the real call now works
    assert fake.calls[-1].outcome == "ok"


def test_injected_error_after_the_operation_was_applied(fake: FakePayPal) -> None:
    _, auth_id = held(fake)
    fake.inject("capture", "INTERNAL_SERVER_ERROR", after=True)
    err = issue_of(lambda: fake.capture(auth_id, D("19.00"), True, "x", "cap-1"))
    assert err.retryable
    (made,) = fake.captures_for(auth_id)  # PayPal made it; only the answer was lost
    assert fake.capture(auth_id, D("19.00"), True, "x", "cap-1").id == made.id
    assert len(fake.captures_for(auth_id)) == 1


def test_injection_validation(fake: FakePayPal) -> None:
    with pytest.raises(ValueError):
        fake.inject("teleport", "X")
    with pytest.raises(ValueError):
        fake.inject("get_capture", "X", after=True)
    fake.inject("capture", "TRANSACTION_REFUSED", times=2)
    fake.clear_injections()
    _, auth_id = held(fake)
    fake.capture(auth_id, D("19.00"), True, "x", "cap-1")


def test_hook_sees_before_and_after(fake: FakePayPal) -> None:
    seen: list[tuple[str, str, str | None]] = []
    fake.hook = lambda op, phase, rid: seen.append((op, phase, rid))
    held(fake)
    assert seen == [
        ("create_hold", "before", "order-1"),
        ("create_hold", "after", "order-1"),
        ("authorize", "before", "auth-1"),
        ("authorize", "after", "auth-1"),
    ]


def test_state_persists_across_instances(tmp_path, clock) -> None:
    path = tmp_path / "paypal.json"
    first = FakePayPal(clock=clock, state_path=path)
    _, auth_id = held(first)
    cap = first.capture(auth_id, D("19.00"), True, "x", "cap-1")
    second = FakePayPal(clock=clock, state_path=path)
    assert second.get_authorization(auth_id).status == "CAPTURED"
    assert second.capture(auth_id, D("19.00"), True, "x", "cap-1") == cap  # idempotency survives too
    assert len(second.captures_for(auth_id)) == 1
    assert json.loads(path.read_text())["captures"]


def test_external_events(fake: FakePayPal) -> None:
    _, auth_id = held(fake)
    fake.capture_status = "PENDING"
    cap = fake.capture(auth_id, D("19.00"), True, "x", "cap-1")
    assert cap.status == "PENDING"
    fake.complete_capture(cap.id)
    assert fake.get_capture(cap.id).status == "COMPLETED"
    ref = fake.refund_externally(cap.id)
    assert ref.amount == D("19.00") and fake.get_capture(cap.id).status == "REFUNDED"
    _, auth2 = held(fake, n=2)
    fake.void_externally(auth2)
    assert fake.get_authorization(auth2).status == "VOIDED"
    types = [e["event_type"] for e in fake.pending_events()]
    assert types == [
        "PAYMENT.AUTHORIZATION.CREATED",
        "PAYMENT.CAPTURE.PENDING",
        "PAYMENT.CAPTURE.COMPLETED",
        "PAYMENT.CAPTURE.REFUNDED",
        "PAYMENT.AUTHORIZATION.CREATED",
        "PAYMENT.AUTHORIZATION.VOIDED",
    ]


def test_deny_capture(fake: FakePayPal) -> None:
    _, auth_id = held(fake)
    fake.capture_status = "PENDING"
    cap = fake.capture(auth_id, D("19.00"), True, "x", "cap-1")
    fake.deny_capture(cap.id)
    assert fake.get_capture(cap.id).status == "DECLINED"
    assert fake.pending_events()[-1]["event_type"] == "PAYMENT.CAPTURE.DECLINED"
    assert issue_of(lambda: fake.refund(cap.id, D("1.00"), "refund-1")).issue == "TRANSACTION_REFUSED"


def test_signed_deliveries_verify_and_tampering_does_not(fake: FakePayPal) -> None:
    held(fake)
    (delivery,) = fake.take_deliveries("WH-A")
    headers, raw = delivery
    assert fake.verify_webhook(headers, raw, "WH-A")
    lower = {k.lower(): v for k, v in headers.items()}
    assert fake.verify_webhook(lower, raw, "WH-A")  # header names in any case
    assert not fake.verify_webhook(headers, raw, "WH-B")
    assert not fake.verify_webhook(headers, raw.replace(b"19.00", b"91.00"), "WH-A")
    assert not fake.verify_webhook({k: v for k, v in headers.items() if "SIG" not in k}, raw, "WH-A")
    assert not fake.verify_webhook(headers, raw, "")
    assert fake.take_deliveries("WH-A") == []  # the queue was drained


def test_order_payments(fake: FakePayPal) -> None:
    order_id, auth_id = held(fake)
    cap = fake.capture(auth_id, D("19.00"), True, "x", "cap-1")
    ref = fake.refund(cap.id, D("19.00"), "refund-1")
    payments = fake.get_order_payments(order_id)
    assert [a.id for a in payments.authorizations] == [auth_id]
    assert [(c.id, c.authorization_id) for c in payments.captures] == [(cap.id, auth_id)]
    assert [(r.id, r.capture_id) for r in payments.refunds] == [(ref.id, cap.id)]
