"""Shared data types and interfaces for Keyhold.

Every module builds against the names in this file so the parts can be written in parallel.
Money is always ``Decimal`` in US dollars with two decimal places. Times are UTC: either an
ISO 8601 string as PayPal returns it, or float epoch seconds from a ``Clock``.

Nothing here talks to the network, the console or the model.
"""

from __future__ import annotations

import enum
import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol, runtime_checkable

CURRENCY = "USD"
CENT = Decimal("0.01")


def money(value: Decimal | str | int) -> Decimal:
    """Return ``value`` as a two-decimal ``Decimal``. Floats are refused to avoid rounding surprises."""
    if isinstance(value, float):
        raise TypeError("use Decimal or str for money, not float")
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def canonical_json(obj: Any) -> str:
    """Deterministic JSON used for every hash in Keyhold (sorted keys, no spaces, UTF-8 kept)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_hex(text: str | bytes) -> str:
    data = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------------------------


class Tier(str, enum.Enum):
    """Price tiers. The fault class decides the tier; see ``keyhold.faults`` for the class table."""

    T1 = "T1"  # single config fix
    T2 = "T2"  # boot-path repair
    T3 = "T3"  # kernel, initramfs or disk

    @property
    def price(self) -> Decimal:
        return TIER_PRICE[self]


TIER_PRICE: Mapping[Tier, Decimal] = {
    Tier.T1: Decimal("9.00"),
    Tier.T2: Decimal("19.00"),
    Tier.T3: Decimal("29.00"),
}


# --------------------------------------------------------------------------------------------
# PayPal objects (both PayPalClient and FakePayPal return these, never SDK models)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Order:
    """A PayPal order. ``status`` is the PayPal string (CREATED, APPROVED, COMPLETED, VOIDED, ...)."""

    id: str
    status: str
    amount: Decimal
    intent: str = "AUTHORIZE"
    custom_id: str | None = None
    invoice_id: str | None = None
    authorization_id: str | None = None  # set once the order has been authorized
    create_time: str | None = None
    # Where the buyer approves the order outside the JS SDK: PayPal's "payer-action" link (or
    # "approve" for an order created without a payment source). None when PayPal sent neither.
    approve_url: str | None = None


@dataclass(frozen=True)
class Authorization:
    """An authorized payment. ``status``: CREATED, CAPTURED, PARTIALLY_CAPTURED, VOIDED, DENIED, PENDING.

    ``expiration_time`` is the ISO 8601 string from PayPal (None if PayPal did not send one).
    """

    id: str
    status: str
    amount: Decimal
    expiration_time: str | None
    order_id: str | None = None
    custom_id: str | None = None
    invoice_id: str | None = None


@dataclass(frozen=True)
class Capture:
    """A captured payment. ``status``: COMPLETED, PENDING, DECLINED, PARTIALLY_REFUNDED, REFUNDED, FAILED."""

    id: str
    status: str
    amount: Decimal
    final_capture: bool
    authorization_id: str | None = None
    invoice_id: str | None = None
    create_time: str | None = None


# Capture statuses that mean no money moved: PayPal declined or failed the capture.
NO_MONEY_CAPTURE: frozenset[str] = frozenset({"DECLINED", "DENIED", "FAILED"})


@dataclass(frozen=True)
class Refund:
    """A refund of a capture. ``status``: COMPLETED, PENDING, FAILED, CANCELLED."""

    id: str
    status: str
    amount: Decimal
    capture_id: str | None = None


class PayPalError(Exception):
    """A PayPal call failed. Raised by both PayPalClient and FakePayPal.

    ``issue`` is the PayPal issue code from ``details[0].issue`` when present (for example
    AUTHORIZATION_EXPIRED, PREVIOUSLY_CAPTURED, REFUND_AMOUNT_EXCEEDED, TRANSACTION_REFUSED,
    PAYMENT_DENIED), otherwise the error ``name`` (for example INTERNAL_SERVER_ERROR).
    ``retryable`` is True when trying again can change the answer and the outcome may be unknown:
    transport failures, 5xx, 401 (the token was refused or could not be fetched), 409 and 429.
    The same request id must then be replayed rather than a new operation started.
    ``retry_after`` is PayPal's Retry-After header in seconds, when it sent one.
    """

    def __init__(
        self,
        issue: str,
        message: str = "",
        *,
        status: int | None = None,
        debug_id: str | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"{issue}: {message}" if message else issue)
        self.issue = issue
        self.message = message
        self.status = status
        self.debug_id = debug_id
        self.retryable = retryable
        self.retry_after = retry_after

    @property
    def refused(self) -> bool:
        """PayPal processed the request and said no: an HTTP 4xx answer that a retry of the same
        request cannot change. Nothing happened at PayPal. Anything else (no answer, 5xx, 401,
        408, 409, 429, a response that could not be read) leaves the outcome open."""
        return (
            not self.retryable
            and self.status is not None
            and 400 <= self.status < 500
            and self.status not in (401, 408, 409, 429)
        )


@runtime_checkable
class PaymentGateway(Protocol):
    """The interface shared by ``keyhold.pay.client.PayPalClient`` and ``FakePayPal``.

    Every money operation takes a caller-chosen ``request_id`` that is sent as PayPal-Request-Id,
    so replaying a call with the same id returns the first result instead of moving money twice.
    """

    def client_token(self) -> str:
        """Browser-safe token for the JS SDK v6 (``createInstance({clientToken})``)."""
        ...

    def create_hold(self, amount: Decimal, custom_id: str, invoice_id: str, request_id: str) -> Order:
        """Create an order with intent AUTHORIZE for ``amount`` USD."""
        ...

    def authorize(self, order_id: str, request_id: str) -> Authorization:
        """Authorize an approved order and return its single authorization."""
        ...

    def get_order(self, order_id: str) -> Order: ...

    def get_authorization(self, auth_id: str) -> Authorization: ...

    def capture(
        self,
        auth_id: str,
        amount: Decimal,
        final_capture: bool,
        invoice_id: str,
        request_id: str,
    ) -> Capture: ...

    def void(self, auth_id: str, request_id: str) -> Authorization | None:
        """Void the authorization. Returns the authorization if PayPal sent one back."""
        ...

    def refund(self, capture_id: str, amount: Decimal, request_id: str) -> Refund: ...

    def get_capture(self, capture_id: str) -> Capture: ...

    def verify_webhook(self, headers: Mapping[str, str], raw: bytes, webhook_id: str) -> bool:
        """True only when PayPal's verify-webhook-signature answers SUCCESS for this delivery."""
        ...


