import faulthandler
import logging
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event, Lock, Thread, Timer
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.command_control import CommandCancelled, command_control, run_command
from models.execution_context import ExecutionContext
from services.function_runner import FunctionRunner
from services.emulator_health_service import EmulatorHealthService
from services.runtime_service import RUNTIME_ERROR, RUNTIME_PAUSED, RUNTIME_STOPPED, RuntimeService
from services.telegram_service import TelegramService


class RuntimeResponsivenessTests(unittest.TestCase):
    def setUp(self):
        self.started = time.monotonic()
        faulthandler.dump_traceback_later(10, exit=True)
        self.addCleanup(faulthandler.cancel_dump_traceback_later)
        self.addCleanup(lambda: print(
            f"Test End: {self.id()} Duration: {time.monotonic() - self.started:.3f}s",
        ))
        print(f"Test Start: {self.id()}")
        self.profile = {"id": "p", "game": "g", "emulator_instance": 0}
        self.runtime = object.__new__(RuntimeService)
        self.runtime._stop_event = Event()
        self.runtime._stop_lock = Lock()
        self.runtime._stop_generation = 0
        self.runtime._pause_event = Event()
        self.runtime._health_failures = set()
        self.runtime._wake_resources = set()
        self.runtime.running = True
        self.runtime.status = "Runtime Running"
        self.runtime.logger = Mock()
        self.runtime.get_status = Mock()
        self.runtime.heartbeat = Mock()
        self.runtime.health_heartbeat = Mock()
        self.runtime.emulator_health_service = Mock()
        self.runtime.emulator_health_service.probe_screenshot.return_value = True

    def test_stop_interrupts_running_external_command_in_under_one_second(self):
        timer = Timer(0.15, self.runtime.stop)
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        with command_control(lambda: self.runtime.execution_cancelled(self.profile)):
            with self.assertRaises(CommandCancelled):
                run_command([sys.executable, "-c", "import time; time.sleep(30)"], timeout=5)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(RUNTIME_STOPPED, self.runtime.status)

    def test_runner_binds_its_context_to_running_external_commands(self):
        runner = object.__new__(FunctionRunner)
        runner._run_block = Mock(side_effect=lambda *_: run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"], timeout=5,
        ))
        context = ExecutionContext(
            "p", "f", cancellation_checker=lambda: self.runtime.execution_cancelled(self.profile),
        )
        timer = Timer(0.15, self.runtime.stop)
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        result = runner.run_block(Mock(), context, self.profile)
        self.assertEqual("cancelled", result[2])
        self.assertLess(time.monotonic() - started, 1.0)

    def test_stop_during_initial_cycle_does_not_restart_heartbeat(self):
        self.runtime.adb_runtime_service = Mock()
        self.runtime.profile_service = Mock()
        self.runtime.profile_service.load_enabled.return_value = []
        self.runtime._scheduler_profile_signatures = {}
        self.runtime._validation_profile_signatures = {}
        self.runtime._active_profiles = {}
        self.runtime._idle_states = {}
        self.runtime._startup_profile_attempts = {}
        self.runtime._startup_checked_resources = set()
        self.runtime._startup_emulator_deadlines = {}
        self.runtime._find_activation_errors = Mock(return_value={})
        self.runtime.heartbeat_once = Mock(side_effect=self.runtime.stop)
        self.runtime._start()
        self.runtime.heartbeat.start.assert_not_called()
        self.assertEqual(RUNTIME_STOPPED, self.runtime.status)

    def test_pause_blocks_existing_context_and_stop_releases_it(self):
        self.runtime.pause()
        self.assertEqual(RUNTIME_PAUSED, self.runtime.status)
        finished = Event()
        values = []
        context = ExecutionContext(
            profile_id="p", function_id="f",
            cancellation_checker=lambda: self.runtime.execution_cancelled(self.profile),
        )
        worker = Thread(target=lambda: (values.append(context.is_cancelled()), finished.set()))
        worker.start()
        self.addCleanup(lambda: worker.join(1))
        self.addCleanup(self.runtime.stop)
        self.assertFalse(finished.wait(0.1))
        self.runtime.stop()
        self.assertTrue(finished.wait(0.5))
        self.assertEqual([True], values)

    def test_resume_unblocks_existing_execution_without_restarting_runtime(self):
        self.runtime.pause()
        finished = Event()
        values = []
        worker = Thread(target=lambda: (
            values.append(self.runtime.execution_cancelled(self.profile)), finished.set(),
        ))
        worker.start()
        self.addCleanup(lambda: worker.join(1))
        self.addCleanup(self.runtime.stop)
        self.assertFalse(finished.wait(0.1))
        self.runtime.resume()
        self.assertTrue(finished.wait(0.5))
        self.assertEqual([False], values)

    def test_health_monitor_cancels_only_failed_resource(self):
        self.runtime.profile_statuses = [{
            "profile_id": "p", "emulator_instance": 0,
            "game": {"id": "g", "package_name": "app"}, "device_id": "device",
            "state": "Healthy",
        }]
        self.runtime.adb_runtime_service = Mock()
        self.runtime.adb_runtime_service.adb.is_responsive.return_value = False
        self.runtime.check_execution_health()
        self.assertEqual(RUNTIME_ERROR, self.runtime.status)
        self.assertEqual("Failed", self.runtime.profile_statuses[0]["state"])
        self.assertTrue(self.runtime.execution_cancelled(self.profile))
        self.assertFalse(self.runtime.execution_cancelled({**self.profile, "emulator_instance": 1}))

    def test_crashed_game_cancels_execution_even_when_android_answers(self):
        self.runtime.profile_statuses = [{
            "profile_id": "p", "emulator_instance": 0,
            "game": {"id": "g", "package_name": "app"}, "device_id": "device",
            "state": "Healthy",
        }]
        self.runtime.adb_runtime_service = Mock()
        self.runtime.adb_runtime_service.adb.is_responsive.return_value = True
        self.runtime.game_runtime_service = Mock()
        self.runtime.game_runtime_service.adb.get_package_pid.return_value = None
        self.runtime.check_execution_health()
        self.assertEqual("no_pid", self.runtime.profile_statuses[0]["reason"])
        self.assertTrue(self.runtime.execution_cancelled(self.profile))

    def test_failed_screenshot_cancels_execution_despite_live_game_pid(self):
        self.runtime.profile_statuses = [{
            "profile_id": "p", "emulator_instance": 0,
            "game": {"id": "g", "package_name": "app"}, "device_id": "device",
            "state": "Healthy",
        }]
        self.runtime.adb_runtime_service = Mock()
        self.runtime.adb_runtime_service.adb.is_responsive.return_value = True
        self.runtime.game_runtime_service = Mock()
        self.runtime.game_runtime_service.adb.get_package_pid.return_value = 123
        self.runtime.game_runtime_service.adb.has_anr.return_value = False
        self.runtime.emulator_health_service.probe_screenshot.return_value = False
        self.runtime.check_execution_health()
        self.assertEqual("screenshot_failed", self.runtime.profile_statuses[0]["reason"])
        self.assertTrue(self.runtime.execution_cancelled(self.profile))

    def test_stale_health_and_stopped_runtime_are_not_reported_healthy(self):
        self.runtime.profile_statuses = [{
            "state": "Healthy", "checked_at": time.monotonic() - 16,
        }]
        self.runtime.last_heartbeat_at = None
        status = RuntimeService.get_status(self.runtime)
        self.assertEqual("Unknown", status["profiles"][0]["state"])
        self.runtime.profile_statuses[0]["checked_at"] = time.monotonic()
        self.assertEqual("Healthy", RuntimeService.get_status(self.runtime)["profiles"][0]["state"])
        self.runtime.running = False
        self.assertEqual("Unknown", RuntimeService.get_status(self.runtime)["profiles"][0]["state"])

    def test_recovered_resource_can_run_startup_screen_again(self):
        resource = self.runtime._execution_resource_key(self.profile)
        self.runtime._health_failures.add(resource)
        self.runtime._startup_checked_resources = set()
        self.runtime._startup_pending_profiles = set()
        self.runtime._startup_emulator_deadlines = {}
        self.runtime._validation_profile_signatures = {}
        self.runtime.game_runtime_service = Mock()
        self.runtime.game_runtime_service.get_package_name.return_value = "app"
        self.runtime.game_runtime_service.validate.return_value = {"state": "Healthy"}
        self.runtime.emulator_health_service.validate.return_value = {
            "state": "Healthy", "message": "Emulator healthy", "device_id": "device",
        }
        self.runtime._prepare_profile_transition = Mock(return_value={"state": "Healthy"})
        self.runtime.recovery_service = Mock()
        def startup(profile):
            self.assertFalse(self.runtime.execution_cancelled(profile))
            self.assertEqual("Healthy", self.runtime.profile_statuses[0]["state"])
            return {"state": "Healthy"}
        self.runtime._run_startup_screen = startup
        self.assertEqual("Healthy", self.runtime._validate_profile(self.profile)["state"])

    def test_expected_idle_adb_wakeup_is_info_not_error(self):
        manager = Mock()
        manager.validate_instance.return_value = True
        manager.is_instance_running.return_value = True
        adb = Mock()
        adb.ensure_device.return_value = False
        adb.adb.devices.return_value = ""
        logger = Mock()
        service = EmulatorHealthService(manager, adb, logger)
        result = service.validate(self.profile, expected_stopped=True, expected_wakeup=True)
        self.assertEqual("device_offline", result["reason"])
        self.assertTrue(adb.ensure_device.call_args.kwargs["expected_offline"])
        logger.error.assert_not_called()
        logger.info.assert_called_once()

    def test_init_event_receives_runtime_cancellation(self):
        self.runtime._ensure_init_event = Mock(return_value=SimpleNamespace(function_ids=["f"]))
        self.runtime.scheduler_service = Mock()
        self.runtime.scheduler_function_root = "unused"
        self.runtime.scheduler_runner_factory = None
        self.runtime._run_profile_init(self.profile)
        checker = self.runtime.scheduler_service.run_event.call_args.kwargs["cancellation_checker"]
        self.assertFalse(checker())
        self.runtime.stop()
        self.assertTrue(checker())

    def test_stop_during_heartbeat_cannot_publish_running_again(self):
        self.runtime.last_heartbeat_at = None
        self.runtime.profile_service = Mock()
        self.runtime.profile_service.load_enabled.return_value = []
        self.runtime._profiles_with_due_events = Mock(return_value={})
        self.runtime._activation_errors = {}
        self.runtime._startup_pending_profiles = set()
        self.runtime._active_profiles = {}
        self.runtime._due_schedule_read_errors = set()
        self.runtime._scheduler_capacities = Mock(return_value={})
        self.runtime._run_schedules_parallel = Mock(side_effect=lambda _: self.runtime.stop())
        self.runtime.heartbeat_once()
        self.assertEqual(RUNTIME_STOPPED, self.runtime.status)

    def test_idle_wakeup_preserves_expected_shutdown_during_recovery(self):
        self.runtime._startup_checked_resources = set()
        self.runtime._startup_pending_profiles = set()
        self.runtime._startup_emulator_deadlines = {}
        self.runtime._wake_resources.add(self.runtime._execution_resource_key(self.profile))
        self.runtime.emulator_health_service = Mock()
        self.runtime.emulator_health_service.validate.return_value = {
            "state": "Failed", "message": "Emulator not running", "reason": "instance_stopped",
        }
        self.runtime.game_runtime_service = Mock()
        self.runtime._recover = Mock(side_effect=lambda _, status: {**status, "state": "Recovering"})
        for _ in range(2):
            self.runtime._validate_profile(self.profile)
            self.assertTrue(self.runtime.emulator_health_service.validate.call_args.kwargs["expected_stopped"])
        self.runtime.logger.error.assert_not_called()

    def test_runner_does_not_dispatch_a_cancelled_block(self):
        runner = object.__new__(FunctionRunner)
        runner._run_block = Mock()
        result = runner.run_block(Mock(), ExecutionContext("p", "f", cancelled=True), self.profile)
        self.assertEqual("cancelled", result[2])
        runner._run_block.assert_not_called()

    def test_telegram_acknowledges_before_slow_command_handler(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            def handler(command, arguments):
                self.assertTrue(calls)
                self.assertIn("Request received", calls[0][1]["text"])
                return "Result"
            service = TelegramService(
                token="test", allowed_user_ids={1}, allowed_chat_ids={2},
                command_handler=handler, logger=logging.getLogger("test.telegram"),
                api_call=lambda method, params: calls.append((method, params)),
                clock=Mock(side_effect=[1, 2, 3, 4, 5, 6, 7, 8]),
                state_file=Path(directory) / "offset.json",
            )
            service._execute_command("events", [], 2, 1)
            self.assertEqual("Result", calls[-1][1]["text"])

    def test_pause_does_not_change_a_stopped_runtime(self):
        self.runtime.running = False
        self.runtime.status = RUNTIME_STOPPED
        self.runtime.pause()
        self.assertFalse(self.runtime.paused)
        self.assertEqual(RUNTIME_STOPPED, self.runtime.status)

    def test_pause_does_not_turn_a_completed_command_into_a_timeout(self):
        popen = subprocess.Popen
        def start_process(*args, **kwargs):
            process = popen(*args, **kwargs)
            self.runtime.pause()
            return process
        timer = Timer(0.15, self.runtime.resume)
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        with (
            command_control(lambda: self.runtime.execution_cancelled(self.profile)),
            patch("core.command_control.subprocess.Popen", side_effect=start_process),
        ):
            result = run_command(
                [sys.executable, "-c", "import time; time.sleep(0.01)"],
                timeout=0.05,
            )
        self.assertEqual(0, result.returncode)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_notification_batch_yields_between_chats_without_losing_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            service = TelegramService(
                token="test", allowed_user_ids={1}, allowed_chat_ids={2, 3},
                command_handler=Mock(return_value="Result"),
                logger=logging.getLogger("test.telegram"),
                api_call=lambda method, params: calls.append((method, params)),
                clock=Mock(side_effect=range(1, 20)),
                state_file=Path(directory) / "offset.json",
            )
            service.send_notification("Health alert")
            service._deliver_queued_notification(max_chats=1)
            self.assertEqual(1, len(calls))
            self.assertEqual(1, service._notification_queue.qsize())
            service._deliver_queued_notification(max_chats=1)
            self.assertEqual([2, 3], [params["chat_id"] for _, params in calls])
            self.assertEqual(0, service._notification_queue.qsize())


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(RuntimeResponsivenessTests),
    )
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE")
    sys.exit(0 if result.wasSuccessful() else 1)
