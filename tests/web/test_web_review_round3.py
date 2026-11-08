"""Third review round of the story-first pages: console crops that never cut a line mid-word without
saying so, the PayPal table after a capture, colours and icons that keep their one meaning, /try
that remembers the paths this browser has done, waits counted down in words, a cancel after a real
hold, the run page's fallback when its stream closes, and the names, roles and titles a screen
reader gets."""

from __future__ import annotations

import html
import re
from pathlib import Path

from conftest import GatedWorlds
from fastapi.testclient import TestClient
from test_web_status import wait_for
from test_web_story import CAPTURED, HOLD, PASSED, ev, rec

from keyhold.types import RescueState
from web.story import KeyMoments, paypal_table, progress_view, status_title, tale_view
from web.views import thumb_crop
from web.worlds import STOP_PATH

STATIC = Path(__file__).resolve().parents[2] / "web" / "static"


# -- the console crop -----------------------------------------------------------------------------


def test_a_live_crop_keeps_every_line_whole() -> None:
    # A line runs to x 1000 of a 1024 px screen: the desktop crop reaches it, so nothing is cut.
    crop = thumb_crop(1024, 768, (0, 0, 1000, 300))
    assert crop is not None and crop["w"] == 1000 and crop["dcut"] == 0
    # Short lines: still at least three quarters of the width, so a live console does not jump.
    assert thumb_crop(1024, 768, (0, 0, 300, 300))["w"] == 768
    # The phone's narrower band cuts that line, and says so (the page fades the edge).
    assert crop["pw"] == round(1024 * 0.45) and crop["cut"] == 1


def test_a_finished_console_crop_hugs_its_text() -> None:
    # Two lines of GRUB: a 20:3 band instead of 10:3, so the lines are not lost in black.
    grub = thumb_crop(1024, 768, (0, 0, 540, 60), tight=True)
    assert grub is not None and grub["w"] == 540 and grub["h"] == round(540 * 0.15) and grub["dcut"] == 0
    # A long last screen: at most 70% of the width, so the text stays readable in a side column;
    # the cut is flagged for the fade.
    long = thumb_crop(1024, 768, (0, 0, 1000, 300), tight=True)
    assert long is not None and long["w"] == round(1024 * 0.7) and long["dcut"] == 1


def test_the_page_marks_a_cut_edge_and_offers_the_full_screen(web) -> None:
    run = web.start()  # the quote: a finished free look, cropped tight
    page = web.client.get(f"/runs/{run.id}").text
    assert 'id="step-full"' in page and "Full screen" in page  # in the title bar, even without a stepper
    assert 'data-cut="1"' in page  # the evidence line runs past the phone's band
    css = (STATIC / "app.css").read_text()
    assert ".shot.crop[data-cut]" in css and ".shot.crop[data-dcut]" in css and "mask-image" in css


# -- the PayPal table -----------------------------------------------------------------------------


def test_the_authorization_row_shows_when_the_capture_answered() -> None:
    captured = rec("CAPTURED", order_id="O1", order_status="COMPLETED", auth_status="CAPTURED", **CAPTURED)
    ticks = [
        {"op": "Authorization", "api_at": "2026-11-07T10:00:00Z", "webhook": True, "webhook_at": None,
         "status": "done", "error": None},
        {"op": "Capture", "api_at": "2026-11-07T10:00:40Z", "webhook": True, "webhook_at": None,
         "status": "done", "error": None},
    ]
    rows = {r["key"]: r for r in paypal_table(captured, None, ticks, source="FakePayPal")["rows"]}
    assert rows["authorization"]["api"] == [{"at": "10:00:00", "label": ""}, {"at": "10:00:40", "label": "capture"}]
    assert rows["capture"]["api"] == [{"at": "10:00:40", "label": ""}]  # its own call carries no label


def test_final_capture_reads_as_a_word(web) -> None:
    run = web.start()
    web.hold(run)
    page = web.client.get(f"/rescues/{run.rescue_id}").text
    assert '<span class="final" title="final_capture=true' in page and ">final</span>" in page
    assert ">final_capture<" not in page
    assert '"final_capture"' not in (STATIC / "live.js").read_text().split("var fin")[1].split(";")[0]


# -- colours, icons and words keep one meaning ----------------------------------------------------


def test_a_released_hold_has_the_void_icon_and_no_hold_has_its_own() -> None:
    stop = [*HOLD, ev("gate_revoked", 9, "the owner pressed Stop"), ev("stop", 9), ev("state", 9, to="VOIDED")]
    stopped = progress_view(rec("VOIDED", end_reason="the owner pressed Stop"), stop)
    assert stopped["money"]["icon"] == "i-voided"
    cancelled = progress_view(rec("VOIDED", authorization_id=None, end_reason="the owner pressed Stop"),
                              [ev("quoted", 0), ev("stop", 9), ev("state", 9, to="VOIDED")])
    assert cancelled["money"]["icon"] == "i-nohold"


