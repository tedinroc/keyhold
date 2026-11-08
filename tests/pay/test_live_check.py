"""tools/live_check.py run end to end against FakePayPal, with a fake buyer approval. No network.

The live check is meant for the PayPal sandbox; here every step runs through the same code with
FakePayPal behind it, so the steps, the report and the failure handling are tested without an
account. Variants of FakePayPal break one rule each, to show that the check reports a FAIL
instead of passing anyway.
"""

from __future__ import annotations

import base64
import dataclasses
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from keyhold.pay.client import FakePayPal
from keyhold.pay.ledger import Ledger
from keyhold.types import RescueState

ROOT = Path(__file__).resolve().parents[2]
WEBHOOK_ID = "WH-LIVE-CHECK-TEST"
SECRET = "sandbox-secret-value-do-not-print"

STEPS = [
    "sandbox_only",
    "client_token",
    "hold1_order",
    "hold1_approved",
    "hold1_authorized",
    "hold1_lease_grants",
    "capture_7_final",
    "auth_after_final_capture",
    "capture_replay_same_id",
    "second_capture_refused",
    "void_after_capture_refused",
    "refund_2",
    "hold2_order",
    "hold2_approved",
    "hold2_authorized",
    "hold2_voided",
    "mock_authorize_INSTRUMENT_DECLINED",
    "mock_capture_AUTHORIZATION_EXPIRED",
    "mock_void_PREVIOUSLY_CAPTURED",
    "mock_refund_CAPTURE_FULLY_REFUNDED",
    "mocks_moved_no_money",
    "ledger_invariants",
    "webhook_verified",
]


