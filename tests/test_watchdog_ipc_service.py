import faulthandler
import os
import sys
import threading
import time
import unittest
from uuid import uuid4

from PySide6.QtCore import QCoreApplication

from services.runtime_service import RuntimeService
from services.watchdog_control_service import WatchdogControlService
from services.watchdog_ipc_service import WatchdogIpcClient, WatchdogIpcServer


class WatchdogIpcServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QCoreApplication.instance() or QCoreApplication([])

    def run_request(self, server, command, arguments=None):
        server.start()
        client = WatchdogIpcClient(server.server_name)
        try:
            return client.request(
                command,
                timeout_ms=2_000,
                arguments=arguments,
            )
        finally:
            server.stop()
            self.application.processEvents()

    def test_local_socket_round_trip_dispatches_command(self):
        received = []
        server = WatchdogIpcServer(
            received.append,
            server_name=f"botstudio-watchdog-test-{uuid4().hex}",
        )

        result = self.run_request(server, "status")

        self.assertIsNone(result)
        self.assertEqual(["status"], received)

    def test_server_returns_dispatcher_result(self):
        server = WatchdogIpcServer(
            lambda command: {"command": command, "ok": True},
            server_name=f"botstudio-watchdog-test-{uuid4().hex}",
        )

        result = self.run_request(server, "runtime.start")

        self.assertEqual(
            {"command": "runtime.start", "ok": True},
            result,
        )

    def test_server_dispatches_command_arguments(self):
        server = WatchdogIpcServer(
            lambda command, arguments: {
                "command": command,
                "arguments": arguments,
            },
            server_name=f"botstudio-watchdog-test-{uuid4().hex}",
        )

        result = self.run_request(
            server,
            "event.enable",
            arguments={"event_id": "event-123"},
        )

        self.assertEqual(
            {
                "command": "event.enable",
                "arguments": {"event_id": "event-123"},
            },
            result,
        )

    def test_client_rejects_invalid_timeout(self):
        client = WatchdogIpcClient()

        for timeout in (0, -1, WatchdogIpcClient.MAX_TIMEOUT_MS + 1, 1.5, True):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    client.request("status", timeout_ms=timeout)

        with self.assertRaisesRegex(ValueError, "arguments"):
            client.request("event.enable", arguments=["event-123"])

    def test_control_service_returns_live_runtime_status_and_handles_controls(self):
        class FakeRuntime:
            def __init__(self):
                self.running = False
                self.logger = type(
                    "Logger",
                    (),
                    {"error": lambda *args, **kwargs: None},
                )()

            def get_status(self):
                state = "running" if self.running else "stopped"
                return {"running": self.running, "status": state}

            def get_recovery_state(self):
                return {"safe_to_resume": True, "interrupted_events": []}

            def start(self):
                self.running = True
                return self.get_status()

            def stop(self):
                self.running = False
                return self.get_status()

        runtime = FakeRuntime()
        control = WatchdogControlService(runtime)

        status = control.handle_request("status")
        self.assertEqual(
            {
                "studio": "running",
                "studio_pid": os.getpid(),
                "runtime": {"running": False, "status": "stopped"},
                "runtime_recovery": {
                    "safe_to_resume": True,
                    "interrupted_events": [],
                },
                "control_operation": "idle",
                "last_control_error": None,
            },
            {
                key: value
                for key, value in status.items()
                if key != "error_journal"
            },
        )
        self.assertEqual([], status["error_journal"]["events"])
        self.assertTrue(status["error_journal"]["stream_id"])
        self.assertEqual(
            {"accepted": True, "state": "starting"},
            control.handle_request("runtime.start"),
        )
        self.assertEqual(
            {"accepted": False, "state": "busy"},
            control.handle_request("runtime.start"),
        )

        deadline = time.monotonic() + 2
        while control._operation and time.monotonic() < deadline:
            self.application.processEvents()
            time.sleep(0.001)

        self.assertIsNone(control._operation)
        self.assertTrue(runtime.running)
        self.assertEqual(
            "stopped",
            control.handle_request("runtime.stop")["state"],
        )
        self.assertFalse(runtime.running)

    def test_control_service_rejects_unsupported_command(self):
        control = WatchdogControlService(object())

        with self.assertRaisesRegex(ValueError, "Unsupported command"):
            control.handle_request("studio.quit")

    def test_control_service_rejects_non_object_arguments(self):
        control = WatchdogControlService(object())

        with self.assertRaisesRegex(ValueError, "arguments must be an object"):
            control.handle_request("events.list", [])

    def test_control_service_runs_scheduler_commands_in_worker(self):
        class FakeRuntime:
            logger = type(
                "Logger",
                (),
                {"error": lambda *args, **kwargs: None},
            )()

            def watchdog_scheduler_command(self, command, arguments):
                return {
                    "command": command,
                    "arguments": arguments,
                    "thread_id": threading.get_ident(),
                }

        caller_thread = threading.get_ident()
        control = WatchdogControlService(FakeRuntime())
        accepted = control.handle_request(
            "event.enable",
            {"event_id": "event-123"},
        )
        self.assertTrue(accepted["accepted"])
        self.assertEqual("running", accepted["state"])
        self.assertEqual(
            {"accepted": False, "state": "busy"},
            control.handle_request("events.list"),
        )

        deadline = time.monotonic() + 2
        operation = None
        while time.monotonic() < deadline:
            self.application.processEvents()
            operation = control.handle_request(
                "control.operation.get",
                {"operation_id": accepted["operation_id"]},
            )
            if operation["state"] != "running":
                break
            time.sleep(0.001)

        self.assertEqual("completed", operation["state"])
        self.assertEqual("event.enable", operation["result"]["command"])
        self.assertEqual(
            {"event_id": "event-123"},
            operation["result"]["arguments"],
        )
        self.assertNotEqual(caller_thread, operation["result"]["thread_id"])

    def test_control_service_runs_emulator_maintenance_outside_gui(self):
        class FakeRuntime:
            logger = type("Logger", (), {"error": lambda *args, **kwargs: None})()

            def watchdog_emulator_command(self, command, arguments):
                return {"instance": arguments["instance"],
                        "thread_id": threading.get_ident()}

        control = WatchdogControlService(FakeRuntime())
        caller = threading.get_ident()
        accepted = control.handle_request("emulator.restart", {"instance": 0})
        self.assertTrue(accepted["accepted"])
        self.assertEqual(
            {"accepted": False, "state": "busy"},
            control.handle_request("runtime.start"),
        )
        deadline = time.monotonic() + 2
        operation = {"state": "running"}
        while time.monotonic() < deadline and operation["state"] == "running":
            self.application.processEvents()
            operation = control.handle_request(
                "control.operation.get", {"operation_id": accepted["operation_id"]}
            )
            time.sleep(0.001)
        self.assertEqual("completed", operation["state"])
        self.assertNotEqual(caller, operation["result"]["thread_id"])

    def test_scheduler_operation_history_evicts_oldest_completed_entry(self):
        class FakeRuntime:
            logger = type(
                "Logger",
                (),
                {"error": lambda *args, **kwargs: None},
            )()

            def watchdog_scheduler_command(self, command, arguments):
                return {"command": command}

        control = WatchdogControlService(FakeRuntime())
        control.MAX_STORED_OPERATIONS = 1
        first = control.handle_request("events.list")

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            self.application.processEvents()
            result = control.handle_request(
                "control.operation.get",
                {"operation_id": first["operation_id"]},
            )
            if result["state"] == "completed":
                break
            time.sleep(0.001)

        second = control.handle_request("tasks.list")

        self.assertTrue(second["accepted"])
        with self.assertRaisesRegex(ValueError, "not found"):
            control.handle_request(
                "control.operation.get",
                {"operation_id": first["operation_id"]},
            )

    def test_runtime_rejects_parallel_start_attempts(self):
        entered_start = threading.Event()
        allow_start_to_finish = threading.Event()
        runtime = RuntimeService.__new__(RuntimeService)
        runtime._start_lock = threading.Lock()

        def slow_start():
            entered_start.set()
            allow_start_to_finish.wait(2)
            return {"running": True}

        runtime._start = slow_start
        runtime.stop = lambda: {"running": False}
        results = []
        worker = threading.Thread(target=lambda: results.append(runtime.start()))
        worker.start()

        self.assertTrue(entered_start.wait(1))
        with self.assertRaisesRegex(RuntimeError, "already in progress"):
            runtime.start()

        allow_start_to_finish.set()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertEqual([{"running": True}], results)


if __name__ == "__main__":
    started_at = time.monotonic()
    print("TEST START: watchdog IPC", flush=True)
    faulthandler.dump_traceback_later(20, exit=True)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(
        WatchdogIpcServiceTests
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
