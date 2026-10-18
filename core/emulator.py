"""
Core Module: Emulator

Verantwortlich für:

- ADB Kommunikation
- Geräteerkennung
- Screenshot-Erstellung
- Tap
- Swipe

Keine Spiellogik.
Keine Emulator-Verwaltung.
"""

from pathlib import Path
import json
import subprocess
import tempfile
import time as _time
from core.command_control import CommandCancelled, check_cancelled, run_command


SCREENSHOT_ATTEMPTS = 4
SCREENSHOT_RETRY_DELAY = 0.25
SCREENSHOT_PATTERN = "botstudio_screen_*.png"
SCREENSHOT_DIR_NAME = "botstudio_screens"


def screenshot_directory() -> Path:
    directory = Path(tempfile.gettempdir()) / SCREENSHOT_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def remove_screenshot(path) -> bool:
    """Best-effort delete; a locked file is left for the next sweep."""
    try:
        Path(path).unlink(missing_ok=True)
        return True
    except (OSError, TypeError, ValueError):
        return False


def cleanup_screenshots(max_age_seconds: float = 600.0, keep=()) -> int:
    """Delete stale screenshots, including legacy ones directly in TEMP."""
    cutoff = _time.time() - max(0.0, float(max_age_seconds))
    keep = {str(path) for path in keep}
    removed = 0
    for directory in (Path(tempfile.gettempdir()), screenshot_directory()):
        try:
            candidates = list(directory.glob(SCREENSHOT_PATTERN))
        except OSError:
            continue
        for candidate in candidates:
            if str(candidate) in keep:
                continue
            try:
                if candidate.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            if remove_screenshot(candidate):
                removed += 1
    return removed


ADB_LOG_NAME = "adb.log"
ADB_LOG_MAX_BYTES = 50 * 1024 * 1024
ADB_LOG_KEEP_BYTES = 2 * 1024 * 1024


def trim_adb_log(
    max_bytes: int = ADB_LOG_MAX_BYTES,
    keep_bytes: int = ADB_LOG_KEEP_BYTES,
    path=None,
) -> bool:
    """Shrink the adb server log to its newest lines.

    The adb server (also MuMu's) appends to %TEMP%\\adb.log forever and never
    rotates it (1.1 GB seen). It opens the file with O_APPEND and shared
    write access, so truncating in place is safe while it runs.
    """
    target = Path(path) if path else Path(tempfile.gettempdir()) / ADB_LOG_NAME
    try:
        if target.stat().st_size <= max_bytes:
            return False
        with open(target, "r+b") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            keep = max(0, min(int(keep_bytes), size))
            handle.seek(size - keep)
            tail = handle.read(keep)
            newline = tail.find(b"\n")
            if 0 <= newline < len(tail) - 1:
                tail = tail[newline + 1:]
            handle.seek(0)
            handle.truncate(0)
            handle.write(tail)
        return True
    except OSError:
        return False


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


def load_emulator_config() -> dict:
    """
    Lädt die Emulator-Konfiguration.
    """

    config_file = Path("config/emulator.json")

    with open(
        config_file,
        "r",
        encoding="utf-8",
    ) as file:
        return json.load(file)


