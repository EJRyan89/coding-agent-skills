"""The one timer of a scaling test, and its own tests.

`assert_time_in_proportion` holds that reading a larger input takes less than a bound times as long as reading a
smaller one. Every scaling ratio test in the repository times through it, importing it from this suite as the suites
under skills/, `.claude/skills/`, and `tests/` import other suites' helpers, so each reads the same clock the same way.
"""

from __future__ import annotations

import contextlib
import functools
import gc
import itertools
import math
import time
from collections.abc import Callable

import pytest

# The least processor time the smaller input's reading is timed for, repeated as often as that takes, so the clock's
# tick, about 16 ms on Windows, is a small part of each measurement; and how many times each input is timed.
LEAST_TIMED_SECONDS = 0.1
TIMINGS = 3


def _seconds[T](read: Callable[[T], object], value: T, repeats: int, limit: float = math.inf) -> float:
    """The processor time the process spends reading `value` `repeats` times, or, once that passes `limit`, the time
    so far, so a reading far slower than the bound fails in about the bound's time rather than the reading's. A runner
    whose pool runs more suites than it has processors shares each processor between them, so a reading long enough to
    span several of the scheduler's slices takes about twice its own time on the wall clock, while a short one may run
    in one slice alone: timed by the wall clock, a linear reading measured about twice its ratio there.

    The cyclic garbage collector is off while it times, as `timeit` turns it off. A collection of the older generations
    walks every object the process holds, and a reading that keeps more of its objects alive at once, as on a longer
    input, moves more of them into those generations, so the collector's share grows faster than the input, and faster
    again the more objects the process holds: a linear reading measured about 1.5 times its ratio with it on in a
    process holding two million objects."""
    collecting = gc.isenabled()
    gc.disable()
    try:
        started = time.process_time()
        for _ in range(repeats):
            read(value)
            if (elapsed := time.process_time() - started) > limit:
                return elapsed
        return time.process_time() - started
    finally:
        if collecting:
            gc.enable()


@functools.cache
def _clock_tick() -> float:
    """The step `time.process_time` advances by, the most of two: 15.625 ms on Windows, where it counts whole ticks of
    the scheduler's clock, and a microsecond or less on Linux and macOS. Each step read starts on a value the clock
    shows, so it is a whole step."""
    steps: list[float] = []
    for _ in range(2):
        started = time.process_time()
        while (now := time.process_time()) == started:
            pass
        steps.append(now - started)
    return max(steps)


def assert_time_in_proportion[T](read: Callable[[T], object], small: T, large: T, bound: float) -> None:
    """Fail when reading `large` takes `bound` times as long as reading `small`, or longer, on every timing. Each
    reading repeats until the smaller takes LEAST_TIMED_SECONDS; the smaller keeps the least of TIMINGS timings, and
    the larger passes at its first timing under the bound, so only a reading slower on every try fails. The bound
    sits between the ratio a linear reading gives and the one the slower reading the test guards against gives, with
    a margin of about two on each side, so neither a noisy runner nor that reading lands on the wrong side.

    A timing read off a clock that advances in ticks comes out up to a tick short or long, and the least of several
    timings is the one most likely short: on Windows, where the smaller reading takes about seven ticks, that raised
    a ratio by up to a seventh. So the smaller reading's least timing counts one tick more, which no tick's rounding
    exceeds, and the ratio is never raised by it; the larger reading takes the bound times as many ticks, so its own
    rounding moves it by less than one part in a hundred."""
    repeats = 1
    while (elapsed := _seconds(read, small, repeats)) < LEAST_TIMED_SECONDS:
        repeats *= 2
    least = min([elapsed, *(_seconds(read, small, repeats) for _ in range(TIMINGS - 1))]) + _clock_tick()
    timed: list[float] = []
    for _ in range(TIMINGS):
        timed.append(_seconds(read, large, repeats, bound * least))
        if timed[-1] < bound * least:
            return
    pytest.fail(f"{min(timed):.4f}s is over {min(timed) / least:.1f} times {least:.4f}s, at least {bound:.1f} times")


# The step of Windows' processor-time clock.
WINDOWS_TICK = 0.015625


