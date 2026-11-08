"""Property tests for every money invariant in PLAN.md.

- captured <= authorized <= quote
- each rescue ends in exactly one of CAPTURED or VOIDED
- never two captures
- never void after capture
- refund <= captured
- replaying a money operation with its request id is a no-op
- webhook replays never change state
- a crash between the outbox write and the PayPal response is repaired by the reconciler with
  exactly one capture

The stateful test drives several rescues through random orders of owner, agent, PayPal and
operator actions: injected PayPal errors, lost responses, simulated crashes with a restart on the
same database, external voids, expiry, webhook deliveries in any order with duplicates, and
reconciler passes. Every invariant is checked after every step, and at the end every rescue is
settled and must be either captured once or voided with nothing captured.

(The two keyboard invariants, "no keystroke without CREATED" and "a revoked lease stops input
within one action", belong to keyhold.pay.gate and are tested there.)
"""

from __future__ import annotations

import copy
import os
import shutil
import tempfile
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, event, given, settings, target
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule
from pay_helpers import WEBHOOK_ID, SimulatedCrash, StepClock, crash_at, make_quote, make_spec

from keyhold.pay.client import FakePayPal, is_expired
from keyhold.pay.ledger import CAPTURED_STATES, Ledger, LedgerError, Payments
from keyhold.pay.reconciler import Reconciler
from keyhold.pay.webhooks import WebhookProcessor
from keyhold.types import STATE_RANK, MoneyOp, OutboxStatus, PayPalError, RescueState, Tier

D = Decimal
CENT = D("0.01")

RETRYABLE = ["INTERNAL_SERVER_ERROR", "TRANSPORT_ERROR"]
FINAL_ISSUES = {
    "create_hold": ["PERMISSION_DENIED"],
    "authorize": ["PAYMENT_DENIED", "TRANSACTION_REFUSED"],
    "capture": ["AUTHORIZATION_EXPIRED", "TRANSACTION_REFUSED", "PAYMENT_DENIED", "PREVIOUSLY_CAPTURED"],
    "void": [],
    "refund": ["REFUND_AMOUNT_EXCEEDED"],
}
MONEY_OPS = sorted(FINAL_ISSUES)


def snapshot(fake: FakePayPal) -> dict:
    state = copy.deepcopy(fake._state)
    state.pop("webhooks")
    return state


# ---------------------------------------------------------------------------------------------
# The stateful machine
# ---------------------------------------------------------------------------------------------


class MoneyMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.dir = tempfile.mkdtemp(prefix="keyhold-prop-")
        self.db_path = os.path.join(self.dir, "ledger.db")
        self.clock = StepClock()
        self.fake = FakePayPal(clock=self.clock)
        self._open()
        self.ids: list[str] = []
        self.max_rank: dict[str, int] = {}
        self.ever_captured: set[str] = set()
        self.ever_voided: set[str] = set()
        self.external_voids: set[str] = set()
        self.pool: list[tuple[dict, bytes]] = []

    def _open(self) -> None:
        self.ledger = Ledger(self.db_path, clock=self.clock)
        self.pay = Payments(self.ledger, self.fake)
        self.hooks = WebhookProcessor(self.ledger, self.fake, WEBHOOK_ID)

    def _restart(self) -> None:
        """The process died: drop everything in memory, open the same database again."""
        self.fake.hook = None
        self.ledger.close()
        self._open()

    def _try(self, fn) -> None:
        try:
            fn()
        except (PayPalError, LedgerError):
            pass  # refusals are expected; the invariants decide whether they were right
        except SimulatedCrash:
            event("simulated crash")
            self._restart()

    def _rec(self, i: int):
        return self.ledger.get(self.ids[i % len(self.ids)])

    # -- owner and orchestrator --------------------------------------------------------------

    @precondition(lambda self: len(self.ids) < 3)
    @rule(tier=st.sampled_from(list(Tier)))
    def new_rescue(self, tier: Tier) -> None:
        rec = self.ledger.create_rescue(make_quote(tier), probe=make_spec(len(self.ids)))
        self.ids.append(rec.id)
        self.max_rank[rec.id] = 0

    @precondition(lambda self: self.ids)
    @rule(i=st.integers(0, 2))
    def next_happy_step(self, i: int) -> None:
        """Whatever moves this rescue one step along the happy path (keeps runs deep)."""
        rec = self._rec(i)
        if rec.state is RescueState.QUOTED:
            self._try(lambda: self.pay.create_hold(rec.id))
        elif rec.state is RescueState.HOLD_PENDING:
            self.fake.approve(rec.order_id)
            self._try(lambda: self.pay.authorize(rec.id))
        elif rec.state is RescueState.HELD:
            self._try(lambda: self.ledger.transition(rec.id, RescueState.ACTIVE))
        elif rec.state is RescueState.ACTIVE:
            self._try(lambda: self.ledger.transition(rec.id, RescueState.PROBING))
        elif rec.state is RescueState.PROBING:
            self._try(lambda: self.pay.capture(rec.id))
        elif rec.state is RescueState.CAPTURED:
            self._try(lambda: self.pay.refund(rec.id, "warranty failed twice"))

    # Hypothesis picks rules uniformly; extra copies make forward progress as likely as chaos.
    @precondition(lambda self: self.ids)
    @rule(i=st.integers(0, 2))
    def next_happy_step_again(self, i: int) -> None:
        self.next_happy_step(i)

    @precondition(lambda self: self.ids)
    @rule(i=st.integers(0, 2))
    def next_happy_step_once_more(self, i: int) -> None:
        self.next_happy_step(i)

    @precondition(lambda self: self.ids)
    @rule(i=st.integers(0, 2))
    def create_hold(self, i: int) -> None:
        self._try(lambda: self.pay.create_hold(self._rec(i).id))

    @precondition(lambda self: self.ids)
    @rule(i=st.integers(0, 2))
    def owner_approves(self, i: int) -> None:
        rec = self._rec(i)
        if rec.order_id:
            self.fake.approve(rec.order_id)

    @precondition(lambda self: self.ids)
    @rule(i=st.integers(0, 2))
    def authorize(self, i: int) -> None:
        self._try(lambda: self.pay.authorize(self._rec(i).id))

    @precondition(lambda self: self.ids)
    @rule(i=st.integers(0, 2), to=st.sampled_from([RescueState.ACTIVE, RescueState.PROBING]))
    def agent_moves(self, i: int, to: RescueState) -> None:
        self._try(lambda: self.ledger.transition(self._rec(i).id, to))

    @precondition(lambda self: self.ids)
    @rule(i=st.integers(0, 2), delta=st.sampled_from([None, D("0.00"), D("-0.01"), D("0.01"), D("-5.00"), D("20.00")]))
    def capture(self, i: int, delta: Decimal | None) -> None:
        rec = self._rec(i)
        amount = None if delta is None else rec.quote_amount + delta
        if amount is not None and amount <= 0:
            amount = CENT
        self._try(lambda: self.pay.capture(rec.id, amount))

    @precondition(lambda self: self.ids)
    @rule(i=st.integers(0, 2))
    def void(self, i: int) -> None:
        self._try(lambda: self.pay.void(self._rec(i).id, "owner pressed Stop"))

    @precondition(lambda self: self.ids)
    @rule(i=st.integers(0, 2))
    def refund(self, i: int) -> None:
        self._try(lambda: self.pay.refund(self._rec(i).id, "warranty failed twice"))

    @precondition(lambda self: self.ids)
    @rule(i=st.integers(0, 2))
    def warranty_ok(self, i: int) -> None:
        self._try(lambda: self.ledger.transition(self._rec(i).id, RescueState.WARRANTY_OK))

    # -- PayPal and the world ----------------------------------------------------------------

    @rule(op=st.sampled_from(MONEY_OPS), data=st.data())
    def inject_error(self, op: str, data: st.DataObject) -> None:
        after = data.draw(st.booleans(), label="after")
        if after:
            issue = data.draw(st.sampled_from(RETRYABLE), label="issue")
        else:
            issue = data.draw(st.sampled_from(RETRYABLE + FINAL_ISSUES[op]), label="issue")
        self.fake.inject(op, issue, after=after)

    @rule(op=st.sampled_from(MONEY_OPS), phase=st.sampled_from(["before", "after"]))
    def arm_crash(self, op: str, phase: str) -> None:
        self.fake.hook = crash_at(op, phase)

    @precondition(lambda self: self.ids)
    @rule(i=st.integers(0, 2), phase=st.sampled_from(["before", "after"]))
    def crash_inside_next_money_call(self, i: int, phase: str) -> None:
        """Kill the process inside the rescue's next PayPal money call, then restart."""
        rec = self._rec(i)
        call = {
            RescueState.QUOTED: ("create_hold", lambda: self.pay.create_hold(rec.id)),
            RescueState.HOLD_PENDING: ("authorize", lambda: self.pay.authorize(rec.id)),
            RescueState.HELD: ("void", lambda: self.pay.void(rec.id, "owner pressed Stop")),
            RescueState.ACTIVE: ("void", lambda: self.pay.void(rec.id, "agent gave up")),
            RescueState.PROBING: ("capture", lambda: self.pay.capture(rec.id)),
            RescueState.CAPTURED: ("refund", lambda: self.pay.refund(rec.id, "warranty failed twice")),
        }.get(rec.state)
        if call is None:
            return
        if rec.state is RescueState.HOLD_PENDING and rec.order_id:
            self.fake.approve(rec.order_id)
        self.fake.hook = crash_at(call[0], phase)
        self._try(call[1])
        self.fake.hook = None

    @precondition(lambda self: self.ids)
    @rule(i=st.integers(0, 2))
    def external_void(self, i: int) -> None:
        rec = self._rec(i)
        if rec.authorization_id and self.fake.get_authorization(rec.authorization_id).status in ("CREATED", "PENDING"):
            self.fake.void_externally(rec.authorization_id)
            self.external_voids.add(rec.authorization_id)

    @precondition(lambda self: self.ids)
    @rule(i=st.integers(0, 2))
    def authorization_expires(self, i: int) -> None:
        rec = self._rec(i)
        if rec.authorization_id:
            self.fake.expire(rec.authorization_id)

    @rule(data=st.data())
    def deliver_webhooks(self, data: st.DataObject) -> None:
        self.pool.extend(self.fake.take_deliveries(WEBHOOK_ID))
        if not self.pool:
            return
        picks = data.draw(st.lists(st.integers(0, len(self.pool) - 1), max_size=8), label="deliveries")
        for k in picks:
            headers, raw = self.pool[k]
            out = self.hooks.handle(headers, raw)
            assert out.outcome != "conflict", out

    @rule(orphans=st.booleans())
    def reconcile(self, orphans: bool) -> None:
        self.fake.hook = None  # an armed crash belongs to the orchestrator's next call
        report = Reconciler(self.ledger, self.fake).run_once(orphans=orphans)
        assert not report.conflicts, report.conflicts

    @rule(seconds=st.sampled_from([1, 60, 3600]))
    def time_passes(self, seconds: int) -> None:
        self.clock.advance(seconds)

    # -- invariants ----------------------------------------------------------------------------

    @invariant()
    def money_invariants_hold(self) -> None:
        for rid in self.ids:
            rec = self.ledger.get(rid)
            assert self.ledger.check_invariants(rid) == [], (rid, self.ledger.check_invariants(rid))

            # State only moves forward; captured and voided are exclusive over the whole history.
            rank = STATE_RANK[rec.state]
            assert rank >= self.max_rank[rid], (rec.state, self.max_rank[rid])
            self.max_rank[rid] = rank
            if rec.state in CAPTURED_STATES:
                self.ever_captured.add(rid)
            if rec.state is RescueState.VOIDED:
                self.ever_voided.add(rid)
            assert not (rid in self.ever_captured and rid in self.ever_voided)

            ops = {e.op: e for e in self.ledger.ops(rid)}
            assert sum(e.status is OutboxStatus.PENDING for e in ops.values()) <= 1

            if not rec.authorization_id:
                continue
            auth = self.fake.get_authorization(rec.authorization_id)
            captures = self.fake.captures_for(auth.id)
            # Never two captures; captured <= authorized <= quote, at PayPal and in the ledger.
            assert len(captures) <= 1
            assert auth.amount <= rec.quote_amount
            for cap in captures:
                assert cap.amount <= auth.amount
                refunds = self.fake.refunds_for(cap.id)
                assert sum((r.amount for r in refunds), D("0")) <= cap.amount
                assert len(refunds) <= 1
                # Money moved: the ledger knows, or a pending capture will learn it on replay.
                if rec.capture_id is None:
                    assert ops[MoneyOp.CAPTURE].status is OutboxStatus.PENDING
                else:
                    assert rec.capture_id == cap.id and rec.captured_amount == cap.amount
            if rec.state is RescueState.VOIDED:
                assert captures == []

        # Never void after capture: Keyhold never even asked PayPal to.
        assert not [c for c in self.fake.calls if c.op == "void" and c.outcome == "error:PREVIOUSLY_CAPTURED"]

    def teardown(self) -> None:
        try:
            self._settle_and_check()
        finally:
            self.ledger.close()
            shutil.rmtree(self.dir, ignore_errors=True)

    def _settle_and_check(self) -> None:
        """Stop the chaos, let the reconciler and orchestrator finish, check the end states."""
        self.fake.clear_injections()
        self.fake.hook = None
        rc = Reconciler(self.ledger, self.fake)
        for _ in range(3):
            assert not rc.run_once().conflicts
            for rid in self.ids:
                if self.ledger.get(rid).state in (
                    RescueState.QUOTED,
                    RescueState.HOLD_PENDING,
                    RescueState.HELD,
                    RescueState.ACTIVE,
                    RescueState.PROBING,
                ):
                    self._try(lambda rid=rid: self.pay.void(rid, "test over"))
        deepest = max(self.max_rank.values(), default=0)
        target(float(deepest), label="deepest state rank reached")
        for rid in self.ids:
            rec = self.ledger.get(rid)
            event(f"settled as {rec.state.value}")
            event(f"deepest rank before settling: {self.max_rank[rid]}")
            assert self.ledger.check_invariants(rid) == []
            assert rec.state in CAPTURED_STATES or rec.state is RescueState.VOIDED, rec.state
            assert not [e for e in self.ledger.ops(rid) if e.status is OutboxStatus.PENDING]
            captures = self.fake.captures_for(rec.authorization_id) if rec.authorization_id else []
            if rec.state in CAPTURED_STATES:
                assert len(captures) == 1 and captures[0].id == rec.capture_id
                assert captures[0].amount <= rec.authorized_amount <= rec.quote_amount
            else:
                assert captures == []
                if rec.authorization_id:
                    auth = self.fake.get_authorization(rec.authorization_id)
                    assert auth.status in ("VOIDED", "DENIED") or is_expired(auth.expiration_time, self.clock.now())


