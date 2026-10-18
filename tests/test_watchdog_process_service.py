import faulthandler
import logging
import sys
import time
import unittest
from unittest.mock import Mock

from services.watchdog_process_service import WatchdogProcessService


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeProcess:
    next_pid = 2000

    def __init__(self, command, **kwargs):
        self.command = command
        self.kwargs = kwargs
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            raise AssertionError("Fake process wait was not bounded by termination")
        return self.returncode


class FakeIpc:
    def __init__(self):
        self.status = {
            "studio": "running",
            "runtime": {"running": False},
            "runtime_recovery": {
                "safe_to_resume": True,
                "interrupted_events": [],
            },
        }
        self.status_error = None
        self.runtime_start_calls = 0
        self.last_command = None
        self.last_arguments = None
        self.control_operation_result = {"enabled": True, "state": "updated"}
        self.on_stop = None

    def request(self, command, timeout_ms, arguments=None):
        self.last_command = command
        self.last_arguments = arguments
        if command == "studio.stop":
            if self.on_stop:
                self.on_stop()
            return {"accepted": True, "state": "stopping"}
        if command == "status":
            if self.status_error is not None:
                raise self.status_error
            return self.status
        if command == "runtime.start":
            self.runtime_start_calls += 1
            self.status["runtime"]["running"] = True
            return {"accepted": True, "state": "starting"}
        if command in {
            "events.list",
            "tasks.list",
            "event.enable",
            "event.disable",
            "event.run_now",
            "emulator.restart",
            "emulators.list",
        }:
            return {
                "accepted": True,
                "state": "running",
                "operation_id": "control-operation-1",
            }
        if command == "control.operation.get":
            return {
                "state": "completed",
                "result": self.control_operation_result,
            }
        raise AssertionError(f"Unexpected IPC command: {command}")


class WatchdogProcessServiceTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.ipc = FakeIpc()
        self.processes = []
        self.notifications = []

        def make_process(command, **kwargs):
            process = FakeProcess(command, **kwargs)
            self.processes.append(process)
            return process

        self.service = WatchdogProcessService(
            ["python.exe", "main.py"],
            "C:\\BotStudio",
            ipc_client_factory=lambda: self.ipc,
            process_factory=make_process,
            logger=logging.getLogger("test.watchdog"),
            health_interval_seconds=5,
            health_timeout_ms=100,
            startup_timeout_seconds=10,
            unhealthy_threshold=2,
            restart_limit=2,
            restart_window_seconds=100,
            backoff_base_seconds=5,
            backoff_max_seconds=10,
            terminate_timeout_seconds=1,
            clock=self.clock,
            notification_handler=self.notifications.append,
        )
        self.service._launch_studio()
        self.ipc.on_stop = lambda: self.processes[-1].terminate()

    def test_watchdog_start_waits_without_launching_studio(self):
        service = WatchdogProcessService(
            ["python.exe", "main.py"], "C:\\BotStudio",
            process_factory=Mock(side_effect=AssertionError("Unexpected Studio launch")),
            health_interval_seconds=0.01,
        )
        self.addCleanup(service.stop)
        service.start()
        self.assertIsNone(service.get_status()["studio_pid"])
        self.assertTrue(service._thread.is_alive())
        service.process_factory.assert_not_called()

    def test_idle_watchdog_attaches_to_manually_started_studio(self):
        attached = FakeProcess(["manual"])
        self.ipc.status["studio_pid"] = attached.pid
        service = WatchdogProcessService(
            ["python.exe", "main.py"], "C:\\BotStudio",
            ipc_client_factory=lambda: self.ipc,
            process_factory=Mock(side_effect=AssertionError("Duplicate Studio")),
            attached_process_factory=lambda pid: attached,
        )
        status = service.monitor_once()
        self.assertEqual(attached.pid, status["studio_pid"])
        self.assertEqual("healthy", status["health"])
        service.process_factory.assert_not_called()
        self.assertEqual("already_running", service.start_studio()["state"])

    def test_close_studio_is_graceful_and_cancels_recovery(self):
        result = self.service.close_studio()
        self.assertTrue(result["accepted"])
        self.assertEqual("studio.stop", self.ipc.last_command)
        self.assertIsNone(self.service.process)
        self.assertFalse(self.processes[0].killed)
        self.clock.advance(20)
        self.service.monitor_once()
        self.assertEqual(1, len(self.processes))

    def test_close_studio_failure_keeps_process_for_retry(self):
        self.ipc.on_stop = None
        self.service.terminate_timeout_seconds = 0.01
        with self.assertRaisesRegex(TimeoutError, "graceful stop"):
            self.service.close_studio()
        self.assertIs(self.processes[0], self.service.process)
        self.assertFalse(self.processes[0].killed)

    def test_pending_graceful_stop_never_triggers_health_recovery(self):
        self.ipc.on_stop = None
        self.service.stop_studio()
        self.ipc.status_error = OSError("Control endpoint closed")
        self.clock.advance(100)
        for _ in range(3):
            status = self.service.monitor_once()
        self.assertEqual("studio_stopping", status["state"])
        self.assertFalse(self.processes[0].terminated)
        self.assertFalse(self.processes[0].killed)
        self.assertFalse(self.service._pending_restart)

    def test_status_does_not_report_dead_pid(self):
        self.processes[0].returncode = 0
        self.assertIsNone(self.service.get_status()["studio_pid"])

    def test_healthy_status_marks_studio_running(self):
        status = self.service.monitor_once()

        self.assertEqual("running", status["state"])
        self.assertEqual("healthy", status["health"])
        self.assertEqual(self.processes[0].pid, status["studio_pid"])

    def test_new_studio_errors_are_notified_once_without_details(self):
        self.ipc.status["error_journal"] = {
            "stream_id": "studio-session",
            "events": [],
        }
        self.service.monitor_once()

        self.ipc.status["error_journal"]["events"].append(
            {
                "sequence": 1,
                "severity": "ERROR",
                "module": "runtime",
                "event": "Runtime Profile Cycle Failed",
                "timestamp": "2026-10-05T10:00:00+00:00",
                "details": "must not be sent",
            }
        )
        self.service.monitor_once()
        self.service.monitor_once()

        self.assertEqual(1, len(self.notifications))
        self.assertIn("Runtime Profile Cycle Failed", self.notifications[0])
        self.assertNotIn("must not be sent", self.notifications[0])

    def test_supervisor_fault_notification_is_deduplicated_until_recovery(self):
        fault = {
            "state": "runtime_recovery_blocked",
            "restarts_last_window": 1,
        }

        self.service._notify_supervisor_fault(fault)
        self.service._notify_supervisor_fault(fault)
        self.service._notify_supervisor_fault({"state": "running"})
        self.service._notify_supervisor_fault(fault)

        self.assertEqual(2, len(self.notifications))

    def test_attached_studio_is_supervised_without_launching_duplicate(self):
        attached = FakeProcess(["python.exe", "main.py"])
        service = WatchdogProcessService(
            ["python.exe", "main.py"],
            "C:\\BotStudio",
            ipc_client_factory=lambda: self.ipc,
            process_factory=lambda command, **kwargs: self.fail(
                "Attached process must not be launched again"
            ),
            logger=logging.getLogger("test.watchdog"),
            clock=self.clock,
            initial_process=attached,
        )

        status = service.monitor_once()

        self.assertEqual("running", status["state"])
        self.assertEqual(attached.pid, status["studio_pid"])

    def test_manual_studio_stop_does_not_schedule_automatic_restart(self):
        status = self.service.stop_studio()

        self.assertEqual("stopping", status["state"])
        self.assertTrue(self.processes[0].terminated)
        self.assertFalse(self.service._pending_restart)
        self.assertEqual("studio_stopped", self.service.monitor_once()["state"])

    def test_manual_studio_restart_relaunches_once(self):
        result = self.service.restart_studio()

        self.assertEqual("restarting", result["state"])
        self.assertTrue(self.processes[0].terminated)
        self.assertEqual(2, len(self.processes))
        self.assertEqual("starting", self.service.get_status()["state"])
        self.service.monitor_once()
        self.assertEqual(2, len(self.processes))

    def test_failed_manual_restart_clears_intentional_exit_marker(self):
        self.ipc.on_stop = None
        self.ipc.request = lambda command, timeout_ms: (
            {"accepted": False} if command == "studio.stop" else {}
        )

        with self.assertRaisesRegex(RuntimeError, "did not accept"):
            self.service.restart_studio()

        self.assertFalse(self.service._intentional_studio_exit)

    def test_runtime_commands_are_forwarded_through_ipc(self):
        result = self.service.runtime_command("runtime.start")

        self.assertEqual({"accepted": True, "state": "starting"}, result)
        self.assertEqual("runtime.start", self.ipc.last_command)

    def test_emulator_restart_returns_operation_without_polling(self):
        result = self.service.runtime_command("emulator.restart", {"instance": "0"})
        self.assertEqual("control-operation-1", result["operation_id"])
        self.assertEqual("emulator.restart", self.ipc.last_command)
        self.assertEqual({"instance": "0"}, self.ipc.last_arguments)

    def test_scheduler_control_waits_for_studio_operation_result(self):
        result = self.service.runtime_command(
            "event.enable",
            {"event_id": "event-123"},
        )

        self.assertEqual(
            {"enabled": True, "state": "updated"},
            result,
        )
        self.assertEqual("control.operation.get", self.ipc.last_command)
        self.assertEqual(
            {"operation_id": "control-operation-1"},
            self.ipc.last_arguments,
        )

    def test_stop_request_requires_studio_ipc_acceptance(self):
        self.ipc.on_stop = None
        self.ipc.request = lambda command, timeout_ms: {
            "accepted": False,
            "state": "unavailable",
        }

        with self.assertRaisesRegex(RuntimeError, "did not accept"):
            self.service.stop_studio()

        self.assertIs(self.processes[0], self.service.process)
        self.assertIsNone(self.processes[0].returncode)

    def test_normal_studio_exit_is_not_restarted(self):
        self.processes[0].returncode = 0

        status = self.service.monitor_once()

        self.assertEqual("studio_stopped", status["state"])
        self.assertFalse(self.service._pending_restart)
        self.assertEqual(1, len(self.processes))

    def test_crash_uses_backoff_before_relaunch(self):
        self.service.monitor_once()
        self.processes[0].returncode = 1

        scheduled = self.service.monitor_once()
        self.assertEqual("restart_backoff", scheduled["state"])
        self.assertEqual(1, scheduled["restarts_last_window"])
        self.assertEqual(1, len(self.processes))

        self.clock.advance(5)
        relaunched = self.service.monitor_once()
        self.assertEqual("running", relaunched["state"])
        self.assertEqual(2, len(self.processes))

    def test_repeated_ipc_failures_restart_unresponsive_studio(self):
        self.ipc.status_error = TimeoutError("socket timed out")
        self.clock.advance(10)

        self.assertEqual("degraded", self.service.monitor_once()["state"])
        status = self.service.monitor_once()

        self.assertEqual("restart_backoff", status["state"])
        self.assertTrue(self.processes[0].terminated)
        self.assertEqual(1, status["restarts_last_window"])

    def test_restart_limit_stops_crash_loop(self):
        self.service.restart_limit = 1
        self.processes[0].returncode = 1
        self.service.monitor_once()
        self.clock.advance(5)
        self.service.monitor_once()
        self.processes[1].returncode = 2

        status = self.service.monitor_once()

        self.assertEqual("restart_limit", status["state"])
        self.assertFalse(self.service._pending_restart)
        self.assertEqual(2, len(self.processes))

    def test_runtime_is_restored_only_when_no_interrupted_work_exists(self):
        self.ipc.status["runtime"]["running"] = True
        self.service.monitor_once()
        self.processes[0].returncode = 1
        self.service.monitor_once()
        self.ipc.status["runtime"]["running"] = False
        self.clock.advance(5)
        self.service.monitor_once()

        status = self.service.monitor_once()

        self.assertEqual(1, self.ipc.runtime_start_calls)
        self.assertEqual("restored", status["runtime_recovery"])
        self.assertEqual("running", status["state"])

    def test_runtime_restore_is_blocked_for_interrupted_event(self):
        self.ipc.status["runtime"]["running"] = True
        self.service.monitor_once()
        self.processes[0].returncode = 1
        self.service.monitor_once()
        self.ipc.status["runtime"]["running"] = False
        self.ipc.status["runtime_recovery"] = {
            "safe_to_resume": False,
            "interrupted_events": [
                {"event_id": "event-1", "running_tasks": [{"task_id": "task-1"}]}
            ],
        }
        self.clock.advance(5)
        self.service.monitor_once()

        status = self.service.monitor_once()

        self.assertEqual(0, self.ipc.runtime_start_calls)
        self.assertEqual("blocked_interrupted_work", status["runtime_recovery"])
        self.assertEqual("runtime_recovery_blocked", status["state"])

    def test_invalid_status_is_not_accepted_as_healthy(self):
        self.ipc.status = {"studio": "running", "runtime": {"running": "yes"}}

        status = self.service.monitor_once()

        self.assertEqual("unhealthy", status["health"])
        self.assertIn("running state is invalid", status["last_error"])

    def test_invalid_limits_are_rejected(self):
        for values in (
            {"restart_limit": 0},
            {"unhealthy_threshold": True},
            {"health_interval_seconds": 0},
            {"backoff_max_seconds": 1, "backoff_base_seconds": 2},
        ):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    WatchdogProcessService(
                        ["python.exe", "main.py"],
                        "C:\\BotStudio",
                        **values,
                    )


if __name__ == "__main__":
    started_at = time.monotonic()
    print("TEST START: watchdog process", flush=True)
    faulthandler.dump_traceback_later(20, exit=True)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(
        WatchdogProcessServiceTests
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    faulthandler.cancel_dump_traceback_later()
    print(
        f"TEST DURATION: {time.monotonic() - started_at:.3f}s",
        flush=True,
    )
    print(
        "TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE",
        flush=True,
    )
    sys.exit(0 if result.wasSuccessful() else 1)
