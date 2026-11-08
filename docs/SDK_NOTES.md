# SDK notes

Names below were read from the installed packages (`uv sync` on 2026-11-07) and checked by running
the SDKs against stub transports with no network. Versions:

| Package | Version | Import name | HTTP library underneath |
|---|---|---|---|
| paypal-server-sdk | 2.4.0 | `paypalserversdk` | `requests` (through `apimatic-requests-client-adapter`) |
| anthropic | 2.6.0 | `anthropic` | `httpx2` (not `httpx`) |
| httpx | 0.28.1 | `httpx` | used for our own REST calls and FastAPI's `TestClient` |

Shared types and interfaces are in `keyhold/types.py`. PayPal and Claude code returns those types,
never SDK model objects.

---

## PayPal: `paypalserversdk`

### Building the client (sandbox only)

```python
from paypalserversdk.paypal_serversdk_client import PaypalServersdkClient
from paypalserversdk.configuration import Environment
from paypalserversdk.http.auth.o_auth_2 import ClientCredentialsAuthCredentials

SANDBOX_BASE = "https://api-m.sandbox.paypal.com"

client = PaypalServersdkClient(
    environment=Environment.SANDBOX,
    timeout=30,
    client_credentials_auth_credentials=ClientCredentialsAuthCredentials(
        o_auth_client_id=os.environ["PAYPAL_CLIENT_ID"],
        o_auth_client_secret=os.environ["PAYPAL_CLIENT_SECRET"],
        o_auth_on_token_update=remember_token,  # optional, see "Bearer token for raw REST calls"
    ),
)
if client.config.get_base_uri() != SANDBOX_BASE:
    raise RuntimeError("Keyhold runs against the PayPal sandbox only")
```

- `Environment` has two members: `PRODUCTION` and `SANDBOX`. The constructor default is `SANDBOX`.
- Base URLs are a class attribute, `Configuration.environments`; `client.config.get_base_uri()`
  returns the one in use. Check it after construction and refuse anything else.
- Do not use `PaypalServersdkClient.from_environment()` or `Configuration.from_environment()`. They
  load a `.env` file and read a generic `ENVIRONMENT` variable that can switch the client to
  production.
- Other constructor arguments: `http_client_instance` (a `requests.Session`; tests mount a stub
  adapter on it), `max_retries` (default 0), `retry_statuses`, `retry_methods`, `backoff_factor`,
  `proxy_settings`, `logging_configuration` (default None, so the SDK logs nothing).

### Calling convention

Every endpoint method takes a single `options` dict with snake_case keys and returns an
`ApiResponse` (`.status_code`, `.headers`, `.body`, `.text`, `.is_success()`, `.is_error()`).
`.body` is the deserialized SDK model, or `None` for an empty body (a 204).

Accessors on the client: `client.orders` (`OrdersController`), `client.payments`
(`PaymentsController`), `client.oauth_2` (the `OAuth2` auth manager), `client.o_auth_authorization`
(`OAuthAuthorizationController`), plus `vault`, `subscriptions`, `transaction_search` (unused).

Do not log an `ApiResponse` or put it in an f-string: its `repr` is the full response text, and for
the token endpoint that includes the access token.

### Mapping `PaymentGateway` to SDK calls

| Keyhold method | SDK call | HTTP | `.body` type |
|---|---|---|---|
| `create_hold` | `client.orders.create_order({"body": OrderRequest(...), "paypal_request_id": rid, "prefer": "return=representation"})` | POST /v2/checkout/orders | `Order` |
| `authorize` | `client.orders.authorize_order({"id": order_id, "paypal_request_id": rid, "prefer": "return=representation"})` | POST /v2/checkout/orders/{id}/authorize | `OrderAuthorizeResponse` |
| `get_order` | `client.orders.get_order({"id": order_id})` | GET /v2/checkout/orders/{id} | `Order` |
| `get_authorization` | `client.payments.get_authorized_payment({"authorization_id": auth_id})` | GET /v2/payments/authorizations/{id} | `PaymentAuthorization` |
| `capture` | `client.payments.capture_authorized_payment({"authorization_id": auth_id, "paypal_request_id": rid, "prefer": "return=representation", "body": CaptureRequest(...)})` | POST /v2/payments/authorizations/{id}/capture | `CapturedPayment` |
| `void` | `client.payments.void_payment({"authorization_id": auth_id, "paypal_request_id": rid, "prefer": "return=representation"})` | POST /v2/payments/authorizations/{id}/void | `PaymentAuthorization` with `prefer`; `None` (204) without |
| `refund` | `client.payments.refund_captured_payment({"capture_id": cap_id, "paypal_request_id": rid, "prefer": "return=representation", "body": RefundRequest(...)})` | POST /v2/payments/captures/{id}/refund | `Refund` |
| `get_capture` | `client.payments.get_captured_payment({"capture_id": cap_id})` | GET /v2/payments/captures/{id} | `CapturedPayment` |
| (reconciler) | `client.payments.get_refund({"refund_id": refund_id})` | GET /v2/payments/refunds/{id} | `Refund` |
| (not used) | `client.payments.reauthorize_payment({...})`, `client.orders.capture_order({...})` | | |

