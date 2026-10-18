import faulthandler
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from services.log_redaction_service import redact_log_line
from services.telegram_log_service import TelegramLogService
from services.telegram_service import TelegramDocuments
from watchdog_app import _handle_telegram_command


class TelegramLogServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.logs = self.root / "logs"
        self.logs.mkdir()
        (self.logs / "archive").mkdir()
        self.service = TelegramLogService(self.root)

    def write_log(self, name, text="Runtime started\n"):
        path = self.logs / name
        path.write_text(text, encoding="utf-8")
        return path

    def reference(self, path):
        return next(
            reference
            for reference, candidate, _, _ in self.service._entries()
            if candidate == path
        )

    def test_list_includes_current_and_rotated_logs_but_not_other_files(self):
        self.write_log("runtime.log")
        self.write_log("watchdog.log.1")
        self.write_log("archive/runtime_001.log")
        self.write_log("Support_Package.zip", "not a log")
        self.write_log("secret.json", "private")
        listing = self.service.list_files([]).text
        for name in ("runtime.log", "watchdog.log.1", "runtime_001.log"):
            self.assertIn(name, listing)
        self.assertIn("current", listing)
        self.assertIn("archive", listing)
        self.assertNotIn("Support_Package", listing)
        self.assertNotIn("secret.json", listing)
        self.assertNotIn(str(self.root), listing)

    def test_pagination_and_argument_validation(self):
        for index in range(12):
            self.write_log(f"module_{index}.log")
        self.assertIn("page 1/2", self.service.list_files([]).text)
        self.assertIn("/logs 2", self.service.list_files([]).text)
        self.assertIn("page 2/2", self.service.list_files(["2"]).text)
        self.assertIn(("Next page", "logs 2"), self.service.list_files([]).buttons)
        self.assertIn("between 1 and 2", self.service.list_files(["0"]))
        self.assertIn("between 1 and 2", self.service.list_files(["3"]))
        self.assertIn("Usage", self.service.list_files(["../secret"]))
        self.assertIn("Usage", self.service.list_files(["1", "2"]))

    def test_selection_sends_separate_files_and_deduplicates(self):
        first = self.write_log("runtime.log", "Runtime password=private\n")
        second = self.write_log("watchdog.log.1", "Watchdog running\n")
        response = self.service.download([self.reference(first), self.reference(second)])
        self.assertIsInstance(response, TelegramDocuments)
        self.assertEqual(["runtime.log", "watchdog.log.1.log"],
                         [document.filename for document in response.documents])
        self.assertNotIn(b"private", response.documents[0].content)
        self.assertEqual("Runtime password=private\n", first.read_text(encoding="utf-8"))
        duplicated = self.service.download([self.reference(first)] * 2)
        self.assertEqual(1, len(duplicated.documents))

    def test_missing_file_invalid_ids_and_path_traversal_are_rejected(self):
        path = self.write_log("runtime.log")
        reference = self.reference(path)
        path.unlink()
        self.assertIn("no longer available", self.service.download([reference]))
        for arguments in ([], ["../secret"], ["a" * 16] * 3, ["A" * 16]):
            self.assertIn("Usage", self.service.download(arguments))
        self.assertEqual("No log files available.", self.service.list_files([]))

    def test_large_files_are_not_truncated_or_sent(self):
        path = self.write_log("runtime.log", "x" * 100)
        self.service.MAX_FILE_BYTES = 20
        listing = self.service.list_files([])
        self.assertIn("too large", listing.text)
        self.assertEqual((), listing.buttons)
        self.assertIn("exceeds", self.service.download([self.reference(path)]))

    def test_redaction_expansion_is_size_bounded(self):
        path = self.write_log("runtime.log", "token=x")
        self.service.MAX_FILE_BYTES = 10
        self.assertIn("Redacted log exceeds", self.service.download([self.reference(path)]))

    def test_exact_production_size_limit_and_long_lines(self):
        limit = TelegramLogService.MAX_FILE_BYTES
        self.assertEqual(4 * 1024 * 1024, limit)
        path = self.write_log("runtime.log", "a" * (limit - 1))
        reference = self.reference(path)
        started = time.monotonic()
        response = self.service.download([reference])
        self.assertEqual(limit, len(response.documents[0].content))
        self.assertLess(time.monotonic() - started, 5)
        path.write_bytes(b"a" * limit)
        self.assertIn("Redacted log exceeds", self.service.download([reference]))
        path.write_bytes(b"a" * (limit + 1))
        self.assertIn("Selected log exceeds", self.service.download([reference]))

    def test_structured_secrets_and_variable_values_are_removed(self):
        line = json.dumps({
            "event": "Variable written",
            "message": "Variable written new_value=very-private",
            "details": {
                "variable_name": "LoginPassword",
                "new_value": "very-private",
                "nested": [{"api_key": "api-private", "result": "ok"}],
            },
            "exception": 'Failure authorization="Bearer bearer-private"',
        })
        result = redact_log_line(line)
        for secret in ("very-private", "api-private", "bearer-private"):
            self.assertNotIn(secret, result)
        parsed = json.loads(result)
        self.assertEqual("ok", parsed["details"]["nested"][0]["result"])
        self.assertEqual("[redacted]", parsed["details"]["new_value"])

    def test_plain_text_secret_patterns_and_partial_json(self):
        line = (
            'password="two word secret" token=123456789:abcdefghijklmnopqrstuv '
            "Authorization: Bearer abcdef123 "
            "https://user:private@example.test/"
        )
        result = redact_log_line(line)
        for secret in ("two word secret", "abcdefghijklmnopqrstuv", "abcdef123", "user:private"):
            self.assertNotIn(secret, result)
        self.assertIn("omitted", redact_log_line('{"message": "private'))

    def test_links_are_not_exported(self):
        path = self.write_log("runtime.log")
        with patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaisesRegex(ValueError, "links"):
                self.service.list_files([])
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda candidate: candidate == path or original(candidate)):
            self.assertEqual("No log files available.", self.service.list_files([]))

    def test_io_failure_is_not_reported_as_success(self):
        path = self.write_log("runtime.log")
        reference = self.reference(path)
        with patch.object(Path, "open", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                self.service.download([reference])

    def test_download_reads_fresh_content_after_listing(self):
        path = self.write_log("runtime.log", "Old state")
        reference = self.reference(path)
        path.write_text("Current state", encoding="utf-8")
        response = self.service.download([reference])
        self.assertEqual(b"Current state\n", response.documents[0].content)

    def test_replaced_open_file_is_not_exported(self):
        path = self.write_log("runtime.log")
        reference = self.reference(path)
        opened = type("Stat", (), {"st_dev": -1, "st_ino": -1})()
        with patch("services.telegram_log_service.os.fstat", return_value=opened):
            with self.assertRaisesRegex(ValueError, "changed during download"):
                self.service.download([reference])

    def test_command_routes_logs_without_studio_ipc(self):
        path = self.write_log("runtime.log")
        control = type("Control", (), {"working_directory": str(self.root)})()
        self.assertIn("runtime.log", _handle_telegram_command("logs", [], control).text)
        response = _handle_telegram_command("log", [self.reference(path)], control)
        self.assertIsInstance(response, TelegramDocuments)


if __name__ == "__main__":
    started_at = time.monotonic()
    print("TEST START: Telegram log files", flush=True)
    faulthandler.dump_traceback_later(20, exit=True)
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(TelegramLogServiceTests)
    )
    faulthandler.cancel_dump_traceback_later()
    print(f"TEST DURATION: {time.monotonic() - started_at:.3f}s", flush=True)
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE", flush=True)
    sys.exit(0 if result.wasSuccessful() else 1)
