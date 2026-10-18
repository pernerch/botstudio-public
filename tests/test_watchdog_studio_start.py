import faulthandler
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from threading import Timer
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QApplication

import watchdog_app
from services.watchdog_autostart_service import WatchdogAutoStartService
from ui.views.telegram_remote_control_view import TelegramRemoteControlView, _WatchdogStartOperation


class WatchdogStudioStartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def dispose_view(self, view):
        view._watchdog_start_operation = None
        view.close()
        view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def operation(self, enabled, auto_start):
        config = Mock()
        config.get_studio_settings.return_value = {
            "telegram_enabled": enabled, "watchdog_auto_start": auto_start,
        }
        startup = Mock()
        worker = _WatchdogStartOperation(config, startup, Path("project"), 123, Path("working"))
        completed = []
        worker.signals.completed.connect(lambda *args: completed.append(args))
        worker.run()
        return startup, completed

    def test_enabled_telegram_starts_watchdog_without_windows_autostart(self):
        startup, completed = self.operation(True, False)
        startup.ensure_running.assert_called_once_with(Path("project"), 123, Path("working"))
        startup.set_registered.assert_not_called()
        self.assertEqual([(True, "")], completed)

    def test_disabled_telegram_without_autostart_does_not_start(self):
        startup, completed = self.operation(False, False)
        startup.ensure_running.assert_not_called()
        self.assertEqual([(True, "")], completed)

    def test_autostart_still_starts_watchdog_without_telegram(self):
        startup, _ = self.operation(False, True)
        startup.ensure_running.assert_called_once()

    def test_worker_surfaces_launch_failure(self):
        config = Mock()
        config.get_studio_settings.return_value = {"telegram_enabled": True, "watchdog_auto_start": False}
        startup = Mock()
        startup.ensure_running.side_effect = OSError("Launch failed")
        worker = _WatchdogStartOperation(config, startup, Path("project"), 123, Path("working"))
        completed = []
        worker.signals.completed.connect(lambda *args: completed.append(args))
        worker.run()
        self.assertEqual([(False, "Launch failed")], completed)

    def test_existing_watchdog_is_not_launched_again(self):
        service = WatchdogAutoStartService(registry=Mock())
        with patch.object(service, "is_running", return_value=True), patch.object(service, "launch") as launch:
            self.assertFalse(service.ensure_running("unused", 123))
        launch.assert_not_called()

    def test_launch_attaches_current_studio_without_changing_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.py").write_text("", encoding="utf-8")
            (root / "watchdog_app.py").write_text("", encoding="utf-8")
            service = WatchdogAutoStartService(registry=Mock(), lock_path=root / "watchdog.lock")
            with patch.object(service, "launch") as launch, patch.object(
                service, "is_running", side_effect=[False, True],
            ), patch.object(service, "set_registered") as register:
                self.assertTrue(service.ensure_running(root, 123, root))
            command, working = launch.call_args.args
            self.assertEqual("123", command[command.index("--studio-pid") + 1])
            self.assertIn(str(root.resolve() / "main.py"), command)
            self.assertEqual(root.resolve(), working)
            register.assert_not_called()

    def test_early_process_exit_is_reported_instead_of_success(self):
        service = WatchdogAutoStartService(registry=Mock())
        with patch.object(service, "is_running", return_value=False), patch.object(
            service, "build_commands", return_value=(["startup"], ["immediate"]),
        ), patch.object(service, "launch") as launch:
            launch.return_value.poll.return_value = 1
            with self.assertRaisesRegex(RuntimeError, "exit code 1"):
                service.ensure_running("project", 123)

    def test_readiness_wait_is_bounded(self):
        service = WatchdogAutoStartService(registry=Mock())
        with patch.object(service, "is_running", return_value=False), patch.object(
            service, "build_commands", return_value=(["startup"], ["immediate"]),
        ), patch.object(service, "launch"), patch(
            "services.watchdog_autostart_service.time.monotonic", side_effect=[0.0, 6.0],
        ):
            with self.assertRaisesRegex(TimeoutError, "5 seconds"):
                service.ensure_running("project", 123)

    def test_view_queues_worker_without_launching_in_gui_thread(self):
        with patch("ui.views.telegram_remote_control_view.AppConfigService"), patch(
            "ui.views.telegram_remote_control_view.TelegramSettingsService"
        ) as settings, patch(
            "ui.views.telegram_remote_control_view.WatchdogAutoStartService"
        ) as startup, patch("ui.views.telegram_remote_control_view.get_logger"), patch(
            "ui.views.telegram_remote_control_view.QThreadPool"
        ) as pool:
            settings.return_value.load.return_value = {
                "enabled": True, "auto_start": False, "allowed_user_ids": "1",
                "allowed_chat_ids": "1", "token_configured": True,
            }
            view = TelegramRemoteControlView()
            self.addCleanup(self.dispose_view, view)
            from ui.widgets.github_account_widget import GitHubAccountWidget
            self.assertEqual([view.github_account], view.findChildren(GitHubAccountWidget))
            self.assertIs(view.github_account.parentWidget(), view.scroll_area.widget())
            view.start_watchdog_if_enabled()
            view.start_watchdog_if_enabled()
            pool.globalInstance.return_value.start.assert_called_once()
            worker = pool.globalInstance.return_value.start.call_args.args[0]
            self.assertIsInstance(worker, _WatchdogStartOperation)
            startup.return_value.ensure_running.assert_not_called()
            view._watchdog_start_completed(False, "Launch failed")
            self.assertIn("Launch failed", view.watchdog_status_label.text())

    def test_save_enabled_without_autostart_schedules_watchdog_start(self):
        with patch("ui.views.telegram_remote_control_view.AppConfigService"), patch(
            "ui.views.telegram_remote_control_view.TelegramSettingsService"
        ) as settings, patch(
            "ui.views.telegram_remote_control_view.WatchdogAutoStartService"
        ) as startup, patch("ui.views.telegram_remote_control_view.get_logger"):
            settings.return_value.load.return_value = {
                "enabled": True, "auto_start": False, "allowed_user_ids": "1",
                "allowed_chat_ids": "1", "token_configured": True,
            }
            view = TelegramRemoteControlView()
            self.addCleanup(self.dispose_view, view)
            with patch.object(view, "start_watchdog_if_enabled") as start:
                view.save_telegram_settings()
            start.assert_called_once()
            startup.return_value.set_registered.assert_called_once_with([], False)

    def test_host_enables_telegram_without_autostart_and_disables_on_reload(self):
        config = Mock()
        config.get_studio_settings.side_effect = [
            {"telegram_settings_configured": True, "telegram_enabled": True,
             "watchdog_auto_start": False, "telegram_allowed_user_ids": "1",
             "telegram_allowed_chat_ids": "1"},
            {"telegram_settings_configured": True, "telegram_enabled": False,
             "watchdog_auto_start": False},
        ]
        with tempfile.TemporaryDirectory() as directory, patch(
            "watchdog_app.QApplication"
        ) as app, patch("watchdog_app._configure_logging"), patch(
            "watchdog_app.QLockFile"
        ) as lock, patch("watchdog_app.WatchdogAutoStartService") as startup, patch(
            "watchdog_app.AppConfigService", return_value=config
        ), patch("watchdog_app.WindowsCredentialService") as credentials, patch(
            "watchdog_app.TelegramService"
        ) as telegram, patch("watchdog_app.WatchdogProcessService") as service, patch(
            "watchdog_app.QTimer"
        ) as timer, patch("watchdog_app.signal.signal"), patch(
            "watchdog_app.QSystemTrayIcon.isSystemTrayAvailable", return_value=True,
        ), patch("watchdog_app.WatchdogTray") as tray, patch(
            "watchdog_app.ThreadPoolExecutor"
        ) as executor:
            executor.return_value.submit.side_effect = lambda callback: callback()
            startup.return_value.lock_path = Path(directory) / "watchdog.lock"
            lock.return_value.tryLock.return_value = True
            credentials.return_value.load.return_value = "synthetic-token"
            app.return_value.exec.side_effect = lambda: (
                timer.return_value.timeout.connect.call_args.args[0]() or 0
            )
            self.assertEqual(0, watchdog_app.main([
                "--working-directory", directory, "--studio-command", "mock-python", "main.py",
            ]))
            telegram.return_value.start.assert_called_once()
            telegram.return_value.stop.assert_called_once()
            credentials.return_value.load.assert_called_once()
            service.return_value.start.assert_called_once_with()
            service.return_value.start_studio.assert_not_called()
            tray.assert_called_once()
            app.return_value.setQuitOnLastWindowClosed.assert_called_once_with(False)

    def test_host_waits_in_real_qt_loop_without_launching_studio(self):
        config = Mock()
        config.get_studio_settings.return_value = {
            "telegram_settings_configured": False,
            "telegram_enabled": False,
        }
        with tempfile.TemporaryDirectory() as directory, patch(
            "watchdog_app.QApplication", return_value=self.app,
        ), patch("watchdog_app._configure_logging"), patch(
            "watchdog_app.WatchdogAutoStartService"
        ) as startup, patch(
            "watchdog_app.AppConfigService", return_value=config,
        ), patch(
            "services.watchdog_process_service.WatchdogIpcClient.request",
            side_effect=OSError("Isolated test has no Studio endpoint"),
        ), patch(
            "services.watchdog_process_service.subprocess.Popen",
            side_effect=AssertionError("Login must not start Studio"),
        ) as process, patch("watchdog_app.signal.signal"), patch(
            "ui.watchdog_tray.QSystemTrayIcon"
        ), patch(
            "watchdog_app.QSystemTrayIcon.isSystemTrayAvailable", return_value=True,
        ):
            startup.return_value.lock_path = Path(directory) / "watchdog.lock"
            QTimer.singleShot(150, self.app.quit)
            self.assertEqual(0, watchdog_app.main([
                "--working-directory", directory,
                "--studio-command", "mock-python", "main.py",
            ]))
            process.assert_not_called()


if __name__ == "__main__":
    faulthandler.enable()
    def timeout():
        print("TEST RESULT: TIMEOUT", flush=True)
        faulthandler.dump_traceback()
        os._exit(2)

    started = time.monotonic()
    print("TEST START: watchdog studio startup", flush=True)
    watchdog = Timer(30, timeout)
    watchdog.daemon = True
    watchdog.start()
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(WatchdogStudioStartTests)
    )
    watchdog.cancel()
    print(f"TEST DURATION: {time.monotonic() - started:.3f}s", flush=True)
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE", flush=True)
    sys.exit(0 if result.wasSuccessful() else 1)