# --------------------------------------------------------------------------------------------
# Rescue state machine and outbox
# --------------------------------------------------------------------------------------------


class RescueState(str, enum.Enum):
    QUOTED = "QUOTED"  # free look done, quote stored, no money yet
    HOLD_PENDING = "HOLD_PENDING"  # order created, waiting for owner approval / authorization
    HELD = "HELD"  # authorization CREATED, keyboard not yet lent
    ACTIVE = "ACTIVE"  # agent is typing through the gate
    PROBING = "PROBING"  # agent finished, owner probe running
    CAPTURED = "CAPTURED"  # probe passed and the capture went through; warranty running
    WARRANTY_OK = "WARRANTY_OK"  # warranty window ended without two consecutive failures
    REFUNDED = "REFUNDED"  # warranty failed, capture refunded in full
    VOIDED = "VOIDED"  # hold released without a capture

    @property
    def terminal(self) -> bool:
        return self in TERMINAL_STATES

    @property
    def rank(self) -> int:
        """Position along the happy path. State only moves to a higher rank (VOIDED ranks above
        every pre-capture state and is never left)."""
        return STATE_RANK[self]


STATE_RANK: Mapping[RescueState, int] = {
    RescueState.QUOTED: 0,
    RescueState.HOLD_PENDING: 1,
    RescueState.HELD: 2,
    RescueState.ACTIVE: 3,
    RescueState.PROBING: 4,
    RescueState.CAPTURED: 5,
    RescueState.VOIDED: 5,
    RescueState.WARRANTY_OK: 6,
    RescueState.REFUNDED: 6,
}

TERMINAL_STATES: frozenset[RescueState] = frozenset(
    {RescueState.WARRANTY_OK, RescueState.REFUNDED, RescueState.VOIDED}
)

# The only legal moves (PLAN.md "Ledger states"). Anything else is a bug or a stale event.
ALLOWED_TRANSITIONS: Mapping[RescueState, frozenset[RescueState]] = {
    RescueState.QUOTED: frozenset({RescueState.HOLD_PENDING, RescueState.VOIDED}),
    RescueState.HOLD_PENDING: frozenset({RescueState.HELD, RescueState.VOIDED}),
    RescueState.HELD: frozenset({RescueState.ACTIVE, RescueState.VOIDED}),
    RescueState.ACTIVE: frozenset({RescueState.PROBING, RescueState.VOIDED}),
    RescueState.PROBING: frozenset({RescueState.CAPTURED, RescueState.VOIDED}),
    # CAPTURED -> VOIDED only when PayPal declined the capture, so no money moved (ledger guard).
    RescueState.CAPTURED: frozenset({RescueState.WARRANTY_OK, RescueState.REFUNDED, RescueState.VOIDED}),
    RescueState.WARRANTY_OK: frozenset(),
    RescueState.REFUNDED: frozenset(),
    RescueState.VOIDED: frozenset(),
}


def can_transition(current: RescueState, target: RescueState) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


