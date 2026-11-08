"""Second review round of the story-first pages: cancel during the boot or the free look, the
cancel that races an authorize, the hourly run limit on /try, the owner's own warranty holding the
server, the Stop path, pages read by someone other than the owner, the PayPal table after the money
is decided, webhook moments in plain words, and the closing rows of a run that ended before a hold."""

from __future__ import annotations

import html
import threading
from decimal import Decimal
from typing import Any

from conftest import GatedWorlds
from fastapi.testclient import TestClient
from test_web_status import wait_for
from test_web_story import CAPTURED, HOLD, PASSED, T0, ev, rec

from keyhold.probe.owner import WarrantyPlan
from keyhold.types import RescueState
from web.config import Settings, SettingsError
from web.security import RateLimiter
from web.story import KeyMoments, paypal_table, progress_view, run_progress, tale_view
from web.worlds import STOP_PATH, SimulatedWorlds


class BootGate:
    """Simulated worlds whose boot waits until the test releases it."""

    kind = "simulated"
    paid_model = False

    def __init__(self) -> None:
        self.inner = SimulatedWorlds()
        self.booting = threading.Event()
        self.release = threading.Event()

    def problems(self) -> list[str]:
        return []

    def boot(self, fault_id: str, run_id: str, progress: Any) -> Any:
        self.booting.set()
        assert self.release.wait(10), "the test never released the boot"
        return self.inner.boot(fault_id, run_id, progress)


# -- cancel ---------------------------------------------------------------------------------------


def test_cancel_during_the_boot_says_it_is_releasing_the_server(make_web) -> None:
    worlds = BootGate()
    web = make_web(worlds=worlds, threaded=True)
    run = web.start()
    assert worlds.booting.wait(5) and run.stage == "booting"
    try:
        response = web.client.post(f"/runs/{run.id}/cancel", data={"csrf": web.csrf()}, follow_redirects=False)
        assert response.headers["location"] == "/try?cancelled=1"
        page = web.client.get("/try?cancelled=1").text
        # The boot still holds the server: the page must not say it was released, nor offer the
        # cancelled run as "Open your run". The busy box says it once; the banner does not repeat it.
        assert "The server was released" not in page and "Open your run" not in page
        assert page.count("Your cancelled run still holds the test server until its boot ends") == 1
        assert "Opens when the cancelled boot ends." in page
        own = web.client.get(f"/runs/{run.id}").text
        assert "Cancelling" in own and "Cancel this run" not in own
        progress = run_progress("booting", cancelling=True)
        assert progress["status"]["head"] == "Cancelling." and progress["outcome"]["label"].endswith("$0")
        assert all(s["state"] != "done" for s in progress["steps"])
    finally:
        worlds.release.set()
    wait_for(lambda: not run.active)
    assert run.stage == "finished" and web.svc.runs.slot_run() is None
    assert "Cancelled by the owner before the free look" in run.detail


def test_cancel_during_the_free_look_is_not_a_no_hold_run(make_web) -> None:
    worlds = GatedWorlds(pause_before=0)  # the free look waits
    web = make_web(worlds=worlds, threaded=True)
    run = web.start("grub-password")
    assert worlds.paused.wait(5) and run.stage == "looking"
    web.client.post(f"/runs/{run.id}/cancel", data={"csrf": web.csrf()}, follow_redirects=False)
    worlds.release.set()
    wait_for(lambda: not run.active)
    assert run.stage == "finished" and "during the free look" in run.detail
    assert web.svc.runs.no_hold_runs() == []  # the ledger does not list it as "No hold offered"
    assert "GRUB locked by a password" not in web.client.get("/judge").text


def test_cancel_decides_under_the_run_lock(web) -> None:
    run = web.start()
    assert run.stage == "quoted"
    out: list[str] = []
    with run.lock:  # an authorize call in flight holds the run's lock
        thread = threading.Thread(target=lambda: out.append(web.svc.runs.cancel(run)))
        thread.start()
        thread.join(0.3)
        assert thread.is_alive() and not out  # the cancel waits for it
        run.stage = "rescuing"  # ... and the hold went live meanwhile
    thread.join(5)
    assert out == ["live"] and not run.stop_requested
    assert web.svc.ledger.get(run.rescue_id).state is RescueState.QUOTED  # nothing was voided


