from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
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
from services.game_registry_service import GameRegistryService
from services.logging_service import get_logger
from services.reinitialize_service import get_default_reinitialize_service
from ui.i18n.translator import t
from ui.widgets.emulator_setup_dialog import EmulatorSetupDialog


class _GameSignals(QObject):
    finished = Signal(object)


class _GameTask(QRunnable):
    def __init__(self, operation):
        super().__init__()
        self.operation = operation
        self.signals = _GameSignals()

    def run(self):
        try:
            result = self.operation()
        except Exception as error:
            get_logger("GameSetup").error("Game Directory Failed", error=str(error))
            result = error
        self.signals.finished.emit(result)


class GameSetupView(QWidget):

    def __init__(
        self,
        parent=None,
    ):
        super().__init__(parent)

        self.game_service = GameService()
        self.registry = GameRegistryService()
        self._worker = None
        self._directory_requested = False
        self._selected_local_game_id = ""
        self.registered_games = []

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
        self.game_id.setReadOnly(True)
        self.catalog_games = QComboBox()
        self.catalog_games.setEditable(True)
        self.catalog_games.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.catalog_games.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.catalog_games.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.btn_reload = QPushButton(t("game_setup.load_directory"))
        self.display_name = QLineEdit()
        self.package_name = QLineEdit()
        self.emulator_instance = QSpinBox()

        self.emulator_instance.setRange(
            0,
            99,
        )

        form_layout.addRow(
            t("game_setup.directory"),
            self.catalog_games,
        )
        form_layout.addRow(self.btn_reload)

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
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)

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
        self.btn_reload.clicked.connect(self.load_directory)
        self.catalog_games.currentIndexChanged.connect(self._catalog_selected)
        self.table.cellDoubleClicked.connect(self._local_selected)

        self.refresh_games()
    def showEvent(self, event):
        super().showEvent(event)
        if not self._directory_requested:
            self.load_directory()

    def _run(self, operation, completion):
        if self._worker is not None:
            return
        self._completion = completion
        self._worker = _GameTask(operation)
        self._worker.signals.finished.connect(
            self._finished, Qt.ConnectionType.QueuedConnection,
        )
        for widget in (self.btn_reload, self.btn_save, self.btn_detect, self.catalog_games):
            widget.setEnabled(False)
        self.status.setText(t("game_setup.directory_busy"))
        QThreadPool.globalInstance().start(self._worker)

    def _finished(self, result):
        self._worker = None
        for widget in (self.btn_reload, self.btn_save, self.btn_detect, self.catalog_games):
            widget.setEnabled(True)
        if isinstance(result, Exception):
            self.status.setText(t("game_setup.directory_failed").format(error=str(result)))
            return
        self._completion(result)

    def load_directory(self):
        self._directory_requested = True
        def load():
            entries = self.registry.load()
            self.registry.adopt_existing(self.game_service.file_name, entries)
            return entries

        self._run(load, self._directory_loaded)

    def _directory_loaded(self, entries):
        self.registered_games = entries
        selected = self.catalog_games.currentData()
        self.catalog_games.blockSignals(True)
        self.catalog_games.clear()
        self.catalog_games.addItem(t("game_setup.register_new"), "")
        for entry in sorted(entries, key=lambda game: game["name"].casefold()):
            self.catalog_games.addItem(entry["name"], entry["id"])
        index = self.catalog_games.findData(selected)
        self.catalog_games.setCurrentIndex(max(0, index))
        self.catalog_games.blockSignals(False)
        self._catalog_selected()
        self.refresh_games()
        self.status.setText(t("game_setup.directory_ready"))

    def _catalog_selected(self, *_):
        identity = self.catalog_games.currentData()
        entry = next((game for game in self.registered_games if game["id"] == identity), None)
        self.game_id.setText(identity or "")
        self.display_name.setReadOnly(entry is not None)
        if entry:
            self.display_name.setText(entry["name"])
            local = next((
                game for game in self.game_service.load()
                if (game.get("catalog_game_id") or game["id"]) == identity
            ), None)
            package = local.get("package_name", "") if local else ""
            self.package_name.setText(package or next(iter(entry["android_packages"]), ""))
        else:
            self.display_name.clear()
            self.package_name.clear()

    def _local_selected(self, row, _column):
        game = self.game_service.load()[row]
        self._selected_local_game_id = game["id"]
        index = self.catalog_games.findData(game.get("catalog_game_id") or game["id"])
        self.catalog_games.setCurrentIndex(max(0, index))
        self.display_name.setText(game["display_name"])
        self.package_name.setText(game.get("package_name", ""))
        self.status.setText(t("game_setup.bind_local").format(name=game["display_name"]))

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

        display_name = self.display_name.text().strip()
        package_name = self.package_name.text().strip()
        identity = self.catalog_games.currentData()
        local_game_id = self._selected_local_game_id

        if not display_name or not package_name:

            self.status.setText(
                t("game_setup.missing_fields")
            )

            return

        if identity and self.catalog_games.currentText() != self.catalog_games.itemText(
            self.catalog_games.currentIndex(),
        ):
            self.status.setText(t("game_setup.choose_game"))
            return

        def save():
            entry = (
                self.registry.require(identity) if identity
                else self.registry.register(display_name, package_name)
            )
            local = self.registry.ensure_local(
                self.game_service.file_name, entry, package_name, local_game_id=local_game_id,
            )
            self.game_service.update_package(local["id"], package_name)
            return entry

        self._run(save, self._game_saved)

    def _game_saved(self, entry):
        self._selected_local_game_id = ""
        if not any(game["id"] == entry["id"] for game in self.registered_games):
            self.registered_games.append(entry)
        self._directory_loaded(self.registered_games)
        self.catalog_games.setCurrentIndex(self.catalog_games.findData(entry["id"]))
        self.status.setText(t("game_setup.saved"))
        get_default_reinitialize_service().publish("game")

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
