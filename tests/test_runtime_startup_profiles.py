import faulthandler
import os
import sys
import time
import unittest
from threading import Event, Lock, Timer
from unittest.mock import Mock, patch

from services.emulator_health_service import EmulatorHealthService, REASON_INSTANCE_STOPPED
from services.logging_service import BotStudioLogger, RecentErrorJournal
from services.runtime_service import RuntimeService


class RuntimeStartupProfilesTests(unittest.TestCase):
    def setUp(self):
        self.profiles = [
            {"id": "a", "name": "Account A", "game": "game", "emulator_instance": "1", "priority": 90},
            {"id": "b", "name": "Account B", "game": "game", "emulator_instance": "1", "priority": 50},
            {"id": "c", "name": "Account C", "game": "game", "emulator_instance": "2", "priority": 50},
        ]
        runtime = self.runtime = object.__new__(RuntimeService)
        runtime.profile_service = Mock()
        runtime.profile_service.load_enabled.return_value = self.profiles
        runtime.logger = Mock()
        runtime.running = True
        runtime.status = "Running"
        runtime._startup_pending_profiles = {"a", "b", "c"}
        runtime._startup_profile_attempts = {}
        runtime._startup_checked_resources = set()
        runtime._startup_emulator_deadlines = {}
        runtime._active_profiles = {}
        runtime._idle_states = {}
        runtime._activation_errors = {}
        runtime._due_schedule_read_errors = set()
        runtime._scheduler_profile_signatures = {}
        runtime._validation_profile_signatures = {}
        runtime._stop_event = Event()
        runtime._stop_lock = Lock()
        runtime._stop_generation = 0
        runtime._paused_interrupt_resources = set()
        runtime.adb_runtime_service = Mock()
        runtime.heartbeat = Mock()
        runtime.scheduler_service = Mock()
        runtime.scheduler_service.list_execution_events.return_value = []
        runtime.statistics_service = Mock()
        runtime.variable_service = Mock()
        runtime._activate_profile_variables = Mock()
        runtime._ensure_init_event = Mock()
        runtime._find_activation_errors = Mock(return_value={})
        runtime.resolve_runnable_profile = lambda profile: profile
        runtime.get_status = Mock(return_value={"running": True})
        runtime._apply_idle_behavior = Mock(side_effect=lambda profile: {
            "state": "Healthy", "profile_id": profile["id"], "idle": True,
        })
        runtime._validate_profile = Mock(side_effect=lambda profile: {
            "state": "Healthy", "profile_id": profile["id"],
        })
        self.checked = []
        self.tasks = []
        runtime._run_profile_init = Mock(side_effect=lambda profile: (
            self.checked.append(profile["id"]) or {"status": "success"}
        ))
        runtime.scheduler_function_root = "unused"
        runtime.scheduler_runner_factory = None
        runtime.scheduler_event_limit = 1
        runtime.interrupt_burst_limit = 1
        runtime.scheduler_service.run_due_events.side_effect = lambda game, **kwargs: (
            self.tasks.append(kwargs["profile"]["id"]) or []
        )
        def schedules(profiles):
            for status, profile in profiles:
                status["scheduler"] = runtime._run_profile_schedule_unlocked(profile)
        runtime._run_schedules_parallel = schedules

    def test_all_accounts_checked_before_resource_idle_even_without_due_tasks(self):
        self.runtime.heartbeat_once()
        self.assertEqual(["a", "c"], self.checked)
        self.runtime._apply_idle_behavior.assert_not_called()
        self.runtime.heartbeat_once()
        self.assertEqual(["a", "c", "b"], self.checked)
        self.assertEqual({"b"}, {
            profile["id"] for (profile,), _ in self.runtime._validate_profile.call_args_list[2:]
        })
        self.runtime.heartbeat_once()
        self.assertEqual(["a", "c", "b"], self.checked)
        self.assertTrue(self.runtime._apply_idle_behavior.called)
        self.assertEqual(set(), self.runtime._startup_pending_profiles)
        self.assertEqual(["a", "c", "b"], self.tasks)

    def test_failed_init_remains_pending_and_retries_instead_of_idle(self):
        self.runtime._run_profile_init.side_effect = [
            {"status": "failed"}, {"status": "success"}, {"status": "success"},
        ]
        self.runtime.heartbeat_once()
        self.assertNotIn(
            ("game", "a"),
            [call.args for call in self.runtime.scheduler_service.prepare_profile_start.call_args_list],
        )
        self.assertIn("a", self.runtime._startup_pending_profiles)
        self.assertNotIn("a", self.runtime._scheduler_profile_signatures)
        self.runtime.heartbeat_once()
        self.assertIn("a", self.runtime._startup_pending_profiles)
        self.assertNotIn("b", self.runtime._startup_pending_profiles)
        self.runtime._run_profile_init.side_effect = [{"status": "success"}]
        self.runtime.heartbeat_once()
        self.assertNotIn("a", self.runtime._startup_pending_profiles)
        self.assertNotIn("a", [
            call.args[0]["id"] for call in self.runtime._apply_idle_behavior.call_args_list
        ])

    def test_unscheduled_events_rearmed_on_each_actual_profile_activation(self):
        self.runtime.heartbeat_once()
        self.runtime.heartbeat_once()
        self.assertEqual(3, self.runtime.scheduler_service.prepare_profile_start.call_count)
        self.runtime._scheduler_profile_signatures.pop("a")
        self.runtime._run_profile_schedule_unlocked(self.profiles[0])
        self.runtime.scheduler_service.prepare_profile_start.assert_called_with("game", "a")
        self.assertEqual(4, self.runtime.scheduler_service.prepare_profile_start.call_count)
        self.runtime._run_profile_schedule_unlocked(self.profiles[0])
        self.assertEqual(4, self.runtime.scheduler_service.prepare_profile_start.call_count)

    def test_recovering_emulator_keeps_initial_check_pending(self):
        self.runtime._validate_profile.side_effect = lambda profile: {
            "state": "Recovering", "profile_id": profile["id"],
        }
        self.runtime.heartbeat_once()
        self.assertEqual([], self.checked)
        self.assertEqual({"a", "b", "c"}, self.runtime._startup_pending_profiles)
        self.runtime._apply_idle_behavior.assert_not_called()

    def test_runtime_start_resets_checks_for_all_currently_enabled_profiles(self):
        self.runtime._startup_pending_profiles = set()
        self.runtime.heartbeat_once = Mock()
        self.runtime._start()
        self.assertEqual({"a", "b", "c"}, self.runtime._startup_pending_profiles)
        self.runtime.heartbeat_once.assert_called_once()
        self.runtime.heartbeat.start.assert_called_once()

    def test_startup_check_takes_precedence_over_completed_account_due_work(self):
        self.runtime._startup_pending_profiles = {"b"}
        self.runtime._profiles_with_due_events = Mock(return_value={"a": (20, "2020")})
        self.runtime._active_profiles = {("game", "1"): "a"}
        self.runtime.heartbeat_once()
        self.assertEqual(["b"], self.checked)
        self.assertEqual(["b"], self.tasks)

    def test_stopped_emulator_uses_existing_recovery_path_even_without_due_events(self):
        self.runtime._validate_profile = RuntimeService._validate_profile.__get__(self.runtime)
        self.runtime.emulator_health_service = Mock()
        self.runtime.emulator_health_service.validate.return_value = {
            "state": "Failed", "message": "Emulator stopped", "reason": "emulator_not_running",
        }
        self.runtime.game_runtime_service = Mock()
        self.runtime.game_runtime_service.get_package_name.return_value = "test.package"
        self.runtime._recover = Mock(side_effect=lambda profile, status: {
            **status, "state": "Recovering",
        })
        self.runtime.heartbeat_once()
        self.assertEqual(["a", "c"], [
            call.args[0]["id"] for call in self.runtime._recover.call_args_list
        ])
        self.assertEqual([], self.checked)
        self.runtime._apply_idle_behavior.assert_not_called()

    def test_stop_prevents_initial_task_execution(self):
        self.runtime._stop_event.set()
        self.runtime.try_acquire_profile_execution = Mock()
        result = self.runtime._run_profile_schedule(self.profiles[0])
        self.assertEqual("stopped", result["status"])
        self.runtime.try_acquire_profile_execution.assert_not_called()
        self.assertEqual([], self.checked)

    def test_invalid_profile_does_not_block_other_initial_checks(self):
        self.runtime._activation_errors = {"a": "Invalid profile"}
        self.runtime._activation_error_status = Mock(return_value={
            "state": "Failed", "profile_id": "a", "message": "Invalid profile",
        })
        self.runtime.heartbeat_once()
        self.assertEqual(["b", "c"], self.checked)
        self.assertIn("a", self.runtime._startup_pending_profiles)
        self.assertEqual("Failed", self.runtime.profile_statuses[0]["state"])

    def test_disabled_profiles_are_not_checked_or_kept_as_resource_work(self):
        self.runtime.profile_service.load_enabled.return_value = [self.profiles[1]]
        self.runtime.heartbeat_once()
        self.assertEqual(["b"], self.checked)
        self.runtime.heartbeat_once()
        self.assertEqual(["b"], self.checked)
        self.assertTrue(self.runtime._apply_idle_behavior.called)

    def test_expected_stopped_emulator_is_logged_but_not_added_to_telegram_errors(self):
        logger = object.__new__(BotStudioLogger)
        logger.module = "emulator"
        logger.logger = Mock()
        manager = Mock()
        manager.validate_instance.return_value = True
        manager.is_instance_running.return_value = False
        manager.get_instance_info.return_value = {}
        journal = RecentErrorJournal()
        health = EmulatorHealthService(manager, Mock(), logger=logger)
        with patch("services.logging_service._RECENT_ERROR_JOURNAL", journal):
            result = health.validate(self.profiles[0], expected_stopped=True)
            self.assertEqual(REASON_INSTANCE_STOPPED, result["reason"])
            self.assertEqual("Failed", result["state"])
            self.assertEqual([], journal.snapshot()["events"])
            logger.logger.log.assert_called_once()
            health.validate(self.profiles[0])
            self.assertEqual("Emulator Not Running", journal.snapshot()["events"][0]["event"])

    def test_startup_suppression_covers_boot_wait_on_shared_instance(self):
        runtime = self.runtime
        runtime._validate_profile = RuntimeService._validate_profile.__get__(runtime)
        runtime.emulator_health_service = Mock()
        runtime.emulator_health_service.validate.return_value = {
            "state": "Failed", "message": "Emulator stopped", "reason": REASON_INSTANCE_STOPPED,
        }
        runtime.game_runtime_service = Mock()
        runtime._recover = Mock(return_value={"state": "Recovering"})
        runtime._validate_profile(self.profiles[0])
        runtime._validate_profile(self.profiles[1])
        runtime._validate_profile(self.profiles[2])
        self.assertEqual([True, True, True], [
            call.kwargs["expected_stopped"]
            for call in runtime.emulator_health_service.validate.call_args_list
        ])
        runtime.heartbeat_once = Mock()
        runtime._start()
        runtime._validate_profile(self.profiles[0])
        self.assertTrue(runtime.emulator_health_service.validate.call_args.kwargs["expected_stopped"])

    def test_boot_suppression_expires_and_does_not_hide_later_crash(self):
        runtime = self.runtime
        runtime._validate_profile = RuntimeService._validate_profile.__get__(runtime)
        runtime.emulator_health_service = Mock()
        stopped = {
            "state": "Failed", "message": "Emulator stopped", "reason": REASON_INSTANCE_STOPPED,
        }
        runtime.emulator_health_service.validate.return_value = stopped
        runtime.game_runtime_service = Mock()
        runtime._recover = Mock(return_value={"state": "Recovering"})
        with patch("services.runtime_service.monotonic", return_value=100):
            runtime._validate_profile(self.profiles[0])
        with patch("services.runtime_service.monotonic", return_value=200):
            runtime._validate_profile(self.profiles[1])
        self.assertFalse(runtime.emulator_health_service.validate.call_args.kwargs["expected_stopped"])
        runtime._startup_emulator_deadlines[("game", "1")] = 1000
        runtime.emulator_health_service.validate.return_value = {
            "state": "Failed", "message": "Device offline", "reason": "device_offline",
        }
        with patch("services.runtime_service.monotonic", return_value=210):
            runtime._validate_profile(self.profiles[0])
            runtime.emulator_health_service.validate.return_value = stopped
            runtime._validate_profile(self.profiles[1])
        self.assertFalse(runtime.emulator_health_service.validate.call_args.kwargs["expected_stopped"])

    def test_failed_initial_emulator_start_is_still_reported(self):
        runtime = self.runtime
        runtime._validate_profile = RuntimeService._validate_profile.__get__(runtime)
        runtime.emulator_health_service = Mock()
        runtime.emulator_health_service.validate.return_value = {
            "state": "Failed", "message": "Emulator stopped", "reason": REASON_INSTANCE_STOPPED,
        }
        runtime.game_runtime_service = Mock()
        runtime._recover = Mock(return_value={"state": "Failed", "message": "Manual recovery required"})
        result = runtime._validate_profile(self.profiles[0])
        self.assertEqual("Failed", result["state"])
        runtime.logger.error.assert_called_once()
        self.assertEqual("Emulator Startup Failed", runtime.logger.error.call_args.args[0])

    def test_other_emulator_errors_are_not_suppressed_at_start(self):
        for invalid_instance in (True, False):
            with self.subTest(invalid_instance=invalid_instance):
                logger = object.__new__(BotStudioLogger)
                logger.module = "emulator"
                logger.logger = Mock()
                manager = Mock()
                manager.validate_instance.return_value = not invalid_instance
                manager.is_instance_running.return_value = True
                manager.get_device_id.return_value = "test-device"
                adb = Mock()
                adb.ensure_device.return_value = False
                adb.adb.devices.return_value = ""
                journal = RecentErrorJournal()
                health = EmulatorHealthService(manager, adb, logger=logger)
                with patch("services.logging_service._RECENT_ERROR_JOURNAL", journal):
                    result = health.validate(self.profiles[0], expected_stopped=True)
                self.assertEqual("Failed", result["state"])
                events = journal.snapshot()["events"]
                self.assertEqual(1, len(events))
                self.assertEqual(
                    "Emulator Validation Failed" if invalid_instance else "ADB Device Lost",
                    events[0]["event"],
                )


if __name__ == "__main__":
    def timeout():
        print("TEST RESULT: TIMEOUT", flush=True)
        faulthandler.dump_traceback()
        os._exit(2)

    started = time.monotonic()
    print("TEST START: runtime startup profiles", flush=True)
    watchdog = Timer(30, timeout)
    watchdog.daemon = True
    watchdog.start()
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(RuntimeStartupProfilesTests)
    )
    watchdog.cancel()
    print(f"TEST DURATION: {time.monotonic() - started:.3f}s", flush=True)
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE", flush=True)
    sys.exit(0 if result.wasSuccessful() else 1)
