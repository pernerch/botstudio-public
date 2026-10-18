import unittest
from unittest.mock import MagicMock, Mock, patch

import numpy as np

from services.function_block_catalog import BLOCK_CATALOG, BLOCK_SWIPE, DEFAULT_SWIPE_SETTLE_MS
from services.function_runner import OUTCOME_FAILED, OUTCOME_OK, FunctionRunner


class SwipeSettleTests(unittest.TestCase):

    def setUp(self):
        adb = Mock()
        adb.ensure_connection.return_value = True
        screen = Mock()
        screen.capture.return_value = "screen.png"
        self.runner = FunctionRunner(adb_service=adb, screen_service=screen, logger=Mock())
        self.profile = {"name": "P", "device_id": "dev"}
        self.context = Mock()
        self.context.is_cancelled.return_value = False

    def run_swipe(self, params):
        sleeps = []

        def fake_sleep(_context, seconds):
            sleeps.append(seconds)
            return not self.context.is_cancelled()

        emulator = MagicMock()
        emulator.return_value.swipe.return_value = True
        with patch("services.function_runner.cv2.imread", return_value=np.zeros((1280, 720, 3))), \
                patch("services.function_runner.Emulator", emulator), \
                patch.object(FunctionRunner, "_cancellable_sleep", staticmethod(fake_sleep)):
            outcome, _value, error = self.runner._swipe(params, self.profile, self.context)
        return outcome, error, sleeps, emulator.return_value.swipe.call_count

    def test_catalog_exposes_settle_time(self):
        self.assertEqual(DEFAULT_SWIPE_SETTLE_MS, BLOCK_CATALOG[BLOCK_SWIPE]["params"]["settle_ms"])

    def test_existing_block_without_param_waits_default_after_last_swipe(self):
        outcome, _error, sleeps, swipes = self.run_swipe(
            {"direction": "right", "max_swipes": 2, "duration_ms": 200, "between_swipes_ms": 200}
        )
        self.assertEqual(OUTCOME_OK, outcome)
        self.assertEqual(2, swipes)
        self.assertEqual([0.2, DEFAULT_SWIPE_SETTLE_MS / 1000], sleeps)

    def test_settle_zero_disables_pause(self):
        outcome, _error, sleeps, _swipes = self.run_swipe({"direction": "up", "settle_ms": 0})
        self.assertEqual(OUTCOME_OK, outcome)
        self.assertEqual([], sleeps)

    def test_cancel_during_settle_fails(self):
        self.context.is_cancelled.return_value = True
        outcome, error, _sleeps, _swipes = self.run_swipe({"direction": "up", "settle_ms": 500})
        self.assertEqual(OUTCOME_FAILED, outcome)
        self.assertEqual("cancelled", error)


if __name__ == "__main__":
    unittest.main()
