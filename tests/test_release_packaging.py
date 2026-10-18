import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


class ApplicationIconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_icon_uses_explicit_resource_root_not_working_directory(self):
        from services.application_paths import ApplicationPaths
        from ui.app_icon import load_app_icon

        project = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as temporary:
            paths = ApplicationPaths(Path(temporary), project, Path(temporary))
            with patch("ui.app_icon.ApplicationPaths.discover", return_value=paths):
                icon = load_app_icon()
            for size in (16, 32, 48, 256):
                image = icon.pixmap(size, size).toImage()
                self.assertFalse(image.isNull())
                self.assertTrue(any(
                    image.pixelColor(x, y).alpha() > 0
                    for x in range(image.width()) for y in range(image.height())
                ))

    def test_missing_resource_icon_is_an_explicit_error(self):
        from services.application_paths import ApplicationPaths
        from ui.app_icon import load_app_icon

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("ui.app_icon.ApplicationPaths.discover", return_value=ApplicationPaths(root, root, root)):
                with self.assertRaisesRegex(RuntimeError, "icon could not be loaded"):
                    load_app_icon()

    @unittest.skipUnless(sys.platform == "win32", "Windows shell API")
    def test_windows_taskbar_identity_failure_is_not_silenced(self):
        from ui.app_icon import apply_taskbar_identity

        with patch("ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID", return_value=-1):
            with self.assertRaisesRegex(OSError, "taskbar identity"):
                apply_taskbar_identity()


class ReleasePackagingTests(unittest.TestCase):
    def test_source_self_tests_use_isolated_data_and_real_offline_ocr(self):
        project = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for script, role in (("main.py", "studio"), ("watchdog_app.py", "watchdog")):
                with self.subTest(role=role):
                    data = root / role
                    environment = dict(os.environ)
                    environment["QT_QPA_PLATFORM"] = "offscreen"
                    environment["BOTSTUDIO_DATA_DIR"] = str(root)
                    result = subprocess.run(
                        [sys.executable, str(project / script), "--self-test",
                         "--data-directory", str(data)],
                        cwd=root, env=environment, capture_output=True, text=True, timeout=40,
                    )
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    report = json.loads((data / "self-test-result.json").read_text())
                    self.assertEqual("SUCCESS", report["result"], report)
                    self.assertFalse(report["frozen"])
                    self.assertTrue(report["application_icon_loaded"])
                    if role == "studio":
                        self.assertEqual("01:23:45", report["offline_ocr"])
                        self.assertTrue(report["main_window_created"])
                        self.assertTrue(report["package_roundtrip"])
                        self.assertTrue(report["markdown_editor"])
                        if sys.platform == "win32":
                            self.assertEqual("BotStudio.Studio", report["taskbar_app_id"])
                    else:
                        self.assertIn(str(project / "main.py"), report["startup_command"])

    def test_self_test_refuses_existing_user_directory(self):
        project = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            existing = root / "personal.json"
            existing.write_text('{"keep": true}', encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(project / "main.py"), "--self-test",
                 "--data-directory", str(root)],
                cwd=root, capture_output=True, text=True, timeout=15,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("empty", result.stderr)
            self.assertEqual('{"keep": true}', existing.read_text())
            self.assertFalse((root / "config").exists())

    def test_shortcut_identity_matches_version_independent_process_identity(self):
        from services.application_info import APP_VERSION, STUDIO_APP_USER_MODEL_ID

        project = Path(__file__).resolve().parents[1]
        script = (project / "release" / "botstudio.iss").read_text(encoding="utf-8")
        studio_shortcuts = [
            line for line in script.splitlines()
            if line.startswith("Name: ") and 'Filename: "{app}\\BotStudio.exe"' in line
        ]
        self.assertEqual(2, len(studio_shortcuts))
        self.assertNotIn(APP_VERSION, STUDIO_APP_USER_MODEL_ID)
        for line in studio_shortcuts:
            self.assertIn(f'AppUserModelID: "{STUDIO_APP_USER_MODEL_ID}"', line)
            self.assertIn('IconFilename: "{app}\\BotStudio.exe"', line)

    def test_installer_and_bundle_include_application_license(self):
        project = Path(__file__).resolve().parents[1]
        spec = (project / "release" / "botstudio.spec").read_text(encoding="utf-8")
        installer = (project / "release" / "botstudio.iss").read_text(encoding="utf-8")
        for filename in ("LICENSE", "LICENSE-NOTICE.txt"):
            self.assertIn(f'(str(root / "{filename}"), "licenses/BotStudio")', spec)
            self.assertTrue((project / filename).is_file())
        self.assertIn("LicenseFile=..\\LICENSE", installer)
