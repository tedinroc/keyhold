"""Helpers for the PayPal-side tests: quotes, probe specs and driving a rescue through the ledger."""

from __future__ import annotations

from keyhold.pay.client import FakePayPal
from keyhold.pay.ledger import Ledger, Payments, RescueRecord
from keyhold.types import ProbeSpec, Quote, RescueState, Tier

WEBHOOK_ID = "WH-TEST-0001"


class SimulatedCrash(BaseException):
    """Raised from a FakePayPal hook to stand in for the process dying at that point.

    A BaseException, so no ``except Exception`` in the code under test can swallow it.
    """


def make_quote(tier: Tier = Tier.T2) -> Quote:
    return Quote.build(
        "sshd_config_error",
        ["sshd[412]: /etc/ssh/sshd_config line 14: Bad configuration option: PermitRootLogn"],
        tier,
        "Fix the misspelled option on line 14 and restart sshd.",
    )


def make_spec(n: int = 0) -> ProbeSpec:
    return ProbeSpec(
        host=f"server{n}.test",
        ssh_port=22,
        ssh_user="probe",
        probe_public_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleOnlyNotARealKey probe",
        url=f"http://server{n}.test/health",
        expect_substring="ok",
    )


def new_rescue(ledger: Ledger, tier: Tier = Tier.T2, n: int = 0) -> RescueRecord:
    return ledger.create_rescue(make_quote(tier), probe=make_spec(n))


def to_held(ledger: Ledger, pay: Payments, fake: FakePayPal, tier: Tier = Tier.T2) -> RescueRecord:
    rec = new_rescue(ledger, tier)
    order = pay.create_hold(rec.id)
    fake.approve(order.id)
    pay.authorize(rec.id)
    rec = ledger.get(rec.id)
    assert rec.state is RescueState.HELD
    return rec


def to_probing(ledger: Ledger, pay: Payments, fake: FakePayPal, tier: Tier = Tier.T2) -> RescueRecord:
    rec = to_held(ledger, pay, fake, tier)
    ledger.transition(rec.id, RescueState.ACTIVE)
    ledger.transition(rec.id, RescueState.PROBING)
    return ledger.get(rec.id)


def crash_at(op: str, phase: str):
    """A FakePayPal hook that raises SimulatedCrash once, at ``op``/``phase``."""
    fired = {"done": False}

    def hook(call_op: str, call_phase: str, _request_id: str | None) -> None:
        if not fired["done"] and call_op == op and call_phase == phase:
            fired["done"] = True
            raise SimulatedCrash(f"{op} {phase}")

    return hook


class StepClock:
    """A Clock that moves only when told to (for hypothesis tests, which cannot use fixtures)."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.t = float(start)

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(0.0, float(seconds))

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)
