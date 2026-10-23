import faulthandler
import os
import sys
from datetime import datetime, timedelta, timezone
from threading import Thread, Timer, get_ident
import time
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QWidget,
    QTreeWidget,
    QTreeWidgetItem,
)

from models.function_block import FunctionBlock
from services.function_block_catalog import (
    BLOCK_COMPARE,
    BLOCK_CONDITION,
    BLOCK_DUE_DATE_REMAINING,
    BLOCK_DURATION_PARSE,
    BLOCK_MATH,
    BLOCK_OCR_READ,
    BLOCK_REPEAT,
    BLOCK_RESCHEDULE_EVENT,
    BLOCK_SEARCH_OCR,
    BLOCK_SEARCH_OCR_TAP,
    BLOCK_STOP_EVENT,
    BLOCK_TIMER,
    BLOCK_TYPE_TEXT,
    BLOCK_VARIABLE,
)
from scheduler.models import Event, SchedulerService, Task
from ui.views.function_editor_view import FunctionEditorView
from ui.views.scheduler_view import SchedulerView
from ui.i18n.translator import t
from services.variable_service import VariableService
from services.scheduler_field_service import SchedulerFieldService, field_scope_key
from ui.views.scheduler_view import _FieldSaveTask
from models.execution_context import ExecutionContext
from ui.views.dependencies_view import DependenciesView


class EventControlUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def editor(self, event_id="Gathering"):
        view = FunctionEditorView.__new__(FunctionEditorView)
        QWidget.__init__(view)
        self.addCleanup(view.close)
        view.selected_block = FunctionBlock(
            block_id="reschedule", block_type="reschedule_event", params={"event_id": event_id},
        )
        view.game_combo = QComboBox()
        view.game_combo.addItem("Game", "game")
        view.profile_combo = QComboBox()
        view.profile_combo.addItem("Account", "account")
        view.param_layout = QFormLayout(view)
        view.block_error = QLabel()
        view.logger = Mock()
        view.profile_service = Mock()
        view.profile_service.get_profile.return_value = {
            "id": "account", "botstudio_profile_id": "account", "game": "game",
        }
        view._event_choice_pool = QThreadPool(view)
        view._event_choice_tasks = {}
        view.runtime_service = Mock()
        view.runtime_service.scheduler_service.list_events.return_value = [
            Event(event_id="uuid-target", name="Gathering", profile_id="account"),
        ]
        self.addCleanup(lambda: view._event_choice_pool.waitForDone(2000))
        return view

    def wait_for(self, condition):
        deadline = time.monotonic() + 2
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertTrue(condition(), "queued UI update did not arrive")

    def test_target_choices_load_off_gui_thread_and_preserve_selection(self):
        view = self.editor()
        calls = []
        events = view.runtime_service.scheduler_service.list_events.return_value
        view.runtime_service.scheduler_service.list_events.side_effect = (
            lambda game, profile: calls.append((get_ident(), game, profile)) or events
        )
        view._add_parameter_row(view.selected_block, "event_id", "")
        self.wait_for(lambda: not view._event_choice_tasks)
        self.assertNotEqual(get_ident(), calls[0][0])
        self.assertEqual(("game", "account"), calls[0][1:])
        self.assertEqual("Gathering", view._event_target_combo.currentText())
        view._event_target_combo.setCurrentText("Another event")
        self.assertEqual("Another event", view.selected_block.params["event_id"])

    def test_legacy_event_id_is_not_silently_rewritten(self):
        view = self.editor("uuid-target")
        view._add_event_choice_row(view.selected_block)
        self.wait_for(lambda: not view._event_choice_tasks)
        self.assertEqual("uuid-target", view.selected_block.params["event_id"])

    def test_field_inspector_shows_field_description(self):
        view = SchedulerView.__new__(SchedulerView)
        QWidget.__init__(view)
        self.addCleanup(view.close)
        view._manual_run_executor = Mock()
        view.field_inspector = QWidget(view)
        view.field_form = QFormLayout(view.field_inspector)

        view._render_field_controls([{
            "name": "Unlock VIP",
            "description": "Enable the 30-day VIP reward.",
            "value_type": "boolean",
            "value": False,
            "required": True,
            "from_helper": False,
            "error": "",
            "unconfirmed": False,
        }])

        label = view.field_form.itemAt(
            0,
            QFormLayout.LabelRole,
        ).widget()
        self.assertIn("Unlock VIP", label.text())
        self.assertIn("Enable the 30-day VIP reward.", label.text())

    def test_profile_variables_appear_without_event_links_and_keep_profile_values(self):
        with tempfile.TemporaryDirectory() as root:
            variables = VariableService(root=root, reinitialize_service=Mock())
            variables.create_variable(
                "game", "Marches", "Available march slots", 5,
                kind="profile", value_type="number",
            )
            variables.create_variable("game", "Remaining", kind="live", value_type="number")
            variables.create_variable("game", "ConfigOnly", kind="config", value_type="number")
            variables.ensure_global("game", "Legacy event cooldown")
            variables.ensure_global("game", "MultiAccount_NextAccountName")
            variables.declare_variables("game", [{
                "name": "EventOnly", "kind": "profile", "value_type": "number",
                "field_target": "event", "initial_value": 2,
                "writable_by_functions": False,
            }])
            functions = Mock()
            functions.list_functions.return_value = []
            fields = SchedulerFieldService(functions, variables)
            scope = field_scope_key("profile", "one", "")
            snapshot = fields.collect_profile_fields("game", "one", [])
            descriptors = snapshot[scope]
            self.assertEqual(["Marches"], [field["name"] for field in descriptors])
            self.assertEqual(5, descriptors[0]["value"])

            request = dict(descriptors[0], game_id="game", value=3)
            results = []
            worker = _FieldSaveTask(root, request)
            worker.signals.finished.connect(lambda value, error: results.append((value, error)))
            worker.run()
            self.assertEqual([(3, None)], results)
            fresh = VariableService(root=root, reinitialize_service=Mock())
            self.assertEqual(3, fresh.get_profile_value("game", "one", "Marches"))
            self.assertEqual(5, fields.collect_profile_fields(
                "game", "two", [],
            )[field_scope_key("profile", "two", "")][0]["value"])
            context = ExecutionContext(
                "one", "fn", game_id="game", variable_service=fresh,
                event_id="event", task_id="task",
            )
            self.assertEqual(3, context.get("Marches"))
            with self.assertRaises(PermissionError):
                context.set("Marches", 4)
            fresh.initialize_profile_values("game", "one", reset=True)
            self.assertEqual(3, fresh.get_profile_value("game", "one", "Marches"))

    def test_profile_field_indicator_is_distinct_and_opens_the_inspector(self):
        view = SchedulerView.__new__(SchedulerView)
        QWidget.__init__(view)
        self.addCleanup(view.close)
        view._manual_run_executor = Mock()
        view.tree = QTreeWidget(view)
        view.tree.setColumnCount(7)
        view._show_field_inspector = Mock()
        profile = QTreeWidgetItem(["Account"])
        profile.setData(0, Qt.UserRole, {"kind": "profile", "profile_id": "one"})
        event = QTreeWidgetItem(["Event"])
        event.setData(0, Qt.UserRole, {"kind": "event", "event_id": "event"})
        view.tree.addTopLevelItems([profile, event])
        fields = [{"name": "Marches", "value_type": "number", "value": 5}]
        view._set_item_fields(profile, fields)
        view._set_item_fields(event, fields)
        self.assertEqual("◈ 1", profile.text(5))
        self.assertNotEqual(profile.text(5), event.text(5))
        view._tree_item_clicked(profile, 5)
        view._show_field_inspector.assert_called_once_with(profile, fields)
        view.tree.takeTopLevelItem(1)
        snapshot = {
            "profiles": [{"botstudio_profile_id": "one", "name": "Account"}],
            "fields_by_scope": {},
        }
        self.assertTrue(view._update_tree_items_in_place(snapshot))
        self.assertEqual("", profile.text(5))
        snapshot["fields_by_scope"][field_scope_key("profile", "one", "")] = fields
        self.assertTrue(view._update_tree_items_in_place(snapshot))
        self.assertEqual("◈ 1", profile.text(5))

    def test_new_variable_notification_refreshes_the_scheduler(self):
        view = SchedulerView.__new__(SchedulerView)
        QWidget.__init__(view)
        self.addCleanup(view.close)
        view._manual_run_executor = Mock()
        received = []
        view.schedule_changed.connect(received.append)
        view._schedule_persisted("variable", {"game_id": "game"})
        self.assertEqual(["game"], received)

    def test_run_selected_persists_queue_without_runtime_or_free_profile(self):
        with tempfile.TemporaryDirectory() as root:
            scheduler = SchedulerService(
                base_path=os.path.join(root, "scheduler"),
                logger=Mock(),
            )
            event = Event(
                event_id="event-queued",
                game_id="game",
                profile_id="account",
                name="Queued event",
                enabled=False,
                next_due_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                tasks=[Task(name="Run", function_ids=["function"])],
            )
            scheduler.save_event(event)
            view = SchedulerView.__new__(SchedulerView)
            QWidget.__init__(view)
            self.addCleanup(view.close)
            view.game_id = "game"
            view._selected_kind = Mock(return_value={
                "kind": "event",
                "event_id": event.event_id,
            })
            view.scheduler_service = scheduler
            view.profile_service = Mock()
            view.profile_service.get_profile.return_value = {"id": "account"}
            view._runtime_service = Mock(running=False)
            view._manual_run_executor = Mock()
            view._manual_run_executor.submit.side_effect = (
                lambda function, *args: function(*args)
            )
            view._active_manual_run = None
            view.logger = Mock()
            feedback = []
            view.action_feedback.connect(
                lambda _event_id, message: feedback.append(message)
            )

            self.assertTrue(view.run_selected())

            restarted_scheduler = SchedulerService(
                base_path=os.path.join(root, "scheduler"),
                logger=Mock(),
            )
            persisted = restarted_scheduler.load_event("game", event.event_id)
            self.assertEqual("queued", persisted.status)
            self.assertTrue(persisted.is_due())
            self.assertIn(t("scheduler.event_queued"), feedback)
            view._runtime_service.request_schedule.assert_called_once_with("account")
            view._runtime_service.try_acquire_profile_execution.assert_not_called()

    def test_profile_selection_disables_event_actions_without_loading_an_event(self):
        view = SchedulerView.__new__(SchedulerView)
        QWidget.__init__(view)
        self.addCleanup(view.close)
        view._manual_run_executor = Mock()
        view.game_id = "game"
        view._active_manual_run = None
        view._selected_kind = Mock(return_value={"kind": "profile", "profile_id": "one"})
        view.scheduler_service = Mock()
        view.selection_label = QLabel()
        for name in ("run_button", "cancel_button", "add_task_button",
                     "edit_button", "toggle_button", "delete_button"):
            setattr(view, name, Mock())
        view._update_action_states()
        self.assertIsNone(view.toggle_selected_enabled())
        view.scheduler_service.load_event.assert_not_called()
        for name in ("run_button", "cancel_button", "add_task_button",
                     "edit_button", "toggle_button", "delete_button"):
            getattr(view, name).setEnabled.assert_called_once_with(False)
        self.assertEqual(t("scheduler.no_event_selected"), view.selection_label.text())

    def test_dependencies_can_create_profile_variable_without_a_function(self):
        from PySide6.QtWidgets import QDialog
        from ui.widgets.variable_dialog import VariableCreateDialog
        with tempfile.TemporaryDirectory() as root:
            variables = VariableService(root=root, reinitialize_service=Mock())
            view = DependenciesView.__new__(DependenciesView)
            QWidget.__init__(view)
            self.addCleanup(view.close)
            view.game_combo = QComboBox()
            view.game_combo.addItem("Game", "game")
            view.dependency_service = Mock(variable_service=variables)
            view.refresh = Mock()

            def accept_dialog(game_id, service, parent):
                dialog = VariableCreateDialog(game_id, service, parent=parent)
                dialog.name_edit.setText("Marches")
                dialog.kind_combo.setCurrentIndex(dialog.kind_combo.findData("profile"))
                dialog.type_combo.setCurrentIndex(dialog.type_combo.findData("number"))
                dialog.initial_value_edit.setText("5")
                dialog._on_accept()
                self.assertEqual(QDialog.Accepted, dialog.result())
                dialog.exec = Mock(return_value=dialog.result())
                return dialog

            with patch("ui.widgets.variable_dialog.VariableCreateDialog", side_effect=accept_dialog):
                view._create_variable()
            self.assertEqual("profile", variables.get_definition("game", "Marches")["kind"])
            self.assertEqual(5, variables.get_definition("game", "Marches")["initial_value"])
            view.refresh.assert_called_once()
            functions = Mock()
            functions.list_functions.return_value = []
            fields = SchedulerFieldService(functions, variables).collect_profile_fields(
                "game", "profile", [], functions=[],
            )
            self.assertEqual(["Marches"], [
                field["name"] for field in fields[field_scope_key("profile", "profile", "")]
            ])

            view.refresh.reset_mock()
            with patch("ui.widgets.variable_dialog.VariableCreateDialog") as factory:
                factory.return_value.exec.return_value = QDialog.Rejected
                view._create_variable()
                view.refresh.assert_not_called()
                view.game_combo.clear()
                factory.reset_mock()
                view._create_variable()
                factory.assert_not_called()

    def test_variable_dialog_explains_kind_and_scope_separately(self):
        from ui.widgets.variable_dialog import VariableCreateDialog

        kind_descriptions = {
            "config": "variables.kind_config_description",
            "profile": "variables.kind_profile_description",
            "live": "variables.kind_live_description",
        }
        for kind, description_key in kind_descriptions.items():
            with self.subTest(kind=kind):
                dialog = VariableCreateDialog("game", Mock())
                self.addCleanup(dialog.close)
                dialog.kind_combo.setCurrentIndex(
                    dialog.kind_combo.findData(kind)
                )
                self.assertEqual(
                    t(description_key),
                    dialog.kind_description.text(),
                )
                self.assertEqual(
                    t("variables.scope_profile_description"),
                    dialog.scope_description.text(),
                )

        for target, description_key in (
            ("event", "variables.scope_event_description"),
            ("task", "variables.scope_task_description"),
        ):
            with self.subTest(target=target):
                dialog = VariableCreateDialog(
                    "game",
                    Mock(),
                    field_target=target,
                )
                self.addCleanup(dialog.close)
                self.assertEqual(
                    t(description_key),
                    dialog.scope_description.text(),
                )

    def test_ingame_account_id_resolves_to_scheduler_profile_id_off_gui_thread(self):
        view = self.editor()
        calls = []
        view.profile_service.get_profile.side_effect = lambda selected: (
            calls.append((get_ident(), selected)) or {
                "id": "account", "botstudio_profile_id": "studio-uuid", "game": "game",
            }
        )
        view.runtime_service.scheduler_service.list_events.return_value = [
            Event(event_id="gather", name="Gather Coal", profile_id="studio-uuid"),
            Event(event_id="foreign", name="Other account", profile_id="foreign-uuid"),
            Event(event_id="global", name="Unassigned legacy", profile_id=""),
        ]
        for block_type in ("reschedule_event", "set_event_due"):
            view.selected_block.block_type = block_type
            view._add_event_choice_row(view.selected_block)
            self.wait_for(lambda: not view._event_choice_tasks)
            combo = view._event_target_combo
            self.assertEqual(["", "Gather Coal"], [
                combo.itemText(index) for index in range(combo.count())
            ])
            combo.setCurrentIndex(1)
            combo._prepare_search()
            self.assertEqual("Gather Coal", combo.currentText())
            self.assertEqual("Gather Coal", view.selected_block.params["event_id"])
        self.assertTrue(all(thread != get_ident() for thread, _ in calls))
        view.runtime_service.scheduler_service.list_events.assert_called_with("game", "studio-uuid")

    def test_wrong_game_account_reports_error_instead_of_listing_events(self):
        view = self.editor()
        view.profile_service.get_profile.return_value["game"] = "different-game"
        view._add_event_choice_row(view.selected_block)
        self.wait_for(lambda: not view._event_choice_tasks)
        self.assertIn("another game", view.block_error.text())
        view.runtime_service.scheduler_service.list_events.assert_not_called()

    def test_target_loading_failure_is_visible(self):
        view = self.editor()
        view.runtime_service.scheduler_service.list_events.side_effect = OSError("test read failure")
        view._add_event_choice_row(view.selected_block)
        self.wait_for(lambda: not view._event_choice_tasks)
        self.assertIn("test read failure", view.block_error.text())
        view.logger.error.assert_called_once()

    def test_scheduler_refresh_is_queued_even_with_runtime_stopped(self):
        view = SchedulerView.__new__(SchedulerView)
        QWidget.__init__(view)
        self.addCleanup(view.close)
        view._manual_run_executor = Mock()
        view.game_id = "game"
        view._field_change_generation = 0
        view.field_inspector = QWidget()
        view._runtime_service = Mock(running=False)
        calls = []
        view.refresh = lambda: calls.append(get_ident())
        view.schedule_changed.connect(view._on_schedule_changed, Qt.QueuedConnection)
        view.show()
        thread = Thread(target=lambda: view._schedule_persisted("scheduler", {"game_id": "game"}))
        thread.start()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], calls)
        self.wait_for(lambda: bool(calls))
        self.assertEqual([get_ident()], calls)
        self.assertEqual(1, view._field_change_generation)
        view._on_schedule_changed("other-game")
        self.assertEqual(1, len(calls))

    def test_unscheduled_event_shows_profile_activation_instead_of_stuck(self):
        from scheduler.models import Task
        event = Event(
            event_id="target", name="Gathering", status="success",
            last_finished_at="2026-10-05T12:00:00+00:00",
            tasks=[Task(name="Gather", function_ids=["function"])],
        )
        self.assertEqual(t("scheduler.on_profile_activation"), SchedulerView._format_next_due(event))
        event.status = "failed"
        self.assertEqual(t("scheduler.due_now"), SchedulerView._format_next_due(event))

    def test_literal_or_variable_inputs_accept_zero_without_autocomplete(self):
        view = FunctionEditorView.__new__(FunctionEditorView)
        QWidget.__init__(view)
        self.addCleanup(view.close)
        view.param_layout = QFormLayout(view)
        view.is_editable = lambda: True
        view._collect_available_variables = lambda: ["$Pet Skill", "$Cooldown"]
        view._parameter_label = lambda key: key

        cases = (
            (BLOCK_DURATION_PARSE, "input"),
            (BLOCK_DUE_DATE_REMAINING, "input"),
            (BLOCK_MATH, "left"),
            (BLOCK_COMPARE, "right"),
            (BLOCK_CONDITION, "expression"),
            (BLOCK_TYPE_TEXT, "text"),
            (BLOCK_TIMER, "variable"),
            (BLOCK_REPEAT, "max_iterations"),
            (BLOCK_REPEAT, "until_variable"),
            (BLOCK_STOP_EVENT, "delay_variable"),
            (BLOCK_RESCHEDULE_EVENT, "delay_variable"),
            (BLOCK_VARIABLE, "value"),
            (BLOCK_OCR_READ, "default_value"),
            (BLOCK_SEARCH_OCR, "query"),
            (BLOCK_SEARCH_OCR_TAP, "query"),
        )
        blocks = []
        for index, (block_type, key) in enumerate(cases):
            block = FunctionBlock(
                block_id=f"literal-{index}",
                block_type=block_type,
                params={key: ""},
            )
            blocks.append(block)
            view._add_parameter_row(block, key, "")

        view.show()
        for index, ((block_type, key), block) in enumerate(zip(cases, blocks)):
            with self.subTest(block_type=block_type, key=key):
                row_widget = view.param_layout.itemAt(index * 2 + 1).widget()
                value_edit = row_widget.findChild(QLineEdit)
                variable_combo = row_widget.findChild(QComboBox)

                self.assertIsNotNone(value_edit)
                self.assertIsNotNone(variable_combo)
                value_edit.setFocus()
                QTest.keyClicks(value_edit, "0")
                self.app.processEvents()

                self.assertEqual("0", block.params[key])
                self.assertEqual("0", value_edit.text())
                self.assertFalse(variable_combo.completer().popup().isVisible())
                self.assertEqual(
                    {"$Pet Skill", "$Cooldown"},
                    {
                        variable_combo.itemText(option_index)
                        for option_index in range(1, variable_combo.count())
                    },
                )
                variable_combo._activate_completion("$Pet Skill")
                self.assertEqual("$Pet Skill", block.params[key])

    def test_existing_variable_references_are_selected_in_the_picker(self):
        view = FunctionEditorView.__new__(FunctionEditorView)
        QWidget.__init__(view)
        self.addCleanup(view.close)
        view.param_layout = QFormLayout(view)
        view.is_editable = lambda: True
        view._collect_available_variables = lambda: ["$Pet Skill", "$Cooldown"]
        view._parameter_label = lambda key: key

        cases = (
            (BLOCK_DURATION_PARSE, "input"),
            (BLOCK_SEARCH_OCR, "query"),
        )
        for index, (block_type, key) in enumerate(cases):
            block = FunctionBlock(
                block_id=f"existing-{index}",
                block_type=block_type,
                params={key: "$Pet Skill"},
            )
            view._add_parameter_row(block, key, "")
            row_widget = view.param_layout.itemAt(index * 2 + 1).widget()
            value_edit = row_widget.findChild(QLineEdit)
            variable_combo = row_widget.findChild(QComboBox)

            with self.subTest(block_type=block_type, key=key):
                self.assertEqual("$Pet Skill", value_edit.text())
                self.assertEqual("$Pet Skill", variable_combo.currentText())
                value_edit.setText("0")
                self.assertEqual(0, variable_combo.currentIndex())


if __name__ == "__main__":
    def timeout():
        print("TEST RESULT: TIMEOUT", flush=True)
        faulthandler.dump_traceback()
        os._exit(2)

    started = time.monotonic()
    print("TEST START: event control UI", flush=True)
    watchdog = Timer(30, timeout)
    watchdog.daemon = True
    watchdog.start()
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(EventControlUiTests)
    )
    watchdog.cancel()
    print(f"TEST DURATION: {time.monotonic() - started:.3f}s", flush=True)
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE", flush=True)
    sys.exit(0 if result.wasSuccessful() else 1)
