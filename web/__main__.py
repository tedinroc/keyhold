"""Run the web app: ``uv run python -m web``.

Listens on loopback port 8000 unless KEYHOLD_HOST / KEYHOLD_PORT say otherwise. Put it behind an
HTTPS proxy to receive PayPal webhooks (set KEYHOLD_TRUST_PROXY=1 there).
"""

from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    from keyhold.console.docker import LOOPBACK

    host = os.environ.get("KEYHOLD_HOST", "").strip() or LOOPBACK
    port = int(os.environ.get("KEYHOLD_PORT", "").strip() or "8000")
    uvicorn.run(
        "web.app:create_app",
        factory=True,
        host=host,
        port=port,
        proxy_headers=os.environ.get("KEYHOLD_TRUST_PROXY", "") in ("1", "true", "yes"),
        log_level="info",
    )


if __name__ == "__main__":
    main()
