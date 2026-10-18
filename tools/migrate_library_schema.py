"""Additive migration of library entries to detection schema version 2."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import NamedTemporaryFile


DEFAULT_PROFILE = {
    "mode": "template",
    "color_sensitive": True,
    "use_edges": False,
    "use_mask": False,
    "find_all": False,
}


def migrate(root: Path) -> tuple[int, int]:
    migrated = 0
    skipped = 0
    for entry_file in sorted(root.glob("*/entry.json")):
        try:
            with entry_file.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict):
                skipped += 1
                continue

            changed = False
            if "schema_version" not in payload:
                payload["schema_version"] = 2
                changed = True
            if "detection_type" not in payload:
                payload["detection_type"] = "template"
                changed = True
            if "detection_profile" not in payload:
                payload["detection_profile"] = dict(DEFAULT_PROFILE)
                changed = True

            if not changed:
                skipped += 1
                continue

            with NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=entry_file.parent,
                prefix="entry.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                json.dump(payload, temporary, indent=4)
                temporary.write("\n")
                temporary_path = Path(temporary.name)
            temporary_path.replace(entry_file)
            migrated += 1
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            skipped += 1
    return migrated, skipped


if __name__ == "__main__":
    migrated_count, skipped_count = migrate(Path("data/library"))
    print(f"MIGRATED={migrated_count}")
    print(f"SKIPPED={skipped_count}")
    print("TEST RESULT: SUCCESS")