def test_the_clock_tick_is_the_step_process_time_advances_by(monkeypatch: pytest.MonkeyPatch) -> None:
    # A clock that counts whole ticks, as Windows' does, read three times in each tick.
    readings = itertools.count()
    monkeypatch.setattr(time, "process_time", lambda: next(readings) // 3 * WINDOWS_TICK)
    assert _clock_tick.__wrapped__() == WINDOWS_TICK


@pytest.mark.parametrize(
    ("tick", "outcome"),
    [
        pytest.param(WINDOWS_TICK, contextlib.nullcontext(), id="a-tick-added"),
        pytest.param(0.0, pytest.raises(pytest.fail.Exception, match=r"at least 32\.0 times"), id="none-added"),
    ],
)
def test_a_shorter_timing_read_a_tick_short_raises_no_ratio_over_the_bound(
    monkeypatch: pytest.MonkeyPatch, tick: float, outcome: contextlib.AbstractContextManager[object]
) -> None:
    # The shorter reading takes almost eight ticks and every timing of it reads seven, while the longer one takes 240:
    # 30 times the shorter one's own time, under the bound of 32, but over 32 times the seven ticks read. Counted one
    # tick longer, the shorter reading is never read short, so only a helper that adds no tick fails it.
    clock = [0.0]
    ticks = {"shorter": 7, "longer": 240}

    def read(value: str) -> None:
        clock[0] += ticks[value] * WINDOWS_TICK

    monkeypatch.setattr(time, "process_time", lambda: clock[0])
    monkeypatch.setitem(globals(), "_clock_tick", lambda: tick)
    with outcome:
        assert_time_in_proportion(read, "shorter", "longer", 32)


def test_the_collector_is_off_while_a_reading_is_timed_and_on_after_it_ends_stops_or_raises() -> None:
    collecting: list[bool] = []

    def read(value: str) -> None:
        collecting.append(gc.isenabled())
        if value == "raise":
            raise ValueError(value)

    _seconds(read, "read", 2)
    assert gc.isenabled()
    # A limit every reading passes stops the timing after its first.
    _seconds(read, "stop", 3, limit=-1.0)
    assert gc.isenabled()
    with pytest.raises(ValueError, match="raise"):
        _seconds(read, "raise", 1)
    assert gc.isenabled()
    assert collecting == [False, False, False, False]


def test_a_collector_off_before_a_timing_stays_off_after_it() -> None:
    gc.disable()
    try:
        _seconds(lambda value: None, "read", 1)
        assert not gc.isenabled()
    finally:
        gc.enable()


class ScriptedReadings:
    """A processor-time clock that only readings advance: each reading of a value adds its next cost, from the costs
    given for that value, the last repeated once they run out, and counts the reading."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, costs: dict[str, list[float]]) -> None:
        self.clock = 0.0
        self.costs = {value: iter(each) for value, each in costs.items()}
        self.last = {value: each[-1] for value, each in costs.items()}
        self.reads = dict.fromkeys(costs, 0)
        monkeypatch.setattr(time, "process_time", lambda: self.clock)
        monkeypatch.setitem(globals(), "_clock_tick", lambda: 0.0)

    def read(self, value: str) -> None:
        self.clock += next(self.costs[value], self.last[value])
        self.reads[value] += 1


def test_the_smaller_reading_repeats_until_it_takes_the_least_timed_seconds_and_a_larger_one_under_the_bound_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One reading of the smaller value takes 0.03 s, so it is timed once, twice, and four times before a timing reaches
    # 0.1 s, then twice more at four; the larger one, eight times as long, passes at its first timing of four.
    readings = ScriptedReadings(monkeypatch, {"smaller": [0.03], "larger": [0.24]})
    assert_time_in_proportion(readings.read, "smaller", "larger", 32)
    assert readings.reads == {"smaller": 1 + 2 + 4 + 4 + 4, "larger": 4}


def test_a_larger_reading_over_the_bound_on_every_timing_fails_each_stopped_once_past_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The smaller reading's four take 0.12 s, so the bound of 32 is 3.84 s; the larger one takes 3 s a reading, so each
    # of its timings stops at its second reading, 6 s, rather than running all four.
    readings = ScriptedReadings(monkeypatch, {"smaller": [0.03], "larger": [3.0]})
    with pytest.raises(pytest.fail.Exception, match=r"^6\.0000s is over 50\.0 times 0\.1200s, at least 32\.0 times$"):
        assert_time_in_proportion(readings.read, "smaller", "larger", 32)
    assert readings.reads["larger"] == 2 * TIMINGS


def test_a_larger_reading_over_the_bound_until_its_last_timing_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    # A runner's noise slows the larger reading's first timings past the bound, each stopped at its second reading; the
    # last timing, at eight times the smaller one's time, is under it.
    noisy = [3.0] * 2 * (TIMINGS - 1)
    readings = ScriptedReadings(monkeypatch, {"smaller": [0.03], "larger": [*noisy, 0.24]})
    assert_time_in_proportion(readings.read, "smaller", "larger", 32)
    assert readings.reads["larger"] == len(noisy) + 4


def test_the_smaller_reading_keeps_the_least_of_its_timings(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each reading of the smaller value takes 0.1 s or more, so it is timed once a timing: first slow at 0.3 s, then
    # 0.1 s, then 0.2 s. The larger one takes 0.5 s, five times the least timing and over the bound of 3, but under
    # three times the first, the last, or the most, so only a helper that keeps the least fails it.
    readings = ScriptedReadings(monkeypatch, {"smaller": [0.3, 0.1, 0.2], "larger": [0.5]})
    with pytest.raises(pytest.fail.Exception, match=r"^0\.5000s is over 5\.0 times 0\.1000s, at least 3\.0 times$"):
        assert_time_in_proportion(readings.read, "smaller", "larger", 3)


def test_a_failure_names_the_least_of_the_larger_reading_s_timings(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every timing of the larger reading is over the bound, at 0.9 s, 0.5 s, and 0.7 s, so the failure names 0.5 s, the
    # one nearest to passing.
    readings = ScriptedReadings(monkeypatch, {"smaller": [0.1], "larger": [0.9, 0.5, 0.7]})
    with pytest.raises(pytest.fail.Exception, match=r"^0\.5000s is over 5\.0 times 0\.1000s, at least 3\.0 times$"):
        assert_time_in_proportion(readings.read, "smaller", "larger", 3)
