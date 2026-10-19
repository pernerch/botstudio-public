import faulthandler
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Event, get_ident
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog

from core.adb_service import AdbService
from core.emulator_manager import EmulatorManager
from services.adb_runtime_service import AdbRuntimeService
from services.emulator_setup_service import EmulatorSetupError, EmulatorSetupService
from services.game_runtime_service import GameRuntimeService
from services.game_service import GameService
from ui.i18n.translator import t
from ui.views.game_setup_view import GameSetupView
from ui.views.runtime_overview_view import RuntimeOverviewView
from ui.widgets.emulator_setup_dialog import EmulatorSetupDialog


class EmulatorSetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.started = time.monotonic()
        print(f"TEST START: {self.id()}")
        faulthandler.dump_traceback_later(15, exit=True)
        self.addCleanup(faulthandler.cancel_dump_traceback_later)
        self.addCleanup(lambda: print(
            f"TEST END: {self.id()} duration {time.monotonic() - self.started:.3f}s",
        ))
        temporary = TemporaryDirectory(prefix="botstudio-adb-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = self.root / "emulator.json"
        self.write_config(adb_path="", mumu_manager_path="")
        self.manager = EmulatorManager(str(self.config))
        self.log_patch = patch("services.emulator_setup_service.get_logger", return_value=Mock())
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)
        self.service = EmulatorSetupService(self.manager)
        version_patch = patch(
            "services.emulator_setup_service.run_command",
            return_value=subprocess.CompletedProcess(
                [], 0, "Android Debug Bridge version 1.0.41\n", "",
            ),
        )
        self.version = version_patch.start()
        self.addCleanup(version_patch.stop)

    def write_config(self, **values):
        self.config.write_text(json.dumps({
            "emulator_type": "mumu",
            "instance_pids": {"2": 123},
            "custom": "preserve",
            **values,
        }), encoding="utf-8")

    def tool(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"mock tool - never execute")
        return path

    def install(self):
        manager = self.tool("MuMuPlayer/nx_main/MuMuManager.exe")
        adb = self.tool("MuMuPlayer/tools/adb/bin/adb.exe")
        return manager, adb

    def assert_error(self, code, installation=""):
        before = self.config.read_bytes()
        with self.assertRaises(EmulatorSetupError) as caught:
            self.service.ensure_tools(installation)
        self.assertEqual(code, caught.exception.code)
        self.assertEqual(before, self.config.read_bytes())

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
        self.assertTrue(predicate(), "Qt operation did not finish within 3 seconds")

    def test_fresh_setup_selects_installation_and_finds_nested_adb(self):
        manager, adb = self.install()
        self.service.ensure_tools(str(manager.parent.parent))
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(str(adb), config["adb_path"])
        self.assertEqual(str(manager), config["mumu_manager_path"])
        self.assertEqual({"2": 123}, config["instance_pids"])
        self.assertEqual("preserve", config["custom"])
        self.version.assert_called_once()
        self.assertEqual([str(adb), "version"], self.version.call_args.args[0])
        self.assertEqual(5, self.version.call_args.kwargs["timeout"])

    def test_configured_manager_searches_parent_installation_not_only_nx_main(self):
        manager, adb = self.install()
        self.manager.set_mumu_manager_path(str(manager))
        self.service.ensure_tools()
        self.assertEqual(str(adb), self.manager.adb_path)

    def test_selecting_nx_main_also_searches_installation_sibling_folders(self):
        manager, adb = self.install()
        self.service.ensure_tools(str(manager.parent))
        self.assertEqual(str(adb), self.manager.adb_path)

    def test_stale_adb_path_is_replaced_by_nested_installation_tool(self):
        manager, adb = self.install()
        self.write_config(
            mumu_manager_path=str(manager), adb_path=str(self.root / "removed.exe"),
        )
        self.service.ensure_tools()
        self.assertEqual(str(adb), self.manager.adb_path)

    def test_manager_adjacent_adb_is_preferred_over_versioned_device_tools(self):
        manager, _ = self.install()
        preferred = self.tool("MuMuPlayer/nx_main/adb.exe")
        self.tool("MuMuPlayer/nx_device/12.0/shell/adb.exe")
        self.tool("MuMuPlayer/nx_device/15.0/shell/adb.exe")
        self.service.ensure_tools(str(manager.parent.parent))
        self.assertEqual(str(preferred), self.manager.adb_path)

    def test_broken_manager_adjacent_adb_falls_back_to_nested_tool(self):
        manager, adb = self.install()
        self.tool("MuMuPlayer/nx_main/adb.exe")
        self.version.side_effect = [
            OSError("missing DLL"), self.version.return_value,
        ]
        self.service.ensure_tools(str(manager.parent.parent))
        self.assertEqual(str(adb), self.manager.adb_path)

    def test_valid_external_adb_is_retained_without_recursive_search(self):
        manager, _ = self.install()
        external = self.tool("external/adb.exe")
        self.write_config(mumu_manager_path=str(manager), adb_path=str(external))
        with patch.object(self.service, "_find_tools") as search:
            self.service.ensure_tools()
        search.assert_not_called()
        self.assertEqual(str(external), self.manager.adb_path)

    def test_invalid_existing_adb_falls_back_to_installation_search(self):
        manager, adb = self.install()
        external = self.tool("old/adb.exe")
        self.write_config(mumu_manager_path=str(manager), adb_path=str(external))
        self.version.side_effect = [
            OSError("not executable"), self.version.return_value,
        ]
        self.service.ensure_tools()
        self.assertEqual(str(adb), self.manager.adb_path)

    def test_selecting_different_installation_replaces_both_paths(self):
        old_manager = self.tool("old/nx_main/MuMuManager.exe")
        old_adb = self.tool("old/adb/adb.exe")
        self.write_config(mumu_manager_path=str(old_manager), adb_path=str(old_adb))
        manager, adb = self.install()
        self.service.ensure_tools(str(manager.parent.parent))
        self.assertEqual(str(adb), self.manager.adb_path)
        self.assertEqual(str(manager), self.manager.mumu_manager_path)

    def test_missing_configuration_requests_folder_without_running_adb(self):
        self.assert_error("select_folder")
        self.version.assert_not_called()

    def test_missing_adb_is_explicit_and_leaves_configuration_unchanged(self):
        manager = self.tool("MuMuPlayer/nx_main/MuMuManager.exe")
        self.assert_error("adb_missing", str(manager.parent.parent))

    def test_missing_manager_and_ambiguous_installations_fail_explicitly(self):
        self.tool("MuMuPlayer/tools/adb.exe")
        self.assert_error("manager_missing", str(self.root / "MuMuPlayer"))
        self.install()
        self.tool("MuMuPlayer/another/MuMuManager.exe")
        self.assert_error("manager_ambiguous", str(self.root / "MuMuPlayer"))

    def test_invalid_adb_or_missing_dll_does_not_persist_success(self):
        manager, _ = self.install()
        for failure in (
            OSError("AdbWinApi.dll missing"),
            subprocess.TimeoutExpired("adb version", 5),
            subprocess.CompletedProcess([], 1, "", "load failed"),
            subprocess.CompletedProcess([], 0, "unrelated executable", ""),
        ):
            with self.subTest(failure=failure):
                self.version.side_effect = (
                    failure if isinstance(failure, Exception) else None
                )
                if not isinstance(failure, Exception):
                    self.version.return_value = failure
                self.assert_error("adb_invalid", str(manager.parent.parent))

    def test_search_budget_fails_explicitly_without_saving_paths(self):
        manager, _ = self.install()
        with patch.object(self.service, "MAX_ENTRIES", 1):
            self.assert_error("search_limit", str(manager.parent.parent))
        with patch.object(self.service, "SEARCH_SECONDS", 0):
            self.assert_error("search_limit", str(manager.parent.parent))

    def test_existing_clients_and_managers_use_new_paths_without_restart(self):
        other_manager = EmulatorManager(str(self.config))
        adb = other_manager.adb_service
        runtime = AdbRuntimeService(lambda: other_manager.adb_path, logger=Mock())
        game = GameRuntimeService(lambda: other_manager.adb_path, Mock(), logger=Mock())
        manager, expected = self.install()
        self.service.ensure_tools(str(manager.parent.parent))
        self.assertEqual(expected, adb.adb_path)
        self.assertEqual(expected, runtime.adb.adb_path)
        self.assertEqual(expected, game.adb.adb_path)
        self.assertEqual(str(manager), other_manager.mumu_manager_path)
        other_manager._set_instance_pid(3, 456)
        self.assertEqual(str(expected), self.manager.adb_path)
        self.assertEqual({"2": 123, "3": 456}, self.manager.instance_pids)

    def make_game_service(self):
        game = object.__new__(GameService)
        game.emulator_manager = EmulatorManager(str(self.config))
        game.adb_service = AdbService(lambda: game.emulator_manager.adb_path)
        game.load = Mock(return_value=[])
        return game

    @staticmethod
    def adb_response(command, **kwargs):
        if command[-1] == "devices":
            return subprocess.CompletedProcess(command, 0, "List of devices attached\n", "")
        if command[1] == "connect":
            return subprocess.CompletedProcess(command, 0, f"connected to {command[2]}", "")
        return subprocess.CompletedProcess(
            command, 0, "mCurrentFocus=Window{abc u0 com.example.game/.Main}", "",
        )

    def test_game_setup_detects_package_after_automatic_adb_setup_on_worker(self):
        manager, adb = self.install()
        self.manager.set_mumu_manager_path(str(manager))
        game = self.make_game_service()
        threads = []
        def response(command, **kwargs):
            threads.append(get_ident())
            return self.adb_response(command, **kwargs)
        with patch("ui.views.game_setup_view.GameService", return_value=game):
            view = GameSetupView()
        self.addCleanup(view.deleteLater)
        view.emulator_instance.setValue(2)
        with patch("core.adb_service.run_command", side_effect=response) as run:
            view.detect_package()
        self.assertEqual("com.example.game", view.package_name.text())
        self.assertEqual(t("game_setup.package_detected"), view.status.text())
        self.assertTrue(all(thread != get_ident() for thread in threads))
        self.assertTrue(all(call.args[0][0] == str(adb) for call in run.call_args_list))
        self.assertEqual(
            [str(adb), "connect", "127.0.0.1:16448"], run.call_args_list[1].args[0],
        )

    def test_game_package_connection_and_shell_errors_are_not_success(self):
        game = self.make_game_service()
        with patch.object(game.adb_service, "ensure_connection", return_value=False):
            with self.assertRaises(ConnectionError):
                game.detect_package(0)
        with (
            patch.object(game.adb_service, "ensure_connection", return_value=True),
            patch.object(game.adb_service, "shell_result", return_value=None),
        ):
            with self.assertRaises(ConnectionError):
                game.detect_package(0)

    def test_dialog_is_responsive_and_cannot_close_while_worker_uses_it(self):
        manager, _ = self.install()
        self.manager.set_mumu_manager_path(str(manager))
        release = Event()
        entered = Event()
        ticks = []
        version_result = self.version.return_value
        def slow_version(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            return version_result
        self.version.side_effect = slow_version
        dialog = EmulatorSetupDialog(self.manager)
        self.addCleanup(dialog.deleteLater)
        self.wait_for(entered.is_set)
        self.assertIsNotNone(dialog._worker)
        dialog.reject()
        self.assertIsNotNone(dialog._worker)
        def heartbeat():
            ticks.append(True)
            release.set()
        QTimer.singleShot(0, heartbeat)
        self.wait_for(lambda: dialog.result() == QDialog.DialogCode.Accepted)
        self.assertTrue(ticks)
        self.assertIsNone(dialog._worker)

    def test_dialog_missing_folder_is_actionable_and_cancel_preserves_config(self):
        before = self.config.read_bytes()
        dialog = EmulatorSetupDialog(self.manager)
        self.addCleanup(dialog.deleteLater)
        self.wait_for(lambda: t("emulator_setup.select_folder") in dialog.status.text())
        self.assertTrue(dialog.choose_button.isEnabled())
        with patch(
            "ui.widgets.emulator_setup_dialog.QFileDialog.getExistingDirectory",
            return_value="",
        ):
            dialog._choose_folder()
        dialog.reject()
        self.assertEqual(before, self.config.read_bytes())
        self.version.assert_not_called()

    def test_dialog_folder_selection_retries_setup_and_detects_game(self):
        manager, adb = self.install()
        operation = Mock(return_value="com.example.game")
        dialog = EmulatorSetupDialog(self.manager, operation=operation)
        self.addCleanup(dialog.deleteLater)
        self.wait_for(lambda: t("emulator_setup.select_folder") in dialog.status.text())
        operation.assert_not_called()
        with patch(
            "ui.widgets.emulator_setup_dialog.QFileDialog.getExistingDirectory",
            return_value=str(manager.parent.parent),
        ):
            dialog._choose_folder()
        self.wait_for(lambda: dialog.result() == QDialog.DialogCode.Accepted)
        self.assertEqual("com.example.game", dialog.package_name)
        self.assertEqual(str(adb), self.manager.adb_path)
        operation.assert_called_once()

    def test_missing_foreground_package_remains_visible_without_success(self):
        manager, _ = self.install()
        self.manager.set_mumu_manager_path(str(manager))
        dialog = EmulatorSetupDialog(self.manager, operation=lambda: None)
        self.addCleanup(dialog.deleteLater)
        self.wait_for(lambda: t("emulator_setup.package_missing") in dialog.status.text())
        self.assertEqual(QDialog.DialogCode.Rejected, dialog.result())
        self.assertTrue(dialog.retry_button.isEnabled())

    def test_runtime_uses_shared_setup_and_respects_cancellation(self):
        view = Mock()
        view.runtime_service.emulator_manager.emulator_type = "mumu"
        dialog = Mock()
        with patch(
            "ui.views.runtime_overview_view.EmulatorSetupDialog", return_value=dialog,
        ) as constructor:
            for code in (QDialog.DialogCode.Rejected, QDialog.DialogCode.Accepted):
                dialog.exec.return_value = code
                self.assertEqual(
                    code == QDialog.DialogCode.Accepted,
                    RuntimeOverviewView._ensure_emulator_manager_path(view),
                )
            constructor.assert_called_with(view.runtime_service.emulator_manager, view)


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE")
    raise SystemExit(0 if result.wasSuccessful() else 1)
