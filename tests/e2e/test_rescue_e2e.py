"""End-to-end rescues with FakePayPal, FakeConsole, FakeModel and fake probe transports.

Each test drives the whole path: free look -> hold -> gated agent -> owner probe -> capture or
void -> warranty -> refund, and then checks PayPal's side (FakePayPal), the ledger and the
console's record of what was actually typed.
"""

from __future__ import annotations

import json
import threading
from decimal import Decimal

import pytest
from e2e_world import (
    CONSOLE_PASSWORD,
    EMERGENCY,
    GRUB_LOCK,
    SPEC,
    WEBHOOK_ID,
    SimulatedCrash,
    SshdWorld,
    all_event_text,
    crash_at,
    emergency_console,
    fix_script,
    grub_console,
    held,
    money_calls,
    quote_reply,
    sshd_quote_reply,
)

from keyhold.agent.model import FakeModel, tool_reply
from keyhold.pay.client import FakePayPal
from keyhold.pay.ledger import IllegalTransition, Ledger
from keyhold.pay.reconciler import Reconciler
from keyhold.pay.webhooks import WebhookProcessor
from keyhold.probe.fake import ScriptedSsh
from keyhold.rescue import OWNER_STOP, Orchestrator, RescueConfig
from keyhold.types import AgentOutcome, MoneyOp, OutboxStatus, RescueState, Tier


def start_sshd(orch, fake, *script):
    """Free look and hold on the sshd world; the model then plays ``script``."""
    world = SshdWorld.build()
    model = FakeModel([sshd_quote_reply(), *script])
    rec = held(orch, fake, world.console, model, screen_text=world.screen_text)
    assert world.console.inputs == []  # the free look never pressed a key
    return world, model, rec


def run(orch, rec, world, model, **kw):
    kw.setdefault("console_password", CONSOLE_PASSWORD)
    return orch.run(rec.id, world.console, model, ssh=world.server.ssh, http=world.server.http, **kw)


def assert_voided(ledger, fake, rid, reason_part):
    rec = ledger.get(rid)
    assert rec.state is RescueState.VOIDED
    assert reason_part in (rec.end_reason or "")
    assert fake.get_authorization(rec.authorization_id).status == "VOIDED"
    assert fake.captures_for(rec.authorization_id) == []
    voids = money_calls(fake, "void")
    assert [c.request_id for c in voids] == [f"void-{rid}"]
    assert ledger.check_invariants(rid) == []
    return rec


# -- success -------------------------------------------------------------------------------------


def test_success_captures_the_quote_and_the_warranty_ends_ok(orch, ledger, fake, events) -> None:
    world, model, rec = start_sshd(orch, fake, *fix_script())
    report = run(orch, rec, world, model)

    assert report.agent.outcome is AgentOutcome.DONE
    assert report.stable is not None and report.stable.stable
    assert report.state is RescueState.WARRANTY_OK
    assert report.captured_amount == Decimal("9.00") == rec.quote_amount
    assert report.warranty is not None and report.warranty.verdict.value == "ok"

    caps = fake.captures_for(rec.authorization_id)
    assert len(caps) == 1
    assert caps[0].amount == Decimal("9.00") and caps[0].final_capture
    assert [c.request_id for c in money_calls(fake, "capture")] == [f"cap-{rec.id}"]
    assert money_calls(fake, "void") == [] and money_calls(fake, "refund") == []
    assert fake.get_authorization(rec.authorization_id).status == "CAPTURED"
    assert ledger.check_invariants(rec.id) == []

    # The keyboard did what the script said, and only through the lease.
    assert world.console.typed_text().endswith("systemctl restart ssh\n")
    assert report.lease_checks >= 2  # re-checked PayPal during the run, not just once
    assert ledger.events(rec.id, kind="gate_check")
    # Two passes at least 30 s apart before the capture.
    passes = report.stable.passes
    assert len(passes) == 2 and passes[1].at - passes[0].at >= 30

    # The console password reached the console and the model, never the ledger or the status sink.
    assert CONSOLE_PASSWORD in world.console.typed_text()
    assert CONSOLE_PASSWORD not in all_event_text(ledger)
    assert all(CONSOLE_PASSWORD not in f"{ev.detail} {dict(ev.data)}" for _, ev in events)
    assert "[console password]" in all_event_text(ledger)
    kinds = [ev.kind for _, ev in events]
    assert "screenshot" in kinds and "money" in kinds and "probe" in kinds

    states = [e.data["to"] for e in ledger.events(rec.id, kind="state")]
    assert states == ["HOLD_PENDING", "HELD", "ACTIVE", "PROBING", "CAPTURED", "WARRANTY_OK"]


