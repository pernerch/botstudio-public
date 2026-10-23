import faulthandler
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event as StopEvent, Timer
from unittest.mock import Mock, patch

from scheduler.account_policy import AccountSchedulingPolicy, AccountWork
from scheduler.models import Event, SchedulerService, Task
from services.runtime_service import RuntimeService


DEFAULTS = {
    "limit_profile_active_time": False,
    "max_active_minutes": 60,
    "scheduling_lookahead_seconds": 120,
    "scheduling_max_wait_minutes": 120,
}


class AccountSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.started = time.monotonic()
        print(f"TEST START: {self.id()}", flush=True)
        timer = Timer(15, lambda: (
            print("TEST RESULT: TIMEOUT", flush=True),
            faulthandler.dump_traceback(), os._exit(124),
        ))
        timer.daemon = True
        timer.start()
        self.addCleanup(timer.cancel)
        self.addCleanup(lambda: print(
            f"TEST END: {self.id()} duration={time.monotonic() - self.started:.3f}s",
            flush=True,
        ))
        self.policy = AccountSchedulingPolicy()
        self.a = {"id": "a", "game": "g", "emulator_instance": 1, "priority": 90}
        self.b = {"id": "b", "game": "g", "emulator_instance": 1, "priority": 50}
        self.profiles = [self.a, self.b]
        self.settings = {"a": dict(DEFAULTS), "b": dict(DEFAULTS)}
        self.now = datetime(2026, 10, 22, tzinfo=timezone.utc)

    def choose(self, a, b, now=1000):
        return self.policy.choose(
            self.profiles, {"a": a, "b": b}, self.settings, now,
        )

    def event(self, seconds=-60, count=1, **kwargs):
        return Event(
            event_id="e", game_id="g", profile_id="a",
            next_due_at=(self.now + timedelta(seconds=seconds)).isoformat(),
            tasks=[Task(function_ids=["f"]) for _ in range(count)], **kwargs,
        )

    def runtime(self):
        runtime = object.__new__(RuntimeService)
        runtime._account_policy = self.policy
        runtime._due_schedule_read_errors = set()
        runtime.logger = Mock()
        runtime.app_config_service = Mock()
        runtime.app_config_service.get_scheduling_settings.side_effect = (
            lambda service, game: dict(DEFAULTS)
        )
        runtime.game_service = Mock()
        runtime.scheduler_service = Mock()
        runtime.scheduler_service.list_events.return_value = []
        return runtime

    def test_equal_work_keeps_priority(self):
        self.assertEqual("a", self.choose(AccountWork(2), AccountWork(2)))

    def test_lower_priority_bundle_can_outweigh_single_high_priority_task(self):
        self.a["priority"] = 70
        self.assertEqual("b", self.choose(AccountWork(1), AccountWork(12, oldest_seconds=1800)))

    def test_minimum_dwell_prevents_ping_pong(self):
        self.policy.activate(self.a, 990)
        self.assertEqual("a", self.choose(AccountWork(1), AccountWork(12, oldest_seconds=10000)))

    def test_manual_request_preempts_priority_and_minimum_dwell(self):
        self.policy.activate(self.a, 999)
        queued = self.event(0, status="queued", enabled=False)
        self.assertEqual("b", self.choose(
            AccountWork(12),
            AccountWork.from_events([queued], self.now, 120),
        ))
        self.assertEqual("manual_request", self.policy.reasons["1"])

    def test_oldest_manual_request_wins_across_accounts(self):
        self.policy.activate(self.a, 999)
        a = AccountWork.from_events([self.event(-1, status="queued")], self.now, 120)
        b = AccountWork.from_events([self.event(-2, status="queued")], self.now, 120)
        self.assertEqual("b", self.choose(a, b))
        self.assertEqual("a", self.choose(b, a))

    def test_manual_request_takes_precedence_over_other_accounts_startup_round(self):
        runtime = self.runtime()
        queued = self.event(0, status="queued")
        queued.profile_id = "b"
        runtime.scheduler_service.list_events.side_effect = (
            lambda game, profile_id: [queued] if profile_id == "b" else []
        )
        selected = runtime._select_account_batches(
            self.profiles, {"a": (1, "2020")}, pending_resources={"1"},
        )
        self.assertEqual({"b"}, set(selected))

    def test_exact_minimum_dwell_boundary_and_post_switch_protection(self):
        self.policy.activate(self.a, 0)
        work = {"a": AccountWork(2), "b": AccountWork(2, oldest_seconds=10000)}
        self.assertEqual("a", self.policy.choose(self.profiles, work, self.settings, 119))
        self.assertEqual("b", self.policy.choose(self.profiles, work, self.settings, 120))
        self.policy.activate(self.b, 120)
        self.policy.served(self.b, 121)
        self.a["priority"] = 1000
        self.assertEqual("b", self.policy.choose(self.profiles, work, self.settings, 239))
        self.assertEqual("a", self.policy.choose(self.profiles, work, self.settings, 240))

    def test_single_task_does_not_preempt_batch_even_with_higher_priority(self):
        self.b["priority"] = 1000
        self.policy.activate(self.a, 0)
        self.assertEqual("a", self.choose(AccountWork(1), AccountWork(1)))

    def test_material_bundle_can_preempt_after_minimum_dwell(self):
        self.a["priority"] = 50
        self.b["priority"] = 90
        self.policy.activate(self.a, 0)
        self.assertEqual("b", self.choose(AccountWork(1), AccountWork(8)))
        self.assertEqual("backlog_benefit_exceeds_switch_cost", self.policy.reasons["1"])

    def test_bundle_may_include_imminent_tasks_but_they_are_discounted(self):
        self.a["priority"] = 50
        self.b["priority"] = 90
        self.policy.activate(self.a, 0)
        self.assertEqual("b", self.choose(
            AccountWork(1), AccountWork(1, upcoming_tasks=4, next_seconds=30),
        ))
        self.a["priority"] = self.b["priority"] = 50
        self.policy = AccountSchedulingPolicy()
        self.assertEqual("a", self.choose(
            AccountWork(2), AccountWork(upcoming_tasks=8, next_seconds=30),
        ))

    def test_expensive_switch_requires_bigger_advantage(self):
        self.a["priority"] = 50
        self.b["priority"] = 70
        self.policy.activate(self.a, 0)
        self.assertEqual("a", self.choose(AccountWork(2), AccountWork(4, switch_seconds=600)))

    def test_maximum_wait_serves_even_extremely_low_priority(self):
        self.a["priority"] = 1000
        self.b["priority"] = 0
        self.policy.activate(self.a, 0)
        self.choose(AccountWork(1), AccountWork(1), now=1000)
        self.assertEqual("b", self.choose(AccountWork(1), AccountWork(1), now=8200))
        self.assertEqual("maximum_wait_reached", self.policy.reasons["1"])

    def test_six_hour_continuous_load_bounds_wait_and_switch_count(self):
        self.a["priority"] = 1000
        self.b["priority"] = 0
        self.policy.activate(self.a, 0)
        current = "a"
        switches = 0
        visits = {"a": [], "b": []}
        for now in range(0, 6 * 3600 + 1, 60):
            target = self.choose(
                AccountWork(2), AccountWork(4, oldest_seconds=now), now,
            )
            if target != current:
                switches += 1
                current = target
                self.policy.activate(self.a if target == "a" else self.b, now)
            visits[target].append(now)
            self.policy.served(self.a if target == "a" else self.b, now)
        self.assertTrue(visits["b"])
        service_times = [0, *visits["b"], 6 * 3600]
        self.assertLessEqual(max(
            later - earlier for earlier, later in zip(service_times, service_times[1:])
        ), 120 * 60 + 120)
        self.assertLessEqual(switches, 8)

    def test_fairness_rotates_three_waiting_accounts(self):
        c = {**self.b, "id": "c", "priority": 0}
        self.profiles.append(c)
        self.settings["c"] = dict(DEFAULTS)
        self.policy.activate(self.a, 0)
        self.policy.served(self.a, 9999)
        work = {identity: AccountWork(1, oldest_seconds=10000) for identity in "abc"}
        self.assertEqual("b", self.policy.choose(self.profiles, work, self.settings, 10000))
        self.policy.activate(self.b, 10000)
        self.policy.served(self.b, 10000)
        self.assertEqual("c", self.policy.choose(self.profiles, work, self.settings, 10121))

    def test_active_limit_yields_without_aborting_running_event(self):
        self.policy.activate(self.a, 0)
        self.settings["a"].update(limit_profile_active_time=True, max_active_minutes=1)
        self.assertEqual("b", self.choose(AccountWork(1), AccountWork(1), now=61))

    def test_no_forced_switch_without_competing_work(self):
        self.policy.activate(self.a, 0)
        self.settings["a"].update(limit_profile_active_time=True, max_active_minutes=1)
        self.assertEqual("a", self.choose(AccountWork(1), AccountWork(), now=10000))

    def test_drained_account_yields_to_due_work(self):
        self.policy.activate(self.a, 990)
        self.assertEqual("b", self.choose(AccountWork(), AccountWork(1)))

    def test_near_due_current_account_avoids_round_trip_for_single_task(self):
        self.policy.activate(self.a, 0)
        self.assertEqual("a", self.choose(
            AccountWork(upcoming_tasks=1, next_seconds=30), AccountWork(1),
        ))

    def test_due_bundle_beats_waiting_for_current_account(self):
        self.policy.activate(self.a, 0)
        self.assertEqual("b", self.choose(
            AccountWork(upcoming_tasks=1, next_seconds=30), AccountWork(3),
        ))

    def test_upcoming_bundle_prepared_but_single_future_task_not_switched_to(self):
        self.policy.activate(self.a, 0)
        self.assertEqual("b", self.choose(
            AccountWork(), AccountWork(upcoming_tasks=3, next_seconds=30),
        ))
        self.assertEqual("a", self.choose(
            AccountWork(), AccountWork(upcoming_tasks=1, next_seconds=30),
        ))

    def test_age_count_lookahead_and_switch_measurement(self):
        init = Event(is_init=True, tasks=[
            Task(kind="profile_validation", function_ids=["check"], duration_samples_seconds=[4, 6]),
            Task(kind="profile_switch", function_ids=["switch"], duration_samples_seconds=[40, 60]),
        ])
        work = AccountWork.from_events([
            init, self.event(-300, 3), self.event(60, 2), self.event(121, 9),
            self.event(-600, 5, enabled=False), self.event(-900, 4, is_interrupt=True),
        ], self.now, 120)
        self.assertEqual(3, work.due_tasks)
        self.assertEqual(2, work.upcoming_tasks)
        self.assertEqual(300, work.oldest_seconds)
        self.assertEqual(60, work.next_seconds)
        self.assertEqual(60, work.switch_seconds)

    def test_missing_due_date_and_failed_retries_do_not_age(self):
        activation = self.event()
        activation.next_due_at = None
        failed = self.event(-100000, status="failed")
        work = AccountWork.from_events([activation, failed], self.now, 120)
        self.assertEqual(2, work.due_tasks)
        self.assertEqual(0, work.oldest_seconds)

    def test_inactive_tasks_do_not_count(self):
        event = self.event(count=2)
        event.tasks[0].enabled = False
        event.tasks[1].function_ids = []
        self.assertEqual(0, AccountWork.from_events([event], self.now, 120).due_tasks)

    def test_horizon_follows_measured_cost_but_is_bounded(self):
        self.assertEqual(600, self.policy.horizon(DEFAULTS, 300))
        self.assertEqual(3600, self.policy.horizon(DEFAULTS, 10000))

    def test_resources_remain_independent(self):
        other = {**self.b, "emulator_instance": 2}
        self.policy.activate(self.a, 0)
        self.policy.activate(other, 5)
        self.assertEqual("b", self.policy.choose(
            [other], {"b": AccountWork(1)}, self.settings, 100,
        ))
        self.assertEqual("a", self.policy.active["1"])

    def test_real_idle_is_not_suppressed(self):
        runtime = self.runtime()
        self.policy.activate(self.a, 0)
        self.assertEqual({}, runtime._select_account_batches(self.profiles, {}))
        self.assertEqual(set(), runtime._account_waiting_resources)

    def test_forecast_prevents_idle_without_executing_early(self):
        runtime = self.runtime()
        event = self.event(60, 2)
        with patch("services.runtime_service.datetime") as clock:
            clock.now.return_value = self.now
            runtime.scheduler_service.list_events.side_effect = (
                lambda game, profile_id: [event] if profile_id == "a" else []
            )
            scores = runtime._select_account_batches(self.profiles, {})
        self.assertEqual({"a"}, set(scores))
        self.assertEqual({"1"}, runtime._account_waiting_resources)
        self.assertFalse(event.is_due(self.now))

    def test_one_resource_refresh_does_not_clear_another_resources_wait_or_error(self):
        runtime = self.runtime()
        runtime._account_waiting_resources = {"2"}
        runtime._account_planning_errors = {"2": ValueError("test")}
        runtime._select_account_batches(self.profiles, {})
        self.assertIn("2", runtime._account_waiting_resources)
        self.assertIn("2", runtime._account_planning_errors)

    def test_planning_failure_is_visible_and_blocks_resource(self):
        runtime = self.runtime()
        runtime.scheduler_service.list_events.side_effect = OSError("cannot read")
        runtime._select_account_batches(self.profiles, {})
        self.assertIn("1", runtime._account_planning_errors)
        runtime.logger.error.assert_called_once()
        self.assertEqual({"a", "b"}, runtime._due_schedule_read_errors)

    def heartbeat_runtime(self):
        runtime = self.runtime()
        runtime.running = True
        runtime._stop_event = StopEvent()
        runtime._active_profiles = {("g", 1): "a"}
        runtime._idle_states = {}
        runtime._activation_errors = {}
        runtime._startup_pending_profiles = set()
        runtime._startup_profile_attempts = {}
        runtime.profile_service = Mock()
        runtime.profile_service.load_enabled.return_value = self.profiles
        runtime.statistics_service = Mock()
        runtime._validate_profile = Mock(side_effect=lambda profile: {
            "profile_id": profile["id"], "state": "Healthy",
        })
        runtime._apply_idle_behavior = Mock(return_value={"state": "Idle"})
        runtime._run_schedules_parallel = Mock()
        runtime._update_runtime_status = Mock()
        runtime.get_status = Mock()
        return runtime

    def test_heartbeat_really_queues_high_priority_account_for_expired_low_priority(self):
        runtime = self.heartbeat_runtime()
        self.a["priority"] = 1000
        self.policy.activate(self.a, 0)
        events = {"a": [self.event(-60)], "b": [self.event(-10000)]}
        runtime.scheduler_service.list_execution_events.side_effect = (
            lambda game, profile_id: events[profile_id]
        )
        runtime.scheduler_service.list_events.side_effect = (
            lambda game, profile_id: events[profile_id]
        )
        with patch("services.runtime_service.datetime") as clock:
            clock.now.return_value = self.now
            with patch("services.runtime_service.monotonic", return_value=10000):
                runtime.heartbeat_once()
        self.assertEqual(["b"], [
            call.args[0]["id"] for call in runtime._validate_profile.call_args_list
        ])
        self.assertEqual("Queued", runtime.profile_statuses[0]["state"])
        runtime._apply_idle_behavior.assert_not_called()

    def test_heartbeat_planning_failure_neither_switches_nor_applies_idle(self):
        runtime = self.heartbeat_runtime()
        runtime.scheduler_service.list_execution_events.return_value = []
        runtime.scheduler_service.list_events.side_effect = OSError("unavailable")
        runtime.heartbeat_once()
        runtime._validate_profile.assert_not_called()
        runtime._apply_idle_behavior.assert_not_called()
        self.assertTrue(all(status["state"] == "Failed" for status in runtime.profile_statuses))

    def test_fresh_due_work_between_reads_does_not_trigger_idle(self):
        runtime = self.heartbeat_runtime()
        self.policy.activate(self.a, 0)
        event = self.event(-1)
        runtime.scheduler_service.list_execution_events.return_value = []
        runtime.scheduler_service.list_events.side_effect = (
            lambda game, profile_id: [event] if profile_id == "a" else []
        )
        with patch("services.runtime_service.datetime") as clock:
            clock.now.return_value = self.now
            runtime.heartbeat_once()
        runtime._apply_idle_behavior.assert_not_called()
        self.assertEqual(["a"], [
            call.args[0]["id"] for call in runtime._validate_profile.call_args_list
        ])

    def test_idle_interrupts_are_not_lost_when_no_account_has_due_work(self):
        runtime = self.heartbeat_runtime()
        self.policy.activate(self.a, 0)
        runtime.scheduler_service.list_execution_events.return_value = []
        runtime.scheduler_service.list_events.return_value = []
        self.assertTrue(runtime._continue_account_batch(self.a))

    def test_runtime_batch_rechecks_after_complete_event_and_yields_without_cancel(self):
        self.now = datetime.now(timezone.utc)
        runtime = self.heartbeat_runtime()
        runtime._activate_profile_variables = Mock()
        runtime.resolve_runnable_profile = lambda profile: profile
        runtime._scheduler_profile_signatures = {"a": ("a", "g", 1)}
        runtime.scheduler_function_root = "unused"
        runtime.scheduler_runner_factory = None
        runtime.scheduler_event_limit = 10
        runtime.interrupt_burst_limit = 1
        runtime._paused_interrupt_resources = set()
        runtime.variable_service = None
        self.policy.activate(self.a, 0)
        with tempfile.TemporaryDirectory() as directory:
            scheduler = SchedulerService(
                base_path=str(Path(directory) / "scheduler"), reinitialize_service=Mock(),
            )
            runtime.scheduler_service = scheduler
            for identity, event_id in (("a", "one"), ("a", "two"), ("b", "waiting")):
                event = self.event(-10000 if identity == "b" else -60)
                event.event_id = event_id
                event.profile_id = identity
                scheduler.save_event(event, register_output_variable=False)

            def run_event(event, **kwargs):
                event.next_due_at = (self.now + timedelta(days=1)).isoformat()
                scheduler.save_event(event, register_output_variable=False)
                return {"event_id": event.event_id, "status": "success", "executed": ["f"]}

            scheduler.run_event = Mock(side_effect=run_event)
            with patch("services.runtime_service.datetime") as clock:
                clock.now.return_value = self.now
                with patch("services.runtime_service.monotonic", return_value=10000):
                    result = runtime._run_profile_schedule_unlocked(self.a)
            self.assertEqual(1, len(result["events"]))
            self.assertEqual("success", result["events"][0]["status"])
            ran = [call.args[0].event_id for call in scheduler.run_event.call_args_list]
            self.assertEqual(["one", "interrupt-g-a"], ran)
            self.assertEqual(1, len(result["interrupt"]))
            self.assertFalse(runtime.execution_cancelled(self.a))

    def test_new_manual_request_on_other_account_runs_at_next_event_boundary(self):
        self._exercise_manual_boundary()

    def test_request_during_init_runs_before_current_accounts_first_regular_event(self):
        self._exercise_manual_boundary(queue_at_init=True)

    def _exercise_manual_boundary(self, queue_at_init=False):
        self.now = datetime.now(timezone.utc)
        runtime = self.heartbeat_runtime()
        runtime._activate_profile_variables = Mock()
        runtime.resolve_runnable_profile = lambda profile: profile
        runtime._scheduler_profile_signatures = {} if queue_at_init else {"a": ("a", "g", 1)}
        runtime.scheduler_function_root = "unused"
        runtime.scheduler_runner_factory = None
        runtime.scheduler_event_limit = 10
        runtime.interrupt_burst_limit = 1
        runtime._paused_interrupt_resources = set()
        runtime.variable_service = None
        runtime._run_profile_init = Mock(return_value={"status": "success"})
        self.policy.activate(self.a, 0)
        with tempfile.TemporaryDirectory() as directory:
            scheduler = SchedulerService(
                base_path=str(Path(directory) / "scheduler"), reinitialize_service=Mock(),
            )
            runtime.scheduler_service = scheduler
            for identity, event_id, order in (("a", "first", 0), ("a", "later", 1), ("b", "manual", 0)):
                event = self.event(-60, sort_order=order)
                event.event_id, event.profile_id = event_id, identity
                if identity == "b":
                    event.enabled = False
                    event.next_due_at = (self.now + timedelta(days=1)).isoformat()
                scheduler.save_event(event, register_output_variable=False)
            executed = []

            def run_init(profile):
                if queue_at_init and profile["id"] == "a":
                    scheduler.queue_event_now(scheduler.load_event("g", "manual"))
                return {"status": "success"}

            runtime._run_profile_init.side_effect = run_init

            def run_event(event, **kwargs):
                executed.append(event.event_id)
                if not event.is_interrupt:
                    event.status = "success"
                    event.next_due_at = (self.now + timedelta(days=1)).isoformat()
                    scheduler.save_event(event, register_output_variable=False)
                if event.event_id == "first" and not queue_at_init:
                    scheduler.queue_event_now(scheduler.load_event("g", "manual"))
                return {"event_id": event.event_id, "status": "success", "executed": ["f"]}

            scheduler.run_event = Mock(side_effect=run_event)
            # A newly active high-priority account normally cannot be preempted.
            with patch("services.runtime_service.monotonic", return_value=1):
                first = runtime._run_profile_schedule_unlocked(self.a)
                self.assertEqual(
                    [] if queue_at_init else ["first"],
                    [item["event_id"] for item in first["events"]],
                )
                scores = runtime._select_account_batches(self.profiles, {})
                self.assertEqual({"b"}, set(scores))
                runtime._prepare_profile_transition(self.b)
                second = runtime._run_profile_schedule_unlocked(self.b)
            self.assertEqual(["manual"], [item["event_id"] for item in second["events"]])
            self.assertEqual(
                ([] if queue_at_init else ["first"]) + ["interrupt-g-a", "manual", "interrupt-g-b"],
                executed,
            )
            self.assertEqual(
                [self.a, self.b] if queue_at_init else [self.b],
                [call.args[0] for call in runtime._run_profile_init.call_args_list],
            )
            self.assertTrue(scheduler.load_event("g", "later").is_due())

    def test_init_batch_runs_interrupt_between_every_event(self):
        self.now = datetime.now(timezone.utc)
        runtime = self.heartbeat_runtime()
        runtime._activate_profile_variables = Mock()
        runtime.resolve_runnable_profile = lambda profile: profile
        runtime._scheduler_profile_signatures = {}
        runtime._run_profile_init = Mock(return_value={"status": "success"})
        runtime.scheduler_function_root = "unused"
        runtime.scheduler_runner_factory = None
        runtime.scheduler_event_limit = 1
        runtime.interrupt_burst_limit = 1
        runtime._paused_interrupt_resources = set()
        runtime.variable_service = None
        self.policy.activate(self.a, 0)
        with tempfile.TemporaryDirectory() as directory:
            scheduler = SchedulerService(
                base_path=str(Path(directory) / "scheduler"), reinitialize_service=Mock(),
            )
            runtime.scheduler_service = scheduler
            for event_id in ("one", "two", "three"):
                event = self.event(-60)
                event.event_id = event_id
                scheduler.save_event(event, register_output_variable=False)

            def run_event(event, **kwargs):
                if not event.is_interrupt:
                    event.next_due_at = (self.now + timedelta(days=1)).isoformat()
                    scheduler.save_event(event, register_output_variable=False)
                return {"event_id": event.event_id, "status": "success", "executed": ["f"]}

            scheduler.run_event = Mock(side_effect=run_event)
            with patch("services.runtime_service.datetime") as clock:
                clock.now.return_value = self.now
                with patch("services.runtime_service.monotonic", return_value=10000):
                    result = runtime._run_profile_schedule_unlocked(self.a)
            ran = [call.args[0].event_id for call in scheduler.run_event.call_args_list]
            self.assertEqual(3, len(result["events"]))
            for position, name in enumerate(ran):
                if position % 2:
                    self.assertEqual("interrupt-g-a", name)
                else:
                    self.assertNotEqual("interrupt-g-a", name)
            self.assertEqual(6, len(ran))

    def test_scheduler_yields_only_between_complete_events(self):
        with tempfile.TemporaryDirectory() as directory:
            service = SchedulerService(
                base_path=str(Path(directory) / "scheduler"), reinitialize_service=Mock(),
            )
            events = [self.event(-60, 2), self.event(-60, 3)]
            events[1].event_id = "e2"
            completed = []
            service.list_execution_events = Mock(return_value=events)
            service.run_event = Mock(side_effect=lambda event, **kwargs: (
                completed.append(event.event_id) or {"status": "success"}
            ))
            boundary = Mock(return_value=False)
            result = service.run_due_events(
                "g", profile=self.a, persist=False, limit=None,
                continuation_checker=boundary,
            )
            self.assertEqual(["e"], completed)
            self.assertEqual(1, len(result))
            boundary.assert_called_once_with()

    def test_cross_game_transition_invalidates_previous_game_owner(self):
        runtime = self.runtime()
        runtime._active_profiles = {("old", 1): "old-profile"}
        runtime._scheduler_profile_signatures = {"old-profile": ("old-profile", "old", 1)}
        runtime._activate_profile_variables = Mock()
        runtime._prepare_profile_transition(self.a)
        self.assertEqual({("g", 1): "a"}, runtime._active_profiles)
        self.assertEqual({}, runtime._scheduler_profile_signatures)
        runtime._activate_profile_variables.assert_called_once_with(
            self.a, invalidate_live=True,
        )


if __name__ == "__main__":
    result = unittest.main(exit=False)
    print(
        "TEST RESULT: SUCCESS" if result.result.wasSuccessful() else "TEST RESULT: FAILURE",
        flush=True,
    )
    raise SystemExit(not result.result.wasSuccessful())
