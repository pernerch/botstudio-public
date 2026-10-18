from core.adb_service import AdbService
from services.logging_service import get_logger


class GameRuntimeService:

    def __init__(
        self,
        adb_path: str,
        game_service,
        logger=None,
    ):

        self.adb = AdbService(
            adb_path
        )

        self.game_service = game_service

        self.logger = (
            logger
            or get_logger("runtime")
        )

    def validate(
        self,
        profile,
        device_id: str,
        *,
        expected_stopped=False,
    ):

        package_name = self.get_package_name(
            profile
        )

        if not package_name:

            self.logger.error(
                "Game Validation Failed",
                **self._metadata(profile),
                reason="package_missing",
                check="games/games.json lookup by profile.game",
                expected="package_name for game id",
                observed=None,
                device_id=device_id,
                next_action="set package_name in Game Setup tab",
            )

            return {
                "state": "Failed",
                "message": "Game package missing",
                "package_name": None,
            }

        current_package = self.adb.get_foreground_package(
            device_id
        )

        if current_package == package_name:

            if self.adb.has_anr(
                device_id,
                package_name,
            ):

                log_missing = self.logger.info if expected_stopped else self.logger.warning
                log_missing(
                    "Game Not Responding",
                    **self._metadata(profile),
                    reason="anr",
                    check="adb shell dumpsys window | mFocusedApp",
                    expected="focused app without 'not responding'",
                    observed="mFocusedApp reports not responding",
                    package_name=package_name,
                    device_id=device_id,
                    next_action="recovery level 2: restart game via monkey",
                )

                return {
                    "state": "Recovering",
                    "message": "Game not responding",
                    "package_name": package_name,
                }

            if not self.adb.get_package_pid(
                device_id,
                package_name,
            ):

                self.logger.warning(
                    "Game Process Missing",
                    **self._metadata(profile),
                    reason="no_pid",
                    check="adb shell pidof <package>",
                    expected="android pid for package",
                    observed="empty pidof output",
                    hint="package is foreground but the process is gone",
                    package_name=package_name,
                    device_id=device_id,
                    next_action="recovery level 2: restart game via monkey",
                )

                return {
                    "state": "Recovering",
                    "message": "Game process missing",
                    "package_name": package_name,
                }

            self.logger.debug(
                "Game Healthy",
                **self._metadata(profile),
                package_name=package_name,
            )

            return {
                "state": "Healthy",
                "message": "Game running",
                "package_name": package_name,
            }

        self.logger.warning(
            "Game Not Running",
            **self._metadata(profile),
            reason="foreground_mismatch",
            check="adb shell dumpsys window | mCurrentFocus",
            expected=package_name,
            observed=current_package,
            device_id=device_id,
            next_action="recovery level 2: start game via monkey",
        )

        return {
            "state": "Recovering",
            "message": "Game not running",
            "package_name": package_name,
        }

    def start_game(
        self,
        profile,
        device_id: str,
        package_name: str,
    ) -> bool:

        self.logger.info(
            "Restart game",
            **self._metadata(profile),
            check="adb shell monkey -p <package> -c LAUNCHER 1",
            package_name=package_name,
            device_id=device_id,
        )

        return self.adb.shell_success(
            device_id,
            [
                "monkey",
                "-p",
                package_name,
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
            ],
        )

    def get_package_name(
        self,
        profile,
    ):

        game = self.game_service.get_game(
            profile.get("game")
        )

        if not game:
            return None

        return game.get(
            "package_name"
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
