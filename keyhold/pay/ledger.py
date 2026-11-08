"""SQLite ledger: rescues, the state machine, the outbox, events and webhook intake.

States and legal moves are ``keyhold.types.RescueState`` and ``ALLOWED_TRANSITIONS``. An outbox
row (op, request_id, status) is written and committed before every PayPal call, with
``request_id = keyhold.types.request_id_for(rescue_id, op)``; at most one money operation is in
flight per rescue (a partial unique index enforces it), and each operation happens at most once
per rescue (a unique index on (rescue_id, op)).

Two classes:

- ``Ledger`` stores everything and refuses anything that would break a money invariant:
  captured <= authorized <= quote, one capture per rescue, refund <= captured, never void after
  capture, state only moves along ``ALLOWED_TRANSITIONS`` (``transition``) or forward by rank
  (``advance``, used by webhooks and the reconciler).
- ``Payments`` runs every PayPal money operation for a rescue: outbox row first, then the call
  with the outbox request id, then one transaction that records the result, moves the state and
  closes the outbox row. A crash anywhere in between leaves a PENDING row that
  ``keyhold.pay.reconciler`` replays with the same request id.

Typical use by the orchestrator::

    ledger = Ledger(path)
    pay = Payments(ledger, gateway)
    rec = ledger.create_rescue(quote, probe=spec)
    order = pay.create_hold(rec.id)          # QUOTED -> HOLD_PENDING; browser approves order.id
    pay.authorize(rec.id)                    # HOLD_PENDING -> HELD
    ledger.transition(rec.id, RescueState.ACTIVE)
    ledger.transition(rec.id, RescueState.PROBING)
    pay.capture(rec.id)                      # PROBING -> CAPTURED   (or pay.void(rec.id, reason))
    ledger.transition(rec.id, RescueState.WARRANTY_OK)   # or pay.refund(rec.id, reason)
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from keyhold.pay.client import from_record, is_expired, to_record
from keyhold.types import (
    ALLOWED_TRANSITIONS,
    NO_MONEY_CAPTURE,
    STATE_RANK,
    Authorization,
    Capture,
    Clock,
    MoneyOp,
    Order,
    OutboxStatus,
    PaymentGateway,
    PayPalError,
    ProbeSpec,
    Quote,
    Refund,
    RescueState,
    SystemClock,
    canonical_json,
    money,
    request_id_for,
    request_id_round,
)

__all__ = [
    "AUTH_STATUS_RANK",
    "CAPTURE_STATUS_RANK",
    "PRE_CAPTURE_STATES",
    "REFUND_STATUS_RANK",
    "EventRecord",
    "IllegalTransition",
    "InvariantViolation",
    "Ledger",
    "LedgerError",
    "OpFailed",
    "OpInFlight",
    "OutboxEntry",
    "Payments",
    "RefundDeferred",
    "RescueRecord",
    "UnknownRescue",
    "WebhookDelivery",
]

PRE_CAPTURE_STATES: frozenset[RescueState] = frozenset(
    {
        RescueState.QUOTED,
        RescueState.HOLD_PENDING,
        RescueState.HELD,
        RescueState.ACTIVE,
        RescueState.PROBING,
    }
)
CAPTURED_STATES: frozenset[RescueState] = frozenset(
    {RescueState.CAPTURED, RescueState.WARRANTY_OK, RescueState.REFUNDED}
)

# PayPal status strings only move to a higher rank; a late or replayed event never regresses them.
AUTH_STATUS_RANK: Mapping[str, int] = {
    "PENDING": 1,
    "CREATED": 2,
    "PARTIALLY_CAPTURED": 3,
    "CAPTURED": 4,
    "VOIDED": 4,
    "DENIED": 4,
}
CAPTURE_STATUS_RANK: Mapping[str, int] = {
    "PENDING": 1,
    "COMPLETED": 2,
    "DECLINED": 2,
    "DENIED": 2,
    "FAILED": 2,
    "PARTIALLY_REFUNDED": 3,
    "REFUNDED": 4,
    "REVERSED": 4,
}
REFUND_STATUS_RANK: Mapping[str, int] = {"PENDING": 1, "COMPLETED": 2, "FAILED": 2, "CANCELLED": 2}

WEBHOOK_TICKS = ("auth", "void", "capture", "refund")


class LedgerError(Exception):
    """A ledger rule refused the request."""


class UnknownRescue(LedgerError):
    pass


class IllegalTransition(LedgerError):
    """The state machine does not allow this move, or its guard failed."""


class InvariantViolation(LedgerError):
    """Recording this would break a money invariant (for example a second capture)."""


class OpInFlight(LedgerError):
    """Another money operation for this rescue is still PENDING in the outbox."""


class OpFailed(LedgerError):
    """This money operation already got a final, non-retryable answer from PayPal."""

    def __init__(self, message: str, issue: str | None = None) -> None:
        super().__init__(message)
        self.issue = issue


class RefundDeferred(LedgerError):
    """The refund waits: PayPal cannot refund a capture that is still PENDING. The decision is
    stored (``refund_due``) and the reconciler sends the refund once the capture completes."""


def _dec(value: Any) -> Decimal | None:
    return None if value is None else money(str(value))


def _rank_up(current: str | None, new: str | None, ranks: Mapping[str, int]) -> str | None:
    """The status to keep: ``new`` only when it ranks strictly higher than ``current``."""
    if not new:
        return current
    if not current:
        return new
    return new if ranks.get(new, 0) > ranks.get(current, 0) else current


@dataclass(frozen=True)
class RescueRecord:
    id: str
    state: RescueState
    quote_amount: Decimal
    quote_hash: str
    quote: dict[str, Any]
    probe_spec_hash: str | None
    probe_spec: dict[str, Any] | None
    invoice_id: str
    order_id: str | None
    order_status: str | None
    authorization_id: str | None
    authorized_amount: Decimal | None
    auth_status: str | None
    auth_expiration_time: str | None
    capture_id: str | None
    captured_amount: Decimal | None
    capture_status: str | None
    refund_id: str | None
    refunded_amount: Decimal | None
    refund_status: str | None
    end_reason: str | None
    auth_webhook_at: float | None
    void_webhook_at: float | None
    capture_webhook_at: float | None
    refund_webhook_at: float | None
    created_at: float
    updated_at: float

    @property
    def terminal(self) -> bool:
        return self.state.terminal

    @property
    def capture_invoice_id(self) -> str:
        """Invoice id sent with the capture. Distinct from the order's, so a merchant account
        that blocks duplicate invoice ids does not refuse the capture."""
        return f"{self.invoice_id}-cap"


@dataclass(frozen=True)
class OutboxEntry:
    id: int
    rescue_id: str
    op: MoneyOp
    request_id: str
    status: OutboxStatus
    args: dict[str, Any]
    note: str
    attempts: int
    result: dict[str, Any] | None
    error: str | None
    created_at: float
    updated_at: float

    def result_object(self) -> Order | Authorization | Capture | Refund | None:
        return from_record(self.result)

    @property
    def round(self) -> int:
        """1 for ``{op}-{rescue_id}``, n for the n-th request id (``{op}-{rescue_id}-n``)."""
        base = request_id_for(self.rescue_id, self.op)
        if self.request_id == base:
            return 1
        tail = self.request_id[len(base) + 1 :]
        return int(tail) if self.request_id.startswith(base + "-") and tail.isdigit() else 1


@dataclass(frozen=True)
class EventRecord:
    id: int
    rescue_id: str | None
    kind: str
    detail: str
    data: dict[str, Any]
    at: float


@dataclass(frozen=True)
class WebhookDelivery:
    id: int
    raw: bytes
    headers: dict[str, str]
    received_at: float
    event_id: str | None
    event_type: str | None
    verified: bool | None
    outcome: str | None
    rescue_id: str | None
    verify_attempts: int = 0


_SCHEMA = """
CREATE TABLE IF NOT EXISTS rescues (
    id                  TEXT PRIMARY KEY,
    state               TEXT NOT NULL,
    quote_amount        TEXT NOT NULL,
    quote_hash          TEXT NOT NULL,
    quote_json          TEXT NOT NULL,
    probe_spec_hash     TEXT,
    probe_spec_json     TEXT,
    invoice_id          TEXT NOT NULL UNIQUE,
    order_id            TEXT UNIQUE,
    order_status        TEXT,
    authorization_id    TEXT UNIQUE,
    authorized_amount   TEXT,
    auth_status         TEXT,
    auth_expiration_time TEXT,
    capture_id          TEXT UNIQUE,
    captured_amount     TEXT,
    capture_status      TEXT,
    refund_id           TEXT UNIQUE,
    refunded_amount     TEXT,
    refund_status       TEXT,
    end_reason          TEXT,
    auth_webhook_at     REAL,
    void_webhook_at     REAL,
    capture_webhook_at  REAL,
    refund_webhook_at   REAL,
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rescue_id   TEXT NOT NULL REFERENCES rescues(id),
    op          TEXT NOT NULL,
    request_id  TEXT NOT NULL UNIQUE,
    status      TEXT NOT NULL CHECK (status IN ('PENDING', 'DONE', 'FAILED')),
    args_json   TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    attempts    INTEGER NOT NULL DEFAULT 0,
    result_json TEXT,
    error       TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    UNIQUE (rescue_id, op)
);
CREATE UNIQUE INDEX IF NOT EXISTS outbox_one_in_flight ON outbox(rescue_id) WHERE status = 'PENDING';
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rescue_id   TEXT,
    kind        TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    data_json   TEXT NOT NULL DEFAULT '{}',
    at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_rescue ON events(rescue_id, id);
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    raw         BLOB NOT NULL,
    headers_json TEXT NOT NULL,
    received_at REAL NOT NULL,
    event_id    TEXT,
    event_type  TEXT,
    verified    INTEGER,
    outcome     TEXT,
    rescue_id   TEXT
);
CREATE INDEX IF NOT EXISTS deliveries_by_rescue ON webhook_deliveries(rescue_id, id);
CREATE INDEX IF NOT EXISTS deliveries_by_outcome ON webhook_deliveries(outcome);
CREATE TABLE IF NOT EXISTS webhook_events (
    event_id    TEXT PRIMARY KEY,
    delivery_id INTEGER NOT NULL REFERENCES webhook_deliveries(id),
    applied_at  REAL NOT NULL
);
"""

# Headers kept with a webhook delivery. Everything else (cookies, proxies' additions) is dropped.
_KEPT_HEADER_PREFIXES = ("paypal-", "content-type", "user-agent", "correlation-id")


class Ledger:
    """The SQLite ledger. Safe to share between threads; each method is atomic."""

    def __init__(self, path: str | os.PathLike[str], *, clock: Clock | None = None) -> None:
        self.path = str(path)
        self._clock: Clock = clock or SystemClock()
        self._lock = threading.RLock()
        self._depth = 0
        self._tx_thread: int | None = None  # the thread that holds the open transaction
        self._conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 30000")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = FULL")
        self._conn.executescript(_SCHEMA)
        self._add_column("webhook_deliveries", "verify_attempts", "INTEGER NOT NULL DEFAULT 0")

    def _add_column(self, table: str, column: str, decl: str) -> None:
        """Add a column to a ledger made by an older version (SQLite has no ADD COLUMN IF NOT EXISTS)."""
        names = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
        if column not in names:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def in_transaction(self) -> bool:
        """True when the calling thread is inside ``transaction()``. Another thread's open
        transaction does not count: that thread holds the lock, not this one."""
        return self._depth > 0 and self._tx_thread == threading.get_ident()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One atomic unit. Nested blocks become savepoints, so an inner failure that the outer
        block catches rolls back only the inner writes."""
        with self._lock:
            depth = self._depth
            if depth == 0:
                self._conn.execute("BEGIN IMMEDIATE")
                self._tx_thread = threading.get_ident()
            else:
                self._conn.execute(f"SAVEPOINT sp{depth}")
            self._depth += 1
            try:
                yield self._conn
            except BaseException:
                self._depth -= 1
                if depth == 0:
                    self._tx_thread = None
                    self._conn.execute("ROLLBACK")
                else:
                    self._conn.execute(f"ROLLBACK TO sp{depth}")
                    self._conn.execute(f"RELEASE sp{depth}")
                raise
            else:
                self._depth -= 1
                if depth == 0:
                    self._tx_thread = None
                    self._conn.execute("COMMIT")
                else:
                    self._conn.execute(f"RELEASE sp{depth}")

    def _now(self) -> float:
        return self._clock.now()

    # -- rescues ---------------------------------------------------------------------------------

    def create_rescue(
        self,
        quote: Quote,
        *,
        rescue_id: str | None = None,
        probe: ProbeSpec | None = None,
    ) -> RescueRecord:
        """Store a free-look quote as a new rescue in QUOTED."""
        if not quote.hash_ok():
            raise InvariantViolation("quote hash does not match its contents")
        rid = rescue_id or str(uuid.uuid4())
        request_id_for(rid, MoneyOp.CAPTURE)  # validates the id
        if len(rid) > 64 or any(c.isspace() for c in rid):
            raise ValueError("rescue_id must be at most 64 characters without spaces")
        quote_json = {
            "fault_class": quote.fault_class,
            "evidence": list(quote.evidence),
            "tier": quote.tier.value,
            "plan": quote.plan,
            "amount": str(quote.amount),
            "quote_hash": quote.quote_hash,
        }
        now = self._now()
        with self.transaction() as db:
            db.execute(
                "INSERT INTO rescues (id, state, quote_amount, quote_hash, quote_json, probe_spec_hash,"
                " probe_spec_json, invoice_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    rid,
                    RescueState.QUOTED.value,
                    str(money(quote.amount)),
                    quote.quote_hash,
                    canonical_json(quote_json),
                    probe.spec_hash() if probe else None,
                    canonical_json(probe.to_dict()) if probe else None,
                    f"keyhold-{rid}",
                    now,
                    now,
                ),
            )
            self.log(rid, "quoted", f"{quote.fault_class} {quote.tier.value} ${quote.amount}", quote_json)
        return self.get(rid)

    def set_probe(self, rescue_id: str, probe: ProbeSpec) -> RescueRecord:
        """Fix the owner probe. Allowed only before the hold exists; its hash becomes custom_id."""
        with self.transaction() as db:
            rec = self.get(rescue_id)
            if rec.state is not RescueState.QUOTED or rec.order_id:
                raise LedgerError("the probe is fixed at checkout and cannot change after the hold")
            db.execute(
                "UPDATE rescues SET probe_spec_hash=?, probe_spec_json=?, updated_at=? WHERE id=?",
                (probe.spec_hash(), canonical_json(probe.to_dict()), self._now(), rescue_id),
            )
            self.log(rescue_id, "probe_fixed", probe.spec_hash(), {"spec_hash": probe.spec_hash()})
        return self.get(rescue_id)

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> RescueRecord:
        return RescueRecord(
            id=row["id"],
            state=RescueState(row["state"]),
            quote_amount=money(row["quote_amount"]),
            quote_hash=row["quote_hash"],
            quote=json.loads(row["quote_json"]),
            probe_spec_hash=row["probe_spec_hash"],
            probe_spec=json.loads(row["probe_spec_json"]) if row["probe_spec_json"] else None,
            invoice_id=row["invoice_id"],
            order_id=row["order_id"],
            order_status=row["order_status"],
            authorization_id=row["authorization_id"],
            authorized_amount=_dec(row["authorized_amount"]),
            auth_status=row["auth_status"],
            auth_expiration_time=row["auth_expiration_time"],
            capture_id=row["capture_id"],
            captured_amount=_dec(row["captured_amount"]),
            capture_status=row["capture_status"],
            refund_id=row["refund_id"],
            refunded_amount=_dec(row["refunded_amount"]),
            refund_status=row["refund_status"],
            end_reason=row["end_reason"],
            auth_webhook_at=row["auth_webhook_at"],
            void_webhook_at=row["void_webhook_at"],
            capture_webhook_at=row["capture_webhook_at"],
            refund_webhook_at=row["refund_webhook_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get(self, rescue_id: str) -> RescueRecord:
        with self._lock:
            row = self._conn.execute("SELECT * FROM rescues WHERE id=?", (rescue_id,)).fetchone()
        if row is None:
            raise UnknownRescue(f"no rescue {rescue_id!r}")
        return self._row_to_record(row)

    def find(
        self,
        *,
        order_id: str | None = None,
        authorization_id: str | None = None,
        capture_id: str | None = None,
        refund_id: str | None = None,
        invoice_id: str | None = None,
    ) -> RescueRecord | None:
        """The rescue that owns one of these PayPal ids (first match in the order given)."""
        for column, value in (
            ("capture_id", capture_id),
            ("refund_id", refund_id),
            ("authorization_id", authorization_id),
            ("order_id", order_id),
            ("invoice_id", invoice_id),
        ):
            if not value:
                continue
            with self._lock:
                row = self._conn.execute(f"SELECT * FROM rescues WHERE {column}=?", (value,)).fetchone()
            if row is not None:
                return self._row_to_record(row)
        return None

    def rescues(self, states: Iterable[RescueState] | None = None) -> list[RescueRecord]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM rescues ORDER BY created_at, id").fetchall()
        wanted = None if states is None else {RescueState(s) for s in states}
        out = [self._row_to_record(r) for r in rows]
        return [r for r in out if wanted is None or r.state in wanted]

    def open_rescues(self) -> list[RescueRecord]:
        """Every rescue that is not in a terminal state (CAPTURED included: warranty runs)."""
        return [r for r in self.rescues() if not r.state.terminal]

    def orphan_holds(self) -> list[RescueRecord]:
        """Rescues that ended VOIDED while PayPal may still hold money for them: the ledger knows
        an authorization that is not voided, captured or expired (for example one PayPal created
        after the rescue gave up on it, or one left behind by a declined capture)."""
        now = self._now()
        return [
            r
            for r in self.rescues([RescueState.VOIDED])
            if r.authorization_id
            and r.auth_status in ("CREATED", "PENDING")
            and not is_expired(r.auth_expiration_time, now)
        ]

    def refund_due(self, rescue_id: str) -> str | None:
        """The reason the warranty decided to refund this rescue, or None. Stored as an event
        before any refund is sent, so the decision survives a refusal or a restart."""
        due = self.events(rescue_id, kind="refund_due")
        return due[0].detail if due else None

    def mark_refund_due(self, rescue_id: str, reason: str) -> None:
        with self.transaction():
            if self.refund_due(rescue_id) is None:
                self.log(rescue_id, "refund_due", reason, {})

    def attention(self, rescue_id: str, detail: str, data: Mapping[str, Any] | None = None) -> None:
        """Flag a rescue for a person to look at (shown on the status page and /judge)."""
        self.log(rescue_id, "attention", detail, data)

    def _set(self, rescue_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = self._now()
        cols = ", ".join(f"{k}=?" for k in fields)
        values = [str(v) if isinstance(v, Decimal) else v for v in fields.values()]
        with self.transaction() as db:
            db.execute(f"UPDATE rescues SET {cols} WHERE id=?", (*values, rescue_id))

    # -- state machine ---------------------------------------------------------------------------

    def _guard(self, rec: RescueRecord, target: RescueState) -> None:
        """Facts that must hold before entering ``target``."""
        if target is RescueState.HOLD_PENDING and not rec.order_id:
            raise IllegalTransition("HOLD_PENDING needs a PayPal order")
        if target is RescueState.HELD:
            if rec.auth_status != "CREATED":
                raise IllegalTransition(f"HELD needs an authorization in CREATED, not {rec.auth_status}")
            if rec.authorized_amount != rec.quote_amount:
                raise IllegalTransition("HELD needs an authorization for exactly the quoted amount")
            if is_expired(rec.auth_expiration_time, self._now()):
                raise IllegalTransition("HELD needs an authorization that has not expired")
        if target is RescueState.CAPTURED:
            if not rec.capture_id:
                raise IllegalTransition("CAPTURED needs a recorded capture")
            if rec.capture_status in NO_MONEY_CAPTURE:
                raise IllegalTransition(f"PayPal answered the capture with {rec.capture_status}; no money moved")
        if target is RescueState.REFUNDED and not self.fully_refunded(rec):
            raise IllegalTransition("REFUNDED needs a completed refund of the whole capture")
        if target is RescueState.WARRANTY_OK:
            if rec.capture_status not in ("COMPLETED", "PARTIALLY_REFUNDED"):
                raise IllegalTransition(
                    f"the warranty can only end OK on a completed capture, not {rec.capture_status}"
                )
            refund = self.get_op(rec.id, MoneyOp.REFUND)
            if self.refund_due(rec.id) or (refund is not None and refund.status is not OutboxStatus.FAILED):
                raise IllegalTransition("a refund was decided for this rescue; the warranty cannot end OK")
        if target is RescueState.VOIDED:
            declined = rec.capture_status in NO_MONEY_CAPTURE
            if rec.capture_id is not None and not declined:
                raise IllegalTransition("never void after capture")
            cap = self.get_op(rec.id, MoneyOp.CAPTURE)
            if cap is not None and cap.status is OutboxStatus.PENDING:
                raise IllegalTransition("a capture is pending for this rescue; it cannot be voided")
            if cap is not None and cap.status is OutboxStatus.DONE and not declined:
                raise IllegalTransition("a capture is done for this rescue; it cannot be voided")

    @staticmethod
    def fully_refunded(rec: RescueRecord) -> bool:
        """PayPal shows the whole capture refunded and completed (not pending, not partial)."""
        if rec.captured_amount is None:
            return False
        if rec.capture_status == "REFUNDED":
            return True
        return rec.refund_status == "COMPLETED" and rec.refunded_amount == rec.captured_amount

    def transition(self, rescue_id: str, target: RescueState, *, reason: str = "", source: str = "app") -> RescueRecord:
        """Move along ``ALLOWED_TRANSITIONS``. Moving to the current state is a no-op.

        Refused with ``OpInFlight`` while a money operation for the rescue is PENDING: its outcome
        decides where the rescue goes (for example no keyboard while a void is in flight).
        """
        target = RescueState(target)
        with self.transaction() as db:
            rec = self.get(rescue_id)
            if rec.state is target:
                return rec
            if target not in ALLOWED_TRANSITIONS[rec.state]:
                raise IllegalTransition(f"{rec.state.value} -> {target.value} is not allowed")
            in_flight = db.execute(
                "SELECT op FROM outbox WHERE rescue_id=? AND status='PENDING'", (rescue_id,)
            ).fetchone()
            if in_flight is not None:
                raise OpInFlight(f"{in_flight['op']} is still in flight for this rescue")
            self._guard(rec, target)
            self._move(rec, target, reason, source)
        return self.get(rescue_id)

    def advance(self, rescue_id: str, target: RescueState, *, reason: str = "", source: str = "") -> bool:
        """Move forward by rank to what PayPal says happened (webhooks, reconciler).

        Returns True if the state changed. Never moves backwards, never leaves a terminal state,
        and never swaps CAPTURED and VOIDED (equal rank). Guards still apply.
        """
        target = RescueState(target)
        if target not in (RescueState.HELD, RescueState.CAPTURED, RescueState.REFUNDED, RescueState.VOIDED):
            raise ValueError(f"advance does not move to {target.value}")
        with self.transaction():
            rec = self.get(rescue_id)
            if rec.state.terminal or STATE_RANK[target] <= STATE_RANK[rec.state]:
                return False
            if target is RescueState.REFUNDED and rec.state is not RescueState.CAPTURED:
                return False
            self._guard(rec, target)
            self._move(rec, target, reason, source)
        return True

    def _move(self, rec: RescueRecord, target: RescueState, reason: str, source: str) -> None:
        fields: dict[str, Any] = {"state": target.value}
        if target.terminal or target is RescueState.VOIDED:
            fields["end_reason"] = reason or rec.end_reason
        self._set(rec.id, **fields)
        self.log(
            rec.id,
            "state",
            f"{rec.state.value} -> {target.value}" + (f": {reason}" if reason else ""),
            {"from": rec.state.value, "to": target.value, "reason": reason, "source": source},
        )

    # -- PayPal facts ------------------------------------------------------------------------------

    def record_order(self, rescue_id: str, order: Order) -> RescueRecord:
        with self.transaction():
            rec = self.get(rescue_id)
            if rec.order_id and rec.order_id != order.id:
                raise InvariantViolation(f"rescue already has order {rec.order_id}")
            if money(order.amount) not in (money("0"), rec.quote_amount):  # 0.00 means PayPal left it out
                raise InvariantViolation("order amount differs from the quote")
            self._set(rescue_id, order_id=order.id, order_status=order.status)
        return self.get(rescue_id)

    def record_authorization(self, rescue_id: str, auth: Authorization) -> RescueRecord:
        """Store the authorization. Its status only moves up by rank."""
        with self.transaction():
            rec = self.get(rescue_id)
            if rec.authorization_id and rec.authorization_id != auth.id:
                raise InvariantViolation(f"rescue already has authorization {rec.authorization_id}")
            if rec.order_id and auth.order_id and auth.order_id != rec.order_id:
                raise InvariantViolation("authorization belongs to another order")
            self._set(
                rescue_id,
                authorization_id=auth.id,
                authorized_amount=money(auth.amount) if rec.authorized_amount is None else rec.authorized_amount,
                auth_status=_rank_up(rec.auth_status, auth.status, AUTH_STATUS_RANK),
                auth_expiration_time=auth.expiration_time or rec.auth_expiration_time,
            )
        return self.get(rescue_id)

    def record_auth_status(self, rescue_id: str, status: str) -> None:
        with self.transaction():
            rec = self.get(rescue_id)
            self._set(rescue_id, auth_status=_rank_up(rec.auth_status, status, AUTH_STATUS_RANK))

    def record_capture(self, rescue_id: str, capture: Capture) -> RescueRecord:
        """Store the one capture of this rescue. Refuses a second capture and any capture above
        the quote or the authorized amount."""
        amount = money(capture.amount)
        with self.transaction():
            rec = self.get(rescue_id)
            if rec.capture_id and rec.capture_id != capture.id:
                raise InvariantViolation(f"second capture {capture.id}; rescue already captured {rec.capture_id}")
            if rec.state is RescueState.VOIDED:
                raise InvariantViolation("capture reported for a voided rescue")
            if capture.authorization_id and rec.authorization_id and capture.authorization_id != rec.authorization_id:
                raise InvariantViolation("capture belongs to another authorization")
            if amount <= 0:
                raise InvariantViolation("capture amount must be positive")
            if amount > rec.quote_amount:
                raise InvariantViolation("capture exceeds the quote")
            if rec.authorized_amount is not None and amount > rec.authorized_amount:
                raise InvariantViolation("capture exceeds the authorized amount")
            if rec.captured_amount is not None and amount != rec.captured_amount:
                raise InvariantViolation("capture amount changed")
            self._set(
                rescue_id,
                capture_id=capture.id,
                captured_amount=amount,
                capture_status=_rank_up(rec.capture_status, capture.status, CAPTURE_STATUS_RANK),
            )
            if capture.status in NO_MONEY_CAPTURE:
                pass  # no money moved: what PayPal does with the authorization is read back, not assumed
            elif capture.final_capture or rec.auth_status in (None, "CREATED", "PENDING"):
                # final_capture releases the remainder: PayPal moves the authorization to CAPTURED.
                self.record_auth_status(rescue_id, "CAPTURED" if capture.final_capture else "PARTIALLY_CAPTURED")
        return self.get(rescue_id)

    def record_capture_status(self, rescue_id: str, status: str) -> None:
        with self.transaction():
            rec = self.get(rescue_id)
            self._set(rescue_id, capture_status=_rank_up(rec.capture_status, status, CAPTURE_STATUS_RANK))

    def record_refund(self, rescue_id: str, refund: Refund, *, total: Decimal | str | None = None) -> RescueRecord:
        """Store a refund of this rescue's capture.

        ``total`` is everything refunded or being refunded on the capture (COMPLETED and PENDING
        refunds), when the caller knows it; otherwise the refund's own amount. A later refund
        replaces an earlier one only when ``total`` accounts for both, or when the earlier one
        FAILED or was CANCELLED. Refuses a total above the captured amount.

        The capture's status moves only for a COMPLETED refund: PayPal may still fail a PENDING one.
        """
        amount = money(refund.amount)
        with self.transaction():
            rec = self.get(rescue_id)
            if rec.capture_id is None or rec.captured_amount is None:
                raise InvariantViolation("refund without a capture")
            if refund.capture_id and refund.capture_id != rec.capture_id:
                raise InvariantViolation("refund belongs to another capture")
            same = rec.refund_id == refund.id
            if rec.refund_id and not same and total is None and rec.refund_status not in ("FAILED", "CANCELLED"):
                raise InvariantViolation(f"second refund {refund.id}; rescue already refunded {rec.refund_id}")
            refunded = money(total) if total is not None else amount
            if amount <= 0 or refunded < 0 or refunded > rec.captured_amount:
                raise InvariantViolation("refund exceeds the captured amount")
            status = _rank_up(rec.refund_status, refund.status, REFUND_STATUS_RANK) if same else refund.status
            self._set(rescue_id, refund_id=refund.id, refunded_amount=refunded, refund_status=status)
            if status == "COMPLETED":
                capture_status = "REFUNDED" if refunded == rec.captured_amount else "PARTIALLY_REFUNDED"
                self.record_capture_status(rescue_id, capture_status)
        return self.get(rescue_id)

    def mark_webhook_tick(self, rescue_id: str, which: str) -> None:
        """The verified webhook (second tick) for ``which`` in auth, void, capture, refund."""
        if which not in WEBHOOK_TICKS:
            raise ValueError(f"unknown tick {which!r}")
        with self.transaction():
            rec = self.get(rescue_id)
            if getattr(rec, f"{which}_webhook_at") is None:
                self._set(rescue_id, **{f"{which}_webhook_at": self._now()})

    # -- outbox ----------------------------------------------------------------------------------

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> OutboxEntry:
        return OutboxEntry(
            id=row["id"],
            rescue_id=row["rescue_id"],
            op=MoneyOp(row["op"]),
            request_id=row["request_id"],
            status=OutboxStatus(row["status"]),
            args=json.loads(row["args_json"]),
            note=row["note"],
            attempts=row["attempts"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_op(self, rescue_id: str, op: MoneyOp) -> OutboxEntry | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM outbox WHERE rescue_id=? AND op=?", (rescue_id, MoneyOp(op).value)
            ).fetchone()
        return self._row_to_entry(row) if row else None

    def get_op_by_id(self, entry_id: int) -> OutboxEntry:
        with self._lock:
            row = self._conn.execute("SELECT * FROM outbox WHERE id=?", (entry_id,)).fetchone()
        if row is None:
            raise LedgerError(f"no outbox row {entry_id}")
        return self._row_to_entry(row)

    def ops(self, rescue_id: str | None = None) -> list[OutboxEntry]:
        with self._lock:
            if rescue_id is None:
                rows = self._conn.execute("SELECT * FROM outbox ORDER BY id").fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM outbox WHERE rescue_id=? ORDER BY id", (rescue_id,)).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def pending_ops(self) -> list[OutboxEntry]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM outbox WHERE status='PENDING' ORDER BY id").fetchall()
        return [self._row_to_entry(r) for r in rows]

    def begin_op(
        self,
        rescue_id: str,
        op: MoneyOp,
        args: Mapping[str, Any],
        *,
        allowed_states: Iterable[RescueState],
        note: str = "",
        reopen: bool = False,
    ) -> OutboxEntry:
        """Write (and commit) the outbox row for ``op`` before PayPal is called.

        - A PENDING row for this op is resumed (same request id, same args).
        - A DONE row is returned as is; the caller must not call PayPal again.
        - A FAILED row raises ``OpFailed``.
        - With ``reopen``, a FAILED or DONE row is started again as the next round: a new request
          id (``{op}-{rescue_id}-n``) and ``args``. Only for an operation PayPal definitely did not
          carry out (or whose result PayPal has since undone, such as a refund that FAILED).
        - Another op PENDING for this rescue raises ``OpInFlight``.
        """
        op = MoneyOp(op)
        args_json = canonical_json(dict(args))
        allowed = {RescueState(s) for s in allowed_states}
        with self.transaction() as db:
            rec = self.get(rescue_id)
            existing = self.get_op(rescue_id, op)
            if existing is not None and not (reopen and existing.status is not OutboxStatus.PENDING):
                if existing.status is OutboxStatus.FAILED:
                    raise OpFailed(f"{op.value} already failed for this rescue: {existing.error}", existing.error)
                if existing.status is OutboxStatus.PENDING and canonical_json(existing.args) != args_json:
                    raise LedgerError(f"{op.value} is pending with different arguments {existing.args}")
                return existing
            if rec.state not in allowed:
                raise IllegalTransition(f"cannot {op.value} while {rec.state.value}")
            in_flight = db.execute(
                "SELECT op FROM outbox WHERE rescue_id=? AND status='PENDING'", (rescue_id,)
            ).fetchone()
            if in_flight is not None:
                raise OpInFlight(f"{in_flight['op']} is still in flight for this rescue")
            if op is MoneyOp.VOID:
                cap = self.get_op(rescue_id, MoneyOp.CAPTURE)
                declined = rec.capture_status in NO_MONEY_CAPTURE
                if (rec.capture_id and not declined) or (
                    cap is not None and cap.status is not OutboxStatus.FAILED and not declined
                ):
                    raise IllegalTransition("never void after capture")
            if op is MoneyOp.CAPTURE:
                void = self.get_op(rescue_id, MoneyOp.VOID)
                if void is not None and void.status is not OutboxStatus.FAILED:
                    raise IllegalTransition("this rescue's hold was voided")
            if existing is not None:
                return self._next_round(existing, args, note=note or existing.note, error=existing.error)
            now = self._now()
            rid = request_id_for(rescue_id, op)
            cur = db.execute(
                "INSERT INTO outbox (rescue_id, op, request_id, status, args_json, note, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (rescue_id, op.value, rid, OutboxStatus.PENDING.value, args_json, note, now, now),
            )
            self.log(
                rescue_id, "outbox", f"{op.value} pending, PayPal-Request-Id {rid}", {"op": op.value, "request_id": rid}
            )
            entry_id = cur.lastrowid
        return self.get_op_by_id(int(entry_id))

    MAX_ROUND = 999

    def _next_round(
        self, entry: OutboxEntry, args: Mapping[str, Any] | None = None, *, note: str | None = None,
        error: str | None = None,
    ) -> OutboxEntry:
        """Start ``entry`` again, PENDING, under the next request id (see ``begin_op``)."""
        nxt = min(entry.round + 1, self.MAX_ROUND)
        rid = request_id_round(entry.rescue_id, entry.op, nxt)
        with self.transaction() as db:
            db.execute(
                "UPDATE outbox SET status='PENDING', request_id=?, args_json=?, note=?, error=?, result_json=NULL,"
                " updated_at=? WHERE id=?",
                (
                    rid,
                    canonical_json(dict(entry.args if args is None else args)),
                    entry.note if note is None else note,
                    error,
                    self._now(),
                    entry.id,
                ),
            )
            self.log(
                entry.rescue_id,
                "outbox",
                f"{entry.op.value} round {nxt}, PayPal-Request-Id {rid}",
                {"op": entry.op.value, "request_id": rid, "round": nxt, "previous": entry.request_id},
            )
        return self.get_op_by_id(entry.id)

    def retry_op(self, entry_id: int, error: PayPalError | str, *, args: Mapping[str, Any] | None = None) -> OutboxEntry:
        """PayPal refused this round, but the operation is still wanted (for example a void while
        the authorization is still CREATED). The row stays PENDING under the next request id, so
        the replay is a new request rather than a replay of the refused one."""
        text = error.issue if isinstance(error, PayPalError) else str(error)
        with self.transaction() as db:
            entry = self.get_op_by_id(entry_id)
            if entry.status is not OutboxStatus.PENDING:
                return entry
            db.execute("UPDATE outbox SET attempts=attempts+1 WHERE id=?", (entry_id,))
            return self._next_round(self.get_op_by_id(entry_id), args, error=text)

    def complete_op(self, entry_id: int, result: Order | Authorization | Capture | Refund | None) -> None:
        with self.transaction() as db:
            entry = self.get_op_by_id(entry_id)
            if entry.status is OutboxStatus.DONE:
                return
            db.execute(
                "UPDATE outbox SET status='DONE', result_json=?, error=NULL, updated_at=? WHERE id=?",
                (json.dumps(to_record(result)) if result is not None else None, self._now(), entry_id),
            )

    def fail_op(self, entry_id: int, error: PayPalError | str) -> None:
        text = error.issue if isinstance(error, PayPalError) else str(error)
        with self.transaction() as db:
            db.execute(
                "UPDATE outbox SET status='FAILED', error=?, updated_at=? WHERE id=? AND status='PENDING'",
                (text, self._now(), entry_id),
            )

    def note_attempt(self, entry_id: int, error: PayPalError | str) -> None:
        """A retryable failure: the row stays PENDING for a replay with the same request id."""
        text = error.issue if isinstance(error, PayPalError) else str(error)
        with self.transaction() as db:
            db.execute(
                "UPDATE outbox SET attempts=attempts+1, error=?, updated_at=? WHERE id=?",
                (text, self._now(), entry_id),
            )

    # -- events ----------------------------------------------------------------------------------

    def log(self, rescue_id: str | None, kind: str, detail: str = "", data: Mapping[str, Any] | None = None) -> int:
        with self.transaction() as db:
            cur = db.execute(
                "INSERT INTO events (rescue_id, kind, detail, data_json, at) VALUES (?,?,?,?,?)",
                (rescue_id, kind, detail, canonical_json(dict(data or {})), self._now()),
            )
            return int(cur.lastrowid)

    def events(self, rescue_id: str | None = None, *, after_id: int = 0, kind: str | None = None) -> list[EventRecord]:
        sql = "SELECT * FROM events WHERE id > ?"
        params: list[Any] = [after_id]
        if rescue_id is not None:
            sql += " AND rescue_id = ?"
            params.append(rescue_id)
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY id", params).fetchall()
        return [
            EventRecord(r["id"], r["rescue_id"], r["kind"], r["detail"], json.loads(r["data_json"]), r["at"])
            for r in rows
        ]

    # -- webhook intake ----------------------------------------------------------------------------

    def store_delivery(self, headers: Mapping[str, str], raw: bytes) -> int:
        """Keep the raw body and PayPal headers of one delivery, before any verification."""
        kept = {str(k): str(v) for k, v in headers.items() if str(k).lower().startswith(_KEPT_HEADER_PREFIXES)}
        with self.transaction() as db:
            cur = db.execute(
                "INSERT INTO webhook_deliveries (raw, headers_json, received_at) VALUES (?,?,?)",
                (bytes(raw), json.dumps(kept, sort_keys=True), self._now()),
            )
            return int(cur.lastrowid)

    @staticmethod
    def _row_to_delivery(row: sqlite3.Row) -> WebhookDelivery:
        keys = row.keys()
        return WebhookDelivery(
            id=row["id"],
            raw=bytes(row["raw"]) if "raw" in keys and row["raw"] is not None else b"",
            headers=json.loads(row["headers_json"]) if "headers_json" in keys else {},
            received_at=row["received_at"],
            event_id=row["event_id"],
            event_type=row["event_type"],
            verified=None if row["verified"] is None else bool(row["verified"]),
            outcome=row["outcome"],
            rescue_id=row["rescue_id"],
            verify_attempts=int(row["verify_attempts"]) if "verify_attempts" in keys else 0,
        )

    def delivery(self, delivery_id: int) -> WebhookDelivery:
        with self._lock:
            row = self._conn.execute("SELECT * FROM webhook_deliveries WHERE id=?", (delivery_id,)).fetchone()
        if row is None:
            raise LedgerError(f"no webhook delivery {delivery_id}")
        return self._row_to_delivery(row)

    _DELIVERY_META = "id, received_at, event_id, event_type, verified, outcome, rescue_id, verify_attempts"

    def deliveries(
        self,
        *,
        unprocessed: bool = False,
        rescue_id: str | None = None,
        after_id: int = 0,
        raw: bool = True,
        limit: int | None = None,
    ) -> list[WebhookDelivery]:
        """Stored deliveries, oldest first. ``raw=False`` leaves the bodies and headers out (for
        views); ``rescue_id`` keeps only deliveries matched to that rescue."""
        sql = f"SELECT {'*' if raw else self._DELIVERY_META} FROM webhook_deliveries WHERE id > ?"
        params: list[Any] = [after_id]
        if unprocessed:
            sql += " AND outcome IS NULL"
        if rescue_id is not None:
            sql += " AND rescue_id = ?"
            params.append(rescue_id)
        sql += " ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_delivery(r) for r in rows]

    def delivery_counts(self) -> dict[str, int]:
        """Number of deliveries per outcome ("stored" for those not processed yet)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT COALESCE(outcome, 'stored') AS outcome, COUNT(*) AS n FROM webhook_deliveries GROUP BY 1"
            ).fetchall()
        return {r["outcome"]: int(r["n"]) for r in rows}

    def unverified_backlog(self) -> int:
        """Deliveries stored but not verified yet, plus those that failed verification and still
        keep their body. Bounds what an anonymous sender can make the ledger hold."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM webhook_deliveries WHERE (outcome IS NULL OR outcome IN"
                " ('unverified', 'malformed')) AND length(raw) > 0"
            ).fetchone()
        return int(row["n"])

    def note_verify_attempt(self, delivery_id: int) -> int:
        """Count one verification that could not run; returns the count."""
        with self.transaction() as db:
            db.execute(
                "UPDATE webhook_deliveries SET verify_attempts = verify_attempts + 1 WHERE id=?", (delivery_id,)
            )
            row = db.execute("SELECT verify_attempts FROM webhook_deliveries WHERE id=?", (delivery_id,)).fetchone()
        return int(row["verify_attempts"]) if row else 0

    def prune_delivery(self, delivery_id: int) -> None:
        """Drop the body and headers of a delivery that was never verified (keep its metadata)."""
        with self.transaction() as db:
            db.execute(
                "UPDATE webhook_deliveries SET raw=x'', headers_json='{}' WHERE id=? AND (verified IS NULL OR verified=0)",
                (delivery_id,),
            )

    def mark_delivery(
        self,
        delivery_id: int,
        *,
        outcome: str,
        verified: bool | None = None,
        event_id: str | None = None,
        event_type: str | None = None,
        rescue_id: str | None = None,
    ) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE webhook_deliveries SET outcome=?, verified=COALESCE(?, verified),"
                " event_id=COALESCE(?, event_id), event_type=COALESCE(?, event_type),"
                " rescue_id=COALESCE(?, rescue_id) WHERE id=?",
                (
                    outcome,
                    None if verified is None else int(verified),
                    event_id,
                    event_type,
                    rescue_id,
                    delivery_id,
                ),
            )

    def claim_event(self, event_id: str, delivery_id: int) -> bool:
        """True the first time a verified event id is seen; False for every replay."""
        with self.transaction() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO webhook_events (event_id, delivery_id, applied_at) VALUES (?,?,?)",
                (event_id, delivery_id, self._now()),
            )
            return cur.rowcount == 1

    # -- invariants --------------------------------------------------------------------------------

    def check_invariants(self, rescue_id: str) -> list[str]:
        """Money invariants for one rescue; an empty list means all hold."""
        rec = self.get(rescue_id)
        problems: list[str] = []
        if rec.authorized_amount is not None and rec.authorized_amount > rec.quote_amount:
            problems.append("authorized above quote")
        if rec.captured_amount is not None:
            if rec.captured_amount > rec.quote_amount:
                problems.append("captured above quote")
            if rec.authorized_amount is None or rec.captured_amount > rec.authorized_amount:
                problems.append("captured above authorized")
        if rec.refunded_amount is not None and (
            rec.captured_amount is None or rec.refunded_amount > rec.captured_amount
        ):
            problems.append("refunded above captured")
        declined = rec.capture_status in NO_MONEY_CAPTURE
        if rec.state is RescueState.VOIDED and rec.capture_id is not None and not declined:
            problems.append("voided rescue has a capture")
        if rec.state is RescueState.REFUNDED and not self.fully_refunded(rec):
            problems.append("refunded state without a completed full refund")
        if rec.state in CAPTURED_STATES and rec.capture_id is None:
            problems.append("captured state without a capture")
        ops = self.ops(rescue_id)
        if sum(1 for e in ops if e.status is OutboxStatus.PENDING) > 1:
            problems.append("more than one money operation in flight")
        done = {e.op for e in ops if e.status is OutboxStatus.DONE}
        if MoneyOp.CAPTURE in done and MoneyOp.VOID in done and not declined:
            problems.append("both captured and voided")
        return problems


