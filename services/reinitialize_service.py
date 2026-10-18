from collections.abc import Callable
from contextlib import contextmanager
from threading import local


_NOTIFICATION_SCOPE = local()


@contextmanager
def defer_notifications():
    """Collect this worker's persistence notifications for replay on the GUI thread."""
    if getattr(_NOTIFICATION_SCOPE, "events", None) is not None:
        raise RuntimeError("persistence notification deferral cannot be nested")
    events = []
    _NOTIFICATION_SCOPE.events = events
    try:
        yield events
    finally:
        del _NOTIFICATION_SCOPE.events


class ReinitializeService:
    """Publishes successful persistence changes to interested application services."""

    def __init__(self) -> None:
        self._listeners: list[Callable[[str, dict], None]] = []

    def subscribe(self, listener: Callable[[str, dict], None]) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def unsubscribe(self, listener: Callable[[str, dict], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def publish(self, source: str, **payload) -> None:
        events = getattr(_NOTIFICATION_SCOPE, "events", None)
        if events is not None:
            events.append((self, source, dict(payload)))
            return
        for listener in tuple(self._listeners):
            listener(source, dict(payload))


_DEFAULT_REINITIALIZE_SERVICE = ReinitializeService()


def get_default_reinitialize_service() -> ReinitializeService:
    return _DEFAULT_REINITIALIZE_SERVICE