MoneyMachine.TestCase.settings = settings(
    max_examples=int(os.environ.get("KEYHOLD_PROPERTY_EXAMPLES", "100")),
    stateful_step_count=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
TestMoneyMachine = MoneyMachine.TestCase


# ---------------------------------------------------------------------------------------------
# Focused properties
# ---------------------------------------------------------------------------------------------

FOCUSED = settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])


def fresh(tier: Tier = Tier.T2) -> tuple[Ledger, FakePayPal, Payments, str]:
    clock = StepClock()
    fake = FakePayPal(clock=clock)
    ledger = Ledger(":memory:", clock=clock)
    pay = Payments(ledger, fake)
    rec = ledger.create_rescue(make_quote(tier), probe=make_spec())
    return ledger, fake, pay, rec.id


def to_probing(ledger: Ledger, fake: FakePayPal, pay: Payments, rid: str) -> None:
    fake.approve(pay.create_hold(rid).id)
    pay.authorize(rid)
    ledger.transition(rid, RescueState.ACTIVE)
    ledger.transition(rid, RescueState.PROBING)


@FOCUSED
@given(tier=st.sampled_from(list(Tier)), cents=st.integers(1, 5000))
def test_captured_never_exceeds_authorized_or_quote(tier: Tier, cents: int) -> None:
    ledger, fake, pay, rid = fresh(tier)
    to_probing(ledger, fake, pay, rid)
    amount = D(cents) / 100
    try:
        pay.capture(rid, amount)
    except LedgerError:
        assert amount > tier.price
        assert fake.all_captures() == [] and ledger.get_op(rid, MoneyOp.CAPTURE) is None
    else:
        rec = ledger.get(rid)
        assert rec.captured_amount == amount <= rec.authorized_amount <= rec.quote_amount == tier.price
        assert [c.amount for c in fake.all_captures()] == [amount]
    ledger.close()


