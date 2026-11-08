"""The story-first pages: the progress strip in every state, plain words with the raw codes kept
in the page, Cancel before a hold, the no-hold list, the busy server, one-press Stop, the PayPal
table, key moments and the cropped console."""

from __future__ import annotations

import io
import re
from decimal import Decimal
from types import SimpleNamespace

import pytest
from conftest import GatedWorlds
from fastapi.testclient import TestClient
from test_web_status import read_stream, sse_events, wait_for

from keyhold.probe.owner import WarrantyPlan
from keyhold.types import RescueState
from web.runs import ThumbStore
from web.story import KeyMoments, hms, progress_view, run_progress, tale_view, visible_moments
from web.views import thumb_crop

T0 = 1_794_000_000.0  # 2026-11-07, any fixed time


def rec(state: str, **fields: object) -> SimpleNamespace:
    base = {
        "state": RescueState(state),
        "quote_amount": Decimal("9.00"),
        "authorized_amount": None,
        "captured_amount": None,
        "refunded_amount": None,
        "capture_status": None,
        "auth_status": None,
        "authorization_id": None,
        "capture_id": None,
        "refund_id": None,
        "refund_status": None,
        "order_id": None,
        "order_status": None,
        "auth_expiration_time": None,
        "end_reason": None,
        "created_at": T0,
    }
    if state not in ("QUOTED", "HOLD_PENDING") and fields.get("authorization_id", "AUTH1") is not None:
        base.update(authorization_id="AUTH1", authorized_amount=Decimal("9.00"), auth_status="CREATED")
    base.update(fields)
    return SimpleNamespace(**base)


def ev(kind: str, when: float, detail: str = "", **data: object) -> SimpleNamespace:
    return SimpleNamespace(kind=kind, at=T0 + when, detail=detail, data=data, id=int(when * 10))


HOLD = [ev("quoted", 0), ev("state", 1, to="HOLD_PENDING"), ev("state", 2, to="HELD"),
        ev("gate_check", 3, granted=True, status="CREATED", amount="9.00"), ev("state", 3, to="ACTIVE"),
        ev("gate_check", 8, granted=True, status="CREATED", amount="9.00")]
DONE = [*HOLD, ev("gate_revoked", 20, "the agent session ended (done)"),
        ev("agent_result", 20, "done: fixed it", turns=7, outcome="done"), ev("state", 20, to="PROBING")]
PASSED = [*DONE, ev("probe", 21, at=T0 + 21, ok=True), ev("probe", 31, at=T0 + 31, ok=True),
          ev("probe_stable", 31, passes=[T0 + 21, T0 + 31]), ev("state", 32, to="CAPTURED"),
          ev("warranty_started", 32, started_at=T0 + 32, interval_s=20.0, checks=6, judge_mode=True,
             failures_to_refund=2)]
CAPTURED = {"captured_amount": Decimal("9.00"), "capture_status": "COMPLETED", "capture_id": "CAP1"}


