"""What the pages show, as plain dicts (rendered by Jinja and sent as JSON over SSE).

- ``PayPalView`` reads PayPal's state live (GET order, authorization, capture, refund) with a
  short per-rescue cache, so several open status pages do not multiply the calls.
- ``rescue_snapshot`` joins the ledger row, the live PayPal read, the outbox (request ids and the
  time of each synchronous answer, the first tick), the verified webhooks (the second tick), the
  event timeline and console thumbnails.
- ``judge_view`` is the ledger of all runs with money totals and the invariant check per rescue.

Times are UTC. Nothing here moves money.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from keyhold.faults import FAULT_CLASSES
from keyhold.pay.ledger import (
    EventRecord,
    Ledger,
    OutboxEntry,
    RescueRecord,
    WebhookDelivery,
)
from keyhold.probe.spec import ProbeSpecError, parse_public_key
from keyhold.types import NO_MONEY_CAPTURE, MoneyOp, OutboxStatus, PayPalError, RescueState

__all__ = [
    "PayPalView",
    "fmt_time",
    "judge_view",
    "money_view",
    "probe_view",
    "quote_view",
    "rescue_snapshot",
    "run_view",
    "timeline_entry",
    "webhook_entry",
]

LIVE_STATES = frozenset({RescueState.HELD, RescueState.ACTIVE, RescueState.PROBING})


def fmt_time(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _money(value: Decimal | str | None) -> str | None:
    return None if value is None else f"{Decimal(str(value)):.2f}"


# --------------------------------------------------------------------------------------------
# Live PayPal reads
# --------------------------------------------------------------------------------------------


class PayPalView:
    """GETs against PayPal (or FakePayPal) for one rescue's ids. Never raises: a failed read is
    reported in ``error`` with PayPal's issue code."""

    def __init__(self, gateway: Any, clock: Any, *, ttl_s: float = 3.0, min_fresh_s: float = 1.0) -> None:
        self.gateway = gateway
        self.clock = clock
        self.ttl_s = ttl_s
        self.min_fresh_s = min_fresh_s  # even a fresh read reuses an answer this young
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, tuple[Any, ...], dict[str, Any]]] = {}

    def read(self, rec: RescueRecord, *, fresh: bool = False) -> dict[str, Any]:
        """PayPal's view of the rescue. At most one live read per rescue per ``ttl_s`` (or
        ``min_fresh_s`` with ``fresh``), however many pages ask."""
        key = (rec.order_id, rec.authorization_id, rec.capture_id, rec.refund_id)
        now = self.clock.now()
        max_age = self.min_fresh_s if fresh else self.ttl_s
        with self._lock:
            cached = self._cache.get(rec.id)
            if cached is not None and cached[1] == key and 0 <= now - cached[0] < max_age:
                return cached[2]
        data = self._fetch(rec)
        data["checked_at"] = fmt_time(now)
        with self._lock:
            self._cache[rec.id] = (now, key, data)
            if len(self._cache) > 500:
                self._cache.pop(next(iter(self._cache)))
        return data

    def _fetch(self, rec: RescueRecord) -> dict[str, Any]:
        out: dict[str, Any] = {"order": None, "authorization": None, "capture": None, "refund": None, "errors": []}

        def attempt(name: str, fn: Any, ident: str | None) -> Any:
            if not ident or fn is None:
                return None
            try:
                return fn(ident)
            except PayPalError as err:
                out["errors"].append(f"{name}: PayPal {err.issue}" + (f" (debug id {err.debug_id})" if err.debug_id else ""))
            except Exception as exc:  # noqa: BLE001 - shown, never raised
                out["errors"].append(f"{name}: {type(exc).__name__}")
            return None

        order = attempt("order", self.gateway.get_order, rec.order_id)
        if order is not None:
            out["order"] = {
                "id": order.id,
                "status": order.status,
                "intent": order.intent,
                "amount": _money(order.amount),
                "create_time": order.create_time,
            }
        auth = attempt("authorization", self.gateway.get_authorization, rec.authorization_id)
        if auth is not None:
            out["authorization"] = {
                "id": auth.id,
                "status": auth.status,
                "amount": _money(auth.amount),
                "expiration_time": auth.expiration_time,
            }
        capture = attempt("capture", self.gateway.get_capture, rec.capture_id)
        if capture is not None:
            out["capture"] = {
                "id": capture.id,
                "status": capture.status,
                "amount": _money(capture.amount),
                "final_capture": capture.final_capture,
                "create_time": capture.create_time,
            }
        refund = attempt("refund", getattr(self.gateway, "get_refund", None), rec.refund_id)
        if refund is not None:
            out["refund"] = {"id": refund.id, "status": refund.status, "amount": _money(refund.amount)}
        return out