Request bodies (all models import from `paypalserversdk.models.<snake_name>`):

```python
from paypalserversdk.models.order_request import OrderRequest
from paypalserversdk.models.checkout_payment_intent import CheckoutPaymentIntent
from paypalserversdk.models.purchase_unit_request import PurchaseUnitRequest
from paypalserversdk.models.amount_with_breakdown import AmountWithBreakdown
from paypalserversdk.models.capture_request import CaptureRequest
from paypalserversdk.models.refund_request import RefundRequest
from paypalserversdk.models.money import Money

OrderRequest(
    intent=CheckoutPaymentIntent.AUTHORIZE,          # the string "AUTHORIZE"
    purchase_units=[PurchaseUnitRequest(
        amount=AmountWithBreakdown(currency_code="USD", value="19.00"),
        custom_id=spec_hash,                         # max 255 chars
        invoice_id=invoice_id,                       # max 127 chars, must be unique per merchant
        description="...",
    )],
)
CaptureRequest(amount=Money(currency_code="USD", value="19.00"), final_capture=True, invoice_id=invoice_id)
RefundRequest(amount=Money(currency_code="USD", value="19.00"))   # always pass amount
```

- Amounts are strings with two decimals: `str(money(x))` from `keyhold.types`.
- `RefundRequest` without `amount` refunds the whole capture. Always pass it.
- Field names: `PurchaseUnitRequest(amount, reference_id, payee, payment_instruction, description,
  custom_id, invoice_id, soft_descriptor, items, shipping, supplementary_data)`;
  `CaptureRequest(amount, invoice_id, final_capture, payment_instruction, note_to_payer,
  soft_descriptor)`; `RefundRequest(amount, custom_id, invoice_id, note_to_payer,
  payment_instruction)`.

Reading responses:

- Authorization after `authorize_order`:
  `resp.body.purchase_units[0].payments.authorizations[0]` is an
  `AuthorizationWithAdditionalData` with `.id`, `.status`, `.amount.value` (str),
  `.expiration_time` (ISO string), `.custom_id`, `.invoice_id`.
- `PaymentAuthorization` (GET authorization, void): `.id`, `.status`, `.amount`, `.expiration_time`,
  `.custom_id`, `.invoice_id`, `.supplementary_data.related_ids.order_id`.
- `CapturedPayment`: `.id`, `.status`, `.amount`, `.final_capture`, `.invoice_id`, `.custom_id`,
  `.create_time`, `.supplementary_data.related_ids.authorization_id` / `.order_id`.
- `Refund`: `.id`, `.status`, `.amount`, `.invoice_id`, `.custom_id`.
- `Order.links` is a list of `LinkDescription` (`.href`, `.rel`, `.method`). Keyhold keeps the
  `payer-action` link, or else the `approve` link, as `Order.approve_url`; `tools/live_check.py`
  prints it for the buyer, since it has no JS SDK page.
- `PaymentAuthorization.status_details` (`AuthorizationStatusDetails`, `.reason`) is not mapped to
  Keyhold's `Authorization`. `PayPalClient.get_authorization_json` parses `ApiResponse.text` (the
  raw body) so the live check can record it exactly as PayPal sent it.
- Status fields are plain strings. The constant classes are `OrderStatus` (CREATED, SAVED, APPROVED,
  VOIDED, COMPLETED, PAYER_ACTION_REQUIRED), `AuthorizationStatus` (CREATED, CAPTURED, DENIED,
  PARTIALLY_CAPTURED, VOIDED, PENDING), `CaptureStatus` (COMPLETED, DECLINED, PARTIALLY_REFUNDED,
  PENDING, REFUNDED, FAILED), `RefundStatus` (CANCELLED, FAILED, PENDING, COMPLETED).