@pytest.mark.parametrize(
    ("record", "events", "steps", "outcome", "lock"),
    [
        (rec("QUOTED"), [ev("quoted", 0)], ["done", "current", "upcoming", "upcoming"], "pending", "locked"),
        (rec("HOLD_PENDING"), [ev("quoted", 0)], ["done", "current", "upcoming", "upcoming"], "pending", "locked"),
        (rec("HELD"), HOLD[:4], ["done", "done", "current", "upcoming"], "pending", "lent"),
        (rec("ACTIVE"), HOLD, ["done", "done", "current", "upcoming"], "pending", "lent"),
        (rec("PROBING"), [*DONE, ev("probe", 21, at=T0 + 21, ok=True)], ["done", "done", "done", "current"],
         "pending", "locked"),
        (rec("CAPTURED", **CAPTURED), [*PASSED, ev("warranty_check", 52, ok=True), ev("warranty_check", 72, ok=True)],
         ["done", "done", "done", "done"], "charged", "locked"),
        (rec("CAPTURED", captured_amount=Decimal("9.00"), capture_status="PENDING", capture_id="CAP1"), PASSED,
         ["done", "done", "done", "done"], "capture_pending", "locked"),
        (rec("WARRANTY_OK", **CAPTURED), [*PASSED, ev("state", 200, to="WARRANTY_OK")],
         ["done", "done", "done", "done"], "charged", "locked"),
        (rec("REFUNDED", **CAPTURED, refunded_amount=Decimal("9.00"), refund_id="R1"),
         [*PASSED, ev("state", 200, to="REFUNDED")], ["done", "done", "done", "done"], "refunded", "locked"),
        (rec("VOIDED", end_reason="the owner pressed Stop"),
         [*HOLD, ev("gate_revoked", 9, "the owner pressed Stop"), ev("stop", 9), ev("state", 9, to="VOIDED")],
         ["done", "done", "done", "skipped"], "voided", "locked"),
        (rec("VOIDED", end_reason="the probe never passed twice"),
         [*DONE, ev("probe", 21, at=T0 + 21, ok=False), ev("state", 330, to="VOIDED")],
         ["done", "done", "done", "failed"], "voided", "locked"),
        (rec("VOIDED", authorization_id=None, end_reason="the owner pressed Stop"), [ev("quoted", 0)],
         ["done", "skipped", "skipped", "skipped"], "cancelled", "locked"),
        (rec("VOIDED", authorization_id=None, end_reason="the quote was not paid within 15 minutes"),
         [ev("quoted", 0)], ["done", "skipped", "skipped", "skipped"], "expired", "locked"),
        (rec("CAPTURED", captured_amount=Decimal("9.00"), capture_status="DECLINED", capture_id="CAP1"), PASSED,
         ["done", "done", "done", "done"], "declined", "locked"),
    ],
    ids=["quoted", "hold-pending", "held", "active", "probing", "captured-warranty", "capture-pending",
         "warranty-ok", "refunded", "stopped", "probe-never-passed", "cancelled", "expired", "declined"],
)
def test_progress_strip_for_every_state(record, events, steps, outcome, lock) -> None:
    p = progress_view(record, events, plan=WarrantyPlan.judge(interval_s=20.0, checks=6))
    assert [s["state"] for s in p["steps"]] == steps
    assert [s["key"] for s in p["steps"]] == ["quote", "hold", "keyboard", "probe"]
    assert p["outcome"]["state"] == outcome
    assert p["lock"]["state"] == lock
    held_now = record.state in (RescueState.HELD, RescueState.ACTIVE, RescueState.PROBING) or outcome == "capture_pending"
    # Amber only while money is held: the Hold step is "held" exactly then.
    assert (p["steps"][1]["tone"] == "held") == held_now
    # No raw state code in the sentence a judge reads; it stays in data-state.
    assert record.state.value not in p["status"]["text"] and p["state"] == record.state.value
    assert p["line"]["lead"] and p["line"]["text"]


