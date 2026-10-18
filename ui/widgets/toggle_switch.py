from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QAbstractButton


class ToggleSwitch(QAbstractButton):
    """Compact checkable switch for binary settings."""

    def __init__(self, parent=None, active_color: str = "#4FD42A"):
        super().__init__(parent)
        self._active_color = QColor(active_color)
        self.setCheckable(True)
        self.setFixedSize(42, 22)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setToolTip("")

    def set_active_color(self, color: str):
        """Switch the on-state track color (context color per row type)."""

        self._active_color = QColor(color)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        track = QColor(self._active_color) if self.isChecked() else QColor("#5B6472")
        if not self.isEnabled():
            track = track.lighter(135)
        painter.setPen(Qt.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(self.rect(), 11, 11)
        painter.setBrush(QColor("#FFFFFF"))
        knob_x = self.width() - 20 if self.isChecked() else 2
        painter.drawEllipse(knob_x, 2, 18, 18)

    def hitButton(self, position):
        return self.rect().contains(position)
