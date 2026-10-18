from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from threading import Event, Lock
from time import monotonic

from core.emulator_manager import EmulatorManager
from core.command_control import CommandCancelled, command_control
from services.adb_runtime_service import AdbRuntimeService
from services.app_config_service import AppConfigService
from services.emulator_health_service import REASON_INSTANCE_STOPPED, STATE_FAILED, STATE_HEALTHY
from services.emulator_health_service import STATE_QUEUED
from services.emulator_health_service import EmulatorHealthService
from services.game_runtime_service import GameRuntimeService
from services.game_service import GameService
from services.function_service import FunctionService
from services.function_runner import FunctionRunner
from services.library_service import LibraryService
from services.opencv_service import OpenCVService
from services.ocr_service import OcrService
from services.screen_service import ScreenService
from models.execution_context import ExecutionContext
from scheduler.models import Event as ScheduleEvent
from services.logging_service import get_logger
from services.profile_service import ProfileService
from services.runtime_heartbeat_service import RuntimeHeartbeatService
from services.runtime_recovery_service import RECOVERY_TIMEOUT_SECONDS, RuntimeRecoveryService
from services.scheduler_service import (
    PROFILE_CHECK_KIND,
    PROFILE_SWITCH_KIND,
    SchedulerService,
)
from services.statistics_service import StatisticsService
from services.execution_activity_service import get_execution_activity
from services.variable_service import VariableService
from services.watchdog_emulator_service import WatchdogEmulatorService


RUNTIME_STOPPED = "Runtime Stopped"
RUNTIME_PAUSED = "Runtime Paused"
RUNTIME_RUNNING = "Runtime Running"
RUNTIME_RECOVERING = "Runtime Recovering"
RUNTIME_ERROR = "Runtime Error"

MULTI_ACCOUNT_VARIABLE_NAME = "MultiAccount_NextAccountName"
MULTI_ACCOUNT_PROFILE_ID_VARIABLE_NAME = "MultiAccount_NextProfileId"


