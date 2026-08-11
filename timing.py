import math
import time
from collections.abc import Callable
from typing import Any, Protocol

MIN_SLEEP_SLICE = 0.001
SLEEP_POLL_INTERVAL_SECONDS = 0.05
INTERRUPT_WAIT_POLL_INTERVAL_SECONDS = 0.01
MAX_SLEEP_ITERATIONS = 120_000


class StopEventLike(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float) -> bool: ...


def duration_seconds(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    if not math.isfinite(number):
        number = float(default)
    return max(0.0, number)


def precise_sleep(duration: Any) -> None:
    wait_event(None, duration)


def sleep_until(deadline: float, stop_event: StopEventLike | None = None) -> bool:
    for _ in range(_sleep_iterations(deadline)):
        if stop_event is not None and stop_event.is_set():
            return False
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return stop_event is None or not stop_event.is_set()
        if stop_event is None:
            time.sleep(min(remaining, SLEEP_POLL_INTERVAL_SECONDS))
        elif stop_event.wait(min(remaining, SLEEP_POLL_INTERVAL_SECONDS)):
            return False
    return stop_event is None or not stop_event.is_set()


def wait_event(stop_event: StopEventLike | None, duration: Any) -> bool:
    duration = duration_seconds(duration)
    if duration <= 0:
        return stop_event is None or not stop_event.is_set()
    return sleep_until(time.perf_counter() + duration, stop_event)


def _sleep_iterations(deadline: float) -> int:
    remaining = max(0.0, deadline - time.perf_counter())
    return min(MAX_SLEEP_ITERATIONS, max(1, math.ceil(remaining / MIN_SLEEP_SLICE) + 3))


def interruptible_delay(
    duration: Any, interrupt_check: Callable[[], bool] | None = None
) -> bool:
    wait_time = duration_seconds(duration)
    if interrupt_check is None:
        precise_sleep(wait_time)
        return True
    return wait_event(InterruptAdapter(interrupt_check), wait_time)


class InterruptAdapter:
    def __init__(self, interrupt_check: Callable[[], bool]) -> None:
        self._interrupt_check = interrupt_check

    def is_set(self) -> bool:
        return bool(self._interrupt_check())

    def wait(self, timeout: float) -> bool:
        end_time = time.perf_counter() + max(0.0, timeout)
        for _ in range(_sleep_iterations(end_time)):
            if self.is_set():
                return True
            remaining = end_time - time.perf_counter()
            if remaining <= 0:
                return self.is_set()
            time.sleep(min(remaining, INTERRUPT_WAIT_POLL_INTERVAL_SECONDS))
        return self.is_set()