# --------------------------------------------------------------------------------------------
# Pieces
# --------------------------------------------------------------------------------------------


def quote_view(quote: dict[str, Any], title: str | None = None) -> dict[str, Any]:
    fault_class = str(quote.get("fault_class", ""))
    klass = FAULT_CLASSES.get(fault_class)
    return {
        "fault_class": fault_class,
        "title": title or (klass.description if klass else fault_class),
        "evidence": list(quote.get("evidence", [])),
        "tier": quote.get("tier"),
        "amount": _money(quote.get("amount")),
        "plan": quote.get("plan", ""),
        "quote_hash": quote.get("quote_hash", ""),
    }


def probe_view(spec: dict[str, Any] | None, spec_hash: str | None) -> dict[str, Any] | None:
    """The owner probe as the page shows it. The host is left out of the page; the probe uses it."""
    if not spec:
        return None
    try:
        fingerprint = parse_public_key(str(spec.get("probe_public_key", ""))).fingerprint
    except ProbeSpecError:
        fingerprint = "unreadable key"
    url = urlsplit(str(spec.get("url", "")))
    port = url.port or (443 if url.scheme == "https" else 80)
    if spec.get("expect_sha256"):
        expect = f"the page body has sha256 {spec['expect_sha256']}"
    else:
        expect = f"the page contains “{spec.get('expect_substring', '')}”"
    return {
        "ssh_user": spec.get("ssh_user"),
        "ssh_port": spec.get("ssh_port"),
        "key_fingerprint": fingerprint,
        "page": f"{url.scheme.upper()} GET {url.path or '/'} on port {port}",
        "expect": expect,
        "spec_hash": spec_hash,
    }