def test_capture_can_undercut_the_quote_and_releases_the_rest(orch, ledger, fake) -> None:
    world, model, rec = start_sshd(orch, fake, *fix_script())
    report = run(orch, rec, world, model, capture_amount="7.00")
    assert report.state is RescueState.WARRANTY_OK
    cap = fake.captures_for(rec.authorization_id)[0]
    assert cap.amount == Decimal("7.00") and cap.final_capture
    assert fake.authorization_released(rec.authorization_id) == Decimal("2.00")
    assert ledger.get(rec.id).captured_amount == Decimal("7.00")


def test_capture_above_the_quote_is_refused_before_any_key(orch, ledger, fake) -> None:
    world, model, rec = start_sshd(orch, fake, *fix_script())
    with pytest.raises(ValueError, match="at most the quote"):
        run(orch, rec, world, model, capture_amount="9.01")
    assert world.console.inputs == []
    assert ledger.get(rec.id).state is RescueState.HELD
    assert orch.stop(rec.id).voided


def test_judge_mode_uses_the_compressed_labelled_timing(ledger, fake, clock) -> None:
    orch = Orchestrator(ledger, fake, clock=clock, config=RescueConfig(judge_mode=True))
    world, model, rec = start_sshd(orch, fake, *fix_script())
    report = run(orch, rec, world, model)
    assert report.state is RescueState.WARRANTY_OK
    started = ledger.events(rec.id, kind="warranty_started")[0]
    assert started.data["judge_mode"] and started.data["interval_s"] == 60.0
    assert "judge mode" in started.detail


# -- voids ---------------------------------------------------------------------------------------


def test_give_up_voids_the_hold(orch, ledger, fake) -> None:
    look = emergency_console()
    model = FakeModel(
        [
            quote_reply("fstab_bad_entry", EMERGENCY, Tier.T2),
            tool_reply("screenshot"),
            tool_reply("give_up", {"reason": "The emergency shell needs the root password, which was not given."}),
        ]
    )
    rec = held(orch, fake, look, model, screen_text=lambda: "\n".join(look.visible_lines()))
    assert rec.quote_amount == Decimal("19.00")
    report = orch.run(rec.id, look, model, ssh=ScriptedSsh([False]), http=None)
    assert report.agent.outcome is AgentOutcome.GAVE_UP
    assert_voided(ledger, fake, rec.id, "the agent gave up: The emergency shell needs the root password")
    assert look.inputs == []
    assert fake.authorization_released(rec.authorization_id) == Decimal("19.00")


def test_a_fault_the_console_cannot_fix_gets_no_hold(orch, ledger, fake) -> None:
    look = grub_console()
    model = FakeModel([quote_reply("console_locked", GRUB_LOCK, Tier.T2)])
    result, rec = orch.free_look(look, model, probe=SPEC, screen_text=lambda: "\n".join(look.visible_lines()))
    assert rec is None and result.quote is None
    assert result.fault_class == "console_locked"
    assert fake.order_count() == 0 and ledger.rescues() == []
    assert look.inputs == []


def test_turn_limit_voids_the_hold(ledger, fake, clock) -> None:
    orch = Orchestrator(ledger, fake, clock=clock, config=RescueConfig(max_turns=3))
    world, model, rec = start_sshd(orch, fake, *[tool_reply("screenshot") for _ in range(3)])
    report = run(orch, rec, world, model)
    assert report.agent.outcome is AgentOutcome.TURN_LIMIT and report.agent.turns == 3
    assert_voided(ledger, fake, rec.id, "ran out of turns")
    assert world.console.inputs == []