def test_a_failed_free_look_says_so_and_offers_a_new_run() -> None:
    p = run_progress("failed", look_failed=True)
    assert p["status"]["head"] == "The free look did not finish." and p["steps"][0]["note"] == "free look failed"
    assert run_progress("failed")["status"]["head"] == "The run could not start."


# -- /try -----------------------------------------------------------------------------------------


def test_no_runs_left_turns_the_buttons_off_and_says_when(make_web, clock) -> None:
    web = make_web(runs_per_hour=1)
    started = clock.now()
    web.start("grub-password")  # no quote: the server is free again at once
    page = web.client.get("/try").text
    assert "0 of 1 runs left this hour" in page
    assert page.count('data-block="runs"') == 3 and page.count("disabled") >= 3
    assert "Next run <span class=\"free-in\"" in page and "btn btn-primary btn-block" not in page
    assert 'data-runs-left="0"' in page
    assert web.svc.limiter.next_at("start", "testclient", limit=1, window_s=3600.0) == started + 3600.0
    assert web.client.get("/try/status").json()["runsLeft"] == 0


def test_rate_limiter_says_when_the_next_attempt_is_allowed() -> None:
    now = [100.0]
    limiter = RateLimiter(lambda: now[0])
    assert limiter.next_at("start", "a", limit=2, window_s=60.0) is None
    limiter.hit("start", "a", limit=2, window_s=60.0)
    now[0] = 110.0
    limiter.hit("start", "a", limit=2, window_s=60.0)
    assert limiter.next_at("start", "a", limit=2, window_s=60.0) == 160.0  # the oldest hit ages out
    now[0] = 161.0
    assert limiter.next_at("start", "a", limit=2, window_s=60.0) is None
    assert limiter.remaining("start", "a", limit=2, window_s=60.0) == 1  # read only: nothing recorded


def test_your_own_warranty_explains_why_the_server_is_busy(make_web) -> None:
    web = make_web(warranty_checks=6, warranty_interval_s=20.0)
    run = web.start()
    web.hold(run)
    # As on the hosted VM, the run keeps the only test server through its warranty.
    run.stage = "warranty"
    web.svc.runs._slots.append(run.id)
    page = web.client.get("/try").text
    assert "Your rescue's warranty is using the test server (check 6 of 6)" in page
    assert "Free <span class=\"free-in\"" in page and "The buttons turn on by themselves." in page
    assert f'href="/rescues/{run.rescue_id}"' in page and "Watch your warranty" in page
    assert "Open your run" not in page  # one card says it, not two
    end = web.client.get(f"/rescues/{run.rescue_id}").text
    assert "The test server stays with your warranty until about" in end
    stranger = TestClient(web.client.app).get("/try").text
    assert "fixed server is in its warranty: Keyhold checks it a few more times, then releases the test server." in stranger
    assert "VM" not in stranger.split('id="busy-box"')[1].split("</div>")[0]  # simulated: no VM


def test_healthz_names_the_warranty_plan(make_web) -> None:
    web = make_web(warranty_checks=6, warranty_interval_s=20.0)
    assert web.client.get("/healthz").json()["warranty"] == (
        "re-checked every 20 s, 6 times (compressed for the demo; the real plan is 24 hourly checks)"
    )


def test_the_passcode_card_matches_the_mode(make_web) -> None:
    web = make_web(passcode="p")
    page = web.client.get("/try").text
    assert "Runs share one test server, so /try is" in page and "paid model" not in page


# -- the Stop path ------------------------------------------------------------------------------------


def test_the_stop_path_says_press_stop_now_and_offers_it_again_after_a_charge(make_web) -> None:
    worlds = GatedWorlds(pause_before=3)
    web = make_web(worlds=worlds, threaded=True)
    run = web.start(STOP_PATH)
    wait_for(lambda: run.stage == "quoted")
    web.hold(run)
    try:
        assert worlds.paused.wait(10)
        live = web.client.get(f"/rescues/{run.rescue_id}").text
        assert "Path 2: press Stop now." in live and "voids the $19.00 hold" in live
    finally:
        worlds.release.set()
    wait_for(lambda: not run.active)
    end = web.client.get(f"/rescues/{run.rescue_id}").text
    assert "Path 2: press Stop now." not in end
    assert "Try Stop again (fstab)" in end and 'href="/try#fault-fstab-missing-disk"' in end


