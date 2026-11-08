"""Check, against the PayPal sandbox, the PayPal behaviour Keyhold depends on.

    uv run --env-file .env python tools/live_check.py
    uv run --env-file .env python tools/live_check.py --webhook-event var/keyhold.db
    uv run --env-file .env python tools/live_check.py --webhook-only --webhook-event delivery.json

Every call goes through Keyhold's own code. The two holds are created, authorized, captured and
voided by ``keyhold.pay.ledger.Payments`` (outbox rows and PayPal-Request-Ids included). The checks
that step outside the ledger on purpose (a replay, a second capture, a partial refund, the
negative tests) call ``keyhold.pay.client.PayPalClient`` directly. Nothing here builds an HTTP
request by hand.

Steps, in order:

1. ``sandbox_only``: the client points at the sandbox, and nothing in the environment asks for
   another PayPal environment.
2. ``client_token``: PayPal issues the browser client token the JS SDK v6 page uses.
3. Hold 1 ($12.00): ``hold1_order`` (order, intent AUTHORIZE, through the ledger),
   ``hold1_approved`` (prints the approval link and polls the order until APPROVED; a person
   approves it with a sandbox personal account), ``hold1_authorized`` (authorization CREATED
   for $12.00, ledger HELD), ``hold1_lease_grants`` (``HoldLease`` grants input, which needs GET
   authorization to return this rescue's order id, custom_id and invoice id).
4. ``capture_7_final``: capture $7.00 with ``final_capture: true`` through the ledger.
5. ``auth_after_final_capture``: GET the authorization and record its exact ``status`` and
   ``status_details``.
6. ``capture_replay_same_id``: the same capture again with the same PayPal-Request-Id returns the
   same capture, and PayPal lists one capture.
7. ``second_capture_refused``: a capture of the $5.00 that final_capture released, under a new
   request id, is refused.
8. ``void_after_capture_refused``: a void of the captured authorization is refused.
9. ``refund_2``: refund $2.00 of the capture (replayed once with the same request id); the ledger
   reads the partial refund back and the rescue stays CAPTURED.
10. Hold 2 ($12.00): ``hold2_order``, ``hold2_approved``, ``hold2_authorized``, then
    ``hold2_voided`` (void through the ledger, exact status and status_details, void replayed).
11. Negative tests with the ``PayPal-Mock-Response`` header (``PayPalClient.mock_next``): authorize,
    capture, void and refund each come back with the issue asked for, as a refusal. Each runs on a
    payment that can no longer move money and asks for an issue that call would not give on its
    own, so a mock header that PayPal ignores shows up as a FAIL, never as a charge.
    ``mocks_moved_no_money`` then reads both payments back.
12. ``ledger_invariants``: every money invariant holds for both rescues (after a hold left open
    by a failed step is voided).
13. ``webhook_verified`` (optional): with ``PAYPAL_WEBHOOK_ID`` and ``--webhook-event`` (a JSON file
    with ``headers`` and ``body`` or ``body_b64``, or a Keyhold ledger, from which the newest stored
    delivery is taken), PayPal verifies the delivery and rejects a copy with one byte added.

Each step prints PASS, FAIL or SKIP (SKIP: a step it needs did not pass). Results go to
``runs/live-check/<UTC time>/``: ``report.json``, ``summary.md`` and the ledger
(``ledger.sqlite3``). Secrets and tokens are never printed or written. Exit status: 0 when no step
failed, 1 when one did, 2 when the check refused to start, 130 when interrupted.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sqlite3
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from keyhold.pay.client import SANDBOX_BASE, PayPalClient, iso_utc, require_sandbox
from keyhold.pay.gate import HoldLease
from keyhold.pay.ledger import Ledger, Payments, UnknownRescue
from keyhold.types import (
    CURRENCY,
    Authorization,
    Capture,
    Clock,
    HoldRevoked,
    MoneyOp,
    Order,
    PaymentGateway,
    PayPalError,
    ProbeSpec,
    Quote,
    Refund,
    RescueState,
    SystemClock,
    Tier,
    canonical_json,
    money,
    sha256_hex,
)

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = PROJECT / "runs" / "live-check"

HOLD = money("12.00")
CAPTURE = money("7.00")
REFUND = money("2.00")
RELEASED = HOLD - CAPTURE  # what final_capture should have released
MOCK_CAPTURE = money("1.00")
MOCK_REFUND = money("6.00")  # more than is left after the $2.00 refund

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

# Variables that could ask for a PayPal environment other than the sandbox. Keyhold reads none of
# them, so any value but the sandbox means the person expects something this check will not do.
ENVIRONMENT_VARS = ("PAYPAL_ENVIRONMENT", "PAYPAL_ENV", "PAYPAL_MODE", "PAYPAL_BASE_URL", "PAYPAL_API_BASE")
SANDBOX_VALUES = frozenset({"", "sandbox", SANDBOX_BASE, SANDBOX_BASE + "/"})

# (step, operation, issue asked for). Authorize runs on order 1 (already authorized), capture and
# void on hold 2 (voided), refund on capture 1 for more than is left after the refund.
MOCKS = (
    ("mock_authorize_INSTRUMENT_DECLINED", "authorize", "INSTRUMENT_DECLINED"),
    ("mock_capture_AUTHORIZATION_EXPIRED", "capture", "AUTHORIZATION_EXPIRED"),
    ("mock_void_PREVIOUSLY_CAPTURED", "void", "PREVIOUSLY_CAPTURED"),
    ("mock_refund_CAPTURE_FULLY_REFUNDED", "refund", "CAPTURE_FULLY_REFUNDED"),
)

# Fields of GET authorization kept in the report (the payee's account details are left out).
AUTH_FIELDS = (
    "id",
    "status",
    "status_details",
    "amount",
    "expiration_time",
    "create_time",
    "update_time",
    "custom_id",
    "invoice_id",
)

Approver = Callable[[int, Order, "str | None"], None]
Say = Callable[[str], None]


# ---------------------------------------------------------------------------------------------
# Quote and probe spec for a check hold
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckQuote(Quote):
    """A quote for the fixed check amount instead of a tier price, so a check hold cannot be
    mistaken for a rescue. Its hash covers that amount, so the stored quote stays consistent."""

    check_amount: Decimal = HOLD

    @property
    def amount(self) -> Decimal:
        return self.check_amount

    @staticmethod
    def check_hash(fault_class: str, evidence: tuple[str, ...], tier: Tier, plan: str, amount: Decimal) -> str:
        return sha256_hex(
            canonical_json(
                {
                    "fault_class": fault_class,
                    "evidence": list(evidence),
                    "tier": Tier(tier).value,
                    "plan": plan,
                    "amount": str(money(amount)),
                    "currency": CURRENCY,
                }
            )
        )

    def hash_ok(self) -> bool:
        return self.quote_hash == self.check_hash(
            self.fault_class, self.evidence, self.tier, self.plan, self.check_amount
        )


def make_check_quote(hold_no: int) -> CheckQuote:
    evidence = (f"PayPal sandbox live check, hold {hold_no}",)
    plan = "No rescue: this hold exists only to check PayPal's answers."
    digest = CheckQuote.check_hash("live_check", evidence, Tier.T1, plan, HOLD)
    return CheckQuote("live_check", evidence, Tier.T1, plan, digest, HOLD)


def check_probe_spec(hold_no: int) -> ProbeSpec:
    """A probe spec for the order's custom_id. Never run: there is no server in this check."""
    return ProbeSpec(
        host=f"live-check-{hold_no}.test",
        ssh_port=22,
        ssh_user="probe",
        probe_public_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILiveCheckPlaceholderNotAKey live-check",
        url=f"http://live-check-{hold_no}.test/",
        expect_substring="ok",
    )