def test_progress_words_for_the_judge_path() -> None:
    plan = WarrantyPlan.judge(interval_s=20.0, checks=6)
    active = progress_view(rec("ACTIVE"), HOLD, plan=plan)
    assert active["status"]["text"] == "Agent at the keyboard · hold live"
    assert active["lock"]["evidence"] == f"PayPal says CREATED · last checked {hms(T0 + 8)} UTC · 2 PayPal checks"
    assert active["money"]["label"] == "Held" and active["money"]["tone"] == "held"

    probing = progress_view(rec("PROBING"), [*DONE, ev("probe", 21, at=T0 + 21, ok=True)], plan=plan)
    assert probing["steps"][3]["note"] == "pass 1 of 2"

    events = [*PASSED, ev("warranty_check", 52, ok=True), ev("warranty_check", 72, ok=True)]
    captured = progress_view(rec("CAPTURED", **CAPTURED), events, plan=plan)
    assert captured["outcome"]["note"] == "warranty 2 of 6"
    assert captured["status"]["head"] == "Fixed. Charged $9.00."
    assert "Warranty check 2 of 6 passed; you can leave." in captured["status"]["sub"]
    assert captured["money"]["tone"] == "charged" and captured["steps"][1]["tone"] == ""

    stop_events = [*HOLD, ev("gate_revoked", 9, "the owner pressed Stop"), ev("stop", 9), ev("state", 9, to="VOIDED")]
    stopped = progress_view(rec("VOIDED", end_reason="the owner pressed Stop"), stop_events, plan=plan)
    assert stopped["status"]["head"] == "Stopped. $0 charged."
    assert "Nothing was typed after Stop." in stopped["status"]["sub"]
    late = progress_view(rec("VOIDED", end_reason="the owner pressed Stop"), [*stop_events, ev("agent_type", 10, text="x")])
    assert "Nothing was typed after Stop." not in late["status"]["sub"]
    # the hosted run of 2026-11-08 20:11: Stop at 9, a line already being typed finished at 14, and
    # the "stop" event was written after it. The page must not claim nothing was typed.
    inflight = [*HOLD, ev("gate_revoked", 9, "the owner pressed Stop"), ev("agent_type", 14, text="cp a b\n"),
                ev("stop", 14.1), ev("state", 15, to="VOIDED")]
    real = progress_view(rec("VOIDED", end_reason="the owner pressed Stop"), inflight)
    assert "Nothing was typed after Stop." not in real["status"]["sub"]
    assert "finished after it" in real["status"]["sub"]
    assert {"value": "0", "label": "keys after Stop"} not in real["money"]["stats"]
    assert {"value": "0", "label": "keys after Stop"} in stopped["money"]["stats"]
    # Stop pressed while the probe ran: the keyboard was already locked (agent done) and the page
    # gives the time Stop was pressed, not the earlier lock
    probing_stop = [*DONE, ev("probe", 21, at=T0 + 21, ok=True), ev("stop", 40), ev("state", 41, to="VOIDED")]
    tale = tale_view(rec("VOIDED", end_reason="the owner pressed Stop"), probing_stop, title="Typo")
    row = next(r for r in tale if r["title"] == "Keyboard: lent, then taken back")
    assert f"pressed Stop at {hms(T0 + 40)}" in row["text"] and hms(T0 + 20) not in row["text"]
    # the fixed gate: Stop cuts the line being typed and its Enter never goes out
    cut = [*HOLD, ev("gate_revoked", 9, "the owner pressed Stop"),
           ev("agent_type_cut", 9.2, "cut", sent=8, total=40, enter_withheld=True), ev("stop", 9.3), ev("state", 10, to="VOIDED")]
    cutv = progress_view(rec("VOIDED", end_reason="the owner pressed Stop"), cut)
    assert "its Enter key was never sent, so it did not run." in cutv["status"]["sub"]
    assert "Nothing was typed after Stop." not in cutv["status"]["sub"]
    assert {"value": "0", "label": "commands run after Stop"} in cutv["money"]["stats"]

    # Cancel before a hold never says "VOIDED" or "pressed Stop", and never ticks Hold.
    cancelled = progress_view(rec("VOIDED", authorization_id=None, end_reason="the owner pressed Stop"), [ev("quoted", 0)])
    words = " ".join([cancelled["status"]["text"], cancelled["status"]["sub"], cancelled["outcome"]["label"]])
    assert "Cancelled" in words and "VOIDED" not in words and "pressed Stop" not in words
    assert cancelled["steps"][1]["state"] == "skipped"


def test_runs_without_a_rescue_never_tick_the_hold() -> None:
    for stage in ("no_quote", "failed", "finished"):
        p = run_progress(stage, no_quote_reason="the fix needs a password", honest_stop=True)
        assert all(s["state"] != "done" for s in p["steps"][1:]), stage
        assert p["outcome"]["label"].endswith("$0"), stage
    looking = run_progress("looking")
    assert [s["state"] for s in looking["steps"]] == ["current", "upcoming", "upcoming", "upcoming"]
    assert looking["lock"]["title"] == "Keyboard locked: screenshots only"
    no_hold = run_progress("no_quote", honest_stop=True)
    assert no_hold["status"]["head"] == "No hold offered." and "$0, no PayPal call" in no_hold["status"]["sub"]