def test_rescue_timeout_voids_the_hold(ledger, fake, clock) -> None:
    orch = Orchestrator(ledger, fake, clock=clock, config=RescueConfig(rescue_timeout_s=5.0))
    world, model, rec = start_sshd(orch, fake, *fix_script())
    report = run(orch, rec, world, model)
    assert report.agent.outcome is AgentOutcome.REVOKED
    assert_voided(ledger, fake, rec.id, "time limit")
    # Each typed line waits 1.5 s for the screen, so only the first few lines got through.
    assert 0 < len(world.console.inputs) < 6


def test_a_hold_voided_at_paypal_mid_run_cuts_the_keyboard(ledger, fake, clock) -> None:
    orch = Orchestrator(ledger, fake, clock=clock, config=RescueConfig(lease_check_every=2))
    world, model, rec = start_sshd(orch, fake, *fix_script())
    world.console.on_input(
        lambda r: fake.void_externally(rec.authorization_id) if len(world.console.inputs) == 2 else None
    )
    report = run(orch, rec, world, model)
    assert report.agent.outcome is AgentOutcome.REVOKED
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.VOIDED and fake.captures_for(rec.authorization_id) == []
    # The lease re-checks every two actions; the void was seen within that window.
    assert len(world.console.inputs) <= 3


@pytest.mark.parametrize(
    "ssh",
    [
        pytest.param([True, False] * 200, id="flapping"),
        pytest.param([True, True, False] * 200, id="flapping-pairs"),
        pytest.param([False], id="never-up"),
    ],
)
def test_a_probe_that_never_passes_stably_never_captures(orch, ledger, fake, ssh) -> None:
    world, model, rec = start_sshd(orch, fake, *fix_script())
    report = orch.run(rec.id, world.console, model, ssh=ScriptedSsh(ssh), http=world.server.http,
                      console_password=CONSOLE_PASSWORD)
    assert report.agent.outcome is AgentOutcome.DONE
    assert report.stable is not None and not report.stable.stable and report.stable.reason == "deadline"
    assert_voided(ledger, fake, rec.id, "the owner probe never passed")
    probes = ledger.events(rec.id, kind="probe")
    assert len(probes) > 2
    assert money_calls(fake, "capture") == []


# -- owner Stop ----------------------------------------------------------------------------------


def test_owner_stop_mid_run_voids_and_cuts_input_at_once(orch, ledger, fake) -> None:
    world, model, rec = start_sshd(orch, fake, *fix_script())
    results = []

    def stop_on_password(record) -> None:
        if record.value == CONSOLE_PASSWORD + "\n":
            results.append(orch.stop(rec.id))

    world.console.on_input(stop_on_password)
    report = run(orch, rec, world, model)

    assert results and results[0].voided and results[0].state is RescueState.VOIDED
    assert report.agent.outcome is AgentOutcome.REVOKED
    assert_voided(ledger, fake, rec.id, OWNER_STOP)
    # Stop landed while the password was being typed: the rest of it, its Enter included,
    # never reached the console, and nothing after it did.
    inputs = world.console.inputs
    assert [r.value for r in inputs[:2]] == ["root\n", CONSOLE_PASSWORD + "\n"]
    assert [r.kind for r in inputs[2:]] == ["cut"] and inputs[2].value < len(CONSOLE_PASSWORD) + 1
    cut = ledger.events(rec.id, kind="agent_type_cut")
    assert len(cut) == 1 and cut[0].data["enter_withheld"] is True
    assert CONSOLE_PASSWORD not in cut[0].detail and CONSOLE_PASSWORD not in str(cut[0].data)
    assert "sent" not in cut[0].data and "total" not in cut[0].data  # key counts would give the password's length
    assert "never sent" in cut[0].detail
    assert not world.server.ssh_up


