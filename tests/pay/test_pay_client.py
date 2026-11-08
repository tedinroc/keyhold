"""PayPalClient over the official SDK, run against stub transports (no sockets).

The SDK talks through a ``requests.Session``; a stub adapter mounted on it answers like the
sandbox and records every request. Webhook verification goes through an ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest
import requests
from requests.adapters import BaseAdapter
from requests.models import Response

from keyhold.pay.client import SANDBOX_BASE, PayPalClient, require_sandbox
from keyhold.types import PaymentGateway, PayPalError

SECRET = "sandbox-secret-value-do-not-print"

AUTH = {
    "id": "8AA831015G517922L",
    "status": "CREATED",
    "amount": {"currency_code": "USD", "value": "19.00"},
    "expiration_time": "2027-02-13T10:00:00Z",
    "custom_id": "spec-hash",
    "invoice_id": "keyhold-r1",
    "supplementary_data": {"related_ids": {"order_id": "5O190127TN364715T"}},
}
ORDER = {
    "id": "5O190127TN364715T",
    "status": "CREATED",
    "intent": "AUTHORIZE",
    "create_time": "2027-01-15T10:00:00Z",
    "purchase_units": [
        {
            "amount": {"currency_code": "USD", "value": "19.00"},
            "custom_id": "spec-hash",
            "invoice_id": "keyhold-r1",
        }
    ],
}
CAPTURE = {
    "id": "2GG279541U471931P",
    "status": "COMPLETED",
    "amount": {"currency_code": "USD", "value": "19.00"},
    "final_capture": True,
    "invoice_id": "keyhold-r1-cap",
    "create_time": "2027-01-15T11:00:00Z",
    "supplementary_data": {"related_ids": {"authorization_id": AUTH["id"], "order_id": ORDER["id"]}},
}
REFUND = {
    "id": "1JU08902781691411",
    "status": "COMPLETED",
    "amount": {"currency_code": "USD", "value": "19.00"},
    "links": [{"rel": "up", "method": "GET", "href": f"{SANDBOX_BASE}/v2/payments/captures/{CAPTURE['id']}"}],
}


def error_body(issue: str, name: str = "UNPROCESSABLE_ENTITY") -> dict:
    return {
        "name": name,
        "message": "The requested action could not be performed.",
        "debug_id": "f2c4b5a1e7d3",
        "details": [{"issue": issue, "description": f"{issue} description"}],
    }


class StubSandbox(BaseAdapter):
    """Answers SDK requests from a route table and records them."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[dict] = []
        self.token_requests = 0
        self.routes: list[tuple[str, re.Pattern, int, object]] = []
        self.raise_on: str | None = None
        self.token_status = 200

    def route(self, method: str, path: str, status: int, body: object) -> None:
        self.routes.insert(0, (method, re.compile(path + "$"), status, body))

    def send(self, request, **kwargs):
        url = request.url
        path = url[len(SANDBOX_BASE) :].split("?")[0]
        body = request.body.decode() if isinstance(request.body, bytes) else request.body
        record = {"method": request.method, "path": path, "headers": dict(request.headers), "body": body}
        resp = Response()
        resp.request = request
        resp.url = url
        resp.encoding = "utf-8"
        resp.headers["Content-Type"] = "application/json"
        if path == "/v1/oauth2/token":
            self.token_requests += 1
            record["form"] = parse_qs(body or "")
            self.requests.append(record)
            resp.status_code = self.token_status
            if self.token_status != 200:
                payload = {"error": "invalid_client", "error_description": "Client Authentication failed"}
            elif "client_token" in (body or ""):
                payload = {"access_token": "browser-client-token", "token_type": "Bearer", "expires_in": 900}
            else:
                payload = {"access_token": "api-access-token", "token_type": "Bearer", "expires_in": 32400}
            resp._content = json.dumps(payload).encode()
            return resp
        self.requests.append(record)
        if self.raise_on and self.raise_on in path:
            raise requests.exceptions.ConnectionError("connection reset")
        for method, pattern, status, payload in self.routes:
            if method == request.method and pattern.match(path):
                resp.status_code = status
                resp._content = b"" if payload is None else json.dumps(payload).encode()
                if payload is None:
                    del resp.headers["Content-Type"]
                return resp
        resp.status_code = 404
        resp._content = json.dumps(error_body("INVALID_RESOURCE_ID", "RESOURCE_NOT_FOUND")).encode()
        return resp

    def close(self) -> None:
        pass

    def api_requests(self) -> list[dict]:
        return [r for r in self.requests if r["path"] != "/v1/oauth2/token"]


