"""Webhook intake for PAYMENT.AUTHORIZATION.CREATED/VOIDED, PAYMENT.CAPTURE.COMPLETED/PENDING/
DECLINED/DENIED/REVERSED/REFUNDED and PAYMENT.REFUND.PENDING/FAILED.

Store the raw body and headers, return 200, verify through POST
/v1/notifications/verify-webhook-signature, dedupe on the event id, and apply with state ranks
(``RescueState.rank``) so a late or replayed event never moves a rescue backwards.

Verification has three answers. SUCCESS applies the event; FAILURE marks the delivery
``unverified`` for good; and when verification could not run (PayPal down, no access token) the
delivery is left unprocessed and ``process_pending`` (the reconciler) tries again, up to
``MAX_VERIFY_ATTEMPTS`` times.

The synchronous API response is the first tick for each money operation; the verified webhook is
the second (``RescueRecord.*_webhook_at``).

Use from the web route::

    hooks = WebhookProcessor(ledger, gateway, webhook_id)
    delivery_id = hooks.receive(request.headers, raw_body)   # store, then answer 200 at once
    hooks.process(delivery_id)                                # verify + dedupe + apply (background)

``process_pending()`` picks up anything stored but not processed (the reconciler calls it).
Only a verified event can claim its event id, so a forged delivery cannot block the real one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from keyhold.pay.client import (
    is_expired,
    parse_authorization_json,
    parse_capture_json,
    parse_refund_json,
)
from keyhold.pay.ledger import (
    PRE_CAPTURE_STATES,
    InvariantViolation,
    Ledger,
    LedgerError,
    Payments,
    RescueRecord,
)
from keyhold.types import (
    NO_MONEY_CAPTURE,
    MoneyOp,
    OutboxStatus,
    PaymentGateway,
    PayPalError,
    Refund,
    RescueState,
    money,
)

__all__ = ["HANDLED_EVENTS", "WebhookOutcome", "WebhookProcessor"]

AUTH_CREATED = "PAYMENT.AUTHORIZATION.CREATED"
AUTH_VOIDED = "PAYMENT.AUTHORIZATION.VOIDED"
CAPTURE_COMPLETED = "PAYMENT.CAPTURE.COMPLETED"
CAPTURE_REFUNDED = "PAYMENT.CAPTURE.REFUNDED"
CAPTURE_DENIED = "PAYMENT.CAPTURE.DENIED"  # Payments v1 and marketplace name
CAPTURE_DECLINED = "PAYMENT.CAPTURE.DECLINED"  # Payments v2 name
CAPTURE_PENDING = "PAYMENT.CAPTURE.PENDING"
CAPTURE_REVERSED = "PAYMENT.CAPTURE.REVERSED"
REFUND_PENDING = "PAYMENT.REFUND.PENDING"
REFUND_FAILED = "PAYMENT.REFUND.FAILED"

REFUND_EVENTS = frozenset({CAPTURE_REFUNDED, REFUND_PENDING, REFUND_FAILED})
HANDLED_EVENTS: frozenset[str] = frozenset(
    {
        AUTH_CREATED,
        AUTH_VOIDED,
        CAPTURE_COMPLETED,
        CAPTURE_DENIED,
        CAPTURE_DECLINED,
        CAPTURE_PENDING,
        CAPTURE_REVERSED,
        *REFUND_EVENTS,
    }
)
MAX_VERIFY_ATTEMPTS = 20  # tries while verification cannot run, then the delivery is unverified

# Outcomes stored on the delivery row.
APPLIED = "applied"  # verified, first time, matched a rescue
DUPLICATE = "duplicate"  # verified, event id already applied
UNVERIFIED = "unverified"  # signature check did not answer SUCCESS; never applied
IGNORED = "ignored"  # verified, event type Keyhold does not handle
MALFORMED = "malformed"  # body is not a PayPal event
UNMATCHED = "unmatched"  # verified, but no rescue owns these PayPal ids
CONFLICT = "conflict"  # verified, but applying it would break a money invariant; logged, not applied
PENDING_VERIFY = "pending"  # not stored: verification could not run, the delivery stays unprocessed

ORPHAN = "orphan hold"  # detail of an AUTHORIZATION.CREATED that arrived after the rescue ended


@dataclass(frozen=True)
class WebhookOutcome:
    delivery_id: int
    outcome: str
    event_id: str | None = None
    event_type: str | None = None
    rescue_id: str | None = None
    detail: str = ""


class _VerifyLater(Exception):
    """Verification could not run; leave the delivery for a later pass."""


class WebhookProcessor:
    def __init__(
        self, ledger: Ledger, gateway: PaymentGateway, webhook_id: str, *, payments: Payments | None = None
    ) -> None:
        self.ledger = ledger
        self.gateway = gateway
        self.webhook_id = webhook_id
        self.payments = payments or Payments(ledger, gateway)

    # -- intake ---------------------------------------------------------------------------------

    def receive(self, headers: Mapping[str, str], raw: bytes) -> int:
        """Store one delivery as received. Fast, never verifies, never raises on bad input."""
        return self.ledger.store_delivery(headers, raw)

    def handle(self, headers: Mapping[str, str], raw: bytes) -> WebhookOutcome:
        """Store, then verify, dedupe and apply in one go."""
        return self.process(self.receive(headers, raw))

    def process_pending(self) -> list[WebhookOutcome]:
        return [self.process(d.id) for d in self.ledger.deliveries(unprocessed=True, raw=False)]

    def process(self, delivery_id: int) -> WebhookOutcome:
        delivery = self.ledger.delivery(delivery_id)
        if delivery.outcome is not None:
            return WebhookOutcome(
                delivery_id,
                delivery.outcome,
                delivery.event_id,
                delivery.event_type,
                delivery.rescue_id,
                "already processed",
            )
        try:
            event = json.loads(delivery.raw)
        except (TypeError, ValueError):
            event = None
        if not isinstance(event, dict) or not event.get("id") or not event.get("event_type"):
            outcome = self._finish(delivery_id, MALFORMED, detail="body is not a PayPal webhook event")
            self.ledger.prune_delivery(delivery_id)  # unverifiable junk is not kept
            return outcome
        event_id, event_type = str(event["id"]), str(event["event_type"])

        try:
            verified = bool(self.webhook_id) and self._verify(delivery.headers, delivery.raw)
        except _VerifyLater as exc:
            attempts = self.ledger.note_verify_attempt(delivery_id)
            if attempts < MAX_VERIFY_ATTEMPTS:
                self.ledger.log(
                    None,
                    "webhook_verify_later",
                    f"{event_type} {event_id}: verification could not run ({exc}); it is tried again",
                    {"event_id": event_id, "event_type": event_type, "delivery_id": delivery_id},
                )
                return WebhookOutcome(delivery_id, PENDING_VERIFY, event_id, event_type, None, str(exc))
            verified = False
        if not verified:
            self.ledger.log(
                None,
                "webhook_unverified",
                f"{event_type} {event_id} failed signature verification",
                {"event_id": event_id, "event_type": event_type, "delivery_id": delivery_id},
            )
            outcome = self._finish(
                delivery_id,
                UNVERIFIED,
                event_id,
                event_type,
                verified=False,
                detail="signature verification did not answer SUCCESS",
            )
            self.ledger.prune_delivery(delivery_id)  # an unverified body is never needed again
            return outcome

        if event_type not in HANDLED_EVENTS:
            # Not claimed: the event id stays free, so a later version that handles this type
            # can still apply PayPal's redelivery of it.
            return self._finish(
                delivery_id, IGNORED, event_id, event_type, verified=True, detail="event type not handled"
            )

        resource = event.get("resource")
        if not isinstance(resource, dict) or not resource.get("id"):
            return self._finish(
                delivery_id, MALFORMED, event_id, event_type, verified=True, detail="event has no resource id"
            )

        with self.ledger.transaction():
            if not self.ledger.claim_event(event_id, delivery_id):
                return self._finish(
                    delivery_id, DUPLICATE, event_id, event_type, verified=True, detail="event id already applied"
                )
            try:
                with self.ledger.transaction():
                    outcome, rescue_id, detail = self._apply(event_type, resource)
            except (InvariantViolation, LedgerError) as exc:
                rec = self._owner(event_type, resource)
                rescue_id = rec.id if rec else None
                self.ledger.log(
                    rescue_id,
                    "conflict",
                    f"{event_type} {event_id}: {exc}",
                    {"event_id": event_id, "event_type": event_type},
                )
                outcome, detail = CONFLICT, str(exc)
            if outcome == UNMATCHED:
                self.ledger.log(
                    None,
                    "webhook_unmatched",
                    f"{event_type} {event_id} matched no rescue",
                    {"event_id": event_id, "resource_id": str(resource.get("id"))},
                )
            result = self._finish(delivery_id, outcome, event_id, event_type, rescue_id, verified=True, detail=detail)
        if detail == ORPHAN and rescue_id is not None:
            self._release_orphan(rescue_id)  # outside the transaction: this calls PayPal
        return result

    def _release_orphan(self, rescue_id: str) -> None:
        try:
            self.payments.void(rescue_id, "PayPal created an authorization after the rescue ended; it is released")
        except (PayPalError, LedgerError) as exc:
            # Left for the reconciler, which voids every orphan hold on each pass.
            self.ledger.log(rescue_id, "void_failed", f"releasing the late authorization: {exc}", {})

    def _verify(self, headers: Mapping[str, str], raw: bytes) -> bool:
        try:
            return bool(self.gateway.verify_webhook(headers, raw, self.webhook_id))
        except PayPalError as err:
            if err.retryable:
                raise _VerifyLater(err.issue) from err
            return False
        except Exception:  # a verifier that blows up has not verified anything
            return False

    def _finish(
        self,
        delivery_id: int,
        outcome: str,
        event_id: str | None = None,
        event_type: str | None = None,
        rescue_id: str | None = None,
        *,
        verified: bool | None = None,
        detail: str = "",
    ) -> WebhookOutcome:
        self.ledger.mark_delivery(
            delivery_id,
            outcome=outcome,
            verified=verified,
            event_id=event_id,
            event_type=event_type,
            rescue_id=rescue_id,
        )
        return WebhookOutcome(delivery_id, outcome, event_id, event_type, rescue_id, detail)

    # -- applying events ---------------------------------------------------------------------------

    def _owner(self, event_type: str, resource: Mapping[str, Any]) -> RescueRecord | None:
        try:
            if event_type in (AUTH_CREATED, AUTH_VOIDED):
                auth = parse_authorization_json(resource)
                return self.ledger.find(authorization_id=auth.id, order_id=auth.order_id, invoice_id=auth.invoice_id)
            if event_type in REFUND_EVENTS:
                refund = parse_refund_json(resource)
                return self.ledger.find(refund_id=refund.id, capture_id=refund.capture_id)
            capture = parse_capture_json(resource)
            return self.ledger.find(capture_id=capture.id, authorization_id=capture.authorization_id)
        except (KeyError, TypeError, ValueError):
            return None

    def _apply(self, event_type: str, resource: Mapping[str, Any]) -> tuple[str, str | None, str]:
        rec = self._owner(event_type, resource)
        if rec is None:
            return UNMATCHED, None, "no rescue owns these PayPal ids"
        if event_type == AUTH_CREATED:
            return self._auth_created(rec, resource)
        if event_type == AUTH_VOIDED:
            return self._auth_voided(rec, resource)
        if event_type in REFUND_EVENTS:
            return self._refunded(rec, event_type, resource)
        return self._capture_event(rec, event_type, resource)

    def _auth_created(self, rec: RescueRecord, resource: Mapping[str, Any]) -> tuple[str, str, str]:
        auth = parse_authorization_json(resource)
        ended = rec.state is RescueState.VOIDED and rec.authorization_id is None
        if ended:
            # The rescue ended before Keyhold recorded an authorization (for example authorize
            # answered with an error after PayPal made the hold). PayPal holds the money now, so
            # record the authorization and release it.
            self.ledger.log(
                rec.id, "late_event", "authorization created after the rescue ended", {"authorization_id": auth.id}
            )
        rec = self.ledger.record_authorization(rec.id, auth)
        if ended and rec.auth_status in ("CREATED", "PENDING") and not is_expired(
            rec.auth_expiration_time, self.ledger.clock.now()
        ):
            self.ledger.mark_webhook_tick(rec.id, "auth")
            return APPLIED, rec.id, ORPHAN
        moved = False
        if (
            rec.state is RescueState.HOLD_PENDING
            and rec.auth_status == "CREATED"
            and rec.authorized_amount == rec.quote_amount
            and not is_expired(rec.auth_expiration_time, self.ledger.clock.now())
        ):
            moved = self.ledger.advance(rec.id, RescueState.HELD, source="webhook")
        self.ledger.mark_webhook_tick(rec.id, "auth")
        return APPLIED, rec.id, "held" if moved else "confirmed"

    def _auth_voided(self, rec: RescueRecord, resource: Mapping[str, Any]) -> tuple[str, str, str]:
        auth = parse_authorization_json(resource)
        if rec.authorization_id is None:
            rec = self.ledger.record_authorization(rec.id, auth)
        if rec.state in (RescueState.CAPTURED, RescueState.WARRANTY_OK, RescueState.REFUNDED):
            raise InvariantViolation("PayPal reports a void for a captured rescue")
        self.ledger.record_auth_status(rec.id, "VOIDED")
        moved = False
        if rec.state in PRE_CAPTURE_STATES:
            capture_op = self.ledger.get_op(rec.id, MoneyOp.CAPTURE)
            if capture_op is not None and capture_op.status is not OutboxStatus.FAILED:
                # PayPal cannot void a captured authorization, but the ledger waits for the
                # reconciler to settle the pending capture rather than guess.
                self.ledger.log(rec.id, "late_event", "void reported while a capture is pending", {})
            else:
                moved = self.ledger.advance(
                    rec.id, RescueState.VOIDED, reason="authorization voided at PayPal", source="webhook"
                )
        self.ledger.mark_webhook_tick(rec.id, "void")
        return APPLIED, rec.id, "voided" if moved else "confirmed"

    def _capture_event(self, rec: RescueRecord, event_type: str, resource: Mapping[str, Any]) -> tuple[str, str, str]:
        capture = parse_capture_json(resource)
        status = {
            CAPTURE_DENIED: "DECLINED",  # the v2 capture status for a denied capture
            CAPTURE_DECLINED: "DECLINED",
            CAPTURE_PENDING: "PENDING",
            CAPTURE_REVERSED: "REVERSED",
        }.get(event_type, capture.status or "COMPLETED")
        if rec.capture_id is None:
            # PayPal captured and Keyhold missed the response (crash window): record PayPal's truth.
            capture = replace(capture, status=status)
            rec = self.ledger.record_capture(rec.id, capture)
            if status in NO_MONEY_CAPTURE:
                self._end_declined(rec, capture.id)
            else:
                self.ledger.advance(rec.id, RescueState.CAPTURED, reason="capture reported by PayPal", source="webhook")
        elif rec.capture_id != capture.id:
            raise InvariantViolation(f"second capture {capture.id}; rescue already captured {rec.capture_id}")
        self.ledger.record_capture_status(rec.id, status)
        rec = self.ledger.get(rec.id)
        if status in NO_MONEY_CAPTURE:
            self.ledger.log(rec.id, "capture_denied", f"PayPal declined capture {capture.id}; no money moved",
                            {"capture_id": capture.id})
            self._end_declined(rec, capture.id)
        if event_type == CAPTURE_REVERSED:
            self.ledger.attention(rec.id, f"PayPal reversed capture {capture.id}: the payment went back to the payer",
                                  {"capture_id": capture.id})
        if event_type != CAPTURE_PENDING:
            self.ledger.mark_webhook_tick(rec.id, "capture")
        return APPLIED, rec.id, status.lower()

    def _end_declined(self, rec: RescueRecord, capture_id: str) -> None:
        rec = self.ledger.get(rec.id)
        if rec.state in (RescueState.CAPTURED, *PRE_CAPTURE_STATES) and rec.capture_status in NO_MONEY_CAPTURE:
            self.ledger.transition(
                rec.id,
                RescueState.VOIDED,
                reason=f"PayPal declined capture {capture_id}; nothing was charged",
                source="webhook",
            )

    def _refunded(self, rec: RescueRecord, event_type: str, resource: Mapping[str, Any]) -> tuple[str, str, str]:
        refund = parse_refund_json(resource)
        status = {REFUND_PENDING: "PENDING", REFUND_FAILED: "FAILED"}.get(event_type, refund.status or "COMPLETED")
        refund = Refund(refund.id, status, refund.amount, refund.capture_id)
        known = rec.refunded_amount or money("0")
        if rec.refund_id == refund.id:
            # A status change of a refund already recorded: PENDING to COMPLETED, or FAILED.
            was_live = rec.refund_status in ("COMPLETED", "PENDING")
            now_live = status in ("COMPLETED", "PENDING")
            total = known - money(refund.amount) if was_live and not now_live else known
        else:
            # Another refund of the capture. Totals from webhooks are a best effort; the
            # reconciler recounts them from PayPal's list of refunds.
            total = known + money(refund.amount) if status in ("COMPLETED", "PENDING") else known
            if Ledger.fully_refunded(rec) or (rec.captured_amount is not None and total > rec.captured_amount):
                # Already counted (it arrived after the refund that completed the total): the
                # event is the second tick only.
                if event_type == CAPTURE_REFUNDED:
                    self.ledger.mark_webhook_tick(rec.id, "refund")
                return APPLIED, rec.id, "confirmed"
        rec = self.ledger.record_refund(rec.id, refund, total=total)
        moved = False
        if rec.state is RescueState.CAPTURED and Ledger.fully_refunded(rec):
            moved = self.ledger.advance(
                rec.id, RescueState.REFUNDED, reason="refund reported by PayPal", source="webhook"
            )
        elif status == "COMPLETED" and not Ledger.fully_refunded(rec):
            self.ledger.log(
                rec.id, "partial_refund",
                f"PayPal refunded ${refund.amount} of ${rec.captured_amount}; the rescue stays captured", {}
            )
        if event_type == CAPTURE_REFUNDED:
            self.ledger.mark_webhook_tick(rec.id, "refund")
        return APPLIED, rec.id, "refunded" if moved else status.lower()
