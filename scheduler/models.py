from __future__ import annotations

import inspect
import json
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import isfinite
from pathlib import Path
import traceback
from typing import Any, Callable
from uuid import uuid4

from models.bot_function import BotFunction
from models.execution_context import ExecutionContext
from services.file_io import atomic_write_json, read_json
from services.function_block_catalog import collect_field_declarations
from services.logging_service import get_logger
from services.reinitialize_service import get_default_reinitialize_service


DEFAULT_PRIORITY = 50
INIT_PRIORITY = 1000
INTERRUPT_PRIORITY = 0
INTERRUPT_EVENT_NAME = "Interrupt"

# Buffer added on top of an Event Output Variable timer so the scheduler
# never re-triggers in the same instant the function just finished in.
EVENT_VARIABLE_GRACE_SECONDS = 2

# Protected Init Task kinds. "profile_validation" answers only whether the
# desired profile is active; "profile_switch" performs the actual switch.
# Both are surfaced to the user as "Profile Check" / "Profile Switch".
PROFILE_CHECK_KIND = "profile_validation"
PROFILE_SWITCH_KIND = "profile_switch"
PROTECTED_TASK_KINDS = (PROFILE_CHECK_KIND, PROFILE_SWITCH_KIND)
PROTECTED_TASK_NAMES = {
    PROFILE_CHECK_KIND: "Profile Check",
    PROFILE_SWITCH_KIND: "Profile Switch",
}


@dataclass(frozen=True)
class SchedulerIntent:
    """Scheduler decision consumed by runtime orchestration."""

    game_id: str
    profile_id: str
    emulator_instance: str | None


