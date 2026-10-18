import base64
import faulthandler
import json
import logging
import sys
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np

from models.execution_context import ExecutionContext
from models.bot_function import BotFunction
from models.function_block import FunctionBlock
from models.function_connection import FunctionConnection
from core.command_control import CommandCancelled
from scheduler.models import Event as SchedulerEvent, PROFILE_CHECK_KIND, PROFILE_SWITCH_KIND, SchedulerService, Task
from services.execution_activity_service import ExecutionActivityService
from services.function_runner import FunctionRunner
from services.remote_monitoring_service import RemoteMonitoringService
from services.runtime_service import RuntimeService
from services.statistics_service import StatisticsService
from services.telegram_control_menu import TelegramControlMenu
from services.telegram_service import TelegramMenu, TelegramPhoto, TelegramService
from services.watchdog_ipc_service import WatchdogIpcServer
from services.watchdog_control_service import WatchdogControlService
from ui.views.scheduler_view import SchedulerView
from ui.views.statistics_view import StatisticsView
from watchdog_app import _handle_telegram_command


class TelegramLiveMonitoringTests(unittest.TestCase):
    def setUp(self):
        self.started = time.monotonic()
        print(f"Test Start: {self.id()}")
        faulthandler.dump_traceback_later(10, exit=True)
        self.addCleanup(faulthandler.cancel_dump_traceback_later)
        self.addCleanup(lambda: print(f"Test End: {self.id()} Duration: {time.monotonic() - self.started:.3f}s"))
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = {"id": "p", "game": "g", "name": "Main", "emulator_instance": 0}
        self.statistics = StatisticsService(str(self.root / "statistics.json"))
        self.activity = ExecutionActivityService()
        self.runtime = SimpleNamespace(
            profile_service=Mock(), execution_activity=self.activity,
            statistics_service=self.statistics, _active_profiles={("g", 0): "p"},
            get_status=lambda: {"status": "Runtime Running"},
            _profile_id=RuntimeService._profile_id,
            emulator_manager=Mock(), adb_runtime_service=Mock(),
            game_runtime_service=Mock(), logger=Mock(),
        )
        self.runtime.profile_service.load_all.return_value = [self.profile]
        self.runtime.emulator_manager.adb_path = "unused-adb"
        self.runtime.emulator_manager.get_device_id.return_value = "device"
        self.runtime.game_runtime_service.get_package_name.return_value = "com.game"
        self.runtime.game_runtime_service.adb.has_anr.return_value = False
        self.monitor = RemoteMonitoringService(self.runtime)

    def service(self, handler, api):
        return TelegramService(
            token="test", allowed_user_ids={1}, allowed_chat_ids={2},
            command_handler=handler, api_call=api, logger=logging.getLogger("test.telegram.live"),
            unix_time=lambda: 1800000000, state_file=self.root / "offset.json",
        )

    def test_every_accepted_command_acknowledges_without_the_send_throttle(self):
        calls = []
        service = self.service(Mock(), lambda method, parameters: calls.append((method, parameters)))
        service._dispatch_command = Mock()
        service._last_send_at = service.clock()
        for command in ("status", "info", "statistics", "logs", "log", "runtime_stop", "help"):
            service._command_times.clear()
            with patch.object(service._stop_event, "wait", side_effect=AssertionError("Acknowledgement was throttled")):
                service._execute_command(command, [], 2, 1)
        self.assertEqual(7, len(calls))
        self.assertTrue(all("Request received" in parameters["text"] for _, parameters in calls))

    def test_polling_acknowledges_next_commands_while_a_handler_is_blocked(self):
        entered, release, acknowledged = Event(), Event(), Event()
        guard = Lock()
        incoming = []
        acknowledgements = []
        def api(method, parameters):
            if method == "getUpdates":
                with guard:
                    batch = list(incoming)
                    incoming.clear()
                if not batch:
                    time.sleep(0.01)
                return batch
            if method == "sendMessage" and "Request received" in parameters.get("text", ""):
                acknowledgements.append(time.monotonic())
                if len(acknowledgements) >= 2:
                    acknowledged.set()
            return {}
        def handler(command, arguments):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("Test command was not released")
            return "Done"
        service = self.service(handler, api)
        def cleanup():
            release.set()
            service.stop()
        self.addCleanup(cleanup)
        def update(index):
            return {"update_id": index, "message": {
                "date": 1800000000, "text": "/status", "chat": {"id": 2}, "from": {"id": 1},
            }}
        incoming.append(update(1))
        service.start()
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        with guard:
            incoming.append(update(2))
        self.assertTrue(acknowledged.wait(0.25), "Second acknowledgement waited for the first handler")
        self.assertLess(acknowledgements[-1] - started, 0.25)
        self.assertFalse(release.is_set())

    def test_acknowledgement_does_not_wait_for_a_busy_result_sender(self):
        calls = []
        service = self.service(Mock(), lambda method, parameters: calls.append((method, parameters)))
        service._dispatch_command = Mock()
        service._send_lock.acquire()
        self.addCleanup(service._send_lock.release)
        started = time.monotonic()
        service._execute_command("info", [], 2, 1)
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertEqual(1, len(calls))

    def test_acknowledgement_http_timeout_is_short_and_has_no_retry(self):
        service = self.service(Mock(), None)
        service._dispatch_command = Mock()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"ok":true,"result":{}}'
        with patch("services.telegram_service.urlopen", return_value=response) as opener:
            service._execute_command("status", [], 2, 1)
        self.assertEqual(1, opener.call_count)
        self.assertEqual(2, opener.call_args.kwargs["timeout"])

    def test_runner_tracks_nested_helpers_and_cleans_up_after_an_exception(self):
        runner = FunctionRunner(logger=Mock(), activity_service=self.activity)
        outer = SimpleNamespace(function_id="outer", name="Outer")
        helper = SimpleNamespace(function_id="helper", name="Helper")
        outer_context = ExecutionContext("p", "outer", game_id="g")
        helper_context = ExecutionContext("p", "helper", game_id="g", parent_id=outer_context.execution_id)
        def execute(function, context, profile, selected=None):
            if function is outer:
                self.assertEqual(["Outer"], [entry["function_name"] for entry in self.activity.snapshot()])
                return runner.run(helper, helper_context, profile)
            self.assertEqual(["Outer", "Helper"], [entry["function_name"] for entry in self.activity.snapshot()])
            raise RuntimeError("helper failed")
        runner._run = execute
        with self.assertRaisesRegex(RuntimeError, "helper failed"):
            runner.run(outer, outer_context, self.profile)
        self.assertEqual([], self.activity.snapshot())

    def test_verified_init_switch_counts_once_in_both_persistent_scopes(self):
        scheduler = SchedulerService(
            str(self.root / "scheduler"), reinitialize_service=object(),
            statistics_service=self.statistics, logger=Mock(),
        )
        event = SchedulerEvent(game_id="g", profile_id="p", is_init=True, tasks=[
            Task(kind=PROFILE_CHECK_KIND, function_ids=["check"]),
            Task(kind=PROFILE_SWITCH_KIND, function_ids=["switch"]),
        ])
        execute = Mock(side_effect=["failed", "success", "success"])
        self.assertEqual("success", scheduler._run_init_gate(event, execute, self.profile))
        for all_time in (False, True):
            self.assertEqual(1, self.statistics.snapshot(all_time)["g:p"]["profile_switches"])
        scheduler._run_init_gate(event, Mock(return_value="success"), self.profile)
        self.assertEqual(1, self.statistics.snapshot()["g:p"]["profile_switches"])

    def test_init_check_counts_the_actual_nested_switch_as_a_task(self):
        scheduler = SchedulerService(
            str(self.root / "scheduler"), reinitialize_service=object(),
            statistics_service=self.statistics, logger=Mock(),
        )
        switch = BotFunction(
            "switch", "Switch", "g", kind="helper", saved=True,
            blocks=[FunctionBlock("done", "success_terminal")],
        )
        check = BotFunction(
            "check", "Check", "g", saved=True,
            blocks=[
                FunctionBlock("call", "call_function", params={"function_id": "switch"}),
                FunctionBlock("done", "success_terminal"),
            ],
            connections=[FunctionConnection("call", "success", "done")],
        )
        root = self.root / "functions"
        (root / "g").mkdir(parents=True)
        for function in (check, switch):
            (root / "g" / f"{function.function_id}.json").write_text(
                json.dumps(function.to_dict()), encoding="utf-8",
            )
        helpers = Mock()
        helpers.load.return_value = switch
        event = SchedulerEvent(game_id="g", profile_id="p", is_init=True, tasks=[
            Task(task_id="check-task", name="Profile Check", kind=PROFILE_CHECK_KIND, function_ids=["check"]),
            Task(task_id="switch-task", name="Profile Switch", kind=PROFILE_SWITCH_KIND, function_ids=["switch"]),
        ])
        def execute():
            return scheduler.run_event(
                event, self.profile, function_root=str(root), persist=False,
                runner_factory=lambda: FunctionRunner(function_service=helpers, logger=Mock()),
            )
        self.assertEqual("success", execute()["status"])
        for all_time in (False, True):
            account = StatisticsService(str(self.statistics.file_name)).snapshot(all_time)["g:p"]
            self.assertEqual(1, account["profile_switches"])
            self.assertEqual(1, account["task_executions"]["switch-task"]["success"])
            self.assertEqual(0, account["task_executions"]["switch-task"]["failure"])
            self.assertEqual(1, account["task_executions"]["check-task"]["success"])

        switch.blocks = [FunctionBlock("fail", "failure_terminal")]
        (root / "g" / "switch.json").write_text(json.dumps(switch.to_dict()), encoding="utf-8")
        self.assertEqual("failed", execute()["status"])
        account = self.statistics.snapshot()["g:p"]
        self.assertEqual(1, account["profile_switches"])
        self.assertEqual(2, account["task_executions"]["switch-task"]["failure"])
        self.assertEqual(1, account["task_executions"]["switch-task"]["success"])

        check.blocks = [FunctionBlock("done", "success_terminal")]
        check.connections = []
        (root / "g" / "check.json").write_text(json.dumps(check.to_dict()), encoding="utf-8")
        self.assertEqual("success", execute()["status"])
        self.assertEqual(account["task_executions"]["switch-task"],
                         self.statistics.snapshot()["g:p"]["task_executions"]["switch-task"])
        self.assertEqual(1, self.statistics.snapshot()["g:p"]["profile_switches"])

    def test_statistics_switch_is_a_task_row_not_a_repeated_column(self):
        scheduler = Mock()
        scheduler.list_events.return_value = [
            SchedulerEvent(game_id="g", profile_id="p", tasks=[
                Task(task_id="switch-task", name="Profile Switch", kind=PROFILE_SWITCH_KIND),
            ]),
        ]
        profiles = Mock()
        profiles.load_all.return_value = [self.profile]
        self.statistics.record_task("g", "p", "Main", "switch-task", "Profile Switch", "success")
        with patch("ui.views.statistics_view.ProfileService", return_value=profiles):
            view = StatisticsView(self.statistics, scheduler_service=scheduler)
        self.addCleanup(view.deleteLater)
        table = view.current_table
        view._fill(table, self.statistics.snapshot())
        self.assertNotIn("Profile switches", StatisticsView.HEADERS)
        self.assertEqual("Profile Switch", table.item(0, 1).text())
        self.assertEqual("1", table.item(0, 2).text())

    def test_failed_switch_or_failed_validation_does_not_count(self):
        scheduler = SchedulerService(
            str(self.root / "scheduler"), reinitialize_service=object(),
            statistics_service=self.statistics, logger=Mock(),
        )
        event = SchedulerEvent(game_id="g", profile_id="p", is_init=True, tasks=[
            Task(kind=PROFILE_CHECK_KIND, function_ids=["check"]),
            Task(kind=PROFILE_SWITCH_KIND, function_ids=["switch"]),
        ])
        for outcomes in (["failed", "failed"], ["failed", "success", "failed"]):
            scheduler._run_init_gate(event, Mock(side_effect=outcomes), self.profile)
        self.assertEqual({}, self.statistics.snapshot())

    def test_manual_profile_switch_persists_verified_target_count(self):
        runtime = object.__new__(RuntimeService)
        runtime.statistics_service = self.statistics
        runtime._ensure_init_event = Mock(return_value=SchedulerEvent(tasks=[
            Task(task_id="switch-task", name="Profile Switch", kind=PROFILE_SWITCH_KIND, function_ids=["switch"]),
        ]))
        runtime._activate_profile_variables = Mock()
        runtime._run_control_function = Mock(return_value={"outcome": "success"})
        runtime.test_profile_validation = Mock(return_value={"state": "Healthy", "message": "validated"})
        runtime.logger = Mock()
        result = runtime.test_profile_switch(self.profile, "target")
        self.assertEqual("Healthy", result["state"])
        self.assertEqual(1, self.statistics.snapshot()["g:target"]["profile_switches"])
        self.assertEqual(1, self.statistics.snapshot()["g:target"]["task_executions"]["switch-task"]["success"])
        runtime.test_profile_validation.return_value = {"state": "Failed", "message": "wrong account"}
        runtime.test_profile_switch(self.profile, "target")
        self.assertEqual(1, self.statistics.snapshot()["g:target"]["profile_switches"])

    def test_lazy_manual_scheduler_uses_runtime_statistics_service(self):
        runtime = SimpleNamespace(statistics_service=self.statistics)
        view = SimpleNamespace(
            _runtime_service=None, variable_service=Mock(), scheduler_service=Mock(),
        )
        with patch("services.runtime_service.RuntimeService", return_value=runtime):
            self.assertIs(runtime, SchedulerView.runtime_service.fget(view))
        self.assertIs(self.statistics, view.scheduler_service.statistics_service)

    def test_monitoring_can_run_while_runtime_start_or_control_is_busy(self):
        control = WatchdogControlService(self.runtime)
        control._operation = "start"
        control._active_scheduler_operation = "maintenance"
        with patch.object(control._monitoring_pool, "start") as start:
            result = control.handle_request("statistics.get", {})
            self.assertTrue(result["accepted"])
            worker = start.call_args.args[0]
            worker.run()
            operation = control.handle_request("control.operation.get", {"operation_id": result["operation_id"]})
        self.assertEqual("completed", operation["state"])
        self.assertEqual([], operation["result"]["accounts"])
        self.assertEqual("maintenance", control._active_scheduler_operation)
        control.deleteLater()

    def test_telegram_statistics_use_live_persisted_counts_and_both_scopes(self):
        self.statistics.record_profile_switch(self.profile)
        self.statistics.record_task("g", "p", "Main", "t", "Collect", "success")
        control = SimpleNamespace(runtime_command=self.monitor.handle)
        before = _handle_telegram_command("statistics", [], control)
        self.assertIn("Switches: 1", before.text)
        self.assertIn("1 successful / 0 failed", before.text)
        self.statistics.reset_current()
        self.assertIn("No statistics", _handle_telegram_command("stats", [], control).text)
        self.assertIn("Switches: 1", _handle_telegram_command("statistics", ["alltime"], control).text)

    def test_info_captures_fresh_photo_and_transfers_bounded_ipc_chunks(self):
        image = np.random.default_rng(3).integers(0, 256, (900, 1600, 3), dtype=np.uint8)
        path = self.root / "capture.png"
        self.assertTrue(cv2.imwrite(str(path), image))
        emulator = Mock()
        emulator.screenshot.return_value = str(path)
        function = SimpleNamespace(function_id="f", name="Gather")
        context = ExecutionContext("p", "f", game_id="g")
        with self.activity.track(function, context, self.profile), patch(
            "services.remote_monitoring_service.Emulator", return_value=emulator,
        ):
            result = self.monitor.info({})
        self.assertFalse(path.exists(), "Temporary screenshot was not removed")
        self.assertEqual("Healthy", result["state"])
        self.assertEqual("Gather", result["activity"][0]["function_name"])
        content = bytearray()
        for index in range(result["photo"]["chunks"]):
            chunk = self.monitor.photo_chunk({"token": result["photo"]["token"], "index": index})
            self.assertLess(len(json.dumps({"result": chunk}).encode()), WatchdogIpcServer.MAX_MESSAGE_BYTES)
            content.extend(base64.b64decode(chunk["content"]))
        self.assertEqual(result["photo"]["bytes"], len(content))
        self.assertTrue(content.startswith(b"\xff\xd8"))
        with self.assertRaises(ValueError):
            self.monitor.photo_chunk({"token": result["photo"]["token"], "index": 999})

    def test_info_does_not_send_old_screenshots_for_stopped_emulator(self):
        self.runtime.emulator_manager.is_instance_running.return_value = False
        with patch("services.remote_monitoring_service.Emulator") as emulator:
            result = self.monitor.info({})
        self.assertEqual("Stopped", result["state"])
        self.assertIsNone(result["photo"])
        emulator.assert_not_called()

    def test_slow_live_info_is_cancelled_with_an_explicit_timeout(self):
        self.runtime.emulator_manager.is_instance_running.side_effect = CommandCancelled("cancelled")
        with self.assertRaisesRegex(TimeoutError, "Live emulator query timed out"):
            self.monitor.info({})
        self.runtime.logger.warning.assert_called_once()

    def test_expired_screenshot_token_is_not_reused(self):
        self.monitor._photos["old"] = (time.monotonic() - 61, b"jpeg")
        with self.assertRaisesRegex(ValueError, "expired"):
            self.monitor.photo_chunk({"token": "old", "index": 0})
        self.assertEqual({}, self.monitor._photos)

    def test_screenshot_failure_is_explicit_and_not_healthy(self):
        with patch("services.remote_monitoring_service.Emulator") as emulator:
            emulator.return_value.screenshot.return_value = None
            result = self.monitor.info({})
        self.assertEqual("Unknown", result["state"])
        self.assertIn("Screenshot unavailable", result["message"])
        self.runtime.logger.error.assert_called_once()

    def test_info_transport_and_menu_preserve_photo_and_running_function(self):
        info = {
            "page": 1, "pages": 1, "runtime": "Paused", "instance": 0,
            "state": "Healthy", "observed_at": "now", "message": "Live capture",
            "activity": [{"execution_id": "run", "parent_id": None, "function_name": "Gather"}],
            "photo": {"token": "token", "bytes": 3, "chunks": 1},
        }
        control = Mock()
        control.runtime_command.side_effect = [info, {"content": base64.b64encode(b"jpg").decode()}]
        photo = _handle_telegram_command("info", [], control)
        self.assertIsInstance(photo, TelegramPhoto)
        self.assertIn("Running Function: Gather", photo.caption)
        self.assertIs(photo, TelegramControlMenu(control, Mock(return_value=photo)).handle("info", []))
        calls = []
        service = self.service(Mock(return_value=photo), lambda method, parameters: calls.append((method, parameters)))
        with patch.object(service._stop_event, "wait", return_value=False):
            service._execute_command("info", [], 2, 1)
        self.assertEqual(["sendMessage", "sendPhoto", "sendMessage"], [method for method, _ in calls])
        self.assertTrue(all(parameters["chat_id"] == 2 for _, parameters in calls))

    def test_photo_http_transport_is_jpeg_multipart(self):
        service = self.service(Mock(), None)
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"ok":true,"result":{}}'
        photo = TelegramPhoto(b"jpeg bytes", "Gather", TelegramMenu("Info", ()))
        with patch("services.telegram_service.urlopen", return_value=response) as opener:
            self.assertTrue(service._send_photo(2, photo))
        request = opener.call_args.args[0]
        self.assertIn(b'name="photo"', request.data)
        self.assertIn(b"image/jpeg", request.data)
        self.assertIn(b"jpeg bytes", request.data)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(TelegramLiveMonitoringTests),
    )
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE")
    sys.exit(not result.wasSuccessful())
