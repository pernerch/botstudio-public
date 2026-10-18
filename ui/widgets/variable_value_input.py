from collections.abc import Iterable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QWidget,
)

from ui.i18n.translator import t
from ui.widgets.searchable_combo import SearchableComboBox


class VariableValueInput(QWidget):
    """One serialized value, selected as either a literal or a variable reference."""

    valueChanged = Signal(str)
    createVariableRequested = Signal()

    def __init__(self, value: object, variables: Iterable[str], parent=None):
        super().__init__(parent)
        self.mode_combo = QComboBox()
        self.mode_combo.setObjectName("valueInputMode")
        self.mode_combo.addItem(t("function.value_mode_literal"), "literal")
        self.mode_combo.addItem(t("function.value_mode_variable"), "variable")

        self.literal_edit = QLineEdit()
        self.literal_edit.setObjectName("literalValueInput")
        self.variable_combo = SearchableComboBox()
        self.variable_combo.setObjectName("variableValueInput")
        self.variable_combo.addItem("", "")
        self.variable_combo.lineEdit().setPlaceholderText(t("function.insert_variable"))
        for variable in variables:
            reference = "$" + variable.lstrip("$")
            if self.variable_combo.findData(reference) < 0:
                self.variable_combo.addItem(reference, reference)

        self.inputs = QStackedWidget()
        self.inputs.addWidget(self.literal_edit)
        self.inputs.addWidget(self.variable_combo)
        self.create_button = QPushButton(t("variables.create"))
        self.create_button.clicked.connect(self.createVariableRequested.emit)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.mode_combo)
        layout.addWidget(self.inputs, stretch=1)
        layout.addWidget(self.create_button)

        self._updating = False
        self.set_value(value)
        self.mode_combo.currentIndexChanged.connect(self._change_mode)
        self.literal_edit.textChanged.connect(self._literal_changed)
        self.variable_combo.currentTextChanged.connect(self._variable_changed)

    def value(self) -> str:
        if self.mode_combo.currentData() == "variable":
            name = self.variable_combo.currentText().strip().lstrip("$")
            return f"${name}" if name else ""
        return self.literal_edit.text()

    def set_value(self, value: object) -> None:
        text = "" if value is None else str(value)
        variable = text.startswith("$")
        self._updating = True
        try:
            self.mode_combo.setCurrentIndex(1 if variable else 0)
            self.literal_edit.setText("" if variable else text)
            if variable and self.variable_combo.findData(text) < 0:
                self.variable_combo.addItem(text, text)
            self.variable_combo.setCurrentIndex(
                self.variable_combo.findData(text) if variable else 0
            )
            self.variable_combo.setEditText(text if variable else "")
            self._sync_mode()
        finally:
            self._updating = False

    def select_variable(self, name: str) -> None:
        self.set_value("$" + name.lstrip("$"))
        self.valueChanged.emit(self.value())

    def _sync_mode(self) -> None:
        variable = self.mode_combo.currentData() == "variable"
        self.inputs.setCurrentIndex(1 if variable else 0)
        self.literal_edit.setEnabled(not variable)
        self.variable_combo.setEnabled(variable)
        self.create_button.setVisible(variable)

    def _change_mode(self, _index: int) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            self.literal_edit.clear()
            self.variable_combo.setCurrentIndex(0)
            self.variable_combo.setEditText("")
            self._sync_mode()
        finally:
            self._updating = False
        self.valueChanged.emit(self.value())

    def _literal_changed(self, text: str) -> None:
        if self._updating or self.mode_combo.currentData() != "literal":
            return
        # The existing runtime reserves a leading $ for variable references.
        if text.startswith("$"):
            self.set_value(text)
            self.variable_combo.setFocus()
        self.valueChanged.emit(self.value())

    def _variable_changed(self, _text: str) -> None:
        if not self._updating and self.mode_combo.currentData() == "variable":
            self.valueChanged.emit(self.value())
