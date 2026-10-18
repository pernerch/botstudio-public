import faulthandler
import subprocess
import sys
import unittest
from unittest.mock import patch

from core.adb_service import AdbService
from services.emulator_connectivity_service import EmulatorConnectivityService


VALIDATED_NETWORK = (
    "NetworkAgentInfo{network{100} ni{WIFI CONNECTED extra: } "
    "nc{[ Transports: WIFI Capabilities: NOT_METERED&INTERNET&"
    "NOT_RESTRICTED&VALIDATED&NOT_ROAMING ]}}"
)


class FakeAdbService:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def shell_result(self, device_id, command, timeout=None):
        self.calls.append((device_id, command, timeout))
        return self.result


class EmulatorConnectivityServiceTests(unittest.TestCase):
    def make_service(self, result):
        adb = FakeAdbService(result)
        return EmulatorConnectivityService(adb), adb

    def test_android_validated_internet_network_succeeds(self):
        service, adb = self.make_service(
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=VALIDATED_NETWORK,
                stderr="",
            )
        )

        result = service.probe("emulator-5554")

        self.assertEqual("validated", result.status)
        self.assertTrue(result.probe_succeeded)
        self.assertEqual("emulator-5554", result.device_id)
        self.assertTrue(result.checked_at)
        self.assertEqual(
            ("emulator-5554", ["dumpsys", "connectivity"], 8),
            adb.calls[0],
        )

    def test_internet_network_without_validation_is_not_success(self):
        output = VALIDATED_NETWORK.replace("&VALIDATED", "")
        service, _ = self.make_service(
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=output,
                stderr="",
            )
        )

        result = service.probe("emulator-5554")

        self.assertEqual("unvalidated", result.status)
        self.assertFalse(result.probe_succeeded)

    def test_connected_network_without_internet_capability_is_reported(self):
        output = VALIDATED_NETWORK.replace("&INTERNET", "").replace("&VALIDATED", "")
        service, _ = self.make_service(
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=output,
                stderr="",
            )
        )

        result = service.probe("emulator-5554")

        self.assertEqual("no_internet", result.status)
        self.assertFalse(result.probe_succeeded)

    def test_disconnected_validated_network_is_not_success(self):
        output = VALIDATED_NETWORK.replace("CONNECTED", "DISCONNECTED")
        service, _ = self.make_service(
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=output,
                stderr="",
            )
        )

        result = service.probe("emulator-5554")

        self.assertEqual("no_network", result.status)
        self.assertFalse(result.probe_succeeded)

    def test_unrelated_validated_network_offer_is_not_treated_as_active(self):
        service, _ = self.make_service(
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="NetworkOffer [Capabilities: INTERNET&VALIDATED]",
                stderr="",
            )
        )

        result = service.probe("emulator-5554")

        self.assertEqual("unknown", result.status)
        self.assertIsNone(result.probe_succeeded)

    def test_adb_failure_is_reported_explicitly(self):
        service, _ = self.make_service(None)

        result = service.probe("emulator-5554")

        self.assertEqual("adb_error", result.status)
        self.assertIsNone(result.probe_succeeded)

    def test_adb_command_error_is_reported_explicitly(self):
        service, _ = self.make_service(
            subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr="error: device offline",
            )
        )

        result = service.probe("emulator-5554")

        self.assertEqual("adb_error", result.status)
        self.assertIsNone(result.probe_succeeded)

    def test_empty_device_id_does_not_call_adb(self):
        service, adb = self.make_service(None)

        result = service.probe(" ")

        self.assertEqual("invalid_device", result.status)
        self.assertEqual([], adb.calls)

    def test_timeout_must_be_finite_and_within_limit(self):
        service, adb = self.make_service(None)

        for timeout in (
            0,
            float("inf"),
            EmulatorConnectivityService.MAX_TIMEOUT_SECONDS + 1,
        ):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    service.probe("emulator-5554", timeout_seconds=timeout)

        self.assertEqual([], adb.calls)

    def test_adb_shell_result_preserves_remote_exit_status_and_stderr(self):
        adb = AdbService("adb.exe")
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=7,
            stdout="remote output",
            stderr="remote error",
        )

        with patch.object(adb, "_run", return_value=completed) as run:
            result = adb.shell_result(
                "emulator-5554",
                ["dumpsys", "connectivity"],
                timeout=5,
            )

        self.assertIs(completed, result)
        run.assert_called_once_with(
            [
                "adb.exe",
                "-s",
                "emulator-5554",
                "shell",
                "dumpsys",
                "connectivity",
            ],
            timeout=5,
        )


if __name__ == "__main__":
    faulthandler.dump_traceback_later(20, exit=True)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(
        EmulatorConnectivityServiceTests
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    faulthandler.cancel_dump_traceback_later()
    print(
        "TEST RESULT: SUCCESS" if result.wasSuccessful() else "TEST RESULT: FAILURE",
        flush=True,
    )
    sys.exit(0 if result.wasSuccessful() else 1)
