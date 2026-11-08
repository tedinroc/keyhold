"""Fixtures for end-to-end rescue tests. Everything is a fake: PayPal, console, model, probe."""

from __future__ import annotations

import pytest

from keyhold.pay.client import FakePayPal
from keyhold.pay.ledger import Ledger
from keyhold.rescue import Orchestrator, RescueConfig


@pytest.fixture
def fake(clock) -> FakePayPal:
    return FakePayPal(clock=clock)


@pytest.fixture
def ledger(db_path, clock):
    led = Ledger(db_path, clock=clock)
    yield led
    led.close()


@pytest.fixture
def events() -> list:
    return []


@pytest.fixture
def orch(ledger, fake, clock, events) -> Orchestrator:
    return Orchestrator(ledger, fake, clock=clock, config=RescueConfig(), on_event=lambda rid, ev: events.append((rid, ev)))
