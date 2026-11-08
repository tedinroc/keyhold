"""Session cookie, CSRF, passcode, per-IP rate limits and response headers.

The session is a small JSON payload in an HMAC-signed cookie (stdlib only): a random session id,
a CSRF token and whether the passcode was entered. Unlocking stores a short HMAC of the passcode
in force, so changing KEYHOLD_TRY_PASSCODE locks every browser that unlocked with the old one.
Runs remember the session id that started them, so only that browser can pay for, or stop, its
own run.

Every state-changing request must carry the CSRF token: a hidden ``csrf`` form field, or the
``X-Keyhold-CSRF`` header for JSON calls. ``/webhooks/paypal`` is the exception: PayPal calls it
and its body is verified with PayPal instead.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
from collections import OrderedDict, deque
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass, field
from typing import Any

from starlette.requests import Request
from starlette.responses import Response

__all__ = [
    "CSRF_HEADER",
    "SESSION_COOKIE",
    "RateLimiter",
    "Session",
    "SessionCodec",
    "check_passcode",
    "judge_token",
    "client_ip",
    "security_headers",
]

SESSION_COOKIE = "keyhold_session"
CSRF_HEADER = "X-Keyhold-CSRF"
SESSION_MAX_AGE_S = 12 * 3600

# The JS SDK v6 loads from paypal.com and paypalobjects.com and opens its checkout in a popup or
# an iframe from paypal.com. Nothing else is loaded from outside this app; there is no inline
# script, so script-src needs no 'unsafe-inline'.
_PAYPAL_HOSTS = "https://*.paypal.com https://*.paypalobjects.com"
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        f"script-src 'self' {_PAYPAL_HOSTS}",
        f"style-src 'self' 'unsafe-inline' {_PAYPAL_HOSTS}",
        f"img-src 'self' data: {_PAYPAL_HOSTS}",
        f"connect-src 'self' {_PAYPAL_HOSTS}",
        "frame-src https://*.paypal.com",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "object-src 'none'",
    ]
)


def security_headers(response: Response) -> None:
    headers = response.headers
    headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")


# --------------------------------------------------------------------------------------------
# Session cookie
# --------------------------------------------------------------------------------------------


@dataclass
class Session:
    sid: str
    csrf: str
    unlocked: bool = False
    issued_at: float = 0.0
    changed: bool = field(default=False, compare=False)

    @classmethod
    def new(cls, now: float) -> Session:
        return cls(sid=secrets.token_urlsafe(18), csrf=secrets.token_urlsafe(24), issued_at=now, changed=True)

    def unlock(self) -> None:
        if not self.unlocked:
            self.unlocked = True
            self.changed = True

    def csrf_ok(self, token: str | None) -> bool:
        return bool(token) and hmac.compare_digest(str(token), self.csrf)


class SessionCodec:
    """Encode and verify the session cookie: ``base64url(json).base64url(hmac_sha256)``."""

    def __init__(self, secret_key: str, *, max_age_s: float = SESSION_MAX_AGE_S, passcode: str = "") -> None:
        if not secret_key:
            raise ValueError("a secret key is required to sign sessions")
        self._key = hashlib.sha256(b"keyhold-session:" + secret_key.encode("utf-8")).digest()
        self.max_age_s = max_age_s
        # Ties an unlocked session to the passcode it was unlocked with (never the passcode itself).
        self._passcode_tag = hmac.new(self._key, b"passcode:" + passcode.encode("utf-8"), hashlib.sha256).hexdigest()[:16]

    def _sign(self, body: bytes) -> str:
        mac = hmac.new(self._key, body, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")

    def encode(self, session: Session) -> str:
        payload = {"v": 1, "sid": session.sid, "csrf": session.csrf, "ok": session.unlocked, "iat": session.issued_at}
        if session.unlocked:
            payload["pv"] = self._passcode_tag
        body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).rstrip(b"=")
        return f"{body.decode('ascii')}.{self._sign(body)}"

    def decode(self, value: str | None, now: float) -> Session | None:
        if not value or value.count(".") != 1 or len(value) > 2048:
            return None
        body_text, mac = value.split(".", 1)
        body = body_text.encode("ascii", "ignore")
        if not hmac.compare_digest(self._sign(body), mac):
            return None
        try:
            padded = body + b"=" * (-len(body) % 4)
            data = json.loads(base64.urlsafe_b64decode(padded))
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict) or data.get("v") != 1:
            return None
        issued = float(data.get("iat") or 0.0)
        if now - issued > self.max_age_s or issued - now > 300:
            return None
        sid, csrf = data.get("sid"), data.get("csrf")
        if not isinstance(sid, str) or not isinstance(csrf, str) or not sid or not csrf:
            return None
        unlocked = bool(data.get("ok")) and hmac.compare_digest(str(data.get("pv") or ""), self._passcode_tag)
        return Session(sid=sid, csrf=csrf, unlocked=unlocked, issued_at=issued)


def judge_token(secret_key: str, passcode: str) -> str:
    """The token in the one-click judge link /j/<token>. It is derived from the passcode, so the
    passcode itself never appears in a URL or an access log, and changing either value retires
    every link handed out before."""
    if not passcode:
        return ""
    digest = hmac.new(secret_key.encode("utf-8"), b"judge-link:" + passcode.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:32]


def check_passcode(given: str | None, expected: str) -> bool:
    """Constant-time comparison. An unset expected passcode never matches."""
    if not expected or given is None:
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


# --------------------------------------------------------------------------------------------
# Client address and rate limits
# --------------------------------------------------------------------------------------------


def client_ip(request: Request, *, trust_proxy: bool = False) -> str:
    """The caller's address. With ``trust_proxy`` (Keyhold behind one HTTPS proxy) it is the last
    X-Forwarded-For entry, the one the proxy appended for the peer it saw. The entries before it
    come from the client and can say anything, so they are never used. Otherwise the socket peer."""
    if trust_proxy:
        forwarded = [e.strip() for e in request.headers.get("x-forwarded-for", "").split(",") if e.strip()]
        if forwarded:
            return forwarded[-1][:64]
    client = request.client
    return (client.host if client and client.host else "unknown")[:64]


class RateLimiter:
    """Sliding-window counters per (bucket, key), in memory. ``hit`` records one attempt and
    returns None when allowed, or the seconds until the next attempt would be allowed."""

    MAX_KEYS = 20_000

    def __init__(self, clock: Callable[[], float]) -> None:
        self._now = clock
        self._lock = threading.Lock()
        self._hits: MutableMapping[tuple[str, str], deque[float]] = OrderedDict()

    def hit(self, bucket: str, key: str, *, limit: int, window_s: float) -> float | None:
        now = self._now()
        with self._lock:
            slot = (bucket, key)
            hits = self._hits.get(slot)
            if hits is None:
                hits = deque()
                self._hits[slot] = hits
                while len(self._hits) > self.MAX_KEYS:
                    self._hits.popitem(last=False)  # type: ignore[call-arg]
            while hits and hits[0] <= now - window_s:
                hits.popleft()
            if len(hits) >= limit:
                return max(1.0, hits[0] + window_s - now)
            hits.append(now)
            return None

    def remaining(self, bucket: str, key: str, *, limit: int, window_s: float) -> int:
        now = self._now()
        with self._lock:
            hits = self._hits.get((bucket, key))
            if not hits:
                return limit
            return max(0, limit - sum(1 for t in hits if t > now - window_s))


def session_cookie_kwargs(request: Request) -> dict[str, Any]:
    return {
        "max_age": SESSION_MAX_AGE_S,
        "httponly": True,
        "samesite": "lax",
        "secure": request.url.scheme == "https",
        "path": "/",
    }