def money_view(rec: RescueRecord, live: dict[str, Any] | None = None) -> dict[str, Any]:
    """The headline: what PayPal holds or moved for this rescue, in one sentence."""
    state = rec.state
    quote = _money(rec.quote_amount)
    held = _money(rec.authorized_amount) or quote
    auth_live = (live or {}).get("authorization") or {}
    warning = None
    if auth_live and state in LIVE_STATES and auth_live.get("status") not in (None, "CREATED"):
        warning = (
            f"PayPal now shows the authorization as {auth_live['status']}. "
            "The keyboard is locked at the next action and the hold is voided."
        )
    if state is RescueState.QUOTED:
        return {"tone": "neutral", "label": "Quoted", "amount": quote, "sentence": "Nothing is held yet.", "warning": warning}
    if state is RescueState.HOLD_PENDING:
        return {
            "tone": "neutral",
            "label": "Awaiting approval",
            "amount": quote,
            "sentence": f"PayPal order {rec.order_id} exists. Nothing is held until the owner approves it.",
            "warning": warning,
        }
    if state in LIVE_STATES:
        sentence = {
            RescueState.HELD: "PayPal holds this amount. The keyboard is lent only while PayPal shows the "
            "authorization as CREATED.",
            RescueState.ACTIVE: "The agent is at the keyboard under the hold. Stop voids the hold and takes the "
            "keyboard back.",
            RescueState.PROBING: "The agent finished. The owner probe decides: a capture only if the server "
            "answers twice, far enough apart.",
        }[state]
        return {"tone": "held", "label": "Held", "amount": held, "sentence": sentence, "warning": warning}
    captured = _money(rec.captured_amount)
    if rec.capture_status in NO_MONEY_CAPTURE:
        return {
            "tone": "voided",
            "label": "Capture declined",
            "amount": "0.00",
            "sentence": f"PayPal answered the capture with {rec.capture_status}. Nothing was charged.",
            "warning": warning,
        }
    if state is RescueState.CAPTURED and rec.capture_status == "PENDING":
        return {
            "tone": "held",
            "label": "Capture pending",
            "amount": captured,
            "sentence": "The owner probe passed and PayPal accepted the capture, but shows it as PENDING: "
            "the money has not moved yet. The warranty keeps re-probing the server.",
            "warning": warning,
        }
    if state is RescueState.CAPTURED:
        partial = (
            f" PayPal lists ${_money(rec.refunded_amount)} of it refunded so far."
            if rec.refunded_amount
            else ""
        )
        return {
            "tone": "captured",
            "label": "Captured",
            "amount": captured,
            "sentence": "The owner probe passed. PayPal captured this amount with final_capture=true; the "
            "warranty keeps re-probing the server." + partial,
            "warning": warning,
        }
    if state is RescueState.WARRANTY_OK:
        return {
            "tone": "captured",
            "label": "Captured",
            "amount": captured,
            "sentence": "The warranty ended with the server still answering.",
            "warning": warning,
        }
    if state is RescueState.REFUNDED:
        return {
            "tone": "refunded",
            "label": "Refunded",
            "amount": _money(rec.refunded_amount),
            "sentence": f"Captured ${captured}, then refunded in full because the server stopped answering "
            "during the warranty.",
            "warning": warning,
        }
    return {
        "tone": "voided",
        "label": "Voided",
        "amount": "0.00",
        "sentence": "Nothing was charged." + (f" {rec.end_reason[0].upper()}{rec.end_reason[1:]}." if rec.end_reason else ""),
        "warning": warning,
    }


_OP_NAMES = {
    MoneyOp.CREATE_ORDER: "Order",
    MoneyOp.AUTHORIZE: "Authorization",
    MoneyOp.CAPTURE: "Capture",
    MoneyOp.VOID: "Void",
    MoneyOp.REFUND: "Refund",
}
_TICK_FIELD = {
    MoneyOp.AUTHORIZE: "auth_webhook_at",
    MoneyOp.CAPTURE: "capture_webhook_at",
    MoneyOp.VOID: "void_webhook_at",
    MoneyOp.REFUND: "refund_webhook_at",
}


def ticks_view(rec: RescueRecord, ops: Iterable[OutboxEntry]) -> list[dict[str, Any]]:
    """One row per money operation: the PayPal-Request-Id, the synchronous answer (first tick)
    and the verified webhook (second tick)."""
    ids = {
        MoneyOp.CREATE_ORDER: rec.order_id,
        MoneyOp.AUTHORIZE: rec.authorization_id,
        MoneyOp.CAPTURE: rec.capture_id,
        MoneyOp.VOID: rec.authorization_id,
        MoneyOp.REFUND: rec.refund_id,
    }
    rows = []
    for entry in ops:
        field_name = _TICK_FIELD.get(entry.op)
        rows.append(
            {
                "op": _OP_NAMES[entry.op],
                "paypal_id": ids.get(entry.op),
                "request_id": entry.request_id,
                "status": entry.status.value,
                "api_at": fmt_time(entry.updated_at) if entry.status is OutboxStatus.DONE else None,
                "error": entry.error,
                "attempts": entry.attempts,
                "webhook": field_name is not None,
                "webhook_at": fmt_time(getattr(rec, field_name)) if field_name else None,
            }
        )
    return rows


def _record_view(rec: RescueRecord) -> dict[str, Any]:
    return {
        "id": rec.id,
        "state": rec.state.value,
        "terminal": rec.state.terminal,
        "invoice_id": rec.invoice_id,
        "quote_amount": _money(rec.quote_amount),
        "order_id": rec.order_id,
        "order_status": rec.order_status,
        "authorization_id": rec.authorization_id,
        "authorized_amount": _money(rec.authorized_amount),
        "auth_status": rec.auth_status,
        "auth_expiration_time": rec.auth_expiration_time,
        "capture_id": rec.capture_id,
        "captured_amount": _money(rec.captured_amount),
        "capture_status": rec.capture_status,
        "refund_id": rec.refund_id,
        "refunded_amount": _money(rec.refunded_amount),
        "refund_status": rec.refund_status,
        "end_reason": rec.end_reason,
        "created_at": fmt_time(rec.created_at),
        "updated_at": fmt_time(rec.updated_at),
    }


