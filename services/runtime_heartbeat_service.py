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
        self._state_lock = threading.Lock()
        self._idle_event = threading.Event()
        self._idle_event.set()

        self.logger = (
            logger
            or get_logger("runtime")
        )

    def start(
        self,
    ):

        if self.running:
            return

        self.running = True

        self.logger.info(
            "Heartbeat Start"
        )

        self._schedule()

    def stop(
        self,
    ):

        if not self.running:
            return

        with self._state_lock:
            self.running = False
            if self._timer:
                self._timer.cancel()
                self._timer = None

        self.logger.info(
            "Heartbeat Stop"
        )

    def wait_until_idle(self, timeout_seconds):
        return self._idle_event.wait(timeout_seconds)

    def _schedule(
        self,
    ):

        with self._state_lock:
            if not self.running:
                return
            self._next_scheduled_at = monotonic() + self.interval_seconds
            self._timer = threading.Timer(
                self.interval_seconds,
                self._tick,
            )
            self._timer.daemon = True
            self._timer.start()

    def _tick(
        self,
    ):

        with self._state_lock:
            if not self.running:
                return
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
            self._schedule()
            self._idle_event.set()
