import json
from pathlib import Path
from services.reinitialize_service import get_default_reinitialize_service


class AppConfigService:

    SCHEDULING_DEFAULTS = {
        "limit_profile_active_time": False,
        "max_active_minutes": 60,
        "scheduling_lookahead_seconds": 120,
        "scheduling_max_wait_minutes": 120,
    }

    STUDIO_SETTINGS_DEFAULTS = {
        **SCHEDULING_DEFAULTS,
        "idle_behavior": "do_nothing",
        "max_idle_minutes": 3,
        "max_concurrent_instances": 1,
        "emulator_start_delay_seconds": 5,
        "startup_view": "game",
        "language": "en",
        "stop_behavior": "do_nothing",
        "auto_start_all_profiles": False,
        "stop_behavior_telegram": "do_nothing",
        "stop_behavior_telegram_delay_minutes": 5,
        "stop_behavior_telegram_mode": "continuous",
        "log_level": "normal",
        "max_log_file_size_mb": 1,
        "max_archive_files": 20,
        "telegram_settings_configured": False,
        "telegram_enabled": False,
        "telegram_allowed_user_ids": "",
        "telegram_allowed_chat_ids": "",
        "watchdog_auto_start": False,
    }

    def __init__(
        self,
        file_name: str = "config/app_config.json",
        reinitialize_service=None,
    ):

        self.file_name = Path(
            file_name
        )
        self.reinitialize_service = (
            reinitialize_service or get_default_reinitialize_service()
        )

    def load(
        self,
    ):

        if not self.file_name.exists():

            return {
                "active_game": ""
            }

        with open(
            self.file_name,
            "r",
            encoding="utf-8",
        ) as fp:

            return json.load(
                fp
            )

    def save(
        self,
        config,
    ):

        self.file_name.parent.mkdir(parents=True, exist_ok=True)

        with open(
            self.file_name,
            "w",
            encoding="utf-8",
        ) as fp:

            json.dump(
                config,
                fp,
                indent=4,
            )
        if self.reinitialize_service:
            self.reinitialize_service.publish(
                "app_config",
                keys=list(config),
            )

    def get_studio_settings(
        self,
    ) -> dict:

        config = self.load()
        settings = dict(self.STUDIO_SETTINGS_DEFAULTS)
        settings.update(config.get("studio_settings", {}))
        return settings

    def save_studio_settings(
        self,
        settings: dict,
    ):

        config = self.load()
        merged = dict(self.STUDIO_SETTINGS_DEFAULTS)
        merged.update(config.get("studio_settings", {}))
        merged.update(settings)
        self.validate_scheduling_settings({
            key: merged[key] for key in self.SCHEDULING_DEFAULTS
        })
        config["studio_settings"] = merged
        self.save(config)
        return merged

    @classmethod
    def validate_scheduling_settings(cls, settings: dict) -> dict:
        """Validate a complete or partial scheduling configuration without coercion."""
        if not isinstance(settings, dict):
            raise ValueError("Scheduling settings must be a dictionary.")
        unknown = set(settings) - cls.SCHEDULING_DEFAULTS.keys()
        if unknown:
            raise ValueError(
                "Unknown scheduling settings: "
                + ", ".join(sorted(str(key) for key in unknown))
            )
        ranges = {
            "max_active_minutes": (1, 100000),
            "scheduling_lookahead_seconds": (0, 3600),
            "scheduling_max_wait_minutes": (1, 100000),
        }
        for key, value in settings.items():
            if key == "limit_profile_active_time":
                if type(value) is not bool:
                    raise ValueError(f"{key} must be a boolean.")
                continue
            minimum, maximum = ranges[key]
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(
                    f"{key} must be an integer between {minimum} and {maximum}."
                )
        return dict(settings)

    def get_scheduling_settings(self, game_service, game_id: str) -> dict:
        settings = dict(self.SCHEDULING_DEFAULTS)
        settings.update({
            key: value
            for key, value in self.get_studio_settings().items()
            if key in self.SCHEDULING_DEFAULTS
        })
        self.validate_scheduling_settings(settings)
        if game_id:
            settings.update(
                self.validate_scheduling_settings(
                    game_service.get_scheduling_overrides(game_id)
                )
            )
        return self.validate_scheduling_settings(settings)

    def get_active_game(
        self,
    ) -> str:

        config = self.load()

        return config.get(
            "active_game",
            "",
        )

    def set_active_game(
        self,
        game_id: str,
    ):

        config = self.load()

        config[
            "active_game"
        ] = game_id

        self.save(
            config
        )

    def is_expert_mode(
        self,
    ) -> bool:
        """Sealed helper functions stay read only until expert mode is unlocked."""

        return bool(
            self.load().get(
                "expert_mode",
                False,
            )
        )

    def is_helper_editing_allowed(
        self,
    ) -> bool:
        """Temporary override for helper-function editing without expert mode."""

        config = self.load()

        return bool(
            config.get(
                "allow_helper_editing",
                config.get(
                    "disable_expert_mode",
                    False,
                ),
            )
        )

    def set_expert_mode(
        self,
        enabled: bool,
    ):

        config = self.load()

        config[
            "expert_mode"
        ] = bool(enabled)

        self.save(
            config
        )

    def set_helper_editing_allowed(
        self,
        enabled: bool,
    ):

        config = self.load()

        config[
            "allow_helper_editing"
        ] = bool(enabled)

        self.save(
            config
        )