@pytest.fixture(scope="module")
def live_check():
    spec = importlib.util.spec_from_file_location("keyhold_tools_live_check", ROOT / "tools" / "live_check.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def buyer(fake: FakePayPal, seen: list | None = None):
    """The fake approval step: the buyer approves each order as soon as its link is shown."""

    def approve(hold_no, order, link):
        if seen is not None:
            seen.append((hold_no, order.id, link))
        fake.approve(order.id)

    return approve


def nobody(hold_no, order, link):
    """A buyer who never approves."""


def run(live_check, out: Path, fake, clock, *args, approve=None, environ=None):
    lines: list[str] = []
    code = live_check.main(
        ["--out", str(out), *args],
        environ=environ if environ is not None else {},
        gateway=fake,
        approve=approve if approve is not None else buyer(fake),
        clock=clock,
        say=lines.append,
    )
    (run_dir,) = out.iterdir()
    report = json.loads((run_dir / "report.json").read_text("utf-8"))
    return code, report, run_dir, lines


def by_name(report: dict) -> dict:
    return {s["name"]: s for s in report["steps"]}


def statuses(report: dict) -> dict:
    return {s["name"]: s["status"] for s in report["steps"]}


# ---------------------------------------------------------------------------------------------
# The whole check
# ---------------------------------------------------------------------------------------------


def test_full_run_passes_every_step_and_moves_only_the_money_it_says(live_check, tmp_path, clock) -> None:
    fake = FakePayPal(clock=clock)
    seen: list = []
    code, report, run_dir, lines = run(live_check, tmp_path / "runs", fake, clock, approve=buyer(fake, seen))

    assert code == 0
    assert [s["name"] for s in report["steps"]] == STEPS
    assert {n: s for n, s in statuses(report).items() if s != "PASS"} == {"webhook_verified": "SKIP"}
    assert report["result"] == "PASS" and report["counts"] == {"PASS": 22, "FAIL": 0, "SKIP": 1}
    assert report["target"] == "fake" and "not the PayPal sandbox" in report["target_detail"]
    assert [hold_no for hold_no, _, _ in seen] == [1, 2]

    steps = by_name(report)
    assert steps["auth_after_final_capture"]["data"]["status"] == "CAPTURED"
    assert steps["auth_after_final_capture"]["data"]["status_details"] is None
    assert steps["auth_after_final_capture"]["data"]["same_as_ledger"] is True
    assert steps["second_capture_refused"]["data"]["issue"] == "AUTHORIZATION_ALREADY_CAPTURED"
    assert steps["second_capture_refused"]["data"]["refused"] is True
    assert steps["void_after_capture_refused"]["data"]["issue"] == "PREVIOUSLY_CAPTURED"
    assert steps["hold2_voided"]["data"]["status"] == "VOIDED"
    assert steps["hold1_lease_grants"]["data"]["matches"] == {"order_id": True, "custom_id": True, "invoice_id": True}
    for name in STEPS:
        if name.startswith("mock_"):
            data = steps[name]["data"]
            assert data["mechanism"] == "FakePayPal.inject" and data["issue"] == data["asked_for"]
    # The read-back around the mocks covers order 1's authorizations too (the authorize mock's target).
    before = steps["mocks_moved_no_money"]["data"]["before"]
    assert [status for _, status, _ in before["order1_authorizations"]] == ["CAPTURED"]
    assert len(before["order1_captures"]) == 1 and len(before["order1_refunds"]) == 1

    # What FakePayPal holds afterwards: one $7.00 final capture, $5.00 released, one $2.00
    # refund, and the second hold voided with nothing captured.
    hold1, hold2 = report["rescues"]["hold1"], report["rescues"]["hold2"]
    (cap,) = fake.captures_for(hold1["authorization_id"])
    assert cap.amount == Decimal("7.00") and cap.final_capture is True
    assert fake.authorization_released(hold1["authorization_id"]) == Decimal("5.00")
    (refund,) = fake.refunds_for(cap.id)
    assert refund.amount == Decimal("2.00") and refund.status == "COMPLETED"
    assert fake.get_authorization(hold2["authorization_id"]).status == "VOIDED"
    assert fake.captures_for(hold2["authorization_id"]) == []
    # Replays answered from PayPal's request-id store; the mocked calls changed nothing.
    assert [len(fake.money_calls(op)) for op in ("create_hold", "authorize", "capture", "void", "refund")] == [
        2, 2, 1, 1, 1,
    ]

    # The ledger path: outbox rows with Keyhold's request ids, and the states they led to.
    rid1, rid2 = hold1["rescue_id"], hold2["rescue_id"]
    assert [(e["op"], e["request_id"], e["status"]) for e in hold1["outbox"]] == [
        ("create_order", f"order-{rid1}", "DONE"),
        ("authorize", f"auth-{rid1}", "DONE"),
        ("capture", f"cap-{rid1}", "DONE"),
    ]
    assert hold1["state"] == "CAPTURED" and hold1["captured_amount"] == "7.00" and hold1["refunded_amount"] == "2.00"
    assert hold1["quote_amount"] == "12.00"
    assert [e["request_id"] for e in hold2["outbox"]] == [f"order-{rid2}", f"auth-{rid2}", f"void-{rid2}"]
    assert hold2["state"] == "VOIDED"
    ledger = Ledger(run_dir / "ledger.sqlite3", clock=clock)
    try:
        assert ledger.get(rid1).state is RescueState.CAPTURED and ledger.check_invariants(rid1) == []
        assert ledger.get(rid2).state is RescueState.VOIDED and ledger.check_invariants(rid2) == []
    finally:
        ledger.close()

    summary = (run_dir / "summary.md").read_text("utf-8")
    assert "FakePayPal in memory (a test run, not the PayPal sandbox)" in summary
    assert "| capture_7_final | PASS |" in summary
    assert "status `CAPTURED`" in summary
    assert any(line.startswith("PASS  hold1_authorized") for line in lines)
    assert any(line.startswith("SKIP  webhook_verified") for line in lines)
    assert report["cleanup"] == []


def test_approval_is_polled_until_the_order_reads_approved(live_check, tmp_path, clock) -> None:
    class SlowBuyer(FakePayPal):
        """The buyer approves while the check is polling: on the third read of each order."""

        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.reads: dict[str, int] = {}

        def get_order(self, order_id):
            self.reads[order_id] = self.reads.get(order_id, 0) + 1
            if self.reads[order_id] == 3:
                self.approve(order_id)
            return super().get_order(order_id)

    fake = SlowBuyer(clock=clock)
    code, report, _, lines = run(live_check, tmp_path / "runs", fake, clock, "--poll-interval", "5", approve=nobody)

    assert code == 0
    approved = by_name(report)["hold1_approved"]["data"]
    assert approved["polls"] == 3 and approved["waited_s"] == 10.0
    assert approved["statuses_seen"] == ["CREATED", "APPROVED"]
    assert 5.0 in clock.sleeps
    assert any("waiting for the buyer to approve it" in line for line in lines)


def test_no_approval_times_out_skips_what_depends_on_it_and_moves_no_money(live_check, tmp_path, clock) -> None:
    fake = FakePayPal(clock=clock)
    code, report, _, _ = run(
        live_check, tmp_path / "runs", fake, clock, "--approval-timeout", "30", "--poll-interval", "10",
        approve=nobody,
    )

    assert code == 1 and report["result"] == "FAIL"
    steps = by_name(report)
    for name in ("hold1_approved", "hold2_approved"):
        assert steps[name]["status"] == "FAIL"
        assert "was not approved within 30 s (last status CREATED)" in steps[name]["detail"]
    skipped = steps["hold1_authorized"]
    assert (skipped["status"], skipped["detail"]) == ("SKIP", "needs hold1_approved")
    for name in ("capture_7_final", "refund_2", "hold2_voided", "mock_capture_AUTHORIZATION_EXPIRED",
                 "mocks_moved_no_money"):
        assert steps[name]["status"] == "SKIP"
    assert steps["ledger_invariants"]["status"] == "PASS"
    # The two unapproved orders are closed in the ledger; nothing was ever authorized.
    assert report["cleanup"] == ["hold 1: HOLD_PENDING -> VOIDED", "hold 2: HOLD_PENDING -> VOIDED"]
    assert fake.money_calls("authorize") == [] and fake.all_authorizations() == []


# ---------------------------------------------------------------------------------------------
# A PayPal that breaks a rule is reported, not passed
# ---------------------------------------------------------------------------------------------


def test_mocks_that_are_ignored_fail_and_move_no_money(live_check, tmp_path, clock) -> None:
    class DeafToMocks(FakePayPal):
        """Accepts PayPal-Mock-Response requests and ignores them, as a sandbox might."""

        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.asked: list[tuple[str, str]] = []

        def mock_next(self, op: str, issue: str) -> None:
            self.asked.append((op, issue))

    fake = DeafToMocks(clock=clock)
    code, report, _, _ = run(live_check, tmp_path / "runs", fake, clock)

    assert code == 1
    steps = by_name(report)
    natural = {
        "mock_authorize_INSTRUMENT_DECLINED": "ORDER_ALREADY_AUTHORIZED",
        "mock_capture_AUTHORIZATION_EXPIRED": "AUTHORIZATION_VOIDED",
        "mock_void_PREVIOUSLY_CAPTURED": "PREVIOUSLY_VOIDED",
        "mock_refund_CAPTURE_FULLY_REFUNDED": "REFUND_AMOUNT_EXCEEDED",
    }
    for name, issue in natural.items():
        assert steps[name]["status"] == "FAIL"
        assert steps[name]["data"]["mechanism"] == "PayPal-Mock-Response"
        assert f"FakePayPal answered {issue}" in steps[name]["detail"]
    assert len(fake.asked) == 4
    # Each mocked call was aimed at a payment that could not move money anyway.
    assert steps["mocks_moved_no_money"]["status"] == "PASS"
    assert len(fake.money_calls("capture")) == 1 and len(fake.money_calls("refund")) == 1
    assert {n for n, s in statuses(report).items() if s == "FAIL"} == set(natural)


def test_a_final_capture_that_releases_nothing_is_reported(live_check, tmp_path, clock) -> None:
    class KeepsTheRest(FakePayPal):
        """Answers final_capture true but keeps the rest of the hold open for another capture."""

        def capture(self, auth_id, amount, final_capture, invoice_id, request_id):
            cap = super().capture(auth_id, amount, False, invoice_id, request_id)
            return dataclasses.replace(cap, final_capture=final_capture)

    fake = KeepsTheRest(clock=clock)
    code, report, _, _ = run(live_check, tmp_path / "runs", fake, clock)

    assert code == 1
    steps = by_name(report)
    assert steps["second_capture_refused"]["status"] == "FAIL"
    assert "FakePayPal accepted a second capture" in steps["second_capture_refused"]["detail"]
    # The exact status is recorded, and it differs from what the ledger assumed.
    after = steps["auth_after_final_capture"]["data"]
    assert after["status"] == "PARTIALLY_CAPTURED" and after["ledger_auth_status"] == "CAPTURED"
    assert after["same_as_ledger"] is False
    assert "the ledger recorded CAPTURED, which differs" in steps["auth_after_final_capture"]["detail"]
    assert {n for n, s in statuses(report).items() if s == "FAIL"} == {"second_capture_refused"}


def test_an_authorization_still_open_after_the_final_capture_is_reported_exactly(live_check, tmp_path, clock) -> None:
    class StillCreated(FakePayPal):
        """GET authorization keeps reading CREATED, with a reason, after the final capture."""

        def get_authorization_json(self, auth_id):
            body = super().get_authorization_json(auth_id)
            if body.get("status") == "CAPTURED":
                body = {**body, "status": "CREATED", "status_details": {"reason": "PENDING_REVIEW"}}
            return body

    fake = StillCreated(clock=clock)
    code, report, run_dir, _ = run(live_check, tmp_path / "runs", fake, clock)

    assert code == 1
    step = by_name(report)["auth_after_final_capture"]
    assert step["status"] == "FAIL"
    assert step["data"]["status"] == "CREATED" and step["data"]["status_details"] == {"reason": "PENDING_REVIEW"}
    assert '"reason": "PENDING_REVIEW"' in step["detail"]
    # The summary lists PayPal's exact answer for a failed step too.
    summary = (run_dir / "summary.md").read_text("utf-8")
    assert "Authorization after the final capture (FAIL): status `CREATED`" in summary
    assert "PENDING_REVIEW" in summary


# ---------------------------------------------------------------------------------------------
# Webhook verification
# ---------------------------------------------------------------------------------------------


def _one_delivery(fake: FakePayPal) -> tuple[dict, bytes]:
    order = fake.create_hold(Decimal("12.00"), "spec-hash", "keyhold-webhook-test", "order-webhook-test")
    fake.approve(order.id)
    fake.authorize(order.id, "auth-webhook-test")
    (delivery,) = fake.take_deliveries(WEBHOOK_ID)
    return delivery


@pytest.mark.parametrize("source", ["json", "json_b64", "ledger"])
def test_webhook_only_verifies_a_received_delivery(live_check, tmp_path, clock, source) -> None:
    fake = FakePayPal(clock=clock)
    headers, raw = _one_delivery(fake)
    if source == "ledger":
        path = tmp_path / "app.db"
        app_ledger = Ledger(path, clock=clock)
        app_ledger.store_delivery(headers, raw)
        app_ledger.close()
    else:
        path = tmp_path / "delivery.json"
        body = {"body": raw.decode("utf-8")} if source == "json" else {"body_b64": base64.b64encode(raw).decode()}
        path.write_text(json.dumps({"headers": headers, **body}), "utf-8")

    code, report, _, _ = run(
        live_check, tmp_path / "runs", fake, clock, "--webhook-only", "--webhook-event", str(path),
        environ={"PAYPAL_WEBHOOK_ID": WEBHOOK_ID},
    )

    assert code == 0
    assert statuses(report) == {"sandbox_only": "PASS", "webhook_verified": "PASS"}
    data = by_name(report)["webhook_verified"]["data"]
    assert data["verified"] is True and data["tampered_copy_verified"] is False
    assert data["event_type"] == "PAYMENT.AUTHORIZATION.CREATED"
    assert data["source"] == ("ledger" if source == "ledger" else "file")
    assert report["rescues"] == {}


def test_the_tampered_copy_differs_in_content_not_only_in_spacing(live_check, tmp_path, clock) -> None:
    class ReadsTheJson(FakePayPal):
        """Verifies the event after re-reading its JSON, so spacing alone does not change the answer."""

        def verify_webhook(self, headers, raw, webhook_id):
            try:
                compact = json.dumps(json.loads(raw), separators=(",", ":")).encode("utf-8")
            except ValueError:
                return False
            return super().verify_webhook(headers, compact, webhook_id)

    fake = ReadsTheJson(clock=clock)
    headers, raw = _one_delivery(fake)
    assert fake.verify_webhook(headers, raw.replace(b"{", b"{ ", 1), WEBHOOK_ID)  # spacing alone would pass
    path = tmp_path / "delivery.json"
    path.write_text(json.dumps({"headers": headers, "body": raw.decode("utf-8")}), "utf-8")

    code, report, _, _ = run(
        live_check, tmp_path / "runs", fake, clock, "--webhook-only", "--webhook-event", str(path),
        environ={"PAYPAL_WEBHOOK_ID": WEBHOOK_ID},
    )

    assert code == 0
    data = by_name(report)["webhook_verified"]["data"]
    assert data["tampered_copy"] == "one character added to the event id"
    assert data["verified"] is True and data["tampered_copy_verified"] is False


def test_a_delivery_for_another_webhook_id_fails(live_check, tmp_path, clock) -> None:
    fake = FakePayPal(clock=clock)
    headers, raw = _one_delivery(fake)
    path = tmp_path / "delivery.json"
    path.write_text(json.dumps({"headers": headers, "body": raw.decode("utf-8")}), "utf-8")

    code, report, _, _ = run(
        live_check, tmp_path / "runs", fake, clock, "--webhook-only", "--webhook-event", str(path),
        environ={"PAYPAL_WEBHOOK_ID": "WH-SOMEONE-ELSE"},
    )

    assert code == 1
    assert by_name(report)["webhook_verified"]["status"] == "FAIL"


# ---------------------------------------------------------------------------------------------
# Refusing to start
# ---------------------------------------------------------------------------------------------


def test_refuses_without_credentials_or_outside_the_sandbox(live_check, tmp_path) -> None:
    out = tmp_path / "runs"
    cases = [
        ({}, [], "PAYPAL_CLIENT_ID"),
        ({"PAYPAL_CLIENT_ID": "cid", "PAYPAL_CLIENT_SECRET": SECRET, "PAYPAL_MODE": "live"}, [], "PAYPAL_MODE"),
        ({"PAYPAL_CLIENT_ID": "cid", "PAYPAL_CLIENT_SECRET": SECRET,
          "PAYPAL_BASE_URL": "https://api-m.paypal.com"}, [], "PAYPAL_BASE_URL"),
        ({"PAYPAL_CLIENT_ID": "cid", "PAYPAL_CLIENT_SECRET": SECRET}, ["--webhook-only"], "--webhook-event"),
    ]
    for environ, args, named in cases:
        lines: list[str] = []
        code = live_check.main(["--out", str(out), *args], environ=environ, say=lines.append)
        assert code == 2
        assert lines and lines[0].startswith("refused:") and named in lines[0]
        assert all(SECRET not in line for line in lines)
    assert not out.exists()


def test_sandbox_values_are_accepted(live_check) -> None:
    assert live_check.environment_problem({}) is None
    assert live_check.environment_problem({"PAYPAL_MODE": "sandbox", "PAYPAL_ENVIRONMENT": "Sandbox"}) is None
    assert live_check.environment_problem({"PAYPAL_BASE_URL": "https://api-m.sandbox.paypal.com"}) is None
    assert live_check.environment_problem({"PAYPAL_ENV": "production"}) is not None