class MoneyOp(str, enum.Enum):
    """PayPal operations that go through the outbox. At most one is in flight per rescue."""

    CREATE_ORDER = "create_order"
    AUTHORIZE = "authorize"
    CAPTURE = "capture"
    VOID = "void"
    REFUND = "refund"


REQUEST_ID_PREFIX: Mapping[MoneyOp, str] = {
    MoneyOp.CREATE_ORDER: "order",
    MoneyOp.AUTHORIZE: "auth",
    MoneyOp.CAPTURE: "cap",
    MoneyOp.VOID: "void",
    MoneyOp.REFUND: "refund",
}


def request_id_for(rescue_id: str, op: MoneyOp) -> str:
    """The PayPal-Request-Id for ``op`` on ``rescue_id``: ``cap-{rescue_id}``, ``void-{rescue_id}``,
    ``refund-{rescue_id}``, ``order-{rescue_id}``, ``auth-{rescue_id}``.

    Deterministic, so a crash between the outbox write and the PayPal response is repaired by
    replaying the same id. Each op happens at most once per rescue, so one id per (rescue, op)
    is enough.
    """
    if not rescue_id:
        raise ValueError("rescue_id is required")
    return f"{REQUEST_ID_PREFIX[MoneyOp(op)]}-{rescue_id}"


def request_id_round(rescue_id: str, op: MoneyOp, attempt: int) -> str:
    """The request id of a later round of ``op``: ``void-{rescue_id}-2`` and so on.

    A new round is started only after PayPal definitely refused the previous one (an HTTP 4xx
    answer, confirmed by reading the payment back), so the earlier request moved no money and a
    fresh id cannot repeat it. Round 1 is ``request_id_for``.
    """
    base = request_id_for(rescue_id, op)
    if attempt < 1:
        raise ValueError("attempt must be at least 1")
    return base if attempt == 1 else f"{base}-{attempt}"


class OutboxStatus(str, enum.Enum):
    PENDING = "PENDING"  # written before the PayPal call; replay with the same request id
    DONE = "DONE"
    FAILED = "FAILED"  # PayPal gave a final, non-retryable answer


# --------------------------------------------------------------------------------------------
# Quote (free look)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Quote:
    """Result of the free look: what is wrong, the screen lines that show it, and the price.

    ``quote_hash`` covers fault_class, evidence, tier, plan and amount, so a stored quote cannot
    be edited without the hash changing. A capture may equal or undercut ``amount``, never exceed it.
    """

    fault_class: str
    evidence: tuple[str, ...]
    tier: Tier
    plan: str
    quote_hash: str

    @property
    def amount(self) -> Decimal:
        return self.tier.price

    @staticmethod
    def compute_hash(fault_class: str, evidence: Sequence[str], tier: Tier, plan: str) -> str:
        return sha256_hex(
            canonical_json(
                {
                    "fault_class": fault_class,
                    "evidence": list(evidence),
                    "tier": Tier(tier).value,
                    "plan": plan,
                    "amount": str(TIER_PRICE[Tier(tier)]),
                    "currency": CURRENCY,
                }
            )
        )

    @classmethod
    def build(
        cls,
        fault_class: str,
        evidence: Sequence[str],
        tier: Tier,
        plan: str,
    ) -> Quote:
        tier = Tier(tier)
        evidence = tuple(evidence)
        return cls(
            fault_class=fault_class,
            evidence=evidence,
            tier=tier,
            plan=plan,
            quote_hash=cls.compute_hash(fault_class, evidence, tier, plan),
        )

    def hash_ok(self) -> bool:
        return self.quote_hash == self.compute_hash(self.fault_class, self.evidence, self.tier, self.plan)


# --------------------------------------------------------------------------------------------
# Owner probe
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeSpec:
    """What "fixed" means, fixed at checkout by the owner.

    ``probe_public_key`` is the OpenSSH public key line the owner installs for ``ssh_user``. The
    matching private key stays with the probe runner; the agent and its process never see it.
    Exactly one of ``expect_substring`` and ``expect_sha256`` (hex digest of the full body) is set.
    """

    host: str
    ssh_port: int
    ssh_user: str
    probe_public_key: str
    url: str
    expect_substring: str | None = None
    expect_sha256: str | None = None

    def __post_init__(self) -> None:
        if not self.host or any(c.isspace() for c in self.host):
            raise ValueError("host must be a non-empty name without spaces")
        if not (1 <= int(self.ssh_port) <= 65535):
            raise ValueError("ssh_port must be 1-65535")
        if not self.ssh_user or self.ssh_user.startswith("-"):
            raise ValueError("ssh_user is invalid")
        if not self.url.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        if (self.expect_substring is None) == (self.expect_sha256 is None):
            raise ValueError("set exactly one of expect_substring and expect_sha256")
        if self.expect_sha256 is not None:
            digest = self.expect_sha256.lower()
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("expect_sha256 must be 64 hex characters")

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "ssh_port": int(self.ssh_port),
            "ssh_user": self.ssh_user,
            "probe_public_key": self.probe_public_key.strip(),
            "url": self.url,
            "expect_substring": self.expect_substring,
            "expect_sha256": self.expect_sha256.lower() if self.expect_sha256 else None,
        }

    def spec_hash(self) -> str:
        """sha256 hex of the canonical spec. Goes into the order's custom_id (max 255 chars)."""
        return sha256_hex(canonical_json(self.to_dict()))