@pytest.fixture
def stub() -> StubSandbox:
    s = StubSandbox()
    s.route("POST", "/v2/checkout/orders", 201, ORDER)
    s.route("GET", "/v2/checkout/orders/[^/]+", 200, ORDER)
    s.route(
        "POST",
        "/v2/checkout/orders/[^/]+/authorize",
        201,
        {
            **ORDER,
            "status": "COMPLETED",
            "purchase_units": [{**ORDER["purchase_units"][0], "payments": {"authorizations": [AUTH]}}],
        },
    )
    s.route("GET", "/v2/payments/authorizations/[^/]+", 200, AUTH)
    s.route("POST", "/v2/payments/authorizations/[^/]+/capture", 201, CAPTURE)
    s.route("POST", "/v2/payments/authorizations/[^/]+/void", 200, {**AUTH, "status": "VOIDED"})
    s.route("POST", "/v2/payments/captures/[^/]+/refund", 201, REFUND)
    s.route("GET", "/v2/payments/captures/[^/]+", 200, CAPTURE)
    s.route("GET", "/v2/payments/refunds/[^/]+", 200, REFUND)
    return s


class VerifyEndpoint:
    def __init__(self, answer: dict | None = None, status: int = 200) -> None:
        self.answer = answer if answer is not None else {"verification_status": "SUCCESS"}
        self.status = status
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return httpx.Response(self.status, json=self.answer)


def make_client(stub: StubSandbox, verify: VerifyEndpoint | None = None, clock=None) -> PayPalClient:
    session = requests.Session()
    session.mount("https://", stub)
    session.mount("http://", stub)
    webhook_http = httpx.Client(transport=httpx.MockTransport(verify or VerifyEndpoint()))
    return PayPalClient("sandbox-client-id", SECRET, http_session=session, webhook_http=webhook_http, clock=clock)


# ---------------------------------------------------------------------------------------------
# sandbox only, credentials
# ---------------------------------------------------------------------------------------------


def test_sandbox_only() -> None:
    from paypalserversdk.configuration import Environment

    require_sandbox(SANDBOX_BASE)
    require_sandbox(SANDBOX_BASE + "/")
    for other in ("https://api-m.paypal.com", "http://api-m.sandbox.paypal.com", ""):
        with pytest.raises(RuntimeError, match="sandbox only"):
            require_sandbox(other)
    with pytest.raises(RuntimeError, match="sandbox only"):
        PayPalClient("id", SECRET, environment=Environment.PRODUCTION)


def test_credentials_required_and_never_shown(stub: StubSandbox) -> None:
    with pytest.raises(RuntimeError, match="PAYPAL_CLIENT_ID"):
        PayPalClient.from_env({})
    with pytest.raises(ValueError):
        PayPalClient("", SECRET)
    client = make_client(stub)
    assert isinstance(client, PaymentGateway)
    assert client.base_uri == SANDBOX_BASE
    assert SECRET not in repr(client) and SECRET not in str(client)


def test_from_env_reads_environment(stub: StubSandbox) -> None:
    client = PayPalClient.from_env({"PAYPAL_CLIENT_ID": "cid", "PAYPAL_CLIENT_SECRET": SECRET})
    assert client.base_uri == SANDBOX_BASE


# ---------------------------------------------------------------------------------------------
# money calls: request ids, bodies and mapping to Keyhold types
# ---------------------------------------------------------------------------------------------


