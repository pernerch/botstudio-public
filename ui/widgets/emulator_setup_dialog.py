from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)

from services.emulator_setup_service import EmulatorSetupError, EmulatorSetupService
from ui.i18n.translator import t


class _SetupSignals(QObject):
    finished = Signal(object)


class _SetupWorker(QRunnable):
    def __init__(self, service, installation, operation):
        super().__init__()
        self.service = service
        self.installation = installation
        self.operation = operation
        self.signals = _SetupSignals()

    def run(self):
        try:
            self.service.ensure_tools(self.installation)
            value = self.operation() if self.operation else ""
            if self.operation and not value:
                raise EmulatorSetupError("package_missing")
            self.signals.finished.emit(value)
        except Exception as error:
            self.service.logger.error("MuMu Setup Failed", error=str(error))
            self.signals.finished.emit(error)


class EmulatorSetupDialog(QDialog):
    """Shared asynchronous tool setup before Runtime start or package detection."""

    def __init__(
        self,
        manager,
        parent=None,
        operation: Callable[[], str | None] | None = None,
    ):
        super().__init__(parent)
        self.service = EmulatorSetupService(manager)
        self.operation = operation
        self.installation = ""
        self.package_name = ""
        self._worker = None
        self.setWindowTitle(t("emulator_setup.title"))
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.choose_button = QPushButton(t("emulator_setup.choose_folder"))
        self.retry_button = QPushButton(t("emulator_setup.retry"))
        self.cancel_button = QPushButton(t("common.cancel"))
        for button in (self.choose_button, self.retry_button, self.cancel_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.choose_button.clicked.connect(self._choose_folder)
        self.retry_button.clicked.connect(self._start)
        self.cancel_button.clicked.connect(self.reject)
        QTimer.singleShot(0, self._start)

    def _choose_folder(self):
        path = QFileDialog.getExistingDirectory(
            self, t("emulator_setup.choose_folder"), self.installation,
        )
        if path:
            self.installation = path
            self._start()

    def _start(self):
        if self._worker is not None:
            return
        self.status.setText(t("emulator_setup.searching"))
        self._worker = _SetupWorker(self.service, self.installation, self.operation)
        self._worker.signals.finished.connect(
            self._finished, Qt.ConnectionType.QueuedConnection,
        )
        self._set_buttons_enabled(False)
        QThreadPool.globalInstance().start(self._worker)

    def _finished(self, result):
        self._worker = None
        self._set_buttons_enabled(True)
        if isinstance(result, Exception):
            code = result.code if isinstance(result, EmulatorSetupError) else "failed"
            message = t(f"emulator_setup.{code}")
            detail = result.detail if isinstance(result, EmulatorSetupError) else str(result)
            self.status.setText(f"{message}\n{detail}".strip())
            return
        self.package_name = result
        self.accept()

    def _set_buttons_enabled(self, enabled):
        for button in (self.choose_button, self.retry_button, self.cancel_button):
            button.setEnabled(enabled)

    def reject(self):
        if self._worker is None:
            super().reject()
