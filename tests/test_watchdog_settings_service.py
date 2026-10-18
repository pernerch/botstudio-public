import faulthandler
import json
import logging
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QLockFile
from services.telegram_settings_service import TelegramSettingsService
from services.watchdog_autostart_service import WatchdogAutoStartService


class FakeConfigService:
    def __init__(self):
        self.settings = {}

    def get_studio_settings(self):
        return {
            "telegram_settings_configured": False,
            "telegram_enabled": False,
            "telegram_allowed_user_ids": "",
            "telegram_allowed_chat_ids": "",
            "watchdog_auto_start": False,
            **self.settings,
        }

    def save_studio_settings(self, values):
        self.settings.update(values)
        return self.get_studio_settings()


class FakeCredentialService:
    def __init__(self, value=None):
        self.value = value

    def load(self):
        return self.value

    def save(self, value):
        self.value = value

    def delete(self):
        self.value = None


class FakeRegistryKey:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeRegistry:
    HKEY_CURRENT_USER = object()
    KEY_READ = 1
    REG_SZ = 2

    def __init__(self):
        self.values = {}

    def OpenKey(self, *_args):
        return FakeRegistryKey()

    def CreateKey(self, *_args):
        return FakeRegistryKey()

    def QueryValueEx(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError
        return self.values[name], self.REG_SZ

    def SetValueEx(self, _key, name, _reserved, _type, value):
        self.values[name] = value

    def DeleteValue(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError
        del self.values[name]


class WatchdogSettingsServiceTests(unittest.TestCase):
    def setUp(self):
        self.config = FakeConfigService()
        self.credentials = FakeCredentialService()
        self.service = TelegramSettingsService(
            config_service=self.config,
            credential_service=self.credentials,
        )

    def test_token_is_saved_separately_from_app_configuration(self):
        settings = self.service.save(
            enabled=True,
            allowed_user_ids="123, 124",
            allowed_chat_ids="-456",
            auto_start=False,
            token="secret-token",
        )

        self.assertEqual("secret-token", self.credentials.value)
        self.assertEqual("123, 124", settings["telegram_allowed_user_ids"])
        self.assertEqual("-456", settings["telegram_allowed_chat_ids"])
        self.assertNotIn("secret-token", json.dumps(settings))
        self.assertTrue(settings["telegram_settings_configured"])

    def test_enabled_telegram_requires_token_and_both_allowlists(self):
        self.credentials.value = "existing-token"

        with self.assertRaisesRegex(ValueError, "saved bot token"):
            self.service.save(
                enabled=True,
                allowed_user_ids="",
                allowed_chat_ids="",
                auto_start=False,
                clear_token=True,
            )

        self.assertEqual("existing-token", self.credentials.value)

    def test_invalid_ids_are_rejected_before_persisting(self):
        with self.assertRaisesRegex(ValueError, "integer IDs"):
            self.service.save(
                enabled=False,
                allowed_user_ids="123; DROP",
                allowed_chat_ids="-456",
                auto_start=False,
            )

        self.assertEqual({}, self.config.settings)

    def test_clearing_token_is_explicit_and_other_settings_remain(self):
        self.credentials.value = "old-token"
        self.service.save(
            enabled=False,
            allowed_user_ids="123",
            allowed_chat_ids="-456",
            auto_start=False,
            clear_token=True,
        )

        self.assertIsNone(self.credentials.value)
        self.assertEqual("123", self.config.settings["telegram_allowed_user_ids"])

    def test_connection_test_can_reuse_the_credential_manager_token(self):
        self.credentials.value = "saved-token"

        self.assertEqual("saved-token", self.service.token_for_connection(""))
        self.assertEqual(
            "new-token",
            self.service.token_for_connection(" new-token "),
        )

    def test_startup_entry_can_be_registered_and_removed(self):
        registry = FakeRegistry()
        startup = WatchdogAutoStartService(registry=registry)
        command = [sys.executable, "watchdog_app.py", "--working-directory", "C:\\Bot Studio"]

        startup.set_registered(command, True)

        self.assertTrue(startup.is_registered())
        self.assertIn('"C:\\Bot Studio"', registry.values[startup.VALUE_NAME])

        startup.set_registered([], False)

        self.assertFalse(startup.is_registered())

    def test_watchdog_commands_attach_to_current_process_for_immediate_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.py").write_text("", encoding="utf-8")
            (root / "watchdog_app.py").write_text("", encoding="utf-8")
            startup, immediate = WatchdogAutoStartService().build_commands(
                root,
                12345,
            )

        self.assertIn("--studio-command", startup)
        self.assertNotIn("--studio-pid", startup)
        self.assertIn("--studio-pid", immediate)
        self.assertEqual("12345", immediate[immediate.index("--studio-pid") + 1])

    def test_windows_login_command_uses_windowless_python(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("main.py", "watchdog_app.py", "python.exe", "pythonw.exe"):
                (root / name).write_text("", encoding="utf-8")
            with patch("services.watchdog_autostart_service.sys.executable", str(root / "python.exe")):
                startup, immediate = WatchdogAutoStartService().build_commands(root, 123)
        if os.name == "nt":
            self.assertEqual(str(root / "pythonw.exe"), startup[0])
            self.assertEqual(str(root / "pythonw.exe"), immediate[0])
        self.assertNotIn("--studio-pid", startup)

    def test_watchdog_status_detects_an_existing_singleton_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "watchdog.lock"
            startup = WatchdogAutoStartService(
                registry=FakeRegistry(),
                lock_path=lock_path,
            )
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock = QLockFile(str(lock_path))
            self.assertTrue(lock.tryLock(0))
            try:
                self.assertTrue(startup.is_running())
            finally:
                lock.unlock()

            self.assertFalse(startup.is_running())


if __name__ == "__main__":
    started_at = time.monotonic()
    print("TEST START: watchdog settings service", flush=True)
    faulthandler.dump_traceback_later(20, exit=True)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(
        WatchdogSettingsServiceTests
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    faulthandler.cancel_dump_traceback_later()
    print(f"TEST DURATION: {time.monotonic() - started_at:.3f}s", flush=True)
    print(
        "TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE",
        flush=True,
    )
    sys.exit(0 if result.wasSuccessful() else 1)
