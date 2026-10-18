"""Exclusive literal/reference inputs; no application data or external services."""

import faulthandler
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFormLayout, QWidget

from models.execution_context import ExecutionContext
from models.function_block import FunctionBlock
from services.function_block_catalog import (
    BLOCK_CATALOG,
    BLOCK_REPEAT,
    BLOCK_SEARCH_OCR,
    BLOCK_SEARCH_OCR_TAP,
    BLOCK_TYPE_TEXT,
    BLOCK_VARIABLE,
)
from services.function_runner import FunctionRunner
from ui.views.function_editor_view import FunctionEditorView
from ui.widgets.variable_value_input import VariableValueInput


class VariableValueInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        faulthandler.dump_traceback_later(15, exit=True)
        self.addCleanup(faulthandler.cancel_dump_traceback_later)

    def make_input(self, value):
        widget = VariableValueInput(value, ["$Pet Skill", "$Cooldown"])
        self.addCleanup(widget.close)
        self.addCleanup(widget.deleteLater)
        return widget

    def test_literals_preserve_zero_false_empty_and_whitespace(self):
        for value, expected in ((0, "0"), (False, "False"), (None, ""), (" x ", " x ")):
            with self.subTest(value=value):
                widget = self.make_input(value)
                self.assertEqual("literal", widget.mode_combo.currentData())
                self.assertEqual(expected, widget.value())
                self.assertIs(widget.literal_edit, widget.inputs.currentWidget())
                self.assertFalse(widget.variable_combo.isEnabled())
                self.assertTrue(widget.create_button.isHidden())

    def test_saved_unknown_and_legacy_scoped_references_open_without_changes(self):
        for reference in ("$Pet Skill", "$global:Pet Skill", "$not registered"):
            with self.subTest(reference=reference):
                widget = self.make_input(reference)
                self.assertEqual("variable", widget.mode_combo.currentData())
                self.assertEqual(reference, widget.value())
                self.assertIs(widget.variable_combo, widget.inputs.currentWidget())
                self.assertEqual("", widget.literal_edit.text())
                self.assertFalse(widget.literal_edit.isEnabled())
                self.assertFalse(widget.create_button.isHidden())

    def test_switching_clears_inactive_value_and_emits_only_active_input(self):
        widget = self.make_input("42")
        changed = Mock()
        widget.valueChanged.connect(changed)
        widget.mode_combo.setCurrentIndex(1)
        self.assertEqual("", widget.literal_edit.text())
        self.assertEqual("", widget.value())
        widget.variable_combo.setCurrentIndex(widget.variable_combo.findData("$Pet Skill"))
        self.assertEqual("$Pet Skill", widget.value())
        changed.assert_called_with("$Pet Skill")
        widget.literal_edit.setText("inactive")
        changed.assert_called_with("$Pet Skill")
        widget.mode_combo.setCurrentIndex(0)
        self.assertEqual("", widget.variable_combo.currentText())
        self.assertEqual("", widget.value())
        widget.literal_edit.setText("0")
        changed.assert_called_with("0")
        widget.variable_combo.setCurrentText("$Cooldown")
        self.assertEqual("0", widget.value())
        changed.assert_called_with("0")

    def test_typed_reference_uses_existing_runtime_prefix_and_search_selection(self):
        widget = self.make_input("")
        changed = Mock()
        widget.valueChanged.connect(changed)
        widget.literal_edit.setText("$Pet Skill")
        self.assertEqual("variable", widget.mode_combo.currentData())
        self.assertEqual("", widget.literal_edit.text())
        changed.assert_called_with("$Pet Skill")
        widget.variable_combo._activate_completion("$Cooldown")
        changed.assert_called_with("$Cooldown")
        widget.variable_combo.setEditText("Pet Skill")
        changed.assert_called_with("$Pet Skill")

    def test_created_variable_becomes_single_selected_reference(self):
        widget = self.make_input("42")
        changed = Mock()
        widget.valueChanged.connect(changed)
        widget.select_variable("New variable")
        self.assertEqual("$New variable", widget.value())
        self.assertEqual("", widget.literal_edit.text())
        changed.assert_called_once_with("$New variable")

    def test_disabled_widget_does_not_expose_editable_input(self):
        for value in ("0", "$Pet Skill"):
            widget = self.make_input(value)
            widget.setEnabled(False)
            self.assertFalse(widget.mode_combo.isEnabled())
            self.assertFalse(widget.literal_edit.isEnabled())
            self.assertFalse(widget.variable_combo.isEnabled())
            self.assertFalse(widget.create_button.isEnabled())

    def editor_harness(self):
        parent = QWidget()
        self.addCleanup(parent.close)
        self.addCleanup(parent.deleteLater)
        editor = SimpleNamespace(
            param_layout=QFormLayout(parent),
            is_editable=lambda: True,
            _collect_available_variables=lambda: ["$Pet Skill", "$Cooldown"],
            _parameter_label=lambda key: key,
            _open_variable_dialog=lambda: "Created",
            current_function=None,
            logger=Mock(),
        )
        for name in ("_make_value_input", "_add_variable_choice_row", "_add_search_ocr_query_row"):
            setattr(editor, name, getattr(FunctionEditorView, name).__get__(editor))
        return editor

    def test_all_existing_variable_capable_parameters_use_exclusive_input(self):
        keys = {
            "until_variable", "delay_variable", "variable", "until_var", "query",
            "expression", "left", "right", "value", "input", "default_value",
        }
        count = 0
        for block_type, definition in BLOCK_CATALOG.items():
            for key, default in definition.get("params", {}).items():
                if not (
                    key in keys
                    or (block_type == BLOCK_TYPE_TEXT and key == "text")
                    or (block_type == BLOCK_REPEAT and key == "max_iterations")
                ):
                    continue
                if block_type == BLOCK_VARIABLE and key == "variable":
                    continue
                with self.subTest(block_type=block_type, key=key):
                    editor = self.editor_harness()
                    block = FunctionBlock("block", block_type, params={key: 0})
                    FunctionEditorView._add_parameter_row(editor, block, key, default)
                    if block_type in (BLOCK_SEARCH_OCR, BLOCK_SEARCH_OCR_TAP) and key == "query":
                        widget = editor.ocr_value_input
                    else:
                        widget = editor.parameter_value_inputs[("block", key)]
                    self.assertEqual("0", widget.value())
                    self.assertEqual(0, block.params[key])
                    widget.select_variable("Pet Skill")
                    self.assertEqual("$Pet Skill", block.params[key])
                    saved = json.loads(json.dumps(block.to_dict()))
                    restored = FunctionBlock.from_dict(saved)
                    self.assertEqual("$Pet Skill", restored.params[key])
                    widget.mode_combo.setCurrentIndex(0)
                    widget.literal_edit.setText("17")
                    self.assertEqual("17", block.params[key])
                    count += 1
        self.assertGreaterEqual(count, 17)

    def test_helper_arguments_use_same_selector_and_nested_serialization(self):
        editor = self.editor_harness()
        editor.game_combo = Mock()
        editor.game_combo.currentData.return_value = "game"
        helper = SimpleNamespace(
            function_id="helper", name="Helper", description="",
            interface=SimpleNamespace(inputs=["duration", "enabled"]),
        )
        editor.function_service = Mock()
        editor.function_service.list_functions.return_value = [helper]
        editor.function_service.load.return_value = helper
        block = FunctionBlock(
            "call", "call_function",
            params={"function_id": "helper", "inputs": {"duration": "$Pet Skill", "enabled": 0}},
        )
        FunctionEditorView._add_function_choice_row(editor, block)
        duration = editor.param_layout.itemAt(1, QFormLayout.FieldRole).widget()
        enabled = editor.param_layout.itemAt(2, QFormLayout.FieldRole).widget()
        self.assertIsInstance(duration, VariableValueInput)
        self.assertEqual("$Pet Skill", duration.value())
        self.assertEqual("0", enabled.value())
        enabled.select_variable("Cooldown")
        restored = FunctionBlock.from_dict(json.loads(json.dumps(block.to_dict())))
        self.assertEqual(
            {"duration": "$Pet Skill", "enabled": "$Cooldown"},
            restored.params["inputs"],
        )

    def test_variable_creation_from_editor_updates_parameter(self):
        editor = self.editor_harness()
        block = FunctionBlock("block", "duration_parse", params={"input": "42"})
        editor._add_variable_choice_row(block, "input", "")
        widget = editor.parameter_value_inputs[("block", "input")]
        widget.createVariableRequested.emit()
        self.assertEqual("$Created", block.params["input"])
        self.assertEqual("", widget.literal_edit.text())

    def test_variable_block_target_is_not_replaced_by_a_value_selector(self):
        editor = self.editor_harness()
        block = FunctionBlock("block", BLOCK_VARIABLE, params={"variable": "Pet Skill"})
        editor._add_variable_choice_row(block, "variable", "")
        self.assertFalse(hasattr(editor, "parameter_value_inputs"))
        self.assertEqual("Pet Skill", block.params["variable"])

    def test_selected_reference_and_literal_resolve_through_unchanged_runner(self):
        widget = self.make_input("17")
        context = ExecutionContext(profile_id="p", function_id="f")
        context.set("Pet Skill", 23, local=True)
        runner = FunctionRunner(logger=Mock())
        self.assertEqual("17", runner._resolve_params({"value": widget.value()}, context)["value"])
        widget.select_variable("Pet Skill")
        self.assertEqual(23, runner._resolve_params({"value": widget.value()}, context)["value"])


if __name__ == "__main__":
    print("TEST START: exclusive variable inputs", flush=True)
    result = unittest.main(exit=False, verbosity=2)
    success = result.result.wasSuccessful()
    print("TEST RESULT: SUCCESS" if success else "TEST RESULT: FAILURE", flush=True)
    raise SystemExit(0 if success else 1)
