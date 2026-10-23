import threading
from time import monotonic

from services.logging_service import get_logger


class RuntimeHeartbeatService:

    def __init__(
        self,
        callback,
        interval_seconds: int = 10,
        logger=None,
    ):

        self.callback = callback
        self.interval_seconds = interval_seconds
        self.running = False
        self._timer = None
        self._next_scheduled_at = None
        self.last_start_delay_seconds = 0.0
        self.last_duration_seconds = 0.0
        self._state_lock = threading.RLock()
        self._idle_event = threading.Event()
        self._idle_event.set()
        self._generation = 0
        self._schedule_id = 0
        self._rerun_requested = False

        self.logger = (
            logger
            or get_logger("runtime")
        )

    def start(
        self,
        *,
        immediate=False,
    ):

        with self._state_lock:
            if self.running:
                return
            if not self._idle_event.is_set():
                raise RuntimeError("Previous heartbeat callback is still stopping")
            self._generation += 1
            generation = self._generation
            self.running = True
            self._rerun_requested = False

        self.logger.info(
            "Heartbeat Start"
        )

        self._schedule(
            delay_seconds=0 if immediate else None, generation=generation,
        )

    def stop(
        self,
    ):

        if not self.running:
            return

        with self._state_lock:
            self.running = False
            self._generation += 1
            self._rerun_requested = False
            if self._timer:
                self._timer.cancel()
                self._timer = None

        self.logger.info(
            "Heartbeat Stop"
        )

    def wait_until_idle(self, timeout_seconds):
        return self._idle_event.wait(timeout_seconds)

    def request_tick(self):
        with self._state_lock:
            if not self.running:
                return False
            if not self._idle_event.is_set():
                self._rerun_requested = True
            else:
                if self._timer:
                    self._timer.cancel()
                self._schedule(delay_seconds=0, generation=self._generation)
            return True

    def _schedule(
        self,
        delay_seconds=None,
        generation=None,
    ):

        with self._state_lock:
            if not self.running or (
                generation is not None and generation != self._generation
            ):
                return
            delay = self.interval_seconds if delay_seconds is None else delay_seconds
            self._schedule_id += 1
            self._next_scheduled_at = monotonic() + delay
            self._timer = threading.Timer(
                delay,
                self._tick,
                args=(self._generation, self._schedule_id),
            )
            self._timer.daemon = True
            self._timer.start()

    def _tick(
        self,
        generation=None,
        schedule_id=None,
    ):

        with self._state_lock:
            if not self.running or (
                generation is not None and generation != self._generation
            ) or (
                schedule_id is not None and schedule_id != self._schedule_id
            ) or not self._idle_event.is_set():
                return
            generation = self._generation
            self._idle_event.clear()

        started_at = monotonic()
        self.last_start_delay_seconds = max(
            0.0,
            started_at - self._next_scheduled_at,
        ) if self._next_scheduled_at is not None else 0.0
        try:
            self.callback()

        except Exception as ex:

            self.logger.error(
                "Heartbeat Error",
                error=str(ex),
            )

        finally:
            self.last_duration_seconds = max(0.0, monotonic() - started_at)
            if (
                self.last_start_delay_seconds >= 1.0
                or self.last_duration_seconds > self.interval_seconds
            ):
                self.logger.warning(
                    "Heartbeat Delayed",
                    interval_seconds=self.interval_seconds,
                    start_delay_seconds=round(self.last_start_delay_seconds, 3),
                    duration_seconds=round(self.last_duration_seconds, 3),
                )
            with self._state_lock:
                self._idle_event.set()
                delay = 0 if self._rerun_requested else None
                self._rerun_requested = False
                self._schedule(delay_seconds=delay, generation=generation)
