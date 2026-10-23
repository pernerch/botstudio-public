"""Independent cycle and health timers, one pair per physical emulator."""

from threading import RLock

from services.runtime_heartbeat_service import RuntimeHeartbeatService


class EmulatorRuntimeWorkers:
    def __init__(self, cycle, health, interval_seconds, logger):
        self._cycle = cycle
        self._health = health
        self._interval = interval_seconds
        self._logger = logger
        self._lock = RLock()
        self._workers = {}

    def reconcile(self, instances):
        wanted = {str(instance) for instance in instances}
        with self._lock:
            for instance, workers in self._workers.items():
                if instance not in wanted:
                    for worker in workers:
                        worker.stop()
            for instance in wanted:
                workers = self._workers.get(instance)
                if workers is None:
                    workers = (
                        RuntimeHeartbeatService(
                            lambda key=instance: self._cycle(key),
                            self._interval, logger=self._logger,
                        ),
                        RuntimeHeartbeatService(
                            lambda key=instance: self._health(key),
                            2, logger=self._logger,
                        ),
                    )
                    self._workers[instance] = workers
                if all(worker.running or worker.wait_until_idle(0) for worker in workers):
                    for worker in workers:
                        worker.start(immediate=True)

    def stop(self):
        with self._lock:
            for workers in self._workers.values():
                for worker in workers:
                    worker.stop()

    def request_cycle(self, instance):
        with self._lock:
            workers = self._workers.get(str(instance))
            return workers[0].request_tick() if workers is not None else False

    def idle(self):
        with self._lock:
            return all(
                worker.wait_until_idle(0)
                for workers in self._workers.values()
                for worker in workers
            )
