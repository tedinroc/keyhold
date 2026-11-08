"""Child process for test_pay_chaos.py: make one capture attempt and die by SIGKILL at a chosen point.

Usage: python chaos_child.py DB PAYPAL_STATE RESCUE_ID MODE POINT

MODE   capture    the orchestrator captures after the probe passed (Payments.capture)
       reconcile  the reconciler's startup pass replays the pending capture
POINT  before     the outbox row is committed, PayPal has not seen the call
       after      PayPal made the capture and saved it, the response never arrives
       commit     the response arrived and the ledger transaction is half written

Not collected by pytest (no test_ prefix). Blocks the network like the test suite does.
"""

from __future__ import annotations

import os
import signal
import socket
import sys


def _blocked(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("the chaos child may not open network connections")


socket.socket.connect = _blocked  # type: ignore[method-assign]
socket.socket.connect_ex = _blocked  # type: ignore[method-assign]

from keyhold.pay.client import FakePayPal  # noqa: E402
from keyhold.pay.ledger import Ledger, Payments  # noqa: E402
from keyhold.pay.reconciler import Reconciler  # noqa: E402


def die() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


def main(db: str, paypal_state: str, rescue_id: str, mode: str, point: str) -> int:
    def hook(op: str, phase: str, _request_id: str | None) -> None:
        if op == "capture" and phase == point:
            die()

    fake = FakePayPal(state_path=paypal_state, hook=hook if point in ("before", "after") else None)
    if point == "commit":
        original = Ledger.record_capture

        def record_then_die(self: Ledger, *args: object, **kwargs: object):
            original(self, *args, **kwargs)  # written inside the open transaction, not committed
            die()

        Ledger.record_capture = record_then_die  # type: ignore[method-assign]

    ledger = Ledger(db)
    pay = Payments(ledger, fake)
    if mode == "capture":
        pay.capture(rescue_id)
    elif mode == "reconcile":
        Reconciler(ledger, fake, payments=pay).startup()
    else:
        return 2
    return 3  # reaching this line means the kill point was never hit


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:6]))
