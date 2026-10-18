import faulthandler
import logging
import os
import sys
import time
import unittest
from threading import Event, Timer
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ui.watchdog_tray import WatchdogTray, indicator_state


def status(state="studio_stopped", pid=None, runtime=None):
    return {
        "state": state,
        "studio_pid": pid,
        "health": "healthy" if pid else "unavailable",
        "runtime_recovery": "not_required",
        "studio_status": {"runtime": runtime or {"running": False, "status": "stopped"}},
    }


class WatchdogTrayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.service = Mock()
        self.service.get_status.return_value = status()
        self.service.start_studio.return_value = {"accepted": True}
        self.service.stop_studio.return_value = {"accepted": True}
        self.service.close_studio.return_value = {"accepted": True}
        tray_patch = patch("ui.watchdog_tray.QSystemTrayIcon")
        tray_patch.start()
        self.addCleanup(tray_patch.stop)
        self.tray = WatchdogTray(self.service, logging.getLogger("test.tray"))
        self.addCleanup(self.tray.close)
        self.tray.timer.stop()
        self.wait_for(lambda: not self.tray._refresh_pending)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.001)
        self.assertTrue(predicate(), "Qt worker signal did not complete within 2 seconds")

    def test_waiting_is_green_and_stop_disabled(self):
        self.assertEqual("ready", self.tray._indicator)
        self.assertTrue(self.tray.start_action.isEnabled())
        self.assertFalse(self.tray.stop_action.isEnabled())

    def test_running_enables_stop_and_disables_start(self):
        self.tray._show_status(status("running", 123))
        self.assertTrue(self.tray.stop_action.isEnabled())
        self.assertFalse(self.tray.start_action.isEnabled())

    def test_recovery_and_failure_colors_include_runtime_health(self):
        for state in ("starting", "restart_backoff", "studio_stopping"):
            self.assertEqual("recovering", indicator_state(status(state, 123)))
        for state in ("restart_limit", "runtime_recovery_blocked", "runtime_recovery_failed"):
            self.assertEqual("failed", indicator_state(status(state)))
        for state, indicator in (("Failed", "failed"), ("Recovering", "recovering")):
            self.assertEqual(indicator, indicator_state(status("running", 123, {
                "running": True, "status": "running", "profiles": [{"state": state}],
            })))

    def test_only_green_pulses_over_branded_icon(self):
        self.assertFalse(self.tray._base_icon.isNull())
        for indicator, changes in (("ready", True), ("recovering", False), ("failed", False)):
            self.tray._indicator = indicator
            self.tray._phase = 5
            first = self.tray._render_icon().pixmap(32, 32).toImage()
            self.tray._phase = 15
            second = self.tray._render_icon().pixmap(32, 32).toImage()
            self.assertEqual(changes, first != second)

    def test_start_and_stop_use_existing_service_contract(self):
        for name in ("start", "stop"):
            self.tray._command(name)
            self.wait_for(lambda: not self.tray._busy and not self.tray._refresh_pending)
        self.service.start_studio.assert_called_once()
        self.service.stop_studio.assert_called_once()

    def test_blocked_command_does_not_block_qt_events(self):
        release = Event()
        started = Event()
        self.addCleanup(release.set)

        def start():
            started.set()
            if not release.wait(2):
                raise TimeoutError("Test command was not released")
            return {"accepted": True}

        self.service.start_studio.side_effect = start
        self.tray._command("start")
        self.assertTrue(started.wait(1))
        ticks = []
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, lambda: ticks.append(True))
        self.wait_for(lambda: bool(ticks))
        self.assertTrue(self.tray._busy)
        release.set()
        self.wait_for(lambda: not self.tray._busy)

    def test_exit_waits_for_graceful_close_before_quitting(self):
        exits = []
        self.tray.quit_requested.connect(lambda: exits.append(True))
        self.tray._command("exit")
        self.wait_for(lambda: bool(exits))
        self.service.close_studio.assert_called_once()

    def test_failed_exit_stays_running_and_reports_error(self):
        exits = []
        self.tray.quit_requested.connect(lambda: exits.append(True))
        self.service.close_studio.side_effect = TimeoutError("Studio did not close")
        self.tray._command("exit")
        self.wait_for(lambda: not self.tray._busy)
        self.assertEqual([], exits)
        self.assertEqual("failed", self.tray._indicator)
        self.tray.tray.showMessage.assert_called_once()
        self.assertTrue(self.tray.exit_action.isEnabled())


if __name__ == "__main__":
    started = time.monotonic()
    print("TEST START: watchdog tray", flush=True)

    def timeout():
        print("TEST RESULT: TIMEOUT", flush=True)
        faulthandler.dump_traceback()
        os._exit(2)

    watchdog = Timer(30, timeout)
    watchdog.daemon = True
    watchdog.start()
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(WatchdogTrayTests)
    )
    watchdog.cancel()
    print(f"TEST DURATION: {time.monotonic() - started:.3f}s", flush=True)
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE", flush=True)
    sys.exit(0 if result.wasSuccessful() else 1)
