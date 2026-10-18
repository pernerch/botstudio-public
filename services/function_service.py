import json

from services.file_io import atomic_write_json, read_json
from pathlib import Path

from models.bot_function import (
    BotFunction,
    FUNCTION_KIND_HELPER,
    FUNCTION_KIND_REGULAR,
    HelperDependency,
)
from services.library_service import LibraryService
from services.function_block_catalog import BLOCK_CONFIGURE_FIELD
from services.logging_service import get_logger
from services.reinitialize_service import get_default_reinitialize_service
from services.variable_service import (
    VALUE_TYPE_COLLECTION,
    VariableDefinitionConflictError,
    VariableService,
)


class CallDirectionError(Exception):
    """Raised when a sealed helper would depend on a user editable function."""


class LibraryDependencyError(Exception):
    """Raised when a Function references a missing Library entry."""


class FieldDeclarationError(ValueError):
    """Raised when a Configure Field block has an invalid contract."""


class FunctionService:

    def __init__(
        self,
        root: str = "data/functions",
        library_service=None,
        logger=None,
        reinitialize_service=None,
        variable_service=None,
    ):

        self.root = Path(
            root
        )

        self.root.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.library_service = (
            library_service
            or LibraryService()
        )
        self.variable_service = variable_service or VariableService()

        self.logger = (
            logger
            or get_logger("function")
        )
        self.reinitialize_service = (
            reinitialize_service or get_default_reinitialize_service()
        )

    def game_root(
        self,
        game_id: str,
    ) -> Path:

        return self.root / game_id

    def function_file(
        self,
        game_id: str,
        function_id: str,
    ) -> Path:

        return (
            self.game_root(game_id)
            / f"{function_id}.json"
        )

    def list_functions(
        self,
        game_id: str,
        kind: str | None = None,
    ) -> list:

        game_root = self.game_root(
            game_id
        )

        if not game_root.exists():
            return []

        self._cleanup_unsaved_functions(game_id)

        functions = []

        for file in sorted(
            game_root.glob("*.json")
        ):

            function = self._read(file)

            if not function:
                continue

            if not function.saved:
                file.unlink(missing_ok=True)
                continue

            if kind and function.kind != kind:
                continue

            functions.append(function)

        return functions

    def list_schedulable(
        self,
        game_id: str,
    ) -> list:

        return [
            function
            for function in self.list_functions(
                game_id,
                FUNCTION_KIND_REGULAR,
            )
            if function.is_schedulable()
        ]

    def missing_library_entries(self, function: BotFunction) -> list[str]:
        missing = []
        for block in function.blocks:
            entry_id = str(block.params.get("library_entry_id") or "").strip()
            if not entry_id or entry_id in missing:
                continue
            if self.library_service.load(entry_id) is None:
                missing.append(entry_id)
        return missing

    def missing_field_dependencies(self, function: BotFunction) -> list[str]:
        """Return missing variables referenced by Configure Field blocks."""

        missing = []
        if not function or not function.game_id:
            return missing
        for block in function.walk_blocks():
            if block.block_type != BLOCK_CONFIGURE_FIELD:
                continue
            name = str(block.params.get("variable_name") or "").strip().lstrip("$")
            if name and self.variable_service.get_definition(function.game_id, name) is None:
                missing.append(name)
            if block.params.get("options_source") != "variable":
                continue
            options_name = str(block.params.get("options_variable") or "").strip().lstrip("$")
            definition = self.variable_service.get_definition(
                function.game_id,
                options_name,
            )
            if (
                not definition
                or definition.get("value_type") != "collection"
            ) and options_name:
                missing.append(options_name)
        return sorted(set(missing), key=str.casefold)

    def load(
        self,
        game_id: str,
        function_id: str,
    ) -> BotFunction | None:

        function = self._read(
            self.function_file(
                game_id,
                function_id,
            )
        )

        if function and not function.saved:
            self.delete(game_id, function_id)
            return None

        return function

    def save(
        self,
        function: BotFunction,
    ) -> BotFunction:

        missing_library_entries = self.missing_library_entries(function)
        if missing_library_entries:
            self.logger.error(
                "Function Save Rejected",
                function_id=function.function_id,
                missing_library_entries=missing_library_entries,
                next_action="restore the Library entries or repair the Function",
            )
            raise LibraryDependencyError(
                "Function references missing Library entries: "
                + ", ".join(missing_library_entries)
            )

        self._validate_call_direction(
            function
        )
        self._register_field_declarations(function)

        missing_field_dependencies = self.missing_field_dependencies(function)
        if missing_field_dependencies:
            raise FieldDeclarationError(
                "Function references missing field dependencies: "
                + ", ".join(missing_field_dependencies)
            )

        function.saved = True
        function.library_entries = (
            function.collect_library_entries()
        )

        self._sync_helper_dependencies(
            function
        )
        function.used = self._build_used_dependencies(
            function
        )

        game_root = self.game_root(
            function.game_id
        )

        game_root.mkdir(
            parents=True,
            exist_ok=True,
        )

        file = self.function_file(
            function.game_id,
            function.function_id,
        )
        previous_function = self._read(file) if file.exists() else None

        atomic_write_json(file, function.to_dict())

        self._cleanup_unsaved_functions(
            function.game_id
        )
        self._sync_library_usage_for_function(previous_function, function)
        self.sync_helper_usage(
            function.game_id
        )

        self.logger.info(
            "Function Saved",
            function_id=function.function_id,
            name=function.name,
            game=function.game_id,
            kind=function.kind,
            revision=function.revision,
            author=function.author,
            library_entries=len(function.library_entries),
        )
        if self.reinitialize_service:
            self.reinitialize_service.publish(
                "function",
                game_id=function.game_id,
                function_id=function.function_id,
            )

        return function

    def _register_field_declarations(self, function: BotFunction) -> list[dict]:
        blocks = [
            block
            for block in function.walk_blocks()
            if block.block_type == BLOCK_CONFIGURE_FIELD
        ]
        if not blocks:
            return []

        declarations = []
        pending_collections = {}
        declared_names = set()
        for block in blocks:
            params = block.params
            name = str(params.get("variable_name") or "").strip().lstrip("$")
            target = str(params.get("field_target") or "")
            options_source = str(params.get("options_source") or "none")
            if not name:
                raise FieldDeclarationError("Configure Field requires a variable name")
            normalized_name = name.casefold()
            if normalized_name in declared_names:
                raise FieldDeclarationError(
                    f"Configure Field '{name}' is declared more than once"
                )
            declared_names.add(normalized_name)
            if target not in {"event", "task"}:
                raise FieldDeclarationError(
                    f"Configure Field '{name}' has invalid target '{target}'"
                )
            if options_source not in {"none", "fixed", "variable"}:
                raise FieldDeclarationError(
                    f"Configure Field '{name}' has invalid options source"
                )

            fixed_options = params.get("fixed_options", [])
            if options_source == "fixed":
                if isinstance(fixed_options, str):
                    try:
                        fixed_options = json.loads(fixed_options)
                    except json.JSONDecodeError as ex:
                        raise FieldDeclarationError(
                            f"Configure Field '{name}' options must be a JSON list"
                        ) from ex
                if not isinstance(fixed_options, list):
                    raise FieldDeclarationError(
                        f"Configure Field '{name}' options must be a JSON list"
                    )
                params["fixed_options"] = fixed_options

            declaration = {
                "name": name,
                "kind": str(params.get("variable_kind") or ""),
                "value_type": str(params.get("value_type") or ""),
                "field_target": target,
                "description": str(params.get("description") or ""),
                "initial_value": params.get("initial_value"),
                "writable_by_functions": bool(
                    params.get("writable_by_functions", False)
                ),
            }
            declarations.append(declaration)
            if declaration["value_type"] == VALUE_TYPE_COLLECTION:
                pending_collections[name.casefold()] = target

        for block in blocks:
            params = block.params
            if params.get("options_source") != "variable":
                continue
            name = str(params.get("variable_name") or "").strip().lstrip("$")
            options_name = str(params.get("options_variable") or "").strip().lstrip("$")
            definition = self.variable_service.get_definition(
                function.game_id,
                options_name,
            )
            pending_target = pending_collections.get(options_name.casefold())
            definition_target = definition.get("field_target") if definition else ""
            is_pending_collection = pending_target is not None
            if not options_name or not (
                is_pending_collection
                or definition
                and definition.get("value_type") == VALUE_TYPE_COLLECTION
            ):
                raise FieldDeclarationError(
                    f"Configure Field '{name}' requires a Collection variable"
                )
            if (
                params.get("field_target") == "event"
                and (pending_target or definition_target) == "task"
            ):
                raise FieldDeclarationError(
                    f"Configure Field '{name}' cannot use a Task Collection on an Event"
                )

        try:
            definitions = self.variable_service.declare_variables(
                function.game_id,
                declarations,
            )
        except VariableDefinitionConflictError as error:
            raise FieldDeclarationError(str(error)) from error

        if len(definitions) != len(blocks):
            raise FieldDeclarationError(
                "Configure Field declaration count mismatch"
            )
        for block, definition in zip(blocks, definitions):
            block.params["variable_name"] = definition["name"]
            block.params["variable_id"] = definition["variable_id"]
        return definitions

    def delete(
        self,
        game_id: str,
        function_id: str,
    ) -> bool:

        file = self.function_file(
            game_id,
            function_id,
        )

        if not file.exists():
            return False

        file.unlink()
        self._disable_scheduler_users(
            game_id,
            function_id,
        )

        self.sync_library_usage(
            game_id
        )
        self.sync_helper_usage(
            game_id
        )

        self.logger.info(
            "Function Deleted",
            function_id=function_id,
            game=game_id,
        )
        if self.reinitialize_service:
            self.reinitialize_service.publish(
                "function",
                game_id=game_id,
                function_id=function_id,
                deleted=True,
            )

        return True

    def _disable_scheduler_users(
        self,
        game_id: str,
        function_id: str,
    ) -> None:
        scheduler_root = Path("data/scheduler") / game_id
        if not scheduler_root.exists():
            return

        for event_file in scheduler_root.glob("*.json"):
            try:
                with open(event_file, "r", encoding="utf-8") as handle:
                    event = json.load(handle)
            except (OSError, ValueError, TypeError):
                continue

            changed = False
            for task in event.get("tasks") or []:
                if function_id not in (task.get("function_ids") or []):
                    continue
                if task.get("enabled", True):
                    task["enabled"] = False
                    changed = True

            if not changed:
                continue
            with open(event_file, "w", encoding="utf-8") as handle:
                json.dump(event, handle, indent=4, ensure_ascii=False)

    def find_scheduler_users(
        self,
        game_id: str,
        function_id: str,
    ) -> list[dict]:
        """Return Events and Tasks that reference a saved Function."""

        scheduler_root = Path("data/scheduler") / game_id
        if not scheduler_root.exists():
            return []

        users = []
        for event_file in sorted(scheduler_root.glob("*.json")):
            try:
                with open(event_file, "r", encoding="utf-8") as handle:
                    event = json.load(handle)
            except (OSError, ValueError, TypeError):
                continue

            for task in event.get("tasks") or []:
                if function_id not in (task.get("function_ids") or []):
                    continue
                users.append({
                    "event_id": event.get("event_id") or event_file.stem,
                    "event_name": event.get("name") or event_file.stem,
                    "task_id": task.get("task_id") or "",
                    "task_name": task.get("name") or task.get("task_id") or "",
                })
        return users

    def find_users_of_entry(
        self,
        game_id: str,
        entry_id: str,
    ) -> list:
        """Functions that break when this library image is replaced."""

        return [
            function
            for function in self.list_functions(game_id)
            if entry_id in function.library_entries
        ]

    def find_users_of_helper(
        self,
        game_id: str,
        helper_id: str,
    ) -> list:

        return [
            function
            for function in self.list_functions(game_id)
            if helper_id in function.collect_helper_ids()
        ]

    def find_outdated_helper_usage(
        self,
        game_id: str,
    ) -> list:
        """Functions tested against an older helper revision than the current one."""

        helpers = {
            helper.function_id: helper
            for helper in self.list_functions(
                game_id,
                FUNCTION_KIND_HELPER,
            )
        }

        outdated = []

        for function in self.list_functions(
            game_id,
            FUNCTION_KIND_REGULAR,
        ):

            for dependency in function.helper_dependencies:

                helper = helpers.get(
                    dependency.function_id
                )

                if not helper:
                    continue

                if helper.revision > dependency.tested_revision:

                    outdated.append(
                        {
                            "function": function,
                            "helper": helper,
                            "tested_revision": dependency.tested_revision,
                            "current_revision": helper.revision,
                        }
                    )

        return outdated

    def sync_library_usage(
        self,
        game_id: str,
    ) -> dict:
        """Synchronize library reverse references from all saved functions."""

        usage = {}

        for function in self._list_all_saved_functions():

            entry_ids = set(function.library_entries)
            entry_ids.update(
                function.collect_library_entries()
            )

            for entry_id in entry_ids:

                function_ids = usage.setdefault(entry_id, [])
                if function.function_id not in function_ids:
                    function_ids.append(function.function_id)

        for entry_file in self.library_service.list_entries():
            entry_id = entry_file.parent.name
            self.library_service.set_used_by(
                entry_id,
                usage.get(entry_id, []),
            )

        return usage

    def _sync_library_usage_for_function(
        self,
        previous_function: BotFunction | None,
        function: BotFunction,
    ) -> None:
        previous_entry_ids = set()
        if previous_function and previous_function.saved:
            previous_entry_ids.update(previous_function.library_entries)
            previous_entry_ids.update(previous_function.collect_library_entries())

        current_entry_ids = set(function.library_entries)
        current_entry_ids.update(function.collect_library_entries())

        for entry_id in previous_entry_ids | current_entry_ids:
            entry = self.library_service.load(entry_id)
            if entry is None:
                continue

            function_ids = list(entry.get("used_by") or [])
            if entry_id in current_entry_ids:
                if function.function_id not in function_ids:
                    function_ids.append(function.function_id)
            else:
                function_ids = [
                    function_id
                    for function_id in function_ids
                    if function_id != function.function_id
                ]

            self.library_service.set_used_by(entry_id, function_ids)

    def cleanup_orphaned_library_entries(
        self,
    ) -> list[str]:
        """Delete library entries that no saved function references."""

        usage = self.sync_library_usage("")
        removed = []

        for entry_file in self.library_service.list_entries():
            entry_id = entry_file.parent.name
            if entry_id in usage:
                continue

            if self.library_service.delete_entry(entry_id):
                removed.append(entry_id)

        return removed

    def sync_helper_usage(
        self,
        game_id: str,
    ) -> dict:
        """Synchronize helper reverse references from saved functions only."""

        usage = {}

        for function in self.list_functions(game_id):
            for dependency in function.helper_dependencies:
                usage.setdefault(
                    dependency.function_id,
                    [],
                ).append(
                    function.function_id
                )

        for helper in self.list_functions(
            game_id,
            FUNCTION_KIND_HELPER,
        ):
            used_by = usage.get(
                helper.function_id,
                [],
            )
            if helper.used_by == used_by:
                continue
            helper.used_by = used_by
            self._write(helper)

        return usage

    def _cleanup_unsaved_functions(
        self,
        game_id: str,
    ) -> int:
        """Delete transient drafts that were never explicitly saved."""

        game_root = self.game_root(game_id)
        if not game_root.exists():
            return 0

        removed = 0

        for file in sorted(game_root.glob("*.json")):
            function = self._read(file)
            if function is None:
                continue
            if function.saved:
                continue
            file.unlink(missing_ok=True)
            removed += 1

        return removed

    def _list_all_saved_functions(
        self,
    ) -> list[BotFunction]:
        """Load saved functions from every game directory."""

        if not self.root.exists():
            return []

        functions = []

        for game_root in sorted(self.root.iterdir()):
            if not game_root.is_dir():
                continue

            functions.extend(
                self.list_functions(game_root.name)
            )

        return functions

    def _sync_helper_dependencies(
        self,
        function: BotFunction,
    ) -> None:

        known = {
            dependency.function_id: dependency
            for dependency in function.helper_dependencies
        }

        dependencies = []

        for helper_id in function.collect_helper_ids():

            helper = self.load(
                function.game_id,
                helper_id,
            )

            if not helper:
                continue

            existing = known.get(
                helper_id
            )

            dependencies.append(
                HelperDependency(
                    function_id=helper_id,
                    name=helper.name,
                    tested_revision=(
                        existing.tested_revision
                        if existing
                        else helper.revision
                    ),
                )
            )

        function.helper_dependencies = dependencies

    def _build_used_dependencies(
        self,
        function: BotFunction,
    ) -> list:
        """Build the persisted forward dependency index at save time."""

        used = [
            {
                "type": "library_entry",
                "id": entry_id,
            }
            for entry_id in function.library_entries
        ]

        used.extend(
            {
                "type": "function",
                "id": dependency.function_id,
                "revision": dependency.tested_revision,
            }
            for dependency in function.helper_dependencies
        )

        return used

    def _write(
        self,
        function: BotFunction,
    ) -> None:
        file = self.function_file(
            function.game_id,
            function.function_id,
        )

        atomic_write_json(file, function.to_dict())

    def _validate_call_direction(
        self,
        function: BotFunction,
    ) -> None:

        if function.kind != FUNCTION_KIND_HELPER:
            return

        for helper_id in function.collect_helper_ids():

            target = self.load(
                function.game_id,
                helper_id,
            )

            if not target:
                continue

            if target.kind == FUNCTION_KIND_HELPER:
                continue

            raise CallDirectionError(
                f"helper '{function.name}' may not call "
                f"regular function '{target.name}'"
            )

    def _read(
        self,
        file: Path,
    ) -> BotFunction | None:

        if not file.exists():
            return None

        try:
            function = BotFunction.from_dict(
                read_json(file)
            )
            missing = self.missing_library_entries(function)
            if missing:
                self.logger.error(
                    "Function Library Dependency Missing",
                    file=str(file),
                    function_id=function.function_id,
                    missing_library_entries=missing,
                    next_action="restore the library entries or repair the function",
                )
            return function

        except (json.JSONDecodeError, OSError, AttributeError, TypeError, KeyError) as ex:

            self.logger.error(
                "Function Load Failed",
                file=str(file),
                check="json.load",
                observed=str(ex),
                next_action="repair or delete the function file",
            )

            return None
