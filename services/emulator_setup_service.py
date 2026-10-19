"""Worker-only discovery of tools inside a selected MuMu installation."""

import os
from pathlib import Path
import subprocess
import time

from core.command_control import run_command
from core.emulator_manager import EmulatorManager, _windows_no_window_kwargs
from services.logging_service import get_logger


class EmulatorSetupError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


class EmulatorSetupService:
    SEARCH_SECONDS = 15
    MAX_ENTRIES = 20000
    ADB_TIMEOUT = 5

    def __init__(self, manager: EmulatorManager):
        self.manager = manager
        self.logger = get_logger("emulator")

    def ensure_tools(self, installation: str = "") -> None:
        """Keep valid configured tools; otherwise search only the MuMu tree."""
        config = self.manager._load_config()
        manager = self._file(config.get("mumu_manager_path", ""))
        adb = self._file(config.get("adb_path", ""))
        if installation:
            root = Path(installation).resolve()
            if root.name.casefold() == "nx_main" and (root / "MuMuManager.exe").is_file():
                root = root.parent
            manager = adb = None
        elif manager:
            root = manager.parent
            if root.name.casefold() == "nx_main":
                root = root.parent
        else:
            raise EmulatorSetupError("select_folder")

        if not root.is_dir():
            raise EmulatorSetupError("select_folder")

        deadline = time.monotonic() + self.SEARCH_SECONDS
        if manager is not None and adb is not None:
            try:
                self._validate_adb(adb)
                return
            except EmulatorSetupError as error:
                # A stale executable must not prevent finding MuMu's working ADB.
                self.logger.warning("Configured ADB Invalid", error=str(error))
                adb = None
        candidates = []
        if manager is None or adb is None:
            files = self._find_tools(root, deadline)
            if manager is None:
                managers = files["mumumanager.exe"]
                if len(managers) != 1:
                    raise EmulatorSetupError(
                        "manager_missing" if not managers else "manager_ambiguous",
                        str(root),
                    )
                manager = managers[0]
            candidates = sorted(
                files["adb.exe"],
                key=lambda path: (
                    path.parent != manager.parent,
                    len(path.parts),
                    str(path).casefold(),
                ),
            )

        if adb is not None:
            candidates.insert(0, adb)
        if not candidates:
            raise EmulatorSetupError("adb_missing", str(root))

        failures = []
        for candidate in candidates:
            if time.monotonic() >= deadline:
                raise EmulatorSetupError("search_limit", str(root))
            try:
                self._validate_adb(candidate)
            except EmulatorSetupError as error:
                failures.append(str(error))
                continue
            paths = {"adb_path": str(candidate.resolve())}
            if manager is not None:
                paths["mumu_manager_path"] = str(manager.resolve())
            self.manager.update_tool_paths(**paths)
            self.logger.info("MuMu Tools Configured", **paths)
            return
        raise EmulatorSetupError("adb_invalid", "\n".join(failures))

    @staticmethod
    def _file(value: str) -> Path | None:
        return Path(value) if value and Path(value).is_file() else None

    def _find_tools(self, root: Path, deadline: float) -> dict[str, list[Path]]:
        found: dict[str, list[Path]] = {"adb.exe": [], "mumumanager.exe": []}
        entries = 0

        def failed(error: OSError):
            raise EmulatorSetupError("search_failed", str(error)) from error

        for directory, folders, files in os.walk(root, onerror=failed, followlinks=False):
            entries += len(folders) + len(files)
            if time.monotonic() >= deadline or entries > self.MAX_ENTRIES:
                raise EmulatorSetupError("search_limit", str(root))
            parent = Path(directory)
            folders[:] = sorted(
                (
                    name for name in folders
                    if not (parent / name).is_symlink()
                    and not (parent / name).is_junction()
                ),
                key=str.casefold,
            )
            for name in sorted(files, key=str.casefold):
                if name.casefold() in found and not (parent / name).is_symlink():
                    found[name.casefold()].append(parent / name)
        return found

    def _validate_adb(self, path: Path) -> None:
        try:
            result = run_command(
                [str(path), "version"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=self.ADB_TIMEOUT,
                check=False,
                **_windows_no_window_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise EmulatorSetupError("adb_invalid", f"{path}: {error}") from error
        if result.returncode != 0 or "android debug bridge" not in result.stdout.lower():
            raise EmulatorSetupError(
                "adb_invalid",
                f"{path}: {(result.stderr or result.stdout).strip()}",
            )