def replay_at_gateway(fake: FakePayPal, entry):
    """Send an outbox row's call to PayPal again, with its own request id and arguments."""
    a, req = entry.args, entry.request_id
    if entry.op is MoneyOp.CREATE_ORDER:
        return fake.create_hold(D(a["amount"]), a["custom_id"], a["invoice_id"], req)
    if entry.op is MoneyOp.AUTHORIZE:
        return fake.authorize(a["order_id"], req)
    if entry.op is MoneyOp.CAPTURE:
        return fake.capture(a["auth_id"], D(a["amount"]), True, a["invoice_id"], req)
    if entry.op is MoneyOp.VOID:
        return fake.void(a["auth_id"], req)
    return fake.refund(a["capture_id"], D(a["amount"]), req)


@FOCUSED
@given(
    steps=st.lists(st.sampled_from(["capture", "void", "refund", "replay"]), min_size=1, max_size=8),
    end=st.sampled_from(["capture", "void"]),
)
def test_replaying_a_money_operation_is_a_no_op(steps: list[str], end: str) -> None:
    ledger, fake, pay, rid = fresh()
    to_probing(ledger, fake, pay, rid)
    getattr(pay, end)(rid, *([] if end == "capture" else ["stop"]))
    if end == "capture":
        pay.refund(rid, "warranty failed")
    before = snapshot(fake)
    rec = ledger.get(rid)
    for step in steps:
        # Gateway level: the same request id and arguments give the same answer and change nothing.
        for entry in ledger.ops(rid):
            again = replay_at_gateway(fake, entry)
            if entry.status is OutboxStatus.DONE:
                assert again == entry.result_object()
        # Ledger level: calling the operation again returns the stored answer or a refusal.
        try:
            if step == "capture":
                pay.capture(rid)
            elif step == "void":
                pay.void(rid, "stop again")
            elif step == "refund":
                pay.refund(rid, "again")
            else:
                Reconciler(ledger, fake).run_once()
        except LedgerError:
            pass
        assert snapshot(fake) == before
        assert ledger.get(rid) == rec
    ledger.close()


