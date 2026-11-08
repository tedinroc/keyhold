"""Everything the routes use, built once per app: ledger, PayPal gateway, orchestrator, webhook
processor, reconciler, /try runs, spend meter and rate limiter, plus the background loops.

Background loops (``start_background``), each on a daemon thread:

- reconciler: ``Reconciler.startup()`` once, then ``run_once()`` every 5 minutes;
- offline webhook pump (offline mode only): FakePayPal queues the webhook events PayPal would
  send; after a short delay they are signed, stored and verified through the same
  ``WebhookProcessor`` path as ``/webhooks/paypal`` uses, so the second tick shows offline too;
- sweeper: expires unpaid quotes and releases their server.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field, replace
from typing import Any

from keyhold.pay.client import FakePayPal, PayPalClient
from keyhold.pay.ledger import Ledger
from keyhold.pay.reconciler import Reconciler, ReconcileReport
from keyhold.pay.webhooks import WebhookOutcome, WebhookProcessor
from keyhold.probe.owner import probe_policy
from keyhold.probe.warranty import WarrantyPlan
from keyhold.rescue import Orchestrator, RescueConfig
from keyhold.types import AgentEvent, Clock, PaymentGateway, SystemClock

from .config import Settings
from .runs import Executor, RunManager, ThumbStore
from .security import RateLimiter, SessionCodec
from .spend import SpendMeter
from .views import PayPalView
from .worlds import SimulatedWorlds, VmWorlds, WorldFactory

__all__ = ["Services", "build_services"]

log = logging.getLogger(__name__)


@dataclass
class Services:
    settings: Settings
    clock: Clock
    ledger: Ledger
    gateway: PaymentGateway
    orchestrator: Orchestrator
    webhooks: WebhookProcessor
    reconciler: Reconciler
    runs: RunManager
    spend: SpendMeter
    limiter: RateLimiter
    sessions: SessionCodec
    paypal_view: PayPalView
    reconcile_status: dict[str, Any] = field(default_factory=dict)
    _stop: threading.Event = field(default_factory=threading.Event)
    _threads: list[threading.Thread] = field(default_factory=list)
    _pump_since: float | None = None

    @property
    def fake(self) -> FakePayPal | None:
        return self.gateway if isinstance(self.gateway, FakePayPal) else None

    # -- reconciler ------------------------------------------------------------------------------

    def reconcile_once(self, *, startup: bool = False) -> ReconcileReport:
        report = self.reconciler.startup() if startup else self.reconciler.run_once()
        try:
            # Captures the reconciler completed (or that a restart left behind) get their warranty.
            self.runs.resume_warranties()
        except Exception:
            log.exception("starting due warranties failed")
        self.reconcile_status = {
            "at": self.clock.now(),
            "startup": startup,
            "clean": report.clean,
            "replayed": len(report.replayed),
            "repaired": len(report.repaired),
            "voided": len(report.voided),
            "conflicts": len(report.conflicts),
            "errors": len(report.errors),
            "webhooks": report.webhooks,
        }
        return report

    # -- offline webhooks ------------------------------------------------------------------------

    def pump_offline_webhooks(self, *, delay_s: float | None = None) -> list[WebhookOutcome]:
        """Deliver FakePayPal's queued webhook events that are older than ``delay_s``.

        Each one goes through ``WebhookProcessor.receive`` (raw body stored first) and
        ``process`` (signature verified with FakePayPal, deduplicated, applied by rank)."""
        fake = self.fake
        if fake is None:
            return []
        delay = self.settings.offline_webhook_delay_s if delay_s is None else delay_s
        now = self.clock.now()
        if not fake.pending_events():
            self._pump_since = None
            return []
        if self._pump_since is None:
            self._pump_since = now
        if now - self._pump_since < delay:
            return []
        self._pump_since = None
        outcomes = []
        for headers, raw in fake.take_deliveries(self.settings.effective_webhook_id):
            delivery_id = self.webhooks.receive(headers, raw)
            outcomes.append(self.webhooks.process(delivery_id))
        return outcomes

    # -- background ------------------------------------------------------------------------------

    def _loop(self, name: str, interval: float, fn: Any, *, first: Any = None) -> None:
        def body() -> None:
            if first is not None:
                self._safe(name, first)
            while not self._stop.wait(interval):
                self._safe(name, fn)

        thread = threading.Thread(target=body, name=f"keyhold-{name}", daemon=True)
        thread.start()
        self._threads.append(thread)

    def _safe(self, name: str, fn: Any) -> None:
        try:
            fn()
        except Exception:
            log.exception("background %s pass failed", name)

    def start_background(self) -> None:
        self._stop.clear()
        self._loop(
            "reconciler",
            self.settings.reconcile_interval_s,
            lambda: self.reconcile_once(),
            first=lambda: self.reconcile_once(startup=True),
        )
        self._loop("sweeper", self.settings.sweep_interval_s, self.runs.sweep)
        if self.fake is not None:
            self._loop("webhook-pump", 1.0, self.pump_offline_webhooks)

    def close(self) -> None:
        self._stop.set()
        try:
            self.runs.shutdown()
        except Exception:
            log.exception("shutting down runs failed")
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads.clear()
        self.ledger.close()


def _orchestrator_config(settings: Settings) -> RescueConfig:
    policy = probe_policy(settings.judge_mode)
    if settings.warranty_checks or settings.warranty_interval_s:
        base = policy.warranty
        policy = replace(
            policy,
            warranty=WarrantyPlan(
                interval_s=float(settings.warranty_interval_s or base.interval_s),
                checks=int(settings.warranty_checks or base.checks),
                failures_to_refund=base.failures_to_refund,
                confirm_last_failure=base.confirm_last_failure,
                judge_mode=base.judge_mode,
            ),
        )
    return RescueConfig(
        max_turns=settings.max_turns,
        judge_mode=settings.judge_mode,
        policy=policy,
        rescue_timeout_s=settings.rescue_timeout_s,
        warranty=False,  # the run manager runs it, so it knows when the money decision is made
    )


def build_services(
    settings: Settings,
    *,
    clock: Clock | None = None,
    gateway: PaymentGateway | None = None,
    worlds: WorldFactory | None = None,
    executor: Executor | None = None,
) -> Services:
    clock = clock or SystemClock()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(settings.db_path, clock=clock)
    if gateway is None:
        if settings.mode == "live":
            gateway = PayPalClient(settings.paypal_client_id, settings.paypal_client_secret, clock=clock)
        else:
            gateway = FakePayPal(clock=clock, state_path=settings.fake_paypal_path)
    if worlds is None:
        worlds = (SimulatedWorlds(stop_path_turn_s=settings.sim_stop_path_turn_s) if settings.simulated
                  else VmWorlds(settings))

    sink_holder: dict[str, RunManager] = {}

    def on_event(rescue_id: str, event: AgentEvent) -> None:
        manager = sink_holder.get("runs")
        if manager is not None:
            manager.sink(rescue_id, event)

    orchestrator = Orchestrator(ledger, gateway, clock=clock, config=_orchestrator_config(settings), on_event=on_event)
    webhooks = WebhookProcessor(ledger, gateway, settings.effective_webhook_id, payments=orchestrator.payments)
    spend = SpendMeter(ledger, settings.spend_cap_usd, clock=clock.now)
    runs = RunManager(
        settings=settings,
        ledger=ledger,
        orchestrator=orchestrator,
        worlds=worlds,
        spend=spend,
        clock=clock,
        executor=executor,
        gateway=gateway,
        thumbs=ThumbStore(directory=settings.data_dir / "thumbs"),
    )
    sink_holder["runs"] = runs
    reconciler = Reconciler(
        ledger,
        gateway,
        payments=orchestrator.payments,
        webhooks=webhooks,
        interval=settings.reconcile_interval_s,
        live_rescues=lambda: sorted(set(orchestrator.live_rescues()) | set(runs.live_rescue_ids())),
    )
    return Services(
        settings=settings,
        clock=clock,
        ledger=ledger,
        gateway=gateway,
        orchestrator=orchestrator,
        webhooks=webhooks,
        reconciler=reconciler,
        runs=runs,
        spend=spend,
        limiter=RateLimiter(clock.now),
        sessions=SessionCodec(settings.secret_key, passcode=settings.passcode),
        paypal_view=PayPalView(gateway, clock, ttl_s=settings.paypal_poll_s),
    )
