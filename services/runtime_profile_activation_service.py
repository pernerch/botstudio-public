from services.logging_service import get_logger
from services.profile_service import ProfileService
from services.game_service import GameService


class RuntimeProfileActivationService:

    def __init__(
        self,
        profile_service=None,
        game_service=None,
        logger=None,
    ):

        self.profile_service = (
            profile_service
            or ProfileService()
        )

        self.game_service = (
            game_service
            or GameService()
        )

        self.logger = (
            logger
            or get_logger("runtime")
        )

    def get_profiles(
        self,
    ):

        return self.profile_service.load_all()

    def get_enable_conflict(
        self,
        profile,
    ):

        if not profile:
            return None

        instance = profile.get(
            "emulator_instance"
        )

        game_id = profile.get(
            "game"
        )

        conflicts = []

        for other in self.profile_service.load_all():

            if other.get("id") == profile.get("id"):
                continue

            if not other.get("enabled", False):
                continue

            if other.get("emulator_instance") != instance:
                continue

            if other.get("game") == game_id:
                continue

            conflicts.append(
                other
            )

        if not conflicts:
            return None

        self.logger.warning(
            "Conflict Detected",
            profile=profile.get("name"),
            game=game_id,
            emulator_instance=instance,
            conflicts=[
                item.get("name")
                for item in conflicts
            ],
        )

        return {
            "profile": profile,
            "conflicts": conflicts,
            "emulator_instance": instance,
        }

    def set_enabled(
        self,
        profile_id: str,
        enabled: bool,
    ):

        profile = self.profile_service.get_profile(
            profile_id
        )

        if not profile:

            return {
                "changed": False,
                "conflict": None,
            }

        if enabled:

            conflict = self.get_enable_conflict(
                profile
            )

            if conflict:

                return {
                    "changed": False,
                    "conflict": conflict,
                }

        profile["enabled"] = enabled

        self.profile_service.save_profile(
            profile
        )

        self._log_enabled_change(
            profile,
            enabled,
        )

        return {
            "changed": True,
            "conflict": None,
        }

    def resolve_conflict_by_replacing(
        self,
        profile_id: str,
        conflicts,
    ):

        profiles = self.profile_service.load_all()

        conflict_ids = {
            profile.get("id")
            for profile in conflicts
        }

        target_profile = None

        for profile in profiles:

            if profile.get("id") in conflict_ids:
                profile["enabled"] = False
                self._log_enabled_change(
                    profile,
                    False,
                )

            if profile.get("id") == profile_id:
                profile["enabled"] = True
                target_profile = profile

        self.profile_service.save_all(
            profiles
        )

        if target_profile:

            self._log_enabled_change(
                target_profile,
                True,
            )

            self.logger.info(
                "Conflict Resolved",
                profile=target_profile.get("name"),
                game=target_profile.get("game"),
                emulator_instance=target_profile.get(
                    "emulator_instance"
                ),
                disabled=[
                    item.get("name")
                    for item in conflicts
                ],
            )

        return target_profile is not None

    def get_game_display_name(
        self,
        game_id: str,
    ):

        game = self.game_service.get_game(
            game_id
        )

        if not game:
            return game_id

        return game.get(
            "display_name",
            game_id,
        )

    def _log_enabled_change(
        self,
        profile,
        enabled,
    ):

        message = (
            "Profile Enabled"
            if enabled
            else "Profile Disabled"
        )

        self.logger.info(
            message,
            profile=profile.get("name"),
            game=profile.get("game"),
            emulator_instance=profile.get(
                "emulator_instance"
            ),
        )
