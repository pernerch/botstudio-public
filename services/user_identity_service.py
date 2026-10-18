import json
from pathlib import Path


DEFAULT_USER_NAME = "unknown"


class UserIdentityService:
    """Local bot user, stamped onto every function the user creates."""

    def __init__(
        self,
        file_name: str = "config/app_config.json",
    ):

        self.file_name = Path(
            file_name
        )

        self.file_name.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

    def load(
        self,
    ) -> dict:

        if not self.file_name.exists():
            return {}

        try:
            with open(
                self.file_name,
                "r",
                encoding="utf-8",
            ) as fp:

                return json.load(fp)

        except (json.JSONDecodeError, OSError):
            return {}

    def save(
        self,
        config: dict,
    ) -> None:

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

    def get_user_name(
        self,
    ) -> str:

        return self.load().get(
            "user_name",
            DEFAULT_USER_NAME,
        ) or DEFAULT_USER_NAME

    def set_user_name(
        self,
        user_name: str,
    ) -> None:

        config = self.load()

        config["user_name"] = user_name.strip()

        self.save(config)

    def is_configured(
        self,
    ) -> bool:

        return self.get_user_name() != DEFAULT_USER_NAME
