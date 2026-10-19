import hashlib
import json
from pathlib import Path
from uuid import uuid4

from services.application_paths import ApplicationPaths
from services.file_io import atomic_write_json
from services.package_service import PackageError


class PackageOutboxService:
    """Persistent prepared archives; no network or active user-store changes."""

    def __init__(self, packages, root=None):
        self.packages = packages
        self.root = Path(root) if root is not None else ApplicationPaths.discover().data_root / "data" / "package-outbox"

    def destination(self, name):
        label = "".join(char if char.isalnum() or char in "-_ ." else "_" for char in name).strip(" .")
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root / f"{label or 'package'}-{uuid4().hex}.botstudio.zip"

    def list_packages(self):
        result = []
        for path in self.root.glob("*.botstudio.zip"):
            manifest = self.packages.inspect_package(str(path))
            sent = path.with_suffix(".uploaded.json")
            publication = json.loads(sent.read_text(encoding="utf-8")) if sent.exists() else None
            result.append({
                "path": str(path), "name": manifest["package_name"],
                "version": manifest["package_metadata"]["package_version"],
                "game_id": manifest["game"]["id"], "size": path.stat().st_size,
                "publication": publication,
            })
        return sorted(result, key=lambda entry: (entry["name"].casefold(), entry["version"], entry["path"]))

    def uploaded(self, path, sha256, publication):
        path = Path(path)
        if path.parent.resolve() != self.root.resolve():
            raise PackageError("uploaded archive is not in the prepared package list")
        if hashlib.sha256(path.read_bytes()).hexdigest() != sha256:
            raise PackageError("prepared archive changed during upload")
        atomic_write_json(path.with_suffix(".uploaded.json"), publication)
