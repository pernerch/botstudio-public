import faulthandler
import os
import sys
import time
import unittest
from threading import Event, Lock, Thread, Timer
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from core.command_control import CommandCancelled, check_cancelled, command_control, run_command
from services.emulator_pause_control import EmulatorPauseControl
from scheduler.models import Event as ScheduleEvent, SchedulerService, Task
from services.runtime_service import RUNTIME_RUNNING, RuntimeService
from services.telegram_control_menu import TelegramControlMenu
from services.telegram_service import TelegramService
from services.watchdog_control_service import WatchdogControlService
from ui.widgets.runtime_status_bar import RuntimeStatusBar
from ui.i18n import translator
from watchdog_app import _handle_telegram_command


class EmulatorPauseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.started = time.monotonic()
        print(f"Test Start: {self.id()}")
        watchdog = Timer(15, lambda: (
            print("TEST RESULT: TIMEOUT", flush=True),
            faulthandler.dump_traceback(), os._exit(124),
        ))
        watchdog.daemon = True
        watchdog.start()
        self.addCleanup(watchdog.cancel)
        self.addCleanup(lambda: print(
            f"Test End: {self.id()} Duration: {time.monotonic() - self.started:.3f}s",
        ))
        self.profiles = [
            {"id": "a", "name": "A", "game": "g", "emulator_instance": 0},
            {"id": "b", "name": "B", "game": "other-game", "emulator_instance": "0"},
            {"id": "c", "name": "C", "game": "g", "emulator_instance": 1},
        ]
        r = self.runtime = object.__new__(RuntimeService)
        r.running = True
        r.status = RUNTIME_RUNNING
        r.last_heartbeat_at = None
        r.logger = Mock()
        r.profile_service = Mock()
        r.profile_service.load_enabled.return_value = self.profiles
        r._stop_event = Event()
        r._stop_lock = Lock()
        r._stop_generation = 0
        r._pause_event = Event()
        r._emulator_control = EmulatorPauseControl()
        r._emulator_control.register([0, 1])
        r._profile_execution_locks = {}
        r._paused_interrupt_resources = set()
        r._health_failures = {"g:emulator:0", "g:emulator:1"}
        r._wake_resources = set(r._health_failures)
        r._startup_pending_profiles = set()
        r._startup_profile_attempts = {"a": 3, "c": 5}
        r._startup_checked_resources = set(r._health_failures)
        r._startup_emulator_deadlines = {key: 999 for key in r._health_failures}
        r._scheduler_profile_signatures = {p["id"]: (p["id"], p["game"], p["emulator_instance"]) for p in self.profiles}
        r._validation_profile_signatures = dict(r._scheduler_profile_signatures)
        r._active_profiles = {("g", 0): "a", ("other-game", "0"): "b", ("g", 1): "c"}
        r._idle_states = {key: {"applied": "close_game"} for key in r._health_failures}
        r._activation_errors = {}
        r._due_schedule_read_errors = set()
        r._select_account_batches = Mock(
            side_effect=lambda profiles, scores, pending_resources=(): scores,
        )
        r.profile_statuses = [{
            "profile_id": p["id"], "profile_name": p["name"],
            "game": {"id": p["game"], "package_name": "app"},
            "emulator_instance": p["emulator_instance"], "state": "Healthy",
            "device_id": f"device-{p['emulator_instance']}", "checked_at": time.monotonic(),
        } for p in self.profiles]
        r.recovery_service = Mock()
        r.variable_service = Mock()
        r._activate_profile_variables = Mock()
        r._ensure_init_event = Mock()
        r.scheduler_service = Mock()
        r.scheduler_service.list_execution_events.return_value = []
        r.scheduler_service.run_due_events.return_value = []
        r.scheduler_service.run_event.return_value = {"status": "idle"}
        r.scheduler_function_root = "unused"
        r.scheduler_runner_factory = None
        r.scheduler_event_limit = 1
        r.scheduler_parallel_workers = 2
        r.interrupt_burst_limit = 1
        r.statistics_service = Mock()
        r.resolve_runnable_profile = lambda profile: profile
        r.heartbeat = Mock()
        r.health_heartbeat = Mock()
        r.adb_runtime_service = Mock()
        r.game_runtime_service = Mock()
        r.emulator_health_service = Mock()
        r._health_failures.clear()
        self.addCleanup(r.stop)

    def test_pause_is_instance_wide_across_games_and_other_emulator_continues(self):
        self.runtime.pause("0")
        self.assertTrue(self.runtime.execution_cancelled(self.profiles[0]))
        self.assertTrue(self.runtime.execution_cancelled(self.profiles[1]))
        self.assertFalse(self.runtime.execution_cancelled(self.profiles[2]))
        self.assertTrue(self.runtime.running)
        self.assertFalse(self.runtime.paused)
        self.assertEqual(RUNTIME_RUNNING, self.runtime.status)
        self.assertFalse(self.runtime.try_acquire_profile_execution(self.profiles[0]))
        self.assertFalse(self.runtime.try_acquire_profile_execution("g:emulator:0"))
        self.assertTrue(self.runtime.try_acquire_profile_execution(self.profiles[2]))
        self.runtime.release_profile_execution(self.profiles[2])

    def test_pause_interrupts_inflight_command_in_under_one_second(self):
        timer = Timer(0.1, lambda: self.runtime.pause(0))
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        with self.runtime._profile_control(self.profiles[0]) as admitted:
            self.assertTrue(admitted)
            with command_control(lambda: self.runtime.execution_cancelled(self.profiles[0])):
                with self.assertRaises(CommandCancelled):
                    run_command([sys.executable, "-c", "import time; time.sleep(30)"], timeout=5)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual("paused", self.runtime.get_status()["paused_emulators"]["0"]["state"])

    def test_resume_waits_for_old_schedule_and_then_invalidates_only_its_instance(self):
        entered, release = Event(), Event()
        def old_schedule(_profile):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("test release missing")
            cancelled.append(self.runtime.execution_cancelled(self.profiles[0]))
            return {"status": "cancelled"}
        cancelled = []
        self.runtime._run_profile_schedule_unlocked = old_schedule
        worker = Thread(target=self.runtime._run_profile_schedule, args=(self.profiles[0],))
        self.addCleanup(lambda: worker.join(2))
        self.addCleanup(release.set)
        worker.start()
        self.assertTrue(entered.wait(1))
        self.runtime.pause(0)
        self.assertEqual("pausing", self.runtime.get_status()["paused_emulators"]["0"]["state"])
        self.runtime.resume(0)
        self.runtime._resume_emulators(self.profiles)
        self.runtime._activate_profile_variables.assert_not_called()
        self.assertTrue(self.runtime.execution_cancelled(self.profiles[0]))
        release.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual([True], cancelled)
        self.runtime._resume_emulators(self.profiles)
        self.assertFalse(self.runtime.execution_cancelled(self.profiles[0]))
        self.assertEqual({"a", "b"}, self.runtime._startup_pending_profiles)
        self.assertEqual({"c"}, set(self.runtime._scheduler_profile_signatures))
        self.assertEqual({"c"}, set(self.runtime._validation_profile_signatures))
        self.assertEqual({("g", 1): "c"}, self.runtime._active_profiles)
        self.assertEqual({"g:emulator:1": {"applied": "close_game"}}, self.runtime._idle_states)
        self.assertEqual({"c": 5}, self.runtime._startup_profile_attempts)
        self.assertEqual(["Resuming", "Resuming", "Healthy"], [
            status["state"] for status in self.runtime.profile_statuses
        ])
        self.assertEqual(2, self.runtime.recovery_service.clear_profile.call_count)
        for profile in self.profiles[:2]:
            self.runtime._activate_profile_variables.assert_any_call(profile, invalidate_live=True)
        self.runtime.heartbeat.stop.assert_not_called()

    def test_resume_waits_for_manual_execution_lock(self):
        self.assertTrue(self.runtime.try_acquire_profile_execution(self.profiles[0]))
        self.runtime.pause(0)
        self.runtime.resume(0)
        self.runtime._resume_emulators(self.profiles)
        self.runtime._activate_profile_variables.assert_not_called()
        self.runtime.release_profile_execution(self.profiles[0])
        self.runtime._resume_emulators(self.profiles)
        self.assertEqual({"a", "b"}, self.runtime._startup_pending_profiles)

    def test_fresh_init_precedes_regular_work_after_resume(self):
        self.runtime.pause(0)
        self.runtime.resume(0)
        self.runtime._resume_emulators(self.profiles)
        order = []
        self.runtime._run_profile_init = Mock(side_effect=lambda _p: (
            order.append("init") or {"status": "success"}
        ))
        self.runtime.scheduler_service.run_due_events.side_effect = lambda *_a, **_kw: (
            order.append("regular") or []
        )
        self.runtime._run_profile_schedule(self.profiles[0])
        self.assertEqual(["init", "regular"], order)
        self.assertNotIn("a", self.runtime._startup_pending_profiles)
        self.assertIn("b", self.runtime._startup_pending_profiles)

    def test_pause_all_then_resume_one_keeps_other_emulator_paused(self):
        self.runtime.pause()
        self.assertTrue(all(self.runtime.execution_cancelled(p) for p in self.profiles))
        self.runtime.resume(0)
        self.runtime._resume_emulators(self.profiles)
        self.assertFalse(self.runtime.execution_cancelled(self.profiles[0]))
        self.assertTrue(self.runtime.execution_cancelled(self.profiles[2]))
        self.runtime.resume()
        self.runtime._resume_emulators(self.profiles)
        self.assertFalse(any(self.runtime.execution_cancelled(p) for p in self.profiles))
        self.assertEqual({"a", "b", "c"}, self.runtime._startup_pending_profiles)

    def test_new_pause_supersedes_pending_resume(self):
        self.runtime.pause(0)
        self.runtime.resume(0)
        with self.runtime._emulator_control.operation(1):
            self.runtime.pause(0)
        self.runtime._resume_emulators(self.profiles)
        self.runtime._activate_profile_variables.assert_not_called()
        self.assertTrue(self.runtime.execution_cancelled(self.profiles[0]))

    def test_pause_during_reset_cannot_be_overwritten_by_resume_completion(self):
        self.runtime.pause(0)
        self.runtime.resume(0)
        self.runtime._activate_profile_variables.side_effect = lambda *_a, **_kw: self.runtime.pause(0)
        self.runtime._resume_emulators(self.profiles)
        self.assertTrue(self.runtime.execution_cancelled(self.profiles[0]))
        self.assertEqual("paused", self.runtime.get_status()["paused_emulators"]["0"]["state"])

    def test_failed_resume_is_visible_and_stays_paused_until_explicit_retry(self):
        self.runtime.pause(0)
        self.runtime.resume(0)
        self.runtime._activate_profile_variables.side_effect = OSError("read-only store")
        self.runtime._resume_emulators(self.profiles)
        pause = self.runtime.get_status()["paused_emulators"]["0"]
        self.assertEqual("paused", pause["state"])
        self.assertIn("read-only store", pause["error"])
        self.runtime.logger.error.assert_called_once()
        self.runtime._activate_profile_variables.side_effect = None
        self.runtime._resume_emulators(self.profiles)
        self.assertTrue(self.runtime.execution_cancelled(self.profiles[0]))
        self.runtime.resume(0)
        self.runtime._resume_emulators(self.profiles)
        self.assertFalse(self.runtime.execution_cancelled(self.profiles[0]))

    def test_paused_validation_idle_recovery_health_and_scheduler_are_excluded(self):
        self.runtime.pause(0)
        self.runtime._apply_idle_behavior = Mock(return_value=None)
        self.runtime._validate_profile = Mock(side_effect=lambda p: next(
            s for s in self.runtime.profile_statuses if s["profile_id"] == p["id"]
        ))
        self.runtime._run_schedules_parallel = Mock()
        self.runtime.heartbeat_once()
        self.assertEqual(["c"], [call.args[0]["id"] for call in self.runtime._validate_profile.call_args_list])
        self.assertEqual(["c"], [call.args[0]["id"] for call in self.runtime._apply_idle_behavior.call_args_list])
        self.assertEqual(["c"], [p["id"] for _s, p in self.runtime._run_schedules_parallel.call_args.args[0]])
        self.runtime.check_execution_health()
        self.assertEqual(1, self.runtime.adb_runtime_service.adb.is_responsive.call_count)
        self.assertEqual("paused", self.runtime._run_profile_schedule(self.profiles[0])["status"])

    def test_health_pause_is_not_reported_as_crash(self):
        def unavailable(*args, **kwargs):
            self.runtime.pause(0)
            return False
        self.runtime.adb_runtime_service.adb.is_responsive.side_effect = unavailable
        self.runtime.check_execution_health()
        self.assertNotIn("g:emulator:0", self.runtime._health_failures)

    def test_real_health_failure_cancels_functions_but_does_not_block_recovery_commands(self):
        self.runtime._health_failures.add("g:emulator:0")
        self.assertTrue(self.runtime.execution_cancelled(self.profiles[0]))
        self.assertFalse(self.runtime.control_cancelled(self.profiles[0]))
        self.runtime._apply_idle_behavior = Mock(return_value=None)
        def recovering(profile):
            check_cancelled()
            return {
                "profile_id": profile["id"], "state": "Recovering",
                "emulator_instance": profile["emulator_instance"], "message": "Recovery running",
            }
        self.runtime._validate_profile = Mock(side_effect=recovering)
        self.runtime._run_schedules_parallel = Mock()
        self.runtime.heartbeat_once()
        self.assertEqual(["Recovering", "Queued", "Recovering"], [
            status["state"] for status in self.runtime.profile_statuses
        ])
        self.assertEqual(2, self.runtime._validate_profile.call_count)
        self.runtime.pause(0)
        self.assertTrue(self.runtime.control_cancelled(self.profiles[0]))
        self.assertFalse(self.runtime.control_cancelled(self.profiles[2]))

    def test_instance_cycle_cannot_finish_another_instances_pending_resume(self):
        self.runtime.pause()
        self.runtime.resume()
        self.runtime._resume_emulators(self.profiles[:2])
        self.assertFalse(self.runtime.automation_paused(self.profiles[0]))
        self.assertTrue(self.runtime.automation_paused(self.profiles[2]))
        self.assertEqual("resuming", self.runtime.get_status()["paused_emulators"]["1"]["state"])

    def test_pause_during_validation_keeps_profile_visible_and_continues_other_instance(self):
        self.runtime._apply_idle_behavior = Mock(return_value=None)
        def validate(profile):
            if profile["id"] == "a":
                self.runtime.pause(0)
            check_cancelled()
            return {
                "profile_id": profile["id"], "emulator_instance": profile["emulator_instance"],
                "state": "Healthy", "checked_at": time.monotonic(),
            }
        self.runtime._validate_profile = Mock(side_effect=validate)
        self.runtime._run_schedules_parallel = Mock()
        self.runtime.heartbeat_once()
        self.assertEqual(["Paused", "Paused", "Healthy"], [
            status["state"] for status in self.runtime.get_status()["profiles"]
        ])
        self.assertEqual(["c"], [
            profile["id"]
            for status, profile in self.runtime._run_schedules_parallel.call_args.args[0]
        ])

    def test_resume_rereads_health_startup_screen_and_profile_init(self):
        self.runtime.pause(0)
        self.runtime.resume(0)
        self.runtime._resume_emulators(self.profiles)
        self.runtime.emulator_health_service.validate.return_value = {
            "state": "Healthy", "message": "Ready", "device_id": "fresh-device",
        }
        self.runtime.game_runtime_service.validate.return_value = {
            "state": "Healthy", "package_name": "app",
        }
        self.runtime.game_service = Mock()
        self.runtime.game_service.get_game_config.return_value = {"startup_screen": "startup"}
        self.runtime._run_control_function = Mock(return_value={"outcome": "success"})
        status = self.runtime._validate_profile(self.profiles[0])
        self.assertEqual("Healthy", status["state"])
        self.runtime.emulator_health_service.validate.assert_called_once()
        self.runtime.game_runtime_service.validate.assert_called_once()
        self.runtime._run_control_function.assert_called_once_with(self.profiles[0], "startup")
        self.runtime._run_profile_schedule(self.profiles[0])
        init_calls = [
            call for call in self.runtime.scheduler_service.run_event.call_args_list
            if "event" in call.kwargs
        ]
        self.assertEqual(1, len(init_calls))
        self.assertIs(self.runtime._ensure_init_event.return_value, init_calls[0].kwargs["event"])
        self.assertNotIn("a", self.runtime._startup_pending_profiles)
        self.runtime.scheduler_service.prepare_profile_start.assert_called_once_with("g", "a")

    def test_cancellation_does_not_start_or_rewrite_later_due_events(self):
        scheduler = object.__new__(SchedulerService)
        scheduler.logger = Mock()
        events = [
            ScheduleEvent(
                game_id="g", profile_id="a", name=f"Event {index}",
                enabled=True, tasks=[Task(function_ids=["function"])],
            )
            for index in range(2)
        ]
        untouched = events[1].to_dict()
        scheduler.list_execution_events = Mock(return_value=events)
        scheduler.clear_event_cancellation = Mock()
        scheduler.run_event = Mock(side_effect=lambda *_args, **_kwargs: (
            self.runtime.pause(0) or {"status": "cancelled"}
        ))
        scheduler.run_due_events(
            "g", profile=self.profiles[0], persist=False, limit=None,
            cancellation_checker=lambda: self.runtime.execution_cancelled(self.profiles[0]),
        )
        scheduler.run_event.assert_called_once()
        self.assertEqual(untouched, events[1].to_dict())

    def test_invalid_instances_do_not_change_state(self):
        for value in (True, -1, "bad", 99):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.runtime.pause(value)
        self.assertEqual({}, self.runtime.get_status()["paused_emulators"])

    def test_stop_during_pending_resume_does_not_reactivate_emulators(self):
        self.runtime.pause()
        self.runtime.resume()
        self.runtime.stop()
        self.runtime._resume_emulators(self.profiles)
        self.runtime._activate_profile_variables.assert_not_called()
        self.assertFalse(self.runtime.running)

    def test_explicit_stop_allows_maintenance_to_acquire_previously_paused_resource(self):
        self.runtime.pause(0)
        self.runtime.stop()
        self.assertTrue(self.runtime.try_acquire_profile_execution(self.profiles[0]))
        self.runtime.release_profile_execution(self.profiles[0])
        self.assertEqual({}, self.runtime.get_status()["paused_emulators"])

    def test_gui_menu_targets_individual_instances_and_all_without_disk_io(self):
        bar = RuntimeStatusBar(self.runtime)
        bar.timer.stop()
        self.addCleanup(bar.deleteLater)
        bar._refresh_pause_menu()
        actions = [action for action in bar.pause_menu.actions() if not action.isSeparator()]
        self.assertEqual(4, len(actions))
        actions[2].trigger()
        self.assertTrue(self.runtime.execution_cancelled(self.profiles[0]))
        self.assertFalse(self.runtime.execution_cancelled(self.profiles[2]))
        bar._refresh_pause_menu()
        actions = [action for action in bar.pause_menu.actions() if not action.isSeparator()]
        actions[2].trigger()
        self.assertEqual("resuming", self.runtime.get_status()["paused_emulators"]["0"]["state"])
        self.runtime.profile_service.load_enabled.assert_not_called()
        self.runtime._activate_profile_variables.assert_not_called()

    def test_ipc_pause_bypasses_busy_worker_and_resume_is_deferred(self):
        service = WatchdogControlService(self.runtime)
        self.addCleanup(service.deleteLater)
        service._active_scheduler_operation = "busy-maintenance"
        result = service.handle_request("runtime.pause", {"instance": "0"})
        self.assertTrue(result["accepted"])
        result = service.handle_request("runtime.resume", {"instance": 0})
        self.assertEqual("resuming", result["state"])
        self.runtime._activate_profile_variables.assert_not_called()
        self.assertTrue(self.runtime.execution_cancelled(self.profiles[0]))
        with self.assertRaises(ValueError):
            service.handle_request("runtime.pause", {"instance": 0, "force": True})

    def test_german_dropdown_and_compact_paused_counter(self):
        with patch.object(translator, "LANG", "de"):
            bar = RuntimeStatusBar(self.runtime)
            bar.timer.stop()
            self.addCleanup(bar.deleteLater)
            bar._refresh_pause_menu()
            self.assertEqual("Alle Emulatoren pausieren", bar.pause_menu.actions()[0].text())
            self.runtime.pause(0)
            bar.refresh()
            self.assertEqual("Pause (1)", bar.btn_pause.text())
            self.assertIn("#FDC601", bar.btn_pause.styleSheet())
            bar._refresh_pause_menu()
            self.assertIn("neu initialisieren", bar.pause_menu.actions()[3].text())

    def test_telegram_has_individual_and_all_commands_with_confirmation(self):
        control = Mock()
        control.runtime_command.return_value = {"accepted": True}
        for command, ipc in (("runtime_pause", "runtime.pause"), ("runtime_resume", "runtime.resume")):
            self.assertIn(command, TelegramService.CONFIRM_COMMANDS)
            _handle_telegram_command(command, ["0"], control)
            control.runtime_command.assert_called_with(ipc, {"instance": "0"})
            _handle_telegram_command(command, ["all"], control)
            control.runtime_command.assert_called_with(ipc, {})
            control.runtime_command.reset_mock()
            self.assertIn("Usage", _handle_telegram_command(command, ["-1"], control))
            control.runtime_command.assert_not_called()
        menu = TelegramControlMenu(control, Mock())
        self.assertIn(("Pause all emulators", "runtime_pause all"), menu.handle("runtime", []).buttons)
        control.runtime_command.return_value = {"emulators": [{
            "instance": 0, "running": True, "automation": "paused",
        }]}
        response = menu.handle("emulators", [])
        self.assertIn("automation: paused", response.text)
        self.assertIn("runtime_resume 0", [command for _label, command in response.buttons])


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(EmulatorPauseTests),
    )
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE")
    sys.exit(0 if result.wasSuccessful() else 1)
