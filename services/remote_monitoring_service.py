import base64
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from time import monotonic
from uuid import uuid4

import cv2

from core.emulator import Emulator
from core.command_control import CommandCancelled, command_control


class RemoteMonitoringService:
    PAGE_SIZE = 6
    MAX_PHOTO_BYTES = 4 * 1024 * 1024
    PHOTO_CHUNK_BYTES = 32 * 1024
    PHOTO_RETENTION_SECONDS = 60
    MAX_PHOTOS = 8
    INFO_TIMEOUT_SECONDS = 8

    def __init__(self, runtime_service):
        self.runtime = runtime_service
        self._photos = {}
        self._lock = RLock()

    @staticmethod
    def _page(arguments, allowed):
        if set(arguments) - allowed:
            raise ValueError("Unsupported monitoring arguments")
        page = arguments.get("page", 1)
        if type(page) is not int or page < 1:
            raise ValueError("page must be a positive integer")
        return page

    def handle(self, command, arguments):
        if command == "statistics.get":
            return self.statistics(arguments)
        if command == "info.get":
            return self.info(arguments)
        if command == "info.photo.get":
            return self.photo_chunk(arguments)
        raise ValueError("Unsupported monitoring command")

    def statistics(self, arguments):
        page = self._page(arguments, {"page", "all_time"})
        all_time = arguments.get("all_time", False)
        if type(all_time) is not bool:
            raise ValueError("all_time must be a boolean")
        accounts = sorted(
            self.runtime.statistics_service.snapshot(all_time=all_time).values(),
            key=lambda item: (item["game_id"], item["profile_name"], item["profile_id"]),
        )
        pages = max(1, (len(accounts) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        if page > pages:
            raise ValueError(f"Page must be between 1 and {pages}")
        entries = []
        for account in accounts[(page - 1) * self.PAGE_SIZE:page * self.PAGE_SIZE]:
            tasks = account["task_executions"].values()
            entries.append({
                "game_id": account["game_id"],
                "profile_name": account["profile_name"],
                "success": sum(task["success"] for task in tasks),
                "failure": sum(task["failure"] for task in tasks),
                "uptime_seconds": round(account.get("uptime_seconds", 0)),
                "profile_switches": account.get("profile_switches", 0),
                "emulator_starts": account.get("emulator_starts", 0),
            })
        return {"page": page, "pages": pages, "all_time": all_time, "accounts": entries}

    def info(self, arguments):
        deadline = monotonic() + self.INFO_TIMEOUT_SECONDS
        try:
            with command_control(lambda: monotonic() >= deadline):
                return self._info(arguments)
        except CommandCancelled as error:
            self.runtime.logger.warning("Remote Live Info Timed Out")
            raise TimeoutError("Live emulator query timed out. Request /info again.") from error

    def _info(self, arguments):
        page = self._page(arguments, {"page"})
        profiles = self.runtime.profile_service.load_all()
        profiles = [profile for profile in profiles if profile.get("emulator_instance") is not None]
        instances = sorted({int(profile["emulator_instance"]) for profile in profiles})
        pages = max(1, len(instances))
        if page > pages:
            raise ValueError(f"Page must be between 1 and {pages}")
        result = {
            "page": page, "pages": pages,
            "runtime": self.runtime.get_status()["status"],
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "instance": None, "state": "Unknown", "activity": [],
            "photo": None, "message": "No configured emulator instances.",
        }
        if not instances:
            return result
        instance = instances[page - 1]
        result["instance"] = instance
        activity = self.runtime.execution_activity.snapshot()
        activity = [entry for entry in activity if str(entry["emulator_instance"]) == str(instance)]
        result["activity"] = activity
        candidates = [profile for profile in profiles if int(profile["emulator_instance"]) == instance]
        profile = candidates[0]
        if activity:
            entry = activity[-1]
            profile = next(
                (item for item in candidates if self.runtime._profile_id(item) == entry["profile_id"]),
                profile,
            )
        else:
            active = getattr(self.runtime, "_active_profiles", {})
            profile = next(
                (item for item in candidates if active.get((item.get("game"), instance)) == self.runtime._profile_id(item)),
                profile,
            )
        result["profile_name"] = profile.get("name") or self.runtime._profile_id(profile)
        result["game_id"] = profile.get("game")
        if not self.runtime.emulator_manager.is_instance_running(instance):
            result.update(state="Stopped", message="Emulator is not running. No screenshot available.")
            return result
        device = self.runtime.emulator_manager.get_device_id(instance)
        result["device_id"] = device
        if not device or not self.runtime.adb_runtime_service.adb.is_responsive(device, timeout=2):
            result.update(state="Failed", message="Emulator is not responding. No screenshot available.")
            return result
        package = self.runtime.game_runtime_service.get_package_name(profile)
        game_running = bool(package) and self.runtime.game_runtime_service.adb.get_package_pid(
            device, package, timeout=2,
        ) is not None
        if game_running:
            game_running = not self.runtime.game_runtime_service.adb.has_anr(device, package, timeout=2)
        result.update(
            state="Healthy" if game_running else "Game unavailable",
            message="Live emulator capture." if game_running else "Game is not running or not responding.",
        )
        try:
            emulator = Emulator(self.runtime.emulator_manager.adb_path, device, command_timeout=2)
            file_name = emulator.screenshot(attempts=1)
            if not file_name:
                raise RuntimeError("Emulator screenshot failed")
            path = Path(file_name)
            try:
                image = cv2.imread(str(path))
                if image is None:
                    raise RuntimeError("Emulator screenshot cannot be decoded")
                height, width = image.shape[:2]
                if max(height, width) > 1280:
                    scale = 1280 / max(height, width)
                    image = cv2.resize(image, (round(width * scale), round(height * scale)))
                ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if not ok:
                    raise RuntimeError("Emulator screenshot cannot be encoded")
                content = encoded.tobytes()
                if not 0 < len(content) <= self.MAX_PHOTO_BYTES:
                    raise RuntimeError("Emulator screenshot exceeds the transfer limit")
            finally:
                path.unlink(missing_ok=True)
            with self._lock:
                self._prune_photos()
                while len(self._photos) >= self.MAX_PHOTOS:
                    del self._photos[next(iter(self._photos))]
                token = uuid4().hex
                self._photos[token] = (monotonic(), content)
            result["photo"] = {
                "token": token, "bytes": len(content),
                "chunks": (len(content) + self.PHOTO_CHUNK_BYTES - 1) // self.PHOTO_CHUNK_BYTES,
            }
            result["captured_at"] = datetime.now(timezone.utc).isoformat()
            # Re-read activity after capture; a completed Function must not be presented as running.
            result["activity"] = [
                entry for entry in self.runtime.execution_activity.snapshot()
                if str(entry["emulator_instance"]) == str(instance)
            ]
            if result["activity"]:
                result["profile_name"] = result["activity"][-1]["profile_name"]
                result["game_id"] = result["activity"][-1]["game_id"]
        except (OSError, RuntimeError, cv2.error) as error:
            if isinstance(error, CommandCancelled):
                raise
            self.runtime.logger.error(
                "Remote Screenshot Failed", emulator_instance=instance, error_type=type(error).__name__,
            )
            result.update(state="Unknown", message=f"Screenshot unavailable: {error}")
        return result

    def _prune_photos(self):
        cutoff = monotonic() - self.PHOTO_RETENTION_SECONDS
        self._photos = {token: value for token, value in self._photos.items() if value[0] > cutoff}

    def photo_chunk(self, arguments):
        if set(arguments) != {"token", "index"}:
            raise ValueError("token and index are required")
        token, index = arguments["token"], arguments["index"]
        if not isinstance(token, str) or type(index) is not int or index < 0:
            raise ValueError("Invalid screenshot chunk reference")
        with self._lock:
            self._prune_photos()
            if token not in self._photos:
                raise ValueError("Screenshot expired. Request /info again.")
            content = self._photos[token][1]
            start = index * self.PHOTO_CHUNK_BYTES
            if start >= len(content):
                raise ValueError("Screenshot chunk is out of range")
            return {"content": base64.b64encode(content[start:start + self.PHOTO_CHUNK_BYTES]).decode("ascii")}
