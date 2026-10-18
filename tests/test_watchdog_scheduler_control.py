import faulthandler
import sys
import time
import unittest
from copy import deepcopy
from datetime import datetime, timezone

from scheduler.models import Event, Task
from services.runtime_service import RuntimeService


class FakeProfileService:
    def __init__(self, profiles):
        self.profiles = profiles

    def load_enabled(self):
        return [profile for profile in self.profiles if profile.get("enabled")]


class FakeSchedulerService:
    def __init__(self, events):
        self.events = {
            (event.game_id, event.event_id): deepcopy(event)
            for event in events
        }

    def list_events(self, game_id, profile_id=None):
        return [
            deepcopy(event)
            for (event_game, _event_id), event in self.events.items()
            if event_game == game_id
            and (
                profile_id is None
                or event.profile_id in ("", profile_id)
            )
        ]

    def load_event(self, game_id, event_id):
        event = self.events.get((game_id, event_id))
        return deepcopy(event) if event else None

    def save_event(self, event):
        self.events[(event.game_id, event.event_id)] = deepcopy(event)
        return event

    def queue_event_now(self, event):
        if event.status in {"queued", "running", "cancelling"}:
            raise ValueError("event is already queued or running")
        if not event.function_ids:
            raise ValueError("event has no active tasks")
        event.status = "queued"
        event.next_due_at = datetime.now(timezone.utc).isoformat()
        self.save_event(event)
        return self.load_event(event.game_id, event.event_id)


