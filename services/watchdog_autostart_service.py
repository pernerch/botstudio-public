import os
from pathlib import Path
import subprocess
import sys
import time

from PySide6.QtCore import QLockFile
from services.application_paths import ApplicationPaths


class WatchdogAutoStartService:
    REGISTRY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
    VALUE_NAME = "BotStudioWatchdog"

    def __init__(self, registry=None, lock_path=None):
        if registry is not None:
            self._registry = registry
        elif os.name == "nt":
            import winreg

            self._registry = winreg
        else:
            self._registry = None
        app_data = os.environ.get("APPDATA")
        base = Path(app_data) if app_data else Path.home() / ".config"
        self.lock_path = Path(lock_path) if lock_path else (
            base / "BotStudio" / "watchdog" / "watchdog.lock"
        )

    def is_registered(self):
        if self._registry is None:
            return False
        try:
            with self._registry.OpenKey(
                self._registry.HKEY_CURRENT_USER,
                self.REGISTRY_PATH,
                0,
                self._registry.KEY_READ,
            ) as key:
                self._registry.QueryValueEx(key, self.VALUE_NAME)
            return True
        except FileNotFoundError:
            return False

    def set_registered(self, command, enabled):
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        if self._registry is None:
            raise OSError("Windows login startup is available only on Windows")
        if enabled and (
            not isinstance(command, (list, tuple))
            or not command
            or not all(isinstance(part, str) and part for part in command)
        ):
            raise ValueError("A valid watchdog command is required")

        with self._registry.CreateKey(
            self._registry.HKEY_CURRENT_USER,
            self.REGISTRY_PATH,
        ) as key:
            if enabled:
                command_line = subprocess.list2cmdline(list(command))
                self._registry.SetValueEx(
                    key,
                    self.VALUE_NAME,
                    0,
                    self._registry.REG_SZ,
                    command_line,
                )
            else:
                try:
                    self._registry.DeleteValue(key, self.VALUE_NAME)
                except FileNotFoundError:
                    pass

    def build_commands(self, project_root, studio_pid, working_directory=None):
        root = Path(project_root).resolve()
        if getattr(sys, "frozen", False):
            paths = ApplicationPaths.discover()
            watchdog_executable = paths.program_root / "BotStudioWatchdog.exe"
            studio_executable = paths.program_root / "BotStudio.exe"
            for entry in (watchdog_executable, studio_executable):
                if not entry.is_file():
                    raise FileNotFoundError(f"BotStudio executable must be available: {entry}")
            watchdog_command = [str(watchdog_executable)]
            studio_command = [str(studio_executable)]
            default_directory = paths.data_root
        else:
            watchdog_script = root / "watchdog_app.py"
            studio_script = root / "main.py"
            if not watchdog_script.is_file() or not studio_script.is_file():
                raise FileNotFoundError(
                    "Watchdog and BotStudio entry points must be available"
                )
            executable = Path(sys.executable)
            windowless = executable.with_name("pythonw.exe")
            watchdog_executable = (
                str(windowless)
                if os.name == "nt" and windowless.is_file()
                else sys.executable
            )
            watchdog_command = [watchdog_executable, str(watchdog_script)]
            studio_command = [sys.executable, str(studio_script)]
            default_directory = root
        working_directory = Path(working_directory or default_directory).resolve()
        base = [
            *watchdog_command,
            "--working-directory",
            str(working_directory),
        ]
        startup_command = [
            *base,
            "--studio-command",
            *studio_command,
        ]
        immediate_command = [
            *base,
            "--studio-pid",
            str(studio_pid),
            "--studio-command",
            *studio_command,
        ]
        return startup_command, immediate_command

    @staticmethod
    def launch(command, working_directory):
        if not command or not all(isinstance(part, str) and part for part in command):
            raise ValueError("A valid watchdog command is required")
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        return subprocess.Popen(
            command,
            cwd=str(Path(working_directory).resolve()),
            creationflags=creationflags,
        )

    def ensure_running(self, project_root, studio_pid, working_directory=None):
        if self.is_running():
            return False
        default_directory = (
            ApplicationPaths.discover().data_root
            if getattr(sys, "frozen", False) else project_root
        )
        directory = Path(working_directory or default_directory).resolve()
        _, immediate_command = self.build_commands(
            project_root, studio_pid, working_directory=directory,
        )
        process = self.launch(immediate_command, directory)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self.is_running():
                return True
            exit_code = process.poll()
            if exit_code is not None:
                raise RuntimeError(f"Watchdog exited during startup (exit code {exit_code})")
            time.sleep(0.05)
        raise TimeoutError("Watchdog did not become ready within 5 seconds")

    def is_running(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(self.lock_path))
        if not lock.tryLock(0):
            return True
        lock.unlock()
        return False
