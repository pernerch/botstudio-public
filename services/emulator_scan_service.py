from core.emulator_manager import EmulatorManager
from time import monotonic
from services.adb_runtime_service import AdbRuntimeService
from services.game_service import GameService
from services.logging_service import get_logger
from services.profile_service import ProfileService
from services.runtime_heartbeat_service import RuntimeHeartbeatService
from services.emulator_health_service import EmulatorHealthService


class EmulatorScanService:
    """Discovers running emulator instances independent of any tracked profile (DEC-091).

    Session adoption for a *known* profile already happens in
    EmulatorHealthService/GameRuntimeService: both check the current state
    (is_instance_running, foreground package) before ever restarting anything,
    so a healthy session is never torn down and relaunched.

    This service covers the complementary, profile-less case: which emulator
    instances are running right now, what app is in their foreground, and
    (only when unambiguous) which profile that corresponds to. It is meant to
    run continuously in the background, independent of whether the Runtime is
    started, so the Game Overview screen can show real instances instead of
    nothing.
    """

    def __init__(
        self,
        emulator_manager=None,
        adb_runtime_service=None,
        game_service=None,
        profile_service=None,
        logger=None,
    ):
        self.emulator_manager = emulator_manager or EmulatorManager()
        self.adb_runtime_service = adb_runtime_service or AdbRuntimeService(
            lambda: self.emulator_manager.adb_path
        )
        self.game_service = game_service or GameService()
        self.profile_service = profile_service or ProfileService()
        self.logger = logger or get_logger("runtime")
        self.health_service = EmulatorHealthService(self.emulator_manager, self.adb_runtime_service)

        self.last_scan: list[dict] = []
        self._heartbeat: RuntimeHeartbeatService | None = None
        self._should_scan = lambda: True

    def start(self, interval_seconds: int = 15, should_scan=None) -> None:
        """Runs continuously in the background; should_scan lets callers pause
        it (e.g. while the Runtime already tracks these instances itself)."""

        self._should_scan = should_scan or (lambda: True)

        if self._heartbeat is None:
            self._heartbeat = RuntimeHeartbeatService(
                self._tick,
                interval_seconds,
            )

        self._heartbeat.start()

    def stop(self) -> None:
        if self._heartbeat:
            self._heartbeat.stop()

    def _tick(self) -> None:
        if not self._should_scan():
            return
        self.scan()

    def known_instances(self) -> list[int]:
        """Every instance referenced by any profile is worth watching."""

        instances = set()

        for profile in self.profile_service.load_all():
            instance = profile.get("emulator_instance")
            if instance is not None:
                instances.add(int(instance))

        return sorted(instances)

    def scan(self, instances: list[int] | None = None) -> list[dict]:

        games_by_package = {
            game.get("package_name"): game.get("id")
            for game in self.game_service.load()
            if game.get("package_name")
        }

        targets = (
            instances
            if instances is not None
            else self.known_instances()
        )

        results = [
            self._scan_instance(instance, games_by_package)
            for instance in targets
        ]

        self.last_scan = results
        return results

    def _scan_instance(self, instance: int, games_by_package: dict) -> dict:

        entry = {
            "instance": instance,
            "running": False,
            "pid": None,
            "device_id": None,
            "foreground_package": None,
            "game_id": None,
            "profile_id": None,
            "profile_name": None,
            "state": "Idle",
            "scanned_at": monotonic(),
        }

        if not self.emulator_manager.is_instance_running(instance):
            return entry

        entry["running"] = True
        entry["pid"] = self.emulator_manager.get_instance_pid(instance)

        device_id = self.emulator_manager.get_device_id(instance)
        entry["device_id"] = device_id

        if not self.adb_runtime_service.ensure_device(device_id):
            entry["state"] = "Failed"
            return entry

        if not self.adb_runtime_service.adb.is_responsive(device_id, timeout=2):
            entry["state"] = "Failed"
            return entry
        if not self.health_service.probe_screenshot(device_id):
            entry["state"] = "Failed"
            return entry

        package = self.adb_runtime_service.adb.get_foreground_package(device_id)
        entry["foreground_package"] = package
        entry["game_id"] = games_by_package.get(package)

        if entry["game_id"]:
            if (
                not self.adb_runtime_service.adb.get_package_pid(device_id, package, timeout=2)
                or self.adb_runtime_service.adb.has_anr(device_id, package, timeout=2)
            ):
                entry["state"] = "Failed"
                entry["foreground_package"] = None
                entry["game_id"] = None
                return entry
            entry["state"] = "Healthy"
            profiles = self._matching_profiles(instance, entry["game_id"])
            if len(profiles) == 1:
                profile = profiles[0]
                entry["profile_id"] = profile.get(
                    "botstudio_profile_id", profile.get("id", "")
                )
                entry["profile_name"] = profile.get("name", "")
            elif profiles:
                names = sorted(
                    (
                        profile.get("name")
                        or profile.get("botstudio_profile_id")
                        or profile.get("id", "")
                    )
                    for profile in profiles
                )
                entry["profile_name"] = f"MultiAccount: {' | '.join(names)}"

        self.logger.debug(
            "Emulator Scan",
            instance=instance,
            device_id=device_id,
            foreground_package=package,
            game=entry["game_id"],
            profile=entry["profile_name"],
        )

        return entry

    def _matching_profiles(self, instance: int, game_id: str) -> list[dict]:
        return [
            profile
            for profile in self.profile_service.load_enabled()
            if profile.get("emulator_instance") == instance
            and profile.get("game") == game_id
        ]
