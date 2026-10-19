from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from ntpath import isreserved
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import tempfile
from uuid import NAMESPACE_URL, uuid4, uuid5

from PySide6.QtCore import QLockFile

from models.bot_function import BotFunction
from scheduler.models import Event, PROTECTED_TASK_KINDS
from services.application_paths import ApplicationPaths
from services.file_io import atomic_write_json, read_json
from services.function_service import FunctionService
from services.library_service import LibraryService
from services.managed_package_service import ManagedPackageService
from services.package_service import PackageError
from services.reinitialize_service import ReinitializeService
from services.variable_service import VariableService, VARIABLE_KINDS, VALUE_TYPES


class PackageActivationService:
    """Explicit, baseline-protected transfer; never installs foreign profile values."""

    def __init__(self, root=None, managed=None, execution_guard=None, logger=None):
        self.root = Path(root if root is not None else ApplicationPaths.discover().data_root).resolve()
        self.managed = managed
        self.execution_guard = execution_guard
        self.logger = logger
        self.journal_root = self.root / "data" / "package-activation"

    @contextmanager
    def _locked(self):
        self.journal_root.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(self.journal_root / ".activation.lock"))
        if not lock.tryLock(5000):
            raise PackageError("active package import is busy or cannot be locked")
        try:
            yield
        finally:
            lock.unlock()

    @staticmethod
    def _identifier(value):
        if (
            not isinstance(value, str) or not value or value in {".", ".."}
            or any(char in value for char in '/\\:<>\"|?*')
            or any(ord(char) < 32 for char in value)
            or value.endswith((" ", "."))
            or isreserved(value)
        ):
            raise PackageError("invalid package content identity")
        return value

    def _target(self, relative):
        if not isinstance(relative, str):
            raise PackageError("invalid active content path")
        path = PurePosixPath(relative)
        if (
            "\\" in relative or ":" in relative
            or path.is_absolute() or any(part in {".", "..", ""} for part in relative.split("/"))
            or any(isreserved(part) for part in path.parts)
            or len(path.parts) < 3 or path.parts[0] != "data"
            or path.parts[1] not in {"functions", "library", "variables", "scheduler", "package-activations"}
        ):
            raise PackageError("invalid active content path")
        target = self.root.joinpath(*path.parts)
        for candidate in (target, *target.parents):
            if candidate == self.root.parent:
                break
            if candidate.is_symlink() or candidate.is_junction():
                raise PackageError("active content paths may not contain links")
        if self.root not in target.resolve().parents:
            raise PackageError("active content path escapes application data")
        return target

    @staticmethod
    def _bytes(path):
        return path.read_bytes() if path.exists() else None

    @staticmethod
    def _digest(value):
        return hashlib.sha256(value).hexdigest() if value is not None else None

    def _local_context(self, game_id):
        games = read_json(self.root / "games" / "games.json")
        profiles = read_json(self.root / "profiles" / "profiles.json")
        if not isinstance(games, list) or not any(
            isinstance(game, dict) and game.get("id") == game_id for game in games
        ):
            raise PackageError("create the package's game locally before activating it")
        if not isinstance(profiles, list) or not all(isinstance(profile, dict) for profile in profiles):
            raise PackageError("invalid local profile store")
        return [
            {
                "id": str(profile.get("botstudio_profile_id") or profile.get("id") or ""),
                "name": str(profile.get("name") or profile.get("id") or ""),
            }
            for profile in profiles if profile.get("game") == game_id
        ]

    def local_game_id(self, source_game_id):
        games = read_json(self.root / "games" / "games.json")
        matches = [
            game["id"] for game in games
            if (game.get("catalog_game_id") or game["id"]) == source_game_id
        ]
        if len(matches) > 1:
            raise PackageError("Multiple local Games share the package's canonical Game.")
        return matches[0] if matches else source_game_id

    def context(self, installation_id):
        status = self.managed.status(installation_id)
        return {
            "profiles": self._local_context(status["game_id"]),
            "game_id": status["game_id"],
            "has_events": any((Path(status["working_directory"]) / "content" / "scheduler").rglob("*.json")),
        }

    def _receipt_path(self, installation_id):
        if not isinstance(installation_id, str) or len(installation_id) != 64 or any(
            char not in "0123456789abcdef" for char in installation_id
        ):
            raise PackageError("invalid package installation identity")
        return self._target(f"data/package-activations/{installation_id}.json")

    def _read_receipt(self, status):
        payload = self._bytes(self._receipt_path(status["installation_id"]))
        if payload is None:
            return None, None
        receipt = json.loads(payload)
        if (
            not isinstance(receipt, dict) or type(receipt.get("schema_version")) is not int
            or receipt["schema_version"] not in {1, 2}
            or any(receipt.get(key) != status[key] for key in ("installation_id", "game_id", "package_id"))
            or not isinstance(receipt.get("profile_id"), str)
            or not isinstance(receipt.get("active_files"), dict) or not receipt["active_files"]
            or receipt.get("schema_version") == 2 and receipt.get("publisher_key") != status["publisher_key"]
        ):
            raise PackageError("invalid active package receipt or different package identity")
        ManagedPackageService._version(receipt.get("version"))
        game_id = status["game_id"]
        paths = set()
        for relative, digest in receipt["active_files"].items():
            self._target(relative)
            parts = PurePosixPath(relative).parts
            if (
                parts[1] not in {"functions", "library", "scheduler", "variables"}
                or parts[1] != "library" and (len(parts) != 4 or parts[2] != game_id)
                or parts[1] == "variables" and parts[-1] != "variables.json"
                or parts[1] == "library" and len(parts) < 4
                or not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
                or relative.casefold() in paths
            ):
                raise PackageError("invalid active package receipt file")
            paths.add(relative.casefold())
        if receipt["schema_version"] == 2:
            self._definitions(game_id, receipt.get("variable_definitions"))
        return receipt, payload

    def _active_baseline(self, status, receipt):
        files, conflicts = {}, []
        registry_relative = f"data/variables/{status['game_id']}/variables.json"
        for relative, digest in receipt["active_files"].items():
            current = self._bytes(self._target(relative))
            files[relative] = current
            if relative == registry_relative and receipt["schema_version"] == 2:
                continue
            if self._digest(current) != digest:
                conflicts.append(relative)
        if receipt["schema_version"] == 2:
            registry = self._bytes(self._target(registry_relative))
            definitions = self._definitions(status["game_id"], json.loads(registry)) if registry is not None else []
            by_name = {entry["name"].casefold(): entry for entry in definitions}
            if any(
                by_name.get(entry["name"].casefold()) != entry for entry in receipt["variable_definitions"]
            ):
                conflicts.append(registry_relative)
        library_ids = {
            PurePosixPath(relative).parts[2]
            for relative in receipt["active_files"] if relative.startswith("data/library/")
        }
        for entry_id in sorted(library_ids):
            directory = self._target(f"data/library/{entry_id}")
            if directory.is_dir():
                for path in directory.rglob("*"):
                    if path.is_file():
                        relative = path.relative_to(self.root).as_posix()
                        self._target(relative)
                        if relative not in receipt["active_files"]:
                            conflicts.append(relative)
        return files, sorted(set(conflicts))

    def active_status(self, installation_id):
        """Read current active bytes, not the last displayed integrity result."""
        with self._locked():
            if (self.journal_root / "pending.json").exists():
                raise PackageError("package activation recovery is pending; restart Studio before continuing")
            status = self.managed.status(installation_id)
            receipt, _ = self._read_receipt(status)
            if receipt is None:
                return {"state": "not_active", "version": "", "update_available": False, "conflicts": []}
            _, conflicts = self._active_baseline(status, receipt)
            return {
                "state": "modified" if conflicts else "unchanged",
                "version": receipt["version"], "conflicts": conflicts,
                "update_available": (
                    not conflicts and not status["modified"]
                    and ManagedPackageService._version(status["version"]) > ManagedPackageService._version(receipt["version"])
                ),
            }

    def _definitions(self, game_id, payload):
        if not isinstance(payload, list):
            raise PackageError("variable definitions must be a list")
        result, names, identities = [], set(), set()
        for raw in payload:
            if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
                raise PackageError("invalid variable definition")
            if any(key in raw and type(raw[key]) is not bool for key in ("debug_visible", "writable_by_functions")):
                raise PackageError("variable flags must be boolean")
            if any(key in raw and not isinstance(raw[key], str) for key in ("variable_id", "kind", "value_type", "field_target")):
                raise PackageError("invalid variable definition types")
            name = VariableService._normalize_name(raw["name"])
            entry = VariableService._normalize_definition(game_id, raw, name)
            if (
                not name or name.casefold() in names
                or entry["variable_id"] in identities
                or entry["kind"] not in VARIABLE_KINDS or entry["value_type"] not in VALUE_TYPES
                or entry.get("field_target", "") not in {"", "event", "task"}
            ):
                raise PackageError(f"invalid or duplicate variable definition: {name}")
            VariableService._coerce_value(entry["value_type"], entry.get("initial_value"))
            names.add(name.casefold())
            identities.add(entry["variable_id"])
            result.append(entry)
        return result

    @staticmethod
    def _asset_path(value, entry_id, directory, destination):
        if not isinstance(value, str) or not value:
            raise PackageError(f"missing Library asset path: {entry_id}")
        parts = PureWindowsPath(value).parts
        if ".." in parts:
            raise PackageError(f"unsafe Library asset path: {entry_id}")
        matches = [index for index, part in enumerate(parts) if part == entry_id]
        if matches:
            relative = Path(*parts[matches[-1] + 1:])
        elif len(parts) == 1:
            relative = Path(parts[0])
        else:
            raise PackageError(f"Library asset does not belong to {entry_id}: {value}")
        if (
            not relative.parts or relative.drive or relative.is_absolute()
            or any(isreserved(part) for part in relative.parts)
            or not (directory / relative).is_file()
        ):
            raise PackageError(f"Library asset is missing: {entry_id}/{relative}")
        return str(destination / relative)

    def _stage(self, installation_id, profile_id, temporary, updating=False, copying=False):
        source_installation_id = installation_id
        status = self.managed.status(installation_id)
        if copying:
            status = dict(status, installation_id=hashlib.sha256(uuid4().bytes).hexdigest())
            installation_id = status["installation_id"]
        source_game_id = self._identifier(status["game_id"])
        game_id = self.local_game_id(source_game_id) if copying else source_game_id
        receipt_path = f"data/package-activations/{installation_id}.json"
        previous_receipt, receipt_bytes = self._read_receipt(status)
        active_before = {}
        if updating:
            if previous_receipt is None:
                raise PackageError("package is not active; activate it first")
            if status["modified"]:
                raise PackageError("local working-copy changes block active package updates")
            if ManagedPackageService._version(status["version"]) <= ManagedPackageService._version(previous_receipt["version"]):
                raise PackageError("active update version must be greater than the active version")
            active_before, conflicts = self._active_baseline(status, previous_receipt)
            if conflicts:
                raise PackageError("local active content changed; update blocked: " + ", ".join(conflicts))
            profile_id = previous_receipt["profile_id"]
        context_snapshot = {
            name: (self.root / name).read_bytes()
            for name in ("games/games.json", "profiles/profiles.json")
        }
        profiles = self._local_context(game_id)
        if copying and not profile_id:
            candidates = [profile for profile in profiles if profile["id"]]
            if len(candidates) == 1:
                profile_id = candidates[0]["id"]
        source = Path(status["working_directory"])
        source_snapshot = ManagedPackageService._snapshot(source)
        content = temporary / "content"
        shutil.copytree(source / "content", content)
        copied_snapshot = ManagedPackageService._snapshot(temporary)
        if (
            source_snapshot != ManagedPackageService._snapshot(source)
            or {name: digest for name, digest in source_snapshot.items() if name.startswith("content/")} != copied_snapshot
        ):
            raise PackageError("package working copy changed while preparing import")
        if game_id != source_game_id:
            from services.game_registry_service import rebind_game_json

            for area in ("functions", "scheduler", "variables"):
                directory = content / area / source_game_id
                if directory.exists():
                    directory.rename(content / area / game_id)
                for path in (content / area).rglob("*.json"):
                    atomic_write_json(
                        path, rebind_game_json(read_json(path), source_game_id, game_id),
                    )
        copied_identities = self._copy_content(content, game_id) if copying else None
        if any(path.name not in {"functions", "library", "scheduler", "variables", "package-assets"} for path in content.iterdir()):
            raise PackageError("unsupported active package content")
        # Non-content files remain part of consent, but are never activated.
        for area in ("functions", "library", "scheduler", "variables"):
            (content / area).mkdir(exist_ok=True)
        functions = {}
        for path in sorted((content / "functions").rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(content / "functions")
            data = read_json(path)
            if (
                len(relative.parts) != 2 or relative.parts[0] != game_id
                or path.suffix != ".json" or not isinstance(data, dict)
                or data.get("function_id") != path.stem or data.get("game_id") != game_id
                or data.get("saved") is not True
            ):
                raise PackageError(f"invalid or unsaved packaged Function: {relative}")
            self._identifier(path.stem)
            blocks = data.get("blocks") or []
            if not isinstance(blocks, list):
                raise PackageError("packaged Function blocks must be a list")
            pending_blocks = list(blocks)
            while pending_blocks:
                block = pending_blocks.pop()
                if not isinstance(block, dict) or not isinstance(block.get("block_id"), str) or not block["block_id"]:
                    raise PackageError("packaged Function blocks require explicit identities")
                for children in (block.get("branches") or {}).values():
                    pending_blocks.extend(child for child in children if isinstance(child, dict))
            function = BotFunction.from_dict(data)
            function.used_by = []
            functions[function.function_id] = function
        if not functions:
            raise PackageError("active packages must include saved Functions")
        library_ids = set()
        for directory in sorted((content / "library").iterdir()):
            self._identifier(directory.name)
            if not directory.is_dir():
                raise PackageError("Library entries must be directories")
            data = read_json(directory / "entry.json")
            if not isinstance(data, dict) or data.get("entry_id") != directory.name:
                raise PackageError("Library entry identity does not match package path")
            destination = self._target(f"data/library/{directory.name}")
            if destination.exists() and f"data/library/{directory.name}/entry.json" not in active_before:
                raise PackageError(f"active Library identity already exists: {directory.name}")
            data["used_by"] = []
            if not isinstance(data.get("pattern"), dict):
                raise PackageError("Library pattern is required")
            data["pattern"]["reference_path"] = self._asset_path(
                data["pattern"].get("reference_path") or "pattern_reference.png",
                directory.name, directory, destination,
            )
            for field in ("accepted_samples", "rejected_samples"):
                for sample in data.get(field, []):
                    if not isinstance(sample, dict):
                        raise PackageError("invalid Library sample")
                    for key in ("source", "crop", "match"):
                        if sample.get(key):
                            sample[key] = self._asset_path(sample[key], directory.name, directory, destination)
            atomic_write_json(directory / "entry.json", data)
            library_ids.add(directory.name)
        for function in functions.values():
            if set(function.collect_helper_ids()) - functions.keys():
                raise PackageError(f"package has missing Function dependencies: {function.name}")
            if set(function.collect_library_entries()) - library_ids:
                raise PackageError(f"package has missing Library dependencies: {function.name}")
            destination = self._target(f"data/functions/{game_id}/{function.function_id}.json")
            if destination.exists() and f"data/functions/{game_id}/{function.function_id}.json" not in active_before:
                raise PackageError(f"active Function identity already exists: {function.function_id}")
            if updating and destination.exists():
                previous_function = BotFunction.from_dict(json.loads(active_before[f"data/functions/{game_id}/{function.function_id}.json"]))
                if previous_function.kind != function.kind or previous_function.interface != function.interface:
                    raise PackageError(f"Function interface changes require migration: {function.name}")
            atomic_write_json(content / "functions" / game_id / f"{function.function_id}.json", function.to_dict())
        registry = self._target(f"data/variables/{game_id}/variables.json")
        before_registry = self._bytes(registry)
        existing = self._definitions(game_id, json.loads(before_registry)) if before_registry is not None else []
        local_definitions = deepcopy(existing)
        by_name = {entry["name"].casefold(): entry for entry in existing}
        incoming_file = content / "variables" / game_id / "variables.json"
        incoming = self._definitions(game_id, read_json(incoming_file)) if incoming_file.exists() else []
        for path in (content / "variables").rglob("*"):
            if not path.is_file() or path == incoming_file:
                continue
            relative = path.relative_to(content / "variables")
            if len(relative.parts) != 3 or relative.parts[:2] != (game_id, "profiles") or path.suffix != ".json":
                raise PackageError(f"unsupported variable content: {relative}")
        for entry in incoming:
            previous = by_name.get(entry["name"].casefold())
            if previous is not None:
                if entry != previous:
                    raise PackageError(f"variable definition conflicts with local data: {entry['name']}")
            else:
                if any(item["variable_id"] == entry["variable_id"] for item in existing):
                    raise PackageError("variable ID conflicts with another local name")
                existing.append(entry)
                by_name[entry["name"].casefold()] = entry
        atomic_write_json(incoming_file, existing)
        bus = ReinitializeService()
        variables = VariableService(root=str(content / "variables"), logger=self.logger, reinitialize_service=bus)
        library = LibraryService(root=content / "library", reinitialize_service=bus)
        service = FunctionService(
            root=str(content / "functions"), library_service=library,
            variable_service=variables, logger=self.logger, reinitialize_service=bus,
        )
        for function in functions.values():
            service.save(function)
        events = []
        for path in sorted((content / "scheduler").rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(content / "scheduler")
            data = read_json(path)
            if (
                len(relative.parts) != 2 or relative.parts[0] != game_id or path.suffix != ".json"
                or not isinstance(data, dict) or data.get("event_id") != path.stem or data.get("game_id") != game_id
            ):
                raise PackageError("Event identity does not match package path/game")
            self._identifier(path.stem)
            if not profile_id or not any(profile["id"] == profile_id for profile in profiles):
                if updating:
                    raise PackageError(
                        "active update requires the original local profile; "
                        "adding or changing its association requires an explicit migration"
                    )
                raise PackageError("choose a current local profile for the package game")
            tasks = data.get("tasks")
            if not isinstance(tasks, list) or any(
                not isinstance(task, dict) or not task.get("task_id") for task in tasks
            ):
                raise PackageError("packaged Event Tasks require explicit identities")
            task_ids = [self._identifier(task["task_id"]) for task in tasks]
            if len({task_id.casefold() for task_id in task_ids}) != len(task_ids):
                raise PackageError("packaged Event Task identities must be unique")
            event = Event.from_dict(data)
            if event.is_init or event.is_interrupt or any(
                task.protected or task.kind in PROTECTED_TASK_KINDS for task in event.tasks
            ):
                raise PackageError("system Init/Interrupt Events and protected Tasks cannot be imported")
            event_relative = f"data/scheduler/{game_id}/{event.event_id}.json"
            if self._target(event_relative).exists() and event_relative not in active_before:
                raise PackageError(f"active Event identity already exists: {event.event_id}")
            event.profile_id = profile_id
            event.enabled = False
            event.status = "idle"
            event.next_due_at = event.last_started_at = event.last_finished_at = None
            event.duration_samples_seconds, event.last_executed = [], []
            event.used, event.used_by = [], []
            for task in event.tasks:
                self._identifier(task.task_id)
                if set(task.function_ids) - functions.keys():
                    raise PackageError(f"Event Task has missing Function dependencies: {task.name}")
                task.status = "idle"
                task.last_started_at = task.last_finished_at = None
                task.duration_samples_seconds = []
            if event.variable_name.strip():
                definition = variables.get_definition(game_id, event.variable_name)
                if definition and (
                    definition["kind"] != "live" or definition["value_type"] not in {"any", "datetime"}
                ):
                    raise PackageError(f"Event output conflicts with variable definition: {event.variable_name}")
                if not definition:
                    variables.ensure_global(game_id, event.variable_name, description=f"Event Output: {event.name}")
            atomic_write_json(path, event.to_dict())
            events.append(event)
        definitions = variables.list_globals(game_id)
        imported_names = {entry["name"].casefold() for entry in existing}
        for definition in definitions:
            if definition["name"].casefold() not in imported_names:
                definition["variable_id"] = str(uuid5(
                    NAMESPACE_URL, f"botstudio:{game_id}:{definition['name'].casefold()}",
                ))
        if definitions:
            atomic_write_json(incoming_file, definitions)
            by_name = {entry["name"].casefold(): entry for entry in definitions}
            for path in sorted((content / "functions" / game_id).glob("*.json")):
                function = BotFunction.from_dict(read_json(path))
                for block in function.walk_blocks():
                    name = str(block.params.get("variable_name") or "").strip().lstrip("$")
                    if block.block_type == "configure_field" and name.casefold() in by_name:
                        block.params["variable_id"] = by_name[name.casefold()]["variable_id"]
                atomic_write_json(path, function.to_dict())
        final_definitions = {entry["name"].casefold(): entry for entry in definitions}
        for entry in local_definitions:
            if final_definitions.get(entry["name"].casefold()) != entry:
                raise PackageError(f"Function fields conflict with local variable definition: {entry['name']}")
        writes = {}
        for area in ("library", "functions", "scheduler"):
            for path in sorted((content / area).rglob("*")):
                if path.is_file():
                    relative = f"data/{area}/{path.relative_to(content / area).as_posix()}"
                    if self._target(relative).exists() and relative not in active_before:
                        raise PackageError(f"active content already exists: {relative}")
                    writes[relative] = path.read_bytes()
        final_registry = incoming_file.read_bytes()
        if existing or before_registry is not None or variables.list_globals(game_id):
            if final_registry != before_registry:
                writes[f"data/variables/{game_id}/variables.json"] = final_registry
        registry_relative = f"data/variables/{game_id}/variables.json"
        removed = set(active_before) - {registry_relative} - writes.keys()
        if removed:
            raise PackageError("active update would remove package content; removal requires migration: " + ", ".join(sorted(removed)))
        before = {
            relative: before_registry if relative == registry_relative else active_before.get(relative)
            for relative in writes
        }
        if self._bytes(registry) != before_registry or any(
            (self.root / name).read_bytes() != value for name, value in context_snapshot.items()
        ):
            raise PackageError("local definitions or profiles changed while preparing activation")
        if any(self._bytes(self._target(relative)) != old for relative, old in before.items()):
            raise PackageError("local content changed while preparing activation")
        if updating:
            _, conflicts = self._active_baseline(status, previous_receipt)
            if conflicts:
                raise PackageError("local active content changed while preparing update: " + ", ".join(conflicts))
        summary = {
            "installation_id": installation_id, "game_id": game_id,
            "source_game_id": source_game_id,
            "profile_id": profile_id, "functions": len(functions),
            "library_entries": len(library_ids), "events": len(events),
            "files": len(writes) + 1, "modified": status["modified"],
            "version": status["version"], "package_id": status["package_id"],
        }
        if updating:
            summary["operation"] = "update"
            summary["previous_version"] = previous_receipt["version"]
        if not updating and self._target(receipt_path).exists():
            raise PackageError("package already activated; active replacements require a separate update workflow")
        definition_names = {entry["name"].casefold() for entry in incoming}
        for function in functions.values():
            for block in function.walk_blocks():
                if block.block_type == "configure_field":
                    definition_names.add(VariableService._normalize_name(block.params.get("variable_name", "")).casefold())
        definition_names.update(VariableService._normalize_name(event.variable_name).casefold() for event in events)
        if updating and previous_receipt["schema_version"] == 2:
            definition_names.update(entry["name"].casefold() for entry in previous_receipt["variable_definitions"])
        active_files = {name: self._digest(value) for name, value in writes.items()}
        # The registry is shared: own definitions are checked independently of unrelated local additions.
        if definitions:
            active_files[registry_relative] = self._digest(final_registry)
        receipt = {
            **summary,
            "schema_version": 2, "publisher_key": status["publisher_key"],
            "working_snapshot": source_snapshot,
            "active_files": active_files,
            "variable_definitions": [entry for entry in definitions if entry["name"].casefold() in definition_names],
        }
        if copying:
            receipt["source_installation_id"] = source_installation_id
            receipt["source_archive"] = status["original_archive"]
            receipt["copied_identities"] = copied_identities
        writes[receipt_path] = json.dumps(receipt, indent=2, sort_keys=True).encode("utf-8")
        before[receipt_path] = receipt_bytes if updating else None
        if self._bytes(self._receipt_path(installation_id)) != receipt_bytes:
            raise PackageError("active package receipt changed while preparing activation")
        consent = {
            "summary": summary, "working": source_snapshot,
            "writes": {name: self._digest(value) for name, value in writes.items()},
            "before": {name: self._digest(value) for name, value in before.items()},
            "context": {name: self._digest(value) for name, value in context_snapshot.items()},
        }
        summary["plan_sha256"] = hashlib.sha256(json.dumps(consent, sort_keys=True).encode()).hexdigest()
        return summary, writes, before

    def prepare_update(self, installation_id):
        with self._locked():
            if (self.journal_root / "pending.json").exists():
                raise PackageError("package activation recovery is pending; restart Studio before continuing")
            with tempfile.TemporaryDirectory(prefix="stage-", dir=self.journal_root) as temporary:
                return self._stage(installation_id, "", Path(temporary), updating=True)[0]

    def import_copy(self, installation_id, profile_id=""):
        """One user download creates independent, non-overwriting active content."""
        if self.execution_guard is None:
            raise PackageError("Runtime exclusion guard is required for active imports")
        with self.execution_guard(), self._locked():
            self._recover()
            with tempfile.TemporaryDirectory(prefix="copy-", dir=self.journal_root) as temporary:
                summary, writes, before = self._stage(
                    installation_id, profile_id, Path(temporary), copying=True,
                )
                self._commit(writes, before)
                return summary

    def _copy_content(self, content, game_id):
        function_root = content / "functions" / game_id
        library_root = content / "library"
        event_root = content / "scheduler" / game_id
        function_files = sorted(function_root.glob("*.json"))
        library_dirs = sorted(path for path in library_root.iterdir() if path.is_dir()) if library_root.exists() else []
        event_files = sorted(event_root.glob("*.json"))
        function_ids = {
            path.stem: str(uuid4()) if self._target(f"data/functions/{game_id}/{path.name}").exists() else path.stem
            for path in function_files
        }
        library_ids = {
            path.name: str(uuid4()) if self._target(f"data/library/{path.name}").exists() else path.name
            for path in library_dirs
        }
        event_ids = {
            path.stem: str(uuid4()) if self._target(f"data/scheduler/{game_id}/{path.name}").exists() else path.stem
            for path in event_files
        }

        def names_in(area):
            directory = self.root / "data" / area / game_id
            return {
                str(read_json(path).get("name", "")).casefold()
                for path in directory.glob("*.json")
            }

        function_names = names_in("functions")
        event_names = names_in("scheduler")

        def unique_name(value, names):
            name = str(value).strip()
            if not name:
                raise PackageError("imported content requires a display name")
            candidate, number = name, 2
            while candidate.casefold() in names:
                candidate = f"{name} {number}"
                number += 1
            names.add(candidate.casefold())
            return candidate

        for path in function_files:
            data = read_json(path)
            if not isinstance(data, dict) or data.get("saved") is not True:
                raise PackageError("active packages require saved Functions")
            blocks = data.get("blocks") or []
            if not isinstance(blocks, list):
                raise PackageError("packaged Function blocks must be a list")
            pending = list(blocks)
            while pending:
                block = pending.pop()
                if not isinstance(block, dict) or not isinstance(block.get("block_id"), str) or not block["block_id"]:
                    raise PackageError("packaged Function blocks require explicit identities")
                for children in (block.get("branches") or {}).values():
                    pending.extend(child for child in children if isinstance(child, dict))
            function = BotFunction.from_dict(data)
            if function.function_id != path.stem or function.game_id != game_id:
                raise PackageError("Function identity does not match package path/game")
            function.function_id = function_ids[path.stem]
            function.name = unique_name(function.name, function_names)
            function.used, function.used_by = [], []
            for block in function.walk_blocks():
                for key, identities in (("function_id", function_ids), ("library_entry_id", library_ids)):
                    if block.params.get(key) in identities:
                        block.params[key] = identities[block.params[key]]
            for dependency in function.helper_dependencies:
                if dependency.function_id in function_ids:
                    dependency.function_id = function_ids[dependency.function_id]
            function.library_entries = function.collect_library_entries()
            atomic_write_json(path.with_name(function.function_id + ".json"), function.to_dict())
            if path.stem != function.function_id:
                path.unlink()

        for directory in library_dirs:
            data = read_json(directory / "entry.json")
            if not isinstance(data, dict) or data.get("entry_id") != directory.name:
                raise PackageError("Library entry identity does not match package path")
            new_id = library_ids[directory.name]
            data["entry_id"], data["used_by"] = new_id, []

            def relocated(value):
                old_path = self._asset_path(value, directory.name, directory, directory)
                return str(Path(new_id) / Path(old_path).relative_to(directory))

            pattern = data.get("pattern")
            if not isinstance(pattern, dict):
                raise PackageError("Library pattern is required")
            pattern["reference_path"] = relocated(pattern.get("reference_path") or "pattern_reference.png")
            for field in ("accepted_samples", "rejected_samples"):
                for sample in data.get(field, []):
                    for key in ("source", "crop", "match"):
                        if sample.get(key):
                            sample[key] = relocated(sample[key])
            atomic_write_json(directory / "entry.json", data)
            if new_id != directory.name:
                directory.rename(directory.with_name(new_id))

        for path in event_files:
            data = read_json(path)
            if not isinstance(data, dict) or data.get("event_id") != path.stem or data.get("game_id") != game_id:
                raise PackageError("Event identity does not match package path/game")
            tasks = data.get("tasks")
            if not isinstance(tasks, list) or any(
                not isinstance(task, dict) or not task.get("task_id") for task in tasks
            ):
                raise PackageError("packaged Event Tasks require explicit identities")
            ids = [self._identifier(task["task_id"]) for task in tasks]
            if len({value.casefold() for value in ids}) != len(ids):
                raise PackageError("packaged Event Task identities must be unique")
            event = Event.from_dict(data)
            event.event_id = event_ids[path.stem]
            event.name = unique_name(event.name, event_names)
            for task in event.tasks:
                task.task_id = str(uuid4())
                task.function_ids = [function_ids.get(value, value) for value in task.function_ids]
            atomic_write_json(path.with_name(event.event_id + ".json"), event.to_dict())
            if path.stem != event.event_id:
                path.unlink()
        return {"functions": function_ids, "scheduler": event_ids}

    def update(self, installation_id, expected_plan):
        return self.activate(installation_id, "", expected_plan, updating=True)

    def prepare(self, installation_id, profile_id=""):
        with self._locked():
            if (self.journal_root / "pending.json").exists():
                raise PackageError("package activation recovery is pending; restart Studio before continuing")
            with tempfile.TemporaryDirectory(prefix="stage-", dir=self.journal_root) as temporary:
                return self._stage(installation_id, profile_id, Path(temporary))[0]

    def activate(self, installation_id, profile_id, expected_plan, *, updating=False):
        if self.execution_guard is None:
            raise PackageError("Runtime exclusion guard is required for active imports")
        with self.execution_guard(), self._locked():
            self._recover()
            with tempfile.TemporaryDirectory(prefix="stage-", dir=self.journal_root) as temporary:
                summary, writes, before = self._stage(installation_id, profile_id, Path(temporary), updating=updating)
                if summary["plan_sha256"] != expected_plan:
                    raise PackageError("package or local data changed since activation preview; prepare it again")
                self._commit(writes, before)
                return summary

    def _commit(self, writes, before):
        journal = {
            "schema_version": 1, "committed": False, "started": 0,
            "created_directories": [],
            "files": [
                {
                    "path": relative, "before": old.hex() if old is not None else None,
                    "after": self._digest(writes[relative]),
                } for relative, old in before.items()
            ],
        }
        journal_file = self.journal_root / "pending.json"
        atomic_write_json(journal_file, journal)
        try:
            for index, (relative, value) in enumerate(writes.items()):
                target = self._target(relative)
                if self._bytes(target) != before[relative]:
                    raise PackageError(f"local data changed during activation: {relative}")
                missing = []
                directory = target.parent
                while not directory.exists():
                    missing.append(directory.relative_to(self.root).as_posix())
                    directory = directory.parent
                journal["created_directories"].extend(reversed(missing))
                journal["started"] = index + 1
                atomic_write_json(journal_file, journal)
                target.parent.mkdir(parents=True, exist_ok=True)
                if before[relative] is None:
                    replacement = self.journal_root / "new.tmp"
                    with replacement.open("wb") as handle:
                        handle.write(value)
                        handle.flush()
                        os.fsync(handle.fileno())
                    # Publish complete bytes exclusively; never overwrite a competing file.
                    try:
                        os.link(replacement, target)
                    except FileExistsError:
                        journal["started"] = index
                        atomic_write_json(journal_file, journal)
                        raise
                    replacement.unlink()
                else:
                    replacement = self.journal_root / "registry.tmp"
                    replacement.write_bytes(value)
                    os.replace(replacement, target)
            journal["committed"] = True
            atomic_write_json(journal_file, journal)
        except Exception:
            self._recover()
            raise
        try:
            self._recover()
        except (OSError, PackageError) as error:
            raise PackageError(
                "Package content was activated, but recovery cleanup failed; restart Studio before continuing"
            ) from error

    def recover(self):
        if not (self.journal_root / "pending.json").exists():
            return
        with self._locked():
            self._recover()

    def _recover(self):
        journal_file = self.journal_root / "pending.json"
        if not journal_file.exists():
            return
        journal = read_json(journal_file)
        if (
            not isinstance(journal, dict) or journal.get("schema_version") != 1
            or type(journal.get("committed")) is not bool or not isinstance(journal.get("files"), list)
        ):
            raise PackageError("invalid package activation recovery journal")
        paths = set()
        for entry in journal["files"]:
            if (
                not isinstance(entry, dict) or not isinstance(entry.get("path"), str)
                or "before" not in entry or not isinstance(entry.get("after"), str)
                or len(entry["after"]) != 64 or any(char not in "0123456789abcdef" for char in entry["after"])
                or entry["before"] is not None and not isinstance(entry["before"], str)
            ):
                raise PackageError("invalid package activation recovery entry")
            self._target(entry["path"])
            if entry["path"].casefold() in paths:
                raise PackageError("duplicate package activation recovery path")
            paths.add(entry["path"].casefold())
            if entry["before"] is not None:
                try:
                    bytes.fromhex(entry["before"])
                except ValueError as error:
                    raise PackageError("invalid package activation recovery bytes") from error
        started = journal.get("started", len(journal["files"]))
        if type(started) is not int or not 0 <= started <= len(journal["files"]):
            raise PackageError("invalid activation recovery progress")
        directories = journal.get("created_directories", [])
        if not isinstance(directories, list) or not all(isinstance(relative, str) for relative in directories):
            raise PackageError("invalid activation recovery directories")
        directory_targets = [self._target(f"{relative}/.activation-directory").parent for relative in directories]
        if journal.get("committed") is not True:
            recovery = []
            for entry in reversed(journal["files"][:started]):
                target = self._target(entry["path"])
                before = bytes.fromhex(entry["before"]) if entry["before"] is not None else None
                current = self._bytes(target)
                if current == before:
                    continue
                if self._digest(current) != entry["after"]:
                    raise PackageError(f"activation recovery would overwrite changed local data: {entry['path']}")
                recovery.append((target, before))
            for target, before in recovery:
                if before is None:
                    target.unlink()
                else:
                    replacement = self.journal_root / "restore.tmp"
                    replacement.write_bytes(before)
                    os.replace(replacement, target)
            for directory in reversed(directory_targets):
                if directory.exists() and not any(directory.iterdir()):
                    directory.rmdir()
        for name in ("new.tmp", "registry.tmp", "restore.tmp"):
            (self.journal_root / name).unlink(missing_ok=True)
        journal_file.unlink()
