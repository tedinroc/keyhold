"""Small helpers shared by the stable-pass driver and the warranty driver."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace

from keyhold.types import Clock, ProbeResult

log = logging.getLogger("keyhold.probe")

ProbeFn = Callable[[], ProbeResult]
StopFn = Callable[[], bool]
ResultFn = Callable[[ProbeResult], None]

# Below this many seconds left, a wait counts as finished (float rounding guard).
WAIT_EPSILON_S = 1e-6


def passed(result: ProbeResult) -> bool:
    """A pass needs both halves, whatever ``ok`` says."""
    return bool(result.ok and result.ssh_ok and result.http_ok)


def sleep_until(clock: Clock, when: float, should_stop: StopFn | None, chunk_s: float) -> bool:
    """Sleep until ``clock.now() >= when`` in steps of at most ``chunk_s``.

    Returns False as soon as ``should_stop()`` is true (checked before every step, including when
    no wait is needed), True when the time has come.
    """
    while True:
        if should_stop is not None and should_stop():
            return False
        remaining = when - clock.now()
        if remaining <= WAIT_EPSILON_S:
            return True
        clock.sleep(min(remaining, chunk_s))


def call_probe(probe: ProbeFn, clock: Clock) -> ProbeResult:
    """Run one probe. A crash or a wrong return value counts as a failed probe, never a pass.

    The result is stamped with the driver's clock (the moment the probe returned), so the 30 s gap
    and the warranty slots are measured on the same clock that does the waiting.
    """
    try:
        result = probe()
    except Exception as exc:  # noqa: BLE001
        log.warning("probe raised %s", type(exc).__name__)
        return ProbeResult(False, False, False, f"probe crashed ({type(exc).__name__})", clock.now())
    if not isinstance(result, ProbeResult):
        return ProbeResult(False, False, False, "probe returned no result", clock.now())
    now = clock.now()
    return result if result.at == now else replace(result, at=now)


def notify(callback: ResultFn | None, result: ProbeResult) -> None:
    """Hand ``result`` to a status callback. A failing callback is logged, not allowed to stop
    the probe schedule that decides capture or refund."""
    if callback is None:
        return
    try:
        callback(result)
    except Exception:  # noqa: BLE001
        log.exception("probe result callback failed")
