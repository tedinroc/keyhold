"""Warranty: hourly re-probes for 24 h (a minute each in judge mode); two failures in a row refund."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from keyhold.probe.fake import FakeServer
from keyhold.probe.owner import (
    Prober,
    WarrantyClosed,
    WarrantyPlan,
    WarrantySchedule,
    WarrantyVerdict,
    run_warranty,
)
from keyhold.types import ProbeResult

HOUR = 3600.0


def scripted(clock, outcomes: list[bool]):
    """A probe that returns the given pass/fail sequence (then keeps passing) and counts calls."""
    calls: list[float] = []

    def probe() -> ProbeResult:
        ok = outcomes[len(calls)] if len(calls) < len(outcomes) else True
        calls.append(clock.now())
        return ProbeResult(ok, ok, ok, "ok" if ok else "connection refused", clock.now())

    return probe, calls


def test_plan_defaults_and_labels() -> None:
    plan = WarrantyPlan()
    assert (plan.interval_s, plan.checks, plan.failures_to_refund, plan.window_s) == (HOUR, 24, 2, 24 * HOUR)
    assert [plan.due_at(0, k) for k in (1, 2, 24)] == [HOUR, 2 * HOUR, 24 * HOUR]
    assert plan.label == (
        "Warranty: re-probed every hour for 24 hours. Two failed checks in a row refund the full capture."
    )
    judge = WarrantyPlan.judge()
    assert (judge.interval_s, judge.checks, judge.judge_mode) == (60.0, 24, True)
    assert judge.label.startswith("Warranty (judge mode, compressed): re-probed every minute for 24 minutes")
    assert "each minute stands in for an hour" in judge.label
    for bad in ({"interval_s": 0}, {"checks": 0}, {"failures_to_refund": 0}):
        with pytest.raises(ValueError):
            WarrantyPlan(**bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("plan", "label", "short"),
    [
        (
            WarrantyPlan.judge(interval_s=20.0, checks=6),
            "Warranty (judge mode, compressed): re-probed every 20 seconds, 6 times (2 minutes in all); "
            "the real plan is 24 hourly checks.",
            "re-checked every 20 s, 6 times (compressed for the demo; the real plan is 24 hourly checks)",
        ),
        (
            WarrantyPlan.judge(),
            "Warranty (judge mode, compressed): re-probed every minute for 24 minutes; each minute stands in "
            "for an hour.",
            "re-checked every minute, 24 times (compressed for the demo: each minute stands in for an hour)",
        ),
        (
            WarrantyPlan.judge(interval_s=60.0, checks=1),
            "Warranty (judge mode, compressed): re-probed every minute, once (1 minute in all); the real plan "
            "is 24 hourly checks.",
            "re-checked every minute, once (compressed for the demo; the real plan is 24 hourly checks)",
        ),
        (
            WarrantyPlan(),
            "Warranty: re-probed every hour for 24 hours.",
            "re-checked every hour, 24 times",
        ),
        (
            # Outside judge mode a short plan is not "for the demo", and the label and the short
            # label say the same thing.
            WarrantyPlan(interval_s=1800.0, checks=12),
            "Warranty: re-probed every 30 minutes, 12 times (6 hours in all), shorter than the standard 24 "
            "hourly checks.",
            "re-checked every 30 min, 12 times (shorter than the standard 24 hourly checks)",
        ),
        (
            # Judge mode with the real plan configured (KEYHOLD_WARRANTY_CHECKS=24,
            # KEYHOLD_WARRANTY_INTERVAL_S=3600): nothing is compressed, so neither label says so.
            WarrantyPlan.judge(interval_s=3600.0, checks=24),
            "Warranty: re-probed every hour for 24 hours.",
            "re-checked every hour, 24 times",
        ),
        (
            # Sparser than the standard plan but over the same 24 hours: not compressed either.
            WarrantyPlan.judge(interval_s=7200.0, checks=12),
            "Warranty: re-probed every 2 hours, 12 times (24 hours in all).",
            "re-checked every 2 h, 12 times",
        ),
        (
            WarrantyPlan(interval_s=7200.0, checks=12),
            "Warranty: re-probed every 2 hours, 12 times (24 hours in all).",
            "re-checked every 2 h, 12 times",
        ),
    ],
)
def test_warranty_labels_read_correctly_for_any_plan(plan: WarrantyPlan, label: str, short: str) -> None:
    # The hosted demo runs a two-minute warranty (KEYHOLD_WARRANTY_CHECKS=6, _INTERVAL_S=20): its
    # label must not claim that "each 20 seconds stands in for an hour".
    assert plan.label == f"{label} Two failed checks in a row refund the full capture."
    assert plan.short_label == short
    assert "stands in for an hour" not in plan.label or plan.is_hour_per_minute
    # "compressed" only for a plan shorter than 24 hours, in both labels alike.
    assert ("compressed" in plan.label) == ("compressed" in plan.short_label) == (
        plan.window_s < 24 * HOUR and plan.judge_mode
    )


def test_a_one_check_warranty_still_refunds_after_two_failures_in_a_row() -> None:
    # The label promises "Two failed checks in a row refund the full capture" for any count: with
    # one scheduled check, a failure adds a confirmation check, so the promise holds.
    schedule = WarrantySchedule(WarrantyPlan.judge(interval_s=20.0, checks=1), 0.0)
    first = schedule.record(ProbeResult(False, False, False, "connection refused", 20.0))
    assert first.verdict is WarrantyVerdict.RUNNING and schedule.next_due() == 40.0
    second = schedule.record(ProbeResult(False, False, False, "connection refused", 40.0))
    assert second.refund


def test_all_checks_pass_gives_ok_after_24_hours(clock) -> None:
    start = clock.now()
    probe, calls = scripted(clock, [True] * 24)
    decision = run_warranty(WarrantySchedule(WarrantyPlan(), start), probe, clock=clock)
    assert decision.verdict is WarrantyVerdict.OK and not decision.refund and decision.final
    assert [c - start for c in calls] == [HOUR * k for k in range(1, 25)]
    assert decision.decided_at == start + 24 * HOUR
    assert decision.checks_run == 24 and decision.checks_missed == 0


def test_two_failures_in_a_row_refund_at_once(clock) -> None:
    start = clock.now()
    outcomes = [True] * 4 + [False, False] + [True] * 18
    probe, calls = scripted(clock, outcomes)
    decision = run_warranty(WarrantySchedule(WarrantyPlan(), start), probe, clock=clock)
    assert decision.refund and decision.verdict is WarrantyVerdict.REFUND
    assert len(calls) == 6  # stops probing as soon as it decides
    assert decision.decided_at == start + 6 * HOUR
    assert decision.reason.startswith("2 failed checks in a row (checks 5 to 6)")
    assert decision.consecutive_failures == 2


def test_failures_that_are_not_consecutive_do_not_refund(clock) -> None:
    outcomes = [k % 2 == 0 for k in range(24)]  # pass, fail, pass, fail, ...
    outcomes[-1] = True  # end on a pass so no confirmation check is needed
    probe, calls = scripted(clock, outcomes)
    decision = run_warranty(WarrantySchedule(WarrantyPlan(), clock.now()), probe, clock=clock)
    assert decision.verdict is WarrantyVerdict.OK and len(calls) == 24


def test_failure_on_the_last_check_gets_a_confirmation_check(clock) -> None:
    start = clock.now()
    probe, calls = scripted(clock, [True] * 23 + [False, False])
    decision = run_warranty(WarrantySchedule(WarrantyPlan(), start), probe, clock=clock)
    assert decision.refund
    assert calls[-1] - start == 25 * HOUR
    assert "checks 24 to 25" in decision.reason


def test_last_check_failure_then_confirmation_pass_is_ok(clock) -> None:
    probe, calls = scripted(clock, [True] * 23 + [False, True])
    decision = run_warranty(WarrantySchedule(WarrantyPlan(), clock.now()), probe, clock=clock)
    assert decision.verdict is WarrantyVerdict.OK and len(calls) == 25
    assert "confirmation check 25 passed" in decision.reason


def test_without_confirmation_the_window_just_ends(clock) -> None:
    plan = WarrantyPlan(confirm_last_failure=False)
    probe, calls = scripted(clock, [True] * 23 + [False])
    decision = run_warranty(WarrantySchedule(plan, clock.now()), probe, clock=clock)
    assert decision.verdict is WarrantyVerdict.OK and len(calls) == 24


def test_judge_mode_runs_in_minutes(spec, clock) -> None:
    server = FakeServer()
    server.fix(body="keyhold-ok")
    start = clock.now()
    seen: list[ProbeResult] = []

    def relapse(result: ProbeResult) -> None:
        seen.append(result)
        if len(seen) == 3:
            server.break_down()  # the fix did not hold

    decision = run_warranty(
        WarrantySchedule(WarrantyPlan.judge(), start),
        Prober(spec, server.ssh, server.http, clock),
        clock=clock,
        on_result=relapse,
    )
    assert decision.refund
    assert [x.at - start for x in seen] == [60, 120, 180, 240, 300]
    assert [x.ok for x in seen] == [True, True, True, False, False]


def test_crashing_probe_counts_as_failure(clock) -> None:
    def probe() -> ProbeResult:
        raise OSError("runner lost its network")

    decision = run_warranty(WarrantySchedule(WarrantyPlan.judge(), clock.now()), probe, clock=clock)
    assert decision.refund and "probe crashed (OSError)" in decision.reason


def test_stop_returns_a_running_decision_that_can_resume(clock) -> None:
    start = clock.now()
    plan = WarrantyPlan()
    history: list[ProbeResult] = []
    probe, _ = scripted(clock, [True, True, False])

    def stop() -> bool:
        return len(history) == 3

    decision = run_warranty(WarrantySchedule(plan, start), probe, clock=clock, should_stop=stop, on_result=history.append)
    assert decision.verdict is WarrantyVerdict.RUNNING and not decision.final
    assert decision.checks_run == 3 and decision.consecutive_failures == 1
    assert "3 of 24 checks done" in decision.reason

    # Restart from stored results: the failure streak carries over.
    resumed = WarrantySchedule(plan, start, history=history)
    assert resumed.next_due(clock.now()) == start + 4 * HOUR
    probe2, calls2 = scripted(clock, [False])
    final = run_warranty(resumed, probe2, clock=clock)
    assert final.refund and "checks 3 to 4" in final.reason and len(calls2) == 1


def test_checks_missed_while_down_are_skipped_not_failed(clock) -> None:
    start = clock.now()
    schedule = WarrantySchedule(WarrantyPlan(), start)
    schedule.record(ProbeResult(False, False, True, "connection refused", start + HOUR))
    # The runner was down from hour 1 to hour 5.5; only check 5 runs now.
    now = start + 5.5 * HOUR
    assert schedule.next_due(now) == start + 5 * HOUR
    decision = schedule.record(ProbeResult(True, True, True, "ok", now))
    assert decision.verdict is WarrantyVerdict.RUNNING
    assert decision.checks_missed == 3 and decision.checks_run == 2
    assert [c.slot for c in schedule.checks if c.missed] == [2, 3, 4]
    assert schedule.next_due(now) == start + 6 * HOUR


def test_refund_reason_names_the_checks_that_ran(clock) -> None:
    start = clock.now()
    schedule = WarrantySchedule(WarrantyPlan(), start)
    schedule.record(ProbeResult(False, False, False, "down", start + 2 * HOUR))
    decision = schedule.record(ProbeResult(False, False, False, "still down", start + 6.2 * HOUR))
    assert decision.refund
    assert decision.reason == "2 failed checks in a row (checks 2 to 6); last: still down"
    assert decision.checks_missed == 4  # checks 1, 3, 4, 5


def test_resuming_after_the_window_runs_one_final_check(clock) -> None:
    start = clock.now()
    schedule = WarrantySchedule(WarrantyPlan(), start)
    clock.advance(30 * HOUR)
    probe, calls = scripted(clock, [True])
    decision = run_warranty(schedule, probe, clock=clock)
    assert decision.verdict is WarrantyVerdict.OK
    assert len(calls) == 1 and decision.checks_missed == 23


def test_record_guards(clock) -> None:
    start = clock.now()
    schedule = WarrantySchedule(WarrantyPlan(failures_to_refund=1), start)
    with pytest.raises(ValueError, match="not due"):
        schedule.record(ProbeResult(True, True, True, "ok", start + 10))
    schedule.record(ProbeResult(False, False, False, "down", start + HOUR))
    assert schedule.decision.refund and schedule.next_due() is None
    with pytest.raises(WarrantyClosed):
        schedule.record(ProbeResult(True, True, True, "ok", start + 2 * HOUR))


# ---------------------------------------------------------------------------------------------
# Property: the driver agrees with a direct reading of the rules
# ---------------------------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(0.0, seconds)


def expected(outcomes: list[bool], checks: int) -> tuple[WarrantyVerdict, int]:
    """(verdict, number of probes) under: refund on 2 failures in a row; a failure on the last
    check gets one confirmation check."""
    streak = 0
    for i, ok in enumerate(outcomes[:checks], start=1):
        streak = 0 if ok else streak + 1
        if streak >= 2:
            return WarrantyVerdict.REFUND, i
    if streak == 1:
        return (WarrantyVerdict.OK if outcomes[checks] else WarrantyVerdict.REFUND), checks + 1
    return WarrantyVerdict.OK, checks


@settings(max_examples=200, deadline=None)
@given(outcomes=st.lists(st.booleans(), min_size=25, max_size=25), checks=st.sampled_from([1, 3, 24]))
def test_warranty_matches_the_rules(outcomes: list[bool], checks: int) -> None:
    clock = _Clock()
    start = clock.now()
    probe, calls = scripted(clock, outcomes)
    plan = WarrantyPlan(interval_s=60.0, checks=checks, judge_mode=True)
    decision = run_warranty(WarrantySchedule(plan, start), probe, clock=clock, sleep_chunk_s=60.0)
    verdict, probes = expected(outcomes, checks)
    assert decision.verdict is verdict
    assert len(calls) == probes
    assert [c - start for c in calls] == [60.0 * k for k in range(1, probes + 1)]
