from copy import deepcopy
import faulthandler
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, get_ident
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from models.bot_function import BotFunction
from models.github_account import GitHubAccount
from services.function_service import FunctionService
from services.github_auth_service import GitHubAuthError
from services.reinitialize_service import ReinitializeService, defer_notifications
from ui.views.function_editor_view import FunctionEditorView, _FunctionSaveTask


class FunctionAccountTests(unittest.TestCase):
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
        self.creator = GitHubAccount("123", "creator")
        self.modifier = GitHubAccount("456", "modifier")

    def test_new_function_first_save_assigns_creator_and_revision_account(self):
        function = BotFunction.create("New", "game", "")
        function.bump_revision("", "saved", account=self.creator)
        self.assertEqual(self.creator, function.creator_account)
        self.assertEqual("@creator", function.author)
        self.assertEqual(self.creator, function.history[0].account)
        self.assertEqual(self.creator, function.history[-1].account)
        self.assertEqual("@creator", function.history[-1].author)

    def test_foreign_edit_rename_and_anonymous_edit_never_replace_creator(self):
        function = BotFunction.create("New", "game", "")
        function.bump_revision("", account=self.creator)
        function.saved = True
        function.bump_revision("", account=self.modifier)
        self.assertEqual(self.creator, function.creator_account)
        self.assertEqual("@creator", function.author)
        self.assertEqual(self.modifier, function.history[-1].account)
        renamed = GitHubAccount("456", "renamed")
        function.bump_revision("", account=renamed)
        self.assertEqual("456", function.history[-1].account.account_id)
        self.assertEqual("@renamed", function.history[-1].author)
        function.bump_revision("")
        self.assertEqual("", function.history[-1].author)
        self.assertIsNone(function.history[-1].account)
        self.assertEqual(self.creator, function.creator_account)

    def test_legacy_and_previously_anonymous_functions_are_not_reassigned(self):
        for function in (
            BotFunction.create("Legacy draft", "game", "Chris"),
            BotFunction(function_id="legacy", name="Legacy", game_id="game", saved=True),
            BotFunction.from_dict(BotFunction.create("Imported draft", "game", "").to_dict()),
        ):
            with self.subTest(function=function.name):
                author = function.author
                function.bump_revision("", account=self.modifier)
                self.assertIsNone(function.creator_account)
                self.assertEqual(author, function.author)
                self.assertEqual(self.modifier, function.history[-1].account)
        anonymous = BotFunction.create("Anonymous", "game", "")
        anonymous.bump_revision("")
        anonymous.saved = True
        anonymous.bump_revision("", account=self.creator)
        self.assertIsNone(anonymous.creator_account)

    def test_copy_preserves_creator_not_current_modifier(self):
        function = BotFunction.create("New", "game", "")
        function.bump_revision("", account=self.creator)
        copied = deepcopy(function)
        copied.function_id = "copy"
        copied.saved = False
        copied.revision = 1
        copied.history = []
        copied.bump_revision("", account=self.modifier)
        self.assertEqual(self.creator, copied.creator_account)
        self.assertEqual("@creator", copied.author)
        self.assertEqual(self.modifier, copied.history[-1].account)

    def test_model_rejects_fake_verification_and_invalid_account_shapes(self):
        data = BotFunction.create("New", "game", "").to_dict()
        invalid = [
            dict(self.creator.to_dict(), verified=True), {"login": "Chris"},
            dict(self.creator.to_dict(), account_id="Chris"),
            dict(self.creator.to_dict(), login="@creator"),
            dict(self.creator.to_dict(), provider="discord"),
        ]
        for account in invalid:
            with self.subTest(account=account):
                with self.assertRaises(ValueError):
                    BotFunction.from_dict(dict(data, creator_account=account))
                revision = dict(data["history"][0], account=account)
                with self.assertRaises(ValueError):
                    BotFunction.from_dict(dict(data, history=[revision]))
        legacy = dict(data)
        del legacy["creator_account"]
        del legacy["history"][0]["account"]
        self.assertIsNone(BotFunction.from_dict(legacy).creator_account)

    def test_worker_save_reload_preserves_accounts_and_queries_each_save(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        service = FunctionService(
            root=str(Path(temporary.name) / "functions"),
            library_service=Mock(), variable_service=Mock(),
            logger=Mock(), reinitialize_service=ReinitializeService(),
        )
        notified = Mock()
        service.reinitialize_service.subscribe(notified)
        auth = Mock()
        auth.restore.side_effect = [self.creator, self.modifier, None]
        function = BotFunction.create("New", "game", "")
        for expected in (self.creator, self.modifier, None):
            worker = _FunctionSaveTask(service, auth, deepcopy(function))
            results = []
            worker.signals.finished.connect(lambda result, error: results.append((result, error)))
            worker.run()
            self.assertEqual("", results[0][1])
            function = service.load("game", function.function_id)
            self.assertIsNotNone(function)
            self.assertEqual(expected, function.history[-1].account)
            self.assertEqual(self.creator, function.creator_account)
        self.assertEqual(3, auth.restore.call_count)
        notified.assert_not_called()
        self.assertEqual("function", worker.notifications[0][1])

    def test_auth_failure_does_not_fall_back_to_local_name_or_write_revision(self):
        for error in (GitHubAuthError("GitHub HTTP 401"), OSError("secret"),
                      ValueError("credential error")):
            with self.subTest(error=error):
                function = BotFunction.create("New", "game", "")
                before = function.to_dict()
                service = Mock()
                worker = _FunctionSaveTask(
                    service, Mock(restore=Mock(side_effect=error)), function,
                )
                results = []
                worker.signals.finished.connect(lambda value, message: results.append((value, message)))
                worker.run()
                self.assertIsNone(results[0][0])
                self.assertTrue(results[0][1])
                self.assertNotIn("secret", results[0][1])
                service.save.assert_not_called()
                self.assertEqual(before, function.to_dict())

    def test_editor_continuation_waits_for_live_account_without_blocking_events(self):
        entered, released = Event(), Event()
        self.addCleanup(released.set)

        def restore():
            entered.set()
            if not released.wait(2):
                raise GitHubAuthError("fixture timed out")
            return self.creator

        view = FunctionEditorView.__new__(FunctionEditorView)
        QWidget.__init__(view)
        self.addCleanup(view.deleteLater)
        view.current_function = BotFunction.create("New", "game", "")
        view.current_function.description = "Description"
        view.selected_block = None
        view._save_task = None
        view._save_callback = None
        view.github_auth = Mock(restore=Mock(side_effect=restore))
        view.function_service = Mock()
        view.function_service.reinitialize_service = ReinitializeService()
        notification_listener = Mock()
        notification_threads = []
        notification_listener.side_effect = lambda *_: notification_threads.append(get_ident())
        view.function_service.reinitialize_service.subscribe(notification_listener)
        def save_with_notification(function):
            view.function_service.reinitialize_service.publish(
                "function", function_id=function.function_id,
            )
            notification_listener.assert_not_called()
        view.function_service.save.side_effect = save_with_notification
        view.is_editable = Mock(return_value=True)
        view.function_name = Mock(text=Mock(return_value="New"))
        view.details = Mock(toPlainText=Mock(return_value="Description"))
        view.game_combo = Mock(currentData=Mock(return_value="game"))
        view.kind_combo = Mock(currentData=Mock(return_value="regular"))
        view.block_error = QLabel()
        self.addCleanup(view.block_error.deleteLater)
        view.canvas = Mock()
        view.logger = Mock()
        view._update_required_field_styles = Mock()
        view.refresh_function_list = Mock()
        view._refresh_details = Mock()
        callback = Mock()
        original = view.current_function
        loop = QEventLoop()
        ticks = []
        timer = QTimer()
        timer.setInterval(10)

        def tick():
            ticks.append(True)
            if entered.is_set():
                self.assertEqual(1, original.revision)
                if not released.is_set():
                    callback.assert_not_called()
                    released.set()
            if view._save_task is None:
                loop.quit()

        timer.timeout.connect(tick)
        timer.start()
        self.addCleanup(timer.stop)
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)
        timeout.start(3000)
        self.addCleanup(timeout.stop)
        self.assertTrue(view.save_function(on_finished=callback))
        self.assertFalse(view.isEnabled())
        loop.exec()
        self.assertTrue(ticks)
        self.assertIsNone(view._save_task)
        self.assertTrue(view.isEnabled())
        callback.assert_called_once_with(True)
        self.assertEqual(self.creator, view.current_function.creator_account)
        self.assertEqual(1, original.revision)
        view.function_service.save.assert_called_once()
        notification_listener.assert_called_once_with(
            "function", {"function_id": view.current_function.function_id},
        )
        self.assertEqual([get_ident()], notification_threads)

    def test_copy_completion_keeps_original_creator_or_restores_original_on_failure(self):
        for success in (True, False):
            with self.subTest(success=success):
                editor = Mock()
                editor._save_task = None
                editor.function_name.text.return_value = "Unsaved name"
                editor.details.toPlainText.return_value = "Unsaved description"
                original = BotFunction.create("Original", "game", "")
                original.bump_revision("", account=self.creator)
                original.saved = True
                editor.current_function = original
                editor.selected_block = None
                editor.is_editable.return_value = True
                editor.save_function.return_value = True
                with patch("ui.views.function_editor_view.QInputDialog.getText",
                           return_value=("Copy", True)):
                    self.assertTrue(FunctionEditorView.save_copy_as(editor))
                copy = editor.current_function
                editor.save_function.call_args.kwargs["on_finished"](success)
                self.assertIs(copy if success else original, editor.current_function)
                self.assertIs(editor.current_function, editor.canvas.current_function)
                self.assertEqual(self.creator, editor.current_function.creator_account)
                self.assertEqual("Original", original.name)
                if not success:
                    editor.function_name.setText.assert_called_with("Unsaved name")
                    editor.details.setPlainText.assert_called_with("Unsaved description")

    def test_debug_starts_only_after_successful_save(self):
        editor = Mock()
        editor.current_function = BotFunction.create("New", "game", "")
        editor.profile_combo.currentData.return_value = "profile"
        editor.runtime_service = None
        editor._debug_values_for_run.return_value = {"Cooldown": 0}
        FunctionEditorView._start_debug_run(editor, "block")
        editor.supervisor.start_function.assert_not_called()
        continuation = editor.save_function.call_args.kwargs["on_finished"]
        continuation(False)
        editor._debug_run_after_save.assert_called_once_with(False, "profile", "block")
        FunctionEditorView._debug_run_after_save(editor, False, "profile", "block")
        editor.supervisor.start_function.assert_not_called()
        FunctionEditorView._debug_run_after_save(editor, True, "profile", "block")
        editor.supervisor.start_function.assert_called_once_with(
            "profile", editor.current_function.function_id, "block",
            allow_disabled=True, debug_values={"Cooldown": 0},
        )

    def test_editor_failed_save_keeps_original_and_notifies_failure_continuation(self):
        view = Mock()
        original = BotFunction.create("New", "game", "")
        view.current_function = original
        callback = Mock()
        view._save_callback = callback
        view._save_task.notifications = []
        with patch("ui.views.function_editor_view.QMessageBox.critical"):
            FunctionEditorView._function_save_finished(view, None, "GitHub HTTP 401")
        self.assertIs(original, view.current_function)
        self.assertEqual(1, original.revision)
        callback.assert_called_once_with(False)
        view.refresh_function_list.assert_not_called()

    def test_creator_and_latest_modifier_display_as_stored_attribution(self):
        function = BotFunction.create("New", "game", "")
        function.bump_revision("", account=self.creator)
        function.bump_revision("", account=self.modifier)
        label = QLabel()
        self.addCleanup(label.deleteLater)
        editor = SimpleNamespace(current_function=function, package_origin_label=label)
        FunctionEditorView._refresh_package_origin(editor)
        self.assertIn("@creator", label.text())
        self.assertIn("@modifier", label.text())
        self.assertIn("123", label.text())
        self.assertIn("456", label.text())

    def test_deferred_dependency_notifications_replay_and_default_stays_synchronous(self):
        service = ReinitializeService()
        listener = Mock()
        service.subscribe(listener)
        with defer_notifications() as notifications:
            service.publish("variable", game_id="game")
            service.publish("library", entry_id="entry")
            listener.assert_not_called()
        for notifier, source, payload in notifications:
            notifier.publish(source, **payload)
        self.assertEqual(2, listener.call_count)
        service.publish("function", function_id="function")
        self.assertEqual(3, listener.call_count)


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE")
    raise SystemExit(0 if result.wasSuccessful() else 1)