def test_key_moments_count_and_collapse() -> None:
    keeper = KeyMoments(warranty_total=6)
    rows = [
        ({"id": 1, "kind": "gate_check", "at": "x", "text": "a: keyboard allowed"}, {"granted": True, "amount": "9.00"}),
        ({"id": 2, "kind": "gate_check", "at": "x", "text": "a: keyboard allowed"}, {"granted": True}),
        *(({"id": 10 + i, "kind": "agent_type", "at": "x", "text": f"typed: cmd{i} ↵"}, {}) for i in range(5)),
        ({"id": 20, "kind": "probe", "at": "x", "text": "pass: SSH ok, page ok. ok"}, {}),
        ({"id": 21, "kind": "probe", "at": "x", "text": "pass: SSH ok, page ok. ok"}, {}),
        ({"id": 22, "kind": "probe_stable", "at": "x", "text": "stable"}, {}),
        ({"id": 23, "kind": "warranty_check", "at": "x", "text": "pass: SSH ok, page ok."}, {}),
        ({"id": 24, "kind": "warranty_check", "at": "x", "text": "pass: SSH ok, page ok."}, {}),
    ]
    moments = [m for m in (keeper.add(entry, data) for entry, data in rows) if m is not None]
    words = [f"{m['strong']} {m['text']}".strip() for m in moments]
    assert words[0] == "Keyboard lent. PayPal says the $9.00 hold is live (CREATED)."
    assert words[1] == "PayPal re-checked: hold still live. Check 2."
    assert sum(1 for w in words if w.startswith("Your check passed (1 of 2)")) == 1  # close passes say it once
    assert "Warranty check 2 of 6 passed." in words
    shown = visible_moments(moments)
    assert [m["code"] for m in shown if m["group"] == "agent"] == ["cmd2", "cmd3", "cmd4"]
    assert [m["strong"] for m in shown if m["group"] == "warranty"] == ["Warranty check 2 of 6 passed."]


def test_console_thumbnails_are_cropped_to_their_text() -> None:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (640, 480), "black")
    ImageDraw.Draw(img).rectangle((4, 4, 300, 120), fill="white")
    out = io.BytesIO()
    img.save(out, format="PNG")
    store = ThumbStore()
    thumb = store.add("abcdef123456", out.getvalue(), at=T0, turn=1, label="free look")
    assert thumb is not None and thumb.box == (0, 0, 309, 129)
    crop = thumb_crop(thumb.width, thumb.height, thumb.box)
    # Desktop: three quarters of the width, from the first text line down, at least a 10:3 band.
    # Phone: the last lines in a narrower band (40 to 45% of the width), near 1x on a phone; the
    # text runs past it (x1 309 > 288), so the page fades that edge.
    assert crop == {"x": 0, "y": 0, "w": 480, "h": 144, "px": 0, "pw": 288, "py": 0, "ph": 144, "iw": 640, "ih": 480,
                    "cut": 1, "dcut": 0}
    assert thumb_crop(640, 480, None) is None


