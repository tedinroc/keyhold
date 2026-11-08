"""/webhooks/paypal: raw body stored first, 200 at once, verified with PayPal, deduplicated, and
never applied when the signature check fails."""

from __future__ import annotations

import json

from keyhold.pay.client import FakePayPal
from keyhold.types import RescueState


def finished_run_deliveries(web):
    run = web.start()
    web.hold(run)
    deliveries = web.fake.take_deliveries(web.svc.settings.effective_webhook_id)
    by_type = {json.loads(raw)["event_type"]: (headers, raw) for headers, raw in deliveries}
    return run, by_type


def test_a_delivery_is_stored_raw_then_verified_and_applied(web) -> None:
    run, by_type = finished_run_deliveries(web)
    headers, raw = by_type["PAYMENT.CAPTURE.COMPLETED"]
    response = web.client.post("/webhooks/paypal", content=raw, headers={**headers, "Cookie": "tracking=1"})
    assert response.status_code == 200 and response.json()["received"] is True
    assert "set-cookie" not in response.headers

    stored = web.svc.ledger.deliveries()[-1]
    assert stored.raw == raw  # byte for byte what PayPal sent
    assert stored.verified is True and stored.outcome == "applied"
    assert stored.event_type == "PAYMENT.CAPTURE.COMPLETED" and stored.rescue_id == run.rescue_id
    kept = {k.lower() for k in stored.headers}
    assert "paypal-transmission-sig" in kept and "paypal-transmission-id" in kept
    assert "cookie" not in kept
    assert web.svc.ledger.get(run.rescue_id).capture_webhook_at is not None


def test_a_replayed_delivery_changes_nothing(web) -> None:
    run, by_type = finished_run_deliveries(web)
    headers, raw = by_type["PAYMENT.AUTHORIZATION.CREATED"]
    assert web.client.post("/webhooks/paypal", content=raw, headers=headers).status_code == 200
    before = web.svc.ledger.get(run.rescue_id)
    assert web.client.post("/webhooks/paypal", content=raw, headers=headers).status_code == 200
    after = web.svc.ledger.get(run.rescue_id)
    outcomes = [d.outcome for d in web.svc.ledger.deliveries()]
    assert outcomes == ["applied", "duplicate"]
    assert after.state is before.state is RescueState.WARRANTY_OK
    assert after.auth_webhook_at == before.auth_webhook_at


def test_a_forged_delivery_is_kept_but_never_applied(web) -> None:
    run, by_type = finished_run_deliveries(web)
    headers, raw = by_type["PAYMENT.AUTHORIZATION.CREATED"]
    event = json.loads(raw)
    event["id"] = "WH-FORGED-0001"
    event["event_type"] = "PAYMENT.AUTHORIZATION.VOIDED"
    forged = json.dumps(event).encode()
    response = web.client.post("/webhooks/paypal", content=forged, headers=headers)
    assert response.status_code == 200
    stored = web.svc.ledger.deliveries()[-1]
    assert stored.verified is False and stored.outcome == "unverified"
    assert stored.event_id == "WH-FORGED-0001" and stored.raw == b""  # metadata kept, body pruned
    rec = web.svc.ledger.get(run.rescue_id)
    assert rec.state is RescueState.WARRANTY_OK and rec.void_webhook_at is None
    # The forged event id did not claim anything: the real event still applies later.
    real_headers, real_raw = by_type["PAYMENT.AUTHORIZATION.CREATED"]
    web.client.post("/webhooks/paypal", content=real_raw, headers=real_headers)
    assert web.svc.ledger.deliveries()[-1].outcome == "applied"


def test_malformed_and_oversized_bodies(make_web) -> None:
    web = make_web(max_webhook_bytes=1024)
    headers = web.fake.sign_delivery(b"not json at all", "any")
    response = web.client.post("/webhooks/paypal", content=b"not json at all", headers=headers)
    assert response.status_code == 200
    stored = web.svc.ledger.deliveries()[-1]
    assert stored.outcome == "malformed"

    count = len(web.svc.ledger.deliveries())
    too_big = web.client.post("/webhooks/paypal", content=b"{" + b" " * 2000 + b"}", headers=headers)
    assert too_big.status_code == 413
    assert len(web.svc.ledger.deliveries()) == count


def test_anonymous_bodies_are_refused_before_they_are_stored(make_web) -> None:
    """Regression: any body up to 256 KB was stored without limit, and every status page read
    all stored bodies back on each poll."""
    web = make_web(webhooks_per_minute=5, max_unverified_deliveries=3)
    big = b'{"id":"WH-X","event_type":"PAYMENT.CAPTURE.COMPLETED","resource":{"id":"C"}}' + b" " * 200_000
    # No PayPal transmission headers: refused, nothing stored.
    assert web.client.post("/webhooks/paypal", content=big).status_code == 400
    assert web.svc.ledger.deliveries() == []
    # Fake PayPal headers pass the first check but never verify; their bodies are pruned.
    forged = {**web.fake.sign_delivery(big, "x"), "PAYPAL-TRANSMISSION-SIG": "Zm9yZ2Vk"}
    assert web.client.post("/webhooks/paypal", content=big, headers=forged).status_code == 200
    stored = web.svc.ledger.deliveries()
    assert len(stored) == 1 and stored[0].outcome == "unverified" and stored[0].raw == b""
    # A burst from one address is rate-limited.
    codes = [web.client.post("/webhooks/paypal", content=big, headers=forged).status_code for _ in range(6)]
    assert 429 in codes


def test_a_backlog_of_unverified_bodies_closes_intake(make_web) -> None:
    web = make_web(max_unverified_deliveries=2)
    raw = b'{"id":"WH-Y","event_type":"PAYMENT.CAPTURE.COMPLETED","resource":{"id":"C"}}'
    headers = web.fake.sign_delivery(raw, "x")
    for _ in range(2):
        web.svc.webhooks.receive(headers, raw)  # stored, not processed yet
    assert web.client.post("/webhooks/paypal", content=raw, headers=headers).status_code == 503


def test_live_mode_without_a_webhook_id_verifies_nothing(make_web, clock) -> None:
    gateway = FakePayPal(clock=clock)
    web = make_web(mode="live", paypal_client_id="id", paypal_client_secret="secret", passcode="pc", gateway=gateway)
    raw = json.dumps({"id": "WH-1", "event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {"id": "C1"}}).encode()
    headers = gateway.sign_delivery(raw, "some-webhook-id")
    assert web.client.post("/webhooks/paypal", content=raw, headers=headers).status_code == 200
    stored = web.svc.ledger.deliveries()[-1]
    assert stored.outcome == "unverified" and stored.verified is False
