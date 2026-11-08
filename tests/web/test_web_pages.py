"""Pages, labels, headers, settings and health."""

from __future__ import annotations

import re

import pytest

from keyhold.pay.client import FakePayPal
from web.app import SDK_URL
from web.config import Settings, SettingsError

PAGES = ["/", "/try", "/judge"]


def test_landing_page_says_what_keyhold_is(web) -> None:
    page = web.client.get("/")
    assert page.status_code == 200
    assert "No hold, no keystrokes." in page.text
    assert "Only your server answering can charge you." in page.text
    for step in ("Free look", "Hold", "The lock", "Capture on your probe only"):
        assert step in page.text
    assert 'href="/try"' in page.text and 'href="/judge"' in page.text


def test_security_headers_on_every_response(web) -> None:
    for path in ("/", "/healthz", "/static/app.css", "/judge.json"):
        response = web.client.get(path)
        csp = response.headers["content-security-policy"]
        assert "script-src 'self' https://*.paypal.com" in csp
        assert "frame-ancestors 'none'" in csp
        assert "'unsafe-inline'" not in csp.split("script-src", 1)[1].split(";", 1)[0]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"


def test_pages_have_no_inline_script(web) -> None:
    run = web.start()
    web.hold(run)
    for path in [*PAGES, f"/runs/{run.id}", f"/rescues/{run.rescue_id}"]:
        html = web.client.get(path).text
        for tag in re.findall(r"<script[^>]*>", html):
            assert "src=" in tag, f"inline script on {path}: {tag}"
        assert "onload=" not in html and "onclick=" not in html


def test_offline_mode_is_labelled_on_every_page(web) -> None:
    run = web.start()
    pages = [*PAGES, f"/runs/{run.id}", "/rescues/does-not-exist"]
    web.hold(run)
    pages.append(f"/rescues/{run.rescue_id}")
    for path in pages:
        html = web.client.get(path).text
        assert "Offline mode." in html, path
        assert "FakePayPal" in html, path
        assert "Simulated server." in html, path


def test_live_mode_says_sandbox_and_never_offline(make_web, clock) -> None:
    web = make_web(
        mode="live",
        paypal_client_id="test-client-id",
        paypal_client_secret="test-client-secret",
        passcode="letmein",
        gateway=FakePayPal(clock=clock),
    )
    for path in PAGES:
        html = web.client.get(path).text
        assert "PayPal sandbox." in html
        assert "Offline mode" not in html


def test_unknown_things_are_404(web) -> None:
    assert web.client.get("/rescues/nope").status_code == 404
    assert web.client.get("/runs/nope").status_code == 404
    assert web.client.get("/api/rescues/nope").status_code == 404
    assert web.client.get("/rescues/nope/events").status_code == 404
    assert web.client.get("/runs/nope/thumbs/1.png").status_code == 404


def test_healthz_reports_mode_without_secrets(make_web) -> None:
    web = make_web(passcode="super-secret-passcode")
    body = web.client.get("/healthz").json()
    assert body["ok"] is True
    assert body["mode"] == "offline" and body["paypal"] == "offline (in memory)"
    assert body["slot"] == "free" and body["rescues"] == 0
    text = web.client.get("/healthz").text
    assert "super-secret-passcode" not in text and "test-secret-key" not in text


def test_static_checkout_script_follows_the_v6_flow(web) -> None:
    js = web.client.get("/static/checkout.js").text
    for name in (
        "createInstance",
        "clientToken",
        "findEligibleMethods",
        'isEligible("paypal")',
        "createPayPalOneTimePaymentSession",
        "onApprove",
        'presentationMode: "auto"',
        "/api/orders",
        "/authorize",
    ):
        assert name in js, name
    assert SDK_URL == "https://www.sandbox.paypal.com/web-sdk/v6/core"


def test_settings_from_environment() -> None:
    offline = Settings.from_env({})
    assert offline.mode == "offline" and offline.world == "simulated"
    assert offline.passcode_required is False and offline.effective_webhook_id

    live = Settings.from_env(
        {"PAYPAL_CLIENT_ID": "id-value", "PAYPAL_CLIENT_SECRET": "secret-value", "KEYHOLD_TRY_PASSCODE": "pc-value"}
    )
    assert live.mode == "live" and live.world == "vm" and live.passcode_required
    assert live.effective_webhook_id == ""  # no webhook id configured: nothing can verify
    for secret in ("id-value", "secret-value", "pc-value"):
        assert secret not in repr(live)

    with pytest.raises(SettingsError, match="PAYPAL_CLIENT_ID"):
        Settings.from_env({"KEYHOLD_MODE": "live"})
    with pytest.raises(SettingsError):
        Settings.from_env({"KEYHOLD_WORLD": "cloud"})
    with pytest.raises(SettingsError, match="KEYHOLD_MAX_TURNS"):
        Settings.from_env({"KEYHOLD_MAX_TURNS": "many"})
    capped = Settings.from_env({"KEYHOLD_MODEL_SPEND_CAP_USD": "2.5", "KEYHOLD_WARRANTY_CHECKS": "3"})
    assert str(capped.spend_cap_usd) == "2.50" and capped.warranty_checks == 3