def test_create_hold_sends_authorize_intent_and_request_id(stub: StubSandbox) -> None:
    client = make_client(stub)
    order = client.create_hold(Decimal("19"), "spec-hash", "keyhold-r1", "order-r1")
    assert order.id == ORDER["id"] and order.status == "CREATED"
    assert order.amount == Decimal("19.00") and order.custom_id == "spec-hash" and order.invoice_id == "keyhold-r1"
    (req,) = stub.api_requests()
    assert req["method"] == "POST" and req["path"] == "/v2/checkout/orders"
    assert req["headers"]["PayPal-Request-Id"] == "order-r1"
    assert req["headers"]["Prefer"] == "return=representation"
    assert req["headers"]["Authorization"] == "Bearer api-access-token"
    body = json.loads(req["body"])
    assert body["intent"] == "AUTHORIZE"
    unit = body["purchase_units"][0]
    assert unit["amount"] == {"currency_code": "USD", "value": "19.00"}
    assert unit["custom_id"] == "spec-hash" and unit["invoice_id"] == "keyhold-r1"
    # A console rescue ships nothing, and approval is the last step: no shipping address step and
    # a "Pay Now" button instead of "Continue to Review Order".
    context = body["payment_source"]["paypal"]["experience_context"]
    assert context["shipping_preference"] == "NO_SHIPPING"
    assert context["user_action"] == "PAY_NOW"
    assert context["brand_name"] == "Keyhold"


def test_create_hold_keeps_the_buyer_approval_link(stub: StubSandbox) -> None:
    payer_action = "https://www.sandbox.paypal.com/checkoutnow?token=" + ORDER["id"]
    links = [
        {"href": f"{SANDBOX_BASE}/v2/checkout/orders/{ORDER['id']}", "rel": "self", "method": "GET"},
        {"href": payer_action, "rel": "payer-action", "method": "GET"},
    ]
    stub.route("POST", "/v2/checkout/orders", 201, {**ORDER, "status": "PAYER_ACTION_REQUIRED", "links": links})
    order = make_client(stub).create_hold(Decimal("12"), "spec-hash", "keyhold-r1", "order-r1")
    assert order.status == "PAYER_ACTION_REQUIRED" and order.approve_url == payer_action

    # An order made without a payment source has an "approve" link instead.
    approve = "https://www.sandbox.paypal.com/checkoutnow?token=other"
    stub.route("GET", "/v2/checkout/orders/[^/]+", 200, {**ORDER, "links": [{"href": approve, "rel": "approve"}]})
    assert make_client(stub).get_order(ORDER["id"]).approve_url == approve
    stub.route("GET", "/v2/checkout/orders/[^/]+", 200, ORDER)
    assert make_client(stub).get_order(ORDER["id"]).approve_url is None


def test_get_authorization_json_keeps_what_paypal_sent(stub: StubSandbox) -> None:
    pending = {**AUTH, "status": "PENDING", "status_details": {"reason": "PENDING_REVIEW"}}
    stub.route("GET", "/v2/payments/authorizations/[^/]+", 200, pending)
    client = make_client(stub)
    body = client.get_authorization_json(AUTH["id"])
    assert body == pending  # status_details included, nothing dropped or renamed
    client.mock_next("get_authorization", "INTERNAL_SERVER_ERROR")
    client.get_authorization_json(AUTH["id"])
    req = stub.api_requests()[-1]
    assert req["path"] == f"/v2/payments/authorizations/{AUTH['id']}"
    assert json.loads(req["headers"]["PayPal-Mock-Response"]) == {"mock_application_codes": "INTERNAL_SERVER_ERROR"}


def test_authorize_returns_the_authorization(stub: StubSandbox) -> None:
    client = make_client(stub)
    auth = client.authorize(ORDER["id"], "auth-r1")
    assert auth.id == AUTH["id"] and auth.status == "CREATED" and auth.amount == Decimal("19.00")
    assert auth.expiration_time == AUTH["expiration_time"] and auth.order_id == ORDER["id"]
    req = stub.api_requests()[-1]
    assert req["path"] == f"/v2/checkout/orders/{ORDER['id']}/authorize"
    assert req["headers"]["PayPal-Request-Id"] == "auth-r1"


def test_authorize_without_authorization_reads_the_order_back(stub: StubSandbox) -> None:
    stub.route("POST", "/v2/checkout/orders/[^/]+/authorize", 201, {**ORDER, "status": "COMPLETED"})
    with pytest.raises(PayPalError) as info:
        make_client(stub).authorize(ORDER["id"], "auth-r1")
    # PayPal said success: the outcome is unknown, so the same request id must be replayed.
    assert info.value.issue == "AUTHORIZATION_MISSING" and info.value.retryable
    # When the order read back lists the authorization, that is the answer.
    stub.route(
        "GET",
        "/v2/checkout/orders/[^/]+",
        200,
        {**ORDER, "status": "COMPLETED",
         "purchase_units": [{**ORDER["purchase_units"][0], "payments": {"authorizations": [AUTH]}}]},
    )
    auth = make_client(stub).authorize(ORDER["id"], "auth-r1")
    assert auth.id == AUTH["id"] and auth.status == "CREATED"


