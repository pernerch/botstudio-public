from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import uuid4


@dataclass
class ExecutionContext:
    """Mutable runtime state isolated to one profile/function execution."""

    profile_id: str

    function_id: str

    game_id: str = ""

    variable_service: object | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    event_id: str = ""

    task_id: str = ""

    field_names: set[str] = field(
        default_factory=set,
    )

    execution_id: str = field(
        default_factory=lambda: str(uuid4())
    )

    parent_id: str | None = None

    variables: dict = field(
        default_factory=dict
    )

    local_variables: dict = field(
        default_factory=dict
    )

    readonly_variables: dict = field(
        default_factory=dict
    )

    transient_variable_names: set[str] = field(
        default_factory=set,
        repr=False,
        compare=False,
    )

    variable_accesses: list[dict] = field(
        default_factory=list,
        repr=False,
        compare=False,
    )

    function_executions: list[dict] = field(default_factory=list, repr=False, compare=False)

    cancelled: bool = False

    event_stop_allowed: bool = True

    event_stop_requested: bool = False

    event_reschedule_seconds: float | None = None

    cancellation_checker: object | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    event_due_handler: Callable[[str, float], str] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def is_cancelled(self) -> bool:
        if self.cancelled:
            return True
        if callable(self.cancellation_checker):
            try:
                return bool(self.cancellation_checker())
            except Exception:
                return True
        return False

    def request_event_stop(self, reschedule_seconds: float) -> None:
        self.event_stop_requested = True
        self.event_reschedule_seconds = float(reschedule_seconds)

    def __post_init__(self):
        if self.variable_service and self.game_id and self.profile_id:
            initializer = getattr(
                self.variable_service,
                "initialize_context_values",
                self.variable_service.initialize_profile_values,
            )
            self.variables.update(
                initializer(
                    self.game_id,
                    self.profile_id,
                    event_id=self.event_id,
                    task_id=self.task_id,
                    field_names=self.field_names,
                )
                if hasattr(self.variable_service, "initialize_context_values")
                else initializer(self.game_id, self.profile_id, reset=False)
            )

    @classmethod
    def from_profile(
        cls,
        profile: dict,
        function_id: str,
        profile_globals: dict | None = None,
        variable_service=None,
    ) -> "ExecutionContext":
        profile_id = str(profile.get("botstudio_profile_id") or "")
        if not profile_id:
            raise ValueError("profile is missing botstudio_profile_id")

        readonly = dict(profile_globals or {})
        return cls(
            game_id=str(profile.get("game") or ""),
            profile_id=profile_id,
            function_id=function_id,
            variable_service=variable_service,
            readonly_variables=readonly,
        )

    def get(
        self,
        name: str,
        default=None,
    ):

        name = str(name).lstrip("$")
        if name.startswith("local:"):
            scope = "local"
            variable_name = name[6:]
            value = self.local_variables.get(variable_name, default)
        elif name.startswith("global:"):
            variable_name = self._canonical_profile_name(name[7:])
            scope = self._variable_scope(variable_name)
            value = self.variables.get(variable_name, default)
        elif name in self.readonly_variables:
            scope = "readonly"
            variable_name = name
            value = self.readonly_variables[name]
        elif name in self.local_variables:
            scope = "local"
            variable_name = name
            value = self.local_variables[name]
        else:
            variable_name = self._canonical_profile_name(name)
            scope = self._variable_scope(variable_name)
            value = self.variables.get(variable_name, default)

        self.variable_accesses.append({
            "operation": "read",
            "scope": scope,
            "name": variable_name,
            "value": value,
        })
        return value

    def set(
        self,
        name: str,
        value,
        local: bool = False,
    ) -> None:

        name = str(name).lstrip("$")
        if name in self.readonly_variables:
            raise ValueError(
                f"variable '{name}' is read-only"
            )
        if name.startswith("local:"):
            variable_name = name[6:]
            self.local_variables[variable_name] = value
            self._record_write("local", variable_name, value)
            return
        if name.startswith("global:"):
            name = name[7:]
            if self.variable_service and self.game_id and self.profile_id:
                definition = self.variable_service.get_definition(self.game_id, name)
                name = definition["name"] if definition else name
                try:
                    self._persist_profile_value(name, value)
                except Exception as error:
                    self._record_rejected_write(name, value, error)
                    raise
            self.variables[name] = value
            self._record_write(self._variable_scope(name), name, value)
            return

        target = (
            self.local_variables
            if local
            else self.variables
        )

        if not local and self.variable_service and self.game_id and self.profile_id:
            definition = self.variable_service.get_definition(self.game_id, name)
            name = definition["name"] if definition else name
            try:
                self._persist_profile_value(name, value)
            except Exception as error:
                self._record_rejected_write(name, value, error)
                raise

        target[name] = value
        self._record_write(
            "local" if local else self._variable_scope(name),
            name,
            value,
        )

    def _variable_scope(self, name: str) -> str:
        if not self.variable_service or not self.game_id:
            return "profile"
        definition = self.variable_service.get_definition(self.game_id, name)
        target = definition.get("field_target") if definition else ""
        return f"{target}_field" if target else "profile"

    def _canonical_profile_name(self, name: str) -> str:
        if not self.variable_service or not self.game_id:
            return name
        definition = self.variable_service.get_definition(self.game_id, name)
        return definition["name"] if definition else name

    def _persist_profile_value(self, name: str, value) -> None:
        if name.casefold() in {
            transient_name.casefold()
            for transient_name in self.transient_variable_names
        }:
            return
        writer = getattr(self.variable_service, "set_context_value", None)
        if writer:
            writer(
                self.game_id,
                self.profile_id,
                self.event_id,
                self.task_id,
                name,
                value,
                source="function",
                field_names=self.field_names,
            )
            return
        self.variable_service.set_profile_value(
            self.game_id,
            self.profile_id,
            name,
            value,
            source="function",
        )

    def _record_write(self, scope: str, name: str, value) -> None:
        self.variable_accesses.append({
            "operation": "write",
            "scope": scope,
            "name": name,
            "value": value,
        })

    def _record_rejected_write(self, name: str, value, error: Exception) -> None:
        self.variable_accesses.append({
            "operation": "write_rejected",
            "scope": self._variable_scope(name),
            "name": name,
            "value": value,
            "error": str(error),
        })

    def snapshot(
        self,
    ) -> dict:

        values = dict(self.readonly_variables)
        values.update(self.variables)
        values.update(self.local_variables)

        return values