@dataclass
class Task:
    """Execution unit owned by an Event. Holds or references Functions."""

    task_id: str = ""
    name: str = ""
    priority: int = DEFAULT_PRIORITY
    sort_order: int = 0
    function_ids: list[str] = field(default_factory=list)
    protected: bool = False
    kind: str = ""
    enabled: bool = True
    status: str = "idle"
    last_started_at: str | None = None
    last_finished_at: str | None = None
    duration_samples_seconds: list[float] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Task":
        data = data or {}
        return cls(
            task_id=str(data.get("task_id") or uuid4()),
            name=str(data.get("name") or ""),
            priority=int(data.get("priority", DEFAULT_PRIORITY)),
            sort_order=int(data.get("sort_order", 0)),
            function_ids=list(data.get("function_ids") or []),
            protected=bool(data.get("protected", False)),
            kind=str(data.get("kind") or ""),
            enabled=bool(data.get("enabled", True)),
            status=str(data.get("status") or "idle"),
            last_started_at=data.get("last_started_at"),
            last_finished_at=data.get("last_finished_at"),
            duration_samples_seconds=[
                float(value) for value in (data.get("duration_samples_seconds") or [])
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "priority": self.priority,
            "sort_order": self.sort_order,
            "function_ids": list(self.function_ids),
            "protected": self.protected,
            "kind": self.kind,
            "enabled": self.enabled,
            "status": self.status,
            "last_started_at": self.last_started_at,
            "last_finished_at": self.last_finished_at,
            "duration_samples_seconds": list(self.duration_samples_seconds),
        }

    @property
    def active(self) -> bool:
        """A Task without Functions is automatically Inactive (never run)."""

        return self.enabled and bool(self.function_ids)

    @property
    def average_duration_seconds(self) -> float:
        samples = [value for value in self.duration_samples_seconds if value >= 0]
        return sum(samples) / len(samples) if samples else 0.0

    def record_duration(self, started_at: datetime | None, finished_at: datetime) -> None:
        if started_at is None:
            return
        duration = (finished_at - started_at).total_seconds()
        if duration >= 0:
            self.duration_samples_seconds.append(duration)


@dataclass
class Event:
    """Fachlicher Container: Profile besitzen Events, Events besitzen Tasks."""

    event_id: str = ""
    game_id: str = ""
    profile_id: str = ""
    name: str = ""
    enabled: bool = True
    is_init: bool = False
    is_interrupt: bool = False
    priority: int = DEFAULT_PRIORITY
    sort_order: int = 0
    tasks: list[Task] = field(default_factory=list)
    output_variable: str = ""
    schedule_mode: str = "manual"
    interval_seconds: int = 0
    run_at_local: list[str] = field(default_factory=list)
    last_started_at: str | None = None
    last_finished_at: str | None = None
    duration_samples_seconds: list[float] = field(default_factory=list)
    last_executed: list[str] = field(default_factory=list)
    next_due_at: str | None = None
    cooldown_seconds: int = 0
    retry_minutes: int = 0
    status: str = "idle"
    used: list[str] = field(default_factory=list)
    used_by: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Event":
        data = data or {}
        return cls(
            event_id=str(data.get("event_id") or data.get("id") or uuid4()),
            game_id=str(data.get("game_id") or ""),
            profile_id=str(data.get("profile_id") or ""),
            name=str(data.get("name") or ""),
            enabled=bool(data.get("enabled", True)),
            is_init=bool(data.get("is_init", False)),
            is_interrupt=bool(data.get("is_interrupt", False)),
            priority=int(data.get("priority", DEFAULT_PRIORITY)),
            sort_order=int(data.get("sort_order", 0)),
            tasks=[Task.from_dict(entry) for entry in (data.get("tasks") or [])],
            output_variable=str(data.get("output_variable") or ""),
            schedule_mode=str(data.get("schedule_mode") or "manual"),
            interval_seconds=int(data.get("interval_seconds", 0)),
            run_at_local=list(data.get("run_at_local") or []),
            last_started_at=data.get("last_started_at"),
            last_finished_at=data.get("last_finished_at"),
            duration_samples_seconds=[
                float(value) for value in (data.get("duration_samples_seconds") or [])
            ],
            last_executed=list(data.get("last_executed") or []),
            next_due_at=data.get("next_due_at"),
            cooldown_seconds=int(data.get("cooldown_seconds", 0)),
            retry_minutes=int(data.get("retry_minutes", 0)),
            status=str(data.get("status") or "idle"),
            used=list(data.get("used") or []),
            used_by=list(data.get("used_by") or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "game_id": self.game_id,
            "profile_id": self.profile_id,
            "name": self.name,
            "enabled": self.enabled,
            "is_init": self.is_init,
            "is_interrupt": self.is_interrupt,
            "priority": self.priority,
            "sort_order": self.sort_order,
            "tasks": [task.to_dict() for task in self.tasks],
            "output_variable": self.output_variable,
            "schedule_mode": self.schedule_mode,
            "interval_seconds": self.interval_seconds,
            "run_at_local": list(self.run_at_local),
            "last_started_at": self.last_started_at,
            "last_finished_at": self.last_finished_at,
            "duration_samples_seconds": list(self.duration_samples_seconds),
            "last_executed": list(self.last_executed),
            "next_due_at": self.next_due_at,
            "cooldown_seconds": self.cooldown_seconds,
            "retry_minutes": self.retry_minutes,
            "status": self.status,
            "used": list(self.used),
            "used_by": list(self.used_by),
        }

    @property
    def variable_name(self) -> str:
        """Name unter dem dieses Event als normale Runtime-Variable erscheint."""

        return self.output_variable or self.name

    @property
    def average_duration_seconds(self) -> float:
        """Mittlere gemessene Laufzeit; unbekannte Werte fallen auf null."""

        samples = [value for value in self.duration_samples_seconds if value >= 0]
        return sum(samples) / len(samples) if samples else 0.0

    def record_duration(self, started_at: datetime | None, finished_at: datetime) -> None:
        if started_at is None:
            return
        duration = (finished_at - started_at).total_seconds()
        if duration >= 0:
            self.duration_samples_seconds.append(duration)

    @property
    def function_ids(self) -> list[str]:
        """Alle Functions über alle aktiven Tasks in Ausführungsreihenfolge."""

        ids: list[str] = []
        for task in self.ordered_tasks():
            if task.active:
                ids.extend(task.function_ids)
        return ids

    def ordered_tasks(self) -> list[Task]:
        def protected_rank(task: Task) -> int:
            if task.kind in PROTECTED_TASK_KINDS:
                return PROTECTED_TASK_KINDS.index(task.kind)
            return len(PROTECTED_TASK_KINDS)

        return sorted(
            self.tasks,
            key=lambda task: (protected_rank(task), task.sort_order, task.priority, task.name.lower()),
        )

    def protected_task(self, kind: str) -> Task | None:
        return next((task for task in self.tasks if task.kind == kind), None)

    def is_due(self, now: datetime | None = None) -> bool:
        if self.status == "queued":
            return True
        if not self.enabled:
            return False
        if not self.next_due_at:
            if self.last_finished_at and self.status not in {"failed", "timeout"}:
                return False
            return True
        when = self._parse_datetime(self.next_due_at)
        current = now or datetime.now(timezone.utc)
        return when is not None and when <= current

    @staticmethod
    def _parse_datetime(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"
            return datetime.fromisoformat(value)
        except ValueError:
            return None


class SchedulerService:
    def __init__(
        self,
        base_path: str = "data/scheduler",
        reinitialize_service=None,
        logger=None,
        variable_service=None,
        statistics_service=None,
    ):
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
        self._cancelled_events: set[str] = set()
        self.reinitialize_service = (
            reinitialize_service or get_default_reinitialize_service()
        )
        self.logger = logger or get_logger("scheduler")
        self.variable_service = variable_service
        self.statistics_service = statistics_service

    def game_root(self, game_id: str) -> Path:
        return self.base_path / game_id

    @staticmethod
    def create_intent(profile: dict[str, Any]) -> SchedulerIntent:
        profile_id = str(
            profile.get("botstudio_profile_id")
            or profile.get("id")
            or ""
        )
        return SchedulerIntent(
            game_id=str(profile.get("game") or ""),
            profile_id=profile_id,
            emulator_instance=profile.get("emulator_instance"),
        )

    @staticmethod
    def queue_assignments(
        profiles: list[dict],
        capacities: dict[str, int] | None = None,
        preferred_profile_ids: set[str] | None = None,
        due_profile_scores: dict[str, tuple[int, str]] | None = None,
    ) -> dict[str, dict]:
        """Queue shared or over-capacity profiles using current due-work scores."""

        groups: dict[tuple, list[dict]] = {}
        preferred_profile_ids = preferred_profile_ids or set()
        due_profile_scores = due_profile_scores or {}

        def profile_id(profile: dict) -> str:
            return SchedulerService.create_intent(profile).profile_id

        def priority_key(profile: dict):
            return (
                -int(profile.get("priority", 50) or 0),
                str(profile.get("name", "")),
            )

        def due_key(profile: dict):
            task_count, oldest_due = due_profile_scores[profile_id(profile)]
            return (
                -int(profile.get("priority", 50) or 0),
                -int(task_count),
                str(oldest_due),
                str(profile.get("name", "")),
            )

        for profile in profiles:
            instance = profile.get("emulator_instance")
            if instance is None:
                continue
            key = (profile.get("game", ""), instance)
            groups.setdefault(key, []).append(profile)

        queued = {}
        capacities = capacities or {}
        ordered_groups = {}
        game_has_due = {
            game_id: any(
                profile_id(profile)
                in due_profile_scores
                for (candidate_game, _instance), group in groups.items()
                if candidate_game == game_id
                for profile in group
            )
            for game_id in {key[0] for key in groups}
        }
        for key, group in groups.items():
            due_group = [profile for profile in group if profile_id(profile) in due_profile_scores]
            remaining_group = [profile for profile in group if profile_id(profile) not in due_profile_scores]
            if due_group:
                ordered = sorted(due_group, key=due_key) + sorted(
                    remaining_group,
                    key=lambda profile: (
                        0 if profile_id(profile) in preferred_profile_ids else 1,
                        *priority_key(profile),
                    ),
                )
            else:
                ordered = sorted(
                    group,
                    key=lambda profile: (
                        0 if profile_id(profile) in preferred_profile_ids else 1,
                        *priority_key(profile),
                    ),
                )
            ordered_groups[key] = ordered
            owner_name = ordered[0].get("name") or ordered[0].get("id", "")
            for position, profile in enumerate(ordered[1:], start=1):
                intent = SchedulerService.create_intent(profile)
                queued[intent.profile_id] = {
                    "position": position,
                    "ahead": owner_name,
                    "reason": "shared_instance",
                }

        for game_id in {key[0] for key in ordered_groups}:
            capacity = max(1, int(capacities.get(game_id, 1) or 1))
            instance_candidates = [
                (key, ordered[0])
                for key, ordered in ordered_groups.items()
                if key[0] == game_id
            ]
            active_instances = {
                key
                for key, _profile in sorted(
                    instance_candidates,
                    key=lambda item: SchedulerService._queue_instance_sort_key(
                        item[1],
                        game_has_due[game_id],
                        preferred_profile_ids,
                        due_profile_scores,
                    ),
                )[:capacity]
            }
            position = capacity
            for key, profile in sorted(
                instance_candidates,
                key=lambda item: SchedulerService._queue_instance_sort_key(
                    item[1],
                    game_has_due[game_id],
                    preferred_profile_ids,
                    due_profile_scores,
                ),
            ):
                if key in active_instances:
                    continue
                intent = SchedulerService.create_intent(profile)
                queued[intent.profile_id] = {
                    "position": position,
                    "ahead": "capacity",
                    "reason": "capacity_limit",
                }
                position += 1

        return queued

    @staticmethod
    def _queue_instance_sort_key(
        profile: dict,
        game_has_due: bool,
        preferred_profile_ids: set[str],
        due_profile_scores: dict[str, tuple[int, str]],
    ) -> tuple:
        profile_id = SchedulerService.create_intent(profile).profile_id
        priority = -int(profile.get("priority", 50) or 0)
        name = str(profile.get("name", ""))
        score = due_profile_scores.get(profile_id)
        if game_has_due:
            if score is None:
                return (1, priority, 0, "9999", 1, name)
            task_count, oldest_due = score
            return (0, priority, -int(task_count), str(oldest_due), name)
        return (
            0 if profile_id in preferred_profile_ids else 1,
            priority,
            name,
        )

    def event_file(self, game_id: str, event_id: str) -> Path:
        return self.game_root(game_id) / f"{event_id}.json"

    @staticmethod
    def init_event_id(game_id: str, profile_id: str = "") -> str:
        return f"init-{game_id}-{profile_id}" if profile_id else f"init-{game_id}"

    @staticmethod
    def interrupt_event_id(game_id: str, profile_id: str) -> str:
        return f"interrupt-{game_id}-{profile_id}"

    def ensure_interrupt_event(self, game_id: str, profile_id: str) -> Event:
        event_id = self.interrupt_event_id(game_id, profile_id)
        event = self.load_event(game_id, event_id)
        if event is None:
            event = Event(
                event_id=event_id,
                game_id=game_id,
                profile_id=profile_id,
                name=INTERRUPT_EVENT_NAME,
                enabled=True,
                is_interrupt=True,
                priority=INTERRUPT_PRIORITY,
                schedule_mode="manual",
            )
        event.profile_id = profile_id
        event.name = INTERRUPT_EVENT_NAME
        event.enabled = True
        event.is_interrupt = True
        event.priority = INTERRUPT_PRIORITY
        event.schedule_mode = "manual"
        event.next_due_at = None
        return self.save_event(event)

    def ensure_init_event(self, game_id: str, profile_id: str = "", seed_functions: dict | None = None) -> Event:
        """Genau ein Init Event pro Profil; Altlasten werden migriert/entfernt."""

        canonical_id = self.init_event_id(game_id, profile_id)
        init_events = [event for event in self.list_events(game_id) if event.is_init]
        owned = [event for event in init_events if event.profile_id == profile_id]
        if not owned and profile_id:
            # Init ohne Profilbindung stammt aus dem alten Schema und wird adoptiert.
            owned = [event for event in init_events if not event.profile_id]
        owned.sort(key=lambda event: (0 if event.event_id == canonical_id else 1, event.event_id))

        for duplicate in owned[1:]:
            self._remove_event_file(game_id, duplicate.event_id)

        if not owned:
            event = self.create_init_event(game_id, profile_id)
        else:
            event = owned[0]
            if event.event_id != canonical_id:
                self._remove_event_file(game_id, event.event_id)
                event.event_id = canonical_id
            event.profile_id = profile_id

        self._seed_protected_tasks(event, init_events, seed_functions)
        return self.save_event(event)

    def _seed_protected_tasks(self, event: Event, siblings: list[Event], seed_functions: dict | None) -> None:
        """Ein noch unkonfiguriertes Init Event erbt von einem Geschwisterprofil
        desselben Games, ersatzweise aus der Altkonfiguration des Games."""

        configured = [
            task for kind in PROTECTED_TASK_KINDS
            if (task := event.protected_task(kind)) is not None and task.function_ids
        ]
        if configured:
            return

        seed_functions = seed_functions if isinstance(seed_functions, dict) else {}
        for kind in PROTECTED_TASK_KINDS:
            task = event.protected_task(kind)
            if task is None:
                continue
            inherited = next(
                (
                    sibling.protected_task(kind).function_ids
                    for sibling in siblings
                    if sibling.event_id != event.event_id
                    and sibling.protected_task(kind) is not None
                    and sibling.protected_task(kind).function_ids
                ),
                None,
            )
            if inherited is None:
                function_id = str(seed_functions.get(kind) or "")
                inherited = [function_id] if function_id else []
            task.function_ids = list(inherited)


    def _remove_event_file(self, game_id: str, event_id: str) -> None:
        self.event_file(game_id, event_id).unlink(missing_ok=True)

    def create_init_event(self, game_id: str, profile_id: str = "") -> Event:
        return Event(
            event_id=self.init_event_id(game_id, profile_id),
            game_id=game_id,
            profile_id=profile_id,
            name="Init",
            enabled=True,
            is_init=True,
            priority=INIT_PRIORITY,
            schedule_mode="manual",
            next_due_at=None,
            tasks=[
                Task(kind=kind, name=name, protected=True)
                for kind, name in PROTECTED_TASK_NAMES.items()
            ],
        )

    def save_event(
        self,
        event: Event,
        variable_service=None,
        register_output_variable: bool = True,
    ) -> Event:
        event.game_id = event.game_id or ""
        if event.is_interrupt:
            event.name = INTERRUPT_EVENT_NAME
            event.enabled = True
            event.priority = INTERRUPT_PRIORITY
            event.schedule_mode = "manual"
            event.interval_seconds = 0
            event.run_at_local = []
            event.next_due_at = None
            event.cooldown_seconds = 0
            event.retry_minutes = 0
        if event.is_init:
            event.enabled = True
            event.priority = INIT_PRIORITY
            existing = {task.kind: task for task in event.tasks if task.kind in PROTECTED_TASK_KINDS}
            protected_tasks = [
                Task(
                    task_id=existing[kind].task_id if kind in existing else "",
                    kind=kind,
                    name=name,
                    protected=True,
                    function_ids=existing[kind].function_ids if kind in existing else [],
                    sort_order=index,
                )
                for index, (kind, name) in enumerate(PROTECTED_TASK_NAMES.items())
            ]
            extra_tasks = [task for task in event.tasks if task.kind not in PROTECTED_TASK_KINDS]
            event.tasks = protected_tasks + extra_tasks
        if not event.event_id:
            event.event_id = str(uuid4())
        if not event.name:
            event.name = event.event_id
        for task in event.tasks:
            if not task.task_id:
                task.task_id = str(uuid4())
        event_path = self.event_file(event.game_id, event.event_id)
        try:
            atomic_write_json(event_path, event.to_dict())
        except (OSError, TypeError, ValueError) as error:
            self.logger.error(
                "Scheduler Event Persistence Failed",
                game=event.game_id,
                event_id=event.event_id,
                error=repr(error),
                next_action="keep runtime state in memory and retry on next scheduler cycle",
            )
            return event
        if register_output_variable and not event.is_init and event.name:
            self._register_output_variable(event, variable_service or self.variable_service)
        if self.reinitialize_service:
            self.reinitialize_service.publish(
                "scheduler",
                game_id=event.game_id,
                event_id=event.event_id,
            )
        return event

    def _register_output_variable(self, event: Event, variable_service=None) -> None:
        """Jedes Event erhält automatisch eine eigene Runtime-Variable."""

        if variable_service is None or not hasattr(variable_service, "ensure_global"):
            return
        name = event.variable_name.strip()
        if not name:
            return
        variable_service.ensure_global(
            event.game_id,
            name=name,
            description=f"Event Output: {event.name}",
        )

    def load_event(self, game_id: str, event_id: str) -> Event | None:
        file = self.event_file(game_id, event_id)
        if not file.exists():
            return None
        try:
            return Event.from_dict(read_json(file))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            self.logger.error(
                "Scheduler Event Load Failed",
                game=game_id,
                event_id=event_id,
                error=repr(error),
                next_action="skip malformed event and continue scheduler",
            )
            return None

    def delete_event(self, game_id: str, event_id: str) -> bool:
        file = self.event_file(game_id, event_id)
        if not file.exists():
            return False
        event = self.load_event(game_id, event_id)
        if event and (event.is_init or event.is_interrupt):
            return False
        file.unlink(missing_ok=True)
        if self.reinitialize_service:
            self.reinitialize_service.publish(
                "scheduler",
                game_id=game_id,
                event_id=event_id,
                deleted=True,
            )
        return True

    def sync_profile_events(
        self,
        game_id: str,
        source_profile_id: str,
        target_profile_ids: list[str],
        now: datetime | None = None,
        excluded_event_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Replace target custom events with fresh copies from a source profile.

        Excluded source events are not copied; target events with the same
        name stay untouched instead of being replaced.
        """

        if not game_id or not source_profile_id:
            raise ValueError("game and source profile are required")

        target_ids = list(dict.fromkeys(
            str(profile_id)
            for profile_id in target_profile_ids
            if profile_id and str(profile_id) != str(source_profile_id)
        ))
        if not target_ids:
            raise ValueError("select at least one target profile")

        current = now or datetime.now(timezone.utc)
        source_all = self.list_events(game_id, profile_id=source_profile_id)
        excluded_ids = {str(event_id) for event_id in (excluded_event_ids or []) if event_id}
        source_custom = [
            event
            for event in source_all
            if not event.is_init and not event.is_interrupt
        ]
        excluded_names = {
            event.name.casefold()
            for event in source_custom
            if event.event_id in excluded_ids
        }
        source_events = [
            event for event in source_custom if event.event_id not in excluded_ids
        ]
        source_init = next(
            (event for event in source_all if event.is_init and event.profile_id == source_profile_id),
            None,
        )
        if any(event.status in {"running", "queued"} for event in source_events):
            raise ValueError("source profile has an event running or queued")

        existing_by_target = {}
        init_by_target: dict[str, Event | None] = {}
        for target_id in target_ids:
            target_events = self.list_events(game_id, profile_id=target_id)
            custom_events = [
                event
                for event in target_events
                if not event.is_init
                and not event.is_interrupt
                and event.name.casefold() not in excluded_names
            ]
            target_init = next(
                (event for event in target_events if event.is_init and event.profile_id == target_id),
                None,
            )
            if any(
                event.status in {"running", "queued"}
                for event in custom_events + ([target_init] if target_init else [])
            ):
                raise ValueError(f"target profile {target_id} has an event running or queued")
            existing_by_target[target_id] = custom_events
            init_by_target[target_id] = target_init

        copies_by_target: dict[str, list[Event]] = {}
        for target_id in target_ids:
            copies = []
            for source_event in source_events:
                copied = deepcopy(source_event)
                copied.event_id = str(uuid4())
                copied.profile_id = target_id
                copied.status = "idle"
                copied.last_started_at = None
                copied.last_finished_at = None
                copied.duration_samples_seconds = []
                copied.last_executed = []
                copied.used_by = []
                if copied.schedule_mode == "fixed" and copied.run_at_local:
                    copied.next_due_at = self._next_run_at_local(current, copied.run_at_local)
                else:
                    copied.next_due_at = None

                for task in copied.tasks:
                    self._reset_synced_task(task)
                copies.append(copied)
            copies_by_target[target_id] = copies

        source_init_tasks = [
            task for task in (source_init.tasks if source_init else [])
            if task.kind not in PROTECTED_TASK_KINDS and not task.protected
        ]
        saved_copies: list[tuple[str, str]] = []
        replaced_inits: list[Event] = []
        try:
            for target_id, copies in copies_by_target.items():
                for copied in copies:
                    self.save_event(copied)
                    if self.load_event(game_id, copied.event_id) is None:
                        raise OSError(f"failed to persist copied event {copied.event_id}")
                    saved_copies.append((target_id, copied.event_id))
            if source_init is not None:
                for target_id in target_ids:
                    original = init_by_target.get(target_id)
                    if original is None:
                        original = self.ensure_init_event(game_id, target_id)
                    replaced_inits.append(deepcopy(original))
                    target_init = deepcopy(original)
                    extra_tasks = []
                    for task in source_init_tasks:
                        copied_task = deepcopy(task)
                        self._reset_synced_task(copied_task)
                        extra_tasks.append(copied_task)
                    target_init.tasks = [
                        task for task in target_init.tasks
                        if task.kind in PROTECTED_TASK_KINDS or task.protected
                    ] + extra_tasks
                    self.save_event(target_init)
        except Exception:
            for target_id, event_id in saved_copies:
                self.delete_event(game_id, event_id)
            for original in replaced_inits:
                self.save_event(original)
            raise

        replaced_count = 0
        for target_id, old_events in existing_by_target.items():
            for old_event in old_events:
                if not self.delete_event(game_id, old_event.event_id):
                    raise OSError(f"failed to replace event {old_event.event_id}")
                replaced_count += 1

        return {
            "source_profile_id": str(source_profile_id),
            "target_profile_ids": target_ids,
            "copied_events": len(source_events) * len(target_ids),
            "replaced_events": replaced_count,
            "copied_init_tasks": len(source_init_tasks) * len(replaced_inits),
            "skipped_events": len(source_custom) - len(source_events),
        }

    def set_events_enabled(
        self,
        game_id: str,
        enabled: bool,
        profile_id: str | None = None,
    ) -> int:
        """Switch all custom events of one profile (or all profiles) on/off.

        Init and interrupt events are never touched. Returns the number of
        events whose state actually changed.
        """

        if not game_id:
            return 0
        changed = 0
        for event in self.list_events(game_id, profile_id=profile_id):
            if event.is_init or event.is_interrupt:
                continue
            if profile_id and event.profile_id != profile_id:
                continue
            if bool(event.enabled) == bool(enabled):
                continue
            event.enabled = bool(enabled)
            self.save_event(event, register_output_variable=False)
            changed += 1
        self.logger.info(
            "Scheduler events bulk toggled",
            game_id=game_id,
            profile_id=profile_id or "*",
            enabled=bool(enabled),
            changed=changed,
        )
        return changed

    def create_event_for_profiles(
        self,
        template: Event,
        profile_ids: list[str],
        task_name: str = "",
        function_ids: list[str] | None = None,
        variable_service=None,
    ) -> list[Event]:
        """Create one independent copy of ``template`` per profile.

        Every copy gets its own ids and, when functions are given, one task
        with those functions. The event output variable is registered once.
        """

        targets = list(dict.fromkeys(str(pid) for pid in profile_ids if pid))
        if not targets:
            raise ValueError("select at least one profile")
        functions = [str(fid) for fid in (function_ids or []) if fid]
        created: list[Event] = []
        try:
            for profile_id in targets:
                event = deepcopy(template)
                event.event_id = str(uuid4())
                event.profile_id = profile_id
                event.tasks = []
                if functions:
                    event.tasks.append(
                        Task(
                            name=(task_name or event.name).strip() or event.name,
                            function_ids=list(functions),
                            priority=event.priority,
                        )
                    )
                self.save_event(event, variable_service=variable_service)
                created.append(event)
        except Exception:
            for event in created:
                self.delete_event(event.game_id, event.event_id)
            raise
        return created

    @staticmethod
    def _reset_synced_task(task: Task) -> None:
        task.task_id = str(uuid4())
        task.status = "idle"
        task.last_started_at = None
        task.last_finished_at = None
        task.duration_samples_seconds = []

    def cancel_event(self, event_id: str) -> bool:
        if not event_id:
            return False
        self._cancelled_events.add(event_id)
        return True

    def clear_event_cancellation(self, event_id: str) -> None:
        self._cancelled_events.discard(event_id)

    def list_events(self, game_id: str, profile_id: str | None = None) -> list[Event]:
        game_root = self.game_root(game_id)
        if not game_root.exists():
            return []
        events: list[Event] = []
        for file in sorted(game_root.glob("*.json")):
            try:
                payload = read_json(file)
            except (OSError, ValueError, TypeError):
                continue
            event = Event.from_dict(payload)
            if profile_id and event.profile_id not in ("", profile_id):
                continue
            if profile_id is None:
                events.append(event)
                continue
            if event.profile_id == "" or event.profile_id == profile_id:
                events.append(event)

        if profile_id and any(event.is_init and event.profile_id == profile_id for event in events):
            # ein profilgebundenes Init verdrängt das profillose Alt-Init
            events = [
                event
                for event in events
                if not (event.is_init and event.profile_id == "")
            ]

        def event_sort_key(event: Event):
            is_init_rank = 0 if event.is_init else 1
            return (
                is_init_rank,
                event.sort_order if not event.is_init else 0,
                event.priority,
                event.name.lower(),
                event.event_id,
            )

        return sorted(events, key=event_sort_key)

    def list_due_events(
        self,
        game_id: str,
        profile_id: str | None = None,
        now: datetime | None = None,
        overdue_only: bool = False,
    ) -> list[Event]:
        events = [
            event for event in self.list_events(game_id, profile_id)
            if event.is_due(now)
        ]
        if not overdue_only:
            return events
        current = now or datetime.now(timezone.utc)
        return [
            event
            for event in events
            if event.status == "queued"
            or not event.next_due_at
            or (
                (due := event._parse_datetime(event.next_due_at)) is not None
                and due <= current
            )
        ]

    def queue_event_now(
        self,
        event: Event,
        now: datetime | None = None,
    ) -> Event:
        if not event:
            raise ValueError("event is required")
        if event.is_init or event.is_interrupt:
            raise PermissionError("system events cannot be queued")
        if event.status in {"queued", "running", "cancelling"}:
            raise ValueError("event is already queued or running")
        if not event.function_ids:
            raise ValueError("event has no active tasks")

        queued_at = (now or datetime.now(timezone.utc)).isoformat()
        event.status = "queued"
        event.next_due_at = queued_at
        self.save_event(event)
        persisted = self.load_event(event.game_id, event.event_id)
        if (
            persisted is None
            or persisted.status != "queued"
            or persisted.next_due_at != queued_at
        ):
            raise OSError(f"failed to persist queue request for event '{event.name}'")
        self.logger.info(
            "Scheduler Event Queued",
            game=event.game_id,
            profile_id=event.profile_id,
            event_id=event.event_id,
            event_name=event.name,
            queued_at=queued_at,
        )
        return persisted

    def _resolve_profile_event(self, game_id: str, profile_id: str, target: str) -> Event:
        """Resolve an event target strictly inside the executing profile.

        Functions are shared per game, so a block may hold the event ID of
        another profile (e.g. the sync source). Such IDs map to the event with
        the same name in the executing profile and never to the foreign event.
        """

        profile_events = self.list_events(game_id, profile_id)
        for candidate in profile_events:
            if candidate.event_id == target:
                return candidate
        name = target
        foreign = self.load_event(game_id, target)
        if foreign is not None:
            name = foreign.name
        matches = [
            candidate for candidate in profile_events
            if candidate.name.strip().casefold() == name.strip().casefold()
        ]
        if len(matches) != 1:
            raise ValueError(f"target event '{name}' is missing or ambiguous in this account")
        return matches[0]

    def set_event_due(
        self,
        game_id: str,
        profile_id: str,
        target: str,
        delay_seconds: float = 0,
        current_event_id: str = "",
        current_event_due: Callable[[str], None] | None = None,
    ) -> str:
        if not game_id or not profile_id or not str(target).strip():
            raise ValueError("game, account and target event are required")
        delay = float(delay_seconds)
        if not isfinite(delay) or delay < 0:
            raise ValueError("reschedule delay must be finite and nonnegative seconds")
        event = self._resolve_profile_event(game_id, profile_id, str(target).strip())
        if event.profile_id != profile_id:
            raise PermissionError("target event belongs to another account")
        if event.is_init or event.is_interrupt or not event.enabled:
            raise PermissionError("target event is protected or disabled")
        if event.event_id == current_event_id:
            if current_event_due is None:
                raise ValueError("the running event cannot reschedule itself here")
            # The running event applies this due date when it finishes successfully.
            due_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            current_event_due(due_at)
            self.logger.info(
                "Scheduler Event Self Rescheduled",
                game=game_id, profile_id=profile_id,
                event_id=event.event_id, event_name=event.name, next_due_at=due_at,
            )
            return due_at
        if event.status == "running":
            raise ValueError("target event is currently running")
        if not event.function_ids:
            raise ValueError("target event has no active functions")
        due_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
        event.next_due_at = due_at
        self._persist_event_schedule(event)
        self.logger.info(
            "Scheduler Event Rescheduled",
            game=game_id, profile_id=profile_id,
            event_id=event.event_id, event_name=event.name, next_due_at=due_at,
        )
        return due_at

    def prepare_profile_start(self, game_id: str, profile_id: str) -> None:
        for event in self.list_events(game_id, profile_id):
            if (
                event.enabled and not event.is_init and not event.is_interrupt
                and event.function_ids and not event.next_due_at
                and event.status not in {"failed", "timeout", "running"}
            ):
                event.next_due_at = datetime.now(timezone.utc).isoformat()
                self._persist_event_schedule(event)

    def _persist_event_schedule(self, event: Event) -> None:
        self.save_event(event, register_output_variable=False)
        persisted = self.load_event(event.game_id, event.event_id)
        if persisted is None or persisted.next_due_at != event.next_due_at:
            raise OSError(f"failed to persist schedule for event '{event.name}'")

    @staticmethod
    def _retry_due_at(event: Event, current: datetime) -> str:
        return (current + timedelta(minutes=max(event.retry_minutes, 1))).isoformat()

    def list_execution_events(
        self,
        game_id: str,
        profile_id: str | None = None,
        now: datetime | None = None,
        overdue_only: bool = False,
        priority_first: bool = False,
    ) -> list[Event]:
        due_events = self.list_due_events(game_id, profile_id, now, overdue_only)

        def execution_sort_key(event: Event):
            order = (
                -event.priority
                if priority_first and not event.is_init
                else event.sort_order if not event.is_init else 0
            )
            queued_at = Event._parse_datetime(event.next_due_at)
            if queued_at is not None:
                if queued_at.tzinfo is None:
                    queued_at = queued_at.replace(tzinfo=timezone.utc)
                queued_at = queued_at.astimezone(timezone.utc).timestamp()
            else:
                queued_at = float("-inf")
            return (
                0 if event.is_init else 1,
                0 if event.status == "queued" else 1,
                queued_at if event.status == "queued" else 0,
                order,
                event.sort_order,
                event.name.lower(),
                event.event_id,
            )

        return sorted(
            (event for event in due_events if not event.is_interrupt),
            key=execution_sort_key,
        )

    def run_due_events(
        self,
        game_id: str,
        profile: dict | None = None,
        profile_id: str | None = None,
        function_root: str | None = None,
        runner_factory=None,
        variable_service=None,
        now: datetime | None = None,
        limit: int | None = None,
        persist: bool = True,
        overdue_only: bool = False,
        priority_first: bool = False,
        cancellation_checker=None,
    ) -> list[dict]:
        if limit is not None and limit <= 0:
            return []

        profile = profile or {}
        profile_id = profile_id or profile.get("botstudio_profile_id") or profile.get("id")
        if not profile_id:
            self.logger.warning(
                "Scheduler Run Skipped",
                game=game_id,
                reason="profile_missing",
            )
            return []
        results = []
        initial_events = self.list_execution_events(
            game_id,
            profile_id=profile_id,
            now=now,
            overdue_only=overdue_only,
            priority_first=priority_first,
        )
        initial_event_ids = {event.event_id for event in initial_events}
        self.logger.info(
            "Scheduler Run Started",
            game=game_id,
            profile=profile.get("name"),
            profile_id=profile_id,
            selected_event_ids=[
                event.event_id for event in initial_events if not event.is_init
            ],
            limit=limit,
        )

        processed_event_ids = set()
        processed_queued_ids = set()
        while limit is None or len(results) < limit:
            events = self.list_execution_events(
                game_id,
                profile_id=profile_id,
                now=now,
                overdue_only=overdue_only,
                priority_first=priority_first,
            )
            event = next(
                (
                    candidate for candidate in events
                    if (
                        candidate.status == "queued"
                        and candidate.event_id not in processed_queued_ids
                    )
                    or (
                        candidate.event_id in initial_event_ids
                        and candidate.event_id not in processed_event_ids
                    )
                ),
                None,
            )
            if event is None:
                deferred_queued = [
                    candidate.event_id
                    for candidate in events
                    if candidate.status == "queued"
                    and candidate.event_id in processed_queued_ids
                ]
                if deferred_queued:
                    self.logger.info(
                        "Scheduler Queued Events Deferred",
                        game=game_id,
                        profile_id=profile_id,
                        event_ids=deferred_queued,
                        reason="already queued once in this scheduler cycle",
                    )
                break

            if persist:
                # Earlier events may have postponed this target after selection.
                latest = self.load_event(game_id, event.event_id)
                if latest is None or not latest.is_due(now):
                    continue
                event = latest
            was_queued = event.status == "queued"
            processed_event_ids.add(event.event_id)
            if was_queued:
                processed_queued_ids.add(event.event_id)

            if event.is_init or event.is_interrupt or not event.function_ids:
                self.logger.debug(
                    "Scheduler Event Skipped",
                    game=game_id,
                    profile=profile.get("name"),
                    event_id=event.event_id,
                    reason=(
                        "init_event"
                        if event.is_init
                        else "interrupt_event"
                        if event.is_interrupt
                        else "no_active_tasks"
                    ),
                )
                continue
            try:
                results.append(
                    self.run_event(
                        event,
                        profile=profile,
                        function_root=function_root,
                        runner_factory=runner_factory,
                        variable_service=variable_service,
                        now=now,
                        persist=persist,
                        cancellation_checker=lambda event_id=event.event_id: (
                            event_id in self._cancelled_events
                            or (cancellation_checker is not None and cancellation_checker())
                        ),
                    )
                )
            except Exception as error:
                finished_at = datetime.now(timezone.utc)
                event.status = "failed"
                event.last_finished_at = finished_at.isoformat()
                event.next_due_at = self._retry_due_at(event, finished_at)
                self.save_event(event, variable_service=variable_service)
                self.logger.error(
                    "Scheduler Event Exception Isolated",
                    game=game_id,
                    profile=profile.get("name"),
                    event_id=event.event_id,
                    error=repr(error),
                    next_action="continue with next due event",
                )
                results.append({
                    "event_id": event.event_id,
                    "executed": [],
                    "status": "failed",
                    "error": repr(error),
                })
            finally:
                self.clear_event_cancellation(event.event_id)
        self.logger.info(
            "Scheduler Run Finished",
            game=game_id,
            profile=profile.get("name"),
            profile_id=profile_id,
            result_count=len(results),
            results=results,
        )
        return results

    def mark_event_result(self, event: Event, result: str, now: datetime | None = None) -> Event:
        current = now or datetime.now(timezone.utc)
        event.last_finished_at = current.isoformat()
        event.status = result
        if result == "success":
            event.next_due_at = (
                (current + timedelta(seconds=event.cooldown_seconds)).isoformat()
                if event.cooldown_seconds > 0 else None
            )
        elif result in {"failed", "timeout"}:
            event.next_due_at = self._retry_due_at(event, current)
        else:
            event.next_due_at = None
        return self.save_event(event)

    def run_event(
        self,
        event: Event,
        profile: dict | None = None,
        function_root: str | None = None,
        runner_factory=None,
        variable_service=None,
        now: datetime | None = None,
        cancellation_checker=None,
        persist: bool = True,
    ) -> dict:
        if not event or not event.function_ids:
            return {"event_id": event.event_id if event else "", "executed": [], "status": "idle"}
        if not profile or not profile.get("game"):
            event.status = "failed"
            event.last_finished_at = (now or datetime.now(timezone.utc)).isoformat()
            event.next_due_at = (
                self._retry_due_at(event, now or datetime.now(timezone.utc))
                if not event.is_init and not event.is_interrupt else None
            )
            if persist:
                self.save_event(event, variable_service=variable_service)
            return {
                "event_id": event.event_id,
                "executed": [],
                "status": "failed",
                "error": "profile game is required",
            }

        if runner_factory is None:
            event.status = "failed"
            event.last_finished_at = (now or datetime.now(timezone.utc)).isoformat()
            event.next_due_at = (
                self._retry_due_at(event, now or datetime.now(timezone.utc))
                if not event.is_init and not event.is_interrupt else None
            )
            if persist:
                self.save_event(event, variable_service=variable_service)
            self.logger.error(
                "Scheduler Event Failed",
                game=event.game_id,
                profile=profile.get("name"),
                event_id=event.event_id,
                reason="runner_not_configured",
            )
            return {
                "event_id": event.event_id,
                "executed": [],
                "status": "failed",
                "error": "runtime runner is not configured",
            }

        current = now or datetime.now(timezone.utc)
        save_state = (
            (
                lambda ignored_event: self.save_event(
                    ignored_event,
                    variable_service=variable_service,
                    register_output_variable=False,
                )
            )
            if persist
            else (lambda ignored_event: ignored_event)
        )
        event.last_started_at = current.isoformat()
        event.status = "running"
        self.logger.info(
            "Scheduler Event Started",
            game=event.game_id,
            profile=profile.get("name"),
            event_id=event.event_id,
            task_ids=[task.task_id for task in event.ordered_tasks() if task.active],
            priority=event.priority,
        )

        executed: list[str] = []
        event_stop_seconds: float | None = None
        self_due_at: list[str] = []
        game_id = str(profile.get("game") or "")
        profile_uid = str(profile.get("botstudio_profile_id") or profile.get("id") or "")
        function_base = Path(function_root or "data/functions") / game_id

        def declared_fields(task: Task) -> tuple[set[str], set[str]]:
            event_names = set()
            task_names = set()
            for function_id in task.function_ids:
                function_file = function_base / f"{function_id}.json"
                try:
                    function = BotFunction.from_dict(read_json(function_file))
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    continue
                for declaration in collect_field_declarations(function):
                    name = str(declaration.get("variable_name") or "").strip().lstrip("$")
                    if not name:
                        continue
                    if variable_service is not None and hasattr(
                        variable_service,
                        "get_definition",
                    ):
                        definition = variable_service.get_definition(
                            game_id,
                            name,
                        )
                        if isinstance(definition, dict):
                            name = definition.get("name") or name
                    if declaration.get("field_target") == "event":
                        event_names.add(name)
                    elif declaration.get("field_target") == "task":
                        task_names.add(name)
            return event_names, task_names

        event_field_names = set()
        task_field_names = {}
        for scoped_task in event.ordered_tasks():
            scoped_event_names, scoped_task_names = declared_fields(scoped_task)
            event_field_names.update(scoped_event_names)
            if scoped_task.active:
                task_field_names[scoped_task.task_id] = scoped_task_names

        def execute_task(task, negative_is_routing: bool = False) -> str:
            """Führt die Functions eines Tasks aus und liefert dessen Endstatus.

            negative_is_routing: ein reguläres negatives Function-Ergebnis ist
            eine Entscheidung (z. B. Profile Check "anderes Profil aktiv") und
            kein Fehler. Status bleibt "failed" für den Aufrufer, Statistik und
            Fehlerlog werten es aber als erfolgreiche Ausführung.
            """

            nonlocal event_stop_seconds

            if not task.active:
                return "success"

            task_started_at = datetime.now(timezone.utc)
            task.last_started_at = task_started_at.isoformat()
            task.status = "running"
            save_state(event)

            failure_context = {}

            def finish_task(status: str, routed: bool = False) -> str:
                task.status = "success" if routed else status
                task_finished_at = datetime.now(timezone.utc)
                task.record_duration(task_started_at, task_finished_at)
                task.last_finished_at = task_finished_at.isoformat()
                counters = None
                if self.statistics_service is not None and status != "cancelled":
                    try:
                        counters = self.statistics_service.record_task(
                            game_id,
                            profile_uid,
                            str(profile.get("name") or profile_uid),
                            task.task_id,
                            task.name,
                            "success" if status == "success" or routed else "failure",
                            (task_finished_at - task_started_at).total_seconds(),
                        )
                    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
                        self.logger.error(
                            "Scheduler Task Statistics Failed",
                            event_name=event.name,
                            task_name=task.name,
                            error_type=type(error).__name__,
                            next_action="inspect statistics storage; counts unavailable",
                        )
                if status in {"failed", "timeout"} and not routed:
                    self.logger.error(
                        "Scheduler Task Failed",
                        game=event.game_id,
                        profile=str(profile.get("name") or profile_uid),
                        event_id=event.event_id,
                        event_name=event.name or event.event_id,
                        task_id=task.task_id,
                        task_name=task.name or task.task_id,
                        **failure_context,
                        **(counters if isinstance(counters, dict) else {}),
                    )
                return status

            for function_id in task.function_ids:
                failure_context = {"function_id": function_id, "function_name": function_id}
                if cancellation_checker and cancellation_checker():
                    return finish_task("cancelled")

                function_file = Path(function_root or "data/functions") / game_id / f"{function_id}.json"
                if not function_file.exists():
                    failure_context["failure_reason"] = "function_file_missing"
                    self.logger.warning(
                        "Scheduler Task Failure Details",
                        game=event.game_id,
                        profile=profile.get("name"),
                        event_id=event.event_id,
                        task_id=task.task_id,
                        function_id=function_id,
                        reason="function_file_missing",
                        check=str(function_file),
                        next_action="reassign the function to this task",
                    )
                    return finish_task("failed")
                try:
                    function = BotFunction.from_dict(read_json(function_file))
                except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
                    failure_context["failure_reason"] = "function_load_failed"
                    self.logger.warning(
                        "Scheduler Function Load Failed",
                        game=event.game_id,
                        profile=profile.get("name"),
                        event_id=event.event_id,
                        task_id=task.task_id,
                        function_id=function_id,
                        error=repr(error),
                        next_action="mark task failed and continue scheduler",
                    )
                    return finish_task("failed")
                failure_context["function_name"] = function.name or function.function_id
                if not function.saved:
                    failure_context["failure_reason"] = "function_not_saved"
                    self.logger.warning(
                        "Scheduler Task Failure Details",
                        game=event.game_id,
                        profile=profile.get("name"),
                        event_id=event.event_id,
                        task_id=task.task_id,
                        function_id=function_id,
                        reason="function_not_saved",
                        next_action="save the function in the Function Editor",
                    )
                    return finish_task("failed")

                context = None
                try:
                    runner = runner_factory()
                    if runner is not None and hasattr(runner, "run"):
                        context = ExecutionContext(
                            game_id=game_id,
                            profile_id=profile_uid,
                            function_id=function.function_id,
                            variable_service=variable_service or profile.get("variable_service"),
                            event_id=event.event_id,
                            task_id=task.task_id,
                            field_names=(
                                event_field_names
                                | task_field_names.get(task.task_id, set())
                            ),
                            cancellation_checker=cancellation_checker,
                            event_stop_allowed=not event.is_init and not event.is_interrupt,
                            event_due_handler=(
                                lambda target, delay: self.set_event_due(
                                    game_id, profile_uid, target, delay,
                                    current_event_id=event.event_id,
                                    current_event_due=self_due_at.append,
                                )
                            ) if persist else None,
                        )
                        target = getattr(runner.run, "side_effect", None) or runner.run
                        try:
                            params = inspect.signature(target).parameters.values()
                        except (TypeError, ValueError):
                            params = []

                        accepts_context = any(
                            p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
                            or p.name == "context"
                            for p in params
                        )
                        if accepts_context:
                            result = runner.run(function, context, profile)
                        else:
                            result = runner.run(function, profile=profile)
                    else:
                        result = {"outcome": "failed", "error": "runtime runner is not configured"}
                except Exception as error:
                    failure_context["failure_reason"] = type(error).__name__
                    self.logger.warning(
                        "Scheduler Function Execution Failed",
                        game=event.game_id,
                        profile=profile.get("name"),
                        event_id=event.event_id,
                        task_id=task.task_id,
                        function_id=function_id,
                        error=repr(error),
                        traceback="".join(
                            traceback.format_exception(type(error), error, error.__traceback__)
                        )[-4000:],
                        next_action="mark task failed and continue scheduler",
                    )
                    return finish_task("failed")

                executed.append(function_id)
                outcome = str(result.get("outcome") or "failed").lower()

                switch_task = event.protected_task(PROFILE_SWITCH_KIND) if event.is_init else None
                if (
                    self.statistics_service is not None
                    and switch_task is not None
                    and task.kind != PROFILE_SWITCH_KIND
                    and context is not None
                ):
                    for execution in context.function_executions:
                        if execution["function_id"] not in switch_task.function_ids:
                            continue
                        helper_outcome = execution["outcome"]
                        if helper_outcome == "cancelled":
                            continue
                        try:
                            self.statistics_service.record_task(
                                game_id, profile_uid, str(profile.get("name") or profile_uid),
                                switch_task.task_id, switch_task.name,
                                "success" if helper_outcome == "success" else "failure",
                                execution["duration_seconds"],
                            )
                            if helper_outcome == "success" and outcome == "success":
                                self.statistics_service.record_profile_switch(profile)
                        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
                            self.logger.error(
                                "Scheduler Nested Profile Switch Statistics Failed",
                                event_name=event.name, task_name=switch_task.name,
                                error_type=type(error).__name__,
                                next_action="inspect statistics storage; counts unavailable",
                            )

                if result.get("event_stop_requested"):
                    try:
                        event_stop_seconds = float(result["reschedule_after_seconds"])
                    except (KeyError, TypeError, ValueError):
                        failure_context["failure_reason"] = "invalid_event_stop_delay"
                        self.logger.warning(
                            "Scheduler Event Stop Rejected",
                            game=event.game_id,
                            event_id=event.event_id,
                            function_id=function.function_id,
                            reason="stop delay is missing or invalid",
                        )
                        return finish_task("failed")
                    finish_task("success")
                    return "event_stop"

                if outcome == "cancelled":
                    return finish_task("cancelled")

                if negative_is_routing and outcome in {"failed", "failure"}:
                    self.logger.debug(
                        "Scheduler Task Negative Outcome Routed",
                        game=event.game_id,
                        profile=profile.get("name"),
                        event_id=event.event_id,
                        task_id=task.task_id,
                        function_id=function_id,
                    )
                    return finish_task("failed", routed=True)

                if outcome in {"failed", "failure", "timeout"}:
                    failure_context["failure_reason"] = outcome
                    provenance = result.get("failure")
                    if isinstance(provenance, dict):
                        failure_context.update({
                            "failed_function_name": provenance.get("function_name"),
                            "failed_block_type": provenance.get("block_type"),
                            "function_chain": [
                                item.get("function_name") for item in provenance.get("function_chain", [])
                                if isinstance(item, dict)
                            ],
                        })
                    self.logger.warning(
                        "Scheduler Task Failure Details",
                        game=event.game_id,
                        profile=profile.get("name"),
                        event_id=event.event_id,
                        task_id=task.task_id,
                        function_id=function_id,
                        executed=executed,
                        retry_minutes=event.retry_minutes,
                        error=result.get("error"),
                    )
                    return finish_task("timeout" if outcome == "timeout" else "failed")

            return finish_task("success")

        def finish(
            status: str,
            reschedule_after_seconds: float | None = None,
        ) -> dict:
            event.status = status
            finished_at = datetime.now(timezone.utc)
            event.record_duration(current, finished_at)
            event.last_finished_at = finished_at.isoformat()
            event.last_executed = list(executed)
            if (
                status == "success"
                and reschedule_after_seconds is not None
                and not event.is_init
                and not event.is_interrupt
            ):
                self._clear_event_variable(
                    event,
                    variable_service,
                    game_id,
                    profile_uid,
                )
                event.next_due_at = (
                    finished_at
                    + timedelta(
                        seconds=max(reschedule_after_seconds, 0)
                        + EVENT_VARIABLE_GRACE_SECONDS
                    )
                ).isoformat()
            elif status in {"failed", "timeout"} and not event.is_init and not event.is_interrupt:
                event.next_due_at = self._retry_due_at(event, finished_at)
            else:
                event.next_due_at = None
            save_state(event)
            return {"event_id": event.event_id, "executed": executed, "status": status}

        if event.is_init:
            gate_status = self._run_init_gate(event, execute_task, profile)
            if gate_status != "success":
                return finish(gate_status)
            remaining = [
                task for task in event.ordered_tasks()
                if task.kind not in PROTECTED_TASK_KINDS
            ]
        else:
            remaining = event.ordered_tasks()

        for task in remaining:
            status = execute_task(task)
            if status == "event_stop":
                return finish("success", event_stop_seconds)
            if status != "success":
                return finish(status)

        finished_at = datetime.now(timezone.utc)
        event.record_duration(current, finished_at)
        event.last_finished_at = finished_at.isoformat()
        event.last_executed = list(executed)
        event.status = "success" if executed else "idle"
        if event.status == "success" and not event.is_interrupt and not event.is_init:
            event.next_due_at = self_due_at[-1] if self_due_at else self._next_due_at(
                event, finished_at, variable_service, game_id, profile_uid,
                profile_name=str(profile.get("name") or ""),
            )
        else:
            event.next_due_at = None
        save_state(event)
        self.logger.info(
            "Scheduler Event Finished",
            game=event.game_id,
            profile=profile.get("name"),
            event_id=event.event_id,
            status=event.status,
            executed=executed,
            cooldown_seconds=event.cooldown_seconds,
            next_due_at=event.next_due_at,
        )
        return {"event_id": event.event_id, "executed": executed, "status": event.status}

    def _run_init_gate(self, event: Event, execute_task, profile: dict) -> str:
        """Profile Check entscheidet; nur ein negativer Check startet Profile Switch.

        Ohne hinterlegte Check-Function gilt das Profil als aktiv.
        """

        check = event.protected_task(PROFILE_CHECK_KIND)
        switch = event.protected_task(PROFILE_SWITCH_KIND)

        if check is None or not check.active:
            self.logger.debug(
                "Runtime Profile Check Skipped",
                game=event.game_id,
                profile=profile.get("name"),
                event_id=event.event_id,
                reason="not_configured",
            )
            return "success"

        status = execute_task(check, negative_is_routing=True)
        if status == "success":
            self.logger.debug(
                "Runtime Profile Check Passed",
                game=event.game_id,
                profile=profile.get("name"),
                event_id=event.event_id,
                switch_required=False,
            )
            return "success"
        if status != "failed":
            return status

        if switch is None or not switch.active:
            self.logger.error(
                "Runtime Profile Switch Missing",
                game=event.game_id,
                profile=profile.get("name"),
                event_id=event.event_id,
                reason="switch_function_missing",
                next_action="assign a function to the protected Init Task 'Profile Switch'",
            )
            return "failed"

        self.logger.info(
            "Runtime Profile Switch Triggered",
            game=event.game_id,
            profile=profile.get("name"),
            event_id=event.event_id,
            reason="profile_check_failed",
        )
        status = execute_task(switch)
        if status != "success":
            return status

        # Gegenprobe: der Switch muss das gewünschte Profil tatsächlich aktiviert haben.
        status = execute_task(check)
        if status == "success" and self.statistics_service is not None:
            try:
                self.statistics_service.record_profile_switch(profile)
            except (OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
                self.logger.error(
                    "Runtime Profile Switch Statistics Failed",
                    profile=profile.get("name"),
                    error_type=type(error).__name__,
                    next_action="inspect statistics storage; counts unavailable",
                )
        return status


    def _next_due_at(
        self,
        event: Event,
        current: datetime,
        variable_service,
        game_id: str,
        profile_id: str,
        profile_name: str = "",
    ) -> str | None:
        """Event Variable gewinnt, sonst Default Scheduling, sonst Fehlerfall.

        Ein konsumierter Variablenwert wird sofort zurückgesetzt: ohne frische
        Rückmeldung der Function fällt die nächste Fälligkeit auf Default
        Scheduling zurück, statt einen veralteten Timer endlos zu wiederholen.
        """

        variable_seconds = self._read_event_variable_seconds(event, variable_service, game_id, profile_id)
        if variable_seconds is not None:
            self._clear_event_variable(event, variable_service, game_id, profile_id)
            return (current + timedelta(seconds=max(variable_seconds, 0) + EVENT_VARIABLE_GRACE_SECONDS)).isoformat()

        if event.schedule_mode == "fixed" and event.run_at_local:
            return self._next_run_at_local(current, event.run_at_local)

        if event.schedule_mode == "fixed" and event.interval_seconds > 0:
            return (current + timedelta(seconds=event.interval_seconds)).isoformat()

        if event.cooldown_seconds > 0:
            return (current + timedelta(seconds=event.cooldown_seconds)).isoformat()

        self.logger.info(
            "Scheduler Event Awaiting Profile Activation",
            game=event.game_id,
            profile=profile_name or "Unknown profile",
            event_id=event.event_id,
            event_name=event.name,
            reason="no_event_variable_and_no_default_scheduling",
            next_action="run once on next profile activation",
        )
        return None

    @staticmethod
    def _read_event_variable_seconds(
        event: Event,
        variable_service,
        game_id: str,
        profile_id: str,
    ) -> float | None:
        if variable_service is None or not hasattr(variable_service, "get_profile_value"):
            return None
        name = event.variable_name.strip()
        if not name or not game_id or not profile_id:
            return None
        value = variable_service.get_profile_value(game_id, profile_id, name)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _clear_event_variable(
        event: Event,
        variable_service,
        game_id: str,
        profile_id: str,
    ) -> None:
        """Ein genutzter Timer-Wert ist verbraucht, bis die Function neu meldet."""

        if variable_service is None or not hasattr(variable_service, "set_profile_value"):
            return
        name = event.variable_name.strip()
        if not name or not game_id or not profile_id:
            return
        variable_service.set_profile_value(game_id, profile_id, name, None)

    def _next_run_at_local(self, current: datetime, run_at_local: list[str]) -> str:
        if not run_at_local:
            return (current + timedelta(seconds=0)).isoformat()

        local_tz = current.astimezone().tzinfo
        current_local = current.astimezone(local_tz)
        selected = None

        for value in sorted(run_at_local):
            try:
                hour, minute = (int(part) for part in str(value).split(":", 1))
            except ValueError:
                continue
            next_time = current_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if next_time > current_local:
                selected = next_time
                break

        if selected is None:
            next_day = current_local + timedelta(days=1)
            first = sorted(run_at_local)[0]
            try:
                hour, minute = (int(part) for part in str(first).split(":", 1))
            except ValueError:
                return current.isoformat()
            selected = next_day.replace(hour=hour, minute=minute, second=0, microsecond=0)

        return selected.astimezone(timezone.utc).isoformat()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = ["Event", "Task", "SchedulerIntent", "SchedulerService"]
