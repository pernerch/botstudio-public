"""Runtime debug delivery without live profiles, emulator actions or stores."""

from contextlib import nullcontext
import faulthandler
import multiprocessing
import os
import threading
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from models.bot_function import BotFunction
from models.execution_context import ExecutionContext
from models.function_block import FunctionBlock
from services.debug_pacing_service import DebugEventHub, DebugPacingService
from services.function_runner import FunctionRunner
from services.profile_worker import ProfileWorker
from ui.views.debug_view import DebugView


def spawned_debug_run(pacing, hub, ready, proceed):
    DebugPacingService._instance = pacing
    DebugEventHub._instance = hub
    ready.set()
    if not proceed.wait(5):
        raise RuntimeError("Parent did not release diagnostic worker")
    runner = FunctionRunner(logger=Mock(), activity_service=Mock(track=lambda *_: nullcontext()))
    function = BotFunction(
        function_id="spawned", name="Spawned", game_id="test",
        blocks=[FunctionBlock(block_id="done", block_type="success_terminal")],
    )
    runner.run(function, ExecutionContext(profile_id="another", function_id="spawned"))
    runner.run(function, ExecutionContext(profile_id="test", function_id="spawned"))


class RuntimeDebugObservationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.started = time.monotonic()
        print(f"TEST START: {self.id()}", flush=True)
        watchdog = threading.Timer(15, self.timeout)
        watchdog.daemon = True
        watchdog.start()
        self.addCleanup(watchdog.cancel)
        self.pacing = DebugPacingService()
        self.hub = DebugEventHub()
        self.addCleanup(self.close_hub)
        self.pacing_patch = patch.object(DebugPacingService, "_instance", self.pacing)
        self.hub_patch = patch.object(DebugEventHub, "_instance", self.hub)
        self.pacing_patch.start()
        self.hub_patch.start()
        self.addCleanup(self.pacing_patch.stop)
        self.addCleanup(self.hub_patch.stop)
        self.runner = FunctionRunner(
            logger=Mock(), activity_service=Mock(track=lambda *_: nullcontext()),
        )
        self.function = BotFunction(
            function_id="main", name="Main", game_id="test",
            blocks=[FunctionBlock(block_id="done", block_type="success_terminal")],
        )
        self.context = ExecutionContext(profile_id="profile", function_id="main", game_id="test")
        self.pacing.select_profile("profile")

    @staticmethod
    def timeout():
        print("TEST RESULT: TIMEOUT", flush=True)
        faulthandler.dump_traceback()
        os._exit(124)

    def close_hub(self):
        self.hub.poll()
        self.hub._queue.close()
        self.hub._queue.join_thread()

    def tearDown(self):
        print(f"TEST END: {self.id()} ({time.monotonic() - self.started:.3f}s)", flush=True)

    def events(self, count):
        result = []
        deadline = time.monotonic() + 3
        while len(result) < count and time.monotonic() < deadline:
            result.extend(self.hub.poll())
            if len(result) < count:
                time.sleep(0.01)
        self.assertEqual(count, len(result))
        return result

    def test_runtime_runner_publishes_graph_blocks_and_completion(self):
        self.pacing.set_enabled(True)
        self.context.variables["Cooldown"] = 42
        self.runner.run(self.function, self.context)
        events = self.events(3)
        self.assertEqual(
            ["function_started", "block_started", "function_finished"],
            [event["event"] for event in events],
        )
        self.assertEqual("Main", events[0]["function"]["name"])
        self.assertEqual("done", events[1]["block_id"])
        self.assertEqual(42, events[1]["variables"]["Cooldown"])
        self.assertEqual(self.context.execution_id, events[1]["execution_id"])
        self.assertEqual("test", events[1]["game_id"])

    def test_off_mode_never_snapshots_or_creates_observation_queue(self):
        with patch.object(self.context, "snapshot", wraps=self.context.snapshot) as snapshot:
            # The ordinary run result has its own snapshot; debug adds none.
            with patch.object(DebugEventHub, "instance", side_effect=AssertionError("Debug must be off")):
                self.runner.run(self.function, self.context)
            self.assertEqual(1, snapshot.call_count)

    def test_no_selection_means_no_observation_or_pacing(self):
        self.pacing.select_profile(None)
        self.pacing.set_enabled(True)
        self.pacing.pause()
        self.pacing.set_factor(10)
        with patch.object(DebugEventHub, "instance", side_effect=AssertionError("No selected profile")):
            with patch("services.debug_pacing_service.time.sleep", side_effect=AssertionError("Must not wait")):
                self.runner.run(self.function, self.context)

    def test_paused_slow_profile_does_not_observe_or_delay_another_profile(self):
        self.pacing.set_enabled(True)
        self.pacing.set_factor(10)
        self.pacing.pause()
        with patch.object(DebugEventHub, "instance", side_effect=AssertionError("Wrong profile")):
            with patch("services.debug_pacing_service.time.sleep", side_effect=AssertionError("Must not wait")):
                self.runner.run(self.function, ExecutionContext(
                    profile_id="another", function_id="main", game_id="test",
                ))

    def test_switching_selected_profile_releases_old_pause_without_delaying_it(self):
        self.pacing.set_enabled(True)
        self.pacing.set_factor(10)
        self.pacing.pause()
        result = []
        thread = threading.Thread(target=lambda: result.append(
            self.runner.run(self.function, self.context)
        ))
        def cleanup():
            self.pacing.set_enabled(False)
            thread.join(2)
        self.addCleanup(cleanup)
        thread.start()
        self.events(2)
        self.assertTrue(thread.is_alive())
        self.pacing.select_profile("another")
        thread.join(0.5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(1, len(result))
        self.assertFalse(self.pacing.observes("profile"))
        self.assertTrue(self.pacing.observes("another"))

    def test_fast_switch_away_and_back_releases_previous_generation_wait(self):
        self.pacing.set_enabled(True)
        self.pacing.set_factor(10)
        self.pacing.pause()
        def switch(_seconds):
            self.pacing.select_profile("another")
            self.pacing.select_profile("profile")
        with patch("services.debug_pacing_service.time.sleep", side_effect=switch) as sleep:
            self.assertTrue(self.pacing.before_block(lambda: False, "profile"))
        sleep.assert_called_once()

    def test_profile_selector_is_explicit_and_rejects_old_target_events(self):
        runtime = Mock()
        runtime.get_status.return_value = {"profiles": [
            {"profile_id": "profile", "profile_name": "A", "emulator_instance": 0},
            {"profile_id": "another", "profile_name": "B", "emulator_instance": 2},
        ]}
        with patch("ui.views.debug_view.get_logger", return_value=Mock()):
            view = DebugView(runtime)
        self.addCleanup(view.deleteLater)
        view._poll_timer.stop()
        view.debug_switch.setChecked(True)
        self.assertEqual("", view.profile_combo.currentData())
        self.assertFalse(view.pause_button.isEnabled())
        self.assertFalse(self.pacing.observes("profile"))
        view.profile_combo.setCurrentIndex(view.profile_combo.findData("profile"))
        self.assertTrue(view.pause_button.isEnabled())
        self.runner.run(self.function, self.context)
        old_events = self.events(3)
        view.profile_combo.setCurrentIndex(view.profile_combo.findData("another"))
        view.profile_combo.setCurrentIndex(view.profile_combo.findData("profile"))
        with patch.object(self.hub, "poll", return_value=old_events):
            view._poll_block_events()
        self.assertIsNone(view._active_function_id)
        self.runner.run(self.function, self.context)
        with patch.object(self.hub, "poll", return_value=self.events(3)):
            view._poll_block_events()
        self.assertEqual("main", view._active_function_id)

    def test_enabled_mid_run_announces_current_graph(self):
        self.runner.block_observer = lambda *_: self.pacing.set_enabled(True)
        self.function.blocks.insert(
            0, FunctionBlock(block_id="config", block_type="configure_field"),
        )
        self.runner.run(self.function, self.context)
        events = self.events(3)
        self.assertEqual("function_started", events[0]["event"])
        self.assertEqual("done", events[1]["block_id"])

    def test_switching_away_and_back_reannounces_graph_in_existing_execution(self):
        self.pacing.set_enabled(True)
        self.function.blocks.insert(
            0, FunctionBlock(block_id="config", block_type="configure_field"),
        )
        def switch(block, _context):
            if block.block_id == "config":
                self.pacing.select_profile("another")
                self.pacing.select_profile("profile")
        self.runner.block_observer = switch
        self.runner.run(self.function, self.context)
        events = self.events(5)
        starts = [event for event in events if event["event"] == "function_started"]
        self.assertEqual(2, len(starts))
        self.assertNotEqual(starts[0]["debug_generation"], starts[1]["debug_generation"])
        self.assertEqual("done", events[-2]["block_id"])

    def test_helper_events_preserve_parent_and_restore_caller(self):
        helper = BotFunction(
            function_id="helper", name="Helper", game_id="test",
            blocks=[FunctionBlock(block_id="helper-done", block_type="success_terminal")],
        )
        self.runner.function_service = Mock()
        self.runner.function_service.load.return_value = helper
        self.function.blocks.insert(0, FunctionBlock(
            block_id="call", block_type="call_function", params={"function_id": "helper"},
        ))
        self.pacing.set_enabled(True)
        self.runner.run(self.function, self.context, {"game": "test"})
        events = self.events(7)
        helper_start = next(e for e in events if e["event"] == "function_started" and e["function_id"] == "helper")
        self.assertEqual(self.context.execution_id, helper_start["parent_id"])
        self.assertEqual("main", events[-2]["function_id"])
        self.assertEqual("done", events[-2]["block_id"])

    def test_queue_overflow_is_nonblocking_and_explicit(self):
        with self.assertLogs("services.debug_pacing_service", level="WARNING") as records:
            accepted = [self.hub.publish({"event": "test"}) for _ in range(300)]
        self.assertEqual(256, sum(accepted))
        self.assertEqual(1, len(records.output))

    def test_cancelled_debug_pause_returns_without_waiting_for_resume(self):
        self.pacing.set_enabled(True)
        self.pacing.pause()
        self.assertFalse(self.pacing.before_block(lambda: True, "profile"))

    def test_disabling_debug_releases_pause_and_resets_speed(self):
        self.pacing.set_enabled(True)
        self.pacing.set_factor(5)
        self.pacing.pause()
        self.pacing.set_enabled(False)
        self.assertFalse(self.pacing.paused)
        self.assertEqual(1, self.pacing.factor)
        self.assertTrue(self.pacing.before_block(lambda: False))

    def test_view_renders_runner_graph_without_file_reads(self):
        self.pacing.set_enabled(True)
        self.runner.run(self.function, self.context)
        events = self.events(3)
        with patch("ui.views.debug_view.get_logger", return_value=Mock()):
            view = DebugView()
        view.profile_combo.addItem("Profile", "profile")
        view.profile_combo.blockSignals(True)
        view.profile_combo.setCurrentIndex(1)
        view.profile_combo.blockSignals(False)
        self.addCleanup(view.deleteLater)
        view._poll_timer.stop()
        with patch.object(self.hub, "poll", return_value=events):
            view._poll_block_events()
        self.assertEqual("Main", view.function_headline.text())
        self.assertTrue(view.canvas._block_highlights["done"].isVisible())
        self.assertIn("block_started", view.log_view.toPlainText())

    def test_call_block_stays_on_caller_until_helper_actually_starts(self):
        self.pacing.set_enabled(True)
        self.function.blocks = [FunctionBlock(
            block_id="call", block_type="call_function", params={"function_id": "missing"},
        )]
        self.runner.run(self.function, self.context)
        events = self.events(3)
        with patch("ui.views.debug_view.get_logger", return_value=Mock()):
            view = DebugView()
        view.profile_combo.addItem("Profile", "profile")
        view.profile_combo.blockSignals(True)
        view.profile_combo.setCurrentIndex(1)
        view.profile_combo.blockSignals(False)
        self.addCleanup(view.deleteLater)
        view._poll_timer.stop()
        with patch.object(self.hub, "poll", return_value=events):
            view._poll_block_events()
        self.assertEqual("main", view._active_function_id)
        self.assertTrue(view.canvas._block_highlights["call"].isVisible())
        self.assertIn("function_failed", view.log_view.toPlainText())

    def test_disabled_view_drains_inflight_events_without_displaying_stale_runs(self):
        with patch("ui.views.debug_view.get_logger", return_value=Mock()):
            view = DebugView()
        self.addCleanup(view.deleteLater)
        view._poll_timer.stop()
        with patch.object(self.hub, "poll", return_value=[{
            "event": "block_started", "function_id": "old",
        }]) as poll:
            view._poll_block_events()
        poll.assert_called_once()
        self.assertIsNone(view._active_function_id)
        self.assertEqual("", view.log_view.toPlainText())

    def test_profile_worker_explicitly_passes_shared_debug_state(self):
        worker = ProfileWorker({"id": "test"})
        with patch("services.profile_worker.Process") as process:
            worker.start("main")
        args = process.call_args.kwargs["args"]
        self.assertIs(self.pacing, args[-2])
        self.assertIs(self.hub, args[-1])
        process.return_value.start.assert_called_once()

    def test_spawned_worker_observes_live_toggle_pause_and_shared_queue(self):
        ready = multiprocessing.Event()
        proceed = multiprocessing.Event()
        process = multiprocessing.get_context("spawn").Process(
            target=spawned_debug_run,
            args=(self.pacing, self.hub, ready, proceed),
        )
        def cleanup():
            self.pacing.set_enabled(False)
            proceed.set()
            if process.pid is not None:
                process.join(3)
                if process.is_alive():
                    process.terminate()
                    process.join(2)
        self.addCleanup(cleanup)
        process.start()
        self.assertTrue(ready.wait(5))
        self.pacing.select_profile("test")
        self.pacing.set_enabled(True)
        self.pacing.pause()
        proceed.set()
        events = self.events(2)
        self.assertEqual(["function_started", "block_started"], [e["event"] for e in events])
        self.assertTrue(process.is_alive(), "Child should wait at debug pause")
        self.pacing.resume()
        process.join(5)
        self.assertFalse(process.is_alive())
        self.assertEqual(0, process.exitcode)
        self.assertEqual("function_finished", self.events(1)[0]["event"])


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE", flush=True)
    raise SystemExit(0 if result.wasSuccessful() else 1)