def test_the_phone_line_says_done_only_when_nothing_can_change() -> None:
    running = progress_view(rec("CAPTURED", **CAPTURED), PASSED)
    assert running["line"]["lead"] == "Charged" and running["line"]["text"].startswith("warranty")
    final = progress_view(rec("WARRANTY_OK", **CAPTURED), [*PASSED, ev("state", 200, to="WARRANTY_OK")])
    assert final["line"]["lead"] == "Done"


def test_a_lent_keyboard_is_now_in_key_moments() -> None:
    moment = KeyMoments().add({"id": "1", "kind": "gate_check", "at": "x", "text": "keyboard allowed"},
                              {"granted": True, "amount": "9.00"})
    assert moment is not None and moment["tone"] == "now" and moment["strong"] == "Keyboard lent."
    assert ".mo-ic.now" in (STATIC / "app.css").read_text()


def test_lock_evidence_and_stats_name_paypal_checks() -> None:
    p = progress_view(rec("ACTIVE"), HOLD)
    assert p["lock"]["evidence"].endswith("· 2 PayPal checks")
    charged = progress_view(rec("CAPTURED", **CAPTURED), PASSED)
    assert any(s["label"] == "from hold to charge" for s in charged["money"]["stats"])


def test_the_warranty_row_has_a_time_before_the_first_check() -> None:
    tale = tale_view(rec("CAPTURED", **CAPTURED), PASSED, title="Typo")
    assert tale[-1]["title"].startswith("Warranty: 0 of 6") and tale[-1]["at"]
    assert progress_view(rec("CAPTURED", **CAPTURED), PASSED)["warranty"]["last_at"]


# -- titles, focus and the status line ------------------------------------------------------------


def test_the_page_title_carries_the_status() -> None:
    assert status_title({"head": "Fixed. Charged $9.00."}) == "Fixed, charged $9.00"
    assert status_title({"head": "Quote ready. Approve the hold to lend the keyboard."}) == "Quote ready"
    assert status_title({"head": "No hold offered."}) == "No hold offered"


def test_status_lines_carry_no_raw_state_code(web) -> None:
    run = web.start()
    page = web.client.get(f"/runs/{run.id}").text
    assert "<title>Quote ready · Typo in sshd_config · Keyhold</title>" in page
    status = page.split('id="status"')[1].split(">")[0]
    assert "title=" not in status and 'tabindex="-1"' in status
    assert "status.title" not in (STATIC / "common.js").read_text()


# -- /try ------------------------------------------------------------------------------------------


def test_the_judge_link_keeps_start_here_on_the_first_screen(make_web) -> None:
    from web.security import judge_token

    web = make_web(passcode="open-sesame")
    web.client.get(f"/j/{judge_token(web.svc.settings.secret_key, 'open-sesame')}")
    page = web.client.get("/try?unlocked=1").text
    # No notice box above the cards: one word on the capacity line, the sentence for screen readers.
    assert 'class="alert alert-ok"' not in page and '<strong class="unl">Unlocked</strong>' in page
    assert '<p class="sr-only" role="status">Unlocked for this browser. Start with the card marked Start here.</p>' in page


def test_try_remembers_the_paths_this_browser_has_done(make_web) -> None:
    worlds = GatedWorlds(pause_before=3)
    web = make_web(worlds=worlds, threaded=True)
    first = web.start()
    wait_for(lambda: first.stage == "quoted")
    page = web.client.get("/try").text
    assert page.count('<span class="ribbon">Start here</span>') == 1
    web.hold(first)
    worlds.release.set()
    wait_for(lambda: first.stage in ("warranty", "finished"))
    page = html.unescape(web.client.get("/try").text)
    sshd = page.split('id="fault-sshd-config-typo"')[1].split("</article>")[0]
    fstab = page.split('id="fault-fstab-missing-disk"')[1].split("</article>")[0]
    assert "Done · Charged $9.00" in sshd and "Run it again" in sshd and "btn-primary" not in sshd
    assert '<span class="ribbon">Next</span>' in fstab and "btn btn-primary" in fstab
    # A visitor in another browser starts at the start.
    assert '<span class="ribbon">Start here</span>' in TestClient(web.client.app).get("/try").text
    # Path 2 pressed Stop, path 3 offered no hold: every path is done, and the ledger is the next step.
    worlds.release.clear()
    worlds.paused.clear()
    second = web.start(STOP_PATH)
    wait_for(lambda: second.stage == "quoted")
    web.hold(second)
    try:
        assert worlds.paused.wait(10)
        stop = web.client.post(f"/rescues/{second.rescue_id}/stop", headers=web.headers())
        assert stop.status_code == 200 and stop.json()["voided"]
    finally:
        worlds.release.set()
    wait_for(lambda: not second.active)
    web.svc.runs.worlds = worlds.inner  # GRUB has no agent turn to pause at
    third = web.start("grub-password")
    wait_for(lambda: third.stage == "no_quote")
    page = html.unescape(web.client.get("/try").text)
    assert "All three paths done." in page and '<span class="ribbon">Next</span>' not in page
    assert page.count("btn btn-primary") == 1 and 'class="btn btn-primary" href="/judge"' in page
    assert "Done · Stopped, $0" in page and "Done · No hold" in page


