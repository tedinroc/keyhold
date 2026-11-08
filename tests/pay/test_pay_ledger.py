"""The SQLite ledger, its state machine and outbox, and the Payments executor."""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest
from pay_helpers import make_quote, make_spec, new_rescue, to_held, to_probing

from keyhold.pay.client import FakePayPal
from keyhold.pay.ledger import (
    IllegalTransition,
    InvariantViolation,
    Ledger,
    LedgerError,
    OpFailed,
    OpInFlight,
    Payments,
)
from keyhold.types import Authorization, MoneyOp, OutboxStatus, PayPalError, Quote, RescueState, Tier

D = Decimal


# ---------------------------------------------------------------------------------------------
# rescues and the state machine
# ---------------------------------------------------------------------------------------------


def test_create_rescue_stores_the_quote(ledger: Ledger) -> None:
    quote = make_quote(Tier.T3)
    rec = ledger.create_rescue(quote, rescue_id="r-1")
    assert rec.state is RescueState.QUOTED and rec.quote_amount == D("29.00")
    assert rec.quote_hash == quote.quote_hash and rec.quote["evidence"] == list(quote.evidence)
    assert rec.invoice_id == "keyhold-r-1" and rec.capture_invoice_id == "keyhold-r-1-cap"
    assert rec.probe_spec_hash is None
    assert [e.kind for e in ledger.events("r-1")] == ["quoted"]


def test_tampered_quote_is_refused(ledger: Ledger) -> None:
    good = make_quote(Tier.T1)
    forged = Quote(good.fault_class, good.evidence, Tier.T3, good.plan, good.quote_hash)
    with pytest.raises(InvariantViolation):
        ledger.create_rescue(forged)


