from time import monotonic, sleep


class WatchdogEmulatorService:
    QUIESCE_TIMEOUT_SECONDS = 30
    TRANSITION_TIMEOUT_SECONDS = 60
    POLL_SECONDS = 1

    def __init__(self, runtime):
        self.runtime = runtime

    def handle(self, command, arguments):
        profiles = self.runtime.profile_service.load_enabled()
        instances = sorted({
            self._instance(profile.get("emulator_instance"))
            for profile in profiles
            if profile.get("emulator_instance") is not None
        })
        if command == "emulators.list":
            if arguments:
                raise ValueError("This command does not accept arguments")
            return {
                "emulators": [
                    {
                        "instance": instance,
                        "running": self.runtime.emulator_manager.is_instance_running(
                            instance
                        ),
                    }
                    for instance in instances[:20]
                ],
                "truncated": len(instances) > 20,
            }
        if command != "emulator.restart":
            raise ValueError("Unsupported emulator command")
        if set(arguments) != {"instance"}:
            raise ValueError("instance is required")
        instance = self._instance(arguments["instance"])
        if instance not in instances:
            raise ValueError("Instance is not assigned to an enabled profile")
        return self._restart(instance, profiles)

    @staticmethod
    def _instance(value):
        if type(value) is int and value >= 0:
            return value
        if isinstance(value, str) and value.isascii() and value.isdigit():
            return int(value)
        raise ValueError("Emulator instance must be a non-negative integer")

    def _restart(self, instance, profiles):
        runtime = self.runtime
        if not runtime._start_lock.acquire(blocking=False):
            raise RuntimeError("Runtime start or emulator maintenance is busy")
        acquired = []
        try:
            resume = runtime.running
            runtime.stop()
            stop_generation = runtime._stop_generation
            runtime.logger.info("Watchdog Emulator Restart Started", instance=instance)
            if not runtime.heartbeat.wait_until_idle(self.QUIESCE_TIMEOUT_SECONDS):
                raise TimeoutError("Runtime did not become idle; emulator unchanged")
            for profile in profiles:
                key = runtime._execution_resource_key(profile)
                if key in acquired:
                    continue
                if not runtime.try_acquire_profile_execution(key):
                    raise RuntimeError("Profile execution is busy; emulator unchanged")
                acquired.append(key)
            if not runtime.get_recovery_state()["safe_to_resume"]:
                raise RuntimeError(
                    "Interrupted tasks require review; emulator unchanged, Runtime stopped"
                )
            manager = runtime.emulator_manager
            if manager.is_instance_running(instance):
                if not manager.stop_instance(instance):
                    raise RuntimeError("Emulator shutdown request failed; Runtime stopped")
                self._wait_for(
                    lambda: not manager.is_instance_running(instance),
                    "Emulator did not stop; Runtime stopped",
                )
            if not manager.start_instance(instance):
                raise RuntimeError("Emulator launch request failed; Runtime stopped")
            device_id = manager.get_device_id(instance)
            self._wait_for(
                lambda: manager.is_instance_running(instance)
                and runtime.adb_runtime_service.ensure_device(device_id),
                "Emulator did not become ADB-ready; Runtime stopped",
            )
            for key in acquired:
                runtime.release_profile_execution(key)
            acquired.clear()
            resumed = False
            if resume and runtime._stop_generation == stop_generation:
                try:
                    runtime._start(expected_stop_generation=stop_generation)
                except Exception:
                    runtime.stop()
                    raise
                resumed = runtime.running
            runtime.logger.info(
                "Watchdog Emulator Restart Completed",
                instance=instance,
                runtime_resumed=resumed,
            )
            return {
                "state": "completed",
                "instance": instance,
                "runtime_resumed": resumed,
            }
        finally:
            for key in acquired:
                runtime.release_profile_execution(key)
            runtime._start_lock.release()

    def _wait_for(self, predicate, error):
        deadline = monotonic() + self.TRANSITION_TIMEOUT_SECONDS
        while monotonic() < deadline:
            if predicate():
                return
            sleep(self.POLL_SECONDS)
        raise TimeoutError(error)
