from dataclasses import dataclass
import os
from pathlib import Path
import sys


@dataclass(frozen=True)
class ApplicationPaths:
    data_root: Path
    resource_root: Path
    program_root: Path

    @classmethod
    def discover(cls) -> "ApplicationPaths":
        source_root = Path(__file__).resolve().parents[1]
        frozen = getattr(sys, "frozen", False)
        program_root = Path(sys.executable).resolve().parent if frozen else source_root
        resource_root = (
            Path(getattr(sys, "_MEIPASS", program_root)).resolve()
            if frozen else source_root
        )
        override = os.environ.get("BOTSTUDIO_DATA_DIR")
        if override is not None:
            data_root = Path(override).expanduser()
            if not override.strip() or not data_root.is_absolute():
                raise ValueError("BOTSTUDIO_DATA_DIR must be an absolute directory path")
        elif frozen:
            local_appdata = os.environ.get("LOCALAPPDATA")
            if not local_appdata or not Path(local_appdata).is_absolute():
                raise ValueError("LOCALAPPDATA must identify an absolute user data directory")
            data_root = Path(local_appdata) / "BotStudio"
        else:
            data_root = source_root
        return cls(data_root.resolve(), resource_root, program_root)

    def resource(self, *parts: str) -> Path:
        return self.resource_root.joinpath(*parts)

    @staticmethod
    def long_path(path: Path) -> Path:
        resolved = str(path.resolve())
        if os.name != "nt" or resolved.startswith("\\\\?\\"):
            return Path(resolved)
        if resolved.startswith("\\\\"):
            return Path("\\\\?\\UNC\\" + resolved[2:])
        return Path("\\\\?\\" + resolved)
