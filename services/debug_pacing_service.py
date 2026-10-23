"""Global debug pacing: slows the runner and allows pause/resume.

Active only while Debug Mode is on; otherwise every call is a no-op and the
runtime behaves exactly like in production. All state lives here so the
runner stays free of UI concerns.
"""

from __future__ import annotations

import multiprocessing
import logging
from queue import Empty, Full
import threading
import time


class DebugEventHub:
    """Process-safe queue for block_started events from any runner."""

    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self):
        self._queue = multiprocessing.Queue(maxsize=256)
        self._overflow_reported = False

    @classmethod
    def instance(cls) -> "DebugEventHub":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
        return cls._instance

    def publish(self, event: dict):
        try:
            self._queue.put_nowait(event)
        except Full:
            if not self._overflow_reported:
                logging.getLogger(__name__).warning(
                    "Debug observation queue full; dropping observations, not blocking Runtime"
                )
                self._overflow_reported = True
            return False
        self._overflow_reported = False
        return True

    def poll(self) -> list:
        events = []
        for _ in range(128):
            try:
                events.append(self._queue.get_nowait())
            except Empty:
                break
        return events

    @classmethod
    def poll_existing(cls) -> list:
        hub = cls._instance
        return hub.poll() if hub is not None else []


class DebugPacingService:
    """Process-wide pacing and pause state for debug mode."""

    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self):
        self._lock = multiprocessing.RLock()
        self._enabled = multiprocessing.Value("b", False, lock=False)
        self._factor = multiprocessing.Value("d", 1.0, lock=False)
        self._profile_id = multiprocessing.Array("u", 256, lock=False)
        self._generation = multiprocessing.Value("L", 0, lock=False)
        self._paused = multiprocessing.Event()
        self._paused.set()  # set = running, cleared = paused

    @classmethod
    def instance(cls) -> "DebugPacingService":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
        return cls._instance

    # -- control ---------------------------------------------------------------

    def set_enabled(self, enabled: bool):
        with self._lock:
            if bool(self._enabled.value) != bool(enabled):
                self._generation.value += 1
            self._enabled.value = bool(enabled)
            if not enabled:
                self._factor.value = 1.0
                self._paused.set()

    def set_factor(self, factor: float):
        with self._lock:
            self._factor.value = max(1.0, float(factor))

    def select_profile(self, profile_id):
        value = str(profile_id or "")
        if len(value) >= len(self._profile_id):
            raise ValueError("Debug profile identity is too long")
        with self._lock:
            self._profile_id.value = value
            self._generation.value += 1
            self._paused.set()

    def observes(self, profile_id):
        return self.observation_generation(profile_id) is not None

    def observation_generation(self, profile_id):
        with self._lock:
            if (
                self._enabled.value and self._profile_id.value
                and self._profile_id.value == str(profile_id or "")
            ):
                return self._generation.value
            return None

    def pause(self):
        self._paused.clear()

    def resume(self):
        self._paused.set()

    @property
    def enabled(self) -> bool:
        with self._lock:
            return bool(self._enabled.value)

    @property
    def factor(self) -> float:
        with self._lock:
            return self._factor.value

    @property
    def paused(self) -> bool:
        return not self._paused.is_set()

    # -- runner hook -------------------------------------------------------------

    def before_block(self, is_cancelled, profile_id=None) -> bool:
        """Wait out a pause and apply the pacing delay before a block.

        Returns False when the run was cancelled while waiting.
        """

        generation = self.observation_generation(profile_id)
        if generation is None:
            return True

        # pause: wait in small slices so cancellation still responds
        while not self._paused.is_set():
            if self.observation_generation(profile_id) != generation:
                return not is_cancelled()
            if is_cancelled():
                return False
            time.sleep(0.05)

        if self.observation_generation(profile_id) != generation:
            return not is_cancelled()
        factor = self.factor
        if factor <= 1.0:
            return not is_cancelled()

        deadline = time.monotonic() + min(0.4 * factor, 4.0)
        while True:
            if self.observation_generation(profile_id) != generation:
                return not is_cancelled()
            if is_cancelled():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(0.05, remaining))