class RuntimeService:

    def __init__(
        self,
        profile_service=None,
        game_service=None,
        emulator_manager=None,
        heartbeat_interval_seconds: int = 10,
        scheduler_event_limit: int = 1,
        scheduler_runner_factory=None,
        scheduler_function_root: str = "data/functions",
        variable_service=None,
        function_service=None,
        app_config_service=None,
        logger=None,
        scheduler_parallel_workers: int = 4,
        statistics_service=None,
        interrupt_burst_limit: int = 8,
    ):

        self.profile_service = (
            profile_service
            or ProfileService()
        )

        self.game_service = (
            game_service
            or GameService()
        )

        self.emulator_manager = (
            emulator_manager
            or EmulatorManager()
        )

        self.logger = (
            logger
            or get_logger("runtime")
        )

        self.adb_runtime_service = AdbRuntimeService(
            self.emulator_manager.adb_path,
        )

        self.emulator_health_service = EmulatorHealthService(
            self.emulator_manager,
            self.adb_runtime_service,
        )

        self.app_config_service = app_config_service or AppConfigService()
        self.statistics_service = statistics_service or StatisticsService()
        self.execution_activity = get_execution_activity()

        self.game_runtime_service = GameRuntimeService(
            self.emulator_manager.adb_path,
            self.game_service,
        )

        self.recovery_service = RuntimeRecoveryService(
            self.adb_runtime_service,
            self.game_runtime_service,
            self.emulator_manager,
            app_config_service=self.app_config_service,
            statistics_service=self.statistics_service,
        )

        self.variable_service = variable_service or VariableService()
        self.scheduler_service = SchedulerService(
            variable_service=self.variable_service,
            statistics_service=self.statistics_service,
        )
        self.scheduler_event_limit = scheduler_event_limit
        self.scheduler_runner_factory = (
            scheduler_runner_factory
            or self.create_function_runner
        )
        self.scheduler_function_root = scheduler_function_root
        self.function_service = function_service or FunctionService(
            root=scheduler_function_root,
        )
        self._scheduler_profile_signatures = {}
        self._validation_profile_signatures = {}
        self._active_profiles = {}
        self._idle_states = {}
        self._due_schedule_read_errors = set()
        self._activation_errors = {}
        self._startup_pending_profiles = set()
        self._startup_profile_attempts = {}
        self._startup_checked_resources = set()
        self._startup_emulator_deadlines = {}

        self.heartbeat = RuntimeHeartbeatService(
            self.heartbeat_once,
            heartbeat_interval_seconds,
        )

        self.running = False
        self.status = RUNTIME_STOPPED
        self.profile_statuses = []
        self.last_heartbeat_at = None
        self.last_health_at = None
        self.scheduler_parallel_workers = max(1, int(scheduler_parallel_workers or 1))
        self.interrupt_burst_limit = max(1, int(interrupt_burst_limit or 1))
        self._profile_execution_locks: dict[str, Lock] = {}
        self._paused_interrupt_resources: set[str] = set()
        self._stop_event = Event()
        self._start_lock = Lock()
        self._stop_lock = Lock()
        self._stop_generation = 0
        self._pause_event = Event()
        self._health_failures = set()
        self._wake_resources = set()
        self.health_heartbeat = RuntimeHeartbeatService(self.check_execution_health, 2)

    def pause(self):
        if not self.running:
            return self.get_status()
        self._pause_event.set()
        self.status = RUNTIME_PAUSED
        return self.get_status()

    def resume(self):
        self._pause_event.clear()
        if self.running:
            self.status = RUNTIME_RUNNING
        return self.get_status()

    @property
    def paused(self):
        pause_event = getattr(self, "_pause_event", None)
        return pause_event is not None and pause_event.is_set()

    def execution_cancelled(self, profile):
        resource = self._execution_resource_key(profile)
        while (
            self.paused and not self.is_stop_requested()
            and resource not in getattr(self, "_health_failures", set())
        ):
            self._stop_event.wait(0.05)
        return self.is_stop_requested() or (
            resource in getattr(self, "_health_failures", set())
        )

    def control_cancelled(self):
        while self.paused and not self.is_stop_requested():
            self._stop_event.wait(0.05)
        return self.is_stop_requested()

    def _publish_profile_status(self, status):
        self.profile_statuses = [
            item for item in getattr(self, "profile_statuses", [])
            if item.get("profile_id") != status.get("profile_id")
        ] + [status]
        self._update_runtime_status(self.profile_statuses)

    def _update_runtime_status(self, statuses):
        if self.is_stop_requested():
            self.status = RUNTIME_STOPPED
        elif self.paused:
            self.status = RUNTIME_PAUSED
        elif any(item.get("state") == STATE_FAILED for item in statuses):
            self.status = RUNTIME_ERROR
        elif any(item.get("state") == "Recovering" for item in statuses):
            self.status = RUNTIME_RECOVERING
        else:
            self.status = RUNTIME_RUNNING

    def check_execution_health(self):
        if self.is_stop_requested():
            return
        generation = self._stop_generation
        for status in list(self.profile_statuses):
            if status.get("state") != STATE_HEALTHY or not status.get("device_id"):
                continue
            profile = {
                "id": status["profile_id"],
                "name": status.get("profile_name"),
                "game": status.get("game", {}).get("id"),
                "emulator_instance": status.get("emulator_instance"),
            }
            resource = self._execution_resource_key(profile)
            device = status["device_id"]
            package = status.get("game", {}).get("package_name")
            try:
                responsive = self.adb_runtime_service.adb.is_responsive(device, timeout=2)
                game_running = responsive and (
                    not package or self.game_runtime_service.adb.get_package_pid(
                        device, package, timeout=2,
                    ) is not None
                )
                if game_running and package:
                    game_running = not self.game_runtime_service.adb.has_anr(
                        device, package, timeout=2,
                    )
                screen_available = game_running and self.emulator_health_service.probe_screenshot(device)
            except Exception as error:
                self.logger.error("Runtime Health Probe Failed", **self._metadata(profile), error=repr(error))
                responsive = game_running = screen_available = False
            if self.is_stop_requested() or generation != self._stop_generation:
                return
            if not any(item is status for item in self.profile_statuses) or status.get("state") != STATE_HEALTHY:
                continue
            status["checked_at"] = monotonic()
            if not responsive or not game_running or not screen_available:
                if not responsive:
                    message, reason = "Emulator not responding", "emulator_unresponsive"
                elif not game_running:
                    message, reason = "Game process missing or not responding", "no_pid"
                else:
                    message, reason = "Screenshot failed", "screenshot_failed"
                self._health_failures.add(resource)
                status.update(
                    state=STATE_FAILED,
                    message=message, reason=reason,
                )
                self._update_runtime_status(self.profile_statuses)
                self.logger.error(
                    "Runtime Execution Health Lost", **self._metadata(profile),
                    reason=status["reason"], next_action="cancel execution, then recover",
                )
            else:
                self.last_health_at = datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _execution_resource_key(profile: dict) -> str:
        game_id = str(profile.get("game") or "")
        emulator_instance = profile.get("emulator_instance")
        profile_id = str(
            profile.get("botstudio_profile_id")
            or profile.get("id")
            or ""
        )
        if emulator_instance is None:
            return f"{game_id}:{profile_id}"
        return f"{game_id}:emulator:{emulator_instance}"

    def try_acquire_profile_execution(self, profile: dict | str) -> bool:
        resource_key = (
            self._execution_resource_key(profile)
            if isinstance(profile, dict)
            else str(profile)
        )
        lock = self._profile_execution_locks.setdefault(resource_key, Lock())
        return lock.acquire(blocking=False)

    def release_profile_execution(self, profile: dict | str) -> None:
        resource_key = (
            self._execution_resource_key(profile)
            if isinstance(profile, dict)
            else str(profile)
        )
        lock = self._profile_execution_locks.get(resource_key)
        if lock and lock.locked():
            lock.release()

    def pause_interrupts(self, profile: dict) -> None:
        self._paused_interrupt_resources.add(
            self._execution_resource_key(profile)
        )

    def resume_interrupts(self, profile: dict) -> None:
        self._paused_interrupt_resources.discard(
            self._execution_resource_key(profile)
        )

    def interrupts_paused(self, profile: dict) -> bool:
        return self._execution_resource_key(profile) in self._paused_interrupt_resources

    def is_stop_requested(self) -> bool:
        return self._stop_event.is_set() or not self.running

    def create_function_runner(self):
        return FunctionRunner(
            screen_service=ScreenService(),
            opencv_service=OpenCVService(),
            adb_service=self.adb_runtime_service,
            ocr_service=OcrService(),
            library_service=LibraryService(),
            function_service=self.function_service,
            activity_service=self.execution_activity,
        )

    def start(
        self,
    ):

        if not self._start_lock.acquire(blocking=False):
            raise RuntimeError("Runtime start is already in progress")
        try:
            if Path("data/package-activation/pending.json").exists():
                raise RuntimeError("Package activation recovery is pending; restart Studio before running")
            if getattr(self, "running", False):
                return self.resume() if self.paused else self.get_status()
            heartbeat = getattr(self, "heartbeat", None)
            if heartbeat is not None and not heartbeat.wait_until_idle(0):
                raise RuntimeError("Previous runtime execution is still stopping")
            if any(lock.locked() for lock in getattr(self, "_profile_execution_locks", {}).values()):
                raise RuntimeError("Previous profile execution is still stopping")
            return self._start()
        except Exception:
            self.stop()
            raise
        finally:
            self._start_lock.release()

    @contextmanager
    def content_import_guard(self):
        """Prevent runtime startup while active content is being transferred."""
        if not self._start_lock.acquire(blocking=False):
            raise RuntimeError("Runtime start or content import is already in progress")
        try:
            if self.running or not self.heartbeat.wait_until_idle(0):
                raise RuntimeError("Stop Runtime before importing active package content")
            if any(lock.locked() for lock in self._profile_execution_locks.values()):
                raise RuntimeError("Previous profile execution is still stopping")
            if self.execution_activity.snapshot():
                raise RuntimeError("Function execution is still active")
            yield
        finally:
            self._start_lock.release()

    def _start(
        self,
        expected_stop_generation: int | None = None,
    ):

        with self._stop_lock:
            if (
                expected_stop_generation is not None
                and self._stop_generation != expected_stop_generation
            ):
                return self.get_status()
            self._stop_event.clear()
        self.logger.info(
            "Runtime Start"
        )

        self.adb_runtime_service.server_available()

        self.running = True
        self.profile_statuses = []
        if hasattr(self, "_pause_event"):
            self._pause_event.clear()
            self._health_failures.clear()
            self._wake_resources.clear()
        self.status = RUNTIME_RUNNING
        if self._stop_event.is_set():
            self.logger.info("Runtime Start Cancelled")
            self.running = False
            self.status = RUNTIME_STOPPED
            return self.get_status()
        self._scheduler_profile_signatures.clear()
        self._validation_profile_signatures.clear()
        self._active_profiles.clear()
        self._idle_states.clear()
        self._startup_pending_profiles = {
            self._profile_id(profile) for profile in self.profile_service.load_enabled()
        }
        self._startup_profile_attempts.clear()
        self._startup_checked_resources.clear()
        self._startup_emulator_deadlines.clear()
        self._activation_errors = self._find_activation_errors(
            self.profile_service.load_enabled(),
        )

        for profile in self.profile_service.load_enabled():
            game_id = profile.get("game", "")
            if not game_id:
                continue
            self._activate_profile_variables(profile, invalidate_live=True)
            self._ensure_init_event(game_id, self._profile_id(profile))
            self.scheduler_service.ensure_interrupt_event(
                game_id,
                self._profile_id(profile),
            )

        if self._stop_event.is_set():
            self.running = False
            self.status = RUNTIME_STOPPED
            return self.get_status()

        if hasattr(self, "health_heartbeat"):
            self.health_heartbeat.start()
        self.heartbeat_once()
        if not self.is_stop_requested():
            self.heartbeat.start()

        return self.get_status()

    def stop(
        self,
    ):

        with self._stop_lock:
            self._stop_generation += 1
            self._stop_event.set()
        self.running = False
        self.status = RUNTIME_STOPPED
        if hasattr(self, "_pause_event"):
            self._pause_event.clear()
            self.health_heartbeat.stop()
        self.logger.info(
            "Runtime Stop"
        )

        self.heartbeat.stop()
        self.running = False
        self.status = RUNTIME_STOPPED

        return self.get_status()

    def heartbeat_once(
        self,
    ):

        if self.is_stop_requested() or self.paused:
            return self.get_status()

        self.last_heartbeat_at = datetime.now(timezone.utc).isoformat()

        statuses = []
        healthy_profiles = []

        enabled_profiles = (
            self.profile_service.load_enabled()
        )

        due_profile_scores = self._profiles_with_due_events(enabled_profiles)
        eligible_profiles = [
            profile for profile in enabled_profiles
            if self._profile_id(profile) not in self._activation_errors
        ]
        pending_profiles = [
            profile for profile in eligible_profiles
            if self._profile_id(profile) in self._startup_pending_profiles
        ]
        pending_resources = {
            self._execution_resource_key(profile) for profile in pending_profiles
        }
        pending_attempts = {
            resource: min(
                self._startup_profile_attempts.get(self._profile_id(profile), 0)
                for profile in pending_profiles
                if self._execution_resource_key(profile) == resource
            )
            for resource in pending_resources
        }
        selection_scores = {
            self._profile_id(profile): (
                1, "0001-01-01T00:00:00+00:00",
            )
            for profile in pending_profiles
            if self._startup_profile_attempts.get(self._profile_id(profile), 0)
            == pending_attempts[self._execution_resource_key(profile)]
        }
        selection_scores.update({
            self._profile_id(profile): due_profile_scores[self._profile_id(profile)]
            for profile in eligible_profiles
            if self._profile_id(profile) in due_profile_scores
            and self._execution_resource_key(profile) not in pending_resources
        })
        preferred_profile_ids = set(self._active_profiles.values())
        due_resources = {
            self._execution_resource_key(profile)
            for profile in enabled_profiles
            if self._profile_id(profile) in due_profile_scores
        }
        unknown_schedule_resources = {
            self._execution_resource_key(profile)
            for profile in enabled_profiles
            if self._profile_id(profile) in self._due_schedule_read_errors
        }
        for resource_key in due_resources:
            idle = self._idle_states.pop(resource_key, None)
            if idle and idle.get("applied"):
                if not hasattr(self, "_wake_resources"):
                    self._wake_resources = set()
                self._wake_resources.add(resource_key)
        queue_assignments = SchedulerService.queue_assignments(
            eligible_profiles,
            capacities=self._scheduler_capacities(enabled_profiles),
            preferred_profile_ids=preferred_profile_ids,
            due_profile_scores=selection_scores,
        )

        for profile in enabled_profiles:
            if self.is_stop_requested() or self.paused:
                break

            profile_id = profile.get(
                "botstudio_profile_id",
                profile.get("id", ""),
            )
            queue_info = queue_assignments.get(profile_id)

            if profile_id in self._activation_errors:
                status = self._activation_error_status(profile)
                statuses.append(status)
                continue

            if queue_info:
                status = self._queued_status(profile, queue_info)
                statuses.append(status)
                continue

            if (
                self._profile_id(profile) not in due_profile_scores
                and self._execution_resource_key(profile) not in pending_resources
                and self._execution_resource_key(profile) not in unknown_schedule_resources
            ):
                try:
                    with command_control(self.control_cancelled):
                        idle_status = self._apply_idle_behavior(profile)
                except CommandCancelled:
                    break
                if idle_status:
                    statuses.append(idle_status)
                    continue

            try:
                if profile_id in self._startup_pending_profiles:
                    self._startup_profile_attempts[profile_id] = (
                        self._startup_profile_attempts.get(profile_id, 0) + 1
                    )
                with command_control(self.control_cancelled):
                    status = self._validate_profile(profile)
            except CommandCancelled:
                break
            except Exception as error:
                status = self._profile_cycle_error_status(profile, error)
                self.logger.error(
                    "Runtime Profile Cycle Failed",
                    **self._metadata(profile),
                    error=repr(error),
                    next_action="retry profile on next heartbeat",
                )

            statuses.append(status)

            if status["state"] == STATE_HEALTHY:
                try:
                    self.statistics_service.record_uptime(profile)
                except Exception as error:
                    self.logger.error(
                        "Runtime Uptime Statistics Failed",
                        **self._metadata(profile),
                        error=repr(error),
                        next_action="continue runtime without uptime sample",
                    )
                healthy_profiles.append((status, profile))

        self.profile_statuses = statuses
        self._update_runtime_status(statuses)
        self._run_schedules_parallel(healthy_profiles)

        self._update_runtime_status(statuses)

        return self.get_status()

    def _find_activation_errors(self, profiles):
        grouped = {}
        for profile in profiles:
            key = (profile.get("game", ""), profile.get("emulator_instance"))
            grouped.setdefault(key, []).append(profile)

        errors = {}
        for (game_id, _instance), group in grouped.items():
            if len(group) < 2:
                continue
            if all(self._has_switch_function(profile) for profile in group):
                continue
            for profile in group:
                errors[self._profile_id(profile)] = (
                    "MultiAccount activation rejected: "
                    "a Profile Switch Function is required for multiple profiles "
                    "on the same emulator instance"
                )
        return errors

    def _ensure_init_event(self, game_id: str, profile_id: str):
        return self.scheduler_service.ensure_init_event(
            game_id,
            profile_id,
            seed_functions=self.game_service.legacy_init_function_ids(game_id),
        )

    def _has_switch_function(self, profile) -> bool:
        init_event = self._ensure_init_event(
            profile.get("game", ""),
            self._profile_id(profile),
        )
        task = init_event.protected_task(PROFILE_SWITCH_KIND)
        return bool(task and task.function_ids)

    def _activation_error_status(self, profile):
        message = self._activation_errors[self._profile_id(profile)]
        self.logger.error(
            "Runtime Profile Activation Rejected",
            **self._metadata(profile),
            reason="multiaccount_switch_function_missing",
        )
        return {
            "profile_id": self._profile_id(profile),
            "profile_name": profile.get("name"),
            "game": {"id": profile.get("game"), "package_name": None},
            "emulator_instance": profile.get("emulator_instance"),
            "state": STATE_FAILED,
            "message": message,
            "reason": "multiaccount_switch_function_missing",
            "device_id": None,
        }

    def _profile_cycle_error_status(self, profile, error):
        return {
            "profile_id": profile.get("id"),
            "profile_name": profile.get("name"),
            "game": {
                "id": profile.get("game"),
                "package_name": None,
            },
            "emulator_instance": profile.get("emulator_instance"),
            "state": STATE_FAILED,
            "message": "Profile cycle failed; retry scheduled",
            "reason": "profile_cycle_exception",
            "error": repr(error),
            "device_id": None,
        }

    def _scheduler_capacities(self, profiles):
        capacities = {}
        for profile in profiles:
            game_id = str(profile.get("game") or "")
            if not game_id:
                continue
            capacities.setdefault(game_id, set())
            instance = profile.get("emulator_instance")
            if instance is not None:
                capacities[game_id].add(str(instance))

        # Separate emulator instances are autonomous and should not be queued
        # behind each other; only shared-instance queueing remains active.
        return {
            game_id: max(1, len(instances))
            for game_id, instances in capacities.items()
        }

    def _profiles_with_due_events(self, profiles):
        due_profile_scores = {}
        self._due_schedule_read_errors = set()
        list_events = getattr(self.scheduler_service, "list_execution_events", None)
        if not callable(list_events):
            self._due_schedule_read_errors.update(
                SchedulerService.create_intent(profile).profile_id
                for profile in profiles
                if SchedulerService.create_intent(profile).profile_id
            )
            return due_profile_scores

        for profile in profiles:
            intent = SchedulerService.create_intent(profile)
            if not intent.game_id or not intent.profile_id:
                continue
            try:
                events = list_events(
                    intent.game_id,
                    profile_id=intent.profile_id,
                )
                due_events = [
                    event
                    for event in events or []
                    if not event.is_init
                    and not event.is_interrupt
                    and bool(event.function_ids)
                ]
                if not due_events:
                    continue
                due_times = [
                    parsed.astimezone(timezone.utc).isoformat()
                    for event in due_events
                    if (parsed := ScheduleEvent._parse_datetime(event.next_due_at)) is not None
                ]
                due_task_count = sum(
                    1
                    for event in due_events
                    for task in event.ordered_tasks()
                    if task.active
                )
                due_profile_scores[intent.profile_id] = (
                    due_task_count,
                    min(due_times, default="0001-01-01T00:00:00+00:00"),
                )
            except Exception as error:
                self._due_schedule_read_errors.add(intent.profile_id)
                self.logger.error(
                    "Runtime Due Schedule Read Failed",
                    **self._metadata(profile),
                    error=repr(error),
                    next_action="do not infer due work or apply idle behavior; retry next heartbeat",
                )
        return due_profile_scores

    def _apply_idle_behavior(self, profile):
        game_id = str(profile.get("game") or "")
        instance = profile.get("emulator_instance")
        resource_key = self._execution_resource_key(profile)
        state = self._idle_states.get(resource_key)
        if state is None:
            state = {"applied": ""}
            self._idle_states[resource_key] = state
        if state["applied"]:
            return self._idle_profile_status(profile, state["applied"])

        try:
            settings = self.app_config_service.get_studio_settings()
            behavior = str(settings.get("idle_behavior") or "do_nothing")
        except (TypeError, ValueError, OSError) as error:
            self.logger.error(
                "Runtime Idle Settings Failed",
                **self._metadata(profile),
                error=repr(error),
                next_action="keep the current session running",
            )
            return None
        if behavior == "do_nothing":
            return None

        for status in getattr(self, "profile_statuses", []):
            if status.get("emulator_instance") == instance:
                status.update(state="Idle", message="Applying idle behavior")
        try:
            if behavior == "close_game":
                if not self.emulator_manager.is_instance_running(instance):
                    stopped = True
                else:
                    device_id = self.emulator_manager.get_device_id(instance)
                    package_name = self.game_runtime_service.get_package_name(profile)
                    stopped = bool(
                        device_id
                        and package_name
                        and self.game_runtime_service.adb.shell_success(
                            device_id,
                            ["am", "force-stop", package_name],
                            timeout=10,
                        )
                    )
            elif behavior == "close_emulator":
                stopped = (
                    not self.emulator_manager.is_instance_running(instance)
                    or self.emulator_manager.stop_instance(instance)
                )
            else:
                self.logger.warning(
                    "Runtime Idle Behavior Unknown",
                    **self._metadata(profile),
                    behavior=behavior,
                    next_action="leave the current session running",
                )
                return None
        except CommandCancelled:
            raise
        except Exception as error:
            self.logger.error(
                "Runtime Idle Behavior Failed",
                **self._metadata(profile),
                behavior=behavior,
                error=repr(error),
            )
            return None

        if not stopped:
            self.logger.warning(
                "Runtime Idle Behavior Failed",
                **self._metadata(profile),
                behavior=behavior,
            )
            return None

        state["applied"] = behavior
        active_profile_id = self._active_profiles.get((game_id, instance))
        if active_profile_id:
            self._scheduler_profile_signatures.pop(active_profile_id, None)
            self._validation_profile_signatures.pop(active_profile_id, None)
        self.logger.info(
            "Runtime Idle Behavior Applied",
            **self._metadata(profile),
            behavior=behavior,
        )
        return self._idle_profile_status(profile, behavior)

    def _idle_profile_status(self, profile, behavior):
        action = {
            "close_game": "Game closed until the next scheduled event",
            "close_emulator": "Emulator closed until the next scheduled event",
            "do_nothing": "Waiting for the next scheduled event",
        }.get(behavior, "Waiting for the next scheduled event")
        return {
            "profile_id": self._profile_id(profile),
            "profile_name": profile.get("name"),
            "game": {"id": profile.get("game"), "package_name": None},
            "emulator_instance": profile.get("emulator_instance"),
            "state": "Idle",
            "message": action,
        }

    def _run_schedules_parallel(self, healthy_profiles):
        if not healthy_profiles:
            return

        workers = min(self.scheduler_parallel_workers, len(healthy_profiles))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="runtime-scheduler") as pool:
            futures = {
                pool.submit(self._run_profile_schedule, profile): status
                for status, profile in healthy_profiles
            }
            for future in as_completed(futures):
                status = futures[future]
                try:
                    status["scheduler"] = future.result()
                except Exception as ex:
                    status["scheduler"] = {
                        "error": repr(ex),
                        "events": [],
                    }
                    status["state"] = STATE_FAILED
                    status["message"] = "Scheduler execution failed"
                    self.logger.error(
                        "Runtime Scheduler Parallel Execution Failed",
                        **self._metadata(profile={
                            "name": status.get("profile_name"),
                            "game": status.get("game", {}).get("id"),
                            "emulator_instance": status.get("emulator_instance"),
                        }),
                        error=repr(ex),
                    )

    def _queued_status(self, profile, queue_info) -> dict:

        self.logger.info(
            "Runtime Profile Queued",
            **self._metadata(profile),
            queue_position=queue_info["position"],
            queue_reason=queue_info.get("reason", "unknown"),
        )

        return {
            "profile_id": profile.get("id"),
            "profile_name": profile.get("name"),
            "game": {
                "id": profile.get("game"),
                "package_name": None,
            },
            "emulator_instance": profile.get("emulator_instance"),
            "state": STATE_QUEUED,
            "message": f"Queue position {queue_info['position']}, waiting behind {queue_info['ahead']}",
            "reason": queue_info.get("reason"),
            "device_id": None,
        }

    def resolve_runnable_profile(self, profile):
        """Der FunctionRunner adressiert ADB ausschliesslich ueber
        `profile['device_id']`; ohne diesen Schluessel scheitert jeder Block."""

        if profile.get("device_id"):
            return profile

        instance = profile.get("emulator_instance")
        if instance is None:
            return profile

        enriched = dict(profile)
        try:
            enriched["device_id"] = self.emulator_manager.get_device_id(instance)
        except (TypeError, ValueError) as ex:
            # Ein unbrauchbarer Instanzwert darf den Heartbeat nicht abreissen.
            self.logger.error(
                "Runtime Device Unresolved",
                **self._metadata(profile),
                reason="emulator_instance_invalid",
                error=repr(ex),
                next_action="set a numeric emulator instance on the profile",
            )
            return profile
        return enriched

    def _run_profile_schedule(self, profile):
        if self.is_stop_requested():
            return {"status": "stopped", "events": [], "interrupt": None}
        if not self.try_acquire_profile_execution(profile):
            return {"status": "busy", "events": [], "interrupt": None}
        try:
            return self._run_profile_schedule_unlocked(profile)
        finally:
            self.release_profile_execution(profile)

    def _run_profile_schedule_unlocked(self, profile):
        profile_id = profile.get("botstudio_profile_id", profile.get("id", ""))
        signature = (
            profile_id,
            profile.get("game", ""),
            profile.get("emulator_instance"),
        )
        init_result = None
        self._activate_profile_variables(profile)
        runnable = self.resolve_runnable_profile(profile)
        if self._scheduler_profile_signatures.get(profile_id) != signature:
            init_result = self._run_profile_init(runnable)
            if init_result.get("status") in {"success", "idle"}:
                self.scheduler_service.prepare_profile_start(
                    runnable.get("game", ""), profile_id,
                )
                self._scheduler_profile_signatures[profile_id] = signature
                self._startup_pending_profiles.discard(profile_id)

        interrupt_event = self.scheduler_service.ensure_interrupt_event(
            runnable.get("game", ""),
            profile_id,
        )
        if init_result is not None and init_result.get("status") not in {"success", "idle"}:
            return {"init": init_result, "events": [], "interrupt": None}

        kwargs = {
            "profile": runnable,
            "function_root": self.scheduler_function_root,
            "limit": None if init_result is not None else self.scheduler_event_limit,
            "overdue_only": init_result is not None,
            "priority_first": init_result is not None,
            "cancellation_checker": partial(self.execution_cancelled, runnable),
        }
        if self.scheduler_runner_factory is not None:
            kwargs["runner_factory"] = self.scheduler_runner_factory
            kwargs["variable_service"] = self.variable_service
        regular_results = []
        interrupt_results = []
        for _ in range(self.interrupt_burst_limit):
            events = self.scheduler_service.run_due_events(
                runnable.get("game", ""),
                **kwargs,
            )
            regular_results.extend(events)

            if self.interrupts_paused(runnable):
                break

            interrupt_result = self.scheduler_service.run_event(
                interrupt_event,
                profile=runnable,
                function_root=self.scheduler_function_root,
                runner_factory=self.scheduler_runner_factory,
                variable_service=self.variable_service,
                cancellation_checker=partial(self.execution_cancelled, runnable),
            )
            interrupt_results.append(interrupt_result)
            if interrupt_result.get("status") != "success":
                break

            regular_due = [
                event
                for event in self.scheduler_service.list_execution_events(
                    runnable.get("game", ""),
                    profile_id=profile_id,
                )
                if not event.is_init and not event.is_interrupt and event.function_ids
            ]
            if regular_due:
                continue

        return {
            "init": init_result,
            "events": regular_results,
            "interrupt": interrupt_results,
        }

    def _activate_profile_variables(
        self,
        profile,
        reset=False,
        invalidate_live=False,
    ):
        """Activate only the target profile's persisted variable room."""

        game_id = str(profile.get("game") or "")
        profile_id = str(
            profile.get("botstudio_profile_id")
            or profile.get("id")
            or ""
        )
        if not game_id or not profile_id:
            return {}

        values = self.variable_service.initialize_profile_values(
            game_id,
            profile_id,
            reset=reset,
            invalidate_live=invalidate_live,
        )

        # Jede Function dieses Zyklus muss wissen, welcher Account gemeint ist -
        # auch ohne vorangehenden Profilwechsel (erster Start, Einzelprofil).
        account_name = profile.get("name") or profile_id
        values[MULTI_ACCOUNT_VARIABLE_NAME] = account_name
        values[MULTI_ACCOUNT_PROFILE_ID_VARIABLE_NAME] = profile_id
        self.variable_service.save_profile_values(
            game_id,
            profile_id,
            values,
        )
        self.logger.debug(
            "Runtime MultiAccount Target Published",
            **self._metadata(profile),
            name_variable=MULTI_ACCOUNT_VARIABLE_NAME,
            profile_name=account_name,
            profile_id_variable=MULTI_ACCOUNT_PROFILE_ID_VARIABLE_NAME,
            target_profile_id=profile_id,
        )
        return values

    def _run_profile_init(self, profile):
        game_id = profile.get("game", "")
        init_event = self._ensure_init_event(game_id, self._profile_id(profile))

        if not init_event.function_ids:
            return {"event_id": init_event.event_id, "status": "idle", "executed": []}

        kwargs = {
            "event": init_event,
            "profile": profile,
            "function_root": self.scheduler_function_root,
            "cancellation_checker": partial(self.execution_cancelled, profile),
        }
        if self.scheduler_runner_factory is not None:
            kwargs["runner_factory"] = self.scheduler_runner_factory
            kwargs["variable_service"] = self.variable_service
        return self.scheduler_service.run_event(**kwargs)

    def cancel_scheduled_event(self, event_id: str) -> bool:
        return self.scheduler_service.cancel_event(event_id)

    def watchdog_emulator_command(self, command, arguments):
        return WatchdogEmulatorService(self).handle(command, arguments)

    def watchdog_scheduler_command(self, command, arguments=None):
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ValueError("Command arguments must be an object")

        if command in {"events.list", "tasks.list"}:
            return self._watchdog_scheduler_snapshot(
                include_events=command == "events.list",
                include_tasks=command == "tasks.list",
            )
        if command in {"event.enable", "event.disable", "event.run_now"}:
            if "event_id" not in arguments or set(arguments) - {
                "event_id",
                "profile_id",
            }:
                raise ValueError("event_id is required")
            event_reference = arguments["event_id"]
            if (
                not isinstance(event_reference, str)
                or not event_reference.strip()
                or len(event_reference) > 128
            ):
                raise ValueError("event_id must be a non-empty reference")
            profile_reference = arguments.get("profile_id")
            if profile_reference is not None and (
                not isinstance(profile_reference, str)
                or not profile_reference.strip()
                or len(profile_reference) > 128
            ):
                raise ValueError("profile_id must be a non-empty reference")
            return self._watchdog_update_event(
                command,
                event_reference.strip(),
                profile_reference.strip() if profile_reference else None,
            )
        raise ValueError(f"Unsupported scheduler command: {command}")

    def _watchdog_scheduler_snapshot(
        self,
        include_events=True,
        include_tasks=True,
    ):
        events = []
        open_tasks = []
        profiles = self.profile_service.load_enabled()
        for profile in profiles:
            game_id = str(profile.get("game") or "")
            profile_id = self._profile_id(profile)
            if not game_id or not profile_id:
                continue
            profile_name = str(profile.get("name") or profile_id)[:100]
            for event in self.scheduler_service.list_events(
                game_id,
                profile_id=profile_id,
            ):
                if not event.is_init and not event.is_interrupt and include_events:
                    events.append(
                        {
                            "event_id": event.event_id,
                            "event_name": str(event.name or event.event_id)[:100],
                            "profile_id": profile_id,
                            "profile_name": profile_name,
                            "enabled": bool(event.enabled),
                            "status": str(event.status or "idle"),
                        }
                    )
                if not include_tasks:
                    continue
                for task in event.ordered_tasks():
                    queued = event.status == "queued" and task.active
                    if task.status != "running" and not queued:
                        continue
                    open_tasks.append(
                        {
                            "task_id": task.task_id,
                            "task_name": str(task.name or task.task_id)[:100],
                            "task_status": (
                                "queued" if queued and task.status != "running"
                                else str(task.status)
                            ),
                            "event_id": event.event_id,
                            "event_name": str(event.name or event.event_id)[:100],
                            "event_status": str(event.status or "idle"),
                            "profile_id": profile_id,
                            "profile_name": profile_name,
                        }
                    )

        events.sort(
            key=lambda item: (
                item["profile_name"].casefold(),
                item["event_name"].casefold(),
                item["event_id"],
            )
        )
        open_tasks.sort(
            key=lambda item: (
                item["profile_name"].casefold(),
                item["event_name"].casefold(),
                item["task_name"].casefold(),
                item["task_id"],
            )
        )
        if include_events and include_tasks:
            return {
                "events": events[:100],
                "events_truncated": len(events) > 100,
                "open_tasks": open_tasks[:100],
                "tasks_truncated": len(open_tasks) > 100,
            }
        if include_events:
            return {
                "events": events[:100],
                "truncated": len(events) > 100,
            }
        return {
            "tasks": open_tasks[:100],
            "truncated": len(open_tasks) > 100,
        }

    def _watchdog_update_event(
        self,
        command,
        event_reference,
        profile_reference=None,
    ):
        matches = []
        for profile in self.profile_service.load_enabled():
            game_id = str(profile.get("game") or "")
            profile_id = self._profile_id(profile)
            if (
                profile_reference is not None
                and not profile_id.casefold().startswith(
                    profile_reference.casefold()
                )
            ):
                continue
            if not game_id or not profile_id:
                continue
            for event in self.scheduler_service.list_events(
                game_id,
                profile_id=profile_id,
            ):
                if (
                    not event.is_init
                    and not event.is_interrupt
                    and event.event_id.casefold().startswith(
                        event_reference.casefold()
                    )
                ):
                    matches.append((profile, game_id, profile_id, event))

        if not matches:
            raise ValueError("Event not found")
        if len(matches) != 1:
            raise ValueError("Event reference is ambiguous; use a longer ID")

        profile, game_id, profile_id, listed_event = matches[0]
        acquired = command != "event.run_now"
        if acquired and not self.try_acquire_profile_execution(profile):
            raise RuntimeError("The profile is busy; try again when it is idle")
        try:
            event = self.scheduler_service.load_event(
                game_id,
                listed_event.event_id,
            )
            if event is None or event.profile_id not in ("", profile_id):
                raise ValueError("Event is no longer available")
            if event.is_init or event.is_interrupt:
                raise ValueError("System events cannot be controlled remotely")

            if command == "event.run_now":
                event = self.scheduler_service.queue_event_now(event)
            else:
                event.enabled = command == "event.enable"

                self.scheduler_service.save_event(event)
            persisted = self.scheduler_service.load_event(
                game_id,
                event.event_id,
            )
            if (
                persisted is None
                or persisted.enabled != event.enabled
                or persisted.status != event.status
                or persisted.next_due_at != event.next_due_at
            ):
                raise OSError("Event change could not be verified")
            return {
                "event_id": event.event_id,
                "event_name": str(event.name or event.event_id)[:100],
                "profile_id": profile_id,
                "profile_name": str(profile.get("name") or profile_id)[:100],
                "enabled": bool(event.enabled),
                "state": "queued" if command == "event.run_now" else "updated",
            }
        finally:
            if acquired:
                self.release_profile_execution(profile)

    def get_status(
        self,
    ):
        profiles = []
        for status in self.profile_statuses:
            snapshot = dict(status)
            if not self.running or (
                snapshot.get("state") == STATE_HEALTHY
                and monotonic() - snapshot.get("checked_at", 0) > 15
            ):
                snapshot.update(state="Unknown", message="Live health has not been confirmed")
            profiles.append(snapshot)
        return {
            "running": self.running,
            "status": self.status,
            "profiles": profiles,
            "last_heartbeat_at": self.last_heartbeat_at,
            "last_health_at": getattr(self, "last_health_at", None),
        }

    def get_recovery_state(self):
        interrupted = []
        list_events = getattr(
            self.scheduler_service,
            "list_execution_events",
            None,
        )
        if not callable(list_events):
            raise RuntimeError("Scheduler recovery state is unavailable")

        for profile in self.profile_service.load_enabled():
            game_id = str(profile.get("game") or "")
            profile_id = self._profile_id(profile)
            if not game_id or not profile_id:
                continue
            events = list_events(game_id, profile_id=profile_id)
            for event in events or []:
                running_tasks = [
                    {
                        "task_id": task.task_id,
                        "task_name": task.name,
                    }
                    for task in event.tasks
                    if task.status == "running"
                ]
                if event.status != "running" and not running_tasks:
                    continue
                interrupted.append(
                    {
                        "game_id": game_id,
                        "profile_id": profile_id,
                        "event_id": event.event_id,
                        "event_name": event.name,
                        "running_tasks": running_tasks,
                    }
                )

        return {
            "safe_to_resume": not interrupted,
            "interrupted_events": interrupted,
        }

    def _validate_profile(
        self,
        profile,
    ):

        metadata = self._metadata(
            profile
        )

        self.logger.debug(
            "Runtime Profile Validation Started",
            **metadata,
        )

        resource = self._execution_resource_key(profile)
        first_check = resource not in self._startup_checked_resources
        waking = resource in getattr(self, "_wake_resources", set())
        if waking and resource not in self._startup_emulator_deadlines:
            self._startup_emulator_deadlines[resource] = monotonic() + RECOVERY_TIMEOUT_SECONDS
        expected_wakeup = waking and monotonic() < self._startup_emulator_deadlines.get(resource, 0)
        expected_stopped = (
            expected_wakeup
            or self._profile_id(profile) in self._startup_pending_profiles
            and (
                first_check
                or monotonic() < self._startup_emulator_deadlines.get(resource, 0)
            )
        )
        self._startup_checked_resources.add(resource)
        emulator_status = (
            self.emulator_health_service.validate(
                profile,
                expected_stopped=expected_stopped,
                expected_wakeup=expected_wakeup,
            )
        )
        if emulator_status["state"] == STATE_HEALTHY or (
            not waking and emulator_status.get("reason") != REASON_INSTANCE_STOPPED
        ):
            self._startup_emulator_deadlines.pop(resource, None)

        package_name = (
            self.game_runtime_service
            .get_package_name(
                profile
            )
        )

        status = {
            "profile_id": self._profile_id(profile),
            "profile_name": profile.get("name"),
            "game": {
                "id": profile.get("game"),
                "package_name": package_name,
            },
            "emulator_instance": profile.get(
                "emulator_instance"
            ),
            "state": emulator_status["state"],
            "message": emulator_status["message"],
            "reason": emulator_status.get("reason"),
            "device_id": emulator_status.get("device_id"),
            "expected_wakeup": expected_wakeup,
            "checked_at": monotonic(),
        }

        if emulator_status["state"] != STATE_HEALTHY:

            recovered = self._recover(
                profile,
                status,
            )
            if (
                first_check
                and expected_stopped
                and emulator_status.get("reason") == REASON_INSTANCE_STOPPED
                and recovered.get("state") == "Recovering"
            ):
                self._startup_emulator_deadlines[resource] = (
                    monotonic() + RECOVERY_TIMEOUT_SECONDS
                )
            if (
                expected_stopped
                and emulator_status.get("reason") == REASON_INSTANCE_STOPPED
                and recovered.get("state") == STATE_FAILED
            ):
                self._startup_emulator_deadlines.pop(resource, None)
                self.logger.error(
                    "Emulator Startup Failed",
                    **metadata,
                    reason=recovered.get("message"),
                    next_action="inspect emulator configuration and start the instance manually",
                )
            return recovered

        game_status = (
            self._prepare_profile_transition(profile)
        )
        if game_status.get("state") != STATE_HEALTHY:
            status["state"] = game_status["state"]
            status["message"] = game_status["message"]
            return status

        game_status = (
            self.game_runtime_service.validate(
                profile,
                emulator_status.get("device_id"),
                expected_stopped=resource in getattr(self, "_wake_resources", set()),
            )
        )

        status["game_status"] = game_status
        status["game"] = {
            "id": profile.get("game"),
            "package_name": game_status.get(
                "package_name",
                package_name,
            ),
        }

        if game_status["state"] == STATE_HEALTHY:
            if hasattr(self, "_health_failures"):
                self._health_failures.discard(resource)
            status["checked_at"] = monotonic()
            status["message"] = "Preparing profile startup"
            self._publish_profile_status(status)
            profile_id = self._profile_id(profile)
            validation_signature = (
                profile_id,
                profile.get("game", ""),
                profile.get("emulator_instance"),
            )
            if self._validation_profile_signatures.get(profile_id) != validation_signature:
                startup_status = self._run_startup_screen(profile)
                if startup_status["state"] != STATE_HEALTHY:
                    status["state"] = startup_status["state"]
                    status["message"] = startup_status["message"]
                    return status
                self._validation_profile_signatures[profile_id] = validation_signature

            status["state"] = STATE_HEALTHY
            status["message"] = "Profile healthy"
            if hasattr(self, "_health_failures"):
                self._health_failures.discard(resource)
            if hasattr(self, "_wake_resources"):
                self._wake_resources.discard(resource)

            self.recovery_service.clear_profile(
                profile
            )

            return status

        status["state"] = game_status["state"]
        status["message"] = game_status["message"]

        return self._recover(
            profile,
            status,
        )

    def _recover(
        self,
        profile,
        status,
    ):

        self.logger.warning(
            "Runtime Recovery Started",
            **self._metadata(profile),
            previous_state=status.get("state"),
            reason=status.get("reason") or status.get("message"),
        )

        profile_id = profile.get(
            "botstudio_profile_id",
            profile.get("id", ""),
        )
        self._scheduler_profile_signatures.pop(profile_id, None)
        self._validation_profile_signatures.pop(profile_id, None)
        self._active_profiles.pop(
            (profile.get("game", ""), profile.get("emulator_instance")),
            None,
        )

        status.update(state="Recovering", message="Recovering session")
        self._publish_profile_status(status)
        recovered = self.recovery_service.recover_profile(
            profile,
            status,
        )

        self._activate_profile_variables(profile, reset=True)

        status["state"] = recovered["state"]
        status["message"] = recovered["message"]

        self.logger.info(
            "Runtime Recovery Finished",
            **self._metadata(profile),
            state=status["state"],
            recovery_message=status["message"],
        )

        return status

    def _prepare_profile_transition(self, profile):
        """Markiert nur das Zielprofil. Der Wechsel selbst gehört dem Init Event
        (Profile Check entscheidet, Profile Switch führt aus)."""

        game_id = profile.get("game", "")
        instance = profile.get("emulator_instance")
        key = (game_id, instance)
        target_id = self._profile_id(profile)
        current_id = self._active_profiles.get(key)
        switch_required = bool(current_id) and current_id != target_id
        self._activate_profile_variables(
            profile,
            invalidate_live=switch_required,
        )

        if switch_required:
            # Das Init Event muss erneut laufen, damit Check/Switch greifen.
            self._scheduler_profile_signatures.pop(target_id, None)

        self._active_profiles[key] = target_id
        self.logger.debug(
            "Runtime Profile Selected",
            **self._metadata(profile),
            previous_profile_id=current_id,
            target_profile_id=target_id,
            switch_required=switch_required,
        )
        return {"state": STATE_HEALTHY, "message": "Profile selected"}

    def _protected_function_id(self, profile, kind: str) -> str:
        init_event = self._ensure_init_event(
            profile.get("game", ""),
            self._profile_id(profile),
        )
        task = init_event.protected_task(kind)
        return task.function_ids[0] if task and task.function_ids else ""

    def _run_startup_screen(self, profile):
        config = self.game_service.get_game_config(profile.get("game", ""))
        function_id = config.get("startup_screen") or ""
        if not function_id:
            self.logger.debug(
                "Runtime Startup Screen Skipped",
                **self._metadata(profile),
                reason="not_configured",
            )
            return {"state": STATE_HEALTHY, "message": "Startup screen skipped"}

        result = self._run_control_function(profile, function_id)
        if result.get("outcome") == "success":
            self.logger.info(
                "Runtime Startup Screen Reached",
                **self._metadata(profile),
                function_id=function_id,
            )
            return {"state": STATE_HEALTHY, "message": "Startup screen reached"}
        self.logger.error(
            "Runtime Startup Screen Failed",
            **self._metadata(profile),
            function_id=function_id,
            error=result.get("error"),
        )
        return {
            "state": STATE_FAILED,
            "message": result.get("error") or "Startup screen failed",
        }

    def test_profile_validation(self, profile):
        profile_id = str(profile.get("botstudio_profile_id") or profile.get("id") or "")
        self._activate_profile_variables(profile)
        function_id = self._protected_function_id(profile, PROFILE_CHECK_KIND)
        if not function_id:
            return {
                "state": STATE_HEALTHY,
                "message": "Profile validation skipped",
                "profile_id": profile_id,
                "game_id": profile.get("game", ""),
                "function_id": "",
            }

        result = self._run_control_function(profile, function_id)
        if result.get("outcome") != "success":
            return {
                "state": STATE_FAILED,
                "message": result.get("error") or "Profile validation failed",
                "profile_id": profile_id,
                "game_id": profile.get("game", ""),
                "function_id": function_id,
            }

        return {
            "state": STATE_HEALTHY,
            "message": "Profile validated",
            "profile_id": profile_id,
            "game_id": profile.get("game", ""),
            "function_id": function_id,
        }

    def test_profile_switch(self, profile, target_profile=None):
        game_id = profile.get("game", "")
        init_event = self._ensure_init_event(game_id, self._profile_id(profile))
        switch_task = init_event.protected_task(PROFILE_SWITCH_KIND)
        switch_id = switch_task.function_ids[0] if switch_task and switch_task.function_ids else ""
        if not switch_id:
            return {
                "state": STATE_FAILED,
                "message": "Profile switch function is not configured",
                "game_id": game_id,
                "function_id": "",
            }

        target_profile_id = str(
            target_profile
            or profile.get("botstudio_profile_id")
            or profile.get("id")
            or ""
        )
        switched_profile = dict(profile)
        switched_profile["botstudio_profile_id"] = target_profile_id
        switched_profile["name"] = profile.get("name", target_profile_id)
        self._activate_profile_variables(
            switched_profile,
            invalidate_live=True,
        )

        started_at = monotonic()
        result = self._run_control_function(switched_profile, switch_id)
        if result.get("outcome") != "cancelled":
            try:
                self.statistics_service.record_task(
                    game_id, target_profile_id, switched_profile["name"],
                    switch_task.task_id, switch_task.name,
                    "success" if result.get("outcome") == "success" else "failure",
                    monotonic() - started_at,
                )
            except (OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
                self.logger.error(
                    "Runtime Profile Switch Task Statistics Failed",
                    **self._metadata(switched_profile),
                    error_type=type(error).__name__,
                    next_action="inspect statistics storage; counts unavailable",
                )
        if result.get("outcome") != "success":
            return {
                "state": STATE_FAILED,
                "message": result.get("error") or "Profile switch failed",
                "game_id": game_id,
                "function_id": switch_id,
                "target_profile_id": target_profile_id,
            }

        validation = self.test_profile_validation(switched_profile)
        if validation["state"] == STATE_HEALTHY:
            try:
                self.statistics_service.record_profile_switch(switched_profile)
            except (OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
                self.logger.error(
                    "Runtime Profile Switch Statistics Failed",
                    **self._metadata(switched_profile),
                    error_type=type(error).__name__,
                    next_action="inspect statistics storage; counts unavailable",
                )
        return {
            "state": validation["state"],
            "message": validation["message"],
            "game_id": game_id,
            "function_id": switch_id,
            "target_profile_id": target_profile_id,
        }

    def test_emulator_switch(self, profile, target_instance):
        game_id = profile.get("game", "")
        function_id = self._protected_function_id(profile, PROFILE_CHECK_KIND)
        if not function_id:
            return {
                "state": STATE_HEALTHY,
                "message": "Profile validation skipped",
                "game_id": game_id,
                "function_id": "",
                "target_instance": target_instance,
            }

        switched_profile = dict(profile)
        switched_profile["emulator_instance"] = target_instance
        validation = self.test_profile_validation(switched_profile)
        return {
            "state": validation["state"],
            "message": validation["message"],
            "game_id": game_id,
            "function_id": function_id,
            "target_instance": target_instance,
        }

    def _run_control_function(self, profile, function_id):
        function = self.function_service.load(profile.get("game", ""), function_id)
        if not function:
            return {"outcome": "failed", "error": f"function '{function_id}' not found"}
        if not self.scheduler_runner_factory:
            return {"outcome": "failed", "error": "runtime runner is not configured"}

        runner = self.scheduler_runner_factory()
        context = ExecutionContext(
            game_id=profile.get("game", ""),
            profile_id=self._profile_id(profile),
            function_id=function.function_id,
            variable_service=self.variable_service,
            cancellation_checker=(
                partial(self.execution_cancelled, profile) if self.running else None
            ),
            event_due_handler=partial(
                self.scheduler_service.set_event_due,
                profile.get("game", ""), self._profile_id(profile),
            ),
        )
        return runner.run(function, context, self.resolve_runnable_profile(profile))

    @staticmethod
    def _profile_id(profile):
        return str(profile.get("botstudio_profile_id") or profile.get("id") or "")

    def _metadata(
        self,
        profile,
    ):

        return {
            "profile": profile.get("name"),
            "game": profile.get("game"),
            "emulator_instance": profile.get(
                "emulator_instance"
            ),
        }
