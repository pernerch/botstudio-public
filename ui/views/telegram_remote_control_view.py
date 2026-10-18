import os
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from services.app_config_service import AppConfigService
from services.application_paths import ApplicationPaths
from services.logging_service import get_logger
from services.telegram_service import TelegramService
from services.telegram_settings_service import TelegramSettingsService
from services.watchdog_autostart_service import WatchdogAutoStartService
from ui.i18n.translator import t
from ui.widgets.github_account_widget import GitHubAccountWidget


class _TelegramTestSignals(QObject):
    completed = Signal(bool, str)


class _TelegramTestOperation(QRunnable):
    def __init__(self, token):
        super().__init__()
        self.token = token
        self.signals = _TelegramTestSignals()

    def run(self):
        try:
            username = TelegramService.probe_connection(self.token)
        except Exception as ex:
            self.signals.completed.emit(False, str(ex))
            return
        self.signals.completed.emit(
            True,
            t("settings.telegram_connected").format(username=username),
        )


class _WatchdogStartOperation(QRunnable):
    def __init__(self, config_service, startup_service, root, studio_pid, directory):
        super().__init__()
        self.config_service = config_service
        self.startup_service = startup_service
        self.root = root
        self.studio_pid = studio_pid
        self.directory = directory
        self.signals = _TelegramTestSignals()

    def run(self):
        try:
            settings = self.config_service.get_studio_settings()
            if settings["telegram_enabled"] or settings["watchdog_auto_start"]:
                self.startup_service.ensure_running(self.root, self.studio_pid, self.directory)
        except Exception as ex:
            self.signals.completed.emit(False, str(ex))
            return
        self.signals.completed.emit(True, "")


