import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from services.watchdog_autostart_service import WatchdogAutoStartService


class FrozenWatchdogStartTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.program = self.root / "program files"
        self.program.mkdir()
        for name in ("BotStudio.exe", "BotStudioWatchdog.exe"):
            (self.program / name).touch()
        self.service = WatchdogAutoStartService(registry=Mock())
        self.frozen = patch.object(sys, "frozen", True, create=True)
        self.frozen.start()
        self.addCleanup(self.frozen.stop)
        executable = patch.object(sys, "executable", str(self.program / "BotStudio.exe"))
        executable.start()
        self.addCleanup(executable.stop)

    def test_frozen_commands_use_companion_exes_not_extracted_scripts(self):
        data = self.root / "user data"
        startup, immediate = self.service.build_commands(self.root / "bundle", 123, data)
        self.assertEqual(str(self.program / "BotStudioWatchdog.exe"), startup[0])
        self.assertEqual(
            [str(self.program / "BotStudio.exe")],
            startup[startup.index("--studio-command") + 1:],
        )
        self.assertEqual(str(data), startup[startup.index("--working-directory") + 1])
        self.assertEqual("123", immediate[immediate.index("--studio-pid") + 1])
        self.assertFalse(any(part.endswith(".py") for part in immediate))

    def test_frozen_default_working_directory_is_local_appdata(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "user")}, clear=True):
            startup, _ = self.service.build_commands(self.program, 123)
        self.assertEqual(
            str(self.root / "user" / "BotStudio"),
            startup[startup.index("--working-directory") + 1],
        )

    def test_missing_companion_is_an_explicit_error(self):
        (self.program / "BotStudioWatchdog.exe").unlink()
        with self.assertRaisesRegex(FileNotFoundError, "BotStudioWatchdog"):
            self.service.build_commands(self.program, 123, self.root)

    def test_ensure_running_uses_selected_frozen_data_directory(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "user")}, clear=True), patch.object(
            self.service, "is_running", side_effect=[False, True],
        ), patch.object(self.service, "launch") as launch:
            self.assertTrue(self.service.ensure_running(self.root / "bundle", 123))
        self.assertEqual(self.root / "user" / "BotStudio", launch.call_args.args[1])

    def test_standalone_frozen_watchdog_defaults_to_companion_studio(self):
        import watchdog_app

        with patch.dict(os.environ, {"BOTSTUDIO_DATA_DIR": str(self.root / "data")}), patch(
            "watchdog_app.QApplication",
        ), patch("watchdog_app.QLockFile") as lock, patch(
            "watchdog_app.WatchdogAutoStartService",
        ) as startup, patch("watchdog_app._configure_logging"), patch(
            "watchdog_app.WatchdogProcessService", side_effect=RuntimeError("captured command"),
        ) as process:
            startup.return_value.lock_path = self.root / "test.lock"
            lock.return_value.tryLock.return_value = True
            with self.assertRaisesRegex(RuntimeError, "captured command"):
                watchdog_app.main([])
        self.assertEqual(
            [str(self.program / "BotStudio.exe")],
            process.call_args.kwargs["studio_command"],
        )
