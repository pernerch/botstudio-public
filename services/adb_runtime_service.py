from core.adb_service import AdbService
from collections.abc import Callable
from services.logging_service import get_logger


class AdbRuntimeService:

    def __init__(
        self,
        adb_path: str | Callable[[], str],
        logger=None,
    ):

        self.adb = AdbService(
            adb_path
        )

        self.logger = (
            logger
            or get_logger("adb")
        )

    def server_available(
        self,
    ) -> bool:

        output = self.adb.devices()

        available = (
            "list of devices attached"
            in output.lower()
        )

        if available:
            self.logger.info(
                "ADB Server Found",
                check="adb devices",
                adb_path=str(self.adb.adb_path),
            )
        else:
            self.logger.warning(
                "ADB Server Not Found",
                check="adb devices",
                expected="output containing 'List of devices attached'",
                observed=output.replace("\n", " ").strip() or "no output",
                adb_path=str(self.adb.adb_path),
                next_action="verify adb_path in config/emulator.json",
            )

        return available

    def ensure_device(
        self,
        device_id: str,
        profile=None,
        *,
        expected_offline=False,
    ) -> bool:

        if self.adb.is_connected(
            device_id
        ):

            self.logger.debug(
                "ADB Device Connected",
                **self._metadata(profile),
                check="adb devices",
                device_id=device_id,
            )

            return True

        self.logger.info(
            "ADB Reconnect Started",
            **self._metadata(profile),
            check="adb connect",
            reason="device not listed with state=device",
            device_id=device_id,
        )

        connected = self.adb.connect(
            device_id
        )

        if connected:

            self.logger.info(
                "ADB Reconnect Successful",
                **self._metadata(profile),
                check="adb connect",
                device_id=device_id,
            )

        else:

            log_failed = self.logger.info if expected_offline else self.logger.error
            log_failed(
                "ADB Reconnect Failed",
                **self._metadata(profile),
                check="adb connect",
                expected=f"connected to {device_id}",
                observed=self.adb.devices().replace("\n", " ").strip()
                or "no devices",
                device_id=device_id,
                next_action="emulator is likely not running: recovery level 3 start_instance",
            )

        return connected

    def ensure_connection(
        self,
        device_id: str,
    ) -> bool:
        """Vertrag des FunctionRunner; identisch zu `ensure_device` ohne Profil."""

        return self.ensure_device(device_id)

    def _metadata(
        self,
        profile,
    ):

        if not profile:
            return {}

        return {
            "profile": profile.get("name"),
            "game": profile.get("game"),
            "emulator_instance": profile.get(
                "emulator_instance"
            ),
        }
