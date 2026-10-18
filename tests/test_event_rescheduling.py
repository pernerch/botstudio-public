import faulthandler
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Timer
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from models.bot_function import BotFunction
from models.execution_context import ExecutionContext
from models.function_block import FunctionBlock
from scheduler.models import Event, SchedulerService, Task
from services.function_runner import FunctionRunner
from services.reinitialize_service import ReinitializeService
from ui.i18n.translator import t
from ui.i18n import translator


class EventReschedulingTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.notifications = []
        bus = ReinitializeService()
        bus.subscribe(lambda source, payload: self.notifications.append((source, payload)))
        self.scheduler = SchedulerService(
            base_path=str(self.root / "scheduler"),
            reinitialize_service=bus, logger=Mock(),
        )
        self.event = Event(
            event_id="target", game_id="game", profile_id="account",
            name="Gathering", schedule_mode="manual",
            tasks=[Task(task_id="task", name="Collect", function_ids=["function"])],
        )
        self.scheduler.save_event(self.event)
        self.notifications.clear()

    def context(self):
        return ExecutionContext(
            game_id="game", profile_id="account", function_id="source-function",
            event_id="source",
            event_due_handler=lambda target, delay: self.scheduler.set_event_due(
                "game", "account", target, delay,
            ),
        )

    def run_block(self, block_type, params, context=None):
        function = BotFunction(
            function_id="source-function", game_id="game", name="Source",
            blocks=[FunctionBlock(block_id="control", block_type=block_type, params=params)],
        )
        return FunctionRunner(logger=Mock()).run(function, context or self.context())

    def test_full_run_follows_find_coordinates_into_ocr_parse_and_reschedule(self):
        # Mirrors the Init function: find_element is wired only via its
        # coordinates output, which full runs previously ignored on "found".
        def block(block_id, block_type, params, output_as=""):
            return {"block_id": block_id, "block_type": block_type,
                    "params": params, "output_as": output_as, "branches": {}}

        def wire(source, outcome, target, target_input="input"):
            return {"source_block_id": source, "source_outcome": outcome,
                    "target_block_id": target, "target_input": target_input}

        function = BotFunction.from_dict({
            "function_id": "source-function", "game_id": "game", "name": "Source",
            "blocks": [
                block("find", "find_element", {"library_entry_id": "anchor"}),
                block("ocr", "ocr_read",
                      {"expect": "time", "offset_x": 57, "offset_y": 10}, "Gather Meat"),
                block("parse", "duration_parse", {"input": "$Gather Meat"}, "Gather Meat"),
                block("reschedule", "reschedule_event",
                      {"event_id": "Gathering", "delay_seconds": 0,
                       "delay_variable": "$Gather Meat"}),
            ],
            "connections": [
                wire("find", "coordinates", "ocr", "coordinates"),
                wire("ocr", "ok", "parse"),
                wire("parse", "ok", "reschedule"),
            ],
        })
        screen = Mock()
        screen.capture.return_value = self.root / "screen.png"
        ocr = Mock()
        ocr.read.return_value = "01:09:36"
        runner = FunctionRunner(screen_service=screen, ocr_service=ocr, logger=Mock())
        match = {"x": 21, "y": 260, "width": 30, "height": 30}
        before = datetime.now(timezone.utc)
        with patch.object(runner, "_find_element", return_value=("found", match, None)):
            result = runner.run(function, self.context(), profile={"name": "Account"})
        self.assertIsNone(result.get("error"))
        ocr.read.assert_called_once()
        due = Event._parse_datetime(self.scheduler.load_event("game", "target").next_due_at)
        self.assertGreaterEqual(due, before + timedelta(seconds=4176))
        self.assertLess(due, datetime.now(timezone.utc) + timedelta(seconds=4177))

    def test_existing_set_due_block_executes_and_publishes_refresh(self):
        self.event.next_due_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        self.scheduler.save_event(self.event)
        self.notifications.clear()
        result = self.run_block("set_event_due", {"event_id": "target"})
        self.assertIsNone(result.get("error"))
        self.assertTrue(self.scheduler.load_event("game", "target").is_due())
        self.assertEqual("scheduler", self.notifications[-1][0])
        self.assertFalse(result.get("event_stop_requested"))

    def test_reschedule_accepts_seconds_variable_and_continues(self):
        context = self.context()
        context.set("remaining", 2520, local=True)
        before = datetime.now(timezone.utc)
        result = self.run_block(
            "reschedule_event", {"event_id": "Gathering", "delay_variable": "remaining"},
            context,
        )
        due = Event._parse_datetime(self.scheduler.load_event("game", "target").next_due_at)
        self.assertGreaterEqual(due, before + timedelta(seconds=2520))
        self.assertLess(due, datetime.now(timezone.utc) + timedelta(seconds=2521))
        self.assertFalse(result.get("event_stop_requested"))

    def test_invalid_delays_fail_without_changing_target(self):
        for delay in (-1, float("inf"), float("nan"), "invalid"):
            with self.subTest(delay=delay):
                result = self.run_block(
                    "reschedule_event", {"event_id": "target", "delay_seconds": delay},
                )
                self.assertEqual("failed", result["outcome"])
                self.assertIsNone(self.scheduler.load_event("game", "target").next_due_at)

    def test_no_scheduler_context_and_current_event_are_rejected(self):
        result = self.run_block("set_event_due", {"event_id": "target"}, ExecutionContext(
            game_id="game", profile_id="account", function_id="source-function",
        ))
        self.assertEqual("failed", result["outcome"])
        result = self.run_block("set_event_due", {"event_id": "source"})
        self.assertEqual("failed", result["outcome"])

    def test_foreign_disabled_protected_running_and_ambiguous_targets_rejected(self):
        for changes in (
            {"profile_id": "other"}, {"enabled": False},
            {"name": "Init", "is_init": True}, {"status": "running"},
        ):
            with self.subTest(changes=changes):
                event = Event.from_dict(self.event.to_dict())
                for key, value in changes.items():
                    setattr(event, key, value)
                self.scheduler.save_event(event)
                with self.assertRaises((ValueError, PermissionError)):
                    self.scheduler.set_event_due("game", "account", "target", 30)
        self.scheduler.save_event(self.event)
        duplicate = Event.from_dict(self.event.to_dict())
        duplicate.event_id = "duplicate"
        self.scheduler.save_event(duplicate)
        with self.assertRaises(ValueError):
            self.scheduler.set_event_due("game", "account", "Gathering", 30)

    def test_foreign_event_id_resolves_to_same_named_event_in_executing_profile(self):
        # Functions are shared per game: a block may hold the sync source's ID.
        source = Event.from_dict(self.event.to_dict())
        source.event_id = "source-copy"
        source.profile_id = "source-account"
        self.scheduler.save_event(source)

        due_at = self.scheduler.set_event_due("game", "account", "source-copy", 30)

        self.assertEqual(due_at, self.scheduler.load_event("game", "target").next_due_at)
        self.assertIsNone(self.scheduler.load_event("game", "source-copy").next_due_at)
        reverse = self.scheduler.set_event_due("game", "source-account", "target", 60)
        self.assertEqual(reverse, self.scheduler.load_event("game", "source-copy").next_due_at)
        self.assertEqual(due_at, self.scheduler.load_event("game", "target").next_due_at)

    def test_unscheduled_success_runs_once_then_rearms_on_profile_activation(self):
        self.event.last_finished_at = datetime.now(timezone.utc).isoformat()
        self.event.status = "success"
        self.scheduler.save_event(self.event)
        self.assertFalse(self.event.is_due())
        self.scheduler.prepare_profile_start("game", "account")
        rearmed = self.scheduler.load_event("game", "target")
        self.assertTrue(rearmed.is_due())
        self.assertEqual(["target"], [
            event.event_id for event in self.scheduler.list_execution_events(
                "game", "account", overdue_only=True,
            )
        ])
        self.scheduler.mark_event_result(rearmed, "success")
        self.assertFalse(self.scheduler.load_event("game", "target").is_due())
        self.scheduler.prepare_profile_start("game", "account")
        self.assertTrue(self.scheduler.load_event("game", "target").is_due())

    def test_new_unscheduled_events_are_in_initial_overdue_selection(self):
        self.assertEqual(["target"], [
            event.event_id for event in self.scheduler.list_execution_events(
                "game", "account", overdue_only=True,
            )
        ])

    def test_failed_and_timeout_events_have_bounded_retry_even_when_manual(self):
        now = datetime.now(timezone.utc)
        for status in ("failed", "timeout"):
            with self.subTest(status=status):
                self.event.retry_minutes = 0
                event = self.scheduler.mark_event_result(self.event, status, now)
                self.assertEqual(now + timedelta(minutes=1), Event._parse_datetime(event.next_due_at))
                self.assertFalse(event.is_due(now))
                self.assertTrue(event.is_due(now + timedelta(minutes=1)))

    def test_profile_activation_preserves_future_plans_and_disabled_events(self):
        self.event.next_due_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        self.scheduler.save_event(self.event)
        self.scheduler.prepare_profile_start("game", "account")
        self.assertEqual(self.event.next_due_at, self.scheduler.load_event("game", "target").next_due_at)
        self.event.enabled = False
        self.event.next_due_at = None
        self.scheduler.save_event(self.event)
        self.scheduler.prepare_profile_start("game", "account")
        self.assertIsNone(self.scheduler.load_event("game", "target").next_due_at)

    def test_block_translations_are_present(self):
        self.addCleanup(translator.set_language, translator.LANG)
        for language in ("en", "de"):
            translator.set_language(language)
            for name in ("set_event_due", "reschedule_event"):
                for prefix in ("function.block", "function.block_description"):
                    key = f"{prefix}.{name}"
                    self.assertNotEqual(key, t(key))
                self.assertNotEqual(f"block.{name}.how_to", t(f"block.{name}.how_to"))
        self.assertEqual("Event sofort fällig setzen", t("function.block.set_event_due"))
        self.assertEqual("Event neu planen", t("function.block.reschedule_event"))

    def test_named_target_is_resolved_only_within_current_account(self):
        foreign = Event.from_dict(self.event.to_dict())
        foreign.event_id = "other-target"
        foreign.profile_id = "other-account"
        self.scheduler.save_event(foreign)
        self.scheduler.set_event_due("game", "account", "Gathering", 60)
        self.assertIsNone(self.scheduler.load_event("game", "other-target").next_due_at)
        self.assertIsNotNone(self.scheduler.load_event("game", "target").next_due_at)

    def save_function(self, name, blocks):
        function = BotFunction(
            function_id=name, game_id="game", name=name, saved=True, blocks=blocks,
        )
        path = self.root / "functions" / "game"
        path.mkdir(parents=True, exist_ok=True)
        (path / f"{name}.json").write_text(json.dumps(function.to_dict()), encoding="utf-8")
        return function

    def run_events(self):
        return self.scheduler.run_due_events(
            "game", profile={"game": "game", "botstudio_profile_id": "account", "name": "Test account"},
            function_root=str(self.root / "functions"),
            runner_factory=lambda: FunctionRunner(logger=Mock()),
        )

    def test_running_event_can_reschedule_itself(self):
        # JourneyOfLight: the block targets the event it runs in.
        self.save_function("function", [
            FunctionBlock(block_id="reschedule", block_type="reschedule_event", params={
                "event_id": "Gathering", "delay_seconds": 7200,
            }),
            FunctionBlock(block_id="continue", block_type="success_terminal"),
        ])
        before = datetime.now(timezone.utc)
        results = self.run_events()
        self.assertEqual("success", results[0]["status"])
        due = Event._parse_datetime(self.scheduler.load_event("game", "target").next_due_at)
        self.assertGreaterEqual(due, before + timedelta(seconds=7200))
        self.assertLess(due, datetime.now(timezone.utc) + timedelta(seconds=7201))
        self.assertEqual([], self.run_events())

    def test_scheduler_wires_control_and_skips_postponed_target_in_selected_batch(self):
        self.save_function("source-function", [
            FunctionBlock(block_id="reschedule", block_type="reschedule_event", params={
                "event_id": "Gathering", "delay_seconds": 1800,
            }),
            FunctionBlock(block_id="continue", block_type="success_terminal"),
        ])
        source = Event(
            event_id="source", game_id="game", profile_id="account", name="Source",
            sort_order=-1, tasks=[Task(name="Check", function_ids=["source-function"])],
        )
        self.scheduler.save_event(source)
        results = self.run_events()
        self.assertEqual(["source"], [result["event_id"] for result in results])
        self.assertEqual("success", results[0]["status"])
        self.assertFalse(self.scheduler.load_event("game", "target").is_due())
        self.assertIsNone(self.scheduler.load_event("game", "source").next_due_at)
        self.assertFalse(self.scheduler.load_event("game", "source").is_due())

    def test_due_now_target_executes_at_next_scheduler_opportunity(self):
        self.save_function("source-function", [
            FunctionBlock(block_id="due", block_type="set_event_due", params={"event_id": "Gathering"}),
            FunctionBlock(block_id="continue", block_type="success_terminal"),
        ])
        self.save_function("function", [FunctionBlock(block_id="finish", block_type="success_terminal")])
        self.event.next_due_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        self.scheduler.save_event(self.event)
        source = Event(
            event_id="source", game_id="game", profile_id="account", name="Source",
            tasks=[Task(name="Trigger", function_ids=["source-function"])],
        )
        self.scheduler.save_event(source)
        self.assertEqual(["source"], [result["event_id"] for result in self.run_events()])
        self.assertEqual(["target"], [result["event_id"] for result in self.run_events()])
        self.assertEqual([], self.run_events())

    def test_manual_queue_is_durable_and_overrides_disabled_event_schedule(self):
        now = datetime.now(timezone.utc)
        event = self.scheduler.load_event("game", "target")
        event.enabled = False
        event.next_due_at = (now + timedelta(days=1)).isoformat()
        self.scheduler.save_event(event)

        queued = self.scheduler.queue_event_now(
            self.scheduler.load_event("game", "target"),
            now=now,
        )
        restarted_scheduler = SchedulerService(
            base_path=str(self.root / "scheduler"),
            logger=Mock(),
        )
        persisted = restarted_scheduler.load_event("game", "target")

        self.assertEqual("queued", queued.status)
        self.assertEqual("queued", persisted.status)
        self.assertTrue(persisted.is_due(now))
        self.assertEqual(
            ["target"],
            [
                event.event_id
                for event in restarted_scheduler.list_execution_events(
                    "game",
                    profile_id="account",
                    now=now,
                    overdue_only=True,
                )
            ],
        )

    def test_new_manual_queue_runs_before_other_due_events_after_current_event(self):
        now = datetime.now(timezone.utc)
        first = Event(
            event_id="first",
            game_id="game",
            profile_id="account",
            name="First",
            sort_order=0,
            next_due_at=(now - timedelta(seconds=1)).isoformat(),
            tasks=[Task(name="First task", function_ids=["function"])],
        )
        next_due = Event(
            event_id="next",
            game_id="game",
            profile_id="account",
            name="Next",
            sort_order=1,
            next_due_at=(now - timedelta(seconds=1)).isoformat(),
            tasks=[Task(name="Next task", function_ids=["function"])],
        )
        target = self.scheduler.load_event("game", "target")
        target.enabled = False
        target.next_due_at = (now + timedelta(days=1)).isoformat()
        self.scheduler.save_event(target)
        self.scheduler.save_event(first)
        self.scheduler.save_event(next_due)
        executed = []

        def run_event(event, **_kwargs):
            executed.append(event.event_id)
            completed = self.scheduler.load_event("game", event.event_id)
            completed.status = "success"
            completed.last_finished_at = now.isoformat()
            completed.next_due_at = None
            self.scheduler.save_event(completed)
            if event.event_id == "first":
                self.scheduler.queue_event_now(
                    self.scheduler.load_event("game", "target"),
                    now=now,
                )
            return {
                "event_id": event.event_id,
                "executed": ["function"],
                "status": "success",
            }

        with patch.object(self.scheduler, "run_event", side_effect=run_event):
            results = self.scheduler.run_due_events(
                "game",
                profile={
                    "game": "game",
                    "botstudio_profile_id": "account",
                    "name": "Test account",
                },
                now=now,
            )

        self.assertEqual(["first", "target", "next"], executed)
        self.assertEqual(["first", "target", "next"], [
            result["event_id"] for result in results
        ])

    def test_failed_execution_without_retry_has_a_persisted_next_due(self):
        self.save_function("function", [FunctionBlock(block_id="failure", block_type="failure_terminal")])
        before = datetime.now(timezone.utc)
        results = self.run_events()
        self.assertEqual("failed", results[0]["status"])
        due = Event._parse_datetime(self.scheduler.load_event("game", "target").next_due_at)
        self.assertGreaterEqual(due, before + timedelta(minutes=1))
        self.assertEqual([], self.run_events())

    def test_scheduler_exception_and_missing_runner_receive_retry(self):
        with patch.object(self.scheduler, "run_event", side_effect=RuntimeError("test exception")):
            self.assertEqual("failed", self.run_events()[0]["status"])
        self.assertIsNotNone(self.scheduler.load_event("game", "target").next_due_at)
        self.scheduler.run_event(
            self.event, profile={"game": "game", "botstudio_profile_id": "account"},
        )
        self.assertIsNotNone(self.scheduler.load_event("game", "target").next_due_at)

    def test_persistence_failure_is_not_reported_as_success(self):
        with patch.object(self.scheduler, "save_event", return_value=self.event):
            result = self.run_block("set_event_due", {"event_id": "target"})
        self.assertEqual("failed", result["outcome"])
        self.assertIn("persist", result["error"])

    def test_helper_inherits_scheduler_control(self):
        helper = self.save_function("helper", [
            FunctionBlock(block_id="due", block_type="set_event_due", params={"event_id": "target"}),
            FunctionBlock(block_id="finish", block_type="success_terminal"),
        ])
        service = Mock()
        service.load.return_value = helper
        function = BotFunction(
            function_id="caller", game_id="game", name="Caller",
            blocks=[FunctionBlock(block_id="call", block_type="call_function", params={"function_id": "helper"})],
        )
        result = FunctionRunner(function_service=service, logger=Mock()).run(
            function, self.context(), {"game": "game"},
        )
        self.assertEqual("success", result["outcome"])
        self.assertTrue(self.scheduler.load_event("game", "target").is_due())


if __name__ == "__main__":
    def timeout():
        print("TEST RESULT: TIMEOUT", flush=True)
        faulthandler.dump_traceback()
        os._exit(2)

    started = time.monotonic()
    print("TEST START: event rescheduling", flush=True)
    watchdog = Timer(45, timeout)
    watchdog.daemon = True
    watchdog.start()
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(EventReschedulingTests)
    )
    watchdog.cancel()
    print(f"TEST DURATION: {time.monotonic() - started:.3f}s", flush=True)
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE", flush=True)
    sys.exit(0 if result.wasSuccessful() else 1)