@FOCUSED
@given(
    story=st.sampled_from(["captured", "refunded", "voided", "external_void", "pending_capture", "denied"]),
    data=st.data(),
)
def test_webhook_replays_never_change_state(story: str, data: st.DataObject) -> None:
    ledger, fake, pay, rid = fresh()
    to_probing(ledger, fake, pay, rid)
    rec = ledger.get(rid)
    if story == "captured":
        pay.capture(rid)
    elif story == "refunded":
        pay.capture(rid)
        pay.refund(rid, "warranty failed")
    elif story == "voided":
        pay.void(rid, "agent gave up")
    elif story == "external_void":
        fake.void_externally(rec.authorization_id)
    else:
        fake.capture_status = "PENDING"
        cap = pay.capture(rid)
        (fake.complete_capture if story == "pending_capture" else fake.deny_capture)(cap.id)
    deliveries = fake.take_deliveries(WEBHOOK_ID)
    hooks = WebhookProcessor(ledger, fake, WEBHOOK_ID)

    order = data.draw(st.lists(st.integers(0, len(deliveries) - 1), min_size=1, max_size=12), label="first pass")
    last_rank = STATE_RANK[ledger.get(rid).state]
    for k in order:
        assert hooks.handle(*deliveries[k]).outcome in ("applied", "duplicate")
        rank = STATE_RANK[ledger.get(rid).state]
        assert rank >= last_rank
        last_rank = rank
    for k in range(len(deliveries)):  # whatever the first pass missed
        hooks.handle(*deliveries[k])
    settled = ledger.get(rid)
    replay = data.draw(st.permutations(list(range(len(deliveries)))), label="replay")
    for k in replay + replay:
        headers, raw = deliveries[k]
        if data.draw(st.booleans(), label="re-sign"):
            headers = fake.sign_delivery(raw, WEBHOOK_ID)  # PayPal's retry: new transmission
        assert hooks.handle(headers, raw).outcome == "duplicate"
    after = ledger.get(rid)
    assert after == settled
    assert ledger.check_invariants(rid) == []
    ledger.close()


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    phase=st.sampled_from(["before", "after"]),
    failures=st.lists(st.sampled_from(["INTERNAL_SERVER_ERROR", "TRANSPORT_ERROR", "REQUEST_IN_PROGRESS"]), max_size=3),
    webhooks_first=st.booleans(),
    second_crash=st.sampled_from([None, "before", "after"]),
)
def test_crash_in_the_capture_window_ends_in_exactly_one_capture(
    phase: str, failures: list[str], webhooks_first: bool, second_crash: str | None
) -> None:
    tmp = tempfile.mkdtemp(prefix="keyhold-crash-")
    try:
        path = os.path.join(tmp, "ledger.db")
        clock = StepClock()
        fake = FakePayPal(clock=clock)
        ledger = Ledger(path, clock=clock)
        pay = Payments(ledger, fake)
        rid = ledger.create_rescue(make_quote(), probe=make_spec()).id
        to_probing(ledger, fake, pay, rid)

        fake.hook = crash_at("capture", phase)
        with pytest.raises(SimulatedCrash):
            pay.capture(rid)
        ledger.close()

        # Restart. PayPal may be flaky for a while, and the reconciler itself may die mid-replay.
        fake.hook = crash_at("capture", second_crash) if second_crash else None
        ledger = Ledger(path, clock=clock)
        if webhooks_first:
            hooks = WebhookProcessor(ledger, fake, WEBHOOK_ID)
            for headers, raw in fake.take_deliveries(WEBHOOK_ID):
                hooks.handle(headers, raw)
        for issue in failures:
            fake.inject("capture", issue)
        try:
            Reconciler(ledger, fake).startup()
        except SimulatedCrash:
            ledger.close()
            ledger = Ledger(path, clock=clock)
        fake.hook = None
        for _ in range(len(failures) + 2):
            Reconciler(ledger, fake).run_once()

        (capture,) = fake.all_captures()
        rec = ledger.get(rid)
        assert rec.state is RescueState.CAPTURED and rec.capture_id == capture.id
        assert ledger.get_op(rid, MoneyOp.CAPTURE).status is OutboxStatus.DONE
        assert {c.request_id for c in fake.calls if c.op == "capture"} == {f"cap-{rid}"}
        ledger.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@FOCUSED
