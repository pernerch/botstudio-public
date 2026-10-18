import faulthandler
import os
from threading import Event, get_ident
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import requests
from PySide6.QtCore import QEventLoop, QMimeData, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QMainWindow, QMessageBox

from services.github_auth_service import (
    GitHubAccount, GitHubAuthError, GitHubAuthService, GitHubCredentials,
    GitHubPublicationAuthService, GitHubPublicationCredentials,
)
from services.reinitialize_service import ReinitializeService
from services.windows_credential_service import WindowsCredentialService
from ui.widgets.github_account_widget import GitHubAccountWidget, _AuthWorker


class _Clock(Event):
    def __init__(self):
        super().__init__()
        self.now = 0
        self.waits = []
        self.cancelled = False

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += seconds
        return self.cancelled

    def is_set(self):
        return self.cancelled


class _AuthTestCase(unittest.TestCase):
    def setUp(self):
        started = time.monotonic()
        print(f"TEST START: {self.id()}")
        faulthandler.dump_traceback_later(15, exit=True)
        self.addCleanup(faulthandler.cancel_dump_traceback_later)
        self.addCleanup(lambda: print(
            f"TEST END: {self.id()} duration {time.monotonic() - started:.3f}s",
        ))
        self.credentials = Mock()
        self.credentials.load.return_value = None
        self.clock = _Clock()
        self.transport = Mock()
        self.service = GitHubAuthService(self.credentials, self.transport, self.clock)
        self.on_code = Mock()
        self.device = {
            "device_code": "private_device_code", "user_code": "ABCD-EFGH",
            "verification_uri": self.service.VERIFICATION_URL,
            "expires_in": 900, "interval": 5,
        }
        self.token = {"access_token": "fixture_token", "token_type": "bearer"}
        self.account = {"id": 12345, "login": "test-user"}

    def respond(self, *payloads):
        self.transport.side_effect = [
            Mock(status_code=200, json=Mock(return_value=payload))
            for payload in payloads
        ]

    def sign_in(self):
        return self.service.sign_in("public_client_id", self.clock, self.on_code)