class Emulator:
    """
    Zentrale ADB-Schnittstelle.
    """

    def __init__(
        self,
        device_id: str | None = None,
    ) -> None:

        self.config = load_emulator_config()

        self.adb_path = self.config["adb_path"]
        self.command_timeout = float(
            self.config.get("adb_timeout_seconds", 10)
        )

        self.device_id = device_id

    def _adb_command(self) -> list[str]:
        """
        Erzeugt den Basis-ADB-Befehl.
        """

        command = [self.adb_path]

        if self.device_id:
           command.extend(
             [
                    "-s",
                    self.device_id,
                ]
                       )

        return command

    def devices(self) -> list[str]:
        """
        Liefert alle äte.
        """

        result = self._run([self.adb_path, "devices"])
        if result is None:
            return []

        devices: list[str] = []

        for line in result.stdout.splitlines():

            if "\tdevice" in line:
                devices.append(
                    line.split("\t")[0]
                )

        return devices

    def _run(self, command, **kwargs):
        try:
            return run_command(
                command,
                timeout=self.command_timeout,
                check=False,
                **kwargs,
                **_windows_no_window_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

    def is_connected(self) -> bool:
        """
        Prüft ob mindestens ein Gerät verbunden ist.
        """

        return len(self.devices()) > 0

    def tap(
        self,
        x: int,
        y: int,
    ) -> bool:
        """
        Führt einen Tap aus.
        """

        result = self._run(
            self._adb_command()
            + [
                "shell",
                "input",
                "tap",
                str(x),
                str(y),
            ],
            capture_output=True,
            text=True,
            errors="replace",
        )

        return result is not None and result.returncode == 0

    def swipe(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration: int = 300,
    ) -> bool:
        """
        Führt einen Swipe aus.
        """

        result = self._run(
            self._adb_command()
            + [
                "shell",
                "input",
                "swipe",
                str(start_x),
                str(start_y),
                str(end_x),
                str(end_y),
                str(duration),
            ],
            capture_output=True,
            text=True,
            errors="replace",
        )

        return result is not None and result.returncode == 0

    def keyevent(
        self,
        key_code: int,
    ) -> bool:
        """
        Sendet einen Android Keyevent.
        """

        result = self._run(
            self._adb_command()
            + [
                "shell",
                "input",
                "keyevent",
                str(key_code),
            ],
            capture_output=True,
            text=True,
            errors="replace",
        )

        return result is not None and result.returncode == 0

    def type_text(
        self,
        text: str,
    ) -> bool:
        """
        Tippt Text über 'input text' in das fokussierte Feld.

        Leerzeichen werden als %s escaped, wie von adb input text gefordert.
        """

        escaped = str(text).replace(" ", "%s")
        result = self._run(
            self._adb_command()
            + [
                "shell",
                "input",
                "text",
                escaped,
            ],
            capture_output=True,
            text=True,
            errors="replace",
        )

        return result is not None and result.returncode == 0

    def clear_text(
        self,
        count: int = 32,
    ) -> bool:
        """
        Löscht Zeichen im fokussierten Feld per Backspace-Keyevents (DEL=67).
        """

        for _ in range(max(1, int(count))):
            if not self.keyevent(67):
                return False
        return True

    def screenshot(self, *, attempts: int = SCREENSHOT_ATTEMPTS) -> Path:
        """
        Erstellt einen Screenshot.

        Rückgabe:
            Path zur PNG-Datei.
        """

        import time

        last_error = "screenshot capture failed"
        # Windows file locks (virus scanner, indexer) are transient: retry
        # with a fresh file instead of failing the running Function (DEC-162)
        for attempt in range(max(1, attempts)):
            check_cancelled()
            if attempt:
                time.sleep(SCREENSHOT_RETRY_DELAY * attempt)
            try:
                return self._capture_screenshot()
            except CommandCancelled:
                raise
            except PermissionError as error:
                last_error = f"screenshot file locked: {error}"
            except RuntimeError as error:
                last_error = str(error)
            except OSError as error:
                last_error = f"screenshot file error: {error}"

        raise RuntimeError(last_error)

    def _capture_screenshot(self) -> Path:
        import uuid

        # unique per call: parallel readers must never race a shared file;
        # sanitize the device id so its port colon stays valid on Windows
        safe_device = "".join(
            char if char.isalnum() or char in "._-" else "_"
            for char in str(self.device_id)
        )
        directory = screenshot_directory()
        target = (
            directory
            / f"botstudio_screen_{safe_device}_{uuid.uuid4().hex}.png"
        )

        try:
            with open(target, "wb") as image_file:
                try:
                    run_command(
                        self._adb_command()
                        + [
                            "exec-out",
                            "screencap",
                            "-p",
                        ],
                        stdout=image_file,
                        timeout=self.command_timeout,
                        check=False,
                        **_windows_no_window_kwargs(),
                    )
                except (OSError, subprocess.TimeoutExpired):
                    raise RuntimeError("screenshot capture failed")

            # a corrupt PNG (exec-out CRLF mangling on Windows) must not reach OCR
            import cv2
            if cv2.imread(str(target)) is None:
                raise RuntimeError(
                    "screenshot PNG is corrupt (decode check failed)"
                )
        except BaseException:
            remove_screenshot(target)
            raise

        return target