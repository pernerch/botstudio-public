import faulthandler
import logging
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from services.telegram_control_menu import TelegramControlMenu
from services.telegram_service import TelegramMenu, TelegramService
from watchdog_app import _handle_telegram_command


class TelegramControlMenuTests(unittest.TestCase):
    def setUp(self):
        self.control = Mock()
        self.handler = Mock(return_value="Done")
        self.menu = TelegramControlMenu(self.control, self.handler)

    def event(self, index=0):
        return {
            "event_id": f"event-{index:032d}",
            "profile_id": f"profile-{index:032d}",
            "profile_name": f"Profile {index}",
            "event_name": f"Event {index}",
            "enabled": True,
            "status": "idle",
        }

    def test_start_and_menu_offer_all_function_categories(self):
        for command in ("start", "menu"):
            response = self.menu.handle(command, [])
            self.assertEqual(
                {"status", "studio", "runtime", "events", "tasks", "emulators", "logs", "help", "info", "statistics"},
                {command for _, command in response.buttons},
            )
        self.control.runtime_command.assert_not_called()

    def test_studio_and_runtime_actions_are_all_available(self):
        response = self.menu.handle("studio", [])
        self.assertTrue(
            {"studio_start", "studio_stop", "studio_restart"}
            <= {command for _, command in response.buttons}
        )
        response = self.menu.handle("runtime", [])
        self.assertTrue(
            {"runtime_start", "runtime_stop"} <= {command for _, command in response.buttons}
        )
        self.handler.assert_not_called()

    def test_event_pages_and_full_profile_scoped_selection(self):
        events = [self.event(index) for index in range(11)]
        self.control.runtime_command.return_value = {"events": events}
        response = self.menu.handle("events", [])
        self.assertIn(("Next page", "events 2"), response.buttons)
        self.assertEqual(8, len([command for _, command in response.buttons if command.startswith("menu_event ")]))
        response = self.menu.handle("events", ["2"])
        self.assertIn(("Previous page", "events 1"), response.buttons)
        event = events[-1]
        self.assertIn(
            f"menu_event {event['event_id']} {event['profile_id']}",
            [command for _, command in response.buttons],
        )
        response = self.menu.handle("menu_event", [event["event_id"], event["profile_id"]])
        for action in ("event_on", "event_off", "event_run"):
            self.assertIn(
                f"{action} {event['event_id']} {event['profile_id']}",
                [command for _, command in response.buttons],
            )
        self.assertGreater(
            len(response.buttons[0][1].encode("utf-8")), 64,
            "Full IDs must be retained internally despite Telegram's callback limit",
        )

    def test_event_is_rechecked_live_and_removed_event_cannot_be_selected(self):
        event = self.event()
        self.control.runtime_command.return_value = {"events": [event]}
        self.menu.handle("events", [])
        self.control.runtime_command.return_value = {"events": []}
        response = self.menu.handle("menu_event", [event["event_id"], event["profile_id"]])
        self.assertIn("no longer available", response.text)
        self.handler.assert_not_called()

    def test_emulator_buttons_and_operation_progress_do_not_require_typed_ids(self):
        self.control.runtime_command.return_value = {
            "emulators": [{"instance": 0, "running": True}, {"instance": 1, "running": False}],
        }
        response = self.menu.handle("emulators", [])
        self.assertIn("emulator_restart 1", [command for _, command in response.buttons])
        self.control.runtime_command.return_value = {"state": "running", "operation_id": "op-123"}
        response = self.menu.handle("emulator_restart", ["1"])
        self.assertIn(("Check progress", "operation op-123"), response.buttons)
        self.control.runtime_command.assert_called_with("emulator.restart", {"instance": "1"})
        response = self.menu.handle("operation", ["op-123"])
        self.assertIn(("Refresh progress", "operation op-123"), response.buttons)

    def test_tasks_are_paginated_and_errors_remain_visible(self):
        task = {
            "profile_name": "Main", "event_name": "Collect",
            "task_name": "Reward", "task_status": "running",
        }
        self.control.runtime_command.return_value = {"tasks": [task] * 10, "truncated": True}
        response = self.menu.handle("tasks", [])
        self.assertIn("Reward | running", response.text)
        self.assertIn(("Next page", "tasks 2"), response.buttons)
        self.assertIn("limit reached", response.text)
        self.control.runtime_command.return_value = {"state": "failed", "error": "Disk unavailable"}
        self.assertEqual("Disk unavailable", self.menu.handle("events", []).text)
        self.control.runtime_command.return_value = None
        self.assertIn("invalid control", self.menu.handle("emulators", []).text)

    def test_log_and_help_responses_keep_navigation(self):
        self.handler.return_value = TelegramMenu("Log files", (("Download", "log abc"),))
        response = self.menu.handle("logs", [])
        self.assertIn(("Download", "log abc"), response.buttons)
        self.assertIn(("Main Menu", "menu"), response.buttons)
        self.handler.return_value = "Commands: /status"
        self.assertIn(("Main Menu", "menu"), self.menu.handle("help", []).buttons)

    def test_main_menu_has_no_return_button_but_submenus_do(self):
        self.assertNotIn("menu", [command for _, command in self.menu.handle("menu", []).buttons])
        for command in ("studio", "runtime", "help"):
            with self.subTest(command=command):
                self.assertIn(("Main Menu", "menu"), self.menu.handle(command, []).buttons)

    def test_help_describes_available_controls_and_operating_requirements(self):
        response = _handle_telegram_command("help", [], self.control)
        for heading in (
            "Studio control", "Bot control", "Events & tasks",
            "Emulator maintenance", "Monitoring & files", "Navigation & help",
        ):
            self.assertIn(heading, response)
        for command in (
            "/studio_start", "/studio_stop", "/studio_restart", "/runtime_start",
            "/runtime_stop", "/event_on", "/event_off", "/event_run",
            "/emulator_restart", "/operation", "/status", "/logs", "/log", "/help",
        ):
            self.assertIn(command, response)
        self.assertIn("even when Studio is closed", response)
        self.assertIn("Interrupted tasks are not blindly replayed", response)
        self.assertLess(len(response), TelegramService.MAX_MESSAGE_LENGTH)
        self.control.runtime_command.assert_not_called()

    def test_invalid_arguments_and_page_ranges_do_not_trigger_actions(self):
        for command in ("studio", "runtime", "start", "menu"):
            self.assertIn("Usage", self.menu.handle(command, ["extra"]).text)
        for command in ("events", "tasks", "emulators"):
            self.assertIn("Usage", self.menu.handle(command, ["../bad"]).text)
        self.control.runtime_command.return_value = {"events": []}
        self.assertIn("between", self.menu.handle("events", ["0"]).text)
        self.assertIn("No entries", self.menu.handle("events", []).text)
        self.handler.assert_not_called()


class TelegramMenuTransportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.calls = []
        self.now = 100.0
        self.handler = Mock(return_value="Done")
        self.service = TelegramService(
            token="test-token", allowed_user_ids={123, 124}, allowed_chat_ids={-456, -457},
            command_handler=self.handler,
            logger=logging.getLogger("test.menu"),
            clock=lambda: self.now,
            unix_time=lambda: 1_800_000_000,
            state_file=Path(temporary.name) / "offset.json",
            api_call=lambda method, parameters: self.calls.append((method, parameters)) or {},
        )
        self.service._last_send_at = None
        wait_patcher = patch.object(self.service._stop_event, "wait", return_value=False)
        wait_patcher.start()
        self.addCleanup(wait_patcher.stop)

    def callback(self, data, user_id=123, chat_id=-456):
        return {
            "callback_query": {
                "id": "cb-1", "data": data, "from": {"id": user_id},
                "message": {"chat": {"id": chat_id}, "date": 1_800_000_000},
            }
        }

    def button(self, label):
        markup = [parameters for method, parameters in self.calls if method == "sendMessage"][-1]["reply_markup"]
        return next(
            button["callback_data"] for row in markup["inline_keyboard"]
            for button in row if button["text"] == label or button["text"].endswith(" " + label)
        )

    def send_menu(self, menu):
        self.service._send_menu(-456, 123, menu)
        self.service._last_send_at = None

    def click(self, data, **kwargs):
        self.service._last_send_at = None
        self.service._handle_update(self.callback(data, **kwargs))

    def test_mutating_button_requires_confirmation_and_executes_once(self):
        self.send_menu(TelegramMenu("Studio", (("Restart", "studio_restart"),)))
        self.click(self.button("Restart"))
        self.handler.assert_not_called()
        confirmation = self.button("Confirm")
        self.click(confirmation)
        self.handler.assert_called_once_with("studio_restart", [])
        self.click(confirmation)
        self.handler.assert_called_once()
        self.assertIn("expired", self.calls[-1][1]["text"])

    def test_start_installs_persistent_keyboard_and_inline_main_menu(self):
        self.handler.return_value = TelegramMenu("Choose", (("Status", "status"),))
        self.service._handle_update({
            "message": {
                "chat": {"id": -456}, "from": {"id": 123},
                "date": 1_800_000_000, "text": "/start",
            }
        })
        messages = [parameters for method, parameters in self.calls if method == "sendMessage"]
        self.assertTrue(messages[0]["reply_markup"]["is_persistent"])
        self.assertIn("inline_keyboard", messages[1]["reply_markup"])
        self.handler.assert_called_once_with("start", [])

    def test_restart_invalidates_previously_issued_tokens(self):
        self.send_menu(TelegramMenu("Status", (("Status", "status"),)))
        old = self.button("Status")
        self.service._callbacks.clear()
        self.click(old)
        self.handler.assert_not_called()

    def test_expiry_during_callback_acknowledgement_prevents_execution(self):
        self.send_menu(TelegramMenu("Status", (("Status", "status"),)))
        data = self.button("Status")
        def api_call(method, parameters):
            self.calls.append((method, parameters))
            if method == "answerCallbackQuery":
                self.now += self.service.MAX_MESSAGE_AGE_SECONDS + 1
            return {}
        self.service._api_call_override = api_call
        self.click(data)
        self.handler.assert_not_called()
        self.assertIn("expired", self.calls[-1][1]["text"])

    def test_all_mutating_buttons_are_confirmation_gated(self):
        for command in self.service.CONFIRM_COMMANDS:
            with self.subTest(command=command):
                self.handler.reset_mock()
                self.service._command_times.clear()
                arguments = (
                    ["event-1", "profile-1"] if command.startswith("event_")
                    else ["0"] if command in {"emulator_restart", "runtime_pause", "runtime_resume"} else []
                )
                full_command = " ".join([command, *arguments])
                self.send_menu(TelegramMenu("Action", (("Action", full_command),)))
                self.click(self.button("Action"))
                self.handler.assert_not_called()
                self.click(self.button("Confirm"))
                self.handler.assert_called_once_with(command, arguments)

    def test_emulator_pause_button_reaches_control_only_after_authorized_confirmation(self):
        control = Mock()
        control.runtime_command.return_value = {"emulators": [{
            "instance": 0, "running": True, "automation": "enabled",
        }]}
        menu = TelegramControlMenu(
            control, lambda command, arguments: _handle_telegram_command(command, arguments, control),
        )
        self.service.command_handler = menu.handle
        self.send_menu(menu.handle("emulators", []))
        self.click(self.button("Pause instance 0"))
        control.runtime_command.assert_called_once_with("emulators.list")
        confirm = self.button("Confirm")
        self.click(confirm, user_id=124)
        control.runtime_command.assert_called_once_with("emulators.list")
        control.runtime_command.return_value = {"accepted": True, "state": "pause_requested"}
        self.click(confirm)
        control.runtime_command.assert_called_with("runtime.pause", {"instance": "0"})
        self.assertIn("pause requested", self.calls[-1][1]["text"])

    def test_event_button_path_reaches_existing_profile_scoped_control_contract(self):
        event = TelegramControlMenuTests.event(self)
        control = Mock()
        control.runtime_command.return_value = {"events": [event]}
        menu = TelegramControlMenu(
            control, lambda command, arguments: _handle_telegram_command(command, arguments, control),
        )
        self.service.command_handler = menu.handle
        self.send_menu(menu.handle("events", []))
        self.click(self.button("Profile 0 / Event 0 | ON"))
        self.click(self.button("Disable"))
        control.runtime_command.assert_called_with("events.list")
        control.runtime_command.return_value = {
            "event_name": "Event 0", "enabled": False,
        }
        self.click(self.button("Confirm"))
        control.runtime_command.assert_called_with(
            "event.disable",
            {"event_id": event["event_id"], "profile_id": event["profile_id"]},
        )
        self.assertIn("disabled", self.calls[-1][1]["text"])

    def test_cancel_invalidates_confirmation(self):
        self.send_menu(TelegramMenu("Runtime", (("Stop", "runtime_stop"),)))
        self.click(self.button("Stop"))
        confirm = self.button("Confirm")
        self.click(self.button("Main Menu"))
        self.handler.assert_called_once_with("menu", [])
        self.click(confirm)
        self.handler.assert_called_once()

    def test_authorized_other_user_or_chat_cannot_use_issued_button(self):
        self.send_menu(TelegramMenu("Status", (("Status", "status"),)))
        data = self.button("Status")
        self.click(data, user_id=124)
        self.click(data, chat_id=-457)
        self.click("studio_stop")
        self.handler.assert_not_called()
        self.click(data)
        self.handler.assert_called_once_with("status", [])

    def test_long_unicode_labels_and_full_ids_use_short_ascii_tokens(self):
        command = "event_on " + "a" * 36 + " " + "b" * 36
        self.send_menu(TelegramMenu("Events", (("Event " + "\u00e4" * 100, command),)))
        keyboard = self.calls[-1][1]["reply_markup"]["inline_keyboard"]
        data = keyboard[0][0]["callback_data"]
        self.assertTrue(data.isascii())
        self.assertLessEqual(len(data.encode("utf-8")), 64)
        self.assertEqual(64, len(keyboard[0][0]["text"]))
        self.click(data)
        self.click(self.button("Confirm"))
        self.handler.assert_called_once_with("event_on", ["a" * 36, "b" * 36])

    def test_buttons_expire_and_registry_is_bounded(self):
        self.send_menu(TelegramMenu("Status", (("Status", "status"),)))
        old = self.button("Status")
        self.now += self.service.MAX_MESSAGE_AGE_SECONDS + 1
        self.click(old)
        self.handler.assert_not_called()
        for _ in range(self.service.MAX_CALLBACKS + 10):
            self.service._register_button(-456, 123, "status", False)
        self.assertEqual(self.service.MAX_CALLBACKS, len(self.service._callbacks))

    def test_reply_keyboard_labels_and_start_are_routed(self):
        for label, command in self.service.MENU_LABEL_COMMANDS.items():
            self.service._last_send_at = None
            self.service._command_times.clear()
            self.service._handle_update({
                "message": {
                    "chat": {"id": -456}, "from": {"id": 123},
                    "date": 1_800_000_000, "text": label,
                }
            })
            self.assertEqual((command, []), self.handler.call_args.args)
        markup = self.calls[-1][1]["reply_markup"]
        self.assertTrue(markup["is_persistent"])
        self.assertTrue(markup["resize_keyboard"])

    def test_persistent_overview_has_icons_without_redundant_menu_button(self):
        self.service._send_message(-456, "Overview")
        keyboard = self.calls[-1][1]["reply_markup"]["keyboard"]
        self.assertEqual(
            ["ℹ️ Status", "🖥️ Studio", "🤖 Runtime", "📅 Events",
             "📋 Tasks", "📱 Emulators", "📸 Info", "📊 Statistics", "📄 Logs", "❓ Help"],
            [button["text"] for row in keyboard for button in row],
        )
        for row in self.service.MAIN_KEYBOARD_ROWS:
            for label, command in row:
                self.service._command_times.clear()
                self.service._last_send_at = None
                self.service._handle_update({
                    "message": {
                        "chat": {"id": -456}, "from": {"id": 123},
                        "date": 1_800_000_000, "text": label,
                    },
                })
                self.assertEqual((command, []), self.handler.call_args.args)

    def test_inline_buttons_have_icons_and_house_return(self):
        menu = TelegramControlMenu(Mock(), Mock(return_value="Help"))
        self.send_menu(menu.handle("runtime", []))
        keyboard = self.calls[-1][1]["reply_markup"]["inline_keyboard"]
        self.assertEqual(
            ["▶️ Start Runtime", "⏹️ Stop Runtime", "⏸️ Pause all emulators",
             "▶️ Resume all (reinitialize)", "📱 Choose emulator",
             "ℹ️ Status", "🏠 Main Menu"],
            [button["text"] for row in keyboard for button in row],
        )
        self.click(self.button("Main Menu"))
        self.handler.assert_called_once_with("menu", [])

    def test_confirmation_cannot_bypass_rate_limit_or_stop(self):
        self.send_menu(TelegramMenu("Studio", (("Start", "studio_start"),)))
        self.service._command_times.extend([self.now] * self.service.MAX_COMMANDS_PER_MINUTE)
        self.click(self.button("Start"))
        self.handler.assert_not_called()
        self.service._command_times.clear()
        self.send_menu(TelegramMenu("Runtime", (("Start", "runtime_start"),)))
        self.click(self.button("Start"))
        confirm = self.button("Confirm")
        self.service._stop_event.set()
        self.click(confirm)
        self.handler.assert_not_called()

    def test_log_buttons_allow_multiple_separate_selections(self):
        self.send_menu(TelegramMenu("Logs", (("First", "log a"), ("Second", "log b"))))
        first, second = self.button("First"), self.button("Second")
        self.click(first)
        self.click(second)
        self.assertEqual(
            [("log", ["a"]), ("log", ["b"])],
            [call.args for call in self.handler.call_args_list],
        )


if __name__ == "__main__":
    started_at = time.monotonic()
    print("TEST START: Telegram control menu", flush=True)
    faulthandler.dump_traceback_later(20, exit=True)
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    )
    faulthandler.cancel_dump_traceback_later()
    print(f"TEST DURATION: {time.monotonic() - started_at:.3f}s", flush=True)
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE", flush=True)
    sys.exit(0 if result.wasSuccessful() else 1)
