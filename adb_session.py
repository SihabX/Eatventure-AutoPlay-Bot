import logging
import re
import shutil
import subprocess
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

ADB_COMMAND_TIMEOUT_SECONDS = 15.0
ADB_SHELL_READY_TIMEOUT_SECONDS = 10.0
ADB_SHELL_RESPONSE_TIMEOUT_SECONDS = 10.0
ADB_SHELL_TERMINATE_TIMEOUT_SECONDS = 3.0
SHELL_SENTINEL = "__EATVENTURE_ADB_DONE__"
_DEVICE_LINE = re.compile(r"^(\S+)\s+(\S+)$")
_SIZE_LINE = re.compile(r"^(Physical|Override) size:\s*(\d+)\s*x\s*(\d+)\s*$")

DeviceSize = tuple[int, int]


class AdbError(RuntimeError):
    pass


class AdbDeviceNotAvailableError(AdbError):
    pass


def _adb_executable(explicit_path: Any = None) -> str:
    if explicit_path:
        candidate = str(explicit_path)
        if shutil.which(candidate) is None:
            raise AdbError(f"Configured ADB executable not found: {candidate}")
        return candidate
    resolved = shutil.which("adb")
    if resolved is None:
        raise AdbError(
            "ADB executable not found on PATH; install platform-tools or set ADB_PATH"
        )
    return resolved


def _run_adb(arguments: list[str], timeout: float = ADB_COMMAND_TIMEOUT_SECONDS) -> str:
    try:
        completed = subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_no_window_flag(),
        )
    except subprocess.TimeoutExpired as exc:
        raise AdbError(f"ADB command timed out: {' '.join(arguments)}") from exc
    except OSError as exc:
        raise AdbError(f"ADB command failed to launch: {exc}") from exc
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "").strip()
        raise AdbError(f"ADB command failed: {' '.join(arguments)}: {message}")
    return completed.stdout or ""


def _no_window_flag() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _attached_device_serials(adb_path: str) -> list[str]:
    output = _run_adb([adb_path, "devices"])
    serials: list[str] = []
    for raw_line in output.splitlines()[1:]:
        line = raw_line.strip()
        if not line:
            continue
        match = _DEVICE_LINE.match(line)
        if match is None:
            continue
        serial, state = match.group(1), match.group(2)
        if state == "device":
            serials.append(serial)
        else:
            logger.warning("Ignoring device %s in state '%s'", serial, state)
    return serials


def resolve_device_serial(adb_path: str, configured_serial: Any = None) -> str:
    serials = _attached_device_serials(adb_path)
    configured = str(configured_serial).strip() if configured_serial else ""
    if configured:
        if configured not in serials:
            raise AdbDeviceNotAvailableError(
                f"Configured device '{configured}' is not attached "
                f"(available: {', '.join(serials) if serials else 'none'})"
            )
        return configured
    if not serials:
        raise AdbDeviceNotAvailableError(
            "No ADB device is attached; connect the device and enable USB debugging"
        )
    if len(serials) > 1:
        raise AdbDeviceNotAvailableError(
            f"Multiple ADB devices attached ({', '.join(serials)}); "
            f"set ADB_SERIAL in config.py to choose one"
        )
    return serials[0]


def _parse_device_size(output: str) -> DeviceSize | None:
    physical: DeviceSize | None = None
    override: DeviceSize | None = None
    for raw_line in output.splitlines():
        match = _SIZE_LINE.match(raw_line.strip())
        if match is None:
            continue
        size = (int(match.group(2)), int(match.group(3)))
        if match.group(1) == "Override":
            override = size
        else:
            physical = size
    return override or physical


