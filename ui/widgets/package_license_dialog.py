import html

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)

from services.package_license_service import PACKAGE_LICENSES
from ui.i18n.translator import t


class PackageLicenseDialog(QDialog):
    def __init__(self, selected_spdx_id: str | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("package_license.title"))
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)

        intro = QLabel(t("package_license.intro"))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.license_combo = QComboBox()
        self.license_combo.addItem(t("package_license.select"), "")
        for option in PACKAGE_LICENSES:
            self.license_combo.addItem(
                f"{option.name} ({option.spdx_id})",
                option.spdx_id,
            )
        selected_index = self.license_combo.findData(selected_spdx_id or "")
        self.license_combo.setCurrentIndex(selected_index)
        self.license_combo.currentIndexChanged.connect(self._update_details)
        layout.addWidget(self.license_combo)

        self.description = QLabel()
        self.description.setWordWrap(True)
        self.description.setTextFormat(Qt.RichText)
        self.description.setOpenExternalLinks(True)
        layout.addWidget(self.description)

        warning = QLabel(t("package_license.mixed_warning"))
        warning.setWordWrap(True)
        layout.addWidget(warning)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel,
            parent=self,
        )
        self.buttons.button(QDialogButtonBox.Ok).setText(t("common.save"))
        self.buttons.button(QDialogButtonBox.Cancel).setText(t("common.cancel"))
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._update_details()

    @property
    def selected_spdx_id(self) -> str | None:
        value = self.license_combo.currentData()
        return value if isinstance(value, str) and value else None

    def accept(self):
        if self.selected_spdx_id is not None:
            super().accept()

    def _update_details(self):
        spdx_id = self.selected_spdx_id
        option = next(
            (item for item in PACKAGE_LICENSES if item.spdx_id == spdx_id),
            None,
        )
        if option is None:
            self.description.clear()
            self.buttons.button(QDialogButtonBox.Ok).setEnabled(False)
            return
        summary = html.escape(t(option.description_key))
        label = html.escape(t("package_license.link"))
        url = html.escape(option.url, quote=True)
        self.description.setText(
            f'{summary}<br><a href="{url}">{label}</a>'
        )
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(True)


class LegacyFunctionAttributionDialog(QDialog):
    def __init__(self, functions, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("github.package_attribute_legacy"))
        self.setMinimumSize(560, 420)
        layout = QVBoxLayout(self)
        intro = QLabel(t("package_license.legacy_intro"))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.functions = QListWidget()
        for function in functions:
            item = QListWidgetItem(
                f"{function.name or function.function_id} — {function.game_id} "
                f"(revision {function.revision})",
                self.functions,
            )
            item.setData(Qt.UserRole, (function.game_id, function.function_id))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
        layout.addWidget(self.functions)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel,
            parent=self,
        )
        self.buttons.button(QDialogButtonBox.Ok).setText(t("common.save"))
        self.buttons.button(QDialogButtonBox.Cancel).setText(t("common.cancel"))
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    @property
    def selections(self) -> list[tuple[str, str]]:
        return [
            self.functions.item(index).data(Qt.UserRole)
            for index in range(self.functions.count())
            if self.functions.item(index).checkState() == Qt.Checked
        ]
