import faulthandler
import sys
import time
import unittest
from threading import Event, Thread, Timer
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from services import vision_cpu_budget
from services.ocr_service import OcrService
from services.opencv_service import OpenCVService


class VisionCpuBudgetTests(unittest.TestCase):
    def setUp(self):
        print(f"TEST START: {self.id()}", flush=True)
        watchdog = Timer(10, self._timeout)
        watchdog.daemon = True
        watchdog.start()
        self.addCleanup(watchdog.cancel)
        self.addCleanup(lambda: print(f"TEST END: {self.id()}", flush=True))

    @staticmethod
    def _timeout():
        print("TEST RESULT: TIMEOUT", flush=True)
        faulthandler.dump_traceback()
        import os
        os._exit(1)

    def test_both_services_configure_opencv_once_per_process(self):
        with (
            patch.object(vision_cpu_budget, "_configured", False),
            patch.object(vision_cpu_budget.cv2, "setNumThreads") as configure,
        ):
            OpenCVService()
            OcrService()
            OpenCVService()
            configure.assert_called_once_with(1)

    def test_ocr_initialization_is_singleton_under_concurrent_emulator_start(self):
        engine = object()
        start = Event()
        results = []
        errors = []

        def initialize(**kwargs):
            time.sleep(0.03)
            return engine

        def worker():
            try:
                if not start.wait(1):
                    raise TimeoutError("start signal missing")
                results.append(OcrService._engine_instance())
            except Exception as error:
                errors.append(error)

        factory = Mock(side_effect=initialize)
        with (
            patch.object(OcrService, "_engine", None),
            patch.dict(sys.modules, {"rapidocr": SimpleNamespace(RapidOCR=factory)}),
        ):
            workers = [Thread(target=worker, daemon=True) for _ in range(8)]
            for thread in workers:
                thread.start()
            start.set()
            for thread in workers:
                thread.join(2)
            self.assertFalse(any(thread.is_alive() for thread in workers))
            self.assertEqual([], errors)
            self.assertEqual([engine] * 8, results)
            factory.assert_called_once_with(params={
                "EngineConfig.onnxruntime.intra_op_num_threads": 1,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            })

    def test_initialization_error_propagates_and_allows_retry(self):
        engine = object()
        factory = Mock(side_effect=[RuntimeError("model unavailable"), engine])
        with (
            patch.object(OcrService, "_engine", None),
            patch.dict(sys.modules, {"rapidocr": SimpleNamespace(RapidOCR=factory)}),
        ):
            with self.assertRaisesRegex(RuntimeError, "model unavailable"):
                OcrService._engine_instance()
            self.assertIsNone(OcrService._engine)
            self.assertIs(engine, OcrService._engine_instance())
            self.assertEqual(2, factory.call_count)

    def test_large_search_crop_is_not_recognized_twice(self):
        image = np.zeros((100, 200, 3), dtype=np.uint8)
        result = SimpleNamespace(
            txts=["Theseus"],
            boxes=[[[5, 10], [55, 10], [55, 30], [5, 30]]],
        )
        engine = Mock(return_value=result)
        service = OcrService()
        with (
            patch("services.ocr_service.cv2.imread", return_value=image),
            patch.object(service, "_engine_instance", return_value=engine),
        ):
            found = service.find_text("synthetic.png", "Theseus")
        engine.assert_called_once_with(image, use_det=True, use_cls=True)
        self.assertEqual(
            {"x": 5, "y": 10, "width": 50, "height": 20,
             "text": "Theseus", "score": 1.0},
            found,
        )

    def test_small_search_keeps_upscaled_pass_and_absolute_geometry(self):
        image = np.zeros((100, 200, 3), dtype=np.uint8)
        # Raw crop misses; the enlarged variant finds the text.
        engine = Mock(side_effect=[
            SimpleNamespace(txts=[], boxes=[]),
            SimpleNamespace(
                txts=["Theseus"],
                boxes=[[[6, 12], [96, 12], [96, 48], [6, 48]]],
            ),
        ])
        service = OcrService()
        with (
            patch("services.ocr_service.cv2.imread", return_value=image),
            patch.object(service, "_engine_instance", return_value=engine),
        ):
            found = service.find_text(
                "synthetic.png", "Theseus",
                {"x": 20, "y": 30, "width": 100, "height": 20},
            )
        self.assertEqual(2, engine.call_count)
        self.assertEqual(
            [(20, 100, 3), (60, 300, 3)],
            [call.args[0].shape for call in engine.call_args_list],
        )
        self.assertEqual(
            {"x": 22, "y": 34, "width": 30, "height": 12,
             "text": "Theseus", "score": 1.0},
            found,
        )
