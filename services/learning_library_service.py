from pathlib import Path
import json
import shutil

from models.library_entry import LibraryEntry


class LearningLibraryService:

    def __init__(self):
        self.root = Path("data/library")
        self.root.mkdir(
            parents=True,
            exist_ok=True,
        )

    def create_entry_folder(
        self,
        entry_id: str,
    ) -> Path:
        entry_root = (
            self.root / entry_id
        )

        entry_root.mkdir(
            parents=True,
            exist_ok=True,
        )

        (entry_root / "accepted").mkdir(
            exist_ok=True
        )

        (entry_root / "rejected").mkdir(
            exist_ok=True
        )

        return entry_root

    def save_entry(
        self,
        entry: LibraryEntry,
    ):
        entry_root = self.create_entry_folder(
            entry.entry_id
        )

        with open(
            entry_root / "entry.json",
            "w",
            encoding="utf-8",
        ) as fp:
            json.dump(
                entry.__dict__,
                fp,
                indent=4,
            )

    def save_source(
        self,
        entry_id: str,
        source_file: str,
    ):
        shutil.copy2(
            source_file,
            self.root
            / entry_id
            / "source.png",
        )

    def save_crop(
        self,
        entry_id: str,
        crop_file: str,
    ):
        shutil.copy2(
            crop_file,
            self.root
            / entry_id
            / "crop.png",
        )

    def save_pattern_reference(
        self,
        entry_id: str,
        source_file: str,
    ):
        shutil.copy2(
            source_file,
            self.root
            / entry_id
            / "pattern_reference.png",
        )

    def save_accepted_sample(
        self,
        entry_id: str,
        sample_file: str,
        filename: str,
    ):
        shutil.copy2(
            sample_file,
            self.root
            / entry_id
            / "accepted"
            / filename,
        )

    def save_rejected_sample(
        self,
        entry_id: str,
        sample_file: str,
        filename: str,
    ):
        shutil.copy2(
            sample_file,
            self.root
            / entry_id
            / "rejected"
            / filename,
        )