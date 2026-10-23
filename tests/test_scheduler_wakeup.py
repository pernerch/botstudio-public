import faulthandler
import os
import time
import unittest
from threading import Event, Lock, Timer
from unittest.mock import Mock, patch

from services.runtime_heartbeat_service import RuntimeHeartbeatService
from services.emulator_runtime_workers import EmulatorRuntimeWorkers
from services.runtime_service import RuntimeService


class SchedulerWakeupTests(unittest.TestCase):
    def setUp(self):
        self.started = time.monotonic()
        print(f"TEST START: {self.id()}", flush=True)
        watchdog = Timer(10, lambda: (
            print("TEST RESULT: TIMEOUT", flush=True),
            faulthandler.dump_traceback(), os._exit(124),
        ))
        watchdog.daemon = True
        watchdog.start()
        self.addCleanup(watchdog.cancel)
        self.addCleanup(lambda: print(
            f"TEST END: {self.id()} duration={time.monotonic() - self.started:.3f}s",
            flush=True,
        ))

    def heartbeat(self, callback):
        worker = RuntimeHeartbeatService(callback, 60, logger=Mock())
        self.addCleanup(lambda: self.assertTrue(worker.wait_until_idle(2)))
        self.addCleanup(worker.stop)
        return worker

    def test_idle_request_runs_without_waiting_for_interval(self):
        called = Event()
        worker = self.heartbeat(called.set)
        worker.start()
        worker.request_tick()
        self.assertTrue(called.wait(1))

    def test_request_during_callback_reruns_serially_without_interval(self):
        entered, release, reran = Event(), Event(), Event()
        calls = []
        guard = Lock()

        def callback():
            self.assertTrue(guard.acquire(blocking=False), "overlapping callbacks")
            try:
                calls.append(time.monotonic())
                if len(calls) == 1:
                    entered.set()
                    if not release.wait(2):
                        raise TimeoutError("test release missing")
                else:
                    reran.set()
            finally:
                guard.release()

        worker = self.heartbeat(callback)
        self.addCleanup(release.set)
        worker.start(immediate=True)
        self.assertTrue(entered.wait(1))
        worker.request_tick()
        worker.request_tick()
        self.assertFalse(reran.is_set())
        release.set()
        self.assertTrue(reran.wait(1))
        worker.stop()
        self.assertTrue(worker.wait_until_idle(1))
        self.assertEqual(2, len(calls))
        worker.logger.error.assert_not_called()

    def test_stop_rejects_wakeup(self):
        callback = Mock()
        worker = self.heartbeat(callback)
        worker.start()
        worker.stop()
        self.assertFalse(worker.request_tick())
        callback.assert_not_called()

    def test_cancelled_timer_cannot_enter_after_immediate_reschedule(self):
        callback = Mock()
        worker = self.heartbeat(callback)
        with patch("services.runtime_heartbeat_service.threading.Timer") as timer:
            worker.start()
            stale = timer.call_args.kwargs["args"]
            worker.request_tick()
            fresh = timer.call_args.kwargs["args"]
            worker._tick(*stale)
            callback.assert_not_called()
            worker._tick(*fresh)
            callback.assert_called_once_with()

    def test_stop_during_callback_cancels_pending_rerun(self):
        entered, release = Event(), Event()
        calls = []

        def callback():
            calls.append(1)
            entered.set()
            if not release.wait(2):
                raise TimeoutError("test release missing")

        worker = self.heartbeat(callback)
        self.addCleanup(release.set)
        worker.start(immediate=True)
        self.assertTrue(entered.wait(1))
        worker.request_tick()
        worker.stop()
        release.set()
        self.assertTrue(worker.wait_until_idle(1))
        self.assertIsNone(worker._timer)
        self.assertEqual([1], calls)
        worker.logger.error.assert_not_called()

    def test_instance_wakeup_does_not_touch_other_instance_or_health(self):
        workers = EmulatorRuntimeWorkers(Mock(), Mock(), 60, Mock())
        first, second, health = Mock(), Mock(), Mock()
        workers._workers = {"1": (first, health), "2": (second, health)}
        workers.request_cycle(1)
        first.request_tick.assert_called_once_with()
        second.request_tick.assert_not_called()
        health.request_tick.assert_not_called()

    def test_runtime_routes_request_to_live_profile_instance(self):
        runtime = object.__new__(RuntimeService)
        runtime.running = True
        runtime.is_stop_requested = Mock(return_value=False)
        runtime.automation_paused = Mock(return_value=False)
        runtime.profile_service = Mock()
        runtime.profile_service.get_profile.return_value = {"id": "p", "emulator_instance": 3}
        runtime._instance_workers = Mock()
        runtime.heartbeat = Mock()
        self.assertTrue(runtime.request_schedule("p"))
        runtime.profile_service.get_profile.assert_called_once_with("p")
        runtime._instance_workers.request_cycle.assert_called_once_with(3)
        runtime.heartbeat.request_tick.assert_not_called()
        runtime.automation_paused.return_value = True
        runtime._instance_workers.reset_mock()
        self.assertFalse(runtime.request_schedule("p"))
        runtime._instance_workers.request_cycle.assert_not_called()

    def test_run_selected_starts_stopped_runtime_and_wakes_instance(self):
        runtime = object.__new__(RuntimeService)
        runtime.running = False
        runtime.logger = Mock()
        runtime.is_stop_requested = Mock(return_value=False)
        runtime.automation_paused = Mock(return_value=False)
        runtime.profile_service = Mock()
        runtime.profile_service.get_profile.return_value = {
            "id": "p", "emulator_instance": 3, "enabled": True,
        }
        runtime._instance_workers = Mock()
        runtime.heartbeat = Mock()
        runtime.start = Mock(side_effect=lambda: setattr(runtime, "running", True))
        self.assertTrue(runtime.request_schedule("p"))
        runtime.start.assert_called_once_with()
        runtime._instance_workers.request_cycle.assert_called_once_with(3)

    def test_run_selected_does_not_start_runtime_for_disabled_profile(self):
        runtime = object.__new__(RuntimeService)
        runtime.running = False
        runtime.logger = Mock()
        runtime.profile_service = Mock()
        runtime.profile_service.get_profile.return_value = {
            "id": "p", "emulator_instance": 3, "enabled": False,
        }
        runtime._instance_workers = Mock()
        runtime.start = Mock()
        self.assertFalse(runtime.request_schedule("p"))
        runtime.start.assert_not_called()
        runtime._instance_workers.request_cycle.assert_not_called()

    def test_run_selected_start_failure_is_reported(self):
        runtime = object.__new__(RuntimeService)
        runtime.running = False
        runtime.logger = Mock()
        runtime.profile_service = Mock()
        runtime.profile_service.get_profile.return_value = {
            "id": "p", "emulator_instance": 3, "enabled": True,
        }
        runtime.start = Mock(side_effect=RuntimeError("still stopping"))
        with self.assertRaises(RuntimeError):
            runtime.request_schedule("p")


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE", flush=True)
    raise SystemExit(0 if result.wasSuccessful() else 1)
