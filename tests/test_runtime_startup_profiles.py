import faulthandler
import os
import sys
import time
import unittest
from threading import Condition, Event, Lock, RLock, Timer
from unittest.mock import Mock, patch

from services.emulator_health_service import EmulatorHealthService, REASON_INSTANCE_STOPPED
from services.logging_service import BotStudioLogger, RecentErrorJournal
from services.runtime_service import RuntimeService
from services.emulator_runtime_workers import EmulatorRuntimeWorkers
from services.runtime_recovery_service import RuntimeRecoveryService


class RuntimeStartupProfilesTests(unittest.TestCase):
    def setUp(self):
        watchdog = Timer(15, lambda: (
            print("TEST RESULT: TIMEOUT", flush=True),
            faulthandler.dump_traceback(), os._exit(124),
        ))
        watchdog.daemon = True
        watchdog.start()
        self.addCleanup(watchdog.cancel)
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
        runtime._select_account_batches = Mock(
            side_effect=lambda profiles, scores, pending_resources=(): scores,
        )
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

    def test_ordinary_cycles_do_not_repeat_init_but_account_switch_does(self):
        runtime = self.runtime
        runtime._continue_account_batch = Mock(return_value=True)
        for profile in (self.profiles[0], self.profiles[0], self.profiles[1], self.profiles[0]):
            runtime._prepare_profile_transition(profile)
            runtime._run_profile_schedule_unlocked(profile)
        self.assertEqual(["a", "b", "a"], self.checked)

    def test_reconnect_preserves_init_and_account_owner(self):
        runtime = self.runtime
        profile = self.profiles[0]
        signature = ("a", "game", "1")
        runtime._scheduler_profile_signatures["a"] = signature
        runtime._validation_profile_signatures["a"] = signature
        runtime._active_profiles[("game", "1")] = "a"
        runtime.recovery_service = Mock()
        runtime.recovery_service.recover_profile.return_value = {
            "state": "Recovering", "message": "ADB reconnected",
        }
        runtime._publish_profile_status = Mock()
        runtime._recover(profile, {"reason": "device_offline"})
        self.assertEqual(signature, runtime._scheduler_profile_signatures["a"])
        self.assertEqual("a", runtime._active_profiles[("game", "1")])
        self.assertNotIn("a", runtime._validation_profile_signatures)
        runtime._activate_profile_variables.assert_called_once_with(profile, invalidate_live=True)

    def test_recovery_receives_actual_trigger_instead_of_progress_message(self):
        runtime = self.runtime
        runtime.recovery_service = Mock()
        runtime.recovery_service.recover_profile.return_value = {
            "state": "Recovering", "message": "Game foreground restored",
        }
        runtime._publish_profile_status = Mock()
        status = {"state": "Recovering", "message": "Game not in foreground"}
        runtime._recover(self.profiles[0], status)
        passed_status = runtime.recovery_service.recover_profile.call_args.args[1]
        self.assertEqual("Game not in foreground", passed_status["message"])

    def test_foreground_restore_of_same_process_does_not_repeat_init(self):
        runtime = self.runtime
        profile = self.profiles[0]
        runtime._scheduler_profile_signatures["a"] = ("a", "game", "1")
        runtime.recovery_service = Mock()
        runtime.recovery_service.recover_profile.return_value = {
            "state": "Recovering", "message": "Game foreground restored",
            "game_init_required": False,
        }
        runtime._publish_profile_status = Mock()
        runtime._recover(profile, {"message": "Game not in foreground"})
        runtime._activate_profile_variables.assert_called_once_with(profile, invalidate_live=True)
        runtime._run_profile_schedule_unlocked(profile)
        self.assertEqual([], self.checked)
        self.assertEqual(["a"], self.tasks)

    def test_actual_emulator_start_invalidates_only_its_instance_init(self):
        runtime = self.runtime
        runtime._scheduler_profile_signatures = {
            p["id"]: (p["id"], p["game"], p["emulator_instance"]) for p in self.profiles
        }
        runtime._validation_profile_signatures = dict(runtime._scheduler_profile_signatures)
        runtime._active_profiles = {("game", "1"): "a", ("game", "2"): "c"}
        runtime.recovery_service = Mock()
        runtime.recovery_service.recover_profile.return_value = {
            "state": "Recovering", "message": "Emulator start requested", "emulator_started": True,
        }
        runtime._publish_profile_status = Mock()
        runtime._recover(self.profiles[0], {"reason": REASON_INSTANCE_STOPPED})
        self.assertEqual({"c"}, set(runtime._scheduler_profile_signatures))
        self.assertEqual({("game", "2"): "c"}, runtime._active_profiles)
        runtime._activate_profile_variables.assert_called_once_with(self.profiles[0], reset=True)

    def test_game_process_restart_requires_only_affected_account_init(self):
        runtime = self.runtime
        runtime._scheduler_profile_signatures = {
            p["id"]: (p["id"], p["game"], p["emulator_instance"]) for p in self.profiles
        }
        runtime._active_profiles = {("game", "1"): "a", ("game", "2"): "c"}
        runtime.recovery_service = Mock()
        runtime.recovery_service.recover_profile.return_value = {
            "state": "Recovering", "message": "Game process changed; Init required",
            "game_init_required": True,
        }
        runtime._publish_profile_status = Mock()
        runtime._recover(self.profiles[0], {"reason": "foreground_mismatch"})
        self.assertEqual({"b", "c"}, set(runtime._scheduler_profile_signatures))
        self.assertEqual({("game", "1"): "a", ("game", "2"): "c"}, runtime._active_profiles)
        runtime._activate_profile_variables.assert_called_once_with(self.profiles[0], reset=True)
        runtime._run_profile_schedule_unlocked(self.profiles[0])
        self.assertEqual(["a"], self.checked)

    def test_recovery_reports_only_accepted_emulator_starts_as_new_session(self):
        manager = Mock()
        recovery = RuntimeRecoveryService(Mock(), Mock(), manager, logger=Mock())
        state = {"emulator_start_attempts": 0}
        for accepted in (False, True):
            with self.subTest(accepted=accepted):
                manager.start_instance.return_value = accepted
                result = recovery._start_emulator(
                    self.profiles[0], state, {}, 3, "Emulator start requested",
                )
                self.assertEqual(accepted, result.get("emulator_started") is True)

    def test_close_game_idle_preserves_init_signature(self):
        runtime = self.runtime
        profile = self.profiles[0]
        runtime.app_config_service = Mock()
        runtime.app_config_service.get_studio_settings.return_value = {"idle_behavior": "close_game"}
        runtime.emulator_manager = Mock()
        runtime.game_runtime_service = Mock()
        runtime._active_profiles[("game", "1")] = "a"
        runtime._scheduler_profile_signatures["a"] = ("a", "game", "1")
        runtime._validation_profile_signatures["a"] = ("a", "game", "1")
        RuntimeService._apply_idle_behavior(runtime, profile)
        self.assertIn("a", runtime._scheduler_profile_signatures)
        self.assertNotIn("a", runtime._validation_profile_signatures)

    def _exercise_independent_instances(self, blocked_stage):
        runtime = self.runtime
        self.profiles[:] = [
            {"id": f"p{i}", "name": f"P{i}", "game": "game", "emulator_instance": i}
            for i in range(6)
        ]
        runtime._startup_pending_profiles = {profile["id"] for profile in self.profiles}
        runtime._profiles_with_due_events = Mock(return_value={
            profile["id"]: (1, "2020") for profile in self.profiles
        })
        runtime._status_lock = RLock()
        runtime._start_lock = Lock()
        runtime.profile_statuses = []
        entered, release, blocked_health = Event(), Event(), Event()
        condition = Condition()
        counts = {str(i): 0 for i in range(6)}
        health_seen = set()
        original_schedules = runtime._run_schedules_parallel

        def validate(profile):
            if blocked_stage == "recovery" and profile["emulator_instance"] == 0:
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("Blocked recovery test was not released")
            return {
                "state": "Healthy", "profile_id": profile["id"],
                "emulator_instance": profile["emulator_instance"],
            }

        def schedules(profiles):
            for _, profile in profiles:
                instance = str(profile["emulator_instance"])
                if blocked_stage == "function" and instance == "0":
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError("Blocked function test was not released")
                with condition:
                    counts[instance] += 1
                    condition.notify_all()
            original_schedules(profiles)

        def health(instance):
            if instance == "0":
                blocked_health.set()
                if not release.wait(5):
                    raise RuntimeError("Blocked health test was not released")
            with condition:
                health_seen.add(instance)
                condition.notify_all()

        runtime._validate_profile = Mock(side_effect=validate)
        runtime._run_schedules_parallel = schedules
        workers = runtime._instance_workers = EmulatorRuntimeWorkers(
            runtime._heartbeat_instance, health, 0.02, runtime.logger,
        )
        def cleanup():
            runtime.stop()
            release.set()
            for pair in workers._workers.values():
                for worker in pair:
                    self.assertTrue(worker.wait_until_idle(2))
        self.addCleanup(cleanup)
        started = time.monotonic()
        runtime._dispatch_instance_cycles()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(entered.wait(2))
        self.assertTrue(blocked_health.wait(2))
        with condition:
            self.assertTrue(condition.wait_for(
                lambda: all(counts[str(i)] >= 3 for i in range(1, 6))
                and all(str(i) in health_seen for i in range(1, 6)), 3,
            ), (counts, health_seen))
        self.assertEqual(0, counts["0"])
        self.assertFalse(workers.idle())
        self.assertTrue({f"p{i}" for i in range(1, 6)}.issubset({
            status["profile_id"] for status in runtime.profile_statuses
        }))
        runtime.stop()
        with patch("services.runtime_service.Path.exists", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "emulator execution is still stopping"):
                runtime.start()
        with self.assertRaisesRegex(RuntimeError, "emulator execution is still stopping"):
            with runtime.content_import_guard():
                self.fail("Import must not enter while emulator callbacks are draining")
        release.set()

    def test_blocked_function_and_health_do_not_delay_five_other_emulator_cycles(self):
        self._exercise_independent_instances("function")

    def test_blocked_recovery_does_not_delay_other_emulator_startup_or_repeated_cycles(self):
        self._exercise_independent_instances("recovery")

    def test_different_games_on_one_physical_instance_remain_serialized(self):
        self.profiles[1]["game"] = "other-game"
        self.runtime.heartbeat_once()
        validated = [call.args[0] for call in self.runtime._validate_profile.call_args_list]
        self.assertEqual(1, sum(str(p["emulator_instance"]) == "1" for p in validated))
        self.assertEqual(1, sum(str(p["emulator_instance"]) == "2" for p in validated))
        self.runtime.heartbeat_once()
        self.assertEqual(["a", "c", "b"], self.checked)

    def test_coordinator_cancels_removed_or_reassigned_profiles_and_exposes_debug_choices(self):
        runtime = self.runtime
        runtime._instance_workers = Mock()
        runtime._status_lock = RLock()
        runtime.profile_statuses = []
        original = dict(self.profiles[0])
        runtime._dispatch_instance_cycles()
        self.assertFalse(runtime.control_cancelled(original))
        self.assertEqual({"a", "b", "c"}, {
            item["profile_id"] for item in runtime._debug_profiles
        })
        self.profiles[0] = {**original, "emulator_instance": "3"}
        runtime._dispatch_instance_cycles()
        self.assertTrue(runtime.control_cancelled(original))
        self.assertFalse(runtime.control_cancelled(self.profiles[0]))
        self.profiles.pop(0)
        runtime._dispatch_instance_cycles()
        self.assertTrue(runtime.execution_cancelled(original))
        self.assertFalse(runtime.control_cancelled(self.profiles[0]))

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
        self.runtime._dispatch_instance_cycles = Mock()
        self.runtime._start()
        self.assertEqual({"a", "b", "c"}, self.runtime._startup_pending_profiles)
        self.runtime._dispatch_instance_cycles.assert_called_once()
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
        runtime._dispatch_instance_cycles = Mock()
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
