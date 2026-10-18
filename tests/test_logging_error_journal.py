import faulthandler
import sys
import time
import unittest
from unittest.mock import Mock, patch

from services.logging_service import BotStudioLogger, RecentErrorJournal


class LoggingErrorJournalTests(unittest.TestCase):
    def test_error_records_are_bounded_and_secrets_are_redacted(self):
        journal = RecentErrorJournal()
        logger = object.__new__(BotStudioLogger)
        logger.module = "runtime"
        logger.logger = Mock()

        with patch(
            "services.logging_service._RECENT_ERROR_JOURNAL",
            journal,
        ):
            logger.error("Runtime failed token=private-token")
            first_snapshot = journal.snapshot()
            for index in range(journal.MAX_EVENTS):
                logger.error(f"Runtime failure {index}")

        snapshot = journal.snapshot()

        self.assertEqual(
            "Runtime failed token=[redacted]",
            first_snapshot["events"][0]["event"],
        )
        self.assertEqual(journal.MAX_EVENTS, len(snapshot["events"]))
        self.assertNotIn(
            "private-token",
            repr(snapshot),
        )
        self.assertEqual("runtime", snapshot["events"][0]["module"])
        self.assertEqual(2, snapshot["events"][0]["sequence"])


if __name__ == "__main__":
    started_at = time.monotonic()
    print("TEST START: logging error journal", flush=True)
    faulthandler.dump_traceback_later(20, exit=True)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(
        LoggingErrorJournalTests
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