- An expired authorization has no EXPIRED status. Compare `expiration_time` with the clock, and expect
  AUTHORIZATION_EXPIRED on capture.

### PayPal-Request-Id

Pass it per call as the `"paypal_request_id"` key. Verified with a recording stub transport: the
header goes out on each request with the value given for that call. Endpoints that accept it:
`create_order`, `authorize_order`, `capture_order`, `capture_authorized_payment`,
`reauthorize_payment`, `void_payment`, `refund_captured_payment`. GET calls do not take one.

PayPal keeps request ids for 6 hours on Orders endpoints and 45 days on Payments endpoints
(per the SDK docstrings). A replay inside that window returns the first result (an idempotent replay
of create order answers 200 instead of 201). Keyhold's ids come from
`keyhold.types.request_id_for(rescue_id, op)`: `order-`, `auth-`, `cap-`, `void-`, `refund-`
followed by the rescue id.

### Errors

| Raised | When | Useful fields |
|---|---|---|
| `paypalserversdk.exceptions.error_exception.ErrorException` (subclass of `ApiException`) | PayPal answered with an error status and body: every 4xx, and 500 on Orders endpoints | `.response_code`, `.name`, `.message`, `.debug_id`, `.details[i].issue`, `.details[i].description`, `.details[i].field` |
| `paypalserversdk.exceptions.api_exception.ApiException` | 500 on every Payments endpoint (get/capture/reauthorize/void authorization, get capture, refund); the body is not parsed | `.response_code`, `.reason`, `.response` |
| `apimatic_core.exceptions.auth_validation_exception.AuthValidationException` | No access token: the client id or secret was rejected, or the token endpoint answered 5xx/429 (the SDK swallows that answer) | message only |
| `requests.exceptions.RequestException` (ConnectionError, Timeout) | Transport failure; the SDK does not wrap it | |

Convert all of them to `keyhold.types.PayPalError(issue, ...)`: `issue = details[0].issue` if present,
else `name`, else `f"HTTP_{response_code}"`; `retryable=True` for transport errors, status >= 500,
401, 408, 409 and 429, for a missing token (the money call was never sent; Keyhold asks the token
endpoint once more to tell bad credentials from an outage) and for anything it cannot read (the
outcome is unknown). `retry_after` carries a Retry-After header. Only a refusal
(`PayPalError.refused`: any other 4xx) may close an outbox row, and a void only once GET
authorization shows the hold is gone. A 409 on void means a previous call for that authorization is
still in progress: retry with the same request id. Issue codes in the Payments schema and docs: AUTHORIZATION_EXPIRED, PREVIOUSLY_CAPTURED,
AUTHORIZATION_ALREADY_CAPTURED, PREVIOUSLY_VOIDED, CANNOT_BE_VOIDED, MAX_CAPTURE_AMOUNT_EXCEEDED,
TRANSACTION_REFUSED, REFUND_AMOUNT_EXCEEDED, CAPTURE_FULLY_REFUNDED, DECIMAL_PRECISION. PLAN.md also
names PAYMENT_DENIED and INTERNAL_SERVER_ERROR for FakePayPal.

### Negative testing in the sandbox

Every money method and every GET above accepts `"paypal_mock_response"`, sent as the
`PayPal-Mock-Response` header. Value: `'{"mock_application_codes": "AUTHORIZATION_EXPIRED"}'` (any
issue code). Use it for scripted unhappy-path runs against the real sandbox; tests use FakePayPal.

### Client token for JS SDK v6

```python
token = client.oauth_2.fetch_token(additional_params={"response_type": "client_token", "intent": "sdk_init"})
browser_token = token.access_token   # the field is access_token even for a client token
expires_at = token.expiry            # int, epoch seconds (see the warning below)
```

Warning: in paypal-server-sdk 2.4.0, `OAuthToken.expiry` is computed from naive local time read as
UTC, so on a machine west of UTC it lies hours in the past. Keyhold ignores `expiry` and computes
the deadline from `expires_in` with its own clock (`PayPalClient._token_deadline`).

Verified with a stub: the form body is
`grant_type=client_credentials&response_type=client_token&intent=sdk_init`. `fetch_token` does not
replace the token the SDK uses for API calls. Cache the client token until shortly before `expiry`.
Do not add `domains[]` in the sandbox (PayPal rejects localhost). A PayPal one-time payment session
also works with `createInstance({clientId})`; the client token is needed only if we choose it.