def test_capture_is_final_with_request_id(stub: StubSandbox) -> None:
    client = make_client(stub)
    cap = client.capture(AUTH["id"], Decimal("19.00"), True, "keyhold-r1-cap", "cap-r1")
    assert cap.id == CAPTURE["id"] and cap.status == "COMPLETED" and cap.final_capture is True
    assert cap.amount == Decimal("19.00") and cap.authorization_id == AUTH["id"]
    req = stub.api_requests()[-1]
    assert req["path"] == f"/v2/payments/authorizations/{AUTH['id']}/capture"
    assert req["headers"]["PayPal-Request-Id"] == "cap-r1"
    assert json.loads(req["body"]) == {
        "amount": {"currency_code": "USD", "value": "19.00"},
        "final_capture": True,
        "invoice_id": "keyhold-r1-cap",
    }


def test_void_with_and_without_a_body(stub: StubSandbox) -> None:
    client = make_client(stub)
    voided = client.void(AUTH["id"], "void-r1")
    assert voided is not None and voided.status == "VOIDED"
    assert stub.api_requests()[-1]["headers"]["PayPal-Request-Id"] == "void-r1"
    stub.route("POST", "/v2/payments/authorizations/[^/]+/void", 204, None)
    assert client.void(AUTH["id"], "void-r1") is None


def test_refund_always_sends_the_amount(stub: StubSandbox) -> None:
    client = make_client(stub)
    ref = client.refund(CAPTURE["id"], Decimal("19.00"), "refund-r1")
    assert ref.id == REFUND["id"] and ref.capture_id == CAPTURE["id"] and ref.amount == Decimal("19.00")
    req = stub.api_requests()[-1]
    assert req["headers"]["PayPal-Request-Id"] == "refund-r1"
    assert json.loads(req["body"]) == {"amount": {"currency_code": "USD", "value": "19.00"}}


def test_reads(stub: StubSandbox) -> None:
    client = make_client(stub)
    assert client.get_order(ORDER["id"]).amount == Decimal("19.00")
    assert client.get_authorization(AUTH["id"]).status == "CREATED"
    assert client.get_capture(CAPTURE["id"]).authorization_id == AUTH["id"]
    assert client.get_refund(REFUND["id"]).capture_id == CAPTURE["id"]
    for req in stub.api_requests():
        assert "PayPal-Request-Id" not in req["headers"]


def test_order_payments_lists_captures_and_refunds(stub: StubSandbox) -> None:
    order_cap = {k: v for k, v in CAPTURE.items() if k != "supplementary_data"}
    order_cap["links"] = [
        {"rel": "up", "method": "GET", "href": f"{SANDBOX_BASE}/v2/payments/authorizations/{AUTH['id']}"}
    ]
    full = {
        **ORDER,
        "status": "COMPLETED",
        "purchase_units": [
            {
                **ORDER["purchase_units"][0],
                "payments": {"authorizations": [AUTH], "captures": [order_cap], "refunds": [REFUND]},
            }
        ],
    }
    stub.route("GET", "/v2/checkout/orders/[^/]+", 200, full)
    payments = make_client(stub).get_order_payments(ORDER["id"])
    assert [a.id for a in payments.authorizations] == [AUTH["id"]]
    assert [(c.id, c.authorization_id) for c in payments.captures] == [(CAPTURE["id"], AUTH["id"])]
    assert [(r.id, r.capture_id) for r in payments.refunds] == [(REFUND["id"], CAPTURE["id"])]


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.create_hold(Decimal("19"), "spec", "inv", ""),
        lambda c: c.authorize("O1", " "),
        lambda c: c.capture("A1", Decimal("19"), True, "inv", ""),
        lambda c: c.void("A1", ""),
        lambda c: c.refund("C1", Decimal("19"), ""),
        lambda c: c.capture("A1", Decimal("0"), True, "inv", "cap-r1"),
        lambda c: c.refund("C1", Decimal("-1"), "refund-r1"),
    ],
)
def test_money_calls_need_a_request_id_and_a_positive_amount(stub: StubSandbox, call) -> None:
    with pytest.raises(ValueError):
        call(make_client(stub))
    assert stub.api_requests() == []


