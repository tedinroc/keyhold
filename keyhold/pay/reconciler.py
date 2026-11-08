"""Reconciler: at startup and every 5 minutes, read every non-terminal rescue back from PayPal,
repair the ledger, and replay PENDING outbox operations with their original request id.

A crash between the outbox write and the PayPal response must end in exactly one capture: the
outbox row is still PENDING, so the reconciler replays the capture with the same
PayPal-Request-Id and PayPal answers with the capture it already made (or makes it now, once).

One pass (``run_once``):

1. Process stored webhook deliveries that were never processed (when a processor is attached).
2. Replay every PENDING outbox row with its own request id and arguments.
3. For every open rescue, read PayPal's view and move the ledger forward to match it:
   an authorization that is VOIDED, DENIED or expired ends the rescue as VOIDED; a capture or
   refund PayPal made that the ledger missed is recorded; a fresh authorization moves
   HOLD_PENDING to HELD.
4. Release holds nobody is using: at startup every HELD/ACTIVE/PROBING rescue is orphaned (its
   agent died with the old process) and is voided; later passes void such rescues only when
   ``live_rescues`` is given and the rescue is not in it. Orders never approved within
   ``approval_timeout`` (or that PayPal no longer knows) end as VOIDED.
5. Void holds PayPal still has for rescues that already ended VOIDED (``Ledger.orphan_holds``).
6. Send refunds the warranty decided (``Ledger.refund_due``) until PayPal confirms them: after a
   PENDING capture completes, after a refund PayPal answered PENDING turns FAILED, after a
   refusal that could change. A refund PayPal refuses for good is flagged for a person.

The reconciler never voids after a capture and never captures on its own: a capture only happens
by replaying a capture the orchestrator already decided on (after the probe passed).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from keyhold.pay.client import is_expired
from keyhold.pay.ledger import (
    InvariantViolation,
    Ledger,
    LedgerError,
    OpFailed,
    OpInFlight,
    Payments,
    RefundDeferred,
    RescueRecord,
)
from keyhold.types import NO_MONEY_CAPTURE, MoneyOp, OutboxStatus, PaymentGateway, PayPalError, RescueState

__all__ = ["DEFAULT_INTERVAL", "ReconcileReport", "Reconciler"]

DEFAULT_INTERVAL = 300.0  # seconds between passes
DEFAULT_APPROVAL_TIMEOUT = 3 * 3600.0  # an order the owner never approved

_LIVE_STATES = frozenset({RescueState.HELD, RescueState.ACTIVE, RescueState.PROBING})


@dataclass
class ReconcileReport:
    replayed: list[str] = field(default_factory=list)  # "rescue op: outcome"
    repaired: list[str] = field(default_factory=list)  # "rescue: what changed"
    voided: list[str] = field(default_factory=list)  # rescues whose hold was released this pass
    refunds: list[str] = field(default_factory=list)  # "rescue: outcome" for refunds sent or waiting
    conflicts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    webhooks: int = 0

    @property
    def clean(self) -> bool:
        return not self.conflicts and not self.errors


class Reconciler:
    def __init__(
        self,
        ledger: Ledger,
        gateway: PaymentGateway,
        *,
        payments: Payments | None = None,
        webhooks: object | None = None,
        interval: float = DEFAULT_INTERVAL,
        approval_timeout: float = DEFAULT_APPROVAL_TIMEOUT,
        live_rescues: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        """``webhooks`` is a ``keyhold.pay.webhooks.WebhookProcessor`` (optional).
        ``live_rescues`` returns the ids of rescues an agent is driving right now."""
        self.ledger = ledger
        self.gateway = gateway
        self.payments = payments or Payments(ledger, gateway)
        self.webhooks = webhooks
        self.interval = float(interval)
        self.approval_timeout = float(approval_timeout)
        self.live_rescues = live_rescues
        self._thread: threading.Thread | None = None

    # -- entry points ------------------------------------------------------------------------

    def startup(self) -> ReconcileReport:
        """The pass to run once before serving: everything in flight belonged to a dead process."""
        return self.run_once(orphans=True)

    def run_once(self, *, orphans: bool = False) -> ReconcileReport:
        report = ReconcileReport()
        self._process_webhooks(report)
        self._replay_outbox(report)
        live = None if self.live_rescues is None else set(self.live_rescues())
        for rec in self.ledger.open_rescues():
            self._guarded(rec.id, report, lambda rid=rec.id: self._repair(rid, report))
            self._guarded(rec.id, report, lambda rid=rec.id: self._release_unused(rid, report, orphans=orphans, live=live))
            self._guarded(rec.id, report, lambda rid=rec.id: self._send_due_refund(rid, report))
        for rec in self.ledger.orphan_holds():
            self._guarded(rec.id, report, lambda rid=rec.id: self._release_orphan(rid, report))
        if report.repaired or report.voided or report.replayed or report.conflicts or report.refunds:
            self.ledger.log(
                None,
                "reconcile",
                f"{len(report.replayed)} replayed, {len(report.repaired)} repaired, {len(report.voided)} voided,"
                f" {len(report.conflicts)} conflicts",
                {
                    "replayed": report.replayed,
                    "repaired": report.repaired,
                    "voided": report.voided,
                    "refunds": report.refunds,
                    "conflicts": report.conflicts,
                    "errors": report.errors,
                },
            )
        return report

    def run_forever(self, stop: threading.Event) -> None:
        """Startup pass, then a pass every ``interval`` seconds until ``stop`` is set."""
        self._safe(lambda: self.startup())
        while not stop.wait(self.interval):
            self._safe(lambda: self.run_once())

    def start(self) -> threading.Event:
        """Run ``run_forever`` on a daemon thread; set the returned event to stop it."""
        stop = threading.Event()
        self._thread = threading.Thread(target=self.run_forever, args=(stop,), name="keyhold-reconciler", daemon=True)
        self._thread.start()
        return stop

    def _guarded(self, rescue_id: str, report: ReconcileReport, fn: Callable[[], object]) -> None:
        try:
            fn()
        except PayPalError as err:
            report.errors.append(f"{rescue_id}: PayPal {err.issue}")
        except InvariantViolation as exc:
            report.conflicts.append(f"{rescue_id}: {exc}")
            self.ledger.log(rescue_id, "conflict", f"reconciler: {exc}", {})
        except LedgerError as exc:
            report.errors.append(f"{rescue_id}: {exc}")

    def _safe(self, fn: Callable[[], object]) -> None:
        try:
            fn()
        except Exception as exc:  # the loop must survive one bad pass
            self.ledger.log(None, "reconcile_error", type(exc).__name__, {"error": str(exc)[:500]})

    # -- steps ---------------------------------------------------------------------------------

    def _process_webhooks(self, report: ReconcileReport) -> None:
        if self.webhooks is None:
            return
        outcomes = self.webhooks.process_pending()  # type: ignore[attr-defined]
        report.webhooks = len(outcomes)

    def _replay_outbox(self, report: ReconcileReport) -> None:
        for entry in self.ledger.pending_ops():
            label = f"{entry.rescue_id} {entry.op.value}"
            try:
                self.payments.replay(entry)
                after = self.ledger.get_op_by_id(entry.id)
                report.replayed.append(f"{label}: {after.status.value}")
            except PayPalError as err:
                after = self.ledger.get_op_by_id(entry.id)
                report.replayed.append(f"{label}: {after.status.value} ({err.issue})")
                if after.status is OutboxStatus.PENDING:
                    report.errors.append(f"{label}: PayPal {err.issue}, will retry ({after.request_id})")
            except RefundDeferred as exc:
                report.refunds.append(f"{entry.rescue_id}: waiting ({exc})")
            except InvariantViolation as exc:
                report.conflicts.append(f"{label}: {exc}")
                self.ledger.log(entry.rescue_id, "conflict", f"replay {entry.op.value}: {exc}", {})
            except LedgerError as exc:
                report.errors.append(f"{label}: {exc}")

    def _has_pending(self, rescue_id: str) -> bool:
        return any(e.status is OutboxStatus.PENDING for e in self.ledger.ops(rescue_id))

    def _repair(self, rescue_id: str, report: ReconcileReport) -> None:
        rec = self.ledger.get(rescue_id)
        if rec.state.terminal or self._has_pending(rescue_id):
            return  # a PENDING op is settled by replay, not by guessing
        now = self.ledger.clock.now()

        if rec.state is RescueState.QUOTED:
            if now - rec.created_at > self.approval_timeout:
                self.ledger.transition(rec.id, RescueState.VOIDED, reason="quote was never taken", source="reconciler")
                report.repaired.append(f"{rec.id}: quote expired")
            return

        if rec.state is RescueState.HOLD_PENDING and rec.authorization_id is None:
            self._repair_unauthorized(rec, report, now)
            return

        if rec.authorization_id and rec.state in (
            RescueState.HOLD_PENDING,
            RescueState.HELD,
            RescueState.ACTIVE,
            RescueState.PROBING,
        ):
            self._repair_authorized(rec, report, now)
            return

        if rec.state is RescueState.CAPTURED and rec.capture_id:
            self._repair_captured(rec, report)

    def _repair_unauthorized(self, rec: RescueRecord, report: ReconcileReport, now: float) -> None:
        if rec.order_id is None:
            return
        try:
            order = self.gateway.get_order(rec.order_id)
        except PayPalError as err:
            gone = err.status == 404 or err.issue in ("RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID")
            auth_op = self.ledger.get_op(rec.id, MoneyOp.AUTHORIZE)
            started = auth_op is not None and auth_op.status is not OutboxStatus.FAILED
            if not gone or started:
                raise
            # PayPal drops an order nobody approved after about three hours. Nothing was ever
            # held, so no PayPal call is needed to end the rescue.
            self.ledger.advance(
                rec.id, RescueState.VOIDED, reason="the order expired at PayPal before it was approved",
                source="reconciler",
            )
            report.repaired.append(f"{rec.id}: order expired before approval")
            return
        if order.authorization_id:
            auth = self.gateway.get_authorization(order.authorization_id)
            self.ledger.record_authorization(rec.id, auth)
            report.repaired.append(f"{rec.id}: found authorization {auth.id} ({auth.status})")
            self._repair_authorized(self.ledger.get(rec.id), report, now)
            return
        if order.status == "VOIDED":
            self.ledger.advance(rec.id, RescueState.VOIDED, reason="order voided at PayPal", source="reconciler")
            report.repaired.append(f"{rec.id}: order voided")
            return
        auth_op = self.ledger.get_op(rec.id, MoneyOp.AUTHORIZE)
        if (auth_op is None or auth_op.status is OutboxStatus.FAILED) and now - rec.updated_at > self.approval_timeout:
            self.ledger.advance(
                rec.id, RescueState.VOIDED, reason="owner did not approve the hold in time", source="reconciler"
            )
            report.repaired.append(f"{rec.id}: approval timed out")

    def _repair_authorized(self, rec: RescueRecord, report: ReconcileReport, now: float) -> None:
        auth = self.gateway.get_authorization(rec.authorization_id)  # type: ignore[arg-type]
        self.ledger.record_authorization(rec.id, auth)
        status = auth.status
        if status in ("CAPTURED", "PARTIALLY_CAPTURED"):
            capture = self.payments.adopt_capture(rec.id)
            if capture is None:
                raise InvariantViolation("PayPal says captured but no capture was found")
            report.repaired.append(f"{rec.id}: recorded capture {capture.id} ({capture.status})")
            return
        if status in ("VOIDED", "DENIED"):
            if self.ledger.advance(
                rec.id, RescueState.VOIDED, reason=f"authorization {status} at PayPal", source="reconciler"
            ):
                report.repaired.append(f"{rec.id}: authorization {status}")
            return
        if is_expired(auth.expiration_time, now):
            if self.ledger.advance(rec.id, RescueState.VOIDED, reason="authorization expired", source="reconciler"):
                report.repaired.append(f"{rec.id}: authorization expired")
            return
        if rec.state is RescueState.HOLD_PENDING and status == "CREATED":
            fresh = self.ledger.get(rec.id)
            if fresh.authorized_amount == fresh.quote_amount:
                if self.ledger.advance(rec.id, RescueState.HELD, source="reconciler"):
                    report.repaired.append(f"{rec.id}: held")

    def _repair_captured(self, rec: RescueRecord, report: ReconcileReport) -> None:
        capture = self.gateway.get_capture(rec.capture_id)  # type: ignore[arg-type]
        if capture.status != rec.capture_status:
            self.ledger.record_capture_status(rec.id, capture.status)
            if self.ledger.get(rec.id).capture_status != rec.capture_status:
                report.repaired.append(f"{rec.id}: capture {capture.status}")
        rec = self.ledger.get(rec.id)
        if rec.capture_status in NO_MONEY_CAPTURE:
            # PayPal declined a capture that was PENDING: no money moved, so the rescue ends.
            self.ledger.transition(
                rec.id,
                RescueState.VOIDED,
                reason=f"PayPal declined the capture ({rec.capture_status}); nothing was charged",
                source="reconciler",
            )
            report.repaired.append(f"{rec.id}: capture {rec.capture_status}, nothing charged")
            return
        refund_open = rec.refund_id is not None and rec.refund_status != "COMPLETED"
        if refund_open or capture.status in ("REFUNDED", "PARTIALLY_REFUNDED"):
            before = self.ledger.get(rec.id)
            self.payments.adopt_refunds(rec.id)
            after = self.ledger.get(rec.id)
            if (after.refund_id, after.refund_status, after.refunded_amount) != (
                before.refund_id, before.refund_status, before.refunded_amount
            ):
                report.repaired.append(f"{rec.id}: refunds at PayPal ${after.refunded_amount} ({after.refund_status})")
            if after.state is RescueState.REFUNDED and before.state is not RescueState.REFUNDED:
                report.repaired.append(f"{rec.id}: refunded")

    def _send_due_refund(self, rescue_id: str, report: ReconcileReport) -> None:
        """Send (or send again) a refund the warranty decided, until PayPal confirms it."""
        rec = self.ledger.get(rescue_id)
        reason = self.ledger.refund_due(rescue_id)
        if reason is None or rec.state is not RescueState.CAPTURED or self._has_pending(rescue_id):
            return
        op = self.ledger.get_op(rescue_id, MoneyOp.REFUND)
        if op is not None and op.status is OutboxStatus.FAILED and op.error != "PENDING_CAPTURE":
            return  # PayPal refused it for good; flagged for a person when that happened
        if op is not None and op.status is OutboxStatus.DONE and rec.refund_status in ("COMPLETED", "PENDING"):
            return  # sent; _repair_captured reads it back until PayPal completes it
        try:
            refund = self.payments.refund(rescue_id, reason)
        except RefundDeferred as exc:
            report.refunds.append(f"{rescue_id}: waiting ({exc})")
            return
        except OpFailed:
            return
        report.refunds.append(f"{rescue_id}: refund {refund.id} {refund.status}")

    def _release_orphan(self, rescue_id: str, report: ReconcileReport) -> None:
        """Void an authorization PayPal still holds for a rescue that already ended."""
        try:
            auth = self.payments.void(rescue_id, "the rescue ended; the authorization PayPal still holds is released")
        except (OpInFlight, OpFailed):
            return
        if auth is not None and self.ledger.get(rescue_id).auth_status == "VOIDED":
            report.voided.append(rescue_id)

    def _release_unused(
        self,
        rescue_id: str,
        report: ReconcileReport,
        *,
        orphans: bool,
        live: set[str] | None,
    ) -> None:
        rec = self.ledger.get(rescue_id)
        if rec.state not in _LIVE_STATES or self._has_pending(rescue_id):
            return
        if orphans:
            reason = "Keyhold restarted and the agent session was lost; the hold is released"
        elif live is not None and rescue_id not in live:
            reason = "no agent is driving this rescue; the hold is released"
        else:
            return
        try:
            self.payments.void(rescue_id, reason)
        except OpInFlight:
            return
        except OpFailed as exc:
            # A void closed by an older version; Payments.void reopens it, so this is rare.
            report.errors.append(f"{rescue_id}: {exc}")
            return
        if self.ledger.get(rescue_id).state is RescueState.VOIDED:
            report.voided.append(rescue_id)