# --------------------------------------------------------------------------------------------
# Payments: the only code that calls PayPal money operations
# --------------------------------------------------------------------------------------------

# A refused capture or void that means "already captured": never void after this.
_ALREADY_CAPTURED = frozenset({"AUTHORIZATION_ALREADY_CAPTURED", "PREVIOUSLY_CAPTURED"})
_AUTH_GONE = frozenset({"AUTHORIZATION_EXPIRED", "CANNOT_BE_VOIDED", "AUTHORIZATION_VOIDED", "AUTHORIZATION_DENIED"})
_REFUND_DONE = frozenset({"CAPTURE_FULLY_REFUNDED", "REFUND_AMOUNT_EXCEEDED"})
# Authorize refusals the buyer can fix on the same order by choosing another funding source.
BUYER_CAN_RETRY = frozenset({"INSTRUMENT_DECLINED", "PAYER_ACTION_REQUIRED"})
MAX_AUTHORIZE_ROUNDS = 3
_LIVE_REFUND = ("COMPLETED", "PENDING")
# A refund row closed with one of these (by an older version) is sent again once refundable.
_REFUND_LATER = frozenset({"PENDING_CAPTURE"})


class Payments:
    """Runs PayPal money operations through the outbox.

    Each method writes the outbox row, calls PayPal with the row's request id, and records the
    answer, the new state and the closed outbox row in one transaction. Calling a method again
    after a crash or a retryable error replays the same request id; calling it after it succeeded
    returns the stored result without calling PayPal.

    How an error is handled depends on what it proves (``PayPalError.refused``):

    - No proof (transport failure, 5xx, 401, 408, 409, 429, an unreadable answer): the outcome is
      unknown, so the row stays PENDING and the same request id is replayed later.
    - A refusal (HTTP 4xx): PayPal did nothing. The payment is read back where the answer could
      hide something (a capture that already exists, a void of a hold that is gone, a refund made
      elsewhere). A void is never closed as failed while the authorization is still CREATED: it
      stays PENDING under the next request id. A refund refused because the capture is still
      PENDING waits for the capture. Everything else closes the row as FAILED.
    """

    def __init__(self, ledger: Ledger, gateway: PaymentGateway) -> None:
        self.ledger = ledger
        self.gateway = gateway

    # -- public operations -------------------------------------------------------------------

    def create_hold(self, rescue_id: str) -> Order:
        """Create the PayPal order (intent AUTHORIZE) for the quoted amount. QUOTED -> HOLD_PENDING."""
        rec = self.ledger.get(rescue_id)
        if not rec.probe_spec_hash:
            raise LedgerError("fix the owner probe (set_probe) before creating the hold")
        args = {"amount": str(rec.quote_amount), "custom_id": rec.probe_spec_hash, "invoice_id": rec.invoice_id}
        return self._run(rescue_id, MoneyOp.CREATE_ORDER, args, {RescueState.QUOTED})

    def authorize(self, rescue_id: str) -> Authorization:
        """Authorize the approved order. HOLD_PENDING -> HELD when the authorization is CREATED for
        exactly the quote.

        After PayPal declined the buyer's funding source (INSTRUMENT_DECLINED), the buyer may pick
        another one on the same order; calling this again then starts a new round with a new
        request id (at most ``MAX_AUTHORIZE_ROUNDS``)."""
        rec = self.ledger.get(rescue_id)
        if not rec.order_id:
            raise LedgerError("no order to authorize")
        existing = self.ledger.get_op(rescue_id, MoneyOp.AUTHORIZE)
        reopen = (
            existing is not None
            and existing.status is OutboxStatus.FAILED
            and existing.error in BUYER_CAN_RETRY
            and existing.round < MAX_AUTHORIZE_ROUNDS
            and rec.state is RescueState.HOLD_PENDING
        )
        return self._run(
            rescue_id, MoneyOp.AUTHORIZE, {"order_id": rec.order_id}, {RescueState.HOLD_PENDING}, reopen=reopen
        )

    def capture(self, rescue_id: str, amount: Decimal | str | None = None) -> Capture:
        """Capture ``amount`` (default: the quote) with final_capture=true. PROBING -> CAPTURED.

        Refuses an amount above the quote or the authorized amount.
        """
        rec = self.ledger.get(rescue_id)
        value = rec.quote_amount if amount is None else money(amount)
        if value <= 0:
            raise InvariantViolation("capture amount must be positive")
        if value > rec.quote_amount:
            raise InvariantViolation(f"capture ${value} exceeds the quote ${rec.quote_amount}")
        if rec.authorized_amount is None or rec.authorization_id is None:
            raise LedgerError("no authorization to capture")
        if value > rec.authorized_amount:
            raise InvariantViolation(f"capture ${value} exceeds the authorized ${rec.authorized_amount}")
        args = {"auth_id": rec.authorization_id, "amount": str(value), "invoice_id": rec.capture_invoice_id}
        return self._run(rescue_id, MoneyOp.CAPTURE, args, {RescueState.PROBING})

    def void(self, rescue_id: str, reason: str) -> Authorization | None:
        """Release the hold. Any pre-capture state -> VOIDED. Never after a capture that moved money.

        With no authorization yet there is nothing to release at PayPal, so only the ledger moves.
        A rescue that already ended VOIDED is voided at PayPal too when the ledger knows of an
        authorization PayPal may still hold (``Ledger.orphan_holds``). Returns the voided
        authorization when PayPal sent one back.
        """
        rec = self.ledger.get(rescue_id)
        declined = rec.capture_status in NO_MONEY_CAPTURE
        orphan = any(r.id == rescue_id for r in self.ledger.orphan_holds())
        if rec.state is RescueState.VOIDED and not orphan:
            return None
        if rec.state not in PRE_CAPTURE_STATES and rec.state is not RescueState.VOIDED and not (
            rec.state is RescueState.CAPTURED and declined
        ):
            raise IllegalTransition(f"never void after capture (state {rec.state.value})")
        if rec.authorization_id is None:
            pending = self.ledger.get_op(rescue_id, MoneyOp.AUTHORIZE)
            if pending is not None and pending.status is OutboxStatus.PENDING:
                raise OpInFlight("authorize is still in flight; reconcile before voiding")
            self.ledger.transition(rescue_id, RescueState.VOIDED, reason=reason or "voided before authorization")
            return None
        if orphan or rec.state is RescueState.CAPTURED:
            # The rescue already ended (or PayPal declined its capture): look before voiding, so
            # an authorization PayPal already closed is only recorded.
            auth = self.gateway.get_authorization(rec.authorization_id)
            rec = self.ledger.record_authorization(rescue_id, auth)
            if auth.status not in ("CREATED", "PENDING") or is_expired(auth.expiration_time, self.ledger.clock.now()):
                if rec.state is RescueState.CAPTURED:
                    self.ledger.transition(rescue_id, RescueState.VOIDED, reason=reason, source="paypal")
                return auth
        existing = self.ledger.get_op(rescue_id, MoneyOp.VOID)
        reopen = existing is not None and existing.status is not OutboxStatus.PENDING and (
            existing.status is OutboxStatus.FAILED or orphan
        )
        args = {"auth_id": rec.authorization_id}
        allowed = PRE_CAPTURE_STATES | {RescueState.VOIDED, RescueState.CAPTURED}
        return self._run(rescue_id, MoneyOp.VOID, args, allowed, note=reason, reopen=reopen)

    def refund(self, rescue_id: str, reason: str) -> Refund:
        """Refund what is left of the capture (warranty failed). CAPTURED -> REFUNDED once PayPal
        shows the refund COMPLETED for the whole capture.

        The decision is stored first (``Ledger.mark_refund_due``), so the reconciler keeps
        sending it until PayPal confirms it. While the capture is PENDING nothing is sent
        (``RefundDeferred``). A refund that PayPal later FAILED or CANCELLED is sent again under
        the next request id. The amount is the capture minus what PayPal already lists as
        refunded, fixed in the outbox row before the call.
        """
        rec = self.ledger.get(rescue_id)
        if rec.capture_id is None or rec.captured_amount is None:
            raise LedgerError("nothing captured to refund")
        existing = self.ledger.get_op(rescue_id, MoneyOp.REFUND)
        if rec.state is not RescueState.CAPTURED:
            if existing is not None and existing.status is OutboxStatus.DONE:
                return existing.result_object()  # type: ignore[return-value]
            raise IllegalTransition(f"cannot refund while {rec.state.value}")
        self.ledger.mark_refund_due(rescue_id, reason)
        if existing is not None and existing.status is OutboxStatus.PENDING:
            self._require_refundable(rescue_id)
            return self._execute(existing)
        if existing is not None and existing.status is OutboxStatus.FAILED and existing.error not in _REFUND_LATER:
            raise OpFailed(f"refund already failed for this rescue: {existing.error}", existing.error)
        if (
            existing is not None
            and existing.status is OutboxStatus.DONE
            and rec.refund_status not in ("FAILED", "CANCELLED")
        ):
            return existing.result_object()  # type: ignore[return-value]
        self._require_refundable(rescue_id)
        rec = self.ledger.get(rescue_id)
        prior = self._refunded_at_paypal(rec)
        assert rec.captured_amount is not None
        remaining = rec.captured_amount - prior
        if remaining <= 0:
            found = self.adopt_refunds(rescue_id)  # refunded in full elsewhere (for example the dashboard)
            assert found is not None
            return found
        args = {"capture_id": rec.capture_id, "amount": str(remaining), "prior": str(prior)}
        return self._run(
            rescue_id, MoneyOp.REFUND, args, {RescueState.CAPTURED}, note=reason, reopen=existing is not None
        )

    def replay(self, entry: OutboxEntry) -> Any:
        """Replay a PENDING outbox row with its own request id and arguments (reconciler)."""
        current = self.ledger.get_op_by_id(entry.id)
        if current.status is OutboxStatus.DONE:
            return current.result_object()
        if current.status is OutboxStatus.FAILED:
            raise OpFailed(f"{current.op.value} already failed: {current.error}", current.error)
        if current.op is MoneyOp.REFUND:
            self._require_refundable(current.rescue_id)
        return self._execute(current)

    def adopt_refunds(self, rescue_id: str) -> Refund | None:
        """Record what PayPal lists as refunded on this rescue's capture: refunds made elsewhere,
        or ones whose answer Keyhold lost. CAPTURED -> REFUNDED once the whole capture is
        refunded and every refund has COMPLETED; a partial refund leaves the rescue CAPTURED."""
        rec = self.ledger.get(rescue_id)
        if rec.capture_id is None:
            return None
        refunds = self._refunds_at_paypal(rec)
        if not refunds:
            return None
        live = [r for r in refunds if r.status in _LIVE_REFUND]
        total = sum((money(r.amount) for r in live), money("0"))
        pending = [r for r in live if r.status == "PENDING"]
        tracked = [r for r in live if r.id == rec.refund_id]
        pick = (pending or tracked or live or refunds)[-1]
        with self.ledger.transaction():
            if pick.id != rec.refund_id or pick.status != rec.refund_status or total != rec.refunded_amount:
                self.ledger.log(
                    rescue_id, "repair", f"PayPal lists ${total} refunded on capture {rec.capture_id}",
                    {"refund_id": pick.id, "total": str(total)},
                )
            rec = self.ledger.record_refund(rescue_id, pick, total=total)
            if rec.state is RescueState.CAPTURED and Ledger.fully_refunded(rec):
                self.ledger.advance(rescue_id, RescueState.REFUNDED, reason="refund found at PayPal", source="paypal")
        return pick

    def adopt_capture(self, rescue_id: str) -> Capture | None:
        """Record the capture PayPal lists for this rescue's authorization, if there is one (a
        capture whose answer Keyhold never got). Moves the rescue to CAPTURED."""
        rec = self.ledger.get(rescue_id)
        found = self._lookup_capture(rec)
        if found is None:
            return None
        with self.ledger.transaction():
            self.ledger.record_capture(rescue_id, found)
            self.ledger.log(rescue_id, "repair", f"capture {found.id} found at PayPal", {"capture_id": found.id})
            self._settle_capture(rescue_id, found, reason="capture found at PayPal")
        return found

    # -- machinery -------------------------------------------------------------------------------

    def _run(
        self,
        rescue_id: str,
        op: MoneyOp,
        args: dict[str, Any],
        allowed: Iterable[RescueState],
        *,
        note: str = "",
        reopen: bool = False,
    ) -> Any:
        entry = self.ledger.begin_op(rescue_id, op, args, allowed_states=allowed, note=note, reopen=reopen)
        if entry.status is OutboxStatus.DONE:
            return entry.result_object()
        return self._execute(entry)

    def _call(self, entry: OutboxEntry) -> Any:
        if self.ledger.in_transaction:
            raise RuntimeError("PayPal must not be called inside a ledger transaction")
        a, rid = entry.args, entry.request_id
        if entry.op is MoneyOp.CREATE_ORDER:
            return self.gateway.create_hold(money(a["amount"]), a["custom_id"], a["invoice_id"], rid)
        if entry.op is MoneyOp.AUTHORIZE:
            return self.gateway.authorize(a["order_id"], rid)
        if entry.op is MoneyOp.CAPTURE:
            return self.gateway.capture(a["auth_id"], money(a["amount"]), True, a["invoice_id"], rid)
        if entry.op is MoneyOp.VOID:
            return self.gateway.void(a["auth_id"], rid)
        if entry.op is MoneyOp.REFUND:
            return self.gateway.refund(a["capture_id"], money(a["amount"]), rid)
        raise LedgerError(f"unknown op {entry.op}")

    def _execute(self, entry: OutboxEntry) -> Any:
        self.ledger.log(
            entry.rescue_id,
            "paypal_call",
            f"{entry.op.value} with PayPal-Request-Id {entry.request_id}",
            {"op": entry.op.value, "request_id": entry.request_id, "attempt": entry.attempts + 1},
        )
        try:
            result = self._call(entry)
        except PayPalError as err:
            self.ledger.log(
                entry.rescue_id,
                "paypal_error",
                f"{entry.op.value}: {err.issue}",
                {
                    "op": entry.op.value,
                    "issue": err.issue,
                    "status": err.status,
                    "retryable": err.retryable,
                    "debug_id": err.debug_id,
                },
            )
            if not err.refused:
                # No proof of what happened: replay the same request id later.
                self.ledger.note_attempt(entry.id, err)
                raise
            return self._final_error(entry, err)
        self._apply(entry, result)
        return result

    def _apply(self, entry: OutboxEntry, result: Any, *, reason: str = "", total: Decimal | None = None) -> None:
        """Record PayPal's answer, move the state and close the outbox row, atomically."""
        rid = entry.rescue_id
        followup_void = False
        with self.ledger.transaction():
            self.ledger.complete_op(entry.id, result)  # same transaction: all or nothing
            if entry.op is MoneyOp.CREATE_ORDER:
                self.ledger.record_order(rid, result)
                self.ledger.log(rid, "paypal_ok", f"order {result.id} {result.status}", {"order_id": result.id})
                self.ledger.transition(rid, RescueState.HOLD_PENDING, source="paypal")
            elif entry.op is MoneyOp.AUTHORIZE:
                rec = self.ledger.record_authorization(rid, result)
                self.ledger.log(
                    rid,
                    "paypal_ok",
                    f"authorization {result.id} {result.status} ${result.amount}",
                    {"authorization_id": result.id, "status": result.status, "amount": str(result.amount)},
                )
                followup_void = self._settle_authorization(rec)
            elif entry.op is MoneyOp.CAPTURE:
                self.ledger.record_capture(rid, result)
                self.ledger.log(
                    rid,
                    "paypal_ok",
                    f"capture {result.id} {result.status} ${result.amount}",
                    {"capture_id": result.id, "status": result.status, "amount": str(result.amount)},
                )
                self._settle_capture(rid, result, reason=reason or "probe passed")
            elif entry.op is MoneyOp.VOID:
                self.ledger.record_auth_status(rid, "VOIDED")
                self.ledger.log(rid, "paypal_ok", "authorization voided", {"authorization_id": entry.args["auth_id"]})
                rec = self.ledger.get(rid)
                why = reason or entry.note or "voided"
                if rec.state is RescueState.CAPTURED:  # a declined capture left the hold behind
                    self.ledger.transition(rid, RescueState.VOIDED, reason=why, source="paypal")
                else:
                    self.ledger.advance(rid, RescueState.VOIDED, reason=why, source="paypal")
            elif entry.op is MoneyOp.REFUND:
                self._record_refund(entry, result, total)
        if followup_void:
            self.void(rid, "authorization does not match the quote")

    def _record_refund(self, entry: OutboxEntry, result: Refund, total: Decimal | None) -> None:
        rid = entry.rescue_id
        if total is None:
            prior = money(entry.args.get("prior", "0"))
            total = prior + money(result.amount) if result.status in _LIVE_REFUND else prior
        rec = self.ledger.record_refund(rid, result, total=total)
        self.ledger.log(
            rid,
            "paypal_ok",
            f"refund {result.id} {result.status} ${result.amount}",
            {"refund_id": result.id, "status": result.status, "amount": str(result.amount)},
        )
        if Ledger.fully_refunded(rec):
            self.ledger.advance(rid, RescueState.REFUNDED, reason=entry.note or "refunded", source="paypal")
        elif result.status == "PENDING":
            self.ledger.log(
                rid, "refund_pending", f"PayPal shows refund {result.id} as PENDING; the reconciler checks it again", {}
            )
        elif result.status in ("FAILED", "CANCELLED"):
            self.ledger.log(
                rid, "refund_failed", f"PayPal answered the refund with {result.status}; it is sent again", {}
            )

    def _settle_capture(self, rid: str, capture: Capture, *, reason: str) -> None:
        """Move the rescue after a capture: CAPTURED, or VOIDED when PayPal declined it."""
        if capture.status in NO_MONEY_CAPTURE:
            self.ledger.log(
                rid,
                "capture_not_settled",
                f"PayPal answered the capture with {capture.status}; no money moved",
                {"capture_id": capture.id, "status": capture.status},
            )
            if self.ledger.get(rid).state is not RescueState.VOIDED:
                self.ledger.transition(
                    rid,
                    RescueState.VOIDED,
                    reason=f"PayPal declined the capture ({capture.status}); nothing was charged",
                    source="paypal",
                )
            return
        self.ledger.advance(rid, RescueState.CAPTURED, reason=reason, source="paypal")

    def _settle_authorization(self, rec: RescueRecord) -> bool:
        """Move a fresh authorization to HELD or VOIDED. True means it must be voided at PayPal."""
        if rec.state is RescueState.VOIDED:
            # The rescue ended before PayPal's answer was known: release whatever PayPal holds.
            return rec.auth_status in ("CREATED", "PENDING")
        if rec.state is not RescueState.HOLD_PENDING:
            return False
        if rec.auth_status in ("DENIED", "VOIDED"):
            self.ledger.advance(rec.id, RescueState.VOIDED, reason=f"authorization {rec.auth_status}", source="paypal")
            return False
        if rec.auth_status != "CREATED":
            return False  # PENDING: a webhook or the reconciler moves it on later
        if rec.authorized_amount != rec.quote_amount:
            self.ledger.log(
                rec.id,
                "conflict",
                "authorized amount differs from the quote",
                {"authorized": str(rec.authorized_amount), "quote": str(rec.quote_amount)},
            )
            return True
        if is_expired(rec.auth_expiration_time, self.ledger.clock.now()):
            self.ledger.advance(rec.id, RescueState.VOIDED, reason="authorization already expired", source="paypal")
            return False
        self.ledger.advance(rec.id, RescueState.HELD, source="paypal")
        return False

    def _read_back(self, entry: OutboxEntry, fn: Any, *args: Any) -> Any:
        """A GET used to interpret a refusal. If it fails, the operation stays PENDING: without
        the read-back nothing proves the refusal is final."""
        try:
            return fn(*args)
        except PayPalError as err:
            self.ledger.note_attempt(entry.id, err)
            raise

    def _lookup_capture(self, rec: RescueRecord) -> Capture | None:
        lookup = getattr(self.gateway, "get_order_payments", None)
        if lookup is None or rec.order_id is None:
            return None
        payments = lookup(rec.order_id)
        mine = [c for c in payments.captures if c.authorization_id in (None, rec.authorization_id)]
        if len(mine) > 1:
            raise InvariantViolation(f"PayPal lists {len(mine)} captures for one authorization")
        return mine[0] if mine else None

    def _find_capture(self, entry: OutboxEntry, rec: RescueRecord) -> Capture | None:
        lookup = getattr(self.gateway, "get_order_payments", None)
        if lookup is None or rec.order_id is None:
            return None
        payments = self._read_back(entry, lookup, rec.order_id)
        mine = [c for c in payments.captures if c.authorization_id in (None, rec.authorization_id)]
        return mine[0] if len(mine) == 1 else None

    def _refunds_at_paypal(self, rec: RescueRecord, entry: OutboxEntry | None = None) -> list[Refund]:
        lookup = getattr(self.gateway, "get_order_payments", None)
        if lookup is None or rec.order_id is None:
            return []
        payments = self._read_back(entry, lookup, rec.order_id) if entry is not None else lookup(rec.order_id)
        return [r for r in payments.refunds if r.capture_id in (None, rec.capture_id)]

    def _refunded_at_paypal(self, rec: RescueRecord) -> Decimal:
        """Money PayPal lists as refunded or being refunded on this capture."""
        return sum((money(r.amount) for r in self._refunds_at_paypal(rec) if r.status in _LIVE_REFUND), money("0"))

    def _require_refundable(self, rescue_id: str) -> None:
        """Raise ``RefundDeferred`` while the capture is PENDING at PayPal (read back to be sure),
        and ``LedgerError`` when the capture moved no money."""
        rec = self.ledger.get(rescue_id)
        if rec.capture_status in ("PENDING", None) and rec.capture_id:
            capture = self.gateway.get_capture(rec.capture_id)
            self.ledger.record_capture_status(rescue_id, capture.status)
            rec = self.ledger.get(rescue_id)
        if rec.capture_status == "PENDING":
            self.ledger.log(
                rescue_id,
                "refund_deferred",
                "the capture is still PENDING at PayPal, which cannot refund it yet; the refund is sent once it completes",
                {},
            )
            raise RefundDeferred("the capture is still PENDING; the refund is sent once it completes")
        if rec.capture_status in NO_MONEY_CAPTURE:
            raise LedgerError(f"the capture is {rec.capture_status}; no money moved, so there is nothing to refund")

    def _final_error(self, entry: OutboxEntry, err: PayPalError) -> Any:
        rid = entry.rescue_id
        rec = self.ledger.get(rid)
        op = entry.op

        if op is MoneyOp.AUTHORIZE:
            return self._authorize_refused(entry, rec, err)

        if op is MoneyOp.CAPTURE:
            # Whatever the refusal says, look for a capture of this authorization first: one that
            # exists (made by this request id or an earlier lost one) is the answer, never a void.
            found = self._find_capture(entry, rec)
            if found is not None:
                self.ledger.log(
                    rid, "repair", f"capture {found.id} already existed at PayPal", {"capture_id": found.id}
                )
                self._apply(entry, found)
                return found

        if op is MoneyOp.VOID:
            return self._void_refused(entry, rec, err)

        if op is MoneyOp.REFUND:
            return self._refund_refused(entry, rec, err)

        # A plain final refusal: close the row as FAILED.
        with self.ledger.transaction():
            self.ledger.fail_op(entry.id, err)
            if op is MoneyOp.CREATE_ORDER:
                # No hold exists and this op cannot run again for the rescue: end it honestly.
                self.ledger.advance(rid, RescueState.VOIDED, reason=f"{op.value} refused: {err.issue}", source="paypal")
        raise err

    def _authorize_refused(self, entry: OutboxEntry, rec: RescueRecord, err: PayPalError) -> Any:
        rid = entry.rescue_id
        # Whatever PayPal said, read the order back first: a hold that exists must be recorded
        # (and released if it cannot be used), never left behind an ended rescue.
        order = self._read_back(entry, self.gateway.get_order, entry.args["order_id"])
        if order.authorization_id:
            auth = self._read_back(entry, self.gateway.get_authorization, order.authorization_id)
            if err.issue == "ORDER_ALREADY_AUTHORIZED":
                self._apply(entry, auth)
                return auth
            with self.ledger.transaction():
                self.ledger.record_authorization(rid, auth)
                self.ledger.fail_op(entry.id, err)
                self.ledger.log(
                    rid,
                    "conflict",
                    f"authorize refused ({err.issue}) but PayPal lists authorization {auth.id} {auth.status}",
                    {"issue": err.issue, "authorization_id": auth.id},
                )
            if auth.status in ("CREATED", "PENDING") and not is_expired(auth.expiration_time, self.ledger.clock.now()):
                self.void(rid, f"authorize refused ({err.issue}); the hold PayPal made anyway is released")
            else:
                self.ledger.advance(rid, RescueState.VOIDED, reason=f"authorize refused: {err.issue}", source="paypal")
            raise err
        if err.issue in BUYER_CAN_RETRY and entry.round < MAX_AUTHORIZE_ROUNDS:
            with self.ledger.transaction():
                self.ledger.fail_op(entry.id, err)
                self.ledger.log(
                    rid,
                    "authorize_declined",
                    f"PayPal declined the funding source ({err.issue}); the buyer can choose another one",
                    {"issue": err.issue, "round": entry.round},
                )
            raise err
        with self.ledger.transaction():
            self.ledger.fail_op(entry.id, err)
            self.ledger.advance(rid, RescueState.VOIDED, reason=f"authorize refused: {err.issue}", source="paypal")
        raise err

    def _void_refused(self, entry: OutboxEntry, rec: RescueRecord, err: PayPalError) -> Any:
        """A void PayPal refused. Read the authorization back: the void is closed only when the
        hold is proven gone (or captured, where voiding is wrong); while it is still CREATED the
        void stays PENDING under the next request id and is tried again."""
        rid = entry.rescue_id
        auth = self._read_back(entry, self.gateway.get_authorization, entry.args["auth_id"])
        now = self.ledger.clock.now()
        if auth.status in ("CAPTURED", "PARTIALLY_CAPTURED"):
            with self.ledger.transaction():
                self.ledger.record_authorization(rid, auth)
                self.ledger.fail_op(entry.id, err)
                self.ledger.log(
                    rid, "conflict", "void refused: PayPal says this authorization was captured", {"issue": err.issue}
                )
            raise err
        if auth.status == "VOIDED":
            with self.ledger.transaction():
                self.ledger.record_authorization(rid, auth)
                self._apply(entry, auth)
            return auth
        if auth.status == "DENIED" or is_expired(auth.expiration_time, now):
            with self.ledger.transaction():
                self.ledger.record_authorization(rid, auth)
                self.ledger.fail_op(entry.id, err)
                gone = "denied" if auth.status == "DENIED" else "expired"
                if self.ledger.get(rid).state is not RescueState.VOIDED:
                    self.ledger.transition(
                        rid,
                        RescueState.VOIDED,
                        reason=f"{entry.note or 'void'}; the authorization {gone}, so nothing is held",
                        source="paypal",
                    )
            return auth
        # Still held at PayPal: the void is still owed.
        self.ledger.record_authorization(rid, auth)
        self.ledger.retry_op(entry.id, err)
        self.ledger.log(
            rid,
            "void_refused",
            f"PayPal refused the void ({err.issue}) while the authorization is {auth.status}; it is sent again",
            {"issue": err.issue, "status": auth.status},
        )
        raise err

    def _refund_refused(self, entry: OutboxEntry, rec: RescueRecord, err: PayPalError) -> Any:
        rid = entry.rescue_id
        assert rec.capture_id is not None and rec.captured_amount is not None
        capture = self._read_back(entry, self.gateway.get_capture, rec.capture_id)
        self.ledger.record_capture_status(rid, capture.status)
        if capture.status == "PENDING":
            # PayPal cannot refund a pending capture (PENDING_CAPTURE). Not a final answer: the
            # row stays PENDING, under a new request id, and is sent once the capture completes.
            self.ledger.retry_op(entry.id, err)
            self.ledger.log(
                rid,
                "refund_deferred",
                f"PayPal refused the refund ({err.issue}) because the capture is still PENDING; it is sent once it completes",
                {"issue": err.issue},
            )
            raise RefundDeferred("the capture is still PENDING; the refund is sent once it completes") from err
        if capture.status in NO_MONEY_CAPTURE:
            with self.ledger.transaction():
                self.ledger.fail_op(entry.id, err)
                self.ledger.attention(rid, f"refund refused ({err.issue}); the capture is {capture.status}", {})
            raise err
        refunds = self._refunds_at_paypal(rec, entry)
        live = [r for r in refunds if r.status in _LIVE_REFUND]
        total = sum((money(r.amount) for r in live), money("0"))
        if live and total >= rec.captured_amount:
            # The whole capture is already refunded at PayPal (for example from the dashboard).
            latest = live[-1]
            self.ledger.log(rid, "repair", f"refund {latest.id} already existed at PayPal", {"refund_id": latest.id})
            self._apply(entry, latest, total=total)
            return latest
        prior = money(entry.args.get("prior", "0"))
        if err.issue in _REFUND_DONE and total != prior:
            # Someone refunded part of the capture since the amount was fixed: send the rest.
            remaining = rec.captured_amount - total
            self.ledger.retry_op(
                entry.id, err, args={**entry.args, "amount": str(remaining), "prior": str(total)}
            )
            self.ledger.log(
                rid, "refund_resized", f"PayPal already lists ${total} refunded; refunding the remaining ${remaining}", {}
            )
            raise err
        with self.ledger.transaction():
            self.ledger.fail_op(entry.id, err)
            self.ledger.attention(
                rid,
                f"PayPal refused the warranty refund ({err.issue}); a person must refund ${rec.captured_amount - total}",
                {"issue": err.issue},
            )
        raise err