class TelegramRemoteControlView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.app_config_service = AppConfigService()
        self.telegram_settings_service = TelegramSettingsService(
            config_service=self.app_config_service
        )
        self.watchdog_autostart_service = WatchdogAutoStartService()
        self._telegram_test_operation = None
        self._watchdog_start_operation = None
        self.logger = get_logger("settings")

        root_layout = QVBoxLayout(self)
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        content = QWidget()
        content_layout = QVBoxLayout(content)

        group = QGroupBox(t("settings.telegram_title"))
        group.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self._telegram_form = QFormLayout(group)
        form = self._telegram_form
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )

        self.telegram_enabled_check = QCheckBox(
            t("settings.telegram_enable")
        )
        form.addRow(self.telegram_enabled_check)

        description = QLabel(t("settings.telegram_description"))
        description.setWordWrap(True)
        form.addRow(description)

        self.telegram_token_edit = QLineEdit()
        self.telegram_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.telegram_token_edit.setPlaceholderText(
            t("settings.telegram_token_placeholder")
        )
        self.telegram_show_token_check = QCheckBox(
            t("settings.telegram_show_token")
        )
        self.telegram_show_token_check.toggled.connect(
            lambda visible: self.telegram_token_edit.setEchoMode(
                QLineEdit.EchoMode.Normal
                if visible
                else QLineEdit.EchoMode.Password
            )
        )
        token_row = QVBoxLayout()
        token_row.addWidget(self.telegram_token_edit)
        token_row.addWidget(self.telegram_show_token_check)
        form.addRow(t("settings.telegram_token"), token_row)

        self.telegram_user_ids_edit = QLineEdit()
        self.telegram_chat_ids_edit = QLineEdit()
        self.telegram_user_ids_edit.setPlaceholderText("123456789")
        self.telegram_chat_ids_edit.setPlaceholderText("-1001234567890")
        form.addRow(
            t("settings.telegram_user_ids"),
            self.telegram_user_ids_edit,
        )
        form.addRow(
            t("settings.telegram_chat_ids"),
            self.telegram_chat_ids_edit,
        )

        self.watchdog_entry_path = QLineEdit(
            str(
                ApplicationPaths.discover().program_root
                / ("BotStudioWatchdog.exe" if getattr(sys, "frozen", False) else "watchdog_app.py")
            )
        )
        self.watchdog_entry_path.setReadOnly(True)
        self.watchdog_entry_path.setToolTip(t("settings.watchdog_path_hint"))
        form.addRow(
            t("settings.watchdog_path"),
            self.watchdog_entry_path,
        )

        self.watchdog_auto_start_check = QCheckBox(
            t("settings.watchdog_auto_start")
        )
        self.watchdog_auto_start_check.setToolTip(
            t("settings.watchdog_auto_start_hint")
        )
        form.addRow(self.watchdog_auto_start_check)

        self.watchdog_status_label = QLabel()
        self.watchdog_status_label.setWordWrap(True)
        form.addRow(
            t("settings.telegram_runtime_status"),
            self.watchdog_status_label,
        )
        self.telegram_status_label = QLabel()
        self.telegram_status_label.setWordWrap(True)
        form.addRow(
            t("settings.telegram_feedback"),
            self.telegram_status_label,
        )

        self.telegram_clear_token_check = QCheckBox(
            t("settings.telegram_forget_token")
        )
        actions = QVBoxLayout()
        actions.addWidget(self.telegram_clear_token_check)
        self.telegram_test_button = QPushButton(
            t("settings.telegram_test_connection")
        )
        self.telegram_test_button.setToolTip(
            t("settings.telegram_test_connection")
        )
        self.telegram_save_button = QPushButton(
            t("settings.telegram_save")
        )
        self.telegram_save_button.setToolTip(t("settings.telegram_save"))
        actions.addWidget(self.telegram_test_button)
        actions.addWidget(self.telegram_save_button)
        form.addRow(actions)

        self.telegram_test_button.clicked.connect(
            self.test_telegram_connection
        )
        self.telegram_save_button.clicked.connect(
            self.save_telegram_settings
        )

        content_layout.addWidget(group)
        self.github_account = GitHubAccountWidget(content)
        content_layout.addWidget(self.github_account)
        content_layout.addStretch(1)
        self.scroll_area.setWidget(content)
        root_layout.addWidget(self.scroll_area)

        self._telegram_status_timer = QTimer(self)
        self._telegram_status_timer.timeout.connect(
            self._refresh_telegram_status
        )
        self._telegram_status_timer.start(2_000)
        self.load_settings()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        wrap_policy = (
            QFormLayout.RowWrapPolicy.WrapAllRows
            if self.scroll_area.viewport().width() < 420
            else QFormLayout.RowWrapPolicy.WrapLongRows
        )
        if self._telegram_form.rowWrapPolicy() != wrap_policy:
            self._telegram_form.setRowWrapPolicy(wrap_policy)
        compact = self.scroll_area.viewport().width() < 420
        self.telegram_test_button.setText(
            t(
                "settings.telegram_test_connection_short"
                if compact
                else "settings.telegram_test_connection"
            )
        )
        self.telegram_save_button.setText(
            t(
                "settings.telegram_save_short"
                if compact
                else "settings.telegram_save"
            )
        )

    def load_settings(self):
        try:
            settings = self.telegram_settings_service.load()
        except OSError as ex:
            self.telegram_status_label.setText(
                t("settings.telegram_settings_load_failed").format(
                    error=str(ex)
                )
            )
            self.telegram_save_button.setEnabled(False)
            self.telegram_test_button.setEnabled(False)
            return

        self.telegram_save_button.setEnabled(True)
        self.telegram_test_button.setEnabled(True)
        self.telegram_enabled_check.setChecked(settings["enabled"])
        self.telegram_user_ids_edit.setText(settings["allowed_user_ids"])
        self.telegram_chat_ids_edit.setText(settings["allowed_chat_ids"])
        self.watchdog_auto_start_check.setChecked(settings["auto_start"])
        self.telegram_token_edit.clear()
        self.telegram_token_edit.setPlaceholderText(
            t("settings.telegram_token_saved")
            if settings["token_configured"]
            else t("settings.telegram_token_placeholder")
        )
        self.telegram_clear_token_check.setChecked(False)
        self._refresh_telegram_status()

    def _refresh_telegram_status(self):
        try:
            running = self.watchdog_autostart_service.is_running()
            registered = self.watchdog_autostart_service.is_registered()
        except OSError as ex:
            self.watchdog_status_label.setText(
                t("settings.watchdog_status_failed").format(error=str(ex))
            )
            return
        self.watchdog_status_label.setText(
            t("settings.telegram_status").format(
                state=(
                    t("settings.telegram_running")
                    if running
                    else t("settings.telegram_stopped")
                ),
                auto_start=(
                    t("settings.telegram_registered")
                    if registered
                    else t("settings.telegram_not_registered")
                ),
            )
        )

    def test_telegram_connection(self):
        try:
            token = self.telegram_settings_service.token_for_connection(
                self.telegram_token_edit.text()
            )
        except OSError as ex:
            self.telegram_status_label.setText(str(ex))
            return
        if not token:
            self.telegram_status_label.setText(
                t("settings.telegram_token_required")
            )
            return
        self.telegram_test_button.setEnabled(False)
        self.telegram_status_label.setText(t("settings.telegram_testing"))
        operation = _TelegramTestOperation(token)
        operation.signals.completed.connect(self._telegram_test_completed)
        self._telegram_test_operation = operation
        QThreadPool.globalInstance().start(operation)

    def _telegram_test_completed(self, success, message):
        self._telegram_test_operation = None
        self.telegram_test_button.setEnabled(True)
        self.telegram_status_label.setText(
            message
            if success
            else t("settings.telegram_test_failed").format(error=message)
        )

    def save_telegram_settings(self):
        root = Path(__file__).resolve().parents[2]
        enabled = self.telegram_enabled_check.isChecked()
        auto_start = self.watchdog_auto_start_check.isChecked()
        try:
            self.telegram_settings_service.save(
                enabled=enabled,
                allowed_user_ids=self.telegram_user_ids_edit.text(),
                allowed_chat_ids=self.telegram_chat_ids_edit.text(),
                auto_start=auto_start,
                token=self.telegram_token_edit.text(),
                clear_token=self.telegram_clear_token_check.isChecked(),
            )
            if auto_start:
                working_directory = Path.cwd()
                startup_command, _ = (
                    self.watchdog_autostart_service.build_commands(
                        root,
                        os.getpid(),
                        working_directory=working_directory,
                    )
                )
                self.watchdog_autostart_service.set_registered(
                    startup_command,
                    True,
                )
            else:
                self.watchdog_autostart_service.set_registered([], False)
        except Exception as ex:
            self.logger.error(
                "Telegram/watchdog settings could not be fully applied",
                error_type=type(ex).__name__,
            )
            self.telegram_status_label.setText(
                t("settings.telegram_save_failed").format(error=str(ex))
            )
            return

        self.load_settings()
        self.telegram_status_label.setText(t("settings.telegram_saved"))
        self.start_watchdog_if_enabled()

    def start_watchdog_if_enabled(self):
        if self._watchdog_start_operation is not None:
            return
        operation = _WatchdogStartOperation(
            self.app_config_service, self.watchdog_autostart_service,
            Path(__file__).resolve().parents[2], os.getpid(), Path.cwd(),
        )
        self._watchdog_start_operation = operation
        operation.signals.completed.connect(
            self._watchdog_start_completed, Qt.ConnectionType.QueuedConnection,
        )
        QThreadPool.globalInstance().start(operation)

    def _watchdog_start_completed(self, success, message):
        self._watchdog_start_operation = None
        if not success:
            self.logger.error("Watchdog Start Failed")
            self.watchdog_status_label.setText(
                t("settings.watchdog_status_failed").format(error=message)
            )
            return
        self._refresh_telegram_status()
