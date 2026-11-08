"""/try: passcode gate, per-IP rate limits, one VM at a time, the model-spend cap and the three
whitelisted faults."""

from __future__ import annotations

from decimal import Decimal

import pytest
from conftest import PaidWorlds
from fastapi.testclient import TestClient

from keyhold.faults import FAULTS, TRY_FAULTS
from keyhold.pay.client import FakePayPal
from keyhold.types import RescueState
from web.runs import TryRefused


def unlock(web, passcode: str, client: TestClient | None = None, **headers: str):
    client = client or web.client
    return client.post(
        "/try/unlock", data={"csrf": web.csrf(client), "passcode": passcode}, headers=headers, follow_redirects=False
    )


def test_passcode_gate(make_web) -> None:
    web = make_web(passcode="open-sesame")
    page = web.client.get("/try")
    assert 'name="passcode"' in page.text and "Break this server" not in page.text

    # Starting without the passcode goes back to the gate; nothing is booted.
    blocked = web.client.post("/try/start", data={"csrf": web.csrf(), "fault": "sshd-config-typo"}, follow_redirects=False)
    assert blocked.status_code == 303 and blocked.headers["location"] == "/try"
    assert web.svc.runs.runs() == []

    wrong = unlock(web, "nope")
    assert wrong.status_code == 401 and "That passcode is not right." in wrong.text
    right = unlock(web, "open-sesame")
    assert right.status_code == 303
    assert "Break this server" in web.client.get("/try").text
    assert web.start() is not None


def test_offline_without_a_passcode_is_open(web) -> None:
    assert "Break this server" in web.client.get("/try").text


def test_live_mode_without_a_passcode_is_closed(make_web, clock) -> None:
    web = make_web(mode="live", paypal_client_id="id", paypal_client_secret="secret", gateway=FakePayPal(clock=clock))
    page = web.client.get("/try")
    assert "has not set a passcode" in page.text
    assert unlock(web, "").status_code == 503
    response = web.client.post("/try/start", data={"csrf": web.csrf(), "fault": "sshd-config-typo"}, follow_redirects=False)
    assert response.status_code == 303 and web.svc.runs.runs() == []


def test_unlock_attempts_are_rate_limited_per_address(make_web) -> None:
    web = make_web(passcode="open-sesame", unlock_attempts=2, trust_proxy=True)
    first = {"X-Forwarded-For": "addr-one"}
    assert unlock(web, "a", **first).status_code == 401
    assert unlock(web, "b", **first).status_code == 401
    limited = unlock(web, "open-sesame", **first)
    assert limited.status_code == 429 and "Retry-After" in limited.headers
    # Another address is counted separately.
    other = TestClient(web.client.app)
    assert unlock(web, "open-sesame", client=other, **{"X-Forwarded-For": "addr-two"}).status_code == 303


def test_run_starts_are_rate_limited_per_address(make_web) -> None:
    web = make_web(runs_per_hour=1)
    run = web.start("grub-password")  # no quote: frees the server at once
    assert run.stage == "no_quote"
    again = web.client.post("/try/start", data={"csrf": web.csrf(), "fault": "grub-password"}, follow_redirects=False)
    assert again.status_code == 429
    assert "runs in the last hour" in again.text


def test_forms_and_api_need_the_csrf_token(web) -> None:
    response = web.client.post("/try/start", data={"csrf": "forged", "fault": "sshd-config-typo"}, follow_redirects=False)
    assert response.status_code == 403 and web.svc.runs.runs() == []
    run = web.start()
    api = web.client.post("/api/orders", json={"rescueId": run.rescue_id})
    assert api.status_code == 403 and api.json()["code"] == "csrf"
    assert web.fake.order_count() == 0


def test_only_the_three_whitelisted_faults(web) -> None:
    assert set(TRY_FAULTS) < set(FAULTS)
    other = next(f for f in FAULTS if f not in TRY_FAULTS)
    response = web.client.post("/try/start", data={"csrf": web.csrf(), "fault": other}, follow_redirects=False)
    assert response.status_code == 400
    with pytest.raises(TryRefused) as refused:
        web.svc.runs.start(other, owner="x", ip="y")
    assert refused.value.code == "unknown_fault"
    page = web.client.get("/try").text
    for fault_id in TRY_FAULTS:
        assert f'value="{fault_id}"' in page