def test_the_simulated_stop_path_is_paced_so_stop_can_be_pressed() -> None:
    pauses: list[float] = []
    worlds = SimulatedWorlds(stop_path_turn_s=3.0, sleep=pauses.append)
    world = worlds.boot(STOP_PATH, "abcdef123456", lambda text: None)
    ask = {"system": "", "messages": []}
    world.model.respond(**ask)  # the free look: no pause
    world.model.respond(**ask)
    assert pauses == [3.0]
    other = worlds.boot("sshd-config-typo", "abcdef123457", lambda text: None)
    other.model.respond(**ask)
    other.model.respond(**ask)
    assert pauses == [3.0]  # only the Stop path is paced
    assert worlds.stop_window_s() > 30 and SimulatedWorlds().stop_window_s() == 10.0
    assert Settings.from_env({}).sim_stop_path_turn_s == 3.0
    assert Settings.from_env({"KEYHOLD_SIM_STOP_PATH_TURN_S": "0"}).sim_stop_path_turn_s == 0.0
    for bad in ("-1", "soon", "61"):
        try:
            Settings.from_env({"KEYHOLD_SIM_STOP_PATH_TURN_S": bad})
        except SettingsError:
            continue
        raise AssertionError(bad)


def test_the_try_page_says_when_to_press_stop(make_web) -> None:
    web = make_web(worlds=SimulatedWorlds(stop_path_turn_s=3.0))
    page = web.client.get("/try").text
    assert "Approve the hold, then press Stop while the agent works (about 35 s here): you pay $0." in page


# -- someone else's rescue ------------------------------------------------------------------------


def test_a_visitor_reads_the_owner_not_you(web) -> None:
    run = web.start()
    web.hold(run)
    web.svc.pump_offline_webhooks(delay_s=0)
    stranger = html.unescape(TestClient(web.client.app).get(f"/rescues/{run.rescue_id}").text)
    primary = stranger.split('id="next-actions"')[1].split("</a>")[0]
    assert "btn-primary" in primary and "Try a rescue yourself" in primary
    assert "The owner's check, not this screen, decided the charge." in stranger
    assert "Hold: the owner approved" in stranger and "your server answered" not in stranger
    mine = html.unescape(web.client.get(f"/rescues/{run.rescue_id}").text)
    assert "Your check, not this screen, decided the charge." in mine and "Hold: you approved" in mine
    # The live stream follows the viewer too.
    snap = TestClient(web.client.app).get(f"/api/rescues/{run.rescue_id}").json()
    assert "the owner's server answered" in snap["progress"]["status"]["sub"]


def test_the_ledger_speaks_of_the_owner() -> None:
    stop_events = [*HOLD, ev("gate_revoked", 9, "the owner pressed Stop"), ev("stop", 9), ev("state", 9, to="VOIDED")]
    record = rec("VOIDED", end_reason="the owner pressed Stop")
    assert progress_view(record, stop_events, owner=False)["outcome"]["note"] == "the owner pressed Stop"
    assert progress_view(record, stop_events)["outcome"]["note"] == "you pressed Stop"


# -- words on the pages -----------------------------------------------------------------------------


