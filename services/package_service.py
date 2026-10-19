from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from models.bot_function import BotFunction, FunctionPackageOrigin
from models.github_account import GitHubAccount
from scheduler.models import PROTECTED_TASK_KINDS
from services.game_service import GameService
from services.library_service import LibraryService
from services.package_license_service import PackagePreparationService
from services.variable_service import VariableService
from services.application_info import APP_VERSION
from services.application_paths import ApplicationPaths
from services.file_io import read_json


PACKAGE_FORMAT = "botstudio-package"
PACKAGE_VERSION = 1
MAX_PUBLISH_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_PACKAGE_IMAGES = 20
MAX_PACKAGE_IMAGE_BYTES = 20 * 1024 * 1024
PACKAGE_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
PackageFiles = dict[str, Path | bytes]


class PackageError(ValueError):
    """Raised when a BotStudio package is invalid or conflicts with local data."""


class PackageService:
    """Creates and imports single-file, manifest-first BotStudio packages."""

    def __init__(
        self,
        function_root: str = "data/functions",
        library_service=None,
        variable_service=None,
        game_service=None,
    ):
        self.function_root = Path(function_root)
        self.library_service = library_service or LibraryService()
        self.variable_service = variable_service or VariableService()
        self.game_service = game_service or GameService()
        self.preparation_service = PackagePreparationService(
            function_root=str(self.function_root),
        )

    def list_saved_functions(self, game_id: str) -> list[BotFunction]:
        self._validate_path_component(game_id, "game")
        game_root = self.function_root / game_id
        if not game_root.exists():
            return []
        functions = []
        for source in sorted(game_root.glob("*.json")):
            function = self._read_function_file(source)
            if function.game_id != game_id or function.function_id != source.stem:
                raise PackageError(
                    f"saved Function file has mismatched identity: {source.name}"
                )
            if function.saved:
                functions.append(function)
        return functions

    def load_saved_function(self, game_id: str, function_id: str) -> BotFunction:
        self._validate_path_component(game_id, "game")
        self._validate_path_component(function_id, "Function")
        source = self.function_root / game_id / f"{function_id}.json"
        if not source.exists():
            raise PackageError("the selected saved Function no longer exists")
        function = self._read_function_file(source)
        if (
            function.game_id != game_id
            or function.function_id != function_id
            or not function.saved
        ):
            raise PackageError("the selected saved Function is no longer available")
        return function

    @staticmethod
    def _event_is_exportable(event) -> bool:
        return not (
            event.is_init or event.is_interrupt
            or any(task.protected or task.kind in PROTECTED_TASK_KINDS for task in event.tasks)
        )

    @staticmethod
    def _portable_event_content(payload):
        payload = deepcopy(payload)
        runtime_fields = {
            "last_started_at", "last_finished_at", "duration_samples_seconds",
            "last_executed", "next_due_at", "status", "used", "used_by",
        }
        local_settings = {
            "enabled", "priority", "sort_order", "schedule_mode", "interval_seconds",
            "run_at_local", "cooldown_seconds", "retry_minutes",
        }
        for key in runtime_fields | local_settings | {"event_id", "profile_id"}:
            payload.pop(key, None)
        for task in payload["tasks"]:
            for key in runtime_fields | {"task_id", "name", "enabled", "priority", "sort_order"}:
                task.pop(key, None)
        return payload

    @staticmethod
    def preparation_event_sources(events) -> list[tuple[str, str, str, str]]:
        """Group portable content, not profile ownership or execution history."""
        unique = {}
        for event in sorted(events, key=lambda item: item.event_id):
            if not PackageService._event_is_exportable(event):
                continue
            if not any(task.function_ids for task in event.tasks):
                continue
            payload = PackageService._portable_event_content(event.to_dict())
            identity = json.dumps(payload, sort_keys=True, ensure_ascii=True)
            unique.setdefault(identity, event)
        names = {}
        for event in unique.values():
            name = event.name or event.event_id
            names[name] = names.get(name, 0) + 1
        return [
            (
                event.event_id,
                (event.name or event.event_id)
                + (f" [{event.event_id}]" if names[event.name or event.event_id] > 1 else ""),
                event.name,
                "",
            )
            for event in unique.values()
        ]

    @staticmethod
    def _validate_path_component(value: str, label: str) -> None:
        if (
            not value
            or value in (".", "..")
            or "/" in value
            or "\\" in value
            or ":" in value
        ):
            raise PackageError(f"invalid {label} identifier")

    @staticmethod
    def _read_function_file(source: Path) -> BotFunction:
        try:
            function = BotFunction.from_dict(
                json.loads(source.read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as error:
            raise PackageError(
                f"saved Function file is invalid: {source.name}"
            ) from error
        if not isinstance(function, BotFunction):
            raise PackageError(f"saved Function file is invalid: {source.name}")
        return function

    def export_function(
        self,
        function: BotFunction,
        output_path: str,
        package_name: str | None = None,
        description: str | None = None,
        profile_id: str | None = None,
        package_metadata: dict | None = None,
    ) -> Path:
        manifest, files = self._build_function_package(
            function, package_name, description, profile_id, package_metadata,
        )
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, indent=2, ensure_ascii=False),
            )
            archive.writestr(
                "checksums.json",
                json.dumps(manifest["checksums"], indent=2, ensure_ascii=False),
            )
            for archive_name, source in files.items():
                self._write_archive_member(archive, archive_name, source)
        return output

    def preview_function_package(
        self,
        function: BotFunction,
        package_name: str | None = None,
        description: str | None = None,
        profile_id: str | None = None,
        package_metadata: dict | None = None,
        package_assets: dict[str, Path] | None = None,
        publication_account: GitHubAccount | None = None,
        package_version: str | None = None,
    ) -> dict:
        manifest, files = self._build_function_package(
            function, package_name, description, profile_id, package_metadata,
            require_complete=True, package_assets=package_assets,
            publication_account=publication_account, package_version=package_version,
        )
        return self._package_preview(manifest, files)

    def prepare_function_directory(
        self,
        function: BotFunction,
        output_directory: str,
        package_name: str | None = None,
        description: str | None = None,
        profile_id: str | None = None,
        package_metadata: dict | None = None,
        expected_checksums: dict[str, str] | None = None,
        package_assets: dict[str, Path] | None = None,
    ) -> Path:
        manifest, files = self._build_function_package(
            function, package_name, description, profile_id, package_metadata,
            require_complete=True, package_assets=package_assets,
        )
        return self._write_ready_directory(
            output_directory, manifest, files, expected_checksums,
        )

    def _build_function_package(
        self,
        function: BotFunction,
        package_name: str | None,
        description: str | None,
        profile_id: str | None,
        package_metadata: dict | None,
        require_complete: bool = False,
        package_assets: dict[str, Path] | None = None,
        publication_account: GitHubAccount | None = None,
        package_version: str | None = None,
    ) -> tuple[dict, PackageFiles]:
        if not function or not function.saved:
            raise PackageError("only saved Functions can be exported")
        if not function.game_id:
            raise PackageError("Function game is required")
        functions = self._collect_functions(function)
        if require_complete and any(not item.saved for item in functions):
            raise PackageError("Function package references an unsaved Function dependency")
        if require_complete and not self._function_dependencies_resolved(functions):
            raise PackageError("Function package references a missing Function dependency")
        if (
            package_metadata is None and self._function_dependencies_resolved(functions)
            and not (publication_account is not None and any(item.package_origins for item in functions))
        ):
            package_metadata = self.preparation_service.function_metadata(
                function,
                functions,
            )
        if publication_account is not None:
            package_metadata = self._publication_metadata(
                package_metadata, functions, function.game_id,
                "helper" if function.kind == "helper" else "function",
                function.function_id, publication_account, package_version,
            )
        self._validate_package_metadata(package_metadata)

        files = self._function_files(functions)
        library_ids = {entry_id for item in functions for entry_id in item.collect_library_entries()}

        variable_file = self.variable_service.root / function.game_id / "variables.json"
        if variable_file.exists():
            files[f"content/variables/{function.game_id}/variables.json"] = variable_file
        if profile_id:
            profile_file = (
                self.variable_service.root
                / function.game_id
                / "profiles"
                / f"{profile_id}.json"
            )
            if profile_file.exists():
                files[
                    f"content/variables/{function.game_id}/profiles/{profile_id}.json"
                ] = profile_file
        self._add_package_assets(files, package_assets)

        game = self.game_service.get_game(function.game_id) or {}
        manifest = {
            "format": PACKAGE_FORMAT,
            "format_version": PACKAGE_VERSION,
            "package_type": "helper" if function.kind == "helper" else "function",
            "source_id": function.function_id,
            "package_name": package_name or function.name or function.function_id,
            "description": description if description is not None else function.description,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "botstudio_version": APP_VERSION,
            "game": {
                "id": function.game_id,
                "name": game.get("display_name", function.game_id),
                "android_package_name": game.get("package_name", ""),
            },
            "contents": {
                "functions": len(functions),
                "library_entries": len(library_ids),
                "variables": int(variable_file.exists()),
                "profiles": int(bool(profile_id)),
                "assets": sum(1 for name in files if "/library/" in name),
            },
        }
        if package_metadata is not None:
            manifest["package_metadata"] = dict(package_metadata)
        self._canonical_game_content(manifest, files, function.game_id)
        checksum_payload = self._checksums(files)
        manifest["checksums"] = checksum_payload
        self._validate_manifest(manifest)
        return manifest, files

    def export_event(
        self,
        event,
        output_path: str,
        package_name: str | None = None,
        description: str = "",
        profile_id: str | None = None,
        package_metadata: dict | None = None,
    ) -> Path:
        """Export one Event, its Tasks, Functions and recursive dependencies."""
        manifest, files, temporary_files = self._build_event_package(
            event, package_name, description, profile_id, package_metadata,
        )
        try:
            output = Path(output_path)
            self._write_package(output, manifest, files)
            return output
        finally:
            self._remove_temporary_files(temporary_files)

    def preview_event_package(
        self,
        event,
        package_name: str | None = None,
        description: str = "",
        profile_id: str | None = None,
        package_metadata: dict | None = None,
        package_assets: dict[str, Path] | None = None,
        publication_account: GitHubAccount | None = None,
        package_version: str | None = None,
    ) -> dict:
        manifest, files, temporary_files = self._build_event_package(
            event, package_name, description, profile_id, package_metadata,
            require_complete=True, package_assets=package_assets,
            publication_account=publication_account, package_version=package_version,
        )
        try:
            return self._package_preview(manifest, files)
        finally:
            self._remove_temporary_files(temporary_files)

    def prepare_event_directory(
        self,
        event,
        output_directory: str,
        package_name: str | None = None,
        description: str = "",
        profile_id: str | None = None,
        package_metadata: dict | None = None,
        expected_checksums: dict[str, str] | None = None,
        package_assets: dict[str, Path] | None = None,
    ) -> Path:
        manifest, files, temporary_files = self._build_event_package(
            event, package_name, description, profile_id, package_metadata,
            require_complete=True, package_assets=package_assets,
        )
        try:
            return self._write_ready_directory(
                output_directory, manifest, files, expected_checksums,
            )
        finally:
            self._remove_temporary_files(temporary_files)

    def _build_event_package(
        self,
        event,
        package_name: str | None,
        description: str,
        profile_id: str | None,
        package_metadata: dict | None,
        require_complete: bool = False,
        package_assets: dict[str, Path] | None = None,
        publication_account: GitHubAccount | None = None,
        package_version: str | None = None,
    ) -> tuple[dict, PackageFiles, list[Path]]:
        if not event or not event.game_id:
            raise PackageError("Event game is required")
        if not self._event_is_exportable(event):
            raise PackageError(
                "system Init/Interrupt Events and protected Tasks cannot be exported; "
                "export their Functions separately"
            )
        self._validate_package_metadata(package_metadata)
        functions = []
        all_event_functions_resolved = True
        for task in event.tasks:
            for function_id in task.function_ids:
                function = self._load_function(event.game_id, function_id)
                if function is not None:
                    functions.extend(self._collect_functions(function))
                else:
                    all_event_functions_resolved = False
        if require_complete and not all_event_functions_resolved:
            raise PackageError("Event package references a missing Function")
        unique_functions = {
            function.function_id: function
            for function in functions
        }
        included_functions = list(unique_functions.values())
        if require_complete and any(not item.saved for item in included_functions):
            raise PackageError("Event package references an unsaved Function")
        if require_complete and not self._function_dependencies_resolved(included_functions):
            raise PackageError("Event package references a missing Function dependency")
        if (
            package_metadata is None
            and all_event_functions_resolved
            and self._function_dependencies_resolved(included_functions)
            and not (
                publication_account is not None
                and any(item.package_origins for item in included_functions)
            )
        ):
            package_metadata = self.preparation_service.event_metadata(
                event,
                included_functions,
            )
        self._validate_package_metadata(package_metadata)
        if publication_account is not None:
            package_metadata = self._publication_metadata(
                package_metadata, included_functions, event.game_id, "event",
                event.event_id, publication_account, package_version,
            )
        files = self._function_files(included_functions)
        self._add_package_assets(files, package_assets)
        event_payload = event.to_dict()
        event_payload["profile_id"] = (
            profile_id if profile_id is not None else event.profile_id
        )
        if profile_id == "":
            for task in event_payload["tasks"]:
                task.update({
                    "last_started_at": None,
                    "last_finished_at": None,
                    "duration_samples_seconds": [],
                    "status": "idle",
                })
            event_payload.update({
                "last_started_at": None,
                "last_finished_at": None,
                "duration_samples_seconds": [],
                "last_executed": [],
                "next_due_at": None,
                "status": "idle",
            })
        event_path = self._write_temp_json(event_payload)
        files[f"content/scheduler/{event.game_id}/{event.event_id}.json"] = event_path
        self._add_variable_files(files, event.game_id, profile_id)
        manifest = self._manifest(
            event.game_id,
            package_type="event",
            package_name=package_name or event.name or event.event_id,
            description=description,
            function_count=len(unique_functions),
            files=files,
            profile_id=profile_id,
        )
        manifest["source_id"] = event.event_id
        if package_metadata is not None:
            manifest["package_metadata"] = dict(package_metadata)
        self._canonical_game_content(manifest, files, event.game_id)
        manifest["checksums"] = self._checksums(files)
        self._validate_manifest(manifest)
        return manifest, files, [event_path]

    def _canonical_game_id(self, game_id):
        game = self.game_service.get_game(game_id) or {}
        return game.get("catalog_game_id") or game_id

    def _canonical_game_content(self, manifest, files, local_game_id):
        game = self.game_service.get_game(local_game_id) or {}
        if game.get("catalog_game_name"):
            manifest["game"]["name"] = game["catalog_game_name"]
        canonical = self._canonical_game_id(local_game_id)
        if canonical == local_game_id:
            return
        from services.game_registry_service import rebind_game_json

        self._validate_path_component(canonical, "game")
        rebound = {}
        for name, source in files.items():
            parts = name.split("/")
            if len(parts) >= 4 and parts[1] in {"functions", "scheduler", "variables"}:
                if parts[2] != local_game_id:
                    raise PackageError("Package contains content from another Game.")
                parts[2] = canonical
            if name.endswith(".json") and len(parts) >= 4 and parts[1] in {
                "functions", "scheduler", "variables",
            }:
                raw = source if isinstance(source, bytes) else source.read_bytes()
                source = json.dumps(
                    rebind_game_json(json.loads(raw), local_game_id, canonical),
                    indent=2, ensure_ascii=False,
                ).encode("utf-8")
            rebound["/".join(parts)] = source
        files.clear()
        files.update(rebound)
        manifest["game"]["id"] = canonical

    def publication_sources(self, functions: list[BotFunction]) -> list[tuple[dict, Path]]:
        """Resolve declared origins against preserved, checksum-verified original bytes."""
        from services.managed_package_service import ManagedPackageService

        origins = {}
        for function in functions:
            for origin in function.package_origins:
                origins[(origin.publisher_key, origin.package_id, origin.game_id)] = origin
        result = []
        for identity, origin in origins.items():
            installation_id = hashlib.sha256(json.dumps(identity).encode("utf-8")).hexdigest()
            generations = self.function_root.parent / "packages" / installation_id / "generations"
            archives = list(generations.glob("*\\original.botstudio.zip"))
            downloads = ApplicationPaths.long_path(self.function_root.parent / "package-downloads")
            archives.extend(downloads.glob(f"*\\{installation_id}\\generations\\*\\original.botstudio.zip"))
            selected = None
            selected_archive = None
            for archive in archives:
                if archive.is_symlink() or archive.parent.is_symlink():
                    raise PackageError("preserved package source may not be a link")
                manifest = self.inspect_package(str(archive))
                details = manifest.get("package_metadata", {})
                function_path = f"content/functions/{origin.game_id}/{origin.function_id}.json"
                if (
                    ManagedPackageService._identity(manifest) == identity
                    and details["package_version"] == origin.package_version
                    and manifest["checksums"].get(function_path) == origin.source_sha256
                ):
                    selected = manifest
                    selected_archive = archive
                    break
            if selected is None or selected_archive is None:
                raise PackageError(
                    "preserved original package is required to retain its license and attribution; "
                    "install the original package in local package storage before exporting"
                )
            result.append((selected, selected_archive))
        return result

    def matches_publication_source(self, manifest: dict, source_id: str, package_type: str) -> bool:
        if manifest.get("package_type") != package_type:
            return False
        if manifest.get("source_id") == source_id:
            return True
        details = manifest["package_metadata"]
        area = "scheduler" if package_type == "event" else "functions"
        for path in (self.function_root.parent / "package-activations").glob("*.json"):
            receipt = read_json(path)
            if (
                receipt.get("source_game_id", receipt.get("game_id")) == manifest["game"]["id"]
                and receipt.get("package_id") == details["package_id"]
                and receipt.get("version") == details["package_version"]
                and receipt.get("publisher_key") == self.publisher_identity_key(details["publisher"])
                and (receipt.get("copied_identities") or {}).get(area, {}).get(manifest.get("source_id")) == source_id
            ):
                return True
        return False

    def _publication_metadata(
        self, base: dict | None, functions: list[BotFunction], game_id: str,
        package_type: str, source_id: str, account: GitHubAccount,
        package_version: str | None,
    ) -> dict:
        from services.managed_package_service import ManagedPackageService

        if not isinstance(account, GitHubAccount):
            raise PackageError("a freshly checked publication account is required")
        sources = [source[0] for source in self.publication_sources(functions)]
        matching = [
            source for source in sources
            if self.matches_publication_source(source, source_id, package_type)
            and source["game"]["id"] == self._canonical_game_id(game_id)
        ]
        if len(matching) > 1:
            raise PackageError("multiple source package channels match; select an unambiguous source")
        source = matching[0] if matching else None
        if source is not None:
            details = deepcopy(source["package_metadata"])
        elif base is not None:
            details = deepcopy(base)
        elif sources:
            prepared = self.preparation_service.license_service.metadata(
                account, package_type, game_id, source_id,
                max(function.revision for function in functions),
            )
            if prepared is None:
                raise PackageError("select your account's package license before publication")
            details = prepared
        else:
            unattributed = [
                function.name or function.function_id
                for function in functions if function.creator_account is None
            ]
            if unattributed:
                raise PackageError(
                    "Missing creator attribution for: " + ", ".join(unattributed)
                    + ". GitHub sign-in and the default package license do not attribute "
                    "existing Functions. Under GitHub account, use "
                    "'Select existing Functions to attribute...' for your own local "
                    "Functions, including referenced Helpers, then refresh the package preview. "
                    "Existing creators and imported content must not be reassigned."
                )
            creators = {function.creator_account for function in functions}
            if len(creators) > 1:
                raise PackageError(
                    "The package contains Functions from multiple creator accounts without "
                    "preserved source-package license metadata. A default account license "
                    "does not establish permission to relicense these Functions."
                )
            if creators:
                creator = next(iter(creators))
                raise PackageError(
                    f"No package license is selected for creator @{creator.login}. "
                    "The selected default license applies only to its GitHub account; "
                    "choose that creator account's package license before publication."
                )
            raise PackageError("The Event contains no Functions to prepare for publication.")
        self._validate_package_metadata(details)
        previous_publisher = details["publisher"]
        same_account = self.publisher_identity_key(previous_publisher) == f"github:{account.account_id}"
        revision = (
            max(function.revision for function in functions)
            if package_type == "event"
            else next(function.revision for function in functions if function.function_id == source_id)
        )
        proposed = (1, 0, revision)
        if source is not None and same_account:
            old_version = ManagedPackageService._version(details["package_version"])
            proposed = max(proposed, (old_version[0], old_version[1], old_version[2] + 1))
        version = package_version or ".".join(str(part) for part in proposed)
        parsed_version = ManagedPackageService._version(version)
        if source is not None and same_account and parsed_version <= old_version:
            raise PackageError("the publication version must be newer than the imported package")
        if not same_account:
            details["fork_of"] = {
                "publisher": deepcopy(previous_publisher),
                "package_id": details["package_id"],
                "package_version": details["package_version"],
            }
            details["original_author"] = deepcopy(
                details.get("original_author", previous_publisher)
            )
        details.update({
            "publisher": account.to_dict(),
            "author": f"@{account.login}",
            "modified_by": account.to_dict(),
            "package_version": version,
            "rights_confirmed": False,
        })
        notices = [details.get("third_party_notices", "")]
        for dependency in sources:
            if dependency is source:
                continue
            terms = dependency["package_metadata"]
            notice = (
                f"Included source: {self.publisher_identity_key(terms['publisher'])}/"
                f"{terms['package_id']} {terms['package_version']}\n"
                f"License: {terms['license_id']}\n{terms['license_text']}\n"
                f"{terms.get('third_party_notices', '')}"
            )
            if notice not in notices[0]:
                notices.append(notice)
        if any(notices):
            details["third_party_notices"] = "\n\n".join(item for item in notices if item)
        self._validate_package_metadata(details)
        return details

    @staticmethod
    def github_release_metadata(manifest: dict, package_path: str | None = None) -> dict:
        """Return release title/body/asset metadata without contacting GitHub."""

        details = manifest.get("package_metadata")
        PackageService._validate_package_metadata(details)
        if details is None:
            raise PackageError("package author and license metadata are required for publication")
        if details["license_id"].strip().upper() in ("UNSPECIFIED", "NOASSERTION", "NONE"):
            raise PackageError("a declared license is required for publication")
        if details.get("rights_confirmed") is not True:
            raise PackageError("publication requires the publisher's explicit rights confirmation")
        if "publisher" not in details:
            raise PackageError("publication requires a stable publisher identity")
        if "login" not in details["publisher"]:
            raise PackageError("publication requires a GitHub login, not a free display name")
        return {
            "title": manifest.get("package_name", "BotStudio Package"),
            "body": PackageService.manifest_markdown(manifest),
            "asset_name": Path(package_path).name if package_path else "package.botstudio.zip",
            "package_format": PACKAGE_FORMAT,
        }

    @staticmethod
    def manifest_markdown(manifest: dict) -> str:
        """Render declared terms before download without implying rights approval."""

        PackageService._validate_manifest(manifest)
        game = manifest.get("game", {})
        contents = manifest.get("contents", {})
        lines = [
            f"# {manifest.get('package_name', 'BotStudio Package')}",
            "",
            manifest.get("description", ""),
            "",
            f"**Game:** {game.get('name', game.get('id', ''))}",
            f"**Android package:** `{game.get('android_package_name', '')}`",
            f"**Package type:** {manifest.get('package_type', '')}",
            "",
            "## Contents",
            "",
            *[f"- **{key}:** {value}" for key, value in contents.items()],
        ]
        if (
            type(contents.get("functions")) is int
            and type(contents.get("assets")) is int
            and contents["functions"] > 0
            and contents["assets"] > 0
        ):
            lines.extend([
                "",
                "## Mixed code and assets: license review required",
                "",
                "This package contains both Function code and assets. Their existing "
                "licenses may differ or be incompatible; verify each included component "
                "before publishing. The selected package license does not replace "
                "third-party terms.",
            ])
        details = manifest.get("package_metadata")
        if details is None:
            lines.extend([
                "", "## License: UNSPECIFIED",
                "", "No permission to redistribute is inferred. Publication is blocked.",
            ])
        else:
            lines.extend([
                "", "## Author and license", "",
                f"**Package ID:** {details['package_id']}",
                f"**Version:** {details['package_version']}",
                f"**Author/publisher (declared):** {details['author']}",
                f"**License (declared):** {details['license_id']}", "",
                details["license_text"], "", "## Third-party notices", "",
                details.get("third_party_notices") or "None declared; this is not a rights verification.",
                "", "## Support", "",
                details.get("support") or "No support terms declared.",
                "", "Author and rights declarations are not independently verified by BotStudio.",
            ])
            if details.get("license_url"):
                lines.insert(
                    lines.index("## Third-party notices") - 1,
                    f"**Full license text:** {details['license_url']}",
                )
            for field, label in (
                ("publisher", "Publisher"),
                ("original_author", "Original author"),
                ("modified_by", "Modified by"),
            ):
                identity = details.get(field)
                if identity is not None:
                    lines.extend([
                        "",
                        f"**{label} (declared, unverified):** "
                        f"{'@' + identity['login'] if 'login' in identity else identity['display_name']} "
                        f"(`{PackageService.publisher_identity_key(identity)}`)",
                    ])
            if "publisher" not in details:
                lines.extend(["", "**Publisher identity:** UNSPECIFIED (legacy/local author label only)"])
            if "fork_of" in details:
                parent = details["fork_of"]
                lines.extend([
                    "", "**Fork of (declared, unverified):** "
                    f"{PackageService.publisher_identity_key(parent['publisher'])}/"
                    f"{parent['package_id']} at {parent['package_version']}",
                    "This is a separate alternative, not an automatic replacement of the original.",
                ])
        return "\n".join(lines)

    def inspect_package(self, package_path: str) -> dict:
        with zipfile.ZipFile(package_path, "r") as archive:
            try:
                manifest = json.loads(archive.read("manifest.json"))
                checksums = json.loads(archive.read("checksums.json"))
            except (KeyError, json.JSONDecodeError) as error:
                raise PackageError("package manifest or checksums are invalid") from error
            self._validate_manifest(manifest)
            names = archive.namelist()
            if len(names) != len({name.casefold() for name in names}):
                raise PackageError("package contains duplicate archive paths")
            content_files = set()
            for name in names:
                path = PurePosixPath(name)
                if (not name or "\\" in name or ":" in name or
                        path.is_absolute() or
                        any(not part or part in ("..", ".") or part.endswith((" ", "."))
                            for part in name.rstrip("/").split("/"))):
                    raise PackageError("package contains an unsafe path")
                if name.startswith("content/") and not name.endswith("/"):
                    content_files.add(name)
            if not isinstance(checksums, dict) or set(checksums) != content_files:
                raise PackageError("package checksums must cover exactly all content files")
            if not content_files:
                raise PackageError("package contains no content files")
            if "checksums" in manifest and manifest["checksums"] != checksums:
                raise PackageError("manifest and archive checksums disagree")
            self._verify_archive_checksums(archive, checksums)
            return manifest

    def import_package(
        self,
        package_path: str,
        target_root: str,
        replace: bool = False,
    ) -> dict:
        manifest = self.inspect_package(package_path)
        target = Path(target_root)
        content_root = target / "content"
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary) / "content"
            with zipfile.ZipFile(package_path, "r") as archive:
                for member in archive.namelist():
                    if not member.startswith("content/"):
                        continue
                    destination = (Path(temporary) / member).resolve()
                    if Path(temporary).resolve() not in destination.parents:
                        raise PackageError("package contains an unsafe path")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if member.endswith("/"):
                        continue
                    destination.write_bytes(archive.read(member))

            self._record_function_origins(staging, manifest)
            conflicts = []
            for source in staging.rglob("*"):
                if source.is_file():
                    destination = content_root / source.relative_to(staging)
                    if target.resolve() not in destination.resolve().parents:
                        raise PackageError("import destination escapes the target directory")
                    if destination.exists():
                        conflicts.append(str(destination))
            if conflicts and not replace:
                raise PackageError(
                    "package conflicts require explicit replace: "
                    + ", ".join(conflicts)
                )
            shutil.copytree(staging, content_root, dirs_exist_ok=True)
        return manifest

    @staticmethod
    def _record_function_origins(content_root: Path, manifest: dict) -> None:
        details = manifest.get("package_metadata")
        if not details or "publisher" not in details:
            return
        publisher_key = PackageService.publisher_identity_key(details["publisher"])
        for source in (content_root / "functions").rglob("*.json"):
            raw = source.read_bytes()
            try:
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("Function must be an object")
                relative = source.relative_to(content_root / "functions")
                if (
                    len(relative.parts) != 2
                    or data.get("game_id") != relative.parts[0]
                    or data.get("game_id") != manifest["game"].get("id")
                    or data.get("function_id") != source.stem
                ):
                    raise ValueError("Function identity does not match its package path/game")
                origins = FunctionPackageOrigin.from_list(data.get("package_origins", []))
                origin = FunctionPackageOrigin(
                    publisher_key=publisher_key,
                    package_id=details["package_id"],
                    package_version=details["package_version"],
                    game_id=data["game_id"],
                    function_id=data["function_id"],
                    revision=data.get("revision", 1),
                    source_sha256=hashlib.sha256(raw).hexdigest(),
                )
            except (ValueError, TypeError, KeyError) as error:
                raise PackageError(f"invalid packaged Function {source.name}: {error}") from error
            if origin not in origins:
                origins.append(origin)
            data["package_origins"] = [item.to_dict() for item in origins]
            source.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _validate_manifest(manifest: dict) -> None:
        if not isinstance(manifest, dict):
            raise PackageError("package manifest must be an object")
        if manifest.get("format") != PACKAGE_FORMAT:
            raise PackageError("unsupported BotStudio package format")
        if manifest.get("format_version") != PACKAGE_VERSION:
            raise PackageError("unsupported package version")
        for key in ("package_name", "description", "game", "contents"):
            if key not in manifest:
                raise PackageError(f"manifest field is missing: {key}")
        PackageService._validate_package_metadata(manifest.get("package_metadata"))

    @staticmethod
    def _validate_package_metadata(details: dict | None) -> None:
        if details is None:
            return
        if not isinstance(details, dict):
            raise PackageError("package metadata must be an object")
        for key in ("package_id", "package_version", "author", "license_id", "license_text"):
            value = details.get(key)
            if not isinstance(value, str) or not value.strip():
                raise PackageError(f"package metadata field is required: {key}")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", details["package_id"]):
            raise PackageError("package_id must be a stable identifier, not a path")
        for key in ("support", "third_party_notices", "license_url"):
            if key in details and not isinstance(details[key], str):
                raise PackageError(f"package metadata field must be text: {key}")
        if "license_url" in details:
            license_url = details["license_url"]
            license_id = details["license_id"]
            if (
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,127}", license_id)
                or not re.fullmatch(
                    r"https://spdx\.org/licenses/[A-Za-z0-9][A-Za-z0-9.-]{0,127}\.html",
                    license_url,
                )
                or license_url != f"https://spdx.org/licenses/{license_id}.html"
            ):
                raise PackageError("license_url must be the matching official SPDX license page")
        if "rights_confirmed" in details and type(details["rights_confirmed"]) is not bool:
            raise PackageError("rights_confirmed must be a boolean")
        for field in ("publisher", "original_author", "modified_by"):
            if field in details:
                PackageService.publisher_identity_key(details[field])
        if "fork_of" in details:
            parent = details["fork_of"]
            if not isinstance(parent, dict) or set(parent) != {"publisher", "package_id", "package_version"}:
                raise PackageError("fork_of requires publisher, package_id and package_version")
            parent_key = PackageService.publisher_identity_key(parent["publisher"])
            if not isinstance(parent["package_id"], str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", parent["package_id"],
            ):
                raise PackageError("fork_of package_id is invalid")
            if not isinstance(parent["package_version"], str) or not parent["package_version"].strip():
                raise PackageError("fork_of package_version is required")
            if details.get("publisher") and (
                parent_key == PackageService.publisher_identity_key(details["publisher"]) and
                parent["package_id"] == details["package_id"]
            ):
                raise PackageError("a fork must have a different package or publisher identity")

    @staticmethod
    def publisher_identity_key(identity: dict) -> str:
        """Return a stable declared account key, never authentication evidence."""

        if not isinstance(identity, dict) or set(identity) not in (
            {"provider", "account_id", "login"},
            {"provider", "account_id", "display_name"},
        ):
            raise PackageError("publisher identity requires provider, account_id and GitHub login")
        if identity["provider"] != "github":
            raise PackageError("publisher identity provider must currently be github")
        account_id = identity["account_id"]
        if not isinstance(account_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", account_id):
            raise PackageError("publisher account_id must be a canonical positive numeric string")
        if "login" in identity:
            login = identity["login"]
            if not isinstance(login, str) or not re.fullmatch(
                r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", login,
            ) or "--" in login:
                raise PackageError("publisher login must be a GitHub username")
        elif not isinstance(identity["display_name"], str) or not identity["display_name"].strip():
            raise PackageError("legacy publisher display_name is required")
        return f"github:{account_id}"

    @staticmethod
    def _checksums(files: PackageFiles) -> dict[str, str]:
        result = {}
        for archive_name, source in files.items():
            digest = hashlib.sha256()
            if isinstance(source, bytes):
                result[archive_name] = hashlib.sha256(source).hexdigest()
                continue
            with open(source, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            result[archive_name] = digest.hexdigest()
        return result

    @staticmethod
    def _add_package_assets(
        files: PackageFiles,
        package_assets: dict[str, Path] | None,
    ) -> None:
        if package_assets is None:
            return
        if not isinstance(package_assets, dict) or len(package_assets) > MAX_PACKAGE_IMAGES:
            raise PackageError(f"package image attachments cannot exceed {MAX_PACKAGE_IMAGES}")
        total_size = 0
        for archive_name, source in package_assets.items():
            path = PurePosixPath(archive_name) if isinstance(archive_name, str) else None
            if (
                path is None or len(path.parts) != 3
                or path.parts[:2] != ("content", "package-assets")
                or not re.fullmatch(r"image-[0-9]{2,3}\.(?:png|jpe?g|webp|gif|bmp)", path.name)
                or path.suffix.lower() not in PACKAGE_IMAGE_EXTENSIONS
                or "\\" in archive_name or ":" in archive_name
                or archive_name in files
                or archive_name.casefold() in {name.casefold() for name in files}
            ):
                raise PackageError("package image attachment has an unsafe or duplicate path")
            if not isinstance(source, Path) or source.is_symlink() or not source.is_file():
                raise PackageError("package image attachment must be an existing regular file")
            try:
                size = source.stat().st_size
            except OSError as error:
                raise PackageError("package image attachment could not be inspected") from error
            if size <= 0 or size > MAX_PACKAGE_IMAGE_BYTES:
                raise PackageError("package image attachment is empty or exceeds 20 MiB")
            total_size += size
            if total_size > MAX_PUBLISH_ARCHIVE_BYTES:
                raise PackageError("package image attachments exceed GitHub's 100 MiB file limit")
            files[archive_name] = source

    def _manifest(
        self,
        game_id: str,
        package_type: str,
        package_name: str,
        description: str,
        function_count: int,
        files: PackageFiles,
        profile_id: str | None,
    ) -> dict:
        game = self.game_service.get_game(game_id) or {}
        return {
            "format": PACKAGE_FORMAT,
            "format_version": PACKAGE_VERSION,
            "package_type": package_type,
            "package_name": package_name,
            "description": description,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "botstudio_version": APP_VERSION,
            "game": {
                "id": game_id,
                "name": game.get("display_name", game_id),
                "android_package_name": game.get("package_name", ""),
            },
            "contents": {
                "functions": function_count,
                "files": len(files),
                "profiles": int(bool(profile_id)),
                "assets": sum(1 for name in files if "/library/" in name),
            },
        }

    def _function_files(self, functions: list[BotFunction]) -> PackageFiles:
        files: PackageFiles = {}
        for function in functions:
            files[
                f"content/functions/{function.game_id}/{function.function_id}.json"
            ] = self.function_root / function.game_id / f"{function.function_id}.json"
        library_ids = sorted({entry_id for function in functions for entry_id in function.collect_library_entries()})
        for entry_id in library_ids:
            self._validate_path_component(entry_id, "Library")
            entry_root = self.library_service.root / entry_id
            if entry_root.is_symlink():
                raise PackageError(f"Library entries cannot be symbolic links: {entry_id}")
            if not entry_root.is_dir():
                raise PackageError(f"missing Library entry: {entry_id}")
            for path in sorted(entry_root.rglob("*")):
                if path.is_symlink():
                    raise PackageError(f"Library assets cannot be symbolic links: {entry_id}")
                if path.is_file():
                    files[f"content/library/{entry_id}/{path.relative_to(entry_root).as_posix()}"] = path
        overrides, removed = self._portable_library_files(files, lambda name: self._file_bytes(files[name]))
        for name in removed:
            del files[name]
        files.update(overrides)
        return files

    @staticmethod
    def _file_bytes(source: Path | bytes) -> bytes:
        return source if isinstance(source, bytes) else source.read_bytes()

    @staticmethod
    def _write_archive_member(archive, name: str, source: Path | bytes) -> None:
        if isinstance(source, bytes):
            archive.writestr(name, source)
        else:
            archive.write(source, name)

    @staticmethod
    def _portable_library_files(names, read) -> tuple[dict[str, bytes], set[str]]:
        """Preserve every sample while sharing byte-identical source images per entry."""
        names = set(names)
        overrides = {}
        removed = set()
        for name in sorted(names):
            if not name.startswith("content/library/") or not name.endswith("/entry.json"):
                continue
            root = name.rsplit("/", 1)[0]
            entry_id = root.split("/")[-1]
            try:
                entry = json.loads(read(name))
                if not isinstance(entry, dict):
                    raise ValueError("Library entry must be an object")

                def resolve(value):
                    if not isinstance(value, str) or not value:
                        raise ValueError("missing Library asset path")
                    parts = PureWindowsPath(value).parts
                    if ".." in parts:
                        raise ValueError("unsafe Library asset path")
                    indices = [index for index, part in enumerate(parts) if part == entry_id]
                    if indices:
                        relative = parts[indices[-1] + 1:]
                    elif len(parts) == 1:
                        relative = parts
                    else:
                        raise ValueError("Library asset belongs to another entry")
                    target = root + "/" + "/".join(relative)
                    if not relative or target not in names:
                        raise ValueError("missing Library asset")
                    return target

                samples = []
                for field in ("accepted_samples", "rejected_samples"):
                    values = entry.get(field, [])
                    if not isinstance(values, list) or any(not isinstance(sample, dict) for sample in values):
                        raise ValueError("invalid Library samples")
                    samples.extend(values)
                sources = sorted({resolve(sample["source"]) for sample in samples if sample.get("source")})
                groups: dict[str, list[str]] = {}
                replacements = {}
                for source in sources:
                    raw = read(source)
                    digest = hashlib.sha256(raw).hexdigest()
                    candidates = groups.setdefault(digest, [])
                    canonical = next((candidate for candidate in candidates if read(candidate) == raw), None)
                    if canonical is None:
                        candidates.append(source)
                    else:
                        replacements[source] = canonical
                        removed.add(source)
                pattern = entry.get("pattern", {})
                if not isinstance(pattern, dict):
                    raise ValueError("invalid Library pattern")
                if pattern.get("reference_path"):
                    target = resolve(pattern["reference_path"])
                    pattern["reference_path"] = replacements.get(target, target).removeprefix("content/library/")
                for sample in samples:
                    for key in ("source", "crop", "match"):
                        if sample.get(key):
                            target = resolve(sample[key])
                            sample[key] = replacements.get(target, target).removeprefix("content/library/")
                overrides[name] = json.dumps(entry, sort_keys=True, ensure_ascii=True).encode()
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                raise PackageError(f"invalid Library package entry {entry_id}: {error}") from error
        return overrides, removed

    def _add_variable_files(
        self,
        files: PackageFiles,
        game_id: str,
        profile_id: str | None,
    ) -> None:
        variable_file = self.variable_service.root / game_id / "variables.json"
        if variable_file.exists():
            files[f"content/variables/{game_id}/variables.json"] = variable_file
        if profile_id:
            profile_file = self.variable_service.root / game_id / "profiles" / f"{profile_id}.json"
            if profile_file.exists():
                files[f"content/variables/{game_id}/profiles/{profile_id}.json"] = profile_file

    @staticmethod
    def _write_temp_json(payload: dict) -> Path:
        descriptor, filename = tempfile.mkstemp(suffix=".json")
        os.close(descriptor)
        temporary = Path(filename)
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return temporary

    @staticmethod
    def _write_package(output: Path, manifest: dict, files: PackageFiles) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        checksums = PackageService._checksums(files)
        manifest["checksums"] = checksums
        try:
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
                archive.writestr("checksums.json", json.dumps(checksums, indent=2, ensure_ascii=False))
                for archive_name, source in files.items():
                    PackageService._write_archive_member(archive, archive_name, source)
        finally:
            for source in files.values():
                if isinstance(source, Path) and source.name.endswith(".json") and source.parent == Path(tempfile.gettempdir()):
                    source.unlink(missing_ok=True)

    @staticmethod
    def _remove_temporary_files(paths: list[Path]) -> None:
        for path in paths:
            path.unlink(missing_ok=True)

    @staticmethod
    def _package_preview(manifest: dict, files: PackageFiles) -> dict:
        return {
            "manifest": manifest,
            "checksums": dict(manifest["checksums"]),
            "files": tuple(sorted(files)),
            "content_fingerprint": PackageService._content_fingerprint(
                manifest, files, lambda name: PackageService._file_bytes(files[name]),
            ),
        }

    @staticmethod
    def _content_fingerprint(manifest, names, read):
        """Compare shareable content, not archive timestamps or version counters."""
        metadata = deepcopy(manifest.get("package_metadata") or {})
        for key in ("package_id", "package_version", "rights_confirmed"):
            metadata.pop(key, None)
        content = []
        names = set(names)

        def verified_read(name):
            raw = read(name)
            if hashlib.sha256(raw).hexdigest() != manifest["checksums"][name]:
                raise PackageError("package content changed during publication comparison")
            return raw

        overrides, removed = PackageService._portable_library_files(names, verified_read)
        for name in sorted(names - removed):
            raw = overrides[name] if name in overrides else verified_read(name)
            canonical_name = name
            if name.startswith("content/functions/") and name.endswith(".json"):
                data = json.loads(raw)
                for key in (
                    "created_at", "revision", "revised_at", "history", "package_origins",
                    "used", "used_by", "schedule", "enabled", "layout_positions",
                ):
                    data.pop(key, None)
                for dependency in data.get("helper_dependencies", []):
                    dependency.pop("tested_revision", None)
                raw = json.dumps(data, sort_keys=True, ensure_ascii=True).encode()
            elif name.startswith("content/scheduler/") and name.endswith(".json"):
                data = PackageService._portable_event_content(json.loads(raw))
                canonical_name = f"content/scheduler/{manifest['game']['id']}/event.json"
                raw = json.dumps(data, sort_keys=True, ensure_ascii=True).encode()
            elif name.endswith(".json"):
                raw = json.dumps(json.loads(raw), sort_keys=True, ensure_ascii=True).encode()
            content.append((canonical_name, hashlib.sha256(raw).hexdigest()))
        payload = {
            "package_type": manifest["package_type"], "game": manifest["game"],
            "package_name": manifest["package_name"], "description": manifest["description"],
            "metadata": metadata, "content": sorted(content),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()).hexdigest()

    def archive_content_fingerprint(self, archive_path) -> str:
        manifest = self.inspect_package(str(archive_path))
        try:
            with zipfile.ZipFile(archive_path) as archive:
                return self._content_fingerprint(
                    manifest, manifest["checksums"], archive.read,
                )
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as error:
            raise PackageError("invalid package content for publication comparison") from error

    def _write_ready_directory(
        self,
        output_directory: str,
        manifest: dict,
        files: PackageFiles,
        expected_checksums: dict[str, str] | None,
    ) -> Path:
        if expected_checksums is None or expected_checksums != manifest["checksums"]:
            raise PackageError(
                "package contents changed after preview; preview and confirm the package again"
            )
        if not files:
            raise PackageError("package contains no content files")
        self.github_release_metadata(manifest)

        output = Path(output_directory).expanduser()
        if output.exists():
            raise PackageError("the ready-to-zip package folder already exists")
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f".{output.name}.",
            dir=output.parent,
        ) as temporary:
            staging = Path(temporary)
            for archive_name, source in files.items():
                archive_path = PurePosixPath(archive_name)
                if (
                    not archive_name.startswith("content/")
                    or archive_path.is_absolute()
                    or any(part in ("", ".", "..") for part in archive_path.parts)
                    or "\\" in archive_name
                    or ":" in archive_name
                ):
                    raise PackageError("package contains an unsafe content path")
                destination = staging.joinpath(*archive_path.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(source, bytes):
                    destination.write_bytes(source)
                else:
                    shutil.copyfile(source, destination)

            copied_files = {
                name: staging.joinpath(*PurePosixPath(name).parts)
                for name in files
            }
            if self._checksums(copied_files) != expected_checksums:
                raise PackageError(
                    "package contents changed while preparing; preview the package again"
                )
            (staging / "checksums.json").write_text(
                json.dumps(expected_checksums, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            (staging / "manifest.json").write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            if output.exists():
                raise PackageError("the ready-to-zip package folder already exists")
            os.rename(staging, output)
        return output

    def create_ready_archive(
        self,
        ready_directory: str,
        archive_path: str,
        expected_checksums: dict[str, str],
    ) -> Path:
        """Create and verify a non-overwriting upload ZIP from a staged package."""
        source_root = Path(ready_directory).expanduser().resolve(strict=True)
        if not source_root.is_dir():
            raise PackageError("ready-to-zip package path must be a directory")
        output = Path(archive_path).expanduser()
        if not output.name.lower().endswith(".botstudio.zip"):
            raise PackageError("upload archive must use the .botstudio.zip extension")
        if output.exists():
            raise PackageError("the upload archive already exists")

        manifest_path = source_root / "manifest.json"
        checksums_path = source_root / "checksums.json"
        if (
            not manifest_path.is_file()
            or manifest_path.is_symlink()
            or not checksums_path.is_file()
            or checksums_path.is_symlink()
        ):
            raise PackageError("ready-to-zip package manifest or checksums are missing")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            checksums = json.loads(checksums_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise PackageError("ready-to-zip package metadata is invalid") from error
        self._validate_manifest(manifest)
        if not isinstance(checksums, dict) or checksums != manifest.get("checksums"):
            raise PackageError("ready-to-zip manifest and checksums disagree")
        if expected_checksums is None or checksums != expected_checksums:
            raise PackageError("ready-to-zip package changed since its preview")
        self.github_release_metadata(manifest)

        files = {}
        for source in source_root.rglob("*"):
            relative = source.relative_to(source_root)
            if source.is_symlink():
                raise PackageError("ready-to-zip package cannot contain symbolic links")
            if source.is_dir():
                if relative.parts[0] != "content":
                    raise PackageError("ready-to-zip package contains an unexpected directory")
                continue
            archive_name = relative.as_posix()
            if archive_name in ("manifest.json", "checksums.json"):
                continue
            archive_path_posix = PurePosixPath(archive_name)
            if (
                not archive_name.startswith("content/")
                or archive_path_posix.is_absolute()
                or "\\" in archive_name
                or ":" in archive_name
                or any(part in ("", ".", "..") or part.endswith((" ", "."))
                       for part in archive_path_posix.parts)
            ):
                raise PackageError("ready-to-zip package contains an unsafe path")
            files[archive_name] = source
        if not files or set(files) != set(checksums):
            raise PackageError("ready-to-zip checksums must cover exactly all content files")
        if self._checksums(files) != checksums:
            raise PackageError("ready-to-zip content does not match its SHA-256 checksums")

        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".botstudio-zip-",
            dir=output.parent,
        ) as temporary:
            temporary_archive = Path(temporary) / output.name
            with zipfile.ZipFile(
                temporary_archive,
                "x",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                archive.write(manifest_path, "manifest.json")
                archive.write(checksums_path, "checksums.json")
                for archive_name, source in sorted(files.items()):
                    archive.write(source, archive_name)
            if temporary_archive.stat().st_size > MAX_PUBLISH_ARCHIVE_BYTES:
                raise PackageError("upload archive exceeds GitHub's 100 MiB file limit")
            verified_manifest = self.inspect_package(str(temporary_archive))
            self.github_release_metadata(verified_manifest, str(temporary_archive))
            try:
                os.link(temporary_archive, output)
            except FileExistsError as error:
                raise PackageError("the upload archive already exists") from error
            except OSError as error:
                raise PackageError(
                    "could not atomically create the upload archive without overwriting a file"
                ) from error
        return output

    @staticmethod
    def _verify_archive_checksums(archive, checksums: dict) -> None:
        for name, expected in checksums.items():
            if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise PackageError(f"invalid package checksum: {name}")
            try:
                actual = hashlib.sha256(archive.read(name)).hexdigest()
            except KeyError as error:
                raise PackageError(f"package file is missing: {name}") from error
            if actual != expected:
                raise PackageError(f"package checksum mismatch: {name}")

    def _collect_functions(self, root: BotFunction) -> list[BotFunction]:
        collected = []
        pending = [root]
        seen = set()
        while pending:
            function = pending.pop(0)
            if function.function_id in seen:
                continue
            seen.add(function.function_id)
            collected.append(function)
            dependency_ids = []
            for dependency in function.helper_dependencies:
                dependency_id = (
                    dependency.get("function_id")
                    if isinstance(dependency, dict)
                    else getattr(dependency, "function_id", "")
                )
                if dependency_id:
                    dependency_ids.append(dependency_id)
            for block in function.walk_blocks():
                dependency_id = str(block.params.get("function_id") or "")
                if dependency_id:
                    dependency_ids.append(dependency_id)
            for dependency_id in dict.fromkeys(dependency_ids):
                dependency = self._load_function(root.game_id, dependency_id)
                if dependency is not None:
                    pending.append(dependency)
        return collected

    def _function_dependencies_resolved(self, functions: list[BotFunction]) -> bool:
        for function in functions:
            dependency_ids = []
            for dependency in function.helper_dependencies:
                dependency_id = (
                    dependency.get("function_id")
                    if isinstance(dependency, dict)
                    else getattr(dependency, "function_id", "")
                )
                if dependency_id:
                    dependency_ids.append(dependency_id)
            for block in function.walk_blocks():
                dependency_id = str(block.params.get("function_id") or "")
                if dependency_id:
                    dependency_ids.append(dependency_id)
            if any(
                self._load_function(function.game_id, dependency_id) is None
                for dependency_id in dict.fromkeys(dependency_ids)
            ):
                return False
        return True

    def _load_function(self, game_id: str, function_id: str) -> BotFunction | None:
        path = self.function_root / game_id / f"{function_id}.json"
        if not path.exists():
            return None
        try:
            return BotFunction.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None