def test_probe_is_fixed_at_checkout(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = ledger.create_rescue(make_quote())
    with pytest.raises(LedgerError, match="probe"):
        pay.create_hold(rec.id)
    rec = ledger.set_probe(rec.id, make_spec())
    assert rec.probe_spec_hash == make_spec().spec_hash()
    order = pay.create_hold(rec.id)
    assert order.custom_id == make_spec().spec_hash()  # the spec hash travels in custom_id
    with pytest.raises(LedgerError):
        ledger.set_probe(rec.id, make_spec(1))


def test_only_allowed_transitions(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = new_rescue(ledger)
    with pytest.raises(IllegalTransition):
        ledger.transition(rec.id, RescueState.HELD)
    with pytest.raises(IllegalTransition):
        ledger.transition(rec.id, RescueState.HOLD_PENDING)  # no order yet
    rec = to_held(ledger, pay, fake)
    with pytest.raises(IllegalTransition):
        ledger.transition(rec.id, RescueState.CAPTURED)
    assert ledger.transition(rec.id, RescueState.HELD).state is RescueState.HELD  # same state: no-op
    ledger.transition(rec.id, RescueState.ACTIVE)
    with pytest.raises(IllegalTransition):
        ledger.transition(rec.id, RescueState.HELD)


def test_terminal_states_never_move(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = to_held(ledger, pay, fake)
    pay.void(rec.id, "owner pressed Stop")
    for target in RescueState:
        if target is RescueState.VOIDED:
            continue
        with pytest.raises(IllegalTransition):
            ledger.transition(rec.id, target)
        if target in (RescueState.HELD, RescueState.CAPTURED, RescueState.REFUNDED):
            assert ledger.advance(rec.id, target) is False
    assert ledger.get(rec.id).end_reason == "owner pressed Stop"


def test_advance_never_moves_backwards(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = to_probing(ledger, pay, fake)
    assert ledger.advance(rec.id, RescueState.HELD) is False
    assert ledger.get(rec.id).state is RescueState.PROBING
    with pytest.raises(ValueError):
        ledger.advance(rec.id, RescueState.ACTIVE)


def test_held_needs_a_live_authorization_for_the_quote(ledger: Ledger, clock) -> None:
    rec = new_rescue(ledger)
    ledger._set(rec.id, order_id="O-1")
    ledger.transition(rec.id, RescueState.HOLD_PENDING)
    ledger.record_authorization(rec.id, Authorization("A-1", "CREATED", D("9.00"), "2099-01-01T00:00:00Z"))
    with pytest.raises(IllegalTransition, match="quoted amount"):
        ledger.transition(rec.id, RescueState.HELD)


# ---------------------------------------------------------------------------------------------
# the happy path and request ids
# ---------------------------------------------------------------------------------------------


def test_happy_path_uses_deterministic_request_ids(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = to_probing(ledger, pay, fake)
    cap = pay.capture(rec.id)
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.CAPTURED
    assert rec.capture_id == cap.id and rec.captured_amount == D("19.00") == rec.authorized_amount == rec.quote_amount
    assert rec.auth_status == "CAPTURED" and rec.capture_status == "COMPLETED"
    ops = {e.op: e for e in ledger.ops(rec.id)}
    assert {op: e.request_id for op, e in ops.items()} == {
        MoneyOp.CREATE_ORDER: f"order-{rec.id}",
        MoneyOp.AUTHORIZE: f"auth-{rec.id}",
        MoneyOp.CAPTURE: f"cap-{rec.id}",
    }
    assert all(e.status is OutboxStatus.DONE for e in ops.values())
    assert [c.request_id for c in fake.calls if c.op == "capture"] == [f"cap-{rec.id}"]
    ledger.transition(rec.id, RescueState.WARRANTY_OK)
    assert ledger.get(rec.id).terminal
    assert ledger.check_invariants(rec.id) == []


def test_capture_may_undercut_but_never_exceed_the_quote(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = to_probing(ledger, pay, fake)
    with pytest.raises(InvariantViolation, match="exceeds the quote"):
        pay.capture(rec.id, D("19.01"))
    assert fake.money_calls("capture") == [] and ledger.get_op(rec.id, MoneyOp.CAPTURE) is None
    cap = pay.capture(rec.id, "12.00")
    assert cap.amount == D("12.00") and cap.final_capture
    assert fake.authorization_released(ledger.get(rec.id).authorization_id) == D("7.00")


def test_capture_only_from_probing(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = to_held(ledger, pay, fake)
    with pytest.raises(IllegalTransition):
        pay.capture(rec.id)
    ledger.transition(rec.id, RescueState.ACTIVE)
    with pytest.raises(IllegalTransition):
        pay.capture(rec.id)
    assert fake.money_calls("capture") == []


def test_outbox_row_is_committed_before_the_call(ledger: Ledger, pay: Payments, fake: FakePayPal, db_path) -> None:
    rec = to_probing(ledger, pay, fake)
    seen = {}

    def hook(op: str, phase: str, request_id: str | None) -> None:
        if op == "capture" and phase == "before":
            other = sqlite3.connect(db_path)  # a different connection sees only committed data
            seen["row"] = other.execute(
                "SELECT status, request_id FROM outbox WHERE rescue_id=? AND op='capture'", (rec.id,)
            ).fetchone()
            other.close()

    fake.hook = hook
    pay.capture(rec.id)
    assert seen["row"] == ("PENDING", f"cap-{rec.id}")


def test_calling_again_after_success_does_not_call_paypal(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = to_probing(ledger, pay, fake)
    first = pay.capture(rec.id)
    calls = len(fake.calls)
    assert pay.capture(rec.id) == first
    assert len(fake.calls) == calls
    assert len(fake.all_captures()) == 1


# ---------------------------------------------------------------------------------------------
# one money operation in flight; retries replay the same request id
# ---------------------------------------------------------------------------------------------


def test_retryable_error_keeps_the_row_pending_and_retry_replays(
    ledger: Ledger, pay: Payments, fake: FakePayPal
) -> None:
    rec = to_probing(ledger, pay, fake)
    fake.inject("capture", "INTERNAL_SERVER_ERROR", after=True)  # PayPal captured, answer lost
    with pytest.raises(PayPalError) as info:
        pay.capture(rec.id)
    assert info.value.retryable
    entry = ledger.get_op(rec.id, MoneyOp.CAPTURE)
    assert entry.status is OutboxStatus.PENDING and entry.attempts == 1 and entry.error == "INTERNAL_SERVER_ERROR"
    assert ledger.get(rec.id).state is RescueState.PROBING
    # Nothing else may move money while the capture is unresolved.
    with pytest.raises(OpInFlight):
        pay.void(rec.id, "owner pressed Stop")
    cap = pay.capture(rec.id)
    assert ledger.get(rec.id).state is RescueState.CAPTURED and ledger.get(rec.id).capture_id == cap.id
    assert len(fake.all_captures()) == 1
    assert [c.request_id for c in fake.calls if c.op == "capture"] == [f"cap-{rec.id}"] * 2


def test_resuming_with_different_arguments_is_refused(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = to_probing(ledger, pay, fake)
    fake.inject("capture", "TRANSPORT_ERROR")
    with pytest.raises(PayPalError):
        pay.capture(rec.id, "10.00")
    with pytest.raises(LedgerError, match="different arguments"):
        pay.capture(rec.id, "19.00")
    pay.capture(rec.id, "10.00")
    assert ledger.get(rec.id).captured_amount == D("10.00")


def test_database_allows_one_pending_row_per_rescue(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = to_probing(ledger, pay, fake)
    fake.inject("capture", "INTERNAL_SERVER_ERROR")
    with pytest.raises(PayPalError):
        pay.capture(rec.id)
    with pytest.raises(sqlite3.IntegrityError), ledger.transaction() as db:
        db.execute(
            "INSERT INTO outbox (rescue_id, op, request_id, status, args_json, created_at, updated_at)"
            " VALUES (?, 'void', ?, 'PENDING', '{}', 0, 0)",
            (rec.id, f"void-{rec.id}"),
        )
    with pytest.raises(OpInFlight):
        ledger.begin_op(rec.id, MoneyOp.VOID, {"auth_id": "x"}, allowed_states=[RescueState.PROBING])


def test_each_operation_happens_once_per_rescue(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = to_probing(ledger, pay, fake)
    pay.capture(rec.id)
    with pytest.raises(sqlite3.IntegrityError), ledger.transaction() as db:
        db.execute(
            "INSERT INTO outbox (rescue_id, op, request_id, status, args_json, created_at, updated_at)"
            " VALUES (?, 'capture', 'cap-other', 'PENDING', '{}', 0, 0)",
            (rec.id,),
        )


# ---------------------------------------------------------------------------------------------
# void
# ---------------------------------------------------------------------------------------------


def test_never_void_after_capture(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = to_probing(ledger, pay, fake)
    pay.capture(rec.id)
    with pytest.raises(IllegalTransition, match="never void after capture"):
        pay.void(rec.id, "too late")
    with pytest.raises(IllegalTransition):
        ledger.transition(rec.id, RescueState.VOIDED)
    with pytest.raises(IllegalTransition):
        ledger.begin_op(rec.id, MoneyOp.VOID, {"auth_id": "x"}, allowed_states=list(RescueState))
    assert fake.money_calls("void") == []
    assert not [c for c in fake.calls if c.op == "void"]


def test_void_releases_the_hold(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = to_held(ledger, pay, fake)
    ledger.transition(rec.id, RescueState.ACTIVE)
    auth = pay.void(rec.id, "agent gave up: GRUB password")
    assert auth is not None and auth.status == "VOIDED"
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.VOIDED and rec.auth_status == "VOIDED"
    assert rec.end_reason == "agent gave up: GRUB password"
    assert ledger.get_op(rec.id, MoneyOp.VOID).request_id == f"void-{rec.id}"
    assert fake.authorization_released(rec.authorization_id) == D("19.00")
    assert pay.void(rec.id, "again") is None  # already voided: nothing to do
    assert len(fake.money_calls("void")) == 1
    with pytest.raises(IllegalTransition):
        pay.capture(rec.id)


def test_void_before_any_authorization_touches_only_the_ledger(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = new_rescue(ledger)
    pay.create_hold(rec.id)
    assert pay.void(rec.id, "owner closed the checkout") is None
    assert ledger.get(rec.id).state is RescueState.VOIDED
    assert [c for c in fake.calls if c.op == "void"] == []


def test_void_of_an_expired_authorization_ends_voided(ledger: Ledger, pay: Payments, fake: FakePayPal, clock) -> None:
    rec = to_held(ledger, pay, fake)
    clock.advance(30 * 24 * 3600)
    pay.void(rec.id, "rescue timeout")
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.VOIDED and "expired" in rec.end_reason
    assert ledger.get_op(rec.id, MoneyOp.VOID).status is OutboxStatus.FAILED


def test_void_of_an_externally_voided_authorization(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = to_held(ledger, pay, fake)
    fake.void_externally(rec.authorization_id)
    auth = pay.void(rec.id, "owner pressed Stop")
    assert auth.status == "VOIDED"
    assert ledger.get(rec.id).state is RescueState.VOIDED
    assert ledger.get_op(rec.id, MoneyOp.VOID).status is OutboxStatus.DONE


# ---------------------------------------------------------------------------------------------
# final refusals
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("issue", ["TRANSACTION_REFUSED", "PAYMENT_DENIED", "AUTHORIZATION_EXPIRED"])
def test_refused_capture_then_void(ledger: Ledger, pay: Payments, fake: FakePayPal, issue: str) -> None:
    rec = to_probing(ledger, pay, fake)
    fake.inject("capture", issue)
    with pytest.raises(PayPalError) as info:
        pay.capture(rec.id)
    assert info.value.issue == issue
    assert ledger.get_op(rec.id, MoneyOp.CAPTURE).status is OutboxStatus.FAILED
    assert ledger.get(rec.id).state is RescueState.PROBING
    with pytest.raises(OpFailed):
        pay.capture(rec.id)
    pay.void(rec.id, f"capture refused: {issue}")
    assert ledger.get(rec.id).state is RescueState.VOIDED
    assert fake.all_captures() == []


def test_previously_captured_with_no_capture_at_paypal_is_a_failure(
    ledger: Ledger, pay: Payments, fake: FakePayPal
) -> None:
    rec = to_probing(ledger, pay, fake)
    fake.inject("capture", "PREVIOUSLY_CAPTURED")
    with pytest.raises(PayPalError):
        pay.capture(rec.id)
    assert ledger.get_op(rec.id, MoneyOp.CAPTURE).status is OutboxStatus.FAILED
    pay.void(rec.id, "capture refused")
    assert ledger.get(rec.id).state is RescueState.VOIDED


def test_already_captured_finds_the_existing_capture(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = to_probing(ledger, pay, fake)
    # PayPal already holds a capture for this authorization whose answer Keyhold never got and
    # whose request id PayPal no longer remembers.
    existing = fake.capture(rec.authorization_id, D("19.00"), True, rec.capture_invoice_id, "lost-request")
    cap = pay.capture(rec.id)
    assert cap.id == existing.id
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.CAPTURED and rec.capture_id == existing.id
    assert ledger.get_op(rec.id, MoneyOp.CAPTURE).status is OutboxStatus.DONE
    assert len(fake.all_captures()) == 1


def test_denied_authorization_ends_voided(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = new_rescue(ledger)
    order = pay.create_hold(rec.id)
    fake.approve(order.id)
    fake.inject("authorize", "PAYMENT_DENIED")
    with pytest.raises(PayPalError):
        pay.authorize(rec.id)
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.VOIDED and "PAYMENT_DENIED" in rec.end_reason


def test_authorization_status_denied_or_pending(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = new_rescue(ledger)
    fake.approve(pay.create_hold(rec.id).id)
    fake.next_authorization_status = "PENDING"
    pay.authorize(rec.id)
    assert ledger.get(rec.id).state is RescueState.HOLD_PENDING  # waits for the webhook/reconciler
    rec2 = new_rescue(ledger, n=2)
    fake.approve(pay.create_hold(rec2.id).id)
    fake.next_authorization_status = "DENIED"
    pay.authorize(rec2.id)
    assert ledger.get(rec2.id).state is RescueState.VOIDED


def test_order_already_authorized_reads_back(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = new_rescue(ledger)
    order = pay.create_hold(rec.id)
    fake.approve(order.id)
    fake.authorize(order.id, "someone-else")  # authorized with another request id
    auth = pay.authorize(rec.id)
    assert ledger.get(rec.id).state is RescueState.HELD and ledger.get(rec.id).authorization_id == auth.id


def test_create_order_failure_ends_the_rescue(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = new_rescue(ledger)
    fake.inject("create_hold", "INTERNAL_SERVER_ERROR")
    with pytest.raises(PayPalError):
        pay.create_hold(rec.id)
    assert ledger.get(rec.id).state is RescueState.QUOTED
    pay.create_hold(rec.id)  # retry with the same request id
    assert ledger.get(rec.id).state is RescueState.HOLD_PENDING and fake.order_count() == 1
    rec2 = new_rescue(ledger, n=2)
    fake.inject("create_hold", "PERMISSION_DENIED")
    with pytest.raises(PayPalError):
        pay.create_hold(rec2.id)
    assert ledger.get(rec2.id).state is RescueState.VOIDED


class MismatchedAuthorization(FakePayPal):
    """Answers authorize with an amount that differs from the order (a PayPal-side surprise)."""

    def authorize(self, order_id: str, request_id: str) -> Authorization:
        auth = super().authorize(order_id, request_id)
        return Authorization(auth.id, auth.status, auth.amount - D("1.00"), auth.expiration_time, auth.order_id)


def test_authorization_amount_mismatch_is_voided(ledger: Ledger, clock) -> None:
    fake = MismatchedAuthorization(clock=clock)
    pay = Payments(ledger, fake)
    rec = new_rescue(ledger)
    fake.approve(pay.create_hold(rec.id).id)
    pay.authorize(rec.id)
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.VOIDED
    assert fake.get_authorization(rec.authorization_id).status == "VOIDED"
    assert any(e.kind == "conflict" for e in ledger.events(rec.id))


# ---------------------------------------------------------------------------------------------
# refund
# ---------------------------------------------------------------------------------------------


def test_full_refund(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = to_probing(ledger, pay, fake)
    pay.capture(rec.id, "15.00")
    with pytest.raises(IllegalTransition):
        ledger.transition(rec.id, RescueState.REFUNDED)  # needs a refund first
    ref = pay.refund(rec.id, "warranty: two failed probes")
    rec = ledger.get(rec.id)
    assert ref.amount == D("15.00") == rec.refunded_amount
    assert rec.state is RescueState.REFUNDED and rec.capture_status == "REFUNDED"
    assert ledger.get_op(rec.id, MoneyOp.REFUND).request_id == f"refund-{rec.id}"
    assert pay.refund(rec.id, "again") == ref
    assert len(fake.all_refunds()) == 1


def test_refund_only_after_capture(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = to_probing(ledger, pay, fake)
    with pytest.raises(LedgerError):
        pay.refund(rec.id, "no")


def test_refund_made_outside_keyhold_is_recognised(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = to_probing(ledger, pay, fake)
    cap = pay.capture(rec.id)
    outside = fake.refund_externally(cap.id)
    ref = pay.refund(rec.id, "warranty failed")
    assert ref.id == outside.id
    assert ledger.get(rec.id).state is RescueState.REFUNDED and len(fake.all_refunds()) == 1


def test_injected_refund_refusal_without_a_refund(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = to_probing(ledger, pay, fake)
    pay.capture(rec.id)
    fake.inject("refund", "REFUND_AMOUNT_EXCEEDED")
    with pytest.raises(PayPalError):
        pay.refund(rec.id, "warranty failed")
    assert ledger.get(rec.id).state is RescueState.CAPTURED
    assert ledger.get_op(rec.id, MoneyOp.REFUND).status is OutboxStatus.FAILED


# ---------------------------------------------------------------------------------------------
# recording guards
# ---------------------------------------------------------------------------------------------


def test_record_guards(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    from keyhold.types import Capture, Refund

    rec = to_probing(ledger, pay, fake)
    big = Capture("C-big", "COMPLETED", D("19.01"), True, rec.authorization_id)
    with pytest.raises(InvariantViolation):
        ledger.record_capture(rec.id, big)
    cap = pay.capture(rec.id)
    with pytest.raises(InvariantViolation, match="second capture"):
        ledger.record_capture(rec.id, Capture("C-other", "COMPLETED", D("1.00"), True, rec.authorization_id))
    with pytest.raises(InvariantViolation):
        ledger.record_refund(rec.id, Refund("R-big", "COMPLETED", D("19.01"), cap.id))
    with pytest.raises(InvariantViolation):
        ledger.record_authorization(rec.id, Authorization("A-other", "CREATED", D("19.00"), None))
    assert ledger.check_invariants(rec.id) == []


def test_status_never_regresses(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = to_probing(ledger, pay, fake)
    cap = pay.capture(rec.id)
    ledger.record_capture_status(rec.id, "PENDING")
    assert ledger.get(rec.id).capture_status == "COMPLETED"
    ledger.record_auth_status(rec.id, "CREATED")
    assert ledger.get(rec.id).auth_status == "CAPTURED"
    pay.refund(rec.id, "warranty failed")
    ledger.record_capture_status(rec.id, "COMPLETED")
    assert ledger.get(rec.id).capture_status == "REFUNDED"
    assert cap.id == ledger.get(rec.id).capture_id


def test_nested_transaction_failure_rolls_back_only_the_inner_part(ledger: Ledger) -> None:
    rec = new_rescue(ledger)
    with ledger.transaction():
        ledger.log(rec.id, "outer")
        with pytest.raises(RuntimeError), ledger.transaction():
            ledger.log(rec.id, "inner")
            raise RuntimeError("boom")
    kinds = [e.kind for e in ledger.events(rec.id)]
    assert "outer" in kinds and "inner" not in kinds


def test_payments_never_call_paypal_inside_a_transaction(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    rec = to_probing(ledger, pay, fake)
    with pytest.raises(RuntimeError, match="inside a ledger transaction"), ledger.transaction():
        pay.capture(rec.id)
    assert fake.all_captures() == []
    assert ledger.get_op(rec.id, MoneyOp.CAPTURE) is None  # the outbox row was rolled back too


def test_another_threads_transaction_does_not_block_a_paypal_call(
    ledger: Ledger, pay: Payments, fake: FakePayPal
) -> None:
    """The owner's Stop runs on a web thread while the agent thread writes events. Its void
    must not be refused because some other thread has a transaction open."""
    import threading

    rec = to_held(ledger, pay, fake)
    inside, release = threading.Event(), threading.Event()

    def busy() -> None:
        with ledger.transaction():
            ledger.log(rec.id, "agent_type", "busy")
            inside.set()
            assert release.wait(5)

    worker = threading.Thread(target=busy)
    worker.start()
    assert inside.wait(5)
    assert not ledger.in_transaction  # this thread is not in it
    release.set()
    worker.join(5)
    pay.void(rec.id, "owner pressed Stop")
    assert ledger.get(rec.id).state is RescueState.VOIDED
    with ledger.transaction():
        assert ledger.in_transaction


def test_lookups_and_listing(ledger: Ledger, pay: Payments, fake: FakePayPal) -> None:
    a = to_probing(ledger, pay, fake)
    cap = pay.capture(a.id)
    b = new_rescue(ledger, n=2)
    assert ledger.find(capture_id=cap.id).id == a.id
    assert ledger.find(authorization_id=a.authorization_id).id == a.id
    assert ledger.find(order_id=a.order_id).id == a.id
    assert ledger.find(invoice_id=b.invoice_id).id == b.id
    assert ledger.find(capture_id="nope") is None
    assert {r.id for r in ledger.open_rescues()} == {a.id, b.id}
    assert [r.id for r in ledger.rescues([RescueState.QUOTED])] == [b.id]
    assert ledger.pending_ops() == []
    assert any(e.kind == "paypal_call" for e in ledger.events(a.id))
    after = ledger.events(a.id)[-1].id
    assert ledger.events(a.id, after_id=after) == []


def test_ledger_survives_reopen(db_path, clock, fake: FakePayPal) -> None:
    led = Ledger(db_path, clock=clock)
    rec = to_probing(led, Payments(led, fake), fake)
    led.close()
    again = Ledger(db_path, clock=clock)
    assert again.get(rec.id).state is RescueState.PROBING
    again.close()
