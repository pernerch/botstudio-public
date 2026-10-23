import os
import tempfile
import time
import unittest
from pathlib import Path
from threading import Timer, get_ident
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from services.app_config_service import AppConfigService
from services.game_service import GameService
from ui.i18n import translator
from ui.views.settings_view import SettingsView


class SchedulingSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def run(self, result=None):
        def timeout():
            print(f"{self.id()} TEST RESULT: TIMEOUT", flush=True)
            os._exit(124)

        watchdog = Timer(20, timeout)
        watchdog.daemon = True
        started = time.monotonic()
        print(f"Test Start: {self.id()}", flush=True)
        watchdog.start()
        result = result or self.defaultTestResult()
        failures = len(result.failures) + len(result.errors)
        try:
            return super().run(result)
        finally:
            watchdog.cancel()
            outcome = (
                "FAILURE" if len(result.failures) + len(result.errors) > failures
                else "SUCCESS"
            )
            print(
                f"Test End: {self.id()} Duration: {time.monotonic() - started:.3f}s "
                f"TEST RESULT: {outcome}",
                flush=True,
            )

    def setUp(self):
        directory = tempfile.TemporaryDirectory(
            prefix=".scheduling-settings-", dir=Path.cwd()
        )
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name in ("EmulatorManager", "AdbService"):
            mocked = patch(f"services.game_service.{name}")
            mocked.start()
            self.addCleanup(mocked.stop)
        self.config = AppConfigService(
            str(self.root / "app_config.json"), reinitialize_service=Mock()
        )
        self.games = GameService(
            str(self.root / "games.json"), reinitialize_service=Mock()
        )

    def make_games(self):
        self.games.save([
            {
                "id": "a", "display_name": "Alpha", "package_name": "test.alpha",
                "settings": {"max_active_minutes": 987},
                "config": {"startup_screen": "home"},
            },
            {"id": "b", "display_name": "Beta", "package_name": "test.beta"},
        ])

    def wait_for_view(self, view):
        deadline = time.monotonic() + 5
        while view._operation is not None and time.monotonic() < deadline:
            QTest.qWait(5)
        self.assertIsNone(view._operation, view.status_label.text())

    def make_view(self, game_id="a"):
        with (
            patch("ui.views.settings_view.GameService", return_value=self.games),
            patch("ui.views.settings_view.AppConfigService", return_value=self.config),
            patch("ui.views.settings_view.get_logger", return_value=Mock()),
        ):
            view = SettingsView(game_id)
            self.wait_for_view(view)
        self.addCleanup(self.dispose_view, view)
        self.assertEqual("", view.status_label.text())
        return view

    def dispose_view(self, view):
        self.wait_for_view(view)
        view.close()
        view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def save_view(self, view):
        view.save_settings()
        self.wait_for_view(view)

    def test_defaults_are_shared_with_studio_settings(self):
        expected = {
            "limit_profile_active_time": False,
            "max_active_minutes": 60,
            "scheduling_lookahead_seconds": 120,
            "scheduling_max_wait_minutes": 120,
        }
        self.assertEqual(expected, AppConfigService.SCHEDULING_DEFAULTS)
        self.assertEqual(expected, self.config.get_scheduling_settings(self.games, ""))
        for key, value in expected.items():
            self.assertEqual(value, self.config.get_studio_settings()[key])

    def test_inheritance_ignores_legacy_game_settings_and_follows_global_changes(self):
        self.make_games()
        self.assertEqual({}, self.games.get_scheduling_overrides("a"))
        self.config.save_studio_settings({"max_active_minutes": 40})
        self.assertEqual(40, self.config.get_scheduling_settings(self.games, "a")["max_active_minutes"])
        self.config.save_studio_settings({"max_active_minutes": 80})
        self.assertEqual(80, self.config.get_scheduling_settings(self.games, "a")["max_active_minutes"])

    def test_sparse_overrides_are_explicit_and_defensive(self):
        self.make_games()
        self.config.save_studio_settings({"scheduling_lookahead_seconds": 45})
        self.games.save_scheduling_overrides("a", {"max_active_minutes": 33})
        overrides = self.games.get_scheduling_overrides("a")
        overrides["max_active_minutes"] = 99
        effective = self.config.get_scheduling_settings(self.games, "a")
        self.assertEqual(33, effective["max_active_minutes"])
        self.assertEqual(45, effective["scheduling_lookahead_seconds"])
        self.assertEqual(60, self.config.get_scheduling_settings(self.games, "b")["max_active_minutes"])
        self.assertEqual({"max_active_minutes": 33}, self.games.get_scheduling_overrides("a"))

    def test_reset_removes_overrides_without_changing_other_game_fields(self):
        self.make_games()
        original = self.games.get_game("a")
        for reset in (None, {}):
            with self.subTest(reset=reset):
                self.games.save_scheduling_overrides("a", {"max_active_minutes": 9})
                self.games.save_scheduling_overrides("a", reset)
                self.assertNotIn("scheduling_overrides", self.games.get_game("a"))
                self.assertEqual(original, self.games.get_game("a"))

    def test_invalid_override_inputs_fail_without_writes(self):
        self.make_games()
        before = self.games.file_name.read_bytes()
        invalid = [
            [], False, 1, "",
            {"unknown": 1}, {"idle_behavior": "close_game"},
            {"limit_profile_active_time": 1},
            {"limit_profile_active_time": "false"},
        ]
        for key, low, high in (
            ("max_active_minutes", 1, 100000),
            ("scheduling_lookahead_seconds", 0, 3600),
            ("scheduling_max_wait_minutes", 1, 100000),
        ):
            invalid.extend({key: value} for value in (low - 1, high + 1, True, 1.5, "2", None))
        for values in invalid:
            with self.subTest(values=values):
                with self.assertRaisesRegex(ValueError, "Scheduling|scheduling|must be"):
                    self.games.save_scheduling_overrides("a", values)
                self.assertEqual(before, self.games.file_name.read_bytes())

    def test_valid_range_boundaries_and_unknown_game(self):
        self.make_games()
        for values in (
            {"limit_profile_active_time": False, "max_active_minutes": 1,
             "scheduling_lookahead_seconds": 0, "scheduling_max_wait_minutes": 1},
            {"limit_profile_active_time": True, "max_active_minutes": 100000,
             "scheduling_lookahead_seconds": 3600, "scheduling_max_wait_minutes": 100000},
        ):
            self.assertEqual(values, self.games.save_scheduling_overrides("a", values))
        with self.assertRaisesRegex(ValueError, "Unknown game"):
            self.games.save_scheduling_overrides("missing", {})

    def test_invalid_global_settings_do_not_write_and_preserve_unrelated_config(self):
        self.config.save({"active_game": "a", "unrelated": {"keep": 1}})
        before = self.config.file_name.read_bytes()
        for values in (
            {"max_active_minutes": 0},
            {"limit_profile_active_time": 0},
            {"scheduling_lookahead_seconds": 3601},
            {"scheduling_max_wait_minutes": True},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.config.save_studio_settings(values)
            self.assertEqual(before, self.config.file_name.read_bytes())
        self.config.save_studio_settings({"max_active_minutes": 12})
        self.assertEqual({"keep": 1}, self.config.load()["unrelated"])
        self.assertEqual("a", self.config.get_active_game())

    def test_invalid_persisted_values_raise_clear_errors(self):
        self.make_games()
        games = self.games.load()
        games[0]["scheduling_overrides"] = {"scheduling_max_wait_minutes": -1}
        self.games.save(games)
        with self.assertRaisesRegex(ValueError, "scheduling_max_wait_minutes"):
            self.config.get_scheduling_settings(self.games, "a")
        games[0]["scheduling_overrides"] = {"unknown": 3}
        self.games.save(games)
        with self.assertRaisesRegex(ValueError, "Unknown scheduling"):
            self.games.get_scheduling_overrides("a")
        self.config.save({"studio_settings": {"limit_profile_active_time": "yes"}})
        with self.assertRaisesRegex(ValueError, "limit_profile_active_time"):
            self.config.get_scheduling_settings(self.games, "")

    def test_no_game_ui_saves_all_global_controls(self):
        view = self.make_view("")
        self.assertEqual(0, view.game_combo.count())
        view.max_idle_spin.setValue(17)
        view.max_concurrent_spin.setValue(4)
        view.limit_active_time_check.setChecked(True)
        view.max_active_spin.setValue(31)
        view.lookahead_spin.setValue(250)
        view.max_wait_spin.setValue(90)
        view.emulator_start_delay_spin.setValue(8)
        self.save_view(view)
        stored = self.config.get_studio_settings()
        self.assertEqual(17, stored["max_idle_minutes"])
        self.assertEqual(4, stored["max_concurrent_instances"])
        self.assertEqual(True, stored["limit_profile_active_time"])
        self.assertEqual(31, stored["max_active_minutes"])
        self.assertEqual(250, stored["scheduling_lookahead_seconds"])
        self.assertEqual(90, stored["scheduling_max_wait_minutes"])
        self.assertEqual(8, stored["emulator_start_delay_seconds"])
        self.assertEqual(translator.t("settings.saved"), view.status_label.text())

    def test_ui_inheritance_is_live_and_existing_active_control_is_bound(self):
        self.make_games()
        view = self.make_view()
        self.assertEqual(3, view.tabs.count())
        self.assertTrue(view.tabs.widget(1).isAncestorOf(view.limit_active_time_check))
        self.assertFalse(view.tabs.widget(0).isAncestorOf(view.limit_active_time_check))
        self.assertTrue(view.use_global_scheduling_check.isChecked())
        self.assertFalse(view.max_active_spin.isEnabled())
        self.assertFalse(view.game_lookahead_spin.isEnabled())
        view.limit_active_time_check.setChecked(True)
        view.max_active_spin.setValue(55)
        view.lookahead_spin.setValue(42)
        view.max_wait_spin.setValue(76)
        self.assertTrue(view.max_active_spin.isEnabled())
        self.assertTrue(view.game_limit_active_time_check.isChecked())
        self.assertEqual(55, view.game_max_active_spin.value())
        self.assertEqual(42, view.game_lookahead_spin.value())
        self.assertEqual(76, view.game_max_wait_spin.value())
        self.assertFalse(view.game_max_active_spin.isEnabled())
        self.assertEqual({}, view._game_drafts)
        view.use_global_scheduling_check.setChecked(False)
        self.assertTrue(view.game_max_active_spin.isEnabled())
        view.game_limit_active_time_check.setChecked(False)
        self.assertFalse(view.game_max_active_spin.isEnabled())
        self.assertTrue(view.limit_active_time_check.isChecked())

    def test_switching_games_preserves_all_drafts_and_global_edits_until_save(self):
        self.make_games()
        view = self.make_view()
        view.max_idle_spin.setValue(29)
        view.lookahead_spin.setValue(19)
        view.use_global_scheduling_check.setChecked(False)
        view.game_lookahead_spin.setValue(111)
        view.game_combo.setCurrentIndex(view.game_combo.findData("b"))
        self.assertEqual(29, view.max_idle_spin.value())
        self.assertEqual(19, view.lookahead_spin.value())
        self.assertTrue(view.use_global_scheduling_check.isChecked())
        view.use_global_scheduling_check.setChecked(False)
        view.game_lookahead_spin.setValue(222)
        view.game_combo.setCurrentIndex(view.game_combo.findData("a"))
        self.assertEqual(111, view.game_lookahead_spin.value())
        self.assertFalse(view.use_global_scheduling_check.isChecked())
        self.assertEqual({}, self.games.get_scheduling_overrides("a"))
        self.assertEqual({}, self.games.get_scheduling_overrides("b"))
        self.assertFalse(self.config.file_name.exists())
        self.save_view(view)
        self.assertEqual(111, self.games.get_scheduling_overrides("a")["scheduling_lookahead_seconds"])
        self.assertEqual(222, self.games.get_scheduling_overrides("b")["scheduling_lookahead_seconds"])
        self.assertEqual(19, self.config.get_studio_settings()["scheduling_lookahead_seconds"])
        self.assertEqual(29, self.config.get_studio_settings()["max_idle_minutes"])
        self.assertEqual({}, view._game_drafts)

    def test_ui_reset_inherits_live_values_and_removes_override_only_on_save(self):
        self.make_games()
        self.games.save_scheduling_overrides("a", {"max_active_minutes": 10})
        view = self.make_view()
        self.assertFalse(view.use_global_scheduling_check.isChecked())
        view.use_global_scheduling_check.setChecked(True)
        view.max_active_spin.setValue(88)
        self.assertEqual(88, view.game_max_active_spin.value())
        self.assertEqual({"max_active_minutes": 10}, self.games.get_scheduling_overrides("a"))
        view.game_combo.setCurrentIndex(view.game_combo.findData("b"))
        view.game_combo.setCurrentIndex(view.game_combo.findData("a"))
        self.assertTrue(view.use_global_scheduling_check.isChecked())
        self.save_view(view)
        self.assertNotIn("scheduling_overrides", self.games.get_game("a"))

    def test_ui_save_error_is_visible_and_drafts_remain_retryable(self):
        self.make_games()
        view = self.make_view()
        view.use_global_scheduling_check.setChecked(False)
        view.game_lookahead_spin.setValue(77)
        with patch.object(self.games, "save_scheduling_overrides", side_effect=ValueError("bad scheduling value")):
            self.save_view(view)
        self.assertIn("bad scheduling value", view.status_label.text())
        self.assertEqual(77, view._game_drafts["a"]["scheduling_lookahead_seconds"])
        self.assertTrue(view.btn_save.isEnabled())
        self.save_view(view)
        self.assertEqual(77, self.games.get_scheduling_overrides("a")["scheduling_lookahead_seconds"])

    def test_ui_load_and_save_file_work_runs_outside_gui_thread(self):
        gui_thread = get_ident()
        calls = []
        original_load = self.config.get_studio_settings
        original_save = self.config.save_studio_settings

        def load():
            calls.append(get_ident())
            return original_load()

        def save(settings):
            calls.append(get_ident())
            return original_save(settings)

        with (
            patch.object(self.config, "get_studio_settings", side_effect=load),
            patch.object(self.config, "save_studio_settings", side_effect=save),
        ):
            view = self.make_view("")
            self.save_view(view)
        self.assertGreaterEqual(len(calls), 3)
        self.assertNotIn(gui_thread, calls)

    def test_new_scheduling_labels_and_help_are_translated_in_english_and_german(self):
        keys = (
            "settings.tab_general", "settings.tab_scheduling", "settings.tab_games",
            "settings.scheduling_lookahead_seconds", "settings.scheduling_max_wait_minutes",
            "settings.use_global_scheduling", "settings.scheduling_help",
            "settings.game_scheduling_help", "settings.operation_failed",
        )
        for language in ("en", "de"):
            with self.subTest(language=language):
                for key in keys:
                    self.assertTrue(translator.STRINGS[language].get(key), key)


if __name__ == "__main__":
    result = unittest.main(verbosity=2, exit=False).result
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE", flush=True)
    raise SystemExit(0 if result.wasSuccessful() else 1)
