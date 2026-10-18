"""Shared widgets to create one event for several profiles of a game."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ui.i18n.translator import t


class ProfileChecklist(QWidget):
    """Checkable profile list with an "all profiles" shortcut."""

    def __init__(self, profiles, checked_ids=(), parent=None):
        super().__init__(parent)
        checked = {str(pid) for pid in checked_ids if pid}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.all_checkbox = QCheckBox(t("scheduler.all_profiles_of_game"))
        self.list_widget = QListWidget()
        self.list_widget.setMaximumHeight(140)
        for label, profile_id in profiles:
            if not profile_id:
                continue
            item = QListWidgetItem(str(label or profile_id))
            item.setData(Qt.UserRole, str(profile_id))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if str(profile_id) in checked else Qt.Unchecked)
            self.list_widget.addItem(item)
        layout.addWidget(self.all_checkbox)
        layout.addWidget(self.list_widget)
        self.all_checkbox.toggled.connect(self._set_all)
        self.list_widget.itemChanged.connect(self._sync_all_checkbox)
        self._sync_all_checkbox()

    def _items(self):
        return [self.list_widget.item(index) for index in range(self.list_widget.count())]

    def _set_all(self, checked):
        self.list_widget.blockSignals(True)
        for item in self._items():
            item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        self.list_widget.blockSignals(False)
        self.list_widget.viewport().update()

    def _sync_all_checkbox(self, *_args):
        items = self._items()
        self.all_checkbox.blockSignals(True)
        self.all_checkbox.setChecked(bool(items) and all(
            item.checkState() == Qt.Checked for item in items
        ))
        self.all_checkbox.blockSignals(False)

    def set_checked(self, profile_ids):
        wanted = {str(pid) for pid in profile_ids if pid}
        self.list_widget.blockSignals(True)
        for item in self._items():
            item.setCheckState(Qt.Checked if item.data(Qt.UserRole) in wanted else Qt.Unchecked)
        self.list_widget.blockSignals(False)
        self._sync_all_checkbox()

    def checked_ids(self) -> list[str]:
        return [
            item.data(Qt.UserRole)
            for item in self._items()
            if item.checkState() == Qt.Checked
        ]


class EventCreateDialog(QDialog):
    """Create an event (optionally with the current function as task) for
    several profiles of one game. Used from the function editor so a new
    function can use the event variable right away."""

    def __init__(self, profiles, checked_ids=(), function_name="", parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("function.create_event"))
        self.resize(400, 420)
        layout = QVBoxLayout(self)
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText(t("scheduler.event_name"))
        self.profiles = ProfileChecklist(profiles, checked_ids)
        self.priority_spin = QSpinBox()
        self.priority_spin.setRange(1, 100)
        self.priority_spin.setValue(50)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem(t("scheduler.mode_manual_label"), "manual")
        self.mode_combo.addItem(t("scheduler.mode_fixed_label"), "fixed")
        self.interval_input = QLineEdit("300")
        self.add_function_checkbox = QCheckBox(
            t("function.create_event_add_task").format(name=function_name)
        )
        self._has_function = bool(function_name)
        self.add_function_checkbox.setVisible(self._has_function)
        self.add_function_checkbox.setChecked(bool(function_name))
        self.task_name_input = QLineEdit()
        self.task_name_input.setPlaceholderText(t("scheduler.task_name_optional"))
        self.task_name_input.setVisible(bool(function_name))
        self.add_function_checkbox.toggled.connect(self.task_name_input.setEnabled)
        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #E57373;")
        self.error_label.setWordWrap(True)

        layout.addWidget(QLabel(t("scheduler.event_name")))
        layout.addWidget(self.name_input)
        layout.addWidget(QLabel(t("scheduler.profiles")))
        layout.addWidget(self.profiles)
        layout.addWidget(QLabel(t("scheduler.priority")))
        layout.addWidget(self.priority_spin)
        layout.addWidget(QLabel(t("scheduler.schedule_mode")))
        layout.addWidget(self.mode_combo)
        layout.addWidget(QLabel(t("scheduler.interval_seconds")))
        layout.addWidget(self.interval_input)
        layout.addWidget(self.add_function_checkbox)
        layout.addWidget(self.task_name_input)
        layout.addWidget(self.error_label)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept_if_valid(self):
        if not self.name_input.text().strip():
            self.error_label.setText(t("scheduler.event_name_required"))
            return
        if not self.profiles.checked_ids():
            self.error_label.setText(t("scheduler.select_profiles_required"))
            return
        self.accept()

    def values(self) -> dict:
        mode = self.mode_combo.currentData() or "manual"
        try:
            interval = int(self.interval_input.text().strip() or "0")
        except ValueError:
            interval = 0
        return {
            "name": self.name_input.text().strip(),
            "profile_ids": self.profiles.checked_ids(),
            "priority": self.priority_spin.value(),
            "schedule_mode": mode,
            "interval_seconds": interval if mode == "fixed" else 0,
            "add_function": self._has_function and self.add_function_checkbox.isChecked(),
            "task_name": self.task_name_input.text().strip(),
        }
