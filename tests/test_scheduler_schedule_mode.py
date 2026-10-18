import faulthandler
import os
import sys
import time
import unittest
from threading import Timer
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTime, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QTimeEdit, QWidget

from scheduler.models import Event
from services.theme_service import ThemeService
from ui.views.scheduler_view import (
    MAX_QTIME_INTERVAL_SECONDS,
    SchedulerView,
    create_interval_time_edit,
    interval_seconds_from_editor,
    interval_seconds_from_time,
    interval_time_from_seconds,
)


class ScheduleModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.previous_style = cls.app.styleSheet()
        cls.app.setStyleSheet(ThemeService().load_stylesheet())

    @classmethod
    def tearDownClass(cls):
        cls.app.setStyleSheet(cls.previous_style)

    def setUp(self):
        self.view = SchedulerView.__new__(SchedulerView)
        QWidget.__init__(self.view)
        self.view._manual_run_executor = Mock()
        self.addCleanup(self.view.close)
        self.view.game_id = "test-game"
        self.view.logger = Mock()
        self.view.scheduler_service = Mock()
        self.view.refresh = Mock()
        self.profile_combo = QComboBox()
        self.profile_combo.addItem("Test profile", "profile")
        self.view._profile_selector = lambda selected_id="": self.profile_combo
        self.event = Event(
            event_id="event", game_id="test-game", profile_id="profile",
            name="Test event", schedule_mode="manual",
        )
        self.view.scheduler_service.load_event.return_value = self.event

    def dialog_interaction(self, initial, selected, accept=True):
        def execute(dialog):
            self.addCleanup(dialog.close)
            modes = [
                combo for combo in dialog.findChildren(QComboBox)
                if [combo.itemText(index) for index in range(combo.count())] == ["fixed", "manual"]
            ]
            self.assertEqual(1, len(modes))
            interval_editors = dialog.findChildren(QTimeEdit)
            self.assertEqual(1, len(interval_editors))
            self.assertEqual("HH:mm:ss", interval_editors[0].displayFormat())
            combo = modes[0]
            self.assertFalse(combo.isEditable())
            self.assertEqual(initial, combo.currentText())
            dialog.show()
            combo.setFocus()
            QTest.qWait(10)
            self.assertEqual(initial, combo.currentText())
            combo.showPopup()
            QTest.qWait(10)
            self.assertTrue(combo.view().isVisible())
            index = combo.model().index(combo.findText(selected), 0)
            rectangle = combo.view().visualRect(index)
            self.assertFalse(rectangle.isEmpty())
            QTest.mouseClick(combo.view().viewport(), Qt.LeftButton, pos=rectangle.center())
            QTest.qWait(10)
            self.assertEqual(selected, combo.currentText())
            self.assertEqual(selected, combo.itemText(combo.currentIndex()))
            combo.clearFocus()
            combo.setFocus()
            QTest.qWait(10)
            self.assertEqual(selected, combo.currentText())
            return QDialog.Accepted if accept else QDialog.Rejected
        return execute

    def test_add_event_can_select_manual_and_keep_it_visible(self):
        with patch.object(QDialog, "exec", self.dialog_interaction("fixed", "manual")):
            event = self.view.add_event_dialog()
        self.assertEqual("manual", event.schedule_mode)
        self.view.scheduler_service.save_event.assert_called_once_with(event)

    def test_edit_manual_event_can_select_fixed(self):
        with patch.object(QDialog, "exec", self.dialog_interaction("manual", "fixed")):
            event = self.view._edit_event_dialog("event")
        self.assertEqual("fixed", event.schedule_mode)
        self.view.scheduler_service.save_event.assert_called_once_with(event)

    def test_edit_fixed_event_can_select_manual(self):
        self.event.schedule_mode = "fixed"
        self.event.interval_seconds = 300
        self.event.run_at_local = ["12:30"]
        with patch.object(QDialog, "exec", self.dialog_interaction("fixed", "manual")):
            event = self.view._edit_event_dialog("event")
        self.assertEqual("manual", event.schedule_mode)
        self.assertEqual(0, event.interval_seconds)
        self.assertEqual([], event.run_at_local)

    def test_cancel_does_not_persist_changed_mode(self):
        with patch.object(QDialog, "exec", self.dialog_interaction("manual", "fixed", accept=False)):
            self.assertIsNone(self.view._edit_event_dialog("event"))
        self.assertEqual("manual", self.event.schedule_mode)
        self.view.scheduler_service.save_event.assert_not_called()

    def test_interval_time_conversion_keeps_seconds_storage_contract(self):
        self.assertEqual(QTime(1, 2, 3), interval_time_from_seconds(3723))
        self.assertEqual(3723, interval_seconds_from_time(QTime(1, 2, 3)))

    def test_edit_fixed_interval_uses_hours_minutes_and_seconds(self):
        self.event.schedule_mode = "fixed"
        self.event.interval_seconds = 300

        def execute(dialog):
            editor = dialog.findChild(QTimeEdit)
            self.assertEqual(QTime(0, 5, 0), editor.time())
            editor.setTime(QTime(1, 2, 3))
            return QDialog.Accepted

        with patch.object(QDialog, "exec", execute):
            event = self.view._edit_event_dialog("event")

        self.assertEqual(3723, event.interval_seconds)

    def test_untouched_legacy_interval_over_24_hours_is_preserved(self):
        legacy_seconds = MAX_QTIME_INTERVAL_SECONDS + 3601
        editor = create_interval_time_edit(legacy_seconds)
        self.addCleanup(editor.close)

        self.assertEqual(
            legacy_seconds,
            interval_seconds_from_editor(editor),
        )

        editor.setTime(QTime(2, 0, 0))
        self.assertEqual(7200, interval_seconds_from_editor(editor))


if __name__ == "__main__":
    def timeout():
        print("TEST RESULT: TIMEOUT", flush=True)
        faulthandler.dump_traceback()
        os._exit(2)

    started = time.monotonic()
    print("TEST START: scheduler schedule mode", flush=True)
    watchdog = Timer(30, timeout)
    watchdog.daemon = True
    watchdog.start()
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(ScheduleModeTests)
    )
    watchdog.cancel()
    print(f"TEST DURATION: {time.monotonic() - started:.3f}s", flush=True)
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE", flush=True)
    sys.exit(0 if result.wasSuccessful() else 1)