def test_the_three_break_buttons_have_their_own_names(web) -> None:
    page = web.client.get("/try").text
    for title in ("Typo in sshd_config", "fstab lists a disk that is gone", "GRUB locked by a password"):
        assert f'<span class="sr-only">: {title}</span>' in page
        assert f'<span class="sr-only"> before breaking {title}</span>' in page
    assert '<div class="faults" role="group" aria-label="Pick a fault">' in page


def test_the_marked_card_steps_back_while_its_button_is_off(make_web) -> None:
    web = make_web(warranty_checks=6, warranty_interval_s=20.0)
    run = web.start()
    web.hold(run)
    run.stage = "warranty"
    web.svc.runs._slots.append(run.id)
    page = web.client.get("/try").text
    assert 'class="card fault start blocked"' in page
    # The wait is counted down in words, with the clock time once (shown in the visitor's own time).
    assert page.count('class="free-in"') == 4 and page.count('class="clock"') == 1
    assert "Opens <span class=\"free-in\"" in page
    assert "pageshow" in (STATIC / "try.js").read_text()


def test_the_run_limit_box_says_when_once_and_never_promises_the_buttons(make_web, clock) -> None:
    web = make_web(runs_per_hour=1)
    web.start("grub-password")
    page = web.client.get("/try").text
    assert page.count('class="clock"') == 1 and "next at about" not in page
    assert "turn on by themselves" not in page and "come back on their own" not in page


def test_a_cancel_during_the_boot_says_it_once(make_web) -> None:
    from test_web_review_round2 import BootGate

    worlds = BootGate()
    web = make_web(worlds=worlds, threaded=True)
    run = web.start()
    assert worlds.booting.wait(5)
    try:
        web.client.post(f"/runs/{run.id}/cancel", data={"csrf": web.csrf()}, follow_redirects=False)
        page = web.client.get("/try?cancelled=1").text
        banner = page.split('<div class="alert" role="status">')[1].split("</div>")[0]
        assert "Run cancelled. Nothing was held." in banner and "boot" not in banner
        assert "Your run is using the test server" not in page
        assert 'class="try-hero after-cancel"' in page
    finally:
        worlds.release.set()
    wait_for(lambda: not run.active)


def test_locked_try_still_leads_to_the_finished_runs(make_web) -> None:
    web = make_web(passcode="p")
    page = web.client.get("/try").text
    assert 'No passcode? Every earlier run is in the <a href="/judge">ledger</a>' in page


def test_try_and_home_give_the_time_of_this_mode(web) -> None:
    page = web.client.get("/try").text
    assert "Try a rescue · about 2 minutes" in page and "~1 min" in page and "~30 s" in page and "~10 s" in page
    assert "Try a rescue (about 2 minutes)" in web.client.get("/").text
    assert "Keyhold offers no hold here, by design." in page


# -- the rescue page -------------------------------------------------------------------------------


def test_path_one_says_just_watch_inside_the_money_box(make_web) -> None:
    worlds = GatedWorlds(pause_before=3)
    web = make_web(worlds=worlds, threaded=True)
    run = web.start()
    wait_for(lambda: run.stage == "quoted")
    web.hold(run)
    try:
        assert worlds.paused.wait(10)
        page = web.client.get(f"/rescues/{run.rescue_id}").text
        assert '<p class="fine" id="stop-help" data-path="1"><strong>Path 1: just watch.</strong>' in page
        assert "callout-now" not in page and 'id="stop-now"' not in page
        assert '<p class="m-warn" id="stop-error" role="alert" hidden></p>' in page
        assert "<title>Agent at the keyboard · Typo in sshd_config · Keyhold</title>" in page
    finally:
        worlds.release.set()
    wait_for(lambda: not run.active)


def test_the_next_path_says_when_the_server_frees(make_web) -> None:
    web = make_web(warranty_checks=6, warranty_interval_s=20.0)
    run = web.start()
    web.hold(run)
    run.stage = "warranty"
    web.svc.runs._slots.append(run.id)
    mine = html.unescape(web.client.get(f"/rescues/{run.rescue_id}").text)
    primary = mine.split('id="next-actions"')[1].split("</a>")[0]
    assert "Try the Stop path (fstab, $19)" in primary and '<span class="opens"> · opens' in primary
    assert "the next path starts then" in mine
    stranger = html.unescape(TestClient(web.client.app).get(f"/rescues/{run.rescue_id}").text)
    assert "you can start a run then" in stranger and "the next path starts then" not in stranger


