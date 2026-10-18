import os

from PySide6.QtCore import QLockFile

from services.application_paths import ApplicationPaths
from services.file_io import atomic_write_json


DATA_DIRECTORIES = (
    "config",
    "games",
    "profiles",
    "data/functions",
    "data/library",
    "data/scheduler",
    "data/variables",
    "data/statistics",
    "data/learning/crops",
    "logs/archive",
    "runs/ocr_debug",
)


def initialize_application(
    paths: ApplicationPaths | None = None,
    *,
    lock_timeout_ms: int = 5000,
    activate: bool = True,
) -> ApplicationPaths:
    if type(lock_timeout_ms) is not int or lock_timeout_ms < 0:
        raise ValueError("Initialization lock timeout must be a non-negative integer")
    paths = paths or ApplicationPaths.discover()
    paths.data_root.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(paths.data_root / ".startup.lock"))
    if not lock.tryLock(lock_timeout_ms):
        if lock.error() == QLockFile.LockError.LockFailedError:
            raise TimeoutError("Another BotStudio data initialization is still running")
        raise OSError(f"Cannot lock BotStudio data directory: {paths.data_root}")
    try:
        for directory in DATA_DIRECTORIES:
            (paths.data_root / directory).mkdir(parents=True, exist_ok=True)
        defaults = {
            "config/app_config.json": {"active_game": ""},
            "config/emulator.json": {
                "emulator_type": "mumu",
                "adb_path": "",
                "mumu_manager_path": "",
            },
            "games/games.json": [],
            "profiles/profiles.json": [],
            "profiles/custom_fields.json": {},
        }
        for relative_path, value in defaults.items():
            destination = paths.data_root / relative_path
            if not destination.exists():
                atomic_write_json(destination, value)
        if (paths.data_root / "data" / "package-activation" / "pending.json").exists():
            from services.package_activation_service import PackageActivationService
            PackageActivationService(root=paths.data_root).recover()
    finally:
        lock.unlock()
    if activate:
        # Existing stores resolve their relative paths from this process-wide
        # root. Set it once, before importing views or starting workers.
        os.chdir(paths.data_root)
    return paths