# ---------------------------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------------------------


class CheckFailed(Exception):
    def __init__(self, detail: str, data: Mapping[str, Any] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.data = dict(data or {})


@dataclass
class StepResult:
    name: str
    status: str
    detail: str
    data: dict[str, Any] = field(default_factory=dict)
    at: str = ""


@dataclass
class Hold:
    no: int
    rescue_id: str
    order: Order | None = None
    auth: Authorization | None = None
    capture: Capture | None = None
    refund: Refund | None = None


def jsonable(value: Any) -> Any:
    """``value`` with Decimals as strings and dataclasses as dicts, safe for json.dumps."""
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def error_data(err: PayPalError) -> dict[str, Any]:
    return {
        "issue": err.issue,
        "message": err.message,
        "http_status": err.status,
        "debug_id": err.debug_id,
        "refused": err.refused,
        "retryable": err.retryable,
    }


def auth_fields(body: Mapping[str, Any]) -> dict[str, Any]:
    kept = {key: body.get(key) for key in AUTH_FIELDS if key in body}
    supp = body.get("supplementary_data")
    related = supp.get("related_ids") if isinstance(supp, Mapping) else None
    if isinstance(related, Mapping):
        kept["related_ids"] = dict(related)
    return kept


def describe_details(details: Any) -> str:
    return "none" if not details else json.dumps(details, sort_keys=True)


# ---------------------------------------------------------------------------------------------
# Webhook input
# ---------------------------------------------------------------------------------------------


def load_webhook(path: Path, delivery_id: int | None) -> tuple[dict[str, str], bytes, dict[str, Any]]:
    """(headers, raw body, where it came from) for one received delivery.

    ``path`` is a JSON file ``{"headers": {...}, "body": "..."}`` (or ``"body_b64"``), or a Keyhold
    ledger: then the delivery ``delivery_id``, or the newest one that still has its body.
    """
    data = path.read_bytes()
    if data.startswith(b"SQLite format 3\x00"):
        con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            if delivery_id is None:
                row = con.execute(
                    "SELECT id, raw, headers_json FROM webhook_deliveries WHERE length(raw) > 0"
                    " ORDER BY id DESC LIMIT 1"
                ).fetchone()
            else:
                row = con.execute(
                    "SELECT id, raw, headers_json FROM webhook_deliveries WHERE id=? AND length(raw) > 0",
                    (delivery_id,),
                ).fetchone()
        finally:
            con.close()
        if row is None:
            raise CheckFailed("the ledger has no stored delivery with its body" + (
                f" under id {delivery_id}" if delivery_id is not None else ""))
        return dict(json.loads(row[2])), bytes(row[1]), {"source": "ledger", "delivery_id": int(row[0])}
    try:
        obj = json.loads(data)
        headers = {str(k): str(v) for k, v in dict(obj["headers"]).items()}
        raw = obj["body"].encode("utf-8") if "body" in obj else base64.b64decode(obj["body_b64"], validate=True)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise CheckFailed(
            'the webhook file must be JSON with "headers" and "body" (or "body_b64"), or a Keyhold ledger'
        ) from exc
    return headers, raw, {"source": "file"}


def tampered_copy(raw: bytes, event: Mapping[str, Any]) -> tuple[bytes, str]:
    """``raw`` with one byte added, and how.

    The byte goes inside the event id, so the copy says something different, not only with other
    spacing: the check must fail whether PayPal compares the bytes or re-reads the JSON. Only when
    the id is not found as written does the byte go after the first ``{``.
    """
    event_id = event.get("id")
    if isinstance(event_id, str) and event_id:
        written = json.dumps(event_id).encode("utf-8")
        if written in raw:
            changed = raw.replace(written, json.dumps(event_id + "0").encode("utf-8"), 1)
            return changed, "one character added to the event id"
    return raw.replace(b"{", b"{ ", 1), "one space added after the first {"


# ---------------------------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------------------------


class LiveCheck:
    def __init__(
        self,
        gateway: PaymentGateway,
        *,
        ledger: Ledger,
        approve: Approver,
        target: str,
        clock: Clock | None = None,
        run_tag: str | None = None,
        poll_interval: float = 5.0,
        approval_timeout: float = 900.0,
        webhook_path: Path | None = None,
        webhook_delivery_id: int | None = None,
        webhook_id: str = "",
        say: Say = print,
    ) -> None:
        self.gateway = gateway
        self.ledger = ledger
        self.payments = Payments(ledger, gateway)
        self.approve = approve
        self.target = target
        self.paypal = "PayPal" if target == "sandbox" else "FakePayPal"  # who answered, in step details
        self.clock: Clock = clock or ledger.clock
        self.poll_interval = float(poll_interval)
        self.approval_timeout = float(approval_timeout)
        self.webhook_path = webhook_path
        self.webhook_delivery_id = webhook_delivery_id
        self.webhook_id = webhook_id
        self.say = say
        tag = run_tag or (datetime.fromtimestamp(self.clock.now(), tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                          + "-" + secrets.token_hex(3))
        self.holds = [Hold(1, f"live-{tag}-1"), Hold(2, f"live-{tag}-2")]
        self.results: list[StepResult] = []
        self.cleanup: list[str] = []
        self.started_at = self.clock.now()
        self.finished_at: float | None = None
        self.interrupted = False

    # -- running steps ---------------------------------------------------------------------------

    def _record(self, result: StepResult) -> StepResult:
        result.at = iso_utc(self.clock.now())
        self.results.append(result)
        self.say(f"{result.status:<4}  {result.name:<36} {result.detail}")
        return result

    def result(self, name: str) -> StepResult | None:
        return next((r for r in self.results if r.name == name), None)

    def passed(self, name: str) -> bool:
        found = self.result(name)
        return found is not None and found.status == PASS

    def skip(self, name: str, reason: str) -> StepResult:
        return self._record(StepResult(name, SKIP, reason))

    def step(
        self, name: str, fn: Callable[[], tuple[str, Mapping[str, Any]]], *, needs: tuple[str, ...] = ()
    ) -> StepResult:
        missing = [n for n in needs if not self.passed(n)]
        if missing:
            return self.skip(name, "needs " + ", ".join(missing))
        try:
            detail, data = fn()
        except CheckFailed as exc:
            return self._record(StepResult(name, FAIL, exc.detail, jsonable(exc.data)))
        except PayPalError as err:
            return self._record(StepResult(
                name, FAIL, f"PayPal error {err.issue} (HTTP {err.status}, debug id {err.debug_id})",
                jsonable(error_data(err)),
            ))
        except HoldRevoked as exc:
            return self._record(StepResult(name, FAIL, f"the lease refused input: {exc}"))
        except Exception as exc:  # noqa: BLE001 - every failure is reported, none ends the run
            return self._record(StepResult(name, FAIL, f"{type(exc).__name__}: {exc}"))
        return self._record(StepResult(name, PASS, detail, jsonable(dict(data))))

    def run(self, *, webhook_only: bool = False) -> None:
        try:
            self.step("sandbox_only", self.check_target)
            if not webhook_only:
                self._run_money_steps()
        except KeyboardInterrupt:
            self.interrupted = True
            self.say("interrupted: releasing any open hold and writing the report for the steps that ran")
        if not webhook_only:
            self.release_open_holds()
            self.step("ledger_invariants", self.check_ledger)
        if self.interrupted:
            self.skip("webhook_verified", "the run was interrupted")
        elif self.webhook_path is None:
            self.skip(
                "webhook_verified",
                "no --webhook-event given" + ("" if self.webhook_id else " and PAYPAL_WEBHOOK_ID is not set"),
            )
        else:
            self.step("webhook_verified", self.check_webhook, needs=("sandbox_only",))
        self.finished_at = self.clock.now()

    def _run_money_steps(self) -> None:
        h1, h2 = self.holds
        self.step("client_token", self.check_client_token, needs=("sandbox_only",))
        self.step("hold1_order", lambda: self.create_order(h1), needs=("sandbox_only",))
        self.step("hold1_approved", lambda: self.wait_for_approval(h1), needs=("hold1_order",))
        self.step("hold1_authorized", lambda: self.authorize(h1), needs=("hold1_approved",))
        self.step("hold1_lease_grants", lambda: self.lease_grants(h1), needs=("hold1_authorized",))
        self.step("capture_7_final", lambda: self.capture(h1), needs=("hold1_authorized",))
        captured = ("capture_7_final",)
        self.step("auth_after_final_capture", lambda: self.auth_after_capture(h1), needs=captured)
        self.step("capture_replay_same_id", lambda: self.capture_replay(h1), needs=captured)
        self.step("second_capture_refused", lambda: self.second_capture(h1), needs=captured)
        self.step("void_after_capture_refused", lambda: self.void_after_capture(h1), needs=captured)
        self.step("refund_2", lambda: self.partial_refund(h1), needs=captured)

        self.step("hold2_order", lambda: self.create_order(h2), needs=("sandbox_only",))
        self.step("hold2_approved", lambda: self.wait_for_approval(h2), needs=("hold2_order",))
        self.step("hold2_authorized", lambda: self.authorize(h2), needs=("hold2_approved",))
        self.step("hold2_voided", lambda: self.void_hold(h2), needs=("hold2_authorized",))

        before = None
        if self.passed("refund_2") and self.passed("hold2_voided"):
            try:
                before = self.money_snapshot()
            except PayPalError as err:
                self.say(f"could not read the payments back before the negative tests: {err.issue}")
        needs_for = {
            "authorize": ("hold1_authorized",),
            "capture": ("hold2_voided",),
            "void": ("hold2_voided",),
            "refund": ("refund_2",),
        }
        for name, op, issue in MOCKS:
            self.step(name, lambda op=op, issue=issue: self.mocked_call(op, issue), needs=needs_for[op])
        if before is None:
            self.skip("mocks_moved_no_money", "needs refund_2, hold2_voided and a read-back before the negative tests")
        else:
            self.step("mocks_moved_no_money", lambda: self.compare_money(before))

    # -- steps -----------------------------------------------------------------------------------

    def check_target(self) -> tuple[str, dict[str, Any]]:
        if self.target == "sandbox":
            base = getattr(self.gateway, "base_uri", "")
            require_sandbox(base)
            return f"PayPalClient (official Server SDK) on {base}", {"base_uri": base}
        return "FakePayPal in memory: a test run, not the PayPal sandbox", {"base_uri": None}

    def check_client_token(self) -> tuple[str, dict[str, Any]]:
        token = self.gateway.client_token()
        if not token:
            raise CheckFailed(f"{self.paypal} issued no client token")
        detail = f"{self.paypal} issued a browser client token ({len(token)} characters; not stored)"
        return detail, {"length": len(token)}

    def create_order(self, hold: Hold) -> tuple[str, dict[str, Any]]:
        rec = self.ledger.create_rescue(make_check_quote(hold.no), rescue_id=hold.rescue_id,
                                        probe=check_probe_spec(hold.no))
        order = self.payments.create_hold(rec.id)
        hold.order = order
        rec = self.ledger.get(rec.id)
        entry = self.ledger.get_op(rec.id, MoneyOp.CREATE_ORDER)
        data = {
            "rescue_id": rec.id,
            "order_id": order.id,
            "status": order.status,
            "intent": order.intent,
            "amount": order.amount,
            "custom_id_returned": order.custom_id == rec.probe_spec_hash,
            "invoice_id_returned": order.invoice_id == rec.invoice_id,
            "request_id": entry.request_id if entry else None,
            "approve_url": order.approve_url,
            "ledger_state": rec.state.value,
        }
        problems = []
        if rec.state is not RescueState.HOLD_PENDING:
            problems.append(f"ledger state {rec.state.value}, not HOLD_PENDING")
        if order.intent != "AUTHORIZE":
            problems.append(f"intent {order.intent}, not AUTHORIZE")
        if money(order.amount) != HOLD:
            problems.append(f"PayPal's answer lists ${money(order.amount)}, not ${HOLD}")
        if self.target == "sandbox" and not order.approve_url:
            problems.append("PayPal sent no approval link (payer-action or approve)")
        if problems:
            raise CheckFailed("; ".join(problems), data)
        return (
            f"order {order.id} {order.status}, intent AUTHORIZE, ${money(order.amount)}, "
            f"PayPal-Request-Id {data['request_id']}, ledger HOLD_PENDING"
        ), data

    def wait_for_approval(self, hold: Hold) -> tuple[str, dict[str, Any]]:
        assert hold.order is not None
        order_id = hold.order.id
        self.approve(hold.no, hold.order, hold.order.approve_url)
        start = self.clock.now()
        deadline = start + self.approval_timeout
        seen: list[str] = []
        poll_errors = 0
        polls = 0
        while True:
            status = None
            try:
                status = self.gateway.get_order(order_id).status
            except PayPalError as err:
                if not err.retryable:
                    raise
                poll_errors += 1
            polls += 1
            if status and (not seen or seen[-1] != status):
                seen.append(status)
                if status != "APPROVED":
                    self.say(f"      order {order_id} is {status}; waiting for the buyer to approve it")
            data = {"order_id": order_id, "statuses_seen": seen, "polls": polls, "poll_errors": poll_errors,
                    "waited_s": round(self.clock.now() - start, 1)}
            if status == "APPROVED":
                plural = "poll" if polls == 1 else "polls"
                return f"order {order_id} APPROVED after {data['waited_s']:.0f} s ({polls} {plural})", data
            if status in ("VOIDED", "COMPLETED"):
                raise CheckFailed(f"order {order_id} is {status}, not APPROVED", data)
            now = self.clock.now()
            if now >= deadline:
                raise CheckFailed(
                    f"order {order_id} was not approved within {self.approval_timeout:.0f} s "
                    f"(last status {seen[-1] if seen else 'unknown'})",
                    data,
                )
            self.clock.sleep(min(self.poll_interval, deadline - now))

    def authorize(self, hold: Hold) -> tuple[str, dict[str, Any]]:
        auth = self.payments.authorize(hold.rescue_id)
        hold.auth = auth
        rec = self.ledger.get(hold.rescue_id)
        entry = self.ledger.get_op(hold.rescue_id, MoneyOp.AUTHORIZE)
        data = {
            "authorization_id": auth.id,
            "status": auth.status,
            "amount": auth.amount,
            "expiration_time": auth.expiration_time,
            "request_id": entry.request_id if entry else None,
            "ledger_state": rec.state.value,
        }
        problems = []
        if auth.status != "CREATED":
            problems.append(f"authorization {auth.status}, not CREATED")
        if money(auth.amount) != HOLD:
            problems.append(f"authorization for ${money(auth.amount)}, not ${HOLD}")
        if not auth.expiration_time:
            problems.append("no expiration_time")
        if rec.state is not RescueState.HELD:
            problems.append(f"ledger state {rec.state.value}, not HELD")
        if problems:
            raise CheckFailed("; ".join(problems), data)
        return (
            f"authorization {auth.id} CREATED for ${money(auth.amount)}, expires {auth.expiration_time}, "
            f"PayPal-Request-Id {data['request_id']}, ledger HELD"
        ), data

    def lease_grants(self, hold: Hold) -> tuple[str, dict[str, Any]]:
        rec = self.ledger.get(hold.rescue_id)
        lease = HoldLease(self.ledger, self.gateway, hold.rescue_id, clock=self.clock)
        auth = lease.check()
        ids = {
            "order_id": (auth.order_id, rec.order_id),
            "custom_id": (auth.custom_id, rec.probe_spec_hash),
            "invoice_id": (auth.invoice_id, rec.invoice_id),
        }
        data = {
            "granted": True,
            "status": auth.status,
            "amount": auth.amount,
            "returned": {name: bool(got) for name, (got, _) in ids.items()},
            "matches": {name: got == want for name, (got, want) in ids.items()},
        }
        missing = [name for name, (got, _) in ids.items() if not got]
        wrong = [name for name, (got, want) in ids.items() if got and got != want]
        if missing or wrong:
            parts = []
            if missing:
                parts.append(
                    "GET authorization did not return " + ", ".join(missing) + ", so the lease cannot check it"
                )
            if wrong:
                parts.append("GET authorization returned another " + ", ".join(wrong))
            raise CheckFailed("; ".join(parts), data)
        return (
            f"HoldLease granted input: GET authorization shows {auth.status} ${money(auth.amount)} with this "
            "rescue's order id, custom_id (probe spec hash) and invoice id"
        ), data

    def capture(self, hold: Hold) -> tuple[str, dict[str, Any]]:
        rid = hold.rescue_id
        self.ledger.transition(rid, RescueState.ACTIVE, reason="live check")
        self.ledger.transition(rid, RescueState.PROBING, reason="live check")
        cap = self.payments.capture(rid, CAPTURE)
        hold.capture = cap
        rec = self.ledger.get(rid)
        entry = self.ledger.get_op(rid, MoneyOp.CAPTURE)
        data = {
            "capture_id": cap.id,
            "status": cap.status,
            "amount": cap.amount,
            "final_capture": cap.final_capture,
            "request_id": entry.request_id if entry else None,
            "ledger_state": rec.state.value,
        }
        problems = []
        if money(cap.amount) != CAPTURE:
            problems.append(f"captured ${money(cap.amount)}, not ${CAPTURE}")
        if cap.final_capture is not True:
            problems.append("PayPal's answer does not show final_capture true")
        if cap.status not in ("COMPLETED", "PENDING"):
            problems.append(f"capture {cap.status}")
        if rec.state is not RescueState.CAPTURED:
            problems.append(f"ledger state {rec.state.value}, not CAPTURED")
        if problems:
            raise CheckFailed("; ".join(problems), data)
        return (
            f"capture {cap.id} {cap.status} ${money(cap.amount)} of ${HOLD}, final_capture true, "
            f"PayPal-Request-Id {data['request_id']}, ledger CAPTURED"
        ), data

    def auth_after_capture(self, hold: Hold) -> tuple[str, dict[str, Any]]:
        assert hold.auth is not None
        body = self.gateway.get_authorization_json(hold.auth.id)
        rec = self.ledger.get(hold.rescue_id)
        status = body.get("status")
        details = body.get("status_details")
        data = {
            "status": status,
            "status_details": details,
            "ledger_auth_status": rec.auth_status,
            "same_as_ledger": status == rec.auth_status,
            "authorization": auth_fields(body),
        }
        ledger_note = f"the ledger recorded {rec.auth_status}"
        if status != rec.auth_status:
            ledger_note += ", which differs"
        if status not in ("CAPTURED", "PARTIALLY_CAPTURED"):
            raise CheckFailed(
                f"after a final capture {self.paypal} shows the authorization as {status}, status_details "
                f"{describe_details(details)}; {ledger_note}",
                data,
            )
        return f"{self.paypal} shows status {status}, status_details {describe_details(details)}; {ledger_note}", data

    def capture_replay(self, hold: Hold) -> tuple[str, dict[str, Any]]:
        assert hold.capture is not None and hold.order is not None
        entry = self.ledger.get_op(hold.rescue_id, MoneyOp.CAPTURE)
        assert entry is not None
        args = entry.args
        again = self.gateway.capture(args["auth_id"], money(args["amount"]), True, args["invoice_id"], entry.request_id)
        stored = self.payments.capture(hold.rescue_id, CAPTURE)
        listed = self.gateway.get_order_payments(hold.order.id).captures
        data = {
            "request_id": entry.request_id,
            "first_capture_id": hold.capture.id,
            "replay_capture_id": again.id,
            "replay_amount": again.amount,
            "ledger_answer_capture_id": stored.id,
            "captures_listed": [{"id": c.id, "status": c.status, "amount": c.amount} for c in listed],
        }
        problems = []
        if again.id != hold.capture.id or money(again.amount) != money(hold.capture.amount):
            problems.append(f"the replay returned capture {again.id} ${money(again.amount)}")
        if stored.id != hold.capture.id:
            problems.append(f"the ledger answered with capture {stored.id}")
        if len(listed) != 1:
            problems.append(f"{self.paypal} lists {len(listed)} captures on the order")
        if problems:
            raise CheckFailed("; ".join(problems), data)
        return (
            f"replaying PayPal-Request-Id {entry.request_id} returned capture {again.id} again; the ledger "
            f"answered from its outbox; {self.paypal} lists 1 capture"
        ), data

    def second_capture(self, hold: Hold) -> tuple[str, dict[str, Any]]:
        assert hold.auth is not None and hold.order is not None
        rec = self.ledger.get(hold.rescue_id)
        request_id = f"cap-{hold.rescue_id}-check"
        try:
            extra = self.gateway.capture(hold.auth.id, RELEASED, True, f"{rec.capture_invoice_id}-2", request_id)
        except PayPalError as err:
            listed = self.gateway.get_order_payments(hold.order.id).captures
            data = {**error_data(err), "request_id": request_id, "amount": RELEASED, "captures_listed": len(listed)}
            if not err.refused:
                raise CheckFailed(
                    f"{self.paypal} answered {err.issue} (HTTP {err.status}), which is not a refusal", data
                )
            if len(listed) != 1:
                raise CheckFailed(f"{self.paypal} lists {len(listed)} captures on the order", data)
            return (
                f"{self.paypal} refused a second capture of the released ${RELEASED}: {err.issue} (HTTP {err.status}); "
                "1 capture listed"
            ), data
        raise CheckFailed(
            f"{self.paypal} accepted a second capture {extra.id} of ${money(extra.amount)}",
            {"request_id": request_id, "capture_id": extra.id, "status": extra.status, "amount": extra.amount},
        )

    def void_after_capture(self, hold: Hold) -> tuple[str, dict[str, Any]]:
        assert hold.auth is not None
        request_id = f"void-{hold.rescue_id}-check"
        try:
            answer = self.gateway.void(hold.auth.id, request_id)
        except PayPalError as err:
            after = self.gateway.get_authorization(hold.auth.id)
            data = {**error_data(err), "request_id": request_id, "status_after": after.status}
            if not err.refused:
                raise CheckFailed(
                    f"{self.paypal} answered {err.issue} (HTTP {err.status}), which is not a refusal", data
                )
            if after.status == "VOIDED":
                raise CheckFailed(f"{self.paypal} refused the void but now shows the authorization VOIDED", data)
            return (
                f"{self.paypal} refused to void the captured authorization: {err.issue} (HTTP {err.status}); "
                f"it still reads {after.status}"
            ), data
        raise CheckFailed(
            f"{self.paypal} accepted a void after the capture",
            {"request_id": request_id, "status": answer.status if answer else None},
        )

    def partial_refund(self, hold: Hold) -> tuple[str, dict[str, Any]]:
        assert hold.capture is not None
        rid = hold.rescue_id
        request_id = f"refund-{rid}-check"
        refund = self.gateway.refund(hold.capture.id, REFUND, request_id)
        hold.refund = refund
        again = self.gateway.refund(hold.capture.id, REFUND, request_id)
        self.payments.adopt_refunds(rid)
        rec = self.ledger.get(rid)
        capture_now = self.gateway.get_capture(hold.capture.id)
        data = {
            "refund_id": refund.id,
            "status": refund.status,
            "amount": refund.amount,
            "request_id": request_id,
            "replay_refund_id": again.id,
            "capture_status_after": capture_now.status,
            "ledger_state": rec.state.value,
            "ledger_refunded": rec.refunded_amount,
            "ledger_capture_status": rec.capture_status,
        }
        problems = []
        if money(refund.amount) != REFUND or refund.status not in ("COMPLETED", "PENDING"):
            problems.append(f"refund {refund.status} ${money(refund.amount)}")
        if again.id != refund.id:
            problems.append(f"the replay with the same request id returned refund {again.id}")
        if rec.state is not RescueState.CAPTURED:
            problems.append(f"ledger state {rec.state.value}, not CAPTURED")
        if rec.refunded_amount != REFUND:
            problems.append(f"the ledger shows ${rec.refunded_amount} refunded")
        expected_capture = "PARTIALLY_REFUNDED" if refund.status == "COMPLETED" else "COMPLETED"
        if capture_now.status != expected_capture:
            problems.append(f"the capture reads {capture_now.status}, not {expected_capture}")
        if problems:
            raise CheckFailed("; ".join(problems), data)
        return (
            f"refund {refund.id} {refund.status} ${money(refund.amount)} of ${money(hold.capture.amount)}; the replay "
            f"returned the same refund; capture {capture_now.status}; the ledger read it back and stays CAPTURED"
        ), data

    def void_hold(self, hold: Hold) -> tuple[str, dict[str, Any]]:
        assert hold.auth is not None
        rid = hold.rescue_id
        self.payments.void(rid, "live check: release the second hold")
        rec = self.ledger.get(rid)
        entry = self.ledger.get_op(rid, MoneyOp.VOID)
        assert entry is not None
        body = self.gateway.get_authorization_json(hold.auth.id)
        status = body.get("status")
        details = body.get("status_details")
        try:
            again = self.gateway.void(hold.auth.id, entry.request_id)
        except PayPalError as err:
            after = self.gateway.get_authorization(hold.auth.id)
            replay = f"was refused with {err.issue} (HTTP {err.status}) and the authorization reads {after.status}"
            replay_ok = err.refused and after.status == "VOIDED"
        else:
            replay = f"returned {again.status if again else 'no body'}"
            replay_ok = again is None or again.status == "VOIDED"
        data = {
            "request_id": entry.request_id,
            "status": status,
            "status_details": details,
            "ledger_state": rec.state.value,
            "replay": replay,
            "authorization": auth_fields(body),
        }
        problems = []
        if rec.state is not RescueState.VOIDED:
            problems.append(f"ledger state {rec.state.value}, not VOIDED")
        if status != "VOIDED":
            problems.append(f"{self.paypal} shows the authorization as {status}")
        if not replay_ok:
            problems.append(f"replaying the void {replay}")
        if problems:
            raise CheckFailed("; ".join(problems), data)
        return (
            f"voided with PayPal-Request-Id {entry.request_id}: {self.paypal} shows status VOIDED, status_details "
            f"{describe_details(details)}; ledger VOIDED; replaying the void {replay}"
        ), data

    def _arm_mock(self, op: str, issue: str) -> str:
        mock_next = getattr(self.gateway, "mock_next", None)
        if callable(mock_next):
            mock_next(op, issue)
            return "PayPal-Mock-Response"
        inject = getattr(self.gateway, "inject", None)
        if callable(inject):
            inject(op, issue)
            return "FakePayPal.inject"
        raise CheckFailed("this gateway cannot ask for a PayPal error")

    def mocked_call(self, op: str, issue: str) -> tuple[str, dict[str, Any]]:
        h1, h2 = self.holds
        calls: dict[str, Callable[[], Any]] = {
            "authorize": lambda: self.gateway.authorize(h1.order.id, f"mock-auth-{h1.rescue_id}"),
            "capture": lambda: self.gateway.capture(
                h2.auth.id, MOCK_CAPTURE, True, f"keyhold-{h2.rescue_id}-mock", f"mock-cap-{h2.rescue_id}"
            ),
            "void": lambda: self.gateway.void(h2.auth.id, f"mock-void-{h2.rescue_id}"),
            "refund": lambda: self.gateway.refund(h1.capture.id, MOCK_REFUND, f"mock-refund-{h1.rescue_id}"),
        }
        mechanism = self._arm_mock(op, issue)
        who = type(self.gateway).__name__
        try:
            result = calls[op]()
        except PayPalError as err:
            data = {**error_data(err), "mechanism": mechanism, "asked_for": issue, "operation": op}
            if err.issue != issue:
                raise CheckFailed(f"asked for {issue}; {self.paypal} answered {err.issue} (HTTP {err.status})", data)
            if not err.refused:
                raise CheckFailed(
                    f"{issue} came back as HTTP {err.status}, which Keyhold treats as an unknown outcome", data
                )
            return f"{mechanism} {issue}: {who}.{op} raised {err.issue}, HTTP {err.status}, a refusal", data
        raise CheckFailed(
            f"asked for {issue}, but the {op} call succeeded",
            {"mechanism": mechanism, "asked_for": issue, "operation": op, "result": jsonable(result)},
        )

    def money_snapshot(self) -> dict[str, Any]:
        h1, h2 = self.holds
        assert h1.order is not None and h2.auth is not None
        payments = self.gateway.get_order_payments(h1.order.id)
        return {
            # The authorize mock runs on order 1, so its authorizations are compared too.
            "order1_authorizations": sorted((a.id, a.status, str(money(a.amount))) for a in payments.authorizations),
            "order1_captures": sorted((c.id, str(money(c.amount))) for c in payments.captures),
            "order1_refunds": sorted((r.id, str(money(r.amount))) for r in payments.refunds),
            "hold2_authorization": self.gateway.get_authorization(h2.auth.id).status,
        }

    def compare_money(self, before: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        after = self.money_snapshot()
        data = {"before": before, "after": after}
        if after != before:
            raise CheckFailed(
                f"{self.paypal} lists different authorizations, captures, refunds or hold 2 status after the mocks",
                data,
            )
        auths, caps, refunds = after["order1_authorizations"], after["order1_captures"], after["order1_refunds"]
        return (
            f"after the four mocked calls {self.paypal} still lists {len(auths)} authorization, {len(caps)} capture "
            f"and {len(refunds)} refund on order 1, and hold 2 is {after['hold2_authorization']}"
        ), data

    def release_open_holds(self) -> None:
        """Void any hold a failed or interrupted step left open, through the ledger."""
        open_states = (RescueState.HOLD_PENDING, RescueState.HELD, RescueState.ACTIVE, RescueState.PROBING)
        for hold in self.holds:
            try:
                rec = self.ledger.get(hold.rescue_id)
            except UnknownRescue:
                continue
            if rec.state not in open_states:
                continue
            try:
                self.payments.void(rec.id, "live check: released at the end of the run")
                note = f"hold {hold.no}: {rec.state.value} -> {self.ledger.get(rec.id).state.value}"
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                note = f"hold {hold.no}: left {rec.state.value} ({type(exc).__name__}: {exc})"
            self.cleanup.append(note)
            self.say(f"cleanup: {note}")

    def check_ledger(self) -> tuple[str, dict[str, Any]]:
        rescues = self.ledger.rescues()
        problems = {r.id: self.ledger.check_invariants(r.id) for r in rescues}
        bad = {rid: found for rid, found in problems.items() if found}
        states = {r.id: r.state.value for r in rescues}
        data = {"states": states, "problems": bad}
        if bad:
            raise CheckFailed("ledger invariant problems: " + "; ".join(f"{k}: {', '.join(v)}" for k, v in bad.items()),
                              data)
        if not rescues:
            return "no rescue was created", data
        listed = ", ".join(f"hold {h.no} {states[h.rescue_id]}" for h in self.holds if h.rescue_id in states)
        return f"no invariant problem in {len(rescues)} rescues ({listed})", data

    def check_webhook(self) -> tuple[str, dict[str, Any]]:
        assert self.webhook_path is not None
        if not self.webhook_id:
            raise CheckFailed("PAYPAL_WEBHOOK_ID is not set, so the delivery cannot be verified")
        headers, raw, meta = load_webhook(self.webhook_path, self.webhook_delivery_id)
        try:
            event = json.loads(raw)
        except ValueError as exc:
            raise CheckFailed("the delivery body is not JSON", meta) from exc
        if not isinstance(event, dict):
            raise CheckFailed("the delivery body is not a JSON object", meta)
        verified = self.gateway.verify_webhook(headers, raw, self.webhook_id)
        tampered, how = tampered_copy(raw, event)
        tampered_verified = self.gateway.verify_webhook(headers, tampered, self.webhook_id)
        data = {
            **meta,
            "event_id": event.get("id"),
            "event_type": event.get("event_type"),
            "verified": verified,
            "tampered_copy": how,
            "tampered_copy_verified": tampered_verified,
        }
        if not verified:
            raise CheckFailed("verify-webhook-signature did not answer SUCCESS for the delivery", data)
        if tampered_verified:
            raise CheckFailed(f"a copy of the delivery with {how} also verified", data)
        return (
            f"{event.get('event_type')} {event.get('id')}: verified as received, and a copy with {how} was rejected"
        ), data

    # -- report ----------------------------------------------------------------------------------

    def counts(self) -> dict[str, int]:
        return {s: sum(1 for r in self.results if r.status == s) for s in (PASS, FAIL, SKIP)}

    @property
    def ok(self) -> bool:
        return not self.interrupted and self.counts()[FAIL] == 0

    def rescue_summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for hold in self.holds:
            try:
                rec = self.ledger.get(hold.rescue_id)
            except UnknownRescue:
                continue
            out[f"hold{hold.no}"] = {
                "rescue_id": rec.id,
                "state": rec.state.value,
                "quote_amount": rec.quote_amount,
                "order_id": rec.order_id,
                "authorization_id": rec.authorization_id,
                "auth_status": rec.auth_status,
                "capture_id": rec.capture_id,
                "captured_amount": rec.captured_amount,
                "refund_id": rec.refund_id,
                "refunded_amount": rec.refunded_amount,
                "outbox": [
                    {"op": e.op.value, "request_id": e.request_id, "status": e.status.value}
                    for e in self.ledger.ops(rec.id)
                ],
            }
        return jsonable(out)

    def report(self) -> dict[str, Any]:
        return {
            "tool": "tools/live_check.py",
            "target": self.target,
            "target_detail": (
                "PayPal sandbox through PayPalClient" if self.target == "sandbox"
                else "FakePayPal in memory (a test run, not the PayPal sandbox)"
            ),
            "started_at": iso_utc(self.started_at),
            "finished_at": iso_utc(self.finished_at if self.finished_at is not None else self.clock.now()),
            "result": PASS if self.ok else FAIL,
            "interrupted": self.interrupted,
            "counts": self.counts(),
            "amounts": {"hold": HOLD, "capture": CAPTURE, "refund": REFUND, "currency": CURRENCY},
            "rescues": self.rescue_summary(),
            "steps": [jsonable(asdict(r)) for r in self.results],
            "cleanup": list(self.cleanup),
        }

    def write(self, out_dir: Path) -> tuple[Path, Path]:
        report = jsonable(self.report())
        report_path = out_dir / "report.json"
        summary_path = out_dir / "summary.md"
        report_path.write_text(json.dumps(report, indent=2) + "\n", "utf-8")
        summary_path.write_text(summary_markdown(report), "utf-8")
        return report_path, summary_path


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def summary_markdown(report: Mapping[str, Any]) -> str:
    steps = {s["name"]: s for s in report["steps"]}
    counts = report["counts"]
    lines = [
        "# PayPal live check",
        "",
        f"- Target: {report['target_detail']}",
        f"- Started {report['started_at']}, finished {report['finished_at']}",
        f"- Result: **{report['result']}**: {counts[PASS]} passed, {counts[FAIL]} failed, {counts[SKIP]} skipped"
        + (" (interrupted)" if report.get("interrupted") else ""),
        f"- Amounts: hold ${report['amounts']['hold']}, capture ${report['amounts']['capture']} with "
        f"final_capture, refund ${report['amounts']['refund']}",
        "",
        "| Step | Result | Detail |",
        "|---|---|---|",
    ]
    lines += [f"| {_cell(s['name'])} | {s['status']} | {_cell(s['detail'])} |" for s in report["steps"]]

    # PayPal's exact answers are listed whether the step passed or not: a FAIL is when they matter.
    answers: list[str] = []
    auth = steps.get("auth_after_final_capture")
    if auth and "status" in auth["data"]:
        d = auth["data"]
        answers.append(
            f"- Authorization after the final capture ({auth['status']}): status `{d['status']}`, status_details "
            f"`{describe_details(d['status_details'])}` (the ledger recorded `{d['ledger_auth_status']}`)."
        )
    for name, label in (
        ("second_capture_refused", "Second capture"),
        ("void_after_capture_refused", "Void after the capture"),
    ):
        s = steps.get(name)
        if s and "issue" in s["data"]:
            answers.append(f"- {label}: `{s['data']['issue']}` (HTTP {s['data']['http_status']}).")
    voided = steps.get("hold2_voided")
    if voided and "status" in voided["data"]:
        d = voided["data"]
        answers.append(
            f"- Voided hold ({voided['status']}): status `{d['status']}`, status_details "
            f"`{describe_details(d['status_details'])}`; the void replay {d['replay']}."
        )
    for name, _op, issue in MOCKS:
        s = steps.get(name)
        if s and "issue" in s["data"]:
            answers.append(
                f"- {s['data'].get('mechanism', 'Mocked')} `{issue}` on {s['data'].get('operation', name)}: "
                f"`{s['data']['issue']}` (HTTP {s['data'].get('http_status')})."
            )
    if answers:
        lines += ["", "## PayPal's answers", ""] + answers
    if report.get("cleanup"):
        lines += ["", "## Cleanup", ""] + [f"- {c}" for c in report["cleanup"]]
    lines += ["", "Full details, with every id and request id, are in `report.json`.", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------------------------


def environment_problem(environ: Mapping[str, str]) -> str | None:
    """Why the environment asks for something other than the sandbox, or None."""
    for name in ENVIRONMENT_VARS:
        value = environ.get(name)
        if value is not None and value.strip().lower() not in {v.lower() for v in SANDBOX_VALUES}:
            return f"{name} asks for a PayPal environment other than the sandbox; this check runs only there"
    return None


def console_approver(say: Say) -> Approver:
    def approve(hold_no: int, order: Order, link: str | None) -> None:
        say("")
        say(f"Hold {hold_no}: approve order {order.id} (${money(order.amount)}) in a browser:")
        say(f"  {link or '(PayPal sent no approval link)'}")
        say("  Log in with a sandbox personal account (a buyer from the developer dashboard's sandbox")
        say("  accounts, not the business account that owns the REST app) and approve the payment.")
        say("  PayPal may not send the browser anywhere afterwards; this check polls the order.")
        say("")

    return approve


def new_run_dir(root: Path, started: float) -> Path:
    stamp = datetime.fromtimestamp(started, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = root / stamp
    n = 2
    while candidate.exists():
        candidate = root / f"{stamp}-{n}"
        n += 1
    candidate.mkdir(parents=True)
    return candidate


def _shown(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def _positive(text: str) -> float:
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return value


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tools/live_check.py",
        description="Check the PayPal sandbox behaviour Keyhold depends on, through Keyhold's own code.",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="folder for the run folders (default: runs/live-check)")
    parser.add_argument("--approval-timeout", type=_positive, default=900.0,
                        help="seconds to wait for each buyer approval (default: 900)")
    parser.add_argument("--poll-interval", type=_positive, default=5.0,
                        help="seconds between reads of an order waiting for approval (default: 5)")
    parser.add_argument("--webhook-event", type=Path, default=None,
                        help="a received delivery to verify: a JSON file with headers and body, or a Keyhold ledger")
    parser.add_argument("--webhook-delivery-id", type=int, default=None,
                        help="with a ledger: the delivery to verify (default: the newest one with a body)")
    parser.add_argument("--webhook-only", action="store_true",
                        help="only check the target and verify the webhook delivery; no holds")
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    gateway: PaymentGateway | None = None,
    approve: Approver | None = None,
    clock: Clock | None = None,
    say: Say | None = None,
) -> int:
    """Run the check. ``gateway``, ``approve`` and ``clock`` exist for tests (FakePayPal)."""
    args = parse_args(argv)
    env = os.environ if environ is None else environ
    say = say or (lambda line: print(line, flush=True))
    if args.webhook_only and args.webhook_event is None:
        say("refused: --webhook-only needs --webhook-event")
        return 2
    if gateway is None:
        problem = environment_problem(env)
        if problem:
            say(f"refused: {problem}")
            return 2
        try:
            gateway = PayPalClient.from_env(env)
            require_sandbox(gateway.base_uri)
        except (RuntimeError, ValueError) as exc:
            say(f"refused: {exc}")
            return 2
    target = "sandbox" if isinstance(gateway, PayPalClient) else "fake"
    clock = clock or SystemClock()
    out_dir = new_run_dir(args.out, clock.now())
    ledger = Ledger(out_dir / "ledger.sqlite3", clock=clock)
    try:
        check = LiveCheck(
            gateway,
            ledger=ledger,
            approve=approve or console_approver(say),
            target=target,
            clock=clock,
            poll_interval=args.poll_interval,
            approval_timeout=args.approval_timeout,
            webhook_path=args.webhook_event,
            webhook_delivery_id=args.webhook_delivery_id,
            webhook_id=env.get("PAYPAL_WEBHOOK_ID", "").strip(),
            say=say,
        )
        what = "the PayPal sandbox through PayPalClient" if target == "sandbox" else "FakePayPal (a test run)"
        say(f"Keyhold live check against {what}. Run folder: {_shown(out_dir)}")
        if not args.webhook_only:
            say(f"Two holds of ${HOLD}: the first is captured (${CAPTURE}, final) and refunded ${REFUND}, "
                "the second is voided.")
        check.run(webhook_only=args.webhook_only)
        report_path, summary_path = check.write(out_dir)
    finally:
        ledger.close()
    counts = check.counts()
    say("")
    say(f"{PASS if check.ok else FAIL}: {counts[PASS]} passed, {counts[FAIL]} failed, {counts[SKIP]} skipped")
    say(f"Report: {_shown(report_path)}")
    say(f"Summary: {_shown(summary_path)}")
    if check.interrupted:
        return 130
    return 0 if check.ok else 1


if __name__ == "__main__":
    sys.exit(main())
