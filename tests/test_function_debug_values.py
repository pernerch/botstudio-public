"""Regression tests for transient Function-debug field values.

All tests are in-memory and have no waits or external dependencies.
"""

import os
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QCheckBox, QWidget

from models.bot_function import BotFunction
from models.execution_context import ExecutionContext
from models.function_block import FunctionBlock
from services.function_block_catalog import (
    BLOCK_CALL_FUNCTION,
    BLOCK_CONFIGURE_FIELD,
)
from services.runtime_supervisor import RuntimeSupervisor
from services.scheduler_field_service import SchedulerFieldService
from services.variable_service import VariableService
from ui.views.function_editor_view import DebugValuesDialog, FunctionEditorView


def configured_field(name, value, value_type="boolean"):
    return FunctionBlock(
        block_id=f"field-{name}",
        block_type=BLOCK_CONFIGURE_FIELD,
        params={
            "variable_name": name,
            "field_target": "event",
            "variable_kind": "config",
            "value_type": value_type,
            "description": "",
            "initial_value": value,
            "required": True,
            "writable_by_functions": False,
            "options_source": "none",
            "fixed_options": [],
            "options_variable": "",
        },
    )


class DebugExecutionContextTests(unittest.TestCase):

    def test_transient_debug_field_can_change_without_persistence(self):
        variable_service = Mock()
        variable_service.initialize_context_values.return_value = {}
        variable_service.get_definition.return_value = {
            "name": "Unlock VIP",
            "field_target": "event",
        }
        context = ExecutionContext(
            game_id="game",
            profile_id="profile",
            function_id="function",
            variable_service=variable_service,
            transient_variable_names={"Unlock VIP"},
        )

        context.set("Unlock VIP", True)

        self.assertIs(context.get("Unlock VIP"), True)
        variable_service.set_context_value.assert_not_called()
        variable_service.set_profile_value.assert_not_called()


class DebugSupervisorTests(unittest.TestCase):

    def test_supervisor_passes_debug_values_to_worker(self):
        profile_service = Mock()
        profile_service.get_profile.return_value = {
            "id": "profile",
            "game": "game",
            "enabled": True,
        }
        worker = Mock()
        worker.process = None
        worker_factory = Mock(return_value=worker)
        supervisor = RuntimeSupervisor(
            profile_service=profile_service,
            function_service=Mock(),
            worker_factory=worker_factory,
        )

        self.assertTrue(
            supervisor.start_function(
                "profile",
                "function",
                allow_disabled=True,
                debug_values={"Unlock VIP": True},
            )
        )
        worker.start.assert_called_once_with(
            "function",
            None,
            debug_values={"Unlock VIP": True},
        )


class DebugFieldCollectionTests(unittest.TestCase):

    def test_helper_fields_are_included(self):
        helper = BotFunction(
            function_id="helper",
            name="Helper",
            game_id="game",
            kind="helper",
            blocks=[configured_field("Helper Flag", "No")],
        )
        caller = BotFunction(
            function_id="caller",
            name="Caller",
            game_id="game",
            blocks=[
                configured_field("Caller Flag", "No"),
                FunctionBlock(
                    block_id="call",
                    block_type=BLOCK_CALL_FUNCTION,
                    params={"function_id": "helper", "inputs": {}},
                ),
            ],
        )
        service = SchedulerFieldService(
            function_service=Mock(),
            variable_service=Mock(),
        )

        declarations = service.collect_function_declarations(
            "game",
            "caller",
            functions=[caller, helper],
        )

        self.assertEqual(
            {"Caller Flag", "Helper Flag"},
            {
                declaration["variable_name"]
                for declaration in declarations
            },
        )
        helper_declaration = next(
            declaration
            for declaration in declarations
            if declaration["variable_name"] == "Helper Flag"
        )
        self.assertTrue(helper_declaration["from_helper"])


class DebugValuesUiTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_boolean_dialog_value_can_be_changed(self):
        fields = [{
            "name": "Unlock VIP",
            "value_type": "boolean",
            "value": False,
            "default": False,
            "options": [],
        }]
        dialog = DebugValuesDialog(fields)
        self.addCleanup(dialog.close)
        control = dialog._controls["Unlock VIP"]
        self.assertIsInstance(control, QCheckBox)
        self.assertFalse(dialog.values()["Unlock VIP"])

        control.setChecked(True)

        self.assertTrue(dialog.values()["Unlock VIP"])

    def test_editor_uses_current_configure_field_default_not_old_registry_default(self):
        function = BotFunction(
            function_id="function",
            name="Function",
            game_id="game",
            blocks=[configured_field("Unlock VIP", "Yes")],
        )
        variable_service = Mock()
        variable_service.get_definition.return_value = {
            "name": "Unlock VIP",
            "value_type": "boolean",
            "initial_value": False,
        }
        variable_service.coerce_value.side_effect = VariableService.coerce_value
        function_service = Mock()
        function_service.list_functions.return_value = [function]

        view = FunctionEditorView.__new__(FunctionEditorView)
        QWidget.__init__(view)
        self.addCleanup(view.close)
        view.current_function = function
        view.function_service = function_service
        view.variable_service = variable_service
        view.scheduler_field_service = SchedulerFieldService(
            function_service=function_service,
            variable_service=variable_service,
        )
        view._debug_values_by_function = {}

        fields = view._debug_field_descriptors()

        self.assertEqual(1, len(fields))
        self.assertIs(fields[0]["default"], True)
        self.assertIs(fields[0]["value"], True)


if __name__ == "__main__":
    try:
        result = unittest.main(exit=False).result
    except Exception as error:  # pragma: no cover - explicit result contract
        print(f"TEST RESULT: FAILURE ({error})")
        raise SystemExit(1)
    if result.wasSuccessful():
        print("TEST RESULT: SUCCESS")
        raise SystemExit(0)
    print("TEST RESULT: FAILURE")
    raise SystemExit(1)
