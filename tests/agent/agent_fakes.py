"""Test doubles for the agent tests: a recording console, a stub lease and a gate like GatedConsole."""

from __future__ import annotations

import io

from PIL import Image

from keyhold.types import HoldRevoked


def make_png(color: tuple[int, int, int] = (0, 0, 0), size: tuple[int, int] = (64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


class FakeConsole:
    """Implements ``keyhold.types.Console``. Returns prepared screens in order (the last one
    repeats) and records every input. ``text`` is what ``screen_text()`` returns, if set."""

    def __init__(self, screens: list[bytes] | None = None, *, text: str | None = None) -> None:
        self.screens = list(screens) if screens else [make_png()]
        self.text = text
        self.inputs: list[tuple] = []
        self.shots = 0
        self.closed = False
        self.fail_inputs = 0  # raise RuntimeError on the next N inputs
        self.fail_screenshots = 0

    def screenshot(self) -> bytes:
        if self.fail_screenshots:
            self.fail_screenshots -= 1
            raise RuntimeError("screendump failed")
        png = self.screens[min(self.shots, len(self.screens) - 1)]
        self.shots += 1
        return png

    def _input(self, record: tuple) -> None:
        if self.fail_inputs:
            self.fail_inputs -= 1
            raise RuntimeError("monitor connection lost")
        self.inputs.append(record)

    def type_text(self, text: str) -> None:
        self._input(("type", text))

    def press(self, keys: list[str]) -> None:
        self._input(("press", list(keys)))

    def power_cycle(self) -> None:
        self._input(("power_cycle",))

    def close(self) -> None:
        self.closed = True


class TextConsole(FakeConsole):
    def screen_text(self) -> str:
        return self.text or ""


class InstantClock:
    """Implements ``keyhold.types.Clock``; sleep returns at once and only moves the time."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.t = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class StubLease:
    """Implements ``keyhold.types.Lease``. ``revoke_after`` lets that many require() calls pass."""

    def __init__(self, revoke_after: int | None = None) -> None:
        self.revoke_after = revoke_after
        self.calls = 0
        self.revoked = False

    def require(self) -> None:
        self.calls += 1
        if self.revoke_after is not None and self.calls > self.revoke_after:
            self.revoked = True
        if self.revoked:
            raise HoldRevoked("authorization VOIDED")


class Gate:
    """Behaves like ``GatedConsole``: screenshot is always allowed, input needs the lease."""

    def __init__(self, console: FakeConsole, lease: StubLease) -> None:
        self.console = console
        self.lease = lease

    def screenshot(self) -> bytes:
        return self.console.screenshot()

    def type_text(self, text: str) -> None:
        self.lease.require()
        self.console.type_text(text)

    def press(self, keys: list[str]) -> None:
        self.lease.require()
        self.console.press(keys)

    def power_cycle(self) -> None:
        self.lease.require()
        self.console.power_cycle()

    def close(self) -> None:
        self.console.close()
