from collections import deque
from pathlib import Path
import threading

from core.emulator_manager import EmulatorManager
from core.emulator import Emulator, cleanup_screenshots, remove_screenshot, trim_adb_log


# a profile keeps its newest captures; older ones are deleted so TEMP never
# fills up (77k leaked PNGs caused scanner locks -> PermissionError, DEC-162)
KEEP_PER_PROFILE = 3
CLEANUP_EVERY = 200
STALE_SECONDS = 600

_RETAINED = set()
_RETAINED_LOCK = threading.Lock()


def _sweep_stale():
    with _RETAINED_LOCK:
        keep = {str(path) for path in _RETAINED}
    cleanup_screenshots(STALE_SECONDS, keep=keep)
    trim_adb_log()


class ScreenService:

    def __init__(self):

        self._latest = {}
        self._history = {}
        self._captures = 0
        self._lock = threading.Lock()

    def capture(
        self,
        profile,
    ):

        if profile is None:

            raise ValueError(
                "Profile is None"
            )

        instance = profile[
            "emulator_instance"
        ]

        manager = EmulatorManager()

        device_id = manager.get_device_id(
            instance
        )

        emulator = Emulator(
            device_id=device_id
        )

        screenshot = emulator.screenshot()

        self._remember(profile["id"], screenshot)

        return screenshot

    def _remember(self, profile_id, screenshot):
        expired = []
        with self._lock:
            self._latest[profile_id] = screenshot
            history = self._history.setdefault(profile_id, deque())
            history.append(screenshot)
            while len(history) > KEEP_PER_PROFILE:
                expired.append(history.popleft())
            self._captures += 1
            sweep = self._captures % CLEANUP_EVERY == 1
        with _RETAINED_LOCK:
            _RETAINED.add(screenshot)
            _RETAINED.difference_update(expired)
        for path in expired:
            remove_screenshot(path)
        if sweep:
            threading.Thread(
                target=_sweep_stale,
                name="screenshot-cleanup",
                daemon=True,
            ).start()

    def get_latest(
        self,
        profile_id,
    ):

        return self._latest.get(
            profile_id
        )