import faulthandler
import logging
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from services.telegram_service import (
    TelegramApiError, TelegramDocument, TelegramDocuments, TelegramFileList, TelegramService,
)


class TelegramServiceTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.responses = []
        self.clock_value = 100.0
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)

        def api_call(method, parameters):
            self.calls.append((method, parameters))
            if method == "getUpdates":
                return []
            if method == "sendMessage":
                self.responses.append(parameters)
                return {"message_id": len(self.responses)}
            return None

        self.api_call = api_call
        self.commands = []
        self.service = TelegramService(
            token="token-not-for-logs",
            allowed_user_ids={123},
            allowed_chat_ids={-456},
            command_handler=lambda command, arguments: self.commands.append(
                (command, arguments)
            ),
            logger=logging.getLogger("test.telegram"),
            api_call=api_call,
            clock=self.clock,
            unix_time=lambda: 1_800_000_000,
            state_file=Path(self.temp_directory.name) / "telegram-offset.json",
        )

    def clock(self):
        self.clock_value += 1
        return self.clock_value

    def make_update(self, update_id=10, command="/status"):
        return {
            "update_id": update_id,
            "message": {
                "date": 1_800_000_000,
                "text": command,
                "chat": {"id": -456},
                "from": {"id": 123},
            },
        }

    def test_environment_configuration_is_opt_in_and_requires_allowlists(self):
        self.assertIsNone(
            TelegramService.from_environment(
                lambda command, arguments: command,
                environ={},
            )
        )

        with self.assertRaisesRegex(ValueError, "both allowed-ID lists"):
            TelegramService.from_environment(
                lambda command, arguments: command,
                environ={"BOTSTUDIO_TELEGRAM_TOKEN": "secret"},
            )

        service = TelegramService.from_environment(
            lambda command, arguments: command,
            environ={
                "BOTSTUDIO_TELEGRAM_TOKEN": "secret",
                "BOTSTUDIO_TELEGRAM_ALLOWED_USER_IDS": "123, 124",
                "BOTSTUDIO_TELEGRAM_ALLOWED_CHAT_IDS": "-456",
            },
            api_call=self.api_call,
            state_file=Path(self.temp_directory.name) / "env-offset.json",
        )
        self.assertEqual(frozenset({123, 124}), service.allowed_user_ids)
        self.assertEqual(frozenset({-456}), service.allowed_chat_ids)
        self.assertNotIn("secret", repr(service.state))

    def test_requires_both_user_and_chat_allowlist_ids(self):
        self.service._handle_update(self.make_update())
        self.assertEqual([("status", [])], self.commands)
        self.assertEqual(2, len(self.responses))

        unauthorized = self.make_update(update_id=11)
        unauthorized["message"]["from"]["id"] = 999
        self.service._handle_update(unauthorized)
        self.assertEqual([("status", [])], self.commands)
        self.assertEqual(2, len(self.responses))

        unauthorized_chat = self.make_update(update_id=12)
        unauthorized_chat["message"]["chat"]["id"] = 888
        self.service._handle_update(unauthorized_chat)
        self.assertEqual([("status", [])], self.commands)

    def test_commands_are_normalized_and_arguments_are_parsed(self):
        self.service._handle_update(
            self.make_update(command="/STATUS@my_bot ignored")
        )
        self.assertEqual([("status", ["ignored"])], self.commands)

        self.service._handle_update(
            self.make_update(update_id=11, command="/event_run event-12345678")
        )
        self.assertEqual(
            [
                ("status", ["ignored"]),
                ("event_run", ["event-12345678"]),
            ],
            self.commands,
        )

    def test_selected_documents_are_sent_only_to_requesting_authorized_chat(self):
        documents = TelegramDocuments((
            TelegramDocument("runtime.log", b"Runtime started\n"),
            TelegramDocument("watchdog.log", b"Watchdog running\n"),
        ))
        self.service.command_handler = lambda command, arguments: documents
        self.service.allowed_chat_ids = frozenset({-456, -457})
        self.service._handle_update(self.make_update(command="/log a b"))
        sends = [parameters for method, parameters in self.calls if method == "sendDocument"]
        self.assertEqual(2, len(sends))
        self.assertEqual([-456, -456], [send["chat_id"] for send in sends])
        self.assertEqual(1, len(self.responses))
        self.assertIn("Request received", self.responses[0]["text"])
        self.assertEqual(-456, self.responses[0]["chat_id"])
        unauthorized = self.make_update(command="/log a")
        unauthorized["message"]["from"]["id"] = 999
        self.service._handle_update(unauthorized)
        self.assertEqual(2, len([call for call in self.calls if call[0] == "sendDocument"]))

    def test_document_delivery_retries_and_stop_cancels(self):
        attempts = []
        def api_call(method, parameters):
            attempts.append(method)
            if len(attempts) == 1:
                raise TelegramApiError("temporary", retry_after=1)
            return {}
        self.service._api_call_override = api_call
        with patch.object(self.service._stop_event, "wait", return_value=False):
            self.assertTrue(self.service._send_document(-456, TelegramDocument("runtime.log", b"ok")))
        self.assertEqual(["sendDocument", "sendDocument"], attempts)
        self.service._stop_event.set()
        self.assertFalse(self.service._send_document(-456, TelegramDocument("runtime.log", b"ok")))
        self.assertEqual(2, len(attempts))

    def test_document_failure_notifies_user_and_does_not_send_remaining_files(self):
        self.service.MAX_SEND_RETRIES = 0
        self.service.command_handler = lambda command, arguments: TelegramDocuments((
            TelegramDocument("first.log", b"first"),
            TelegramDocument("second.log", b"second"),
        ))
        def api_call(method, parameters):
            self.calls.append((method, parameters))
            if method == "sendDocument":
                raise TelegramApiError("unavailable")
            self.responses.append(parameters)
            return {}
        self.service._api_call_override = api_call
        self.service._handle_update(self.make_update(command="/log a b"))
        self.assertEqual(1, len([call for call in self.calls if call[0] == "sendDocument"]))
        self.assertIn("delivery failed", self.responses[-1]["text"])

    def test_document_bounds_and_unsafe_filenames_are_rejected(self):
        for document in (
            TelegramDocument("../secret.log", b"private"),
            TelegramDocument('bad"\r\n.log', b"private"),
            TelegramDocument("config.json", b"private"),
            TelegramDocument("runtime.log", b""),
            TelegramDocument("runtime.log", b"x" * (self.service.MAX_DOCUMENT_BYTES + 1)),
        ):
            self.assertFalse(self.service._send_document(-456, document))
        self.assertEqual([], self.calls)
        self.service.command_handler = lambda command, arguments: TelegramDocuments(())
        self.service._handle_update(self.make_update(command="/log a"))
        self.assertIn("Invalid log selection", self.responses[-1]["text"])

    def test_document_transport_uses_multipart_and_bounded_http_timeout(self):
        self.service._api_call_override = None
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok":true,"result":{"message_id":1}}'
        with patch("services.telegram_service.urlopen", return_value=response) as opener:
            self.assertTrue(self.service._send_document(-456, TelegramDocument("runtime.log", b"safe log\n")))
        request = opener.call_args.args[0]
        self.assertIn("multipart/form-data; boundary=", request.get_header("Content-type"))
        self.assertIn(b'name="document"; filename="runtime.log"', request.data)
        self.assertIn(b"safe log\n", request.data)
        self.assertIn(b"-456", request.data)
        self.assertEqual(self.service.API_TIMEOUT_SECONDS, opener.call_args.kwargs["timeout"])

    def make_callback(self, data="log abcdef1234567890"):
        if data.split()[0] in {"log", "logs"}:
            data = self.service._register_button(-456, 123, data, False)
        return {
            "callback_query": {
                "id": "callback-1",
                "from": {"id": 123},
                "message": {
                    "chat": {"id": -456},
                    "date": 1_800_000_000,
                },
                "data": data,
            }
        }

    def test_log_list_is_sent_with_mobile_download_buttons(self):
        self.service.command_handler = lambda command, arguments: TelegramFileList(
            "Log files", (("Download runtime.log", "log abcdef1234567890"),)
        )
        self.service._handle_update(self.make_update(command="/logs"))
        keyboard = self.responses[-1]["reply_markup"]["inline_keyboard"]
        data = keyboard[0][0]["callback_data"]
        self.assertLessEqual(len(data.encode("utf-8")), 64)
        self.assertEqual("log", self.service._callbacks[data]["command"])
        self.assertEqual(["abcdef1234567890"], self.service._callbacks[data]["arguments"])

    def test_file_callbacks_are_authorized_and_restricted_to_file_commands(self):
        self.service._handle_update(self.make_callback())
        self.assertEqual([("log", ["abcdef1234567890"])], self.commands)
        self.assertEqual("answerCallbackQuery", self.calls[0][0])
        unauthorized = self.make_callback()
        unauthorized["callback_query"]["from"]["id"] = 999
        self.service._handle_update(unauthorized)
        unauthorized_chat = self.make_callback()
        unauthorized_chat["callback_query"]["message"]["chat"]["id"] = 888
        self.service._handle_update(unauthorized_chat)
        self.service._handle_update(self.make_callback("runtime_start"))
        self.assertEqual(1, len(self.commands))
        self.service._handle_update(self.make_callback("logs 2"))
        self.assertEqual(("logs", ["2"]), self.commands[-1])

    def test_download_button_sends_document_without_touching_other_chats(self):
        self.service.command_handler = lambda command, arguments: TelegramDocuments(
            (TelegramDocument("runtime.log", b"safe content\n"),)
        )
        self.service._handle_update(self.make_callback())
        self.assertEqual(
            ["answerCallbackQuery", "sendMessage", "sendDocument"],
            [method for method, _ in self.calls],
        )
        self.assertEqual(-456, self.calls[-1][1]["chat_id"])
        self.assertEqual(b"safe content\n", self.calls[-1][1]["document"].content)

    def test_expired_file_callbacks_are_not_executed(self):
        update = self.make_callback()
        self.clock_value += self.service.MAX_MESSAGE_AGE_SECONDS + 1
        self.service._handle_update(update)
        self.assertEqual([], self.commands)
        self.assertIn("expired", self.calls[-1][1]["text"])
        self.service._stop_event.set()
        self.service._handle_update(self.make_callback())
        self.assertEqual(1, len(self.calls))

    def test_file_callbacks_share_command_rate_limit(self):
        for _ in range(self.service.MAX_COMMANDS_PER_MINUTE + 1):
            self.service._handle_update(self.make_callback())
        self.assertEqual(self.service.MAX_COMMANDS_PER_MINUTE, len(self.commands))
        self.assertIn("Too many", self.responses[-1]["text"])

    def test_file_buttons_are_serialized_as_json_in_http_transport(self):
        from urllib.parse import parse_qs
        import json
        self.service._api_call_override = None
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok":true,"result":{}}'
        markup = {"inline_keyboard": [[{"text": "Download", "callback_data": "log abc"}]]}
        with patch("services.telegram_service.urlopen", return_value=response) as opener:
            self.service._api_call("sendMessage", {"chat_id": -456, "text": "Logs", "reply_markup": markup})
        fields = parse_qs(opener.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(markup, json.loads(fields["reply_markup"][0]))

    def test_command_arguments_are_bounded(self):
        self.service._handle_update(
            self.make_update(command="/event_on " + ("a" * 129))
        )
        self.assertEqual([("event_on", ["", "", ""])], self.commands)

    def test_stale_messages_and_non_commands_are_ignored(self):
        self.service._handle_update(
            self.make_update(command="plain message")
        )
        stale = self.make_update(update_id=11)
        stale["message"]["date"] -= TelegramService.MAX_MESSAGE_AGE_SECONDS + 1
        self.service._handle_update(stale)

        self.assertEqual([], self.commands)
        self.assertEqual([], self.responses)

    def test_duplicate_update_id_is_ignored(self):
        updates = [self.make_update(), self.make_update()]
        original_call = self.service._api_call_override
        self.service._api_call_override = (
            lambda method, params: updates
            if method == "getUpdates"
            else original_call(method, params)
        )

        self.service.poll_once()

        self.assertEqual([("status", [])], self.commands)
        self.assertEqual(11, self.service._offset)

    def test_update_offset_is_persisted_across_service_instances(self):
        self.service._api_call_override = (
            lambda method, params: [self.make_update(update_id=41)]
            if method == "getUpdates"
            else None
        )
        self.service.poll_once()

        restarted = TelegramService(
            token="token-not-for-logs",
            allowed_user_ids={123},
            allowed_chat_ids={-456},
            command_handler=lambda command, arguments: command,
            state_file=self.service.state_file,
        )

        self.assertEqual(42, restarted._offset)

    def test_connection_test_checks_bot_identity_without_starting_polling(self):
        self.service._api_call_override = lambda method, params: (
            {"id": 999, "username": "botstudio_test"}
            if method == "getMe"
            else None
        )

        self.assertEqual("botstudio_test", self.service.test_connection())
        self.assertEqual("stopped", self.service.state["state"])

    def test_notifications_are_queued_and_delivered_to_allowed_chats(self):
        self.service.allowed_chat_ids = frozenset({-456, -457})

        self.assertTrue(
            self.service.send_notification("BotStudio reported an error.")
        )
        self.assertEqual([], self.calls)

        self.service._deliver_queued_notification()

        self.assertEqual(
            [-457, -456],
            [
                call[1]["chat_id"]
                for call in self.calls
                if call[0] == "sendMessage"
            ],
        )
        self.assertTrue(
            all(
                call[1]["text"] == "BotStudio reported an error."
                for call in self.calls
                if call[0] == "sendMessage"
            )
        )

    def test_notification_remains_queued_when_delivery_fails(self):
        self.service.MAX_SEND_RETRIES = 0
        self.service._api_call_override = (
            lambda method, params: (_ for _ in ()).throw(
                TelegramApiError("temporary failure")
            )
            if method == "sendMessage"
            else []
        )

        self.assertTrue(self.service.send_notification("Recovery alert."))
        self.service._deliver_queued_notification()
        self.assertEqual(1, self.service._notification_queue.qsize())

        self.service._api_call_override = self.api_call
        self.service._deliver_queued_notification()

        self.assertEqual(0, self.service._notification_queue.qsize())
        self.assertEqual("Recovery alert.", self.responses[-1]["text"])

    def test_unknown_command_returns_help_hint(self):
        self.service.command_handler = (
            lambda command, arguments: (
                "Unknown command. Send /help for supported commands."
            )
        )
        self.service._handle_update(self.make_update(command="/not_supported"))

        self.assertEqual(
            "Unknown command. Send /help for supported commands.",
            self.responses[-1]["text"],
        )

    def test_command_rate_limit_rejects_excess_commands(self):
        for update_id in range(TelegramService.MAX_COMMANDS_PER_MINUTE):
            self.service._handle_update(self.make_update(update_id=update_id))

        self.service._handle_update(
            self.make_update(update_id=99, command="/runtime_start")
        )

        self.assertEqual(
            TelegramService.MAX_COMMANDS_PER_MINUTE,
            len(self.commands),
        )
        self.assertEqual(
            "Too many commands. Please try again shortly.",
            self.responses[-1]["text"],
        )

    def test_api_transport_never_exposes_token_in_errors(self):
        from unittest.mock import patch
        from urllib.error import URLError

        def fail_request(_request, timeout):
            raise URLError("network unavailable")

        transport = TelegramService(
            token="token-not-for-logs",
            allowed_user_ids={123},
            allowed_chat_ids={-456},
            command_handler=lambda command, arguments: command,
            state_file=Path(self.temp_directory.name) / "transport-offset.json",
        )
        with patch(
            "services.telegram_service.urlopen",
            side_effect=fail_request,
        ):
            with self.assertRaisesRegex(
                Exception,
                "network request failed",
            ) as caught:
                transport._api_call("getUpdates", {})

        self.assertNotIn("token-not-for-logs", str(caught.exception))

    def test_stop_event_interrupts_poll_backoff_without_unbounded_wait(self):
        service = TelegramService(
            token="token",
            allowed_user_ids={123},
            allowed_chat_ids={-456},
            command_handler=lambda command, arguments: "ok",
            api_call=lambda method, params: (_ for _ in ()).throw(
                RuntimeError("temporary")
            ),
            state_file=Path(self.temp_directory.name) / "stop-offset.json",
        )
        service.start()
        time.sleep(0.02)
        started = time.monotonic()
        service.stop()

        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual("stopped", service.state["state"])


if __name__ == "__main__":
    started_at = time.monotonic()
    print("TEST START: telegram service", flush=True)
    faulthandler.dump_traceback_later(20, exit=True)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TelegramServiceTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    faulthandler.cancel_dump_traceback_later()
    print(f"TEST DURATION: {time.monotonic() - started_at:.3f}s", flush=True)
    print(
        "TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE",
        flush=True,
    )
    sys.exit(0 if result.wasSuccessful() else 1)
