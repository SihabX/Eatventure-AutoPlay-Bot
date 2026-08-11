"""Occlusion-proof window capture built on the Win32 PrintWindow API.

A screen-region grab (mss) reads whatever pixels currently occupy the target
rectangle, so any window stacked above the mirrored view is captured instead of
the game. PrintWindow asks the window to render its own client area into an
offscreen bitmap, which keeps working while the window is unfocused, fully
covered, or positioned off-screen. A minimized window has no surface at all and
reports a zero-sized client area; that is reported as a distinct error so the
caller can pause rather than treat it as a capture failure.
"""

import ctypes
import logging
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

_WIN32_IMPORT_ERROR: Exception | None
win32gui: Any = None
win32ui: Any = None

try:
    import win32gui as _imported_win32gui
    import win32ui as _imported_win32ui
except Exception as exc:  # pragma: no cover - platform dependent
    _WIN32_IMPORT_ERROR = exc
else:
    win32gui = _imported_win32gui
    win32ui = _imported_win32ui
    _WIN32_IMPORT_ERROR = None

# PrintWindow flags: render only the client area, and ask DWM for the full
# composited content rather than whatever is currently on screen.
PW_CLIENTONLY = 0x1
PW_RENDERFULLCONTENT = 0x2
PRINT_WINDOW_FLAGS = PW_CLIENTONLY | PW_RENDERFULLCONTENT

BITMAP_CHANNELS = 4
MINIMUM_PROBE_STDDEV = 0.5


class WindowBackendError(RuntimeError):
    pass


class WindowBackendUnavailableError(WindowBackendError):
    """The Win32 capture backend cannot be used in this environment."""


class WindowSurfaceUnavailableError(WindowBackendError):
    """The window exists but has no renderable surface, e.g. it is minimized."""


def backend_available() -> bool:
    return win32gui is not None and win32ui is not None


def backend_import_error() -> Exception | None:
    return _WIN32_IMPORT_ERROR


def _require_backend() -> None:
    if not backend_available():
        raise WindowBackendUnavailableError(
            f"Win32 capture backend unavailable: {_WIN32_IMPORT_ERROR}"
        )


def client_size(hwnd: Any) -> tuple[int, int]:
    """Returns the client area size in the pixels PrintWindow will render.

    A minimized window reports 0x0 here, which is what distinguishes "no
    surface to render" from "render failed".
    """
    _require_backend()
    try:
        left, top, right, bottom = win32gui.GetClientRect(hwnd)
    except Exception as exc:
        raise WindowBackendError(f"Cannot read client rect: {exc}") from exc
    return right - left, bottom - top


def is_minimized(hwnd: Any) -> bool:
    if not backend_available():
        return False
    try:
        return bool(win32gui.IsIconic(hwnd))
    except Exception:
        return False


def capture_client_area(hwnd: Any, max_height: Any = None) -> np.ndarray:
    """Renders the window's client area into a BGR array.

    The returned image is ordered exactly like an mss grab (BGR, top-down), so
    it is a drop-in replacement for template matching.
    """
    _require_backend()
    width, height = client_size(hwnd)
    if width <= 0 or height <= 0:
        raise WindowSurfaceUnavailableError(
            f"Window has no renderable surface (client area {width}x{height}); "
            "it is most likely minimized"
        )
    image = _render(hwnd, width, height)
    if max_height is not None:
        limit = max(1, min(height, int(max_height)))
        image = image[:limit, :, :]
    return np.ascontiguousarray(image)


def _render(hwnd: Any, width: int, height: int) -> np.ndarray:
    window_dc = None
    source_dc = None
    memory_dc = None
    bitmap = None
    try:
        window_dc = win32gui.GetWindowDC(hwnd)
        if not window_dc:
            raise WindowBackendError("Could not obtain a device context")
        source_dc = win32ui.CreateDCFromHandle(window_dc)
        memory_dc = source_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(source_dc, width, height)
        memory_dc.SelectObject(bitmap)
        rendered = ctypes.windll.user32.PrintWindow(
            hwnd, memory_dc.GetSafeHdc(), PRINT_WINDOW_FLAGS
        )
        if not rendered:
            raise WindowBackendError("PrintWindow reported failure")
        raw = bitmap.GetBitmapBits(True)
        expected = width * height * BITMAP_CHANNELS
        if len(raw) != expected:
            raise WindowBackendError(
                f"Captured {len(raw)} bytes, expected {expected} "
                f"for {width}x{height}"
            )
        buffer = np.frombuffer(raw, dtype=np.uint8).reshape(
            height, width, BITMAP_CHANNELS
        )
        return buffer[:, :, :3].copy()
    except WindowBackendError:
        raise
    except Exception as exc:
        raise WindowBackendError(f"PrintWindow capture failed: {exc}") from exc
    finally:
        _release(hwnd, window_dc, source_dc, memory_dc, bitmap)


def _release(
    hwnd: Any, window_dc: Any, source_dc: Any, memory_dc: Any, bitmap: Any
) -> None:
    if bitmap is not None:
        try:
            win32gui.DeleteObject(bitmap.GetHandle())
        except Exception as exc:
            logger.debug("Bitmap release failed: %s", exc)
    for device_context in (memory_dc, source_dc):
        if device_context is None:
            continue
        try:
            device_context.DeleteDC()
        except Exception as exc:
            logger.debug("Device context release failed: %s", exc)
    if window_dc:
        try:
            win32gui.ReleaseDC(hwnd, window_dc)
        except Exception as exc:
            logger.debug("Window device context release failed: %s", exc)


def probe(hwnd: Any) -> tuple[bool, str]:
    """Checks once whether this window can actually be captured offscreen.

    Some GPU-composited windows satisfy PrintWindow with a blank surface. A
    uniform frame is treated as a failed probe so the caller can fall back to
    screen grabbing instead of feeding the matcher an empty image. A missing
    surface propagates instead of failing the probe, because a minimized window
    says nothing about whether the backend works.
    """
    try:
        image = capture_client_area(hwnd)
    except WindowSurfaceUnavailableError:
        raise
    except WindowBackendError as exc:
        return False, str(exc)
    if image.size == 0:
        return False, "captured an empty frame"
    deviation = float(image.std())
    if deviation < MINIMUM_PROBE_STDDEV:
        return False, f"captured a uniform frame (stddev {deviation:.3f})"
    return True, f"{image.shape[1]}x{image.shape[0]} stddev {deviation:.1f}"