class WatchdogSchedulerControlTests(unittest.TestCase):
    def setUp(self):
        profile = {
            "game": "game-a",
            "id": "profile-a",
            "name": "Profile A",
            "enabled": True,
            "emulator_instance": "emulator-a",
        }
        self.event = Event(
            event_id="event-12345678",
            game_id="game-a",
            profile_id="profile-a",
            name="Collect",
            enabled=True,
            tasks=[
                Task(
                    task_id="task-running",
                    name="Running task",
                    function_ids=["function-a"],
                    status="running",
                ),
                Task(
                    task_id="task-queued",
                    name="Next task",
                    function_ids=["function-b"],
                    status="idle",
                ),
            ],
        )
        self.runtime = RuntimeService.__new__(RuntimeService)
        self.runtime.profile_service = FakeProfileService([profile])
        self.runtime.scheduler_service = FakeSchedulerService([self.event])
        self.runtime._profile_execution_locks = {}
        self.runtime.running = True

    def test_snapshot_lists_custom_events_and_open_tasks(self):
        snapshot = self.runtime.watchdog_scheduler_command("events.list")
        tasks = self.runtime.watchdog_scheduler_command("tasks.list")

        self.assertEqual(["event-12345678"], [
            event["event_id"] for event in snapshot["events"]
        ])
        self.assertEqual(
            ["task-running"],
            [task["task_id"] for task in tasks["tasks"]],
        )
        self.assertEqual("running", tasks["tasks"][0]["task_status"])

    def test_snapshot_lists_active_tasks_of_queued_events(self):
        queued_event = deepcopy(self.event)
        queued_event.event_id = "event-queued"
        queued_event.status = "queued"
        queued_event.tasks = [queued_event.tasks[1]]
        self.runtime.scheduler_service.events[
            ("game-a", queued_event.event_id)
        ] = queued_event

        tasks = self.runtime.watchdog_scheduler_command("tasks.list")

        self.assertEqual(
            [("task-queued", "queued")],
            [
                (task["task_id"], task["task_status"])
                for task in tasks["tasks"]
                if task["event_id"] == queued_event.event_id
            ],
        )

    def test_event_disable_preserves_current_scheduler_state(self):
        event = self.runtime._watchdog_update_event(
            "event.disable",
            "event-123",
        )
        persisted = self.runtime.scheduler_service.load_event(
            "game-a",
            "event-12345678",
        )

        self.assertFalse(event["enabled"])
        self.assertFalse(persisted.enabled)
        self.assertEqual("idle", persisted.status)
        self.assertIsNone(persisted.next_due_at)

    def test_run_now_queues_enabled_event_for_scheduler_cycle(self):
        result = self.runtime._watchdog_update_event(
            "event.run_now",
            "event-12345678",
        )
        persisted = self.runtime.scheduler_service.load_event(
            "game-a",
            "event-12345678",
        )

        self.assertEqual("queued", result["state"])
        self.assertEqual("queued", persisted.status)
        self.assertTrue(Event._parse_datetime(persisted.next_due_at))

    def test_run_now_is_persisted_when_runtime_is_stopped_or_event_disabled(self):
        self.runtime.running = False
        stopped_result = self.runtime._watchdog_update_event(
            "event.run_now",
            "event-123",
        )
        self.assertEqual("queued", stopped_result["state"])
        self.runtime.running = True
        disabled_event = self.runtime.scheduler_service.load_event(
            "game-a",
            "event-12345678",
        )
        disabled_event.status = "idle"
        disabled_event.enabled = False
        self.runtime.scheduler_service.save_event(disabled_event)
        disabled_result = self.runtime._watchdog_update_event(
            "event.run_now",
            "event-123",
        )
        self.assertEqual("queued", disabled_result["state"])
        self.assertEqual(
            "queued",
            self.runtime.scheduler_service.load_event(
                "game-a",
                "event-12345678",
            ).status,
        )

    def test_run_now_queues_without_acquiring_busy_profile(self):
        profile = self.runtime.profile_service.profiles[0]
        self.assertTrue(self.runtime.try_acquire_profile_execution(profile))
        try:
            result = self.runtime._watchdog_update_event(
                "event.run_now",
                "event-12345678",
            )
        finally:
            self.runtime.release_profile_execution(profile)

        self.assertEqual("queued", result["state"])
        queued = self.runtime.scheduler_service.load_event(
            "game-a",
            "event-12345678",
        )
        self.assertEqual("queued", queued.status)
        self.assertTrue(Event._parse_datetime(queued.next_due_at))

    def test_event_reference_must_resolve_to_one_controllable_event(self):
        duplicate = deepcopy(self.event)
        duplicate.game_id = "game-b"
        duplicate.event_id = "event-12399999"
        duplicate.profile_id = "profile-b"
        self.runtime.profile_service.profiles.append(
            {
                "game": "game-b",
                "id": "profile-b",
                "name": "Profile B",
                "enabled": True,
                "emulator_instance": "emulator-b",
            }
        )
        self.runtime.scheduler_service.events[
            ("game-b", duplicate.event_id)
        ] = duplicate

        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.runtime._watchdog_update_event(
                "event.enable",
                "event-123",
            )
        selected = self.runtime._watchdog_update_event(
            "event.disable",
            "event-123",
            "profile-b",
        )
        self.assertEqual("Profile B", selected["profile_name"])
        with self.assertRaisesRegex(ValueError, "not found"):
            self.runtime._watchdog_update_event(
                "event.enable",
                "missing",
            )

    def test_system_events_are_not_exposed_for_remote_control(self):
        system_event = Event(
            event_id="init-game-a-profile-a",
            game_id="game-a",
            profile_id="profile-a",
            name="Init",
            is_init=True,
            tasks=[],
        )
        self.runtime.scheduler_service.events[
            ("game-a", system_event.event_id)
        ] = system_event

        result = self.runtime.watchdog_scheduler_command("events.list")
        self.assertNotIn(
            system_event.event_id,
            [event["event_id"] for event in result["events"]],
        )
        with self.assertRaisesRegex(ValueError, "not found"):
            self.runtime._watchdog_update_event(
                "event.disable",
                "init-game-a",
            )

    def test_rejects_unrecognized_scheduler_commands_and_arguments(self):
        with self.assertRaisesRegex(ValueError, "Unsupported scheduler command"):
            self.runtime.watchdog_scheduler_command("runtime.start")
        with self.assertRaisesRegex(ValueError, "event_id is required"):
            self.runtime.watchdog_scheduler_command("event.enable", {})
        with self.assertRaisesRegex(ValueError, "Command arguments"):
            self.runtime.watchdog_scheduler_command("events.list", [])


if __name__ == "__main__":
    started_at = time.monotonic()
    print("TEST START: watchdog scheduler control", flush=True)
    faulthandler.dump_traceback_later(20, exit=True)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(
        WatchdogSchedulerControlTests
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
