from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from services.game_service import GameService
from ui.i18n.translator import t
from ui.widgets.emulator_setup_dialog import EmulatorSetupDialog


class GameSetupView(QWidget):

    def __init__(
        self,
        parent=None,
    ):
        super().__init__(parent)

        self.game_service = GameService()

        layout = QVBoxLayout(
            self
        )

        form_group = QGroupBox(
            t("game_setup.title")
        )

        form_layout = QFormLayout(
            form_group
        )

        self.game_id = QLineEdit()
        self.display_name = QLineEdit()
        self.package_name = QLineEdit()
        self.emulator_instance = QSpinBox()

        self.emulator_instance.setRange(
            0,
            99,
        )

        form_layout.addRow(
            t("game_setup.game_id"),
            self.game_id,
        )

        form_layout.addRow(
            t("game_setup.display_name"),
            self.display_name,
        )

        form_layout.addRow(
            t("game_setup.package_name"),
            self.package_name,
        )

        form_layout.addRow(
            t("game_setup.emulator_instance"),
            self.emulator_instance,
        )

        button_layout = QHBoxLayout()

        self.btn_detect = QPushButton(
            t("game_setup.detect_package")
        )

        self.btn_save = QPushButton(
            t("common.save")
        )

        button_layout.addWidget(
            self.btn_detect
        )

        button_layout.addWidget(
            self.btn_save
        )

        form_layout.addRow(
            button_layout
        )

        self.status = QLabel(
            t("game_setup.open_game_hint")
        )

        layout.addWidget(
            form_group
        )

        layout.addWidget(
            self.status
        )

        self.table = QTableWidget(
            0,
            3,
        )

        self.table.setHorizontalHeaderLabels(
            [
                t("game_setup.game_id"),
                t("game_setup.display_name"),
                t("game_setup.package_name"),
            ]
        )

        layout.addWidget(
            self.table,
            stretch=1,
        )

        self.btn_detect.clicked.connect(
            self.detect_package
        )

        self.btn_save.clicked.connect(
            self.save_game
        )

        self.refresh_games()

    def detect_package(
        self,
    ):

        instance = self.emulator_instance.value()
        dialog = EmulatorSetupDialog(
            self.game_service.emulator_manager,
            self,
            operation=lambda: self.game_service.detect_package(instance),
        )
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                self.status.setText(t("game_setup.package_not_detected"))
                return
            self.package_name.setText(dialog.package_name)
            self.status.setText(
                t("game_setup.package_detected")
            )
        finally:
            dialog.deleteLater()

    def save_game(
        self,
    ):

        game_id = self.game_id.text().strip()
        display_name = self.display_name.text().strip()
        package_name = self.package_name.text().strip()

        if not game_id or not display_name or not package_name:

            self.status.setText(
                t("game_setup.missing_fields")
            )

            return

        self.game_service.create_game(
            game_id,
            display_name,
        )

        self.game_service.update_package(
            game_id,
            package_name,
        )

        self.status.setText(
            t("game_setup.saved")
        )

        self.refresh_games()

    def refresh_games(
        self,
    ):

        games = self.game_service.load()

        self.table.setRowCount(
            len(games)
        )

        for row, game in enumerate(games):

            self.table.setItem(
                row,
                0,
                QTableWidgetItem(
                    game.get("id", "-")
                ),
            )

            self.table.setItem(
                row,
                1,
                QTableWidgetItem(
                    game.get("display_name", "-")
                ),
            )

            self.table.setItem(
                row,
                2,
                QTableWidgetItem(
                    game.get("package_name", "-")
                ),
            )
