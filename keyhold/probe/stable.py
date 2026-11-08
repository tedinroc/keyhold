"""The stable-pass rule (flapping rule) that gates a capture.

A server counts as fixed only after ``passes`` probe passes (default two), each at least
``min_gap_s`` (default 30 s) after the previous counted pass, with no failed probe anywhere in
between. Probes keep running at a fixed interval in the meantime, so "no failure between" is
checked actively rather than assumed. Any failure starts the count again.

This module only decides. It never captures; the orchestrator does that after a stable outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from keyhold.types import Clock, ProbeResult, SystemClock

from ._timing import ProbeFn, ResultFn, StopFn, call_probe, notify, passed, sleep_until


def _seconds(value: float) -> str:
    return f"{value:g} s"


@dataclass(frozen=True)
class StableRule:
    min_gap_s: float = 30.0
    passes: int = 2

    def __post_init__(self) -> None:
        if self.passes < 2:
            raise ValueError("the stable-pass rule needs at least two passes")
        if self.min_gap_s < 0:
            raise ValueError("min_gap_s must not be negative")

    @property
    def label(self) -> str:
        count = "two" if self.passes == 2 else str(self.passes)
        return f"{count} passes at least {_seconds(self.min_gap_s)} apart, with no failure between"


class StableTracker:
    """Feed probe results in time order; ``observe`` returns True once the rule is met.

    A failure clears the count. A pass less than ``min_gap_s`` after the last counted pass keeps the
    run alive but is not counted. A result older than the previous one (clock went backwards)
    also clears the count, since the gap can no longer be trusted.
    """

    def __init__(self, rule: StableRule | None = None) -> None:
        self.rule = rule or StableRule()
        self._counted: list[ProbeResult] = []
        self._last_at: float | None = None

    @property
    def counted(self) -> tuple[ProbeResult, ...]:
        return tuple(self._counted)

    @property
    def stable(self) -> bool:
        return len(self._counted) >= self.rule.passes

    def reset(self) -> None:
        self._counted.clear()

    def observe(self, result: ProbeResult) -> bool:
        if self._last_at is not None and result.at < self._last_at:
            self._counted.clear()
        self._last_at = result.at
        if not passed(result):
            self._counted.clear()
            return False
        if not self._counted:
            self._counted.append(result)
        elif len(self._counted) < self.rule.passes and result.at - self._counted[-1].at >= self.rule.min_gap_s:
            self._counted.append(result)
        return self.stable


@dataclass(frozen=True)
class StableOutcome:
    """``reason``: ``stable`` (capture may go ahead), ``deadline`` (no stable pass in time: void) or
    ``stopped`` (``should_stop`` became true, for example the owner pressed Stop)."""

    stable: bool
    reason: str
    started_at: float
    ended_at: float
    results: tuple[ProbeResult, ...] = field(default_factory=tuple)
    passes: tuple[ProbeResult, ...] = field(default_factory=tuple)

    @property
    def detail(self) -> str:
        if self.stable:
            gap = self.passes[-1].at - self.passes[0].at
            return f"stable: {len(self.passes)} passes {gap:g} s apart, no failure between"
        if self.reason == "stopped":
            return f"stopped after {len(self.results)} probes"
        last = self.results[-1].detail if self.results else "no probe ran"
        return f"no stable pass within the deadline ({len(self.results)} probes; last: {last})"


def wait_for_stable_pass(
    probe: ProbeFn,
    *,
    rule: StableRule | None = None,
    clock: Clock | None = None,
    interval_s: float = 10.0,
    deadline_s: float = 600.0,
    should_stop: StopFn | None = None,
    on_result: ResultFn | None = None,
    sleep_chunk_s: float = 5.0,
) -> StableOutcome:
    """Probe every ``interval_s`` until the rule is met, ``deadline_s`` passes, or ``should_stop()``.

    The first probe runs at once. A probe starts only if its start time is within the deadline.
    ``should_stop`` is checked before every wait step and again after every probe, so a Stop that
    arrives while the deciding probe runs still ends in ``stopped``, not ``stable``.
    """
    if interval_s <= 0 or deadline_s < 0 or sleep_chunk_s <= 0:
        raise ValueError("interval_s and sleep_chunk_s must be positive, deadline_s not negative")
    clock = clock or SystemClock()
    tracker = StableTracker(rule)
    started = clock.now()
    deadline = started + deadline_s
    results: list[ProbeResult] = []

    def outcome(reason: str) -> StableOutcome:
        return StableOutcome(
            stable=reason == "stable",
            reason=reason,
            started_at=started,
            ended_at=clock.now(),
            results=tuple(results),
            passes=tracker.counted if reason == "stable" else (),
        )

    next_at = started
    while True:
        if not sleep_until(clock, next_at, should_stop, sleep_chunk_s):
            return outcome("stopped")
        began = clock.now()
        result = call_probe(probe, clock)
        results.append(result)
        notify(on_result, result)
        now_stable = tracker.observe(result)
        if should_stop is not None and should_stop():
            return outcome("stopped")  # Stop pressed while the probe ran: Stop wins over a capture
        if now_stable:
            return outcome("stable")
        next_at = began + interval_s
        if next_at > deadline:
            return outcome("deadline")