class AdbSession:
    def __init__(
        self, serial: Any = None, adb_path: Any = None, connect: bool = True
    ) -> None:
        self._adb_path = _adb_executable(adb_path)
        self._serial = resolve_device_serial(self._adb_path, serial)
        self._process: subprocess.Popen[str] | None = None
        self._process_lock = threading.RLock()
        logger.info("ADB device selected: %s", self._serial)
        if connect:
            self.ensure_shell()

    @property
    def serial(self) -> str:
        return self._serial

    def _base_arguments(self) -> list[str]:
        return [self._adb_path, "-s", self._serial]

    def run(self, arguments: list[str]) -> str:
        return _run_adb(self._base_arguments() + list(arguments))

    def device_size(self) -> DeviceSize:
        output = self.run(["shell", "wm", "size"])
        size = _parse_device_size(output)
        if size is None:
            raise AdbError(f"Could not parse device size from 'wm size': {output!r}")
        width, height = size
        if width <= 0 or height <= 0:
            raise AdbError(f"Device reported invalid screen size: {width}x{height}")
        return width, height

    def supports_motionevent(self) -> bool:
        try:
            completed = subprocess.run(
                self._base_arguments() + ["shell", "input"],
                capture_output=True,
                text=True,
                timeout=ADB_COMMAND_TIMEOUT_SECONDS,
                creationflags=_no_window_flag(),
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("Could not query 'input' capabilities: %s", exc)
            return False
        usage = (completed.stdout or "") + (completed.stderr or "")
        return "motionevent" in usage

    def ensure_shell(self) -> subprocess.Popen[str]:
        with self._process_lock:
            if self._process is not None and self._process.poll() is None:
                return self._process
            self._process = self._start_shell()
            return self._process

    def _start_shell(self) -> subprocess.Popen[str]:
        try:
            process = subprocess.Popen(
                self._base_arguments() + ["shell"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=_no_window_flag(),
            )
        except OSError as exc:
            raise AdbError(f"Could not start persistent ADB shell: {exc}") from exc
        logger.info("Persistent ADB shell started for %s", self._serial)
        try:
            self._handshake(process)
        except AdbError:
            self._terminate(process)
            raise
        return process

    def _handshake(self, process: subprocess.Popen[str]) -> None:
        deadline = time.perf_counter() + ADB_SHELL_READY_TIMEOUT_SECONDS
        self._write(process, f"echo {SHELL_SENTINEL}")
        if not self._await_sentinel(process, deadline):
            raise AdbError("Persistent ADB shell did not become ready")

    @staticmethod
    def _write(process: subprocess.Popen[str], command: str) -> None:
        stdin = process.stdin
        if stdin is None:
            raise AdbError("Persistent ADB shell has no input stream")
        try:
            stdin.write(command + "\n")
            stdin.flush()
        except (OSError, ValueError) as exc:
            raise AdbError(f"Writing to persistent ADB shell failed: {exc}") from exc

    @staticmethod
    def _await_sentinel(process: subprocess.Popen[str], deadline: float) -> bool:
        stdout = process.stdout
        if stdout is None:
            raise AdbError("Persistent ADB shell has no output stream")
        while time.perf_counter() < deadline:
            line = stdout.readline()
            if not line:
                return False
            if SHELL_SENTINEL in line:
                return True
        return False

    def execute(self, command: str) -> bool:
        with self._process_lock:
            process = self.ensure_shell()
            deadline = time.perf_counter() + ADB_SHELL_RESPONSE_TIMEOUT_SECONDS
            try:
                self._write(process, f"{command}; echo {SHELL_SENTINEL}")
                if self._await_sentinel(process, deadline):
                    return True
                raise AdbError("Persistent ADB shell stopped responding")
            except AdbError as exc:
                logger.error("ADB shell command failed (%s): %s", command, exc)
                self._reset_shell()
                return False

    def _reset_shell(self) -> None:
        with self._process_lock:
            if self._process is not None:
                self._terminate(self._process)
                self._process = None

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        stdin = process.stdin
        if stdin is not None:
            try:
                stdin.write("exit\n")
                stdin.flush()
                stdin.close()
            except (OSError, ValueError):
                pass
        try:
            process.wait(timeout=ADB_SHELL_TERMINATE_TIMEOUT_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            process.kill()
            process.wait(timeout=ADB_SHELL_TERMINATE_TIMEOUT_SECONDS)
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("Could not terminate persistent ADB shell: %s", exc)

    def close(self) -> None:
        self._reset_shell()
