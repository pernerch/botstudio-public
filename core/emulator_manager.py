"""
Core Module: Emulator Manager

Verantwortlich für:

- Emulator-Konfiguration
- Emulator-Typ
- Profil ↔ Instanz Zuordnung
- Instanzverwaltung
- ADB Device-ID Auflösung
- Emulator Verbindungstrennung
- Emulator Verbindungsaufbau

Keine Screenshots.
Keine Spiellogik.
"""

from pathlib import Path
import csv
import ctypes
import io
import json
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

from core.adb_service import (
    AdbService,
)


class EmulatorManager:

    BASE_PORT = 16384
    PORT_STEP = 32
    MUMU_PROCESS_NAMES = (
        "MuMuPlayer.exe",
        "MuMuNxDevice.exe",
        "MuMuNxMain.exe",
        "MuMuNxSVC.exe",
        "MuMuVMMHeadless.exe",
    )

    def __init__(
        self,
        config_file: str = "config/emulator.json",
    ) -> None:

        self.config_file = Path(
            config_file
        )

        self.config = (
            self._load_config()
        )

        self.adb_service = (
            AdbService(
                self.adb_path
            )
        )

    def _load_config(
        self,
    ) -> dict:

        if not self.config_file.exists():

            raise FileNotFoundError(
                self.config_file
            )

        with open(
            self.config_file,
            "r",
            encoding="utf-8",
        ) as file:

            return json.load(
                file
            )

    @property
    def emulator_type(
        self,
    ) -> str:

        return self.config[
            "emulator_type"
        ]

    @property
    def adb_path(
        self,
    ) -> str:

        return self.config[
            "adb_path"
        ]

    @property
    def mumu_manager_path(
        self,
    ) -> str:

        return self.config.get(
            "mumu_manager_path",
            "",
        )

    @property
    def instance_pids(
        self,
    ) -> dict:

        return self.config.setdefault(
            "instance_pids",
            {},
        )

    @property
    def instance_processes(
        self,
    ) -> dict:

        return self.config.setdefault(
            "instance_processes",
            {},
        )

    def set_mumu_manager_path(
        self,
        path: str,
    ) -> None:

        self.config[
            "mumu_manager_path"
        ] = path

        self._save_config()

    def _save_config(
        self,
    ) -> None:

        with open(
            self.config_file,
            "w",
            encoding="utf-8",
        ) as file:

            json.dump(
                self.config,
                file,
                indent=4,
            )

    def validate_instance(
        self,
        instance_number: int,
    ) -> bool:

        return (
            instance_number >= 0
        )

    def get_port(
        self,
        instance_number: int,
    ) -> int:
        """
        Berechnet den MuMu ADB-Port
        aus der Instanznummer.
        """

        if not self.validate_instance(
            instance_number
        ):

            raise ValueError(
                "Ungültige Instanznummer."
            )

        return (
            self.BASE_PORT
            + (
                instance_number
                * self.PORT_STEP
            )
        )

    def get_device_id(
        self,
        instance_number: int,
    ) -> str:
        """
        Liefert die Device-ID
        einer Emulator-Instanz.
        """

        port = self.get_port(
            instance_number
        )

        return (
            f"127.0.0.1:{port}"
        )

    def connect_instance(
        self,
        instance_number: int,
    ) -> bool:

        device_id = (
            self.get_device_id(
                instance_number
            )
        )

        return (
            self.adb_service.connect(
                device_id
            )
        )

    def disconnect_instance(
        self,
        instance_number: int,
    ) -> bool:

        device_id = (
            self.get_device_id(
                instance_number
            )
        )

        return (
            self.adb_service.disconnect(
                device_id
            )
        )

    def connect_profiles(
        self,
        profiles,
    ):

        for profile in profiles:

            self.connect_instance(
                profile[
                    "emulator_instance"
                ]
            )

    def disconnect_profiles(
        self,
        profiles,
    ):

        for profile in profiles:

            self.disconnect_instance(
                profile[
                    "emulator_instance"
                ]
            )

    def get_instance_info(
        self,
        instance_number: int,
    ) -> dict:
        """Official MuMuManager state; empty dict when unavailable."""

        if self.emulator_type != "mumu":
            return {}

        manager_path = Path(
            self.mumu_manager_path
        )

        if not manager_path.exists():
            return {}

        try:
            result = run_command(
                [
                    str(manager_path),
                    "info",
                    "-v",
                    str(instance_number),
                ],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=20,
                check=False,
                **_windows_no_window_kwargs(),
            )

        except (subprocess.TimeoutExpired, OSError):
            return {}

        try:
            info = json.loads(
                result.stdout
            )

        except (json.JSONDecodeError, TypeError):
            return {}

        if not isinstance(info, dict):
            return {}

        if info.get("errcode"):
            return {}

        return info

    def is_instance_running(
        self,
        instance_number: int,
    ) -> bool:

        if not self.validate_instance(
            instance_number
        ):

            return False

        info = self.get_instance_info(
            instance_number
        )

        if info:

            return bool(
                info.get("is_process_started")
                and info.get("is_android_started")
            )

        return bool(
            self.resolve_instance_pid(
                instance_number
            )
        )

    def get_instance_pid(
        self,
        instance_number: int,
    ) -> int | None:

        pid = self.get_instance_info(
            instance_number
        ).get("pid")

        if pid:
            return int(pid)

        pid = self.resolve_instance_pid(
            instance_number
        )

        if pid:
            return pid

        value = self.instance_pids.get(
            str(instance_number)
        )

        if value and self._pid_exists(
            int(value)
        ):
            return int(value)

        return None

    def resolve_instance_pid(
        self,
        instance_number: int,
    ) -> int | None:
        """Port ownership maps an already running instance to its process."""

        port = self.get_port(
            instance_number
        )

        try:
            result = run_command(
                [
                    "netstat",
                    "-ano",
                    "-p",
                    "TCP",
                ],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=10,
                check=False,
                **_windows_no_window_kwargs(),
            )

        except (subprocess.TimeoutExpired, OSError):
            return None

        for line in result.stdout.splitlines():

            parts = line.split()

            if len(parts) < 4:
                continue

            if parts[0].upper() != "TCP":
                continue

            # match the local address only; the status column is localized
            if not parts[1].endswith(
                f":{port}"
            ):
                continue

            try:
                pid = int(parts[-1])

            except ValueError:
                continue

            # netstat only lists live sockets, so the owner is running
            return pid

        return None

    def _set_instance_pid(
        self,
        instance_number: int,
        pid: int,
    ) -> None:

        self.instance_pids[
            str(instance_number)
        ] = pid

        self._save_config()

    def _set_instance_processes(
        self,
        instance_number: int,
        pids: list[int],
    ) -> None:

        self.instance_processes[
            str(instance_number)
        ] = pids

        self._save_config()

    def start_instance(
        self,
        instance_number: int,
    ) -> bool:

        if not self.validate_instance(
            instance_number
        ):

            raise ValueError(
                "Ungültige Instanznummer."
            )

        if self.emulator_type != "mumu":
            raise NotImplementedError

        manager_path = Path(
            self.mumu_manager_path
        )

        if not manager_path.exists():
            return False

        # a second launcher for a live instance would only pile up processes
        if self.is_instance_running(
            instance_number
        ):
            return True

        process = subprocess.Popen(
            [
                str(manager_path),
                "api",
                "-v",
                str(instance_number),
                "launch_player",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_windows_no_window_kwargs(),
        )

        self._set_instance_pid(
            instance_number,
            process.pid,
        )

        return True

    def stop_instance(
        self,
        instance_number: int,
        force: bool = False,
    ) -> bool:

        if self.emulator_type != "mumu":
            raise NotImplementedError

        manager_path = Path(
            self.mumu_manager_path
        )

        if force:
            return self.kill_instance(
                instance_number
            )

        if not manager_path.exists():
            return False

        try:
            result = run_command(
                [
                    str(manager_path),
                    "api",
                    "-v",
                    str(instance_number),
                    "shutdown_player",
                ],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=30,
                check=False,
                **_windows_no_window_kwargs(),
            )

        except (subprocess.TimeoutExpired, OSError):
            return False

        return result.returncode == 0

    def kill_instance(
        self,
        instance_number: int,
    ) -> bool:
        # only the port-owning process is provably this instance
        pid = self.get_instance_pid(
            instance_number
        )

        if not pid:
            return False

        if not self._pid_exists(pid):
            return False

        try:
            result = run_command(
                [
                    "taskkill",
                    "/PID",
                    str(pid),
                    "/T",
                    "/F",
                ],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=30,
                check=False,
                **_windows_no_window_kwargs(),
            )

        except (subprocess.TimeoutExpired, OSError):
            return False

        return result.returncode == 0

    def get_mumu_process_pids(
        self,
    ) -> list[int]:

        try:
            result = run_command(
                [
                    "tasklist",
                    "/FO",
                    "CSV",
                    "/NH",
                ],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=30,
                check=False,
                **_windows_no_window_kwargs(),
            )

        except (subprocess.TimeoutExpired, OSError):
            return []

        pids = []

        for row in csv.reader(
            io.StringIO(result.stdout)
        ):

            if len(row) < 2:
                continue

            if row[0] not in self.MUMU_PROCESS_NAMES:
                continue

            pids.append(
                int(row[1])
            )

        return pids

    def _pid_exists(
        self,
        pid: int,
    ) -> bool:
        # os.kill(pid, 0) calls TerminateProcess on Windows, so query the handle
        query_limited_information = 0x1000
        still_active = 259

        kernel32 = ctypes.windll.kernel32

        handle = kernel32.OpenProcess(
            query_limited_information,
            False,
            pid,
        )

        if not handle:
            return False

        try:
            exit_code = ctypes.c_ulong()

            if not kernel32.GetExitCodeProcess(
                handle,
                ctypes.byref(exit_code),
            ):
                return False

            return exit_code.value == still_active

        finally:
            kernel32.CloseHandle(handle)