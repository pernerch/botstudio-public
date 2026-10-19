from services.profile_service import (
    ProfileService,
)

from core.emulator_manager import (
    EmulatorManager,
)

from core.adb_service import (
    AdbService,
)


class ProfileRuntimeService:

    def __init__(
        self,
    ):

        self.profile_service = (
            ProfileService()
        )

        self.emulator_manager = (
            EmulatorManager()
        )

        self.adb_service = (
            AdbService(
                lambda: self.emulator_manager.adb_path
            )
        )

    def get_active_profile(
        self,
        game_id: str,
    ):

        return (
            self.profile_service.get_default_profile(
                game_id
            )
        )

    def get_emulator_instance(
        self,
        profile: dict,
    ) -> int | None:

        if not profile:
            return None

        return profile.get(
            "emulator_instance"
        )

    def get_device_id(
        self,
        profile: dict,
    ) -> str | None:

        emulator_instance = (
            self.get_emulator_instance(
                profile
            )
        )

        if emulator_instance is None:
            return None

        return (
            self.emulator_manager.get_device_id(
                emulator_instance
            )
        )

    def connect_profile(
        self,
        profile: dict,
    ):

        device_id = (
            self.get_device_id(
                profile
            )
        )

        if not device_id:
            return False

        try:

            self.adb_service.connect(
                device_id
            )

            return True

        except Exception:

            return False

    def get_runtime_context(
        self,
        game_id: str,
    ) -> dict:

        profile = (
            self.get_active_profile(
                game_id
            )
        )

        if not profile:

            return {
                "game_id": game_id,
                "profile": None,
                "device_id": None,
                "connected": False,
            }

        device_id = (
            self.get_device_id(
                profile
            )
        )

        connected = (
            self.connect_profile(
                profile
            )
        )

        return {
            "game_id": game_id,
            "profile": profile,
            "device_id": device_id,
            "connected": connected,
        }
    def disconnect_profile(
        self,
        profile: dict,
    ) -> bool:

        device_id = (
            self.get_device_id(
                profile
            )
        )

        if not device_id:
            return False

        try:

            return (
                self.adb_service.disconnect(
                    device_id
                )
            )

        except Exception:

            return False