from datetime import datetime, timedelta
from threading import Lock
import time
from core.command_control import check_cancelled

from services.emulator_health_service import (
    REASON_SCREENSHOT_FAILED,
    REASON_UNRESPONSIVE,
)
from services.logging_service import get_logger


RECOVERY_TIMEOUT_SECONDS = 90

# the VM process is alive but Android is dead, so ADB and game calls are futile
FROZEN_REASONS = (
    REASON_UNRESPONSIVE,
    REASON_SCREENSHOT_FAILED,
)


class RuntimeRecoveryService:

    def __init__(
        self,
        adb_runtime_service,
        game_runtime_service,
        emulator_manager,
        app_config_service=None,
        statistics_service=None,
        logger=None,
    ):

        self.adb_runtime_service = adb_runtime_service
        self.game_runtime_service = game_runtime_service
        self.emulator_manager = emulator_manager
        self.app_config_service = app_config_service
        self.statistics_service = statistics_service
        self._last_emulator_start_at = None
        self._emulator_start_lock = Lock()
        self._next_emulator_start_at = 0.0

        self.logger = (
            logger
            or get_logger("runtime")
        )

        self.recovery_state = {}

    def recover_profile(
        self,
        profile,
        status,
    ):

        metadata = self._metadata(
            profile
        )

        instance = profile.get(
            "emulator_instance"
        )

        state = self._get_state(
            profile
        )

        self.logger.warning(
            "Recovery Started",
            **metadata,
            reason=status.get("reason"),
            trigger=status.get("message"),
            device_id=status.get("device_id"),
            elapsed_seconds=round(
                (
                    datetime.now()
                    - state["started_at"]
                ).total_seconds(),
                1,
            ),
            emulator_start_attempts=state[
                "emulator_start_attempts"
            ],
            timeout_seconds=RECOVERY_TIMEOUT_SECONDS,
        )

        if self._timed_out(
            state
        ):

            return self._restart_emulator(
                profile,
                state,
                metadata,
            )

        # a stale ADB port must never mask a stopped instance
        if not self.emulator_manager.is_instance_running(
            instance
        ):

            return self._start_emulator(
                profile,
                state,
                metadata,
                3,
                "Emulator start requested",
            )

        if status.get("reason") in FROZEN_REASONS:
            if status.get("expected_wakeup"):
                return {"state": "Recovering", "message": "Waiting for Android startup"}

            return self._restart_emulator(
                profile,
                state,
                metadata,
                status.get("reason"),
            )

        device_id = status.get(
            "device_id"
        )
        if status.get("expected_wakeup") and status.get("reason") == "device_offline":
            return {"state": "Recovering", "message": "Waiting for ADB startup"}

        if device_id:

            if self.adb_runtime_service.ensure_device(
                device_id,
                profile,
            ):
                self.logger.info(
                    "ADB Connection Verified",
                    **metadata,
                    level=1,
                    check="adb connect <device>",
                    device_id=device_id,
                    next_action="restore game foreground and verify process continuity",
                )

                package_name = self._package_name(
                    profile,
                    status,
                )

                if package_name:

                    return self.game_runtime_service.restore_game(
                        profile,
                        device_id,
                        package_name,
                    )

                return {
                    "state": "Recovering",
                    "message": "ADB connection verified; game package unavailable",
                }

        package_name = self._package_name(
            profile,
            status,
        )

        if device_id and package_name:

            return self.game_runtime_service.restore_game(
                profile,
                device_id,
                package_name,
            )

        return self._start_emulator(
            profile,
            state,
            metadata,
            3,
            "Emulator start requested",
        )

    def _start_emulator(
        self,
        profile,
        state,
        metadata,
        level,
        message,
    ):

        instance = profile.get(
            "emulator_instance"
        )

        try:
            delay_seconds = 0.0
            if self.app_config_service:
                settings = self.app_config_service.get_studio_settings()
                delay_seconds = max(
                    0.0,
                    float(settings.get("emulator_start_delay_seconds", 0) or 0),
                )
            deadline = self._reserve_emulator_start(delay_seconds)
            while time.monotonic() < deadline:
                check_cancelled()
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))

            check_cancelled()
            started = self.emulator_manager.start_instance(
                instance
            )
            if started:
                with self._emulator_start_lock:
                    self._last_emulator_start_at = time.monotonic()
                if self.statistics_service is not None:
                    try:
                        self.statistics_service.record_emulator_start(profile)
                    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
                        self.logger.error(
                            "Emulator Start Statistics Failed",
                            **metadata,
                            error_type=type(error).__name__,
                            next_action="inspect statistics storage; counts unavailable",
                        )

        except NotImplementedError:
            started = False

        if started:

            state["emulator_start_attempts"] += 1

            self.logger.info(
                "Emulator Started",
                **metadata,
                level=level,
                action="MuMuManager api -v <instance> launch_player",
                pid=self.emulator_manager.get_instance_pid(
                    instance
                ),
                attempt=state["emulator_start_attempts"],
                next_action="wait for adb device, then start game",
            )

            return {
                "state": "Recovering",
                "message": message,
                "emulator_started": True,
            }

        self.logger.warning(
            "Recovery Failed",
            **metadata,
            level=level,
            reason="emulator_start_unavailable",
            check="EmulatorManager.start_instance",
            expected="MuMuManager launch_player accepted",
            observed="start_instance returned False",
            mumu_manager_path=self.emulator_manager.mumu_manager_path,
            next_action="verify mumu_manager_path in config/emulator.json and start the instance manually",
        )

        return {
            "state": "Failed",
            "message": "Manual emulator recovery required",
        }

    def _reserve_emulator_start(self, delay_seconds):
        with self._emulator_start_lock:
            now = time.monotonic()
            previous = self._last_emulator_start_at
            deadline = max(
                now, self._next_emulator_start_at,
                previous + delay_seconds if previous is not None else now,
            )
            self._next_emulator_start_at = deadline + delay_seconds
            return deadline

    def clear_profile(
        self,
        profile,
    ):

        self.recovery_state.pop(
            profile.get("id"),
            None,
        )

    def _get_state(
        self,
        profile,
    ):

        profile_id = profile.get(
            "id"
        )

        if profile_id not in self.recovery_state:

            self.recovery_state[profile_id] = {
                "started_at": datetime.now(),
                "emulator_start_attempts": 0,
            }

        return self.recovery_state[profile_id]

    def _timed_out(
        self,
        state,
    ):

        return (
            datetime.now()
            - state["started_at"]
            >= timedelta(
                seconds=RECOVERY_TIMEOUT_SECONDS
            )
        )

    def _restart_emulator(
        self,
        profile,
        state,
        metadata,
        reason=None,
    ):

        instance = profile.get(
            "emulator_instance"
        )

        self.logger.warning(
            "Emulator Restart Required",
            **metadata,
            level=4,
            reason=reason or "recovery_timeout",
            trigger=(
                "android frozen while the vm process is alive"
                if reason
                else f"no healthy state within {RECOVERY_TIMEOUT_SECONDS}s"
            ),
            emulator_start_attempts=state[
                "emulator_start_attempts"
            ],
            next_action="stop_instance, verify, force kill by pid, start_instance",
        )

        if self.emulator_manager.is_instance_running(
            instance
        ):

            self.emulator_manager.stop_instance(
                instance
            )

            # a frozen instance can report a clean shutdown without stopping
            if self.emulator_manager.is_instance_running(
                instance
            ):

                self.emulator_manager.stop_instance(
                    instance,
                    force=True,
                )

                self.logger.warning(
                    "Emulator Crash Detected",
                    **metadata,
                    level=4,
                    action="force_kill",
                    check="MuMuManager shutdown_player then re-check instance state",
                    expected="instance stopped after graceful shutdown",
                    observed="instance still reported as running",
                    killed_pid=self.emulator_manager.get_instance_pid(
                        instance
                    ),
                    next_action="taskkill /PID <pid> /T /F, then start_instance",
                )

            else:

                self.logger.info(
                    "Emulator Stopped",
                    **metadata,
                    level=4,
                    action="graceful_shutdown",
                    check="MuMuManager shutdown_player",
                    next_action="start_instance",
                )

        state["started_at"] = datetime.now()
        state["emulator_start_attempts"] = 0

        return self._start_emulator(
            profile,
            state,
            metadata,
            4,
            "Emulator restart requested",
        )

    def _package_name(
        self,
        profile,
        status,
    ):

        game = status.get(
            "game"
        ) or {}

        package_name = game.get(
            "package_name"
        )

        if package_name:
            return package_name

        return (
            self.game_runtime_service
            .get_package_name(
                profile
            )
        )

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
