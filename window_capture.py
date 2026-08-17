import logging
import threading
from collections.abc import Callable
from typing import Any

import numpy as np

import window_backend

_MSS_IMPORT_ERROR: Exception | None
_PYWINCTL_IMPORT_ERROR: Exception | None
mss_module: Any = None
pywinctl_module: Any = None

try:
    import mss as imported_mss
except Exception as exc:
    _MSS_IMPORT_ERROR = exc
else:
    mss_module = imported_mss
    _MSS_IMPORT_ERROR = None

try:
    import pywinctl as imported_pywinctl
except Exception as exc:
    _PYWINCTL_IMPORT_ERROR = exc
else:
    pywinctl_module = imported_pywinctl
    _PYWINCTL_IMPORT_ERROR = None

logger = logging.getLogger(__name__)

WindowRect = tuple[int, int, int, int]
ScreenshotterProvider = Callable[[], Any]
FrameCapture = Callable[[Any, ScreenshotterProvider], np.ndarray]
_MISSING = object()

# Capture backends. "printwindow" renders the window's own client area and keeps
# working while the window is covered or unfocused; "mss" grabs the screen
# region the window occupies and therefore captures whatever is on top of it.
CAPTURE_BACKEND_AUTO = "auto"
CAPTURE_BACKEND_WINDOW = "printwindow"
CAPTURE_BACKEND_SCREEN = "mss"
SUPPORTED_CAPTURE_BACKENDS = (
    CAPTURE_BACKEND_AUTO,
    CAPTURE_BACKEND_WINDOW,
    CAPTURE_BACKEND_SCREEN,
)

# Consecutive PrintWindow failures tolerated in "auto" mode before the backend
# is abandoned for the rest of the run.
WINDOW_BACKEND_FAILURE_LIMIT = 5


class WindowCaptureError(RuntimeError):
    pass


class WindowNotAvailableError(WindowCaptureError):
    pass


def _create_screenshotter() -> Any:
    if mss_module is None:
        raise WindowCaptureError(
            f"Cannot initialize screenshot backend: {_MSS_IMPORT_ERROR}"
        )
    # mss 10.x renamed the class to MSS and deprecated the lowercase alias,
    # which it drops in a future release.
    factory = getattr(mss_module, "MSS", None) or mss_module.mss
    try:
        return factory()
    except Exception as exc:
        raise WindowCaptureError(
            f"Cannot initialize screenshot backend: {exc}"
        ) from exc


