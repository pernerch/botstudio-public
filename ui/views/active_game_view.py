from PySide6.QtWidgets import (
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


class ActiveGameView(QWidget):

    def __init__(
        self,
        game_id: str,
    ):
        super().__init__()

        self.game_id = game_id

        layout = QVBoxLayout(
            self
        )

        self.tabs = QTabWidget()

        layout.addWidget(
            self.tabs,
            stretch=1,
        )

    def set_tab(
        self,
        title: str,
        widget: QWidget,
    ):

        self.tabs.addTab(
            widget,
            title,
        )