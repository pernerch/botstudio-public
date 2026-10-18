from threading import Event

from PySide6.QtCore import QCoreApplication, QObject, QRunnable, QThreadPool, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QVBoxLayout,
)

from services.github_auth_service import GitHubAuthError, GitHubAuthService, GitHubPublicationAuthService
from services.application_info import GITHUB_OAUTH_CLIENT_ID
from services.logging_service import get_logger
from services.package_license_service import (
    PackageLicenseService,
    PackagePreparationService,
)
from services.reinitialize_service import defer_notifications
from ui.i18n.translator import t
from ui.widgets.package_license_dialog import (
    LegacyFunctionAttributionDialog,
    PackageLicenseDialog,
)


class _AuthSignals(QObject):
    code = Signal(str, str)
    finished = Signal(object, str)


class _AuthWorker(QRunnable):
    def __init__(self, service, operation, cancel, client_id=""):
        super().__init__()
        self.service = service
        self.operation = operation
        self.cancel = cancel
        self.client_id = client_id
        self.signals = _AuthSignals()

    def run(self):
        try:
            if self.operation == "sign_in":
                account = self.service.sign_in(self.client_id, self.cancel, self.signals.code.emit)
            elif self.operation == "restore":
                account = self.service.restore()
            else:
                self.service.sign_out()
                account = None
        except GitHubAuthError as error:
            self.signals.finished.emit(None, str(error))
        except (OSError, ValueError):
            # Raw credential errors may contain OS details; never forward secrets.
            self.signals.finished.emit(None, t("github.operation_failed"))
        else:
            self.signals.finished.emit(account, "")


class _PackagePreparationSignals(QObject):
    finished = Signal(object, str)


class _PackagePreparationWorker(QRunnable):
    def __init__(self, operation):
        super().__init__()
        self.operation = operation
        self.notifications = []
        self.signals = _PackagePreparationSignals()

    def run(self):
        try:
            with defer_notifications() as self.notifications:
                result = self.operation()
        except Exception as error:
            self.signals.finished.emit(None, str(error) or type(error).__name__)
        else:
            self.signals.finished.emit(result, "")


class GitHubDeviceCodeDialog(QDialog):
    def __init__(self, code, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("github.sign_in"))
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        instructions = QLabel(t("github.code_instructions"))
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        self.code = QLineEdit(code)
        self.code.setReadOnly(True)
        self.code.setAlignment(Qt.AlignCenter)
        font = self.code.font()
        font.setPointSize(24)
        font.setBold(True)
        self.code.setFont(font)
        self.code.setAccessibleName(t("github.device_code"))
        layout.addWidget(self.code)
        self.copy = QPushButton(t("github.copy_code"))
        self.copy.clicked.connect(self._copy_code)
        layout.addWidget(self.copy)
        self.status = QLabel()
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.browser = QPushButton(t("github.open_browser"))
        self.abort = QPushButton(t("common.cancel"))
        self.abort.clicked.connect(self.reject)
        buttons.addWidget(self.browser)
        buttons.addWidget(self.abort)
        layout.addLayout(buttons)
        self.copy.setFocus()

    def _copy_code(self):
        QApplication.clipboard().setText(self.code.text())
        self.copy.setText(t("github.code_copied"))


