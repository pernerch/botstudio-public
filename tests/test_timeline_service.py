import faulthandler
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from threading import Timer

from scheduler.models import Event, Task
from services.timeline_service import build_timeline_items


class TimelineServiceTests(unittest.TestCase):
    def setUp(self):
        started = time.monotonic()
        print(f"TEST START: {self.id()}", flush=True)
        timer = Timer(10, lambda: (
            print("TEST RESULT: TIMEOUT", flush=True),
            faulthandler.dump_traceback(), os._exit(124),
        ))
        timer.daemon = True
        timer.start()
        self.addCleanup(timer.cancel)
        self.addCleanup(lambda: print(
            f"TEST END: {self.id()} duration={time.monotonic() - started:.3f}s",
            flush=True,
        ))
        self.now = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)

    def stamp(self, seconds=0):
        return (self.now + timedelta(seconds=seconds)).isoformat()

    def task(self, **kwargs):
        values = dict(
            task_id="task", name="Task", function_ids=["function"],
            duration_samples_seconds=[60],
        )
        values.update(kwargs)
        return Task(**values)

    def event(self, **kwargs):
        values = dict(
            event_id="event", profile_id="profile", name="Event",
            next_due_at=self.stamp(60), tasks=[self.task()],
        )
        values.update(kwargs)
        return Event(**values)

    def items(self, *events):
        return build_timeline_items(events, horizon_hours=1, now=self.now)[0]

    def test_disabled_event_is_hidden_even_with_due_date(self):
        self.assertEqual([], self.items(self.event(enabled=False)))

    def test_all_disabled_tasks_are_hidden(self):
        self.assertEqual([], self.items(self.event(tasks=[self.task(enabled=False)])))

    def test_tasks_without_functions_are_hidden(self):
        self.assertEqual([], self.items(self.event(tasks=[self.task(function_ids=[])])))

    def test_inactive_tasks_do_not_delay_the_next_active_task(self):
        items = self.items(self.event(tasks=[
            self.task(task_id="disabled", enabled=False, sort_order=0),
            self.task(task_id="empty", function_ids=[], sort_order=1),
            self.task(task_id="active", sort_order=2),
        ]))
        self.assertEqual(["active"], [item.item_id for item in items])
        self.assertEqual(self.now + timedelta(seconds=60), items[0].start)

    def test_no_concrete_due_date_means_no_planned_block(self):
        for mode in ("manual", "interval", "daily"):
            with self.subTest(mode=mode):
                self.assertEqual([], self.items(self.event(
                    schedule_mode=mode, next_due_at=None,
                )))

    def test_invalid_due_date_does_not_create_a_forecast(self):
        self.assertEqual([], self.items(self.event(next_due_at="not-a-date")))

    def test_explicit_due_date_is_shown_for_dynamic_and_queued_work(self):
        for status in ("idle", "queued", "success", "failed"):
            with self.subTest(status=status):
                items = self.items(self.event(status=status))
                self.assertEqual(1, len(items))
                self.assertEqual("planned", items[0].status)
                self.assertEqual(self.now + timedelta(seconds=60), items[0].start)

    def test_system_events_do_not_forecast_from_due_dates(self):
        for flags in ({"is_init": True}, {"is_interrupt": True}):
            with self.subTest(flags=flags):
                self.assertEqual([], self.items(self.event(**flags)))

    def test_inactive_system_tasks_are_not_restored_as_fallback(self):
        self.assertEqual([], self.items(self.event(
            is_init=True, status="success", last_started_at=self.stamp(-120),
            last_finished_at=self.stamp(-60),
            tasks=[self.task(enabled=False, last_started_at=self.stamp(-120))],
        )))

    def test_profile_switch_is_visible_when_it_really_runs(self):
        items = self.items(self.event(
            is_init=True, status="running", last_started_at=self.stamp(-120),
            tasks=[
                self.task(
                    task_id="switch", name="Profile Switch", kind="profile_switch",
                    status="running", last_started_at=self.stamp(-30),
                ),
                self.task(task_id="pending", sort_order=1),
            ],
        ))
        self.assertEqual(["switch"], [item.item_id for item in items])
        self.assertEqual("Init: Profile Switch", items[0].label)
        self.assertEqual("running", items[0].status)
        self.assertEqual(self.now - timedelta(seconds=30), items[0].start)

    def test_completed_profile_switch_retains_measured_times(self):
        items = self.items(self.event(
            is_init=True, status="success", last_started_at=self.stamp(-180),
            last_finished_at=self.stamp(-30),
            tasks=[self.task(
                name="Profile Switch", kind="profile_switch", status="success",
                last_started_at=self.stamp(-120), last_finished_at=self.stamp(-60),
            )],
        ))
        self.assertEqual(1, len(items))
        self.assertEqual("success", items[0].status)
        self.assertEqual(self.now - timedelta(seconds=120), items[0].start)
        self.assertEqual(self.now - timedelta(seconds=60), items[0].end)

    def test_restarted_init_does_not_show_tasks_from_previous_activation(self):
        self.assertEqual([], self.items(self.event(
            is_init=True, status="running", last_started_at=self.stamp(-10),
            tasks=[self.task(
                name="Profile Switch", kind="profile_switch", status="success",
                last_started_at=self.stamp(-600), last_finished_at=self.stamp(-540),
            )],
        )))

    def test_interrupt_uses_actual_task_times_not_stale_due_date(self):
        items = self.items(self.event(
            is_interrupt=True, status="running", last_started_at=self.stamp(-120),
            tasks=[self.task(status="running", last_started_at=self.stamp(-90))],
        ))
        self.assertEqual(1, len(items))
        self.assertEqual(self.now - timedelta(seconds=90), items[0].start)
        self.assertEqual(self.now, items[0].end)

    def test_running_regular_task_uses_its_observed_start(self):
        items = self.items(self.event(
            status="running", last_started_at=self.stamp(-300),
            tasks=[self.task(status="running", last_started_at=self.stamp(-30))],
        ))
        self.assertEqual(self.now - timedelta(seconds=30), items[0].start)
        self.assertEqual("running", items[0].status)

    def test_running_task_does_not_reuse_previous_completion_time(self):
        items = self.items(self.event(
            status="running", last_started_at=self.stamp(-120),
            tasks=[self.task(
                status="running", last_started_at=self.stamp(-90),
                last_finished_at=self.stamp(-600),
            )],
        ))
        self.assertEqual(self.now, items[0].end)

    def test_pending_regular_task_does_not_reuse_previous_run_status(self):
        items = self.items(self.event(
            status="running", last_started_at=self.stamp(-120),
            tasks=[
                self.task(
                    task_id="current", sort_order=0, status="running",
                    last_started_at=self.stamp(-90),
                ),
                self.task(
                    task_id="pending", sort_order=1, status="running",
                    last_started_at=self.stamp(-600),
                ),
            ],
        ))
        pending = next(item for item in items if item.item_id == "pending")
        self.assertEqual("planned", pending.status)
        self.assertEqual(self.now, pending.start)

    def test_task_inside_window_is_not_hidden_by_short_event_estimate(self):
        items = self.items(self.event(
            next_due_at=self.stamp(-4000), duration_samples_seconds=[60],
            tasks=[self.task(duration_samples_seconds=[800])],
        ))
        self.assertEqual(1, len(items))


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE", flush=True)
    raise SystemExit(0 if result.wasSuccessful() else 1)