### Webhook signature verification: not in the SDK, call REST

paypal-server-sdk 2.4.0 has no notifications or webhooks controller. Call
`POST {client.config.get_base_uri()}/v1/notifications/verify-webhook-signature` yourself with
`httpx` and a Bearer token.

Bearer token for raw REST calls: keep the token the SDK fetches by passing
`o_auth_on_token_update=callback` to `ClientCredentialsAuthCredentials` (the callback gets an
`OAuthToken` with `.access_token` and `.expiry`, an int epoch). If no token is held yet or it expires
within 60 seconds, call `client.oauth_2.fetch_token()` and keep that one. Do not read
`client.oauth_2._o_auth_token` (private).

Request body (all fields required):

| Body field | Source |
|---|---|
| `auth_algo` | header `PAYPAL-AUTH-ALGO` |
| `cert_url` | header `PAYPAL-CERT-URL` |
| `transmission_id` | header `PAYPAL-TRANSMISSION-ID` |
| `transmission_sig` | header `PAYPAL-TRANSMISSION-SIG` |
| `transmission_time` | header `PAYPAL-TRANSMISSION-TIME` |
| `webhook_id` | our webhook id from the app settings (`PAYPAL_WEBHOOK_ID`) |
| `webhook_event` | the event JSON exactly as received |

Header names arrive in any case; look them up case-insensitively. Response:
`{"verification_status": "SUCCESS" | "FAILURE"}`. Treat anything but SUCCESS (including a missing
header or an HTTP error) as not verified.

Build the body by splicing the raw bytes in, not by `json.loads` then `json.dumps`, so the event is
byte-for-byte what PayPal signed:

```python
meta = json.dumps({...the six string fields...})  # ends with "}"
body = meta[:-1].encode() + b',"webhook_event":' + raw + b"}"
httpx.post(url, content=body, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, timeout=15)
```

Other webhook facts: return 2xx quickly or PayPal retries up to 25 times over 3 days; events from the
dashboard simulator use webhook id `WEBHOOK_ID` and never verify; sandbox delivery takes 8-25 s and
sometimes 1-2 minutes; the endpoint must be public HTTPS. Event types Keyhold handles:
PAYMENT.AUTHORIZATION.CREATED, PAYMENT.AUTHORIZATION.VOIDED, PAYMENT.CAPTURE.COMPLETED,
PAYMENT.CAPTURE.PENDING, PAYMENT.CAPTURE.DECLINED (the Payments v2 name; PAYMENT.CAPTURE.DENIED is
the v1 name and is handled the same way), PAYMENT.CAPTURE.REVERSED, PAYMENT.CAPTURE.REFUNDED,
PAYMENT.REFUND.PENDING and PAYMENT.REFUND.FAILED. Dedupe on the event `id`; an unhandled type does
not claim its id. When verify-webhook-signature cannot be reached, `verify_webhook` raises a
retryable error and the delivery is checked again later instead of being marked unverified.

In tests, give the client an `httpx.Client(transport=httpx.MockTransport(handler))`.

---

## Claude: `anthropic`

### Client

```python
import anthropic
client = anthropic.Anthropic(timeout=120.0, max_retries=2)   # reads ANTHROPIC_API_KEY
```

- The SDK is built on `httpx2`. For a custom transport use
  `anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler))`; an object from the `httpx`
  package is rejected. Tests should use `FakeModel` instead; the mock transport is for testing the
  wrapper itself (verified working, no sockets opened).
- The SDK retries connection errors, 408, 409, 429 and 5xx (2 retries by default).
- `message._request_id` holds the API request id (public despite the underscore).

### Request shape for claude-opus-5-5

```python
resp = client.beta.messages.create(
    model="claude-opus-5-5",
    max_tokens=16000,                                  # thinking counts toward this
    system=SYSTEM,
    messages=messages,
    tools=TOOLS,                                       # fixed for the whole conversation
    output_config={"effort": "medium"},                # low | medium | high | xhigh | max
    thinking={"type": "adaptive", "display": "summarized"},
    betas=["server-side-fallback-2026-07-01"],
    fallbacks="default",
)
```