def _close_screenshotter(screenshotter: Any) -> None:
    close = getattr(screenshotter, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception as exc:
        logger.debug("Screenshot backend close failed: %s", exc)


def _geometry_attribute_value(geometry: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        if hasattr(geometry, name):
            return getattr(geometry, name)
    return _MISSING


def _geometry_mapping_value(geometry: Any, names: tuple[str, ...]) -> Any:
    if not isinstance(geometry, dict):
        return _MISSING
    for name in names:
        if name in geometry:
            return geometry[name]
    return _MISSING


def _geometry_value(geometry: Any, names: tuple[str, ...], index: int) -> Any:
    value = _geometry_attribute_value(geometry, names)
    if value is not _MISSING:
        return value
    value = _geometry_mapping_value(geometry, names)
    if value is not _MISSING:
        return value
    return geometry[index]


def _bounds_from_geometry(geometry: Any) -> WindowRect:
    left = int(_geometry_value(geometry, ("left", "x"), 0))
    top = int(_geometry_value(geometry, ("top", "y"), 1))
    if hasattr(geometry, "right") or (
        isinstance(geometry, dict) and "right" in geometry
    ):
        right = int(_geometry_value(geometry, ("right",), 2))
        bottom = int(_geometry_value(geometry, ("bottom",), 3))
        return left, top, right - left, bottom - top
    width = int(_geometry_value(geometry, ("width", "w"), 2))
    height = int(_geometry_value(geometry, ("height", "h"), 3))
    return left, top, width, height


class WindowCapture:
    def __init__(
        self,
        window_title: str,
        target_width: int = 800,
        target_height: int = 600,
        backend: str = CAPTURE_BACKEND_AUTO,
    ) -> None:
        self.window_title = str(window_title)
        self.target_width = int(target_width)
        self.target_height = int(target_height)
        if self.target_width <= 0 or self.target_height <= 0:
            raise WindowCaptureError(
                f"Invalid target window size: {self.target_width}x{self.target_height}"
            )
        self.hwnd = None
        self._window = None
        self._lock = threading.RLock()
        self._backend_lock = threading.RLock()
        self._backend = str(backend).strip().lower() or CAPTURE_BACKEND_AUTO
        if self._backend not in SUPPORTED_CAPTURE_BACKENDS:
            raise WindowCaptureError(
                f"Unsupported capture backend '{backend}'; expected one of "
                f"{', '.join(SUPPORTED_CAPTURE_BACKENDS)}"
            )
        # None until the backend has been probed against the live window.
        self._window_backend_enabled: bool | None = None
        self._window_backend_failures = 0
        self._reported_client_size: tuple[int, int] | None = None
        self._screenshotter: Any = None
        self._resolve_window_backend()
        if self._backend != CAPTURE_BACKEND_WINDOW:
            self._screenshotter = _create_screenshotter()
        try:
            self.ensure_window(resize=True)
        except WindowCaptureError as exc:
            logger.warning("%s", exc)

    def _resolve_window_backend(self) -> None:
        if self._backend == CAPTURE_BACKEND_SCREEN:
            self._window_backend_enabled = False
            return
        if window_backend.backend_available():
            return
        import_error = window_backend.backend_import_error()
        if self._backend == CAPTURE_BACKEND_WINDOW:
            raise WindowCaptureError(
                f"Occlusion-proof capture requires pywin32: {import_error}"
            )
        self._window_backend_enabled = False
        logger.warning(
            "Occlusion-proof capture unavailable (%s); falling back to screen capture, "
            "so keep the window unobstructed",
            import_error,
        )

    @staticmethod
    def _alive(window: Any) -> bool:
        alive = getattr(window, "isAlive", None)
        try:
            return bool(
                alive() if callable(alive) else True if alive is None else alive
            )
        except Exception:
            return False

    @staticmethod
    def _handle(window: Any) -> Any:
        getter = getattr(window, "getHandle", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                return None
        return getattr(window, "handle", None)

    @staticmethod
    def _title(window: Any) -> str | None:
        try:
            title = getattr(window, "title", None)
        except Exception:
            return None
        return str(title) if title is not None else None

    @staticmethod
    def _minimized(window: Any) -> bool:
        minimized = getattr(window, "isMinimized", False)
        try:
            return bool(minimized() if callable(minimized) else minimized)
        except Exception:
            return False

    def _find_window(self) -> Any | None:
        if pywinctl_module is None:
            raise WindowCaptureError(
                f"Could not initialize window backend: {_PYWINCTL_IMPORT_ERROR}"
            )
        try:
            windows = pywinctl_module.getWindowsWithTitle(self.window_title) or []
        except Exception as exc:
            raise WindowCaptureError(
                f"Could not search for window '{self.window_title}': {exc}"
            ) from exc
        live_windows = [window for window in windows if self._alive(window)]
        exact_windows = [
            window
            for window in live_windows
            if self._title(window) == self.window_title
        ]
        if len(exact_windows) > 1:
            raise WindowCaptureError(
                f"Multiple live windows have title '{self.window_title}'"
            )
        return exact_windows[0] if exact_windows else None

    def ensure_window(self, resize: bool = False) -> Any:
        with self._lock:
            window = self._ensure_window_reference()
            if resize:
                self._resize()
            return window

    def _ensure_window_reference(self) -> Any:
        if self._window is not None and self._alive(self._window):
            return self._window
        self._window = self._find_window()
        if self._window is None:
            self.hwnd = None
            raise WindowNotAvailableError(f"Window '{self.window_title}' not found")
        self.hwnd = self._handle(self._window)
        logger.info("Window found: %s (handle: %s)", self.window_title, self.hwnd)
        return self._window

    def _resize(self) -> None:
        if self._window is None or not self._alive(self._window):
            return
        try:
            resized = self._window.resizeTo(
                self.target_width, self.target_height, wait=True
            )
        except Exception as exc:
            raise WindowCaptureError(
                f"Resizing window '{self.window_title}' failed: {exc}"
            ) from exc
        if resized is False:
            raise WindowCaptureError(f"Resizing window '{self.window_title}' failed")
        logger.info("Window resized to %sx%s", self.target_width, self.target_height)
        self._report_client_size()

    def _report_client_size(self) -> None:
        """Logs the real client area, which is what coordinates are scaled to."""
        try:
            _, _, width, height = self.get_window_rect()
        except WindowCaptureError as exc:
            logger.debug("Could not read client size after resize: %s", exc)
            return
        if width == self.target_width and height == self.target_height:
            return
        if self._reported_client_size == (width, height):
            return
        self._reported_client_size = (width, height)
        # resizeTo sets the outer window size, so the client area comes out
        # smaller by the border and title bar. Capture and click coordinates
        # both live in client space, so they must be calibrated against this
        # size rather than the configured one -- the difference below is the
        # window chrome, not an error to correct in the coordinates.
        logger.info(
            "Window '%s' client area is %sx%s after resizing the frame to %sx%s "
            "(%s x %s of window chrome); coordinates are client-relative and "
            "must be calibrated to %sx%s",
            self.window_title,
            width,
            height,
            self.target_width,
            self.target_height,
            self.target_width - width,
            self.target_height - height,
            width,
            height,
        )

    def find_window(self) -> Any:
        return self.ensure_window()

    def get_hwnd(self) -> Any:
        self.ensure_window()
        return self.hwnd

    def resize_window(self) -> None:
        with self._lock:
            self.ensure_window()
            self._resize()

    def get_window_rect(self) -> WindowRect:
        with self._lock:
            window = self.ensure_window()
            x, y, width, height = self._window_bounds(window)
            if width <= 0 or height <= 0:
                # A window reports a degenerate rect while minimized or mid
                # restore, which the user can undo, so this is a pause signal
                # rather than a fatal capture failure.
                raise WindowNotAvailableError(
                    f"Window '{self.window_title}' has invalid size: {width}x{height}"
                )
            return x, y, width, height

    def activate_for_input(self) -> None:
        """Best-effort raise so the mirrored view is unobstructed at startup.

        Skipped entirely when occlusion-proof capture works, because stealing
        focus is exactly what that backend exists to avoid. Touch input is
        delivered over ADB, so focus is never required; a failure here is
        logged rather than fatal.
        """
        if self.occlusion_proof_capture_available():
            logger.info(
                "Occlusion-proof capture active for '%s'; leaving window focus and "
                "position untouched. You can cover it and keep using the computer",
                self.window_title,
            )
            return
        with self._lock:
            window = self.ensure_window()
            activate = getattr(window, "activate", None)
            if not callable(activate):
                logger.warning(
                    "Window '%s' cannot be raised; ensure it stays visible",
                    self.window_title,
                )
                return
            try:
                activate(wait=True)
            except Exception as exc:
                logger.warning(
                    "Raising window '%s' failed: %s; ensure it stays visible",
                    self.window_title,
                    exc,
                )

    def get_input_window_rect(self) -> WindowRect:
        """Client rect used to map coordinates onto the device.

        The window must exist and be on-screen for capture to be meaningful,
        but it does not need keyboard focus.
        """
        with self._lock:
            window = self.ensure_window()
            if self._title(window) != self.window_title:
                raise WindowNotAvailableError(
                    f"Window '{self.window_title}' is unavailable; input rejected"
                )
            if self._minimized(window):
                raise WindowNotAvailableError(
                    f"Window '{self.window_title}' is minimized; input rejected"
                )
            return self.get_window_rect()

    def _window_bounds(self, window: Any) -> WindowRect:
        try:
            return _bounds_from_geometry(window.getClientFrame())
        except Exception:
            return self._window_box_bounds(window)

    def _window_box_bounds(self, window: Any) -> WindowRect:
        try:
            return _bounds_from_geometry(window.box)
        except Exception as exc:
            raise WindowCaptureError(
                f"Cannot read window bounds for '{self.window_title}': {exc}"
            ) from exc

    def capture(self, max_y: Any = None) -> np.ndarray:
        return self._capture(max_y, self._shared_screenshotter)

    def create_frame_source(self, max_height: Any = None) -> "WindowFrameSource":
        """Returns a frame producer a background thread can own exclusively.

        PrintWindow keeps no per-call state, but mss instances are not
        thread-safe, so a source that has to fall back to screen capture
        creates its own handle.
        """
        return WindowFrameSource(self._capture, max_height)

    def occlusion_proof_capture_available(self) -> bool:
        """True when the window can be captured while covered or unfocused."""
        if self._window_backend_enabled is not None:
            return self._window_backend_enabled
        try:
            hwnd = self._capture_target_handle()
        except WindowNotAvailableError as exc:
            logger.debug("Cannot probe occlusion-proof capture yet: %s", exc)
            return False
        try:
            self._probe_window_backend(hwnd)
        except window_backend.WindowBackendError as exc:
            logger.debug("Occlusion-proof capture probe failed: %s", exc)
            return False
        return bool(self._window_backend_enabled)

    def _capture(
        self, max_y: Any, screenshotter_provider: ScreenshotterProvider
    ) -> np.ndarray:
        hwnd = self._capture_target_handle()
        image = self._window_backend_image(hwnd, max_y)
        if image is not None:
            return image
        return self._screen_region_image(max_y, screenshotter_provider)

    def _capture_target_handle(self) -> Any:
        with self._lock:
            window = self.ensure_window()
            if self._minimized(window):
                raise WindowNotAvailableError(
                    f"Window '{self.window_title}' is minimized and cannot be captured"
                )
            return self.hwnd

    def _window_backend_image(self, hwnd: Any, max_y: Any) -> np.ndarray | None:
        if self._window_backend_enabled is False or hwnd is None:
            return None
        try:
            if self._window_backend_enabled is None:
                self._probe_window_backend(hwnd)
            if not self._window_backend_enabled:
                return None
            image = window_backend.capture_client_area(hwnd, max_y)
        except window_backend.WindowSurfaceUnavailableError as exc:
            raise WindowNotAvailableError(
                f"Window '{self.window_title}' cannot be captured: {exc}"
            ) from exc
        except window_backend.WindowBackendError as exc:
            self._record_window_backend_failure(exc)
            return None
        self._window_backend_failures = 0
        return self._decoded_bgr_image(image)

    def _probe_window_backend(self, hwnd: Any) -> None:
        with self._backend_lock:
            if self._window_backend_enabled is not None:
                return
            usable, detail = window_backend.probe(hwnd)
            if usable:
                self._window_backend_enabled = True
                logger.info(
                    "Occlusion-proof capture enabled for '%s' (%s); other windows may "
                    "cover it without affecting detection",
                    self.window_title,
                    detail,
                )
                return
            if self._backend == CAPTURE_BACKEND_WINDOW:
                raise WindowCaptureError(
                    f"Occlusion-proof capture of '{self.window_title}' is not "
                    f"possible: {detail}"
                )
            self._window_backend_enabled = False
            logger.warning(
                "Occlusion-proof capture unavailable for '%s' (%s); falling back to "
                "screen capture, so keep the window unobstructed",
                self.window_title,
                detail,
            )

    def _record_window_backend_failure(self, exc: Exception) -> None:
        if self._backend == CAPTURE_BACKEND_WINDOW:
            raise WindowCaptureError(
                f"Occlusion-proof capture of '{self.window_title}' failed: {exc}"
            ) from exc
        with self._backend_lock:
            self._window_backend_failures += 1
            failures = self._window_backend_failures
            if failures < WINDOW_BACKEND_FAILURE_LIMIT:
                logger.debug(
                    "Occlusion-proof capture failed (%s); using screen capture for "
                    "this frame",
                    exc,
                )
                return
            self._window_backend_enabled = False
        logger.warning(
            "Disabling occlusion-proof capture for '%s' after %s consecutive "
            "failures (%s); keep the window unobstructed",
            self.window_title,
            failures,
            exc,
        )

    def _shared_screenshotter(self) -> Any:
        with self._lock:
            if self._screenshotter is None:
                self._screenshotter = _create_screenshotter()
            return self._screenshotter

    def _screen_region_image(
        self, max_y: Any, screenshotter_provider: ScreenshotterProvider
    ) -> np.ndarray:
        with self._lock:
            x, y, width, height = self.get_window_rect()
        if max_y is not None:
            height = min(height, int(max_y))
        if width <= 0 or height <= 0:
            raise WindowNotAvailableError(
                f"Window '{self.window_title}' cannot be captured with size "
                f"{width}x{height}"
            )
        image = self._grab_window_image(screenshotter_provider(), x, y, width, height)
        return self._decoded_bgr_image(image)

    def _grab_window_image(
        self, screenshotter: Any, x: int, y: int, width: int, height: int
    ) -> np.ndarray:
        try:
            screenshot = screenshotter.grab(
                {"left": x, "top": y, "width": width, "height": height}
            )
            return np.asarray(screenshot)
        except Exception as exc:
            if not self.is_window_available():
                raise WindowNotAvailableError(
                    f"Window '{self.window_title}' is no longer available"
                ) from exc
            raise WindowCaptureError(
                f"Capturing window '{self.window_title}' failed: {exc}"
            ) from exc

    @staticmethod
    def _decoded_bgr_image(image: np.ndarray) -> np.ndarray:
        if image.ndim != 3 or image.shape[2] < 3:
            raise WindowCaptureError(f"Captured image has invalid shape: {image.shape}")
        decoded = image[:, :, :3].astype(np.uint8, copy=False)
        return decoded if decoded.flags.c_contiguous else np.ascontiguousarray(decoded)

    def _refresh_window_reference(self) -> Any | None:
        if (
            self._window is None
            or not self._alive(self._window)
            or self._title(self._window) != self.window_title
        ):
            self._window = self._find_window()
        self.hwnd = self._handle(self._window) if self._window is not None else None
        return self._window

    def is_window_available(self) -> bool:
        """True when the window exists and can be captured.

        Neither focus nor being on top matters: PrintWindow renders the client
        area regardless, and ADB delivers input without the cursor. Only a
        minimized window has no surface to read.
        """
        with self._lock:
            window = self._refresh_window_reference()
            return window is not None and not self._minimized(window)

    def close(self) -> None:
        with self._lock:
            screenshotter = self._screenshotter
            self._screenshotter = None
        if screenshotter is not None:
            _close_screenshotter(screenshotter)


class WindowFrameSource:
    """Single-owner frame producer used by background capture threads.

    `grab` reports a missed frame as None instead of raising, so a worker can
    idle through a minimized or missing window and pick up again afterwards.
    """

    def __init__(self, capture: FrameCapture, max_height: Any = None) -> None:
        self._capture = capture
        self._max_height = max_height
        self._screenshotter: Any = None

    def grab(self) -> np.ndarray | None:
        try:
            return self._capture(self._max_height, self._provide)
        except WindowCaptureError as exc:
            logger.debug("Frame source could not capture a frame: %s", exc)
            return None

    def _provide(self) -> Any:
        if self._screenshotter is None:
            self._screenshotter = _create_screenshotter()
        return self._screenshotter

    def close(self) -> None:
        screenshotter = self._screenshotter
        self._screenshotter = None
        if screenshotter is not None:
            _close_screenshotter(screenshotter)
