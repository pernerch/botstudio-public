from contextlib import contextmanager
from datetime import datetime, timezone
from threading import RLock


class ExecutionActivityService:
    """In-flight execution only; entries disappear even when a runner raises."""

    def __init__(self):
        self._lock = RLock()
        self._active = {}

    @contextmanager
    def track(self, function, context, profile):
        profile = profile or {}
        entry = {
            "execution_id": context.execution_id,
            "parent_id": context.parent_id,
            "game_id": context.game_id or profile.get("game"),
            "profile_id": context.profile_id,
            "profile_name": profile.get("name") or context.profile_id,
            "emulator_instance": profile.get("emulator_instance"),
            "device_id": profile.get("device_id"),
            "function_id": function.function_id,
            "function_name": function.name or function.function_id,
            "event_id": context.event_id,
            "task_id": context.task_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            self._active[context.execution_id] = entry
        try:
            yield
        finally:
            with self._lock:
                self._active.pop(context.execution_id, None)

    def snapshot(self):
        with self._lock:
            return [dict(entry) for entry in self._active.values()]


_studio_activity = ExecutionActivityService()


def get_execution_activity():
    return _studio_activity