@given(
    attempts=st.lists(
        st.sampled_from([None, "INTERNAL_SERVER_ERROR", "REFUND_AMOUNT_EXCEEDED", "after"]), min_size=1, max_size=5
    ),
    external_first=st.booleans(),
    cents=st.integers(1, 1900),
)
def test_refund_never_exceeds_the_capture(attempts: list[str | None], external_first: bool, cents: int) -> None:
    ledger, fake, pay, rid = fresh()
    to_probing(ledger, fake, pay, rid)
    cap = pay.capture(rid, D(cents) / 100)
    if external_first:
        fake.refund_externally(cap.id)
    for attempt in attempts:
        if attempt == "after":
            fake.inject("refund", "INTERNAL_SERVER_ERROR", after=True)
        elif attempt:
            fake.inject("refund", attempt)
        try:
            pay.refund(rid, "warranty failed")
        except (PayPalError, LedgerError):
            pass
        Reconciler(ledger, fake).run_once()
        refunds = fake.refunds_for(cap.id)
        assert len(refunds) <= 1 and sum((r.amount for r in refunds), D("0")) <= cap.amount
        rec = ledger.get(rid)
        assert rec.refunded_amount is None or rec.refunded_amount <= rec.captured_amount
        assert ledger.check_invariants(rid) == []
    ledger.close()


@FOCUSED
@given(
    ending=st.sampled_from(["give_up", "turn_limit", "stop", "timeout", "probe_never_passed", "probe_passed"]),
    capture_issue=st.sampled_from([None, "TRANSACTION_REFUSED", "AUTHORIZATION_EXPIRED", "INTERNAL_SERVER_ERROR"]),
)
def test_every_ending_is_captured_or_voided(ending: str, capture_issue: str | None) -> None:
    ledger, fake, pay, rid = fresh()
    to_probing(ledger, fake, pay, rid)
    if ending == "probe_passed":
        if capture_issue:
            fake.inject("capture", capture_issue)
        try:
            pay.capture(rid)
        except PayPalError as err:
            if not err.retryable:
                pay.void(rid, f"capture refused: {err.issue}")
        Reconciler(ledger, fake).run_once()
    else:
        pay.void(rid, ending)
    rec = ledger.get(rid)
    captures = fake.all_captures()
    assert (rec.state is RescueState.CAPTURED) != (rec.state is RescueState.VOIDED)
    assert len(captures) == (1 if rec.state is RescueState.CAPTURED else 0)
    ledger.close()