def test_paypal_table_after_the_money_is_decided() -> None:
    voided = rec("VOIDED", end_reason="the owner pressed Stop", order_id="O1", order_status="COMPLETED",
                 auth_status="VOIDED")
    rows = {r["key"]: r for r in paypal_table(voided, None, [], source="FakePayPal")["rows"]}
    capture = rows["capture"]
    assert capture["status_text"] == "none" and capture["pill"] == "none" and capture["absent"]
    assert capture["means"] == "Never charged: a voided hold cannot be captured."
    assert not rows["authorization"]["absent"]

    cancelled = rec("VOIDED", authorization_id=None, end_reason="the owner pressed Stop", order_id="O2",
                    order_status="CREATED")
    rows = {r["key"]: r for r in paypal_table(cancelled, None, [], source="FakePayPal")["rows"]}
    assert rows["order"]["means"] == "Never approved; Keyhold released it. Nothing was held."
    assert rows["authorization"]["status_text"] == "none" and rows["authorization"]["means"] == "Never held."

    active = rec("ACTIVE", order_id="O3", order_status="COMPLETED")
    rows = {r["key"]: r for r in paypal_table(active, None, [], source="FakePayPal", owner=False)["rows"]}
    assert rows["capture"]["status_text"] == "not yet" and "the owner's check" in rows["capture"]["means"]
    assert rows["order"]["means"] == "The owner approved the hold in PayPal."

    ticks = [{"op": "Authorization", "api_at": "2026-11-07T10:00:00Z", "webhook": True, "webhook_at": None,
              "status": "done", "error": None}]
    table = paypal_table(active, None, ticks, source="the PayPal sandbox", webhooks_verifiable=False)
    auth = {r["key"]: r for r in table["rows"]}["authorization"]
    assert auth["webhook_empty"] == "cannot verify (no webhook id)" and not table["webhooks_verifiable"]
    auth = {r["key"]: r for r in paypal_table(active, None, ticks, source="x")["rows"]}["authorization"]
    assert auth["webhook_empty"] == "not yet"


def test_webhook_moments_are_plain_words_with_the_code_in_the_title() -> None:
    keeper = KeyMoments()
    moment = keeper.add({"id": "w1", "kind": "webhook", "at": "x", "event_type": "PAYMENT.AUTHORIZATION.CREATED",
                         "verified": True, "outcome": "applied"})
    assert moment is not None
    assert moment["strong"] == "PayPal confirmed the hold" and moment["text"] == "(signature verified)."
    assert moment["title"] == "PAYMENT.AUTHORIZATION.CREATED" and moment["icon"] == "i-webhook"
    assert "PAYMENT." not in moment["strong"] + moment["text"]
    for code, words in (("PAYMENT.AUTHORIZATION.VOIDED", "PayPal confirmed the release"),
                        ("PAYMENT.CAPTURE.COMPLETED", "PayPal confirmed the charge"),
                        ("PAYMENT.CAPTURE.REFUNDED", "PayPal confirmed the refund")):
        got = keeper.add({"id": code, "kind": "webhook", "at": "x", "event_type": code, "verified": True})
        assert got is not None and got["strong"] == words
    bad = keeper.add({"id": "w9", "kind": "webhook", "at": "x", "event_type": "PAYMENT.CAPTURE.COMPLETED",
                      "verified": False, "outcome": "unverified"})
    assert bad is not None and bad["tone"] == "danger" and "not verified" in bad["text"]


def test_charged_words_while_the_warranty_runs() -> None:
    plan = WarrantyPlan.judge(interval_s=20.0, checks=6)
    events = [*PASSED, ev("warranty_check", 52, ok=True)]
    p = progress_view(rec("CAPTURED", **CAPTURED), events, plan=plan)
    assert p["money"]["unit"] == "captured · warranty running"  # not "final": a refund is still possible
    assert p["outcome"]["label"] == "Charged $9.00"
    assert p["steps"][1]["note"].startswith("approved ") and p["steps"][1]["note"] != "approved"
    done = progress_view(rec("WARRANTY_OK", **CAPTURED), [*PASSED, ev("state", 200, to="WARRANTY_OK")], plan=plan)
    assert done["money"]["unit"] == "captured, final"


def test_one_paypal_check_is_singular() -> None:
    events = [*HOLD[:4], ev("gate_revoked", 9, "the owner pressed Stop"), ev("stop", 9), ev("state", 9, to="VOIDED")]
    p = progress_view(rec("VOIDED", end_reason="the owner pressed Stop"), events)
    assert {"value": "1", "label": "PayPal check"} in p["money"]["stats"]


def test_a_run_that_ended_before_a_hold_gets_a_closing_row() -> None:
    cancelled = rec("VOIDED", authorization_id=None, end_reason="the owner pressed Stop")
    tale = tale_view(cancelled, [ev("quoted", 0), ev("stop", 30), ev("state", 30, to="VOIDED")], title="Typo")
    assert [t["title"] for t in tale] == ["Quote: $9.00, fixed before any key",
                                          "Cancelled: server released, nothing held"]
    assert tale[-1]["tone"] == "voided" and tale[-1]["at"]
    expired = rec("VOIDED", authorization_id=None, end_reason="the quote was not paid within 15 minutes")
    assert tale_view(expired, [ev("quoted", 0)], title="Typo")[-1]["title"] == "Expired: not approved in time"
    stopped = rec("VOIDED", end_reason="the owner pressed Stop")
    words = " ".join(t["text"] for t in tale_view(stopped, [*HOLD, ev("stop", 9)], title="Typo", owner=False))
    assert "The owner pressed Stop" in words and "You pressed Stop" not in words


