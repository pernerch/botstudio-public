"""Global debug pacing: slows the runner and allows pause/resume.

Active only while Debug Mode is on; otherwise every call is a no-op and the
runtime behaves exactly like in production. All state lives here so the
runner stays free of UI concerns.
"""

from __future__ import annotations

import multiprocessing
import threading
import time


class DebugEventHub:
    """Process-safe queue for block_started events from any runner."""

    _instance = None

    def __init__(self):
        self._queue = multiprocessing.Queue()

    @classmethod
    def instance(cls) -> "DebugEventHub":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def publish(self, event: dict):
        try:
            self._queue.put(event)
        except Exception:
            pass

    def poll(self) -> list:
        events = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except Exception:
                break
        return events


class DebugPacingService:
    """Process-wide pacing and pause state for debug mode."""

    _instance = None

    def __init__(self):
        self._lock = threading.Lock()
        self._enabled = False
        self._factor = 1.0
        self._paused = threading.Event()
        self._paused.set()  # set = running, cleared = paused

    @classmethod
    def instance(cls) -> "DebugPacingService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # -- control ---------------------------------------------------------------

    def set_enabled(self, enabled: bool):
        with self._lock:
            self._enabled = bool(enabled)
            if not enabled:
                self._factor = 1.0
                self._paused.set()

    def set_factor(self, factor: float):
        with self._lock:
            self._factor = max(1.0, float(factor))

    def pause(self):
        self._paused.clear()

    def resume(self):
        self._paused.set()

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @property
    def factor(self) -> float:
        with self._lock:
            return self._factor

    @property
    def paused(self) -> bool:
        return not self._paused.is_set()

    # -- runner hook -------------------------------------------------------------

    def before_block(self, is_cancelled) -> bool:
        """Wait out a pause and apply the pacing delay before a block.

        Returns False when the run was cancelled while waiting.
        """

        if not self.enabled:
            return True

        # pause: wait in small slices so cancellation still responds
        while not self._paused.is_set():
            if is_cancelled():
                return False
            time.sleep(0.05)

        factor = self.factor
        if factor <= 1.0:
            return not is_cancelled()

        deadline = time.monotonic() + min(0.4 * factor, 4.0)
        while True:
            if is_cancelled():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(0.05, remaining))
