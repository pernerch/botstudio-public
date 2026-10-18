import faulthandler
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtCore import QThreadPool, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFormLayout, QLineEdit, QStackedWidget, QWidget

from core.adb_service import AdbService
from core.emulator_manager import EmulatorManager
from services.emulator_scan_service import EmulatorScanService
from services.issue_service import IssueService
from services.reinitialize_service import ReinitializeService
from services.variable_service import VariableService
from ui.views.function_editor_view import FunctionEditorView
from ui.views.main_window import MainWindow
from ui.views.log_view import LogView
from ui.views.runtime_overview_view import RuntimeOverviewView
from ui.views.scheduler_view import SchedulerView
from ui.widgets.searchable_combo import SearchableComboBox


class LiveViewsRefreshTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.started = time.monotonic()
        print(f"Test Start: {self.id()}")
        faulthandler.dump_traceback_later(10, exit=True)
        self.addCleanup(faulthandler.cancel_dump_traceback_later)
        self.addCleanup(lambda: print(
            f"Test End: {self.id()} Duration: {time.monotonic() - self.started:.3f}s",
        ))

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate())

    def make_log_view(self):
        issue_service = Mock()
        issue_service.list_issues.return_value = []
        issue_service.status_counts.return_value = {
            status: 0 for status in (
                IssueService.STATUS_OPEN, IssueService.STATUS_IN_PROGRESS,
                IssueService.STATUS_WAITING_FOR_USER, IssueService.STATUS_CLOSED,
            )
        }
        profile_service = Mock()
        profile_service.load_all.return_value = []
        issue_factory = Mock(return_value=issue_service)
        for name in ("STATUS_OPEN", "STATUS_IN_PROGRESS", "STATUS_WAITING_FOR_USER", "STATUS_CLOSED"):
            setattr(issue_factory, name, getattr(IssueService, name))
        for replacement in (
            patch("ui.views.log_view.IssueService", issue_factory),
            patch("ui.views.log_view.ProfileService", return_value=profile_service),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        view = LogView()
        self.addCleanup(view.deleteLater)
        self.addCleanup(view.hide)
        return view

    @staticmethod
    def log_snapshot(message):
        return (
            [{"message": message, "severity": "INFO"}], [], [],
            {status: 0 for status in (
                IssueService.STATUS_OPEN, IssueService.STATUS_IN_PROGRESS,
                IssueService.STATUS_WAITING_FOR_USER, IssueService.STATUS_CLOSED,
            )},
        )

    def test_live_variable_writes_preserve_open_function_dropdown(self):
        with tempfile.TemporaryDirectory() as directory:
            notifications = ReinitializeService()
            variables = VariableService(
                root=directory, logger=Mock(), reinitialize_service=notifications,
            )
            variables.create_variable("game", "Remaining", value_type="number", initial_value=0)
            editor = FunctionEditorView.__new__(FunctionEditorView)
            QWidget.__init__(editor)
            self.addCleanup(editor.deleteLater)
            self.addCleanup(editor.close)
            editor.param_layout = QFormLayout(editor)
            editor.selected_block = SimpleNamespace(block_type="test")
            editor.logger = Mock()
            combo = SearchableComboBox()
            combo.addItem("Alpha", "a")
            combo.addItem("Beta", "b")
            editor.param_layout.addRow("Variable", combo)
            # Rendering removes the old rows, including the active dropdown.
            editor._render_parameter_controls = Mock(
                side_effect=lambda _block: editor._clear_parameter_controls(),
            )
            window = SimpleNamespace(function_editor_view=editor)
            notifications.subscribe(
                lambda source, payload: MainWindow._handle_reinitialize(window, source, payload),
            )
            editor.show()
            editor.activateWindow()
            combo.setFocus()
            combo.showPopup()
            self.app.processEvents()
            self.assertTrue(combo.view().isVisible())
            focus = QApplication.focusWidget()
            timer = QTimer(editor)
            timer.setInterval(5000)
            timer.timeout.connect(
                lambda: variables.set_profile_value("game", "profile", "Remaining", 42, source="function"),
            )
            timer.start()
            self.addCleanup(timer.stop)
            QTest.qWait(5500)
            self.assertEqual(42, variables.list_profile_values("game", "profile")["Remaining"])
            editor._render_parameter_controls.assert_not_called()
            self.assertTrue(combo.view().isVisible())
            self.assertIs(focus, QApplication.focusWidget())
            combo.hidePopup()

    def test_variable_definition_changes_still_refresh_function_choices(self):
        editor = Mock()
        window = SimpleNamespace(function_editor_view=editor)
        with tempfile.TemporaryDirectory() as directory:
            notifications = ReinitializeService()
            notifications.subscribe(
                lambda source, payload: MainWindow._handle_reinitialize(window, source, payload),
            )
            variables = VariableService(
                root=directory, logger=Mock(), reinitialize_service=notifications,
            )
            variables.create_variable("game", "New", value_type="number", initial_value=0)
            variables.delete_global("game", "New")
        self.assertEqual(2, editor.refresh_variable_choices.call_count)

    def test_field_value_and_profile_sync_notifications_do_not_rebuild_editor(self):
        editor = Mock()
        window = SimpleNamespace(function_editor_view=editor)
        for payload in (
            {"game_id": "game", "profile_id": "p", "variable_name": "Field", "field_target": "event"},
            {"game_id": "game", "profile_id": "p", "synchronized_from": "other"},
        ):
            MainWindow._handle_reinitialize(window, "variable", payload)
        editor.refresh_variable_choices.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows console creation flags")
    def test_background_instance_pid_probe_never_opens_console(self):
        manager = EmulatorManager.__new__(EmulatorManager)
        manager.get_port = Mock(return_value=16384)
        with patch("core.emulator_manager.run_command", return_value=subprocess.CompletedProcess(
            [], 0, stdout="TCP 127.0.0.1:16384 0.0.0.0:0 LISTENING 1234\n",
        )) as run:
            self.assertEqual(1234, manager.resolve_instance_pid(0))
        self.assertEqual(subprocess.CREATE_NO_WINDOW, run.call_args.kwargs.get("creationflags"))
        startup = run.call_args.kwargs["startupinfo"]
        self.assertTrue(startup.dwFlags & subprocess.STARTF_USESHOWWINDOW)
        self.assertEqual(subprocess.SW_HIDE, startup.wShowWindow)

    def test_hidden_logs_never_start_refresh_worker_or_timer(self):
        with patch.object(QThreadPool, "start") as start:
            view = self.make_log_view()
            self.assertFalse(view._refresh_timer.isActive())
            view.refresh()
            view._auto_refresh()
            start.assert_not_called()

    def test_switching_to_functions_discards_late_logs_and_preserves_focus(self):
        with patch.object(QThreadPool, "start") as start:
            view = self.make_log_view()
            stack = QStackedWidget()
            self.addCleanup(stack.deleteLater)
            self.addCleanup(stack.close)
            editor = QLineEdit()
            stack.addWidget(view)
            stack.addWidget(editor)
            stack.show()
            self.app.processEvents()
            self.assertTrue(view._refresh_timer.isActive())
            operation = start.call_args.args[0]
            stack.setCurrentWidget(editor)
            stack.activateWindow()
            editor.setFocus()
            self.app.processEvents()
            self.assertFalse(view._refresh_timer.isActive())
            with patch.object(view, "_sync_filter_items") as filters, patch.object(
                view, "_refresh_issues",
            ) as issues:
                operation.signals.finished.emit(self.log_snapshot("Late"), None)
                QTest.qWait(2100)
                filters.assert_not_called()
                issues.assert_not_called()
            self.assertEqual(0, view.table.topLevelItemCount())
            self.assertIs(editor, QApplication.focusWidget())
            QTest.keyClicks(editor, "Function input")
            self.assertEqual("Function input", editor.text())
            self.assertEqual(1, start.call_count)

    def test_reentering_logs_reloads_instead_of_applying_old_worker_result(self):
        with patch.object(QThreadPool, "start") as start:
            view = self.make_log_view()
            view.show()
            old_operation = start.call_args.args[0]
            view.hide()
            view.show()
            self.assertEqual(1, start.call_count)
            old_operation.signals.finished.emit(self.log_snapshot("Stale"), None)
            self.assertEqual(2, start.call_count)
            self.assertEqual(0, view.table.topLevelItemCount())
            operation = start.call_args.args[0]
            operation.signals.finished.emit(self.log_snapshot("Fresh"), None)
            self.assertEqual("Fresh", view.table.topLevelItem(0).text(5))
            self.assertTrue(view._refresh_timer.isActive())

    def test_visible_log_view_refreshes_new_records_without_click(self):
        issue_service = Mock()
        issue_service.list_issues.return_value = []
        issue_service.status_counts.return_value = {
            status: 0 for status in (
                IssueService.STATUS_OPEN, IssueService.STATUS_IN_PROGRESS,
                IssueService.STATUS_WAITING_FOR_USER, IssueService.STATUS_CLOSED,
            )
        }
        profile_service = Mock()
        profile_service.load_all.return_value = []
        issue_factory = Mock(return_value=issue_service)
        for name in ("STATUS_OPEN", "STATUS_IN_PROGRESS", "STATUS_WAITING_FOR_USER", "STATUS_CLOSED"):
            setattr(issue_factory, name, getattr(IssueService, name))
        records = [{"message": "First", "severity": "INFO"}]
        with (
            patch("ui.views.log_view.IssueService", issue_factory),
            patch("ui.views.log_view.ProfileService", return_value=profile_service),
            patch.object(LogView, "_load_records", side_effect=lambda: list(records)),
            patch("sys.excepthook") as exceptions,
        ):
            view = LogView()
            self.addCleanup(view.deleteLater)
            view.show()
            self.assertTrue(view._refresh_timer.isActive())
            self.assertEqual(2000, view._refresh_timer.interval())
            self.wait_for(lambda: view.table.topLevelItemCount() == 1)
            view.table.setCurrentItem(view.table.topLevelItem(0))
            records.insert(0, {"message": "Second", "severity": "ERROR"})
            view._refresh_timer.setInterval(20)
            self.wait_for(lambda: view.table.topLevelItemCount() == 2)
            self.assertEqual("First", view.table.currentItem().text(5))
            self.assertEqual("Second", view.table.topLevelItem(0).text(5))
            view._refresh_timer.stop()
            self.assertTrue(QThreadPool.globalInstance().waitForDone(1000))
            self.app.processEvents()
            exceptions.assert_not_called()

    def test_overview_refresh_timer_starts_even_when_runtime_was_started_remotely(self):
        runtime = Mock()
        runtime.get_status.return_value = {
            "status": "Runtime Running", "running": True, "profiles": [],
        }
        runtime.heartbeat.interval_seconds = 10
        with (
            patch.object(RuntimeOverviewView, "refresh_profiles"),
            patch.object(RuntimeOverviewView, "_read_events", return_value=[]),
        ):
            view = RuntimeOverviewView(runtime)
            self.addCleanup(view.deleteLater)
            self.assertTrue(view.refresh_timer.isActive())
            self.assertTrue(QThreadPool.globalInstance().waitForDone(1000))
            self.app.processEvents()
            with patch.object(QThreadPool, "start"):
                view._start_runtime_operation("start")
                self.assertTrue(view.btn_stop.isEnabled())
            view.refresh_timer.stop()

    def test_runtime_event_reader_uses_recent_tail_and_ignores_partial_record(self):
        with tempfile.TemporaryDirectory() as directory:
            log_file = Path(directory) / "runtime.log"
            log_file.write_text(
                "x" * 70000 + "\n" + "\n".join(
                    '{"message":"%s"}' % index for index in range(12)
                ) + "\n{",
                encoding="utf-8",
            )
            with patch("ui.views.runtime_overview_view.LOG_DIR", Path(directory)):
                records = RuntimeOverviewView._read_events()
            self.assertEqual([str(index) for index in range(11, 3, -1)], [
                record["message"] for record in records
            ])

    def test_scan_never_marks_unresponsive_android_healthy(self):
        manager = Mock()
        manager.is_instance_running.return_value = True
        adb_runtime = Mock()
        adb_runtime.ensure_device.return_value = True
        adb_runtime.adb.is_responsive.return_value = False
        service = EmulatorScanService(manager, adb_runtime, Mock(), Mock(), Mock())
        result = service._scan_instance(0, {})
        self.assertEqual("Failed", result["state"])
        self.assertIsNone(result["foreground_package"])
        adb_runtime.adb.get_foreground_package.assert_not_called()

    def test_scan_rejects_frozen_renderer_even_when_android_answers(self):
        manager = Mock()
        manager.is_instance_running.return_value = True
        adb_runtime = Mock()
        adb_runtime.ensure_device.return_value = True
        adb_runtime.adb.is_responsive.return_value = True
        service = EmulatorScanService(manager, adb_runtime, Mock(), Mock(), Mock())
        service.health_service.probe_screenshot = Mock(return_value=False)
        result = service._scan_instance(0, {})
        self.assertEqual("Failed", result["state"])
        adb_runtime.adb.get_foreground_package.assert_not_called()

    def test_health_probe_requires_activity_manager_not_static_boot_property(self):
        adb = AdbService("unused")
        with patch.object(adb, "_run", return_value=Mock(returncode=0, stdout="0\n")) as run:
            self.assertTrue(adb.is_responsive("device"))
            self.assertEqual(
                ["shell", "cmd", "activity", "get-current-user"],
                run.call_args.args[0][-4:],
            )
        with patch.object(adb, "_run", return_value=Mock(returncode=0, stdout="service unavailable")):
            self.assertFalse(adb.is_responsive("device"))

    def test_manual_event_queue_does_not_wait_for_stopped_runtime(self):
        runtime = Mock()
        runtime.is_stop_requested.return_value = True
        event = SimpleNamespace(
            event_id="e", status="idle", profile_id="p", next_due_at=None,
        )
        scheduler = Mock()
        scheduler.load_event.return_value = event
        view = SimpleNamespace(
            runtime_service=runtime, scheduler_service=scheduler, game_id="g",
            action_feedback=Mock(), logger=Mock(), _active_manual_run="e",
            schedule_changed=Mock(),
        )
        started = time.monotonic()
        SchedulerView._queue_selected_event_background(view, "e")
        self.assertLess(time.monotonic() - started, 1)
        scheduler.queue_event_now.assert_called_once_with(event)
        scheduler.run_event.assert_not_called()
        runtime.try_acquire_profile_execution.assert_not_called()
        view.action_feedback.emit.assert_called_once()
        view.schedule_changed.emit.assert_called_once_with("g")
        self.assertIsNone(view._active_manual_run)

    def test_manual_event_queue_failure_reports_error_and_releases_ui_state(self):
        runtime = Mock()
        event = SimpleNamespace(
            event_id="e", status="idle", profile_id="p", next_due_at=None,
        )
        scheduler = Mock()
        scheduler.load_event.return_value = event
        scheduler.queue_event_now.side_effect = OSError("test queue write failed")
        view = SimpleNamespace(
            runtime_service=runtime, scheduler_service=scheduler, game_id="g",
            action_feedback=Mock(), logger=Mock(), _active_manual_run="e",
            schedule_changed=Mock(),
        )
        SchedulerView._queue_selected_event_background(view, "e")
        scheduler.queue_event_now.assert_called_once_with(event)
        scheduler.run_event.assert_not_called()
        runtime.try_acquire_profile_execution.assert_not_called()
        view.logger.error.assert_called_once()
        self.assertIn("test queue write failed", view.logger.error.call_args.kwargs["error"])
        view.action_feedback.emit.assert_called_once()
        view.schedule_changed.emit.assert_called_once_with("g")
        self.assertIsNone(view._active_manual_run)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(LiveViewsRefreshTests),
    )
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE")
    sys.exit(0 if result.wasSuccessful() else 1)