def test_mock_response_header_for_negative_testing(stub: StubSandbox) -> None:
    client = make_client(stub)
    client.mock_next("capture", "AUTHORIZATION_EXPIRED")
    client.capture(AUTH["id"], Decimal("19"), True, "inv", "cap-r1")
    client.capture(AUTH["id"], Decimal("19"), True, "inv", "cap-r1")
    first, second = stub.api_requests()[-2:]
    assert json.loads(first["headers"]["PayPal-Mock-Response"]) == {"mock_application_codes": "AUTHORIZATION_EXPIRED"}
    assert "PayPal-Mock-Response" not in second["headers"]
    with pytest.raises(ValueError):
        client.mock_next("transfer", "X")


# ---------------------------------------------------------------------------------------------
# errors become typed PayPalError
# ---------------------------------------------------------------------------------------------


def test_unprocessable_maps_to_issue(stub: StubSandbox) -> None:
    stub.route("POST", "/v2/payments/authorizations/[^/]+/capture", 422, error_body("AUTHORIZATION_EXPIRED"))
    with pytest.raises(PayPalError) as info:
        make_client(stub).capture(AUTH["id"], Decimal("19"), True, "inv", "cap-r1")
    err = info.value
    assert err.issue == "AUTHORIZATION_EXPIRED" and err.status == 422 and not err.retryable
    assert err.debug_id == "f2c4b5a1e7d3"


def test_payments_500_is_retryable(stub: StubSandbox) -> None:
    stub.route(
        "POST",
        "/v2/payments/authorizations/[^/]+/capture",
        500,
        {"name": "INTERNAL_SERVER_ERROR", "message": "An internal server error occurred.", "debug_id": "abc"},
    )
    with pytest.raises(PayPalError) as info:
        make_client(stub).capture(AUTH["id"], Decimal("19"), True, "inv", "cap-r1")
    assert info.value.issue == "INTERNAL_SERVER_ERROR" and info.value.retryable and info.value.status == 500


def test_orders_500_is_retryable(stub: StubSandbox) -> None:
    stub.route("POST", "/v2/checkout/orders", 500, {"name": "INTERNAL_SERVER_ERROR", "message": "x", "debug_id": "d"})
    with pytest.raises(PayPalError) as info:
        make_client(stub).create_hold(Decimal("19"), "spec", "inv", "order-r1")
    assert info.value.issue == "INTERNAL_SERVER_ERROR" and info.value.retryable


def test_void_409_means_retry_with_same_id(stub: StubSandbox) -> None:
    stub.route(
        "POST",
        "/v2/payments/authorizations/[^/]+/void",
        409,
        error_body("PREVIOUS_REQUEST_IN_PROGRESS", "RESOURCE_CONFLICT"),
    )
    with pytest.raises(PayPalError) as info:
        make_client(stub).void(AUTH["id"], "void-r1")
    assert info.value.retryable and info.value.status == 409


def test_not_found_and_body_without_details(stub: StubSandbox) -> None:
    stub.route("GET", "/v2/payments/captures/UNKNOWN", 404, error_body("INVALID_RESOURCE_ID", "RESOURCE_NOT_FOUND"))
    with pytest.raises(PayPalError) as info:
        make_client(stub).get_capture("UNKNOWN")
    assert info.value.issue == "INVALID_RESOURCE_ID" and info.value.status == 404
    stub.route("GET", "/v2/payments/captures/[^/]+", 403, {"name": "NOT_AUTHORIZED", "message": "no"})
    with pytest.raises(PayPalError) as info:
        make_client(stub).get_capture("C1")
    assert info.value.issue == "NOT_AUTHORIZED" and not info.value.retryable


def test_transport_failure_is_retryable(stub: StubSandbox) -> None:
    stub.raise_on = "/capture"
    with pytest.raises(PayPalError) as info:
        make_client(stub).capture(AUTH["id"], Decimal("19"), True, "inv", "cap-r1")
    assert info.value.issue == "TRANSPORT_ERROR" and info.value.retryable


