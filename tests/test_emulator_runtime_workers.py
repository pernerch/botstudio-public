"""Timer generation safety without live Runtime, stores or emulator commands."""

import faulthandler
import os
import time
import unittest
from threading import Thread, Timer
from unittest.mock import Mock, patch

from services.emulator_runtime_workers import EmulatorRuntimeWorkers
from services.runtime_heartbeat_service import RuntimeHeartbeatService
from services.runtime_recovery_service import RuntimeRecoveryService


class EmulatorRuntimeWorkersTests(unittest.TestCase):
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

    def tearDown(self):
        print(f"TEST END: {self.id()} ({time.monotonic() - self.started:.3f}s)", flush=True)

    def test_cancelled_timer_cannot_enter_after_restart(self):
        callback = Mock()
        heartbeat = RuntimeHeartbeatService(callback, 10, logger=Mock())
        self.addCleanup(heartbeat.stop)
        with patch("services.runtime_heartbeat_service.threading.Timer") as timer:
            heartbeat.start(immediate=True)
            self.assertEqual(0, timer.call_args.args[0])
            old_generation = timer.call_args.kwargs["args"][0]
            heartbeat.stop()
            heartbeat.start(immediate=True)
            heartbeat._tick(old_generation)
            callback.assert_not_called()
            heartbeat._tick(heartbeat._generation)
            callback.assert_called_once()
            self.assertEqual(10, timer.call_args.args[0])

    def test_registry_groups_numeric_and_string_instance_ids_and_stops_removed_instances(self):
        with patch("services.emulator_runtime_workers.RuntimeHeartbeatService") as factory:
            pair = [Mock(running=False), Mock(running=False)]
            factory.side_effect = pair
            workers = EmulatorRuntimeWorkers(Mock(), Mock(), 10, Mock())
            workers.reconcile([0, "0", 0])
            self.assertEqual(2, factory.call_count)
            self.assertEqual({"0"}, set(workers._workers))
            workers.reconcile([])
            for worker in pair:
                worker.start.assert_called_once_with(immediate=True)
                worker.stop.assert_called_once()
            self.assertTrue(workers.idle())

    def test_parallel_launches_keep_configured_stagger_without_holding_lock_during_wait(self):
        recovery = RuntimeRecoveryService(Mock(), Mock(), Mock(), logger=Mock())
        deadlines = []
        with patch("services.runtime_recovery_service.time.monotonic", return_value=100.0):
            threads = [
                Thread(target=lambda: deadlines.append(recovery._reserve_emulator_start(5)))
                for _ in range(6)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(1)
                self.assertFalse(thread.is_alive())
        self.assertEqual([100, 105, 110, 115, 120, 125], sorted(deadlines))


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE", flush=True)
    raise SystemExit(0 if result.wasSuccessful() else 1)
