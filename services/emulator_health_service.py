from core.emulator import Emulator, remove_screenshot
from core.command_control import CommandCancelled
from services.logging_service import get_logger


STATE_HEALTHY = "Healthy"
STATE_RECOVERING = "Recovering"
STATE_FAILED = "Failed"
# a MultiAccount profile waiting its turn on a shared instance (DEC-089)
STATE_QUEUED = "Queued"

REASON_INVALID_INSTANCE = "invalid_instance"
REASON_INSTANCE_STOPPED = "instance_stopped"
REASON_DEVICE_OFFLINE = "device_offline"
REASON_UNRESPONSIVE = "emulator_unresponsive"
REASON_SCREENSHOT_FAILED = "screenshot_failed"


class EmulatorHealthService:

    def __init__(
        self,
        emulator_manager,
        adb_runtime_service,
        logger=None,
    ):

        self.emulator_manager = emulator_manager
        self.adb_runtime_service = adb_runtime_service

        self.logger = (
            logger
            or get_logger("emulator")
        )

    def validate(
        self,
        profile,
        *,
        expected_stopped=False,
        expected_wakeup=False,
    ):

        metadata = self._metadata(
            profile
        )

        instance = profile.get(
            "emulator_instance"
        )

        if not self.emulator_manager.validate_instance(
            instance
        ):

            self.logger.error(
                "Emulator Validation Failed",
                **metadata,
                reason=REASON_INVALID_INSTANCE,
                check="EmulatorManager.validate_instance",
                expected="emulator_instance >= 0",
                observed=instance,
                next_action="fix emulator_instance in the profile",
            )

            return self._result(
                STATE_FAILED,
                "Invalid emulator instance",
                reason=REASON_INVALID_INSTANCE,
            )

        device_id = self.emulator_manager.get_device_id(
            instance
        )

        if not self.emulator_manager.is_instance_running(
            instance
        ):

            info = self.emulator_manager.get_instance_info(
                instance
            )

            log_stopped = self.logger.info if expected_stopped else self.logger.error
            log_stopped(
                "Emulator Startup Required" if expected_stopped else "Emulator Not Running",
                **metadata,
                reason=REASON_INSTANCE_STOPPED,
                check="MuMuManager info -v <instance>",
                expected="is_process_started=True is_android_started=True",
                observed=(
                    f"is_process_started={info.get('is_process_started')} "
                    f"is_android_started={info.get('is_android_started')} "
                    f"player_state={info.get('player_state')}"
                ),
                mumu_pid=info.get("pid"),
                launch_err_msg=info.get("launch_err_msg"),
                device_id=device_id,
                next_action="recovery level 3: start_instance",
            )

            return self._result(
                STATE_FAILED,
                "Emulator not running",
                device_id,
                reason=REASON_INSTANCE_STOPPED,
            )

        connection_args = {"expected_offline": True} if expected_wakeup else {}
        log_transient = self.logger.info if expected_wakeup else self.logger.error
        if not self.adb_runtime_service.ensure_device(device_id, profile, **connection_args):

            log_transient(
                "ADB Device Lost",
                **metadata,
                reason=REASON_DEVICE_OFFLINE,
                check="adb devices / adb connect",
                expected=f"{device_id} state=device",
                observed=self.adb_runtime_service.adb.devices()
                .replace("\n", " ")
                .strip(),
                device_id=device_id,
                next_action="recovery level 1: adb reconnect",
            )

            return self._result(
                STATE_FAILED,
                "Device offline",
                device_id,
                reason=REASON_DEVICE_OFFLINE,
            )

        if not self.adb_runtime_service.adb.is_responsive(
            device_id
        ):

            log_transient(
                "Emulator Not Responding",
                **metadata,
                reason=REASON_UNRESPONSIVE,
                check="adb shell cmd activity get-current-user",
                expected="numeric user ID from Android ActivityManager",
                observed="no answer within timeout (android frozen)",
                timeout_seconds=5,
                hint="adb still reports state=device because adbd answers on TCP",
                device_id=device_id,
                next_action="recovery level 4: stop_instance, force kill by pid, start_instance",
            )

            return self._result(
                STATE_FAILED,
                "Emulator not responding",
                device_id,
                reason=REASON_UNRESPONSIVE,
            )

        screenshot_size, capture_error = self._probe_screenshot(device_id)
        if screenshot_size == 0:

            log_transient(
                "Emulator Validation Failed",
                **metadata,
                reason=REASON_SCREENSHOT_FAILED,
                check="adb exec-out screencap -p",
                expected="png file with size > 0",
                observed=capture_error or "empty screenshot",
                device_id=device_id,
                next_action="recovery level 4: restart emulator instance",
            )

            return self._result(
                STATE_FAILED,
                "Screenshot failed",
                device_id,
                reason=REASON_SCREENSHOT_FAILED,
            )

        self.logger.debug(
            "Emulator Healthy",
            **metadata,
            device_id=device_id,
        )

        return self._result(
            STATE_HEALTHY,
            "Emulator healthy",
            device_id,
        )

    @staticmethod
    def _probe_screenshot(device_id):
        emulator = Emulator(device_id=device_id)
        emulator.command_timeout = 2
        screenshot = None
        try:
            screenshot = emulator.screenshot(attempts=1)
            return screenshot.stat().st_size, None
        except CommandCancelled:
            raise
        except (RuntimeError, OSError) as error:
            return 0, str(error)
        finally:
            if screenshot is not None:
                remove_screenshot(screenshot)

    def probe_screenshot(self, device_id):
        size, _error = self._probe_screenshot(device_id)
        return size > 0

    def _result(
        self,
        state,
        message,
        device_id=None,
        reason=None,
    ):

        return {
            "state": state,
            "message": message,
            "device_id": device_id,
            "reason": reason,
        }

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