# --------------------------------------------------------------------------------------------
# Timeline
# --------------------------------------------------------------------------------------------

_HIDDEN = frozenset({"agent_screenshot", "model_spend", "outbox"})
_GROUP = {
    "quoted": "quote",
    "probe_fixed": "quote",
    "free_look": "quote",
    "paypal_call": "money",
    "paypal_ok": "money",
    "paypal_error": "money",
    "state": "state",
    "gate_check": "lock",
    "gate_revoked": "lock",
    "agent_firewall_refusal": "firewall",
    "agent_result": "agent",
    "probe": "probe",
    "probe_stable": "probe",
    "warranty_started": "warranty",
    "warranty_check": "warranty",
    "warranty_error": "warranty",
    "refund_due": "warranty",
    "refund_deferred": "money",
    "refund_pending": "money",
    "refund_failed": "money",
    "void_refused": "money",
    "attention": "money",
    "stop": "owner",
}
_WARN = frozenset(
    {
        "paypal_error",
        "gate_revoked",
        "agent_firewall_refusal",
        "agent_error",
        "agent_model_refusal",
        "agent_revoked",
        "capture_pending",
        "capture_refused",
        "capture_not_settled",
        "void_failed",
        "refund_failed",
        "conflict",
        "warranty_error",
        "refund_deferred",
        "void_refused",
        "attention",
    }
)
# A lease revoked because the agent's session ended normally is not a warning.
_NORMAL_LOCK = ("the agent session ended", "the rescue session ended")
_GROUP_LABEL = {
    "quote": "Quote",
    "money": "PayPal",
    "state": "State",
    "lock": "Lock",
    "firewall": "Firewall",
    "agent": "Agent",
    "probe": "Probe",
    "warranty": "Warranty",
    "owner": "Owner",
    "webhook": "Webhook",
    "system": "Keyhold",
}


def _keys_text(value: Any) -> str:
    """Typed text on one line, with Enter shown as a return sign."""
    return str(value).replace("\r", "").replace("\n", " \u21b5 ").strip()


def timeline_entry(event: EventRecord) -> dict[str, Any] | None:
    """One ledger event as a timeline row, or None for events shown elsewhere (screenshots as
    thumbnails, model spend in its own line, outbox rows next to each PayPal call)."""
    kind = event.kind
    if kind in _HIDDEN:
        return None
    group = _GROUP.get(kind) or ("agent" if kind.startswith("agent_") else "system")
    data = event.data or {}
    text = event.detail
    if kind == "agent_type":
        text = "typed: " + _keys_text(data.get("text", event.detail))
    elif kind == "agent_key":
        text = f"pressed {event.detail}"
    elif kind == "agent_firewall_refusal":
        text = f"refused ({data.get('rule')}): {event.detail} Not sent: " + _keys_text(data.get("input", ""))
    elif kind == "agent_note":
        text = event.detail
    elif kind in ("probe", "warranty_check"):
        verdict = "pass" if data.get("ok") else "fail"
        text = f"{verdict}: SSH {'ok' if data.get('ssh_ok') else 'failed'}, page {'ok' if data.get('http_ok') else 'failed'}. {event.detail}"
    elif kind == "gate_check":
        granted = data.get("granted", True)
        text = f"{event.detail}: keyboard {'allowed' if granted else 'refused'}"
    elif kind == "free_look":
        text = f"diagnosis: {event.detail}"
    elif kind == "gate_revoked":
        text = f"keyboard locked: {event.detail}"
    tone = "warn" if kind in _WARN or (kind in ("probe", "warranty_check") and not data.get("ok")) else ""
    if kind == "gate_revoked" and event.detail.startswith(_NORMAL_LOCK):
        tone = ""
    if kind == "gate_check" and not data.get("granted", True):
        tone = "warn"
    if kind in ("probe", "warranty_check", "probe_stable") and data.get("ok", kind == "probe_stable"):
        tone = "ok"
    if kind == "paypal_ok":
        tone = "ok"
    return {
        "id": event.id,
        "at": fmt_time(event.at),
        "kind": kind,
        "group": group,
        "group_label": _GROUP_LABEL.get(group, group),
        "text": text[:1500],
        "tone": tone,
        "muted": kind == "agent_note",
    }