class GitHubAuthTests(_AuthTestCase):
    def publication_service(self):
        return GitHubPublicationAuthService(self.credentials, self.transport, self.clock)

    def respond_publication(self, scopes="public_repo"):
        self.transport.side_effect = [
            Mock(status_code=200, json=Mock(return_value=self.device)),
            Mock(status_code=200, json=Mock(return_value=self.token)),
            Mock(status_code=200, headers={"X-OAuth-Scopes": scopes}, json=Mock(return_value=self.account)),
        ]

    def test_publication_scope_is_opt_in_and_token_target_is_distinct(self):
        service = self.publication_service()
        self.respond_publication()
        account = service.sign_in("public_client_id", self.clock, self.on_code)
        self.assertEqual("12345", account.account_id)
        self.assertEqual("public_repo", self.transport.call_args_list[0].kwargs["data"]["scope"])
        self.credentials.save.assert_called_once_with("fixture_token")
        self.assertNotEqual(GitHubCredentials.TARGET, GitHubPublicationCredentials.TARGET)
        self.assertIsInstance(GitHubPublicationAuthService().credentials, GitHubPublicationCredentials)
        self.assertEqual("", GitHubAuthService.REQUIRED_SCOPE)

    def test_publication_rejects_missing_reduced_and_broader_live_scopes_without_saving(self):
        service = self.publication_service()
        self.credentials.load.return_value = "previous_publication_token"
        for scopes in (None, "", "read:user", "repo", "public_repo, workflow", "public_repo, repo"):
            with self.subTest(scopes=scopes):
                self.respond_publication(scopes)
                with self.assertRaisesRegex(GitHubAuthError, "exactly public_repo"):
                    service.sign_in("public_client_id", self.clock, self.on_code)
        self.credentials.save.assert_not_called()
        self.credentials.delete.assert_not_called()

    def test_publication_restore_rechecks_live_scope_and_identity_without_token_upgrade(self):
        service = self.publication_service()
        self.assertIsNone(service.restore())
        self.transport.assert_not_called()
        self.credentials.load.return_value = "fixture_token"
        self.transport.side_effect = [
            Mock(status_code=200, headers={"X-OAuth-Scopes": "public_repo"}, json=Mock(return_value=self.account)),
            Mock(status_code=200, headers={"X-OAuth-Scopes": ""}, json=Mock(return_value=self.account)),
        ]
        self.assertEqual("test-user", service.restore().login)
        with self.assertRaisesRegex(GitHubAuthError, "exactly public_repo"):
            service.restore()
        self.credentials.save.assert_not_called()
        service.sign_out()
        self.credentials.delete.assert_called_once_with()

    def test_cancelled_publication_does_not_replace_prior_token(self):
        service = self.publication_service()
        self.credentials.load.return_value = "previous_publication_token"
        self.respond_publication()
        self.on_code.side_effect = lambda *_: setattr(self.clock, "cancelled", True)
        with self.assertRaisesRegex(GitHubAuthError, "cancelled"):
            service.sign_in("public_client_id", self.clock, self.on_code)
        self.credentials.save.assert_not_called()
        self.credentials.delete.assert_not_called()

    def test_success_validates_live_account_before_storing_only_token(self):
        self.respond(self.device, self.token, self.account)
        result = self.sign_in()
        self.assertEqual(GitHubAccount("12345", "test-user"), result)
        self.assertEqual({"provider": "github", "account_id": "12345", "login": "test-user"},
                         result.to_dict())
        self.credentials.save.assert_called_once_with("fixture_token")
        self.on_code.assert_called_once_with("ABCD-EFGH", self.service.VERIFICATION_URL)
        self.assertEqual([5], self.clock.waits)
        first, poll, user = self.transport.call_args_list
        self.assertEqual("", first.kwargs["data"]["scope"])
        self.assertNotIn("client_secret", poll.kwargs["data"])
        self.assertEqual("Bearer fixture_token", user.kwargs["headers"]["Authorization"])
        for call in self.transport.call_args_list:
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertEqual((5, 10), call.kwargs["timeout"])
        self.assertNotEqual(WindowsCredentialService.TARGET, GitHubCredentials.TARGET)

    def test_pending_and_slow_down_respect_minimum_polling_intervals(self):
        self.respond(self.device, {"error": "authorization_pending"},
                     {"error": "slow_down"}, {"error": "authorization_pending"},
                     self.token, self.account)
        self.sign_in()
        self.assertEqual([5, 5, 10, 10], self.clock.waits)

    def test_expiry_stops_polling_without_saving_credentials(self):
        self.respond(dict(self.device, expires_in=6), {"error": "authorization_pending"})
        with self.assertRaisesRegex(GitHubAuthError, "expired"):
            self.sign_in()
        self.assertEqual([5, 1], self.clock.waits)
        self.assertEqual(2, self.transport.call_count)
        self.credentials.save.assert_not_called()

    def test_cancel_before_request_and_after_user_code(self):
        self.clock.cancelled = True
        with self.assertRaisesRegex(GitHubAuthError, "cancelled"):
            self.sign_in()
        self.transport.assert_not_called()
        self.clock.cancelled = False
        self.respond(self.device)
        self.on_code.side_effect = lambda *_: setattr(self.clock, "cancelled", True)
        with self.assertRaisesRegex(GitHubAuthError, "cancelled"):
            self.sign_in()
        self.credentials.save.assert_not_called()

    def test_denials_and_unknown_errors_fail_explicitly_without_raw_response(self):
        for error in ("access_denied", "expired_token", "device_flow_disabled",
                      "incorrect_client_credentials", "private_device_code", {"bad": "shape"}):
            with self.subTest(error=error):
                self.respond(self.device, {"error": error})
                with self.assertRaises(GitHubAuthError) as caught:
                    self.sign_in()
                self.assertNotIn("private_device_code", str(caught.exception))
        self.credentials.save.assert_not_called()

    def test_invalid_device_data_or_url_never_opens_browser_or_polls(self):
        for changes in ({"verification_uri": "https://example.org"},
                        {"expires_in": True}, {"expires_in": 901},
                        {"interval": 0}, {"user_code": "<script>"},
                        {"device_code": None}):
            with self.subTest(changes=changes):
                self.respond(dict(self.device, **changes))
                with self.assertRaises(GitHubAuthError):
                    self.sign_in()
        self.on_code.assert_not_called()

    def test_bad_account_and_token_cannot_be_stored(self):
        for account in ({"id": True, "login": "valid"}, {"id": 1, "login": "bad--login"},
                        {"id": 1, "login": "@claimed"}, {"id": "123", "login": "valid"}):
            with self.subTest(account=account):
                self.respond(self.device, self.token, account)
                with self.assertRaises(GitHubAuthError):
                    self.sign_in()
        for token in ({"access_token": "", "token_type": "bearer"},
                      dict(self.token, token_type=None)):
            with self.subTest(token=token):
                self.respond(self.device, token)
                with self.assertRaises(GitHubAuthError):
                    self.sign_in()
        self.credentials.save.assert_not_called()

    def test_http_redirect_network_failure_and_non_object_fail_closed(self):
        for response in (Mock(status_code=302), Mock(status_code=401),
                         Mock(status_code=200, json=Mock(return_value=[]))):
            with self.subTest(response=response):
                self.transport.side_effect = None
                self.transport.return_value = response
                with self.assertRaises(GitHubAuthError):
                    self.sign_in()
        self.transport.side_effect = requests.ConnectionError("fixture_token")
        with self.assertRaises(GitHubAuthError) as caught:
            self.sign_in()
        self.assertNotIn("fixture_token", str(caught.exception))
        self.credentials.save.assert_not_called()

    def test_restore_always_requeries_user_and_signout_only_removes_local_token(self):
        self.assertIsNone(self.service.restore())
        self.transport.assert_not_called()
        self.credentials.load.return_value = "fixture_token"
        self.respond(self.account, {"id": 12345, "login": "renamed"})
        self.assertEqual("test-user", self.service.restore().login)
        self.assertEqual("renamed", self.service.restore().login)
        self.service.sign_out()
        self.credentials.delete.assert_called_once()
        self.assertEqual(2, self.transport.call_count)

    def test_storage_failure_does_not_return_success_and_cancellation_cleans_token(self):
        self.respond(self.device, self.token, self.account)
        self.credentials.save.side_effect = OSError("fixture failure")
        with self.assertRaises(OSError):
            self.sign_in()
        self.credentials.save.side_effect = lambda *_: setattr(self.clock, "cancelled", True)
        self.respond(self.device, self.token, self.account)
        with self.assertRaisesRegex(GitHubAuthError, "cancelled"):
            self.sign_in()
        self.credentials.delete.assert_called_once()

    def test_cancellation_during_storage_preserves_previous_login(self):
        self.credentials.load.return_value = "previous_token"
        self.credentials.save.side_effect = lambda *_: setattr(self.clock, "cancelled", True)
        self.respond(self.device, self.token, self.account)
        with self.assertRaisesRegex(GitHubAuthError, "cancelled"):
            self.sign_in()
        self.assertEqual(["fixture_token", "previous_token"],
                         [call.args[0] for call in self.credentials.save.call_args_list])
        self.credentials.delete.assert_not_called()

    def test_slow_network_response_past_expiry_does_not_store_token(self):
        payloads = iter((self.device, self.token, self.account))

        def response(method, *_args, **_kwargs):
            if method == "GET":
                self.clock.now = 901
            return Mock(status_code=200, json=Mock(return_value=next(payloads)))

        self.transport.side_effect = response
        with self.assertRaisesRegex(GitHubAuthError, "expired"):
            self.sign_in()
        self.credentials.save.assert_not_called()


