from pathlib import Path
from collections.abc import Callable
import subprocess
from core.command_control import run_command


def _windows_no_window_kwargs() -> dict:

    if __import__("os").name != "nt":
        return {}

    startup_info = subprocess.STARTUPINFO()
    startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup_info.wShowWindow = subprocess.SW_HIDE

    return {
        "startupinfo": startup_info,
        "creationflags": subprocess.CREATE_NO_WINDOW,
    }


class AdbService:

    DEFAULT_TIMEOUT = 10

    def __init__(
        self,
        adb_path: str | Callable[[], str],
    ):
        self._adb_path = adb_path

    @property
    def adb_path(self) -> Path:
        value = self._adb_path() if callable(self._adb_path) else self._adb_path
        return Path(value)

    def _run(
        self,
        command: list[str],
        timeout: int | None = None,
    ):
        # a frozen emulator never returns, so every call must be bounded
        try:
            return run_command(
                command,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout or self.DEFAULT_TIMEOUT,
                check=False,
                **_windows_no_window_kwargs(),
            )

        except (subprocess.TimeoutExpired, OSError):
            return None

    def devices(
        self,
    ) -> str:

        result = self._run(
            [
                str(self.adb_path),
                "devices",
            ]
        )

        if result is None:
            return ""

        return result.stdout

    def is_connected(
        self,
        device_id: str,
    ) -> bool:

        devices = self.devices()

        return device_id in devices

    def connect(
        self,
        device_id: str,
    ) -> bool:

        result = self._run(
            [
                str(self.adb_path),
                "connect",
                device_id,
            ]
        )

        if result is None:
            return False

        output = (
            result.stdout
            + result.stderr
        ).lower()

        return (
            "connected" in output
            or "already connected" in output
        )

    def ensure_connection(
        self,
        device_id: str,
    ) -> bool:

        if self.is_connected(
            device_id
        ):
            return True

        return self.connect(
            device_id
        )

    def disconnect(
        self,
        device_id: str,
    ) -> bool:

        result = self._run(
            [
                str(self.adb_path),
                "disconnect",
                device_id,
            ]
        )

        if result is None:
            return False

        return (
            result.returncode == 0
        )

    def shell(
        self,
        device_id: str,
        command: list[str],
        timeout: int | None = None,
    ) -> str:

        result = self.shell_result(device_id, command, timeout)

        if result is None:
            return ""

        return result.stdout

    def shell_result(
        self,
        device_id: str,
        command: list[str],
        timeout: int | None = None,
    ):
        """Run an Android shell command and preserve its exit code and stderr."""

        return self._run(
            [
                str(self.adb_path),
                "-s",
                device_id,
                "shell",
                *command,
            ],
            timeout=timeout,
        )

    def shell_success(
        self,
        device_id: str,
        command: list[str],
        timeout: int | None = None,
    ) -> bool:

        result = self.shell_result(device_id, command, timeout)

        if result is None:
            return False

        return (
            result.returncode == 0
        )

    def is_responsive(
        self,
        device_id: str,
        timeout: int = 5,
    ) -> bool:
        """Android answers only while system_server is not frozen."""

        result = self._run(
            [
                str(self.adb_path),
                "-s",
                device_id,
                "shell",
                "cmd",
                "activity",
                "get-current-user",
            ],
            timeout=timeout,
        )

        if result is None:
            return False

        return result.returncode == 0 and result.stdout.strip().isdigit()

    def get_package_pid(
        self,
        device_id: str,
        package_name: str,
        timeout: int = 5,
        *,
        strict: bool = False,
    ) -> int | None:

        result = self._run(
            [
                str(self.adb_path),
                "-s",
                device_id,
                "shell",
                "pidof",
                package_name,
            ],
            timeout=timeout,
        )

        if strict:
            if result is None:
                raise RuntimeError("ADB process query failed or timed out")
            if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
                return None
            if (
                result.returncode != 0
                or result.stderr.strip()
                or not result.stdout.strip()
                or not all(value.isdigit() and int(value) > 0 for value in result.stdout.split())
            ):
                raise RuntimeError(
                    f"ADB process query returned invalid output (exit {result.returncode}): "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )

        if result is None:
            return None

        value = result.stdout.strip().split()

        if not value:
            return None

        try:
            return int(value[0])

        except ValueError:
            return None

    def has_anr(
        self,
        device_id: str,
        package_name: str,
        timeout: int = 8,
    ) -> bool:

        result = self._run(
            [
                str(self.adb_path),
                "-s",
                device_id,
                "shell",
                "dumpsys",
                "window",
            ],
            timeout=timeout,
        )

        if result is None:
            return False

        for line in result.stdout.splitlines():

            if "mFocusedApp" not in line:
                continue

            if package_name not in line:
                continue

            return "not responding" in line.lower()

        return False

    def get_foreground_package(
        self,
        device_id: str,
    ) -> str | None:

        result = self.shell(
            device_id,
            [
                "dumpsys",
                "window",
            ],
        )

        for line in result.splitlines():

            if (
                "mCurrentFocus"
                not in line
            ):
                continue

            if "/" not in line:
                continue

            return (
                line.split("/")[
                    0
                ]
                .split()[
                    -1
                ]
            )

        return None