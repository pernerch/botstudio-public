"""
Profile Service

Verantwortlich für:

- Profile laden
- Einzelne Profile abrufen
- Aktivierte Profile liefern
- Profile nach Instanz gruppieren
- Profile nach Spiel filtern
- Standardprofil bestimmen
- Profile validieren

Keine Scheduler-Logik.
Keine Emulator-Logik.
Keine ADB-Logik.
"""
import uuid

from pathlib import Path
import json
from services.reinitialize_service import get_default_reinitialize_service


class ProfileGameFrozenError(ValueError):
    """Raised when a persisted profile is assigned to another game."""


class ProfileIdentityError(ValueError):
    """Raised when a technical profile identity is changed."""


class ProfileService:

    def __init__(
        self,
        profile_file: str = "profiles/profiles.json",
        custom_field_file: str | None = None,
        reinitialize_service=None,
    ) -> None:

        self.profile_file = Path(
            profile_file
        )

        self.profile_file.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        if not self.profile_file.exists():
            with open(
                self.profile_file,
                "w",
                encoding="utf-8",
            ) as handle:
                json.dump(
                    [],
                    handle,
                    indent=4,
                )

        self.custom_field_file = Path(
            custom_field_file or self.profile_file.parent / "custom_fields.json"
        )
        if not self.custom_field_file.exists():
            with open(
                self.custom_field_file,
                "w",
                encoding="utf-8",
            ) as handle:
                json.dump(
                    {},
                    handle,
                    indent=4,
                )

        self.reinitialize_service = (
            reinitialize_service or get_default_reinitialize_service()
        )

    def load_all(
        self,
    ):

        if not self.profile_file.exists():
            return []

        with open(
            self.profile_file,
            "r",
            encoding="utf-8",
        ) as handle:
            profiles = json.load(handle)

        indexed_profiles = list(enumerate(profiles))
        indexed_profiles.sort(
            key=lambda indexed: (
                str(indexed[1].get("game", "")).casefold(),
                self._profile_order(indexed[1], indexed[0]),
                indexed[0],
            )
        )
        return [profile for _, profile in indexed_profiles]

    @staticmethod
    def _profile_order(profile: dict, fallback: int) -> int:
        try:
            return int(profile.get("account_order", fallback))
        except (TypeError, ValueError):
            return fallback

    def reorder_profiles(self, game_id: str, profile_ids: list[str]) -> None:
        profiles = self.load_all()
        positions = {str(profile_id): index for index, profile_id in enumerate(profile_ids)}
        for profile in profiles:
            if str(profile.get("game", "")) != str(game_id):
                continue
            profile_id = str(
                profile.get("botstudio_profile_id") or profile.get("id") or ""
            )
            if profile_id in positions:
                profile["account_order"] = positions[profile_id]
        self.save_all(profiles)

    def save_all(
        self,
        profiles: list[dict],
    ) -> None:

        with open(
            self.profile_file,
            "w",
            encoding="utf-8",
        ) as handle:

            json.dump(
                profiles,
                handle,
                indent=4,
            )
        if self.reinitialize_service:
            self.reinitialize_service.publish("profile", count=len(profiles))

    def load_enabled(
        self,
    ):
        return [
            profile
            for profile in self.load_all()
            if profile.get(
                "enabled",
                False,
            )
        ]

    def get_profile(
        self,
        profile_id: str,
    ) -> dict | None:

        for profile in self.load_all():

            if (
                profile.get("botstudio_profile_id") == profile_id
                or profile.get("id") == profile_id
            ):
                return profile

        return None

    def get_profiles_for_game(
        self,
        game_id: str,
    ):
        return [
            profile
            for profile in self.load_enabled()
            if profile.get("game")
            == game_id
        ]

    def build_runtime_globals(
        self,
        profile: dict,
    ) -> dict:
        """Expose profile fields under immutable BotStudio profile keys."""

        profile_key = profile.get("botstudio_profile_id", "")
        if not profile_key:
            return {}

        prefix = f"global.{profile_key}"
        values = {
            f"{prefix}.id": profile.get("id", "")
        }
        values.update({
            f"{prefix}.{key}": value
            for key, value in profile.items()
            if key not in {"custom_fields", "botstudio_profile_id", "id"}
        })

        for key, value in (profile.get("custom_fields") or {}).items():
            values[f"{prefix}.custom.{key}"] = value

        return values

    def get_custom_field_definitions(self, game_id: str) -> list[dict]:
        if not self.custom_field_file.exists():
            return []
        try:
            with open(self.custom_field_file, "r", encoding="utf-8") as handle:
                definitions = json.load(handle)
        except (OSError, ValueError, TypeError):
            return []
        return list(definitions.get(game_id, []))

    def save_custom_field_definitions(
        self,
        game_id: str,
        definitions: list[dict],
    ) -> list[dict]:
        normalized = []
        for definition in definitions:
            key = str(definition.get("key") or "").strip()
            if not key or key in {"id", "botstudio_profile_id"}:
                raise ValueError("custom field key must be stable and non-reserved")
            normalized.append({
                "key": key,
                "label": str(definition.get("label") or key),
                "type": str(definition.get("type") or "string"),
            })

        existing = {}
        if self.custom_field_file.exists():
            try:
                with open(self.custom_field_file, "r", encoding="utf-8") as handle:
                    existing = json.load(handle)
            except (OSError, ValueError, TypeError):
                existing = {}
        existing[str(game_id)] = normalized
        self.custom_field_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.custom_field_file, "w", encoding="utf-8") as handle:
            json.dump(existing, handle, indent=4, ensure_ascii=False)
        if self.reinitialize_service:
            self.reinitialize_service.publish(
                "profile",
                game_id=str(game_id),
                custom_fields=True,
            )
        return normalized

    def get_default_profile(
        self,
        game_id: str,
    ) -> dict | None:

        profiles = (
            self.get_profiles_for_game(
                game_id
            )
        )

        if not profiles:
            return None

        profiles.sort(
            key=lambda p: (
                -p.get(
                    "priority",
                    0,
                )
            )
        )

        return profiles[0]

    def get_profiles_by_instance(
        self,
        instance_number: int,
    ):
        return [
            profile
            for profile in self.load_enabled()
            if profile.get(
                "emulator_instance"
            )
            == instance_number
        ]

    def get_instance_groups(
        self,
    ) -> dict[int, list[dict]]:

        groups = {}

        for profile in self.load_enabled():

            instance = profile.get(
                "emulator_instance"
            )

            if instance not in groups:
                groups[instance] = []

            groups[instance].append(
                profile
            )

        return groups

    def validate_profile(
        self,
        profile: dict,
    ) -> bool:

        required_fields = [
            "id",
            "game",
            "name",
            "emulator_instance",
        ]

        for field in required_fields:

            if field not in profile:
                return False

        return True

    def get_new_profile(
        self,
    ) -> dict:

        return {
            "botstudio_profile_id": "",
            "id": "",
            "game": "",
            "name": "",

            "emulator_instance": 0,

            "priority": 50,

            "enabled": True,

            "last_run": None,
            "next_run": None,
        }

    def add_profile(
        self,
        profile: dict,
    ):

        profiles = self.load_all()
        profile = dict(profile)
        game_profiles = [
            existing for existing in profiles
            if str(existing.get("game", "")) == str(profile.get("game", ""))
        ]
        profile["account_order"] = max(
            (
                self._profile_order(existing, index)
                for index, existing in enumerate(game_profiles)
            ),
            default=-1,
        ) + 1

        profiles.append(
            profile
        )

        self.save_all(
            profiles
        )
        return profile

    def update_profile(
        self,
        profile: dict,
    ):

        profiles = self.load_all()

        for index, existing in enumerate(
            profiles
        ):

            same_studio_profile = (
                existing.get("botstudio_profile_id")
                == profile.get("botstudio_profile_id")
            )
            same_legacy_profile = (
                not existing.get("botstudio_profile_id")
                and existing.get("id") == profile.get("id")
            )

            if same_studio_profile or same_legacy_profile:

                if not profile.get("botstudio_profile_id"):
                    profile["botstudio_profile_id"] = str(uuid.uuid4())

                if existing.get("game") != profile.get("game"):
                    raise ProfileGameFrozenError(
                        "the game of a saved profile cannot be changed"
                    )

                profiles[index] = profile

                self.save_all(
                    profiles
                )

                return True

        return False

    def delete_profile(
        self,
        profile_id: str,
    ):

        profiles = [
            profile
            for profile in self.load_all()
            if profile.get("botstudio_profile_id") != profile_id
            and profile.get("id") != profile_id
        ]

        self.save_all(
            profiles
        )


    def clone_profile(
        self,
        profile_id: str,
    ):

        source = self.get_profile(
            profile_id
        )

        if not source:
            return None

        clone = dict(
            source
        )

        clone["id"] = (
            f"{source['id']}_{uuid.uuid4().hex[:8]}"
        )
        

        clone["name"] = (
            source["name"]
            + " Copy"
        )

        return self.add_profile(clone)

    def save_profile(
        self,
        profile: dict,
    ):

        profile = dict(profile)
        profile_id = profile.get("botstudio_profile_id", "")
        existing = self.get_profile(profile_id) if profile_id else None

        if not existing and profile.get("id"):
            existing = self.get_profile(profile["id"])

        if existing:

            existing_studio_id = existing.get("botstudio_profile_id")
            requested_studio_id = profile.get("botstudio_profile_id")
            if (
                existing_studio_id
                and requested_studio_id
                and existing_studio_id != requested_studio_id
            ):
                raise ProfileIdentityError(
                    "botstudio_profile_id cannot be changed"
                )
            profile["botstudio_profile_id"] = (
                existing_studio_id or requested_studio_id or str(uuid.uuid4())
            )

            self.update_profile(profile)
            return profile

        if not profile.get("botstudio_profile_id"):
            profile["botstudio_profile_id"] = str(uuid.uuid4())

        return self.add_profile(profile)
