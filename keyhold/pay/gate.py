"""The lock: HoldLease and GatedConsole.

HoldLease grants input only after GET authorization shows CREATED with amount >= quote for this
rescue. It re-checks PayPal every N actions and when its short cache TTL runs out, and raises
HoldRevoked as soon as the authorization is VOIDED, expired or missing, or the owner pressed Stop.

GatedConsole(console, lease) allows screenshot() always; every input method calls
lease.require() first.

``HoldRevoked`` is defined in ``keyhold.types`` and re-exported here.

What "for this rescue" means. The lease reads the authorization id from the ledger row of its
rescue, never from the caller, and the authorization PayPal returns must match that row: same
id, same order id, ``custom_id`` equal to the probe spec hash fixed at checkout, and the rescue's
invoice id. An authorization for another rescue, or for less than the quote, grants nothing.

What is checked on every ``require()`` (no network): the lease was not revoked (owner Stop, end
of the agent's session, an earlier failed check), the rescue's time limit has not passed, the
ledger row is HELD or ACTIVE with no void or capture started, and the last authorization PayPal
returned is not about to expire. PayPal is asked again on the first call, after
``check_every`` calls since the last answer, and when the last answer is older than ``ttl_s``.

Fail closed. Any doubt ends the session with ``HoldRevoked``: PayPal answering anything but
CREATED, a missing authorization, a PayPal error that persists after ``retries`` quick retries
(a 429 throttle is retried like a 5xx, waiting out a short Retry-After), or an unexpected
exception. A revoked lease never grants again.

Stop within one action. ``revoke()`` marks the lease revoked at once, then waits for an input
that is already being sent to finish. When it returns, nothing more can reach the console
through any ``GatedConsole`` on this lease: input is sent while holding the lease's input lock,
and the revoked mark is checked inside that lock.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from keyhold.pay.client import parse_paypal_time
from keyhold.pay.ledger import Ledger, LedgerError, RescueRecord
from keyhold.types import (
    Authorization,
    Clock,
    Console,
    HoldRevoked,
    Lease,
    MoneyOp,
    PaymentGateway,
    PayPalError,
    RescueState,
    SystemClock,
    money,
)

__all__ = [
    "DEFAULT_CHECK_EVERY",
    "DEFAULT_EXPIRY_MARGIN_S",
    "DEFAULT_TTL_S",
    "GatedConsole",
    "HoldLease",
    "HoldRevoked",
]

DEFAULT_CHECK_EVERY = 5  # ask PayPal again after this many require() calls
DEFAULT_TTL_S = 15.0  # ask PayPal again when the last answer is older than this
DEFAULT_EXPIRY_MARGIN_S = 120.0  # treat an authorization this close to expiry as expired
MAX_RETRY_AFTER_S = 10.0  # longest Retry-After waited out during a check

# Ledger states in which the agent may hold the keyboard.
_INPUT_STATES = frozenset({RescueState.HELD, RescueState.ACTIVE})


class HoldLease:
    """Permission to send input for one rescue, backed by a live PayPal authorization.

    ``deadline`` (epoch seconds on ``clock``) is the rescue time limit: once it passes, the lease
    revokes itself. ``retries`` and ``retry_wait_s`` cover brief PayPal outages during a check;
    the wait goes through ``clock.sleep``.
    """

    def __init__(
        self,
        ledger: Ledger,
        gateway: PaymentGateway,
        rescue_id: str,
        *,
        clock: Clock | None = None,
        check_every: int = DEFAULT_CHECK_EVERY,
        ttl_s: float = DEFAULT_TTL_S,
        expiry_margin_s: float = DEFAULT_EXPIRY_MARGIN_S,
        deadline: float | None = None,
        retries: int = 2,
        retry_wait_s: float = 1.0,
    ) -> None:
        if check_every < 1:
            raise ValueError("check_every must be at least 1")
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        if retries < 0 or retry_wait_s < 0 or expiry_margin_s < 0:
            raise ValueError("retries, retry_wait_s and expiry_margin_s must not be negative")
        self.ledger = ledger
        self.gateway = gateway
        self.rescue_id = rescue_id
        self.clock: Clock = clock or ledger.clock or SystemClock()
        self.check_every = int(check_every)
        self.ttl_s = float(ttl_s)
        self.expiry_margin_s = float(expiry_margin_s)
        self.deadline = deadline
        self.retries = int(retries)
        self.retry_wait_s = float(retry_wait_s)

        self._revoked = threading.Event()
        self._reason: str | None = None
        self._check_lock = threading.RLock()  # guards the cached answer below
        self._input_lock = threading.RLock()  # held while one input is sent
        self._auth: Authorization | None = None
        self._checked_at: float | None = None
        self._since_check = 0

        self.actions = 0  # require() calls that granted input
        self.checks = 0  # times PayPal was asked

    # -- reading -----------------------------------------------------------------------------

    @property
    def revoked(self) -> bool:
        return self._revoked.is_set()

    @property
    def reason(self) -> str | None:
        """Why the lease was revoked (None while it is live)."""
        return self._reason

    @property
    def authorization(self) -> Authorization | None:
        """The last authorization PayPal returned for this rescue."""
        return self._auth

    # -- the lock ------------------------------------------------------------------------------

    def require(self) -> None:
        """Return if input is allowed right now; raise ``HoldRevoked`` otherwise."""
        with self._check_lock:
            self._raise_if_revoked()
            now = self.clock.now()
            if self.deadline is not None and now >= self.deadline:
                self._revoke("the rescue time limit was reached")
            rec = self._local_checks()
            if self._needs_check(now):
                self._check_paypal(rec)
            self._raise_if_revoked()  # a Stop that arrived while PayPal answered wins
            self._since_check += 1
            self.actions += 1

    def check(self) -> Authorization:
        """Ask PayPal now, whatever the cache says. Raises ``HoldRevoked`` like ``require``."""
        with self._check_lock:
            self._raise_if_revoked()
            rec = self._local_checks()
            self._check_paypal(rec)
            self._raise_if_revoked()
            assert self._auth is not None
            return self._auth

    @contextmanager
    def input_window(self) -> Iterator[None]:
        """Hold the input lock, check the lease, and let exactly one input through.

        ``revoke()`` waits for an open window to close, so after it returns no input that was
        not already being sent can reach the console.
        """
        with self._input_lock:
            self.require()
            yield

    def revoke(self, reason: str, *, wait: bool = True) -> None:
        """End the session: no input from now on. With ``wait`` (the default), return only
        after any input already being sent has finished. Revoking twice keeps the first reason."""
        self._revoke(reason, raise_after=False)
        if wait:
            with self._input_lock:
                pass

    # -- internals -----------------------------------------------------------------------------

    def _raise_if_revoked(self) -> None:
        if self._revoked.is_set():
            raise HoldRevoked(self._reason or "the hold is no longer valid")

    def _revoke(self, reason: str, *, raise_after: bool = True) -> None:
        first = False
        if not self._revoked.is_set():
            # Set the reason before the flag so a reader that sees the flag sees the reason.
            self._reason = reason
            self._revoked.set()
            first = True
        if first:
            try:
                self.ledger.log(self.rescue_id, "gate_revoked", reason, {"actions": self.actions, "checks": self.checks})
            except Exception:  # logging must never re-open the gate
                pass
        if raise_after:
            raise HoldRevoked(self._reason or reason)

    def _local_checks(self) -> RescueRecord:
        try:
            rec = self.ledger.get(self.rescue_id)
        except LedgerError:
            self._revoke("this rescue is not in the ledger")
        except Exception as exc:
            self._revoke(f"the ledger could not be read ({type(exc).__name__})")
        if rec.state not in _INPUT_STATES:
            self._revoke(f"the rescue is {rec.state.value}; the keyboard is only lent while the hold is live")
        if rec.authorization_id is None:
            self._revoke("there is no PayPal authorization for this rescue")
        for op, what in ((MoneyOp.VOID, "the hold is being released"), (MoneyOp.CAPTURE, "a capture has started")):
            if self.ledger.get_op(self.rescue_id, op) is not None:
                self._revoke(what)
        if rec.auth_status not in (None, "CREATED"):
            self._revoke(f"the ledger shows the authorization as {rec.auth_status}")
        if self._auth is not None and self._expiring(self._auth.expiration_time):
            self._revoke("the PayPal authorization has expired")
        return rec

    def _needs_check(self, now: float) -> bool:
        if self._auth is None or self._checked_at is None:
            return True
        if self._since_check >= self.check_every:
            return True
        return now - self._checked_at >= self.ttl_s or now < self._checked_at

    def _expiring(self, expiration_time: str | None) -> bool:
        expires = parse_paypal_time(expiration_time)
        return expires is not None and expires - self.expiry_margin_s <= self.clock.now()

    def _fetch(self, auth_id: str) -> Authorization:
        attempt = 0
        while True:
            self._raise_if_revoked()
            wait = self.retry_wait_s * (attempt + 1)
            try:
                return self.gateway.get_authorization(auth_id)
            except PayPalError as err:
                if not err.retryable:
                    if err.status == 404 or err.issue in ("RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID"):
                        self._revoke(f"PayPal has no authorization {auth_id} ({err.issue})")
                    self._revoke(f"PayPal refused to show the authorization ({err.issue})")
                if attempt >= self.retries:
                    self._revoke(f"PayPal could not confirm the hold ({err.issue})")
                if err.retry_after is not None:  # a short throttle (429) is waited out, not a revoke
                    wait = max(wait, min(err.retry_after, MAX_RETRY_AFTER_S))
            except Exception as exc:
                self._revoke(f"the hold could not be checked ({type(exc).__name__})")
            attempt += 1
            if wait:
                self.clock.sleep(wait)

    def _check_paypal(self, rec: RescueRecord) -> None:
        assert rec.authorization_id is not None
        auth = self._fetch(rec.authorization_id)
        self.checks += 1
        problem = self._mismatch(auth, rec)
        try:
            self.ledger.log(
                self.rescue_id,
                "gate_check",
                f"PayPal authorization {auth.id} {auth.status} ${money(auth.amount)}"
                + (f": {problem}" if problem else ""),
                {
                    "authorization_id": auth.id,
                    "status": auth.status,
                    "amount": str(money(auth.amount)),
                    "expiration_time": auth.expiration_time,
                    "granted": problem is None,
                },
            )
        except Exception:
            pass
        if problem is not None:
            self._revoke(problem)
        self._auth = auth
        self._checked_at = self.clock.now()
        self._since_check = 0

    def _mismatch(self, auth: Authorization, rec: RescueRecord) -> str | None:
        """None when ``auth`` is a live hold of at least the quote for this rescue."""
        if auth.id != rec.authorization_id:
            return "PayPal returned a different authorization"
        if auth.status != "CREATED":
            return f"the PayPal authorization is {auth.status}"
        if self._expiring(auth.expiration_time):
            return "the PayPal authorization has expired"
        if money(auth.amount) < money(rec.quote_amount):
            return f"the authorization (${money(auth.amount)}) is below the quote (${rec.quote_amount})"
        # The ids must be present and match; an answer without them grants nothing.
        if rec.order_id and auth.order_id != rec.order_id:
            return ("PayPal's answer does not name this rescue's order" if not auth.order_id
                    else "the authorization belongs to another order")
        if rec.probe_spec_hash and auth.custom_id != rec.probe_spec_hash:
            return ("PayPal's answer does not carry the probe spec hash" if not auth.custom_id
                    else "the authorization was made for another probe spec")
        if auth.invoice_id != rec.invoice_id:
            return ("PayPal's answer does not carry this rescue's invoice id" if not auth.invoice_id
                    else "the authorization belongs to another rescue")
        return None

    def __repr__(self) -> str:
        state = f"revoked: {self._reason}" if self.revoked else "live"
        return f"HoldLease(rescue={self.rescue_id!r}, {state}, actions={self.actions}, checks={self.checks})"


class GatedConsole:
    """A ``Console`` whose keyboard works only while ``lease`` grants it.

    ``screenshot()`` is always allowed: looking costs nothing and changes nothing. ``type_text``,
    ``press`` and ``power_cycle`` each pass through the lease first (inside its input window
    when the lease has one) and raise ``HoldRevoked`` without touching the console otherwise.

    ``close()`` ends this gated session: no more input through it. It does not close the wrapped
    console, which belongs to whoever opened it (the status page may still take thumbnails).
    """

    def __init__(self, console: Console, lease: Lease) -> None:
        self._console = console
        self._lease = lease
        self._closed = False
        self.inputs_sent = 0
        self.inputs_refused = 0

    @property
    def lease(self) -> Lease:
        return self._lease

    @property
    def closed(self) -> bool:
        return self._closed

    def screenshot(self) -> bytes:
        return self._console.screenshot()

    def screen_text(self) -> str:
        """The screen as text when the wrapped console can give it (used by the free look)."""
        reader = getattr(self._console, "screen_text", None)
        if not callable(reader):
            raise AttributeError("the wrapped console cannot give the screen as text")
        return reader()

    def type_text(self, text: str) -> None:
        with self._gate():
            self._console.type_text(text)
            self.inputs_sent += 1

    def press(self, keys: list[str]) -> None:
        with self._gate():
            self._console.press(keys)
            self.inputs_sent += 1

    def power_cycle(self) -> None:
        with self._gate():
            self._console.power_cycle()
            self.inputs_sent += 1

    def close(self) -> None:
        self._closed = True

    @contextmanager
    def _gate(self) -> Iterator[None]:
        if self._closed:
            self.inputs_refused += 1
            raise HoldRevoked("the console session is closed")
        window = getattr(self._lease, "input_window", None)
        try:
            if callable(window):
                with window():
                    if self._closed:
                        raise HoldRevoked("the console session is closed")
                    yield
                return
            self._lease.require()
        except HoldRevoked:
            self.inputs_refused += 1
            raise
        yield
