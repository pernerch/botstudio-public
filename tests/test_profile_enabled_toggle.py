"""Profile enabled switch, without accessing production profile files."""

import faulthandler
import os
import threading
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from ui.views.profile_view import ProfileView
from ui.widgets.toggle_switch import ToggleSwitch


class ProfileEnabledToggleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.started = time.monotonic()
        print(f"TEST START: {self.id()}", flush=True)
        watchdog = threading.Timer(15, self.timeout)
        watchdog.daemon = True
        watchdog.start()
        self.addCleanup(watchdog.cancel)
        self.profiles = Mock()
        self.profiles.load_all.return_value = []
        self.profiles.save_profile.side_effect = lambda profile: dict(profile)
        games = Mock()
        games.load.return_value = [{"id": "test", "display_name": "Test"}]
        with (
            patch("ui.views.profile_view.ProfileService", return_value=self.profiles),
            patch("ui.views.profile_view.GameService", return_value=games),
        ):
            self.view = ProfileView()
        self.addCleanup(self.view.deleteLater)
        self.profile = {
            "id": "account", "botstudio_profile_id": "profile",
            "name": "Test account", "game": "test", "enabled": True,
            "emulator_instance": 2, "priority": 30,
        }

    @staticmethod
    def timeout():
        print("TEST RESULT: TIMEOUT", flush=True)
        faulthandler.dump_traceback()
        os._exit(124)

    def tearDown(self):
        print(f"TEST END: {self.id()} ({time.monotonic() - self.started:.3f}s)", flush=True)

    def test_loading_enabled_and_disabled_does_not_mark_profile_dirty(self):
        self.assertIsInstance(self.view.enabled_checkbox, ToggleSwitch)
        self.assertTrue(self.view.enabled_checkbox.accessibleName())
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.view.load_profile_into_editor({**self.profile, "enabled": enabled})
                self.assertEqual(enabled, self.view.enabled_checkbox.isChecked())
                self.assertFalse(self.view.is_dirty)
        self.profiles.save_profile.assert_not_called()

    def test_click_and_keyboard_toggle_preserve_explicit_save(self):
        self.view.load_profile_into_editor(self.profile)
        QTest.mouseClick(self.view.enabled_checkbox, Qt.LeftButton)
        self.assertFalse(self.view.enabled_checkbox.isChecked())
        self.assertTrue(self.view.is_dirty)
        self.profiles.save_profile.assert_not_called()
        self.view.on_save()
        self.assertEqual({**self.profile, "enabled": False}, self.profiles.save_profile.call_args.args[0])
        self.assertFalse(self.view.is_dirty)
        QTest.keyClick(self.view.enabled_checkbox, Qt.Key_Space)
        self.assertTrue(self.view.enabled_checkbox.isChecked())
        self.assertTrue(self.view.is_dirty)
        self.view.on_save()
        self.assertTrue(self.profiles.save_profile.call_args.args[0]["enabled"])


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE", flush=True)
    raise SystemExit(0 if result.wasSuccessful() else 1)
