import logging
import math
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

import config
from adb_session import AdbError, AdbSession
from forbidden_zones import (
    ForbiddenZone,
    configured_forbidden_zones,
    first_forbidden_zone_containing_point,
)
from timing import (
    StopEventLike,
    duration_seconds,
    interruptible_delay,
    precise_sleep,
    sleep_until,
)

logger = logging.getLogger(__name__)

Point = tuple[int, int]
WindowBounds = tuple[int, int, int, int]
MAX_STATS_UPGRADE_TAPS = 500
MAX_STATS_UPGRADE_BATCH = 32
DRAG_PATH_SAMPLES = 20
MIN_SWIPE_DURATION_MS = 1


def _as_bounds(bounds: Any) -> WindowBounds:
    if isinstance(bounds, (str, bytes, bytearray)) or not isinstance(bounds, Sequence):
        raise TypeError(
            f"expected a 4-item window bounds sequence, got {type(bounds).__name__}"
        )
    if len(bounds) != 4:
        raise ValueError(f"expected 4 window bounds values, got {len(bounds)}")
    x, y, width, height = (
        int(bounds[0]),
        int(bounds[1]),
        int(bounds[2]),
        int(bounds[3]),
    )
    if width <= 0 or height <= 0:
        raise ValueError(f"Target window has invalid client size: {width}x{height}")
    return x, y, width, height


