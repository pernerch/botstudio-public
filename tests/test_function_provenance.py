import faulthandler
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel

from models.bot_function import BotFunction, FunctionPackageOrigin
from services.function_service import FunctionService
from ui.i18n.translator import set_language, t
from ui.views.function_editor_view import FunctionEditorView


class FunctionProvenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        started = time.monotonic()
        print(f"TEST START: {self.id()}")
        faulthandler.dump_traceback_later(15, exit=True)
        self.addCleanup(faulthandler.cancel_dump_traceback_later)
        self.addCleanup(lambda: print(
            f"TEST END: {self.id()} duration {time.monotonic() - started:.3f}s",
        ))
        self.origin = FunctionPackageOrigin(
            "github:123", "example", "1.0.0", "game", "function", 3, "a" * 64,
        )
        self.function = BotFunction(
            function_id="function", name="Example", game_id="game",
            revision=3, author="Legacy author", package_origins=[self.origin],
        )

    def test_real_save_reload_and_revision_bump_preserve_source(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        service = FunctionService(
            root=str(root / "functions"), library_service=Mock(),
            variable_service=Mock(), logger=Mock(), reinitialize_service=Mock(),
        )
        self.function.bump_revision("Local modifier", "Fixed")
        service.save(self.function)
        restored = service.load("game", "function")
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(4, restored.revision)
        self.assertEqual("Legacy author", restored.author)
        self.assertEqual("Local modifier", restored.history[-1].author)
        self.assertEqual([self.origin], restored.package_origins)
        self.assertEqual(3, restored.package_origins[0].revision)
        on_disk = json.loads(service.function_file("game", "function").read_text(encoding="utf-8"))
        self.assertEqual([self.origin.to_dict()], on_disk["package_origins"])

    def test_editor_displays_plain_read_only_unverified_lineage_and_clears_it(self):
        from ui.i18n import translator
        self.addCleanup(set_language, translator.LANG)
        label = QLabel()
        label.setTextFormat(Qt.PlainText)
        self.addCleanup(label.deleteLater)
        editor = SimpleNamespace(
            current_function=self.function,
            package_origin_label=label,
        )
        for language in ("en", "de"):
            with self.subTest(language=language):
                set_language(language)
                FunctionEditorView._refresh_package_origin(editor)
                self.assertFalse(label.isHidden())
                for value in (t("function.package_origin_unverified"), "github:123",
                              "example 1.0.0", "game / function", "3", "a" * 64):
                    self.assertIn(value, label.text())
                self.assertNotIn("function.package_origin_", label.text())
                self.assertEqual(Qt.PlainText, label.textFormat())
        editor.current_function = BotFunction.create("Local", "game", "")
        FunctionEditorView._refresh_package_origin(editor)
        self.assertEqual("", label.text())
        self.assertTrue(label.isHidden())
        editor.current_function = None
        FunctionEditorView._refresh_package_origin(editor)
        self.assertEqual("", label.text())

    def test_editor_save_copy_keeps_origin_but_has_independent_identity(self):
        editor = Mock()
        editor._save_task = None
        editor.current_function = self.function
        editor.selected_block = None
        editor.is_editable.return_value = True
        editor.save_function.return_value = True
        with patch("ui.views.function_editor_view.QInputDialog.getText",
                   return_value=("Bugfix copy", True)):
            self.assertTrue(FunctionEditorView.save_copy_as(editor))
        copied = editor.current_function
        self.assertNotEqual(self.function.function_id, copied.function_id)
        self.assertEqual([self.origin], copied.package_origins)
        self.assertEqual("function", self.function.function_id)
        self.assertEqual("Legacy author", copied.author)


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE")
    raise SystemExit(0 if result.wasSuccessful() else 1)
