"""FastAPI application.

Routes: / (what it is), /try (passcode gate, per-IP rate limit, one VM at a time, model-spend cap),
the live status page (SSE), /judge (ledger of all runs), /webhooks/paypal, /api/orders,
/api/orders/{id}/authorize, /healthz.

Run it with ``uv run python -m web`` (or ``uvicorn web.app:create_app --factory``). Importing this
module has no side effects; ``create_app()`` reads the environment (see ``web.config``).

The checkout follows the JS SDK v6 flow: the page loads ``web-sdk/v6/core`` from the PayPal
sandbox, calls ``createInstance`` with a client token from ``/api/client-token``, checks
``findEligibleMethods``, and starts a PayPal one-time payment session whose order promise calls
``POST /api/orders`` (the server creates the order with intent AUTHORIZE for the stored quote;
the browser never sends an amount). ``onApprove`` calls ``POST /api/orders/{id}/authorize``, which
authorizes the order and, when PayPal shows the hold as CREATED, lends the keyboard to the agent.
In offline mode the same two endpoints are used, with a simulated approval in between.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from keyhold.faults import TRY_FAULTS, get_fault
from keyhold.pay.client import HANDLED_WEBHOOK_HEADERS, header_value
from keyhold.pay.ledger import UnknownRescue
from keyhold.types import MoneyOp, OutboxStatus, PayPalError, RescueState

from .config import Settings
from .runs import PRE_PAYMENT, TryRefused, TryRun
from .security import (
    CSRF_HEADER,
    SESSION_COOKIE,
    Session,
    check_passcode,
    judge_token,
    client_ip,
    security_headers,
    session_cookie_kwargs,
)
from .services import Services, build_services
from .story import (
    KeyMoments,
    code_paths,
    evidence_markup,
    hms,
    paypal_table,
    progress_view,
    run_progress,
    short_id,
    status_title,
    visible_moments,
)
from .views import (
    _record_view,
    as_json,
    fmt_time,
    judge_view,
    money_view,
    probe_view,
    quote_view,
    rescue_snapshot,
    run_view,
    thumb_view,
    ticks_view,
    timeline_entry,
    webhook_entry,
)

__all__ = ["SDK_URL", "create_app"]

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
SDK_URL = "https://www.sandbox.paypal.com/web-sdk/v6/core"
_NO_COOKIE_PREFIXES = ("/static/", "/webhooks/", "/healthz")


class ApiError(Exception):
    def __init__(self, status: int, message: str, code: str = "error", headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.headers = headers or {}


def _sse(event: str, data: Any, *, event_id: str | int | None = None) -> str:
    lines = [f"event: {event}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    payload = json.dumps(data, separators=(",", ":"), default=str)
    lines.append(f"data: {payload}")
    return "\n".join(lines) + "\n\n"


# The three demo paths on /try, in the order a judge should take them (D3): the capture first,
# so a judge who does one path sees the money story; then Stop; then the honest no-hold stop.
# Times from Break to the end: simulated (measured offline: about 30 s to the charge, 25 s for the
# Stop path, a few seconds to the no-hold page) and on a KVM test VM with Claude (a boot and free
# look of about a minute; the hosted capture came 1 min 45 s after the hold). "done" is the
# outcome that path is there to show.
_PATHS = {
    "sshd-config-typo": {"path": 1, "outcome": "Fixed and charged", "short": "Charged", "sim": "~1 min",
                         "vm": "~4 min", "chip": "hold", "icon": "f-sshd", "done": "charged"},
    "fstab-missing-disk": {"path": 2, "outcome": "Stop, $0", "short": "Stop, $0", "sim": "~30 s",
                           "vm": "~2 min", "chip": "hold · try Stop", "icon": "f-fstab", "done": "stopped"},
    "grub-password": {"path": 3, "outcome": "No hold", "short": "No hold", "sim": "~10 s", "vm": "~1 min",
                      "chip": "No hold", "icon": "f-grub", "done": "no_hold"},
}
# The three paths back to back, in minutes, without waiting for a server.
_ALL_PATHS_MIN = {"sim": 2, "vm": 7}


def _try_minutes(simulated: bool, warranty_wait_s: float = 0.0) -> int:
    """About how long the three paths take: on a single test VM the first rescue's warranty keeps
    the VM before the next path can start, so its length is added."""
    if simulated:
        return _ALL_PATHS_MIN["sim"]
    return _ALL_PATHS_MIN["vm"] + int(-(-max(0.0, warranty_wait_s) // 60))


def _fault_cards(simulated: bool = True, stop_window_s: float | None = None) -> list[dict[str, Any]]:
    """The three /try cards, in path order. ``stop_window_s``: about how long Stop stays useful on
    the simulated Stop path (the line under its button says so)."""
    cards = []
    window = f" (about {max(5, int(round(stop_window_s / 5.0)) * 5)} s here)" if simulated and stop_window_s else ""
    fine = {
        "sshd-config-typo": "Starts a fresh copy with this fault, then quotes. Nothing is held yet.",
        "fstab-missing-disk": f"Approve the hold, then press Stop while the agent works{window}: you pay $0.",
        "grub-password": "Starts a fresh copy with this fault. Keyhold offers no hold here, by design.",
    }
    blurbs = {
        "sshd-config-typo": "A misspelled option in sshd_config stops SSH from starting. The web page still answers.",
        "fstab-missing-disk": "A data disk was removed but stayed in /etc/fstab. The boot stops in emergency mode.",
        "grub-password": "The boot menu asks for a GRUB password nobody has. The console alone cannot fix this.",
    }
    short_blurbs = {
        "sshd-config-typo": "SSH will not start after an edit.",
        "fstab-missing-disk": "Boots to emergency mode.",
        "grub-password": "The console alone cannot fix it.",
    }
    hints = {
        "sshd-config-typo": "ssh: connection refused since I edited sshd_config",
        "fstab-missing-disk": "rebooted after removing the backup disk, now no SSH",
        "grub-password": "reboot hangs at a boot menu asking for a password",
    }
    for fault_id in TRY_FAULTS:
        fault = get_fault(fault_id)
        path = _PATHS.get(fault.id, {})
        price = f"{fault.price:.2f}" if fault.price is not None else None
        whole = f"${fault.price:.0f}" if fault.price is not None else ""
        chip = path.get("chip", "")
        cards.append(
            {
                "id": fault.id,
                "title": fault.title,
                "fault_class": fault.fault_class,
                "tier": fault.tier.value if fault.tier else None,
                "price": price,
                "blurb": blurbs.get(fault.id, ""),
                "short_blurb": short_blurbs.get(fault.id, blurbs.get(fault.id, "")),
                "note_hint": hints.get(fault.id, "what you saw before SSH stopped"),
                "solvable": fault.solvable,
                "path": path.get("path"),
                "outcome": path.get("outcome", ""),
                "short_outcome": path.get("short", ""),
                "time": path.get("sim" if simulated else "vm", ""),
                "chip": f"{whole} {chip}" if price else chip,
                "icon": path.get("icon", "f-sshd"),
                "fine": fine.get(fault.id, ""),
                "whole": whole,
                "goal": path.get("done", ""),
            }
        )
    return sorted(cards, key=lambda c: c["path"] or 99)


def create_app(settings: Settings | None = None, *, services: Services | None = None) -> FastAPI:
    if services is None:
        settings = settings or Settings.from_env()
        services = build_services(settings)
    settings = services.settings
    svc = services

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if settings.background:
            svc.start_background()
        try:
            yield
        finally:
            if settings.background:
                svc.close()

    app = FastAPI(title="Keyhold", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.services = svc
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["utc"] = lambda epoch: (fmt_time(epoch) or "").replace("T", " ").replace("Z", " UTC")
    templates.env.filters["hms"] = hms
    templates.env.filters["visible_moments"] = visible_moments
    templates.env.filters["short_id"] = short_id
    templates.env.filters["code_paths"] = code_paths
    templates.env.filters["evidence"] = evidence_markup
    templates.env.filters["status_title"] = status_title
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    # -- middleware ---------------------------------------------------------------------------

    @app.middleware("http")
    async def session_and_headers(request: Request, call_next: Any) -> Response:
        now = svc.clock.now()
        session = svc.sessions.decode(request.cookies.get(SESSION_COOKIE), now) or Session.new(now)
        request.state.session = session
        response = await call_next(request)
        if session.changed and not request.url.path.startswith(_NO_COOKIE_PREFIXES):
            response.set_cookie(SESSION_COOKIE, svc.sessions.encode(session), **session_cookie_kwargs(request))
        security_headers(response)
        return response

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse({"error": exc.message, "code": exc.code}, status_code=exc.status, headers=exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        """A browser asking for a page that is not there gets a page with a way on; everything
        else (APIs, webhooks, static files, scripts) keeps the JSON answer."""
        path = request.url.path
        wants_html = "text/html" in request.headers.get("accept", "")
        if not wants_html or path.startswith(("/api/", "/webhooks/", "/static/", "/healthz", "/judge.json")):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))
        if exc.status_code == 404:
            return error_page(request, 404, "Page not found", "There is nothing at this address.")
        return error_page(request, exc.status_code, "That did not work", str(exc.detail))

    # -- helpers ------------------------------------------------------------------------------

    def session_of(request: Request) -> Session:
        return request.state.session

    def ip_of(request: Request) -> str:
        return client_ip(request, trust_proxy=settings.trust_proxy, edge_key=settings.edge_key)

    agent_name = "The recorded agent" if settings.simulated else "Claude"
    paypal_source = "FakePayPal" if settings.offline else "the PayPal sandbox"

    def warranty_plan() -> Any:
        return svc.orchestrator.config.probe_policy.warranty

    def page(request: Request, name: str, context: dict[str, Any], status: int = 200) -> HTMLResponse:
        session = session_of(request)
        path = request.url.path
        base = {
            "settings": settings,
            "banners": settings.banners(),
            "full_banners": path in ("/", "/try"),
            "csrf": session.csrf,
            "path": path,
            "offline": settings.offline,
            "agent_name": agent_name,
            "paypal_source": paypal_source,
            "owns_rescue": False,
        }
        base.update(context)
        response = templates.TemplateResponse(request, name, base, status_code=status)
        response.headers["Cache-Control"] = "no-store"
        return response

    def error_page(request: Request, status: int, title: str, message: str) -> HTMLResponse:
        return page(request, "error.html", {"title": title, "message": message, "status": status}, status)

    def api_guard(request: Request, *, csrf: bool = True, unlocked: bool = True) -> Session:
        session = session_of(request)
        wait = svc.limiter.hit("api", ip_of(request), limit=settings.api_calls_per_minute, window_s=60.0)
        if wait is not None:
            raise ApiError(429, "Too many requests. Wait a moment and try again.", "rate_limited",
                           {"Retry-After": str(int(wait) + 1)})
        if csrf and not session.csrf_ok(request.headers.get(CSRF_HEADER)):
            raise ApiError(403, "This request is missing its security token. Reload the page.", "csrf")
        if unlocked and settings.passcode_required and not session.unlocked:
            raise ApiError(403, "Enter the passcode on /try first.", "locked")
        return session

    def owned_run(session: Session, run: TryRun | None) -> TryRun:
        if run is None or run.owner != session.sid:
            raise ApiError(404, "No such run for this browser.", "not_found")
        return run

    def paypal_error(err: PayPalError) -> ApiError:
        text = f"PayPal answered {err.issue}." + (f" Debug id {err.debug_id}." if err.debug_id else "")
        return ApiError(502 if err.retryable else 422, text, "paypal")

    def refused(exc: TryRefused) -> ApiError:
        return ApiError(exc.status, exc.message, exc.code)

    def find_rescue(rescue_id: str) -> Any:
        try:
            return svc.ledger.get(rescue_id)
        except UnknownRescue:
            return None

    def owns(request: Request, rescue_id: str) -> bool:
        """The viewer started this rescue's run (the pages then say "you", else "the owner")."""
        run = svc.runs.for_rescue(rescue_id)
        return run is not None and run.owner == session_of(request).sid

    def snapshot(rec: Any, live: dict[str, Any] | None, *, owner: bool = True) -> dict[str, Any]:
        run = svc.runs.for_rescue(rec.id)
        thumbs_run = run.id if run is not None else svc.runs.thumbs.run_for_rescue(rec.id)
        return rescue_snapshot(
            svc.ledger,
            rec,
            live=live,
            run=run,
            thumbs=svc.runs.thumbs.list(thumbs_run) if thumbs_run else [],
            thumbs_run_id=thumbs_run,
            policy_label=svc.orchestrator.config.probe_policy.label,
            plan=warranty_plan(),
            agent=agent_name,
            source=paypal_source,
            owner=owner,
            webhooks_verifiable=settings.webhooks_verifiable,
        )

    def run_progress_for(run: TryRun, rec: Any = None) -> dict[str, Any]:
        """The progress strip for a run page: from the rescue row once there is one."""
        if rec is None and run.rescue_id:
            rec = find_rescue(run.rescue_id)
        if rec is not None:
            return progress_view(rec, svc.ledger.events(rec.id), plan=warranty_plan(), agent=agent_name)
        look = run.look
        return run_progress(
            run.stage,
            no_quote_reason=look.reason if look is not None and look.quote is None else None,
            honest_stop=run.stage == "no_quote" and look is not None and look.fault_class == "console_locked",
            cancelling=run.stop_requested,
            look_failed=run.look_failed,
        )

    def finished_examples() -> dict[str, str | None]:
        """Links for "Short on time? See a finished one": the latest captured and stopped rescues
        in the ledger (public pages), and the no-hold list on /judge."""
        captured = stopped = None
        for rec in reversed(svc.ledger.rescues()):
            if captured is None and rec.state in (RescueState.WARRANTY_OK, RescueState.CAPTURED) and rec.captured_amount:
                captured = f"/rescues/{rec.id}"
            if stopped is None and rec.state is RescueState.VOIDED and rec.authorization_id \
                    and rec.end_reason == "the owner pressed Stop":
                stopped = f"/rescues/{rec.id}"
            if captured and stopped:
                break
        return {"captured": captured, "stopped": stopped,
                "no_hold": "/judge#no-hold" if svc.runs.no_hold_runs(limit=1) else None}

    async def live_read(rec: Any, *, fresh: bool = False) -> dict[str, Any]:
        return await run_in_threadpool(svc.paypal_view.read, rec, fresh=fresh)

    def can_stop(session: Session, rec: Any) -> bool:
        run = svc.runs.for_rescue(rec.id)
        return (
            run is not None
            and run.owner == session.sid
            and run.active
            and rec.state in (RescueState.QUOTED, RescueState.HOLD_PENDING, RescueState.HELD,
                              RescueState.ACTIVE, RescueState.PROBING)
        )

    # -- pages --------------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return page(request, "index.html", {"faults": _fault_cards(settings.simulated),
                                            "try_minutes": try_minutes()})

    def boot_span() -> str:
        worlds = svc.runs.worlds
        return worlds.boot_span() if hasattr(worlds, "boot_span") else "30 to 90 seconds"

    def warranty_wait_s() -> float:
        """How long a capture's warranty keeps the only test VM (0 when it does not)."""
        if settings.simulated or svc.runs.slot_count > 1:
            return 0.0
        plan = warranty_plan()
        return float(plan.interval_s) * int(plan.checks)

    def try_minutes() -> int:
        return _try_minutes(settings.simulated, warranty_wait_s())

    def path_results(sid: str) -> dict[str, dict[str, Any]]:
        """This browser's latest result on each /try path: what it showed ("Charged $9", "Stopped,
        $0", "No hold"), where to see it, and whether it is the outcome the path is there for. A
        charge whose warranty is still running counts: the money story is told."""
        out: dict[str, dict[str, Any]] = {}
        for run in svc.runs.runs():  # oldest first: a later run of the same fault wins
            if run.owner != sid or run.fault_id not in _PATHS:
                continue
            result = None
            if run.stage == "no_quote":
                result = ("no_hold", "No hold", f"/runs/{run.id}")
            elif run.rescue_id and (run.stage in ("warranty", "finished") or not run.active):
                rec = find_rescue(run.rescue_id)
                if rec is None:
                    continue
                href = f"/rescues/{rec.id}"
                if rec.state in (RescueState.CAPTURED, RescueState.WARRANTY_OK) and rec.captured_amount \
                        and rec.capture_status not in ("DECLINED", "DENIED", "FAILED"):
                    result = ("charged", f"Charged ${rec.captured_amount:.2f}", href)
                elif rec.state is RescueState.REFUNDED:
                    result = ("refunded", "Refunded", href)
                elif rec.state is RescueState.VOIDED and rec.authorization_id and rec.end_reason == "the owner pressed Stop":
                    result = ("stopped", "Stopped, $0", href)
                elif rec.state is RescueState.VOIDED and rec.authorization_id:
                    result = ("voided", "Voided, $0", href)
            if result is not None:
                kind, label, href = result
                out[run.fault_id] = {"kind": kind, "label": label, "href": href,
                                     "ok": kind == _PATHS[run.fault_id]["done"]}
        return out

    def look_span() -> str:
        worlds = svc.runs.worlds
        return worlds.look_span() if hasattr(worlds, "look_span") else "20 to 60 seconds"

    def try_context(request: Request, **extra: Any) -> dict[str, Any]:
        session = session_of(request)
        ip = ip_of(request)
        mine = [r for r in svc.runs.runs() if r.owner == session.sid and r.active]
        my_run = mine[0] if mine else None
        busy = svc.runs.busy_view(session.sid)
        now = svc.clock.now()
        runs_left = svc.limiter.remaining("start", ip, limit=settings.runs_per_hour, window_s=3600.0)
        next_run_at = (svc.limiter.next_at("start", ip, limit=settings.runs_per_hour, window_s=3600.0)
                       if runs_left == 0 else None)
        my_holds_slot = my_run is not None and svc.runs.holds_slot(my_run)
        window = svc.runs.worlds.stop_window_s() if hasattr(svc.runs.worlds, "stop_window_s") else None
        faults = _fault_cards(settings.simulated, window)
        # The paths this browser has done: their cards say so, and "Start here" / "Next" moves to
        # the first path not done yet, so a judge does not spend a run repeating one.
        results = path_results(session.sid)
        for card in faults:
            card["done"] = results.get(card["id"])
        next_path = next((c for c in faults if not (c["done"] and c["done"]["ok"])), None)
        context = {
            "faults": faults,
            "next_id": next_path["id"] if next_path else None,
            "all_done": next_path is None,
            "try_minutes": try_minutes(),
            "warranty_wait_min": int(-(-warranty_wait_s() // 60)),
            "busy": busy,
            "free_in_s": max(0, int(busy["free_at"] - now)) if busy["free_at"] else None,
            "next_run_in_s": max(0, int(next_run_at - now)) if next_run_at else None,
            "examples": finished_examples(),
            "cancelled": request.query_params.get("cancelled") == "1",
            "just_unlocked": request.query_params.get("unlocked") == "1" and session.unlocked,
            "my_progress": run_progress_for(my_run) if my_run is not None else None,
            "my_holds_slot": my_holds_slot,
            "my_warranty": svc.runs.warranty_progress(my_run) if my_run is not None else None,
            "my_free_at": (svc.runs.free_at(my_run) or (None, None))[0] if my_holds_slot else None,
            "unlocked": session.unlocked or not settings.passcode_required,
            "passcode_set": bool(settings.passcode),
            "slot_busy": svc.runs.slots_full(),
            "slot_message": svc.runs.slot_message(),
            "slots_total": svc.runs.slot_count,
            "boot_span": boot_span(),
            "slots_free": svc.runs.slot_count - svc.runs.slots_used(),
            "problems": svc.runs.worlds.problems(),
            "paid_model": svc.runs.worlds.paid_model,
            "spend_remaining": f"{svc.spend.remaining():.2f}",
            "spend_cap": f"{settings.spend_cap_usd:.2f}",
            "runs_left": runs_left,
            "next_run_at": next_run_at,
            "runs_per_hour": settings.runs_per_hour,
            "my_run": my_run,
            "error": None,
        }
        context.update(extra)
        return context

    async def try_ctx(request: Request, **extra: Any) -> dict[str, Any]:
        # Off the event loop: checking a VM lab may run docker.
        return await run_in_threadpool(try_context, request, **extra)

    @app.get("/try", response_class=HTMLResponse)
    async def try_page(request: Request) -> HTMLResponse:
        return page(request, "try.html", await try_ctx(request))

    @app.get("/j/{token}")
    async def judge_link(request: Request, token: str) -> Response:
        """One-click unlock for judges: the link in the testing instructions."""
        session = session_of(request)
        wait = svc.limiter.hit("unlock", ip_of(request), limit=settings.unlock_attempts, window_s=settings.unlock_window_s)
        if wait is not None:
            return error_page(request, 429, "Too many attempts", f"Try again in {int(wait // 60) + 1} minutes.")
        expected = judge_token(settings.secret_key, settings.passcode)
        if not expected or not check_passcode(token, expected):
            return error_page(request, 404, "This link does not work",
                              "The judge link is wrong or was replaced. Use the passcode on /try instead.")
        session.unlock()
        # The page says once that this browser is unlocked.
        return RedirectResponse("/try?unlocked=1", status_code=303)

    @app.post("/try/unlock")
    async def try_unlock(request: Request) -> Response:
        session = session_of(request)
        form = await request.form()
        if not session.csrf_ok(str(form.get("csrf", ""))):
            return error_page(request, 403, "Security token missing", "Reload the page and try again.")
        if not settings.passcode_required:
            return RedirectResponse("/try", status_code=303)
        wait = svc.limiter.hit("unlock", ip_of(request), limit=settings.unlock_attempts, window_s=settings.unlock_window_s)
        if wait is not None:
            response = page(
                request,
                "try.html",
                await try_ctx(request, error=f"Too many passcode attempts. Try again in {int(wait // 60) + 1} minutes."),
                429,
            )
            response.headers["Retry-After"] = str(int(wait) + 1)
            return response
        if not settings.passcode:
            return page(
                request, "try.html",
                await try_ctx(request, error="The operator has not set a passcode, so /try is closed in this mode."), 503,
            )
        if not check_passcode(str(form.get("passcode", "")), settings.passcode):
            return page(request, "try.html", await try_ctx(request, error="That passcode is not right."), 401)
        session.unlock()
        return RedirectResponse("/try", status_code=303)

    @app.post("/try/start")
    async def try_start(request: Request) -> Response:
        session = session_of(request)
        form = await request.form()
        if not session.csrf_ok(str(form.get("csrf", ""))):
            return error_page(request, 403, "Security token missing", "Reload the page and try again.")
        if settings.passcode_required and not session.unlocked:
            return RedirectResponse("/try", status_code=303)
        fault_id = str(form.get("fault", ""))
        if fault_id not in TRY_FAULTS:
            return page(request, "try.html", await try_ctx(request, error="Pick one of the three faults below."), 400)
        ip = ip_of(request)
        wait = svc.limiter.hit("start", ip, limit=settings.runs_per_hour, window_s=3600.0)
        if wait is not None:
            response = page(
                request,
                "try.html",
                await try_ctx(request, error=f"This address started {settings.runs_per_hour} runs in the last hour. "
                                           f"Try again in {int(wait // 60) + 1} minutes."),
                429,
            )
            response.headers["Retry-After"] = str(int(wait) + 1)
            return response
        try:
            note = str(form.get("note", ""))[:2000]
            run = await run_in_threadpool(svc.runs.start, fault_id, owner=session.sid, ip=ip, note=note)
        except TryRefused as exc:
            if exc.code == "own_run":
                # The same fault again (a double submit): go back to the run that is already going.
                same = [r for r in svc.runs.runs() if r.owner == session.sid and r.active and r.fault_id == fault_id]
                if same:
                    return RedirectResponse(f"/runs/{same[0].id}", status_code=303)
            return page(request, "try.html", await try_ctx(request, error=exc.message), exc.status)
        return RedirectResponse(f"/runs/{run.id}", status_code=303)

    @app.get("/try/status")
    async def try_status(request: Request) -> JSONResponse:
        """What /try polls every 10 s while the test server is busy, to turn the buttons back on."""
        api_guard(request, csrf=False, unlocked=False)
        session = session_of(request)
        problems = await run_in_threadpool(svc.runs.worlds.problems)
        busy = svc.runs.busy_view(session.sid)
        now = svc.clock.now()
        return JSONResponse(
            {
                "busy": busy["busy"],
                "ready": not problems,
                "message": busy["message"],
                "mine": busy["mine"],
                "watchUrl": busy["watch_url"],
                "freeAt": hms(busy["free_at"]) if busy["free_at"] else None,
                "freeInS": max(0, int(busy["free_at"] - now)) if busy["free_at"] else None,
                "slotsFree": svc.runs.slot_count - svc.runs.slots_used(),
                "slotsTotal": svc.runs.slot_count,
                "runsLeft": svc.limiter.remaining("start", ip_of(request), limit=settings.runs_per_hour,
                                                  window_s=3600.0),
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_page(request: Request, run_id: str) -> Response:
        session = session_of(request)
        run = svc.runs.get(run_id)
        if run is None or run.owner != session.sid:
            return error_page(request, 404, "Run not found", "This run does not exist or was started in another browser.")
        if run.rescue_id and run.stage in ("rescuing", "warranty"):
            return RedirectResponse(f"/rescues/{run.rescue_id}", status_code=303)
        rec = find_rescue(run.rescue_id) if run.rescue_id else None
        if rec is not None and run.stage == "finished" and rec.state not in (RescueState.QUOTED, RescueState.HOLD_PENDING) \
                and rec.authorization_id:
            return RedirectResponse(f"/rescues/{run.rescue_id}", status_code=303)
        context = {
            # Only the boot and the free look add screens; elsewhere the crop hugs the text.
            "run": run_view(run, svc.runs.thumbs.list(run.id), tight=run.stage not in ("booting", "looking")),
            "quote": quote_view(rec.quote, run.look.title if run.look else None) if rec is not None else None,
            "probe": probe_view(rec.probe_spec, rec.probe_spec_hash) if rec is not None else None,
            "rescue": rec,
            "payable": run.stage in ("quoted", "checkout"),
            "sdk_url": SDK_URL,
            "minutes_left": None
            if run.quote_deadline is None
            else max(1, round((run.quote_deadline - svc.clock.now()) / 60)),
            "seconds_left": None if run.quote_deadline is None else max(0, int(run.quote_deadline - svc.clock.now())),
            "prog": run_progress_for(run, rec),
            "boot_span": boot_span(),
            "look_span": look_span(),
            "elapsed_s": max(0, int(svc.clock.now() - run.created_at)),
            # The free look has its own timer: a long boot must not make a normal look read as late.
            "look_s": max(0, int(svc.clock.now() - run.look_started_at)) if run.look_started_at else 0,
            "cancelling": run.stop_requested and run.stage in ("booting", "looking"),
            "owns_rescue": True,
        }
        return page(request, "run.html", context)

    @app.get("/api/runs/{run_id}")
    async def run_status(request: Request, run_id: str) -> JSONResponse:
        """The run page's fallback when its event stream closes: the stage and the progress strip,
        for the browser that started the run only."""
        session = api_guard(request, csrf=False, unlocked=False)
        run = owned_run(session, svc.runs.get(run_id))
        return JSONResponse({"stage": run.stage, "rescueId": run.rescue_id, "progress": run_progress_for(run)},
                            headers={"Cache-Control": "no-store"})

    @app.post("/runs/{run_id}/cancel")
    async def cancel_run(request: Request, run_id: str) -> Response:
        """Cancel before a hold: release the server and go back to /try. Same Stop underneath
        (the quote, or an unapproved order, is voided); the page says "Nothing was held"."""
        session = session_of(request)
        form = await request.form()
        if not session.csrf_ok(str(form.get("csrf", ""))):
            return error_page(request, 403, "Security token missing", "Reload the page and try again.")
        run = svc.runs.get(run_id)
        if run is None or run.owner != session.sid:
            return error_page(request, 404, "Run not found", "This run does not exist or was started in another browser.")
        # Decided under the run's lock: if the hold went live meanwhile, that is Stop on the live
        # page, never a cancel that would void a hold the owner just approved.
        if await run_in_threadpool(svc.runs.cancel, run) == "live":
            return RedirectResponse(f"/rescues/{run.rescue_id}", status_code=303)
        return RedirectResponse("/try?cancelled=1", status_code=303)

    @app.get("/runs/{run_id}/events")
    async def run_events(request: Request, run_id: str) -> Response:
        session = session_of(request)
        run = svc.runs.get(run_id)
        if run is None or run.owner != session.sid:
            raise ApiError(404, "No such run for this browser.", "not_found")

        async def stream() -> AsyncIterator[str]:
            version = -1
            started = time.monotonic()
            last_ping = started
            thumb_cursor = 0
            yield "retry: 3000\n\n"
            while True:
                current = svc.runs.get(run_id)
                if current is None:
                    yield _sse("end", {"reason": "gone"})
                    return
                for thumb in svc.runs.thumbs.list(run_id, after=thumb_cursor):
                    thumb_cursor = thumb.n
                    yield _sse("thumb", thumb_view(run_id, thumb))
                if current.version != version:
                    version = current.version
                    yield _sse("run", {**run_view(current, []), "progress": run_progress_for(current)})
                if current.stage not in PRE_PAYMENT:
                    yield _sse("end", {"stage": current.stage, "rescue_id": current.rescue_id})
                    return
                now = time.monotonic()
                if now - started > settings.sse_max_s:
                    return
                if now - last_ping > 15:
                    last_ping = now
                    yield ": ping\n\n"
                await asyncio.sleep(settings.sse_poll_s)

        return StreamingResponse(stream(), media_type="text/event-stream", headers=_SSE_HEADERS)

    @app.get("/runs/{run_id}/thumbs/{n}.png")
    async def thumb(run_id: str, n: int) -> Response:
        if not svc.runs.thumbs.valid_run_id(run_id):
            return Response(status_code=404)
        found = svc.runs.thumbs.get(run_id, n)
        if found is None:
            return Response(status_code=404)
        return Response(found.png, media_type="image/png", headers={"Cache-Control": "public, max-age=86400, immutable"})

    @app.get("/rescues/{rescue_id}", response_class=HTMLResponse)
    async def rescue_page(request: Request, rescue_id: str) -> HTMLResponse:
        rec = find_rescue(rescue_id)
        if rec is None:
            return error_page(request, 404, "Rescue not found", "There is no rescue with that id in this ledger.")
        live = await live_read(rec)
        session = session_of(request)
        run = svc.runs.for_rescue(rec.id)
        mine = run is not None and run.owner == session.sid
        snap = snapshot(rec, live, owner=mine)
        # While this rescue's warranty keeps the only test server, the next path starts later.
        slot_free_at = None
        if run is not None and run.stage == "warranty" and svc.runs.holds_slot(run) and svc.runs.slots_full():
            slot_free_at = (svc.runs.free_at(run) or (None, None))[0]
        prices = {c["id"]: c["whole"] for c in _fault_cards(settings.simulated)}
        return page(
            request,
            "rescue.html",
            {
                "snap": snap,
                "rescue_id": rec.id,
                "can_stop": can_stop(session, rec),
                "owns_rescue": mine,
                "fault_id": run.fault_id if run is not None else None,
                "slot_free_at": slot_free_at,
                "slot_free_in_s": max(0, int(slot_free_at - svc.clock.now())) if slot_free_at else None,
                "prices": prices,
            },
        )

    @app.get("/api/rescues/{rescue_id}")
    async def rescue_json(request: Request, rescue_id: str) -> JSONResponse:
        # Public (the ids are on /judge), so it is rate-limited per address and reads PayPal
        # through the shared per-rescue cache: callers cannot turn it into a PayPal read flood.
        api_guard(request, csrf=False, unlocked=False)
        rec = find_rescue(rescue_id)
        if rec is None:
            raise ApiError(404, "No such rescue.", "not_found")
        live = await live_read(rec)
        return JSONResponse(snapshot(rec, live, owner=owns(request, rec.id)), headers={"Cache-Control": "no-store"})

    @app.get("/rescues/{rescue_id}/events")
    async def rescue_events(request: Request, rescue_id: str) -> Response:
        wait = svc.limiter.hit("sse", ip_of(request), limit=settings.sse_streams_per_minute, window_s=60.0)
        if wait is not None:
            raise ApiError(429, "Too many status streams from this address. Wait a moment.", "rate_limited",
                           {"Retry-After": str(int(wait) + 1)})
        rec = find_rescue(rescue_id)
        if rec is None:
            raise ApiError(404, "No such rescue.", "not_found")
        return StreamingResponse(_rescue_stream(rescue_id, owner=owns(request, rescue_id)),
                                 media_type="text/event-stream", headers=_SSE_HEADERS)

    async def _rescue_stream(rescue_id: str, *, owner: bool = True) -> AsyncIterator[str]:
        rec = svc.ledger.get(rescue_id)
        live = await live_read(rec)
        snap = snapshot(rec, live, owner=owner)
        yield "retry: 3000\n\n"
        yield _sse("snapshot", snap)
        # Key moments keep counts (lock check n, probe pass k of 2, warranty check n of N):
        # replay the snapshot's events into the counter, then continue with the stream.
        moments = KeyMoments(warranty_total=(snap["progress"].get("warranty") or {}).get("total"), owner=owner)
        for event in svc.ledger.events(rescue_id):
            entry = timeline_entry(event)
            if entry is not None and event.id <= snap["cursor"]["event"]:
                moments.add(entry, event.data)
        event_cursor = snap["cursor"]["event"]
        delivery_cursor = snap["cursor"]["delivery"]
        thumb_cursor = max((t["n"] for t in snap["thumbs"]), default=0)
        last_sig = _state_sig(_state_payload(rec, live, owner=owner))
        started = time.monotonic()
        last_read = last_ping = started
        ended_at: float | None = None
        while True:
            await asyncio.sleep(settings.sse_poll_s)
            now = time.monotonic()
            changed = False
            for event in svc.ledger.events(rescue_id, after_id=event_cursor):
                event_cursor = event.id
                changed = True
                entry = timeline_entry(event)
                if entry is not None:
                    if event.kind == "warranty_started":
                        moments.warranty_total = int(event.data.get("checks") or 0) or moments.warranty_total
                    yield _sse("log", {**entry, "moment": moments.add(entry, event.data)}, event_id=event.id)
            for delivery in svc.ledger.deliveries(rescue_id=rescue_id, after_id=delivery_cursor, raw=False):
                delivery_cursor = delivery.id
                changed = True
                entry = webhook_entry(delivery)
                yield _sse("log", {**entry, "moment": moments.add(entry)})
            run = svc.runs.for_rescue(rescue_id)
            if run is not None:
                for item in svc.runs.thumbs.list(run.id, after=thumb_cursor):
                    thumb_cursor = item.n
                    yield _sse("thumb", thumb_view(run.id, item))
            rec = svc.ledger.get(rescue_id)
            if changed or now - last_read >= settings.paypal_poll_s:
                live = await live_read(rec)
                last_read = now
                state = _state_payload(rec, live, owner=owner)
                sig = _state_sig(state)
                if sig != last_sig:
                    last_sig = sig
                    yield _sse("state", state)
            done = rec.state.terminal and (run is None or not run.active)
            if done:
                ended_at = ended_at if ended_at is not None else now
                if _ticks_complete(rec) or now - ended_at >= settings.sse_tail_s:
                    live = await live_read(rec, fresh=True)
                    yield _sse("state", _state_payload(rec, live, owner=owner))
                    yield _sse("end", {"state": rec.state.value})
                    return
            if now - started > settings.sse_max_s:
                return
            if now - last_ping > 15:
                last_ping = now
                yield ": ping\n\n"

    def _state_payload(rec: Any, live: dict[str, Any] | None, *, owner: bool = True) -> dict[str, Any]:
        run = svc.runs.for_rescue(rec.id)
        ticks = ticks_view(rec, svc.ledger.ops(rec.id))
        return {
            "rescue": _record_view(rec),
            "money": money_view(rec, live),
            "paypal": live,
            "ticks": ticks,
            "invariants": svc.ledger.check_invariants(rec.id),
            "run": None if run is None else {"stage": run.stage, "stage_label": run.stage_label,
                                             "detail": run.detail, "active": run.active},
            "progress": progress_view(rec, svc.ledger.events(rec.id), live=live, plan=warranty_plan(),
                                      agent=agent_name, owner=owner),
            "paypal_table": paypal_table(rec, live, ticks, source=paypal_source, owner=owner,
                                         webhooks_verifiable=settings.webhooks_verifiable),
        }

    def _state_sig(state: dict[str, Any]) -> str:
        """What a status page would show differently; PayPal's read time alone is not a change."""
        live = {k: v for k, v in (state.get("paypal") or {}).items() if k != "checked_at"}
        table = {k: v for k, v in (state.get("paypal_table") or {}).items() if k != "checked"}
        return json.dumps({**state, "paypal": live, "paypal_table": table}, sort_keys=True, default=str)

    def _ticks_complete(rec: Any) -> bool:
        if rec.authorization_id and rec.auth_webhook_at is None:
            return False
        if rec.capture_id and rec.capture_webhook_at is None:
            return False
        if rec.refund_id and rec.refund_webhook_at is None:
            return False
        void = svc.ledger.get_op(rec.id, MoneyOp.VOID)
        return not (void is not None and void.status is OutboxStatus.DONE and rec.void_webhook_at is None)

    @app.post("/rescues/{rescue_id}/stop")
    async def stop_rescue(request: Request, rescue_id: str) -> Response:
        session = session_of(request)
        is_form = request.headers.get("content-type", "").startswith(("application/x-www-form-urlencoded", "multipart/"))
        token = request.headers.get(CSRF_HEADER)
        if is_form:
            token = str((await request.form()).get("csrf", ""))
        if not session.csrf_ok(token):
            raise ApiError(403, "This request is missing its security token. Reload the page.", "csrf")
        run = owned_run(session, svc.runs.for_rescue(rescue_id))
        result = await run_in_threadpool(svc.runs.stop, run)
        if is_form:
            return RedirectResponse(f"/rescues/{rescue_id}", status_code=303)
        rec = svc.ledger.get(rescue_id)
        return JSONResponse(
            {
                "rescueId": rescue_id,
                "voided": bool(result and result.voided),
                "state": rec.state.value,
                "detail": result.detail if result else "Stopped.",
            }
        )

    # -- checkout API -------------------------------------------------------------------------

    @app.get("/api/client-token")
    async def client_token(request: Request) -> JSONResponse:
        api_guard(request, csrf=False)
        if settings.offline:
            raise ApiError(404, "Offline mode does not load the PayPal JS SDK.", "offline")
        try:
            token = await run_in_threadpool(svc.gateway.client_token)
        except PayPalError as err:
            raise paypal_error(err) from None
        return JSONResponse({"clientToken": token}, headers={"Cache-Control": "no-store"})

    @app.post("/api/orders")
    async def create_order(request: Request) -> JSONResponse:
        session = api_guard(request)
        try:
            body = await request.json()
        except ValueError:
            raise ApiError(400, "Send JSON with rescueId.", "bad_request") from None
        rescue_id = str((body or {}).get("rescueId", "")) if isinstance(body, dict) else ""
        run = owned_run(session, svc.runs.for_rescue(rescue_id))
        try:
            order = await run_in_threadpool(svc.runs.create_order, run)
        except TryRefused as exc:
            raise refused(exc) from None
        except PayPalError as err:
            raise paypal_error(err) from None
        return JSONResponse(
            {"orderId": order.id, "status": order.status, "intent": order.intent, "amount": f"{order.amount:.2f}",
             "currency": "USD", "rescueId": rescue_id},
            status_code=201,
        )

    @app.post("/api/offline/orders/{order_id}/approve")
    async def offline_approve(request: Request, order_id: str) -> JSONResponse:
        session = api_guard(request)
        if not settings.offline or svc.fake is None:
            raise ApiError(404, "Approval is simulated only in offline mode.", "not_offline")
        rec = svc.ledger.find(order_id=order_id)
        run = owned_run(session, svc.runs.for_rescue(rec.id) if rec else None)
        try:
            await run_in_threadpool(svc.runs.approve_offline, run, order_id)
        except TryRefused as exc:
            raise refused(exc) from None
        except PayPalError as err:
            raise paypal_error(err) from None
        return JSONResponse({"orderId": order_id, "approved": True, "simulated": True})

    @app.post("/api/orders/{order_id}/authorize")
    async def authorize_order(request: Request, order_id: str) -> JSONResponse:
        session = api_guard(request)
        rec = svc.ledger.find(order_id=order_id)
        run = owned_run(session, svc.runs.for_rescue(rec.id) if rec else None)
        try:
            rec = await run_in_threadpool(svc.runs.authorize, run, order_id)
        except TryRefused as exc:
            raise refused(exc) from None
        except PayPalError as err:
            raise paypal_error(err) from None
        return JSONResponse(
            {
                "rescueId": rec.id,
                "state": rec.state.value,
                "authorizationId": rec.authorization_id,
                "authorizationStatus": rec.auth_status,
                "amount": f"{rec.authorized_amount:.2f}" if rec.authorized_amount is not None else None,
                "statusUrl": f"/rescues/{rec.id}",
            }
        )

    # -- PayPal webhooks ----------------------------------------------------------------------

    @app.post("/webhooks/paypal")
    async def paypal_webhook(request: Request) -> Response:
        # PayPal signs every delivery with these headers; anything without them cannot be
        # verified, so it is refused before it is stored.
        if any(not header_value(request.headers, name) for name, _ in HANDLED_WEBHOOK_HEADERS):
            return JSONResponse({"error": "not a PayPal webhook delivery"}, status_code=400)
        wait = svc.limiter.hit("webhook", ip_of(request), limit=settings.webhooks_per_minute, window_s=60.0)
        if wait is not None:
            return JSONResponse({"error": "too many deliveries"}, status_code=429,
                                headers={"Retry-After": str(int(wait) + 1)})
        if await run_in_threadpool(svc.ledger.unverified_backlog) >= settings.max_unverified_deliveries:
            # PayPal retries a delivery that is not answered 2xx, so a real event is not lost.
            return JSONResponse({"error": "intake is busy"}, status_code=503, headers={"Retry-After": "60"})
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > settings.max_webhook_bytes:
            return JSONResponse({"error": "body too large"}, status_code=413)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > settings.max_webhook_bytes:
                return JSONResponse({"error": "body too large"}, status_code=413)
        # Store first (raw body and PayPal headers), answer 200, then verify, dedupe and apply.
        delivery_id = await run_in_threadpool(svc.webhooks.receive, dict(request.headers), bytes(raw))
        return JSONResponse(
            {"received": True, "delivery": delivery_id},
            background=BackgroundTask(_process_delivery, delivery_id),
        )

    def _process_delivery(delivery_id: int) -> None:
        try:
            svc.webhooks.process(delivery_id)
        except Exception:
            log.exception("processing webhook delivery %s failed", delivery_id)

    # -- judge and health ---------------------------------------------------------------------

    def runs_by_rescue() -> dict[str, TryRun]:
        return {r.rescue_id: r for r in svc.runs.runs() if r.rescue_id}

    @app.get("/judge", response_class=HTMLResponse)
    async def judge(request: Request) -> HTMLResponse:
        view = await run_in_threadpool(judge_view, svc.ledger, runs_by_rescue=runs_by_rescue(),
                                       no_hold=svc.runs.no_hold_runs())
        return page(
            request,
            "judge.html",
            {"view": view, "reconcile": svc.reconcile_status, "policy": svc.orchestrator.config.probe_policy.label},
        )

    @app.get("/judge.json")
    async def judge_json() -> JSONResponse:
        view = await run_in_threadpool(judge_view, svc.ledger, runs_by_rescue=runs_by_rescue(),
                                       no_hold=svc.runs.no_hold_runs())
        return JSONResponse(as_json({"mode": settings.mode, "world": settings.world, **view}))

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        body: dict[str, Any] = {
            "ok": True,
            "mode": settings.mode,
            "paypal": "sandbox" if settings.mode == "live" else "offline (in memory)",
            "world": settings.world,
            "judge_mode": settings.judge_mode,
            # The warranty plan in force, so an operator can confirm KEYHOLD_WARRANTY_* took effect.
            "warranty": warranty_plan().short_label,
            "slot": "busy" if svc.runs.slots_full() else "free",
            "slots": {"used": svc.runs.slots_used(), "total": svc.runs.slot_count},
            "reconciler": svc.reconcile_status or None,
            "webhooks": "verifiable" if settings.webhooks_verifiable
            else "not verifiable (PAYPAL_WEBHOOK_ID unset)",
        }
        try:
            body["rescues"] = len(svc.ledger.rescues())
            body["pending_money_ops"] = len(svc.ledger.pending_ops())
            counts = svc.ledger.delivery_counts()
            body["unprocessed_webhooks"] = counts.get("stored", 0)
            body["unverified_webhooks"] = counts.get("unverified", 0)
        except Exception as exc:  # noqa: BLE001
            body.update(ok=False, error=f"ledger: {type(exc).__name__}")
            return JSONResponse(body, status_code=503, headers={"Cache-Control": "no-store"})
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    return app


_SSE_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
