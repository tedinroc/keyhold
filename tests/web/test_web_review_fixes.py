"""Regression tests for the web review findings: proxy addresses, passcode rotation, the public
rescue API, warranties for captures the reconciler completes, late holds, declined funding
sources, the webhook-id warning and model spend with prompt caching."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from keyhold.types import ModelReply, MoneyOp, OutboxStatus, RescueState
from web.security import Session, SessionCodec
from web.spend import reply_cost


def unlock(web, passcode: str, client: TestClient | None = None, **headers: str):
    client = client or web.client
    return client.post(
        "/try/unlock", data={"csrf": web.csrf(client), "passcode": passcode}, headers=headers, follow_redirects=False
    )


# -- client address and sessions --------------------------------------------------------------------


def test_a_spoofed_first_forwarded_entry_does_not_dodge_the_limit(make_web) -> None:
    """Regression: with KEYHOLD_TRUST_PROXY the first X-Forwarded-For entry (set by the client)
    was the address, so changing it per request bypassed every per-address limit."""
    web = make_web(passcode="open-sesame", unlock_attempts=3, trust_proxy=True)
    codes = [
        unlock(web, "wrong", **{"X-Forwarded-For": f"10.9.{i}.1, 203.0.113.7"}).status_code for i in range(6)
    ]
    assert codes[:3] == [401, 401, 401] and set(codes[3:]) == {429}


def test_the_edge_workers_client_address_counts_only_with_its_key(make_web) -> None:
    """Behind the pages.dev edge every request reaches the proxy from a Cloudflare address, so the
    edge passes the visitor's address in X-Keyhold-Client-IP with a shared key. Without the key
    the header is ignored, so a client cannot pick its own address."""
    web = make_web(passcode="open-sesame", unlock_attempts=2, trust_proxy=True, edge_key="edge-secret-123")
    edge = {"X-Forwarded-For": "104.16.0.1", "X-Keyhold-Edge-Key": "edge-secret-123"}
    a = [unlock(web, "wrong", **edge, **{"X-Keyhold-Client-IP": "198.51.100.1"}).status_code for _ in range(3)]
    b = [unlock(web, "wrong", **edge, **{"X-Keyhold-Client-IP": "198.51.100.2"}).status_code for _ in range(2)]
    assert a == [401, 401, 429] and b == [401, 401]  # two visitors behind one edge address, two limits
    forged = {"X-Forwarded-For": "203.0.113.9", "X-Keyhold-Edge-Key": "wrong"}
    c = [unlock(web, "wrong", **forged, **{"X-Keyhold-Client-IP": f"198.51.100.{9 + i}"}).status_code for i in range(3)]
    assert c == [401, 401, 429]  # a wrong key falls back to the proxy's address, so changing the header does not help


def test_changing_the_passcode_locks_browsers_unlocked_with_the_old_one() -> None:
    old = SessionCodec("secret-key-0123456789", passcode="old-pass")
    session = Session.new(1000.0)
    session.unlock()
    cookie = old.encode(session)
    assert old.decode(cookie, 1001.0).unlocked
    rotated = SessionCodec("secret-key-0123456789", passcode="new-pass")  # same key, new passcode
    decoded = rotated.decode(cookie, 1001.0)
    assert decoded is not None and not decoded.unlocked  # still a session, no longer unlocked


# -- public rescue API -----------------------------------------------------------------------------------


def test_the_public_rescue_api_is_rate_limited_and_cached(make_web) -> None:
    """Regression: anonymous GET /api/rescues/{id} made up to four live PayPal reads each, with
    no limit, so a loop could exhaust the sandbox app's rate limit."""
    web = make_web(api_calls_per_minute=20)
    run = web.start()
    web.hold(run)
    rid = run.rescue_id
    before = len([c for c in web.fake.calls if c.op.startswith("get_")])
    anonymous = TestClient(web.client.app)
    codes = [anonymous.get(f"/api/rescues/{rid}").status_code for _ in range(40)]
    assert codes.count(200) <= 20 and 429 in codes
    reads = len([c for c in web.fake.calls if c.op.startswith("get_")]) - before
    assert reads <= 4  # one live read for the whole burst (clock does not move)


# -- warranties and late holds -----------------------------------------------------------------------------