def test_owner_stop_from_another_thread(orch, ledger, fake) -> None:
    model_waiting = threading.Event()
    stop_done = threading.Event()

    def blocked_turn(_request):
        model_waiting.set()
        assert stop_done.wait(10)
        return tool_reply("type", {"text": "sshd -t\n"})

    world, model, rec = start_sshd(orch, fake, *fix_script()[:2], blocked_turn, *fix_script()[3:])
    out = {}
    worker = threading.Thread(target=lambda: out.setdefault("report", run(orch, rec, world, model)))
    worker.start()
    assert model_waiting.wait(10)
    assert rec.id in orch.live_rescues()

    stopped = orch.stop(rec.id)
    sent_when_stop_returned = len(world.console.inputs)
    stop_done.set()
    worker.join(10)

    assert stopped.voided
    assert len(world.console.inputs) == sent_when_stop_returned == 2
    assert out["report"].agent.outcome is AgentOutcome.REVOKED
    assert_voided(ledger, fake, rec.id, OWNER_STOP)
    assert orch.live_rescues() == []


def test_owner_stop_while_probing_voids_and_never_captures(orch, ledger, fake, events) -> None:
    world, model, rec = start_sshd(orch, fake, *fix_script())
    stopped = []
    orch.on_event = lambda rid, ev: (
        stopped.append(orch.stop(rid)) if ev.kind == "probe" and ev.data.get("ok") and not stopped else None
    )
    report = run(orch, rec, world, model)
    assert stopped and stopped[0].voided
    assert report.stable is not None and report.stable.reason == "stopped"
    assert_voided(ledger, fake, rec.id, OWNER_STOP)


def test_owner_stop_after_the_capture_cannot_void(orch, ledger, fake) -> None:
    world, model, rec = start_sshd(orch, fake, *fix_script())
    stopped = []
    orch.on_event = lambda rid, ev: (
        stopped.append(orch.stop(rid)) if ev.kind == "money" and ev.data.get("op") == "capture" else None
    )
    report = run(orch, rec, world, model)
    assert stopped and not stopped[0].voided
    assert "warranty still applies" in stopped[0].detail
    assert report.state is RescueState.WARRANTY_OK
    assert money_calls(fake, "void") == []
    assert len(fake.captures_for(rec.authorization_id)) == 1


def test_stop_before_the_run_means_no_keyboard(orch, ledger, fake) -> None:
    world, model, rec = start_sshd(orch, fake, *fix_script())
    assert orch.stop(rec.id).voided
    report = run(orch, rec, world, model)
    assert report.state is RescueState.VOIDED and report.agent is None
    assert world.console.inputs == []
    assert len(model.calls) == 1  # only the free look


# -- warranty ------------------------------------------------------------------------------------


def test_relapse_during_the_warranty_refunds_in_full(orch, ledger, fake) -> None:
    world, model, rec = start_sshd(orch, fake, *fix_script())
    orch.on_event = lambda rid, ev: (
        world.server.break_down() if ev.kind == "money" and ev.data.get("op") == "capture" else None
    )
    report = run(orch, rec, world, model)

    assert report.state is RescueState.REFUNDED
    assert report.warranty is not None and report.warranty.refund
    assert report.captured_amount == report.refunded_amount == Decimal("9.00")
    cap = fake.captures_for(rec.authorization_id)[0]
    refunds = fake.refunds_for(cap.id)
    assert len(refunds) == 1 and refunds[0].amount == Decimal("9.00")
    assert [c.request_id for c in money_calls(fake, "refund")] == [f"refund-{rec.id}"]
    checks = ledger.events(rec.id, kind="warranty_check")
    assert [c.data["ok"] for c in checks] == [False, False]  # two failures in a row, then the refund
    assert ledger.check_invariants(rec.id) == []


def test_one_failed_warranty_check_is_not_a_refund(orch, ledger, fake) -> None:
    world, model, rec = start_sshd(orch, fake, *fix_script())
    flips = {"n": 0}

    def flaky_once(rid, ev) -> None:
        if ev.kind == "money" and ev.data.get("op") == "capture":
            world.server.break_down()
        if ev.kind == "warranty" and "ok" in ev.data:
            flips["n"] += 1
            if flips["n"] == 1:
                world.server.fix()

    orch.on_event = flaky_once
    report = run(orch, rec, world, model)
    assert report.state is RescueState.WARRANTY_OK
    assert money_calls(fake, "refund") == []


