import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from scheduler.models import PROTECTED_TASK_KINDS, Event, SchedulerService, Task


class InitTaskProfileSyncTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.scheduler = SchedulerService(
            base_path=str(Path(temporary.name) / "scheduler"), logger=Mock(),
        )
        self.source_init = self.scheduler.ensure_init_event("game", "source")
        self.target_init = self.scheduler.ensure_init_event("game", "target")
        self._set_protected_functions(self.source_init, "source-protected")
        self._set_protected_functions(self.target_init, "target-protected")
        self.source_init.tasks.append(Task(
            task_id="source-extra", name="Open Daily", function_ids=["daily"],
            status="success", last_started_at="2026-09-28T10:00:00+00:00",
            last_finished_at="2026-09-28T10:01:00+00:00",
            duration_samples_seconds=[60.0],
        ))
        self.target_init.tasks.append(Task(
            task_id="target-extra", name="Old extra", function_ids=["old"],
        ))
        self.scheduler.save_event(self.source_init)
        self.scheduler.save_event(self.target_init)

    @staticmethod
    def _set_protected_functions(event, function_id):
        for task in event.tasks:
            if task.kind in PROTECTED_TASK_KINDS:
                task.function_ids = [f"{function_id}-{task.kind}"]

    def _init(self, profile_id):
        return next(
            event for event in self.scheduler.list_events("game", profile_id)
            if event.is_init
        )

    def test_sync_replaces_extra_init_tasks_and_keeps_protected_tasks(self):
        result = self.scheduler.sync_profile_events("game", "source", ["target"])

        target = self._init("target")
        protected = [task for task in target.tasks if task.kind in PROTECTED_TASK_KINDS]
        extras = [task for task in target.tasks if task.kind not in PROTECTED_TASK_KINDS]
        self.assertEqual(target.event_id, self.target_init.event_id)
        self.assertEqual(
            sorted(task.function_ids[0] for task in protected),
            sorted(f"target-protected-{kind}" for kind in PROTECTED_TASK_KINDS),
        )
        self.assertEqual(len(extras), 1)
        copied = extras[0]
        self.assertEqual(copied.name, "Open Daily")
        self.assertEqual(copied.function_ids, ["daily"])
        self.assertNotEqual(copied.task_id, "source-extra")
        self.assertEqual(copied.status, "idle")
        self.assertIsNone(copied.last_started_at)
        self.assertIsNone(copied.last_finished_at)
        self.assertEqual(copied.duration_samples_seconds, [])
        self.assertEqual(result["copied_init_tasks"], 1)

        source = self._init("source")
        self.assertIn("source-extra", [task.task_id for task in source.tasks])
        self.assertEqual(
            next(task for task in source.tasks if task.task_id == "source-extra").status,
            "success",
        )

    def test_sync_rejects_running_target_init(self):
        self.target_init.status = "running"
        self.scheduler.save_event(self.target_init)

        with self.assertRaisesRegex(ValueError, "running or queued"):
            self.scheduler.sync_profile_events("game", "source", ["target"])

        self.assertIn("target-extra", [task.task_id for task in self._init("target").tasks])

    def test_sync_restores_target_init_when_saving_fails(self):
        original_save = self.scheduler.save_event
        failures = []

        def failing_save(event):
            if event.is_init and event.profile_id == "target" and not failures:
                failures.append(event)
                original_save(event)
                raise OSError("disk full")
            return original_save(event)

        with patch.object(self.scheduler, "save_event", side_effect=failing_save):
            with self.assertRaises(OSError):
                self.scheduler.sync_profile_events("game", "source", ["target"])

        self.assertEqual(
            [task.task_id for task in self._init("target").tasks
             if task.kind not in PROTECTED_TASK_KINDS],
            ["target-extra"],
        )


class EventBulkToolsTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.scheduler = SchedulerService(
            base_path=str(Path(temporary.name) / "scheduler"), logger=Mock(),
        )
        for profile in ("source", "target"):
            self.scheduler.ensure_init_event("game", profile)

    def _event(self, profile, name, enabled=True):
        event = Event(game_id="game", profile_id=profile, name=name, enabled=enabled)
        self.scheduler.save_event(event, register_output_variable=False)
        return event

    def _custom(self, profile):
        return {
            event.name: event
            for event in self.scheduler.list_events("game", profile)
            if not event.is_init and not event.is_interrupt
        }

    def test_sync_skips_excluded_events_and_keeps_target_copy(self):
        keep = self._event("source", "Keep")
        excluded = self._event("source", "Bear")
        target_bear = self._event("target", "Bear")
        self._event("target", "Old")

        result = self.scheduler.sync_profile_events(
            "game", "source", ["target"], excluded_event_ids=[excluded.event_id],
        )

        target = self._custom("target")
        self.assertEqual(sorted(target), ["Bear", "Keep"])
        self.assertEqual(target["Bear"].event_id, target_bear.event_id)
        self.assertNotEqual(target["Keep"].event_id, keep.event_id)
        self.assertEqual(result["copied_events"], 1)
        self.assertEqual(result["skipped_events"], 1)

    def test_set_events_enabled_per_profile_and_for_all(self):
        self._event("source", "A")
        self._event("source", "B", enabled=False)
        self._event("target", "C")

        changed = self.scheduler.set_events_enabled("game", False, profile_id="source")

        self.assertEqual(changed, 1)
        self.assertFalse(any(e.enabled for e in self._custom("source").values()))
        self.assertTrue(self._custom("target")["C"].enabled)
        inits = [e for e in self.scheduler.list_events("game") if e.is_init]
        self.assertTrue(all(e.enabled for e in inits))

        changed = self.scheduler.set_events_enabled("game", True)

        self.assertEqual(changed, 2)
        self.assertTrue(all(e.enabled for e in self._custom("source").values()))

    def test_create_event_for_profiles_creates_independent_copies_with_task(self):
        variables = Mock()
        template = Event(game_id="game", profile_id="", name="Bear Hunt", priority=70)

        events = self.scheduler.create_event_for_profiles(
            template, ["source", "target", "source"], task_name="Hunt",
            function_ids=["bear"], variable_service=variables,
        )

        self.assertEqual([e.profile_id for e in events], ["source", "target"])
        self.assertEqual(len({e.event_id for e in events}), 2)
        for profile in ("source", "target"):
            event = self._custom(profile)["Bear Hunt"]
            self.assertEqual(event.priority, 70)
            self.assertEqual([(t.name, t.function_ids) for t in event.tasks], [("Hunt", ["bear"])])
        self.assertTrue(variables.ensure_global.called)

    def test_create_event_for_profiles_requires_profiles(self):
        with self.assertRaises(ValueError):
            self.scheduler.create_event_for_profiles(Event(game_id="game", name="X"), [])


if __name__ == "__main__":
    unittest.main()