class GitHubAccountWidget(QGroupBox):
    account_changed = Signal(object)

    def __init__(self, parent=None, service=None, publication_service=None):
        super().__init__(t("github.account"), parent)
        self.service = service if service is not None else GitHubAuthService()
        self.publication_service = (
            publication_service if publication_service is not None else GitHubPublicationAuthService()
        )
        self.publication_confirmation = None
        self._publication_operation = False
        self._last_operation = None
        self.current_account = None
        self.package_license_service = PackageLicenseService()
        self.package_preparation_service = PackagePreparationService()
        self.logger = get_logger("github")
        self.cancel = Event()
        self.destroyed.connect(lambda _object=None, cancel=self.cancel: cancel.set())
        application = QCoreApplication.instance()
        if application is not None:
            application.aboutToQuit.connect(self._cancel)
        self.worker = None
        self.package_worker = None
        self._package_operation = None
        self._package_operation_account = None
        self._package_license_option = None
        self._package_license_prompt_pending = False
        self.code_dialog = None
        layout = QFormLayout(self)
        self.client_id = QLineEdit(GITHUB_OAUTH_CLIENT_ID)
        self.client_id.setPlaceholderText(t("github.client_id_hint"))
        layout.addRow(t("github.client_id"), self.client_id)
        self.status = QLabel(t("github.local_only"))
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addRow(self.status)
        row = QHBoxLayout()
        self.login = QPushButton(t("github.sign_in"))
        self.restore = QPushButton(t("github.check_saved"))
        self.logout = QPushButton(t("github.sign_out"))
        self.abort = QPushButton(t("common.cancel"))
        self.abort.setEnabled(False)
        for button in (self.login, self.restore, self.logout, self.abort):
            row.addWidget(button)
        layout.addRow(row)
        hint = QLabel(t("github.permissions_hint"))
        hint.setWordWrap(True)
        layout.addRow(hint)
        self.publication_status = QLabel(t("github.publication_unchecked"))
        self.publication_status.setTextFormat(Qt.PlainText)
        self.publication_status.setWordWrap(True)
        layout.addRow(self.publication_status)
        publication_row = QHBoxLayout()
        self.publication_authorize = QPushButton(t("github.publication_authorize"))
        self.publication_check = QPushButton(t("github.publication_check"))
        self.publication_remove = QPushButton(t("github.publication_remove"))
        for button in (self.publication_authorize, self.publication_check, self.publication_remove):
            publication_row.addWidget(button)
        layout.addRow(publication_row)
        self.package_license_status = QLabel(t("package_license.no_account"))
        self.package_license_status.setTextFormat(Qt.PlainText)
        self.package_license_status.setWordWrap(True)
        layout.addRow(self.package_license_status)
        package_row = QHBoxLayout()
        self.package_license_button = QPushButton(t("github.package_license_choose"))
        self.package_attribute_button = QPushButton(t("github.package_attribute_legacy"))
        for button in (self.package_license_button, self.package_attribute_button):
            package_row.addWidget(button)
        layout.addRow(package_row)
        self.publication_authorize.clicked.connect(self._confirm_publication)
        self.publication_check.clicked.connect(lambda: self._start("restore", publication=True))
        self.publication_remove.clicked.connect(lambda: self._start("sign_out", publication=True))
        self.login.clicked.connect(lambda: self._start("sign_in"))
        self.restore.clicked.connect(lambda: self._start("restore"))
        self.logout.clicked.connect(lambda: self._start("sign_out"))
        self.abort.clicked.connect(self._cancel)
        self.package_license_button.clicked.connect(self._choose_package_license)
        self.package_attribute_button.clicked.connect(self._attribute_legacy_functions)

    def restore_session(self):
        self._start("restore")

    def _confirm_publication(self):
        if (
            self.worker is not None
            or self.package_worker is not None
            or self.publication_confirmation is not None
        ):
            return
        dialog = QMessageBox(
            QMessageBox.Warning, t("github.publication_authorize"), t("github.publication_consent"),
            QMessageBox.Yes | QMessageBox.No, self,
        )
        dialog.setTextFormat(Qt.PlainText)
        dialog.setDefaultButton(QMessageBox.No)
        self.publication_confirmation = dialog
        dialog.finished.connect(self._publication_confirmed)
        dialog.open()

    def _publication_confirmed(self, result):
        self.publication_confirmation.deleteLater()
        self.publication_confirmation = None
        if result == QMessageBox.Yes:
            self._start("sign_in", publication=True)

    def _start(self, operation, *, publication=False):
        if (
            self.worker is not None
            or self.package_worker is not None
            or self.publication_confirmation is not None
        ):
            return
        self._publication_operation = publication
        self._last_operation = operation
        if not publication:
            self.account_changed.emit(None)
        self.cancel.clear()
        self._close_code_dialog()
        status = self.publication_status if publication else self.status
        status.setText(t("github.busy"))
        for control in self._operation_controls():
            control.setEnabled(False)
        self.abort.setEnabled(operation == "sign_in")
        service = self.publication_service if publication else self.service
        self.worker = _AuthWorker(service, operation, self.cancel, self.client_id.text().strip())
        self.worker.signals.code.connect(self._show_code)
        self.worker.signals.finished.connect(self._finished)
        QThreadPool.globalInstance().start(self.worker)

    def _operation_controls(self):
        return (
            self.login, self.restore, self.logout, self.client_id,
            self.publication_authorize, self.publication_check, self.publication_remove,
            self.package_license_button, self.package_attribute_button,
        )

    def _show_code(self, code, url):
        if not self.cancel.is_set() and url == GitHubAuthService.VERIFICATION_URL:
            self._close_code_dialog()
            self.code_dialog = GitHubDeviceCodeDialog(code, self)
            self.code_dialog.rejected.connect(self._cancel)
            self.code_dialog.browser.clicked.connect(self._open_browser)
            self.code_dialog.open()
            self._open_browser()

    def _close_code_dialog(self):
        dialog = self.code_dialog
        self.code_dialog = None
        if dialog is not None:
            dialog.code.clear()
            dialog.accept()
            dialog.deleteLater()

    def _cancel(self):
        self.cancel.set()
        self._close_code_dialog()
        self.abort.setEnabled(False)

    def _open_browser(self):
        if not QDesktopServices.openUrl(QUrl(GitHubAuthService.VERIFICATION_URL)):
            message = t("github.browser_failed")
            status = self.publication_status if self._publication_operation else self.status
            status.setText(message)
            if self.code_dialog is not None:
                self.code_dialog.status.setText(message)
            self.logger.warning("GitHub Browser Open Failed", next_action="open the GitHub device URL manually")
        elif self.code_dialog is not None:
            self.code_dialog.status.clear()

    def _finished(self, account, error):
        self.worker = None
        self._close_code_dialog()
        self.abort.setEnabled(False)
        for control in self._operation_controls():
            control.setEnabled(True)
        status = self.publication_status if self._publication_operation else self.status
        if error:
            status.setText(error)
            self.logger.warning("GitHub Account Operation Failed", next_action="retry sign-in or check saved authorization")
        elif account is None:
            status.setText(t("github.publication_unchecked" if self._publication_operation else "github.local_only"))
        else:
            status.setText(t(
                "github.publication_connected" if self._publication_operation else "github.connected",
            ).format(login=account.login, account_id=account.account_id))
        if not self._publication_operation:
            self.current_account = None if error else account
            self.account_changed.emit(None if error else account)
            self._package_license_option = None
            if account is None or error:
                self._package_license_prompt_pending = False
                self._render_package_license_status()
            else:
                self._refresh_package_license(
                    prompt_if_missing=self._last_operation in {"sign_in", "restore"}
                )
        else:
            self._render_package_license_status()

    def _refresh_package_license(self, *, prompt_if_missing=False):
        account = self.current_account
        if account is None:
            self._package_license_option = None
            self._package_license_prompt_pending = False
            self._render_package_license_status()
            return
        self._package_license_prompt_pending = prompt_if_missing
        self._start_package_preparation(
            "license_load",
            lambda: self.package_license_service.get_license(account.account_id),
            account,
        )

    def _render_package_license_status(self):
        account = self.current_account
        idle = self.worker is None and self.package_worker is None
        self.package_license_button.setEnabled(account is not None and idle)
        self.package_attribute_button.setEnabled(account is not None and idle)
        if account is None:
            self.package_license_status.setText(t("package_license.no_account"))
        elif self._package_license_option is None:
            self.package_license_status.setText(t("github.package_license_unset"))
        else:
            self.package_license_status.setText(
                t("github.package_license").format(
                    license=(
                        f"{self._package_license_option.name} "
                        f"({self._package_license_option.spdx_id})"
                    )
                )
            )

    def _choose_package_license(self):
        account = self.current_account
        if account is None:
            return
        dialog = PackageLicenseDialog(
            self._package_license_option.spdx_id
            if self._package_license_option is not None
            else None,
            self,
        )
        if dialog.exec() != QDialog.Accepted or dialog.selected_spdx_id is None:
            return
        self._package_license_prompt_pending = False
        self._start_package_preparation(
            "license_save",
            lambda: self.package_license_service.set_license(
                account.account_id,
                dialog.selected_spdx_id,
            ),
            account,
        )

    def _attribute_legacy_functions(self):
        account = self.current_account
        if account is None:
            return
        self._start_package_preparation(
            "scan",
            self.package_preparation_service.list_unattributed_functions,
            account,
        )

    def _start_package_preparation(self, operation, action, account):
        if self.worker is not None or self.package_worker is not None or account is None:
            return
        self._package_operation = operation
        self._package_operation_account = account
        self.package_license_status.setText(t("github.busy"))
        for control in self._operation_controls():
            control.setEnabled(False)
        self.abort.setEnabled(False)
        self.package_worker = _PackagePreparationWorker(action)
        self.package_worker.signals.finished.connect(self._package_preparation_finished)
        QThreadPool.globalInstance().start(self.package_worker)

    def _package_preparation_finished(self, result, error):
        operation = self._package_operation
        account = self._package_operation_account
        worker = self.package_worker
        self.package_worker = None
        self._package_operation = None
        self._package_operation_account = None
        for control in self._operation_controls():
            control.setEnabled(True)
        if worker is not None:
            for service, source, payload in worker.notifications:
                service.publish(source, **payload)
        self._render_package_license_status()
        if operation == "license_load":
            if error:
                self._package_license_prompt_pending = False
                self.package_license_status.setText(
                    t("package_license.load_failed").format(error=error)
                )
            else:
                self._package_license_option = result
                self._render_package_license_status()
                if self._package_license_prompt_pending and result is None:
                    self._package_license_prompt_pending = False
                    QTimer.singleShot(0, self._choose_package_license)
                else:
                    self._package_license_prompt_pending = False
            return
        if operation == "license_save":
            if error:
                QMessageBox.critical(
                    self,
                    t("package_license.title"),
                    t("package_license.save_failed").format(error=error),
                )
            else:
                self._package_license_option = result
                self._render_package_license_status()
            return
        if error:
            message_key = (
                "package_license.scan_failed"
                if operation == "scan"
                else "package_license.attribute_failed"
            )
            QMessageBox.critical(
                self,
                t("github.package_attribute_legacy"),
                t(message_key).format(error=error),
            )
            return
        if operation == "scan":
            self._present_legacy_functions(result, account)
        elif operation == "attribute":
            QMessageBox.information(
                self,
                t("github.package_attribute_legacy"),
                t("package_license.legacy_done").format(
                    count=result,
                    login=account.login,
                ),
            )

    def _present_legacy_functions(self, functions, account):
        if not functions:
            QMessageBox.information(
                self,
                t("github.package_attribute_legacy"),
                t("package_license.legacy_empty"),
            )
            return
        dialog = LegacyFunctionAttributionDialog(functions, self)
        if dialog.exec() != QDialog.Accepted or not dialog.selections:
            return
        confirmation = QMessageBox(
            QMessageBox.Warning,
            t("github.package_attribute_legacy"),
            t("package_license.legacy_confirm").format(
                count=len(dialog.selections),
                login=account.login,
                account_id=account.account_id,
            ),
            QMessageBox.Yes | QMessageBox.No,
            self,
        )
        confirmation.setTextFormat(Qt.PlainText)
        confirmation.setDefaultButton(QMessageBox.No        )
        if confirmation.exec() != QMessageBox.Yes:
            return
        selections = list(dialog.selections)
        self._start_package_preparation(
            "attribute",
            lambda: self.package_preparation_service.attribute_functions(selections, account),
            account,
        )