def test_a_capture_the_reconciler_completes_gets_its_warranty(make_web) -> None:
    """Regression: a capture completed by the reconciler stayed CAPTURED for good, with no
    warranty, so a later failure was never refunded."""
    web = make_web(capture_wait_s=1.0)
    run = web.start()
    web.fake.inject("capture", "INTERNAL_SERVER_ERROR", times=20, after=True)
    web.hold(run)
    rid = run.rescue_id
    assert web.svc.ledger.get_op(rid, MoneyOp.CAPTURE).status is OutboxStatus.PENDING
    assert run.stage == "finished"
    web.fake.clear_injections()
    web.svc.reconcile_once()
    rec = web.svc.ledger.get(rid)
    assert web.svc.ledger.events(rid, kind="warranty_started")
    assert rec.state is RescueState.WARRANTY_OK and rec.captured_amount == Decimal("9.00")
    assert len(web.fake.all_captures()) == 1


def test_a_run_replays_a_capture_whose_answer_was_lost(make_web) -> None:
    web = make_web()
    run = web.start()
    web.fake.inject("capture", "INTERNAL_SERVER_ERROR", times=3, after=True)
    web.hold(run)
    rec = web.svc.ledger.get(run.rescue_id)
    assert rec.state is RescueState.WARRANTY_OK
    assert len(web.fake.all_captures()) == 1


def test_a_hold_paypal_confirms_late_starts_the_rescue(make_web) -> None:
    """Regression: an authorization that was PENDING at checkout and cleared later was voided at
    the quote deadline as 'not paid', although PayPal held the money."""
    web = make_web()
    run = web.start()
    web.fake.next_authorization_status = "PENDING"
    order_id = web.order(run)
    web.client.post(f"/api/offline/orders/{order_id}/approve", headers=web.headers())
    body = web.client.post(f"/api/orders/{order_id}/authorize", headers=web.headers()).json()
    assert body["state"] == "HOLD_PENDING" and run.stage == "checkout"
    rec = web.svc.ledger.get(run.rescue_id)
    web.fake.set_authorization_status(rec.authorization_id, "CREATED")
    web.svc.pump_offline_webhooks(delay_s=0)
    assert web.svc.ledger.get(run.rescue_id).state is RescueState.HELD
    web.svc.runs.sweep()
    rec = web.svc.ledger.get(run.rescue_id)
    assert rec.state is RescueState.WARRANTY_OK and run.stage == "finished"


def test_a_declined_funding_source_lets_the_buyer_try_another(make_web) -> None:
    web = make_web()
    run = web.start()
    order_id = web.order(run)
    web.client.post(f"/api/offline/orders/{order_id}/approve", headers=web.headers())
    web.fake.inject("authorize", "INSTRUMENT_DECLINED")
    declined = web.client.post(f"/api/orders/{order_id}/authorize", headers=web.headers())
    assert declined.status_code == 402 and declined.json()["code"] == "instrument_declined"
    assert run.stage == "checkout"
    assert web.svc.ledger.get(run.rescue_id).state is RescueState.HOLD_PENDING
    again = web.client.post(f"/api/orders/{order_id}/authorize", headers=web.headers())
    assert again.status_code == 200
    assert web.svc.ledger.get(run.rescue_id).state is RescueState.WARRANTY_OK


# -- configuration ----------------------------------------------------------------------------------------------


def test_live_mode_without_a_webhook_id_says_so(make_web, clock) -> None:
    from keyhold.pay.client import FakePayPal

    web = make_web(mode="live", paypal_client_id="id", paypal_client_secret="secret", passcode="pc",
                   gateway=FakePayPal(clock=clock))
    assert "Webhooks not verifiable" in web.client.get("/").text
    health = web.client.get("/healthz").json()
    assert health["webhooks"].startswith("not verifiable") and "unverified_webhooks" in health


# -- model spend -------------------------------------------------------------------------------------------------


def test_reply_cost_counts_cache_writes_and_reads() -> None:
    """Regression: cache writes and reads were not priced at all, so a cached agent turn was
    metered at a fraction of its cost."""
    reply = ModelReply(
        stop_reason="tool_use", content=[], input_tokens=12, output_tokens=1500,
        cache_creation_input_tokens=3000, cache_read_input_tokens=180_000,
    )
    expected = (Decimal(12) * 4 + Decimal(1500) * 20 + Decimal(3000) * 5 + Decimal(180_000) * Decimal("0.4")) / 1_000_000
    assert reply_cost(reply) == expected.quantize(Decimal("0.000001"))
    assert reply_cost(reply) > Decimal("0.11")


def test_reply_from_message_keeps_cache_tokens() -> None:
    from types import SimpleNamespace

    from keyhold.agent.model import reply_from_message

    usage = SimpleNamespace(input_tokens=5, output_tokens=7, cache_creation_input_tokens=11, cache_read_input_tokens=13)
    reply = reply_from_message(SimpleNamespace(content=[], stop_reason="end_turn", model="m", usage=usage))
    assert (reply.cache_creation_input_tokens, reply.cache_read_input_tokens) == (11, 13)