def webhook_entry(delivery: WebhookDelivery) -> dict[str, Any]:
    verified = "verified" if delivery.verified else ("not verified" if delivery.verified is False else "not checked")
    return {
        "id": f"w{delivery.id}",
        "delivery_id": delivery.id,
        "at": fmt_time(delivery.received_at),
        "kind": "webhook",
        "group": "webhook",
        "group_label": "Webhook",
        "text": f"{delivery.event_type or 'unknown event'} {delivery.event_id or ''}: {verified}, {delivery.outcome or 'stored'}".replace("  ", " "),
        "tone": "ok" if delivery.outcome == "applied" else ("warn" if delivery.outcome in ("unverified", "conflict") else ""),
        "muted": delivery.outcome == "duplicate",
        "event_type": delivery.event_type,
        "outcome": delivery.outcome,
        "verified": delivery.verified,
    }


def rescue_deliveries(ledger: Ledger, rescue_id: str) -> list[WebhookDelivery]:
    """This rescue's deliveries, metadata only (bodies are never shown)."""
    return ledger.deliveries(rescue_id=rescue_id, raw=False)


def spend_for(ledger: Ledger, rescue_id: str) -> str | None:
    total = Decimal(0)
    found = False
    for event in ledger.events(rescue_id, kind="model_spend"):
        found = True
        total += Decimal(str(event.data.get("usd", "0")))
    if not found:
        return None
    # A Haiku 5.5 rescue costs a fraction of a cent; show it rather than "0.00".
    return f"{total:.2f}" if total >= Decimal("0.01") or total == 0 else f"{total:.4f}"


# --------------------------------------------------------------------------------------------
# Snapshots
# --------------------------------------------------------------------------------------------


def run_view(run: Any, thumbs: Iterable[Any]) -> dict[str, Any]:
    look = run.look
    return {
        "id": run.id,
        "fault_id": run.fault_id,
        "fault_title": run.fault_title,
        "stage": run.stage,
        "stage_label": run.stage_label,
        "detail": run.detail,
        "notes": [{"at": fmt_time(at), "text": text} for at, text in run.notes],
        "rescue_id": run.rescue_id,
        "order_id": run.order_id,
        "world_kind": run.world_kind,
        "world_label": run.world_label,
        "owner_note": getattr(run, "note", None),
        "active": run.active,
        "version": run.version,
        "no_quote_reason": look.reason if look is not None and look.quote is None else None,
        "fault_class": look.fault_class if look is not None else None,
        "quote_deadline": fmt_time(run.quote_deadline),
        "thumbs": [thumb_view(run.id, t) for t in thumbs],
    }


def thumb_view(run_id: str, thumb: Any) -> dict[str, Any]:
    return {
        "n": thumb.n,
        "url": f"/runs/{run_id}/thumbs/{thumb.n}.png",
        "at": fmt_time(thumb.at),
        "label": thumb.label,
        "width": thumb.width,
        "height": thumb.height,
    }


