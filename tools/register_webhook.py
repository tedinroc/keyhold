"""Register Keyhold's webhook with the PayPal sandbox app, or show the one already registered.

    uv run --env-file .env python tools/register_webhook.py https://keyhold.example

The URL must be HTTPS; PayPal posts to ``<base>/webhooks/paypal``. The webhook subscribes to
exactly the events Keyhold handles (``keyhold.pay.webhooks.HANDLED_EVENTS``). If the app already
has a webhook for that URL, its id is printed and nothing changes. Set the printed id as
``PAYPAL_WEBHOOK_ID`` so deliveries are verified with ``verify-webhook-signature``.

Sandbox only: it talks to https://api-m.sandbox.paypal.com and nothing else. It reads
PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET from the environment and never prints them.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from keyhold.pay.client import SANDBOX_BASE  # noqa: E402
from keyhold.pay.webhooks import HANDLED_EVENTS  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].startswith("https://"):
        print("usage: tools/register_webhook.py <https base URL of the deployment>", file=sys.stderr)
        return 2
    url = argv[1].rstrip("/") + "/webhooks/paypal"
    client_id, secret = os.environ.get("PAYPAL_CLIENT_ID", ""), os.environ.get("PAYPAL_CLIENT_SECRET", "")
    if not client_id or not secret:
        print("set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET (sandbox app) first", file=sys.stderr)
        return 2
    with httpx.Client(base_url=SANDBOX_BASE, timeout=30.0) as http:
        token = http.post(
            "/v1/oauth2/token", data={"grant_type": "client_credentials"}, auth=(client_id, secret)
        )
        token.raise_for_status()
        headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
        listed = http.get("/v1/notifications/webhooks", headers=headers)
        listed.raise_for_status()
        for hook in listed.json().get("webhooks", []):
            if hook.get("url") == url:
                have = sorted(e.get("name", "") for e in hook.get("event_types", []))
                missing = sorted(HANDLED_EVENTS - set(have))
                print(f"already registered: {hook['id']} for {url}")
                if missing:
                    print("missing events (update it in the developer dashboard): " + ", ".join(missing))
                return 0
        body = {"url": url, "event_types": [{"name": name} for name in sorted(HANDLED_EVENTS)]}
        made = http.post("/v1/notifications/webhooks", headers=headers, json=body)
        if made.status_code >= 400:
            print(f"PayPal refused the webhook (HTTP {made.status_code}): {made.text[:400]}", file=sys.stderr)
            return 1
        print(f"registered: {made.json()['id']} for {url}")
        print("set PAYPAL_WEBHOOK_ID to that id")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
