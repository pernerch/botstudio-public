import json
from datetime import datetime
from functools import wraps
from pathlib import Path
from threading import RLock
from uuid import NAMESPACE_URL, uuid4, uuid5

from services.file_io import atomic_write_json, read_json
from services.logging_service import get_logger
from services.reinitialize_service import get_default_reinitialize_service


VARIABLE_KIND_CONFIG = "config"
VARIABLE_KIND_PROFILE = "profile"
VARIABLE_KIND_LIVE = "live"
VARIABLE_KINDS = (
    VARIABLE_KIND_CONFIG,
    VARIABLE_KIND_PROFILE,
    VARIABLE_KIND_LIVE,
)

VALUE_TYPE_TEXT = "text"
VALUE_TYPE_NUMBER = "number"
VALUE_TYPE_BOOLEAN = "boolean"
VALUE_TYPE_DATETIME = "datetime"
VALUE_TYPE_DURATION = "duration"
VALUE_TYPE_COLLECTION = "collection"
VALUE_TYPE_ANY = "any"
VALUE_TYPES = (
    VALUE_TYPE_ANY,
    VALUE_TYPE_TEXT,
    VALUE_TYPE_NUMBER,
    VALUE_TYPE_BOOLEAN,
    VALUE_TYPE_DATETIME,
    VALUE_TYPE_DURATION,
    VALUE_TYPE_COLLECTION,
)


class VariableNameConflictError(Exception):
    """Raised when a variable name already exists for the game."""


class VariableValueError(ValueError):
    """Raised when a value does not match its variable definition."""


class VariableDefinitionConflictError(ValueError):
    """Raised when one name is declared with incompatible contracts."""


def _profile_synchronized(method):
    @wraps(method)
    def wrapped(self, game_id, profile_id, *args, **kwargs):
        with self._profile_lock(game_id, profile_id):
            return method(self, game_id, profile_id, *args, **kwargs)

    return wrapped


