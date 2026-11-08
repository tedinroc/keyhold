"""The live status page: PayPal read live, SSE, thumbnails, Stop, the ledger page."""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable

from conftest import GatedWorlds
from fastapi.testclient import TestClient

from keyhold.types import RescueState


def wait_for(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


def sse_events(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.split("\n\n"):
        name = re.search(r"^event: (\S+)$", block, re.MULTILINE)
        data = re.search(r"^data: (.*)$", block, re.MULTILINE)
        if name and data:
            events.append((name.group(1), json.loads(data.group(1))))
    return events


def read_stream(client: TestClient, url: str) -> str:
    text = ""
    with client.stream("GET", url) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        for chunk in response.iter_text():
            text += chunk
            if "event: end" in text:
                break
    return text


def held_and_paused(make_web, **settings):
    worlds = GatedWorlds(pause_before=3)
    web = make_web(worlds=worlds, threaded=True, **settings)
    run = web.start()
    wait_for(lambda: run.stage == "quoted")
    result = web.hold(run)
    assert result["state"] in ("HELD", "ACTIVE")
    assert worlds.paused.wait(10)
    return web, worlds, run


def test_the_status_page_shows_the_hold_with_ids_and_reads_paypal_live(make_web) -> None:
    web, worlds, run = held_and_paused(make_web)
    try:
        rid = run.rescue_id
        rec = web.svc.ledger.get(rid)
        assert rec.state is RescueState.ACTIVE

        page = web.client.get(f"/rescues/{rid}").text
        assert "Held" in page and "$9.00" in page
        assert rec.order_id in page and rec.authorization_id in page
        assert f"auth-{rid}" in page and f"order-{rid}" in page  # request ids in the ticks table
        assert "Stop and void the hold" in page

        snap = web.client.get(f"/api/rescues/{rid}").json()
        assert snap["money"]["label"] == "Held"
        assert snap["paypal"]["authorization"]["status"] == "CREATED"
        assert snap["paypal"]["authorization"]["expiration_time"]
        assert snap["paypal"]["order"]["intent"] == "AUTHORIZE"
        assert snap["paypal"]["order"]["create_time"]

        # Voided at PayPal behind Keyhold's back: the page shows PayPal's answer on the next
        # read, once the shared per-rescue cache (paypal_poll_s) has run out.
        web.fake.void_externally(rec.authorization_id)
        web.svc.clock.advance(web.svc.settings.paypal_poll_s)
        snap = web.client.get(f"/api/rescues/{rid}").json()
        assert snap["paypal"]["authorization"]["status"] == "VOIDED"
        assert snap["rescue"]["state"] == "ACTIVE"
        assert "VOIDED" in snap["money"]["warning"]
    finally:
        worlds.release.set()
    wait_for(lambda: run.stage == "finished")
    rec = web.svc.ledger.get(run.rescue_id)
    assert rec.state is RescueState.VOIDED
    assert web.fake.all_captures() == []


def test_stop_voids_the_hold_and_no_key_reaches_the_console_after_it(make_web) -> None:
    web, worlds, run = held_and_paused(make_web)
    try:
        world = worlds.worlds[0]
        sent_before = len(world.console.fake.inputs)
        stranger = TestClient(web.client.app)
        assert "Stop and void the hold" not in stranger.get(f"/rescues/{run.rescue_id}").text

        response = web.client.post(f"/rescues/{run.rescue_id}/stop", headers=web.headers())
        assert response.status_code == 200
        body = response.json()
        assert body["voided"] is True and body["state"] == "VOIDED"
        assert "Nothing was charged" in body["detail"]
    finally:
        worlds.release.set()
    wait_for(lambda: run.stage == "finished")
    assert len(world.console.fake.inputs) == sent_before
    rec = web.svc.ledger.get(run.rescue_id)
    assert rec.state is RescueState.VOIDED and rec.end_reason == "the owner pressed Stop"
    auth = web.fake.get_authorization(rec.authorization_id)
    assert auth.status == "VOIDED"
    assert [c.request_id for c in web.fake.money_calls("void")] == [f"void-{rec.id}"]
    assert web.fake.all_captures() == []
    assert web.svc.runs.slot_run() is None


def test_sse_streams_agent_actions_probe_and_capture(make_web) -> None:
    web, worlds, run = held_and_paused(make_web, sse_max_s=8.0, sse_poll_s=0.02)
    timer = threading.Timer(0.2, worlds.release.set)
    timer.start()
    try:
        text = read_stream(web.client, f"/rescues/{run.rescue_id}/events")
    finally:
        timer.cancel()
        worlds.release.set()
    events = sse_events(text)
    names = [name for name, _ in events]
    assert names[0] == "snapshot" and names[-1] == "end"
    logs = [data["text"] for name, data in events if name == "log"]
    snapshot_logs = [entry["text"] for entry in events[0][1]["timeline"]]
    every_log = snapshot_logs + logs
    assert any(text.startswith("typed: sshd -t") for text in every_log)
    assert any("keyboard allowed" in text for text in every_log)
    assert any(text.startswith("pass:") for text in every_log)
    assert any("capture with PayPal-Request-Id cap-" in text for text in every_log)
    states = [data["rescue"]["state"] for name, data in events if name == "state"]
    assert states[-1] == "WARRANTY_OK"
    final = [data for name, data in events if name == "state"][-1]
    assert final["paypal"]["capture"]["status"] == "COMPLETED"
    assert final["paypal"]["capture"]["final_capture"] is True
    assert final["money"]["label"] == "Captured" and final["money"]["amount"] == "9.00"
    assert "thumb" in names or events[0][1]["thumbs"]


def test_a_finished_rescue_stream_is_short(web) -> None:
    run = web.start()
    web.hold(run)
    events = sse_events(read_stream(web.client, f"/rescues/{run.rescue_id}/events"))
    assert [name for name, _ in events] == ["snapshot", "state", "end"]
    snap = events[0][1]
    assert snap["rescue"]["capture_id"] and snap["paypal"]["capture"]["create_time"]
    assert snap["invariants"] == []
    assert {t["op"] for t in snap["ticks"]} == {"Order", "Authorization", "Capture"}
    assert all(t["api_at"] for t in snap["ticks"])


def test_run_page_stream_reports_the_stage(web) -> None:
    run = web.start("grub-password")
    events = sse_events(read_stream(web.client, f"/runs/{run.id}/events"))
    assert events[0][0] == "thumb" or events[0][0] == "run"
    stages = [data["stage"] for name, data in events if name == "run"]
    assert stages == ["no_quote"]
    assert events[-1][0] == "end"


def test_webhooks_give_the_second_tick(web) -> None:
    run = web.start()
    web.hold(run)
    before = web.client.get(f"/api/rescues/{run.rescue_id}").json()
    assert all(t["webhook_at"] is None for t in before["ticks"])
    outcomes = web.svc.pump_offline_webhooks(delay_s=0)
    assert {o.outcome for o in outcomes} == {"applied"}
    after = web.client.get(f"/api/rescues/{run.rescue_id}").json()
    ticks = {t["op"]: t for t in after["ticks"]}
    assert ticks["Authorization"]["webhook_at"] and ticks["Capture"]["webhook_at"]
    assert ticks["Order"]["webhook"] is False
    hooks = [e for e in after["timeline"] if e["group"] == "webhook"]
    assert {h["event_type"] for h in hooks} == {"PAYMENT.AUTHORIZATION.CREATED", "PAYMENT.CAPTURE.COMPLETED"}
    assert all("verified, applied" in h["text"] for h in hooks)


def test_console_thumbnails_are_served(web) -> None:
    run = web.start()
    web.hold(run)
    thumbs = web.svc.runs.thumbs.list(run.id)
    assert len(thumbs) >= 3  # free look, then the agent's screens
    assert thumbs[0].label == "free look"
    image = web.client.get(f"/runs/{run.id}/thumbs/{thumbs[-1].n}.png")
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    assert image.content.startswith(b"\x89PNG")
    page = web.client.get(f"/rescues/{run.rescue_id}").text
    assert f"/runs/{run.id}/thumbs/{thumbs[-1].n}.png" in page


def test_the_console_password_never_reaches_a_page(web) -> None:
    run = web.start()
    password = run.world.console_password
    assert password
    web.hold(run)
    web.svc.pump_offline_webhooks(delay_s=0)
    rid = run.rescue_id
    bodies = [
        web.client.get(f"/rescues/{rid}").text,
        web.client.get(f"/api/rescues/{rid}").text,
        read_stream(web.client, f"/rescues/{rid}/events"),
        web.client.get("/judge").text,
        web.client.get("/judge.json").text,
    ]
    for body in bodies:
        assert password not in body
    assert any("[console password]" in body for body in bodies)
    assert all(password not in f"{e.detail} {e.data}" for e in web.svc.ledger.events())


def test_the_ledger_page_lists_every_rescue(web) -> None:
    first = web.start("sshd-config-typo")
    web.hold(first)
    second = web.start("fstab-missing-disk")
    stop = web.client.post(f"/rescues/{second.rescue_id}/stop", headers=web.headers())
    assert stop.json()["state"] == "VOIDED"

    page = web.client.get("/judge").text
    assert first.rescue_id[:8] in page and second.rescue_id[:8] in page
    assert "$9.00" in page and "WARRANTY_OK" in page and "VOIDED" in page

    data = web.client.get("/judge.json").json()
    assert data["count"] == 2 and data["problems"] == 0
    assert data["states"] == {"WARRANTY_OK": 1, "VOIDED": 1}
    assert data["totals"] == {"held": "0.00", "captured": "9.00", "refunded": "0.00"}
    assert data["mode"] == "offline"
    row = next(r for r in data["rows"] if r["id"] == first.rescue_id)
    assert row["capture_id"] and row["authorization_id"] and row["order_id"]


def test_console_screens_survive_a_restart(make_web) -> None:
    first = make_web()
    run = first.start()
    first.hold(run)
    count = len(first.svc.runs.thumbs.list(run.id))
    assert count >= 3

    # A new process on the same data directory: the run is gone from memory, its screens are not.
    second = make_web(gateway=first.fake)
    assert second.svc.runs.for_rescue(run.rescue_id) is None
    snap = second.client.get(f"/api/rescues/{run.rescue_id}").json()
    assert len(snap["thumbs"]) == count
    image = second.client.get(snap["thumbs"][-1]["url"])
    assert image.status_code == 200 and image.content.startswith(b"\x89PNG")


def test_thumbnail_paths_are_checked(web) -> None:
    for bad in ("..", "ABCDEF123456", "zz", "../../etc"):
        assert web.client.get(f"/runs/{bad}/thumbs/1.png").status_code == 404
