from services.app_config_service import AppConfigService
from services.telegram_service import TelegramService
from services.windows_credential_service import WindowsCredentialService


class TelegramSettingsService:
    def __init__(self, config_service=None, credential_service=None):
        self.config_service = config_service or AppConfigService()
        self.credential_service = credential_service or WindowsCredentialService()

    def load(self):
        settings = self.config_service.get_studio_settings()
        return {
            "configured": bool(settings["telegram_settings_configured"]),
            "enabled": bool(settings["telegram_enabled"]),
            "allowed_user_ids": str(settings["telegram_allowed_user_ids"]),
            "allowed_chat_ids": str(settings["telegram_allowed_chat_ids"]),
            "auto_start": bool(settings["watchdog_auto_start"]),
            "token_configured": self.credential_service.load() is not None,
        }

    def token_for_connection(self, entered_token):
        token = entered_token.strip() if isinstance(entered_token, str) else ""
        return token or self.credential_service.load()

    def save(
        self,
        enabled,
        allowed_user_ids,
        allowed_chat_ids,
        auto_start,
        token=None,
        clear_token=False,
    ):
        if type(enabled) is not bool or type(auto_start) is not bool:
            raise ValueError("Telegram and watchdog settings must be booleans")
        if type(clear_token) is not bool:
            raise ValueError("clear_token must be a boolean")
        users = self._normalize_ids(allowed_user_ids, "allowed_user_ids")
        chats = self._normalize_ids(allowed_chat_ids, "allowed_chat_ids")

        token_will_exist = self.credential_service.load() is not None
        if token is not None and token.strip():
            token_will_exist = True
        elif clear_token:
            token_will_exist = False
        if enabled and (not token_will_exist or not users or not chats):
            raise ValueError(
                "Enabling Telegram requires a saved bot token and both ID lists"
            )

        if token is not None and token.strip():
            self.credential_service.save(token.strip())
        elif clear_token:
            self.credential_service.delete()

        return self.config_service.save_studio_settings(
            {
                "telegram_settings_configured": True,
                "telegram_enabled": enabled,
                "telegram_allowed_user_ids": users,
                "telegram_allowed_chat_ids": chats,
                "watchdog_auto_start": auto_start,
            }
        )

    @staticmethod
    def _normalize_ids(values, name):
        if not isinstance(values, str):
            raise ValueError(f"{name} must be a comma-separated ID list")
        text = values.strip()
        if not text:
            return ""
        normalized = TelegramService._validate_ids(text.split(","), name)
        return ", ".join(str(value) for value in sorted(normalized))
