"""Settings for the web app, read from the environment.

Two independent choices:

- ``mode``: ``live`` uses the PayPal sandbox through ``PayPalClient`` (needs PAYPAL_CLIENT_ID and
  PAYPAL_CLIENT_SECRET); ``offline`` uses ``FakePayPal`` in memory and says so on every page.
  Default: live when both PayPal variables are set, offline otherwise.
- ``world``: what the agent is lent a keyboard to. ``vm`` boots a fresh copy-on-write overlay of
  a local test VM (see tools/vm/README.md) and uses Claude; ``simulated`` uses a scripted console
  and a recorded agent script, with no model call. Default: vm in live mode, simulated offline.

Environment variables (all optional unless noted):

=================================  ==========================================================
KEYHOLD_MODE                       ``live`` or ``offline``
KEYHOLD_WORLD                      ``vm`` or ``simulated``
PAYPAL_CLIENT_ID, _CLIENT_SECRET   sandbox REST app credentials (live mode, required there)
PAYPAL_WEBHOOK_ID                  webhook id used by verify-webhook-signature
KEYHOLD_TRY_PASSCODE               passcode for /try (live mode refuses to start runs without it); the judge
                                   link /j/<token> unlocks with one click (tools/judge_link.py prints it)
KEYHOLD_TRY_SLOTS                  test servers that may run at once (default 1, at most 8)
KEYHOLD_JUDGE_BUYER_EMAIL          sandbox buyer login shown next to the PayPal button after unlocking
KEYHOLD_JUDGE_BUYER_PASSWORD       its password (a sandbox test account; kept out of logs)
KEYHOLD_SECRET_KEY                 signs the session cookie (random per process when unset)
KEYHOLD_DATA_DIR                   default ``var``
KEYHOLD_DB                         default ``var/keyhold.db``
KEYHOLD_JUDGE_MODE                 ``1`` (default): compressed, labelled probe and warranty timing
KEYHOLD_WARRANTY_CHECKS            override the number of warranty checks
KEYHOLD_WARRANTY_INTERVAL_S        override the seconds between warranty checks
KEYHOLD_MAX_TURNS                  agent turn limit (default 30)
KEYHOLD_RESCUE_TIMEOUT_S           agent wall-clock limit (default 1200)
KEYHOLD_QUOTE_TTL_S                time to check out before the VM is released (default 900)
KEYHOLD_MODEL_SPEND_CAP_USD        model spend allowed per rolling 24 hours (default 10)
KEYHOLD_RUN_BUDGET_USD             model spend allowed per run (default 3)
KEYHOLD_TRY_RUNS_PER_HOUR          runs one IP address may start per hour (default 4)
KEYHOLD_TRUST_PROXY                ``1``: the client address is the last X-Forwarded-For entry
KEYHOLD_VM_HOME                    VM images and runs (default ``var/vm``)
KEYHOLD_PROBE_STATE_DIR            known_hosts files of the owner probe (default ``var/probe``)
KEYHOLD_MODEL                      claude-opus-5-5 (default) or claude-haiku-5-5
KEYHOLD_MODEL_SESSION_HEADER       header that carries one id per rescue, for gateways that ask for it
KEYHOLD_MODEL_EFFORT               Claude effort for the free look and agent (default medium)
=================================  ==========================================================

Secrets are kept out of ``repr`` and are never logged or rendered.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

__all__ = ["MODES", "WORLDS", "Settings", "SettingsError"]

MODES = ("live", "offline")
WORLDS = ("vm", "simulated")
# Webhook id FakePayPal signs its own deliveries for, when no real one is configured.
OFFLINE_WEBHOOK_ID = "offline-webhook"


class SettingsError(ValueError):
    """The environment asks for something the app cannot do. The message names the variable."""


def _flag(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(env: Mapping[str, str], name: str, default: int, minimum: int = 1) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{name} must be a whole number") from None
    if value < minimum:
        raise SettingsError(f"{name} must be at least {minimum}")
    return value


def _float(env: Mapping[str, str], name: str, default: float | None) -> float | None:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SettingsError(f"{name} must be a number") from None
    if value <= 0:
        raise SettingsError(f"{name} must be positive")
    return value


def _money(env: Mapping[str, str], name: str, default: str) -> Decimal:
    raw = env.get(name, "").strip() or default
    try:
        value = Decimal(raw).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise SettingsError(f"{name} must be an amount in US dollars, for example 10.00") from None
    if value < 0:
        raise SettingsError(f"{name} must not be negative")
    return value


@dataclass(frozen=True)
class Settings:
    mode: str = "offline"
    world: str = "simulated"
    data_dir: Path = Path("var")
    db_path: Path = Path("var/keyhold.db")
    passcode: str = field(default="", repr=False)
    secret_key: str = field(default_factory=lambda: secrets.token_hex(32), repr=False)
    paypal_client_id: str = field(default="", repr=False)
    paypal_client_secret: str = field(default="", repr=False)
    webhook_id: str = ""
    judge_mode: bool = True
    warranty_checks: int | None = None
    warranty_interval_s: float | None = None
    max_turns: int = 30
    rescue_timeout_s: float = 20 * 60.0
    quote_ttl_s: float = 15 * 60.0
    boot_timeout_s: float = 6 * 60.0
    spend_cap_usd: Decimal = Decimal("10.00")
    run_budget_usd: Decimal = Decimal("3.00")
    runs_per_hour: int = 4
    try_slots: int = 1
    judge_buyer_email: str = ""
    judge_buyer_password: str = field(default="", repr=False)
    unlock_attempts: int = 8  # per unlock window
    unlock_window_s: float = 600.0
    api_calls_per_minute: int = 120
    trust_proxy: bool = False
    background: bool = True  # reconciler, webhook pump and sweeper threads
    sse_poll_s: float = 1.0
    sse_max_s: float = 900.0  # a stream ends after this; EventSource reconnects
    sse_tail_s: float = 180.0  # keep streaming after the end, waiting for webhook second ticks
    paypal_poll_s: float = 3.0
    offline_webhook_delay_s: float = 4.0
    reconcile_interval_s: float = 300.0
    sweep_interval_s: float = 10.0
    model: str = "claude-opus-5-5"
    model_session_header: str = ""
    model_effort: str = "medium"
    vm_home: Path | None = None
    probe_state_dir: Path = Path("var/probe")
    max_webhook_bytes: int = 256 * 1024
    webhooks_per_minute: int = 60  # deliveries one address may post per minute
    max_unverified_deliveries: int = 500  # stored but unverified bodies before intake answers 503
    sse_streams_per_minute: int = 30  # status streams one address may open per minute
    capture_wait_s: float = 120.0  # how long a run keeps replaying a capture PayPal has not answered

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise SettingsError(f"KEYHOLD_MODE must be one of {', '.join(MODES)}")
        if self.world not in WORLDS:
            raise SettingsError(f"KEYHOLD_WORLD must be one of {', '.join(WORLDS)}")
        if self.mode == "live" and not (self.paypal_client_id and self.paypal_client_secret):
            raise SettingsError("live mode needs PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET (a sandbox app)")

    # -- derived -------------------------------------------------------------------------------

    @property
    def offline(self) -> bool:
        return self.mode == "offline"

    @property
    def simulated(self) -> bool:
        return self.world == "simulated"

    @property
    def effective_webhook_id(self) -> str:
        """The webhook id signatures are checked against. Offline, FakePayPal signs for a fixed
        id of its own; live, it is PAYPAL_WEBHOOK_ID (empty means no delivery can verify)."""
        if self.webhook_id:
            return self.webhook_id
        return OFFLINE_WEBHOOK_ID if self.offline else ""

    @property
    def webhooks_verifiable(self) -> bool:
        """False in live mode without PAYPAL_WEBHOOK_ID: no delivery can pass verification."""
        return bool(self.effective_webhook_id)

    @property
    def passcode_required(self) -> bool:
        """Offline runs cost nothing, so an unset passcode leaves /try open there. Live runs
        refuse to start until a passcode is set."""
        return bool(self.passcode) or not self.offline

    @property
    def fake_paypal_path(self) -> Path:
        return self.data_dir / "fake-paypal.json"

    def banners(self) -> list[dict[str, str]]:
        """Plain notices shown on every page. Offline mode is always labelled."""
        notes: list[dict[str, str]] = []
        if self.offline:
            notes.append(
                {
                    "tone": "offline",
                    "title": "Offline mode",
                    "text": "PayPal is simulated in memory (FakePayPal). No PayPal account is used and no "
                    "money moves, test or real.",
                }
            )
        else:
            notes.append(
                {
                    "tone": "sandbox",
                    "title": "PayPal sandbox",
                    "text": "Every order, hold, capture and refund here uses PayPal sandbox test money.",
                }
            )
        if not self.webhooks_verifiable:
            notes.append(
                {
                    "tone": "warn",
                    "title": "Webhooks not verifiable",
                    "text": "PAYPAL_WEBHOOK_ID is not set, so PayPal's webhooks cannot pass signature "
                    "verification and the second tick never shows. Set it to the sandbox app's webhook id.",
                }
            )
        if self.simulated:
            notes.append(
                {
                    "tone": "simulated",
                    "title": "Simulated server",
                    "text": "The broken server is a scripted console and the agent replays a recorded "
                    "script. No model is called.",
                }
            )
        return notes

    # -- environment ---------------------------------------------------------------------------

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, **overrides: object) -> Settings:
        env = os.environ if environ is None else environ
        client_id = env.get("PAYPAL_CLIENT_ID", "").strip()
        client_secret = env.get("PAYPAL_CLIENT_SECRET", "").strip()
        mode = env.get("KEYHOLD_MODE", "").strip().lower() or ("live" if client_id and client_secret else "offline")
        world = env.get("KEYHOLD_WORLD", "").strip().lower() or ("vm" if mode == "live" else "simulated")
        data_dir = Path(env.get("KEYHOLD_DATA_DIR", "").strip() or "var")
        db_path = Path(env.get("KEYHOLD_DB", "").strip() or str(data_dir / "keyhold.db"))
        vm_home = env.get("KEYHOLD_VM_HOME", "").strip()
        effort = env.get("KEYHOLD_MODEL_EFFORT", "").strip() or "medium"
        model = env.get("KEYHOLD_MODEL", "").strip() or "claude-opus-5-5"
        from keyhold.agent.model import SUPPORTED_MODELS

        if model not in SUPPORTED_MODELS:
            raise SettingsError(f"KEYHOLD_MODEL must be one of {', '.join(SUPPORTED_MODELS)}")
        session_header = env.get("KEYHOLD_MODEL_SESSION_HEADER", "").strip()
        from keyhold.agent.model import valid_session_header

        if session_header and not valid_session_header(session_header):
            raise SettingsError(
                "KEYHOLD_MODEL_SESSION_HEADER must be an HTTP header name that the SDK does not set itself"
            )
        checks = env.get("KEYHOLD_WARRANTY_CHECKS", "").strip()
        values: dict[str, object] = {
            "mode": mode,
            "world": world,
            "data_dir": data_dir,
            "db_path": db_path,
            "passcode": env.get("KEYHOLD_TRY_PASSCODE", ""),
            "paypal_client_id": client_id,
            "paypal_client_secret": client_secret,
            "webhook_id": env.get("PAYPAL_WEBHOOK_ID", "").strip(),
            "judge_mode": _flag(env.get("KEYHOLD_JUDGE_MODE"), True),
            "warranty_checks": _int(env, "KEYHOLD_WARRANTY_CHECKS", 0) if checks else None,
            "warranty_interval_s": _float(env, "KEYHOLD_WARRANTY_INTERVAL_S", None),
            "max_turns": _int(env, "KEYHOLD_MAX_TURNS", 30),
            "rescue_timeout_s": _float(env, "KEYHOLD_RESCUE_TIMEOUT_S", 20 * 60.0),
            "quote_ttl_s": _float(env, "KEYHOLD_QUOTE_TTL_S", 15 * 60.0),
            "spend_cap_usd": _money(env, "KEYHOLD_MODEL_SPEND_CAP_USD", "10.00"),
            "run_budget_usd": _money(env, "KEYHOLD_RUN_BUDGET_USD", "3.00"),
            "runs_per_hour": _int(env, "KEYHOLD_TRY_RUNS_PER_HOUR", 4),
            "try_slots": _int(env, "KEYHOLD_TRY_SLOTS", 1),
            "judge_buyer_email": env.get("KEYHOLD_JUDGE_BUYER_EMAIL", "").strip(),
            "judge_buyer_password": env.get("KEYHOLD_JUDGE_BUYER_PASSWORD", "").strip(),
            "trust_proxy": _flag(env.get("KEYHOLD_TRUST_PROXY"), False),
            "vm_home": Path(vm_home) if vm_home else None,
            "probe_state_dir": Path(env.get("KEYHOLD_PROBE_STATE_DIR", "").strip() or str(data_dir / "probe")),
            "model": model,
            "model_session_header": session_header,
            "model_effort": effort,
        }
        if int(values["try_slots"]) > 8:  # type: ignore[call-overload]
            raise SettingsError("KEYHOLD_TRY_SLOTS must be at most 8")
        secret = env.get("KEYHOLD_SECRET_KEY", "")
        if secret:
            if len(secret) < 16:
                raise SettingsError("KEYHOLD_SECRET_KEY must be at least 16 characters")
            values["secret_key"] = secret
        values.update(overrides)
        return cls(**values)  # type: ignore[arg-type]
