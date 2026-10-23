import faulthandler
import json
import logging
import os
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Timer
from unittest.mock import Mock, patch

from models.bot_function import BotFunction
from models.execution_context import ExecutionContext
from models.function_block import FunctionBlock
from scheduler.models import Event, SchedulerService, Task
from services.function_block_catalog import BLOCK_CALL_FUNCTION, BLOCK_FAILURE, BLOCK_REPEAT, BLOCK_SUCCESS
from services.function_runner import FunctionRunner
from services.logging_service import BotStudioLogger, RecentErrorJournal
from services.statistics_service import StatisticsService
from services.watchdog_process_service import WatchdogProcessService


class TaskErrorContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.functions = self.root / "functions"
        (self.functions / "game").mkdir(parents=True)
        self.profile = {"game": "game", "id": "profile", "name": "Main account"}
        self.journal = RecentErrorJournal()
        journal_patch = patch("services.logging_service._RECENT_ERROR_JOURNAL", self.journal)
        journal_patch.start()
        self.addCleanup(journal_patch.stop)
        self.logger = object.__new__(BotStudioLogger)
        self.logger.module = "scheduler"
        self.logger.logger = Mock()
        self.stats = StatisticsService(str(self.root / "statistics.json"))
        self.scheduler = SchedulerService(
            base_path=str(self.root / "scheduler"),
            logger=self.logger,
            statistics_service=self.stats,
        )

    def function(self, name, blocks):
        function = BotFunction.create(name=name, game_id="game", author="test")
        function.blocks = blocks
        function.saved = True
        (self.functions / "game" / f"{function.function_id}.json").write_text(
            json.dumps(function.to_dict()), encoding="utf-8",
        )
        return function

    def event(self, function_id, task_id="task"):
        return Event(
            event_id="event", game_id="game", profile_id="profile", name="Daily rewards",
            tasks=[Task(task_id=task_id, name="Collect reward", function_ids=[function_id])],
        )

    def run_event(self, event, runner, **kwargs):
        return self.scheduler.run_event(
            event, profile=self.profile, function_root=str(self.functions),
            runner_factory=lambda: runner, persist=False, **kwargs,
        )

    def notifications(self, events):
        messages = []
        watchdog = WatchdogProcessService(
            ["python.exe", "main.py"], str(self.root),
            logger=logging.getLogger("test.task.errors"),
            notification_handler=messages.append,
        )
        watchdog._notify_new_studio_errors({
            "error_journal": {"stream_id": self.journal.stream_id, "events": []},
        })
        watchdog._notify_new_studio_errors({
            "error_journal": {"stream_id": self.journal.stream_id, "events": events},
        })
        watchdog._notify_new_studio_errors({
            "error_journal": {"stream_id": self.journal.stream_id, "events": events},
        })
        self.assertEqual(1, len(messages))
        return messages[0]

    def test_nested_function_failure_has_named_chain_and_counts_once(self):
        leaf = self.function("Read reward timer", [FunctionBlock.create(BLOCK_FAILURE)])
        helper = self.function("Reward helper", [
            FunctionBlock.create(BLOCK_CALL_FUNCTION, params={"function_id": leaf.function_id}),
        ])
        parent = self.function("Collect rewards", [
            FunctionBlock.create(BLOCK_CALL_FUNCTION, params={"function_id": helper.function_id}),
        ])
        functions = {function.function_id: function for function in (leaf, helper, parent)}
        function_service = Mock()
        function_service.load.side_effect = lambda game, reference: functions.get(reference)
        runner = FunctionRunner(function_service=function_service, logger=self.logger)
        for expected in (1, 2):
            self.assertEqual("failed", self.run_event(self.event(parent.function_id), runner)["status"])
            records = [
                item for item in self.journal.snapshot()["events"]
                if item["event"] == "Scheduler Task Failed"
            ]
            context = records[-1]["context"]
            self.assertEqual(expected, context["task_failures_all_time"])
            self.assertEqual("Collect reward", context["task_name"])
            self.assertEqual("Daily rewards", context["event_name"])
            self.assertEqual("Main account", context["profile"])
            self.assertEqual("Read reward timer", context["failed_function_name"])
            self.assertEqual(["Collect rewards", "Reward helper", "Read reward timer"], context["function_chain"])
            self.assertEqual(BLOCK_FAILURE, context["failed_block_type"])
        self.assertEqual(2, len(records))
        message = self.notifications(self.journal.snapshot()["events"])
        self.assertIn("Task: Collect reward", message)
        self.assertIn("2 failed task execution(s)", message)
        self.assertIn("Failed function: Read reward timer", message)
        self.assertIn("2 all-time", message)
        self.assertIn("Collect rewards -> Reward helper -> Read reward timer", message)
        self.assertLessEqual(len(message), 3500)

    def test_success_and_cancel_do_not_increment_error_counter(self):
        function = self.function("Normal task", [FunctionBlock.create(BLOCK_SUCCESS)])
        runner = FunctionRunner(logger=self.logger)
        self.assertEqual("success", self.run_event(self.event(function.function_id), runner)["status"])
        self.assertEqual("cancelled", self.run_event(
            self.event(function.function_id), runner, cancellation_checker=lambda: True,
        )["status"])
        stats = self.stats.snapshot(all_time=True)["game:profile"]["task_executions"]["task"]
        self.assertEqual(1, stats["success"])
        self.assertEqual(0, stats["failure"])
        self.assertFalse(any(
            item["event"] == "Scheduler Task Failed" for item in self.journal.snapshot()["events"]
        ))

    def test_failed_helper_recovered_by_branch_does_not_poison_next_function(self):
        helper = self.function("Sometimes fails", [FunctionBlock.create(BLOCK_FAILURE)])
        recovery = FunctionBlock.create(BLOCK_SUCCESS)
        parent = self.function("Recovered task", [
            FunctionBlock.create(BLOCK_CALL_FUNCTION,
                                 params={"function_id": helper.function_id},
                                 branches={"failed": [recovery]}),
        ])
        function_service = Mock()
        function_service.load.return_value = helper
        runner = FunctionRunner(function_service=function_service, logger=self.logger)
        result = runner.run(parent, ExecutionContext(game_id="game", profile_id="profile",
                            function_id=parent.function_id), self.profile)
        self.assertEqual("success", result["outcome"])
        self.assertNotIn("failure", result)
        self.assertTrue(any(
            step.get("function_name") == "Sometimes fails" for step in result["trace"]
        ))
        failing = self.function("Unrelated failure", [FunctionBlock.create(BLOCK_FAILURE)])
        result = runner.run(failing, ExecutionContext(game_id="game", profile_id="profile",
                            function_id=failing.function_id), self.profile)
        self.assertEqual("Unrelated failure", result["failure"]["function_name"])
        self.assertEqual(1, len(result["failure"]["function_chain"]))

    def test_recovered_helper_followed_by_own_failure_names_own_block(self):
        helper = self.function("Handled helper", [FunctionBlock.create(BLOCK_FAILURE)])
        own_failure = FunctionBlock.create(BLOCK_FAILURE)
        parent = self.function("Own failure", [
            FunctionBlock.create(
                BLOCK_CALL_FUNCTION, params={"function_id": helper.function_id},
                branches={"failed": [own_failure]},
            ),
        ])
        function_service = Mock()
        function_service.load.return_value = helper
        runner = FunctionRunner(function_service=function_service, logger=self.logger)
        result = runner.run(
            parent, ExecutionContext(game_id="game", profile_id="profile",
                                     function_id=parent.function_id), self.profile,
        )
        self.assertEqual("Own failure", result["failure"]["function_name"])
        self.assertEqual(own_failure.block_id, result["failure"]["block_id"])
        self.assertEqual(["Own failure"], [
            entry["function_name"] for entry in result["failure"]["function_chain"]
        ])

    def test_block_error_on_wired_route_is_not_reported(self):
        function_service = Mock()
        function_service.load.return_value = None
        runner = FunctionRunner(function_service=function_service, logger=self.logger)
        wired = self.function("Wired", [FunctionBlock.create(
            BLOCK_CALL_FUNCTION, params={"function_id": "missing"},
            branches={"failed": [FunctionBlock.create(BLOCK_SUCCESS)]},
        )])
        result = runner.run(wired, ExecutionContext(game_id="game", profile_id="profile",
                            function_id=wired.function_id), self.profile)
        self.assertEqual("success", result["outcome"])
        self.assertEqual([], self.journal.snapshot()["events"])
        unwired = self.function("Unwired", [FunctionBlock.create(
            BLOCK_CALL_FUNCTION, params={"function_id": "missing"},
        )])
        result = runner.run(unwired, ExecutionContext(game_id="game", profile_id="profile",
                            function_id=unwired.function_id), self.profile)
        self.assertEqual("failed", result["outcome"])
        self.assertEqual(["Function Block Failed"], [
            item["event"] for item in self.journal.snapshot()["events"]
        ])

    def test_embedded_failure_inside_repeat_keeps_deepest_function(self):
        helper = self.function("Repeat helper", [FunctionBlock.create(BLOCK_FAILURE)])
        parent = self.function("Repeat task", [
            FunctionBlock.create(
                BLOCK_REPEAT, params={"max_iterations": 1, "timeout_ms": 1000},
                branches={"body": [
                    FunctionBlock.create(BLOCK_CALL_FUNCTION, params={"function_id": helper.function_id}),
                ]},
            ),
        ])
        function_service = Mock()
        function_service.load.return_value = helper
        runner = FunctionRunner(function_service=function_service, logger=self.logger)
        result = runner.run(
            parent, ExecutionContext(game_id="game", profile_id="profile",
                                     function_id=parent.function_id), self.profile,
        )
        self.assertEqual("Repeat helper", result["failure"]["function_name"])

    def test_maximum_journal_fields_and_multiple_tasks_fit_telegram_message(self):
        for index in range(4):
            self.journal.record("ERROR", "scheduler", "Scheduler Task Failed", {
                **dict.fromkeys((
                    "profile", "event_name", "task_name", "function_name",
                    "failed_function_name", "failed_block_type", "failure_reason",
                ), "\u00e4" * 100),
                "function_chain": ["\u00e4" * 100] * 4,
                "task_failures_current": 1000 + index,
                "task_failures_all_time": 10000 + index,
                "task_successes_all_time": 90000,
            })
        message = self.notifications(self.journal.snapshot()["events"])
        self.assertLessEqual(len(message), 3500)
        self.assertIn("10003 all-time", message)
        self.assertIn("1 additional error", message)

    def test_missing_unsaved_corrupt_exception_and_timeout_count_once_each(self):
        saved = self.function("Throwing function", [FunctionBlock.create(BLOCK_SUCCESS)])
        unsaved = self.function("Draft function", [FunctionBlock.create(BLOCK_SUCCESS)])
        unsaved.saved = False
        (self.functions / "game" / f"{unsaved.function_id}.json").write_text(
            json.dumps(unsaved.to_dict()), encoding="utf-8",
        )
        corrupt = self.functions / "game" / "corrupt.json"
        corrupt.write_text("{", encoding="utf-8")
        runner = Mock()
        for reference in ("missing", unsaved.function_id, "corrupt", saved.function_id):
            runner.run.side_effect = RuntimeError("raw-private-exception")
            self.assertEqual("failed", self.run_event(self.event(reference), runner)["status"])
        runner.run.side_effect = None
        runner.run.return_value = {"outcome": "timeout"}
        self.assertEqual("timeout", self.run_event(self.event(saved.function_id), runner)["status"])
        records = [item for item in self.journal.snapshot()["events"] if item["event"] == "Scheduler Task Failed"]
        self.assertEqual(5, len(records))
        self.assertEqual(5, records[-1]["context"]["task_failures_all_time"])
        self.assertNotIn("raw-private-exception", json.dumps(records))

    def test_storage_failure_preserves_original_failure_and_marks_counts_unavailable(self):
        self.stats.file_name.write_text("{", encoding="utf-8")
        runner = Mock()
        result = self.run_event(self.event("missing"), runner)
        self.assertEqual("failed", result["status"])
        message = self.notifications(self.journal.snapshot()["events"])
        self.assertIn("Task: Collect reward", message)
        self.assertIn("counters: unavailable", message)
        self.assertEqual("{", self.stats.file_name.read_text(encoding="utf-8"))

    def test_persistent_counters_keep_task_profile_isolation_and_reset_semantics(self):
        self.stats.record_task("game", "profile", "Main", "task", "Task", "failure")
        self.stats.record_task("game", "profile", "Main", "other", "Other", "failure")
        self.stats.record_task("game", "other-profile", "Other", "task", "Task", "failure")
        reopened = StatisticsService(str(self.stats.file_name))
        reopened.reset_current()
        counters = reopened.record_task("game", "profile", "Main", "task", "Task", "failure")
        self.assertEqual(1, counters["task_failures_current"])
        self.assertEqual(2, counters["task_failures_all_time"])
        self.assertEqual(1, reopened.snapshot(True)["game:other-profile"]["task_executions"]["task"]["failure"])
        self.assertEqual(1, reopened.snapshot(True)["game:profile"]["task_executions"]["other"]["failure"])

    def test_parallel_statistics_writes_do_not_lose_counts(self):
        services = [StatisticsService(str(self.stats.file_name)) for _ in range(4)]
        def record(index):
            services[index % 4].record_task("game", "profile", "Main", "task", "Task", "failure")
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(record, index) for index in range(40)]
            for future in futures:
                future.result(timeout=5)
        self.assertEqual(40, self.stats.snapshot(True)["game:profile"]["task_executions"]["task"]["failure"])

    def test_journal_context_is_allowlisted_redacted_isolated_and_size_bounded(self):
        for _ in range(50):
            self.journal.record("ERROR", "scheduler", "\u00e4" * 120, {
                "task_name": "Collect token=private",
                "event_name": "\u00e4" * 100,
                "function_name": "\u00e4" * 100,
                "failed_function_name": "\u00e4" * 100,
                "function_chain": ["\u00e4" * 100] * 10,
                "error": "private stacktrace", "value": "private value",
                "task_failures_all_time": 7,
            })
        snapshot = self.journal.snapshot()
        self.assertLessEqual(len(json.dumps(snapshot).encode("utf-8")), self.journal.MAX_SNAPSHOT_BYTES)
        from services.watchdog_control_service import WatchdogControlService
        from services.watchdog_ipc_service import WatchdogIpcServer
        runtime = Mock()
        runtime.get_status.return_value = {"running": True, "status": "running"}
        runtime.get_recovery_state.return_value = {"safe_to_resume": True, "interrupted_events": []}
        status = WatchdogControlService(runtime).handle_request("status")
        wire_payload = json.dumps(
            {"id": "test", "ok": True, "result": status}, separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        self.assertLessEqual(len(wire_payload), WatchdogIpcServer.MAX_MESSAGE_BYTES)
        self.assertNotIn("private", json.dumps(snapshot))
        snapshot["events"][-1]["context"]["task_name"] = "modified"
        self.assertNotEqual("modified", self.journal.snapshot()["events"][-1]["context"]["task_name"])

    def test_event_without_schedule_is_armed_once_instead_of_reporting_error(self):
        event = self.event("unused")
        next_due = self.scheduler._next_due_at(
            event, datetime.now(timezone.utc), None, "game", "profile",
            profile_name="Main account",
        )
        self.assertIsNone(next_due)
        self.assertFalse(any(
            item["event"] == "Scheduler Event Fehlerfall"
            for item in self.journal.snapshot()["events"]
        ))

    def test_successful_init_does_not_require_a_repeat_schedule(self):
        function = self.function("Init setup", [FunctionBlock.create(BLOCK_SUCCESS)])
        event = self.event(function.function_id)
        event.is_init = True
        event.name = "Init"
        result = self.run_event(event, FunctionRunner(logger=self.logger))
        self.assertEqual("success", result["status"])
        self.assertIsNone(event.next_due_at)
        self.assertFalse(any(
            item["event"] == "Scheduler Event Fehlerfall"
            for item in self.journal.snapshot()["events"]
        ))

    def test_regular_event_without_schedule_runs_without_error_notification(self):
        function = self.function("Collect", [FunctionBlock.create(BLOCK_SUCCESS)])
        event = self.event(function.function_id)
        self.assertEqual("success", self.run_event(event, FunctionRunner(logger=self.logger))["status"])
        self.assertIsNone(event.next_due_at)
        self.assertFalse(any(
            item["level"] in {"ERROR", "CRITICAL"} for item in self.journal.snapshot()["events"]
        ))

    def test_task_summary_does_not_hide_unrelated_errors_or_event_configuration_errors(self):
        self.journal.record("ERROR", "function", "Function Block Failed")
        self.journal.record("ERROR", "scheduler", "Scheduler Task Failed", {"task_name": "Collect"})
        self.journal.record("ERROR", "scheduler", "Scheduler Event Fehlerfall", {"event_name": "Daily"})
        self.journal.record("CRITICAL", "runtime", "Independent runtime error")
        message = self.notifications(self.journal.snapshot()["events"])
        self.assertIn("Task: Collect", message)
        self.assertIn("Event: Daily", message)
        self.assertIn("Independent runtime error", message)
        self.assertNotIn("Function Block Failed", message)

    def test_malformed_statistics_shapes_never_reset_existing_counts(self):
        for data in (
            [],
            {"current": []},
            {"current": {"game:profile": []}},
            {"current": {"game:profile": {"task_executions": {"task": {"failure": -1, "success": 0}}}}},
        ):
            original = json.dumps(data)
            self.stats.file_name.write_text(original, encoding="utf-8")
            with self.assertRaises(RuntimeError):
                self.stats.record_task("game", "profile", "Main", "task", "Task", "failure")
            self.assertEqual(original, self.stats.file_name.read_text(encoding="utf-8"))

    def test_statistics_view_surfaces_storage_error_without_zero_counters_or_reset(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from ui.views.statistics_view import StatisticsView
        app = QApplication.instance() or QApplication([])
        self.stats.file_name.write_text("{", encoding="utf-8")
        with patch("ui.views.statistics_view.get_logger", return_value=self.logger), patch(
            "ui.views.statistics_view.ProfileService"
        ):
            view = StatisticsView(self.stats)
        self.addCleanup(view.close)
        self.assertFalse(view.error_label.isHidden())
        self.assertFalse(view.reset_button.isEnabled())
        self.assertEqual(0, view.cards_layout.count())
        self.stats.file_name.write_text(
            json.dumps({"current": {}, "all_time": {}}), encoding="utf-8",
        )
        view.refresh()
        self.assertTrue(view.error_label.isHidden())
        self.assertTrue(view.reset_button.isEnabled())
        self.assertGreater(view.cards_layout.count(), 0)
        app.processEvents()

    def test_statistics_view_marks_deleted_tasks_and_sorts_them_last(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from ui.views.statistics_view import StatisticsView
        app = QApplication.instance() or QApplication([])
        self.stats.record_task("game", "profile", "Main account", "gone", "Old task", "failure", 1.0)
        self.stats.record_task("game", "profile", "Main account", "task", "Collect reward", "success", 1.0)
        self.scheduler.save_event(self.event("fn"))
        with patch("ui.views.statistics_view.get_logger", return_value=self.logger), patch(
            "ui.views.statistics_view.ProfileService"
        ):
            view = StatisticsView(self.stats, scheduler_service=self.scheduler)
        self.addCleanup(view.close)
        table = view.all_time_table
        names = [table.item(row, 1).text() for row in range(table.rowCount())]
        self.assertEqual(["Collect reward", "Old task (deleted)"], names)
        app.processEvents()

    def init_gate(self, check_outcomes, switch_outcome="success"):
        check = self.function("Check profile", [FunctionBlock.create(BLOCK_SUCCESS)])
        switch = self.function("Switch profile", [FunctionBlock.create(BLOCK_SUCCESS)])
        outcomes = {check.function_id: list(check_outcomes), switch.function_id: [switch_outcome]}
        calls = []

        class ScriptedRunner:
            def run(_self, function, context, profile):
                calls.append(function.function_id)
                outcome = outcomes[function.function_id].pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return {"outcome": outcome}

        event = Event(
            event_id="init", game_id="game", profile_id="profile", name="Init", is_init=True,
            tasks=[
                Task(task_id="check", name="Profile Check", kind="profile_validation",
                     protected=True, function_ids=[check.function_id]),
                Task(task_id="switch", name="Profile Switch", kind="profile_switch",
                     protected=True, function_ids=[switch.function_id]),
            ],
        )
        result = self.run_event(event, ScriptedRunner())
        tasks = self.stats.snapshot(all_time=True)["game:profile"]
        errors = [
            item for item in self.journal.snapshot()["events"]
            if item["event"] == "Scheduler Task Failed"
        ]
        return result, tasks, errors, calls, switch.function_id

    def test_negative_profile_check_is_routing_and_switch_is_counted(self):
        result, account, errors, calls, switch_id = self.init_gate(["failed", "success"])
        self.assertEqual("success", result["status"])
        self.assertIn(switch_id, calls)
        self.assertEqual({"success": 2, "failure": 0}, {
            key: account["task_executions"]["check"][key] for key in ("success", "failure")
        })
        self.assertEqual(1, account["task_executions"]["switch"]["success"])
        self.assertEqual(1, account["profile_switches"])
        self.assertEqual([], errors)

    def test_failing_recheck_is_real_failure_without_switch_count(self):
        result, account, errors, _calls, _switch = self.init_gate(["failed", "failed"])
        self.assertEqual("failed", result["status"])
        self.assertEqual(1, account["task_executions"]["check"]["success"])
        self.assertEqual(1, account["task_executions"]["check"]["failure"])
        self.assertEqual(0, account.get("profile_switches", 0))
        self.assertEqual(1, len(errors))

    def test_profile_check_exception_is_counted_as_failure_not_routing(self):
        result, account, errors, _calls, _switch = self.init_gate(
            [RuntimeError("adb gone"), RuntimeError("adb gone")],
        )
        self.assertNotEqual("success", result["status"])
        self.assertEqual(0, account["task_executions"]["check"]["success"])
        self.assertEqual(2, account["task_executions"]["check"]["failure"])
        self.assertEqual(0, account.get("profile_switches", 0))
        self.assertEqual(2, len(errors))

    def test_statistics_failure_does_not_break_profile_switch(self):
        with patch.object(self.stats, "record_profile_switch", side_effect=RuntimeError("unavailable")):
            result, _account, _errors, calls, switch_id = self.init_gate(["failed", "success"])
        self.assertEqual("success", result["status"])
        self.assertIn(switch_id, calls)

    def test_statistics_failure_does_not_invalidate_successful_emulator_start(self):
        from services.runtime_recovery_service import RuntimeRecoveryService
        recovery = RuntimeRecoveryService(Mock(), Mock(), Mock(), logger=self.logger)
        recovery.logger = self.logger
        recovery.app_config_service = None
        recovery._last_emulator_start_at = None
        recovery.emulator_manager = Mock()
        recovery.emulator_manager.start_instance.return_value = True
        recovery.statistics_service = Mock()
        recovery.statistics_service.record_emulator_start.side_effect = RuntimeError("unavailable")
        state = {"emulator_start_attempts": 0}
        result = recovery._start_emulator(
            {"emulator_instance": "mock"}, state, {}, 3, "Start requested",
        )
        self.assertEqual("Recovering", result["state"])
        self.assertEqual(1, state["emulator_start_attempts"])
        self.assertTrue(any(
            item["event"] == "Emulator Start Statistics Failed" for item in self.journal.snapshot()["events"]
        ))


if __name__ == "__main__":
    def timeout():
        print("TEST RESULT: TIMEOUT", flush=True)
        faulthandler.dump_traceback()
        os._exit(2)

    started = time.monotonic()
    print("TEST START: task error context", flush=True)
    watchdog = Timer(30, timeout)
    watchdog.daemon = True
    watchdog.start()
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(TaskErrorContextTests)
    )
    watchdog.cancel()
    print(f"TEST DURATION: {time.monotonic() - started:.3f}s", flush=True)
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE", flush=True)
    sys.exit(0 if result.wasSuccessful() else 1)
