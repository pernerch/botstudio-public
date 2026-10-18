from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QListWidget,
    QVBoxLayout,
    QWidget,
)

from ui.i18n.translator import t


class NavigationView(QWidget):

    VIEW_KEYS = (
        "game",
        "profiles",
        "learning",
        "scheduler",
        "statistics",
        "settings",
        "telegram",
        "functions",
        "dependencies",
        "debug",
        "logs",
    )

    def __init__(
        self,
    ):
        super().__init__()

        layout = QVBoxLayout(
            self
        )

        self.menu = QListWidget()
        self.menu.setDragDropMode(QListWidget.InternalMove)
        self.menu.setDragEnabled(True)
        self.menu.setAcceptDrops(True)
        self.menu.setDropIndicatorShown(True)

        self.menu.setMinimumWidth(
            140
        )

        self.menu.setStyleSheet(
            """
            QListWidget
            {
                border: none;
            }

            QListWidget::item
            {
                padding: 8px;
            }

            QListWidget::item:selected
            {
                background-color: #334155;
                border-left: 4px solid #60a5fa;
                font-weight: bold;
            }
            """
        )


        labels = (
            t("nav.game"),
            t("nav.profiles"),
            t("nav.learning"),
            t("nav.scheduler"),
            "Statistics",
            t("nav.settings"),
            t("nav.telegram"),
            t("nav.functions"),
            t("nav.dependencies"),
            t("nav.debug"),
            t("logs.title"),
        )
        for key, label in zip(self.VIEW_KEYS, labels):
            self.menu.addItem(label)
            self.menu.item(self.menu.count() - 1).setData(Qt.UserRole, key)

        self._settings = QSettings("BotStudio", "BotStudio")
        self._restore_order()
        self.menu.model().rowsMoved.connect(self._save_order)

        layout.addWidget(
            self.menu,
            stretch=1,
        )

    def _restore_order(self):
        stored = self._settings.value("navigation/order", "")
        requested = [key for key in str(stored).split(",") if key]
        ordered = [key for key in requested if key in self.VIEW_KEYS]
        ordered.extend(key for key in self.VIEW_KEYS if key not in ordered)
        items = {
            self.menu.item(index).data(Qt.UserRole): self.menu.takeItem(index)
            for index in range(self.menu.count() - 1, -1, -1)
        }
        for key in ordered:
            self.menu.addItem(items[key])

    def _save_order(self, *_args):
        order = [
            self.menu.item(index).data(Qt.UserRole)
            for index in range(self.menu.count())
        ]
        self._settings.setValue("navigation/order", ",".join(order))

    def select_view(self, key: str):
        for index in range(self.menu.count()):
            if self.menu.item(index).data(Qt.UserRole) == key:
                self.menu.setCurrentRow(index)
                return