Verified against the installed SDK: `client.beta.messages.create` accepts `fallbacks`, `betas`,
`output_config`, `thinking`, `tools`, `tool_choice`, `system`, `cache_control`; with the call above the
request carries `anthropic-beta: server-side-fallback-2026-07-01` and the body keys `fallbacks`,
`max_tokens`, `messages`, `model`, `output_config`, `thinking`. The response type is `BetaMessage`.
`client.messages.create` (non-beta) has no `fallbacks` parameter.

- Thinking is always on for this model. Omit `thinking` or send `{"type": "adaptive"}`.
  `{"type": "disabled"}` and `{"type": "enabled", "budget_tokens": N}` return 400. Effort is the only
  depth control and the default is `medium`; set it explicitly anyway.
- `display: "summarized"` returns a readable summary in `thinking` blocks; the default returns empty
  thinking text.
- Fallbacks: the scalar form `fallbacks="default"` needs exactly the beta
  `server-side-fallback-2026-07-01`. The array form `fallbacks=[{"model": "claude-opus-4-8"}]` needs
  `server-side-fallback-2026-06-01`. Mixing a form with the other header is a 400. When a fallback
  ran, `resp.content` can contain a `fallback` block (`BetaFallbackBlock`) and `resp.usage.iterations`
  has an entry with `type == "fallback_message"`; `resp.model` names the model that answered. The
  fallback model cannot read Opus 5.5 thinking blocks; the API drops them silently.
- `stop_reason` values (`anthropic.types.StopReason`): `end_turn`, `max_tokens`, `stop_sequence`,
  `tool_use`, `pause_turn`, `refusal`, `model_context_window_exceeded`.
- Refusal: HTTP 200 with `stop_reason == "refusal"`. `resp.stop_details` is `BetaRefusalStopDetails`
  (`.category` such as "cyber", "bio", "reasoning_extraction" or None, `.explanation`,
  `.recommended_model`) or None. Branch on `stop_reason`, never on `stop_details`. `content` may be
  empty, so never index `content[0]`. A refusal is `AgentOutcome.ERROR` (or a failed free look),
  which voids any hold.

### Tools

- Tool definition: `{"name", "description", "input_schema": {..., "additionalProperties": False},
  "strict": True}`.
- `tool_choice` `{"type": "any"}` or `{"type": "tool", ...}` returns 400 on this model. Use the
  default `auto`, say in the prompt which tool to call, and re-prompt if no `tool_use` block came back.
- Loop: append `resp.content` unchanged as the assistant message (it includes `thinking` blocks that
  must go back as they are). Then send one user message holding a `tool_result` for every `tool_use`
  block: `{"type": "tool_result", "tool_use_id": block.id, "content": ..., "is_error": bool}`.
  `content` can be a string or a list of blocks including images:
  `{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}}`.
- Response blocks: `BetaTextBlock` (`type "text"`), `BetaThinkingBlock` (`"thinking"`, `.thinking`,
  `.signature`), `BetaToolUseBlock` (`"tool_use"`, `.id`, `.name`, `.input`), `BetaFallbackBlock`
  (`"fallback"`). Dispatch on `block.type`, not position. On this model the narration between tool
  calls arrives in `thinking` blocks, not `text` blocks.
- `block.to_dict()` gives the API-shaped dict (unset fields left out) and can be sent back. This is
  how `ModelReply.content` is filled.
- Do not edit or remove earlier messages, old screenshots included. Thinking blocks are bound to the
  exact prefix, and an edited prefix makes replayed blocks fail (400 on newer accounts). Keep the
  turn limit small instead. If pruning is ever needed, send
  `thinking={"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}` with
  the beta `thinking-binding-controls-2026-08-01`.
- Keep screenshots at or below 2000 px per side.

### Structured output (free look)

```python
resp = client.beta.messages.create(
    ...,
    output_config={"effort": "medium", "format": {"type": "json_schema", "schema": QUOTE_SCHEMA}},
)
text = next(b.text for b in resp.content if b.type == "text")   # after checking stop_reason
```

`client.beta.messages.parse(..., output_format=PydanticModel)` also exists and accepts `fallbacks` and
`betas`; the result has `.parsed_output`. The schema needs `"additionalProperties": False` and every
property in `required`. Code still checks the returned tier against the class-to-tier table.

### Errors

`anthropic.APIStatusError` (`.status_code`, `.message`) and its subclasses `BadRequestError`,
`AuthenticationError`, `PermissionDeniedError`, `NotFoundError`, `RateLimitError`,
`InternalServerError`; `anthropic.APIConnectionError`; `anthropic.APITimeoutError`. All derive from
`anthropic.APIError`.
