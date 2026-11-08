"""The warranty after a capture: re-probe on a schedule and decide whether to refund.

Default plan: one check every hour for 24 hours, starting one hour after the capture. Judge mode
compresses the hour to a minute (24 checks over 24 minutes) and says so in its label.

Rules:
- ``failures_to_refund`` (default 2) failed checks in a row give a REFUND decision at once.
- A check that passes resets the count.
- If the window ends while failures are still counting up (the last check failed), extra
  confirmation checks run one interval apart until the count either reaches the refund threshold
  or a check passes, so a failure on the last check is never silently dropped.
- Checks missed while the runner was down are skipped, not counted as failures. When the runner
  comes back it runs only the latest check that is due. "In a row" means consecutive checks that
  actually ran.

This module only decides. It never calls PayPal; the orchestrator refunds with
``refund-{rescue_id}`` after a REFUND decision.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import dataclass

from keyhold.types import Clock, ProbeResult, SystemClock

from ._timing import ProbeFn, ResultFn, StopFn, call_probe, notify, passed, sleep_until

# A result may carry a time this much before its slot was due (clock rounding) and still count.
DUE_TOLERANCE_S = 1e-3


def _span(seconds: float) -> str:
    """Plain duration: '1 hour', '24 hours', '1 minute', '90 seconds'."""
    for unit, size in (("hour", 3600), ("minute", 60), ("second", 1)):
        if seconds >= size and float(seconds) % size == 0:
            n = int(seconds // size)
            return f"{n} {unit}" + ("" if n == 1 else "s")
    return f"{seconds:g} seconds"


@dataclass(frozen=True)
class WarrantyPlan:
    interval_s: float = 3600.0
    checks: int = 24
    failures_to_refund: int = 2
    confirm_last_failure: bool = True
    judge_mode: bool = False

    def __post_init__(self) -> None:
        if self.interval_s <= 0:
            raise ValueError("interval_s must be positive")
        if self.checks < 1:
            raise ValueError("checks must be at least 1")
        if self.failures_to_refund < 1:
            raise ValueError("failures_to_refund must be at least 1")

    @classmethod
    def judge(cls, interval_s: float = 60.0, checks: int = 24) -> WarrantyPlan:
        """Compressed plan for judge mode: by default a minute stands in for each hour."""
        return cls(interval_s=interval_s, checks=checks, judge_mode=True)

    @property
    def window_s(self) -> float:
        return self.interval_s * self.checks

    def due_at(self, started_at: float, slot: int) -> float:
        """Due time of check ``slot`` (1-based; slots past ``checks`` are confirmation checks)."""
        return started_at + slot * self.interval_s

    @property
    def label(self) -> str:
        every = _span(self.interval_s)
        every = every[2:] if every.startswith("1 ") else every
        rule = (
            "Two failed checks in a row refund the full capture."
            if self.failures_to_refund == 2
            else f"{self.failures_to_refund} failed checks in a row refund the full capture."
        )
        if self.judge_mode:
            return (
                f"Warranty (judge mode, compressed): re-probed every {every} for {_span(self.window_s)}; "
                f"each {every} stands in for an hour. {rule}"
            )
        return f"Warranty: re-probed every {every} for {_span(self.window_s)}. {rule}"


class WarrantyVerdict(str, enum.Enum):
    RUNNING = "running"  # checks still to run
    REFUND = "refund"  # refund the full capture now
    OK = "ok"  # window ended without the refund condition


@dataclass(frozen=True)
class WarrantyCheck:
    slot: int
    due_at: float
    result: ProbeResult | None  # None: the check was missed (runner down) and skipped

    @property
    def missed(self) -> bool:
        return self.result is None


@dataclass(frozen=True)
class WarrantyDecision:
    verdict: WarrantyVerdict
    reason: str
    checks_run: int
    checks_missed: int
    consecutive_failures: int
    decided_at: float | None

    @property
    def refund(self) -> bool:
        return self.verdict is WarrantyVerdict.REFUND

    @property
    def final(self) -> bool:
        return self.verdict is not WarrantyVerdict.RUNNING


class WarrantyClosed(RuntimeError):
    """A result was recorded after the warranty was already decided."""


class WarrantySchedule:
    """Pure warranty state: when the next check is due and what the results so far decide.

    Rebuild after a restart with ``WarrantySchedule(plan, started_at, history=stored_results)``.
    """

    def __init__(self, plan: WarrantyPlan, started_at: float, history: Iterable[ProbeResult] = ()) -> None:
        self.plan = plan
        self.started_at = float(started_at)
        self._next_slot = 1
        self._last_slot = plan.checks
        self._streak = 0
        self._streak_from = 0  # slot of the first failure in the current streak
        self._checks: list[WarrantyCheck] = []
        self._verdict = WarrantyVerdict.RUNNING
        self._reason = ""
        self._decided_at: float | None = None
        for result in history:
            self.record(result)

    # -- reading ---------------------------------------------------------------------------

    @property
    def checks(self) -> tuple[WarrantyCheck, ...]:
        return tuple(self._checks)

    @property
    def last_slot(self) -> int:
        """The final scheduled slot, including any confirmation slots added so far."""
        return self._last_slot

    @property
    def decision(self) -> WarrantyDecision:
        ran = sum(1 for c in self._checks if not c.missed)
        missed = len(self._checks) - ran
        reason = self._reason or f"warranty running: {ran} of {self.plan.checks} checks done"
        return WarrantyDecision(self._verdict, reason, ran, missed, self._streak, self._decided_at)

    def due_at(self, slot: int) -> float:
        return self.plan.due_at(self.started_at, slot)

    def _slot_for(self, now: float | None) -> int:
        slot = self._next_slot
        if now is not None:
            while slot < self._last_slot and self.due_at(slot + 1) <= now:
                slot += 1
        return slot

    def next_due(self, now: float | None = None) -> float | None:
        """Due time of the next check, or None once decided. Given ``now``, overdue checks are
        skipped so that only the latest due one runs."""
        if self._verdict is not WarrantyVerdict.RUNNING:
            return None
        return self.due_at(self._slot_for(now))

    # -- writing ---------------------------------------------------------------------------

    def record(self, result: ProbeResult) -> WarrantyDecision:
        """Record one check result (its ``at`` picks the slot) and return the decision so far."""
        if self._verdict is not WarrantyVerdict.RUNNING:
            raise WarrantyClosed(f"warranty already decided: {self._verdict.value}")
        slot = self._slot_for(result.at)
        due = self.due_at(slot)
        if result.at < due - DUE_TOLERANCE_S:
            raise ValueError(f"check {slot} is not due until {due:.3f}; result is from {result.at:.3f}")
        for missed in range(self._next_slot, slot):
            self._checks.append(WarrantyCheck(missed, self.due_at(missed), None))
        self._checks.append(WarrantyCheck(slot, due, result))
        self._next_slot = slot + 1

        ok = passed(result)
        if ok:
            self._streak = 0
        else:
            self._streak += 1
            if self._streak == 1:
                self._streak_from = slot
        in_confirmation = slot > self.plan.checks

        if self._streak >= self.plan.failures_to_refund:
            self._decide(
                WarrantyVerdict.REFUND,
                f"{self._streak} failed checks in a row (checks {self._streak_from} to {slot}); "
                f"last: {result.detail}",
                result.at,
            )
        elif ok and in_confirmation:
            self._decide(WarrantyVerdict.OK, f"confirmation check {slot} passed after a failure", result.at)
        elif slot >= self._last_slot:
            if self._streak > 0 and self.plan.confirm_last_failure:
                self._last_slot = slot + (self.plan.failures_to_refund - self._streak)
                self._reason = (
                    f"check {slot} failed at the end of the window; "
                    f"confirmation check {slot + 1} is due"
                )
            else:
                self._decide(
                    WarrantyVerdict.OK,
                    f"window ended: {self.decision.checks_run} checks, never "
                    f"{self.plan.failures_to_refund} failures in a row",
                    result.at,
                )
        else:
            self._reason = ""
        return self.decision

    def _decide(self, verdict: WarrantyVerdict, reason: str, at: float) -> None:
        self._verdict = verdict
        self._reason = reason
        self._decided_at = at


def run_warranty(
    schedule: WarrantySchedule,
    probe: ProbeFn,
    *,
    clock: Clock | None = None,
    should_stop: StopFn | None = None,
    on_result: ResultFn | None = None,
    sleep_chunk_s: float = 30.0,
) -> WarrantyDecision:
    """Run the remaining checks of ``schedule`` and return the decision.

    Returns as soon as the verdict is REFUND or OK. If ``should_stop()`` becomes true (shutdown),
    returns the RUNNING decision; resume later from the stored results.
    """
    if sleep_chunk_s <= 0:
        raise ValueError("sleep_chunk_s must be positive")
    clock = clock or SystemClock()
    while True:
        due = schedule.next_due(clock.now())
        if due is None:
            return schedule.decision
        if not sleep_until(clock, due, should_stop, sleep_chunk_s):
            return schedule.decision
        result = call_probe(probe, clock)
        notify(on_result, result)
        schedule.record(result)
