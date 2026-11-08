"""Orchestrator: quote -> hold -> gated agent -> probe -> capture or void -> warranty.

Capture only after a stable probe pass (final_capture=true, amount <= quote). Void on give_up,
turn limit, owner Stop, rescue timeout or a probe that never passes. Never void after capture.

The steps, as the web app drives them::

    rescues = Orchestrator(ledger, gateway)
    look, rec = rescues.free_look(console, model, probe=spec)   # screenshots only, no keys
    order = rescues.place_hold(rec.id)          # order with intent AUTHORIZE for the quote
    ...                                          # the owner approves order.id with the JS SDK
    rescues.confirm_hold(rec.id)                 # authorize: HOLD_PENDING -> HELD
    report = rescues.run(rec.id, console, model, ssh=ssh, http=http, console_password=pw)
    rescues.stop(rec.id)                         # from another thread: the owner's Stop button

What ``run`` does:

1. Checks the hold with PayPal through a ``HoldLease`` (GET authorization: CREATED, amount at
   least the quote, this rescue's ids) and moves the rescue to ACTIVE.
2. Runs the keyboard-only agent on a ``GatedConsole``: every key goes through the lease, which
   re-checks PayPal every few actions and on a short TTL. The lease also carries the rescue
   time limit. When the agent returns, the lease is revoked, so the keyboard is gone.
3. If the agent called finished: PROBING, then the owner probe built from the spec fixed at
   checkout (never one the caller passes in) must pass twice, far enough apart, with no failure
   between. Then the capture: the quoted amount or less, final_capture=true, request id
   ``cap-{rescue_id}``. Any other ending voids with ``void-{rescue_id}``.
4. After a capture, the warranty re-probes on its schedule. Two failed checks in a row refund
   the capture in full with ``refund-{rescue_id}``; otherwise the rescue ends WARRANTY_OK.

All money moves through ``keyhold.pay.ledger.Payments``, so every PayPal call has an outbox row
written first and a deterministic request id. Nothing here retries a money call with a new id:
a retryable PayPal error is retried a few times with the same id, and if it persists the outbox
row stays PENDING for the reconciler.

``stop`` revokes the lease first (input is cut; an input already being sent finishes, nothing
after it gets through) and then voids. Stop and the capture decision take the same per-rescue
lock, so exactly one of them wins: a Stop before the capture voids, a Stop after it cannot.

Faults the free look can see are unfixable from the console (for example a password-protected
boot menu, class ``console_locked``) get no quote and so no hold. A fault that turns out to be
unfixable only after the hold is placed ends with the agent's give_up, which voids the hold.

Crashes. Nothing here moves money in a ``finally`` block or on ``BaseException``. A process that
dies mid-rescue leaves the ledger and outbox as they were; ``Reconciler.startup()`` replays an
unfinished capture with its original request id (exactly one capture) and voids holds whose
agent died. ``resume_warranty`` picks the warranty up again from the checks stored in the ledger;
``warranties_due`` lists the captured rescues whose warranty nobody is running, including
captures the reconciler completed. The warranty clock starts when the rescue was captured,
whoever completed the capture.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from keyhold.agent.firewall import KeystrokeFirewall
from keyhold.agent.loop import DEFAULT_MAX_TURNS, Settle, run_agent
from keyhold.agent.quote import FreeLook, clean_owner_note, free_look, verify_quote
from keyhold.pay.gate import DEFAULT_CHECK_EVERY, DEFAULT_TTL_S, GatedConsole, HoldLease
from keyhold.pay.ledger import (
    PRE_CAPTURE_STATES,
    IllegalTransition,
    Ledger,
    LedgerError,
    OpFailed,
    OpInFlight,
    Payments,
    RefundDeferred,
    RescueRecord,
)
from keyhold.probe.owner import (
    HttpTransport,
    ProbePolicy,
    Prober,
    SshTransport,
    StableOutcome,
    WarrantyDecision,
    WarrantyPlan,
    WarrantySchedule,
    WarrantyVerdict,
    probe_policy,
    run_warranty,
    wait_for_stable_pass,
)
from keyhold.types import (
    NO_MONEY_CAPTURE,
    AgentEvent,
    AgentOutcome,
    AgentResult,
    Authorization,
    Capture,
    Clock,
    Console,
    Firewall,
    HoldRevoked,
    ModelClient,
    MoneyOp,
    Order,
    OutboxStatus,
    PaymentGateway,
    PayPalError,
    ProbeResult,
    ProbeSpec,
    Quote,
    Refund,
    RescueState,
    SystemClock,
    Tier,
    money,
)

__all__ = [
    "OWNER_STOP",
    "Orchestrator",
    "RescueConfig",
    "RescueEventSink",
    "RescueReport",
    "StopResult",
    "default_goal",
    "redact",
]

OWNER_STOP = "the owner pressed Stop"

# Capture statuses that mean no money moved, so there is nothing for a warranty to protect.
_NO_MONEY_CAPTURE = NO_MONEY_CAPTURE
MAX_RETRY_AFTER_S = 30.0  # longest Retry-After honoured inline; longer waits go to the reconciler

RescueEventSink = Callable[[str, AgentEvent], None]
"""Status-page sink: called with (rescue_id, event). Screenshot events carry PNG bytes."""


@dataclass(frozen=True)
class RescueConfig:
    """Knobs for one orchestrator. ``policy`` overrides the probe timing that ``judge_mode`` picks.

    ``rescue_timeout_s`` is the agent's wall-clock limit, enforced by the lease: when it passes,
    the next key or turn ends the session and the hold is voided.
    """

    max_turns: int = DEFAULT_MAX_TURNS
    judge_mode: bool = False
    policy: ProbePolicy | None = None
    rescue_timeout_s: float = 45 * 60.0
    lease_check_every: int = DEFAULT_CHECK_EVERY
    lease_ttl_s: float = DEFAULT_TTL_S
    paypal_attempts: int = 3  # tries per money call with the same request id
    paypal_retry_s: float = 2.0
    warranty: bool = True  # run the warranty in ``run`` right after a capture
    settle: Settle | None = None
    console_user: str = "root"

    def __post_init__(self) -> None:
        if self.max_turns < 1 or self.paypal_attempts < 1:
            raise ValueError("max_turns and paypal_attempts must be at least 1")
        if self.rescue_timeout_s <= 0:
            raise ValueError("rescue_timeout_s must be positive")

    @property
    def probe_policy(self) -> ProbePolicy:
        return self.policy or probe_policy(self.judge_mode)


@dataclass(frozen=True)
class RescueReport:
    """How a ``run`` ended. ``state`` is the ledger state when ``run`` returned."""

    rescue_id: str
    state: RescueState
    reason: str
    agent: AgentResult | None = None
    stable: StableOutcome | None = None
    warranty: WarrantyDecision | None = None
    capture_id: str | None = None
    captured_amount: Decimal | None = None
    refunded_amount: Decimal | None = None
    lease_checks: int = 0
    lease_actions: int = 0


@dataclass(frozen=True)
class StopResult:
    rescue_id: str
    state: RescueState
    voided: bool
    detail: str


@dataclass
class _Session:
    lease: HoldLease
    stop: threading.Event = field(default_factory=threading.Event)
    stop_reason: str | None = None


def redact(value: Any, secrets: Iterable[str]) -> Any:
    """``value`` with every secret string replaced, through dicts, lists and tuples."""
    secrets = [s for s in secrets if s]
    if not secrets:
        return value
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[console password]")
        return value
    if isinstance(value, dict):
        return {k: redact(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact(v, secrets) for v in value)
    return value


def default_goal(
    spec: ProbeSpec,
    console_password: str | None,
    console_user: str = "root",
    server_ssh_port: int | None = None,
) -> str:
    """The agent's goal. It names what must work again but not the owner's exact check.

    ``server_ssh_port`` is the port sshd listens on inside the server. It differs from the probe's
    port when the owner reaches the server through port forwarding (the local test VMs do); the
    agent is told the server's own port so it never "fixes" the forwarding by adding a port."""
    port = server_ssh_port if server_ssh_port is not None else spec.ssh_port
    goal = (
        f"Bring this server back so that sshd accepts logins again on port {port} (its own port, as "
        "configured before the fault) and its web page is served again, then call finished. Do not "
        "change the SSH port or any other setting the fault does not need."
    )
    if console_password:
        goal += (
            f" Console login: user {console_user}, password {console_password}. Type the password only "
            "at a password prompt."
        )
    else:
        goal += " No console password was given; if the fix needs one, call give_up."
    return goal