@pytest.mark.parametrize(("width", "height"), [(640, 480), (720, 400), (1024, 768), (1280, 800)])
def test_a_one_line_screen_is_not_blown_up(width: int, height: int) -> None:
    # "Password: _" on an otherwise black screen: the crop keeps at least 3/4 of the width and a
    # 10:3 band, so the page never enlarges a one-line screen until it blurs.
    crop = thumb_crop(width, height, (0, 0, width // 8, 24))
    assert crop is not None
    assert crop["w"] >= width * 0.75 - 1 and crop["h"] >= crop["w"] * 0.3 - 1
    assert width * 0.4 - 1 <= crop["pw"] <= width * 0.45 + 1 and crop["ph"] <= crop["h"]
    # A text block at the bottom of the screen: the band moves up rather than past the edge.
    low = thumb_crop(width, height, (0, height - 30, width // 2, height))
    assert low is not None and low["y"] + low["h"] == height and low["py"] + low["ph"] == height


def test_thumbnails_keep_up_to_1280_pixels() -> None:
    from PIL import Image

    def png(w: int, h: int) -> bytes:
        out = io.BytesIO()
        Image.new("RGB", (w, h), "black").save(out, format="PNG")
        return out.getvalue()

    store = ThumbStore()
    big = store.add("abcdef123456", png(1024, 768), at=T0, turn=1, label="x")
    assert big is not None and (big.width, big.height) == (1024, 768)  # a 1024 screen is kept at 1x
    huge = store.add("abcdef123456", png(2560, 1600), at=T0, turn=2, label="y")
    assert huge is not None and (huge.width, huge.height) == (1280, 800)


# -- pages ---------------------------------------------------------------------------------------


def test_cancel_before_the_hold_returns_to_try(web) -> None:
    run = web.start()
    response = web.client.post(f"/runs/{run.id}/cancel", data={"csrf": web.csrf()}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/try?cancelled=1"
    assert "Run cancelled. Nothing was held." in web.client.get("/try?cancelled=1").text
    assert web.svc.ledger.get(run.rescue_id).state is RescueState.VOIDED
    assert web.fake.order_count() == 0 and web.svc.runs.slot_run() is None
    page = web.client.get(f"/rescues/{run.rescue_id}").text
    assert "Cancelled before the hold." in page and "Stopped. $0 charged." not in page
    # A forged form changes nothing.
    other = web.start("fstab-missing-disk")
    forged = web.client.post(f"/runs/{other.id}/cancel", data={"csrf": "forged"}, follow_redirects=False)
    assert forged.status_code == 403 and other.stage == "quoted"


def test_the_ledger_lists_runs_that_offered_no_hold(web) -> None:
    run = web.start("grub-password")
    assert run.stage == "no_quote"
    assert web.svc.ledger.rescues() == []
    page = web.client.get("/judge").text
    assert "No hold offered" in page and "GRUB locked by a password" in page and "$0, no PayPal call" in page
    data = web.client.get("/judge.json").json()
    assert data["count"] == 0 and len(data["no_hold"]) == 1 and data["no_hold"][0]["fault_class"] == "console_locked"


def test_a_busy_server_links_to_watching_the_live_run(make_web) -> None:
    worlds = GatedWorlds(pause_before=3)
    web = make_web(worlds=worlds, threaded=True)
    run = web.start()
    wait_for(lambda: run.stage == "quoted")
    web.hold(run)
    try:
        assert worlds.paused.wait(10)
        other = TestClient(web.client.app)
        page = other.get("/try").text
        assert "Watch it live" in page and f'href="/rescues/{run.rescue_id}"' in page
        assert "Another run is using the test server" in page and "disabled" in page
        status = other.get("/try/status").json()
        assert status["busy"] is True and status["watchUrl"] == f"/rescues/{run.rescue_id}" and not status["mine"]
        mine = web.client.get("/try").text
        assert f'href="/runs/{run.id}"' in mine and "Open your run" in mine
        assert web.client.get("/try/status").json()["mine"] is True
    finally:
        worlds.release.set()
    wait_for(lambda: run.stage == "finished")
    assert web.client.get("/try/status").json()["busy"] is False


def test_offline_pages_never_mention_a_buyer_login(web) -> None:
    assert "buyer" not in web.client.get("/try").text.lower()
    run = web.start()
    page = web.client.get(f"/runs/{run.id}").text
    assert "Approve<span class=\"wide-only\"> the hold</span> (simulated PayPal)" in page and "Approve a $9.00 hold" in page
    assert "test buyer" not in page and "sandbox buyer" not in page and "buyer-email" not in page


def test_the_rescue_page_speaks_in_words_and_keeps_the_raw_codes(make_web) -> None:
    worlds = GatedWorlds(pause_before=3)
    web = make_web(worlds=worlds, threaded=True)
    run = web.start()
    wait_for(lambda: run.stage == "quoted")
    web.hold(run)
    try:
        assert worlds.paused.wait(10)
        rec = web.svc.ledger.get(run.rescue_id)
        page = web.client.get(f"/rescues/{rec.id}").text
        assert "Agent at the keyboard" in page and 'data-state="ACTIVE"' in page
        assert "Keyboard lent: hold live" in page and "PayPal says CREATED" in page
        assert rec.order_id in page and rec.authorization_id in page and f"auth-{rec.id}" in page
        # Stop is one press, and the line under it says what it does; on path 1 it says to watch.
        assert "Path 1: just watch." in page and "one press here would void the hold" in page
        assert "Press again" not in page and "Press again" not in web.client.get("/static/live.js").text
        # Short ids on the main path, full ids in their title.
        assert f'title="{rec.authorization_id}"' in page
    finally:
        worlds.release.set()
    wait_for(lambda: run.stage == "finished")


def test_a_repeated_start_goes_back_to_the_run(web) -> None:
    first = web.start()
    again = web.client.post("/try/start", data={"csrf": web.csrf(), "fault": "sshd-config-typo"}, follow_redirects=False)
    assert again.status_code == 303 and again.headers["location"] == f"/runs/{first.id}"
    assert len(web.svc.runs.runs()) == 1


def test_the_end_page_tells_how_the_money_moved(make_web) -> None:
    web = make_web(warranty_checks=6, warranty_interval_s=20.0)
    run = web.start()
    web.hold(run)
    web.svc.pump_offline_webhooks(delay_s=0)
    page = web.client.get(f"/rescues/{run.rescue_id}").text
    assert "Fixed. Charged $9.00." in page and "How your $9.00 moved" in page
    assert "re-checked every 20 s, 6 times (compressed for the demo; the real plan is 24 hourly checks)" in page
    stranger = TestClient(web.client.app).get(f"/rescues/{run.rescue_id}").text
    assert "Try the Stop path (fstab, $19)" not in stranger and "See all runs" in stranger
    snap = web.client.get(f"/api/rescues/{run.rescue_id}").json()
    rows = {r["key"]: r for r in snap["paypal_table"]["rows"]}
    assert rows["authorization"]["status"] == "CAPTURED" and rows["authorization"]["means"] == "The hold became the charge."
    assert rows["capture"]["pill"] == "charged" and rows["capture"]["final_capture"] is True
    assert rows["capture"]["api"] and rows["capture"]["webhook"]  # two ticks
    assert rows["order"]["pill"] == "neutral"  # an order's status is not a money state
    assert [t["icon"] for t in snap["tale"]][-1] == "i-shield"


def test_the_owner_gets_the_next_path_after_a_capture(web) -> None:
    run = web.start()
    web.hold(run)
    page = web.client.get(f"/rescues/{run.rescue_id}").text
    assert "Try the Stop path (fstab, $19)" in page and 'href="/try#fault-fstab-missing-disk"' in page


def test_sse_carries_progress_and_moments(web) -> None:
    run = web.start()
    web.hold(run)
    events = sse_events(read_stream(web.client, f"/rescues/{run.rescue_id}/events"))
    snapshot = events[0][1]
    assert snapshot["progress"]["outcome"]["state"] == "charged" and snapshot["moments"]
    states = [data for name, data in events if name == "state"]
    assert states and states[-1]["progress"]["state"] == "WARRANTY_OK" and states[-1]["paypal_table"]["rows"]


def test_run_stream_carries_progress(web) -> None:
    run = web.start("grub-password")
    events = sse_events(read_stream(web.client, f"/runs/{run.id}/events"))
    runs = [data for name, data in events if name == "run"]
    assert runs[-1]["progress"]["outcome"]["state"] == "nohold"


def test_a_browser_gets_an_html_page_for_an_unknown_address(web) -> None:
    html = web.client.get("/no/such/page", headers={"Accept": "text/html"})
    assert html.status_code == 404 and "Page not found." in html.text and 'href="/try"' in html.text
    assert web.client.get("/no/such/page").json() == {"detail": "Not Found"}
    assert web.client.get("/api/nothing", headers={"Accept": "text/html"}).headers["content-type"].startswith(
        "application/json"
    )


def test_pages_keep_the_csp_shape(web) -> None:
    run = web.start()
    web.hold(run)
    for path in ("/", "/try", "/judge", f"/rescues/{run.rescue_id}"):
        html = web.client.get(path).text
        assert not re.search(r"<script(?![^>]*\ssrc=)[^>]*>", html), path
        assert not re.search(r"\son[a-z]+=", html), path
