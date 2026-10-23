from pathlib import Path
from functools import wraps
import json
from threading import RLock
from typing import Callable, ParamSpec, TypeVar

from core.emulator_manager import (
    EmulatorManager,
)

from core.adb_service import (
    AdbService,
)
from services.reinitialize_service import get_default_reinitialize_service
from services.file_io import atomic_write_json
from services.app_config_service import AppConfigService


GAME_STORE_LOCK = RLock()
_P = ParamSpec("_P")
_R = TypeVar("_R")


def game_store_locked(operation: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(operation)
    def locked(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        with GAME_STORE_LOCK:
            return operation(*args, **kwargs)
    return locked


class GameService:

    def __init__(
        self,
        file_name: str = "games/games.json",
        reinitialize_service=None,
    ):

        self.file_name = Path(
            file_name
        )

        self.file_name.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        self.reinitialize_service = (
            reinitialize_service or get_default_reinitialize_service()
        )

        if not self.file_name.exists():
            self.save([])

        self.emulator_manager = (
            EmulatorManager()
        )

        self.adb_service = (
            AdbService(
                lambda: self.emulator_manager.adb_path
            )
        )

    def load(
        self,
    ):

        if not self.file_name.exists():

            return []

        with open(
            self.file_name,
            "r",
            encoding="utf-8",
        ) as fp:

            return json.load(
                fp
            )

    @game_store_locked
    def save(
        self,
        games,
    ):

        atomic_write_json(self.file_name, games)
        if self.reinitialize_service:
            self.reinitialize_service.publish(
                "game",
                count=len(games),
            )

    def get_game(
        self,
        game_id: str,
    ):

        for game in self.load():

            if (
                game["id"]
                == game_id
            ):
                return game

        return None

    def get_default_game(
        self,
    ):

        games = self.load()

        if not games:
            return None

        for game in games:

            if game.get(
                "is_default",
                False,
            ):
                return game

        return games[0]

    @game_store_locked
    def create_game(
        self,
        game_id: str,
        display_name: str,
    ):

        games = self.load()

        existing = self.get_game(
            game_id
        )

        if existing:
            return existing

        game = {
            "id": game_id,
            "display_name": display_name,
            "package_name": "",
            "is_default": (
                len(games) == 0
            ),
        }

        games.append(
            game
        )

        self.save(
            games
        )

        return game

    @game_store_locked
    def set_default_game(
        self,
        game_id: str,
    ):

        games = self.load()

        for game in games:

            game[
                "is_default"
            ] = (
                game["id"]
                == game_id
            )

        self.save(
            games
        )

    # DEC-087: instance/game settings and the MultiAccount switch convention
    DEFAULT_SETTINGS = {
        "idle_behavior": "do_nothing",
        "max_idle_minutes": 3,
        "max_concurrent_instances": 1,
        "limit_profile_active_time": False,
        "max_active_minutes": 60,
        "stop_behavior": "do_nothing",
        "auto_start_all_profiles": False,
        "stop_behavior_telegram": "do_nothing",
        "stop_behavior_telegram_delay_minutes": 5,
        "stop_behavior_telegram_mode": "continuous",
    }

    DEFAULT_GAME_CONFIG = {
        "package_name": "",
        "startup_screen": "",
    }

    # Profile Check/Switch leben als geschützte Init Tasks (DEC-097); diese
    # Schlüssel existieren nur noch, um Altstände einmalig zu übernehmen.
    LEGACY_INIT_FUNCTION_KEYS = {
        "profile_validation": "profile_validation_function_id",
        "profile_switch": "profile_switch_function_id",
    }
    LEGACY_SWITCH_FALLBACK_KEY = "multi_account_switch_function_id"

    # Runtime publishes both target identifiers together for profile checks
    # and switch helpers.
    MULTI_ACCOUNT_VARIABLE = "global:MultiAccount_NextAccountName"
    MULTI_ACCOUNT_PROFILE_ID_VARIABLE = "global:MultiAccount_NextProfileId"

    def get_settings(
        self,
        game_id: str,
    ) -> dict:

        game = self.get_game(game_id) or {}
        settings = dict(self.DEFAULT_SETTINGS)
        settings.update(game.get("settings", {}))
        for key in self.DEFAULT_GAME_CONFIG:
            settings.pop(key, None)
        return settings

    def get_game_config(
        self,
        game_id: str,
    ) -> dict:

        game = self.get_game(game_id) or {}
        stored = dict(game.get("settings", {}))
        stored.update(game.get("config", {}))

        config = dict(self.DEFAULT_GAME_CONFIG)
        for key in self.DEFAULT_GAME_CONFIG:
            if stored.get(key):
                config[key] = stored[key]

        if not config.get("package_name") and game.get("package_name"):
            config["package_name"] = game["package_name"]

        return config

    def get_scheduling_overrides(self, game_id: str) -> dict:
        game = self.get_game(game_id) or {}
        return AppConfigService.validate_scheduling_settings(
            game.get("scheduling_overrides", {})
        )

    @game_store_locked
    def save_scheduling_overrides(
        self, game_id: str, overrides: dict | None
    ) -> dict:
        validated = AppConfigService.validate_scheduling_settings(
            {} if overrides is None else overrides
        )
        games = self.load()
        for game in games:
            if game["id"] == game_id:
                if validated:
                    game["scheduling_overrides"] = validated
                else:
                    game.pop("scheduling_overrides", None)
                self.save(games)
                return dict(validated)
        raise ValueError(f"Unknown game: {game_id}")

    def legacy_init_function_ids(
        self,
        game_id: str,
    ) -> dict:
        """Altbestand für die einmalige Übernahme in die geschützten Init Tasks."""

        game = self.get_game(game_id) or {}
        stored = dict(game.get("settings", {}))
        stored.update(game.get("config", {}))

        seed = {}
        for kind, key in self.LEGACY_INIT_FUNCTION_KEYS.items():
            seed[kind] = str(stored.get(key) or "")

        if not seed["profile_switch"]:
            seed["profile_switch"] = str(
                stored.get(self.LEGACY_SWITCH_FALLBACK_KEY) or ""
            )

        return seed

    @game_store_locked
    def save_settings(
        self,
        game_id: str,
        settings: dict,
    ):

        games = self.load()

        for game in games:
            if game["id"] == game_id:
                cleaned = {
                    key: value
                    for key, value in settings.items()
                    if key not in self.DEFAULT_GAME_CONFIG
                }
                merged = dict(self.DEFAULT_SETTINGS)
                merged.update(game.get("settings", {}))
                merged.update(cleaned)
                game["settings"] = merged
                for key in self.DEFAULT_GAME_CONFIG:
                    game["settings"].pop(key, None)
                break
        else:
            return None

        self.save(games)

        return merged

    @game_store_locked
    def save_game_config(
        self,
        game_id: str,
        config: dict,
    ):

        games = self.load()

        for game in games:
            if game["id"] == game_id:
                stored = dict(game.get("config", {}))
                stored.update(config)
                # Nur noch bekannte Schlüssel bleiben stehen; Altschlüssel für
                # Profile Check/Switch verschwinden mit dem ersten Speichern.
                config_merged = {
                    key: stored.get(key, default)
                    for key, default in self.DEFAULT_GAME_CONFIG.items()
                }
                # Package Name lebt doppelt (Top-Level + Config); ein leerer
                # Wert darf einen vorhandenen niemals überschreiben.
                config_merged["package_name"] = (
                    config_merged.get("package_name")
                    or game.get("package_name", "")
                )
                game["config"] = config_merged
                game["package_name"] = config_merged["package_name"]
                settings = dict(game.get("settings", {}))
                for key in self.DEFAULT_GAME_CONFIG:
                    settings.pop(key, None)
                game["settings"] = settings
                break
        else:
            return None

        self.save(games)

        return config_merged

    def detect_package(
        self,
        emulator_instance: int,
    ) -> str | None:

        device_id = (
            self.emulator_manager.get_device_id(
                emulator_instance
            )
        )

        if not self.adb_service.ensure_connection(device_id):
            raise ConnectionError("Could not connect to the selected MuMu instance.")

        result = self.adb_service.shell_result(
            device_id,
            [
                "dumpsys",
                "window",
            ],
        )

        if result is None or result.returncode != 0:
            raise ConnectionError("Could not read the foreground app from MuMu.")

        for line in result.stdout.splitlines():

            if (
                "mCurrentFocus"
                not in line
            ):
                continue

            if "/" not in line:
                continue

            package = (
                line.split("/")[
                    0
                ]
                .split()[
                    -1
                ]
            )

            return package

        return None

    @game_store_locked
    def update_package(
        self,
        game_id: str,
        package_name: str,
    ):

        games = self.load()

        for game in games:

            if (
                game["id"]
                == game_id
            ):

                game[
                    "package_name"
                ] = package_name

                config = dict(
                    self.DEFAULT_GAME_CONFIG
                )
                config.update(
                    game.get(
                        "config",
                        {},
                    )
                )
                config[
                    "package_name"
                ] = package_name
                game[
                    "config"
                ] = config

                break

        self.save(
            games
        )