class GitHubAccountUiTests(_AuthTestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        browser_patch = patch(
            "ui.widgets.github_account_widget.QDesktopServices.openUrl", return_value=True,
        )
        self.open_browser = browser_patch.start()
        self.addCleanup(browser_patch.stop)

    def wait_for_idle(self, widget):
        loop = QEventLoop()
        timer = QTimer()
        timer.timeout.connect(lambda: loop.quit() if widget.worker is None else None)
        timer.start(5)
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)
        timeout.start(3000)
        try:
            loop.exec()
        finally:
            timer.stop()
            timeout.stop()
        self.assertIsNone(widget.worker, "GitHub operation exceeded 3 seconds")

    def test_publication_consent_defaults_no_and_does_not_touch_normal_account(self):
        normal, publication = Mock(), Mock()
        widget = GitHubAccountWidget(service=normal, publication_service=publication)
        self.addCleanup(widget.deleteLater)
        changed = []
        widget.account_changed.connect(changed.append)
        widget.publication_authorize.click()
        self.assertIsNotNone(widget.publication_confirmation)
        dialog = widget.publication_confirmation
        self.assertEqual(QMessageBox.No, dialog.standardButton(dialog.defaultButton()))
        self.assertIn("public_repo", dialog.text())
        self.assertIn("pernerch/botstudio-catalog", dialog.text())
        widget.restore_session()
        normal.restore.assert_not_called()
        self.assertIsNone(widget.worker)
        dialog.done(QMessageBox.No)
        self.assertIsNone(widget.publication_confirmation)
        publication.sign_in.assert_not_called()
        normal.sign_in.assert_not_called()
        self.assertEqual([], changed)
        self.open_browser.assert_not_called()

    def test_publication_uses_worker_and_separate_status_without_changing_header_account(self):
        normal = Mock()
        threads = []
        def sign_in(_client, _cancel, on_code):
            threads.append(get_ident())
            on_code("ABCD-EFGH", GitHubAuthService.VERIFICATION_URL)
            return GitHubAccount("999", "publication-user")
        publication = Mock(sign_in=Mock(side_effect=sign_in))
        widget = GitHubAccountWidget(service=normal, publication_service=publication)
        self.addCleanup(widget.deleteLater)
        widget.status.setText("normal-account")
        changed = []
        widget.account_changed.connect(changed.append)
        widget.publication_authorize.click()
        widget.publication_confirmation.done(QMessageBox.Yes)
        self.assertFalse(widget.login.isEnabled())
        self.assertFalse(widget.publication_authorize.isEnabled())
        self.wait_for_idle(widget)
        self.assertEqual("normal-account", widget.status.text())
        self.assertIn("@publication-user", widget.publication_status.text())
        self.assertEqual([], changed)
        self.assertEqual(1, len(threads))
        self.assertNotIn(get_ident(), threads)
        normal.sign_in.assert_not_called()
        self.assertIsNone(widget.code_dialog)
        self.open_browser.assert_called_once()
        publication.restore.return_value = GitHubAccount("999", "renamed")
        widget.publication_check.click()
        self.wait_for_idle(widget)
        self.assertIn("@renamed", widget.publication_status.text())
        publication.restore.side_effect = GitHubAuthError("publication scope revoked")
        widget.publication_check.click()
        self.wait_for_idle(widget)
        self.assertIn("revoked", widget.publication_status.text())
        self.assertNotIn("@renamed", widget.publication_status.text())
        widget.publication_remove.click()
        self.wait_for_idle(widget)
        publication.sign_out.assert_called_once_with()
        normal.sign_out.assert_not_called()
        self.assertEqual([], changed)
        self.assertTrue(widget.login.isEnabled())

    def test_worker_keeps_ui_responsive_and_cancel_completes(self):
        entered = Event()

        def waiting_login(_client, cancel, on_code):
            entered.set()
            on_code("ABCD-EFGH", GitHubAuthService.VERIFICATION_URL)
            if cancel.wait(2):
                raise GitHubAuthError("GitHub sign-in cancelled")
            raise GitHubAuthError("test timeout")

        service = Mock(sign_in=Mock(side_effect=waiting_login))
        widget = GitHubAccountWidget(service=service)
        self.addCleanup(widget.deleteLater)
        widget.client_id.setText("public_client_id")
        loop = QEventLoop()
        ticked = []
        timer = QTimer()
        timer.setInterval(10)

        def tick():
            ticked.append(True)
            if entered.is_set():
                widget.abort.click()
            if widget.worker is None:
                loop.quit()

        timer.timeout.connect(tick)
        timer.start()
        self.addCleanup(timer.stop)
        widget.login.click()
        self.assertFalse(widget.login.isEnabled())
        deadline = QTimer()
        deadline.setSingleShot(True)
        deadline.timeout.connect(loop.quit)
        deadline.start(3000)
        self.addCleanup(deadline.stop)
        loop.exec()
        self.assertTrue(ticked)
        self.assertIsNone(widget.worker)
        self.assertTrue(widget.login.isEnabled())
        self.assertIsNone(widget.code_dialog)
        self.assertIn("cancelled", widget.status.text())

    def test_code_popup_opens_browser_and_copies_only_the_code(self):
        from ui.i18n import translator
        from ui.i18n.translator import set_language, t

        self.addCleanup(set_language, translator.LANG)
        clipboard = QApplication.clipboard()
        previous = clipboard.mimeData()
        saved = {} if previous is None else {
            name: previous.data(name) for name in previous.formats()
        }

        def restore_clipboard():
            if not saved:
                clipboard.clear()
                return
            restored = QMimeData()
            for name, data in saved.items():
                restored.setData(name, data)
            clipboard.setMimeData(restored)

        self.addCleanup(restore_clipboard)
        widget = GitHubAccountWidget(service=Mock())
        self.addCleanup(widget.deleteLater)
        for language in ("en", "de"):
            with self.subTest(language=language):
                set_language(language)
                self.open_browser.reset_mock()
                widget._show_code("ABCD-EFGH", GitHubAuthService.VERIFICATION_URL)
                dialog = widget.code_dialog
                self.assertIsNotNone(dialog)
                self.assertTrue(dialog.isVisible())
                self.assertTrue(dialog.code.isReadOnly())
                self.assertEqual("ABCD-EFGH", dialog.code.text())
                self.assertGreaterEqual(dialog.code.font().pointSize(), 24)
                self.open_browser.assert_called_once()
                self.assertEqual(GitHubAuthService.VERIFICATION_URL,
                                 self.open_browser.call_args.args[0].toString())
                dialog.copy.click()
                self.assertEqual("ABCD-EFGH", clipboard.text())
                self.assertEqual(t("github.code_copied"), dialog.copy.text())
                widget._finished(GitHubAccount("123", "test-user"), "")
                self.assertIsNone(widget.code_dialog)
                self.assertFalse(dialog.isVisible())
                self.assertEqual("", dialog.code.text())
                self.assertFalse(widget.cancel.is_set())

    def test_popup_close_escape_and_cancel_stop_login_without_reopening_browser(self):
        for action in ("close", "reject", "abort"):
            with self.subTest(action=action):
                widget = GitHubAccountWidget(service=Mock())
                self.addCleanup(widget.deleteLater)
                widget._show_code("ABCD-EFGH", GitHubAuthService.VERIFICATION_URL)
                dialog = widget.code_dialog
                if action == "abort":
                    dialog.abort.click()
                else:
                    getattr(dialog, action)()
                self.assertTrue(widget.cancel.is_set())
                self.assertIsNone(widget.code_dialog)
                calls = self.open_browser.call_count
                widget._show_code("ABCD-EFGH", GitHubAuthService.VERIFICATION_URL)
                self.assertIsNone(widget.code_dialog)
                self.assertEqual(calls, self.open_browser.call_count)

    def test_popup_shows_browser_failure_and_allows_retry(self):
        from ui.i18n.translator import t

        widget = GitHubAccountWidget(service=Mock())
        self.addCleanup(widget.deleteLater)
        self.open_browser.return_value = False
        widget._show_code("ABCD-EFGH", GitHubAuthService.VERIFICATION_URL)
        dialog = widget.code_dialog
        self.assertTrue(dialog.isVisible())
        self.assertEqual(t("github.browser_failed"), dialog.status.text())
        self.open_browser.return_value = True
        dialog.browser.click()
        self.assertEqual("", dialog.status.text())
        self.assertEqual(2, self.open_browser.call_count)
        widget._finished(None, "expired")
        self.assertIsNone(widget.code_dialog)
        self.assertFalse(dialog.isVisible())
        self.assertEqual("expired", widget.status.text())

    def test_untrusted_url_neither_opens_browser_nor_popup(self):
        widget = GitHubAccountWidget(service=Mock())
        self.addCleanup(widget.deleteLater)
        widget._show_code("ABCD-EFGH", "https://example.org")
        self.assertIsNone(widget.code_dialog)
        self.open_browser.assert_not_called()

    def test_restore_and_logout_render_no_verified_authorship_claim(self):
        widget = GitHubAccountWidget(service=Mock())
        self.addCleanup(widget.deleteLater)
        widget._finished(GitHubAccount("123", "renamed"), "")
        self.assertIn("@renamed", widget.status.text())
        self.assertIn("123", widget.status.text())
        widget._finished(None, "")
        self.assertNotIn("@renamed", widget.status.text())

    def test_account_changes_update_native_title_and_centered_header(self):
        from services.application_info import APP_NAME, APP_VERSION
        from ui.views.main_window import MainWindow
        from ui.widgets.app_header_bar import AppHeaderBar

        with patch("ui.widgets.app_header_bar.ThemeService") as theme:
            theme.return_value.get_font_size_px.return_value = 12
            header = AppHeaderBar(clock_service=Mock(format_pair=Mock(return_value="clock")))
            self.addCleanup(header.deleteLater)
            window = QMainWindow()
            title = f"{APP_NAME} --V{APP_VERSION}--"
            window.setWindowTitle(title)
            self.addCleanup(window.deleteLater)
            window.header_bar = header
            widget = GitHubAccountWidget(service=Mock())
            self.addCleanup(widget.deleteLater)
            widget.account_changed.connect(lambda account: MainWindow._github_account_changed(window, account))
            for login in ("first-user", "renamed-user"):
                widget._finished(GitHubAccount("123", login), "")
                header.refresh()
                self.assertEqual(title, window.windowTitle())
                self.assertIn(f"GitHub: @{login}", header.label.text())
                self.assertNotIn("--V", header.label.text())
            widget._finished(None, "authorization failed")
            self.assertEqual(title, window.windowTitle())
            self.assertNotIn("GitHub:", header.label.text())
            widget._finished(GitHubAccount("123", "first-user"), "")
            widget._finished(None, "")
            self.assertEqual(title, window.windowTitle())
            self.assertNotIn("GitHub:", header.label.text())

    def test_combined_navigation_label_in_both_languages_preserves_saved_key(self):
        from PySide6.QtCore import Qt
        from ui.i18n import translator
        from ui.i18n.translator import set_language
        from ui.views.navigation_view import NavigationView

        self.addCleanup(set_language, translator.LANG)
        for language in ("en", "de"):
            with self.subTest(language=language), patch("ui.views.navigation_view.QSettings") as settings:
                settings.return_value.value.return_value = "telegram,settings"
                set_language(language)
                view = NavigationView()
                self.addCleanup(view.deleteLater)
                self.assertEqual("Telegram/GitHub", view.menu.item(0).text())
                self.assertEqual("telegram", view.menu.item(0).data(Qt.UserRole))

    def test_saved_session_restores_in_worker_and_notifies_gui_after_live_check(self):
        entered, release = Event(), Event()
        self.addCleanup(release.set)
        worker_threads, notifications = [], []

        def restore():
            worker_threads.append(get_ident())
            entered.set()
            if not release.wait(2):
                raise GitHubAuthError("fixture restore timeout")
            return GitHubAccount("123", "restored-user")

        service = Mock(restore=Mock(side_effect=restore))
        widget = GitHubAccountWidget(service=service)
        self.addCleanup(widget.deleteLater)
        widget.package_license_service = Mock(
            get_license=Mock(return_value=SimpleNamespace(name="MIT", spdx_id="MIT")),
        )
        widget.account_changed.connect(lambda account: notifications.append((account, get_ident())))
        widget.restore_session()
        self.assertEqual([(None, get_ident())], notifications)
        loop = QEventLoop()
        timer = QTimer()
        self.addCleanup(timer.stop)
        def tick():
            if entered.is_set():
                release.set()
            if widget.worker is None:
                loop.quit()
        timer.timeout.connect(tick)
        timer.start(10)
        deadline = QTimer()
        deadline.setSingleShot(True)
        deadline.timeout.connect(loop.quit)
        deadline.start(3000)
        self.addCleanup(deadline.stop)
        loop.exec()
        self.assertIsNone(widget.worker)
        self.assertEqual(1, len(worker_threads))
        self.assertNotIn(get_ident(), worker_threads)
        self.assertEqual((GitHubAccount("123", "restored-user"), get_ident()), notifications[-1])
        service.restore.assert_called_once_with()

    def test_missing_package_license_is_requested_after_account_restore(self):
        account = GitHubAccount("123", "restored-user")
        service = Mock(restore=Mock(return_value=account))
        widget = GitHubAccountWidget(service=service)
        self.addCleanup(widget.deleteLater)
        preference_threads = []
        preferences = Mock()
        preferences.get_license.side_effect = lambda _account_id: (
            preference_threads.append(get_ident()) or None
        )
        preferences.set_license.side_effect = lambda _account_id, _spdx_id: (
            preference_threads.append(get_ident())
            or SimpleNamespace(name="MIT", spdx_id="MIT")
        )
        widget.package_license_service = preferences

        with patch("ui.widgets.github_account_widget.PackageLicenseDialog") as license_dialog:
            license_dialog.return_value.exec.return_value = QDialog.Accepted
            license_dialog.return_value.selected_spdx_id = "MIT"
            widget.restore_session()
            loop = QEventLoop()
            timer = QTimer()
            timer.timeout.connect(
                lambda: loop.quit()
                if (
                    widget.worker is None
                    and widget.package_worker is None
                    and preferences.set_license.called
                )
                else None
            )
            timer.start(10)
            self.addCleanup(timer.stop)
            deadline = QTimer()
            deadline.setSingleShot(True)
            deadline.timeout.connect(loop.quit)
            deadline.start(3000)
            self.addCleanup(deadline.stop)
            loop.exec()

        self.assertIsNone(widget.worker)
        self.assertIsNone(widget.package_worker)
        preferences.get_license.assert_called_once_with("123")
        preferences.set_license.assert_called_once_with("123", "MIT")
        self.assertEqual(2, len(preference_threads))
        self.assertTrue(all(thread_id != get_ident() for thread_id in preference_threads))

    def test_legacy_function_scan_runs_off_the_gui_thread(self):
        scan_threads = []

        def scan():
            scan_threads.append(get_ident())
            return []

        widget = GitHubAccountWidget(service=Mock())
        self.addCleanup(widget.deleteLater)
        widget.current_account = GitHubAccount("123", "restored-user")
        widget.package_license_service = Mock(
            get_license=Mock(return_value=SimpleNamespace(name="MIT", spdx_id="MIT")),
        )
        widget.package_preparation_service.list_unattributed_functions = scan

        with patch.object(QMessageBox, "information"):
            widget._attribute_legacy_functions()
            loop = QEventLoop()
            timer = QTimer()
            timer.timeout.connect(
                lambda: loop.quit()
                if widget.package_worker is None and scan_threads
                else None
            )
            timer.start(10)
            self.addCleanup(timer.stop)
            deadline = QTimer()
            deadline.setSingleShot(True)
            deadline.timeout.connect(loop.quit)
            deadline.start(3000)
            self.addCleanup(deadline.stop)
            loop.exec()

        self.assertIsNone(widget.package_worker)
        self.assertEqual(1, len(scan_threads))
        self.assertNotEqual(get_ident(), scan_threads[0])

    def test_package_preparation_replays_persistence_notifications_on_gui_thread(self):
        account = GitHubAccount("123", "restored-user")
        reinitialize = ReinitializeService()
        notification_threads = []
        reinitialize.subscribe(lambda *_args: notification_threads.append(get_ident()))
        widget = GitHubAccountWidget(service=Mock())
        self.addCleanup(widget.deleteLater)
        widget.current_account = account

        def attribute():
            reinitialize.publish("function", function_id="function-a")
            return 1

        with patch.object(QMessageBox, "information"):
            widget._start_package_preparation("attribute", attribute, account)
            loop = QEventLoop()
            timer = QTimer()
            timer.timeout.connect(
                lambda: loop.quit() if widget.package_worker is None else None
            )
            timer.start(10)
            self.addCleanup(timer.stop)
            deadline = QTimer()
            deadline.setSingleShot(True)
            deadline.timeout.connect(loop.quit)
            deadline.start(3000)
            self.addCleanup(deadline.stop)
            loop.exec()

        self.assertIsNone(widget.package_worker)
        self.assertEqual([get_ident()], notification_threads)

    def test_regular_startup_schedules_saved_session_check_without_blocking(self):
        import main

        with (
            patch("main.initialize_application"),
            patch("main.install_exception_logging"),
            patch("main._apply_taskbar_identity"),
            patch("threading.Thread"),
            patch("PySide6.QtWidgets.QApplication"),
            patch("PySide6.QtCore.QTimer.singleShot") as schedule,
            patch("services.theme_service.ThemeService"),
            patch("ui.app_icon.load_app_icon"),
            patch("ui.views.main_window.MainWindow") as window,
        ):
            with self.assertRaises(SystemExit):
                main.main()
        window.return_value.show.assert_called_once_with()
        schedule.assert_called_once_with(
            0, window.return_value.telegram_remote_control_view.github_account.restore_session,
        )
        window.return_value.telegram_remote_control_view.github_account.restore_session.assert_not_called()

    def test_registered_client_is_default_but_forks_can_override_without_persistence(self):
        from services.application_info import GITHUB_OAUTH_CLIENT_ID

        service = Mock()
        widget = GitHubAccountWidget(service=service)
        self.addCleanup(widget.deleteLater)
        self.assertEqual("Ov23liEXc2Px1TtXAbpw", GITHUB_OAUTH_CLIENT_ID)
        self.assertEqual(GITHUB_OAUTH_CLIENT_ID, widget.client_id.text())
        widget.client_id.setText("fork_client")
        self.assertEqual("fork_client", widget.client_id.text())
        service.sign_in.assert_not_called()
        other = GitHubAccountWidget(service=service)
        self.addCleanup(other.deleteLater)
        self.assertEqual(GITHUB_OAUTH_CLIENT_ID, other.client_id.text())

    def test_worker_does_not_leak_credential_errors(self):
        service = Mock()
        service.restore.side_effect = OSError("secret fixture_token")
        worker = _AuthWorker(service, "restore", Event())
        results = []
        worker.signals.finished.connect(lambda account, error: results.append((account, error)))
        worker.run()
        self.assertEqual(1, len(results))
        self.assertIsNone(results[0][0])
        self.assertNotIn("fixture_token", results[0][1])


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE")
    raise SystemExit(0 if result.wasSuccessful() else 1)
