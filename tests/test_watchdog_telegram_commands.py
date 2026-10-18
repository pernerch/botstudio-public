import faulthandler
import sys
import time
import unittest
from unittest.mock import patch

from watchdog_app import _handle_telegram_command


class FakeWatchdogControl:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.status = {
            "state": "running",
            "health": "healthy",
            "studio_pid": 123,
            "restarts_last_window": 1,
            "runtime_recovery": "restored",
        }

    def runtime_command(self, command, arguments=None):
        self.calls.append((command, arguments))
        return self.result

    def get_status(self):
        return self.status


class WatchdogTelegramCommandTests(unittest.TestCase):
    def test_emulator_restart_returns_operation_without_waiting(self):
        control = FakeWatchdogControl({
            "accepted": True, "state": "running", "operation_id": "op-1"
        })
        response = _handle_telegram_command("emulator_restart", ["0"], control)
        self.assertIn("/operation op-1", response)
        self.assertEqual([("emulator.restart", {"instance": "0"})], control.calls)

    def test_emulator_operation_reports_failure_and_completion(self):
        control = FakeWatchdogControl({"state": "failed", "error": "Runtime busy"})
        self.assertEqual(
            "Runtime busy", _handle_telegram_command("operation", ["op-1"], control)
        )
        control.result = {
            "state": "completed", "result": {"instance": 0, "runtime_resumed": True}
        }
        self.assertIn(
            "Runtime resumed: yes",
            _handle_telegram_command("operation", ["op-1"], control),
        )

    def test_status_includes_runtime_recovery_state(self):
        control = FakeWatchdogControl({})
        ipc_client = type(
            "FakeIpcClient",
            (),
            {
                "request": lambda _self, command, timeout_ms: {
                    "runtime": {"status": "Running"}
                }
            },
        )()

        with patch(
            "watchdog_app.WatchdogIpcClient",
            return_value=ipc_client,
        ):
            response = _handle_telegram_command("status", [], control)

        self.assertIn("Runtime: Running", response)
        self.assertIn("Recovery: restored", response)
        self.assertIn("Restarts last 24h: 1", response)

    def test_events_command_formats_snapshot(self):
        control = FakeWatchdogControl(
            {
                "events": [
                    {
                        "event_id": "event-12345678",
                        "event_name": "Collect",
                        "profile_id": "profile-123456",
                        "profile_name": "Main",
                        "enabled": True,
                    }
                ],
                "truncated": False,
            }
        )

        response = _handle_telegram_command("events", [], control)

        self.assertEqual(
            "Main [profile-1234] / Collect | ON | id=event-12345678",
            response,
        )
        self.assertEqual([("events.list", None)], control.calls)

    def test_tasks_command_reports_operation_failure_not_busy(self):
        control = FakeWatchdogControl(
            {"accepted": False, "state": "failed", "error": "Storage unavailable"}
        )

        response = _handle_telegram_command("tasks", [], control)

        self.assertEqual("Storage unavailable", response)
        self.assertNotIn("busy", response.lower())

    def test_event_run_forwards_event_and_profile_references(self):
        control = FakeWatchdogControl(
            {"event_name": "Collect", "state": "queued"}
        )

        response = _handle_telegram_command(
            "event_run",
            ["event-123", "profile-456"],
            control,
        )

        self.assertEqual(
            "Event 'Collect' queued for the next Runtime scheduler cycle.",
            response,
        )
        self.assertEqual(
            (
                "event.run_now",
                {"event_id": "event-123", "profile_id": "profile-456"},
            ),
            control.calls[0],
        )

    def test_busy_event_list_and_invalid_argument_count_are_explicit(self):
        control = FakeWatchdogControl({"accepted": False, "state": "busy"})

        self.assertEqual(
            "Studio is busy. Try again shortly.",
            _handle_telegram_command("events", [], control),
        )
        self.assertEqual(
            "Usage: /event_on <event-id> [profile-id]. Use /events to find event IDs.",
            _handle_telegram_command("event_on", [], control),
        )
        self.assertEqual([("events.list", None)], control.calls)


if __name__ == "__main__":
    started_at = time.monotonic()
    print("TEST START: watchdog Telegram commands", flush=True)
    faulthandler.dump_traceback_later(20, exit=True)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(
        WatchdogTelegramCommandTests
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