@dataclass(frozen=True)
class ProbeResult:
    """One probe run. ``ok`` is ``ssh_ok and http_ok``. ``at`` is epoch seconds."""

    ok: bool
    ssh_ok: bool
    http_ok: bool
    detail: str
    at: float


# --------------------------------------------------------------------------------------------
# Console, lease and firewall
# --------------------------------------------------------------------------------------------


class HoldRevoked(Exception):
    """The PayPal hold behind the lease is gone (voided, expired, missing, or Stop pressed).

    Raised by ``HoldLease.require()``. Once raised, the session is over: no further input.
    Re-exported by ``keyhold.pay.gate``.
    """


@runtime_checkable
class Console(Protocol):
    """A server's web console: a screen and a keyboard. Re-exported by ``keyhold.console.base``.

    Key names for ``press`` are QEMU QKeyCode names (``ret``, ``esc``, ``tab``, ``ctrl``, ``alt``,
    ``delete``, ``f2``, ``a``...); a list is one chord pressed together, for example
    ``["ctrl", "alt", "delete"]``.
    """

    def screenshot(self) -> bytes:
        """PNG bytes of the current screen."""
        ...

    def type_text(self, text: str) -> None: ...

    def press(self, keys: list[str]) -> None: ...

    def power_cycle(self) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class Lease(Protocol):
    """Permission to send input, backed by a live PayPal authorization (see ``keyhold.pay.gate``)."""

    def require(self) -> None:
        """Return if input is allowed right now; raise ``HoldRevoked`` otherwise."""
        ...


@dataclass(frozen=True)
class FirewallVerdict:
    allowed: bool
    rule: str | None = None  # short rule name, for example "mkfs" or "shadow-read"
    reason: str = ""


@runtime_checkable
class Firewall(Protocol):
    """Keystroke firewall. Stateful: it tracks the current command line across calls, so a command
    split over several ``type`` calls is still caught when Enter is pressed. A refused input is
    not sent to the console."""

    def inspect_type(self, text: str) -> FirewallVerdict: ...

    def inspect_keys(self, keys: list[str]) -> FirewallVerdict: ...


# --------------------------------------------------------------------------------------------
# Model and agent
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelReply:
    """One model turn.

    ``content`` is the list of API content blocks as plain dicts (``block.to_dict()``), including
    ``thinking`` blocks; the agent loop appends it unchanged as the assistant message.
    ``stop_reason`` is the API value: end_turn, tool_use, max_tokens, pause_turn, refusal, ...
    """

    stop_reason: str
    content: list[dict[str, Any]]
    model: str = ""
    refusal_category: str | None = None
    input_tokens: int = 0  # input not read from or written to the prompt cache
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0  # input written to the prompt cache
    cache_read_input_tokens: int = 0  # input read from the prompt cache


@runtime_checkable
class ModelClient(Protocol):
    """What the agent loop and the quote need from a model. ``keyhold.agent.model`` provides the
    Claude implementation and a scripted ``FakeModel``."""

    def respond(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> ModelReply:
        """Send one request. With ``output_schema`` the reply's text block is JSON matching it."""
        ...


class AgentOutcome(str, enum.Enum):
    DONE = "done"  # agent called finished(summary); the probe decides whether it worked
    GAVE_UP = "gave_up"  # agent called give_up(reason)
    TURN_LIMIT = "turn_limit"
    REVOKED = "revoked"  # HoldRevoked: the hold is gone, input stopped
    ERROR = "error"  # model refusal, API failure or a bug


@dataclass(frozen=True)
class AgentResult:
    outcome: AgentOutcome
    reason: str
    turns: int


@dataclass(frozen=True)
class AgentEvent:
    """Something the agent did or was refused, streamed to the status page.

    ``kind``: screenshot, type, key, power_cycle, firewall_refusal, give_up, finished, revoked,
    model_refusal, error, note.
    """

    kind: str
    detail: str
    turn: int
    at: float
    data: Mapping[str, Any] = field(default_factory=dict)


EventSink = Callable[[AgentEvent], None]


# --------------------------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------------------------


@runtime_checkable
class Clock(Protocol):
    """Injectable time so TTLs, the 30 s flapping rule and the warranty schedule are testable."""

    def now(self) -> float:
        """Epoch seconds."""
        ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)