# -- crash and replay ----------------------------------------------------------------------------


@pytest.mark.parametrize("phase", ["before", "after"])
def test_crash_after_the_probe_passed_ends_in_exactly_one_capture(tmp_path, clock, phase) -> None:
    """The process dies inside the capture call: before PayPal saw it, or after PayPal made it
    but before the answer arrived. The reconciler on the next start makes it exactly one."""
    db_path = tmp_path / "ledger.db"
    state_path = tmp_path / "paypal.json"
    ledger = Ledger(db_path, clock=clock)
    fake = FakePayPal(clock=clock, state_path=state_path, hook=crash_at("capture", phase))
    orch = Orchestrator(ledger, fake, clock=clock)
    world, model, rec = start_sshd(orch, fake, *fix_script())

    with pytest.raises(SimulatedCrash):
        run(orch, rec, world, model)
    ledger.close()

    # A new process: fresh ledger connection, PayPal's state read back from its own store.
    ledger = Ledger(db_path, clock=clock)
    paypal = FakePayPal(clock=clock, state_path=state_path)
    pending = ledger.get_op(rec.id, MoneyOp.CAPTURE)
    assert pending is not None and pending.status is OutboxStatus.PENDING
    assert pending.request_id == f"cap-{rec.id}"
    assert ledger.get(rec.id).state is RescueState.PROBING
    assert len(paypal.captures_for(rec.authorization_id)) == (1 if phase == "after" else 0)

    report = Reconciler(ledger, paypal).startup()
    assert report.clean and not report.voided
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.CAPTURED
    caps = paypal.captures_for(rec.authorization_id)
    assert len(caps) == 1 and caps[0].amount == rec.quote_amount and caps[0].id == rec.capture_id
    Reconciler(ledger, paypal).run_once()
    assert len(paypal.all_captures()) == 1

    # The warranty picks up in the new process.
    decision = Orchestrator(ledger, paypal, clock=clock).resume_warranty(
        rec.id, ssh=world.server.ssh, http=world.server.http
    )
    assert decision is not None and decision.verdict.value == "ok"
    assert ledger.get(rec.id).state is RescueState.WARRANTY_OK
    assert ledger.check_invariants(rec.id) == []
    ledger.close()


def test_crash_during_the_warranty_resumes_from_the_stored_checks(orch, ledger, fake, clock) -> None:
    world, model, rec = start_sshd(orch, fake, *fix_script())
    orch.on_event = lambda rid, ev: (
        world.server.break_down() if ev.kind == "money" and ev.data.get("op") == "capture" else None
    )
    orch.config = RescueConfig(warranty=False)
    report = run(orch, rec, world, model)
    assert report.state is RescueState.CAPTURED

    # First warranty check fails, then the process stops.
    calls = {"n": 0}

    def stop_after_first(rid, ev) -> None:
        if ev.kind == "warranty" and "ok" in ev.data:
            calls["n"] += 1
            orch.shutdown()

    orch.on_event = stop_after_first
    first = orch.resume_warranty(rec.id, ssh=world.server.ssh, http=world.server.http)
    assert first is not None and not first.final and first.consecutive_failures == 1

    # A new orchestrator reads the stored failure back; one more failure is two in a row.
    again = Orchestrator(ledger, fake, clock=clock)
    decision = again.resume_warranty(rec.id, ssh=world.server.ssh, http=world.server.http)
    assert decision is not None and decision.refund
    assert ledger.get(rec.id).state is RescueState.REFUNDED


# -- webhooks ------------------------------------------------------------------------------------