def test_rejected_credentials(stub: StubSandbox) -> None:
    stub.token_status = 401
    with pytest.raises(PayPalError) as info:
        make_client(stub).get_authorization(AUTH["id"])
    # The call was never sent, so replaying it is safe once the credentials are fixed.
    assert info.value.issue == "AUTHENTICATION_FAILURE" and info.value.retryable
    assert not info.value.refused
    assert SECRET not in str(info.value)


@pytest.mark.parametrize("token_status", [500, 503, 429])
def test_token_endpoint_outage_during_capture_is_retryable(stub: StubSandbox, token_status: int) -> None:
    """Regression: the SDK swallows the token endpoint's 5xx/429 and raises
    AuthValidationException, which used to become a final AUTHENTICATION_FAILURE. Payments then
    closed cap-{rescue_id} as FAILED and the orchestrator voided a hold whose probe had passed."""
    stub.token_status = token_status
    with pytest.raises(PayPalError) as info:
        make_client(stub).capture(AUTH["id"], Decimal("19"), True, "inv", "cap-r1")
    err = info.value
    assert err.issue == "TOKEN_UNAVAILABLE" and err.retryable and not err.refused
    assert err.status == token_status
    assert stub.api_requests() == []  # the capture itself never reached PayPal


@pytest.mark.parametrize("path", ["/v2/payments/authorizations/[^/]+/capture", "/v2/payments/authorizations/[^/]+/void"])
def test_rate_limit_is_retryable_with_retry_after(stub: StubSandbox, path: str) -> None:
    stub.route("POST", path, 429, error_body("RATE_LIMIT_REACHED", "RATE_LIMIT_REACHED"))
    client = make_client(stub)
    with pytest.raises(PayPalError) as info:
        if path.endswith("capture"):
            client.capture(AUTH["id"], Decimal("19"), True, "inv", "cap-r1")
        else:
            client.void(AUTH["id"], "void-r1")
    assert info.value.issue == "RATE_LIMIT_REACHED" and info.value.status == 429
    assert info.value.retryable and not info.value.refused


# ---------------------------------------------------------------------------------------------
# client token for JS SDK v6
# ---------------------------------------------------------------------------------------------


def test_client_token_is_fetched_once_and_cached(stub: StubSandbox, clock) -> None:
    client = make_client(stub, clock=clock)
    assert client.client_token() == "browser-client-token"
    clock.advance(800)  # still more than a minute before the 900 s expiry
    assert client.client_token() == "browser-client-token"
    token_calls = [r for r in stub.requests if r["path"] == "/v1/oauth2/token"]
    assert len(token_calls) == 1
    clock.advance(60)  # inside the last minute: fetch a fresh one
    client.client_token()
    token_calls = [r for r in stub.requests if r["path"] == "/v1/oauth2/token"]
    assert len(token_calls) == 2
    form = token_calls[0]["form"]
    assert form == {"grant_type": ["client_credentials"], "response_type": ["client_token"], "intent": ["sdk_init"]}
    assert "domains[]" not in token_calls[0]["body"]


# ---------------------------------------------------------------------------------------------
# webhook verification through REST
# ---------------------------------------------------------------------------------------------

DELIVERY_HEADERS = {
    "paypal-auth-algo": "SHA256withRSA",
    "Paypal-Cert-Url": f"{SANDBOX_BASE}/v1/notifications/certs/CERT-360caa42-fca2a594-1d93a270",
    "PAYPAL-TRANSMISSION-ID": "69cd13f0-d67a-11e5-baa3-778b53f4ae55",
    "paypal-transmission-sig": "c2lnbmF0dXJlLXBsYWNlaG9sZGVyLWZvci10ZXN0cw==",
    "PAYPAL-TRANSMISSION-TIME": "2027-01-15T10:00:00Z",
    "Content-Type": "application/json",
}


