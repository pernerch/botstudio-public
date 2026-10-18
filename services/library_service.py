import json
import shutil

from pathlib import Path
from dataclasses import asdict

from models.library_entry import (
    LibraryEntry,
)
from services.reinitialize_service import get_default_reinitialize_service


class LibraryService:

    def __init__(self, reinitialize_service=None, root: str | Path = "data/library"):

        self.root = Path(root)

        self.root.mkdir(
            parents=True,
            exist_ok=True,
        )
        self.reinitialize_service = (
            reinitialize_service or get_default_reinitialize_service()
        )

    def save(
        self,
        entry: LibraryEntry,
    ):

        entry_root = (
            self.root
            / entry.entry_id
        )

        entry_root.mkdir(
            parents=True,
            exist_ok=True,
        )

        file = (
            entry_root
            / "entry.json"
        )

        with open(
            file,
            "w",
            encoding="utf-8",
        ) as fp:

            json.dump(
                asdict(entry),
                fp,
                indent=4,
            )
        if self.reinitialize_service:
            self.reinitialize_service.publish("library", entry_id=entry.entry_id)

    def load(
        self,
        entry_id: str,
    ):

        file = (
            self.root
            / entry_id
            / "entry.json"
        )

        if not file.exists():
            return None

        try:
            with open(
                file,
                "r",
                encoding="utf-8",
            ) as fp:
                payload = fp.read().strip()

            if not payload:
                return None

            data = json.loads(payload)
            if not isinstance(data, dict):
                return None
            return data
        except (OSError, ValueError, TypeError):
            return None

    def load_model(
        self,
        entry_id: str,
    ):
        data = self.load(entry_id)
        if not data:
            return None

        return LibraryEntry(
            entry_id=data.get("entry_id", entry_id),
            name=data.get("name", ""),
            boundary=data.get("boundary", {}),
            pattern=data.get("pattern", {}),
            verify_best=float(data.get("verify_best", 0)),
            verify_worst=float(data.get("verify_worst", 0)),
            verify_average=float(data.get("verify_average", 0)),
            verify_success=float(data.get("verify_success", 0)),
            sample_count=int(data.get("sample_count", 0)),
            accepted_samples=list(data.get("accepted_samples", [])),
            rejected_samples=list(data.get("rejected_samples", [])),
            used_by=list(data.get("used_by", [])),
            schema_version=int(data.get("schema_version", 2)),
            detection_type=str(data.get("detection_type", "template")),
            detection_profile=data.get("detection_profile"),
        )

    def list_entries(
        self,
    ):

        entries = []

        for folder in self.root.iterdir():
            if not folder.is_dir():
                continue

            entry_file = folder / "entry.json"
            if not entry_file.exists():
                continue

            if self.load(folder.name) is None:
                continue

            entries.append(entry_file)

        return entries

    def set_used_by(
        self,
        entry_id: str,
        function_ids: list,
    ) -> bool:
        """Back reference so a replaced image can be traced into every function."""

        file = (
            self.root
            / entry_id
            / "entry.json"
        )

        if not file.exists():
            return False

        try:
            with open(
                file,
                "r",
                encoding="utf-8",
            ) as fp:
                payload = fp.read().strip()

            if not payload:
                return False

            data = json.loads(payload)
            if not isinstance(data, dict):
                return False
        except (OSError, ValueError, TypeError):
            return False

        updated_function_ids = list(function_ids)
        if list(data.get("used_by") or []) == updated_function_ids:
            return True

        data["used_by"] = updated_function_ids

        with open(
            file,
            "w",
            encoding="utf-8",
        ) as fp:
            json.dump(data, fp, indent=4)

        if self.reinitialize_service:
            self.reinitialize_service.publish(
                "library",
                entry_id=entry_id,
                used_by=updated_function_ids,
            )
        return True

    def rename_entry(
        self,
        entry_id: str,
        name: str,
    ) -> bool:
        """Change only the display name and preserve all entry data."""
        file = self.root / entry_id / "entry.json"
        if not file.exists() or not str(name).strip():
            return False

        try:
            with open(file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                return False
            data["name"] = str(name).strip()
            with open(file, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=4)
        except (OSError, ValueError, TypeError):
            return False

        if self.reinitialize_service:
            self.reinitialize_service.publish(
                "library",
                entry_id=entry_id,
                renamed=True,
            )
        return True

    def delete_entry(
        self,
        entry_id: str,
    ) -> bool:
        """Delete a library entry and all of its stored samples and artifacts."""

        entry_root = self.root / entry_id

        if not entry_root.is_dir():
            return False

        shutil.rmtree(entry_root)
        if self.reinitialize_service:
            self.reinitialize_service.publish(
                "library",
                entry_id=entry_id,
                deleted=True,
            )
        return True

    def create_entry_structure(
        self,
        entry_id: str,
    ):

        entry_root = (
            self.root
            / entry_id
        )

        accepted = (
            entry_root
            / "accepted"
        )

        rejected = (
            entry_root
            / "rejected"
        )

        accepted.mkdir(
            parents=True,
            exist_ok=True,
        )

        rejected.mkdir(
            parents=True,
            exist_ok=True,
        )

        return (
            entry_root,
            accepted,
            rejected,
        )

    def copy_sample(
        self,
        source_file: str,
        target_file,
    ):

        shutil.copy2(
            source_file,
            target_file,
        )
