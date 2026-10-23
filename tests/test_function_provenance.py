from contextlib import ExitStack
import faulthandler
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, get_ident
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QThreadPool, QTimer, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

from models.bot_function import BotFunction, FunctionPackageOrigin
from services.function_service import FunctionService
from ui.i18n.translator import set_language, t
from ui.views.function_editor_view import FunctionEditorView, _FunctionPublicationTask


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
                    self.assertIn(value, label.toolTip())
                self.assertEqual(t("function.package_origin_compact").format(revision=3), label.text())
                self.assertNotIn("\n", label.text())
                self.assertNotIn("function.package_origin_", label.toolTip())
                self.assertEqual(Qt.PlainText, label.textFormat())
        editor.current_function = BotFunction.create("Local", "game", "")
        FunctionEditorView._refresh_package_origin(editor)
        self.assertEqual("", label.text())
        self.assertEqual("", label.toolTip())
        self.assertTrue(label.isHidden())
        editor.current_function = None
        FunctionEditorView._refresh_package_origin(editor)
        self.assertEqual("", label.text())
        self.assertEqual("", label.toolTip())

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

    def test_sidebar_scrolls_controls_without_squeezing_screenshot_at_small_heights(self):
        with ExitStack() as patches:
            for name in (
                "GameService", "AppConfigService", "LibraryService", "VariableService",
                "FunctionService", "SchedulerFieldService", "ScreenService",
                "ProfileService", "RuntimeSupervisor", "GitHubAuthService",
            ):
                service = Mock()
                service.load.return_value = []
                service.load_all.return_value = []
                patches.enter_context(patch(f"ui.views.function_editor_view.{name}", return_value=service))
            patches.enter_context(patch(
                "ui.views.function_editor_view.ProfileEmulatorBar", side_effect=QWidget,
            ))
            for method in ("_sync_profile_emulator_bar", "new_function", "refresh_function_list"):
                patches.enter_context(patch.object(FunctionEditorView, method))
            view = FunctionEditorView()
        view.event_timer.stop()
        self.addCleanup(view.deleteLater)
        sidebar = view.sidebar_splitter.parentWidget()
        sidebar.setParent(None)
        self.addCleanup(sidebar.deleteLater)
        self.addCleanup(sidebar.close)
        view.current_function = self.function
        self.function.package_origins = [self.origin] * 20
        view._refresh_package_origin()
        view.function_name.setText(self.function.name)
        view.details.setPlainText("A description of the Function.")
        pixmap = QPixmap(420, 900)
        pixmap.fill(Qt.blue)
        view.live_image.set_screenshot(pixmap, 420)
        sidebar.show()
        for height in (600, 760, 900):
            with self.subTest(height=height):
                sidebar.resize(330, height)
                self.app.processEvents()
                self.assertLessEqual(sidebar.height(), height)
                self.assertEqual(330, sidebar.width())
                self.assertGreaterEqual(
                    view.live_scroll.viewport().height(),
                    view.fontMetrics().lineSpacing() * 9,
                )
                if height <= 760:
                    self.assertGreater(view.sidebar_controls_scroll.verticalScrollBar().maximum(), 0)
                view.sidebar_controls_scroll.ensureWidgetVisible(view.btn_delete_function)
                self.app.processEvents()
                controls_viewport = view.sidebar_controls_scroll.viewport()
                button_position = view.btn_delete_function.mapTo(controls_viewport, view.btn_delete_function.rect().center())
                self.assertTrue(controls_viewport.rect().contains(button_position))
                self.assertEqual(1, len(view.package_origin_label.text().splitlines()))
                self.assertLessEqual(
                    view.package_origin_label.height(),
                    view.package_origin_label.fontMetrics().lineSpacing() * 2,
                )
                self.assertGreater(view.live_scroll.verticalScrollBar().maximum(), 0)
        view.sidebar_splitter.setSizes([10000, 1])
        self.app.processEvents()
        self.assertGreaterEqual(
            view.live_scroll.viewport().height(), view.fontMetrics().lineSpacing() * 9,
        )
        view.current_function = None
        view._refresh_package_origin()
        self.assertTrue(view.package_origin_label.isHidden())
        self.assertEqual("", view.package_origin_label.toolTip())

    def publication_editor(self):
        view = FunctionEditorView.__new__(FunctionEditorView)
        QWidget.__init__(view)
        self.addCleanup(view.deleteLater)
        view.current_function = self.function
        self.function.saved = True
        view._publication_key = None
        view._publication_task = None
        view._publication = None
        view._save_task = None
        view.function_service = Mock(root=Path("isolated-functions"))
        view.game_service = Mock()
        view.library_service = Mock()
        view.variable_service = Mock()
        view.btn_publish_version = QPushButton(view)
        view.btn_publish_version.hide()
        view.publication_status = QLabel(view)
        view.logger = Mock()
        view._start_function_publication_check = Mock()
        return view

    def test_publish_button_requires_live_root_match_and_ignores_stale_results(self):
        view = self.publication_editor()
        view._refresh_function_publication()
        self.assertTrue(view.btn_publish_version.isHidden())
        view._start_function_publication_check.assert_called_once()
        old_task = SimpleNamespace(key=view._publication_key)
        view._publication_task = old_task
        self.function = BotFunction.create("Different", "game", "")
        self.function.saved = True
        view.current_function = self.function
        view._refresh_function_publication()
        view._function_publication_checked(old_task, {"source_id": "old"}, "")
        self.assertTrue(view.btn_publish_version.isHidden())
        self.assertEqual(2, view._start_function_publication_check.call_count)
        current_task = SimpleNamespace(key=view._publication_key)
        view._function_publication_checked(current_task, {"source_id": self.function.function_id}, "")
        self.assertFalse(view.btn_publish_version.isHidden())
        view._refresh_function_publication()
        self.assertEqual(2, view._start_function_publication_check.call_count)
        view.current_function = BotFunction.create("Draft", "game", "")
        view._refresh_function_publication()
        self.assertTrue(view.btn_publish_version.isHidden())
        self.assertIsNone(view._publication)

    def test_publication_check_failure_is_visible_and_does_not_offer_upload(self):
        view = self.publication_editor()
        view._refresh_function_publication()
        task = SimpleNamespace(key=view._publication_key)
        view._function_publication_checked(task, None, "GitHub HTTP 401")
        self.assertTrue(view.btn_publish_version.isHidden())
        self.assertIn("401", view.publication_status.text())
        self.assertFalse(view.publication_status.isHidden())
        view.logger.warning.assert_called_once()
        view._function_publication_checked(task, None, "")
        self.assertTrue(view.btn_publish_version.isHidden())

    def test_publish_action_saves_first_and_opens_only_the_current_function(self):
        view = self.publication_editor()
        view._publication = {"source_id": self.function.function_id}
        view.save_function = Mock()
        view._publish_function_version()
        continuation = view.save_function.call_args.kwargs["on_finished"]
        with (
            patch("ui.widgets.package_preparation_dialog.PackagePreparationDialog") as dialog,
            patch("services.package_service.PackageService"),
        ):
            continuation(False)
            dialog.assert_not_called()
            view._refresh_function_publication = Mock()
            continuation(True)
            self.assertEqual(self.function.function_id, dialog.call_args.kwargs["source_id"])
            self.assertEqual("game", dialog.call_args.kwargs["game_id"])
            self.assertTrue(dialog.call_args.kwargs["publish_after_pack"])
            dialog.return_value.exec.assert_called_once()
        view._publication = None
        view.save_function.reset_mock()
        view._publish_function_version()
        view.save_function.assert_not_called()

    def test_online_lookup_uses_worker_and_snapshot_while_gui_keeps_processing(self):
        entered, release = Event(), Event()
        self.addCleanup(release.set)
        gui_thread = get_ident()
        worker_threads = []

        def lookup(function, packages):
            worker_threads.append(get_ident())
            entered.set()
            if not release.wait(2):
                raise RuntimeError("fixture lookup timed out")
            return {"source_id": function.function_id}

        loop = QEventLoop()
        results = []
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: release.set() if entered.is_set() else None)
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)
        self.addCleanup(timer.stop)
        self.addCleanup(timeout.stop)
        with (
            patch("services.package_service.PackageService") as packages,
            patch("services.github_catalog_service.GitHubCatalogService") as catalog,
        ):
            catalog.return_value.function_publication.side_effect = lookup
            task = _FunctionPublicationTask(
                "key", self.function, Mock(), Mock(root=Path("isolated-functions")), Mock(), Mock(),
            )
            source_id = self.function.function_id
            self.function.function_id = "later-change"

            def finished(task, publication, error):
                results.append((publication, error, get_ident()))
                loop.quit()

            task.signals.finished.connect(finished)
            timer.start()
            timeout.start(3000)
            QThreadPool.globalInstance().start(task)
            loop.exec()
        self.assertEqual([({"source_id": source_id}, "", gui_thread)], results)
        self.assertNotEqual([gui_thread], worker_threads)
        self.assertTrue(release.is_set())
        packages.assert_called_once()


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE")
    raise SystemExit(0 if result.wasSuccessful() else 1)
