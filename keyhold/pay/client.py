"""PayPalClient and FakePayPal, both implementing ``keyhold.types.PaymentGateway``.

PayPalClient wraps the official ``paypal-server-sdk`` (import name ``paypalserversdk``); see
docs/SDK_NOTES.md for the real class and method names. Sandbox only: it refuses any base URL other
than https://api-m.sandbox.paypal.com. It converts SDK models to ``Order``/``Authorization``/
``Capture``/``Refund`` and SDK exceptions to ``PayPalError``, and it never logs tokens or secrets.
Every mutating call requires an explicit request id, sent as PayPal-Request-Id.

FakePayPal keeps the same interface in memory and enforces: capture <= authorized; final_capture
releases the remainder; the same request id returns the same result; void after capture fails;
refund <= captured; injectable errors (AUTHORIZATION_EXPIRED, INTERNAL_SERVER_ERROR,
TRANSACTION_REFUSED, PAYMENT_DENIED, PREVIOUSLY_CAPTURED, REFUND_AMOUNT_EXCEEDED and any other
issue code). It can persist its state to a JSON file so a test can kill a process in the middle
of a call and read PayPal's side back from a fresh process. It also emits signed webhook
deliveries that its own ``verify_webhook`` accepts.

Both classes also offer ``get_order_payments(order_id)`` and ``get_refund(refund_id)``, which the
ledger and reconciler use to find a capture or refund that PayPal made but Keyhold never heard
about (for example after a crash).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import zlib
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from keyhold.types import (
    CURRENCY,
    Authorization,
    Capture,
    Clock,
    Order,
    PayPalError,
    Refund,
    SystemClock,
    money,
)

__all__ = [
    "HANDLED_WEBHOOK_HEADERS",
    "SANDBOX_BASE",
    "FakePayPal",
    "OrderPayments",
    "PayPalClient",
    "from_record",
    "header_value",
    "is_expired",
    "iso_utc",
    "parse_authorization_json",
    "parse_capture_json",
    "parse_paypal_time",
    "parse_refund_json",
    "require_sandbox",
    "to_record",
]

SANDBOX_BASE = "https://api-m.sandbox.paypal.com"
VERIFY_WEBHOOK_PATH = "/v1/notifications/verify-webhook-signature"

# (header name, verify-webhook-signature body field)
HANDLED_WEBHOOK_HEADERS: tuple[tuple[str, str], ...] = (
    ("PAYPAL-AUTH-ALGO", "auth_algo"),
    ("PAYPAL-CERT-URL", "cert_url"),
    ("PAYPAL-TRANSMISSION-ID", "transmission_id"),
    ("PAYPAL-TRANSMISSION-SIG", "transmission_sig"),
    ("PAYPAL-TRANSMISSION-TIME", "transmission_time"),
)

MAX_CUSTOM_ID = 255
MAX_INVOICE_ID = 127
MAX_REQUEST_ID = 108


# --------------------------------------------------------------------------------------------
# Small helpers shared by the client, the fake, the ledger and the webhook code
# --------------------------------------------------------------------------------------------


def require_sandbox(base_uri: str) -> None:
    """Raise unless ``base_uri`` is exactly the PayPal sandbox."""
    if (base_uri or "").rstrip("/") != SANDBOX_BASE:
        raise RuntimeError("Keyhold runs against the PayPal sandbox only; refusing another base URL")


def header_value(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup."""
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value)
    return None


def parse_paypal_time(value: str | None) -> float | None:
    """ISO 8601 time as PayPal sends it ("2026-12-06T10:00:00Z") to epoch seconds, or None."""
    if not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def iso_utc(epoch: float) -> str:
    """Epoch seconds to the ISO 8601 form PayPal uses."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_expired(expiration_time: str | None, now: float) -> bool:
    """True when PayPal's ``expiration_time`` is at or before ``now``. Unknown means not expired."""
    expires = parse_paypal_time(expiration_time)
    return expires is not None and expires <= now


def _require_request_id(request_id: str) -> str:
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("an explicit PayPal-Request-Id is required for every money operation")
    if len(request_id) > MAX_REQUEST_ID:
        raise ValueError(f"PayPal-Request-Id longer than {MAX_REQUEST_ID} characters")
    return request_id


def _positive_money(amount: Decimal | str | int) -> Decimal:
    value = money(amount)
    if value <= 0:
        raise ValueError("amount must be greater than zero")
    return value


