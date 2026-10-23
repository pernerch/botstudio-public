"""Scheduler refresh races must not interrupt input or touch live stores."""

import faulthandler
import os
import time
import unittest
from threading import Timer
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFormLayout, QInputDialog, QLineEdit, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ui.views.scheduler_view import SchedulerView


class SchedulerFieldFocusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.started = time.monotonic()
        print(f"TEST START: {self.id()}", flush=True)
        watchdog = Timer(15, lambda: (
            print("TEST RESULT: TIMEOUT", flush=True),
            faulthandler.dump_traceback(), os._exit(124),
        ))
        watchdog.daemon = True
        watchdog.start()
        self.addCleanup(watchdog.cancel)
        view = self.view = SchedulerView.__new__(SchedulerView)
        QWidget.__init__(view)
        layout = QVBoxLayout(view)
        view.tree = QTreeWidget()
        layout.addWidget(view.tree)
        view.field_inspector = QWidget()
        view.field_form = QFormLayout(view.field_inspector)
        layout.addWidget(view.field_inspector)
        self.editor = QLineEdit("original")
        view.field_form.addRow("Value", self.editor)
        self.outside = QLineEdit()
        layout.addWidget(self.outside)
        view.game_id = "isolated"
        view.logger = Mock()
        view._runtime_service = SimpleNamespace(running=False)
        view._current_event_filter = Mock(return_value="")
        view._inspector_scope = {"kind": "task", "task_id": "t"}
        item = QTreeWidgetItem(["Task"])
        item.setData(0, Qt.UserRole, view._inspector_scope)
        item.setData(0, view.FIELD_DATA_ROLE, [{"name": "Value", "value": "server"}])
        view.tree.addTopLevelItem(item)
        view.tree.setCurrentItem(item)
        view._current_view_mode = Mock(return_value="Tree")
        view._field_change_generation = 0
        view._field_save_tasks = set()
        view._field_snapshot = {}
        view._refresh_running = True
        view._refresh_pending = False
        view._refresh_task = None
        view._refresh_state = (dict(view._inspector_scope), True, 0, 0)
        view._filter_snapshot_events = Mock()
        view._update_tree_items_in_place = Mock(return_value=True)
        view._refresh_impl = Mock()
        view._update_action_states = Mock()
        view._apply_pending_reveal = Mock()
        view._render_field_controls = Mock()
        view.refresh = Mock()
        view.show()
        view.activateWindow()
        self.app.processEvents()
        def cleanup():
            view.hide()
            view.deleteLater()
            QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            self.app.processEvents()
        self.addCleanup(cleanup)

    def tearDown(self):
        print(f"TEST END: {self.id()} ({time.monotonic() - self.started:.3f}s)", flush=True)

    def apply_snapshot(self):
        self.view._apply_refresh_snapshot(({
            "game_id": "isolated", "view_mode": "Tree",
            "field_generation": 0, "fields_by_scope": {},
        }, None))
        self.app.processEvents()

    def test_late_snapshot_never_restores_tree_focus_over_another_input(self):
        self.outside.setFocus()
        QTest.keyClicks(self.outside, "draft")
        self.apply_snapshot()
        self.assertIs(self.outside, QApplication.focusWidget())
        self.assertEqual("draft", self.outside.text())
        self.view._update_tree_items_in_place.assert_called_once()

    def test_inflight_refresh_does_not_touch_field_editor_with_runtime_off_or_on(self):
        for running in (False, True):
            with self.subTest(runtime_running=running):
                self.view.runtime_service.running = running
                self.editor.setFocus()
                self.editor.setText("uncommitted")
                self.editor.setCursorPosition(4)
                self.apply_snapshot()
                self.assertIs(self.editor, QApplication.focusWidget())
                self.assertEqual("uncommitted", self.editor.text())
                self.assertEqual(4, self.editor.cursorPosition())
                self.view._update_tree_items_in_place.assert_not_called()
                self.view._render_field_controls.assert_not_called()
                self.assertFalse(self.view._refresh_running)
        self.outside.setFocus()
        self.apply_snapshot()
        self.view._update_tree_items_in_place.assert_called_once()

    def test_collection_dialog_is_protected_from_an_inflight_refresh(self):
        dialog = QInputDialog(self.view)
        dialog.setInputMode(QInputDialog.TextInput)
        dialog.setOption(QInputDialog.UsePlainTextEditForTextInput)
        dialog.setModal(True)
        dialog.show()
        self.app.processEvents()
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        self.apply_snapshot()
        self.assertIs(dialog, QApplication.activeModalWidget())
        self.view._update_tree_items_in_place.assert_not_called()
        self.view._render_field_controls.assert_not_called()

    def test_open_dropdown_is_not_replaced_or_closed_by_refresh(self):
        combo = QComboBox()
        combo.addItems(["A", "B", "C"])
        self.view.field_form.addRow("Options", combo)
        combo.setFocus()
        combo.showPopup()
        self.app.processEvents()
        self.assertTrue(combo.view().isVisible())
        self.apply_snapshot()
        self.assertTrue(combo.view().isVisible())
        self.view._update_tree_items_in_place.assert_not_called()
        self.view._render_field_controls.assert_not_called()
        combo.hidePopup()

    def test_timer_starts_refresh_only_after_input_finishes(self):
        self.editor.setFocus()
        self.view._refresh_if_visible()
        self.view.refresh.assert_not_called()
        self.outside.setFocus()
        self.view._refresh_if_visible()
        self.view.refresh.assert_called_once()

    def test_inflight_field_save_defers_snapshot_application(self):
        self.outside.setFocus()
        self.view._field_save_tasks.add(object())
        self.apply_snapshot()
        self.view._update_tree_items_in_place.assert_not_called()
        self.view._render_field_controls.assert_not_called()

    def test_structure_change_cannot_destroy_active_field_controls(self):
        self.view._update_tree_items_in_place.return_value = False
        self.editor.setFocus()
        self.editor.setSelection(1, 4)
        self.apply_snapshot()
        self.view._refresh_impl.assert_not_called()
        self.assertEqual("rigi", self.editor.selectedText())
        self.assertIs(self.editor, QApplication.focusWidget())

    def test_already_queued_inspector_refresh_keeps_active_input(self):
        self.outside.setFocus()
        QTimer.singleShot(0, self.view._refresh_open_field_inspector)
        self.editor.setFocus()
        self.editor.setText("typing")
        self.app.processEvents()
        self.view._render_field_controls.assert_not_called()
        self.assertEqual("typing", self.editor.text())
        self.assertIs(self.editor, QApplication.focusWidget())

    def test_repeated_timer_results_do_not_interrupt_typing(self):
        self.editor.setFocus()
        self.editor.clear()
        delivered = []
        timer = QTimer(self.view)
        timer.setInterval(20)
        def deliver():
            self.view._refresh_state = (dict(self.view._inspector_scope), True, 0, 0)
            self.view._apply_refresh_snapshot(({
                "game_id": "isolated", "view_mode": "Tree", "field_generation": 0,
            }, None))
            delivered.append(True)
        timer.timeout.connect(deliver)
        timer.start()
        self.addCleanup(timer.stop)
        for text in ("draft", " still", " typing"):
            QTest.keyClicks(self.editor, text)
            QTest.qWait(40)
        self.assertGreaterEqual(len(delivered), 3)
        self.assertEqual("draft still typing", self.editor.text())
        self.assertIs(self.editor, QApplication.focusWidget())
        self.view._render_field_controls.assert_not_called()

    def test_worker_error_is_reported_without_replacing_active_input(self):
        self.editor.setFocus()
        self.view._apply_refresh_snapshot((None, OSError("isolated failure")))
        self.view.logger.error.assert_called_once()
        self.assertIn("isolated failure", self.view.logger.error.call_args.kwargs["error"])
        self.assertIs(self.editor, QApplication.focusWidget())


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE", flush=True)
    raise SystemExit(0 if result.wasSuccessful() else 1)
