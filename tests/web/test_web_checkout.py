"""Checkout: orders with intent AUTHORIZE priced from the quote, authorization, capture only after
the owner probe, and the honest stop."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from keyhold.pay.client import FakePayPal
from keyhold.types import MoneyOp, RescueState, request_id_for
from web.app import SDK_URL


def test_offline_checkout_holds_then_captures_after_a_stable_probe(web) -> None:
    run = web.start("sshd-config-typo")
    assert run.stage == "quoted"
    rec = web.svc.ledger.get(run.rescue_id)
    assert rec.state is RescueState.QUOTED and rec.quote_amount == Decimal("9.00")
    assert rec.probe_spec_hash and rec.probe_spec  # fixed before the order

    order_id = web.order(run)
    order = web.fake.get_order(order_id)
    assert order.intent == "AUTHORIZE" and order.amount == Decimal("9.00")
    assert order.custom_id == rec.probe_spec_hash  # the probe spec hash rides in custom_id
    assert run.stage == "checkout"

    assert web.client.post(f"/api/offline/orders/{order_id}/approve", headers=web.headers()).status_code == 200
    body = web.client.post(f"/api/orders/{order_id}/authorize", headers=web.headers()).json()
    assert body["statusUrl"] == f"/rescues/{run.rescue_id}"

    rec = web.svc.ledger.get(run.rescue_id)
    assert rec.state is RescueState.WARRANTY_OK
    captures = web.fake.all_captures()
    assert len(captures) == 1
    assert captures[0].amount == Decimal("9.00") and captures[0].final_capture
    cap_calls = web.fake.money_calls("capture")
    assert [c.request_id for c in cap_calls] == [request_id_for(rec.id, MoneyOp.CAPTURE)]
    assert web.fake.money_calls("void") == []
    assert web.svc.ledger.check_invariants(rec.id) == []
    assert run.stage == "finished" and web.svc.runs.slot_run() is None


def test_the_order_is_priced_from_the_quote_not_the_browser(web) -> None:
    run = web.start()
    response = web.client.post(
        "/api/orders", json={"rescueId": run.rescue_id, "amount": "0.01", "value": "0.01"}, headers=web.headers()
    )
    assert response.status_code == 201
    assert response.json()["amount"] == "9.00"
    assert web.fake.get_order(response.json()["orderId"]).amount == Decimal("9.00")


def test_creating_the_order_twice_returns_the_same_order(web) -> None:
    run = web.start()
    first = web.order(run)
    second = web.order(run)
    assert first == second
    assert web.fake.order_count() == 1
    assert len(web.fake.money_calls("create_hold")) == 1


def test_authorizing_twice_starts_one_rescue(web) -> None:
    run = web.start()
    result = web.hold(run)
    again = web.client.post(f"/api/orders/{web.svc.ledger.get(result['rescueId']).order_id}/authorize",
                            headers=web.headers())
    assert again.status_code in (200, 409)
    assert len(web.fake.money_calls("authorize")) == 1
    assert len(web.fake.all_captures()) == 1


def test_authorizing_an_unapproved_order_holds_nothing(web) -> None:
    run = web.start()
    order_id = web.order(run)
    response = web.client.post(f"/api/orders/{order_id}/authorize", headers=web.headers())
    assert response.status_code == 422
    assert "ORDER_NOT_APPROVED" in response.json()["error"]
    rec = web.svc.ledger.get(run.rescue_id)
    assert rec.state is RescueState.VOIDED and rec.authorization_id is None
    assert run.stage == "finished" and web.svc.runs.slot_run() is None


def test_another_browser_cannot_pay_for_or_stop_a_run(web) -> None:
    run = web.start()
    order_id = web.order(run)
    stranger = TestClient(web.client.app)
    headers = web.headers(stranger)
    assert stranger.post("/api/orders", json={"rescueId": run.rescue_id}, headers=headers).status_code == 404
    assert stranger.post(f"/api/offline/orders/{order_id}/approve", headers=headers).status_code == 404
    assert stranger.post(f"/api/orders/{order_id}/authorize", headers=headers).status_code == 404
    assert stranger.post(f"/rescues/{run.rescue_id}/stop", headers=headers).status_code == 404
    assert stranger.get(f"/runs/{run.id}").status_code == 404
    assert web.svc.ledger.get(run.rescue_id).state is RescueState.HOLD_PENDING


def test_the_boot_path_fault_shows_a_firewall_refusal_and_captures_t2(web) -> None:
    run = web.start("fstab-missing-disk")
    rec = web.svc.ledger.get(run.rescue_id)
    assert rec.quote["tier"] == "T2" and rec.quote_amount == Decimal("19.00")
    web.hold(run)
    rec = web.svc.ledger.get(run.rescue_id)
    assert rec.state is RescueState.WARRANTY_OK and rec.captured_amount == Decimal("19.00")
    refusals = web.svc.ledger.events(rec.id, kind="agent_firewall_refusal")
    assert len(refusals) == 1 and refusals[0].data["rule"] == "mkfs"
    snap = web.client.get(f"/api/rescues/{rec.id}").json()
    firewall = [e for e in snap["timeline"] if e["group"] == "firewall"]
    assert firewall and "mkfs.ext4 /dev/vdb" in firewall[0]["text"] and firewall[0]["tone"] == "warn"


def test_the_honest_stop_offers_no_hold(web) -> None:
    run = web.start("grub-password")
    assert run.stage == "no_quote" and run.rescue_id is None
    page = web.client.get(f"/runs/{run.id}").text
    assert "No hold offered" in page and "honest stop" in page
    assert web.fake.order_count() == 0
    assert web.svc.ledger.rescues() == []
    assert web.svc.runs.slot_run() is None


def test_run_page_shows_quote_evidence_and_probe(web) -> None:
    run = web.start("sshd-config-typo")
    page = web.client.get(f"/runs/{run.id}").text
    rec = web.svc.ledger.get(run.rescue_id)
    # The evidence line is word for word, with "[FAILED]" marked as a failure.
    assert '<span class="ev-fail">[FAILED]</span> Failed to start ssh.service - OpenBSD Secure Shell server.' in page
    assert "an error in <code>/etc/ssh/sshd_config</code>" in page  # the path in the diagnosis is a mono chip
    assert rec.quote_hash in page and rec.probe_spec_hash in page
    assert "custom_id" in page and "intent <strong>AUTHORIZE</strong>" in page
    assert "Approve<span class=\"wide-only\"> the hold</span> (simulated PayPal)" in page
    assert "web-sdk/v6/core" in page  # data attribute only; offline never loads it
    assert "simulated.test" not in page  # the probe host is not printed


def test_live_checkout_page_uses_js_sdk_v6(make_web, clock) -> None:
    web = make_web(
        mode="live",
        paypal_client_id="test-client-id",
        paypal_client_secret="test-client-secret",
        passcode="pc",
        gateway=FakePayPal(clock=clock),
    )
    web.client.post("/try/unlock", data={"csrf": web.csrf(), "passcode": "pc"})
    run = web.start()
    page = web.client.get(f"/runs/{run.id}").text
    assert f'data-sdk-url="{SDK_URL}"' in page
    assert '<paypal-button id="paypal-button" type="pay" class="paypal-gold" hidden>' in page
    assert "/static/checkout.js" in page
    assert "simulated PayPal" not in page

    token = web.client.get("/api/client-token")
    assert token.status_code == 200 and token.json()["clientToken"].startswith("fake-client-token-")
    assert token.headers["cache-control"] == "no-store"
    # The simulated approval does not exist in live mode.
    order_id = web.order(run)
    assert web.client.post(f"/api/offline/orders/{order_id}/approve", headers=web.headers()).status_code == 404


def test_client_token_needs_the_passcode_and_live_mode(make_web, web, clock) -> None:
    assert web.client.get("/api/client-token").status_code == 404  # offline: no JS SDK
    live = make_web(
        mode="live",
        paypal_client_id="test-client-id",
        paypal_client_secret="test-client-secret",
        passcode="pc",
        gateway=FakePayPal(clock=clock),
    )
    locked = live.client.get("/api/client-token")
    assert locked.status_code == 403 and locked.json()["code"] == "locked"


def test_a_paypal_outage_at_order_time_is_reported_and_retryable(web) -> None:
    run = web.start()
    web.fake.inject("create_hold", "INTERNAL_SERVER_ERROR", times=5)
    response = web.client.post("/api/orders", json={"rescueId": run.rescue_id}, headers=web.headers())
    assert response.status_code == 502 and "INTERNAL_SERVER_ERROR" in response.json()["error"]
    assert run.stage == "quoted"
    web.fake.clear_injections()
    order_id = web.order(run)  # same request id replayed: one order
    assert web.fake.order_count() == 1 and web.fake.get_order(order_id).amount == Decimal("9.00")

