from services.profile_service import ProfileService
from services.profile_worker import ProfileWorker
from pathlib import Path


class RuntimeSupervisor:
    """Supervises isolated function workers, one per enabled profile."""

    def __init__(
        self,
        profile_service=None,
        function_service=None,
        worker_factory=ProfileWorker,
    ):

        self.profile_service = profile_service or ProfileService()
        self.function_service = function_service
        self.worker_factory = worker_factory
        self.workers = {}

    @staticmethod
    def _execution_resource_key(profile):
        game_id = str(profile.get("game") or "")
        emulator_instance = profile.get("emulator_instance")
        profile_id = str(
            profile.get("botstudio_profile_id")
            or profile.get("id")
            or ""
        )
        if emulator_instance is None:
            return f"{game_id}:{profile_id}"
        return f"{game_id}:emulator:{emulator_instance}"

    def _resource_is_busy(self, profile):
        resource_key = self._execution_resource_key(profile)
        for other_profile_id, worker in self.workers.items():
            if other_profile_id == self._profile_id(profile):
                continue
            if not worker.process or not worker.process.is_alive():
                continue
            if self._execution_resource_key(worker.profile) == resource_key:
                return True
        return False

    @staticmethod
    def _profile_id(profile):
        return profile.get("botstudio_profile_id", profile.get("id", ""))

    def start_function(
        self,
        profile_id: str,
        function_id: str,
        block_id: str | None = None,
        allow_disabled: bool = False,
        debug_values: dict | None = None,
    ) -> bool:

        if Path("data/package-activation/pending.json").exists():
            raise RuntimeError("Package activation recovery is pending; restart Studio before debugging")
        profile = self.profile_service.get_profile(
            profile_id
        )

        if not profile:
            return False

        if not allow_disabled and not profile.get("enabled", False):
            return False

        if self._resource_is_busy(profile):
            return False

        worker = self.workers.get(profile_id)

        if worker is None:
            worker = self.worker_factory(
                profile,
                self.function_service,
            )
            self.workers[profile_id] = worker

        return worker.start(
            function_id,
            block_id,
            debug_values=debug_values,
        )

    def stop_profile(
        self,
        profile_id: str,
    ) -> None:

        worker = self.workers.get(profile_id)

        if worker:
            worker.stop()

    def stop_all(
        self,
    ) -> None:

        for worker in self.workers.values():
            worker.stop()

    def poll_events(
        self,
    ) -> list:

        events = []

        for worker in self.workers.values():
            events.extend(worker.poll_events())

        return events