def _dec(value: Any) -> Decimal:
    """Money from a PayPal JSON value; missing or malformed values read as 0.00."""
    if value is None:
        return money("0")
    try:
        return money(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return money("0")


def _link_target(links: Any, rel: str, segment: str) -> str | None:
    """Id after ``/{segment}/`` in the href of the first link with ``rel``."""
    for link in links or []:
        if isinstance(link, Mapping):
            link_rel, href = link.get("rel"), link.get("href")
        else:
            link_rel, href = getattr(link, "rel", None), getattr(link, "href", None)
        if link_rel != rel or not href:
            continue
        parts = str(href).rstrip("/").split("/")
        if segment in parts:
            idx = parts.index(segment)
            if idx + 1 < len(parts):
                return parts[idx + 1]
    return None


def _link_href(links: Any, *rels: str) -> str | None:
    """The href of a link whose rel is one of ``rels``, preferring the earlier rels."""
    found: dict[str, str] = {}
    for link in links or []:
        if isinstance(link, Mapping):
            link_rel, href = link.get("rel"), link.get("href")
        else:
            link_rel, href = getattr(link, "rel", None), getattr(link, "href", None)
        if link_rel in rels and href and link_rel not in found:
            found[str(link_rel)] = str(href)
    return next((found[rel] for rel in rels if rel in found), None)


# --------------------------------------------------------------------------------------------
# PayPal JSON (webhook resources, fake state) to Keyhold types
# --------------------------------------------------------------------------------------------


def _related(resource: Mapping[str, Any], key: str) -> str | None:
    supp = resource.get("supplementary_data")
    related = supp.get("related_ids") if isinstance(supp, Mapping) else None
    value = related.get(key) if isinstance(related, Mapping) else None
    return str(value) if value else None


def parse_authorization_json(resource: Mapping[str, Any]) -> Authorization:
    return Authorization(
        id=str(resource["id"]),
        status=str(resource.get("status") or ""),
        amount=_dec((resource.get("amount") or {}).get("value")),
        expiration_time=resource.get("expiration_time"),
        order_id=_related(resource, "order_id"),
        custom_id=resource.get("custom_id"),
        invoice_id=resource.get("invoice_id"),
    )


def parse_capture_json(resource: Mapping[str, Any]) -> Capture:
    auth_id = _related(resource, "authorization_id") or _link_target(resource.get("links"), "up", "authorizations")
    return Capture(
        id=str(resource["id"]),
        status=str(resource.get("status") or ""),
        amount=_dec((resource.get("amount") or {}).get("value")),
        final_capture=bool(resource.get("final_capture", False)),
        authorization_id=auth_id,
        invoice_id=resource.get("invoice_id"),
        create_time=resource.get("create_time"),
    )


def parse_refund_json(resource: Mapping[str, Any]) -> Refund:
    return Refund(
        id=str(resource["id"]),
        status=str(resource.get("status") or ""),
        amount=_dec((resource.get("amount") or {}).get("value")),
        capture_id=_link_target(resource.get("links"), "up", "captures"),
    )


_RECORD_TYPES: dict[str, type] = {
    "Order": Order,
    "Authorization": Authorization,
    "Capture": Capture,
    "Refund": Refund,
}


def to_record(obj: Order | Authorization | Capture | Refund | None) -> dict[str, Any] | None:
    """A PayPal result as a JSON-safe dict (money as strings) with its type name."""
    if obj is None:
        return None
    data = asdict(obj)
    for key, value in list(data.items()):
        if isinstance(value, Decimal):
            data[key] = str(value)
    data["_type"] = type(obj).__name__
    return data


def from_record(data: Mapping[str, Any] | None) -> Order | Authorization | Capture | Refund | None:
    """Inverse of ``to_record``."""
    if data is None:
        return None
    fields = dict(data)
    cls = _RECORD_TYPES[fields.pop("_type")]
    if "amount" in fields and fields["amount"] is not None:
        fields["amount"] = money(fields["amount"])
    return cls(**fields)


@dataclass(frozen=True)
class OrderPayments:
    """Everything PayPal lists under an order's purchase unit."""

    order_id: str
    authorizations: tuple[Authorization, ...] = ()
    captures: tuple[Capture, ...] = ()
    refunds: tuple[Refund, ...] = ()


# --------------------------------------------------------------------------------------------
# PayPalClient: the official SDK
# --------------------------------------------------------------------------------------------


def _g(obj: Any, name: str, default: Any = None) -> Any:
    """getattr for SDK models, which leave unset optional fields off the object entirely."""
    return getattr(obj, name, default) if obj is not None else default


def _sdk_money(obj: Any) -> Decimal:
    return _dec(_g(_g(obj, "amount"), "value"))


def _sdk_related(obj: Any, key: str) -> str | None:
    return _g(_g(_g(obj, "supplementary_data"), "related_ids"), key)


def _sdk_auth(obj: Any, order_id: str | None = None) -> Authorization:
    return Authorization(
        id=str(_g(obj, "id")),
        status=str(_g(obj, "status") or ""),
        amount=_sdk_money(obj),
        expiration_time=_g(obj, "expiration_time"),
        order_id=_sdk_related(obj, "order_id") or order_id,
        custom_id=_g(obj, "custom_id"),
        invoice_id=_g(obj, "invoice_id"),
    )


def _sdk_capture(obj: Any, auth_id: str | None = None) -> Capture:
    return Capture(
        id=str(_g(obj, "id")),
        status=str(_g(obj, "status") or ""),
        amount=_sdk_money(obj),
        final_capture=bool(_g(obj, "final_capture", False)),
        authorization_id=_sdk_related(obj, "authorization_id")
        or _link_target(_g(obj, "links"), "up", "authorizations")
        or auth_id,
        invoice_id=_g(obj, "invoice_id"),
        create_time=_g(obj, "create_time"),
    )


def _sdk_refund(obj: Any, capture_id: str | None = None) -> Refund:
    return Refund(
        id=str(_g(obj, "id")),
        status=str(_g(obj, "status") or ""),
        amount=_sdk_money(obj),
        capture_id=_link_target(_g(obj, "links"), "up", "captures") or capture_id,
    )


def _sdk_order(obj: Any) -> Order:
    units = _g(obj, "purchase_units") or []
    unit = units[0] if units else None
    auths = _g(_g(unit, "payments"), "authorizations") or []
    return Order(
        id=str(_g(obj, "id")),
        status=str(_g(obj, "status") or ""),
        amount=_sdk_money(unit),
        intent=str(_g(obj, "intent") or "AUTHORIZE"),
        custom_id=_g(unit, "custom_id"),
        invoice_id=_g(unit, "invoice_id"),
        authorization_id=_g(auths[0], "id") if auths else None,
        create_time=_g(obj, "create_time"),
        approve_url=_link_href(_g(obj, "links"), "payer-action", "approve"),
    )


def _error_from_body(text: str | None) -> tuple[str | None, str, str | None]:
    """(issue, message, debug_id) from a PayPal error body, best effort."""
    try:
        body = json.loads(text or "")
    except (TypeError, ValueError):
        return None, "", None
    if not isinstance(body, dict):
        return None, "", None
    details = body.get("details") or []
    first = details[0] if details and isinstance(details[0], dict) else {}
    issue = first.get("issue") or body.get("name") or body.get("error")
    message = first.get("description") or body.get("message") or body.get("error_description") or ""
    return issue, str(message), body.get("debug_id")


# HTTP statuses after which trying again can give another answer. 401: the access token was
# refused (it expired, or the token endpoint failed); the request was never carried out.
RETRYABLE_STATUS = frozenset({401, 408, 409, 429})


def _status_retryable(status: int | None) -> bool:
    return status is None or status >= 500 or status in RETRYABLE_STATUS


def _retry_after(exc: BaseException) -> float | None:
    """Seconds from a Retry-After header on the response behind ``exc``, if any."""
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    try:
        value = header_value(headers, "Retry-After")
    except Exception:  # noqa: BLE001 - odd header containers; Retry-After is optional
        return None
    try:
        seconds = float(value) if value is not None else None
    except ValueError:
        return None
    return seconds if seconds is not None and 0 <= seconds <= 3600 else None


def _to_paypal_error(exc: BaseException) -> PayPalError:
    """Convert anything the SDK or its transport raises into ``PayPalError``."""
    import requests
    from apimatic_core.exceptions.auth_validation_exception import AuthValidationException
    from paypalserversdk.exceptions.api_exception import ApiException
    from paypalserversdk.exceptions.error_exception import ErrorException

    if isinstance(exc, ErrorException):
        status = int(exc.response_code)
        details = getattr(exc, "details", None) or []
        first = details[0] if details else None
        issue = _g(first, "issue") or getattr(exc, "name", None) or f"HTTP_{status}"
        message = _g(first, "description") or getattr(exc, "message", None) or ""
        return PayPalError(
            str(issue),
            str(message),
            status=status,
            debug_id=getattr(exc, "debug_id", None),
            retryable=_status_retryable(status),
            retry_after=_retry_after(exc),
        )
    if isinstance(exc, ApiException):
        status = int(exc.response_code)
        text = getattr(getattr(exc, "response", None), "text", None)
        issue, message, debug_id = _error_from_body(text)
        return PayPalError(
            str(issue or f"HTTP_{status}"),
            message,
            status=status,
            debug_id=debug_id,
            retryable=_status_retryable(status),
            retry_after=_retry_after(exc),
        )
    if isinstance(exc, AuthValidationException):
        # The SDK could not get an access token, so the request was never sent. Whether the
        # token endpoint refused the credentials or was down, trying again is safe.
        return PayPalError(
            "AUTHENTICATION_FAILURE",
            "PayPal did not issue an access token",
            status=401,
            retryable=True,
        )
    if isinstance(exc, requests.exceptions.RequestException):
        return PayPalError("TRANSPORT_ERROR", type(exc).__name__, retryable=True)
    # Anything else (for example a response the SDK could not read): the outcome is unknown, so
    # the same request id must be replayed rather than the operation given up.
    return PayPalError("UNEXPECTED_ERROR", type(exc).__name__, retryable=True)


class PayPalClient:
    """``PaymentGateway`` over the official PayPal Server SDK, sandbox only.

    ``http_session`` (a ``requests.Session``) and ``webhook_http`` (an ``httpx.Client``) exist so
    tests can plug in stub transports. ``mock_next(op, issue)`` adds PayPal's
    ``PayPal-Mock-Response`` header to the next call of ``op`` for scripted unhappy-path runs
    against the sandbox.
    """

    MOCKABLE_OPS = frozenset(
        {
            "create_hold",
            "authorize",
            "get_order",
            "get_authorization",
            "capture",
            "void",
            "refund",
            "get_capture",
            "get_refund",
        }
    )

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        environment: Any = None,
        http_session: Any = None,
        webhook_http: httpx.Client | None = None,
        timeout: float = 30.0,
        clock: Clock | None = None,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError("PayPal client id and client secret are required")
        from paypalserversdk.configuration import Environment
        from paypalserversdk.http.auth.o_auth_2 import ClientCredentialsAuthCredentials
        from paypalserversdk.paypal_serversdk_client import PaypalServersdkClient

        env = Environment.SANDBOX if environment is None else environment
        if env != Environment.SANDBOX:
            raise RuntimeError("Keyhold runs against the PayPal sandbox only; refusing another environment")
        self._lock = threading.RLock()
        self._token: tuple[str, float] | None = None  # (access token, usable until) for raw REST
        self._client_token: tuple[str, float] | None = None
        self._mocks: dict[str, list[str]] = {}
        self._clock: Clock = clock or SystemClock()
        self._sdk = PaypalServersdkClient(
            http_client_instance=http_session,
            environment=env,
            timeout=timeout,
            client_credentials_auth_credentials=ClientCredentialsAuthCredentials(
                o_auth_client_id=client_id,
                o_auth_client_secret=client_secret,
                o_auth_on_token_update=self._remember_token,
            ),
        )
        self.base_uri = self._sdk.config.get_base_uri()
        require_sandbox(self.base_uri)
        self._http = webhook_http if webhook_http is not None else httpx.Client(timeout=timeout)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, **kwargs: Any) -> PayPalClient:
        """Build from PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET. Never prints them."""
        env = os.environ if environ is None else environ
        client_id = env.get("PAYPAL_CLIENT_ID", "")
        secret = env.get("PAYPAL_CLIENT_SECRET", "")
        if not client_id or not secret:
            raise RuntimeError("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set (sandbox app)")
        return cls(client_id, secret, **kwargs)

    def __repr__(self) -> str:  # never show credentials or tokens
        return f"PayPalClient(base_uri={self.base_uri!r})"

    # -- plumbing ------------------------------------------------------------------------------

    def _token_deadline(self, token: Any) -> float:
        """When ``token`` stops being usable, by our clock.

        Computed from ``expires_in``. The SDK's own ``token.expiry`` comes from naive local time
        read as UTC, so on a machine west of UTC it is hours in the past.
        """
        expires_in = _g(token, "expires_in")
        try:
            seconds = float(expires_in) if expires_in is not None else 300.0
        except (TypeError, ValueError):
            seconds = 300.0
        return self._clock.now() + seconds

    def _remember_token(self, token: Any) -> None:
        value = _g(token, "access_token")
        if value:
            with self._lock:
                self._token = (str(value), self._token_deadline(token))

    def mock_next(self, op: str, issue: str) -> None:
        """Ask the sandbox to fail the next ``op`` call with ``issue`` (PayPal-Mock-Response)."""
        if op not in self.MOCKABLE_OPS:
            raise ValueError(f"unknown operation {op!r}")
        with self._lock:
            self._mocks.setdefault(op, []).append(issue)

    def _options(self, op: str, options: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            queue = self._mocks.get(op)
            if queue:
                issue = queue.pop(0)
                options["paypal_mock_response"] = json.dumps({"mock_application_codes": issue})
        return options

    def _call(self, fn: Callable[[dict[str, Any]], Any], options: dict[str, Any]) -> Any:
        from apimatic_core.exceptions.auth_validation_exception import AuthValidationException

        try:
            return fn(options)
        except PayPalError:
            raise
        except AuthValidationException as exc:
            raise self._token_problem() from exc
        except Exception as exc:  # converted, never swallowed
            raise _to_paypal_error(exc) from exc

    def _token_problem(self) -> PayPalError:
        """Why the SDK had no access token. The SDK swallows the token endpoint's answer, so ask
        it once more and report what it says. Always retryable: the money call was never sent."""
        try:
            self._sdk.oauth_2.fetch_token()
        except Exception as exc:  # noqa: BLE001 - classified below
            err = _to_paypal_error(exc)
            if err.status in (400, 401):
                return PayPalError(
                    "AUTHENTICATION_FAILURE",
                    "PayPal refused the sandbox client id and secret",
                    status=401,
                    debug_id=err.debug_id,
                    retryable=True,
                )
            return PayPalError(
                "TOKEN_UNAVAILABLE",
                f"PayPal's token endpoint did not answer ({err.issue})",
                status=err.status,
                debug_id=err.debug_id,
                retryable=True,
                retry_after=err.retry_after,
            )
        return PayPalError("TOKEN_UNAVAILABLE", "PayPal issued no access token in time", status=401, retryable=True)

    # -- PaymentGateway ------------------------------------------------------------------------

    def client_token(self) -> str:
        """Browser-safe client token for JS SDK v6, cached until a minute before it expires."""
        with self._lock:
            now = self._clock.now()
            if self._client_token and self._client_token[1] - 60 > now:
                return self._client_token[0]
            try:
                token = self._sdk.oauth_2.fetch_token(
                    additional_params={"response_type": "client_token", "intent": "sdk_init"}
                )
            except Exception as exc:
                raise _to_paypal_error(exc) from exc
            value = _g(token, "access_token")
            if not value:
                raise PayPalError("CLIENT_TOKEN_MISSING", "PayPal returned no client token")
            self._client_token = (str(value), self._token_deadline(token))
            return str(value)

    def create_hold(self, amount: Decimal, custom_id: str, invoice_id: str, request_id: str) -> Order:
        from paypalserversdk.models.amount_with_breakdown import AmountWithBreakdown
        from paypalserversdk.models.checkout_payment_intent import CheckoutPaymentIntent
        from paypalserversdk.models.order_request import OrderRequest
        from paypalserversdk.models.payment_source import PaymentSource
        from paypalserversdk.models.paypal_experience_user_action import PaypalExperienceUserAction
        from paypalserversdk.models.paypal_wallet import PaypalWallet
        from paypalserversdk.models.paypal_wallet_context_shipping_preference import (
            PaypalWalletContextShippingPreference,
        )
        from paypalserversdk.models.paypal_wallet_experience_context import PaypalWalletExperienceContext
        from paypalserversdk.models.purchase_unit_request import PurchaseUnitRequest

        _require_request_id(request_id)
        value = _positive_money(amount)
        if not custom_id or len(custom_id) > MAX_CUSTOM_ID:
            raise ValueError(f"custom_id must be 1-{MAX_CUSTOM_ID} characters")
        if not invoice_id or len(invoice_id) > MAX_INVOICE_ID:
            raise ValueError(f"invoice_id must be 1-{MAX_INVOICE_ID} characters")
        body = OrderRequest(
            intent=CheckoutPaymentIntent.AUTHORIZE,
            purchase_units=[
                PurchaseUnitRequest(
                    amount=AmountWithBreakdown(currency_code=CURRENCY, value=str(value)),
                    custom_id=custom_id,
                    invoice_id=invoice_id,
                    description="Keyhold console rescue (authorization hold)",
                )
            ],
            # A console rescue ships nothing, and the buyer's approval is the final step: the
            # server authorizes as soon as it is approved, with no review page in between.
            payment_source=PaymentSource(
                paypal=PaypalWallet(
                    experience_context=PaypalWalletExperienceContext(
                        brand_name="Keyhold",
                        shipping_preference=PaypalWalletContextShippingPreference.NO_SHIPPING,
                        user_action=PaypalExperienceUserAction.PAY_NOW,
                    )
                )
            ),
        )
        options = self._options(
            "create_hold",
            {"body": body, "paypal_request_id": request_id, "prefer": "return=representation"},
        )
        resp = self._call(self._sdk.orders.create_order, options)
        return _sdk_order(resp.body)

    def authorize(self, order_id: str, request_id: str) -> Authorization:
        _require_request_id(request_id)
        options = self._options(
            "authorize",
            {"id": order_id, "paypal_request_id": request_id, "prefer": "return=representation"},
        )
        resp = self._call(self._sdk.orders.authorize_order, options)
        units = _g(resp.body, "purchase_units") or []
        auths = _g(_g(units[0], "payments"), "authorizations") if units else None
        if auths:
            return _sdk_auth(auths[0], order_id=order_id)
        # PayPal answered with success but the body lists no authorization: read the order back
        # instead of guessing, so a hold PayPal made is never left unrecorded.
        order = self.get_order(order_id)
        if order.authorization_id:
            return self.get_authorization(order.authorization_id)
        raise PayPalError(
            "AUTHORIZATION_MISSING", "authorize answered without an authorization", retryable=True
        )

    def get_order(self, order_id: str) -> Order:
        resp = self._call(self._sdk.orders.get_order, self._options("get_order", {"id": order_id}))
        return _sdk_order(resp.body)

    def get_order_payments(self, order_id: str) -> OrderPayments:
        resp = self._call(self._sdk.orders.get_order, self._options("get_order", {"id": order_id}))
        units = _g(resp.body, "purchase_units") or []
        payments = _g(units[0], "payments") if units else None
        return OrderPayments(
            order_id=order_id,
            authorizations=tuple(_sdk_auth(a, order_id) for a in _g(payments, "authorizations") or []),
            captures=tuple(_sdk_capture(c) for c in _g(payments, "captures") or []),
            refunds=tuple(_sdk_refund(r) for r in _g(payments, "refunds") or []),
        )

    def get_authorization(self, auth_id: str) -> Authorization:
        options = self._options("get_authorization", {"authorization_id": auth_id})
        resp = self._call(self._sdk.payments.get_authorized_payment, options)
        return _sdk_auth(resp.body)

    def get_authorization_json(self, auth_id: str) -> dict[str, Any]:
        """GET authorization exactly as PayPal sent it (the parsed response body, nothing dropped).

        For checks that need fields ``Authorization`` leaves out, such as ``status_details``.
        """
        options = self._options("get_authorization", {"authorization_id": auth_id})
        resp = self._call(self._sdk.payments.get_authorized_payment, options)
        try:
            body = json.loads(resp.text or "")
        except (TypeError, ValueError) as exc:
            raise PayPalError("UNEXPECTED_ERROR", "unreadable authorization", status=resp.status_code,
                              retryable=True) from exc
        if not isinstance(body, dict):
            raise PayPalError("UNEXPECTED_ERROR", "unreadable authorization", status=resp.status_code,
                              retryable=True)
        return body

    def capture(
        self,
        auth_id: str,
        amount: Decimal,
        final_capture: bool,
        invoice_id: str,
        request_id: str,
    ) -> Capture:
        from paypalserversdk.models.capture_request import CaptureRequest
        from paypalserversdk.models.money import Money

        _require_request_id(request_id)
        value = _positive_money(amount)
        if invoice_id and len(invoice_id) > MAX_INVOICE_ID:
            raise ValueError(f"invoice_id must be at most {MAX_INVOICE_ID} characters")
        body = CaptureRequest(
            amount=Money(currency_code=CURRENCY, value=str(value)),
            final_capture=bool(final_capture),
            invoice_id=invoice_id,
        )
        options = self._options(
            "capture",
            {
                "authorization_id": auth_id,
                "paypal_request_id": request_id,
                "prefer": "return=representation",
                "body": body,
            },
        )
        resp = self._call(self._sdk.payments.capture_authorized_payment, options)
        return _sdk_capture(resp.body, auth_id=auth_id)

    def void(self, auth_id: str, request_id: str) -> Authorization | None:
        _require_request_id(request_id)
        options = self._options(
            "void",
            {"authorization_id": auth_id, "paypal_request_id": request_id, "prefer": "return=representation"},
        )
        resp = self._call(self._sdk.payments.void_payment, options)
        return _sdk_auth(resp.body) if resp.body is not None else None

    def refund(self, capture_id: str, amount: Decimal, request_id: str) -> Refund:
        from paypalserversdk.models.money import Money
        from paypalserversdk.models.refund_request import RefundRequest

        _require_request_id(request_id)
        value = _positive_money(amount)
        options = self._options(
            "refund",
            {
                "capture_id": capture_id,
                "paypal_request_id": request_id,
                "prefer": "return=representation",
                "body": RefundRequest(amount=Money(currency_code=CURRENCY, value=str(value))),
            },
        )
        resp = self._call(self._sdk.payments.refund_captured_payment, options)
        return _sdk_refund(resp.body, capture_id=capture_id)

    def get_capture(self, capture_id: str) -> Capture:
        options = self._options("get_capture", {"capture_id": capture_id})
        resp = self._call(self._sdk.payments.get_captured_payment, options)
        return _sdk_capture(resp.body)

    def get_refund(self, refund_id: str) -> Refund:
        resp = self._call(self._sdk.payments.get_refund, self._options("get_refund", {"refund_id": refund_id}))
        return _sdk_refund(resp.body)

    # -- webhooks ------------------------------------------------------------------------------

    def _bearer(self) -> str:
        """An API access token for raw REST calls the SDK does not cover."""
        with self._lock:
            if self._token is None or self._token[1] - 60 <= self._clock.now():
                try:
                    token = self._sdk.oauth_2.fetch_token()
                except Exception as exc:
                    raise _to_paypal_error(exc) from exc
                if not _g(token, "access_token"):
                    raise PayPalError("AUTHENTICATION_FAILURE", "PayPal issued no access token")
                self._remember_token(token)
            assert self._token is not None
            return self._token[0]

    def verify_webhook(self, headers: Mapping[str, str], raw: bytes, webhook_id: str) -> bool:
        """POST /v1/notifications/verify-webhook-signature.

        True when PayPal answers SUCCESS, False when it answers anything else or the delivery
        lacks what verification needs. When verification could not run (no answer, a non-200
        answer, an unreadable body, no access token) it raises a retryable ``PayPalError``, so
        the delivery is checked again later instead of being dropped as unverified.
        """
        if not webhook_id or not raw:
            return False
        meta: dict[str, str] = {}
        for header, field_name in HANDLED_WEBHOOK_HEADERS:
            value = header_value(headers, header)
            if not value:
                return False
            meta[field_name] = value
        meta["webhook_id"] = webhook_id
        try:
            event = json.loads(raw)
        except (TypeError, ValueError):
            return False
        if not isinstance(event, dict):
            return False
        prefix = json.dumps(meta)
        body = prefix[:-1].encode("utf-8") + b',"webhook_event":' + bytes(raw) + b"}"
        try:
            token = self._bearer()
        except PayPalError as err:
            raise PayPalError(err.issue, "no access token to verify the webhook", status=err.status,
                              retryable=True) from err
        try:
            resp = self._http.post(
                self.base_uri + VERIFY_WEBHOOK_PATH,
                content=body,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise PayPalError("TRANSPORT_ERROR", type(exc).__name__, retryable=True) from exc
        if resp.status_code != 200:
            if resp.status_code == 401:
                with self._lock:
                    self._token = None  # fetch a fresh token next time
            issue, message, debug_id = _error_from_body(resp.text)
            raise PayPalError(
                str(issue or f"HTTP_{resp.status_code}"),
                message or "verify-webhook-signature did not answer",
                status=resp.status_code,
                debug_id=debug_id,
                retryable=True,
            )
        try:
            answer = resp.json()
        except ValueError as exc:
            raise PayPalError("UNEXPECTED_ERROR", "unreadable verification answer", status=200,
                              retryable=True) from exc
        return isinstance(answer, dict) and answer.get("verification_status") == "SUCCESS"


# --------------------------------------------------------------------------------------------
# FakePayPal
# --------------------------------------------------------------------------------------------

# HTTP status PayPal uses for each issue code the fake can raise; anything else is 422.
ISSUE_STATUS: Mapping[str, int | None] = {
    "INTERNAL_SERVER_ERROR": 500,
    "SERVICE_UNAVAILABLE": 503,
    "TRANSPORT_ERROR": None,
    "RESOURCE_NOT_FOUND": 404,
    "INVALID_RESOURCE_ID": 404,
    "REQUEST_IN_PROGRESS": 409,
    "PERMISSION_DENIED": 403,
    "AUTHENTICATION_FAILURE": 401,
    "TOKEN_UNAVAILABLE": 503,
    "RATE_LIMIT_REACHED": 429,
    "UNEXPECTED_ERROR": None,
    "INVALID_PARAMETER_VALUE": 400,
}

FAKE_OPS = frozenset(
    {
        "client_token",
        "create_hold",
        "authorize",
        "get_order",
        "get_order_payments",
        "get_authorization",
        "capture",
        "void",
        "refund",
        "get_capture",
        "get_refund",
    }
)
FAKE_MUTATING_OPS = frozenset({"create_hold", "authorize", "capture", "void", "refund"})


def fake_error(issue: str, message: str = "", *, status: int | None = -1) -> PayPalError:
    """A PayPalError shaped the way PayPalClient would raise it for ``issue``."""
    code = ISSUE_STATUS.get(issue, 422) if status == -1 else status
    return PayPalError(issue, message, status=code, debug_id="fake-debug", retryable=_status_retryable(code))


@dataclass
class _Injection:
    op: str
    issue: str
    remaining: int
    after: bool
    status: int | None


@dataclass
class FakeCall:
    """One call FakePayPal received. ``outcome``: ok, replay, error:<ISSUE>."""

    op: str
    request_id: str | None
    target: str | None
    outcome: str


class FakePayPal:
    """In-memory PayPal with the rules Keyhold relies on.

    Buyer-side and PayPal-side events that Keyhold does not cause are test helpers: ``approve``
    (the owner approves the order in the JS SDK), ``expire``, ``void_externally``,
    ``complete_capture``, ``deny_capture``, ``refund_externally``.

    ``inject(op, issue, after=False)`` makes the next ``op`` call fail with ``issue``. With
    ``after=True`` the operation is applied first and only the response is lost, which is what a
    timeout or a 500 after PayPal committed looks like. ``hook(op, phase, request_id)`` is called
    with phase "before" (nothing applied yet) and "after" (applied and persisted, response not yet
    returned); a chaos test kills the process from it.

    With ``state_path`` every change is written to that JSON file before the call returns, and a
    new FakePayPal on the same path sees it.
    """

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        state_path: str | os.PathLike[str] | None = None,
        auto_approve: bool = False,
        auth_valid_seconds: float = 29 * 24 * 3600,
        hook: Callable[[str, str, str | None], None] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._clock: Clock = clock or SystemClock()
        self._path = Path(state_path) if state_path is not None else None
        self.auto_approve = auto_approve
        self.auth_valid_seconds = float(auth_valid_seconds)
        self.hook = hook
        self.capture_status = "COMPLETED"  # status new captures get; set to PENDING to test that path
        self.refund_status = "COMPLETED"  # status new refunds get; set to PENDING to test that path
        self.next_authorization_status = "CREATED"  # status new authorizations get
        self.calls: list[FakeCall] = []
        self._injections: list[_Injection] = []
        self._state: dict[str, Any] = {
            "orders": {},
            "auths": {},
            "captures": {},
            "refunds": {},
            "idempotency": {},
            "invoices": {},
            "webhooks": [],
            "counter": 0,
            "sign_key": secrets.token_hex(32),
        }
        if self._path is not None and self._path.exists():
            self._state = json.loads(self._path.read_text("utf-8"))

    # -- persistence and ids ---------------------------------------------------------------------

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".fakepaypal-", dir=str(self._path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._state, fh, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _new_id(self, prefix: str) -> str:
        self._state["counter"] += 1
        digest = hashlib.sha256(f"{self._state['sign_key']}:{self._state['counter']}".encode()).hexdigest()
        body = format(int(digest[:24], 16), "X").rjust(17, "0")[: 17 - len(prefix)]
        return f"{prefix}{body}"

    def _now_iso(self) -> str:
        return iso_utc(self._clock.now())

    # -- injection and call bookkeeping ----------------------------------------------------------

    def inject(self, op: str, issue: str, *, times: int = 1, after: bool = False, status: int | None = -1) -> None:
        """Fail the next ``times`` calls of ``op`` with PayPal issue ``issue``."""
        if op not in FAKE_OPS:
            raise ValueError(f"unknown operation {op!r}")
        if after and op not in FAKE_MUTATING_OPS:
            raise ValueError("after=True only makes sense for money operations")
        if times < 1:
            raise ValueError("times must be at least 1")
        code = ISSUE_STATUS.get(issue, 422) if status == -1 else status
        with self._lock:
            self._injections.append(_Injection(op, issue, times, after, code))

    def clear_injections(self) -> None:
        with self._lock:
            self._injections.clear()

    def _take_injection(self, op: str) -> _Injection | None:
        for inj in self._injections:
            if inj.op == op and inj.remaining > 0:
                inj.remaining -= 1
                if inj.remaining == 0:
                    self._injections.remove(inj)
                return inj
        return None

    def _raise(self, op: str, request_id: str | None, target: str | None, err: PayPalError) -> None:
        self.calls.append(FakeCall(op, request_id, target, f"error:{err.issue}"))
        raise err

    def _read(self, op: str, target: str, fn: Callable[[], Any]) -> Any:
        with self._lock:
            inj = self._take_injection(op)
            if inj is not None:
                self._raise(op, None, target, fake_error(inj.issue, "injected", status=inj.status))
            try:
                result = fn()
            except PayPalError as err:
                self._raise(op, None, target, err)
            self.calls.append(FakeCall(op, None, target, "ok"))
            return result

    def _mutate(
        self,
        op: str,
        request_id: str,
        target: str,
        fingerprint: dict[str, Any],
        apply: Callable[[], Any],
    ) -> Any:
        _require_request_id(request_id)
        with self._lock:
            if self.hook is not None:
                self.hook(op, "before", request_id)
            inj = self._take_injection(op)
            if inj is not None and not inj.after:
                self._raise(op, request_id, target, fake_error(inj.issue, "injected", status=inj.status))
            key = f"{op}:{request_id}"
            cached = self._state["idempotency"].get(key)
            if cached is not None:
                if cached["fingerprint"] != fingerprint:
                    self._raise(
                        op,
                        request_id,
                        target,
                        fake_error("DUPLICATE_REQUEST_ID", "request id reused with a different request"),
                    )
                result = from_record(cached["result"])
                self.calls.append(FakeCall(op, request_id, target, "replay"))
            else:
                try:
                    result = apply()
                except PayPalError as err:
                    self._raise(op, request_id, target, err)
                self._state["idempotency"][key] = {"fingerprint": fingerprint, "result": to_record(result)}
                self._save()
                self.calls.append(FakeCall(op, request_id, target, "ok"))
            if self.hook is not None:
                self.hook(op, "after", request_id)
            if inj is not None and inj.after:
                raise fake_error(inj.issue, "injected after the operation was applied", status=inj.status)
            return result

    # -- JSON views of state ---------------------------------------------------------------------

    @staticmethod
    def _money_json(value: str) -> dict[str, str]:
        return {"currency_code": CURRENCY, "value": value}

    def _auth_json(self, a: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": a["id"],
            "status": a["status"],
            "amount": self._money_json(a["amount"]),
            "expiration_time": a["expiration_time"],
            "custom_id": a["custom_id"],
            "invoice_id": a["invoice_id"],
            "create_time": a["create_time"],
            "supplementary_data": {"related_ids": {"order_id": a["order_id"]}},
        }

    def _capture_json(self, c: Mapping[str, Any]) -> dict[str, Any]:
        order_id = self._state["auths"][c["auth_id"]]["order_id"]
        return {
            "id": c["id"],
            "status": c["status"],
            "amount": self._money_json(c["amount"]),
            "final_capture": c["final_capture"],
            "invoice_id": c["invoice_id"],
            "create_time": c["create_time"],
            "supplementary_data": {"related_ids": {"authorization_id": c["auth_id"], "order_id": order_id}},
        }

    def _refund_json(self, r: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": r["id"],
            "status": r["status"],
            "amount": self._money_json(r["amount"]),
            "create_time": r["create_time"],
            "links": [{"rel": "up", "method": "GET", "href": f"{SANDBOX_BASE}/v2/payments/captures/{r['capture_id']}"}],
        }

    def _order_obj(self, o: Mapping[str, Any]) -> Order:
        return Order(
            id=o["id"],
            status=o["status"],
            amount=money(o["amount"]),
            intent="AUTHORIZE",
            custom_id=o["custom_id"],
            invoice_id=o["invoice_id"],
            authorization_id=o.get("auth_id"),
            create_time=o["create_time"],
        )

    def _auth_obj(self, a: Mapping[str, Any]) -> Authorization:
        return parse_authorization_json(self._auth_json(a))

    def _capture_obj(self, c: Mapping[str, Any]) -> Capture:
        return parse_capture_json(self._capture_json(c))

    def _refund_obj(self, r: Mapping[str, Any]) -> Refund:
        return parse_refund_json(self._refund_json(r))

    def _emit(self, event_type: str, resource_type: str, resource: dict[str, Any]) -> None:
        event = {
            "id": self._new_id("WH-"),
            "event_version": "1.0",
            "create_time": self._now_iso(),
            "resource_type": resource_type,
            "resource_version": "2.0",
            "event_type": event_type,
            "summary": event_type.replace(".", " ").lower(),
            "resource": resource,
        }
        self._state["webhooks"].append(event)

    def _get(self, kind: str, obj_id: str) -> dict[str, Any]:
        obj = self._state[kind].get(obj_id)
        if obj is None:
            raise fake_error("RESOURCE_NOT_FOUND", f"no such {kind[:-1]} {obj_id}")
        return obj

    def _auth_expired(self, a: Mapping[str, Any]) -> bool:
        return is_expired(a["expiration_time"], self._clock.now())

    # -- PaymentGateway ------------------------------------------------------------------------

    def client_token(self) -> str:
        return self._read("client_token", "-", lambda: "fake-client-token-" + self._state["sign_key"][:12])

    def create_hold(self, amount: Decimal, custom_id: str, invoice_id: str, request_id: str) -> Order:
        value = _positive_money(amount)
        if not custom_id or len(custom_id) > MAX_CUSTOM_ID:
            raise ValueError(f"custom_id must be 1-{MAX_CUSTOM_ID} characters")
        if not invoice_id or len(invoice_id) > MAX_INVOICE_ID:
            raise ValueError(f"invoice_id must be 1-{MAX_INVOICE_ID} characters")

        def apply() -> Order:
            if invoice_id in self._state["invoices"]:
                raise fake_error("DUPLICATE_INVOICE_ID", "invoice id already used")
            order = {
                "id": self._new_id("O"),
                "status": "APPROVED" if self.auto_approve else "CREATED",
                "amount": str(value),
                "custom_id": custom_id,
                "invoice_id": invoice_id,
                "auth_id": None,
                "create_time": self._now_iso(),
            }
            self._state["orders"][order["id"]] = order
            self._state["invoices"][invoice_id] = order["id"]
            return self._order_obj(order)

        fingerprint = {"amount": str(value), "custom_id": custom_id, "invoice_id": invoice_id}
        return self._mutate("create_hold", request_id, invoice_id, fingerprint, apply)

    def authorize(self, order_id: str, request_id: str) -> Authorization:
        def apply() -> Authorization:
            order = self._get("orders", order_id)
            if order.get("auth_id"):
                raise fake_error("ORDER_ALREADY_AUTHORIZED", "order already authorized")
            if order["status"] == "CREATED":
                raise fake_error("ORDER_NOT_APPROVED", "payer has not approved the order")
            if order["status"] != "APPROVED":
                raise fake_error("ORDER_CANNOT_BE_AUTHORIZED", f"order is {order['status']}")
            now = self._clock.now()
            auth = {
                "id": self._new_id("A"),
                "status": self.next_authorization_status,
                "amount": order["amount"],
                "captured": "0.00",
                "expiration_time": iso_utc(now + self.auth_valid_seconds),
                "order_id": order_id,
                "custom_id": order["custom_id"],
                "invoice_id": order["invoice_id"],
                "create_time": iso_utc(now),
            }
            self._state["auths"][auth["id"]] = auth
            order["auth_id"] = auth["id"]
            order["status"] = "COMPLETED"
            self._emit("PAYMENT.AUTHORIZATION.CREATED", "authorization", self._auth_json(auth))
            return self._auth_obj(auth)

        return self._mutate("authorize", request_id, order_id, {"order_id": order_id}, apply)

    def get_order(self, order_id: str) -> Order:
        return self._read("get_order", order_id, lambda: self._order_obj(self._get("orders", order_id)))

    def get_order_payments(self, order_id: str) -> OrderPayments:
        def read() -> OrderPayments:
            order = self._get("orders", order_id)
            auth_id = order.get("auth_id")
            auths = [self._state["auths"][auth_id]] if auth_id else []
            caps = [c for c in self._state["captures"].values() if c["auth_id"] == auth_id] if auth_id else []
            cap_ids = {c["id"] for c in caps}
            refunds = [r for r in self._state["refunds"].values() if r["capture_id"] in cap_ids]
            return OrderPayments(
                order_id=order_id,
                authorizations=tuple(self._auth_obj(a) for a in auths),
                captures=tuple(self._capture_obj(c) for c in caps),
                refunds=tuple(self._refund_obj(r) for r in refunds),
            )

        return self._read("get_order_payments", order_id, read)

    def get_authorization(self, auth_id: str) -> Authorization:
        return self._read("get_authorization", auth_id, lambda: self._auth_obj(self._get("auths", auth_id)))

    def get_authorization_json(self, auth_id: str) -> dict[str, Any]:
        """The authorization as PayPal's JSON. The fake never sets ``status_details``."""
        return self._read(
            "get_authorization", auth_id, lambda: json.loads(json.dumps(self._auth_json(self._get("auths", auth_id))))
        )

    def capture(
        self,
        auth_id: str,
        amount: Decimal,
        final_capture: bool,
        invoice_id: str,
        request_id: str,
    ) -> Capture:
        value = _positive_money(amount)

        def apply() -> Capture:
            auth = self._get("auths", auth_id)
            status = auth["status"]
            if status == "VOIDED":
                raise fake_error("AUTHORIZATION_VOIDED", "authorization was voided")
            if status == "CAPTURED":
                raise fake_error("AUTHORIZATION_ALREADY_CAPTURED", "authorization already captured")
            if status == "DENIED":
                raise fake_error("AUTHORIZATION_DENIED", "authorization was denied")
            if status == "PENDING":
                raise fake_error("AUTHORIZATION_PENDING", "authorization is pending")
            if self._auth_expired(auth):
                raise fake_error("AUTHORIZATION_EXPIRED", "authorization expired")
            captured = money(auth["captured"])
            authorized = money(auth["amount"])
            if captured + value > authorized:
                raise fake_error("MAX_CAPTURE_AMOUNT_EXCEEDED", "capture exceeds the authorized amount")
            cap = {
                "id": self._new_id("C"),
                "status": self.capture_status,
                "amount": str(value),
                "refunded": "0.00",
                "final_capture": bool(final_capture),
                "auth_id": auth_id,
                "invoice_id": invoice_id,
                "create_time": self._now_iso(),
            }
            self._state["captures"][cap["id"]] = cap
            auth["captured"] = str(captured + value)
            # final_capture releases whatever was not captured.
            auth["status"] = "CAPTURED" if final_capture or captured + value == authorized else "PARTIALLY_CAPTURED"
            event = "PAYMENT.CAPTURE.PENDING" if cap["status"] == "PENDING" else "PAYMENT.CAPTURE.COMPLETED"
            self._emit(event, "capture", self._capture_json(cap))
            return self._capture_obj(cap)

        fingerprint = {
            "auth_id": auth_id,
            "amount": str(value),
            "final_capture": bool(final_capture),
            "invoice_id": invoice_id,
        }
        return self._mutate("capture", request_id, auth_id, fingerprint, apply)

    def void(self, auth_id: str, request_id: str) -> Authorization | None:
        def apply() -> Authorization:
            auth = self._get("auths", auth_id)
            status = auth["status"]
            if status == "VOIDED":
                raise fake_error("PREVIOUSLY_VOIDED", "authorization already voided")
            if status in ("CAPTURED", "PARTIALLY_CAPTURED"):
                raise fake_error("PREVIOUSLY_CAPTURED", "a captured authorization cannot be voided")
            if status == "DENIED":
                raise fake_error("CANNOT_BE_VOIDED", "authorization was denied")
            if self._auth_expired(auth):
                raise fake_error("AUTHORIZATION_EXPIRED", "authorization expired")
            auth["status"] = "VOIDED"
            self._emit("PAYMENT.AUTHORIZATION.VOIDED", "authorization", self._auth_json(auth))
            return self._auth_obj(auth)

        return self._mutate("void", request_id, auth_id, {"auth_id": auth_id}, apply)

    def refund(self, capture_id: str, amount: Decimal, request_id: str) -> Refund:
        value = _positive_money(amount)

        def apply() -> Refund:
            cap = self._get("captures", capture_id)
            if cap["status"] == "REFUNDED":
                raise fake_error("CAPTURE_FULLY_REFUNDED", "capture already fully refunded")
            if cap["status"] == "PENDING":
                raise fake_error("PENDING_CAPTURE", "Cannot initiate a refund as the capture is pending")
            if cap["status"] not in ("COMPLETED", "PARTIALLY_REFUNDED"):
                raise fake_error("TRANSACTION_REFUSED", f"capture is {cap['status']}")
            refunded = money(cap["refunded"])
            captured = money(cap["amount"])
            if refunded + value > captured:
                raise fake_error("REFUND_AMOUNT_EXCEEDED", "refund exceeds the captured amount")
            return self._apply_refund(cap, value)

        fingerprint = {"capture_id": capture_id, "amount": str(value)}
        return self._mutate("refund", request_id, capture_id, fingerprint, apply)

    def _apply_refund(self, cap: dict[str, Any], value: Decimal, status: str | None = None) -> Refund:
        """A refund of ``value``. ``cap["refunded"]`` counts COMPLETED and PENDING refunds (both
        reserve the money); the capture's status moves only for COMPLETED ones."""
        ref = {
            "id": self._new_id("R"),
            "status": status or self.refund_status,
            "amount": str(value),
            "capture_id": cap["id"],
            "create_time": self._now_iso(),
        }
        self._state["refunds"][ref["id"]] = ref
        cap["refunded"] = str(money(cap["refunded"]) + value)
        if ref["status"] == "COMPLETED":
            self._settle_capture_refunds(cap)
            self._emit("PAYMENT.CAPTURE.REFUNDED", "refund", self._refund_json(ref))
        else:
            self._emit("PAYMENT.REFUND.PENDING", "refund", self._refund_json(ref))
        return self._refund_obj(ref)

    def _settle_capture_refunds(self, cap: dict[str, Any]) -> None:
        done = sum(
            (money(r["amount"]) for r in self._state["refunds"].values()
             if r["capture_id"] == cap["id"] and r["status"] == "COMPLETED"),
            money("0"),
        )
        if done == money(cap["amount"]):
            cap["status"] = "REFUNDED"
        elif done > 0:
            cap["status"] = "PARTIALLY_REFUNDED"

    def get_capture(self, capture_id: str) -> Capture:
        return self._read("get_capture", capture_id, lambda: self._capture_obj(self._get("captures", capture_id)))

    def get_refund(self, refund_id: str) -> Refund:
        return self._read("get_refund", refund_id, lambda: self._refund_obj(self._get("refunds", refund_id)))

    # -- webhooks ------------------------------------------------------------------------------

    def _signature(self, transmission_id: str, transmission_time: str, webhook_id: str, raw: bytes) -> str:
        message = f"{transmission_id}|{transmission_time}|{webhook_id}|{zlib.crc32(raw)}".encode()
        digest = hmac.new(bytes.fromhex(self._state["sign_key"]), message, hashlib.sha256).digest()
        return base64.b64encode(digest).decode("ascii")

    def sign_delivery(self, raw: bytes, webhook_id: str) -> dict[str, str]:
        """Headers for a delivery of ``raw`` to ``webhook_id`` that ``verify_webhook`` accepts.

        Same signed string as PayPal (transmission id, time, webhook id, CRC32 of the body), signed
        with an HMAC key instead of PayPal's certificate.
        """
        with self._lock:
            transmission_id = self._new_id("T-")
            transmission_time = self._now_iso()
            return {
                "PAYPAL-AUTH-ALGO": "FAKE-HMAC-SHA256",
                "PAYPAL-CERT-URL": "fake://paypal/certs/test",
                "PAYPAL-TRANSMISSION-ID": transmission_id,
                "PAYPAL-TRANSMISSION-SIG": self._signature(transmission_id, transmission_time, webhook_id, raw),
                "PAYPAL-TRANSMISSION-TIME": transmission_time,
                "Content-Type": "application/json",
            }

    def pending_events(self) -> list[dict[str, Any]]:
        """Webhook events emitted so far and not yet taken (oldest first)."""
        with self._lock:
            return [json.loads(json.dumps(e)) for e in self._state["webhooks"]]

    def take_deliveries(self, webhook_id: str) -> list[tuple[dict[str, str], bytes]]:
        """Signed (headers, raw body) for every pending event, oldest first, and clear the queue."""
        with self._lock:
            events, self._state["webhooks"] = self._state["webhooks"], []
            self._save()
            out = []
            for event in events:
                raw = json.dumps(event, separators=(",", ":")).encode("utf-8")
                out.append((self.sign_delivery(raw, webhook_id), raw))
            return out

    def verify_webhook(self, headers: Mapping[str, str], raw: bytes, webhook_id: str) -> bool:
        if not webhook_id or not raw:
            return False
        values = {}
        for header, field_name in HANDLED_WEBHOOK_HEADERS:
            value = header_value(headers, header)
            if not value:
                return False
            values[field_name] = value
        expected = self._signature(values["transmission_id"], values["transmission_time"], webhook_id, bytes(raw))
        return hmac.compare_digest(expected, values["transmission_sig"])

    # -- buyer-side and PayPal-side helpers for tests ------------------------------------------

    def approve(self, order_id: str) -> None:
        """The owner approved the order in the JS SDK payment session."""
        with self._lock:
            order = self._get("orders", order_id)
            if order["status"] == "CREATED":
                order["status"] = "APPROVED"
                self._save()

    def expire(self, auth_id: str) -> None:
        """Move the authorization's expiration_time into the past."""
        with self._lock:
            auth = self._get("auths", auth_id)
            auth["expiration_time"] = iso_utc(self._clock.now() - 1)
            self._save()

    def void_externally(self, auth_id: str) -> None:
        """The authorization was voided outside Keyhold (for example from the PayPal dashboard)."""
        with self._lock:
            auth = self._get("auths", auth_id)
            if auth["status"] in ("CREATED", "PENDING"):
                auth["status"] = "VOIDED"
                self._emit("PAYMENT.AUTHORIZATION.VOIDED", "authorization", self._auth_json(auth))
                self._save()

    def set_authorization_status(self, auth_id: str, status: str) -> None:
        with self._lock:
            auth = self._get("auths", auth_id)
            auth["status"] = status
            if status == "CREATED":
                self._emit("PAYMENT.AUTHORIZATION.CREATED", "authorization", self._auth_json(auth))
            self._save()

    def complete_capture(self, capture_id: str) -> None:
        """A PENDING capture cleared."""
        with self._lock:
            cap = self._get("captures", capture_id)
            if cap["status"] == "PENDING":
                cap["status"] = "COMPLETED"
                self._emit("PAYMENT.CAPTURE.COMPLETED", "capture", self._capture_json(cap))
                self._save()

    def deny_capture(self, capture_id: str, event_type: str = "PAYMENT.CAPTURE.DECLINED") -> None:
        """A PENDING capture was declined (Payments v2 sends PAYMENT.CAPTURE.DECLINED)."""
        with self._lock:
            cap = self._get("captures", capture_id)
            if cap["status"] == "PENDING":
                cap["status"] = "DECLINED"
                self._emit(event_type, "capture", self._capture_json(cap))
                self._save()

    def refund_externally(self, capture_id: str, amount: Decimal | str | None = None) -> Refund:
        """A refund made outside Keyhold: the rest of the capture, or ``amount`` of it."""
        with self._lock:
            cap = self._get("captures", capture_id)
            remaining = money(cap["amount"]) - money(cap["refunded"])
            value = remaining if amount is None else money(amount)
            if value <= 0 or value > remaining:
                raise ValueError("refund amount must be above zero and at most what is left")
            ref = self._apply_refund(cap, value, status="COMPLETED")
            self._save()
            return ref

    def complete_refund(self, refund_id: str) -> None:
        """A PENDING refund went through."""
        with self._lock:
            ref = self._get("refunds", refund_id)
            if ref["status"] == "PENDING":
                ref["status"] = "COMPLETED"
                self._settle_capture_refunds(self._get("captures", ref["capture_id"]))
                self._emit("PAYMENT.CAPTURE.REFUNDED", "refund", self._refund_json(ref))
                self._save()

    def fail_refund(self, refund_id: str, status: str = "FAILED") -> None:
        """A PENDING refund failed (or was cancelled); its money is no longer reserved."""
        if status not in ("FAILED", "CANCELLED"):
            raise ValueError("status must be FAILED or CANCELLED")
        with self._lock:
            ref = self._get("refunds", refund_id)
            if ref["status"] == "PENDING":
                ref["status"] = status
                cap = self._get("captures", ref["capture_id"])
                cap["refunded"] = str(money(cap["refunded"]) - money(ref["amount"]))
                if status == "FAILED":
                    self._emit("PAYMENT.REFUND.FAILED", "refund", self._refund_json(ref))
                self._save()

    # -- inspection ----------------------------------------------------------------------------

    def captures_for(self, auth_id: str) -> list[Capture]:
        with self._lock:
            return [self._capture_obj(c) for c in self._state["captures"].values() if c["auth_id"] == auth_id]

    def refunds_for(self, capture_id: str) -> list[Refund]:
        with self._lock:
            return [self._refund_obj(r) for r in self._state["refunds"].values() if r["capture_id"] == capture_id]

    def all_captures(self) -> list[Capture]:
        with self._lock:
            return [self._capture_obj(c) for c in self._state["captures"].values()]

    def all_authorizations(self) -> list[Authorization]:
        with self._lock:
            return [self._auth_obj(a) for a in self._state["auths"].values()]

    def all_refunds(self) -> list[Refund]:
        with self._lock:
            return [self._refund_obj(r) for r in self._state["refunds"].values()]

    def authorization_released(self, auth_id: str) -> Decimal:
        """Money no longer held: authorized minus captured, once the authorization is final."""
        with self._lock:
            auth = self._get("auths", auth_id)
            if auth["status"] in ("CAPTURED", "VOIDED", "DENIED") or self._auth_expired(auth):
                return money(auth["amount"]) - money(auth["captured"])
            return money("0")

    def order_count(self) -> int:
        with self._lock:
            return len(self._state["orders"])

    def money_calls(self, op: str | None = None) -> list[FakeCall]:
        """Calls of money operations that changed PayPal state (not replays, not errors)."""
        return [c for c in self.calls if c.op in FAKE_MUTATING_OPS and c.outcome == "ok" and (op is None or c.op == op)]