def test_webhook_replays_change_nothing(orch, ledger, fake) -> None:
    world, model, rec = start_sshd(orch, fake, *fix_script())
    run(orch, rec, world, model)
    hooks = WebhookProcessor(ledger, fake, WEBHOOK_ID)
    deliveries = fake.take_deliveries(WEBHOOK_ID)
    types = [json.loads(raw)["event_type"] for _, raw in deliveries]
    assert types == ["PAYMENT.AUTHORIZATION.CREATED", "PAYMENT.CAPTURE.COMPLETED"]

    first = [hooks.handle(h, raw).outcome for h, raw in deliveries]
    assert first == ["applied", "applied"]
    after_first = ledger.get(rec.id)
    assert after_first.state is RescueState.WARRANTY_OK
    assert after_first.auth_webhook_at is not None and after_first.capture_webhook_at is not None
    state_events = len(ledger.events(rec.id, kind="state"))

    for h, raw in [*deliveries, *reversed(deliveries)]:
        assert hooks.handle(h, raw).outcome == "duplicate"
    assert ledger.get(rec.id) == after_first
    assert len(ledger.events(rec.id, kind="state")) == state_events
    assert len(fake.all_captures()) == 1


def test_a_late_authorization_event_never_reopens_a_voided_rescue(orch, ledger, fake) -> None:
    world, model, rec = start_sshd(
        orch, fake, tool_reply("give_up", {"reason": "The console does not respond to keys."})
    )
    run(orch, rec, world, model)
    assert ledger.get(rec.id).state is RescueState.VOIDED
    hooks = WebhookProcessor(ledger, fake, WEBHOOK_ID)
    deliveries = fake.take_deliveries(WEBHOOK_ID)
    assert [hooks.handle(h, raw).outcome for h, raw in deliveries] == ["applied", "applied"]
    snapshot = ledger.get(rec.id)

    created = next(json.loads(raw) for _, raw in deliveries if b"AUTHORIZATION.CREATED" in raw)
    created["id"] = "WH-RESENT-LATE-0001"  # a different event id, so it is not a duplicate
    raw = json.dumps(created).encode()
    assert hooks.handle(fake.sign_delivery(raw, WEBHOOK_ID), raw).outcome == "applied"
    for h, raw in deliveries:
        assert hooks.handle(h, raw).outcome == "duplicate"
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.VOIDED and rec.auth_status == "VOIDED"
    assert rec == snapshot


# -- PayPal trouble ------------------------------------------------------------------------------


def test_a_lost_capture_answer_is_replayed_with_the_same_request_id(orch, ledger, fake) -> None:
    world, model, rec = start_sshd(orch, fake, *fix_script())
    fake.inject("capture", "INTERNAL_SERVER_ERROR", after=True)  # PayPal captured, the answer was lost
    report = run(orch, rec, world, model)
    assert report.state is RescueState.WARRANTY_OK
    calls = [c for c in fake.calls if c.op == "capture"]
    assert [(c.request_id, c.outcome) for c in calls] == [(f"cap-{rec.id}", "ok"), (f"cap-{rec.id}", "replay")]
    assert len(fake.all_captures()) == 1


def test_a_capture_paypal_refuses_releases_the_hold(orch, ledger, fake) -> None:
    world, model, rec = start_sshd(orch, fake, *fix_script())
    fake.inject("capture", "TRANSACTION_REFUSED")
    report = run(orch, rec, world, model)
    assert report.stable is not None and report.stable.stable
    assert_voided(ledger, fake, rec.id, "PayPal refused the capture (TRANSACTION_REFUSED)")


def test_paypal_down_at_capture_leaves_it_for_the_reconciler(orch, ledger, fake) -> None:
    world, model, rec = start_sshd(orch, fake, *fix_script())
    fake.inject("capture", "INTERNAL_SERVER_ERROR", times=3)
    report = run(orch, rec, world, model)
    assert report.state is RescueState.PROBING and "reconciler" in report.reason
    assert ledger.get_op(rec.id, MoneyOp.CAPTURE).status is OutboxStatus.PENDING
    assert orch.stop(rec.id).voided is False  # a capture in flight is never voided

    Reconciler(ledger, fake, live_rescues=orch.live_rescues).run_once()
    assert ledger.get(rec.id).state is RescueState.CAPTURED
    assert len(fake.all_captures()) == 1
    decision = orch.resume_warranty(rec.id, ssh=world.server.ssh, http=world.server.http)
    assert decision is not None and ledger.get(rec.id).state is RescueState.WARRANTY_OK