def test_one_server_at_a_time(web) -> None:
    run = web.start()
    assert run.stage == "quoted"
    assert web.client.get("/healthz").json()["slot"] == "busy"

    other = TestClient(web.client.app)
    busy = other.post("/try/start", data={"csrf": web.csrf(other), "fault": "fstab-missing-disk"}, follow_redirects=False)
    assert busy.status_code == 409 and "Another run is using the test server" in busy.text
    assert 'disabled' in other.get("/try").text

    # Cancelling the unpaid run releases the server; nothing was ever held.
    cancel = web.client.post(f"/rescues/{run.rescue_id}/stop", headers=web.headers())
    assert cancel.status_code == 200 and cancel.json()["state"] == "VOIDED"
    assert web.fake.order_count() == 0
    assert web.start("fstab-missing-disk", client=other).stage == "quoted"


def test_an_unpaid_quote_expires_and_frees_the_server(web, clock) -> None:
    run = web.start()
    web.order(run)  # an order exists, never approved
    clock.advance(web.svc.settings.quote_ttl_s + 1)
    web.svc.runs.sweep()
    assert run.stage == "expired"
    rec = web.svc.ledger.get(run.rescue_id)
    assert rec.state is RescueState.VOIDED and "not paid within" in rec.end_reason
    assert web.svc.runs.slot_run() is None
    late = web.client.post("/api/orders", json={"rescueId": run.rescue_id}, headers=web.headers())
    assert late.status_code == 409


def test_the_spend_cap_blocks_new_runs(make_web) -> None:
    web = make_web(worlds=PaidWorlds(), spend_cap_usd=Decimal("2.00"), run_budget_usd=Decimal("3.00"))
    response = web.client.post("/try/start", data={"csrf": web.csrf(), "fault": "sshd-config-typo"}, follow_redirects=False)
    assert response.status_code == 503 and "model budget" in response.text
    assert web.svc.runs.runs() == []
    assert "Model budget, last 24 h" in web.client.get("/try").text


def test_a_run_that_spends_its_budget_is_stopped_and_voided(make_web) -> None:
    # Each reply is priced at 50k output tokens (about $1.00); the run budget is $3.
    web = make_web(worlds=PaidWorlds(output_tokens=50_000), spend_cap_usd=Decimal(50), run_budget_usd=Decimal(3))
    run = web.start()
    assert run.stage == "quoted"
    result = web.hold(run)
    rec = web.svc.ledger.get(result["rescueId"])
    assert rec.state is RescueState.VOIDED
    assert "model budget" in rec.end_reason
    assert web.fake.money_calls("capture") == []
    assert len(web.fake.money_calls("void")) == 1
    spent = web.svc.spend.spent()
    assert Decimal(3) <= spent < Decimal("4.2")
    assert web.svc.ledger.events(kind="model_spend")


def test_vm_lab_problems_are_shown_and_block_starts(make_web) -> None:
    class Broken:
        kind = "vm"
        paid_model = True

        def problems(self) -> list[str]:
            return ["the golden image is not built (tools/vm/vm.py golden)"]

        def boot(self, *args):  # pragma: no cover - never reached
            raise AssertionError("must not boot")

    web = make_web(worlds=Broken())
    page = web.client.get("/try").text
    assert "The test server is not ready." in page and "golden image is not built" in page
    response = web.client.post("/try/start", data={"csrf": web.csrf(), "fault": "sshd-config-typo"}, follow_redirects=False)
    assert response.status_code == 503


def test_owner_note_travels_from_the_form_to_the_free_look_and_the_page(web) -> None:
    note = "ssh refused & <b>I</b> edited sshd_config\nline two"
    response = web.client.post(
        "/try/start", data={"csrf": web.csrf(), "fault": "sshd-config-typo", "note": note}, follow_redirects=False
    )
    assert response.status_code == 303
    run = web.svc.runs.get(response.headers["location"].rsplit("/", 1)[-1])
    # Angle brackets are replaced (the note sits inside tags in the prompt), the rest is kept.
    assert run.note == "ssh refused & (b)I(/b) edited sshd_config line two"
    looks = web.svc.ledger.events(run.rescue_id, kind="free_look")
    assert looks and looks[-1].data["owner_note"] == run.note
    page = web.client.get(f"/runs/{run.id}").text
    assert "ssh refused &amp; (b)I(/b) edited sshd_config line two" in page
    assert 'name="note"' in web.client.get("/try").text


def test_a_run_without_a_note_shows_none(web) -> None:
    run = web.start()
    assert run.note is None
    assert "Your note, read as data" not in web.client.get(f"/runs/{run.id}").text


