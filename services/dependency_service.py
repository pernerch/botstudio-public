"""Dependency inventory: which object uses which variable/function/library entry.

The service scans saved functions and scheduler events once per request and
builds forward and reverse references. The GUI never scans by itself; all
fachliche rules (what counts as usage, what is protected) live here.
"""

from __future__ import annotations

import re

from services.function_service import FUNCTION_KIND_HELPER, FunctionService
from services.game_service import GameService
from services.library_service import LibraryService
from services.logging_service import get_logger
from services.variable_service import VariableService
from scheduler.models import SchedulerService


class DependencyService:
    """Builds usage maps for variables, functions and library entries."""

    _VARIABLE_TOKEN = re.compile(r"\$([A-Za-z0-9_.:-]+)")

    def __init__(
        self,
        function_service=None,
        variable_service=None,
        library_service=None,
        scheduler_service=None,
    ):
        self.logger = get_logger("function")
        self.variable_service = variable_service or VariableService()
        self.library_service = library_service or LibraryService()
        self.function_service = function_service or FunctionService(
            library_service=self.library_service,
            variable_service=self.variable_service,
        )
        self.scheduler_service = scheduler_service or SchedulerService(
            variable_service=self.variable_service,
        )

    # -- scanning helpers ------------------------------------------------------

    def _scan_variable_names(self, function) -> set[str]:
        """Collect $-referenced and Configure Field declared variable names."""

        names = set()
        for block in function.walk_blocks():
            params = block.params or {}
            if block.block_type == "configure_field":
                declared = str(params.get("variable_name") or "").strip().lstrip("$")
                if declared:
                    names.add(declared)
            for value in params.values():
                names.update(self._extract_variable_tokens(value))
            if block.output_as:
                output = str(block.output_as).strip().lstrip("$")
                if output:
                    names.add(output)
        return names

    @classmethod
    def _extract_variable_tokens(cls, value) -> set[str]:
        if isinstance(value, str):
            return {
                match.group(1)
                for match in cls._VARIABLE_TOKEN.finditer(value)
            }
        if isinstance(value, dict):
            found = set()
            for item in value.values():
                found.update(cls._extract_variable_tokens(item))
            return found
        if isinstance(value, (list, tuple)):
            found = set()
            for item in value:
                found.update(cls._extract_variable_tokens(item))
            return found
        return set()

    @staticmethod
    def _profile_names() -> dict:
        """profile_id -> display name; users must recognise profiles by name."""

        try:
            from services.profile_service import ProfileService

            profiles = ProfileService().load_all()
        except Exception:
            return {}
        names = {}
        for profile in profiles:
            name = profile.get("name") or ""
            for key in ("botstudio_profile_id", "id"):
                if profile.get(key):
                    names[profile[key]] = name
        return names

    @staticmethod
    def _disabled_profile_ids() -> set:
        """Disabled profiles are hidden in the scheduler; users must be told."""

        try:
            from services.profile_service import ProfileService

            profiles = ProfileService().load_all()
        except Exception:
            return set()
        disabled = set()
        for profile in profiles:
            if profile.get("enabled", True):
                continue
            for key in ("botstudio_profile_id", "id"):
                if profile.get(key):
                    disabled.add(profile[key])
        return disabled

    # -- public inventory -------------------------------------------------------

    @staticmethod
    def _collect_missing_functions(functions, function_users) -> dict:
        """Referenced function ids that no longer exist (deleted/recreated).

        Returns {function_id: {"label": last known name or "", "users": [...]}};
        call_function blocks keep the callee name as their block label, task
        references only store the id.
        """

        existing = {function.function_id for function in functions}
        labels = {}
        for function in functions:
            for block in function.walk_blocks():
                function_id = block.params.get("function_id")
                if function_id and function_id not in existing:
                    name = str(getattr(block, "name", "") or "").strip()
                    if name and function_id not in labels:
                        labels[function_id] = name
        return {
            function_id: {
                "label": labels.get(function_id, ""),
                "users": list(users),
            }
            for function_id, users in function_users.items()
            if function_id and function_id not in existing
        }

    def build_inventory(self, game_id: str) -> dict:
        """One pass over functions and events; returns forward+reverse maps."""

        functions = self.function_service.list_functions(game_id)
        events = self.scheduler_service.list_events(game_id)
        scheduler_variables = {
            GameService.MULTI_ACCOUNT_VARIABLE.split(":", 1)[1],
            GameService.MULTI_ACCOUNT_PROFILE_ID_VARIABLE.split(":", 1)[1],
        }
        # every event auto-registers an "Event Output" variable under its own
        # name (SchedulerService._register_output_variable) — these are system
        # managed and must never appear as deletable
        event_names = {
            (event.name or "").strip()
            for event in events
            if (event.name or "").strip()
        }
        task_names = {
            (task.name or "").strip()
            for event in events
            for task in event.tasks
            if (task.name or "").strip()
        }

        variable_users = {}
        function_users = {}
        library_users = {}

        def record_usage(target_map, key, user):
            if not key:
                return
            users = target_map.setdefault(key, [])
            if user not in users:
                users.append(user)

        for function in functions:
            user = {"kind": "function", "id": function.function_id, "name": function.name or function.function_id[:8]}
            for name in self._scan_variable_names(function):
                record_usage(variable_users, name, user)
            for helper_id in function.collect_helper_ids():
                record_usage(function_users, helper_id, user)
            for entry_id in function.collect_library_entries():
                record_usage(library_users, entry_id, user)

        profile_names = self._profile_names()
        disabled_profiles = self._disabled_profile_ids()
        for event in events:
            for task in event.tasks:
                for function_id in task.function_ids:
                    user = {
                        "kind": "task",
                        "id": task.task_id,
                        "name": task.name or task.task_id[:8],
                        "event_id": event.event_id,
                        "event_name": event.name or event.event_id[:8],
                        "profile_id": event.profile_id or "",
                        "profile_name": profile_names.get(event.profile_id or "", ""),
                        "profile_enabled": (event.profile_id or "") not in disabled_profiles,
                    }
                    record_usage(function_users, function_id, user)

        missing_functions = self._collect_missing_functions(
            functions, function_users
        )

        return {
            "variables": variable_users,
            "functions": function_users,
            "library": library_users,
            "missing_functions": missing_functions,
            "scheduler_variables": scheduler_variables,
            "event_names": event_names,
            "task_names": task_names,
            "function_names": {
                function.function_id: function.name or function.function_id[:8]
                for function in functions
            },
            "helper_ids": {
                function.function_id
                for function in functions
                if function.kind == FUNCTION_KIND_HELPER
            },
        }

    def variable_audit(self, game_id: str) -> dict:
        """Split registry variables into protected, used and unused groups."""

        inventory = self.build_inventory(game_id)
        declared_names = set()
        for function in self.function_service.list_functions(game_id):
            for block in function.walk_blocks():
                if block.block_type == "configure_field":
                    name = str(block.params.get("variable_name") or "").strip().lstrip("$")
                    if name:
                        declared_names.add(name)

        protected = []
        used = []
        unused = []
        protected_names = (
            declared_names
            | inventory["scheduler_variables"]
            | inventory["event_names"]
            | inventory["task_names"]
        )
        protected_casefold = {name.casefold() for name in protected_names}
        for entry in self.variable_service.list_globals(game_id):
            name = entry.get("name", "")
            users = inventory["variables"].get(name, [])
            record = {
                "name": name,
                "value_type": entry.get("value_type", ""),
                "kind": entry.get("kind", ""),
                "users": users,
                "protected_reason": "",
            }
            is_event_output = str(entry.get("description") or "").startswith(
                "Event Output:"
            )
            if name.casefold() in protected_casefold or is_event_output:
                if name in declared_names:
                    record["protected_reason"] = "configure_field"
                elif is_event_output or name in inventory["event_names"]:
                    record["protected_reason"] = "event_output"
                elif name in inventory["task_names"]:
                    record["protected_reason"] = "task_name"
                else:
                    record["protected_reason"] = "scheduler"
                protected.append(record)
            elif users:
                used.append(record)
            else:
                unused.append(record)

        return {
            "protected": sorted(protected, key=lambda r: r["name"].casefold()),
            "used": sorted(used, key=lambda r: r["name"].casefold()),
            "unused": sorted(unused, key=lambda r: r["name"].casefold()),
        }

    def delete_unused_variable(self, game_id: str, name: str) -> bool:
        """Delete only when the audit says the variable is unused and unprotected."""

        audit = self.variable_audit(game_id)
        allowed = {record["name"] for record in audit["unused"]}
        if name not in allowed:
            self.logger.warning(
                "Variable Delete Rejected",
                game=game_id,
                variable=name,
                reason="variable is used or protected",
            )
            return False
        return self.variable_service.delete_global(game_id, name)

    def delete_unused_function(self, game_id: str, function_id: str) -> bool:
        """Delete a function only when no task and no caller references it."""

        inventory = self.build_inventory(game_id)
        if inventory["functions"].get(function_id):
            self.logger.warning(
                "Function Delete Rejected",
                game=game_id,
                function_id=function_id,
                reason="function is still in use",
            )
            return False
        function = self.function_service.load(game_id, function_id)
        if function is None:
            return False
        return self.function_service.delete(game_id, function_id)

    def delete_unused_library_entry(self, game_id: str, entry_id: str) -> bool:
        """Delete a library entry only when no saved function references it."""

        inventory = self.build_inventory(game_id)
        if inventory["library"].get(entry_id):
            self.logger.warning(
                "Library Delete Rejected",
                game=game_id,
                entry_id=entry_id,
                reason="library entry is still in use",
            )
            return False
        return self.library_service.delete_entry(entry_id)