def _quote_from_record(rec: RescueRecord) -> Quote:
    q = rec.quote
    return Quote(
        fault_class=q["fault_class"],
        evidence=tuple(q["evidence"]),
        tier=Tier(q["tier"]),
        plan=q["plan"],
        quote_hash=q["quote_hash"],
    )


def _spec_from_record(rec: RescueRecord) -> ProbeSpec:
    if not rec.probe_spec or not rec.probe_spec_hash:
        raise LedgerError("this rescue has no owner probe; it is fixed at checkout")
    spec = ProbeSpec(**rec.probe_spec)
    if spec.spec_hash() != rec.probe_spec_hash:
        raise LedgerError("the stored probe spec does not match its hash")
    return spec


def _result_data(result: ProbeResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "ssh_ok": result.ssh_ok,
        "http_ok": result.http_ok,
        "detail": result.detail,
        "at": result.at,
    }


class Orchestrator:
    """Runs rescues for one ledger and one PayPal gateway. Safe to share between threads: each
    rescue runs on the caller's thread, and ``stop`` may come from any other thread."""

    def __init__(
        self,
        ledger: Ledger,
        gateway: PaymentGateway,
        *,
        payments: Payments | None = None,
        clock: Clock | None = None,
        config: RescueConfig | None = None,
        on_event: RescueEventSink | None = None,
        firewall_factory: Callable[[], Firewall] = KeystrokeFirewall,
    ) -> None:
        self.ledger = ledger
        self.gateway = gateway
        self.payments = payments or Payments(ledger, gateway)
        self.clock: Clock = clock or ledger.clock or SystemClock()
        self.config = config or RescueConfig()
        self.on_event = on_event
        self.firewall_factory = firewall_factory
        self._sessions: dict[str, _Session] = {}
        self._reserved: set[str] = set()
        self._warranties: set[str] = set()  # rescues whose warranty runs in this process
        # One lock per rescue around money decisions (Stop vs capture). Re-entrant, so a status
        # sink that calls stop() from inside an event does not deadlock.
        self._money_locks: dict[str, threading.RLock] = {}
        self._lock = threading.Lock()
        self._shutdown = threading.Event()

    # -- process-level -----------------------------------------------------------------------

    def live_rescues(self) -> list[str]:
        """Rescues this process is driving or about to drive (for ``Reconciler(live_rescues=...)``)."""
        with self._lock:
            return sorted(set(self._sessions) | self._reserved)

    def shutdown(self) -> None:
        """Stop probe and warranty waits. Running agents end at their next action; holds whose
        agent did not finish are voided."""
        self._shutdown.set()
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            session.stop_reason = session.stop_reason or "Keyhold is shutting down"
            session.stop.set()
            session.lease.revoke("Keyhold is shutting down", wait=False)

    # -- before the hold -----------------------------------------------------------------------

    def free_look(
        self,
        console: Console,
        model: ModelClient | None,
        *,
        probe: ProbeSpec | None = None,
        screen_text: Callable[[], str] | None = None,
        rescue_id: str | None = None,
        owner_note: str | None = None,
    ) -> tuple[FreeLook, RescueRecord | None]:
        """Screenshots only, no keys. Stores a rescue in QUOTED when the look gives a quote."""
        events: list[AgentEvent] = []
        owner_note = clean_owner_note(owner_note)
        look = free_look(console, model, screen_text=screen_text, on_event=events.append, clock=self.clock,
                         owner_note=owner_note)
        rec = None
        if look.quote is not None and verify_quote(look.quote):
            rec = self.ledger.create_rescue(look.quote, rescue_id=rescue_id, probe=probe)
        rid = rec.id if rec is not None else ""
        for event in events:
            self._emit(rid, event)
        if rec is not None:
            self.ledger.log(
                rec.id,
                "free_look",
                look.reason,
                {"source": look.source, "model": look.model, "notes": list(look.notes),
                 "screenshot_sha256": look.screenshot_sha256, "owner_note": owner_note},
            )
        return look, rec

    def open_quote(self, quote: Quote, *, probe: ProbeSpec | None = None, rescue_id: str | None = None) -> RescueRecord:
        """Store a quote made elsewhere. It must pass the same checks as a free-look quote."""
        if not verify_quote(quote):
            raise LedgerError("the quote's tier or hash does not match the class table")
        return self.ledger.create_rescue(quote, rescue_id=rescue_id, probe=probe)

    def fix_probe(self, rescue_id: str, spec: ProbeSpec) -> RescueRecord:
        """Fix the owner probe at checkout. Its hash becomes the order's custom_id."""
        return self.ledger.set_probe(rescue_id, spec)

    def place_hold(self, rescue_id: str) -> Order:
        """Create the PayPal order (intent AUTHORIZE) for the quote. QUOTED -> HOLD_PENDING."""
        order = self._with_retries(lambda: self.payments.create_hold(rescue_id))
        self._emit_money(rescue_id, "order", f"order {order.id} created for ${money(order.amount)}", order_id=order.id)
        return order

    def confirm_hold(self, rescue_id: str) -> RescueRecord:
        """Authorize the order the owner approved. HOLD_PENDING -> HELD (or VOIDED if PayPal's
        authorization is not a clean hold of the quote)."""
        with self._lock:
            self._reserved.add(rescue_id)
        try:
            auth: Authorization = self._with_retries(lambda: self.payments.authorize(rescue_id))
        except BaseException:
            with self._lock:
                self._reserved.discard(rescue_id)
            raise
        rec = self.ledger.get(rescue_id)
        if rec.state is not RescueState.HELD:
            with self._lock:
                self._reserved.discard(rescue_id)
        self._emit_money(
            rescue_id,
            "hold",
            f"held ${money(auth.amount)} (authorization {auth.id} {auth.status})",
            authorization_id=auth.id,
            status=auth.status,
        )
        return rec

    # -- the rescue ----------------------------------------------------------------------------

    def run(
        self,
        rescue_id: str,
        console: Console,
        model: ModelClient,
        *,
        ssh: SshTransport,
        http: HttpTransport | None = None,
        console_password: str | None = None,
        goal: str | None = None,
        capture_amount: Decimal | str | None = None,
        server_ssh_port: int | None = None,
    ) -> RescueReport:
        """Lend the keyboard under the hold, then capture or void, then the warranty.

        ``ssh`` and ``http`` are the probe transports; the probe spec itself comes from the ledger.
        ``capture_amount`` (default: the quote) may undercut the quote, never exceed it.
        """
        rec = self.ledger.get(rescue_id)
        if rec.state is RescueState.VOIDED:
            return self._report(rescue_id, rec.end_reason or "the hold was released before the rescue started")
        if rec.state is not RescueState.HELD:
            raise IllegalTransition(f"run needs a HELD rescue, not {rec.state.value}")
        quote = _quote_from_record(rec)
        spec = _spec_from_record(rec)
        amount = rec.quote_amount if capture_amount is None else money(capture_amount)
        if amount <= 0 or amount > rec.quote_amount:
            raise ValueError(f"capture amount ${amount} must be above zero and at most the quote ${rec.quote_amount}")

        cfg = self.config
        lease = HoldLease(
            self.ledger,
            self.gateway,
            rescue_id,
            clock=self.clock,
            check_every=cfg.lease_check_every,
            ttl_s=cfg.lease_ttl_s,
            deadline=self.clock.now() + cfg.rescue_timeout_s,
        )
        session = _Session(lease)
        with self._lock:
            if rescue_id in self._sessions:
                raise LedgerError("this rescue is already running")
            self._sessions[rescue_id] = session
            self._reserved.discard(rescue_id)
        try:
            return self._run(rescue_id, session, quote, spec, amount, console, model, ssh, http,
                             console_password, goal, server_ssh_port)
        finally:
            # Only the keyboard is touched here; money never moves in a finally block.
            lease.revoke("the rescue session ended", wait=False)
            with self._lock:
                self._sessions.pop(rescue_id, None)

    def _run(
        self,
        rid: str,
        session: _Session,
        quote: Quote,
        spec: ProbeSpec,
        amount: Decimal,
        console: Console,
        model: ModelClient,
        ssh: SshTransport,
        http: HttpTransport | None,
        console_password: str | None,
        goal: str | None,
        server_ssh_port: int | None = None,
    ) -> RescueReport:
        cfg = self.config
        lease = session.lease
        secrets = [console_password] if console_password else []

        if not verify_quote(quote):
            self._void(rid, "the stored quote failed its check against the class table")
            return self._report(rid, "the stored quote failed its check", lease=lease)

        # 1. The grant: PayPal must show a live hold of at least the quote for this rescue.
        with self._money_lock(rid):
            try:
                if session.stop.is_set():
                    raise HoldRevoked(session.stop_reason or OWNER_STOP)
                auth = lease.check()
                self.ledger.transition(rid, RescueState.ACTIVE, reason="keyboard lent to the agent")
            except (HoldRevoked, LedgerError) as exc:
                reason = (
                    (session.stop_reason or OWNER_STOP)
                    if session.stop.is_set()
                    else f"the hold could not be confirmed: {exc}"
                )
                lease.revoke(reason, wait=False)
                self._void_locked(rid, reason)
                return self._report(rid, reason, lease=lease)
        self._emit_note(rid, "gate", f"keyboard lent: PayPal shows authorization {auth.id} CREATED for ${money(auth.amount)}")

        # 2. The agent, on the gated console.
        gated = GatedConsole(console, lease)
        result = run_agent(
            gated,
            model,
            goal=goal or default_goal(spec, console_password, cfg.console_user, server_ssh_port),
            quote=quote,
            max_turns=cfg.max_turns,
            firewall=self.firewall_factory(),
            on_event=self._agent_sink(rid, secrets),
            lease=lease,
            clock=self.clock,
            settle=cfg.settle,
            secrets=secrets,
        )
        gated.close()
        lease.revoke(f"the agent session ended ({result.outcome.value})")
        self.ledger.log(
            rid,
            "agent_result",
            redact(f"{result.outcome.value}: {result.reason}", secrets),
            {"outcome": result.outcome.value, "turns": result.turns, "inputs": gated.inputs_sent},
        )

        if session.stop.is_set():
            reason = session.stop_reason or OWNER_STOP
            self._void(rid, reason)
            return self._report(rid, reason, agent=result, lease=lease)
        if result.outcome is not AgentOutcome.DONE:
            reason = self._void_reason(result, secrets)
            self._void(rid, reason)
            return self._report(rid, reason, agent=result, lease=lease)

        # 3. The owner probe decides.
        with self._money_lock(rid):
            if session.stop.is_set():
                reason = session.stop_reason or OWNER_STOP
                self._void_locked(rid, reason)
                return self._report(rid, reason, agent=result, lease=lease)
            try:
                self.ledger.transition(rid, RescueState.PROBING, reason="the agent called finished; the owner probe decides")
            except LedgerError as exc:
                return self._report(rid, f"could not start the probe: {exc}", agent=result, lease=lease)
        policy = cfg.probe_policy
        self._emit_note(rid, "probe", f"owner probe started: {policy.label}")
        prober = Prober(spec, ssh=ssh, http=http, clock=self.clock)
        stable = wait_for_stable_pass(
            prober,
            rule=policy.stable,
            clock=self.clock,
            interval_s=policy.interval_s,
            deadline_s=policy.deadline_s,
            should_stop=lambda: session.stop.is_set() or self._shutdown.is_set(),
            on_result=lambda r: self._log_probe(rid, "probe", r),
        )

        # 4. Capture or void. Stop and capture take the same lock: exactly one of them wins.
        with self._money_lock(rid):
            if not stable.stable or session.stop.is_set():
                if session.stop.is_set():
                    reason = session.stop_reason or OWNER_STOP
                elif self._shutdown.is_set():
                    reason = "Keyhold shut down before the owner probe finished"
                else:
                    reason = f"the owner probe never passed: {stable.detail}"
                self._void_locked(rid, reason)
                return self._report(rid, reason, agent=result, stable=stable, lease=lease)
            capture = self._capture_locked(rid, amount, stable)
        if capture is None:
            rec = self.ledger.get(rid)
            op = self.ledger.get_op(rid, MoneyOp.CAPTURE)
            if op is not None and op.status is OutboxStatus.PENDING:
                reason = "the capture is waiting for PayPal; the reconciler replays it with the same request id"
            else:
                reason = rec.end_reason or "the capture did not go through"
            return self._report(rid, reason, agent=result, stable=stable, lease=lease)

        # 5. Warranty.
        if capture.status in _NO_MONEY_CAPTURE:
            reason = f"PayPal answered the capture with {capture.status}; no money moved, so there is no warranty"
            self._release_leftover(rid)
            return self._report(rid, reason, agent=result, stable=stable, lease=lease)
        decision = None
        if cfg.warranty:
            decision = self._warranty(rid, spec, ssh, http)
        reason = f"captured ${money(capture.amount)} after a stable probe pass"
        if decision is not None:
            reason += f"; warranty {decision.verdict.value}: {decision.reason}"
        return self._report(rid, reason, agent=result, stable=stable, warranty=decision, lease=lease)

    # -- owner Stop ------------------------------------------------------------------------------

    def stop(self, rescue_id: str, reason: str = OWNER_STOP) -> StopResult:
        """The owner's Stop: cut the keyboard now, then void the hold.

        When this returns, no further input reaches the console. A Stop after the capture cannot
        void (the probe already passed); the warranty still protects the owner.
        """
        with self._lock:
            session = self._sessions.get(rescue_id)
            self._reserved.discard(rescue_id)
        if session is not None:
            session.stop_reason = session.stop_reason or reason
            session.stop.set()
            session.lease.revoke(reason)  # waits for an input already being sent
        self.ledger.log(rescue_id, "stop", reason, {"session": session is not None})
        voided = self._void(rescue_id, reason)
        rec = self.ledger.get(rescue_id)
        void_op = self.ledger.get_op(rescue_id, MoneyOp.VOID)
        if voided:
            detail = "The keyboard was taken back and the hold was released. Nothing was charged."
        elif rec.state in (RescueState.CAPTURED, RescueState.WARRANTY_OK, RescueState.REFUNDED):
            detail = (
                "The owner probe had already passed and the capture was made, so the hold cannot be "
                "voided. The warranty still applies."
            )
        elif rec.state in PRE_CAPTURE_STATES and void_op is not None and void_op.status is OutboxStatus.FAILED:
            detail = (
                f"The keyboard was taken back, but PayPal refused the void ({void_op.error}). The hold "
                "stays until PayPal releases it or it expires."
            )
        elif rec.state in PRE_CAPTURE_STATES and void_op is not None and void_op.status is OutboxStatus.PENDING:
            detail = (
                "The keyboard was taken back. PayPal has not confirmed the void yet; Keyhold keeps "
                f"sending it ({void_op.request_id}) until PayPal does."
            )
        elif rec.state in PRE_CAPTURE_STATES:
            detail = (
                "The keyboard was taken back. The void could not be sent yet; the reconciler sends it "
                "on its next pass."
            )
        else:
            detail = f"The rescue is {rec.state.value}."
        return StopResult(rescue_id, rec.state, voided, detail)

    # -- warranty --------------------------------------------------------------------------------

    def resume_warranty(
        self, rescue_id: str, *, ssh: SshTransport, http: HttpTransport | None = None
    ) -> WarrantyDecision | None:
        """Run (or pick up again after a restart) the warranty of a CAPTURED rescue. Returns
        None without probing when the rescue is not CAPTURED or its warranty already runs here."""
        rec = self.ledger.get(rescue_id)
        if rec.state is not RescueState.CAPTURED:
            return None
        return self._warranty(rescue_id, _spec_from_record(rec), ssh, http)

    def warranties_due(self) -> list[str]:
        """CAPTURED rescues whose warranty is not decided and not running in this process (for
        example a capture the reconciler completed, or one whose process restarted)."""
        with self._lock:
            running = set(self._warranties) | set(self._sessions)
        return [
            r.id
            for r in self.ledger.rescues([RescueState.CAPTURED])
            if r.id not in running and r.capture_status not in _NO_MONEY_CAPTURE and self.ledger.refund_due(r.id) is None
        ]

    def settle_capture(self, rescue_id: str) -> Capture | None:
        """Replay a capture still PENDING in the outbox (same request id), a few times. Returns
        the capture once PayPal confirms it; None while the answer is still missing."""
        op = self.ledger.get_op(rescue_id, MoneyOp.CAPTURE)
        if op is None or op.status is not OutboxStatus.PENDING:
            return None
        with self._money_lock(rescue_id):
            try:
                capture: Capture = self._with_retries(lambda: self.payments.replay(op))
            except (PayPalError, LedgerError) as exc:
                self.ledger.log(rescue_id, "capture_pending", f"capture still waiting: {exc}", {})
                return None
        self._emit_money(
            rescue_id, "capture", f"captured ${money(capture.amount)} (capture {capture.id} {capture.status})",
            capture_id=capture.id, amount=str(money(capture.amount)), status=capture.status,
        )
        return capture

    def _warranty(
        self, rid: str, spec: ProbeSpec, ssh: SshTransport, http: HttpTransport | None
    ) -> WarrantyDecision | None:
        with self._lock:
            if rid in self._warranties:
                return None
            self._warranties.add(rid)
        try:
            return self._warranty_locked(rid, spec, ssh, http)
        finally:
            with self._lock:
                self._warranties.discard(rid)

    def _warranty_locked(
        self, rid: str, spec: ProbeSpec, ssh: SshTransport, http: HttpTransport | None
    ) -> WarrantyDecision | None:
        rec = self.ledger.get(rid)
        if rec.state is not RescueState.CAPTURED:
            return None
        if rec.capture_status in _NO_MONEY_CAPTURE:
            return None
        due = self.ledger.refund_due(rid)
        if due is not None:
            # Decided earlier; the refund itself may still be waiting (the reconciler sends it).
            self._refund(rid, due)
            return None
        plan, started_at = self._warranty_start(rid)
        history = [
            ProbeResult(bool(d["ok"]), bool(d["ssh_ok"]), bool(d["http_ok"]), str(d["detail"]), float(d["at"]))
            for d in (e.data for e in self.ledger.events(rid, kind="warranty_check"))
        ]
        schedule = WarrantySchedule(plan, started_at, history)
        decision = schedule.decision
        if not decision.final:
            decision = run_warranty(
                schedule,
                Prober(spec, ssh=ssh, http=http, clock=self.clock),
                clock=self.clock,
                should_stop=self._shutdown.is_set,
                on_result=lambda r: self._log_probe(rid, "warranty_check", r),
            )
        if decision.verdict is WarrantyVerdict.REFUND:
            reason = f"warranty: {decision.reason}"
            self.ledger.mark_refund_due(rid, reason)  # stored before PayPal is asked
            self._refund(rid, reason)
        elif decision.verdict is WarrantyVerdict.OK:
            try:
                self.ledger.transition(rid, RescueState.WARRANTY_OK, reason=decision.reason, source="warranty")
            except LedgerError as exc:
                self.ledger.log(rid, "warranty_error", f"could not close the warranty: {exc}", {})
        return decision

    def _warranty_start(self, rid: str) -> tuple[WarrantyPlan, float]:
        started = self.ledger.events(rid, kind="warranty_started")
        if started:
            d = started[0].data
            plan = WarrantyPlan(
                interval_s=float(d["interval_s"]),
                checks=int(d["checks"]),
                failures_to_refund=int(d["failures_to_refund"]),
                confirm_last_failure=bool(d["confirm_last_failure"]),
                judge_mode=bool(d["judge_mode"]),
            )
            return plan, float(d["started_at"])
        plan = self.config.probe_policy.warranty
        # The clock starts when the rescue was captured, whoever completed the capture (this
        # process, the reconciler after a crash, or a webhook).
        captured = [
            e.at for e in self.ledger.events(rid, kind="state") if e.data.get("to") == RescueState.CAPTURED.value
        ]
        now = captured[0] if captured else self.clock.now()
        self.ledger.log(
            rid,
            "warranty_started",
            plan.label,
            {
                "started_at": now,
                "interval_s": plan.interval_s,
                "checks": plan.checks,
                "failures_to_refund": plan.failures_to_refund,
                "confirm_last_failure": plan.confirm_last_failure,
                "judge_mode": plan.judge_mode,
            },
        )
        self._emit_note(rid, "warranty", plan.label)
        return plan, now

    # -- money -----------------------------------------------------------------------------------

    def _money_lock(self, rid: str) -> threading.RLock:
        with self._lock:
            lock = self._money_locks.get(rid)
            if lock is None:
                lock = self._money_locks[rid] = threading.RLock()
            return lock

    def _with_retries(self, call: Callable[[], Any]) -> Any:
        """Call a Payments method, retrying retryable PayPal errors with the same request id.
        Waits at least PayPal's Retry-After (up to ``MAX_RETRY_AFTER_S``)."""
        attempt = 0
        while True:
            try:
                return call()
            except PayPalError as err:
                attempt += 1
                if not err.retryable or attempt >= self.config.paypal_attempts:
                    raise
                wait = self.config.paypal_retry_s * attempt
                if err.retry_after is not None:
                    wait = max(wait, min(err.retry_after, MAX_RETRY_AFTER_S))
                self.clock.sleep(wait)

    def _void(self, rid: str, reason: str) -> bool:
        with self._money_lock(rid):
            return self._void_locked(rid, reason)

    def _void_locked(self, rid: str, reason: str) -> bool:
        """Release the hold. True when the rescue ends VOIDED. Never raises for PayPal trouble:
        an unfinished void stays in the outbox for the reconciler."""
        rec = self.ledger.get(rid)
        if rec.state is RescueState.VOIDED:
            return True
        if rec.state not in PRE_CAPTURE_STATES:
            return False
        try:
            auth = self._with_retries(lambda: self.payments.void(rid, reason))
        except PayPalError as err:
            self.ledger.log(rid, "void_failed", f"void: PayPal {err.issue}", {"retryable": err.retryable})
        except (OpInFlight, OpFailed, IllegalTransition) as exc:
            self.ledger.log(rid, "void_failed", f"void: {exc}", {})
        else:
            if self.ledger.get(rid).state is RescueState.VOIDED:
                self._emit_money(
                    rid, "void", f"hold released: {reason}", authorization_id=getattr(auth, "id", None)
                )
        return self.ledger.get(rid).state is RescueState.VOIDED

    def _capture_locked(self, rid: str, amount: Decimal, stable: StableOutcome) -> Capture | None:
        """Capture after a stable pass. On a final refusal the authorization is read back first:
        a capture PayPal made anyway is recorded; otherwise the hold is voided."""
        self.ledger.log(rid, "probe_stable", stable.detail, {"passes": [p.at for p in stable.passes]})
        try:
            capture: Capture = self._with_retries(lambda: self.payments.capture(rid, amount))
        except PayPalError as err:
            op = self.ledger.get_op(rid, MoneyOp.CAPTURE)
            if op is not None and op.status is OutboxStatus.PENDING:
                self.ledger.log(rid, "capture_pending", f"capture: PayPal {err.issue}; left for the reconciler", {})
                return None
            self.ledger.log(rid, "capture_refused", f"capture refused: {err.issue}", {"issue": err.issue})
            return self._after_capture_refusal(rid, err)
        except (OpInFlight, OpFailed, IllegalTransition) as exc:
            self.ledger.log(rid, "capture_refused", f"capture: {exc}", {})
            return None
        self._emit_money(
            rid,
            "capture",
            f"captured ${money(capture.amount)} (capture {capture.id} {capture.status})",
            capture_id=capture.id,
            amount=str(money(capture.amount)),
            status=capture.status,
        )
        return capture

    def _after_capture_refusal(self, rid: str, err: PayPalError) -> Capture | None:
        rec = self.ledger.get(rid)
        try:
            auth = self.gateway.get_authorization(rec.authorization_id)  # type: ignore[arg-type]
        except PayPalError as read_err:
            # Without a read-back nothing proves the hold is unused: leave it to the reconciler,
            # which reads the authorization again before voiding.
            self.ledger.log(rid, "void_failed", f"authorization read-back failed ({read_err.issue}); left for the reconciler", {})
            return None
        try:
            self.ledger.record_authorization(rid, auth)
        except LedgerError as exc:
            self.ledger.log(rid, "conflict", f"capture refusal read-back: {exc}", {})
            return None
        if auth.status in ("CAPTURED", "PARTIALLY_CAPTURED"):
            try:
                found = self.payments.adopt_capture(rid)
            except (PayPalError, LedgerError) as exc:
                self.ledger.log(rid, "conflict", f"PayPal shows the authorization {auth.status}: {exc}", {})
                return None
            if found is not None:
                self._emit_money(rid, "capture", f"captured ${money(found.amount)} (capture {found.id} {found.status})",
                                 capture_id=found.id, amount=str(money(found.amount)), status=found.status)
            return found
        self._void_locked(rid, f"PayPal refused the capture ({err.issue}); the hold is released")
        return None

    def _release_leftover(self, rid: str) -> None:
        """After a declined capture, void the authorization if PayPal still holds it."""
        try:
            self._with_retries(lambda: self.payments.void(rid, "PayPal declined the capture; the hold is released"))
        except (PayPalError, LedgerError) as exc:
            self.ledger.log(rid, "void_failed", f"releasing the hold after a declined capture: {exc}", {})

    def _refund(self, rid: str, reason: str) -> Refund | None:
        with self._money_lock(rid):
            try:
                refund: Refund = self._with_retries(lambda: self.payments.refund(rid, reason))
            except RefundDeferred:
                return None  # logged by Payments; the reconciler sends it once the capture completes
            except PayPalError as err:
                self.ledger.log(rid, "refund_failed", f"refund: PayPal {err.issue}; the reconciler retries",
                                {"retryable": err.retryable})
                return None
            except LedgerError as exc:
                self.ledger.log(rid, "refund_failed", f"refund: {exc}", {})
                return None
        self._emit_money(
            rid,
            "refund",
            f"refunded ${money(refund.amount)} (refund {refund.id} {refund.status}): {reason}",
            refund_id=refund.id,
            amount=str(money(refund.amount)),
        )
        return refund

    @staticmethod
    def _void_reason(result: AgentResult, secrets: list[str]) -> str:
        detail = redact(result.reason, secrets)
        if result.outcome is AgentOutcome.GAVE_UP:
            return f"the agent gave up: {detail}"
        if result.outcome is AgentOutcome.TURN_LIMIT:
            return f"the agent ran out of turns ({detail})"
        if result.outcome is AgentOutcome.REVOKED:
            return f"the hold ended during the rescue: {detail}"
        return f"the agent stopped on an error: {detail}"

    # -- events ----------------------------------------------------------------------------------

    def _emit(self, rid: str, event: AgentEvent) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(rid, event)
        except Exception:  # a broken status page must not stop a rescue
            pass

    def _emit_note(self, rid: str, kind: str, detail: str, **data: Any) -> None:
        self._emit(rid, AgentEvent(kind=kind, detail=detail, turn=0, at=self.clock.now(), data=data))

    def _emit_money(self, rid: str, op: str, detail: str, **data: Any) -> None:
        self._emit_note(rid, "money", detail, op=op, **data)

    def _agent_sink(self, rid: str, secrets: list[str]) -> Callable[[AgentEvent], None]:
        def sink(event: AgentEvent) -> None:
            event = replace(event, detail=redact(event.detail, secrets), data=redact(dict(event.data), secrets))
            stored = {k: v for k, v in event.data.items() if not isinstance(v, (bytes, bytearray))}
            stored["turn"] = event.turn
            try:
                self.ledger.log(rid, f"agent_{event.kind}", event.detail[:2000], stored)
            except Exception:
                pass
            self._emit(rid, event)

        return sink

    def _log_probe(self, rid: str, kind: str, result: ProbeResult) -> None:
        data = _result_data(result)
        self.ledger.log(rid, kind, result.detail, data)
        data.pop("detail")
        self._emit_note(rid, "probe" if kind == "probe" else "warranty", result.detail, **data)

    def _report(
        self,
        rid: str,
        reason: str,
        *,
        agent: AgentResult | None = None,
        stable: StableOutcome | None = None,
        warranty: WarrantyDecision | None = None,
        lease: HoldLease | None = None,
    ) -> RescueReport:
        rec = self.ledger.get(rid)
        return RescueReport(
            rescue_id=rid,
            state=rec.state,
            reason=reason,
            agent=agent,
            stable=stable,
            warranty=warranty,
            capture_id=rec.capture_id,
            captured_amount=rec.captured_amount,
            refunded_amount=rec.refunded_amount,
            lease_checks=lease.checks if lease else 0,
            lease_actions=lease.actions if lease else 0,
        )
