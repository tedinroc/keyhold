"""The stable-pass rule: two passes at least 30 s apart with no failure between."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from keyhold.probe.fake import FakeServer
from keyhold.probe.owner import (
    JUDGE_POLICY,
    STANDARD_POLICY,
    Prober,
    StableRule,
    StableTracker,
    probe_policy,
    wait_for_stable_pass,
)
from keyhold.types import ProbeResult

T0 = 1_800_000_000.0


def r(ok: bool, at: float, *, ssh_ok: bool | None = None, http_ok: bool | None = None) -> ProbeResult:
    return ProbeResult(ok, ok if ssh_ok is None else ssh_ok, ok if http_ok is None else http_ok, "", T0 + at)


def feed(tracker: StableTracker, *results: ProbeResult) -> list[bool]:
    return [tracker.observe(x) for x in results]


def test_two_passes_thirty_seconds_apart_are_stable() -> None:
    assert feed(StableTracker(), r(True, 0), r(True, 30)) == [False, True]


def test_passes_closer_than_the_gap_are_not_enough() -> None:
    assert feed(StableTracker(), r(True, 0), r(True, 10), r(True, 29.9)) == [False, False, False]


def test_a_failure_between_starts_over() -> None:
    tracker = StableTracker()
    assert feed(tracker, r(True, 0), r(False, 15), r(True, 30), r(True, 45)) == [False, False, False, False]
    assert tracker.observe(r(True, 60))  # 30 s after the pass at 30
    assert [p.at - T0 for p in tracker.counted] == [30, 60]


def test_a_failure_after_stable_clears_it() -> None:
    tracker = StableTracker()
    feed(tracker, r(True, 0), r(True, 30))
    assert tracker.stable
    assert tracker.observe(r(False, 40)) is False and not tracker.stable


def test_half_a_pass_is_a_failure() -> None:
    # ok=True with a failed half can only come from a buggy transport; it must not count.
    tracker = StableTracker()
    assert feed(tracker, r(True, 0), r(True, 30, ssh_ok=False)) == [False, False]
    assert tracker.counted == ()


def test_clock_going_backwards_starts_over() -> None:
    tracker = StableTracker()
    assert feed(tracker, r(True, 100), r(True, 50), r(True, 79)) == [False, False, False]
    assert [p.at - T0 for p in tracker.counted] == [50]  # the count restarted at the earlier time


def test_rule_validation_and_labels() -> None:
    with pytest.raises(ValueError):
        StableRule(passes=1)
    with pytest.raises(ValueError):
        StableRule(min_gap_s=-1)
    assert StableRule().label == "two passes at least 30 s apart, with no failure between"
    assert StableRule(min_gap_s=60, passes=3).label.startswith("3 passes at least 60 s apart")
    assert probe_policy(False) is STANDARD_POLICY and STANDARD_POLICY.stable.min_gap_s == 30
    assert probe_policy(True) is JUDGE_POLICY and JUDGE_POLICY.judge_mode
    assert "Judge mode" in JUDGE_POLICY.label and "Judge mode" not in STANDARD_POLICY.label


# ---------------------------------------------------------------------------------------------
# wait_for_stable_pass with a fake server and a fake clock
# ---------------------------------------------------------------------------------------------


def test_fixed_server_becomes_stable_after_thirty_seconds(spec, clock) -> None:
    server = FakeServer()
    server.fix(body="keyhold-ok")
    start = clock.now()
    seen: list[ProbeResult] = []

    outcome = wait_for_stable_pass(
        Prober(spec, server.ssh, server.http, clock), clock=clock, interval_s=10, deadline_s=600, on_result=seen.append
    )

    assert outcome.stable and outcome.reason == "stable"
    assert [x.at - start for x in outcome.results] == [0, 10, 20, 30]
    assert [x.at - start for x in outcome.passes] == [0, 30]
    assert seen == list(outcome.results)
    assert outcome.detail == "stable: 2 passes 30 s apart, no failure between"


def test_server_that_comes_up_late_is_stable_thirty_seconds_after_its_first_pass(spec, clock) -> None:
    server = FakeServer()
    start = clock.now()

    def probe() -> ProbeResult:
        if clock.now() - start >= 40:
            server.fix(body="keyhold-ok")
        return Prober(spec, server.ssh, server.http, clock)()

    outcome = wait_for_stable_pass(probe, clock=clock, interval_s=10, deadline_s=600)
    assert outcome.stable
    assert [x.at - start for x in outcome.passes] == [40, 70]


def test_flapping_server_never_reaches_stable_and_hits_the_deadline(spec, clock) -> None:
    server = FakeServer(body="keyhold-ok")
    flips = iter(range(10_000))

    def probe() -> ProbeResult:
        if next(flips) % 3 == 2:
            server.break_down()
        else:
            server.fix()
        return Prober(spec, server.ssh, server.http, clock)()

    start = clock.now()
    outcome = wait_for_stable_pass(probe, clock=clock, interval_s=10, deadline_s=120)
    assert not outcome.stable and outcome.reason == "deadline"
    assert outcome.passes == ()
    assert outcome.results[-1].at - start <= 120
    assert len(outcome.results) == 13  # probes at 0, 10, ..., 120
    assert "no stable pass within the deadline" in outcome.detail


def test_stop_ends_the_wait(spec, clock) -> None:
    server = FakeServer()
    stop = {"now": False}

    def on_result(result: ProbeResult) -> None:
        if len(server.logins) == 3:
            stop["now"] = True

    outcome = wait_for_stable_pass(
        Prober(spec, server.ssh, server.http, clock),
        clock=clock,
        interval_s=10,
        should_stop=lambda: stop["now"],
        on_result=on_result,
    )
    assert outcome.reason == "stopped" and not outcome.stable
    assert len(outcome.results) == 3


def test_stop_during_the_deciding_probe_wins_over_stable(spec, clock) -> None:
    server = FakeServer()
    server.fix(body="keyhold-ok")
    prober = Prober(spec, server.ssh, server.http, clock)
    stop = {"now": False}

    def probe() -> ProbeResult:
        result = prober()
        if len(server.logins) == 4:  # the probe at 30 s would make it stable
            stop["now"] = True
        return result

    outcome = wait_for_stable_pass(probe, clock=clock, interval_s=10, should_stop=lambda: stop["now"])
    assert outcome.reason == "stopped" and not outcome.stable and outcome.passes == ()


def test_a_crashing_probe_counts_as_a_failure(clock) -> None:
    calls = {"n": 0}

    def probe() -> ProbeResult:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("probe bug")
        return ProbeResult(True, True, True, "ok", clock.now())

    start = clock.now()
    outcome = wait_for_stable_pass(probe, clock=clock, interval_s=10, deadline_s=600)
    assert outcome.stable
    assert outcome.results[1].detail == "probe crashed (RuntimeError)"
    assert [x.at - start for x in outcome.passes] == [20, 50]


def test_results_are_timed_by_the_waiting_clock(clock) -> None:
    # A probe that reports its own (wrong) time cannot shorten the 30 s gap.
    def probe() -> ProbeResult:
        return ProbeResult(True, True, True, "ok", clock.now() + 1000 * len(seen))

    seen: list[ProbeResult] = []
    start = clock.now()
    outcome = wait_for_stable_pass(probe, clock=clock, interval_s=10, on_result=seen.append)
    assert [x.at - start for x in outcome.passes] == [0, 30]


def test_judge_rule_uses_a_shorter_gap(spec, clock) -> None:
    server = FakeServer()
    server.fix(body="keyhold-ok")
    start = clock.now()
    outcome = wait_for_stable_pass(
        Prober(spec, server.ssh, server.http, clock),
        rule=JUDGE_POLICY.stable,
        clock=clock,
        interval_s=JUDGE_POLICY.interval_s,
        deadline_s=JUDGE_POLICY.deadline_s,
    )
    assert outcome.stable and outcome.passes[-1].at - start == JUDGE_POLICY.stable.min_gap_s


def test_bad_arguments_are_refused(clock) -> None:
    with pytest.raises(ValueError):
        wait_for_stable_pass(lambda: r(True, 0), clock=clock, interval_s=0)


# ---------------------------------------------------------------------------------------------
# Property: the tracker agrees with a direct reading of the rule
# ---------------------------------------------------------------------------------------------


def oracle(results: list[ProbeResult], gap: float) -> bool:
    """Stable iff the passes since the last failure span at least ``gap`` seconds."""
    run: list[ProbeResult] = []
    for x in results:
        run = run + [x] if x.ok else []
    return bool(run) and run[-1].at - run[0].at >= gap


@settings(max_examples=300, deadline=None)
@given(
    steps=st.lists(st.tuples(st.booleans(), st.sampled_from([0, 1, 5, 10, 15, 29, 30, 31, 60])), max_size=30),
    gap=st.sampled_from([10.0, 30.0]),
)
def test_tracker_matches_the_rule(steps: list[tuple[bool, int]], gap: float) -> None:
    tracker = StableTracker(StableRule(min_gap_s=gap))
    results: list[ProbeResult] = []
    at = 0.0
    for ok, dt in steps:
        at += dt
        results.append(r(ok, at))
        assert tracker.observe(results[-1]) == oracle(results, gap)
        if not ok:
            assert not tracker.stable