def test_the_ledger_shows_refunds_as_net_and_ticks_as_counts(web) -> None:
    run = web.start()
    web.hold(run)
    web.svc.pump_offline_webhooks(delay_s=0)
    page = web.client.get("/judge").text
    # Order, authorize and capture answered; authorize and capture confirmed by webhook (the
    # order has none), as counts instead of a row of tick marks, and in words for screen readers.
    assert "API 3/3 · Webhook 2/2" in page and "PayPal API answers 3 of 3, verified webhooks 2 of 2" in page
    # No money held now: the "Held now" tile is not amber; nothing refunded: not purple.
    assert 'class="tile held"' not in page and 'class="tile refunded"' not in page
    assert 'class="tile charged"' in page
    assert "console_locked" not in page.split('id="no-hold"')[1].split("</section>")[0].replace('title="', "")


def test_the_checkout_bar_goes_to_the_button_and_never_approves(web) -> None:
    run = web.start()
    page = web.client.get(f"/runs/{run.id}").text
    assert 'href="#paypal-area" data-hidden="1">' in page and "Go to approve the $9.00 hold" in page
    assert "What this button does" in page  # the simulated steps fold under the button
    assert page.index('id="offline-approve"') < page.index("What this button does")


def test_the_run_page_times_the_free_look_on_its_own(make_web, clock) -> None:
    worlds = GatedWorlds(pause_before=0)
    web = make_web(worlds=worlds, threaded=True)
    run = web.start()
    assert worlds.paused.wait(5)
    try:
        assert run.look_started_at is not None and run.stage == "looking"
        clock.advance(7)
        page = web.client.get(f"/runs/{run.id}").text
        assert "Free look: reading the screen" in page and "Taking the first screenshot" in page
        assert "Booting" not in page and 'data-since="7"' in page
        assert "Looking at the screen<" in page  # no full stop before " · keyboard locked"
    finally:
        worlds.release.set()
    wait_for(lambda: run.stage == "quoted")


def test_sse_and_json_follow_the_viewer(web) -> None:
    run = web.start()
    web.hold(run)
    other = TestClient(web.client.app)
    words = other.get(f"/api/rescues/{run.rescue_id}").json()["tale"][1]["title"]
    assert words == f"Hold: the owner approved, PayPal held ${Decimal('9.00')}"
    assert T0  # (shared fixtures imported for the story tests)



class RelapseWorlds:
    """Simulated worlds whose fixed server falls over again once the rescue is charged, so the
    warranty refunds it."""

    kind = "simulated"
    paid_model = False

    def __init__(self) -> None:
        self.inner = SimulatedWorlds()
        self.ledger: Any = None

    def problems(self) -> list[str]:
        return []

    def boot(self, fault_id: str, run_id: str, progress: Any) -> Any:
        world = self.inner.boot(fault_id, run_id, progress)
        ssh, outer = world.ssh, self

        class Relapsing:
            def login(self, spec: Any) -> Any:
                if outer.ledger is not None and any(r.state is RescueState.CAPTURED for r in outer.ledger.rescues()):
                    ssh._server.break_down()
                return ssh.login(spec)

        world.ssh = Relapsing()
        return world


def test_the_ledger_shows_a_refunded_rescue_as_net_zero(make_web) -> None:
    worlds = RelapseWorlds()
    web = make_web(worlds=worlds, warranty_checks=6, warranty_interval_s=20.0)
    worlds.ledger = web.svc.ledger
    run = web.start()
    web.hold(run)
    assert web.svc.ledger.get(run.rescue_id).state is RescueState.REFUNDED
    page = web.client.get("/judge").text
    amount = page.split('class="c-amt amt">')[1].split("</td>")[0]
    assert amount.startswith("$0.00 net") and "refunded $9.00" in amount  # not "$9.00" as if charged
    assert 'class="tile refunded"' in page and 'class="tile charged"' in page
