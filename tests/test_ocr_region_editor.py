import faulthandler
import os
import sys
import time
import tempfile
import unittest
from copy import deepcopy
from threading import Timer
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFormLayout, QLabel, QSpinBox, QWidget

from models.bot_function import BotFunction
from models.execution_context import ExecutionContext
from models.function_block import FunctionBlock
from models.function_connection import FunctionConnection
from services.function_block_catalog import BLOCK_CATALOG, BLOCK_OCR_READ
from services.function_runner import FunctionRunner
from services.function_service import FunctionService
from ui.views.function_editor_view import FunctionCanvas, FunctionEditorView, ClickableImage


class OcrRegionEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.view = FunctionEditorView.__new__(FunctionEditorView)
        QWidget.__init__(self.view)
        view = self.view
        view.param_layout = QFormLayout(view)
        view.block_error = QLabel(view)
        view.live_image = ClickableImage(parent=view)
        view.live_image.set_screenshot(QPixmap(800, 600), 400)
        view._region_selection_block = None
        view._last_runtime_matches = {}
        view._last_runtime_coordinates = {}
        view.current_function = None
        view.library_service = Mock()
        view.library_service.load.return_value = {
            "boundary": {"x": 10, "y": 193, "width": 238, "height": 421},
            "pattern": {"x": 74, "y": 272, "width": 97, "height": 18},
        }
        view.is_editable = Mock(return_value=True)
        self.block = FunctionBlock.create(
            block_type="ocr_read",
            params={"region": None, "library_entry_id": "entry", "offset_x": 57, "offset_y": 10},
        )
        view.selected_block = self.block
        view._refresh_details = Mock(side_effect=view._refresh_live_ocr_overlay)
        view.live_image.region_selected.connect(view._apply_search_region_selection)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.view.close()
        self.view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def runner_boundary(self):
        runner = object.__new__(FunctionRunner)
        runner.logger = Mock()
        runner.library_service = self.view.library_service
        context = ExecutionContext(profile_id="test", function_id="test")
        coordinates = self.view._last_runtime_coordinates.get(self.block.block_id)
        if coordinates:
            context.set(f"__coordinates_{self.block.block_id}__", coordinates)
        return runner._ocr_boundary(self.block, self.block.params, context)

    def test_read_ocr_has_region_controls(self):
        self.view._add_parameter_row(self.block, "region", None)
        self.assertTrue(hasattr(self.view, "select_region_button"))
        self.assertTrue(self.view.select_region_button.isEnabled())

    def test_read_ocr_delay_defaults_to_200_and_is_adjustable(self):
        self.assertEqual(
            200,
            BLOCK_CATALOG[BLOCK_OCR_READ]["params"]["delay_before_read_ms"],
        )
        self.view._add_parameter_row(self.block, "delay_before_read_ms", 200)
        delay_control = self.view.param_layout.itemAt(1).widget()

        self.assertIsInstance(delay_control, QSpinBox)
        self.assertEqual(200, delay_control.value())
        self.assertEqual(0, delay_control.minimum())
        delay_control.setValue(350)
        self.assertEqual(350, self.block.params["delay_before_read_ms"])

    def test_read_ocr_waits_before_capture_and_honors_configured_delay(self):
        calls = []
        runner = object.__new__(FunctionRunner)
        runner.ocr_service = Mock()
        runner.ocr_service.read.side_effect = (
            lambda *args, **kwargs: calls.append("read") or "recognized"
        )
        runner.screen_service = Mock()
        runner.screen_service.capture.side_effect = (
            lambda _profile: calls.append("capture") or "screen.png"
        )
        runner._ocr_boundary = Mock(return_value=None)
        runner._cancellable_sleep = Mock(
            side_effect=lambda _context, seconds: (
                calls.append(("delay", seconds)) or True
            )
        )
        context = ExecutionContext(profile_id="test", function_id="test")

        outcome, value, error = runner._ocr_read(
            self.block,
            {"library_entry_id": "entry", "delay_before_read_ms": 350},
            context,
            profile={},
        )

        self.assertEqual(("ok", "recognized", None), (outcome, value, error))
        self.assertEqual(
            [("delay", 0.35), "capture", "read"],
            calls,
        )

    def test_read_ocr_defaults_missing_delay_and_stops_if_cancelled(self):
        runner = object.__new__(FunctionRunner)
        runner.ocr_service = Mock()
        runner.screen_service = Mock()
        runner._ocr_boundary = Mock(return_value=None)
        context = ExecutionContext(profile_id="test", function_id="test")

        runner._cancellable_sleep = Mock(return_value=True)
        runner.screen_service.capture.return_value = "screen.png"
        runner.ocr_service.read.return_value = "recognized"
        runner._ocr_read(
            self.block,
            {"library_entry_id": "entry"},
            context,
            profile={},
        )
        runner._cancellable_sleep.assert_called_once_with(context, 0.2)

        runner._cancellable_sleep.reset_mock(return_value=True)
        runner._cancellable_sleep.return_value = False
        runner.screen_service.capture.reset_mock()
        runner.ocr_service.read.reset_mock()
        outcome, value, error = runner._ocr_read(
            self.block,
            {"library_entry_id": "entry", "delay_before_read_ms": 350},
            context,
            profile={},
        )
        self.assertEqual(("failed", None, "cancelled"), (outcome, value, error))
        runner.screen_service.capture.assert_not_called()
        runner.ocr_service.read.assert_not_called()

    def test_drawn_region_is_exact_runtime_region_and_survives_model_reload(self):
        self.view._add_search_region_row(self.block)
        self.view.show()
        self.view._begin_search_region_selection(self.block)
        QTest.mousePress(self.view.live_image, Qt.LeftButton, pos=QPoint(50, 60))
        QTest.mouseMove(self.view.live_image, QPoint(150, 100))
        QTest.mouseRelease(self.view.live_image, Qt.LeftButton, pos=QPoint(150, 100))
        expected = {
            "x": 100, "y": 120, "width": 200, "height": 80,
            "screen_width": 800, "screen_height": 600,
        }
        self.assertEqual(expected, self.block.params["region"])
        self.assertEqual(0, self.block.params["offset_x"])
        self.assertEqual(0, self.block.params["offset_y"])
        self.assertEqual(expected, self.runner_boundary())
        self.assertEqual(expected, self.view.live_image.overlay_region)
        function = BotFunction.create(name="OCR", game_id="test", author="test")
        function.blocks = [self.block]
        loaded = BotFunction.from_dict(function.to_dict())
        self.assertEqual(expected, loaded.blocks[0].params["region"])

    def test_coordinate_follow_uses_taught_pattern_size_not_search_boundary(self):
        self.view._last_runtime_coordinates[self.block.block_id] = {
            "x": 21, "y": 382, "width": 30, "height": 21,
        }
        self.view._refresh_live_ocr_overlay()
        self.assertEqual(self.runner_boundary(), self.view.live_image.overlay_region)
        self.assertEqual(
            {"x": 78, "y": 392, "width": 97, "height": 18},
            self.view.live_image.overlay_region,
        )

    def test_explicit_region_does_not_add_anchor_and_keeps_resolution(self):
        self.block.params["region"] = {
            "x": 100, "y": 120, "width": 200, "height": 80,
            "screen_width": 800, "screen_height": 600,
        }
        self.view._last_runtime_coordinates[self.block.block_id] = {"x": 500, "y": 400}
        self.view._refresh_live_ocr_overlay()
        self.assertEqual(self.runner_boundary(), self.view.live_image.overlay_region)
        self.assertEqual(157, self.view.live_image.overlay_region["x"])
        self.assertEqual(800, self.view.live_image.overlay_region["screen_width"])

    def test_cancel_does_not_change_saved_region_or_offsets(self):
        self.block.params["region"] = {"x": 1, "y": 2, "width": 3, "height": 4}
        original = deepcopy(self.block.params)
        self.view._add_search_region_row(self.block)
        self.view._begin_search_region_selection(self.block)
        self.view._apply_search_region_selection(None)
        self.assertEqual(original, self.block.params)

    def test_clear_restores_library_region_instead_of_claiming_fullscreen(self):
        self.block.params["region"] = {"x": 1, "y": 2, "width": 3, "height": 4}
        self.view._clear_search_region(self.block)
        self.assertIsNone(self.block.params["region"])
        self.assertEqual(self.runner_boundary(), self.view.live_image.overlay_region)
        self.assertIn("Library", self.view.block_error.text())

    def test_library_without_pattern_preserves_relative_boundary_fallback(self):
        self.view.library_service.load.return_value = {
            "boundary": {"x": 10, "y": 20, "width": 40, "height": 30},
        }
        self.view._last_runtime_coordinates[self.block.block_id] = {"x": 100, "y": 200}
        self.assertEqual(
            {"x": 167, "y": 230, "width": 40, "height": 30},
            self.runner_boundary(),
        )
        self.view._refresh_live_ocr_overlay()
        self.assertEqual(self.runner_boundary(), self.view.live_image.overlay_region)

    def test_without_coordinates_preserves_absolute_library_boundary(self):
        self.assertEqual(
            {"x": 67, "y": 203, "width": 238, "height": 421},
            self.runner_boundary(),
        )

    def test_no_library_uses_incoming_coordinate_rectangle(self):
        self.block.params["library_entry_id"] = ""
        self.view._last_runtime_coordinates[self.block.block_id] = {
            "x": 100, "y": 200, "width": 40, "height": 30,
        }
        self.assertEqual(
            {"x": 157, "y": 210, "width": 40, "height": 30},
            self.runner_boundary(),
        )

    def test_drawn_region_scales_at_actual_ocr_crop(self):
        import numpy as np
        from services.ocr_service import OcrService

        self.block.params["region"] = {
            "x": 100, "y": 120, "width": 200, "height": 80,
            "screen_width": 800, "screen_height": 600,
        }
        self.block.params["offset_x"] = self.block.params["offset_y"] = 0
        image = np.zeros((300, 400, 3), dtype=np.uint8)
        self.assertEqual((40, 100, 3), OcrService._crop(image, self.runner_boundary()).shape)

    def test_search_ocr_selection_still_preserves_tap_offsets(self):
        self.block.block_type = "search_ocr_tap"
        self.view._region_selection_block = self.block
        self.view._apply_search_region_selection({"x": 10, "y": 20, "width": 30, "height": 40})
        self.assertEqual(57, self.block.params["offset_x"])
        self.assertEqual(10, self.block.params["offset_y"])
        self.assertEqual(800, self.block.params["region"]["screen_width"])

    def test_search_ocr_blocks_expose_coordinate_input_and_preview_incoming_region(self):
        region = {"x": 100, "y": 200, "width": 80, "height": 30}
        for block_type in ("search_ocr", "search_ocr_tap"):
            with self.subTest(block_type=block_type):
                self.view._last_runtime_coordinates[self.block.block_id] = region
                overlay = self.overlay_for(block_type, region=None, offset_x=5, offset_y=7)
                self.assertEqual(["input", "coordinates"], FunctionCanvas.input_ports(self.block))
                self.assertEqual(region, {key: overlay[key] for key in region})
                if block_type == "search_ocr_tap":
                    self.assertEqual((145, 222), (overlay["tap_x"], overlay["tap_y"]))

    def test_search_ocr_without_wire_keeps_whole_screen_search(self):
        runner = FunctionRunner(screen_service=Mock(), ocr_service=Mock(), logger=Mock())
        runner.screen_service.capture.return_value = "isolated.png"
        runner.ocr_service.find_text.return_value = None
        for block_type in ("search_ocr", "search_ocr_tap"):
            block = FunctionBlock.create(block_type=block_type, params={"query": "missing"})
            outcome, value, error = runner.run_block(
                block, ExecutionContext(profile_id="test", function_id="test"),
                {"device_id": "isolated"},
            )
            self.assertEqual(("not_found", None, None), (outcome, value, error))
            self.assertIsNone(runner.ocr_service.find_text.call_args.args[2])

    def test_coordinate_anchor_preview_uses_wired_rectangle_before_debug_run(self):
        anchor = FunctionBlock.create(block_type="coordinate_anchor", params={
            "x": 100, "y": 200, "width": 80, "height": 30,
        })
        self.block.block_type = "search_ocr"
        self.block.params = {"region": None, "query": "Target"}
        self.view.current_function = BotFunction.create(name="Test", game_id="test", author="test")
        self.view.current_function.blocks = [anchor, self.block]
        self.view.current_function.connections = [FunctionConnection(
            source_block_id=anchor.block_id, source_outcome="coordinates",
            target_block_id=self.block.block_id, target_input="coordinates",
        )]
        self.view._refresh_live_ocr_overlay()
        self.assertEqual(anchor.params, self.view.live_image.overlay_region)

    def test_search_ocr_coordinate_wire_executes_source_and_limits_search_and_tap(self):
        for block_type in ("search_ocr", "search_ocr_tap"):
            for explicit in (None, {"x": 2, "y": 4, "width": 60, "height": 20}):
                with self.subTest(block_type=block_type, explicit=explicit):
                    anchor = FunctionBlock.create(block_type="coordinate_anchor", params={
                        "x": 100, "y": 200, "width": 80, "height": 30,
                        "screen_width": 800, "screen_height": 600,
                    })
                    target = FunctionBlock.create(block_type=block_type, params={
                        "query": "Target", "region": explicit, "offset_x": 5,
                        "offset_y": 7, "tap_delay_ms": 0,
                    })
                    function = BotFunction.create(name="OCR search", game_id="test", author="test")
                    function.blocks = [anchor, target]
                    function.connections = [FunctionConnection(
                        source_block_id=anchor.block_id, source_outcome="coordinates",
                        target_block_id=target.block_id, target_input="coordinates",
                    )]
                    runner = FunctionRunner(
                        screen_service=Mock(), ocr_service=Mock(), adb_service=Mock(), logger=Mock(),
                    )
                    runner.screen_service.capture.return_value = "isolated.png"
                    runner.ocr_service.find_text.return_value = {
                        "x": 110, "y": 205, "width": 40, "height": 10,
                    }
                    context = ExecutionContext(profile_id="test", function_id=function.function_id)
                    with patch("services.function_runner.Emulator") as emulator:
                        emulator.return_value.tap.return_value = True
                        outcome, value, error = runner._run_debug_operation(
                            function, target, context, {"device_id": "isolated"}, set(),
                        )
                    self.assertIsNone(error)
                    self.assertEqual("ok" if block_type == "search_ocr_tap" else "found", outcome)
                    expected_region = explicit or anchor.params
                    actual_region = runner.ocr_service.find_text.call_args.args[2]
                    self.assertEqual(
                        {key: expected_region[key] for key in actual_region},
                        actual_region,
                    )
                    if block_type == "search_ocr_tap":
                        emulator.return_value.tap.assert_called_once_with(135, 217)
                    else:
                        emulator.return_value.tap.assert_not_called()
                    self.assertEqual(explicit, target.params["region"])

    def test_explicit_region_survives_real_function_save_and_reload(self):
        self.block.params["library_entry_id"] = ""
        self.view._region_selection_block = self.block
        self.view._apply_search_region_selection({"x": 10, "y": 20, "width": 30, "height": 40})
        expected = deepcopy(self.block.params)
        function = BotFunction.create(name="OCR", game_id="test", author="test")
        function.blocks = [self.block]
        with tempfile.TemporaryDirectory() as root:
            service = FunctionService(
                root=root, library_service=Mock(), variable_service=Mock(),
                logger=Mock(), reinitialize_service=Mock(),
            )
            service.save(function)
            loaded = service.load("test", function.function_id)
            self.assertIsNotNone(loaded)
            self.assertEqual(expected, loaded.blocks[0].params)

    def overlay_for(self, block_type, **params):
        self.block.block_type = block_type
        self.block.params = params
        self.view._refresh_live_ocr_overlay()
        return self.view.live_image.overlay_region

    def test_find_and_tap_offset_moves_live_tap_marker(self):
        overlay = self.overlay_for("find_and_tap_element", library_entry_id="entry", offset_x=0, offset_y=0)
        self.assertEqual({"x": 10, "y": 193, "width": 238, "height": 421}, {k: overlay[k] for k in ("x", "y", "width", "height")})
        self.assertEqual((122, 281), (overlay["tap_x"], overlay["tap_y"]))
        overlay = self.overlay_for("find_and_tap_element", library_entry_id="entry", offset_x=30, offset_y=-5)
        self.assertEqual((152, 276), (overlay["tap_x"], overlay["tap_y"]))

    def test_runtime_match_tap_marker_follows_changed_offset(self):
        self.view._last_runtime_matches[self.block.block_id] = {"x": 100, "y": 100, "width": 40, "height": 20}
        overlay = self.overlay_for("tap_element", library_entry_id="entry", offset_x=15, offset_y=5)
        self.assertEqual((135, 115), (overlay["tap_x"], overlay["tap_y"]))

    def test_find_element_without_tap_shows_search_area_only(self):
        overlay = self.overlay_for("find_element", library_entry_id="entry")
        self.assertEqual(238, overlay["width"])
        self.assertNotIn("tap_x", overlay)

    def test_tap_point_shows_point_marker(self):
        overlay = self.overlay_for("tap_point", x=320, y=240)
        self.assertTrue(overlay["point_only"])
        self.assertEqual((320, 240), (overlay["tap_x"], overlay["tap_y"]))

    def test_swipe_shows_start_and_end(self):
        overlay = self.overlay_for("swipe", direction="up", start_x=0, start_y=0)
        expected = FunctionRunner._swipe_points("up", 0, 0, 800, 600)
        self.assertEqual(expected, (overlay["swipe_start_x"], overlay["swipe_start_y"], overlay["swipe_end_x"], overlay["swipe_end_y"]))

    def test_swipe_search_tap_shows_area_swipe_and_tap(self):
        overlay = self.overlay_for(
            "swipe_search_tap", library_entry_id="entry", direction="left",
            start_x=0, start_y=0, offset_x=10, offset_y=0,
        )
        self.assertEqual(238, overlay["width"])
        self.assertIn("swipe_end_x", overlay)
        self.assertEqual((132, 281), (overlay["tap_x"], overlay["tap_y"]))

    def test_coordinate_anchor_shows_rectangle(self):
        overlay = self.overlay_for(
            "coordinate_anchor", x=5, y=6, width=70, height=80, screen_width=800, screen_height=600,
        )
        self.assertEqual({"x": 5, "y": 6, "width": 70, "height": 80, "screen_width": 800, "screen_height": 600}, overlay)

    def test_point_overlay_paints_without_error(self):
        self.overlay_for("swipe", direction="down", start_x=100, start_y=100)
        self.view.live_image.resize(400, 300)
        self.view.live_image.grab()


if __name__ == "__main__":
    def timeout():
        print("TEST RESULT: TIMEOUT", flush=True)
        faulthandler.dump_traceback()
        os._exit(2)

    started = time.monotonic()
    print("TEST START: OCR region editor", flush=True)
    watchdog = Timer(30, timeout)
    watchdog.daemon = True
    watchdog.start()
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(OcrRegionEditorTests)
    )
    watchdog.cancel()
    print(f"TEST DURATION: {time.monotonic() - started:.3f}s", flush=True)
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE", flush=True)
    sys.exit(0 if result.wasSuccessful() else 1)
