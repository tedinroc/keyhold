"""Fixtures for the PayPal-side tests. Helpers live in ``pay_helpers``.

Everything runs against FakePayPal (or stub transports for PayPalClient); nothing reaches PayPal.
"""

from __future__ import annotations

import pytest

from keyhold.pay.client import FakePayPal
from keyhold.pay.ledger import Ledger, Payments


@pytest.fixture
def fake(clock) -> FakePayPal:
    return FakePayPal(clock=clock)


@pytest.fixture
def ledger(db_path, clock):
    led = Ledger(db_path, clock=clock)
    yield led
    led.close()


@pytest.fixture
def pay(ledger, fake) -> Payments:
    return Payments(ledger, fake)
