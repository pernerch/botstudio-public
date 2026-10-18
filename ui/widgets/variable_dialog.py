from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from services.variable_service import (
    VALUE_TYPE_BOOLEAN,
    VALUE_TYPE_COLLECTION,
    VALUE_TYPE_DATETIME,
    VALUE_TYPE_DURATION,
    VALUE_TYPE_NUMBER,
    VALUE_TYPE_TEXT,
    VARIABLE_KIND_CONFIG,
    VARIABLE_KIND_LIVE,
    VARIABLE_KIND_PROFILE,
    VariableNameConflictError,
    VariableValueError,
)
from ui.i18n.translator import t


class VariableCreateDialog(QDialog):
    """Create a uniquely named profile variable.

    Existing local/global prefixes remain an execution compatibility detail;
    new variables never expose that technical choice in the UI.
    """

    def __init__(
        self,
        game_id,
        variable_service,
        parent=None,
        edit_name=None,
        field_target=None,
    ):
        super().__init__(parent)

        self.game_id = game_id
        self.variable_service = variable_service
        self.edit_name = edit_name
        self.field_target = field_target
        self.result_value = ""

        self.setWindowTitle(
            t("variables.edit_title") if edit_name else t("variables.create_title")
        )

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.name_edit = QLineEdit()
        form.addRow(t("variables.name"), self.name_edit)

        self.description_edit = QLineEdit()
        form.addRow(t("variables.description"), self.description_edit)

        self.kind_combo = QComboBox()
        for kind, label_key in (
            (VARIABLE_KIND_CONFIG, "variables.kind_config"),
            (VARIABLE_KIND_PROFILE, "variables.kind_profile"),
            (VARIABLE_KIND_LIVE, "variables.kind_live"),
        ):
            self.kind_combo.addItem(t(label_key), kind)
        form.addRow(t("variables.kind"), self.kind_combo)

        self.kind_description = QLabel()
        self.kind_description.setObjectName("variableKindDescription")
        self.kind_description.setWordWrap(True)
        form.addRow(self.kind_description)

        self.scope_description = QLabel()
        self.scope_description.setObjectName("variableScopeDescription")
        self.scope_description.setWordWrap(True)
        form.addRow(self.scope_description)

        self.type_combo = QComboBox()
        for value_type, label_key in (
            (VALUE_TYPE_TEXT, "variables.type_text"),
            (VALUE_TYPE_NUMBER, "variables.type_number"),
            (VALUE_TYPE_BOOLEAN, "variables.type_boolean"),
            (VALUE_TYPE_DATETIME, "variables.type_datetime"),
            (VALUE_TYPE_DURATION, "variables.type_duration"),
            (VALUE_TYPE_COLLECTION, "variables.type_collection"),
        ):
            self.type_combo.addItem(t(label_key), value_type)
        form.addRow(t("variables.value_type"), self.type_combo)

        self.initial_value_edit = QLineEdit()
        form.addRow(t("variables.initial_value"), self.initial_value_edit)

        self.function_writable_check = QCheckBox(
            t("variables.function_writable")
        )
        form.addRow(self.function_writable_check)

        self.debug_visible_check = QCheckBox(t("variables.debug_visible"))
        form.addRow(self.debug_visible_check)

        self.kind_combo.currentIndexChanged.connect(
            self._sync_write_default
        )
        self.kind_combo.currentIndexChanged.connect(
            self._sync_descriptions
        )
        self._sync_write_default()
        self._sync_descriptions()

        layout.addLayout(form)

        if edit_name:
            self._load_existing(edit_name)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #f87171;")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self):

        name = self.name_edit.text().strip()

        if not name:
            self.error_label.setText(t("variables.error_name_required"))
            return

        try:
            if self.edit_name:
                self.variable_service.update_global(
                    self.game_id,
                    self.edit_name,
                    description=self.description_edit.text().strip(),
                    initial_value=self.initial_value_edit.text(),
                    debug_visible=self.debug_visible_check.isChecked(),
                    kind=self.kind_combo.currentData(),
                    value_type=self.type_combo.currentData(),
                    writable_by_functions=self.function_writable_check.isChecked(),
                )
                self.result_value = self.edit_name
            else:
                self.variable_service.create_variable(
                    self.game_id,
                    name,
                    description=self.description_edit.text().strip(),
                    initial_value=self.initial_value_edit.text(),
                    debug_visible=self.debug_visible_check.isChecked(),
                    kind=self.kind_combo.currentData(),
                    value_type=self.type_combo.currentData(),
                    writable_by_functions=self.function_writable_check.isChecked(),
                )
                self.result_value = name
        except VariableNameConflictError:
            self.error_label.setText(
                t("variables.error_exists").format(name=name)
            )
            return
        except VariableValueError as error:
            self.error_label.setText(str(error))
            return

        self.accept()

    def _load_existing(self, name):
        definition = self.variable_service.get_definition(self.game_id, name)
        if not definition:
            return
        self.name_edit.setText(definition.get("name", name))
        self.name_edit.setReadOnly(True)  # the name is the stable key
        self.description_edit.setText(definition.get("description", ""))
        kind_index = self.kind_combo.findData(definition.get("kind", VARIABLE_KIND_LIVE))
        if kind_index >= 0:
            self.kind_combo.setCurrentIndex(kind_index)
        type_index = self.type_combo.findData(definition.get("value_type", "any"))
        if type_index >= 0:
            self.type_combo.setCurrentIndex(type_index)
        initial = definition.get("initial_value")
        if initial is not None:
            if isinstance(initial, list):
                import json
                self.initial_value_edit.setText(json.dumps(initial, ensure_ascii=False))
            else:
                self.initial_value_edit.setText(str(initial))
        self.function_writable_check.setChecked(
            bool(definition.get("writable_by_functions", True))
        )
        self.debug_visible_check.setChecked(
            bool(definition.get("debug_visible", False))
        )

    def _sync_write_default(self):
        self.function_writable_check.setChecked(
            self.kind_combo.currentData() == VARIABLE_KIND_LIVE
        )

    def _sync_descriptions(self):
        description_keys = {
            VARIABLE_KIND_CONFIG: "variables.kind_config_description",
            VARIABLE_KIND_PROFILE: "variables.kind_profile_description",
            VARIABLE_KIND_LIVE: "variables.kind_live_description",
        }
        self.kind_description.setText(
            t(
                description_keys.get(
                    self.kind_combo.currentData(),
                    "variables.kind_live_description",
                )
            )
        )
        scope_key = {
            "event": "variables.scope_event_description",
            "task": "variables.scope_task_description",
        }.get(
            str(self.field_target or "").casefold(),
            "variables.scope_profile_description",
        )
        self.scope_description.setText(t(scope_key))
