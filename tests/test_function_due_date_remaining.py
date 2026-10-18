"""Tests for calculating the remaining time until a due date."""

import faulthandler
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from threading import Timer
import time
import unittest
from unittest.mock import Mock

from models.bot_function import BotFunction
from models.execution_context import ExecutionContext
from models.function_block import FunctionBlock
from services.function_block_catalog import (
    BLOCK_DUE_DATE_REMAINING,
    OUTCOME_FAILED,
    OUTCOME_OK,
)
from services.function_runner import FunctionRunner
from services.variable_service import VariableService


class DueDateRemainingTests(unittest.TestCase):
    def setUp(self):
        self.runner = FunctionRunner(logger=Mock())
        self.context = ExecutionContext(profile_id="p1", function_id="f1")

    def run_block(self, value):
        block = FunctionBlock(
            block_id="due-date",
            block_type=BLOCK_DUE_DATE_REMAINING,
            params={"input": "$Pet Skill"},
            output_as="remaining_seconds",
        )
        self.context.set("Pet Skill", value, local=True)
        function = BotFunction(
            function_id="f1",
            name="Pet Skill",
            game_id="g1",
            blocks=[block],
        )
        return self.runner.run(function, self.context, {"name": "P"})

    def test_future_due_date_returns_seconds_as_output_variable(self):
        due_date = datetime.now(timezone.utc) + timedelta(seconds=60)
        result = self.run_block(due_date.isoformat())

        self.assertEqual("success", result["outcome"])
        self.assertEqual(OUTCOME_OK, result["trace"][0]["outcome"])
        self.assertGreater(self.context.get("remaining_seconds"), 50)
        self.assertLessEqual(self.context.get("remaining_seconds"), 60)

    def test_empty_and_overdue_values_return_zero(self):
        for value in (None, "", "  "):
            with self.subTest(value=value):
                result = self.run_block(value)
                self.assertEqual("success", result["outcome"])
                self.assertEqual(OUTCOME_OK, result["trace"][0]["outcome"])
                self.assertEqual(0, self.context.get("remaining_seconds"))

        overdue = datetime.now(timezone.utc) - timedelta(seconds=1)
        result = self.run_block(overdue.isoformat())
        self.assertEqual("success", result["outcome"])
        self.assertEqual(OUTCOME_OK, result["trace"][0]["outcome"])
        self.assertEqual(0, self.context.get("remaining_seconds"))

    def test_invalid_date_fails_instead_of_becoming_zero(self):
        result = self.run_block("not-a-date")
        self.assertEqual(OUTCOME_FAILED, result["outcome"])
        self.assertIn("valid ISO date/time", result["error"])

    def test_uninitialized_variable_returns_zero(self):
        block = FunctionBlock(
            block_id="due-date",
            block_type=BLOCK_DUE_DATE_REMAINING,
            params={"input": "$Missing"},
            output_as="remaining_seconds",
        )
        function = BotFunction(
            function_id="f1",
            name="Pet Skill",
            game_id="g1",
            blocks=[block],
        )
        result = self.runner.run(function, self.context, {"name": "P"})
        self.assertEqual("success", result["outcome"])
        self.assertEqual(OUTCOME_OK, result["trace"][0]["outcome"])
        self.assertEqual(0, self.context.get("remaining_seconds"))

    def test_live_profile_variable_is_writable_without_scheduler_context(self):
        with tempfile.TemporaryDirectory() as root:
            variable_service = VariableService(
                root=root,
                reinitialize_service=Mock(),
            )
            variable_service.create_variable(
                "g1",
                "S_PetSkill",
                initial_value=0,
                kind="live",
                value_type="duration",
                writable_by_functions=True,
            )
            context = ExecutionContext(
                game_id="g1",
                profile_id="p1",
                function_id="f1",
                variable_service=variable_service,
            )

            context.set("S_PetSkill", 300)

            self.assertEqual(300, context.get("S_PetSkill"))
            self.assertEqual(
                300,
                variable_service.get_profile_value(
                    "g1",
                    "p1",
                    "S_PetSkill",
                ),
            )


if __name__ == "__main__":
    def timeout():
        print("TEST RESULT: TIMEOUT", flush=True)
        faulthandler.dump_traceback()
        os._exit(2)

    started = time.monotonic()
    print("TEST START: due date remaining", flush=True)
    watchdog = Timer(30, timeout)
    watchdog.daemon = True
    watchdog.start()
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(DueDateRemainingTests)
    )
    watchdog.cancel()
    print(f"TEST DURATION: {time.monotonic() - started:.3f}s", flush=True)
    print(
        "TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE",
        flush=True,
    )
    sys.exit(0 if result.wasSuccessful() else 1)