def rescue_snapshot(
    ledger: Ledger,
    rec: RescueRecord,
    *,
    live: dict[str, Any] | None,
    run: Any = None,
    thumbs: Iterable[Any] = (),
    thumbs_run_id: str | None = None,
    policy_label: str = "",
) -> dict[str, Any]:
    events = ledger.events(rec.id)
    entries = [e for e in (timeline_entry(ev) for ev in events) if e is not None]
    deliveries = rescue_deliveries(ledger, rec.id)
    timeline = sorted(
        [(e["at"] or "", 0, int(e["id"]), e) for e in entries]
        + [(w["at"] or "", 1, int(w["delivery_id"]), w) for w in (webhook_entry(d) for d in deliveries)],
        key=lambda item: item[:3],
    )
    timeline = [item[3] for item in timeline]
    last_event = events[-1].id if events else 0
    last_delivery = max((d.id for d in deliveries), default=0)
    quote = quote_view(rec.quote, run.look.title if run is not None and run.look is not None else None)
    return {
        "rescue": _record_view(rec),
        "money": money_view(rec, live),
        "paypal": live,
        "quote": quote,
        "probe": probe_view(rec.probe_spec, rec.probe_spec_hash),
        "ticks": ticks_view(rec, ledger.ops(rec.id)),
        "timeline": timeline,
        "cursor": {"event": last_event, "delivery": last_delivery},
        "thumbs": [thumb_view(thumbs_run_id, t) for t in thumbs] if thumbs_run_id else [],
        "run": None
        if run is None
        else {
            "id": run.id,
            "stage": run.stage,
            "stage_label": run.stage_label,
            "detail": run.detail,
            "active": run.active,
            "world_label": run.world_label,
            "fault_title": run.fault_title,
        },
        "invariants": ledger.check_invariants(rec.id),
        "model_spend": spend_for(ledger, rec.id),
        "policy": policy_label,
        "live_state": rec.state in LIVE_STATES or rec.state in (RescueState.HOLD_PENDING, RescueState.CAPTURED),
    }


def judge_view(ledger: Ledger, *, runs_by_rescue: dict[str, Any] | None = None) -> dict[str, Any]:
    rescues = list(reversed(ledger.rescues()))
    runs_by_rescue = runs_by_rescue or {}
    rows = []
    totals = {"held": Decimal(0), "captured": Decimal(0), "refunded": Decimal(0)}
    states: dict[str, int] = {}
    problems_total = 0
    for rec in rescues:
        states[rec.state.value] = states.get(rec.state.value, 0) + 1
        if rec.state in LIVE_STATES and rec.authorized_amount is not None:
            totals["held"] += rec.authorized_amount
        if rec.captured_amount is not None and rec.capture_status not in ("DECLINED", "DENIED", "FAILED"):
            totals["captured"] += rec.captured_amount
        if rec.refunded_amount is not None:
            totals["refunded"] += rec.refunded_amount
        problems = ledger.check_invariants(rec.id)
        problems_total += len(problems)
        run = runs_by_rescue.get(rec.id)
        rows.append(
            {
                **_record_view(rec),
                "fault_class": rec.quote.get("fault_class"),
                "tier": rec.quote.get("tier"),
                "ticks": {
                    "auth": rec.auth_webhook_at is not None,
                    "capture": rec.capture_webhook_at is not None,
                    "void": rec.void_webhook_at is not None,
                    "refund": rec.refund_webhook_at is not None,
                },
                "problems": problems,
                "attention": [e.detail for e in ledger.events(rec.id, kind="attention")],
                "run_stage": run.stage_label if run is not None else None,
                "money": money_view(rec),
            }
        )
    outcomes = ledger.delivery_counts()
    pending = ledger.pending_ops()
    return {
        "rows": rows,
        "count": len(rows),
        "states": states,
        "totals": {k: f"{v:.2f}" for k, v in totals.items()},
        "problems": problems_total,
        "webhooks": {"total": sum(outcomes.values()), "outcomes": outcomes},
        "pending_ops": [
            {"rescue_id": e.rescue_id, "op": e.op.value, "request_id": e.request_id, "attempts": e.attempts}
            for e in pending
        ],
    }


def as_json(obj: Any) -> Any:
    """Make dataclasses and Decimals JSON-friendly (used by /judge.json)."""
    if hasattr(obj, "__dataclass_fields__"):
        return as_json(asdict(obj))
    if isinstance(obj, Decimal):
        return f"{obj:.2f}"
    if isinstance(obj, dict):
        return {k: as_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [as_json(v) for v in obj]
    return obj
