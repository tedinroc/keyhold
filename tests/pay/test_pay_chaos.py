"""Chaos: kill a real process (SIGKILL) between the outbox write and the PayPal response, then prove
that the reconciler, in a fresh process view, ends with exactly one capture.

The ledger is a SQLite file and PayPal is a FakePayPal that persists its state to a JSON file, so a
child process can make the call and die, and this process reads both sides back afterwards. The
child is ``chaos_child.py``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from pay_helpers import WEBHOOK_ID, to_probing

from keyhold.pay.client import FakePayPal
from keyhold.pay.ledger import Ledger, Payments
from keyhold.pay.reconciler import Reconciler
from keyhold.pay.webhooks import WebhookProcessor
from keyhold.types import MoneyOp, OutboxStatus, RescueState

CHILD = Path(__file__).with_name("chaos_child.py")

pytestmark = pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="needs POSIX signals")


def run_child(db: Path, state: Path, rescue_id: str, mode: str, point: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CHILD), str(db), str(state), rescue_id, mode, point],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def setup(tmp_path: Path) -> tuple[Path, Path, str]:
    db, state = tmp_path / "ledger.db", tmp_path / "paypal.json"
    fake = FakePayPal(state_path=state)
    ledger = Ledger(db)
    rec = to_probing(ledger, Payments(ledger, fake), fake)
    ledger.close()
    return db, state, rec.id


def assert_killed(proc: subprocess.CompletedProcess) -> None:
    assert proc.returncode == -signal.SIGKILL, (proc.returncode, proc.stdout, proc.stderr)


def assert_exactly_one_capture(db: Path, state: Path, rescue_id: str) -> None:
    ledger = Ledger(db)
    fake = FakePayPal(state_path=state)
    try:
        (capture,) = fake.all_captures()
        rec = ledger.get(rescue_id)
        assert rec.state is RescueState.CAPTURED
        assert rec.capture_id == capture.id and rec.captured_amount == capture.amount == rec.quote_amount
        assert capture.authorization_id == rec.authorization_id and capture.final_capture
        entry = ledger.get_op(rescue_id, MoneyOp.CAPTURE)
        assert entry.status is OutboxStatus.DONE and entry.request_id == f"cap-{rescue_id}"
        assert ledger.check_invariants(rescue_id) == []

        # Nothing can make a second one: another reconcile, the orchestrator retrying, a void
        # attempt, and PayPal's webhooks arriving (twice).
        assert Reconciler(ledger, fake).startup().replayed == []
        assert Payments(ledger, fake).capture(rescue_id).id == capture.id
        with pytest.raises(Exception, match="never void after capture"):
            Payments(ledger, fake).void(rescue_id, "late Stop")
        hooks = WebhookProcessor(ledger, fake, WEBHOOK_ID)
        deliveries = fake.take_deliveries(WEBHOOK_ID)
        for headers, raw in deliveries + deliveries:
            assert hooks.handle(headers, raw).outcome in ("applied", "duplicate")
        assert len(fake.all_captures()) == 1
        assert ledger.get(rescue_id).state is RescueState.CAPTURED
    finally:
        ledger.close()


@pytest.mark.parametrize("point", ["before", "after", "commit"])
def test_sigkill_during_capture_then_reconcile(tmp_path: Path, point: str) -> None:
    db, state, rescue_id = setup(tmp_path)

    proc = run_child(db, state, rescue_id, "capture", point)
    assert_killed(proc)

    # What the dead process left behind.
    ledger = Ledger(db)
    fake = FakePayPal(state_path=state)
    entry = ledger.get_op(rescue_id, MoneyOp.CAPTURE)
    assert entry.status is OutboxStatus.PENDING and entry.request_id == f"cap-{rescue_id}"
    rec = ledger.get(rescue_id)
    assert rec.state is RescueState.PROBING and rec.capture_id is None  # half-written commit rolled back
    assert len(fake.all_captures()) == (0 if point == "before" else 1)

    # Restart: the reconciler replays the capture with the same PayPal-Request-Id.
    report = Reconciler(ledger, fake).startup()
    assert report.replayed == [f"{rescue_id} capture: DONE"]
    assert report.voided == []  # a pending capture is never mistaken for an orphaned hold
    ledger.close()

    assert_exactly_one_capture(db, state, rescue_id)


@pytest.mark.parametrize(
    "kills",
    [
        [("capture", "before"), ("reconcile", "after")],
        [("capture", "after"), ("reconcile", "after"), ("reconcile", "commit")],
        [("capture", "commit"), ("reconcile", "commit"), ("reconcile", "commit")],
    ],
    ids=["paypal-reached-on-replay", "killed-three-times", "commit-never-lands-twice"],
)
def test_repeated_sigkill_still_one_capture(tmp_path: Path, kills: list[tuple[str, str]]) -> None:
    db, state, rescue_id = setup(tmp_path)
    for mode, point in kills:
        assert_killed(run_child(db, state, rescue_id, mode, point))
        fake = FakePayPal(state_path=state)
        assert len(fake.all_captures()) <= 1
    ledger = Ledger(db)
    fake = FakePayPal(state_path=state)
    Reconciler(ledger, fake).startup()
    ledger.close()
    assert_exactly_one_capture(db, state, rescue_id)


def test_child_reaches_the_end_without_a_kill_point(tmp_path: Path) -> None:
    """Guard for the harness itself: with no kill point hit, the child exits normally (code 3)."""
    db, state, rescue_id = setup(tmp_path)
    proc = run_child(db, state, rescue_id, "capture", "never")
    assert proc.returncode == 3, proc.stderr
    assert_exactly_one_capture(db, state, rescue_id)
