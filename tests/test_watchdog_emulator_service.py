import faulthandler
import sys
import threading
import time
import unittest
from unittest.mock import Mock

from services.runtime_heartbeat_service import RuntimeHeartbeatService
from services.runtime_service import RuntimeService
from services.watchdog_emulator_service import WatchdogEmulatorService


class WatchdogEmulatorTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Mock()
        self.runtime._start_lock = threading.Lock()
        self.runtime._stop_generation = 0
        self.runtime.running = True
        self.runtime.profile_service.load_enabled.return_value = [
            {"id": "profile-1", "game": "game", "emulator_instance": 0}
        ]
        self.runtime._execution_resource_key.return_value = "game:emulator:0"
        self.runtime.try_acquire_profile_execution.return_value = True
        self.runtime.heartbeat.wait_until_idle.return_value = True
        self.runtime.get_recovery_state.return_value = {"safe_to_resume": True}
        self.runtime.emulator_manager.is_instance_running.side_effect = [
            True, False, True
        ]
        self.runtime.emulator_manager.stop_instance.return_value = True
        self.runtime.emulator_manager.start_instance.return_value = True
        self.runtime.adb_runtime_service.ensure_device.return_value = True

        def stop():
            self.runtime.running = False
            self.runtime._stop_generation += 1

        def start(expected_stop_generation=None):
            self.runtime.running = True

        self.runtime.stop.side_effect = stop
        self.runtime._start.side_effect = start
        self.service = WatchdogEmulatorService(self.runtime)
        self.service.POLL_SECONDS = 0.001
        self.service.TRANSITION_TIMEOUT_SECONDS = 0.01

    def test_restart_stops_waits_and_resumes_runtime(self):
        result = self.service.handle("emulator.restart", {"instance": "0"})
        self.assertTrue(result["runtime_resumed"])
        self.runtime.stop.assert_called_once()
        self.runtime.heartbeat.wait_until_idle.assert_called_once_with(30)
        self.runtime.emulator_manager.stop_instance.assert_called_once_with(0)
        self.runtime.emulator_manager.start_instance.assert_called_once_with(0)
        self.runtime._start.assert_called_once()
        self.assertFalse(self.runtime._start_lock.locked())
        self.runtime.release_profile_execution.assert_called_once()

    def test_stopped_runtime_remains_stopped(self):
        self.runtime.running = False
        result = self.service.handle("emulator.restart", {"instance": 0})
        self.assertFalse(result["runtime_resumed"])
        self.runtime._start.assert_not_called()

    def test_stop_timeout_leaves_emulator_unchanged(self):
        self.runtime.heartbeat.wait_until_idle.return_value = False
        with self.assertRaisesRegex(TimeoutError, "did not become idle"):
            self.service.handle("emulator.restart", {"instance": 0})
        self.runtime.emulator_manager.stop_instance.assert_not_called()
        self.runtime.emulator_manager.start_instance.assert_not_called()
        self.assertFalse(self.runtime._start_lock.locked())

    def test_interrupted_tasks_block_restart(self):
        self.runtime.get_recovery_state.return_value = {"safe_to_resume": False}
        with self.assertRaisesRegex(RuntimeError, "Interrupted tasks"):
            self.service.handle("emulator.restart", {"instance": 0})
        self.runtime.emulator_manager.stop_instance.assert_not_called()
        self.runtime.release_profile_execution.assert_called_once()

    def test_busy_profile_blocks_restart(self):
        self.runtime.try_acquire_profile_execution.return_value = False
        with self.assertRaisesRegex(RuntimeError, "execution is busy"):
            self.service.handle("emulator.restart", {"instance": 0})
        self.runtime.emulator_manager.stop_instance.assert_not_called()

    def test_unconfigured_instance_and_invalid_arguments_do_not_stop_runtime(self):
        for arguments in ({"instance": "1"}, {"instance": "-1"},
                          {"instance": True}, {}, {"instance": 0, "force": True}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.service.handle("emulator.restart", arguments)
        self.runtime.stop.assert_not_called()

    def test_shutdown_failure_does_not_launch_or_resume(self):
        self.runtime.emulator_manager.stop_instance.return_value = False
        with self.assertRaisesRegex(RuntimeError, "shutdown request failed"):
            self.service.handle("emulator.restart", {"instance": 0})
        self.runtime.emulator_manager.start_instance.assert_not_called()
        self.runtime._start.assert_not_called()

    def test_launch_failure_keeps_runtime_stopped_and_releases_locks(self):
        self.runtime.emulator_manager.start_instance.return_value = False
        with self.assertRaisesRegex(RuntimeError, "launch request failed"):
            self.service.handle("emulator.restart", {"instance": 0})
        self.runtime._start.assert_not_called()
        self.assertFalse(self.runtime.running)
        self.assertFalse(self.runtime._start_lock.locked())
        self.runtime.release_profile_execution.assert_called_once()

    def test_shutdown_timeout_does_not_launch(self):
        self.runtime.emulator_manager.is_instance_running.side_effect = None
        self.runtime.emulator_manager.is_instance_running.return_value = True
        with self.assertRaisesRegex(TimeoutError, "did not stop"):
            self.service.handle("emulator.restart", {"instance": 0})
        self.runtime.emulator_manager.start_instance.assert_not_called()

    def test_runtime_resume_exception_returns_to_stopped_state(self):
        self.runtime._start.side_effect = RuntimeError("Runtime validation failed")
        with self.assertRaisesRegex(RuntimeError, "validation failed"):
            self.service.handle("emulator.restart", {"instance": 0})
        self.assertEqual(2, self.runtime.stop.call_count)
        self.assertFalse(self.runtime.running)
        self.assertFalse(self.runtime._start_lock.locked())

    def test_readiness_timeout_does_not_resume(self):
        self.runtime.emulator_manager.is_instance_running.side_effect = None
        self.runtime.emulator_manager.is_instance_running.return_value = False
        with self.assertRaisesRegex(TimeoutError, "ADB-ready"):
            self.service.handle("emulator.restart", {"instance": 0})
        self.runtime._start.assert_not_called()

    def test_explicit_stop_during_maintenance_prevents_resume(self):
        def launch(_instance):
            self.runtime.stop()
            return True
        self.runtime.emulator_manager.start_instance.side_effect = launch
        result = self.service.handle("emulator.restart", {"instance": 0})
        self.assertFalse(result["runtime_resumed"])
        self.runtime._start.assert_not_called()

    def test_resume_start_does_not_clear_a_newer_stop_request(self):
        runtime = RuntimeService.__new__(RuntimeService)
        runtime._stop_lock = threading.Lock()
        runtime._stop_generation = 2
        runtime._stop_event = threading.Event()
        runtime._stop_event.set()
        runtime.get_status = Mock(return_value={"running": False})
        self.assertEqual(
            {"running": False}, runtime._start(expected_stop_generation=1)
        )
        self.assertTrue(runtime._stop_event.is_set())

    def test_list_returns_live_state_and_unique_instances(self):
        self.runtime.emulator_manager.is_instance_running.side_effect = None
        self.runtime.emulator_manager.is_instance_running.return_value = True
        result = self.service.handle("emulators.list", {})
        self.assertEqual([{"instance": 0, "running": True}], result["emulators"])
        self.runtime.stop.assert_not_called()

    def test_heartbeat_wait_tracks_active_callback_and_cancelled_timer(self):
        entered, release = threading.Event(), threading.Event()
        heartbeat = RuntimeHeartbeatService(
            lambda: (entered.set(), release.wait(1)),
            interval_seconds=10,
            logger=Mock(),
        )
        heartbeat.start()
        heartbeat.stop()
        self.assertTrue(heartbeat.wait_until_idle(0.01))
        heartbeat.running = True
        thread = threading.Thread(target=heartbeat._tick)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            heartbeat.stop()
            self.assertFalse(heartbeat.wait_until_idle(0.01))
        finally:
            release.set()
            thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(heartbeat.wait_until_idle(0.01))


if __name__ == "__main__":
    started_at = time.monotonic()
    print("TEST START: watchdog emulator", flush=True)
    faulthandler.dump_traceback_later(20, exit=True)
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(WatchdogEmulatorTests)
    )
    faulthandler.cancel_dump_traceback_later()
    print(f"TEST DURATION: {time.monotonic() - started_at:.3f}s", flush=True)
    print("TEST RESULT: SUCCESS" if result.wasSuccessful()
          else "TEST RESULT: FAILURE", flush=True)
    sys.exit(0 if result.wasSuccessful() else 1)
