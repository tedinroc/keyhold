"""Fixtures for the web app tests: a TestClient over a fresh ledger, FakePayPal, the simulated
worlds and a FakeClock. Background threads are off; work runs inline unless a test asks for
threads. No network: PayPal, the console, the model and the probe are all fakes."""

from __future__ import annotations

import contextlib
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

from keyhold.pay.client import FakePayPal
from web.app import create_app
from web.config import Settings
from web.runs import InlineExecutor, ThreadExecutor
from web.services import Services, build_services
from web.worlds import SimulatedWorlds, World

CSRF_RE = re.compile(r'data-csrf="([^"]+)"')


@dataclass
class Web:
    client: TestClient
    svc: Services

    @property
    def fake(self) -> FakePayPal:
        assert self.svc.fake is not None
        return self.svc.fake

    def csrf(self, client: TestClient | None = None) -> str:
        page = (client or self.client).get("/try")
        match = CSRF_RE.search(page.text)
        assert match, "no CSRF token on /try"
        return match.group(1)

    def headers(self, client: TestClient | None = None) -> dict[str, str]:
        return {"X-Keyhold-CSRF": self.csrf(client)}

    def start(self, fault: str = "sshd-config-typo", client: TestClient | None = None) -> Any:
        client = client or self.client
        response = client.post("/try/start", data={"csrf": self.csrf(client), "fault": fault}, follow_redirects=False)
        assert response.status_code == 303, response.text
        run_id = response.headers["location"].rsplit("/", 1)[-1]
        return self.svc.runs.get(run_id)

    def order(self, run: Any, client: TestClient | None = None) -> str:
        response = (client or self.client).post(
            "/api/orders", json={"rescueId": run.rescue_id}, headers=self.headers(client)
        )
        assert response.status_code == 201, response.text
        return response.json()["orderId"]

    def hold(self, run: Any, client: TestClient | None = None) -> dict[str, Any]:
        """Order, simulated approval, authorize: what the offline checkout button does."""
        client = client or self.client
        order_id = self.order(run, client)
        approved = client.post(f"/api/offline/orders/{order_id}/approve", headers=self.headers(client))
        assert approved.status_code == 200, approved.text
        response = client.post(f"/api/orders/{order_id}/authorize", headers=self.headers(client))
        assert response.status_code == 200, response.text
        return response.json()


def settings_for(tmp_path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "mode": "offline",
        "world": "simulated",
        "data_dir": tmp_path,
        "db_path": tmp_path / "keyhold.db",
        "background": False,
        "sse_poll_s": 0.01,
        "sse_tail_s": 0.0,
        "sse_max_s": 5.0,
        "offline_webhook_delay_s": 0.0,
        "secret_key": "test-secret-key-not-real-0123456789",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def make_web(tmp_path, clock) -> Callable[..., Web]:
    made: list[Web] = []

    def make(
        *,
        gateway: Any = None,
        worlds: Any = None,
        threaded: bool = False,
        **overrides: Any,
    ) -> Web:
        settings = settings_for(tmp_path, **overrides)
        if settings.mode == "offline" and gateway is None:
            gateway = FakePayPal(clock=clock)
        svc = build_services(
            settings,
            clock=clock,
            gateway=gateway,
            worlds=worlds or SimulatedWorlds(),
            executor=ThreadExecutor() if threaded else InlineExecutor(),
        )
        web = Web(TestClient(create_app(services=svc)), svc)
        made.append(web)
        return web

    yield make
    for web in made:
        with contextlib.suppress(Exception):
            web.svc.runs.shutdown()
        web.svc.ledger.close()


@pytest.fixture
def web(make_web) -> Web:
    return make_web()


class GatedWorlds:
    """Simulated worlds whose agent stops before a chosen turn until the test releases it, so a
    test can look at (or stop) a rescue while the agent holds the keyboard."""

    kind = "simulated"
    paid_model = False

    def __init__(self, pause_before: int = 3) -> None:
        self.inner = SimulatedWorlds()
        self.pause_before = pause_before
        self.paused = threading.Event()
        self.release = threading.Event()
        self.worlds: list[World] = []

    def problems(self) -> list[str]:
        return []

    def boot(self, fault_id: str, run_id: str, progress: Any) -> World:
        world = self.inner.boot(fault_id, run_id, progress)
        steps = world.model.script  # FakeModel: [free look, agent turns...]
        index = self.pause_before

        def wait(request: Any, step: Any = steps[index]) -> Any:
            self.paused.set()
            assert self.release.wait(10), "the test never released the agent"
            return step

        steps[index] = wait
        self.worlds.append(world)
        return world


class PaidWorlds:
    """Simulated worlds that report a paid model, with every reply priced at ``tokens`` output
    tokens, to exercise the spend cap without calling a model."""

    kind = "vm"
    paid_model = True

    def __init__(self, output_tokens: int = 50_000) -> None:
        self.inner = SimulatedWorlds()
        self.output_tokens = output_tokens

    def problems(self) -> list[str]:
        return []

    def boot(self, fault_id: str, run_id: str, progress: Any) -> World:
        world = self.inner.boot(fault_id, run_id, progress)
        world.paid_model = True
        inner = world.model
        tokens = self.output_tokens

        class Priced:
            model = "claude-opus-5-5"

            def respond(self, **kwargs: Any) -> Any:
                from dataclasses import replace

                reply = inner.respond(**kwargs)
                return replace(reply, input_tokens=1000, output_tokens=tokens)

        world.model = Priced()
        return world
