from services.function_block_catalog import (
    BLOCK_CALL_FUNCTION,
    collect_field_declarations,
)
from services.function_service import FunctionService
from services.variable_service import VARIABLE_KIND_PROFILE, VariableService

MAX_HELPER_DEPTH = 8


def field_scope_key(
    kind: str,
    profile_id: str,
    event_id: str,
    task_id: str = "",
) -> str:
    if kind == "task" and not task_id:
        raise ValueError("task field scope requires task_id")
    return f"{profile_id}:{kind}:{event_id}:{task_id}"


class SchedulerFieldService:
    """Builds UI-ready field snapshots outside the GUI thread."""

    def __init__(self, function_service=None, variable_service=None):
        self.variable_service = variable_service or VariableService()
        self.function_service = function_service or FunctionService(
            variable_service=self.variable_service,
        )

    def collect_profile_fields(
        self,
        game_id: str,
        profile_id: str,
        events,
        functions=None,
    ) -> dict[str, list[dict]]:
        functions = functions or self.function_service.list_functions(game_id)
        functions_by_id = {
            function.function_id: function
            for function in functions
        }
        declarations_by_function = {
            function_id: self._collect_declarations_deep(
                function_id,
                functions_by_id,
            )
            for function_id in functions_by_id
        }
        fields_by_scope = {}
        profile_fields = []
        for definition in self.variable_service.list_globals(game_id):
            if (
                definition.get("field_target")
                or definition.get("kind") != VARIABLE_KIND_PROFILE
                or int(definition.get("schema_version", 1)) < 2
            ):
                continue
            name = definition["name"]
            kind = definition.get("kind", "live")
            value = self.variable_service.get_profile_value(
                game_id, profile_id, name,
                default=definition.get("initial_value"),
            )
            profile_fields.append({
                "name": name,
                "variable_id": definition.get("variable_id", ""),
                "description": definition.get("description", ""),
                "field_target": "profile",
                "variable_kind": kind,
                "value_type": definition.get("value_type", "any"),
                "writable_by_functions": definition.get("writable_by_functions", True),
                "value": value,
                "unconfirmed": self.variable_service.is_unconfirmed(
                    game_id, profile_id, "profile", "", name,
                ),
                "profile_id": profile_id,
                "event_id": "",
                "task_id": "",
            })
        if profile_fields:
            fields_by_scope[field_scope_key("profile", profile_id, "")] = sorted(
                profile_fields, key=lambda field: field["name"].casefold(),
            )

        for event in events:
            event_declarations = []
            for task in event.ordered_tasks():
                task_declarations = self._task_declarations(
                    task,
                    declarations_by_function,
                )
                event_declarations.extend(
                    declaration
                    for declaration in task_declarations
                    if declaration.get("field_target") == "event"
                )
                task_fields = self._resolve_fields(
                    game_id,
                    profile_id,
                    event.event_id,
                    task.task_id,
                    [
                        declaration
                        for declaration in task_declarations
                        if declaration.get("field_target") == "task"
                    ],
                )
                if task_fields:
                    fields_by_scope[
                        field_scope_key(
                            "task",
                            profile_id,
                            event.event_id,
                            task.task_id,
                        )
                    ] = task_fields

            resolved_event_fields = self._resolve_fields(
                game_id,
                profile_id,
                event.event_id,
                "",
                event_declarations,
            )
            if resolved_event_fields:
                fields_by_scope[
                    field_scope_key("event", profile_id, event.event_id)
                ] = resolved_event_fields

        return fields_by_scope

    def collect_function_declarations(
        self,
        game_id: str,
        function_id: str,
        functions=None,
    ) -> list[dict]:
        """Return a Function's own and transitively called Helper fields."""

        functions = functions or self.function_service.list_functions(game_id)
        functions_by_id = {
            function.function_id: function
            for function in functions
        }
        return self._collect_declarations_deep(
            function_id,
            functions_by_id,
        )

    @staticmethod
    def _task_declarations(task, declarations_by_function) -> list[dict]:
        declarations = []
        for function_id in task.function_ids:
            declarations.extend(declarations_by_function.get(function_id, []))
        return declarations

    def _collect_declarations_deep(
        self,
        function_id: str,
        functions_by_id: dict,
        _visited: frozenset = frozenset(),
        _depth: int = 0,
    ) -> list[dict]:
        """Own Configure Field blocks plus those of called helpers.

        A helper never runs on its own, so its fields must surface at the
        calling task/event. Cycles and very deep nesting stop traversal; the
        caller's own declarations are always kept.
        """

        if function_id in _visited or _depth >= MAX_HELPER_DEPTH:
            return []

        function = functions_by_id.get(function_id)
        if function is None:
            return []

        visited = _visited | {function_id}
        declarations = list(collect_field_declarations(function))
        for declaration in declarations:
            declaration.setdefault("from_helper", False)

        for block in function.walk_blocks():
            if block.block_type != BLOCK_CALL_FUNCTION:
                continue
            helper_id = str(block.params.get("function_id") or "")
            if not helper_id or helper_id in visited:
                continue
            for helper_declaration in self._collect_declarations_deep(
                helper_id,
                functions_by_id,
                visited,
                _depth + 1,
            ):
                helper_declaration = dict(helper_declaration)
                helper_declaration["from_helper"] = True
                declarations.append(helper_declaration)

        return declarations

    def _resolve_fields(
        self,
        game_id: str,
        profile_id: str,
        event_id: str,
        task_id: str,
        declarations: list[dict],
    ) -> list[dict]:
        merged = {}
        for declaration in declarations:
            name = str(declaration.get("variable_name") or "").strip().lstrip("$")
            if not name:
                continue
            definition = self.variable_service.get_definition(game_id, name)
            canonical_name = definition.get("name", name) if definition else name
            key = canonical_name.casefold()
            descriptor = merged.setdefault(
                key,
                {
                    "name": canonical_name,
                    "variable_id": definition.get("variable_id", "") if definition else "",
                    "description": str(declaration.get("description") or ""),
                    "field_target": str(declaration.get("field_target") or ""),
                    "variable_kind": str(declaration.get("variable_kind") or ""),
                    "value_type": str(declaration.get("value_type") or "text"),
                    "required": bool(declaration.get("required", False)),
                    "writable_by_functions": bool(
                        declaration.get("writable_by_functions", False)
                    ),
                    "options_source": str(declaration.get("options_source") or "none"),
                    "fixed_options": list(declaration.get("fixed_options") or []),
                    "options_variable": str(declaration.get("options_variable") or ""),
                    "origin_functions": [],
                    "from_helper": False,
                    "error": "" if definition else "missing_definition",
                    "unconfirmed": False,
                    "profile_id": profile_id,
                    "event_id": event_id,
                    "task_id": task_id,
                },
            )
            if declaration.get("from_helper"):
                descriptor["from_helper"] = True
            origin = str(declaration.get("function_name") or declaration.get("function_id") or "")
            if origin and origin not in descriptor["origin_functions"]:
                descriptor["origin_functions"].append(origin)

            target_kind = str(declaration.get("field_target") or "")
            scope_id = event_id if target_kind == "event" else task_id
            descriptor["unconfirmed"] = self.variable_service.is_unconfirmed(
                game_id,
                profile_id,
                target_kind,
                scope_id,
                canonical_name,
            )

        for descriptor in merged.values():
            if descriptor["options_source"] != "variable":
                continue
            options_name = descriptor["options_variable"].lstrip("$")
            options_definition = self.variable_service.get_definition(
                game_id,
                options_name,
            )
            if options_definition is None:
                descriptor["error"] = "missing_options_definition"
            elif options_definition.get("value_type") != "collection":
                descriptor["error"] = "invalid_options_collection"

        for descriptor in merged.values():
            descriptor["value"] = self.variable_service.get_context_value(
                game_id,
                profile_id,
                event_id,
                task_id,
                descriptor["name"],
            )
            descriptor["options"] = self._resolve_options(
                game_id,
                profile_id,
                event_id,
                task_id,
                descriptor,
            )
        return sorted(merged.values(), key=lambda field: field["name"].casefold())

    def _resolve_options(
        self,
        game_id: str,
        profile_id: str,
        event_id: str,
        task_id: str,
        descriptor: dict,
    ) -> list:
        source = descriptor.get("options_source")
        if source == "fixed":
            return list(descriptor.get("fixed_options") or [])
        if source != "variable":
            return []

        variable_name = str(descriptor.get("options_variable") or "").lstrip("$")
        definition = self.variable_service.get_definition(game_id, variable_name)
        if definition and definition.get("field_target"):
            value = self.variable_service.get_context_value(
                game_id,
                profile_id,
                event_id,
                task_id,
                variable_name,
            )
        else:
            value = self.variable_service.get_profile_value(
                game_id,
                profile_id,
                variable_name,
            )
        return list(value) if isinstance(value, list) else []