def test_paypal_down_at_void_still_cuts_the_keyboard(orch, ledger, fake) -> None:
    world, model, rec = start_sshd(
        orch, fake, tool_reply("give_up", {"reason": "Cannot tell what is wrong from this screen."})
    )
    fake.inject("void", "INTERNAL_SERVER_ERROR", times=3)
    report = run(orch, rec, world, model)
    assert report.state is RescueState.ACTIVE  # the void is waiting in the outbox
    assert ledger.get_op(rec.id, MoneyOp.VOID).status is OutboxStatus.PENDING
    with pytest.raises(IllegalTransition, match="HELD"):
        run(orch, rec, world, FakeModel(fix_script()))  # no second session on this hold
    assert world.console.inputs == []

    Reconciler(ledger, fake).run_once()
    assert_voided(ledger, fake, rec.id, "the agent gave up")


def test_a_pending_authorization_never_lends_the_keyboard(orch, ledger, fake) -> None:
    fake.next_authorization_status = "PENDING"
    world = SshdWorld.build()
    model = FakeModel([sshd_quote_reply(), *fix_script()])
    _, rec = orch.free_look(world.console, model, probe=SPEC, screen_text=world.screen_text)
    fake.approve(orch.place_hold(rec.id).id)
    assert orch.confirm_hold(rec.id).state is RescueState.HOLD_PENDING
    with pytest.raises(IllegalTransition, match="HELD"):
        run(orch, rec, world, model)
    assert world.console.inputs == []


def test_the_firewall_refusal_never_reaches_the_console(orch, ledger, fake, events) -> None:
    script = fix_script()
    script.insert(2, tool_reply("type", {"text": "dd if=/dev/zero of=/dev/vda bs=1M\n"}))
    world, model, rec = start_sshd(orch, fake, *script)
    report = run(orch, rec, world, model)
    assert report.state is RescueState.WARRANTY_OK
    assert "dd if=/dev/zero" not in world.console.typed_text()
    refusals = ledger.events(rec.id, kind="agent_firewall_refusal")
    assert len(refusals) == 1 and refusals[0].data["tool"] == "type"
    assert any(ev.kind == "firewall_refusal" for _, ev in events)


def test_a_declined_capture_moves_no_money_and_starts_no_warranty(orch, ledger, fake) -> None:
    world, model, rec = start_sshd(orch, fake, *fix_script())
    fake.capture_status = "DECLINED"
    report = run(orch, rec, world, model)
    # Regression: a declined capture used to leave the rescue CAPTURED (open) for good.
    assert report.state is RescueState.VOIDED and "no money moved" in report.reason
    assert report.warranty is None
    rec2 = ledger.get(rec.id)
    assert rec2.capture_status == "DECLINED" and "nothing was charged" in rec2.end_reason
    assert ledger.check_invariants(rec.id) == []
    assert rec2 not in ledger.open_rescues()
    assert ledger.events(rec.id, kind="warranty_check") == []
    assert orch.resume_warranty(rec.id, ssh=world.server.ssh, http=world.server.http) is None


# -- review fixes ----------------------------------------------------------------------------------


@pytest.mark.parametrize(("issue", "status"), [("AUTHENTICATION_FAILURE", 401), ("RATE_LIMIT_REACHED", 429)])
def test_paypal_unable_to_answer_the_capture_never_voids_a_fixed_server(orch, ledger, fake, issue, status) -> None:
    """Regression: a token outage or a 429 at capture closed the capture as FAILED and voided the
    hold after the owner probe had passed twice."""
    world, model, rec = start_sshd(orch, fake, *fix_script())
    fake.inject("capture", issue, times=3, status=status)
    report = run(orch, rec, world, model)
    assert report.state is RescueState.PROBING and "reconciler" in report.reason
    assert ledger.get_op(rec.id, MoneyOp.CAPTURE).status is OutboxStatus.PENDING
    assert money_calls(fake, "void") == []
    Reconciler(ledger, fake, live_rescues=orch.live_rescues).run_once()
    assert ledger.get(rec.id).state is RescueState.CAPTURED and len(fake.all_captures()) == 1


