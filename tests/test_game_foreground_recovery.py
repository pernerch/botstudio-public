import faulthandler
import os
import time
import unittest
from subprocess import CompletedProcess
from threading import Timer
from unittest.mock import Mock

from core.adb_service import AdbService
from services.game_runtime_service import GameRuntimeService
from services.runtime_recovery_service import RuntimeRecoveryService


class GameForegroundRecoveryTests(unittest.TestCase):
    def setUp(self):
        started = time.monotonic()
        print(f"TEST START: {self.id()}", flush=True)
        watchdog = Timer(10, lambda: (
            print("TEST RESULT: TIMEOUT", flush=True),
            faulthandler.dump_traceback(), os._exit(124),
        ))
        watchdog.daemon = True
        watchdog.start()
        self.addCleanup(watchdog.cancel)
        self.addCleanup(lambda: print(
            f"TEST END: {self.id()} duration={time.monotonic() - started:.3f}s",
            flush=True,
        ))
        self.profile = {"id": "a", "game": "game", "emulator_instance": "1"}
        self.game = GameRuntimeService("adb", Mock(), logger=Mock())
        self.game.adb = Mock()
        self.game.adb.get_foreground_package.return_value = "test.game"
        self.game.adb.shell_success.return_value = True

    def restore(self):
        return self.game.restore_game(self.profile, "device", "test.game")

    def test_background_process_is_restored_without_init_or_force_stop(self):
        self.game.adb.get_package_pid.side_effect = [123, 123]
        result = self.restore()
        self.assertFalse(result["game_init_required"])
        self.assertEqual("Game foreground restored", result["message"])
        self.game.adb.shell_success.assert_called_once_with(
            "device", ["monkey", "-p", "test.game", "-c",
                       "android.intent.category.LAUNCHER", "1"],
        )
        self.game.logger.info.assert_any_call(
            "Game Foreground Restored", profile=None, game="game",
            emulator_instance="1", device_id="device", package_name="test.game",
            pid_before=123, pid_after=123, foreground_package="test.game",
            launch_accepted=True, game_init_required=False,
        )

    def test_missing_or_changed_pid_requires_init(self):
        for before, after in ((None, 123), (123, 456)):
            with self.subTest(before=before):
                self.game.adb.get_package_pid.side_effect = [before, after]
                result = self.restore()
                self.assertTrue(result["game_init_required"])
                self.assertIn("Init required", result["message"])
                self.game.adb.get_package_pid.assert_called_with(
                    "device", "test.game", strict=True,
                )
                self.assertEqual(
                    "Game Process Started" if before is None else "Game Process Changed",
                    self.game.logger.info.call_args.args[0],
                )

    def test_delayed_launch_does_not_claim_process_started(self):
        self.game.adb.get_package_pid.side_effect = [None, None]
        result = self.restore()
        self.assertTrue(result["game_init_required"])
        self.assertEqual("Game launch pending; Init required", result["message"])

    def test_same_pid_without_foreground_does_not_claim_restored(self):
        self.game.adb.get_package_pid.side_effect = [123, 123]
        self.game.adb.get_foreground_package.return_value = "android.launcher"
        result = self.restore()
        self.assertFalse(result["game_init_required"])
        self.assertEqual("Game foreground request pending", result["message"])

    def test_failed_launch_is_explicit_and_does_not_force_stop(self):
        self.game.adb.get_package_pid.side_effect = [123, 123]
        self.game.adb.shell_success.return_value = False
        result = self.restore()
        self.assertEqual("Game foreground request failed", result["message"])
        self.assertEqual("Recovering", result["state"])
        self.game.logger.error.assert_called()
        self.assertEqual(1, self.game.adb.shell_success.call_count)

    def test_failed_launch_with_changed_pid_still_requires_init(self):
        self.game.adb.get_package_pid.side_effect = [123, 456]
        self.game.adb.shell_success.return_value = False
        result = self.restore()
        self.assertTrue(result["game_init_required"])
        self.assertEqual("Game foreground request failed", result["message"])

    def test_failed_initial_pid_probe_does_not_launch(self):
        self.game.adb.get_package_pid.side_effect = RuntimeError("ADB unavailable")
        result = self.restore()
        self.assertEqual("Game process check failed", result["message"])
        self.game.adb.shell_success.assert_not_called()
        self.game.logger.error.assert_called()

    def test_failed_post_launch_probe_requires_init_without_claiming_restart(self):
        self.game.adb.get_package_pid.side_effect = [123, RuntimeError("ADB unavailable")]
        result = self.restore()
        self.assertTrue(result["game_init_required"])
        self.assertEqual("Game process unverified; Init required", result["message"])
        self.game.logger.error.assert_called()

    def test_recovery_propagates_game_init_requirement_without_emulator_restart(self):
        for connected in (True, False):
            with self.subTest(connected=connected):
                adb, manager = Mock(), Mock()
                adb.ensure_device.return_value = connected
                manager.is_instance_running.return_value = True
                recovery = RuntimeRecoveryService(adb, self.game, manager, logger=Mock())
                self.game.adb.get_package_pid.side_effect = [123, 456]
                result = recovery.recover_profile(self.profile, {
                    "device_id": "device", "game": {"package_name": "test.game"},
                })
                self.assertTrue(result["game_init_required"])
                manager.start_instance.assert_not_called()
                manager.stop_instance.assert_not_called()

    def test_strict_pid_probe_distinguishes_absence_from_adb_failure(self):
        adb = AdbService("adb")
        adb._run = Mock()
        for code, output, error, expected in (
            (0, "123\n", "", 123), (1, "", "", None),
        ):
            with self.subTest(code=code):
                adb._run.return_value = CompletedProcess([], code, output, error)
                self.assertEqual(expected, adb.get_package_pid("device", "test.game", strict=True))
        for response in (
            None,
            CompletedProcess([], 1, "", "error: device offline"),
            CompletedProcess([], 127, "", "pidof: not found"),
            CompletedProcess([], 0, "invalid", ""),
            CompletedProcess([], 0, "", ""),
        ):
            with self.subTest(response=response):
                adb._run.return_value = response
                with self.assertRaises(RuntimeError):
                    adb.get_package_pid("device", "test.game", strict=True)

    def test_existing_pid_callers_keep_optional_timeout_and_non_strict_behavior(self):
        adb = AdbService("adb")
        adb._run = Mock(return_value=None)
        self.assertIsNone(adb.get_package_pid("device", "test.game", timeout=2))
        self.assertEqual(2, adb._run.call_args.kwargs["timeout"])


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    print("TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE", flush=True)
    raise SystemExit(0 if result.wasSuccessful() else 1)