# -- the judge link, several test servers, the buyer login ------------------------------------------


def test_judge_link_unlocks_with_one_click(make_web) -> None:
    from web.security import judge_token

    web = make_web(passcode="open-sesame")
    token = judge_token(web.svc.settings.secret_key, "open-sesame")
    assert token and token != "open-sesame" and "open-sesame" not in token
    assert "Break this server" not in web.client.get("/try").text
    response = web.client.get(f"/j/{token}", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/try?unlocked=1"
    # The page says once that this browser is unlocked; a plain /try does not repeat it.
    assert "Unlocked for this browser." in web.client.get("/try?unlocked=1").text
    page = web.client.get("/try").text
    assert "Break this server" in page and "Unlocked for this browser." not in page
    # The note follows the session, not the address: another browser is still locked.
    assert "Unlocked for this browser." not in TestClient(web.client.app).get("/try?unlocked=1").text


def test_a_wrong_judge_link_unlocks_nothing(make_web) -> None:
    web = make_web(passcode="open-sesame")
    assert web.client.get("/j/open-sesame").status_code == 404  # the passcode itself is not a link
    assert web.client.get("/j/" + "0" * 32).status_code == 404
    assert "Break this server" not in web.client.get("/try").text


def test_without_a_passcode_there_is_no_judge_link(make_web) -> None:
    from web.security import judge_token

    web = make_web()
    assert judge_token(web.svc.settings.secret_key, "") == ""
    assert web.client.get("/j/anything").status_code == 404


def test_several_test_servers_one_run_per_browser(make_web) -> None:
    web = make_web(try_slots=2)
    first = web.start()
    assert first.stage == "quoted"
    again = web.client.post("/try/start", data={"csrf": web.csrf(), "fault": "fstab-missing-disk"}, follow_redirects=False)
    assert again.status_code == 409 and "already has a run" in again.text
    other = TestClient(web.client.app)
    assert web.start("fstab-missing-disk", client=other).stage == "quoted"
    third = TestClient(web.client.app)
    busy = third.post("/try/start", data={"csrf": web.csrf(third), "fault": "grub-password"}, follow_redirects=False)
    assert busy.status_code == 409 and "All 2 test servers are in use" in busy.text
    health = web.client.get("/healthz").json()
    assert health["slot"] == "busy" and health["slots"] == {"used": 2, "total": 2}


def test_a_run_in_its_warranty_says_why_the_next_one_waits(make_web) -> None:
    web = make_web(try_slots=2)
    first = web.start()
    first.stage = "warranty"  # captured; the warranty re-probes keep its test server up
    again = web.client.post("/try/start", data={"csrf": web.csrf(), "fault": "fstab-missing-disk"}, follow_redirects=False)
    assert again.status_code == 409 and "still in its warranty checks" in again.text


def test_one_slot_still_says_one_server(make_web) -> None:
    web = make_web()
    assert "One server at a time." in web.client.get("/try").text
    assert web.client.get("/healthz").json()["slots"] == {"used": 0, "total": 1}


def test_buyer_login_is_shown_next_to_the_paypal_button_only_in_live_mode(make_web, clock) -> None:
    live = make_web(mode="live", passcode="p", paypal_client_id="id", paypal_client_secret="secret",
                    gateway=FakePayPal(clock=clock), judge_buyer_email="buyer@example.com",
                    judge_buyer_password="sandbox-pass")
    unlock(live, "p")
    run = live.start()
    page = live.client.get(f"/runs/{run.id}").text
    assert "buyer@example.com" in page and "sandbox-pass" in page and 'data-copy="buyer-password"' in page
    assert "sandbox-pass" not in repr(live.svc.settings)

    offline = make_web(judge_buyer_email="buyer@example.com", judge_buyer_password="sandbox-pass")
    run = offline.start()
    assert "buyer@example.com" not in offline.client.get(f"/runs/{run.id}").text


def test_slot_setting_is_bounded() -> None:
    from web.config import Settings, SettingsError

    assert Settings.from_env({"KEYHOLD_TRY_SLOTS": "3"}).try_slots == 3
    with pytest.raises(SettingsError, match="KEYHOLD_TRY_SLOTS"):
        Settings.from_env({"KEYHOLD_TRY_SLOTS": "9"})
    with pytest.raises(SettingsError, match="KEYHOLD_TRY_SLOTS"):
        Settings.from_env({"KEYHOLD_TRY_SLOTS": "0"})