def test_verify_webhook_splices_the_raw_body(stub: StubSandbox) -> None:
    endpoint = VerifyEndpoint()
    client = make_client(stub, endpoint)
    # Unusual spacing and key order: the event must reach PayPal exactly as it was signed.
    raw = (
        b'{ "id":"WH-1",  "event_type":"PAYMENT.CAPTURE.COMPLETED","resource":{"id":"C1","amount":{"value":"19.00"}} }'
    )
    assert client.verify_webhook(DELIVERY_HEADERS, raw, "WH-ID-123") is True
    (req,) = endpoint.seen
    assert str(req.url) == SANDBOX_BASE + "/v1/notifications/verify-webhook-signature"
    assert req.headers["Authorization"] == "Bearer api-access-token"
    assert req.content.endswith(b',"webhook_event":' + raw + b"}")
    body = json.loads(req.content)
    assert body["webhook_id"] == "WH-ID-123"
    assert body["transmission_id"] == DELIVERY_HEADERS["PAYPAL-TRANSMISSION-ID"]
    assert body["auth_algo"] == "SHA256withRSA" and body["cert_url"].startswith(SANDBOX_BASE)
    assert body["webhook_event"]["id"] == "WH-1"
    # The bearer token is reused, not fetched per delivery.
    assert client.verify_webhook(DELIVERY_HEADERS, raw, "WH-ID-123") is True
    assert stub.token_requests == 1


@pytest.mark.parametrize("answer", [{"verification_status": "FAILURE"}, {}])
def test_verify_webhook_anything_but_success_is_false(stub: StubSandbox, answer: dict) -> None:
    client = make_client(stub, VerifyEndpoint(answer, 200))
    assert client.verify_webhook(DELIVERY_HEADERS, b'{"id":"WH-1"}', "WH-ID-123") is False


@pytest.mark.parametrize("status", [400, 500, 503])
def test_verify_webhook_that_could_not_run_raises_retryable(stub: StubSandbox, status: int) -> None:
    """Regression: a verify endpoint outage used to read as a FAILURE, so a genuine event was
    stored as unverified for good."""
    client = make_client(stub, VerifyEndpoint({"name": "INTERNAL_SERVER_ERROR"}, status))
    with pytest.raises(PayPalError) as info:
        client.verify_webhook(DELIVERY_HEADERS, b'{"id":"WH-1"}', "WH-ID-123")
    assert info.value.retryable


def test_verify_webhook_refuses_incomplete_deliveries_without_calling(stub: StubSandbox) -> None:
    endpoint = VerifyEndpoint()
    client = make_client(stub, endpoint)
    missing = {k: v for k, v in DELIVERY_HEADERS.items() if k != "paypal-transmission-sig"}
    assert client.verify_webhook(missing, b'{"id":"WH-1"}', "WH-ID-123") is False
    assert client.verify_webhook(DELIVERY_HEADERS, b"not json", "WH-ID-123") is False
    assert client.verify_webhook(DELIVERY_HEADERS, b'{"id":"WH-1"}', "") is False
    assert endpoint.seen == []


def test_verify_webhook_network_failure_is_retryable(stub: StubSandbox) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    session = requests.Session()
    session.mount("https://", stub)
    client = PayPalClient(
        "cid", SECRET, http_session=session, webhook_http=httpx.Client(transport=httpx.MockTransport(boom))
    )
    with pytest.raises(PayPalError) as info:
        client.verify_webhook(DELIVERY_HEADERS, b'{"id":"WH-1"}', "WH-ID-123")
    assert info.value.issue == "TRANSPORT_ERROR" and info.value.retryable


def test_client_token_refused(stub: StubSandbox) -> None:
    stub.token_status = 401
    with pytest.raises(PayPalError) as info:
        make_client(stub).client_token()
    assert info.value.status == 401 and not info.value.refused
    assert SECRET not in str(info.value) and "sandbox-client-id" not in str(info.value)


def test_every_money_call_carries_its_request_id(stub: StubSandbox) -> None:
    client = make_client(stub)
    client.create_hold(Decimal("19"), "spec", "inv", "order-r9")
    client.authorize(ORDER["id"], "auth-r9")
    client.capture(AUTH["id"], Decimal("19"), True, "inv-cap", "cap-r9")
    client.void(AUTH["id"], "void-r9")
    client.refund(CAPTURE["id"], Decimal("19"), "refund-r9")
    posts = [r for r in stub.api_requests() if r["method"] == "POST"]
    assert [r["headers"]["PayPal-Request-Id"] for r in posts] == [
        "order-r9",
        "auth-r9",
        "cap-r9",
        "void-r9",
        "refund-r9",
    ]
