import faulthandler
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Timer
import time
import unittest
from unittest.mock import patch

from services.application_paths import ApplicationPaths
from services.application_bootstrap import initialize_application


class ApplicationPathsTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.original_directory = Path.cwd()
        self.addCleanup(os.chdir, self.original_directory)

    def test_development_keeps_existing_project_data(self):
        with patch("services.application_paths.sys.frozen", False, create=True), patch.dict(
            os.environ, {}, clear=True,
        ):
            paths = ApplicationPaths.discover()
        self.assertEqual(Path(__file__).resolve().parents[1], paths.data_root)
        self.assertEqual(paths.resource_root, paths.data_root)

    def test_installed_application_uses_local_appdata_not_bundle(self):
        with patch("services.application_paths.sys.frozen", True, create=True), patch(
            "services.application_paths.sys._MEIPASS", str(self.root / "bundle"), create=True,
        ), patch("services.application_paths.sys.executable", str(self.root / "program" / "BotStudio.exe")), patch.dict(
            os.environ, {"LOCALAPPDATA": str(self.root / "user")}, clear=True,
        ):
            paths = ApplicationPaths.discover()
        self.assertEqual(self.root / "user" / "BotStudio", paths.data_root)
        self.assertEqual(self.root / "bundle", paths.resource_root)
        self.assertEqual(self.root / "program", paths.program_root)

    def test_explicit_data_directory_is_available_for_development_and_tests(self):
        with patch.dict(os.environ, {"BOTSTUDIO_DATA_DIR": str(self.root / "custom")}):
            self.assertEqual(self.root / "custom", ApplicationPaths.discover().data_root)

    def test_relative_data_override_is_rejected(self):
        with patch.dict(os.environ, {"BOTSTUDIO_DATA_DIR": "relative"}):
            with self.assertRaisesRegex(ValueError, "absolute"):
                ApplicationPaths.discover()

    def test_installed_application_requires_known_local_appdata(self):
        with patch("services.application_paths.sys.frozen", True, create=True), patch.dict(
            os.environ, {}, clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "LOCALAPPDATA"):
                ApplicationPaths.discover()

    def test_first_start_creates_empty_environment_without_personal_content(self):
        paths = ApplicationPaths(self.root, self.root / "resources", self.root / "program")
        initialize_application(paths)
        self.assertEqual(self.root, Path.cwd())
        for directory in (
            "config", "games", "profiles", "data/functions", "data/library",
            "data/scheduler", "data/variables", "data/statistics",
            "data/learning/crops", "logs/archive", "runs/ocr_debug",
        ):
            with self.subTest(directory=directory):
                self.assertTrue((self.root / directory).is_dir())
        self.assertEqual([], json.loads((self.root / "games" / "games.json").read_text()))
        self.assertEqual([], json.loads((self.root / "profiles" / "profiles.json").read_text()))
        emulator = json.loads((self.root / "config" / "emulator.json").read_text())
        self.assertEqual("", emulator["adb_path"])
        self.assertEqual("", emulator["mumu_manager_path"])
        self.assertEqual([], list((self.root / "data" / "functions").iterdir()))
        self.assertFalse((self.root / "ui").exists())

    def test_repeated_start_preserves_existing_files_byte_for_byte(self):
        paths = ApplicationPaths(self.root, self.root, self.root)
        initialize_application(paths)
        existing = {
            self.root / "config" / "app_config.json": b'{"active_game":"existing"}',
            self.root / "config" / "emulator.json": b'{"custom":"keep"}',
            self.root / "games" / "games.json": b'[{"id":"existing"}]',
            self.root / "data" / "functions" / "user.json": b'{"user":"content"}',
        }
        for file, content in existing.items():
            file.write_bytes(content)
        initialize_application(paths)
        for file, content in existing.items():
            with self.subTest(file=file):
                self.assertEqual(content, file.read_bytes())

    def test_initialization_failure_does_not_change_working_directory(self):
        blocker = self.root / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        paths = ApplicationPaths(blocker, self.root, self.root)
        with self.assertRaises(OSError):
            initialize_application(paths)
        self.assertEqual(self.original_directory, Path.cwd())

    def test_watchdog_can_prepare_shared_data_without_changing_its_directory(self):
        paths = ApplicationPaths(self.root, self.root, self.root)
        initialize_application(paths, activate=False)
        self.assertEqual(self.original_directory, Path.cwd())
        self.assertTrue((self.root / "config" / "app_config.json").is_file())

    def test_startup_failure_is_visible_and_does_not_construct_studio(self):
        import main

        with patch("main.initialize_application", side_effect=PermissionError("Read-only data directory")), patch(
            "PySide6.QtWidgets.QApplication",
        ), patch("PySide6.QtWidgets.QMessageBox.critical") as message, patch(
            "sys.stderr", new_callable=StringIO,
        ) as stderr:
            with self.assertRaises(SystemExit) as stopped:
                main.main()
        self.assertEqual(1, stopped.exception.code)
        self.assertIn("Read-only data directory", message.call_args.args[2])
        self.assertIn("Read-only data directory", stderr.getvalue())

    def test_parallel_initializer_has_bounded_lock_timeout(self):
        from PySide6.QtCore import QLockFile

        lock = QLockFile(str(self.root / ".startup.lock"))
        self.assertTrue(lock.tryLock(0))
        self.addCleanup(lock.unlock)
        paths = ApplicationPaths(self.root, self.root, self.root)
        with self.assertRaisesRegex(TimeoutError, "initialization"):
            initialize_application(paths, lock_timeout_ms=0)
        self.assertEqual(self.original_directory, Path.cwd())

    def test_negative_lock_timeout_cannot_wait_indefinitely(self):
        paths = ApplicationPaths(self.root, self.root, self.root)
        with self.assertRaisesRegex(ValueError, "non-negative"):
            initialize_application(paths, lock_timeout_ms=-1)

    def test_default_theme_resources_are_independent_of_data_directory(self):
        from services.theme_service import ThemeService

        os.chdir(self.root)
        theme = ThemeService()
        self.assertTrue(theme.file_name.is_absolute())
        self.assertTrue(theme.load_stylesheet())
        self.assertNotIn("@ICON_DIR@", theme.load_stylesheet())

    def test_explicit_theme_file_keeps_existing_behavior(self):
        from services.theme_service import ThemeService

        custom = self.root / "custom.qss"
        custom.write_text("QWidget { color: red; }", encoding="utf-8")
        self.assertEqual("QWidget { color: red; }", ThemeService(str(custom)).load_stylesheet())

    def test_configuration_save_creates_parent_directory(self):
        from services.app_config_service import AppConfigService

        destination = self.root / "nested" / "config" / "app.json"
        service = AppConfigService(str(destination))
        service.save({"active_game": ""})
        self.assertEqual({"active_game": ""}, json.loads(destination.read_text()))

    def test_real_studio_starts_with_empty_data_and_no_emulator(self):
        code = """
from unittest.mock import patch
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
import main
import sys
assert "ui.views.main_window" not in sys.modules
from services.emulator_scan_service import EmulatorScanService
from services.watchdog_ipc_service import WatchdogIpcServer
from ui.views.telegram_remote_control_view import TelegramRemoteControlView
from ui.views.main_window import MainWindow

original_show = MainWindow.show
def show(window):
    original_show(window)
    assert window.isVisible()
    assert window.game_service.load() == []
    assert window.profile_runtime_service.profile_service.load_all() == []
    assert not window.windowIcon().isNull()
    assert QApplication.instance().styleSheet()
    QTimer.singleShot(100, window.close)
    print("STUDIO START: SUCCESS", flush=True)

with patch.object(MainWindow, "show", show), \\
     patch.object(WatchdogIpcServer, "start"), \\
     patch.object(WatchdogIpcServer, "stop"), \\
     patch.object(EmulatorScanService, "start"), \\
     patch.object(TelegramRemoteControlView, "start_watchdog_if_enabled"), \\
     patch("core.emulator.cleanup_screenshots"), \\
     patch("core.emulator.trim_adb_log"), \\
     patch("subprocess.Popen", side_effect=AssertionError("No external process in startup test")):
    main.main()
"""
        environment = dict(os.environ)
        environment["BOTSTUDIO_DATA_DIR"] = str(self.root / "empty data")
        environment["QT_QPA_PLATFORM"] = "offscreen"
        project = Path(__file__).resolve().parents[1]
        environment["PYTHONPATH"] = str(project)
        for installed in (False, True):
            with self.subTest(installed=installed):
                startup_code = code
                expected_root = self.root / "empty data"
                if installed:
                    environment.pop("BOTSTUDIO_DATA_DIR")
                    environment["LOCALAPPDATA"] = str(self.root / "local appdata")
                    expected_root = self.root / "local appdata" / "BotStudio"
                    startup_code = (
                        f"import sys\nsys.frozen = True\nsys._MEIPASS = {str(project)!r}\n"
                        + code
                    )
                result = subprocess.run(
                    [sys.executable, "-c", startup_code], cwd=self.root, env=environment,
                    capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn("STUDIO START: SUCCESS", result.stdout)
                self.assertTrue((expected_root / "config" / "app_config.json").is_file())


if __name__ == "__main__":
    faulthandler.enable()

    def timeout():
        print("TEST RESULT: TIMEOUT", flush=True)
        faulthandler.dump_traceback()
        os._exit(2)

    started = time.monotonic()
    print("TEST START: application paths and first start", flush=True)
    watchdog = Timer(60, timeout)
    watchdog.daemon = True
    watchdog.start()
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(ApplicationPathsTests)
    )
    watchdog.cancel()
    print(f"TEST DURATION: {time.monotonic() - started:.3f}s", flush=True)
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE", flush=True)
    sys.exit(0 if result.wasSuccessful() else 1)
