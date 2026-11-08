"""/try runs: one broken server at a time, from boot to the end of the warranty.

A run goes through these stages::

    booting -> looking -> quoted -> checkout -> rescuing -> warranty -> finished
                        \\-> no_quote          \\-> finished (voided, stopped, refused)
    (any pre-payment stage) -> expired | failed | finished (Stop)

``RunManager`` owns the VM slots (``KEYHOLD_TRY_SLOTS``, default one). A run takes a slot when it starts and gives it back
when its server is torn down. A VM stays up through the warranty, because the warranty
re-probes it; a simulated server costs nothing, so its slot is released when the money decision
(capture or void) is made.

Money never moves here directly. Every PayPal operation goes through ``keyhold.rescue``'s
``Orchestrator`` (and so through the outbox with deterministic request ids):

- ``create_order`` -> ``Orchestrator.place_hold`` (order with intent AUTHORIZE for the quote)
- ``authorize`` -> ``Orchestrator.confirm_hold``, then ``Orchestrator.run`` on a worker thread
- ``stop`` -> ``Orchestrator.stop`` (cut the keyboard, then void)
- unpaid quotes past their deadline -> ``Orchestrator.stop`` as well
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import logging
import re
import secrets
import shutil
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from keyhold.agent.quote import FreeLook, clean_owner_note
from keyhold.faults import TRY_FAULTS, get_fault
from keyhold.pay.ledger import BUYER_CAN_RETRY, Ledger, LedgerError
from keyhold.rescue import Orchestrator, RescueReport, StopResult
from keyhold.types import NO_MONEY_CAPTURE, AgentEvent, Clock, MoneyOp, Order, OutboxStatus, PayPalError, RescueState

from .spend import MeteredModel, SpendMeter
from .worlds import World, WorldError, WorldFactory

__all__ = [
    "ACTIVE_STAGES",
    "STAGES",
    "InlineExecutor",
    "RunManager",
    "ThreadExecutor",
    "Thumb",
    "ThumbStore",
    "TryRefused",
    "TryRun",
]

log = logging.getLogger(__name__)

STAGES: dict[str, str] = {
    "booting": "Starting a fresh copy of the broken server",
    "looking": "Free look: screenshots only, no keys",
    "quoted": "Quote ready",
    "no_quote": "No hold offered",
    "checkout": "Waiting for PayPal approval",
    "rescuing": "Agent at the keyboard under the hold",
    "warranty": "Captured; warranty checks running",
    "finished": "Finished",
    "expired": "Quote expired unpaid",
    "failed": "Could not start",
}
ACTIVE_STAGES = frozenset({"booting", "looking", "quoted", "checkout", "rescuing", "warranty"})
PRE_PAYMENT = frozenset({"booting", "looking", "quoted", "checkout"})
PAYABLE = frozenset({"quoted", "checkout"})
KEEP_FINISHED_RUNS = 50


class TryRefused(Exception):
    """A /try request that cannot be served. ``code`` is stable for tests and the JS; ``status``
    is the HTTP status to answer with."""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class Executor(Protocol):
    def submit(self, name: str, fn: Callable[[], None]) -> None: ...


class ThreadExecutor:
    def submit(self, name: str, fn: Callable[[], None]) -> None:
        threading.Thread(target=fn, name=name, daemon=True).start()


class InlineExecutor:
    """Runs work on the caller's thread (tests)."""

    def submit(self, name: str, fn: Callable[[], None]) -> None:
        fn()


# --------------------------------------------------------------------------------------------
# Console thumbnails
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Thumb:
    n: int
    at: float
    turn: int
    png: bytes
    width: int
    height: int
    label: str


