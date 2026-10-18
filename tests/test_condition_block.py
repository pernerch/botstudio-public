"""Regression tests for the Condition block and rejected output_as writes.

Pure in-memory checks: no emulator, no file I/O, no waiting, so the suite can
only end in SUCCESS or FAILURE.
"""

import unittest
from unittest.mock import Mock

from models.bot_function import BotFunction
from models.execution_context import ExecutionContext
from models.function_block import FunctionBlock
from services.function_block_catalog import (
    BLOCK_CONDITION,
    OUTCOME_FAILED,
    OUTCOME_FALSE,
    OUTCOME_TRUE,
)
from services.function_runner import FunctionRunner


class ConditionBlockTests(unittest.TestCase):

    def setUp(self):
        self.runner = FunctionRunner(logger=Mock())
        # no variable_service: values stay in memory, the test writes no files
        self.context = ExecutionContext(
            profile_id="p1",
            function_id="f1",
        )

    def run_condition(self, expression, output_as=""):
        block = FunctionBlock(
            block_id="b1",
            block_type=BLOCK_CONDITION,
            params={"expression": expression},
            output_as=output_as,
        )
        return self.runner.run_block(block, self.context, {"name": "P"})

    def test_boolean_true_variable_takes_true_branch(self):
        self.context.set("Unlock VIP", True)
        outcome, value, error = self.run_condition("$Unlock VIP")
        self.assertIsNone(error)
        self.assertEqual(OUTCOME_TRUE, outcome)
        self.assertTrue(value["value"])

    def test_boolean_false_variable_takes_false_branch(self):
        """A Yes/No field set to No must branch, not fail (was AttributeError)."""

        self.context.set("Unlock VIP", False)
        outcome, value, error = self.run_condition("$Unlock VIP")
        self.assertIsNone(error)
        self.assertEqual(OUTCOME_FALSE, outcome)
        self.assertFalse(value["value"])

    def test_missing_variable_takes_false_branch(self):
        outcome, _value, error = self.run_condition("$Unknown")
        self.assertIsNone(error)
        self.assertEqual(OUTCOME_FALSE, outcome)

    def test_non_boolean_value_uses_truthiness(self):
        self.context.set("Count", 3)
        outcome, _value, error = self.run_condition("$Count")
        self.assertIsNone(error)
        self.assertEqual(OUTCOME_TRUE, outcome)

    def test_empty_expression_takes_false_branch(self):
        outcome, _value, error = self.run_condition("")
        self.assertIsNone(error)
        self.assertEqual(OUTCOME_FALSE, outcome)


class RejectedOutputWriteTests(unittest.TestCase):

    def setUp(self):
        self.runner = FunctionRunner(logger=Mock())
        self.context = ExecutionContext(
            profile_id="p1",
            function_id="f1",
        )

    def test_rejected_output_write_fails_run_instead_of_raising(self):
        """A write the variable layer rejects must not crash the run."""

        self.context.readonly_variables["Unlock VIP"] = False
        self.context.set("Flag", True)
        function = BotFunction(
            function_id="f1",
            name="ClaimVIP",
            game_id="g1",
            blocks=[
                FunctionBlock(
                    block_id="b1",
                    block_type=BLOCK_CONDITION,
                    params={"expression": "$Flag"},
                    output_as="Unlock VIP",
                )
            ],
        )
        result = self.runner.run(function, self.context, {"name": "P"})
        self.assertEqual(OUTCOME_FAILED, result["outcome"])
        self.assertIn("could not be written", str(result.get("error")))


if __name__ == "__main__":
    try:
        result = unittest.main(exit=False).result
    except Exception as error:  # pragma: no cover - explicit result contract
        print(f"TEST RESULT: FAILURE ({error})")
        raise SystemExit(1)
    if result.wasSuccessful():
        print("TEST RESULT: SUCCESS")
        raise SystemExit(0)
    print("TEST RESULT: FAILURE")
    raise SystemExit(1)
