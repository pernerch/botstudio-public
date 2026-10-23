"""Value-flow logging against real JSON log files, without emulator or live stores."""

from contextlib import nullcontext
from datetime import datetime, timezone
import faulthandler
import json
import logging
import os
from pathlib import Path
import tempfile
from threading import Timer
import time
import unittest
from unittest.mock import Mock

from models.bot_function import BotFunction
from models.execution_context import ExecutionContext
from models.function_block import FunctionBlock
from models.function_connection import FunctionConnection
from services.function_runner import FunctionRunner
from services.logging_service import BotStudioLogger, JsonLogFormatter


class FunctionValueLoggingTests(unittest.TestCase):
    def setUp(self):
        self.started = time.monotonic()
        print(f"TEST START: {self.id()}", flush=True)
        watchdog = Timer(30, self.timeout)
        watchdog.daemon = True
        watchdog.start()
        self.addCleanup(watchdog.cancel)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.log_path = Path(temporary.name) / "function.log"
        handler = logging.FileHandler(self.log_path, encoding="utf-8")
        handler.setLevel(logging.INFO)
        handler.setFormatter(JsonLogFormatter())
        logger = logging.Logger("isolated-function-values", level=logging.INFO)
        logger.addHandler(handler)
        self.addCleanup(handler.close)
        self.logger = BotStudioLogger.__new__(BotStudioLogger)
        self.logger.module = "function"
        self.logger.logger = logger
        self.ocr = Mock()
        self.ocr.read.return_value = "01:02:03"
        screen = Mock()
        screen.capture.return_value = "not-read-by-mocked-ocr.png"
        activity = Mock()
        activity.track.side_effect = lambda *_args: nullcontext()
        self.runner = FunctionRunner(
            logger=self.logger,
            ocr_service=self.ocr,
            screen_service=screen,
            activity_service=activity,
        )
        self.context = ExecutionContext(
            profile_id="profile", function_id="gather", game_id="game"
        )
        self.profile = {"name": "Test profile", "game": "game"}

    @staticmethod
    def timeout():
        print("TEST RESULT: TIMEOUT", flush=True)
        faulthandler.dump_traceback()
        os._exit(2)

    def tearDown(self):
        print(f"TEST END: {self.id()} ({time.monotonic() - self.started:.3f}s)", flush=True)

    def records(self, event=None):
        records = [
            json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()
        ]
        return [r for r in records if event is None or r["event"] == event]

    def function(self, blocks):
        function = BotFunction(
            function_id="gather",
            name="CheckAllGatherAndAdjustDueDate",
            game_id="game",
            blocks=blocks,
            connections=[
                FunctionConnection(
                    source_block_id=source.block_id,
                    source_outcome="ok",
                    target_block_id=target.block_id,
                )
                for source, target in zip(blocks, blocks[1:])
            ],
        )
        function.normalize_graph()
        return function

    def ocr_block(self, **params):
        return FunctionBlock(
            block_id="ocr",
            block_type="ocr_read",
            name="Read OCR",
            params={
                "region": {"x": 0, "y": 0, "width": 40, "height": 20},
                "expect": "time",
                "delay_before_read_ms": 0,
                **params,
            },
            output_as="Gather Meat",
        )

    def test_ocr_parse_assignment_and_due_date_are_visible_at_info(self):
        due = datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc)
        self.context.event_due_handler = Mock(return_value=due.isoformat())
        function = self.function([
            self.ocr_block(),
            FunctionBlock(
                block_id="parse",
                block_type="duration_parse",
                params={"input": "$Gather Meat"},
                output_as="Gather Meat",
            ),
            FunctionBlock(
                block_id="due",
                block_type="reschedule_event",
                params={"event_id": "Gather Meat", "delay_variable": "$Gather Meat"},
                output_as="Gather Meat",
            ),
        ])
        result = self.runner.run(function, self.context, self.profile)
        self.assertEqual("success", result["outcome"])
        records = self.records("Function Block Executed")
        self.assertEqual(3, len(records))
        self.assertEqual("01:02:03", records[0]["details"]["value"])
        self.assertEqual("Gather Meat", records[0]["details"]["output_as"])
        parse = records[1]["details"]
        self.assertEqual(3723.0, parse["value"])
        self.assertIn(
            {"operation": "read", "scope": "profile", "name": "Gather Meat", "value": "01:02:03"},
            parse["variable_accesses"],
        )
        self.assertIn(
            {"operation": "write", "scope": "profile", "name": "Gather Meat", "value": 3723.0},
            parse["variable_accesses"],
        )
        adjusted = self.records("Function Event Due Date Adjusted")[0]
        self.assertEqual(3723.0, adjusted["details"]["delay_seconds"])
        self.assertEqual(due.isoformat(), adjusted["details"]["due_at"])
        for record in records:
            self.assertEqual("INFO", record["severity"])
            self.assertEqual(function.name, record["details"]["function_name"])
            self.assertEqual("gather", record["function_id"])
            self.assertEqual("profile", record["details"]["profile_id"])
            self.assertIn("value=", record["message"])
        self.assertIn("01:02:03", records[1]["message"])
        self.assertEqual(due.isoformat(), self.context.get("Gather Meat"))

    def test_single_block_debug_logs_same_value_and_assignment(self):
        function = self.function([self.ocr_block()])
        result = self.runner.run(
            function, self.context, self.profile, selected_block_id="ocr"
        )
        self.assertEqual("success", result["outcome"])
        record = self.records("Function Block Executed")[0]
        self.assertEqual("01:02:03", record["details"]["value"])
        self.assertTrue(any(
            a["operation"] == "write" and a["value"] == "01:02:03"
            for a in record["details"]["variable_accesses"]
        ))

    def test_calculation_keeps_resolved_expression_and_numeric_output(self):
        self.context.set("Seconds", 12)
        function = self.function([FunctionBlock(
            block_id="math",
            block_type="math",
            params={"left": "$Seconds", "operator": "+", "right": 5},
            output_as="Next",
        )])
        self.runner.run(function, self.context, self.profile)
        details = self.records("Function Block Executed")[0]["details"]
        self.assertEqual(17, details["value"])
        self.assertEqual("12.0 + 5.0 = 17", details["calculation"]["expression"])
        self.assertEqual(17, self.context.get("Next"))

    def test_failed_assignment_is_not_logged_as_success(self):
        self.context.readonly_variables["Gather Meat"] = 42
        result = self.runner.run(
            self.function([self.ocr_block()]), self.context, self.profile
        )
        self.assertEqual("failed", result["outcome"])
        record = self.records("Function Block Executed")[0]
        self.assertEqual("failed", record["details"]["outcome"])
        self.assertIn("read-only", record["details"]["error"])
        self.assertFalse(any(
            a["operation"] == "write"
            for a in record["details"]["variable_accesses"]
        ))
        self.assertEqual(42, self.context.get("Gather Meat"))

    def test_failed_debug_assignment_is_not_logged_as_success(self):
        self.context.readonly_variables["Gather Meat"] = 42
        result = self.runner.run(
            self.function([self.ocr_block()]), self.context, self.profile,
            selected_block_id="ocr",
        )
        self.assertEqual("failed", result["outcome"])
        record = self.records("Function Block Executed")[0]
        self.assertEqual("failed", record["details"]["outcome"])

    def test_ocr_fallback_is_distinguishable_from_a_real_zero(self):
        self.ocr.read.side_effect = RuntimeError("no valid time recognized")
        result = self.runner.run(
            self.function([self.ocr_block(default_value="0")]),
            self.context, self.profile,
        )
        self.assertEqual("success", result["outcome"])
        fallback = self.records("Function OCR Default Used")[0]
        self.assertEqual("WARNING", fallback["severity"])
        self.assertEqual("0", fallback["details"]["value"])
        self.assertIn("no valid time recognized", fallback["details"]["error"])
        self.assertEqual("0", self.context.get("Gather Meat"))

    def test_own_assignment_without_output_as_is_visible(self):
        self.context.set("Source", 18)
        self.runner.run(self.function([FunctionBlock(
            block_id="assign",
            block_type="variable",
            params={"mode": "write", "variable": "Target", "value": "$Source"},
        )]), self.context, self.profile)
        details = self.records("Function Block Executed")[0]["details"]
        self.assertIn(
            {"operation": "write", "scope": "profile", "name": "Target", "value": 18},
            details["variable_accesses"],
        )

    def test_empty_zero_and_invalid_inputs_are_not_hidden(self):
        for raw, expected in ((None, "failed"), ("0", "ok"), ("invalid", "failed")):
            with self.subTest(raw=raw):
                self.context.set("Raw", raw)
                self.runner.run(self.function([FunctionBlock(
                    block_id="parse", block_type="duration_parse",
                    params={"input": "$Raw"},
                )]), self.context, self.profile)
                details = self.records("Function Block Executed")[-1]["details"]
                self.assertEqual(expected, details["outcome"])
                self.assertEqual(raw, details["variable_accesses"][0]["value"])

    def test_logs_do_not_read_variables_again(self):
        self.context.set("Raw", "00:00:02")
        self.runner.run(self.function([FunctionBlock(
            block_id="parse", block_type="duration_parse",
            params={"input": "$Raw"}, output_as="Seconds",
        )]), self.context, self.profile)
        accesses = self.context.variable_accesses
        self.assertEqual(1, sum(
            a["operation"] == "read" and a["name"] == "Raw" for a in accesses
        ))

    def test_literal_input_and_long_values_are_logged_but_bounded(self):
        function = self.function([FunctionBlock(
            block_id="parse", block_type="duration_parse",
            params={"input": "invalid" * 100},
        )])
        self.runner.run(function, self.context, self.profile)
        details = self.records("Function Block Executed")[0]["details"]
        self.assertTrue(details["input"].endswith("..."))
        self.assertLessEqual(len(details["input"]), 123)
        self.assertEqual("failed", details["outcome"])

    def test_previous_accesses_do_not_leak_into_current_block(self):
        self.context.set("Previous", "unrelated")
        self.context.set("Raw", "00:00:02")
        self.runner.run(self.function([FunctionBlock(
            block_id="parse", block_type="duration_parse",
            params={"input": "$Raw"}, output_as="Seconds",
        )]), self.context, self.profile)
        accesses = self.records("Function Block Executed")[0]["details"]["variable_accesses"]
        self.assertEqual(["Raw", "Seconds"], [a["name"] for a in accesses])

    def test_nested_helper_logs_are_correlated_without_losing_parent_accesses(self):
        helper = self.function([self.ocr_block()])
        helper.function_id = "helper"
        helper.name = "Read gathering time"
        helper.kind = "helper"
        service = Mock()
        service.load.return_value = helper
        self.runner.function_service = service
        caller = self.function([FunctionBlock(
            block_id="call", block_type="call_function",
            params={"function_id": "helper"}, output_as="local:ReturnedTime",
        )])
        self.runner.run(caller, self.context, self.profile)
        records = self.records("Function Block Executed")
        self.assertEqual(["helper", "gather"], [r["function_id"] for r in records])
        self.assertEqual(self.context.execution_id, records[0]["parent_id"])
        self.assertEqual("01:02:03", records[1]["details"]["value"])
        self.assertEqual("ReturnedTime", records[1]["details"]["variable_accesses"][0]["name"])

    def test_actual_ocr_zero_has_no_fallback_warning(self):
        self.ocr.read.return_value = "0"
        self.runner.run(self.function([self.ocr_block(default_value="0")]), self.context, self.profile)
        self.assertEqual([], self.records("Function OCR Default Used"))
        self.assertEqual("0", self.records("Function Block Executed")[0]["details"]["value"])

    def test_ocr_error_without_default_keeps_null_and_error_visible(self):
        self.ocr.read.side_effect = RuntimeError("no readable time")
        result = self.runner.run(
            self.function([self.ocr_block()]), self.context, self.profile
        )
        self.assertEqual("failed", result["outcome"])
        details = self.records("Function Block Executed")[0]["details"]
        self.assertIsNone(details["value"])
        self.assertEqual("no readable time", details["error"])
        self.assertEqual([], self.records("Function OCR Default Used"))

    def test_access_limit_keeps_final_assignment_and_reports_omission(self):
        def operation(block, context, profile):
            for _ in range(40):
                context.get("Raw")
            return "ok", 7, None

        self.runner.run_block = operation
        self.runner.run(self.function([FunctionBlock(
            block_id="many", block_type="variable", output_as="Target",
        )]), self.context, self.profile)
        details = self.records("Function Block Executed")[0]["details"]
        self.assertEqual(32, len(details["variable_accesses"]))
        self.assertEqual(9, details["variable_accesses_omitted"])
        self.assertEqual("Target", details["variable_accesses"][-1]["name"])
        self.assertEqual(7, details["variable_accesses"][-1]["value"])


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=2).result
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE", flush=True)
    raise SystemExit(0 if result.wasSuccessful() else 1)
