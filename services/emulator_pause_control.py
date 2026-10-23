from contextlib import contextmanager
from threading import RLock


class EmulatorPauseControl:
    """Keep paused instances excluded until their old operations have drained."""

    def __init__(self):
        self._lock = RLock()
        self._known = set()
        self._states = {}
        self._active = {}
        self._generation = 0
        self._epochs = {}
        self.all_paused = False

    def register(self, instances):
        with self._lock:
            self._known = {str(instance) for instance in instances if instance is not None}
            if self.all_paused:
                for instance in self._known:
                    self._states.setdefault(instance, ("paused", self._generation, None))

    def pause(self, instance=None):
        with self._lock:
            self._require_known(instance)
            self._generation += 1
            if instance is None:
                self.all_paused = True
                targets = self._known | set(self._states)
            else:
                targets = {str(instance)}
            for target in targets:
                self._states[target] = ("paused", self._generation, None)
                self._epochs[target] = self._generation

    def resume(self, instance=None):
        with self._lock:
            self._require_known(instance)
            if self.all_paused:
                for target in self._known:
                    self._states.setdefault(target, ("paused", self._generation, None))
            self.all_paused = False
            targets = set(self._states) if instance is None else {str(instance)}
            for target in targets:
                if target in self._states:
                    self._generation += 1
                    self._states[target] = ("resuming", self._generation, None)
                    self._epochs[target] = self._generation

    def _require_known(self, instance):
        if instance is not None and str(instance) not in self._known | set(self._states):
            raise ValueError("Instance is not assigned to an enabled profile")

    def blocked(self, instance):
        with self._lock:
            return self.all_paused or str(instance) in self._states

    def epoch(self, instance):
        with self._lock:
            return self._epochs.get(str(instance), 0)

    @contextmanager
    def operation(self, instance):
        key = str(instance)
        with self._lock:
            admitted = not self.blocked(instance)
            if admitted:
                self._active[key] = self._active.get(key, 0) + 1
        try:
            yield admitted
        finally:
            if admitted:
                with self._lock:
                    self._active[key] -= 1
                    if not self._active[key]:
                        del self._active[key]

    def ready(self):
        with self._lock:
            return [
                (instance, generation)
                for instance, (state, generation, _error) in self._states.items()
                if state == "resuming" and not self._active.get(instance)
            ]

    def finish_resume(self, instance, generation, error=None):
        with self._lock:
            if self._states.get(instance, ())[:2] != ("resuming", generation):
                return
            if error is None:
                del self._states[instance]
            else:
                self._states[instance] = ("paused", generation, str(error))

    def snapshot(self):
        with self._lock:
            return {
                instance: {
                    "state": state,
                    "active_operations": self._active.get(instance, 0),
                    "error": error,
                }
                for instance, (state, _generation, error) in self._states.items()
            }