def test_a_refused_capture_is_read_back_before_the_void(orch, ledger, fake) -> None:
    """PayPal captured, then answered with a refusal: the read-back finds the capture, so the
    hold is not voided and the capture is recorded."""
    world, model, rec = start_sshd(orch, fake, *fix_script())
    fake.inject("capture", "TRANSACTION_REFUSED", after=True)
    report = run(orch, rec, world, model)
    assert report.state is RescueState.WARRANTY_OK
    assert money_calls(fake, "void") == [] and len(fake.all_captures()) == 1
    assert ledger.check_invariants(rec.id) == []


def test_stop_says_what_happened_to_the_void(orch, ledger, fake) -> None:
    _, _, rec = start_sshd(orch, fake, *fix_script())
    fake.inject("void", "RATE_LIMIT_REACHED", times=5, status=429)
    result = orch.stop(rec.id)
    assert result.voided is False and result.state is RescueState.HELD
    assert f"void-{rec.id}" in result.detail and "keeps sending" in result.detail
    fake.clear_injections()
    Reconciler(ledger, fake, live_rescues=orch.live_rescues).run_once()
    assert ledger.get(rec.id).state is RescueState.VOIDED
    assert fake.get_authorization(rec.authorization_id).status == "VOIDED"


def test_a_failed_warranty_on_a_pending_capture_is_refunded_once_it_clears(orch, ledger, fake) -> None:
    world, model, rec = start_sshd(orch, fake, *fix_script())
    fake.capture_status = "PENDING"
    real_ssh = world.server.ssh

    class BreaksAfterCapture:
        """The server answers until the capture, then SSH goes down for the warranty."""

        def login(self, spec):
            if ledger.get(rec.id).state is RescueState.CAPTURED:
                world.server.ssh_up = False
            return real_ssh.login(spec)

    report = orch.run(rec.id, world.console, model, ssh=BreaksAfterCapture(), http=world.server.http,
                      console_password=CONSOLE_PASSWORD)
    assert report.state is RescueState.CAPTURED  # refund decided, but the capture is PENDING
    assert ledger.refund_due(rec.id) and money_calls(fake, "refund") == []
    cap = fake.all_captures()[0]
    fake.complete_capture(cap.id)
    Reconciler(ledger, fake, live_rescues=orch.live_rescues).run_once()
    final = ledger.get(rec.id)
    assert final.state is RescueState.REFUNDED and final.refunded_amount == final.captured_amount
    assert [c.request_id for c in money_calls(fake, "refund")] == [f"refund-{rec.id}"]


# -- the goal names the server's own SSH port ------------------------------------------------------


def _goal_text(model) -> str:
    content = model.calls[1].messages[0]["content"]
    return " ".join(part["text"] for part in content if part.get("type") == "text")


def test_the_goal_names_the_servers_own_ssh_port_when_given(orch, ledger, fake) -> None:
    world, model, rec = start_sshd(orch, fake, *fix_script())
    run(orch, rec, world, model, server_ssh_port=2200)
    goal = _goal_text(model)
    assert "port 2200 (its own port" in goal
    assert "Do not change the SSH port" in goal


def test_the_goal_falls_back_to_the_probe_port(orch, ledger, fake) -> None:
    world, model, rec = start_sshd(orch, fake, *fix_script())
    run(orch, rec, world, model)
    spec_port = ledger.get(rec.id).probe_spec["ssh_port"]
    assert f"port {spec_port} (its own port" in _goal_text(model)


def test_agent_rules_forbid_unneeded_setting_changes() -> None:
    from keyhold.agent.loop import system_prompt

    assert "Do not change ports, listen addresses, users or other settings" in system_prompt(30)