def test_the_end_page_shows_the_last_typed_line(web) -> None:
    run = web.start()
    web.hold(run)
    page = web.client.get(f"/rescues/{run.rescue_id}").text
    aside = page.split('id="last-h"')[1]
    assert "The agent's last screen" in aside.split("</h2>")[0] and 'id="typed-text"' in aside


def test_a_cancel_after_paypal_answered_goes_to_the_live_page(web) -> None:
    run = web.start()
    web.order(run)
    # PayPal answered the authorization (PENDING, say) while the run still sat at the checkout.
    web.svc.ledger._conn.execute("UPDATE rescues SET authorization_id='AUTHX' WHERE id=?", (run.rescue_id,))
    response = web.client.post(f"/runs/{run.id}/cancel", data={"csrf": web.csrf()}, follow_redirects=False)
    assert response.headers["location"] == f"/rescues/{run.rescue_id}"
    assert not run.stop_requested  # the hold is the live page's to Stop, which says what happened to it
    page = web.client.get(f"/rescues/{run.rescue_id}").text
    assert 'id="cancel-form"' not in page and 'id="stop-form"' in page


# -- the run page, the ledger, the checkout ------------------------------------------------------


def test_the_run_page_has_a_status_api_for_its_fallback(web) -> None:
    run = web.start()
    body = web.client.get(f"/api/runs/{run.id}").json()
    assert body["stage"] == "quoted" and body["rescueId"] == run.rescue_id and body["progress"]["steps"]
    assert TestClient(web.client.app).get(f"/api/runs/{run.id}").status_code == 404
    js = (STATIC / "run.js").read_text()
    assert '"/api/runs/"' in js and "Updates paused" in js and "Reload" in js


def test_the_boot_timer_is_in_the_card_only(make_web) -> None:
    from test_web_review_round2 import BootGate

    worlds = BootGate()
    web = make_web(worlds=worlds, threaded=True)
    run = web.start()
    assert worlds.booting.wait(5)
    try:
        page = web.client.get(f"/runs/{run.id}").text
        assert "Starting the server…" in page and page.count('id="elapsed"') == 1
        assert "Booting 0:" not in page
        web.client.post(f"/runs/{run.id}/cancel", data={"csrf": web.csrf()}, follow_redirects=False)
    finally:
        worlds.release.set()
    wait_for(lambda: not run.active)


def test_the_no_hold_page_describes_its_screen_and_links_to_its_row(web) -> None:
    run = web.start("grub-password")
    page = html.unescape(web.client.get(f"/runs/{run.id}").text)
    assert "GRUB stops at “Enter username:”, then asks for a password. The console gives neither." in page
    assert 'href="/judge#no-hold">See it in the ledger' in page


def test_the_ledger_names_its_tables_and_sums_up(web) -> None:
    run = web.start()
    web.hold(run)
    web.start("grub-password")
    page = web.client.get("/judge").text
    assert "Runs with a hold" in page and "All rescues" not in page
    assert re.search(r'id="ledger-sum">2 runs: 1 charged, 1 no hold\.', page)
    assert '<table class="ledger" role="table">' in page and 'role="cell"' in page
    assert '<span class="card-only">Checks </span>ok' in page


def test_buttons_that_wait_keep_keyboard_focus() -> None:
    live = (STATIC / "live.js").read_text()
    checkout = (STATIC / "checkout.js").read_text()
    assert "stopButton.disabled = true" not in live and 'setAttribute("aria-disabled", "true")' in live
    assert "stopButton.focus()" in live
    assert "offlineButton.disabled = true" not in checkout and "offlineButton.focus()" in checkout


def test_the_buyer_login_is_labelled_and_announced(make_web, clock) -> None:
    from keyhold.pay.client import FakePayPal

    web = make_web(mode="live", paypal_client_id="i", paypal_client_secret="s", passcode="pc",
                   judge_buyer_email="buyer@example.com", judge_buyer_password="example-password",
                   gateway=FakePayPal(clock=clock))
    web.client.post("/try/unlock", data={"csrf": web.csrf(), "passcode": "pc"})
    run = web.start()
    page = web.client.get(f"/runs/{run.id}").text
    assert '<span class="copy-label">Copy email</span>' in page and '<span class="copy-label">Copy password</span>' in page
    assert 'id="paypal-loading" role="status"' in page
    assert "copy it by hand" in (STATIC / "common.js").read_text()
    assert RescueState.QUOTED.value in web.client.get(f"/api/rescues/{run.rescue_id}").json()["rescue"]["state"]
