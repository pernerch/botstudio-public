"""Local description drafts and bounded, verified package image decoding.

All methods performing file access or image decoding belong on a worker.
Drafts are local authoring data, never publication or rights confirmation.
"""

import hashlib
import io
import json
import os
from pathlib import Path
import re
import uuid
import zipfile

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QImageReader

from services.package_service import (
    MAX_PACKAGE_IMAGE_BYTES, MAX_PACKAGE_IMAGES, MAX_PUBLISH_ARCHIVE_BYTES,
    PACKAGE_IMAGE_EXTENSIONS, PackageError,
)


class PackageDescriptionService:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else (
            Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
            / "BotStudio" / "package-descriptions"
        )

    @staticmethod
    def _asset_name(name):
        if not isinstance(name, str) or not re.fullmatch(
            r"content/package-assets/image-[0-9]{2,3}\.(png|jpe?g|webp|gif|bmp)", name,
        ):
            raise PackageError("unsafe package image path")
        return name

    def _directory(self, key):
        identity = json.dumps(list(key), ensure_ascii=True).encode("utf-8")
        return self.root / hashlib.sha256(identity).hexdigest()

    @staticmethod
    def decode(data):
        if not 0 < len(data) <= MAX_PACKAGE_IMAGE_BYTES:
            raise PackageError("image is empty or exceeds 20 MiB")
        buffer = QBuffer()
        buffer.setData(QByteArray(data))
        buffer.open(QIODevice.ReadOnly)
        reader = QImageReader(buffer)
        size = reader.size()
        if not size.isValid() or size.width() * size.height() > 16_000_000:
            raise PackageError("image dimensions exceed 16 megapixels or are invalid")
        reader.setScaledSize(size.scaled(1600, 1600, Qt.KeepAspectRatio))
        image = reader.read()
        if image.isNull():
            raise PackageError("image could not be decoded")
        return image

    @classmethod
    def read_image(cls, source):
        source = Path(source)
        if source.suffix.lower() not in PACKAGE_IMAGE_EXTENSIONS:
            raise PackageError("unsupported image format")
        if source.is_symlink() or not source.is_file():
            raise PackageError("image must be a regular file")
        with source.open("rb") as stream:
            data = stream.read(MAX_PACKAGE_IMAGE_BYTES + 1)
        return data, cls.decode(data)

    def has_draft(self, key):
        return (self._directory(key) / "draft.json").exists()

    def load(self, key, seed="", package_source=None):
        directory = self._directory(key)
        path = directory / "draft.json"
        if not path.exists():
            if package_source is not None:
                manifest, archive_path = package_source
                return self._seed_archive(key, manifest, archive_path)
            return {"markdown": seed, "assets": {}}
        if directory.is_symlink() or path.is_symlink():
            raise PackageError("unsafe description draft")
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document.get("markdown"), str):
            raise PackageError("invalid description draft")
        assets = {}
        for name, filename in document.get("assets", {}).items():
            self._asset_name(name)
            if not re.fullmatch(r"[0-9a-f]{64}\.(png|jpe?g|webp|gif|bmp)", filename):
                raise PackageError("invalid draft image")
            source = directory / filename
            data, _ = self.read_image(source)
            if hashlib.sha256(data).hexdigest() != source.stem:
                raise PackageError("draft image checksum changed")
            assets[name] = source
        if len(assets) > MAX_PACKAGE_IMAGES:
            raise PackageError("too many draft images")
        return {"markdown": document["markdown"], "assets": assets}

    def save(self, key, markdown, assets, image_bytes=None):
        if len(assets) > MAX_PACKAGE_IMAGES:
            raise PackageError("too many draft images")
        if image_bytes is not None and set(image_bytes) != set(assets):
            raise PackageError("draft image selection changed")
        images = {}
        for name, source in assets.items():
            self._asset_name(name)
            if image_bytes is None:
                data, _ = self.read_image(source)
            else:
                data = image_bytes[name]
                self.decode(data)
            images[name] = data
        return self._persist(key, markdown, images)

    def _persist(self, key, markdown, images):
        directory = self._directory(key)
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink():
            raise PackageError("unsafe description draft")
        stored = {}
        validated = {}
        total_size = 0
        for name, data in images.items():
            self._asset_name(name)
            total_size += len(data)
            if total_size > MAX_PUBLISH_ARCHIVE_BYTES:
                raise PackageError("draft images exceed the 100 MiB package limit")
            filename = hashlib.sha256(data).hexdigest() + Path(name).suffix
            target = directory / filename
            if target.is_symlink():
                raise PackageError("unsafe draft image")
            validated[filename] = data
            stored[name] = filename
        destination = directory / "draft.json"
        staging = directory / (uuid.uuid4().hex + ".json")
        created = []
        try:
            for filename, data in validated.items():
                target = directory / filename
                if not target.exists():
                    created.append(target)
                target.write_bytes(data)
            staging.write_text(json.dumps({
                "markdown": markdown, "assets": stored,
            }, ensure_ascii=False), encoding="utf-8")
            staging.replace(destination)
        except Exception:
            for target in created:
                target.unlink(missing_ok=True)
            raise
        finally:
            staging.unlink(missing_ok=True)
        for previous in directory.iterdir():
            if (re.fullmatch(r"[0-9a-f]{64}\.(png|jpe?g|webp|gif|bmp)", previous.name)
                    and previous.name not in stored.values()):
                previous.unlink()
        return {"markdown": markdown, "assets": {
            name: directory / filename for name, filename in stored.items()
        }}

    def _seed_archive(self, key, manifest, path):
        path = Path(path)
        if path.is_symlink():
            raise PackageError("unsafe preserved package")
        with path.open("rb") as stream:
            data = stream.read(MAX_PUBLISH_ARCHIVE_BYTES + 1)
        if len(data) > MAX_PUBLISH_ARCHIVE_BYTES:
            raise PackageError("preserved package exceeds 100 MiB")
        images = {}
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for name, checksum in manifest["checksums"].items():
                if not name.startswith("content/package-assets/"):
                    continue
                self._asset_name(name)
                if len(images) >= MAX_PACKAGE_IMAGES:
                    raise PackageError("too many preserved package images")
                if archive.getinfo(name).file_size > MAX_PACKAGE_IMAGE_BYTES:
                    raise PackageError("preserved image exceeds 20 MiB")
                image_data = archive.read(name)
                if hashlib.sha256(image_data).hexdigest() != checksum:
                    raise PackageError("preserved package image changed")
                self.decode(image_data)
                images[name] = image_data
        return self._persist(key, manifest.get("description", ""), images)

    @classmethod
    def preview_images(cls, preview):
        """Read only the exact archive already inspected by the package worker."""
        path = Path(preview["path"])
        if path.is_symlink():
            raise PackageError("unsafe package archive")
        with path.open("rb") as stream:
            data = stream.read(MAX_PUBLISH_ARCHIVE_BYTES + 1)
        if len(data) > MAX_PUBLISH_ARCHIVE_BYTES:
            raise PackageError("package exceeds image preview limit")
        if hashlib.sha256(data).hexdigest() != preview["archive_sha256"]:
            raise PackageError("package changed during image preview")
        images = {}
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            checksums = json.loads(archive.read("checksums.json"))
            for name in archive.namelist():
                if not name.startswith("content/package-assets/"):
                    continue
                cls._asset_name(name)
                if len(images) >= MAX_PACKAGE_IMAGES:
                    raise PackageError("too many package images")
                if archive.getinfo(name).file_size > MAX_PACKAGE_IMAGE_BYTES:
                    raise PackageError("package image exceeds 20 MiB")
                image_data = archive.read(name)
                if hashlib.sha256(image_data).hexdigest() != checksums.get(name):
                    raise PackageError("package image checksum changed")
                images[name] = cls.decode(image_data)
        return images