class VariableService:
    """Per-game registry and profile-bound runtime values (DEC-092).

    Users create functional variable names once per game. Runtime values are
    stored separately per (game_id, profile_id, variable_name), so the same
    function can run for multiple profiles without sharing values.
    """

    _lock_guard = RLock()
    _profile_locks = {}

    def __init__(self, root: str = "data/variables", logger=None, reinitialize_service=None):
        self.root = Path(root)
        self.logger = logger or get_logger("function")
        self.reinitialize_service = (
            reinitialize_service or get_default_reinitialize_service()
        )

    def _file(self, game_id: str) -> Path:
        game_root = self.root / game_id
        game_root.mkdir(parents=True, exist_ok=True)
        return game_root / "variables.json"

    def _profile_file(
        self,
        game_id: str,
        profile_id: str,
    ) -> Path:

        if not profile_id:
            raise ValueError("profile_id is required")

        profile_root = self.root / game_id / "profiles"
        profile_root.mkdir(parents=True, exist_ok=True)
        return profile_root / f"{profile_id}.json"

    def _profile_lock(self, game_id: str, profile_id: str):
        key = str(self._profile_file(game_id, profile_id).resolve()).casefold()
        with self._lock_guard:
            return self._profile_locks.setdefault(key, RLock())

    def list_globals(self, game_id: str) -> list:
        path = self._file(game_id)
        if not path.exists():
            return []

        try:
            payload = read_json(path)
        except (json.JSONDecodeError, OSError) as ex:
            self.logger.error(
                "Variable Registry Error",
                error=repr(ex),
                source="VariableService.list_globals",
                next_action="inspect variables.json for the game",
            )
            return []

        normalized = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "")).strip()
            if name.startswith("global:"):
                name = name[7:]
            if name.startswith("local:"):
                name = name[6:]
            if not name:
                continue
            normalized_entry = self._normalize_definition(game_id, entry, name)
            normalized.append(normalized_entry)
        return normalized

    @staticmethod
    def _normalize_definition(game_id: str, entry: dict, name: str) -> dict:
        normalized = dict(entry)
        normalized["name"] = name
        if not normalized.get("variable_id"):
            normalized["variable_id"] = str(
                uuid5(NAMESPACE_URL, f"botstudio:{game_id}:{name.casefold()}")
            )
        normalized.setdefault("schema_version", 1)
        normalized.setdefault("kind", VARIABLE_KIND_LIVE)
        normalized.setdefault("value_type", VALUE_TYPE_ANY)
        normalized.setdefault("writable_by_functions", True)
        return normalized

    def get_definition(self, game_id: str, name: str) -> dict | None:
        normalized_name = self._normalize_name(name)
        return next(
            (
                entry
                for entry in self.list_globals(game_id)
                if entry["name"].casefold() == normalized_name.casefold()
            ),
            None,
        )

    def _read_profile_payload(self, game_id: str, profile_id: str) -> dict:
        path = self._profile_file(game_id, profile_id)
        if not path.exists():
            return self._empty_profile_payload()

        try:
            payload = read_json(path)
        except (json.JSONDecodeError, OSError) as ex:
            self.logger.error(
                "Profile Variable Store Error",
                error=repr(ex),
                source="VariableService._read_profile_payload",
                game=game_id,
                profile=profile_id,
                next_action="inspect profile variable file",
            )
            return self._empty_profile_payload()

        return {
            "values": dict(payload.get("values", {})),
            "freshness": dict(payload.get("freshness", {})),
            "event_fields": dict(payload.get("event_fields", {})),
            "event_freshness": dict(payload.get("event_freshness", {})),
            "task_fields": dict(payload.get("task_fields", {})),
            "task_freshness": dict(payload.get("task_freshness", {})),
            "unconfirmed": dict(payload.get("unconfirmed", {})),
        }

    @staticmethod
    def _empty_profile_payload() -> dict:
        return {
            "values": {},
            "freshness": {},
            "event_fields": {},
            "event_freshness": {},
            "task_fields": {},
            "task_freshness": {},
            "unconfirmed": {},
        }

    def _write_profile_payload(
        self,
        game_id: str,
        profile_id: str,
        payload: dict,
    ) -> None:
        stored = {
            "game_id": game_id,
            "profile_id": profile_id,
            **self._empty_profile_payload(),
            **payload,
        }
        path = self._profile_file(game_id, profile_id)
        atomic_write_json(path, stored, indent=2)

    @_profile_synchronized
    def list_profile_values(
        self,
        game_id: str,
        profile_id: str,
    ) -> dict:

        return self._read_profile_payload(game_id, profile_id)["values"]

    @_profile_synchronized
    def initialize_profile_values(
        self,
        game_id: str,
        profile_id: str,
        reset: bool = True,
        invalidate_live: bool = False,
    ) -> dict:

        payload = self._read_profile_payload(game_id, profile_id)
        existing_values = payload["values"]
        freshness = payload["freshness"]
        values = {} if reset else dict(existing_values)

        for entry in self.list_globals(game_id):
            name = str(entry.get("name", "")).strip()
            if not name:
                continue
            if entry.get("field_target"):
                continue
            is_typed = int(entry.get("schema_version", 1)) >= 2
            kind = entry.get("kind", VARIABLE_KIND_LIVE)
            if reset and is_typed and kind in {
                VARIABLE_KIND_CONFIG,
                VARIABLE_KIND_PROFILE,
            }:
                values[name] = existing_values.get(name, entry.get("initial_value"))
            elif reset:
                values[name] = entry.get("initial_value")
            else:
                values.setdefault(name, entry.get("initial_value"))

            if is_typed and kind == VARIABLE_KIND_LIVE:
                if reset or invalidate_live or name not in existing_values:
                    values[name] = None
                    freshness[name] = False
                else:
                    freshness.setdefault(name, values[name] is not None)
            else:
                freshness[name] = True

        if reset or invalidate_live:
            live_fields = {
                target: {
                    entry["name"]
                    for entry in self.list_globals(game_id)
                    if entry.get("field_target") == target
                    and entry.get("kind") == VARIABLE_KIND_LIVE
                }
                for target in ("event", "task")
            }
            for target, names in live_fields.items():
                field_key = f"{target}_fields"
                freshness_key = f"{target}_freshness"
                for scope_id, scoped_values in payload[field_key].items():
                    scoped_freshness = payload[freshness_key].setdefault(
                        scope_id,
                        {},
                    )
                    for name in names:
                        if name in scoped_values:
                            scoped_values[name] = None
                            scoped_freshness[name] = False

        payload["values"] = dict(values)
        payload["freshness"] = dict(freshness)
        self._write_profile_payload(game_id, profile_id, payload)
        return values

    @_profile_synchronized
    def save_profile_values(
        self,
        game_id: str,
        profile_id: str,
        values: dict,
        freshness: dict | None = None,
    ) -> dict:

        if freshness is None:
            freshness = self._read_profile_payload(game_id, profile_id)["freshness"]
        payload = {
            **self._read_profile_payload(game_id, profile_id),
            "values": dict(values),
            "freshness": dict(freshness),
        }
        self._write_profile_payload(game_id, profile_id, payload)
        return payload["values"]

    @_profile_synchronized
    def initialize_context_values(
        self,
        game_id: str,
        profile_id: str,
        event_id: str = "",
        task_id: str = "",
        field_names: set[str] | None = None,
    ) -> dict:
        values = self.initialize_profile_values(
            game_id,
            profile_id,
            reset=False,
        )
        payload = self._read_profile_payload(game_id, profile_id)
        changed = False
        allowed_names = {
            str(name).casefold()
            for name in (field_names or set())
        }

        for definition in self.list_globals(game_id):
            target = definition.get("field_target")
            if target and definition["name"].casefold() not in allowed_names:
                continue
            scope_id = event_id if target == "event" else task_id if target == "task" else ""
            if not scope_id:
                continue
            field_key = f"{target}_fields"
            freshness_key = f"{target}_freshness"
            scoped_values = payload[field_key].setdefault(scope_id, {})
            scoped_freshness = payload[freshness_key].setdefault(scope_id, {})
            name = definition["name"]
            if name not in scoped_values:
                scoped_values[name] = (
                    None
                    if definition.get("kind") == VARIABLE_KIND_LIVE
                    else definition.get("initial_value")
                )
                scoped_freshness[name] = definition.get("kind") != VARIABLE_KIND_LIVE
                changed = True
            if (
                definition.get("kind") == VARIABLE_KIND_LIVE
                and scoped_freshness.get(name) is False
            ):
                values[name] = None
            else:
                values[name] = scoped_values.get(name)

        if changed:
            self._write_profile_payload(game_id, profile_id, payload)
        return values

    @_profile_synchronized
    def set_context_value(
        self,
        game_id: str,
        profile_id: str,
        event_id: str,
        task_id: str,
        name: str,
        value,
        source: str = "function",
        field_names: set[str] | None = None,
    ):
        name = self._normalize_name(name)
        definition = self.get_definition(game_id, name)
        if not definition or not definition.get("field_target"):
            return self.set_profile_value(
                game_id,
                profile_id,
                name,
                value,
                source=source,
            )

        name = definition["name"]
        if (
            source == "function"
            and name.casefold()
            not in {str(entry).casefold() for entry in (field_names or set())}
        ):
            raise PermissionError(
                f"variable '{name}' is not available in this execution context"
            )
        if source == "function" and not bool(
            definition.get("writable_by_functions", True)
        ):
            raise PermissionError(f"variable '{name}' is read-only for functions")
        target = definition["field_target"]
        scope_id = event_id if target == "event" else task_id
        if not scope_id:
            raise PermissionError(
                f"variable '{name}' requires a {target} execution context"
            )

        value = self._coerce_value(definition.get("value_type"), value)
        payload = self._read_profile_payload(game_id, profile_id)
        field_key = f"{target}_fields"
        freshness_key = f"{target}_freshness"
        payload[field_key].setdefault(scope_id, {})[name] = value
        payload[freshness_key].setdefault(scope_id, {})[name] = True
        self._write_profile_payload(game_id, profile_id, payload)
        self.reinitialize_service.publish(
            "variable",
            game_id=game_id,
            profile_id=profile_id,
            variable_name=name,
            field_target=target,
            scope_id=scope_id,
        )
        return value

    @_profile_synchronized
    def get_context_value(
        self,
        game_id: str,
        profile_id: str,
        event_id: str,
        task_id: str,
        name: str,
        default=None,
    ):
        name = self._normalize_name(name)
        definition = self.get_definition(game_id, name)
        if not definition or not definition.get("field_target"):
            return self.get_profile_value(
                game_id,
                profile_id,
                name,
                default,
            )
        name = definition["name"]
        target = definition["field_target"]
        scope_id = event_id if target == "event" else task_id
        if not scope_id:
            return default
        payload = self._read_profile_payload(game_id, profile_id)
        values = payload[f"{target}_fields"].get(scope_id, {})
        freshness = payload[f"{target}_freshness"].get(scope_id, {})
        if (
            definition.get("kind") == VARIABLE_KIND_LIVE
            and freshness.get(name) is False
        ):
            return default
        return values.get(name, definition.get("initial_value", default))

    def sync_profile_values(
        self,
        game_id: str,
        source_profile_id: str,
        target_profile_id: str,
        event_id_map: dict[str, str],
        task_id_map: dict[str, str],
    ) -> dict:
        """Copy a profile payload and mark copied values as unconfirmed."""

        source = self._read_profile_payload(game_id, source_profile_id)
        target = self._read_profile_payload(game_id, target_profile_id)
        target["values"].update(source["values"])
        target["freshness"].update(source["freshness"])

        for target_kind, id_map in (
            ("event", event_id_map),
            ("task", task_id_map),
        ):
            source_values = source[f"{target_kind}_fields"]
            source_freshness = source[f"{target_kind}_freshness"]
            target_values = target[f"{target_kind}_fields"]
            target_freshness = target[f"{target_kind}_freshness"]
            for source_scope_id, values in source_values.items():
                target_scope_id = id_map.get(source_scope_id)
                if not target_scope_id:
                    continue
                target_values[target_scope_id] = dict(values)
                target_freshness[target_scope_id] = dict(
                    source_freshness.get(source_scope_id, {})
                )

        unconfirmed = target.setdefault("unconfirmed", {})
        unconfirmed["profile"] = sorted(source["values"].keys())
        for target_kind, id_map in (
            ("event", event_id_map),
            ("task", task_id_map),
        ):
            target_marks = unconfirmed.setdefault(target_kind, {})
            for source_scope_id, target_scope_id in id_map.items():
                names = source[f"{target_kind}_fields"].get(source_scope_id, {})
                target_marks[target_scope_id] = sorted(names.keys())

        self._write_profile_payload(game_id, target_profile_id, target)
        self.reinitialize_service.publish(
            "variable",
            game_id=game_id,
            profile_id=target_profile_id,
            synchronized_from=source_profile_id,
        )
        return unconfirmed

    def is_unconfirmed(
        self,
        game_id: str,
        profile_id: str,
        target_kind: str,
        scope_id: str,
        name: str,
    ) -> bool:
        payload = self._read_profile_payload(game_id, profile_id)
        if target_kind == "profile":
            return name in payload.get("unconfirmed", {}).get("profile", [])
        return name in payload.get("unconfirmed", {}).get(target_kind, {}).get(scope_id, [])

    def confirm_value(
        self,
        game_id: str,
        profile_id: str,
        target_kind: str,
        scope_id: str,
        name: str,
    ) -> None:
        payload = self._read_profile_payload(game_id, profile_id)
        marks = payload.setdefault("unconfirmed", {})
        if target_kind == "profile":
            names = marks.setdefault("profile", [])
        else:
            names = marks.setdefault(target_kind, {}).setdefault(scope_id, [])
        if name in names:
            names.remove(name)
            self._write_profile_payload(game_id, profile_id, payload)

    @_profile_synchronized
    def get_profile_value(
        self,
        game_id: str,
        profile_id: str,
        name: str,
        default=None,
    ):

        name = self._normalize_name(name)
        payload = self._read_profile_payload(game_id, profile_id)
        definition = self.get_definition(game_id, name)
        if definition:
            name = definition["name"]
            if definition.get("field_target"):
                return default
        if (
            definition
            and int(definition.get("schema_version", 1)) >= 2
            and definition.get("kind") == VARIABLE_KIND_LIVE
            and payload["freshness"].get(name) is False
        ):
            return default
        return payload["values"].get(name, default)

    @_profile_synchronized
    def set_profile_value(
        self,
        game_id: str,
        profile_id: str,
        name: str,
        value,
        source: str = "user",
    ):

        name = self._normalize_name(name)
        if not name:
            raise ValueError("variable name is required")

        definition = self.get_definition(game_id, name)
        if definition:
            name = definition["name"]
            if definition.get("field_target"):
                raise ValueError(
                    f"variable '{name}' requires an Event or Task context"
                )
        if (
            source == "function"
            and definition
            and not bool(definition.get("writable_by_functions", True))
        ):
            raise PermissionError(f"variable '{name}' is read-only for functions")

        if definition and int(definition.get("schema_version", 1)) >= 2:
            value = self._coerce_value(definition.get("value_type"), value)

        payload = self._read_profile_payload(game_id, profile_id)
        values = payload["values"]
        freshness = payload["freshness"]
        values[name] = value
        freshness[name] = True
        self.save_profile_values(
            game_id,
            profile_id,
            values,
            freshness=freshness,
        )
        self.reinitialize_service.publish(
            "variable",
            game_id=game_id,
            profile_id=profile_id,
            variable_name=name,
            write_source=source,
        )
        return value

    def create_global(
        self,
        game_id: str,
        name: str,
        description: str = "",
        initial_value=None,
        debug_visible: bool = False,
        kind: str = VARIABLE_KIND_LIVE,
        value_type: str = VALUE_TYPE_ANY,
        writable_by_functions: bool | None = None,
    ) -> dict:

        name = self._normalize_name(name)
        if not name:
            raise ValueError("variable name is required")
        if kind not in VARIABLE_KINDS:
            raise ValueError(f"unsupported variable kind '{kind}'")
        if value_type not in VALUE_TYPES:
            raise ValueError(f"unsupported variable type '{value_type}'")

        entries = self.list_globals(game_id)
        if any(entry["name"].casefold() == name.casefold() for entry in entries):
            raise VariableNameConflictError(
                f"global variable '{name}' already exists"
            )

        if writable_by_functions is None:
            writable_by_functions = kind == VARIABLE_KIND_LIVE
        entry = {
            "schema_version": 1 if value_type == VALUE_TYPE_ANY else 2,
            "variable_id": str(uuid4()),
            "name": name,
            "description": description,
            "initial_value": self._coerce_value(value_type, initial_value),
            "debug_visible": bool(debug_visible),
            "kind": kind,
            "value_type": value_type,
            "writable_by_functions": bool(writable_by_functions),
        }
        entries.append(entry)
        self._save(game_id, entries)
        return entry

    def create_variable(
        self,
        game_id: str,
        name: str,
        description: str = "",
        initial_value=None,
        debug_visible: bool = False,
        kind: str = VARIABLE_KIND_LIVE,
        value_type: str = VALUE_TYPE_ANY,
        writable_by_functions: bool | None = None,
    ) -> dict:
        """Register a user-facing profile variable for a game."""

        return self.create_global(
            game_id,
            name,
            description,
            initial_value,
            debug_visible,
            kind,
            value_type,
            writable_by_functions,
        )

    def delete_global(self, game_id: str, name: str) -> bool:
        """Remove a registry entry; profile payloads stay untouched history."""

        normalized = self._normalize_name(name)
        entries = self.list_globals(game_id)
        remaining = [
            entry
            for entry in entries
            if entry["name"].casefold() != normalized.casefold()
        ]
        if len(remaining) == len(entries):
            return False
        self._save(game_id, remaining)
        self.logger.info(
            "Variable Deleted",
            game=game_id,
            variable=normalized,
        )
        return True

    def update_global(
        self,
        game_id: str,
        name: str,
        description: str | None = None,
        value_type: str | None = None,
        kind: str | None = None,
        initial_value=None,
        writable_by_functions: bool | None = None,
        debug_visible: bool | None = None,
    ) -> dict | None:
        """Edit an existing registry entry; the name stays stable."""

        normalized = self._normalize_name(name)
        entries = self.list_globals(game_id)
        for entry in entries:
            if entry["name"].casefold() != normalized.casefold():
                continue
            if description is not None:
                entry["description"] = description
            if value_type is not None:
                if value_type not in VALUE_TYPES:
                    raise ValueError(f"unsupported variable type '{value_type}'")
                entry["value_type"] = value_type
            if kind is not None:
                if kind not in VARIABLE_KINDS:
                    raise ValueError(f"unsupported variable kind '{kind}'")
                entry["kind"] = kind
            if initial_value is not None:
                entry["initial_value"] = self._coerce_value(
                    entry["value_type"],
                    initial_value,
                )
            if writable_by_functions is not None:
                entry["writable_by_functions"] = bool(writable_by_functions)
            if debug_visible is not None:
                entry["debug_visible"] = bool(debug_visible)
            self._save(game_id, entries)
            self.logger.info(
                "Variable Updated",
                game=game_id,
                variable=normalized,
            )
            return entry
        return None

    def declare_variables(
        self,
        game_id: str,
        declarations: list[dict],
    ) -> list[dict]:
        """Validate and register a declaration set in one registry write."""

        entries = self.list_globals(game_id)
        definitions_by_name = {
            entry["name"].casefold(): entry
            for entry in entries
        }
        resolved = []
        changed = False

        for declaration in declarations:
            name = self._normalize_name(declaration.get("name", ""))
            kind = str(declaration.get("kind") or "")
            value_type = str(declaration.get("value_type") or "")
            field_target = str(declaration.get("field_target") or "")
            writable = bool(declaration.get("writable_by_functions", False))
            if not name:
                raise VariableDefinitionConflictError("variable name is required")
            if kind not in VARIABLE_KINDS:
                raise VariableDefinitionConflictError(
                    f"variable '{name}' has unsupported kind '{kind}'"
                )
            if value_type not in VALUE_TYPES or value_type == VALUE_TYPE_ANY:
                raise VariableDefinitionConflictError(
                    f"variable '{name}' has unsupported type '{value_type}'"
                )
            if field_target not in {"event", "task"}:
                raise VariableDefinitionConflictError(
                    f"variable '{name}' has unsupported field target '{field_target}'"
                )

            initial_value = self._coerce_value(
                value_type,
                declaration.get("initial_value"),
            )
            existing = definitions_by_name.get(name.casefold())
            if existing:
                if int(existing.get("schema_version", 1)) < 2:
                    raise VariableDefinitionConflictError(
                        f"variable '{name}' is an untyped legacy definition"
                    )
                conflicts = [
                    key
                    for key, requested in (
                        ("kind", kind),
                        ("value_type", value_type),
                        ("writable_by_functions", writable),
                    )
                    if existing.get(key) != requested
                ]
                existing_target = existing.get("field_target")
                if existing_target and existing_target != field_target:
                    conflicts.append("field_target")
                if conflicts:
                    raise VariableDefinitionConflictError(
                        f"variable '{name}' conflicts in: {', '.join(conflicts)}"
                    )
                if not existing_target:
                    existing["field_target"] = field_target
                    changed = True
                resolved.append(existing)
                continue

            definition = {
                "schema_version": 2,
                "variable_id": str(uuid4()),
                "name": name,
                "description": str(declaration.get("description") or ""),
                "initial_value": initial_value,
                "debug_visible": bool(declaration.get("debug_visible", False)),
                "kind": kind,
                "value_type": value_type,
                "field_target": field_target,
                "writable_by_functions": writable,
            }
            entries.append(definition)
            definitions_by_name[name.casefold()] = definition
            resolved.append(definition)
            changed = True

        if changed:
            self._save(game_id, entries)
        return resolved

    def ensure_global(
        self,
        game_id: str,
        name: str,
        description: str = "",
        initial_value=None,
        debug_visible: bool = False,
        kind: str = VARIABLE_KIND_LIVE,
        value_type: str = VALUE_TYPE_ANY,
        writable_by_functions: bool | None = None,
    ) -> dict:
        """Idempotently register a functional variable name for a game."""

        for entry in self.list_globals(game_id):
            if entry["name"].casefold() == name.casefold():
                return entry

        return self.create_global(
            game_id,
            name,
            description,
            initial_value,
            debug_visible,
            kind,
            value_type,
            writable_by_functions,
        )

    def ensure_variable(
        self,
        game_id: str,
        name: str,
        description: str = "",
        initial_value=None,
        debug_visible: bool = False,
        kind: str = VARIABLE_KIND_LIVE,
        value_type: str = VALUE_TYPE_ANY,
        writable_by_functions: bool | None = None,
    ) -> dict:
        """Idempotently register a user-facing profile variable."""

        return self.ensure_global(
            game_id,
            name,
            description,
            initial_value,
            debug_visible,
            kind,
            value_type,
            writable_by_functions,
        )

    @staticmethod
    def _normalize_name(name: str) -> str:
        normalized = str(name).strip().lstrip("$")
        if normalized.startswith("global:"):
            return normalized[7:]
        if normalized.startswith("local:"):
            return normalized[6:]
        return normalized

    @staticmethod
    def coerce_value(value_type: str, value):
        """Normalize a user-facing value according to a registered type."""

        return VariableService._coerce_value(value_type, value)

    @staticmethod
    def _coerce_value(value_type: str, value):
        if value is None:
            return None
        if value_type == VALUE_TYPE_ANY:
            return value
        if value_type != VALUE_TYPE_TEXT and value == "":
            return None
        if value_type == VALUE_TYPE_TEXT:
            return str(value)
        if value_type == VALUE_TYPE_NUMBER:
            if isinstance(value, bool):
                # function results such as compare outcomes must not crash the
                # runtime when a legacy config points them at a number slot
                return 1 if value else 0
            try:
                number = float(value)
            except (TypeError, ValueError) as ex:
                raise VariableValueError("number value is invalid") from ex
            return int(number) if number.is_integer() else number
        if value_type == VALUE_TYPE_BOOLEAN:
            if isinstance(value, bool):
                return value
            normalized = str(value).strip().casefold()
            if normalized in {"true", "1", "yes", "ja", "on"}:
                return True
            if normalized in {"false", "0", "no", "nein", "off"}:
                return False
            raise VariableValueError("boolean value is invalid")
        if value_type == VALUE_TYPE_DATETIME:
            text = str(value).strip()
            # an empty or literal "None" write clears the value instead of crashing
            if not text or text.casefold() == "none":
                return None
            try:
                return datetime.fromisoformat(text).isoformat()
            except ValueError:
                pass
            # a duration text (e.g. an OCR timer "04:54:00" or "1d 23:43:15")
            # becomes a future timestamp instead of crashing the write
            from services.function_runner import _DURATION_PATTERN
            match = _DURATION_PATTERN.match(text)
            if match:
                days = int(match.group("days") or 0)
                hours = int(match.group("hours"))
                minutes = int(match.group("minutes"))
                seconds = int(match.group("seconds"))
                total = days * 86400 + hours * 3600 + minutes * 60 + seconds
                from datetime import timezone, timedelta
                return (
                    datetime.now(timezone.utc) + timedelta(seconds=total)
                ).isoformat()
            raise VariableValueError(
                "datetime value is invalid"
            )
        if value_type == VALUE_TYPE_DURATION:
            text = str(value).strip()
            if not text or text.casefold() == "none":
                return None
            try:
                duration = float(text)
            except (TypeError, ValueError):
                # duration text from OCR (e.g. '1d 23:33:06') parses to seconds
                from services.function_runner import _DURATION_PATTERN
                match = _DURATION_PATTERN.match(text)
                if not match:
                    raise VariableValueError(
                        f"duration value is invalid: {text!r}"
                    )
                days = int(match.group("days") or 0)
                hours = int(match.group("hours"))
                minutes = int(match.group("minutes"))
                seconds = int(match.group("seconds"))
                duration = float(
                    days * 86400 + hours * 3600 + minutes * 60 + seconds
                )
            if duration < 0:
                raise VariableValueError("duration must not be negative")
            return int(duration) if duration.is_integer() else duration
        if value_type == VALUE_TYPE_COLLECTION:
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError as ex:
                    raise VariableValueError("collection must be a JSON list") from ex
            if not isinstance(value, list):
                raise VariableValueError("collection must be a list")
            return value
        raise VariableValueError(f"unsupported variable type '{value_type}'")

    def _save(self, game_id: str, entries: list):
        self._file(game_id).write_text(
            json.dumps(entries, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        self.reinitialize_service.publish("variable", game_id=game_id)
