from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
from uuid import uuid4
from zipfile import BadZipFile

from PySide6.QtCore import QLockFile

from services.application_paths import ApplicationPaths
from services.file_io import atomic_write_json, read_json
from services.github_catalog_service import GitHubCatalogService
from services.package_service import PackageError, PackageService


class ManagedPackageService:
    """Keep local originals and working copies; never authenticate a remote publisher."""

    def __init__(self, root: Path | None = None, packages: PackageService | None = None):
        self.root = (
            Path(root) if root is not None
            else ApplicationPaths.discover().data_root / "data" / "packages"
        ).resolve()
        self.packages = packages or PackageService()

    @contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(self.root / ".packages.lock"))
        if not lock.tryLock(5000):
            raise PackageError("package store is busy or cannot be locked")
        try:
            yield
        finally:
            lock.unlock()

    @staticmethod
    def _digest(path: Path) -> str:
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()

    @staticmethod
    def _version(value: str) -> tuple[int, ...]:
        if not isinstance(value, str) or not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value):
            raise PackageError("managed package version must be canonical major.minor.patch")
        return tuple(int(part) for part in value.split("."))

    @staticmethod
    def _identity(manifest: dict) -> tuple[str, str, str]:
        details = manifest.get("package_metadata")
        if not details or not details.get("publisher"):
            raise PackageError("managed packages require package metadata and publisher identity")
        publisher = PackageService.publisher_identity_key(details["publisher"])
        game_id = manifest["game"].get("id")
        if not isinstance(game_id, str) or not game_id.strip():
            raise PackageError("managed package game ID is required")
        return publisher, details["package_id"], game_id

    @classmethod
    def _installation_id(cls, manifest: dict) -> str:
        return hashlib.sha256(json.dumps(cls._identity(manifest)).encode("utf-8")).hexdigest()

    def _directory(self, installation_id: str) -> Path:
        if not isinstance(installation_id, str) or not re.fullmatch(r"[0-9a-f]{64}", installation_id):
            raise PackageError("invalid package installation ID")
        directory = self.root / installation_id
        if self.root.resolve() not in directory.resolve().parents:
            raise PackageError("package directory escapes the store")
        return directory

    @classmethod
    def _snapshot(cls, directory: Path) -> dict[str, str]:
        if not directory.is_dir():
            raise PackageError("package working directory is missing")
        result = {}
        for path in directory.rglob("*"):
            if path.is_symlink() or path.is_junction():
                raise PackageError("package working directory contains a link")
            if path.is_file():
                result[path.relative_to(directory).as_posix()] = cls._digest(path)
        return result

    def status(self, installation_id: str) -> dict:
        with self._locked():
            return self._status(installation_id)

    def list_installations(self) -> list[dict]:
        """Read current integrity, reporting damaged installations individually."""
        with self._locked():
            result = []
            for directory in sorted(self.root.iterdir()):
                if not re.fullmatch(r"[0-9a-f]{64}", directory.name):
                    continue
                try:
                    result.append(self._status(directory.name))
                except (OSError, ValueError, KeyError, TypeError, BadZipFile) as error:
                    result.append({"installation_id": directory.name, "error": str(error)})
            return result

    def preview(self, package_path: Path) -> dict:
        """Bind the inspected manifest to the exact bytes presented for consent."""
        with tempfile.TemporaryDirectory(prefix="botstudio-package-preview-") as temporary:
            original = Path(temporary) / "original.zip"
            shutil.copyfile(package_path, original)
            manifest = self.packages.inspect_package(str(original))
            return {
                "path": str(package_path),
                "manifest": manifest,
                "archive_sha256": self._digest(original),
            }

    def _status(self, installation_id: str) -> dict:
        directory = self._directory(installation_id)
        state = read_json(directory / "current.json")
        if not isinstance(state, dict) or not isinstance(state.get("baseline"), dict):
            raise PackageError("invalid managed package state")
        generation = state.get("generation")
        if state.get("schema_version") != 1 or not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{32}", generation):
            raise PackageError("invalid managed package state")
        location = directory / "generations" / generation
        if directory.resolve() not in location.resolve().parents:
            raise PackageError("package generation escapes its installation")
        original = location / "original.botstudio.zip"
        if not original.is_file() or self._digest(original) != state.get("archive_sha256"):
            raise PackageError("preserved package original is missing or changed")
        manifest = self.packages.inspect_package(str(original))
        if self._installation_id(manifest) != installation_id:
            raise PackageError("preserved original has a different package identity")
        working = location / "working"
        modified = self._snapshot(working) != state["baseline"]
        details = manifest["package_metadata"]
        source = None
        if "catalog_source" in state:
            source = GitHubCatalogService.validate_source(state["catalog_source"], state["archive_sha256"])
        source_path = location / "catalog-source.json"
        if source is not None or source_path.exists():
            receipt = GitHubCatalogService.validate_source(read_json(source_path), state["archive_sha256"])
            if receipt != source:
                raise PackageError("managed package catalog source receipt changed")
        return {
            "installation_id": installation_id,
            "version": details["package_version"],
            "publisher_key": PackageService.publisher_identity_key(details["publisher"]),
            "publisher_login": details["publisher"].get("login"),
            "publisher_verification": "unverified",
            "package_id": details["package_id"],
            "game_id": manifest["game"]["id"],
            "fork_of": details.get("fork_of"),
            "catalog_source": source,
            "modified": modified,
            "integrity": "modified" if modified else "matches_original",
            "working_directory": str(working),
            "original_archive": str(original),
        }

    def install(
        self, package_path: Path, *, expected_sha256: str | None = None,
        catalog_source: dict | None = None,
    ) -> dict:
        with self._locked():
            return self._activate(
                Path(package_path), expected_sha256=expected_sha256, catalog_source=catalog_source,
            )

    def update(
        self, installation_id: str, package_path: Path, *, expected_sha256: str | None = None,
        catalog_source: dict | None = None,
    ) -> dict:
        """Explicit local update only; remote channel verification is not implemented."""
        with self._locked():
            return self._activate(
                Path(package_path), installation_id,
                expected_sha256=expected_sha256, catalog_source=catalog_source,
            )

    def _activate(
        self, package_path: Path, expected_id: str | None = None,
        *, expected_sha256: str | None = None,
        catalog_source: dict | None = None,
    ) -> dict:
        with tempfile.TemporaryDirectory(prefix=".package-staging-", dir=self.root) as temporary:
            staging = Path(temporary)
            original = staging / "original.botstudio.zip"
            shutil.copyfile(package_path, original)
            if expected_sha256 is not None and self._digest(original) != expected_sha256:
                raise PackageError("package changed since preview; inspect it again before confirming")
            source = None
            if catalog_source is not None:
                if expected_sha256 is None:
                    raise PackageError("catalog source requires hash-bound preview consent")
                source = GitHubCatalogService.validate_source(catalog_source, self._digest(original))
            manifest = self.packages.inspect_package(str(original))
            installation_id = self._installation_id(manifest)
            version = self._version(manifest["package_metadata"]["package_version"])
            directory = self._directory(installation_id)
            if expected_id is not None:
                self._directory(expected_id)
                if installation_id != expected_id:
                    raise PackageError("different package or publisher: install as a separate alternative, not an update")
                current = self._status(expected_id)
                old_source = current["catalog_source"]
                if (old_source is None) != (source is None) or (
                    old_source is not None and source is not None
                    and (old_source["repository"], old_source["repository_id"])
                    != (source["repository"], source["repository_id"])
                ):
                    raise PackageError("package source changed; explicit source migration is required")
                if old_source is not None and source is not None and source["asset_id"] == old_source["asset_id"]:
                    raise PackageError("catalog update requires a new package asset ID")
                if current["modified"]:
                    raise PackageError("local changes must be preserved or resolved before updating")
                if version <= self._version(current["version"]):
                    raise PackageError("update version must be greater than the installed version")
            elif (directory / "current.json").exists():
                raise PackageError("package already installed; use the explicit update operation")
            working = staging / "working"
            self.packages.import_package(str(original), str(working))
            baseline = self._snapshot(working)
            generation = uuid4().hex
            generations = directory / "generations"
            generations.mkdir(parents=True, exist_ok=True)
            destination = generations / generation
            state = {
                "schema_version": 1, "generation": generation,
                "archive_sha256": self._digest(original), "baseline": baseline,
            }
            if source is not None:
                state["catalog_source"] = source
                atomic_write_json(staging / "catalog-source.json", source)
            if expected_id is not None and self._status(expected_id)["modified"]:
                raise PackageError("local changes appeared while preparing the update")
            staging.rename(destination)
            try:
                atomic_write_json(directory / "current.json", state)
            except OSError:
                shutil.rmtree(destination)
                raise
        return self._status(installation_id)