class AdbController:
    """Sends touch input to the Android device instead of driving the PC mouse.

    Coordinates are supplied in scrcpy-window space exactly as before and are
    scaled to device pixels here, so callers and config values are unchanged.
    """

    def __init__(
        self,
        window_bounds_source: Any,
        click_delay: Any = None,
        move_delay: Any = None,
        session: AdbSession | None = None,
        device_size: tuple[int, int] | None = None,
        stop_event: StopEventLike | None = None,
    ) -> None:
        self._window_bounds_source = window_bounds_source
        self._stop_event = stop_event
        self._session = (
            session
            if session is not None
            else AdbSession(config.ADB_SERIAL, config.ADB_PATH)
        )
        self.click_delay = duration_seconds(
            config.CLICK_DELAY if click_delay is None else click_delay, 0.05
        )
        self.move_delay = duration_seconds(
            config.MOUSE_MOVE_DELAY if move_delay is None else move_delay, 0.0
        )
        self.touch_down_duration = duration_seconds(config.TOUCH_DOWN_DURATION)
        self._input_lock = threading.RLock()
        self._touch_is_down = False
        self._touch_down_point: Point = (0, 0)
        self._forbidden_zones = list(configured_forbidden_zones())
        self._device_width, self._device_height = self._resolve_device_size(device_size)
        self._motionevent_supported = self._session.supports_motionevent()
        if not self._motionevent_supported:
            logger.warning(
                "Device 'input' has no motionevent support; holds fall back to swipe "
                "and cannot be interrupted early"
            )
        logger.info(
            "ADB touch input ready: device=%s resolution=%sx%s",
            self._session.serial,
            self._device_width,
            self._device_height,
        )

    @property
    def session(self) -> AdbSession:
        return self._session

    @property
    def device_size(self) -> tuple[int, int]:
        return self._device_width, self._device_height

    def _resolve_device_size(
        self, device_size: tuple[int, int] | None
    ) -> tuple[int, int]:
        if device_size is not None:
            width, height = int(device_size[0]), int(device_size[1])
        elif config.DEVICE_WIDTH and config.DEVICE_HEIGHT:
            width, height = int(config.DEVICE_WIDTH), int(config.DEVICE_HEIGHT)
        else:
            width, height = self._session.device_size()
        if width <= 0 or height <= 0:
            raise AdbError(f"Invalid device resolution: {width}x{height}")
        return width, height

    def get_window_bounds(self) -> WindowBounds:
        try:
            source = (
                self._window_bounds_source()
                if callable(self._window_bounds_source)
                else self._window_bounds_source
            )
            return _as_bounds(source)
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc

    def get_window_position(self) -> Point:
        x, y, _, _ = self.get_window_bounds()
        return x, y

    def _stopped(self) -> bool:
        return self._stop_event is not None and self._stop_event.is_set()

    def _relative_position(
        self, x: Any, y: Any, relative: bool
    ) -> tuple[int, int, int, int] | None:
        try:
            window_x, window_y, width, height = self.get_window_bounds()
            if relative:
                rel_x, rel_y = int(x), int(y)
            else:
                rel_x, rel_y = int(x) - window_x, int(y) - window_y
        except (RuntimeError, TypeError, ValueError) as exc:
            logger.error("Cannot resolve input position: %s", exc)
            return None
        if not (0 <= rel_x < width and 0 <= rel_y < height):
            logger.warning(
                "Rejected input outside target window: relative=(%s, %s), bounds=%sx%s",
                rel_x,
                rel_y,
                width,
                height,
            )
            return None
        return rel_x, rel_y, width, height

    def _device_position(
        self, x: Any, y: Any, relative: bool = True, check_forbidden: bool = True
    ) -> Point | None:
        if self._stopped():
            return None
        position = self._relative_position(x, y, relative)
        if position is None:
            return None
        rel_x, rel_y, width, height = position
        if check_forbidden and self._blocked(rel_x, rel_y):
            return None
        device_x = int(round(rel_x * self._device_width / width))
        device_y = int(round(rel_y * self._device_height / height))
        return (
            max(0, min(self._device_width - 1, device_x)),
            max(0, min(self._device_height - 1, device_y)),
        )

    def _blocked(self, rel_x: int, rel_y: int) -> bool:
        zone = first_forbidden_zone_containing_point(
            rel_x, rel_y, self._forbidden_zones
        )
        if zone is None:
            return False
        logger.debug("Coordinates (%s, %s) blocked by %s", rel_x, rel_y, zone.name)
        return True

    def is_in_forbidden_zone(self, x: Any, y: Any, relative: bool = True) -> bool:
        position = self._relative_position(x, y, relative)
        if position is None:
            return True
        rel_x, rel_y, _, _ = position
        return self._blocked(rel_x, rel_y)

    def _send(self, command: str) -> bool:
        if self._stopped():
            return False
        return self._session.execute(command)

    def move_to(self, x: Any, y: Any, relative: bool = True) -> bool:
        """Kept for API compatibility; touch input has no hover pointer."""
        return self._device_position(x, y, relative=relative) is not None

    def click(self, x: Any, y: Any, relative: bool = True, delay: Any = None) -> bool:
        return self._tap(x, y, relative, delay)

    def precise_click(
        self, x: Any, y: Any, relative: bool = True, delay: Any = None
    ) -> bool:
        """Identical to click: injected taps land exactly on the requested pixel."""
        return self._tap(x, y, relative, delay)

    def _tap(self, x: Any, y: Any, relative: bool, delay: Any) -> bool:
        with self._input_lock:
            position = self._device_position(x, y, relative=relative)
            if position is None:
                return False
            if not self._tap_device_point(*position):
                return False
            precise_sleep(
                self.click_delay
                if delay is None
                else duration_seconds(delay, self.click_delay)
            )
            return True

    def _tap_device_point(self, device_x: int, device_y: int) -> bool:
        if self.touch_down_duration > 0 and self._motionevent_supported:
            return self._held_tap(device_x, device_y, self.touch_down_duration)
        return self._send(f"input tap {device_x} {device_y}")

    def _held_tap(self, device_x: int, device_y: int, duration: float) -> bool:
        if not self._press(device_x, device_y):
            return False
        precise_sleep(duration)
        return self._release(device_x, device_y)

    def _press(self, device_x: int, device_y: int) -> bool:
        self._touch_is_down = True
        self._touch_down_point = (device_x, device_y)
        if self._send(f"input motionevent DOWN {device_x} {device_y}"):
            return True
        self._release(device_x, device_y)
        return False

    def _release(self, device_x: int, device_y: int) -> bool:
        """Releases at the press point; a different point would become a swipe.

        Bypasses the stop-event gate so a held touch is never left down.
        """
        released = self._session.execute(f"input motionevent UP {device_x} {device_y}")
        if released:
            self._touch_is_down = False
        else:
            logger.error(
                "Could not release touch at device (%s, %s)", device_x, device_y
            )
        return released

    def release_touch(self) -> bool:
        with self._input_lock:
            if not self._touch_is_down:
                return True
            return self._release(*self._touch_down_point)

    def hold_at(
        self,
        x: Any,
        y: Any,
        duration: Any = None,
        relative: bool = True,
        interrupt_check: Callable[[], bool] | None = None,
    ) -> bool:
        with self._input_lock:
            position = self._device_position(x, y, relative=relative)
            if position is None:
                return False
            hold_duration = duration_seconds(4.0 if duration is None else duration, 4.0)
            if not self._motionevent_supported:
                return self._swipe_hold(position, hold_duration)
            return self._motionevent_hold(position, hold_duration, interrupt_check)

    def _motionevent_hold(
        self,
        position: Point,
        hold_duration: float,
        interrupt_check: Callable[[], bool] | None,
    ) -> bool:
        device_x, device_y = position
        if not self._press(device_x, device_y):
            return False
        try:
            completed = interruptible_delay(hold_duration, interrupt_check)
        except BaseException:
            self._release(device_x, device_y)
            raise
        if not self._release(device_x, device_y):
            return False
        if completed:
            precise_sleep(self.click_delay)
        return completed

    def _swipe_hold(self, position: Point, hold_duration: float) -> bool:
        device_x, device_y = position
        milliseconds = max(MIN_SWIPE_DURATION_MS, int(round(hold_duration * 1000)))
        if not self._send(
            f"input swipe {device_x} {device_y} {device_x} {device_y} {milliseconds}"
        ):
            return False
        precise_sleep(self.click_delay)
        return True

    def click_stats_upgrade_at(
        self,
        x: Any,
        y: Any,
        duration: Any,
        click_delay: Any,
        relative: bool = True,
        interrupt_check: Callable[[], bool] | None = None,
    ) -> bool:
        duration = duration_seconds(duration)
        click_delay = duration_seconds(click_delay)
        if duration <= 0 or click_delay <= 0:
            logger.warning(
                "Rejected stats upgrade tap loop: duration=%.3f delay=%.3f",
                duration,
                click_delay,
            )
            return False
        with self._input_lock:
            position = self._device_position(x, y, relative=relative)
            if position is None:
                return False
            tap_count = self._stats_upgrade_tap_loop(
                position, duration, click_delay, interrupt_check
            )
            if tap_count is None:
                return False
            logger.debug("Stats upgrade tapping complete: %s taps", tap_count)
            return tap_count > 0

    def _stats_upgrade_tap_loop(
        self,
        position: Point,
        duration: float,
        click_delay: float,
        interrupt_check: Callable[[], bool] | None,
    ) -> int | None:
        device_x, device_y = position
        total = min(MAX_STATS_UPGRADE_TAPS, max(1, math.ceil(duration / click_delay)))
        batch_size = max(
            1, min(MAX_STATS_UPGRADE_BATCH, int(config.TOUCH_TAP_BATCH_SIZE))
        )
        deadline = time.perf_counter() + duration
        sent = 0
        while sent < total:
            if interrupt_check and interrupt_check():
                return None
            if time.perf_counter() >= deadline:
                break
            count = min(batch_size, total - sent)
            if not self._send(self._batched_tap_command(device_x, device_y, count)):
                return None
            sent += count
            if sent >= total:
                break
            next_tap_at = min(deadline, time.perf_counter() + click_delay)
            if not sleep_until(next_tap_at, self._stop_event):
                return None
        return sent

    @staticmethod
    def _batched_tap_command(device_x: int, device_y: int, count: int) -> str:
        """Backgrounds taps so the device overlaps their JVM startup cost.

        Every tap targets the same pixel, so completion order does not matter.
        """
        tap = f"input tap {device_x} {device_y}"
        if count <= 1:
            return tap
        return " ".join([f"{tap} &"] * count) + " wait"

    def drag(
        self,
        from_x: Any,
        from_y: Any,
        to_x: Any,
        to_y: Any,
        duration: Any = None,
        relative: bool = True,
    ) -> bool:
        with self._input_lock:
            if not self._drag_path_allowed(from_x, from_y, to_x, to_y, relative):
                return False
            start = self._device_position(from_x, from_y, relative=relative)
            end = self._device_position(to_x, to_y, relative=relative)
            if start is None or end is None:
                return False
            seconds = max(
                0.01,
                duration_seconds(
                    config.SCROLL_DURATION if duration is None else duration,
                    config.SCROLL_DURATION,
                ),
            )
            milliseconds = max(MIN_SWIPE_DURATION_MS, int(round(seconds * 1000)))
            if not self._send(
                f"input swipe {start[0]} {start[1]} {end[0]} {end[1]} {milliseconds}"
            ):
                return False
            precise_sleep(self.click_delay)
            return True

    def _drag_path_allowed(
        self, from_x: Any, from_y: Any, to_x: Any, to_y: Any, relative: bool
    ) -> bool:
        start = self._relative_position(from_x, from_y, relative)
        end = self._relative_position(to_x, to_y, relative)
        if start is None or end is None:
            return False
        start_x, start_y = start[0], start[1]
        end_x, end_y = end[0], end[1]
        for index in range(DRAG_PATH_SAMPLES + 1):
            ratio = index / DRAG_PATH_SAMPLES
            sample_x = int(round(start_x + (end_x - start_x) * ratio))
            sample_y = int(round(start_y + (end_y - start_y) * ratio))
            if self._blocked(sample_x, sample_y):
                return False
        return True

    def close(self) -> None:
        self.release_touch()
        self._session.close()
