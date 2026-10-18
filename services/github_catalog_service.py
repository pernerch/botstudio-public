import base64
import binascii
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import hashlib
import os
from pathlib import Path
import re
import tempfile
from time import monotonic
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile

import requests

from models.github_account import GitHubAccount, GitHubAssetUploader
from services.github_auth_service import GitHubCredentials
from services.package_service import PackageError
from ui.i18n.translator import t


class GitHubCatalogService:
    """Inspect public catalog packages; never grants package update authority."""

    REPOSITORY = "pernerch/botstudio-catalog"
    API_ROOT = "https://api.github.com"
    MAX_RESPONSE_BYTES = 2 * 1024 * 1024
    RELEASE_LIMIT = 30
    MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
    MAX_EXPANDED_BYTES = 512 * 1024 * 1024
    MAX_ARCHIVE_FILES = 10000

    def __init__(self, request=None, credentials=None):
        self.request = request if request is not None else requests.get
        self.credentials = credentials if credentials is not None else GitHubCredentials()

    def _api_headers(self, accept):
        headers = {"Accept": accept, "User-Agent": "BotStudio"}
        try:
            token = self.credentials.load()
        except (OSError, UnicodeError):
            raise PackageError(t("packages.catalog_credentials_failed")) from None
        if token is not None:
            if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_]+", token):
                raise PackageError(t("packages.catalog_credentials_failed"))
            headers["Authorization"] = f"Bearer {token}"
        return headers

    @staticmethod
    def _http_error(response):
        status = response.status_code
        if status == 401:
            return PackageError(t("packages.catalog_auth_failed"))
        headers = response.headers
        remaining = headers.get("X-RateLimit-Remaining")
        retry = headers.get("Retry-After")
        if status in {403, 429} and (status == 429 or remaining == "0" or isinstance(retry, str)):
            retry_at = None
            reset = headers.get("X-RateLimit-Reset")
            if isinstance(retry, str):
                try:
                    if re.fullmatch(r"[0-9]{1,10}", retry):
                        retry_at = datetime.fromtimestamp(
                            datetime.now(timezone.utc).timestamp() + int(retry), timezone.utc,
                        )
                    else:
                        retry_at = parsedate_to_datetime(retry)
                        if retry_at.tzinfo is None:
                            retry_at = None
                except (ValueError, TypeError, OverflowError, OSError):
                    retry_at = None
            if retry_at is None and isinstance(reset, str) and re.fullmatch(r"[0-9]{1,12}", reset):
                try:
                    retry_at = datetime.fromtimestamp(int(reset), timezone.utc)
                except (ValueError, OverflowError, OSError):
                    retry_at = None
            if retry_at is not None:
                return PackageError(t("packages.catalog_rate_limit_at").format(
                    when=retry_at.astimezone().strftime("%Y-%m-%d %H:%M:%S %z"),
                ))
            return PackageError(t("packages.catalog_rate_limit"))
        return PackageError(f"GitHub catalog HTTP {status}")

    def _json(self, path):
        if not isinstance(path, str) or not re.match(
            rf"/(?:repos/{re.escape(self.REPOSITORY)}(?:/|$)|repositories/[1-9][0-9]*(?:/|$))",
            path,
        ):
            raise PackageError("invalid GitHub catalog API path")
        response = None
        started = monotonic()
        try:
            headers = self._api_headers("application/vnd.github+json")
            headers["X-GitHub-Api-Version"] = "2022-11-28"
            response = self.request(
                self.API_ROOT + path,
                headers=headers,
                timeout=(5, 10), allow_redirects=False, stream=True,
            )
            if response.status_code != 200:
                raise self._http_error(response)
            content = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                if monotonic() - started > 20:
                    raise PackageError("GitHub catalog response exceeded its time budget")
                content.extend(chunk)
                if len(content) > self.MAX_RESPONSE_BYTES:
                    raise PackageError("GitHub catalog response exceeds size limit")
            return json.loads(content)
        except (requests.RequestException, json.JSONDecodeError, UnicodeDecodeError):
            raise PackageError("GitHub catalog request failed or returned invalid JSON") from None
        finally:
            if response is not None:
                response.close()

    @staticmethod
    def _id(value):
        if type(value) is not int or not 1 <= value < 10**20:
            raise PackageError("GitHub catalog returned an invalid identity")
        return value

    @staticmethod
    def _text(value, limit=256):
        if not isinstance(value, str) or not value.strip() or len(value) > limit or any(
            ord(char) < 32 or ord(char) == 127 for char in value
        ):
            raise PackageError("GitHub catalog returned invalid display text")
        return value

    def load(self, *, include_details=True, page=1, release_id=None):
        if type(page) is not int or not 1 <= page <= 100000:
            raise PackageError("invalid GitHub catalog page")
        if release_id is not None:
            self._id(release_id)
        repository = self._json(f"/repos/{self.REPOSITORY}")
        if not isinstance(repository, dict) or repository.get("full_name") != self.REPOSITORY:
            raise PackageError("GitHub catalog repository identity does not match")
        if repository.get("private") is True:
            raise PackageError("GitHub catalog repository must be public")
        repository_id = self._id(repository.get("id"))
        root = f"/repositories/{repository_id}/releases"
        if release_id is not None:
            release = self._json(f"{root}/{release_id}")
            if not isinstance(release, dict) or release.get("id") != release_id:
                raise PackageError("GitHub catalog returned a different release")
            releases = [release]
        else:
            suffix = f"&page={page}" if page != 1 else ""
            releases = self._json(f"{root}?per_page={self.RELEASE_LIMIT}{suffix}")
        if not isinstance(releases, list) or len(releases) > self.RELEASE_LIMIT:
            raise PackageError("GitHub catalog returned an invalid release list")
        next_page = page + 1 if release_id is None and len(releases) == self.RELEASE_LIMIT else None
        entries, seen = [], set()
        for release in releases:
            if not isinstance(release, dict) or type(release.get("draft")) is not bool:
                raise PackageError("GitHub catalog returned an invalid release")
            if release["draft"]:
                continue
            release_id = self._id(release.get("id"))
            tag = self._text(release.get("tag_name"))
            if type(release.get("prerelease")) is not bool or not isinstance(release.get("assets"), list):
                raise PackageError("GitHub catalog returned invalid release assets")
            if len(release["assets"]) > 100:
                raise PackageError("GitHub catalog release exceeds asset limit")
            for asset in release["assets"]:
                if not isinstance(asset, dict):
                    raise PackageError("GitHub catalog returned an invalid asset")
                name = self._text(asset.get("name"))
                if not name.endswith(".botstudio.zip"):
                    continue
                asset_id = self._id(asset.get("id"))
                if asset_id in seen or asset.get("state") != "uploaded":
                    raise PackageError("GitHub catalog returned a duplicate or incomplete package asset")
                seen.add(asset_id)
                size = asset.get("size")
                uploader = asset.get("uploader")
                if type(size) is not int or size <= 0 or not isinstance(uploader, dict):
                    raise PackageError("GitHub catalog returned invalid package asset metadata")
                try:
                    account = GitHubAssetUploader(
                        str(self._id(uploader.get("id"))), self._text(uploader.get("login"), 44),
                    )
                except ValueError:
                    raise PackageError("GitHub catalog returned an invalid uploader account") from None
                entry = {
                    "release_id": release_id, "tag": tag, "prerelease": release["prerelease"],
                    "asset_id": asset_id, "name": name, "size": size,
                    "uploader": account,
                    "digest": asset.get("digest"),
                }
                details = self._release_details(repository_id, release, entry) if include_details else None
                if details is not None:
                    entry["package"] = details
                entries.append(entry)
        return {
            "repository": self.REPOSITORY, "repository_id": repository_id, "entries": entries,
            "next_page": next_page,
        }

    def _release_details(self, repository_id, release, entry):
        body = release.get("body")
        if not body:
            return None
        if isinstance(body, str):
            body = body.replace("\r\n", "\n")
        marker = "<!-- botstudio-publication-v1\n"
        marked = isinstance(body, str) and marker in body
        if marked:
            if body.count(marker) != 1:
                raise PackageError("GitHub catalog contains multiple publication metadata blocks")
            _, _, metadata = body.partition(marker)
            body, separator, remaining = metadata.partition("\n-->")
            if not separator or remaining.strip():
                raise PackageError("GitHub catalog contains malformed publication metadata")
        try:
            publication = json.loads(body)
        except (TypeError, json.JSONDecodeError):
            if marked:
                raise PackageError("GitHub catalog contains invalid publication metadata") from None
            return None
        if not isinstance(publication, dict) or publication.get("schema_version") != 1:
            if marked:
                raise PackageError("GitHub catalog contains unsupported publication metadata")
            return None
        try:
            if type(publication["schema_version"]) is not int:
                raise ValueError("invalid publication schema")
            number = self._id(publication["pull_request"])
            head = publication["head"]
            if (
                not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head)
                or entry["tag"] != f"catalog-pr-{number}-{head}"
                or entry["digest"] != "sha256:" + publication["archive_sha256"]
            ):
                raise ValueError("publication identity mismatch")
            publisher = GitHubAccount.from_dict(publication["submitter"])
            if "submission" in publication:
                descriptor = publication["submission"]
                if not isinstance(descriptor, dict):
                    raise ValueError("invalid embedded submission")
                metadata = [descriptor["metadata_path"]]
                files = [{"filename": descriptor[key]} for key in ("archive_path", "metadata_path")]
            else:
                root = f"/repositories/{repository_id}"
                files = self._json(f"{root}/pulls/{number}/files?per_page=3")
                if (
                    not isinstance(files, list) or len(files) != 2
                    or not all(isinstance(item, dict) and item.get("status") == "added" for item in files)
                ):
                    raise ValueError("invalid submission files")
                metadata = [
                    item["filename"] for item in files
                    if item["filename"].endswith("/submission.json")
                ]
                if len(metadata) != 1 or not re.fullmatch(
                    r"games/[0-9a-f]{64}/publishers/[1-9][0-9]{0,19}/packages/[0-9a-f]{64}/"
                    r"[0-9]+\.[0-9]+\.[0-9]+/submission\.json", metadata[0],
                ):
                    raise ValueError("invalid submission path")
                content = self._json(f"{root}/contents/{metadata[0]}?ref={head}")
                if content.get("type") != "file" or content.get("encoding") != "base64":
                    raise ValueError("invalid submission content")
                raw = base64.b64decode("".join(content["content"].splitlines()), validate=True)
                descriptor = json.loads(raw)
            game = self._text(descriptor["game_id"])
            package = self._text(descriptor["package_id"], 128)
            version = descriptor["package_version"]
            if (
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", package)
                or not isinstance(version, str)
                or not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version)
            ):
                raise ValueError("invalid package identity")
            prefix = (
                f"games/{hashlib.sha256(game.encode()).hexdigest()}/publishers/{publisher.account_id}/"
                f"packages/{hashlib.sha256(package.encode()).hexdigest()}/{version}"
            )
            if (
                type(descriptor["schema_version"]) is not int or descriptor["schema_version"] != 1
                or descriptor["repository"] != self.REPOSITORY
                or descriptor["submitter"] != publisher.to_dict()
                or descriptor["archive_sha256"] != publication["archive_sha256"]
                or type(descriptor["archive_size"]) is not int or descriptor["archive_size"] != entry["size"]
                or descriptor["metadata_path"] != metadata[0]
                or metadata[0] != prefix + "/submission.json"
                or descriptor["archive_path"] != prefix + "/package.botstudio.zip"
                or {item["filename"] for item in files} != {
                    descriptor["metadata_path"], descriptor["archive_path"],
                }
            ):
                raise ValueError("submission does not match release")
            details = {
                "game_id": game, "package_id": package, "version": version,
                "publisher": publisher,
            }
            if "display" in publication:
                display = publication["display"]
                if not isinstance(display, dict) or set(display) != {
                    "package_name", "game_name", "package_type", "description", "contents", "author",
                }:
                    raise ValueError("invalid package display metadata")
                for field in ("package_name", "game_name", "author"):
                    self._text(display[field], 10000)
                if display["package_type"] not in ("event", "function", "helper"):
                    raise ValueError("invalid package type")
                if not isinstance(display["description"], str) or len(display["description"]) > 120000:
                    raise ValueError("invalid package description")
                if not isinstance(display["contents"], dict) or not all(
                    isinstance(key, str) and type(value) is int and value >= 0
                    for key, value in display["contents"].items()
                ):
                    raise ValueError("invalid package contents")
                details.update(display)
            return details
        except (KeyError, TypeError, ValueError, AttributeError, binascii.Error):
            raise PackageError("GitHub catalog publication metadata is invalid") from None

    def _current_entry(self, catalog, entry):
        current = self.load(include_details=False, release_id=entry["release_id"])
        transport_entry = {key: value for key, value in entry.items() if key != "package"}
        if (
            current["repository_id"] != catalog["repository_id"]
            or current["repository"] != catalog["repository"]
            or transport_entry not in [
                {key: value for key, value in item.items() if key != "package"}
                for item in current["entries"]
            ]
        ):
            raise PackageError("GitHub catalog changed; reload and select the package again")

    def published_content(self, game_id, account, packages):
        """Fresh, bounded comparison against the account's latest public channels."""
        from services.managed_package_service import ManagedPackageService

        started = monotonic()
        latest = {}
        catalog = None
        for page in range(1, 11):
            if monotonic() - started > 120:
                raise PackageError("GitHub publication comparison exceeded its time budget")
            current = self.load(page=page)
            if catalog is not None and current["repository_id"] != catalog["repository_id"]:
                raise PackageError("GitHub catalog changed during publication comparison")
            catalog = current
            for entry in current["entries"]:
                details = entry.get("package")
                if (
                    not details or details["game_id"] != game_id
                    or details["publisher"].account_id != account.account_id
                ):
                    continue
                key = details["package_id"]
                version = tuple(int(part) for part in details["version"].split("."))
                if key not in latest or version > latest[key][0]:
                    latest[key] = (version, entry)
            if current["next_page"] is None:
                break
        else:
            raise PackageError("GitHub publication comparison exceeds 10 catalog pages")
        entries = [item[1] for item in latest.values()]
        if len(entries) > 30 or sum(entry["size"] for entry in entries) > self.MAX_DOWNLOAD_BYTES:
            raise PackageError("GitHub publication comparison exceeds its package download budget")
        fingerprints = {}
        with tempfile.TemporaryDirectory(prefix="botstudio-published-content-") as temporary:
            managed = ManagedPackageService(Path(temporary) / "managed", packages)
            snapshot = {**catalog, "entries": entries}
            for index, entry in enumerate(entries):
                if monotonic() - started > 120:
                    raise PackageError("GitHub publication comparison exceeded its time budget")
                archive = Path(temporary) / f"{index}.botstudio.zip"
                preview = self.download_preview(snapshot, entry, archive, managed)
                manifest = preview["manifest"]
                fingerprints[packages.archive_content_fingerprint(archive)] = (
                    manifest["package_name"], manifest["package_type"],
                )
        return fingerprints

    def check_archive_limits(self, archive_path):
        try:
            with ZipFile(archive_path) as package:
                entries = package.infolist()
                if (
                    len(entries) > self.MAX_ARCHIVE_FILES
                    or sum(item.file_size for item in entries) > self.MAX_EXPANDED_BYTES
                ):
                    raise PackageError("GitHub package exceeds archive expansion limits")
        except BadZipFile:
            raise PackageError("GitHub package is not a valid ZIP archive") from None

    @classmethod
    def validate_source(cls, source, archive_sha256):
        """Validate historical download evidence, not publisher authorization."""
        keys = {
            "repository", "repository_id", "release_id", "asset_id",
            "archive_sha256", "uploader",
        }
        if not isinstance(source, dict) or set(source) != keys:
            raise PackageError("invalid GitHub catalog source receipt")
        if (
            source["repository"] != cls.REPOSITORY
            or not isinstance(archive_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", archive_sha256)
            or source["archive_sha256"] != archive_sha256
        ):
            raise PackageError("GitHub catalog source receipt does not match the archive")
        for key in ("repository_id", "release_id", "asset_id"):
            cls._id(source[key])
        uploader = source["uploader"]
        if not isinstance(uploader, dict) or set(uploader) != {"account_id", "login"}:
            raise PackageError("invalid GitHub catalog source uploader")
        try:
            account = GitHubAssetUploader(uploader["account_id"], uploader["login"])
        except (TypeError, ValueError):
            raise PackageError("invalid GitHub catalog source uploader") from None
        return {
            **source,
            "uploader": {"account_id": account.account_id, "login": account.login},
        }

    def download_preview(self, catalog, entry, destination, managed):
        """Publish only validated bytes to a user-chosen new local file."""
        if entry not in catalog["entries"]:
            raise PackageError("select a package from the current GitHub catalog")
        digest = entry.get("digest")
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise PackageError("GitHub package asset requires a SHA-256 digest; ask the catalog maintainer to reupload it")
        if not 0 < entry["size"] <= self.MAX_DOWNLOAD_BYTES:
            raise PackageError("GitHub package exceeds the download size limit")
        destination = Path(destination)
        if destination.exists() or destination.is_symlink():
            raise PackageError("download target already exists; choose a new file")
        self._current_entry(catalog, entry)
        with tempfile.TemporaryDirectory(prefix="botstudio-download-", dir=destination.parent) as temporary:
            archive = Path(temporary) / "package.botstudio.zip"
            self._download(catalog["repository_id"], entry, archive)
            self.check_archive_limits(archive)
            preview = managed.preview(archive)
            if preview["archive_sha256"] != digest.removeprefix("sha256:"):
                raise PackageError("downloaded package changed during inspection")
            details = entry.get("package")
            if details is not None:
                manifest = preview["manifest"]
                metadata = manifest.get("package_metadata", {})
                if (
                    manifest["game"]["id"] != details["game_id"]
                    or metadata.get("package_id") != details["package_id"]
                    or metadata.get("package_version") != details["version"]
                    or metadata.get("publisher") != details["publisher"].to_dict()
                ):
                    raise PackageError("downloaded package does not match catalog grouping")
                if "package_name" in details:
                    kind = manifest.get("package_type")
                    if kind == "function" and metadata["package_id"].startswith("helper-"):
                        kind = "helper"
                    display = {
                        "package_name": manifest["package_name"],
                        "game_name": manifest["game"].get("name") or manifest["game"]["id"],
                        "package_type": kind, "description": manifest["description"],
                        "contents": manifest["contents"], "author": metadata["author"],
                    }
                    if any(details[key] != value for key, value in display.items()):
                        raise PackageError("downloaded package does not match catalog display metadata")
            self._current_entry(catalog, entry)
            source = self.validate_source({
                "repository": catalog["repository"],
                "repository_id": catalog["repository_id"],
                "release_id": entry["release_id"], "asset_id": entry["asset_id"],
                "archive_sha256": preview["archive_sha256"],
                "uploader": {
                    "account_id": entry["uploader"].account_id,
                    "login": entry["uploader"].login,
                },
            }, preview["archive_sha256"])
            # Publish a complete new file without replacing a concurrent local file.
            os.link(archive, destination)
            preview["path"] = str(destination)
            preview["catalog_source"] = source
            return preview

    def _download(self, repository_id, entry, destination):
        url = f"{self.API_ROOT}/repositories/{repository_id}/releases/assets/{entry['asset_id']}"
        started = monotonic()
        response = None
        try:
            for attempt in range(3):
                headers = (
                    self._api_headers("application/octet-stream") if attempt == 0
                    else {"Accept": "application/octet-stream", "User-Agent": "BotStudio"}
                )
                response = self.request(
                    url, headers=headers,
                    timeout=(5, 10), allow_redirects=False, stream=True,
                )
                if response.status_code not in {301, 302, 303, 307, 308}:
                    break
                location = response.headers.get("Location", "")
                target = urlsplit(location)
                if (
                    attempt == 2 or monotonic() - started > 60
                    or target.scheme != "https"
                    or target.hostname not in {"release-assets.githubusercontent.com", "objects.githubusercontent.com"}
                    or target.username is not None or target.password is not None
                    or target.port not in {None, 443} or target.fragment
                ):
                    raise PackageError("GitHub package redirect is not permitted")
                response.close()
                response = None
                url = location
            if response.status_code != 200:
                raise self._http_error(response)
            size, digest = 0, hashlib.sha256()
            with destination.open("xb") as handle:
                for chunk in response.iter_content(chunk_size=65536):
                    size += len(chunk)
                    if size > entry["size"] or size > self.MAX_DOWNLOAD_BYTES:
                        raise PackageError("GitHub package download exceeds expected size")
                    if monotonic() - started > 60:
                        raise PackageError("GitHub package download exceeded its time budget")
                    digest.update(chunk)
                    handle.write(chunk)
            if size != entry["size"] or "sha256:" + digest.hexdigest() != entry["digest"]:
                raise PackageError("GitHub package download size or SHA-256 does not match")
        except PackageError:
            raise
        except (requests.RequestException, ValueError):
            raise PackageError("GitHub package download failed or returned an invalid redirect") from None
        finally:
            if response is not None:
                response.close()
