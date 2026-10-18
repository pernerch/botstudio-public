from PySide6.QtCore import QCoreApplication, Qt, QTimer

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QWidget,
)

from services.clock_service import ClockService
from services.theme_service import ThemeService


class AppHeaderBar(QWidget):
    """Centered application banner.

    The native window title bar is drawn by the OS and cannot center its
    text, so the centered variant lives inside the window.
    """

    REFRESH_MS = 1000

    _debug_active = False

    @classmethod
    def set_debug_active(cls, active: bool):
        """Global debug banner switch; the running instance picks it up."""

        cls._debug_active = bool(active)

    def __init__(
        self,
        clock_service=None,
        parent=None,
    ):
        super().__init__(parent)
        self._github_login = ""

        self.clock_service = (
            clock_service
            or ClockService()
        )

        layout = QHBoxLayout(
            self
        )

        layout.setContentsMargins(
            8,
            2,
            8,
            2,
        )

        self.label = QLabel("-")
        self.label.setTextFormat(Qt.PlainText)

        self.label.setAlignment(
            Qt.AlignCenter
        )

        font_px = (
            ThemeService()
            .get_font_size_px()
        )

        self.label.setStyleSheet(
            f"font-weight: bold; font-size: {font_px}px;"
        )

        layout.addWidget(
            self.label,
            stretch=1,
        )

        self.timer = QTimer(
            self
        )

        self.timer.setInterval(
            self.REFRESH_MS
        )

        self.timer.timeout.connect(
            self.refresh
        )

        self.timer.start()

        self.refresh()

    def build_text(
        self,
    ) -> str:

        app = QCoreApplication.instance()

        name = (
            app.applicationName()
            if app
            else ""
        )

        base = (
            f"{name} "
            f"| {self.clock_service.format_pair()}"
        )
        if self._github_login:
            base += f" | GitHub: @{self._github_login}"
        if self._debug_active:
            return f"DEBUG MODE | {base}"
        return base

    def set_github_account(self, account):
        self._github_login = account.login if account is not None else ""
        self.refresh()

    def refresh(
        self,
    ):

        if self._debug_active:
            self.setStyleSheet(
                "background-color: #B91C1C;"
            )
            self.label.setStyleSheet(
                f"font-weight: bold; font-size: {ThemeService().get_font_size_px()}px;"
                " color: #FFFFFF;"
            )
        else:
            self.setStyleSheet("")
            self.label.setStyleSheet(
                f"font-weight: bold; font-size: {ThemeService().get_font_size_px()}px;"
            )

        self.label.setText(
            self.build_text()
        )