class ThumbStore:
    """Small PNG copies of console screenshots, per run. Repeated identical screens are stored
    once. With ``directory`` they are also written to disk (``<run id>/<n>.png`` plus an index),
    so a rescue's screens can still be shown after a restart; otherwise they live in memory."""

    RUN_ID = re.compile(r"[0-9a-f]{6,32}")

    def __init__(
        self,
        *,
        directory: Path | None = None,
        width: int = 640,
        per_run: int = 60,
        runs: int = 40,
        runs_on_disk: int = 300,
    ) -> None:
        self.directory = Path(directory) if directory is not None else None
        self.width = width
        self.per_run = per_run
        self.runs = runs
        self.runs_on_disk = runs_on_disk
        self._lock = threading.Lock()
        self._data: OrderedDict[str, list[Thumb]] = OrderedDict()
        self._last_sha: dict[str, str] = {}
        self._counter: dict[str, int] = {}
        self._rescues: dict[str, str] = {}

    def valid_run_id(self, run_id: str) -> bool:
        return bool(self.RUN_ID.fullmatch(run_id or ""))

    def add(self, run_id: str, png: bytes, *, at: float, turn: int, label: str) -> Thumb | None:
        if not self.valid_run_id(run_id):
            return None
        digest = hashlib.sha256(png).hexdigest()
        with self._lock:
            if self._last_sha.get(run_id) == digest:
                return None
        try:
            small, width, height = self._shrink(png)
        except Exception:  # noqa: BLE001 - a bad image must not stop a rescue
            return None
        with self._lock:
            self._last_sha[run_id] = digest
            n = self._counter.get(run_id, 0) + 1
            self._counter[run_id] = n
            thumbs = self._data.setdefault(run_id, [])
            self._data.move_to_end(run_id)
            thumb = Thumb(n, at, turn, small, width, height, label)
            thumbs.append(thumb)
            del thumbs[: max(0, len(thumbs) - self.per_run)]
            while len(self._data) > self.runs:
                old, _ = self._data.popitem(last=False)
                self._last_sha.pop(old, None)
                self._counter.pop(old, None)
        self._save(run_id, thumb)
        return thumb

    def bind(self, run_id: str, rescue_id: str) -> None:
        """Remember which rescue a run's screens belong to (on disk too)."""
        with self._lock:
            self._rescues[rescue_id] = run_id
        if self.directory is not None and self.valid_run_id(run_id):
            folder = self.directory / run_id
            with contextlib.suppress(OSError):
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "rescue").write_text(rescue_id)

    def run_for_rescue(self, rescue_id: str) -> str | None:
        with self._lock:
            found = self._rescues.get(rescue_id)
        if found is not None or self.directory is None or not self.directory.is_dir():
            return found
        for marker in self.directory.glob("*/rescue"):
            with contextlib.suppress(OSError):
                if marker.read_text().strip() == rescue_id:
                    with self._lock:
                        self._rescues[rescue_id] = marker.parent.name
                    return marker.parent.name
        return None

    def _shrink(self, png: bytes) -> tuple[bytes, int, int]:
        from PIL import Image

        with Image.open(io.BytesIO(png)) as img:
            img = img.convert("RGB")
            if img.width > self.width:
                height = max(1, round(img.height * self.width / img.width))
                img = img.resize((self.width, height), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            img.save(out, format="PNG", optimize=True)
            return out.getvalue(), img.width, img.height

    def _save(self, run_id: str, thumb: Thumb) -> None:
        if self.directory is None:
            return
        folder = self.directory / run_id
        try:
            fresh = not folder.exists()
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{thumb.n}.png").write_bytes(thumb.png)
            meta = {"n": thumb.n, "at": thumb.at, "turn": thumb.turn, "label": thumb.label,
                    "width": thumb.width, "height": thumb.height}
            with (folder / "index.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(meta) + "\n")
            stale = folder / f"{thumb.n - self.per_run}.png"
            stale.unlink(missing_ok=True)
            if fresh:
                self._prune_disk()
        except OSError:
            log.warning("could not save a console thumbnail for run %s", run_id, exc_info=True)

    def _prune_disk(self) -> None:
        assert self.directory is not None
        folders = sorted((p for p in self.directory.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
        for old in folders[: max(0, len(folders) - self.runs_on_disk)]:
            shutil.rmtree(old, ignore_errors=True)

    def _from_disk(self, run_id: str) -> list[Thumb]:
        if self.directory is None or not self.valid_run_id(run_id):
            return []
        index = self.directory / run_id / "index.jsonl"
        out: list[Thumb] = []
        try:
            lines = index.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            try:
                meta = json.loads(line)
                png = (self.directory / run_id / f"{int(meta['n'])}.png").read_bytes()
            except (OSError, ValueError, KeyError, TypeError):
                continue
            out.append(Thumb(int(meta["n"]), float(meta["at"]), int(meta["turn"]), png,
                             int(meta["width"]), int(meta["height"]), str(meta["label"])))
        return out[-self.per_run :]

    def list(self, run_id: str, after: int = 0) -> list[Thumb]:
        with self._lock:
            kept = self._data.get(run_id)
            if kept is not None:
                return [t for t in kept if t.n > after]
        return [t for t in self._from_disk(run_id) if t.n > after]

    def get(self, run_id: str, n: int) -> Thumb | None:
        for thumb in self.list(run_id, after=n - 1):
            if thumb.n == n:
                return thumb
        return None


# --------------------------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------------------------


@dataclass(eq=False)
class TryRun:
    id: str
    fault_id: str
    owner: str  # session id of the browser that started it
    ip: str
    created_at: float
    world_kind: str
    note: str | None = None  # the owner's description of the symptom, cleaned
    stage: str = "booting"
    detail: str = ""
    notes: list[tuple[float, str]] = field(default_factory=list)
    rescue_id: str | None = None
    order_id: str | None = None
    look: FreeLook | None = None
    world: World | None = None
    world_label: str = ""
    model: Any = None
    quote_deadline: float | None = None
    report: RescueReport | None = None
    stop_requested: bool = False
    finished_at: float | None = None
    version: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    @property
    def active(self) -> bool:
        return self.stage in ACTIVE_STAGES

    @property
    def stage_label(self) -> str:
        return STAGES.get(self.stage, self.stage)

    @property
    def fault_title(self) -> str:
        return get_fault(self.fault_id).title


class RunManager:
    def __init__(
        self,
        *,
        settings: Any,
        ledger: Ledger,
        orchestrator: Orchestrator,
        worlds: WorldFactory,
        spend: SpendMeter,
        clock: Clock,
        executor: Executor | None = None,
        thumbs: ThumbStore | None = None,
        gateway: Any = None,
    ) -> None:
        self.settings = settings
        self.ledger = ledger
        self.orch = orchestrator
        self.worlds = worlds
        self.spend = spend
        self.clock = clock
        self.executor: Executor = executor or ThreadExecutor()
        self.thumbs = thumbs or ThumbStore()
        self.gateway = gateway
        self._lock = threading.RLock()
        self._runs: OrderedDict[str, TryRun] = OrderedDict()
        self._by_rescue: dict[str, str] = {}
        self._slots: list[str] = []  # ids of the runs holding a test server, oldest first
        self._tls = threading.local()

    # -- lookups -------------------------------------------------------------------------------

    def get(self, run_id: str) -> TryRun | None:
        with self._lock:
            return self._runs.get(run_id)

    def for_rescue(self, rescue_id: str) -> TryRun | None:
        with self._lock:
            run_id = self._by_rescue.get(rescue_id)
            return self._runs.get(run_id) if run_id else None

    def runs(self) -> list[TryRun]:
        with self._lock:
            return list(self._runs.values())

    @property
    def slot_count(self) -> int:
        return max(1, min(8, int(getattr(self.settings, "try_slots", 1) or 1)))

    def slot_run(self) -> TryRun | None:
        """The oldest run holding a test server, or None when every server is free."""
        with self._lock:
            return self._runs.get(self._slots[0]) if self._slots else None

    def slots_used(self) -> int:
        with self._lock:
            return len(self._slots)

    def slots_full(self) -> bool:
        with self._lock:
            return len(self._slots) >= self.slot_count

    def live_rescue_ids(self) -> list[str]:
        """Rescues a run of this process is driving (the reconciler leaves them alone)."""
        with self._lock:
            return [r.rescue_id for r in self._runs.values() if r.rescue_id and r.active]

    def slot_message(self) -> str:
        with self._lock:
            used = [self._runs[i] for i in self._slots if i in self._runs]
        total = self.slot_count
        if len(used) < total:
            free = total - len(used)
            return "The test server is free." if total == 1 else f"{free} of {total} test servers are free."
        if total == 1:
            run = used[0]
            if run.stage == "warranty":
                return (
                    "Another rescue is in its warranty checks, which re-probe the running VM. "
                    "The VM is released when they end."
                )
            return f"Another run is using the test server ({run.stage_label.lower()}). Try again in a few minutes."
        warranty = sum(1 for run in used if run.stage == "warranty")
        tail = f" {warranty} of them are in warranty checks, which keep their VM until they end." if warranty else ""
        return f"All {total} test servers are in use.{tail} Try again in a few minutes."

    # -- stage bookkeeping ---------------------------------------------------------------------

    def _bump(self, run: TryRun) -> None:
        with self._lock:
            run.version += 1

    def _set(self, run: TryRun, stage: str, detail: str = "") -> None:
        with self._lock:
            run.stage = stage
            run.detail = detail
            run.version += 1

    def _note(self, run: TryRun, text: str) -> None:
        with self._lock:
            run.notes.append((self.clock.now(), text))
            run.version += 1

    def _release_slot(self, run: TryRun) -> None:
        with self._lock:
            if run.id in self._slots:
                self._slots.remove(run.id)

    def _teardown(self, run: TryRun) -> None:
        world = run.world
        if world is not None:
            try:
                world.close()
            except Exception:
                log.warning("closing the server of run %s failed", run.id, exc_info=True)
        self._release_slot(run)

    def _end(self, run: TryRun, stage: str, detail: str) -> None:
        with self._lock:
            if run.finished_at is None:
                run.finished_at = self.clock.now()
        self._set(run, stage, detail)
        self._teardown(run)

    # -- starting a run ------------------------------------------------------------------------

    def start(self, fault_id: str, *, owner: str, ip: str, note: str | None = None) -> TryRun:
        if fault_id not in TRY_FAULTS:
            raise TryRefused("unknown_fault", "Pick one of the three faults offered on this page.", 400)
        problems = self.worlds.problems()
        if problems:
            raise TryRefused("not_ready", "The test server is not ready: " + "; ".join(problems) + ".", 503)
        if self.worlds.paid_model and self.spend.remaining() < Decimal(self.settings.run_budget_usd):
            raise TryRefused(
                "budget",
                "The model budget for the last 24 hours is used up, so no new run can start. "
                "Runs already started finish normally.",
                503,
            )
        self.sweep()
        with self._lock:
            mine = [r for r in self._runs.values() if r.owner == owner and r.active and r.id in self._slots]
            if mine:
                raise TryRefused("own_run", "This browser already has a run in progress; finish or cancel it first.", 409)
            if len(self._slots) >= self.slot_count:
                raise TryRefused("busy", self.slot_message(), 409)
            run = TryRun(
                id=secrets.token_hex(6),
                fault_id=fault_id,
                owner=owner,
                ip=ip,
                created_at=self.clock.now(),
                world_kind=self.worlds.kind,
                note=clean_owner_note(note),
                detail="Starting a fresh copy of the server with the fault planted.",
            )
            self._runs[run.id] = run
            self._slots.append(run.id)
        self.executor.submit(f"keyhold-prepare-{run.id}", lambda: self._prepare(run))
        return run

    def _model_for(self, run: TryRun) -> Any:
        if run.model is None:
            assert run.world is not None
            if run.world.paid_model:
                run.model = MeteredModel(
                    run.world.model,
                    self.spend,
                    run_id=run.id,
                    budget_usd=Decimal(self.settings.run_budget_usd),
                    rescue_id=lambda: run.rescue_id,
                )
            else:
                run.model = run.world.model
        return run.model

    def _prepare(self, run: TryRun) -> None:
        try:
            world = self.worlds.boot(run.fault_id, run.id, lambda text: self._note(run, text))
        except WorldError as exc:
            self._end(run, "failed", f"The server could not be started: {exc}.")
            return
        except Exception as exc:
            log.exception("booting run %s failed", run.id)
            self._end(run, "failed", f"The server could not be started ({type(exc).__name__}).")
            return
        run.world = world
        run.world_label = world.label
        if run.stop_requested:
            self._end(run, "finished", "Stopped by the owner before the free look.")
            return
        self._set(run, "looking", "Taking a screenshot of the console. No key is pressed.")
        self._tls.run = run
        try:
            look, rec = self.orch.free_look(
                world.console, self._model_for(run), probe=world.probe, screen_text=world.screen_text,
                owner_note=run.note,
            )
        except Exception as exc:
            log.exception("free look of run %s failed", run.id)
            self._end(run, "failed", f"The free look failed ({type(exc).__name__}). Nothing was held.")
            return
        finally:
            self._tls.run = None
        run.look = look
        if rec is None:
            self._end(run, "no_quote", look.reason)
            return
        with self._lock:
            run.rescue_id = rec.id
            self._by_rescue[rec.id] = run.id
        self.thumbs.bind(run.id, rec.id)
        with self._lock:
            run.quote_deadline = self.clock.now() + float(self.settings.quote_ttl_s)
        if run.stop_requested:
            self.orch.stop(rec.id, "the owner pressed Stop before checkout")
            self._end(run, "finished", "Stopped by the owner before checkout. Nothing was held.")
            return
        self._set(run, "quoted", f"Diagnosis: {look.reason}")

    # -- checkout ------------------------------------------------------------------------------

    def _require_payable(self, run: TryRun) -> str:
        if run.stage not in PAYABLE or run.rescue_id is None:
            if run.stage == "expired":
                raise TryRefused("expired", "This quote expired before checkout. Start a new run.", 409)
            raise TryRefused("not_payable", f"This run cannot take a payment now ({run.stage_label.lower()}).", 409)
        if run.stop_requested:
            raise TryRefused("stopped", "This run was stopped.", 409)
        if run.quote_deadline is not None and self.clock.now() > run.quote_deadline:
            self._expire(run)
            raise TryRefused("expired", "This quote expired before checkout. Start a new run.", 409)
        return run.rescue_id

    def create_order(self, run: TryRun) -> Order:
        """Create (or return the already created) PayPal order with intent AUTHORIZE for the quote."""
        with run.lock:
            rescue_id = self._require_payable(run)
            try:
                order = self.orch.place_hold(rescue_id)
            except PayPalError:
                if self.ledger.get(rescue_id).state is RescueState.VOIDED:
                    self._end(run, "finished", self.ledger.get(rescue_id).end_reason or "PayPal refused the order.")
                raise
            run.order_id = order.id
            self._set(
                run,
                "checkout",
                f"PayPal order {order.id} (intent AUTHORIZE, ${order.amount}) is waiting for approval.",
            )
            return order

    def approve_offline(self, run: TryRun, order_id: str) -> None:
        """Offline mode only: the simulated buyer approves the order in FakePayPal."""
        approve = getattr(self.gateway, "approve", None)
        if not callable(approve):
            raise TryRefused("not_offline", "Approval is simulated only in offline mode.", 404)
        with run.lock:
            self._require_payable(run)
            if order_id != run.order_id:
                raise TryRefused("wrong_order", "That order does not belong to this run.", 404)
            approve(order_id)

    def authorize(self, run: TryRun, order_id: str) -> Any:
        """Authorize the order the owner approved. On a clean hold the agent starts."""
        with run.lock:
            if run.rescue_id is not None and run.stage in ("rescuing", "warranty"):
                return self.ledger.get(run.rescue_id)
            rescue_id = self._require_payable(run)
            owner = self.ledger.find(order_id=order_id)
            if owner is None or owner.id != rescue_id:
                raise TryRefused("wrong_order", "That order does not belong to this run.", 404)
            try:
                rec = self.orch.confirm_hold(rescue_id)
            except PayPalError as err:
                rec = self.ledger.get(rescue_id)
                if rec.state is RescueState.VOIDED:
                    self._end(run, "finished", rec.end_reason or "PayPal refused the authorization.")
                elif err.issue in BUYER_CAN_RETRY and rec.state is RescueState.HOLD_PENDING:
                    # The order is still approved: the buyer picks another funding source in the
                    # same PayPal session and the page authorizes again with a new request id.
                    self._set(run, "checkout", "PayPal declined that funding source. Choose another one to finish the hold.")
                    raise TryRefused(
                        "instrument_declined",
                        "PayPal declined that funding source. Choose another one in PayPal to finish the hold.",
                        402,
                    ) from None
                raise
            except LedgerError as exc:
                raise TryRefused("ledger", str(exc), 409) from None
            if rec.state is RescueState.HELD:
                run.quote_deadline = None
                self._set(run, "rescuing", "PayPal shows the hold as CREATED. The agent has the keyboard.")
                self.executor.submit(f"keyhold-rescue-{run.id}", lambda: self._rescue(run))
            elif rec.state is RescueState.VOIDED:
                self._end(run, "finished", rec.end_reason or "The hold was released.")
            else:
                self._set(
                    run,
                    "checkout",
                    f"PayPal has not confirmed the hold yet (authorization {rec.auth_status}). The keyboard stays locked.",
                )
            return self.ledger.get(rescue_id)

    # -- the rescue ----------------------------------------------------------------------------

    def _rescue(self, run: TryRun) -> None:
        rid = run.rescue_id
        world = run.world
        assert rid is not None and world is not None
        try:
            run.report = self.orch.run(
                rid,
                world.console,
                self._model_for(run),
                ssh=world.ssh,
                http=world.http,
                console_password=world.console_password,
                server_ssh_port=world.server_ssh_port,
            )
        except Exception as exc:
            log.exception("rescue %s stopped on an error", rid)
            self._note(run, f"The rescue stopped on an internal error ({type(exc).__name__}).")
            try:
                self.orch.stop(rid, "Keyhold stopped the rescue after an internal error")
            except Exception:
                log.exception("void after the error in rescue %s failed", rid)
        self._wait_for_capture(run, rid)
        rec = self.ledger.get(rid)
        if rec.state is RescueState.CAPTURED:
            self._set(run, "warranty", self.orch.config.probe_policy.warranty.label)
            if not world.keep_slot_for_warranty:
                self._release_slot(run)
            try:
                self.orch.resume_warranty(rid, ssh=world.ssh, http=world.http)
            except Exception:
                log.exception("warranty of rescue %s stopped on an error", rid)
        rec = self.ledger.get(rid)
        self._end(run, "finished", _final_sentence(rec, run.report, self.ledger.refund_due(rid)))

    def _wait_for_capture(self, run: TryRun, rid: str) -> None:
        """The probe passed but PayPal's answer to the capture was lost: keep the server up and
        replay the capture (same request id) for a while, so the warranty can still probe it."""
        op = self.ledger.get_op(rid, MoneyOp.CAPTURE)
        if op is None or op.status is not OutboxStatus.PENDING:
            return
        self._note(run, "PayPal has not answered the capture yet; Keyhold replays it with the same request id.")
        deadline = self.clock.now() + float(self.settings.capture_wait_s)
        while self.clock.now() < deadline:
            if self.orch.settle_capture(rid) is not None:
                return
            op = self.ledger.get_op(rid, MoneyOp.CAPTURE)
            if op is None or op.status is not OutboxStatus.PENDING:
                return
            self.clock.sleep(min(10.0, float(self.settings.capture_wait_s)))

    def resume_warranties(self) -> list[str]:
        """Start the warranty of every captured rescue nobody runs it for (a capture the
        reconciler completed, or one whose process restarted). The probe uses the transports of
        the run that made the rescue when this process still has it, otherwise transports built
        from the probe spec stored at checkout. Returns the rescue ids started."""
        started = []
        live = set(self.live_rescue_ids())
        for rid in self.orch.warranties_due():
            if rid in live:
                continue  # that run's own thread runs (or is about to run) the warranty
            run = self.for_rescue(rid)
            transports = None
            if run is not None and run.world is not None:
                transports = (run.world.ssh, run.world.http)
            else:
                maker = getattr(self.worlds, "transports_for", None)
                transports = maker(self.ledger.get(rid)) if callable(maker) else None
            if transports is None:
                note = "the warranty cannot probe: this process has no way to reach that server"
                if not any(e.detail == note for e in self.ledger.events(rid, kind="warranty_error")):
                    self.ledger.log(rid, "warranty_error", note, {})
                continue
            ssh, http = transports
            self.executor.submit(f"keyhold-warranty-{rid[:12]}", lambda rid=rid, ssh=ssh, http=http: self._warranty(rid, ssh, http))
            started.append(rid)
        return started

    def _warranty(self, rid: str, ssh: Any, http: Any) -> None:
        try:
            self.orch.resume_warranty(rid, ssh=ssh, http=http)
        except Exception:
            log.exception("warranty of rescue %s stopped on an error", rid)

    # -- owner Stop ----------------------------------------------------------------------------

    def stop(self, run: TryRun) -> StopResult | None:
        with run.lock:
            run.stop_requested = True
            self._bump(run)
            result = self.orch.stop(run.rescue_id) if run.rescue_id else None
            if run.stage in ("quoted", "checkout"):
                self._end(run, "finished", "Stopped by the owner before the hold. Nothing was charged.")
            elif result is not None and run.active:
                self._set(run, run.stage, f"Stop pressed. {result.detail}")
            return result

    # -- housekeeping --------------------------------------------------------------------------

    def _start_if_held(self, run: TryRun) -> None:
        """An authorization PayPal answered PENDING and later cleared (webhook or reconciler)
        moves the rescue to HELD after checkout returned. The owner paid, so start the agent."""
        if run.stage != "checkout" or run.rescue_id is None or run.stop_requested:
            return
        if self.ledger.get(run.rescue_id).state is not RescueState.HELD:
            return
        run.quote_deadline = None
        self._set(run, "rescuing", "PayPal confirmed the hold as CREATED. The agent has the keyboard.")
        self.executor.submit(f"keyhold-rescue-{run.id}", lambda: self._rescue(run))

    def _expire(self, run: TryRun) -> None:
        minutes = round(float(self.settings.quote_ttl_s) / 60)
        if run.rescue_id is not None:
            try:
                self.orch.stop(run.rescue_id, f"the quote was not paid within {minutes} minutes")
            except Exception:
                log.exception("expiring rescue %s failed", run.rescue_id)
        self._end(run, "expired", f"The quote was not paid within {minutes} minutes. Nothing was held.")

    def sweep(self) -> None:
        """Start runs whose hold PayPal confirmed late, expire unpaid quotes (releasing the
        server) and forget old finished runs."""
        now = self.clock.now()
        for run in self.runs():
            if run.stage == "checkout" and run.rescue_id and run.lock.acquire(blocking=False):
                try:
                    self._start_if_held(run)
                finally:
                    run.lock.release()
        for run in self.runs():
            due = run.stage in PAYABLE and run.quote_deadline is not None and now > run.quote_deadline
            if due and run.lock.acquire(blocking=False):  # a checkout in progress is left alone
                try:
                    if run.stage in PAYABLE:
                        self._expire(run)
                finally:
                    run.lock.release()
        with self._lock:
            done = [r for r in self._runs.values() if not r.active]
            for run in done[: max(0, len(done) - KEEP_FINISHED_RUNS)]:
                self._runs.pop(run.id, None)
                if run.rescue_id:
                    self._by_rescue.pop(run.rescue_id, None)

    def shutdown(self) -> None:
        for run in self.runs():
            run.stop_requested = True
        self.orch.shutdown()
        for run in self.runs():
            if run.stage in PRE_PAYMENT:
                self._teardown(run)

    # -- orchestrator events ---------------------------------------------------------------------

    def sink(self, rescue_id: str, event: AgentEvent) -> None:
        """``Orchestrator(on_event=...)``: keep console thumbnails and wake the status page."""
        run = self.for_rescue(rescue_id) if rescue_id else None
        if run is None:
            run = getattr(self._tls, "run", None)
        if run is None:
            return
        if event.kind == "screenshot":
            png = event.data.get("png")
            if isinstance(png, (bytes, bytearray)):
                label = "free look" if run.stage in ("booting", "looking") else f"turn {event.turn}"
                self.thumbs.add(run.id, bytes(png), at=event.at, turn=event.turn, label=label)
        self._bump(run)


def _final_sentence(rec: Any, report: RescueReport | None, refund_due: str | None = None) -> str:
    state = rec.state
    if state is RescueState.CAPTURED and refund_due:
        return (
            f"Captured ${rec.captured_amount}, but the warranty failed ({refund_due}). The full refund is "
            "sent as soon as PayPal accepts it; the reconciler keeps trying."
        )
    if rec.capture_status in NO_MONEY_CAPTURE:
        return f"PayPal declined the capture ({rec.capture_status}). Nothing was charged."
    if state is RescueState.CAPTURED and rec.capture_status == "PENDING":
        return (
            f"Captured ${rec.captured_amount}, but PayPal still shows the capture as PENDING. "
            "The warranty did not finish in this process."
        )
    if state is RescueState.WARRANTY_OK:
        return f"Captured ${rec.captured_amount}. The warranty ended with the server answering."
    if state is RescueState.REFUNDED:
        return f"Captured ${rec.captured_amount}, then refunded ${rec.refunded_amount} because the warranty failed."
    if state is RescueState.VOIDED:
        return f"Voided. Nothing was charged: {rec.end_reason or 'the hold was released'}."
    if state is RescueState.CAPTURED:
        return f"Captured ${rec.captured_amount}. The warranty did not finish in this process."
    if report is not None:
        return report.reason
    return f"The rescue ended in {state.value}."
